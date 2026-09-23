"""Assistant runtime: bounded suggestions, local feedback, and MI chat."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from tempo.assistant_chat import (
    ChatSendRequest, ChatSession, ChatSessions, EntityId, prepare_turn, response_messages,
    strategy_messages, validate_reply,
)
from tempo.assistant_context import RefreshGate, _available, _node, _utc, _visible, build_context
from tempo.assistant_inputs import SuggestionHistoryItem, personal_context, recent_suggestions
from tempo.db import Database
from tempo.models import Entity, Relation
from tempo.prompts.assistant_chat import STRATEGIES
from tempo.prompts.contextual_assistant import SYSTEM_PROMPT


class AssistantError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Suggestion(StrictModel):
    action: str = Field(min_length=1, max_length=600)
    why: str = Field(min_length=1, max_length=1000)
    goal_ids: list[EntityId] = Field(min_length=1, max_length=3)
    evidence_ids: list[EntityId] = Field(min_length=1, max_length=6)
    edge_ids: list[EntityId] = Field(default_factory=list, max_length=8)
    confidence: int = Field(strict=True, ge=1, le=10)
    valid_for_minutes: int = Field(strict=True, ge=1, le=30)

    @model_validator(mode="after")
    def distinct_references(self):
        if any(len(values) != len(set(values)) for values in (self.goal_ids, self.evidence_ids, self.edge_ids)):
            raise ValueError("Suggestion references must be distinct")
        return self


class Tradeoff(StrictModel):
    goal_ids: list[EntityId] = Field(min_length=2, max_length=3)
    description: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[EntityId] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def distinct_references(self):
        if len(set(self.goal_ids)) != len(self.goal_ids) or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Tradeoff references must be distinct")
        return self


class SuggestionResult(StrictModel):
    summary: str = Field(max_length=1200)
    options: list[Suggestion] = Field(max_length=3)
    tradeoffs: list[Tradeoff] = Field(default_factory=list, max_length=3)


class AssistantSettingsRequest(StrictModel):
    enabled: bool
    connection_id: str | None = None


class AssistantRefineRequest(StrictModel):
    context_id: str | None = None
    goal_ids: list[EntityId] | None = Field(default=None, min_length=1, max_length=3)
    situation: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def unique_goals(self):
        if self.goal_ids and (len(set(self.goal_ids)) != len(self.goal_ids) or min(self.goal_ids) <= 0):
            raise ValueError("Choose distinct available goals")
        return self


class FeedbackRequest(StrictModel):
    suggestion_id: str = Field(min_length=1, max_length=100)
    status: Literal["dismissed", "completed", "discussed"]


class AssistantState(StrictModel):
    enabled: bool = False
    connection_id: str | None = None
    goal_ids: list[int] | None = None
    user_note: str = ""
    feedback: list[SuggestionHistoryItem] = Field(default_factory=list)


def _save_state(path: Path, state: AssistantState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(state.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _load_state(path: Path) -> AssistantState:
    try:
        return AssistantState.model_validate_json(path.read_text())
    except FileNotFoundError:
        return AssistantState()


def suggestion_id(action: str, goal_ids: list[int]) -> str:
    key = json.dumps([" ".join(action.casefold().split()), sorted(set(goal_ids))])
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def validate_suggestions(raw: str, context: dict, now: datetime) -> dict:
    draft = SuggestionResult.model_validate_json(raw)
    nodes = {node["id"]: node for node in context["nodes"]}
    goals = set(context["goal_ids"])
    recent = set(context["recent_ids"])
    edges = {edge["id"] for edge in context["edges"]}
    suppressed = {item["suggestion_id"] for item in context["suggestion_history"] if item["status"] in {"dismissed", "completed"}}
    options = []
    seen = set()
    for option in draft.options:
        if (not set(option.goal_ids) <= goals or not set(option.evidence_ids) <= nodes.keys()
                or not set(option.edge_ids) <= edges):
            raise ValueError("Suggestion cites unavailable evidence")
        if context["focus_goal_ids"] and not set(option.goal_ids).intersection(context["focus_goal_ids"]):
            raise ValueError("Suggestion ignores the chosen goals")
        current = [nodes[eid] for eid in option.evidence_ids if eid in recent and nodes[eid]["type"] != "goal"]
        identifier = suggestion_id(option.action, option.goal_ids)
        if option.confidence < 8 or not current or identifier in suppressed or identifier in seen:
            continue
        # The suggestion cannot outlive both its own TTL and its newest timely evidence.
        evidence_expiry = max(_utc(datetime.fromisoformat(node["observed_at"])) + (
            timedelta(minutes=context["windows"]["operations_minutes"]) if node["type"] == "operation"
            else timedelta(hours=context["windows"]["session_hours"])
        ) for node in current)
        expires = min(now + timedelta(minutes=option.valid_for_minutes), evidence_expiry)
        if expires <= now:
            continue
        seen.add(identifier)
        options.append({**option.model_dump(exclude={"valid_for_minutes"}), "id": identifier, "expires_at": expires.isoformat()})
    for tradeoff in draft.tradeoffs:
        if not set(tradeoff.goal_ids) <= goals or not set(tradeoff.evidence_ids) <= nodes.keys():
            raise ValueError("Tradeoff cites unavailable evidence")
    # Do not retain an unfiltered summary of recommendations that failed the gates.
    retained_goals = {gid for option in options for gid in option["goal_ids"]}
    tradeoffs = [item.model_dump() for item in draft.tradeoffs if set(item.goal_ids) <= retained_goals]
    return {
        "summary": draft.summary if len(options) == len(draft.options) else "Only suggestions with current evidence and confidence of at least 8/10 are shown.",
        "options": options, "tradeoffs": tradeoffs, "generated_at": now.isoformat(),
        "context_id": context["context_id"],
        "goals": [node for node in nodes.values() if node["type"] == "goal"],
        "evidence": [{"id": node["id"], "type": node["type"], "text": node["text"],
                      "timestamp_start": node["observed_at"], "timestamp_end": None,
                      "goal_ids": []} for node in nodes.values() if node["type"] in {"operation", "action", "activity"}],
    }


class AssistantService:
    """One runtime per served database. No model calls until explicitly enabled."""

    def __init__(self, db: Database, provider_factory: Callable, connection: Callable,
                 *, clock: Callable = lambda: datetime.now(timezone.utc)):
        self.db, self.provider_factory, self.connection, self.clock = db, provider_factory, connection, clock
        self.path = Path(db.data_directory) / "assistant" / "state.json"
        self.sessions = ChatSessions(db.data_directory)
        self.state = AssistantState()
        self.gate = RefreshGate()
        self.context: dict | None = None
        self.cached: dict | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self._session_locks: dict[UUID, asyncio.Lock] = {}
        self._chat_tasks: set[asyncio.Task] = set()
        self._model_slots = asyncio.Semaphore(2)
        self._job: asyncio.Task | None = None
        self._retired_tasks: set[asyncio.Task] = set()
        self._worker: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._epoch = 0
        self._closed = False

    async def start(self, *, background: bool = True):
        self.state = await asyncio.to_thread(_load_state, self.path)
        await self.db.connect()
        if background:
            self._worker = asyncio.create_task(self._run(), name="assistant-context")

    def enabled(self) -> bool:
        connection = self.connection()
        return not self._closed and self.state.enabled and connection["configured"] and self.state.connection_id == connection["id"]

    def _check_enabled(self):
        if not self.enabled():
            raise AssistantError("Enable the assistant for the current model in the Assistant view.", 409)

    async def _persist(self):
        await asyncio.to_thread(_save_state, self.path, self.state.model_copy(deep=True))

    async def _snapshot(self) -> dict:
        if self.state.goal_ids:
            async with self.db.session() as session:
                available = set(await session.scalars(select(Entity.id).where(
                    Entity.id.in_(self.state.goal_ids), Entity.type == "goal", _available(),
                )))
            if available != set(self.state.goal_ids):
                self.state.goal_ids = [eid for eid in self.state.goal_ids if eid in available]
                self.error = "A chosen goal was removed. Choose another goal or reset your focus."
                await self._persist()
        return await build_context(self.db, now=self.clock(), focus_goal_ids=self.state.goal_ids or None,
                                   user_note=self.state.user_note, suggestion_history=self.state.feedback)

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            try:
                if self.state.enabled:
                    await self.tick()
            except Exception:
                self.error = "Could not refresh assistant context. Retry from the Assistant view."
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except TimeoutError:
                pass

    async def tick(self, *, explicit: bool = False):
        async with self._lock:
            first_context = self.context is None
            self.context = await self._snapshot()
            self.gate.observe(self.context["context_id"], at=time.monotonic())
            if not self.enabled() or self.state.goal_ids == []:
                return
            if not self.context["recent_ids"] or not self.context["goal_ids"]:
                self.cached = None
                return
            if self.cached and any(datetime.fromisoformat(item["expires_at"]) <= self.clock() for item in self.cached["options"]):
                self.gate.cached = None
                self.gate.observe(self.context["context_id"], at=time.monotonic())
            if self._job is not None and not self._job.done():
                return
            identifier = self.gate.begin(at=time.monotonic(), explicit=explicit or first_context)
            if identifier:
                self.error = None
                self._job = asyncio.create_task(self._generate(self.context, self._epoch), name="assistant-suggestions")

    async def _completion(self, messages: list[dict], *, structured: bool = False) -> str:
        self._check_enabled()
        async with self._model_slots:
            self._check_enabled()
            provider = self.provider_factory()
            if provider is None:
                raise AssistantError("Configure an AI model in Record settings first.", 503)
            kwargs = {"response_format": {"type": "json_object"}} if structured else {}
            return await asyncio.wait_for(provider.chat_completion(messages, **kwargs), timeout=90)

    async def _generate(self, context: dict, epoch: int):
        success = False
        try:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps({
                "context": context, "output_schema": SuggestionResult.model_json_schema(),
                "confidence_scale": "1–10 evidence strength, not probability. Show only 8–10. Return none when uncertain.",
                "freshness": "valid_for_minutes is 1–30; choose a short lifetime for transient next steps.",
            }, ensure_ascii=False)}]
            result = validate_suggestions(await self._completion(messages, structured=True), context, self.clock())
            async with self._lock:
                current = await self._snapshot()
                self.context = current
                self.gate.observe(current["context_id"], at=time.monotonic())
                if epoch == self._epoch and self.enabled() and current["context_id"] == context["context_id"]:
                    self.cached = result
                    success = True
                    self.error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            if epoch == self._epoch:
                self.error = "Could not generate suggestions. Check your model settings or retry."
        finally:
            if epoch == self._epoch:
                self.gate.finish(context["context_id"], success=success)

    async def feed(self) -> dict:
        await self.tick()
        async with self._lock:
            context = self.context
            connection = self.connection()
            advice = None
            if self.enabled() and self.cached and self.cached["context_id"] == context["context_id"]:
                options = [item for item in self.cached["options"] if datetime.fromisoformat(item["expires_at"]) > self.clock()]
                retained = {gid for option in options for gid in option["goal_ids"]}
                advice = {**self.cached, "options": options,
                          "summary": self.cached["summary"] if len(options) == len(self.cached["options"]) else "Some suggestions expired as their context aged.",
                          "tradeoffs": [item for item in self.cached["tradeoffs"] if set(item["goal_ids"]) <= retained]}
            nodes = {node["id"]: node for node in context["nodes"]}
            recent = [nodes[eid] for eid in context["recent_ids"]]
            lenses = {}
            for option in (advice or {}).get("options", []):
                for gid in option["goal_ids"]:
                    lenses.setdefault(gid, {"goal_id": gid, "why": option["why"], "evidence_ids": option["evidence_ids"]})
            preparing = self.enabled() and self.state.goal_ids != [] and bool(context["recent_ids"] and context["goal_ids"]) and (
                self.gate.pending_since is not None or (self._job is not None and not self._job.done()))
            return {"enabled": self.enabled(), "connection": connection, "busy": preparing,
                    "error": self.error, "focus_goal_ids": self.state.goal_ids, "user_note": self.state.user_note,
                    "context": {"id": context["context_id"], "summary": recent[0]["text"] if recent else "No recent observations. You can still discuss your goals in chat.",
                                "observed_at": context["last_observed_at"], "evidence_ids": context["recent_ids"]},
                    "goals": [nodes[eid] for eid in context["goal_ids"]], "lenses": list(lenses.values())[:3], "advice": advice}

    def _invalidate(self):
        self._epoch += 1
        if self._job and not self._job.done():
            self._job.cancel()
            self._retired_tasks.add(self._job)
            self._job.add_done_callback(self._retired_tasks.discard)
        self._job = None
        self.cached = None
        self.gate = RefreshGate()

    async def settings(self, request: AssistantSettingsRequest) -> dict:
        async with self._lock:
            connection = self.connection()
            if request.enabled and (not connection["configured"] or request.connection_id != connection["id"]):
                raise AssistantError("Model configuration changed. Review the current destination and enable again.", 409)
            self._invalidate()
            self.state.enabled = request.enabled
            self.state.connection_id = connection["id"] if request.enabled else None
            self.error = None
            await self._persist()
        self._wake.set()
        if request.enabled:
            await self.tick(explicit=True)
        return await self.feed()

    async def refine(self, request: AssistantRefineRequest) -> dict:
        async with self._lock:
            self._check_enabled()
            current = await self._snapshot()
            if request.context_id and current["context_id"] != request.context_id:
                raise AssistantError("Your context changed. Refresh and try again.", 409)
            await build_context(self.db, now=self.clock(), focus_goal_ids=request.goal_ids, user_note=request.situation,
                                suggestion_history=self.state.feedback)
            self._invalidate()
            self.state.goal_ids, self.state.user_note = request.goal_ids, request.situation
            await self._persist()
        await self.tick(explicit=True)
        return await self.feed()

    async def feedback(self, request: FeedbackRequest) -> dict:
        async with self._lock:
            option = next((item for item in (self.cached or {}).get("options", []) if item["id"] == request.suggestion_id), None)
            if option is None:
                raise AssistantError("This suggestion is no longer available.", 404)
            self.state.feedback.append(SuggestionHistoryItem(suggestion_id=option["id"], action=option["action"], status=request.status, updated_at=self.clock()))
            self.state.feedback = [SuggestionHistoryItem.model_validate(item) for item in recent_suggestions(self.state.feedback, self.clock())]
            self._invalidate()
            await self._persist()
        self._wake.set()
        return await self.feed()

    async def chat(self, request: ChatSendRequest) -> dict:
        self._check_enabled()
        identifier = request.session_id or request.request_id
        lock = self._session_locks.setdefault(identifier, asyncio.Lock())
        task = asyncio.current_task()
        self._chat_tasks.add(task)
        try:
            async with lock:
                return await self._chat_locked(identifier, request)
        finally:
            self._chat_tasks.discard(task)

    async def _chat_locked(self, identifier: UUID, request: ChatSendRequest) -> dict:
        self._check_enabled()
        request_hash = hashlib.sha256(request.model_dump_json(exclude={"context_id"}).encode()).hexdigest()
        try:
            session = await asyncio.to_thread(self.sessions.load, identifier)
        except FileNotFoundError:
            if request.session_id:
                raise AssistantError("Conversation not found. Start a new chat.", 404)
            session = ChatSession(id=identifier)
        if session.last_request_id == request.request_id:
            if session.last_request_hash != request_hash:
                raise AssistantError("This request ID was already used for a different message.", 409)
            return self._chat_reply(session)
        if session.revision != request.expected_revision:
            raise AssistantError("The conversation changed. Reload it before sending another message.", 409)
        if len(session.messages) >= 198:
            raise AssistantError("This conversation is full. Start a new chat.", 409)
        epoch = self._epoch
        prepared = await prepare_turn(self.db, request, session.messages, now=self.clock(), user_note=self.state.user_note,
                                      focus_goal_ids=self.state.goal_ids or None, suggestion_history=self.state.feedback)
        strategy = prepared["forced_strategy"] or (await self._completion(strategy_messages(prepared))).strip()
        if strategy not in STRATEGIES:
            raise ValueError("The model returned an unknown conversation strategy")
        if epoch != self._epoch:
            raise AssistantError("Assistant settings changed. Retry your message.", 409)
        response = await self._completion(response_messages(prepared, strategy))
        references = validate_reply(response, prepared)
        async with self._lock:
            self._check_enabled()
            if epoch != self._epoch:
                raise AssistantError("Assistant context changed. Retry your message.", 409)
            person, _ = await personal_context(self.db.data_directory, None)
            if person != prepared["payload"]["context"]["person_context"]:
                raise AssistantError("Your personal context changed. Retry your message.", 409)
            # New observations need not cancel a conversation, but removed or
            # edited cited evidence must never be presented as unchanged.
            supplied = {node["id"]: node for key in ("context", "focused_graph", "question_graph") for node in prepared["payload"][key]["nodes"]}
            async with self.db.session() as db_session:
                checked_ids = set(references) | set(request.focus.entity_ids)
                for eid in checked_ids:
                    entity = await db_session.get(Entity, eid)
                    old = supplied[eid]
                    if entity is None or not _visible(entity) or _node(entity)["revision"] != old["revision"] or not entity.text.startswith(old["text"]):
                        raise AssistantError("Referenced evidence changed. Retry your message.", 409)
                old_edges = {edge["id"]: edge for key in ("context", "focused_graph", "question_graph")
                             for edge in prepared["payload"][key]["edges"]
                             if edge["source_id"] in checked_ids or edge["target_id"] in checked_ids}
                current_edges = {edge.id: edge for edge in await db_session.scalars(select(Relation).where(Relation.id.in_(old_edges)))}
                for eid, edge in old_edges.items():
                    current = current_edges.get(eid)
                    if current is None or (current.source_id, current.target_id, current.relation_type, current.relation_subtype) != (edge["source_id"], edge["target_id"], edge["type"], edge["subtype"]):
                        raise AssistantError("A referenced relationship changed. Retry your message.", 409)
            prepared.update(request_id=request.request_id, request_hash=request_hash, entity_refs=references)
            updated = await asyncio.to_thread(self.sessions.append_turn, session, request, response, strategy, prepared)
        return self._chat_reply(updated)

    @staticmethod
    def _chat_reply(session: ChatSession) -> dict:
        return {"session_id": str(session.id), "revision": session.revision,
                "message": session.messages[-1].model_dump(), "entity_refs": session.last_entity_refs}

    async def history(self, session_id: UUID) -> dict:
        try:
            session = await asyncio.to_thread(self.sessions.load, session_id)
        except FileNotFoundError:
            raise AssistantError("Conversation not found.", 404)
        return {"session_id": str(session.id), "revision": session.revision,
                "messages": [message.model_dump() for message in session.messages]}

    async def threads(self) -> dict:
        return {"threads": await asyncio.to_thread(self.sessions.list_recent)}

    async def refresh(self) -> dict:
        self._check_enabled()
        await self.tick(explicit=True)
        return await self.feed()

    def notify_context_change(self):
        self._wake.set()

    async def close(self):
        self._closed = True
        tasks = [task for task in [self._worker, self._job, *self._chat_tasks, *self._retired_tasks] if task and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.db.close()

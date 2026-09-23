"""Local MI chat preparation, focused retrieval, and conversation storage.

Adapted from YouBeAnything's shardul/ui coaching assistant. This module does
not invoke a provider or expose an HTTP endpoint; model transport is separate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import case, func, or_, select

from tempo.assistant_context import _available, _node, _utc, _visible, bound_node_text, build_context
from tempo.assistant_inputs import SuggestionHistoryItem
from tempo.db import Database
from tempo.models import Entity, Relation
from tempo.prompts.assistant_chat import STRATEGIES, STRATEGY_PROMPT, SYSTEM_PROMPT

EntityId = Annotated[int, Field(strict=True, gt=0)]
REFERENCE = re.compile(r"\[entity:([1-9][0-9]*):([^\[\]:\n]{1,100})\]")


class ChatFocus(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["overview", "goal", "activity", "action", "operation", "suggestion", "tradeoff"] = "overview"
    title: str = Field(default="Your goals and recent activity", min_length=1, max_length=300)
    detail: str = Field(default="", max_length=1500)
    entity_ids: list[EntityId] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def check_entities(self):
        if len(self.entity_ids) != len(set(self.entity_ids)):
            raise ValueError("Duplicate focus entities")
        if self.kind in {"goal", "activity", "action", "operation"} and len(self.entity_ids) != 1:
            raise ValueError("An entity focus needs exactly one entity")
        if self.kind in {"suggestion", "tradeoff"} and not self.entity_ids:
            raise ValueError("A selected detail needs supporting entities")
        if self.kind == "overview" and self.entity_ids:
            raise ValueError("An overview does not select an entity")
        return self


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)
    timestamp: str
    focus: ChatFocus = Field(default_factory=ChatFocus)
    strategy: str | None = None

    @field_validator("strategy")
    @classmethod
    def known_strategy(cls, value):
        if value is not None and value not in STRATEGIES:
            raise ValueError("Unknown MI strategy")
        return value


class ChatSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    revision: int = Field(default=0, ge=0)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=200)
    last_request_id: UUID | None = None
    last_request_hash: str | None = None
    last_entity_refs: list[EntityId] = Field(default_factory=list)


class ChatTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)
    intent: Literal["reflect", "suggest", "prepare"] = "reflect"
    focus: ChatFocus = Field(default_factory=ChatFocus)


class ChatSendRequest(ChatTurnRequest):
    request_id: UUID = Field(default_factory=uuid4)
    session_id: UUID | None = None
    expected_revision: int = Field(default=0, ge=0)
    context_id: str | None = Field(default=None, max_length=128)


async def _focused_graph(db: Database, focus: ChatFocus, *, max_nodes: int = 40, max_edges: int = 80) -> dict:
    """Retrieve bounded detail even when a selected item is older than the feed."""
    nodes: dict[int, dict] = {}
    edges: dict[int, dict] = {}
    truncated = False
    async with db.session() as session:
        for entity_id in focus.entity_ids:
            entity = await session.get(Entity, entity_id)
            if entity is None or not _visible(entity):
                raise ValueError("The selected detail is no longer available")
            if focus.kind in {"goal", "activity", "action", "operation"} and entity.type != focus.kind:
                raise ValueError("The selected entity has a different type")
            nodes[entity.id] = _node(entity)
        frontier = set(nodes)
        visited: set[int] = set()
        for _ in range(3):
            if not frontier:
                break
            visited.update(frontier)
            relations = list(await session.scalars(select(Relation).where(
                or_(Relation.source_id.in_(frontier), Relation.target_id.in_(frontier)),
                Relation.relation_type.in_(["structural", "behavioral", "temporal"]),
            ).order_by(Relation.id.desc()).limit(max_edges + 1)))
            truncated |= len(relations) > max_edges
            relations = relations[:max_edges]
            neighbors = {eid for edge in relations for eid in (edge.source_id, edge.target_id)} - nodes.keys()
            for entity in await session.scalars(select(Entity).where(
                Entity.id.in_(neighbors), Entity.type.in_(["goal", "activity", "action", "operation"]),
            ).order_by(Entity.id)):
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
                if _visible(entity):
                    nodes[entity.id] = _node(entity)
            for edge in relations:
                if edge.source_id in nodes and edge.target_id in nodes and len(edges) < max_edges:
                    edges[edge.id] = {"id": edge.id, "source_id": edge.source_id, "target_id": edge.target_id,
                                      "type": edge.relation_type, "subtype": edge.relation_subtype,
                                      "confidence": edge.confidence}
                elif edge.id not in edges:
                    truncated = True
            frontier = set(nodes) - visited
    text_truncated = bound_node_text(list(nodes.values()), budget=max_nodes * 300)
    return {"nodes": [nodes[eid] for eid in sorted(nodes)], "edges": [edges[eid] for eid in sorted(edges)],
            "graph_truncated": truncated, "text_truncated": text_truncated}


_QUESTION_WORDS = frozenset("""
    a about after again all also am an and any are as at be because been before being
    but can could did do does doing done for from had has have help here how i if in
    into is it its just like me might more most my need next not now of on one only
    or our out please really say should some something suggest suggestions than
    that the their them then there these they thing things think this those through
    time today too us very want was we were what when where which who why will with
    would you your yours myself yourself recently lately yesterday tomorrow tell
""".split())


async def _question_graph(db: Database, question: str, *, now: datetime) -> dict:
    """Find older text matches locally, then fetch their directed graph evidence.

    This is lexical retrieval, not a claim that a match is relevant or current.
    It deliberately searches labels only, excluding screenshot metadata.
    """
    terms = list(dict.fromkeys(word for word in re.findall(r"\b\w{3,}\b", question.casefold())
                               if word not in _QUESTION_WORDS and not word.isdecimal()))[:8]
    if not terms:
        return {"matched_ids": [], "terms": [], "nodes": [], "edges": [], "graph_truncated": False}
    conditions = [func.lower(Entity.text).contains(term, autoescape=True) for term in terms]
    score = sum(case((condition, 1), else_=0) for condition in conditions)
    seen_at = func.coalesce(Entity.timestamp_end, Entity.timestamp_start)
    async with db.session() as session:
        ids = list(await session.scalars(select(Entity.id).where(
            _available(), Entity.type.in_(["goal", "activity", "action", "operation"]),
            seen_at <= _utc(now).replace(tzinfo=None), or_(*conditions),
        ).order_by(score.desc(), seen_at.desc(), Entity.id.desc()).limit(6)))
    graph = await _focused_graph(db, ChatFocus(kind="suggestion", entity_ids=ids), max_nodes=24, max_edges=40) if ids else {
        "nodes": [], "edges": [], "graph_truncated": False,
    }
    return {"matched_ids": ids, "terms": terms, **graph}


async def prepare_turn(
    db: Database, request: ChatTurnRequest, history: list[ChatMessage], *,
    now: datetime | None = None, person_context: str | None = None, user_note: str = "",
    focus_goal_ids: list[int] | None = None,
    suggestion_history: list[SuggestionHistoryItem] | None = None,
) -> dict:
    """Prepare an MI turn locally; generating the actual reply is not done here."""
    now = _utc(now or datetime.now(timezone.utc))
    if request.focus.kind == "goal":
        focus_goal_ids = request.focus.entity_ids
    context = await build_context(db, now=now, person_context=person_context, user_note=user_note,
                                  focus_goal_ids=focus_goal_ids, suggestion_history=suggestion_history)
    focused = await _focused_graph(db, request.focus)
    question_graph = await _question_graph(db, request.message, now=now)
    known_nodes = {node["id"]: node for node in context["nodes"]}
    known_nodes.update((node["id"], node) for node in focused["nodes"])
    known_nodes.update((node["id"], node) for node in question_graph["nodes"])
    focus = request.focus.model_dump()
    if focus["kind"] in {"goal", "activity", "action", "operation"}:
        focus["title"] = known_nodes[focus["entity_ids"][0]]["text"][:300]
    strategies = [message.strategy for message in history if message.role == "assistant" and message.strategy]
    # Bound conversation context by characters as well as turn count.
    recent_history: list[dict] = []
    budget = 20000
    for message in reversed(history[-16:]):
        serialized = message.model_dump()
        size = len(json.dumps(serialized, ensure_ascii=False))
        if size > budget:
            break
        recent_history.append(serialized)
        budget -= size
    recent_history.reverse()
    # Keep a small verbatim record of earlier user statements instead of
    # presenting a generated summary as something the person actually said.
    omitted = history[:len(history) - len(recent_history)]
    earlier_user_messages = []
    for message in reversed(omitted):
        if message.role == "user":
            earlier_user_messages.append({"content": message.content[:1000], "timestamp": message.timestamp,
                                          "focus": message.focus.model_dump(exclude={"detail"}),
                                          "text_truncated": len(message.content) > 1000})
            if len(earlier_user_messages) == 4:
                break
    earlier_user_messages.reverse()
    payload = {"context": context, "focused_graph": focused, "focus": focus,
               "question_graph": question_graph, "earlier_user_messages": earlier_user_messages,
               "conversation": recent_history, "history_truncated": len(recent_history) < len(history),
               "message": request.message, "intent": request.intent, "recent_strategies": strategies[-3:]}
    context_id = hashlib.sha256(json.dumps({"context": context["context_id"], "focused": focused,
                                          "question_graph": question_graph}, sort_keys=True).encode()).hexdigest()
    return {"payload": payload, "context_id": context_id, "allowed_entity_ids": sorted(known_nodes),
            "forced_strategy": "Advise with Permission" if request.intent in {"suggest", "prepare"} else None}


def strategy_messages(prepared: dict) -> list[dict]:
    return [{"role": "system", "content": STRATEGY_PROMPT},
            {"role": "user", "content": json.dumps({"strategies": list(STRATEGIES), **prepared["payload"]}, ensure_ascii=False)}]


def response_messages(prepared: dict, strategy: str) -> list[dict]:
    strategy = prepared["forced_strategy"] or strategy
    if strategy not in STRATEGIES:
        raise ValueError("Unknown MI strategy")
    return [{"role": "system", "content": SYSTEM_PROMPT + "\n\nStrategy for this turn: " + strategy + "\n" + STRATEGIES[strategy]},
            {"role": "user", "content": json.dumps(prepared["payload"], ensure_ascii=False)}]


def validate_reply(content: str, prepared: dict) -> list[int]:
    """Only entities actually retrieved for this conversation become links."""
    if not content.strip() or len(content) > 8000:
        raise ValueError("The assistant returned an invalid reply")
    references = list(REFERENCE.finditer(content))
    if "[entity:" in REFERENCE.sub("", content):
        raise ValueError("Malformed entity reference")
    ids = list(dict.fromkeys(int(match.group(1)) for match in references))
    if not set(ids).issubset(prepared["allowed_entity_ids"]):
        raise ValueError("The reply refers to evidence outside the conversation context")
    return ids


class ChatSessions:
    """Atomic local history, stored under the caller's Tempo data directory."""

    def __init__(self, data_directory: str | Path):
        self.directory = Path(data_directory) / "assistant" / "chats"
        self._lock = threading.RLock()

    def load(self, session_id: str | UUID) -> ChatSession:
        identifier = UUID(str(session_id))
        session = ChatSession.model_validate_json((self.directory / f"{identifier}.json").read_text())
        if session.id != identifier:
            raise ValueError("Conversation identity mismatch")
        return session

    def list_recent(self, limit: int = 50) -> list[dict]:
        """List saved threads using their first topic, without another model call."""
        if not self.directory.exists():
            return []
        paths = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        threads = []
        for path in paths:
            try:
                session = self.load(path.stem)
            except (OSError, ValueError):
                continue
            if not session.messages:
                continue
            first, last = session.messages[0], session.messages[-1]
            title = first.focus.title if first.focus.kind != "overview" else first.content
            threads.append({"session_id": str(session.id), "title": title[:100],
                            "updated_at": last.timestamp, "focus": last.focus.model_dump()})
            if len(threads) >= limit:
                break
        return threads

    def save(self, session: ChatSession) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                handle.write(session.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / f"{session.id}.json")
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def append_turn(self, session: ChatSession, request: ChatTurnRequest, response: str, strategy: str, prepared: dict) -> ChatSession:
        validate_reply(response, prepared)
        strategy = prepared["forced_strategy"] or strategy
        if strategy not in STRATEGIES:
            raise ValueError("Unknown MI strategy")
        timestamp = datetime.now(timezone.utc).isoformat()
        focus = ChatFocus.model_validate(prepared["payload"]["focus"])
        updated = ChatSession(id=session.id, revision=session.revision + 1,
                              last_request_id=prepared.get("request_id"), last_request_hash=prepared.get("request_hash"),
                              last_entity_refs=prepared.get("entity_refs", []), messages=[
            *session.messages,
            ChatMessage(role="user", content=request.message, timestamp=timestamp, focus=focus),
            ChatMessage(role="assistant", content=response, timestamp=timestamp, focus=focus, strategy=strategy),
        ])
        with self._lock:
            path = self.directory / f"{session.id}.json"
            if path.exists() and self.load(session.id).revision != session.revision:
                raise ValueError("The conversation changed while this reply was being prepared")
            if not path.exists() and session.revision != 0:
                raise ValueError("The conversation is no longer available")
            self.save(updated)
        return updated

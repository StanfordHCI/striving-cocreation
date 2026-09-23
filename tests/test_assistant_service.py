"""Assistant integration against fictional data and a deterministic provider."""

import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from tempo.assistant_chat import ChatFocus, ChatSendRequest
from tempo.assistant_context import build_context
from tempo.assistant_service import (
    AssistantError, AssistantRefineRequest, AssistantService, AssistantSettingsRequest,
    FeedbackRequest, validate_suggestions,
)
from tempo.db import Database
from tempo.models import Entity, Relation

NOW = datetime(2026, 9, 23, 17, tzinfo=timezone.utc)


async def seed(db, now=NOW):
    async with db.session() as session:
        goal = Entity(type="goal", text="Support collaborators", timestamp_start=now.replace(tzinfo=None) - timedelta(days=30))
        second = Entity(type="goal", text="Develop my research skills", timestamp_start=now.replace(tzinfo=None) - timedelta(days=20))
        activity = Entity(type="activity", text="Reviewing a colleague's manuscript", timestamp_start=now.replace(tzinfo=None))
        operation = Entity(type="operation", text="Read a request for feedback on a draft", timestamp_start=now.replace(tzinfo=None), metadata_json='{"screenshot_path":"NOT_FOR_MODEL"}')
        session.add_all([goal, second, activity, operation])
        await session.flush()
        session.add(Relation(source_id=activity.id, target_id=goal.id, relation_type="structural", relation_subtype="part_of"))
        return {"goal": goal.id, "second": second.id, "activity": activity.id, "operation": operation.id}


def proposal(context, *, confidence=9, minutes=10):
    return {"summary": "A draft review is a possible next step.", "options": [{
        "action": "Review one section of the draft", "why": "You just received a request for feedback.",
        "goal_ids": context["focus_goal_ids"] or context["goal_ids"][:2],
        "evidence_ids": [context["recent_ids"][0]], "edge_ids": [],
        "confidence": confidence, "valid_for_minutes": minutes,
    }], "tradeoffs": []}


class SyntheticProvider:
    model = "synthetic-assistant-test"
    api_base = "http://127.0.0.1:8767"
    api_key = None
    use_vertexai = False
    vertex_location = "global"

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
        self.fail = False
        self.invalid_citation = False

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        if "output_schema" in payload:
            self.started.set()
            if self.release is not None:
                await self.release.wait()
            if self.fail:
                raise RuntimeError("PRIVATE_PROVIDER_DIAGNOSTICS")
            return json.dumps(proposal(payload["context"]))
        if "strategies" in payload:
            return "Reflect"
        if self.invalid_citation:
            return "Try [entity:999999:an invented action]."
        goal_id = payload["context"]["goal_ids"][0]
        if payload["intent"] == "prepare":
            return (f"Draft review plan for [entity:{goal_id}:this goal]:\n"
                    "1. Read the section once for its main claim.\n"
                    "2. Note one point that needs clarification.\n"
                    "3. Draft feedback here before sharing it.")
        return f"It sounds like [entity:{goal_id}:this goal] matters to you. Which part feels difficult?"


@pytest_asyncio.fixture
async def runtime(db):
    ids = await seed(db)
    provider = SyntheticProvider()
    connection = {"id": "test-connection", "configured": True, "model": provider.model,
                  "label": "Synthetic local provider", "destination": "127.0.0.1"}
    clock = [NOW]
    service = AssistantService(db, lambda: provider, lambda: connection, clock=lambda: clock[0])
    await service.start(background=False)
    yield service, provider, ids, clock, connection
    await service.close()


async def enable(service):
    await service.settings(AssistantSettingsRequest(enabled=True, connection_id=service.connection()["id"]))
    if service._job:
        await service._job
    return await service.feed()


@pytest.mark.asyncio
async def test_disabled_is_read_only_and_enable_connects_provider(runtime):
    service, provider, ids, _, _ = runtime
    feed = await service.feed()
    assert not feed["enabled"] and not provider.calls
    with pytest.raises(AssistantError):
        await service.chat(ChatSendRequest(message="Hello"))
    feed = await enable(service)
    assert feed["enabled"]
    assert feed["advice"]["options"][0]["confidence"] == 9
    assert set(feed["advice"]["options"][0]["goal_ids"]) == {ids["goal"], ids["second"]}
    assert "NOT_FOR_MODEL" not in json.dumps(provider.calls)
    assert service.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_confidence_and_freshness_are_independent(runtime):
    service, _, _, _, _ = runtime
    context = await build_context(service.db, now=NOW)
    assert validate_suggestions(json.dumps(proposal(context, confidence=7)), context, NOW)["options"] == []
    result = validate_suggestions(json.dumps(proposal(context, confidence=10, minutes=1)), context, NOW)
    assert datetime.fromisoformat(result["options"][0]["expires_at"]) == NOW + timedelta(minutes=1)
    assert validate_suggestions(json.dumps(proposal(context, confidence=10)), context, NOW + timedelta(hours=5))["options"] == []
    bad = proposal(context)
    bad["options"][0]["edge_ids"] = [999999]
    with pytest.raises(ValueError):
        validate_suggestions(json.dumps(bad), context, NOW)


@pytest.mark.asyncio
async def test_expired_suggestions_disappear(runtime):
    service, provider, _, clock, _ = runtime
    feed = await enable(service)
    assert feed["advice"]["options"]
    clock[0] += timedelta(minutes=11)
    feed = await service.feed()
    assert feed["advice"] is None or not feed["advice"]["options"]
    assert len(provider.calls) == 1  # Cooldown still governs automatic requests.


@pytest.mark.asyncio
async def test_background_prepares_chosen_goals_before_opening_feed(runtime):
    service, provider, ids, clock, connection = runtime
    await enable(service)
    await service.refine(AssistantRefineRequest(goal_ids=[ids["goal"], ids["second"]]))
    await service._job
    await service.close()
    provider.started.clear()
    count = len(provider.calls)
    restarted = AssistantService(Database(db_name=service.db.db_name, data_directory=service.db.data_directory),
                                 lambda: provider, lambda: connection, clock=lambda: clock[0])
    await restarted.start()  # No feed, refresh, or goal-selection request.
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        await restarted._job
        assert len(provider.calls) == count + 1
        assert restarted.cached["options"]
        for _ in range(3):
            feed = await restarted.feed()
            for goal_id in feed["focus_goal_ids"]:
                assert any(goal_id in option["goal_ids"] for option in feed["advice"]["options"])
        assert len(provider.calls) == count + 1  # Reading prepared steps does not regenerate.
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_partial_expiry_refreshes_without_hiding_other_prepared_steps(runtime):
    service, provider, ids, clock, _ = runtime
    await enable(service)
    raw = proposal(service.context, minutes=1)
    raw["options"][0]["goal_ids"] = [ids["goal"]]
    raw["options"].append({**raw["options"][0], "action": "Outline the review", "goal_ids": [ids["second"]], "valid_for_minutes": 10})
    service.cached = validate_suggestions(json.dumps(raw), service.context, clock[0])
    clock[0] += timedelta(minutes=2)
    service.gate.last_started -= 121
    service.gate.changed_at -= 31
    provider.release = asyncio.Event()
    provider.started.clear()
    feed = await service.feed()
    await asyncio.wait_for(provider.started.wait(), timeout=3)
    assert feed["busy"]
    assert [option["action"] for option in feed["advice"]["options"]] == ["Outline the review"]
    provider.release.set()
    await service._job


@pytest.mark.asyncio
async def test_completed_suggestions_stay_suppressed_after_restart(runtime):
    service, provider, _, clock, connection = runtime
    option = (await enable(service))["advice"]["options"][0]
    await service.feedback(FeedbackRequest(suggestion_id=option["id"], status="completed"))
    saved_path = service.db.data_directory
    await service.close()
    restarted = AssistantService(Database(db_name=service.db.db_name, data_directory=saved_path), lambda: provider, lambda: connection, clock=lambda: clock[0])
    await restarted.start(background=False)
    try:
        await restarted.refresh()
        await restarted._job
        assert (await restarted.feed())["advice"]["options"] == []
        assert restarted.context["suggestion_history"][0]["status"] == "completed"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_changing_context_rejects_an_inflight_suggestion(runtime):
    service, provider, ids, _, _ = runtime
    provider.release = asyncio.Event()
    await service.settings(AssistantSettingsRequest(enabled=True, connection_id="test-connection"))
    await provider.started.wait()
    async with service.db.session() as session:
        goal = await session.get(Entity, ids["goal"])
        goal.text = "Finish my own draft before helping others"
        goal.metadata_dict = {"user_edited": True, "revision": 1}
    provider.release.set()
    await service._job
    assert (await service.feed())["advice"] is None
    assert not service.gate.cache_is_current


@pytest.mark.asyncio
async def test_pause_cancels_generation_and_destination_change_stops_new_calls(runtime):
    service, provider, _, _, connection = runtime
    provider.release = asyncio.Event()
    await service.settings(AssistantSettingsRequest(enabled=True, connection_id="test-connection"))
    await provider.started.wait()
    feed = await service.settings(AssistantSettingsRequest(enabled=False))
    assert not feed["enabled"] and feed["advice"] is None
    provider.release.set()
    await enable(service)
    count = len(provider.calls)
    connection["id"] = "a-different-destination"
    assert not (await service.feed())["enabled"]
    with pytest.raises(AssistantError):
        await service.chat(ChatSendRequest(message="Hello"))
    assert len(provider.calls) == count


@pytest.mark.asyncio
async def test_focus_and_user_note_reach_suggestions_and_chat_and_survive_polling(runtime):
    service, provider, ids, _, _ = runtime
    feed = await enable(service)
    await service.refine(AssistantRefineRequest(context_id=feed["context"]["id"], goal_ids=[ids["second"]], situation="Research comes first today"))
    await service._job
    feed = await service.feed()
    assert feed["focus_goal_ids"] == [ids["second"]]
    assert feed["advice"]["options"][0]["goal_ids"] == [ids["second"]]
    await service.chat(ChatSendRequest(message="I feel torn"))
    for messages in provider.calls[-2:]:
        context = json.loads(messages[-1]["content"])["context"]
        assert context["focus_goal_ids"] == [ids["second"]]
        assert context["user_note"] == "Research comes first today"


@pytest.mark.asyncio
async def test_retries_are_idempotent_and_history_survives_new_service(runtime):
    service, provider, _, _, _ = runtime
    await enable(service)
    request = ChatSendRequest(message="I feel torn about the review")
    first, retry = await asyncio.gather(service.chat(request), service.chat(request))
    assert first == retry and first["revision"] == 1
    assert len(provider.calls) == 3  # One suggestion call, strategy, response.
    history = await service.history(UUID(first["session_id"]))
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert service.sessions.load(first["session_id"]).last_request_id == request.request_id
    with pytest.raises(AssistantError):
        await service.chat(request.model_copy(update={"message": "A different request"}))
    with pytest.raises(AssistantError):
        await service.chat(ChatSendRequest(session_id=first["session_id"], expected_revision=0, message="A stale turn"))


@pytest.mark.asyncio
async def test_bad_citation_never_enters_history(runtime):
    service, provider, _, _, _ = runtime
    await enable(service)
    provider.invalid_citation = True
    request = ChatSendRequest(message="How should I think about this?")
    with pytest.raises(ValueError):
        await service.chat(request)
    assert not (service.sessions.directory / f"{request.request_id}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["selected_entity", "relationship", "onboarding"])
async def test_chat_rechecks_user_context_and_evidence_before_saving(runtime, change):
    service, provider, ids, _, _ = runtime
    await enable(service)
    started, release = asyncio.Event(), asyncio.Event()
    original = provider.chat_completion

    async def delayed_response(messages, **kwargs):
        response = await original(messages, **kwargs)
        payload = json.loads(messages[-1]["content"])
        if "strategies" not in payload and "output_schema" not in payload:
            started.set()
            await release.wait()
        return response

    provider.chat_completion = delayed_response
    request = ChatSendRequest(message="I feel torn", focus=ChatFocus(kind="activity", entity_ids=[ids["activity"]]))
    pending = asyncio.create_task(service.chat(request))
    await started.wait()
    if change == "onboarding":
        Path(service.db.data_directory, "onboarding.json").write_text(json.dumps({"responses": {"roles": "I am taking a break from research"}}))
    else:
        async with service.db.session() as session:
            if change == "selected_entity":
                entity = await session.get(Entity, ids["activity"])
                entity.text = "Working on my own manuscript"
                entity.metadata_dict = {"revision": 1, "user_edited": True}
            else:
                edge = await session.scalar(select(Relation))
                await session.delete(edge)
    release.set()
    with pytest.raises(AssistantError):
        await pending
    assert not (service.sessions.directory / f"{request.request_id}.json").exists()


@pytest.mark.asyncio
async def test_direct_suggestions_skip_strategy_call(runtime):
    service, provider, ids, _, _ = runtime
    await enable(service)
    count = len(provider.calls)
    reply = await service.chat(ChatSendRequest(message="Suggest next steps", intent="suggest", focus=ChatFocus(kind="goal", entity_ids=[ids["goal"]])))
    assert len(provider.calls) == count + 1
    assert reply["message"]["strategy"] == "Advise with Permission"


@pytest.mark.asyncio
async def test_prepare_drafts_in_chat_and_preserves_the_conversation(runtime):
    service, provider, _, _, _ = runtime
    feed = await enable(service)
    option = feed["advice"]["options"][0]
    focus = ChatFocus(kind="suggestion", title=option["action"], detail=option["action"] + "\n\n" + option["why"],
                      entity_ids=option["goal_ids"] + option["evidence_ids"])
    request = ChatSendRequest(message="Prepare this next step here. Write a draft or plan I can refine.", intent="prepare", focus=focus)
    count = len(provider.calls)
    first = await service.chat(request)
    assert len(provider.calls) == count + 1
    assert "Draft review plan" in first["message"]["content"]
    assert first == await service.chat(request)
    assert len(provider.calls) == count + 1
    assert (await service.feed())["advice"] == feed["advice"]  # Preparing does not clear or complete suggestions.
    second = await service.chat(ChatSendRequest(session_id=first["session_id"], expected_revision=1,
                                               message="Make that plan shorter", focus=focus))
    payload = json.loads(provider.calls[-1][-1]["content"])
    assert payload["conversation"][-1]["content"] == first["message"]["content"]
    assert payload["focus"]["detail"] == focus.detail
    assert second["revision"] == 2
    history = await service.history(UUID(second["session_id"]))
    assert len(history["messages"]) == 4
    assert not service.state.feedback


@pytest.mark.asyncio
async def test_saved_threads_are_independent_and_available_when_paused(runtime):
    service, provider, ids, _, _ = runtime
    await enable(service)
    first = await service.chat(ChatSendRequest(message="Help with the review", focus=ChatFocus(kind="goal", entity_ids=[ids["goal"]])))
    second = await service.chat(ChatSendRequest(message="A separate research question"))
    count = len(provider.calls)
    await service.settings(AssistantSettingsRequest(enabled=False))
    threads = (await service.threads())["threads"]
    assert [thread["session_id"] for thread in threads] == [second["session_id"], first["session_id"]]
    assert [thread["title"] for thread in threads] == ["A separate research question", "Support collaborators"]
    assert len(provider.calls) == count
    assert len((await service.history(UUID(first["session_id"])))["messages"]) == 2
    assert service.sessions.list_recent(limit=1) == threads[:1]
    (service.sessions.directory / f"{uuid4()}.json").write_text("{corrupt")
    assert (await service.threads())["threads"] == threads


@pytest.mark.asyncio
async def test_provider_failure_is_visible_and_can_retry(runtime):
    service, provider, _, _, _ = runtime
    provider.fail = True
    feed = await enable(service)
    assert feed["error"] and feed["advice"] is None
    assert "PRIVATE_PROVIDER_DIAGNOSTICS" not in str(feed)
    provider.fail = False
    await service.refresh()
    await service._job
    assert (await service.feed())["advice"]["options"]


def test_http_routes_connect_the_synthetic_provider_and_enforce_local_origin(tmp_path, monkeypatch):
    import tempo.server as server

    provider = SyntheticProvider()
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)
    monkeypatch.setattr(server, "current_db_name", "tempo.db")
    monkeypatch.setattr(server, "_compile_provider", lambda: provider)

    async def populate():
        db = Database(data_directory=str(tmp_path))
        await db.connect()
        await seed(db, datetime.now(timezone.utc))
        await db.close()

    asyncio.run(populate())
    with TestClient(server.app) as client:
        initial = client.get("/api/assistant/feed").json()
        assert initial["enabled"] is False and provider.calls == []
        assert client.put("/api/assistant/settings", json={"enabled": True, "connection_id": initial["connection"]["id"]}, headers={"origin": "https://untrusted.example"}).status_code == 403
        enabled = client.put("/api/assistant/settings", json={"enabled": True, "connection_id": initial["connection"]["id"]})
        assert enabled.status_code == 200
        message = {"request_id": str(uuid4()), "message": "I feel torn"}
        response = client.post("/api/assistant/chat", json=message)
        assert response.status_code == 200
        reply = response.json()
        assert reply["entity_refs"]
        assert client.get("/api/assistant/chats").json()["threads"][0]["session_id"] == reply["session_id"]
        assert client.post("/api/assistant/chat", json=message).json() == reply
        assert len(client.get("/api/assistant/chats/" + reply["session_id"]).json()["messages"]) == 2
        paused = client.put("/api/assistant/settings", json={"enabled": False})
        assert paused.status_code == 200 and not paused.json()["enabled"]
        assert client.post("/api/assistant/chat", json={"message": "After pausing"}).status_code == 409
    assert server.assistant_service is None

"""MI preparation, selected-detail retrieval, and local history; no LLM calls."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from tempo.assistant_chat import (
    ChatFocus, ChatMessage, ChatSession, ChatSessions, ChatTurnRequest,
    prepare_turn, response_messages, strategy_messages, validate_reply,
)
from tempo.models import Entity, Relation
from tempo.assistant_inputs import SuggestionHistoryItem

NOW = datetime(2026, 9, 22, 17)


async def seed(db):
    async with db.session() as session:
        goal = Entity(type="goal", text="Find meaningful work", timestamp_start=NOW - timedelta(days=60))
        activity = Entity(type="activity", text="Preparing an application", timestamp_start=NOW - timedelta(days=20))
        action = Entity(type="action", text="Revising a cover letter", timestamp_start=NOW - timedelta(days=20))
        recent = Entity(type="operation", text="Reading a colleague's message", timestamp_start=NOW)
        session.add_all([goal, activity, action, recent])
        await session.flush()
        session.add_all([
            Relation(source_id=activity.id, target_id=goal.id, relation_type="structural", relation_subtype="part_of"),
            Relation(source_id=action.id, target_id=activity.id, relation_type="structural", relation_subtype="part_of"),
        ])
        return {"goal": goal.id, "activity": activity.id, "action": action.id, "recent": recent.id}


@pytest.mark.asyncio
async def test_old_selected_detail_gets_current_graph_context(db):
    ids = await seed(db)
    request = ChatTurnRequest(message="I feel torn about this.", focus=ChatFocus(kind="action", title="Untrusted client label", entity_ids=[ids["action"]]))
    prepared = await prepare_turn(db, request, [], now=NOW)
    payload = prepared["payload"]
    assert payload["focus"]["title"] == "Revising a cover letter"
    assert {node["id"] for node in payload["focused_graph"]["nodes"]} == {ids["goal"], ids["activity"], ids["action"]}
    assert ids["recent"] in payload["context"]["recent_ids"]
    assert prepared["forced_strategy"] is None
    assert json.loads(strategy_messages(prepared)[1]["content"])["message"] == request.message


@pytest.mark.asyncio
async def test_requesting_suggestions_is_already_permission_to_advise(db):
    ids = await seed(db)
    request = ChatTurnRequest(message="Suggest next steps for this.", intent="suggest", focus=ChatFocus(kind="goal", entity_ids=[ids["goal"]]))
    prepared = await prepare_turn(db, request, [], now=NOW)
    assert prepared["forced_strategy"] == "Advise with Permission"
    messages = response_messages(prepared, "Question")
    assert "Strategy for this turn: Advise with Permission" in messages[0]["content"]
    assert "do not ask again" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["focus"]["entity_ids"] == [ids["goal"]]


@pytest.mark.asyncio
async def test_preparing_uses_full_step_and_does_not_select_a_reflective_strategy(db):
    ids = await seed(db)
    detail = "Draft a short follow-up about the application.\n\nThe colleague's message is relevant."
    request = ChatTurnRequest(message="Write a draft I can refine here.", intent="prepare",
                              focus=ChatFocus(kind="suggestion", title="Draft a follow-up", detail=detail,
                                              entity_ids=[ids["goal"], ids["recent"]]))
    prepared = await prepare_turn(db, request, [], now=NOW)
    assert prepared["forced_strategy"] == "Advise with Permission"
    messages = response_messages(prepared, "Reflect")
    assert json.loads(messages[1]["content"])["focus"]["detail"] == detail
    assert "artifact itself" in messages[0]["content"]
    assert "no connected tools" in messages[0]["content"]


@pytest.mark.asyncio
async def test_history_preserves_scope_and_strategy_with_bounded_context(db):
    await seed(db)
    history = [ChatMessage(role="assistant", content="x" * 7000, timestamp=NOW.isoformat(), strategy="Reflect") for _ in range(20)]
    prepared = await prepare_turn(db, ChatTurnRequest(message="Could we keep talking?"), history, now=NOW)
    assert prepared["payload"]["history_truncated"]
    assert sum(len(message["content"]) for message in prepared["payload"]["conversation"]) <= 20000
    assert prepared["payload"]["recent_strategies"] == ["Reflect"] * 3


@pytest.mark.asyncio
async def test_both_chat_stages_receive_onboarding_focus_user_note_and_feedback(db, monkeypatch):
    ids = await seed(db)
    Path(db.data_directory, "onboarding.json").write_text(json.dumps({"user_name": "Test Person", "responses": {"roles": "Researcher and caregiver", "work": "Looking for a flexible role"}}))
    feedback = SuggestionHistoryItem(suggestion_id="old-advice", action="Revise the introduction", status="completed", updated_at=NOW)
    monkeypatch.setattr("tempo.assistant_context.MAX_GOALS", 1)
    request = ChatTurnRequest(message="What could I do?", focus=ChatFocus(kind="goal", entity_ids=[ids["goal"]]))
    prepared = await prepare_turn(db, request, [], now=NOW, user_note="Family is my priority today", suggestion_history=[feedback])
    for messages in [strategy_messages(prepared), response_messages(prepared, "Giving Information")]:
        context = json.loads(messages[1]["content"])["context"]
        assert "Researcher and caregiver" in context["person_context"]
        assert "Looking for a flexible role" in context["person_context"]
        assert context["user_note"] == "Family is my priority today"
        assert context["focus_goal_ids"] == context["goal_ids"] == [ids["goal"]]
        assert context["suggestion_history"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_chat_finds_old_evidence_for_a_question_without_a_selected_card(db):
    ids = await seed(db)
    async with db.session() as session:
        session.add_all(Entity(type="action", text="cover letter", timestamp_start=NOW, metadata_json='{"removed_by_user":true}') for _ in range(12))
        old_action = await session.get(Entity, ids["action"])
        old_action.metadata_dict = {"screenshot_path": "NOT_ALLOWED"}
    prepared = await prepare_turn(db, ChatTurnRequest(message="Can you help with my cover letter?"), [], now=NOW)
    graph = prepared["payload"]["question_graph"]
    assert ids["action"] in graph["matched_ids"]
    assert {node["id"] for node in graph["nodes"]} == {ids["goal"], ids["activity"], ids["action"]}
    assert "NOT_ALLOWED" not in str(prepared)
    assert ids["action"] not in prepared["payload"]["context"]["recent_ids"]
    assert validate_reply(f"We can revisit [entity:{ids['action']}:your cover letter].", prepared) == [ids["action"]]


@pytest.mark.asyncio
async def test_generic_question_does_not_retrieve_arbitrary_old_evidence(db):
    await seed(db)
    prepared = await prepare_turn(db, ChatTurnRequest(message="What should I do next?"), [], now=NOW)
    assert prepared["payload"]["question_graph"]["matched_ids"] == []


@pytest.mark.asyncio
async def test_earlier_user_statements_survive_a_long_conversation(db):
    history = [ChatMessage(role="user", content="Please stop suggesting evening work.", timestamp=NOW.isoformat())]
    history += [ChatMessage(role="assistant", content="x" * 7000, timestamp=NOW.isoformat(), strategy="Reflect") for _ in range(20)]
    prepared = await prepare_turn(db, ChatTurnRequest(message="What about now?"), history, now=NOW)
    assert prepared["payload"]["earlier_user_messages"][0]["content"] == "Please stop suggesting evening work."
    assert prepared["payload"]["history_truncated"]


@pytest.mark.asyncio
async def test_explicit_personal_context_override_is_not_dropped(db):
    prepared = await prepare_turn(db, ChatTurnRequest(message="Can you help?"), [], now=NOW, person_context="I am on leave this week")
    assert prepared["payload"]["context"]["person_context"] == "I am on leave this week"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_focus", ["missing", "removed", "wrong_type"])
async def test_invalid_selected_detail_is_not_used(db, bad_focus):
    ids = await seed(db)
    entity_id = ids["goal"]
    if bad_focus == "missing": entity_id = 9999
    if bad_focus == "wrong_type": entity_id = ids["action"]
    if bad_focus == "removed":
        async with db.session() as session:
            entity = await session.get(Entity, entity_id)
            entity.metadata_dict = {"removed_by_user": True}
    with pytest.raises(ValueError):
        await prepare_turn(db, ChatTurnRequest(message="Help me with this", focus=ChatFocus(kind="goal", entity_ids=[entity_id])), [], now=NOW)


@pytest.mark.asyncio
async def test_citations_must_refer_to_retrieved_entities(db):
    ids = await seed(db)
    prepared = await prepare_turn(db, ChatTurnRequest(message="What could I do?"), [], now=NOW)
    assert validate_reply(f"You could revisit [entity:{ids['goal']}:this goal].", prepared) == [ids["goal"]]
    for text in ["[entity:999999:invented]", "[entity:1:2:extra]", "[entity:x:bad ID]", ""]:
        with pytest.raises(ValueError): validate_reply(text, prepared)


@pytest.mark.asyncio
async def test_session_round_trip_keeps_focus_and_does_not_overwrite_new_turns(db, tmp_path):
    ids = await seed(db)
    store = ChatSessions(tmp_path)
    original = ChatSession()
    request = ChatTurnRequest(message="Suggest something concrete.", intent="suggest", focus=ChatFocus(kind="activity", entity_ids=[ids["activity"]]))
    prepared = await prepare_turn(db, request, [], now=NOW)
    response = f"Revise one example in [entity:{ids['action']}:your cover letter]."
    updated = store.append_turn(original, request, response, "Question", prepared)
    loaded = store.load(updated.id)
    assert loaded == updated and len(loaded.messages) == 2
    assert loaded.messages[1].strategy == "Advise with Permission"
    assert loaded.messages[1].focus.title == "Preparing an application"
    assert (store.directory / f"{loaded.id}.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError): store.append_turn(original, request, "A late reply", "Reflect", prepared)
    assert store.load(updated.id) == updated


@pytest.mark.asyncio
async def test_invalid_reply_leaves_history_untouched(db, tmp_path):
    await seed(db)
    store = ChatSessions(tmp_path)
    session = ChatSession()
    request = ChatTurnRequest(message="Can you help?")
    prepared = await prepare_turn(db, request, [], now=NOW)
    with pytest.raises(ValueError):
        store.append_turn(session, request, "[entity:987654:fake evidence]", "Reflect", prepared)
    assert not store.directory.exists()


def test_session_paths_cannot_escape_data_directory(tmp_path):
    store = ChatSessions(tmp_path)
    with pytest.raises(ValueError): store.load("../../outside")
    with pytest.raises(FileNotFoundError): store.load(uuid4())

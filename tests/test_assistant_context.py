"""Local context retrieval and refresh behavior; no provider or live user data."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tempo.assistant_context import MAX_EDGES, MAX_NODE_TEXT, MAX_NODES, RefreshGate, build_context
from tempo.assistant_inputs import SuggestionHistoryItem
from tempo.models import Entity, Relation

NOW = datetime(2026, 9, 22, 17, 0)


async def seed_context(db):
    async with db.session() as session:
        nodes = {
            "career": Entity(type="goal", text="Find meaningful work", timestamp_start=NOW - timedelta(days=90), metadata_json='{"user_edited":true,"revision":3}'),
            "family": Entity(type="goal", text="Be present for my family", timestamp_start=NOW - timedelta(days=60)),
            "collaboration": Entity(type="goal", text="Support collaborators", timestamp_start=NOW - timedelta(days=30)),
            "job": Entity(type="activity", text="Applying for roles", timestamp_start=NOW - timedelta(days=7)),
            "review": Entity(type="activity", text="Helping a colleague revise a paper", timestamp_start=NOW - timedelta(days=2)),
            "cover_letter": Entity(type="action", text="Revising a cover letter", timestamp_start=NOW - timedelta(minutes=5)),
            "new_context": Entity(type="operation", text="Read a new message asking for feedback", timestamp_start=NOW - timedelta(minutes=1), metadata_json='{"screenshot_path":"SHOULD_NOT_BE_INCLUDED","secret":"SHOULD_NOT_BE_INCLUDED"}'),
            "old_context": Entity(type="operation", text="Unrelated old observation", timestamp_start=NOW - timedelta(days=2)),
            "removed": Entity(type="goal", text="Removed goal", timestamp_start=NOW, metadata_json='{"removed_by_user":true}'),
        }
        session.add_all(nodes.values())
        await session.flush()
        for child, parent, kind, subtype in [
            ("cover_letter", "job", "structural", "part_of"),
            ("job", "career", "structural", "part_of"),
            ("review", "collaboration", "structural", "part_of"),
            ("review", "job", "behavioral", "hinders"),
            ("review", "collaboration", "behavioral", "supports"),
        ]:
            session.add(Relation(source_id=nodes[child].id, target_id=nodes[parent].id, relation_type=kind, relation_subtype=subtype))
        return {name: node.id for name, node in nodes.items()}


@pytest.mark.asyncio
async def test_context_combines_unlinked_recent_activity_broader_goals_and_graph(db):
    ids = await seed_context(db)
    result = await build_context(db, now=NOW, person_context="I am considering a career change.")
    assert result["recent_ids"][0] == ids["new_context"]
    assert ids["cover_letter"] in result["recent_ids"]
    assert ids["old_context"] not in result["recent_ids"]
    assert set(result["goal_ids"]) == {ids["career"], ids["family"], ids["collaboration"]}
    assert result["person_context"] == "I am considering a career change."
    assert "SHOULD_NOT_BE_INCLUDED" not in str(result)
    assert {edge["subtype"] for edge in result["edges"]} >= {"part_of", "supports", "hinders"}
    edge = next(edge for edge in result["edges"] if edge["subtype"] == "hinders")
    assert (edge["source_id"], edge["target_id"]) == (ids["review"], ids["job"])
    career = next(node for node in result["nodes"] if node["id"] == ids["career"])
    assert career["user_edited"] and career["revision"] == 3
    assert result["focus_goal_ids"] is None  # The generator can choose the lens jointly with actions.


@pytest.mark.asyncio
async def test_clock_ticks_do_not_regenerate_unchanged_context(db):
    await seed_context(db)
    first = await build_context(db, now=NOW)
    later = await build_context(db, now=NOW + timedelta(minutes=1))
    aware = await build_context(db, now=NOW.replace(tzinfo=timezone.utc))
    assert first["context_id"] == later["context_id"] == aware["context_id"]
    assert first["prepared_at"] != later["prepared_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["goal_edit", "new_observation", "graph_edge", "user_note"])
async def test_meaningful_changes_invalidate_context(db, change):
    ids = await seed_context(db)
    before = await build_context(db, now=NOW)
    if change != "user_note":
        async with db.session() as session:
            if change == "goal_edit":
                goal = await session.get(Entity, ids["career"])
                goal.text = "Choose a stable role near family"
            elif change == "new_observation":
                session.add(Entity(type="operation", text="Finished submitting the application", timestamp_start=NOW))
            else:
                session.add(Relation(source_id=ids["job"], target_id=ids["family"], relation_type="behavioral", relation_subtype="supports"))
    after = await build_context(db, now=NOW, user_note="Focus on family today" if change == "user_note" else "")
    assert before["context_id"] != after["context_id"]


@pytest.mark.asyncio
async def test_empty_or_stale_context_does_not_masquerade_as_current(db):
    empty = await build_context(db, now=NOW)
    assert empty["recent_ids"] == [] and empty["last_observed_at"] is None
    await seed_context(db)
    old = await build_context(db, now=NOW + timedelta(days=1))
    assert old["recent_ids"] == [] and old["last_observed_at"] is None
    assert old["goal_ids"]  # Background remains available with its actual timestamps.


@pytest.mark.asyncio
async def test_explicit_focus_survives_goal_budget(db, monkeypatch):
    ids = await seed_context(db)
    monkeypatch.setattr("tempo.assistant_context.MAX_GOALS", 1)
    result = await build_context(db, now=NOW, focus_goal_ids=[ids["career"]])
    assert result["goal_ids"] == result["focus_goal_ids"] == [ids["career"]]
    assert result["omitted_goal_count"] == 2
    with pytest.raises(ValueError):
        await build_context(db, now=NOW, focus_goal_ids=[ids["removed"]])


@pytest.mark.asyncio
async def test_large_neighborhood_is_bounded(db):
    ids = await seed_context(db)
    async with db.session() as session:
        for index in range(110):
            neighbor = Entity(type="activity", text=f"Connected pursuit {index}", timestamp_start=NOW - timedelta(days=1))
            session.add(neighbor)
            await session.flush()
            session.add(Relation(source_id=ids["job"], target_id=neighbor.id, relation_type="behavioral", relation_subtype="supports"))
    result = await build_context(db, now=NOW)
    assert len(result["nodes"]) <= MAX_NODES
    assert result["graph_truncated"] is True
    assert ids["new_context"] in result["recent_ids"]


@pytest.mark.asyncio
async def test_busy_latest_task_does_not_hide_earlier_tasks_or_unlinked_operations(db):
    async with db.session() as session:
        earlier = Entity(type="operation", text="Read a request to review a manuscript", timestamp_start=NOW - timedelta(minutes=18))
        stale = Entity(type="operation", text="Closed an old window", timestamp_start=NOW - timedelta(minutes=45))
        session_action = Entity(type="action", text="Drafting manuscript feedback", timestamp_start=NOW - timedelta(hours=3))
        session.add_all([earlier, stale, session_action])
        session.add_all(Entity(type="operation", text=f"Edited code section {index}", timestamp_start=NOW - timedelta(seconds=index)) for index in range(180))
        await session.flush()
        earlier_id, stale_id, action_id = earlier.id, stale.id, session_action.id
    result = await build_context(db, now=NOW)
    assert earlier_id in result["recent_ids"]  # Unlinked, before the large editing burst.
    assert action_id in result["recent_ids"]  # Broader session survives the shorter operation window.
    assert stale_id not in result["recent_ids"]
    assert len([node for node in result["nodes"] if node["type"] == "operation"]) <= 20
    assert result["graph_truncated"]


@pytest.mark.asyncio
async def test_repeated_operations_are_condensed_without_merging_different_parents(db):
    async with db.session() as session:
        tasks = [Entity(type="action", text=text, timestamp_start=NOW) for text in ["Reviewing a paper", "Editing code"]]
        session.add_all(tasks)
        await session.flush()
        for index in range(12):
            operation = Entity(type="operation", text="Clicked save", timestamp_start=NOW - timedelta(seconds=index))
            session.add(operation)
            await session.flush()
            session.add(Relation(source_id=operation.id, target_id=tasks[index % 2].id, relation_type="structural", relation_subtype="part_of"))
    result = await build_context(db, now=NOW)
    operations = [node for node in result["nodes"] if node["type"] == "operation"]
    assert len(operations) == 2
    assert [node["sampled_occurrences"] for node in operations] == [6, 6]
    assert {edge["target_id"] for edge in result["edges"]} == {task.id for task in tasks}


@pytest.mark.asyncio
async def test_onboarding_is_shared_reloaded_and_preserves_later_answers(db):
    path = Path(db.data_directory) / "onboarding.json"
    data = {"user_name": "Test Person", "responses": {"roles": "a" * 5000, "health": "Recovering from an injury", "additional_context": "A change later this year", "private_extra": "NOT_ALLOWED"}, "secret": "NOT_ALLOWED"}
    path.write_text(json.dumps(data))
    first = await build_context(db, now=NOW)
    assert "Recovering from an injury" in first["person_context"]
    assert "A change later this year" in first["person_context"]
    assert "NOT_ALLOWED" not in str(first)
    assert first["person_context_truncated"]
    assert len(first["person_context"]) <= 12000
    data["responses"]["health"] = "Returned to regular exercise"
    path.write_text(json.dumps(data))
    updated = await build_context(db, now=NOW)
    assert updated["context_id"] != first["context_id"]
    assert "Returned to regular exercise" in updated["person_context"]
    assert (await build_context(db, now=NOW, person_context=""))["person_context"] == ""


@pytest.mark.asyncio
async def test_old_user_corrections_and_goal_relationships_remain_available(db):
    ids = await seed_context(db)
    async with db.session() as session:
        correction = Entity(type="activity", text="Caring for a family member", timestamp_start=NOW - timedelta(days=100), metadata_json=json.dumps({"user_edited": True, "user_annotations": [{"type": "note", "text": "This is voluntary, not an obligation", "private": "NOT_ALLOWED"}]}))
        session.add(correction)
        await session.flush()
        correction_id = correction.id
        # Goals without recent children still have relevant relationships.
        session.add(Relation(source_id=ids["family"], target_id=ids["career"], relation_type="behavioral", relation_subtype="hinders"))
    result = await build_context(db, now=NOW)
    assert correction_id in result["correction_ids"] and correction_id not in result["recent_ids"]
    node = next(node for node in result["nodes"] if node["id"] == correction_id)
    assert node["user_annotations"][0]["text"] == "This is voluntary, not an obligation"
    assert "NOT_ALLOWED" not in str(result)
    assert any(edge["source_id"] == ids["family"] and edge["target_id"] == ids["career"] for edge in result["edges"])


@pytest.mark.asyncio
async def test_background_budget_covers_multiple_goals(db):
    async with db.session() as session:
        goals = [Entity(type="goal", text=f"Goal {index}", timestamp_start=NOW - timedelta(days=90)) for index in range(3)]
        session.add_all(goals)
        await session.flush()
        for index in range(25):
            activity = Entity(type="activity", text=f"Pursuit {index}", timestamp_start=NOW - timedelta(days=index + 1))
            session.add(activity)
            await session.flush()
            goal = goals[0] if index < 23 else goals[index - 22]
            session.add(Relation(source_id=activity.id, target_id=goal.id, relation_type="structural", relation_subtype="part_of"))
    result = await build_context(db, now=NOW)
    assert {edge["target_id"] for edge in result["edges"]} == {goal.id for goal in goals}


@pytest.mark.asyncio
async def test_feedback_updates_fingerprint_and_excludes_future_and_old_events(db):
    shown = SuggestionHistoryItem(suggestion_id="review", action="Review the draft", status="shown", updated_at=NOW - timedelta(hours=1))
    dismissed = shown.model_copy(update={"status": "dismissed", "updated_at": NOW})
    expired = shown.model_copy(update={"suggestion_id": "old", "updated_at": NOW - timedelta(days=31)})
    future = shown.model_copy(update={"suggestion_id": "future", "updated_at": NOW + timedelta(minutes=1)})
    before = await build_context(db, now=NOW, suggestion_history=[shown])
    after = await build_context(db, now=NOW, suggestion_history=[shown, dismissed, expired, future])
    assert after["context_id"] != before["context_id"]
    assert len(after["suggestion_history"]) == 1
    assert after["suggestion_history"][0]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_seed_and_text_budgets_hold_when_every_layer_is_full(db):
    async with db.session() as session:
        session.add_all(Entity(type="goal", text=f"{index} " + "goal " * 300, timestamp_start=NOW) for index in range(45))
        for kind, count in [("operation", 25), ("action", 12), ("activity", 15)]:
            session.add_all(Entity(type=kind, text=f"{index} " + kind * 1500, timestamp_start=NOW - timedelta(seconds=index), metadata_json='{"user_edited":true}') for index in range(count))
    result = await build_context(db, now=NOW)
    assert len(result["nodes"]) <= MAX_NODES
    assert len(result["edges"]) <= MAX_EDGES
    assert sum(len(node["text"]) for node in result["nodes"]) <= MAX_NODE_TEXT
    assert result["text_truncated"]
    assert result["omitted_goal_count"] == 5


def test_refresh_batches_changes_and_never_accepts_a_stale_result():
    gate = RefreshGate()
    gate.observe("a", at=0)
    assert gate.begin(at=5) is None
    gate.observe("b", at=10)
    assert gate.begin(at=39) is None
    assert gate.begin(at=40) == "b"
    gate.observe("c", at=45)
    assert not gate.finish("b")
    assert not gate.cache_is_current
    assert gate.begin(at=100) is None  # Cooldown protects against rapid repeated requests.
    assert gate.begin(at=160) == "c"
    assert gate.finish("c")
    assert gate.cache_is_current
    assert gate.begin(at=1000) is None  # Unchanged input reuses the cache.


def test_continuous_activity_cannot_postpone_updates_forever():
    gate = RefreshGate()
    for second in range(0, 90, 5):
        gate.observe(str(second), at=second)
        assert gate.begin(at=second) is None
    gate.observe("90", at=90)
    assert gate.begin(at=90) == "90"  # Maximum wait is reached despite no quiet period.


def test_explicit_refresh_can_bypass_cooldown_but_not_an_inflight_request():
    gate = RefreshGate()
    gate.observe("a", at=0)
    assert gate.begin(at=0, explicit=True) == "a"
    assert gate.begin(at=1, explicit=True) is None
    assert gate.finish("a")
    gate.observe("b", at=2)
    assert gate.begin(at=2, explicit=True) == "b"


def test_failed_request_can_retry_without_overwriting_cached_result():
    gate = RefreshGate()
    gate.observe("a", at=0)
    assert gate.begin(at=0, explicit=True) == "a"
    assert gate.finish("a")
    gate.observe("b", at=10)
    assert gate.begin(at=120) == "b"
    assert not gate.finish("b", success=False)
    assert gate.cached == "a" and not gate.cache_is_current
    assert gate.begin(at=240) == "b"

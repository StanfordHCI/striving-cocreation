"""Compile drafts: isolation from live data, edit application, accept/revert."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tempo.db import Database
from tempo.hierarchy import HierarchyService
from tempo.hierarchy_compile import (
    CompileNotFound,
    HierarchyCompiler,
    HierarchyEdits,
)
from tempo.models import EntityType, RelationSubtype, RelationType
from tempo.store import Store


NOW = datetime(2026, 9, 8, 12, 0, 0)


async def _entity(store: Store, entity_type: str, text: str):
    return await store.entities.create(
        entity_type=entity_type, text=text, timestamp_start=NOW
    )


async def _link(store: Store, child_id: int, parent_id: int):
    return await store.relations.create(
        source_id=child_id,
        target_id=parent_id,
        relation_type=RelationType.STRUCTURAL,
        relation_subtype=RelationSubtype.PART_OF,
    )


async def _seed(db: Database) -> dict[str, int]:
    async with db.session() as session:
        store = Store(session)
        goal_a = await _entity(store, EntityType.GOAL, "Stay on top of coursework")
        goal_b = await _entity(store, EntityType.GOAL, "Keep up with classes")
        activity_a = await _entity(store, EntityType.ACTIVITY, "Reading lecture notes")
        activity_b = await _entity(store, EntityType.ACTIVITY, "Watching recordings")
        action_a = await _entity(store, EntityType.ACTION, "Scrolling the slide deck")
        await _link(store, activity_a.id, goal_a.id)
        await _link(store, activity_b.id, goal_b.id)
        await _link(store, action_a.id, activity_a.id)
        return {
            "goal_a": goal_a.id,
            "goal_b": goal_b.id,
            "activity_a": activity_a.id,
            "activity_b": activity_b.id,
            "action_a": action_a.id,
        }


@pytest.fixture
def compiler(tmp_path) -> HierarchyCompiler:
    return HierarchyCompiler(data_directory=tmp_path, db_name="live.db")


async def _live_db(tmp_path) -> Database:
    database = Database(db_name="live.db", data_directory=str(tmp_path))
    await database.connect()
    return database


async def _drain(compiler: HierarchyCompiler, edits: HierarchyEdits, **kwargs):
    """Run a compile to completion, returning (events, terminal_event)."""
    events = [event async for event in compiler.compile(edits, **kwargs)]
    return events, events[-1]


# ── payload parsing ──────────────────────────────────────────────────────────


def test_payload_parsing_coerces_and_drops_malformed_entries():
    edits = HierarchyEdits.from_payload({
        "text_overrides": {"12": "  Renamed  ", "13": "   ", "bad": "x"},
        "rejected_ids": [1, "2", 2, None, "nope"],
        "goal_merges": [[3, 4], [5], [6, 6], ["7", "8"]],
        "removed_action_relations": ["9:10", "bogus", "11:12", "9:10"],
        "annotations": [
            {"entity_id": 20, "type": "why", "text": "  belongs elsewhere "},
            {"entity_id": 21, "text": "   "},
            {"text": "orphan"},
        ],
    })

    assert edits.text_overrides == {12: "Renamed"}
    assert edits.rejected_ids == [1, 2]
    assert edits.goal_merges == [(3, 4), (7, 8)]
    assert edits.removed_action_relations == ["9:10", "11:12"]
    assert edits.annotations == [
        {"entity_id": 20, "type": "why", "text": "belongs elsewhere"}
    ]
    assert not edits.is_empty()


def test_empty_payload_is_empty():
    assert HierarchyEdits.from_payload({}).is_empty()


def test_draft_path_rejects_traversal(compiler):
    with pytest.raises(CompileNotFound):
        compiler.draft_path("../../etc/passwd")
    with pytest.raises(CompileNotFound):
        compiler.draft_path("")


# ── compile ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compile_leaves_the_live_database_untouched(tmp_path, compiler):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    try:
        edits = HierarchyEdits(
            text_overrides={ids["goal_a"]: "Finish the term strong"},
            rejected_ids=[ids["goal_b"]],
        )
        _, terminal = await _drain(compiler, edits)
        assert terminal["type"] == "complete", terminal

        # Live data still reads exactly as seeded.
        async with db.session() as session:
            live = await HierarchyService(session).get_hierarchy()
        texts = {node["text"] for node in live["roots"]}
        assert texts == {"Stay on top of coursework", "Keep up with classes"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_compile_applies_edits_and_reports_a_diff(tmp_path, compiler):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    edits = HierarchyEdits(
        text_overrides={ids["goal_a"]: "Finish the term strong"},
        rejected_ids=[ids["goal_b"]],
        locked_ids=[ids["goal_a"]],
        annotations=[
            {"entity_id": ids["goal_a"], "type": "why", "text": "this is the real one"}
        ],
    )
    _, terminal = await _drain(compiler, edits)
    assert terminal["type"] == "complete"
    data = terminal["data"]

    assert data["applied"]["text_edits"] == 1
    assert data["applied"]["rejections"] == 1
    assert data["applied"]["locks"] == 1
    assert data["applied"]["annotations"] == 1
    # No provider was supplied, so nothing was re-synthesized.
    assert data["synthesized"] is False

    assert data["highlights"][str(ids["goal_a"])] == "modified"
    # Only the rejected goal disappears. Its activity is real observed
    # behaviour, so it is orphaned to the root rather than deleted — that is
    # what re-synthesis then re-homes under a surviving goal.
    assert {node["id"] for node in data["removed"]} == {ids["goal_b"]}
    orphan = next(n for n in data["modified"] if n["id"] == ids["activity_b"])
    assert orphan["previous_parent_id"] == ids["goal_b"]
    assert orphan["parent_id"] is None

    # The draft carries the edits the live database never saw.
    draft = Database(
        db_name=compiler.draft_path(data["token"]).name,
        data_directory=str(compiler.drafts_dir),
    )
    await draft.connect()
    try:
        async with draft.session() as session:
            goal = await Store(session).entities.get(ids["goal_a"])
            assert goal.text == "Finish the term strong"
            assert goal.metadata_dict["user_locked"] is True
            assert goal.metadata_dict["user_annotations"][0]["text"] == "this is the real one"
    finally:
        await draft.close()


@pytest.mark.asyncio
async def test_compile_merges_move_children_to_the_survivor(tmp_path, compiler):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    edits = HierarchyEdits(goal_merges=[(ids["goal_a"], ids["goal_b"])])
    _, terminal = await _drain(compiler, edits)
    assert terminal["type"] == "complete"
    token = terminal["data"]["token"]

    draft = Database(
        db_name=compiler.draft_path(token).name,
        data_directory=str(compiler.drafts_dir),
    )
    await draft.connect()
    try:
        async with draft.session() as session:
            tree = await HierarchyService(session).get_hierarchy()
        roots = {node["id"]: node for node in tree["roots"]}
        # The absorbed goal is gone and both activities hang off the survivor.
        assert set(roots) == {ids["goal_a"]}
        assert {child["id"] for child in roots[ids["goal_a"]]["children"]} == {
            ids["activity_a"], ids["activity_b"]
        }
    finally:
        await draft.close()


@pytest.mark.asyncio
async def test_compile_reparent_records_the_reassignment(tmp_path, compiler):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    edits = HierarchyEdits(reparents=[(ids["activity_b"], ids["goal_a"])])
    _, terminal = await _drain(compiler, edits)
    token = terminal["data"]["token"]

    draft = Database(
        db_name=compiler.draft_path(token).name,
        data_directory=str(compiler.drafts_dir),
    )
    await draft.connect()
    try:
        async with draft.session() as session:
            activity = await Store(session).entities.get(ids["activity_b"])
            assert activity.metadata_dict["user_reassigned"] is True
            assert activity.metadata_dict["reassigned_to"] == ids["goal_a"]
    finally:
        await draft.close()


@pytest.mark.asyncio
async def test_compile_runs_synthesis_when_a_provider_is_present(tmp_path, compiler, monkeypatch):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    calls: list[str] = []

    class _StubJob:
        def __init__(self, label):
            self.label = label

        def __call__(self, provider, store, **kwargs):
            calls.append(self.label)
            return self

        async def run(self, *args, **kwargs):
            return {"created": 0, "updated": 0}

    monkeypatch.setattr(
        "tempo.pipelines.action_to_activities.ActivityProposeJob", _StubJob("activities")
    )
    monkeypatch.setattr(
        "tempo.pipelines.goal_synthesis.GoalSynthesisJob", _StubJob("goals")
    )

    edits = HierarchyEdits(text_overrides={ids["goal_a"]: "Renamed"})
    _, terminal = await _drain(compiler, edits, provider=object(), user_name="Test")

    assert terminal["type"] == "complete"
    assert terminal["data"]["synthesized"] is True
    assert calls == ["activities", "goals"]


# ── accept / revert ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_promotes_the_draft_and_backs_up_the_previous_database(tmp_path, compiler):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    edits = HierarchyEdits(text_overrides={ids["goal_a"]: "Finish the term strong"})
    _, terminal = await _drain(compiler, edits)
    token = terminal["data"]["token"]

    phases: list[str] = []

    async def quiesce(phase):
        phases.append(phase)

    result = await compiler.accept(token, on_quiesce=quiesce)
    assert phases == ["before", "after"]
    assert result["accepted_at"]

    # Live database now carries the compiled text.
    live = Database(db_name="live.db", data_directory=str(tmp_path))
    await live.connect()
    try:
        async with live.session() as session:
            goal = await Store(session).entities.get(ids["goal_a"])
            assert goal.text == "Finish the term strong"
    finally:
        await live.close()

    # The pre-compile database was kept, and the draft is gone.
    assert Path(result["backup"]).exists()
    assert not compiler.draft_path(token).exists()
    assert not compiler.meta_path(token).exists()


@pytest.mark.asyncio
async def test_revert_discards_the_draft_and_keeps_live_data(tmp_path, compiler):
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    edits = HierarchyEdits(text_overrides={ids["goal_a"]: "Never applied"})
    _, terminal = await _drain(compiler, edits)
    token = terminal["data"]["token"]

    await compiler.revert(token)
    assert not compiler.draft_path(token).exists()

    live = Database(db_name="live.db", data_directory=str(tmp_path))
    await live.connect()
    try:
        async with live.session() as session:
            goal = await Store(session).entities.get(ids["goal_a"])
            assert goal.text == "Stay on top of coursework"
    finally:
        await live.close()


@pytest.mark.asyncio
async def test_accept_and_revert_reject_unknown_tokens(compiler):
    with pytest.raises(CompileNotFound):
        await compiler.accept("deadbeef")
    with pytest.raises(CompileNotFound):
        await compiler.revert("deadbeef")


# ── HTTP surface ─────────────────────────────────────────────────────────────


def _sse_events(body: str) -> list[dict]:
    """Parse the compile endpoint's SSE frames into event dicts."""
    events = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


@pytest.fixture
def api_data_dir(tmp_path, monkeypatch):
    """Point the server's compiler and read endpoints at a seeded database."""
    import tempo.server as server

    async def seed():
        db = Database(db_name="tempo.db", data_directory=str(tmp_path))
        await db.connect()
        ids = await _seed(db)
        await db.close()
        return ids

    ids = asyncio.run(seed())
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)
    monkeypatch.setattr(
        server,
        "get_query_database",
        lambda: Database(db_name="tempo.db", data_directory=str(tmp_path)),
    )
    # No model in the test environment: compile applies edits without re-synthesis.
    monkeypatch.setattr(server, "_compile_provider", lambda: None)
    return tmp_path, ids


def test_compile_endpoint_streams_progress_then_a_result(api_data_dir):
    import tempo.server as server

    _, ids = api_data_dir
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/api/hierarchy/compile",
            json={"text_overrides": {str(ids["goal_a"]): "Finish the term strong"}},
        )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert [event["type"] for event in events[:-1]] == ["progress"] * (len(events) - 1)

    terminal = events[-1]
    assert terminal["type"] == "complete"
    assert terminal["data"]["applied"]["text_edits"] == 1
    assert terminal["data"]["token"]


def test_compile_endpoint_rejects_an_empty_edit_set(api_data_dir):
    import tempo.server as server

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/hierarchy/compile", json={})
    assert response.status_code == 400
    assert "No edits" in response.json()["detail"]


def test_accept_is_refused_while_recording(api_data_dir, monkeypatch):
    import tempo.server as server

    _, ids = api_data_dir
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        compiled = client.post(
            "/api/hierarchy/compile",
            json={"text_overrides": {str(ids["goal_a"]): "Renamed"}},
        )
        token = _sse_events(compiled.text)[-1]["data"]["token"]

        # Swapping the database file underneath a live pipeline would corrupt
        # its open handle, so the endpoint refuses rather than risking it.
        monkeypatch.setitem(server.status, "running", True)
        blocked = client.post(f"/api/hierarchy/compile/{token}/accept")
        assert blocked.status_code == 409
        assert "Stop recording" in blocked.json()["detail"]

        monkeypatch.setitem(server.status, "running", False)
        accepted = client.post(f"/api/hierarchy/compile/{token}/accept")
        assert accepted.status_code == 200

        hierarchy = client.get("/api/hierarchy").json()
        assert any(node["text"] == "Renamed" for node in hierarchy["roots"])


def test_revert_endpoint_discards_the_draft(api_data_dir):
    import tempo.server as server

    _, ids = api_data_dir
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        compiled = client.post(
            "/api/hierarchy/compile",
            json={"text_overrides": {str(ids["goal_a"]): "Never applied"}},
        )
        token = _sse_events(compiled.text)[-1]["data"]["token"]

        assert client.post(f"/api/hierarchy/compile/{token}/revert").status_code == 200
        # The token is spent, and the live tree still reads as seeded.
        assert client.post(f"/api/hierarchy/compile/{token}/revert").status_code == 404

        hierarchy = client.get("/api/hierarchy").json()
        assert any(node["text"] == "Stay on top of coursework" for node in hierarchy["roots"])


def test_unknown_token_is_rejected_without_touching_the_filesystem(api_data_dir):
    import tempo.server as server

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        # Tokens are minted as hex, so anything else cannot name a draft.
        assert client.post("/api/hierarchy/compile/zzzz/revert").status_code == 404
        assert client.post("/api/hierarchy/compile/zzzz/accept").status_code == 404

        # A traversal attempt never reaches the handler: the extra segments stop
        # it matching the route at all.
        traversal = client.post("/api/hierarchy/compile/..%2F..%2Fetc/accept")
        assert traversal.status_code in (404, 405)


# ── lock enforcement ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_cannot_relabel_a_locked_goal(db):
    """A pinned goal keeps its exact text when synthesis proposes a new one.

    The editor's Pin button promises the label is kept verbatim, and
    `HierarchyService` refuses to edit a locked goal's text. Synthesis has to
    honour the same rule — the prompt asks it to, but the prompt is advisory
    and a model can ignore it, so the persist path enforces it.
    """
    from tempo.pipelines.goal_synthesis import GoalSynthesisJob
    from tempo.schemas import GoalItem

    async with db.session() as session:
        store = Store(session)
        locked = await _entity(store, EntityType.GOAL, "Finish the paper draft")
        free = await _entity(store, EntityType.GOAL, "Stay on top of coursework")
        await store.entities.update(locked.id, metadata={"user_locked": True})
        locked = await store.entities.get(locked.id)

        job = GoalSynthesisJob(provider=object(), store=store, user_name="Test")

        # Synthesis matches both goals and proposes new wording for each.
        await job._persist_goals(
            goal_items=[
                GoalItem(text="A grander restatement", activity_ids=[],
                         matches_goal_id=locked.id),
                GoalItem(text="Keep up with coursework", activity_ids=[],
                         matches_goal_id=free.id),
            ],
            existing_goals=[locked, free],
            all_activities=[],
        )

        # The pinned goal keeps its label; the unpinned one takes the new one.
        assert (await store.entities.get(locked.id)).text == "Finish the paper draft"
        assert (await store.entities.get(free.id)).text == "Keep up with coursework"

        # The constraint is also stated to the model, not only enforced after.
        block = await job._user_constraints_block()
        assert f"Goal ID:{locked.id}" in block
        assert "[locked]" in block


@pytest.mark.asyncio
async def test_compile_locks_survive_into_the_draft(tmp_path, compiler):
    """Pinning through a compile writes the flag synthesis reads."""
    db = await _live_db(tmp_path)
    ids = await _seed(db)
    await db.close()

    edits = HierarchyEdits(
        locked_ids=[ids["goal_a"]],
        annotations=[{"entity_id": ids["goal_a"], "type": "why", "text": "exactly right"}],
    )
    _, terminal = await _drain(compiler, edits)
    token = terminal["data"]["token"]

    draft = Database(
        db_name=compiler.draft_path(token).name,
        data_directory=str(compiler.drafts_dir),
    )
    await draft.connect()
    try:
        async with draft.session() as session:
            goal = await Store(session).entities.get(ids["goal_a"])
            assert goal.metadata_dict["user_locked"] is True
            assert goal.metadata_dict["user_annotations"][0]["text"] == "exactly right"
    finally:
        await draft.close()

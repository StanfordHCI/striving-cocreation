"""Transactional hierarchy editing and optimistic-conflict coverage."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import tempo.server as server
from tempo.db import Database
from tempo.hierarchy import (
    HierarchyConflict,
    HierarchyLocked,
    HierarchyService,
    HierarchyValidationError,
    SplitPart,
)
from tempo.models import EntityType, RelationSubtype, RelationType
from tempo.pipelines.activity_reconcile import ActivityReconcileJob
from tempo.pipelines.goal_reconcile import GoalReconcileJob
from tempo.store import Store


NOW = datetime(2026, 8, 20, 12, 0, 0)


async def _entity(store: Store, entity_type: str, text: str, metadata=None):
    return await store.entities.create(
        entity_type=entity_type,
        text=text,
        timestamp_start=NOW,
        metadata=metadata,
    )


async def _link(store: Store, child_id: int, parent_id: int):
    return await store.relations.create(
        source_id=child_id,
        target_id=parent_id,
        relation_type=RelationType.STRUCTURAL,
        relation_subtype=RelationSubtype.PART_OF,
    )


def test_database_managers_for_one_file_share_the_write_lock(tmp_path):
    first = Database(db_name="shared.db", data_directory=str(tmp_path))
    second = Database(db_name="shared.db", data_directory=str(tmp_path))
    other = Database(db_name="other.db", data_directory=str(tmp_path))

    assert first._write_lock is second._write_lock
    assert first._write_lock is not other._write_lock


async def _seed_tree(db: Database):
    async with db.session() as session:
        store = Store(session)
        goal_a = await _entity(store, EntityType.GOAL, "Goal A")
        goal_b = await _entity(store, EntityType.GOAL, "Goal B")
        activity_a = await _entity(store, EntityType.ACTIVITY, "Activity A")
        activity_b = await _entity(store, EntityType.ACTIVITY, "Activity B")
        action_a = await _entity(store, EntityType.ACTION, "Action A")
        action_b = await _entity(store, EntityType.ACTION, "Action B")
        operation = await _entity(store, EntityType.OPERATION, "Operation")
        await _link(store, activity_a.id, goal_a.id)
        await _link(store, activity_b.id, goal_a.id)
        await _link(store, action_a.id, activity_a.id)
        await _link(store, action_b.id, activity_b.id)
        await _link(store, operation.id, action_a.id)
        return {
            "goal_a": goal_a.id,
            "goal_b": goal_b.id,
            "activity_a": activity_a.id,
            "activity_b": activity_b.id,
            "action_a": action_a.id,
            "action_b": action_b.id,
            "operation": operation.id,
        }


@pytest.mark.asyncio
async def test_hierarchy_returns_the_full_depth_limited_tree(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        result = await HierarchyService(session).get_hierarchy(max_depth=4)

    roots = {node["id"]: node for node in result["roots"]}
    assert set(roots) == {ids["goal_a"], ids["goal_b"]}
    goal_a = roots[ids["goal_a"]]
    activity_a = next(n for n in goal_a["children"] if n["id"] == ids["activity_a"])
    action_a = activity_a["children"][0]
    assert action_a["children"][0]["id"] == ids["operation"]
    assert result["count"] == 7


@pytest.mark.asyncio
async def test_update_marks_user_edit_and_rejects_a_stale_revision(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        service = HierarchyService(session)
        updated = await service.update_entity(
            ids["goal_a"], expected_revision=0, text="Renamed goal"
        )
    assert updated["entity"]["text"] == "Renamed goal"
    assert updated["entity"]["revision"] == 1
    assert updated["entity"]["metadata"]["user_edited"] is True
    assert updated["entity"]["metadata"]["original_text"] == "Goal A"

    with pytest.raises(HierarchyConflict):
        async with db.session() as session:
            await HierarchyService(session).update_entity(
                ids["goal_a"], expected_revision=0, text="Stale rename"
            )


@pytest.mark.asyncio
async def test_pipeline_updates_also_advance_the_conflict_revision(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        store = Store(session)
        goal = await store.entities.get(ids["goal_a"])
        await store.entities.update(goal.id, metadata={"pipeline_update": True})

    with pytest.raises(HierarchyConflict):
        async with db.session() as session:
            await HierarchyService(session).update_entity(
                ids["goal_a"], expected_revision=0, text="Stale client edit"
            )


@pytest.mark.asyncio
async def test_locked_nodes_must_be_unlocked_before_structural_edits(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        locked = await HierarchyService(session).update_entity(
            ids["activity_a"], expected_revision=0, locked=True
        )
    assert locked["entity"]["revision"] == 1
    assert locked["entity"]["locked"] is True

    with pytest.raises(HierarchyLocked):
        async with db.session() as session:
            await HierarchyService(session).reparent_entity(
                ids["activity_a"],
                new_parent_id=ids["goal_b"],
                expected_revision=1,
            )

    async with db.session() as session:
        unlocked = await HierarchyService(session).update_entity(
            ids["activity_a"], expected_revision=1, locked=False
        )
    assert unlocked["entity"]["revision"] == 2
    assert unlocked["entity"]["locked"] is False


@pytest.mark.asyncio
async def test_reparent_replaces_the_old_edge_and_validates_tiers_and_cycles(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        result = await HierarchyService(session).reparent_entity(
            ids["activity_a"],
            new_parent_id=ids["goal_b"],
            expected_revision=0,
        )
    assert result["entity"]["metadata"]["user_reassigned"] is True

    async with db.session() as session:
        store = Store(session)
        parents = await store.relations.get_by_source(
            ids["activity_a"], RelationType.STRUCTURAL, RelationSubtype.PART_OF
        )
        assert [rel.target_id for rel in parents] == [ids["goal_b"]]

    with pytest.raises(HierarchyValidationError):
        async with db.session() as session:
            await HierarchyService(session).reparent_entity(
                ids["action_a"],
                new_parent_id=ids["goal_a"],
                expected_revision=0,
            )

    # Corrupt edge makes the otherwise tier-valid move cyclic.
    async with db.session() as session:
        store = Store(session)
        await _link(store, ids["activity_b"], ids["action_a"])
    with pytest.raises(HierarchyValidationError, match="cycle"):
        async with db.session() as session:
            await HierarchyService(session).reparent_entity(
                ids["action_a"],
                new_parent_id=ids["activity_b"],
                expected_revision=0,
            )


@pytest.mark.asyncio
async def test_failed_mutation_rolls_back_relation_changes(db, monkeypatch):
    ids = await _seed_tree(db)
    with pytest.raises(RuntimeError, match="injected failure"):
        async with db.session() as session:
            service = HierarchyService(session)
            monkeypatch.setattr(
                service.store.relations,
                "create",
                AsyncMock(side_effect=RuntimeError("injected failure")),
            )
            await service.reparent_entity(
                ids["activity_a"],
                new_parent_id=ids["goal_b"],
                expected_revision=0,
            )

    async with db.session() as session:
        store = Store(session)
        parents = await store.relations.get_by_source(
            ids["activity_a"], RelationType.STRUCTURAL, RelationSubtype.PART_OF
        )
        assert [relation.target_id for relation in parents] == [ids["goal_a"]]
        assert (await store.entities.get(ids["activity_a"])).metadata_dict["revision"] == 0


@pytest.mark.asyncio
async def test_merge_rewires_children_and_deletes_the_merged_entities(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        result = await HierarchyService(session).merge_entities(
            ids=[ids["activity_a"], ids["activity_b"]],
            text="Combined activity",
            expected_revisions={ids["activity_a"]: 0, ids["activity_b"]: 0},
        )
    assert result["entity"]["id"] == ids["activity_a"]
    assert result["entity"]["text"] == "Combined activity"
    assert {n["id"] for n in result["entity"]["children"]} == {
        ids["action_a"], ids["action_b"]
    }

    async with db.session() as session:
        store = Store(session)
        assert await store.entities.get(ids["activity_b"]) is None
        moved_action = await store.entities.get(ids["action_b"])
        assert moved_action.metadata_dict["user_reassigned"] is True
        assert moved_action.metadata_dict["reassigned_to"] == ids["activity_a"]


@pytest.mark.asyncio
async def test_split_is_atomic_and_requires_an_exact_child_partition(db):
    ids = await _seed_tree(db)
    with pytest.raises(HierarchyValidationError):
        async with db.session() as session:
            await HierarchyService(session).split_entity(
                ids["goal_a"],
                expected_revision=0,
                into=[
                    SplitPart(text="One", child_ids=[ids["activity_a"]]),
                    SplitPart(text="Two", child_ids=[]),
                ],
            )

    async with db.session() as session:
        assert await Store(session).entities.get(ids["goal_a"]) is not None

    async with db.session() as session:
        result = await HierarchyService(session).split_entity(
            ids["goal_a"],
            expected_revision=0,
            into=[
                SplitPart(text="Goal one", child_ids=[ids["activity_a"]]),
                SplitPart(text="Goal two", child_ids=[ids["activity_b"]]),
            ],
        )
    assert len(result["entities"]) == 2
    assert {node["text"] for node in result["entities"]} == {"Goal one", "Goal two"}

    async with db.session() as session:
        assert await Store(session).entities.get(ids["goal_a"]) is None


@pytest.mark.asyncio
async def test_delete_can_reparent_children_without_orphaning_them(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        result = await HierarchyService(session).delete_entity(
            ids["activity_a"],
            expected_revision=0,
            reparent_children_to=ids["activity_b"],
        )
    assert result["deleted"]["id"] == ids["activity_a"]

    async with db.session() as session:
        store = Store(session)
        assert await store.entities.get(ids["activity_a"]) is None
        parents = await store.relations.get_by_source(
            ids["action_a"], RelationType.STRUCTURAL, RelationSubtype.PART_OF
        )
        assert [rel.target_id for rel in parents] == [ids["activity_b"]]
        action = await store.entities.get(ids["action_a"])
        assert action.metadata_dict["user_edited"] is True


@pytest.mark.asyncio
async def test_create_user_authored_node_with_a_legal_parent(db):
    ids = await _seed_tree(db)
    async with db.session() as session:
        result = await HierarchyService(session).create_entity(
            entity_type=EntityType.ACTIVITY,
            text="User-created activity",
            parent_id=ids["goal_a"],
        )
    assert result["entity"]["metadata"]["user_provided"] is True
    assert result["entity"]["revision"] == 0


@pytest.mark.asyncio
async def test_reconcile_never_merges_away_user_edited_entities(db):
    async with db.session() as session:
        store = Store(session)
        edited_goal = await _entity(
            store, EntityType.GOAL, "Edited goal", {"user_edited": True}
        )
        other_goal = await _entity(store, EntityType.GOAL, "Other goal")
        edited_activity = await _entity(
            store, EntityType.ACTIVITY, "Edited activity", {"user_edited": True}
        )
        other_activity = await _entity(store, EntityType.ACTIVITY, "Other activity")

        await GoalReconcileJob(None, store)._merge_goal_into_target(
            edited_goal.id, other_goal.id
        )
        await ActivityReconcileJob(None, store)._merge_goal_into_target(
            edited_activity.id, other_activity.id
        )
        assert await store.entities.get(edited_goal.id) is not None
        assert await store.entities.get(edited_activity.id) is not None


def test_http_mutation_persists_and_emits_a_hierarchy_event_afterward(tmp_path, monkeypatch):
    data_dir = str(tmp_path)

    async def seed():
        db = Database(db_name="api.db", data_directory=data_dir)
        await db.connect()
        async with db.session() as session:
            goal = await _entity(Store(session), EntityType.GOAL, "Before")
            goal_id = goal.id
        await db.close()
        return goal_id

    import asyncio
    goal_id = asyncio.run(seed())
    monkeypatch.setattr(
        server,
        "get_query_database",
        lambda: Database(db_name="api.db", data_directory=data_dir),
    )
    async def assert_committed(action, data):
        check_db = Database(db_name="api.db", data_directory=data_dir)
        await check_db.connect()
        async with check_db.session() as session:
            persisted = await Store(session).entities.get(goal_id)
            assert persisted.text == "After"
            assert persisted.metadata_dict["revision"] == 1
        await check_db.close()
        assert action == "update"
        assert data["entity"]["text"] == "After"

    emitted = AsyncMock(side_effect=assert_committed)
    monkeypatch.setattr(server, "broadcast_hierarchy_update", emitted)

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        response = client.patch(
            f"/api/entity/{goal_id}",
            json={"text": "After", "expected_revision": 0},
        )

    assert response.status_code == 200
    assert response.json()["entity"]["text"] == "After"
    emitted.assert_awaited_once()

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        stale = client.patch(
            f"/api/entity/{goal_id}",
            json={"text": "Stale", "expected_revision": 0},
        )
    assert stale.status_code == 409
    emitted.assert_awaited_once()

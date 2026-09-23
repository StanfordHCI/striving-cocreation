"""Tests for Stage 2: Operations -> Actions (ActionBuilder).

Covers:
1. ActionBuilder.build_actions() creates action entities from buffered operations
2. Structural relations: operations linked to actions via PART_OF
3. Temporal relations: FOLLOWS created between sequential actions
4. CO_OCCURS relations created between actions in same batch
5. Empty operations list returns empty action_ids
6. Cross-batch chaining: new actions chain to existing actions via FOLLOWS
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from tempo.buffer import BufferedOperation
from tempo.models import EntityType, RelationType, RelationSubtype
from tempo.pipelines.operations_to_actions import ActionBuilder
from tempo.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OPERATION_TEXTS = [
    "Opened VS Code editor",
    "Navigated to server.py file",
    "Edited line 42 in server.py",
    "Saved the file",
    "Switched to terminal panel",
    "Ran pytest command",
    "Reviewed test output - 3 tests passing",
    "Switched to Chrome browser",
    "Opened GitHub Pull Request #42",
    "Clicked 'Review changes' button",
]

BASE_TIME = datetime(2026, 1, 15, 14, 30, 0)
INTERVAL = 30  # seconds


async def _seed_operations(db, texts=OPERATION_TEXTS, base_time=BASE_TIME):
    """Create operation entities in the DB, return BufferedOperation list."""
    ops = []
    async with db.session() as session:
        store = Store(session)
        for i, text in enumerate(texts):
            ts = base_time + timedelta(seconds=i * INTERVAL)
            entity = await store.create_operation(
                text=text,
                timestamp=ts,
                metadata={"confidence": "8", "decay": "3"},
            )
            ops.append(BufferedOperation(
                entity_id=entity.id,
                text=text,
                timestamp=ts,
            ))
    return ops


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_actions_creates_entities(db, replay_provider):
    """build_actions() creates action entities from buffered operations."""
    ops = await _seed_operations(db)
    provider = replay_provider("stage2_ops_to_actions")

    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions(ops)

    # The fixture produces 2 segments -> 2 actions
    assert len(action_ids) >= 2

    # Verify actions exist in DB with correct type
    async with db.session() as session:
        store = Store(session)
        for aid in action_ids:
            entity = await store.entities.get(aid)
            assert entity is not None
            assert entity.type == EntityType.ACTION
            assert len(entity.text) > 0
            assert entity.timestamp_start is not None


@pytest.mark.asyncio
async def test_structural_relations_part_of(db, replay_provider):
    """Operations are linked to their parent actions via PART_OF relations."""
    ops = await _seed_operations(db)
    provider = replay_provider("stage2_ops_to_actions")

    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions(ops)

    # Check that every action has incoming PART_OF relations from operations
    async with db.session() as session:
        store = Store(session)
        op_ids = {op.entity_id for op in ops}

        for aid in action_ids:
            part_of_rels = await store.relations.get_by_target(
                target_id=aid,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            # Each action should have at least one operation linked
            assert len(part_of_rels) >= 1, (
                f"Action {aid} has no PART_OF relations from operations"
            )
            # All sources should be valid operation IDs
            for rel in part_of_rels:
                assert rel.source_id in op_ids


@pytest.mark.asyncio
async def test_temporal_follows_between_actions(db, replay_provider):
    """Sequential actions are linked via FOLLOWS temporal relations."""
    ops = await _seed_operations(db)
    provider = replay_provider("stage2_ops_to_actions")

    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions(ops)

    assert len(action_ids) >= 2

    # Check FOLLOWS relation: first action -> second action
    async with db.session() as session:
        store = Store(session)
        first_id = action_ids[0]
        second_id = action_ids[1]

        follows_rels = await store.relations.get_by_source(
            source_id=first_id,
            relation_type=RelationType.TEMPORAL,
            relation_subtype=RelationSubtype.FOLLOWS,
        )
        targets = [r.target_id for r in follows_rels]
        assert second_id in targets, (
            f"Expected FOLLOWS from action {first_id} to {second_id}, "
            f"found targets {targets}"
        )


@pytest.mark.asyncio
async def test_co_occurs_between_batch_actions(db, replay_provider):
    """Actions in the same batch have CO_OCCURS relations."""
    ops = await _seed_operations(db)
    provider = replay_provider("stage2_ops_to_actions")

    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions(ops)

    assert len(action_ids) >= 2

    # All pairs should have CO_OCCURS
    async with db.session() as session:
        store = Store(session)
        for i in range(len(action_ids)):
            for j in range(i + 1, len(action_ids)):
                id_a, id_b = action_ids[i], action_ids[j]
                co_occurs = await store.relations.get_by_source(
                    source_id=id_a,
                    relation_type=RelationType.TEMPORAL,
                    relation_subtype=RelationSubtype.CO_OCCURS,
                )
                targets = [r.target_id for r in co_occurs]
                assert id_b in targets, (
                    f"Expected CO_OCCURS between action {id_a} and {id_b}"
                )


@pytest.mark.asyncio
async def test_empty_operations_returns_empty(db, replay_provider):
    """build_actions() with empty list returns empty action_ids."""
    provider = replay_provider("stage2_ops_to_actions")

    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions([])

    assert action_ids == []


@pytest.mark.asyncio
async def test_cross_batch_chaining(db, replay_provider):
    """New actions chain to existing actions from a prior batch via FOLLOWS."""
    # Phase 1: Create a pre-existing action in the DB to simulate a prior batch
    existing_action_time = BASE_TIME - timedelta(minutes=5)
    async with db.session() as session:
        store = Store(session)
        existing_action = await store.create_action(
            text="Previously completed code review",
            timestamp_start=existing_action_time,
            timestamp_end=existing_action_time + timedelta(minutes=3),
            operation_ids=[],
        )
        existing_action_id = existing_action.id

    # Phase 2: Seed operations and run build_actions
    ops = await _seed_operations(db)
    provider = replay_provider("stage2_ops_to_actions")

    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions(ops, enable_cross_batch_chain=True)

    assert len(action_ids) >= 1

    # Phase 3: Verify the existing action chains to the first new action via FOLLOWS
    async with db.session() as session:
        store = Store(session)
        first_new_action_id = action_ids[0]

        follows_rels = await store.relations.get_by_source(
            source_id=existing_action_id,
            relation_type=RelationType.TEMPORAL,
            relation_subtype=RelationSubtype.FOLLOWS,
        )
        targets = [r.target_id for r in follows_rels]
        assert first_new_action_id in targets, (
            f"Expected cross-batch FOLLOWS from existing action {existing_action_id} "
            f"to first new action {first_new_action_id}, found targets {targets}"
        )

        # The second new action should NOT have a direct FOLLOWS from the existing action
        if len(action_ids) >= 2:
            second_new_action_id = action_ids[1]
            assert second_new_action_id not in targets, (
                "Existing action should only chain to the FIRST new action, not subsequent ones"
            )

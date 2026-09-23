"""Tests for Stage 3: Actions → Activities (ActivityProposeJob).

Covers:
1. ActivityProposeJob.run() creates activity entities from actions using replay provider
2. Behavioral relations (SUPPORTS/HINDERS) are created
3. Actions are linked to activities via structural relations
4. Empty actions list returns early with no entities created
5. Result dict has expected keys
"""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta

from tempo.models import EntityType, RelationType, RelationSubtype
from tempo.pipelines.action_to_activities import ActivityProposeJob
from tempo.store import Store


# ---------------------------------------------------------------------------
# Action texts and timing — must match record_fixtures.py exactly
# ---------------------------------------------------------------------------

ACTION_TEXTS = [
    "Editing server.py to add new API endpoint",
    "Running pytest to verify changes",
    "Reviewing Pull Request #42 on GitHub",
    "Responding to code review comments",
    "Checking Slack messages from team",
]

BASE_TIME = datetime(2026, 1, 15, 14, 0, 0)
INTERVAL_MINUTES = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_actions(db):
    """Create the 5 canonical actions in their own session block and return their IDs."""
    action_ids = []
    async with db.session() as session:
        store = Store(session)
        for i, text in enumerate(ACTION_TEXTS):
            start = BASE_TIME + timedelta(minutes=i * INTERVAL_MINUTES)
            end = start + timedelta(minutes=14)
            action = await store.create_action(
                text=text,
                timestamp_start=start,
                timestamp_end=end,
                operation_ids=[],
            )
            action_ids.append(action.id)
    return action_ids


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_creates_activities(db, replay_provider):
    """ActivityProposeJob.run() creates activity entities from actions."""
    action_ids = await _seed_actions(db)

    async with db.session() as session:
        store = Store(session)
        provider = replay_provider("stage3_actions_to_activities")
        job = ActivityProposeJob(provider=provider, store=store)
        result = await job.run(action_ids=action_ids)

    # Verify at least one activity was created
    async with db.session() as session:
        store = Store(session)
        activities = await store.entities.get_by_type(EntityType.ACTIVITY)

    assert len(activities) >= 1, "Expected at least one activity to be created"
    # Activity text should be non-empty
    for act in activities:
        assert act.text, "Activity text should not be empty"


@pytest.mark.asyncio
async def test_behavioral_relations_created(db, replay_provider):
    """SUPPORTS and/or HINDERS behavioral relations are created."""
    action_ids = await _seed_actions(db)

    async with db.session() as session:
        store = Store(session)
        provider = replay_provider("stage3_actions_to_activities")
        job = ActivityProposeJob(provider=provider, store=store)
        result = await job.run(action_ids=action_ids)

    supports_count = result.get("supports_created", 0)
    hinders_count = result.get("hinders_created", 0)

    # The fixture has all "supports" valences, so we expect supports > 0
    assert supports_count > 0, "Expected at least one SUPPORTS behavioral relation"

    # Verify behavioral relations exist in the database
    async with db.session() as session:
        store = Store(session)
        activity_ids = result["created_activity_ids"]
        behavioral_rels = []
        for aid in activity_ids:
            rels = await store.relations.get_by_target(
                aid,
                relation_type=RelationType.BEHAVIORAL,
            )
            behavioral_rels.extend(rels)

    assert len(behavioral_rels) > 0, "Expected behavioral relations in database"
    subtypes = {r.relation_subtype for r in behavioral_rels}
    assert RelationSubtype.SUPPORTS in subtypes, "Expected SUPPORTS relations"


@pytest.mark.asyncio
async def test_actions_linked_via_structural_relations(db, replay_provider):
    """Actions are linked to activities via STRUCTURAL/PART_OF relations."""
    action_ids = await _seed_actions(db)

    async with db.session() as session:
        store = Store(session)
        provider = replay_provider("stage3_actions_to_activities")
        job = ActivityProposeJob(provider=provider, store=store)
        result = await job.run(action_ids=action_ids)

    # Check structural relations: action → activity
    async with db.session() as session:
        store = Store(session)
        linked_action_ids = set()
        for action_id in action_ids:
            rels = await store.relations.get_by_source(
                action_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            # Filter to only relations pointing at activities
            for rel in rels:
                target = await store.entities.get(rel.target_id)
                if target and target.type == EntityType.ACTIVITY:
                    linked_action_ids.add(action_id)

    # All 5 actions should be linked to an activity (fixture assigns all to C1)
    assert linked_action_ids == set(action_ids), (
        f"Expected all actions linked to activities, got {linked_action_ids}"
    )


@pytest.mark.asyncio
async def test_empty_actions_returns_early(db, replay_provider):
    """Empty actions list returns early with no entities created."""
    # Do NOT seed any actions — pass an empty list
    async with db.session() as session:
        store = Store(session)
        provider = replay_provider("stage3_actions_to_activities")
        job = ActivityProposeJob(provider=provider, store=store)
        result = await job.run(action_ids=[])

    assert result["selected"] == 0
    assert result["created"] == 0
    assert result["reassigned"] == 0
    assert result["created_activity_ids"] == []

    # Confirm no activities were created
    async with db.session() as session:
        store = Store(session)
        activities = await store.entities.get_by_type(EntityType.ACTIVITY)

    assert len(activities) == 0, "No activities should exist after empty run"


@pytest.mark.asyncio
async def test_result_dict_has_expected_keys(db, replay_provider):
    """Result dict has expected keys: selected, created, reassigned, created_activity_ids."""
    action_ids = await _seed_actions(db)

    async with db.session() as session:
        store = Store(session)
        provider = replay_provider("stage3_actions_to_activities")
        job = ActivityProposeJob(provider=provider, store=store)
        result = await job.run(action_ids=action_ids)

    expected_keys = {"selected", "created", "reassigned", "created_activity_ids"}
    assert expected_keys.issubset(result.keys()), (
        f"Missing keys: {expected_keys - result.keys()}"
    )

    # Type checks
    assert isinstance(result["selected"], int)
    assert isinstance(result["created"], int)
    assert isinstance(result["reassigned"], int)
    assert isinstance(result["created_activity_ids"], list)

    # The fixture proposes 1 candidate with all 5 actions
    assert result["selected"] >= 1
    assert result["created"] >= 1
    assert len(result["created_activity_ids"]) >= 1
    # Each created_activity_id should be an integer
    for aid in result["created_activity_ids"]:
        assert isinstance(aid, int)

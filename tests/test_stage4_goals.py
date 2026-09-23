"""Tests for Stage 4: Activities → Goals (GoalSynthesisJob)."""

import pytest
import pytest_asyncio

from tempo.models import EntityType
from tempo.pipelines.goal_synthesis import GoalSynthesisJob
from tempo.store import Store


ACTIVITY_TEXTS = [
    "Software development - working on API features",
    "Code review and collaboration",
    "Team communication via Slack",
    "Research and documentation",
]


@pytest_asyncio.fixture
async def seeded_db(db):
    """Seed activities into the database matching the recorded fixture prompts."""
    async with db.session() as session:
        store = Store(session)
        for text in ACTIVITY_TEXTS:
            await store.create_activity(
                text=text,
                action_ids=[],
                metadata={"status": "active"},
            )
    return db


@pytest.mark.asyncio
async def test_goal_synthesis_creates_goals(seeded_db, replay_provider):
    """GoalSynthesisJob.run() creates goal entities from activities using replay provider."""
    provider = replay_provider("stage4_goal_synthesis")

    async with seeded_db.session() as session:
        store = Store(session)
        job = GoalSynthesisJob(provider=provider, store=store, enable_self_refine=True)
        result = await job.run()

    assert result["created"] > 0
    assert result["total"] > 0


@pytest.mark.asyncio
async def test_goal_synthesis_result_keys(seeded_db, replay_provider):
    """Result dict has expected keys (created, updated, total)."""
    provider = replay_provider("stage4_goal_synthesis")

    async with seeded_db.session() as session:
        store = Store(session)
        job = GoalSynthesisJob(provider=provider, store=store, enable_self_refine=True)
        result = await job.run()

    assert "created" in result
    assert "updated" in result
    assert "total" in result
    assert isinstance(result["created"], int)
    assert isinstance(result["updated"], int)
    assert isinstance(result["total"], int)


@pytest.mark.asyncio
async def test_goal_synthesis_no_activities_returns_early(db, replay_provider):
    """No activities returns early gracefully with zeroed counts."""
    provider = replay_provider("stage4_goal_synthesis")

    async with db.session() as session:
        store = Store(session)
        job = GoalSynthesisJob(provider=provider, store=store, enable_self_refine=True)
        result = await job.run()

    assert result == {"created": 0, "updated": 0, "total": 0}


@pytest.mark.asyncio
async def test_goals_persisted_in_database(seeded_db, replay_provider):
    """Goals are actually persisted in the database after run."""
    provider = replay_provider("stage4_goal_synthesis")

    async with seeded_db.session() as session:
        store = Store(session)
        job = GoalSynthesisJob(provider=provider, store=store, enable_self_refine=True)
        result = await job.run()

    # Verify goals exist in a fresh session
    async with seeded_db.session() as session:
        store = Store(session)
        goals = await store.entities.get_by_type(EntityType.GOAL)

    assert len(goals) > 0
    assert len(goals) == result["total"]
    for goal in goals:
        assert goal.type == EntityType.GOAL
        assert len(goal.text) > 0

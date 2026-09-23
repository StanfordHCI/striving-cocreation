"""Detaching an action from an activity has to survive re-synthesis.

Removing an action from an activity is a judgment about that pairing, not about
the action — the action stays in the graph and may still join other activities.
But a detached action reads as *unassigned*, which is exactly what the propose
job targets, so without an explicit guard the next pipeline run re-attaches it
to the activity it was just removed from and the user's edit silently vanishes.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tempo.models import EntityType, RelationSubtype, RelationType
from tempo.pipelines.action_to_activities import ActivityProposeJob
from tempo.store import Store


NOW = datetime(2026, 9, 10, 12, 0, 0)


async def _seed(store: Store):
    activity = await store.entities.create(
        entity_type=EntityType.ACTIVITY, text="Writing in Overleaf", timestamp_start=NOW,
    )
    other = await store.entities.create(
        entity_type=EntityType.ACTIVITY, text="Reading related work", timestamp_start=NOW,
    )
    action = await store.entities.create(
        entity_type=EntityType.ACTION, text="Editing the methods section", timestamp_start=NOW,
    )
    return activity, other, action


async def _links(store: Store, action_id: int) -> set[int]:
    relations = await store.relations.get_by_source(
        action_id,
        relation_type=RelationType.STRUCTURAL,
        relation_subtype=RelationSubtype.PART_OF,
    )
    return {relation.target_id for relation in relations or []}


@pytest.mark.asyncio
async def test_detached_action_is_not_relinked_to_that_activity(db):
    async with db.session() as session:
        store = Store(session)
        activity, _, action = await _seed(store)
        await store.entities.update(
            action.id, metadata={"removed_from_activities": [activity.id]}
        )

        job = ActivityProposeJob(provider=object(), store=store)
        created = await job._link_action(action.id, activity.id)

        assert created is False
        assert activity.id not in await _links(store, action.id)


@pytest.mark.asyncio
async def test_detachment_is_scoped_to_that_one_activity(db):
    """The action is still free to belong somewhere else — it was not deleted."""
    async with db.session() as session:
        store = Store(session)
        activity, other, action = await _seed(store)
        await store.entities.update(
            action.id, metadata={"removed_from_activities": [activity.id]}
        )

        job = ActivityProposeJob(provider=object(), store=store)
        assert await job._link_action(action.id, other.id) is True
        assert await _links(store, action.id) == {other.id}


@pytest.mark.asyncio
async def test_actions_without_a_detachment_link_normally(db):
    async with db.session() as session:
        store = Store(session)
        activity, _, action = await _seed(store)

        job = ActivityProposeJob(provider=object(), store=store)
        assert await job._link_action(action.id, activity.id) is True
        assert activity.id in await _links(store, action.id)


@pytest.mark.asyncio
async def test_detachments_are_stated_in_the_prompt_constraints(db):
    """The model is told, as well as being enforced after the fact."""
    async with db.session() as session:
        store = Store(session)
        activity, _, action = await _seed(store)
        await store.entities.update(
            action.id, metadata={"removed_from_activities": [activity.id]}
        )

        job = ActivityProposeJob(provider=object(), store=store, user_name="Test")
        block = await job._get_user_constraints_block()

        assert f"Action ID:{action.id}" in block
        assert f"Activity ID:{activity.id}" in block
        assert "[detached]" in block

"""Release gate 6: one end-to-end pass over the whole product.

start -> observe -> all four pipeline stages -> edit hierarchy -> restart -> query,
against a temporary database and cache directory.

The recorded stage fixtures cannot be chained directly: they hard-code entity IDs
(stage 3 references ``action_ids [1..5]``, stage 4 references ``activity_ids
[1..4]``) that only line up because each unit test seeds exactly those rows. Here
the IDs are whatever the previous stage actually created, so the provider builds
each response against the live IDs instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

import pytest

from tempo.api import Tempo
from tempo.buffer import BufferedOperation
from tempo.db import Database
from tempo.hierarchy import HierarchyConflict, HierarchyService
from tempo.models import EntityType
from tempo.pipelines.action_to_activities import ActivityProposeJob
from tempo.pipelines.goal_synthesis import GoalSynthesisJob
from tempo.pipelines.observation_to_operation import ObservationAdapter
from tempo.pipelines.operations_to_actions import ActionBuilder
from tempo.providers import ModelProvider
from tempo.queries import QueryInterface
from tempo.store import Store


OBSERVATION = (
    "The user's screen shows VS Code with server.py open, a terminal running "
    "pytest, and Chrome on a GitHub pull request."
)
OBSERVED_AT = datetime(2026, 1, 15, 14, 35, 0)
DB_NAME = "tempo.db"


class ScriptedProvider(ModelProvider):
    """Returns a stage-appropriate response built from the live entity IDs.

    The caller sets ``stage`` before driving each pipeline stage, so no prompt
    sniffing is involved and a stage that stops calling the model is visible as a
    missing entry in ``calls``.
    """

    def __init__(self) -> None:
        super().__init__(model="scripted", api_key=None, api_base=None)
        self.stage: str | None = None
        self.payloads: dict[str, Callable[[], str]] = {}
        self.calls: list[str] = []

    async def chat_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        if self.stage is None or self.stage not in self.payloads:
            raise AssertionError(f"Unexpected model call for stage {self.stage!r}")
        self.calls.append(self.stage)
        return self.payloads[self.stage]()

    async def vision_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return await self.chat_completion(messages, **kwargs)


def _operations_payload() -> str:
    return json.dumps({
        "operations": [
            {
                "text": "Edited FastAPI routes in server.py",
                "confidence": 9,
                "decay": 2,
                "context": {"app": "VS Code", "tool_kind": "editor"},
            },
            {
                "text": "Ran pytest and reviewed passing output",
                "confidence": 9,
                "decay": 2,
                "context": {"app": "Terminal", "tool_kind": "terminal"},
            },
            {
                "text": "Reviewed GitHub pull request 42",
                "confidence": 7,
                "decay": 4,
                "context": {"app": "Chrome", "tool_kind": "browser"},
            },
            {
                "text": "Replied to a review comment",
                "confidence": 8,
                "decay": 3,
                "context": {"app": "Chrome", "tool_kind": "browser"},
            },
        ]
    })


def _segments_payload(operations: list[BufferedOperation]) -> str:
    """Split the operations into two contiguous, 1-indexed segments."""
    midpoint = max(1, len(operations) // 2)
    spans = [(1, midpoint), (midpoint + 1, len(operations))]
    labels = ["Implemented and tested the server API change", "Reviewed pull request 42"]

    segments = []
    for index, ((start, end), text) in enumerate(zip(spans, labels), start=1):
        if start > end:
            continue
        segments.append({
            "label": f"S{index}",
            "start_index": start,
            "end_index": end,
            "action": {
                "text": text,
                "timestamp_start": operations[start - 1].timestamp.isoformat(),
                "timestamp_end": operations[end - 1].timestamp.isoformat(),
                "confidence": 9,
                "decay": 2,
            },
        })
    return json.dumps({"segments": segments})


def _candidates_payload(action_ids: list[int]) -> str:
    return json.dumps({
        "candidates": [{
            "candidate_id": "C1",
            "description": "Developing and collaboratively shipping the server API",
            "action_ids": action_ids,
            "action_valences": ["supports"] * len(action_ids),
            "reasoning": "Every action advances the same software change.",
            "confidence": 9,
            "purpose": "Ship a reliable API change",
            "people": ["engineering team"],
            "resources": ["VS Code", "pytest", "GitHub"],
        }]
    })


def _goals_payload(activity_ids: list[int]) -> str:
    return json.dumps({
        "goals": [{
            "text": "the user is building reliable software while collaborating effectively",
            "activity_ids": activity_ids,
            "reasoning": "Development and review form one sustained objective.",
            "confidence": 9,
            "needs": ["competence", "relatedness"],
            "orientation": "approach",
            "autonomy": "autonomous",
        }],
        "goal_relations": [],
        "dropped_goals": [],
    })


async def _ids_of_type(database: Database, entity_type: str) -> list[int]:
    async with database.session() as session:
        entities = await Store(session).entities.get_by_type(entity_type)
        return [entity.id for entity in entities]


@pytest.mark.asyncio
async def test_record_edit_restart_and_query(tmp_path):
    data_dir = str(tmp_path)
    provider = ScriptedProvider()

    # ---- Session 1: observe and run all four stages -----------------------
    database = Database(db_name=DB_NAME, data_directory=data_dir)
    await database.connect()
    try:
        # Stage 1: observation -> operations
        provider.stage = "operations"
        provider.payloads["operations"] = _operations_payload
        async with database.session() as session:
            adapter = ObservationAdapter(provider=provider, store=Store(session))
            operation_ids = await adapter.process_observation(
                OBSERVATION, observation_timestamp=OBSERVED_AT
            )
        assert len(operation_ids) == 4

        buffered = [
            BufferedOperation(
                entity_id=entity_id,
                text=f"operation {entity_id}",
                timestamp=OBSERVED_AT + timedelta(seconds=30 * index),
            )
            for index, entity_id in enumerate(operation_ids)
        ]

        # Stage 2: operations -> actions
        provider.stage = "actions"
        provider.payloads["actions"] = lambda: _segments_payload(buffered)
        async with database.session() as session:
            builder = ActionBuilder(provider=provider, store=Store(session))
            action_ids = await builder.build_actions(buffered)
        assert len(action_ids) >= 1

        # Stage 3: actions -> activities
        provider.stage = "activities"
        provider.payloads["activities"] = lambda: _candidates_payload(action_ids)
        async with database.session() as session:
            job = ActivityProposeJob(provider=provider, store=Store(session))
            await job.run(action_ids=action_ids)
        activity_ids = await _ids_of_type(database, EntityType.ACTIVITY)
        assert activity_ids, "stage 3 produced no activities"

        # Stage 4: activities -> goals
        provider.stage = "goals"
        provider.payloads["goals"] = lambda: _goals_payload(activity_ids)
        async with database.session() as session:
            synthesis = GoalSynthesisJob(
                provider=provider, store=Store(session), enable_self_refine=False
            )
            await synthesis.run()
        goal_ids = await _ids_of_type(database, EntityType.GOAL)
        assert goal_ids, "stage 4 produced no goals"

        # Every stage actually reached the model.
        assert set(provider.calls) == {"operations", "actions", "activities", "goals"}

        # ---- Edit the hierarchy through the transactional service ---------
        goal_id = goal_ids[0]
        async with database.session() as session:
            hierarchy = HierarchyService(session)
            before = await hierarchy.get_hierarchy()
            assert before["roots"], "hierarchy has no goal roots to edit"

            revision = next(
                root["revision"] for root in before["roots"] if root["id"] == goal_id
            )
            await hierarchy.update_entity(
                goal_id,
                expected_revision=revision,
                text="Ship the API change without regressions",
            )

        # A stale revision must not silently overwrite the edit.
        async with database.session() as session:
            with pytest.raises(HierarchyConflict):
                await HierarchyService(session).update_entity(
                    goal_id, expected_revision=revision, text="clobbered"
                )

        async with database.session() as session:
            still = await Store(session).entities.get(goal_id)
            assert still.text == "Ship the API change without regressions"
    finally:
        await database.close()

    # ---- Restart against the same directory -------------------------------
    database = Database(db_name=DB_NAME, data_directory=data_dir)
    await database.connect()
    try:
        async with database.session() as session:
            store = Store(session)
            goal = await store.entities.get(goal_id)
            assert goal is not None, "the goal did not survive the restart"
            assert goal.text == "Ship the API change without regressions"
            assert goal.metadata_dict.get("user_edited") is True

            results = await QueryInterface(store).search_entities("API", top_k=10)
            assert results, "search returned nothing after restart"
    finally:
        await database.close()

    # ---- Query through the public synchronous API -------------------------
    tempo = Tempo(data_dir=data_dir, db_name=DB_NAME)

    goals = tempo.goals()
    assert any(item.id == goal_id for item in goals)
    assert any(item.text == "Ship the API change without regressions" for item in goals)

    tree = tempo.hierarchy(goal_id=goal_id)
    assert [root["id"] for root in tree["roots"]] == [goal_id]

    assert tempo.trace_down(goal_id), "the edited goal has no descendants"

"""Installed-package-facing synchronous API contract."""

import asyncio
from datetime import datetime

import tempo
from tempo.db import Database
from tempo.store import Store


async def _seed(data_dir):
    database = Database(data_directory=str(data_dir))
    await database.connect()
    async with database.session() as session:
        store = Store(session)
        operation = await store.create_operation("Verified strict types", datetime.utcnow())
        action = await store.create_action(
            "Harden the public package", datetime.utcnow(), None, [operation.id]
        )
        activity = await store.create_activity("Prepare Tempo release", [action.id])
        goal = await store.create_goal("Publish Tempo safely", [activity.id])
    await database.close()
    return operation.id, goal.id


def test_public_api_queries_hierarchy_search_timeline_and_traces(tmp_path):
    operation_id, goal_id = asyncio.run(_seed(tmp_path))
    client = tempo.Tempo(data_dir=tmp_path)

    goals = client.goals()
    assert len(goals) == 1
    assert isinstance(goals[0], tempo.Goal)
    assert (goals[0].id, goals[0].text) == (goal_id, "Publish Tempo safely")
    assert client.hierarchy(goal_id=goal_id)["count"] == 4
    assert any(result.entity.text == "Harden the public package" for result in client.search("Harden"))
    assert [entity.text for entity in client.timeline(days=1)] == ["Prepare Tempo release"]
    assert [entity.type for entity in client.trace_up(operation_id)[0].path] == [
        "operation", "action", "activity", "goal"
    ]
    assert [entity.type for entity in client.trace_down(goal_id)[0].path] == [
        "goal", "activity", "action", "operation"
    ]


async def _query_from_running_loop(tmp_path):
    client = tempo.Tempo(data_dir=tmp_path)
    return client.goals()


def test_public_api_works_inside_notebook_style_event_loop(tmp_path):
    assert asyncio.run(_query_from_running_loop(tmp_path)) == []

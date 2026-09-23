#!/usr/bin/env python3
"""Record LLM fixtures for the test harness.

Run once with Vertex AI credentials to capture real LLM responses:

    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json \
    GOOGLE_CLOUD_PROJECT=your-gcp-project-id \
    python tests/record_fixtures.py

This creates JSON fixture files in tests/fixtures/ that are committed to git.
Tests replay these fixtures without needing API keys.
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest import RecordReplayProvider, FIXTURES_DIR
from tempo.providers import create_provider
from tempo.db import Database
from tempo.store import Store
from tempo.models import EntityType
from tempo.pipelines.observation_to_operation import ObservationAdapter
from tempo.pipelines.operations_to_actions import ActionBuilder
from tempo.pipelines.action_to_activities import ActivityProposeJob
from tempo.pipelines.goal_synthesis import GoalSynthesisJob
from tempo.buffer import BufferedOperation


SAMPLE_OBSERVATION = """The user's screen shows a code editor (VS Code) with a Python file open.
The file appears to be `server.py` with FastAPI route definitions visible.
The user has the terminal panel open at the bottom showing `pytest` output with 3 tests passing.
A web browser (Chrome) is visible in the background with the tab title "GitHub - Pull Request #42".
The mouse cursor is positioned over the "Run" button in VS Code's top toolbar.
The system clock shows 2:35 PM. The dock at the bottom shows icons for Finder, VS Code, Chrome, Terminal, and Slack."""


async def record_stage1(real_provider, tmp_dir: str):
    """Record Stage 1: Observation -> Operations."""
    print("Recording Stage 1 (Obs -> Ops)...")
    fixture_path = FIXTURES_DIR / "stage1_obs_to_ops.json"
    provider = RecordReplayProvider(fixture_path, real_provider, record=True)

    db = Database(db_name="stage1.db", data_directory=tmp_dir)
    await db.connect()

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        op_ids = await adapter.process_observation(
            observation_text=SAMPLE_OBSERVATION,
            observation_timestamp=datetime(2026, 1, 15, 14, 35, 0),
        )
        print(f"  Created {len(op_ids)} operations")

    await db.close()
    provider.save()
    print(f"  Saved to {fixture_path}")
    return op_ids


async def record_stage2(real_provider, tmp_dir: str):
    """Record Stage 2: Operations -> Actions."""
    print("Recording Stage 2 (Ops -> Actions)...")
    fixture_path = FIXTURES_DIR / "stage2_ops_to_actions.json"
    provider = RecordReplayProvider(fixture_path, real_provider, record=True)

    db = Database(db_name="stage2.db", data_directory=tmp_dir)
    await db.connect()

    # First session: create operations
    base_time = datetime(2026, 1, 15, 14, 30, 0)
    op_texts = [
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
    op_ids = []
    async with db.session() as session:
        store = Store(session)
        for i, text in enumerate(op_texts):
            entity = await store.create_operation(
                text=text,
                timestamp=base_time + timedelta(seconds=i * 30),
                metadata={"confidence": "8", "decay": "3"},
            )
            op_ids.append(entity.id)

    # Build BufferedOperation objects
    ops = [
        BufferedOperation(
            entity_id=op_ids[i],
            text=op_texts[i],
            timestamp=base_time + timedelta(seconds=i * 30),
        )
        for i in range(len(op_texts))
    ]

    # Second session: build actions
    async with db.session() as session:
        store = Store(session)
        builder = ActionBuilder(provider=provider, store=store)
        action_ids = await builder.build_actions(ops)
        print(f"  Created {len(action_ids)} actions from {len(ops)} operations")

    await db.close()
    provider.save()
    print(f"  Saved to {fixture_path}")
    return action_ids


async def record_stage3(real_provider, tmp_dir: str):
    """Record Stage 3: Actions -> Activities."""
    print("Recording Stage 3 (Actions -> Activities)...")
    fixture_path = FIXTURES_DIR / "stage3_actions_to_activities.json"
    provider = RecordReplayProvider(fixture_path, real_provider, record=True)

    db = Database(db_name="stage3.db", data_directory=tmp_dir)
    await db.connect()

    # Create actions
    base_time = datetime(2026, 1, 15, 14, 0, 0)
    action_texts = [
        "Editing server.py to add new API endpoint",
        "Running pytest to verify changes",
        "Reviewing Pull Request #42 on GitHub",
        "Responding to code review comments",
        "Checking Slack messages from team",
    ]
    action_ids = []
    async with db.session() as session:
        store = Store(session)
        for i, text in enumerate(action_texts):
            action = await store.create_action(
                text=text,
                timestamp_start=base_time + timedelta(minutes=i * 15),
                timestamp_end=base_time + timedelta(minutes=i * 15 + 14),
                operation_ids=[],
            )
            action_ids.append(action.id)

    # Run activity proposal
    async with db.session() as session:
        store = Store(session)
        job = ActivityProposeJob(provider=provider, store=store)
        result = await job.run(action_ids=action_ids)
        print(f"  Result: {result}")

    await db.close()
    provider.save()
    print(f"  Saved to {fixture_path}")


async def record_stage4(real_provider, tmp_dir: str):
    """Record Stage 4: Activities -> Goals."""
    print("Recording Stage 4 (Activities -> Goals)...")
    fixture_path = FIXTURES_DIR / "stage4_goal_synthesis.json"
    provider = RecordReplayProvider(fixture_path, real_provider, record=True)

    db = Database(db_name="stage4.db", data_directory=tmp_dir)
    await db.connect()

    # Create activities
    async with db.session() as session:
        store = Store(session)
        activity_texts = [
            "Software development - working on API features",
            "Code review and collaboration",
            "Team communication via Slack",
            "Research and documentation",
        ]
        for text in activity_texts:
            await store.create_activity(
                text=text,
                action_ids=[],
                metadata={"status": "active"},
            )

    # Run goal synthesis
    async with db.session() as session:
        store = Store(session)
        job = GoalSynthesisJob(provider=provider, store=store)
        result = await job.run()
        print(f"  Result keys: {list(result.keys())}")

    await db.close()
    provider.save()
    print(f"  Saved to {fixture_path}")


async def main():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Create real provider (Vertex AI)
    real_provider = create_provider(
        model="gemini-3.8-flash",
        gemini_vertexai=True,
        vertex_project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        vertex_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        await record_stage1(real_provider, tmp_dir)
        await record_stage2(real_provider, tmp_dir)
        await record_stage3(real_provider, tmp_dir)
        await record_stage4(real_provider, tmp_dir)

    print("\nAll fixtures recorded!")


if __name__ == "__main__":
    asyncio.run(main())

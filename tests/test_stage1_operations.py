"""Tests for Stage 1: Observation -> Operations pipeline.

Covers ObservationAdapter.process_observation() creating Entity rows
with type='operation', correct timestamps, metadata pass-through,
malformed JSON fallback, and empty observation handling.
"""

import json
from datetime import datetime

import pytest
import pytest_asyncio

from tempo.models import EntityType
from tempo.pipelines.observation_to_operation import ObservationAdapter
from tempo.store import Store


# The exact observation text used when recording the replay fixture.
# Must match tests/record_fixtures.py SAMPLE_OBSERVATION so the prompt hash aligns.
SAMPLE_OBSERVATION = """The user's screen shows a code editor (VS Code) with a Python file open.
The file appears to be `server.py` with FastAPI route definitions visible.
The user has the terminal panel open at the bottom showing `pytest` output with 3 tests passing.
A web browser (Chrome) is visible in the background with the tab title "GitHub - Pull Request #42".
The mouse cursor is positioned over the "Run" button in VS Code's top toolbar.
The system clock shows 2:35 PM. The dock at the bottom shows icons for Finder, VS Code, Chrome, Terminal, and Slack."""

# Timestamp used when the fixture was recorded.
SAMPLE_TIMESTAMP = datetime(2026, 1, 15, 14, 35, 0)


# ---------------------------------------------------------------------------
# 1. Replay provider: creates operation entities with type="operation"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_creates_operation_entities(db, replay_provider):
    """process_observation() creates Entity rows with type='operation' using the replay provider."""
    provider = replay_provider("stage1_obs_to_ops")

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation(
            SAMPLE_OBSERVATION, observation_timestamp=SAMPLE_TIMESTAMP,
        )

    # Read back in a fresh session
    async with db.session() as session:
        store = Store(session)
        for eid in ids:
            entity = await store.entities.get(eid)
            assert entity is not None, f"Entity {eid} should exist"
            assert entity.type == EntityType.OPERATION


@pytest.mark.asyncio
async def test_replay_returns_nonempty_ids(db, replay_provider):
    """Replay fixture produces at least one operation."""
    provider = replay_provider("stage1_obs_to_ops")

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation(
            SAMPLE_OBSERVATION, observation_timestamp=SAMPLE_TIMESTAMP,
        )

    assert len(ids) >= 1


# ---------------------------------------------------------------------------
# 2. Timestamps are passed through correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_operation_timestamps_match_observation(db, replay_provider):
    """Operation entities have timestamp_start matching the observation timestamp."""
    provider = replay_provider("stage1_obs_to_ops")

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation(
            SAMPLE_OBSERVATION, observation_timestamp=SAMPLE_TIMESTAMP,
        )

    async with db.session() as session:
        store = Store(session)
        for eid in ids:
            entity = await store.entities.get(eid)
            assert entity.timestamp_start == SAMPLE_TIMESTAMP, (
                f"Expected timestamp {SAMPLE_TIMESTAMP}, got {entity.timestamp_start}"
            )


@pytest.mark.asyncio
async def test_default_timestamp_when_none(db, mock_provider):
    """When no timestamp is provided, a default (utcnow) is used."""
    response = json.dumps({"operations": [{"text": "Opened browser"}]})
    provider = mock_provider(response)

    before = datetime.utcnow()
    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation("Opened browser")
    after = datetime.utcnow()

    async with db.session() as session:
        store = Store(session)
        entity = await store.entities.get(ids[0])
        assert before <= entity.timestamp_start <= after


# ---------------------------------------------------------------------------
# 3. Metadata (confidence, decay) when LLM provides them
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metadata_confidence_and_decay(db, replay_provider):
    """Operations carry confidence and decay in metadata when the LLM provides them."""
    provider = replay_provider("stage1_obs_to_ops")

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation(
            SAMPLE_OBSERVATION, observation_timestamp=SAMPLE_TIMESTAMP,
        )

    async with db.session() as session:
        store = Store(session)
        for eid in ids:
            entity = await store.entities.get(eid)
            meta = entity.metadata_dict
            assert "confidence" in meta, "Expected 'confidence' in metadata"
            assert "decay" in meta, "Expected 'decay' in metadata"


@pytest.mark.asyncio
async def test_metadata_includes_context_fields(db, mock_provider):
    """Context dict from LLM response is merged into entity metadata."""
    response = json.dumps({
        "operations": [{
            "text": "Editing code",
            "confidence": 7,
            "decay": 2,
            "context": {"app": "VS Code", "tool_kind": "editor"},
        }]
    })
    provider = mock_provider(response)

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation("Editing code", observation_timestamp=datetime(2026, 1, 1))

    async with db.session() as session:
        store = Store(session)
        entity = await store.entities.get(ids[0])
        meta = entity.metadata_dict
        assert meta.get("app") == "VS Code"
        assert meta.get("tool_kind") == "editor"
        assert meta.get("confidence") == "7"
        assert meta.get("decay") == "2"


# ---------------------------------------------------------------------------
# 4. Malformed JSON falls back to a single fallback operation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_json_fallback(db, mock_provider):
    """When the LLM returns invalid JSON, a single fallback operation is created."""
    provider = mock_provider("this is not valid json {{{")

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation(
            "User clicked a button",
            observation_timestamp=datetime(2026, 2, 1, 12, 0, 0),
        )

    assert len(ids) == 1, "Malformed JSON should produce exactly 1 fallback operation"

    async with db.session() as session:
        store = Store(session)
        entity = await store.entities.get(ids[0])
        assert entity.type == EntityType.OPERATION
        assert "[Observation]" in entity.text
        assert "User clicked a button" in entity.text
        meta = entity.metadata_dict
        assert "parse_error" in meta


@pytest.mark.asyncio
async def test_malformed_json_preserves_timestamp(db, mock_provider):
    """Fallback operation still uses the provided observation timestamp."""
    provider = mock_provider("~~~not json~~~")
    ts = datetime(2026, 6, 15, 8, 0, 0)

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation("Some observation", observation_timestamp=ts)

    async with db.session() as session:
        store = Store(session)
        entity = await store.entities.get(ids[0])
        assert entity.timestamp_start == ts


# ---------------------------------------------------------------------------
# 5. Empty observation text still produces output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_observation_produces_output(db, mock_provider):
    """An empty observation string still yields at least one operation."""
    response = json.dumps({"operations": [{"text": "[No activity observed]"}]})
    provider = mock_provider(response)

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation("", observation_timestamp=datetime(2026, 1, 1))

    assert len(ids) >= 1

    async with db.session() as session:
        store = Store(session)
        entity = await store.entities.get(ids[0])
        assert entity.type == EntityType.OPERATION
        assert entity.text  # non-empty text stored


@pytest.mark.asyncio
async def test_empty_observation_fallback_on_bad_json(db, mock_provider):
    """Empty observation + bad JSON still produces a fallback operation."""
    provider = mock_provider("not json")

    async with db.session() as session:
        store = Store(session)
        adapter = ObservationAdapter(provider=provider, store=store)
        ids = await adapter.process_observation("", observation_timestamp=datetime(2026, 1, 1))

    assert len(ids) == 1

    async with db.session() as session:
        store = Store(session)
        entity = await store.entities.get(ids[0])
        assert entity.type == EntityType.OPERATION
        assert "[Observation]" in entity.text

"""Shared test fixtures for Tempo test harness.

Provides:
- In-memory SQLite database via Database class
- Record/Replay LLM provider
- Store wired to test DB
- Sample data factories
"""

import asyncio
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tempo.db import Database
from tempo.models import Base, Entity, EntityType
from tempo.providers import ModelProvider
from tempo.store import Store
from tempo.buffer import BufferedOperation


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolate_error_log(tmp_path, monkeypatch):
    """Keep test diagnostics out of the user's real Tempo data directory."""
    monkeypatch.setenv("TEMPO_ERROR_LOG_PATH", str(tmp_path / "errors.log"))


# ---------------------------------------------------------------------------
# Record/Replay Provider
# ---------------------------------------------------------------------------


class FixtureMissing(Exception):
    """Raised when a prompt has no recorded fixture."""


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


class RecordReplayProvider(ModelProvider):
    """ModelProvider that records or replays LLM responses from fixtures.

    - record=True: forwards to `real_provider`, saves responses to `fixture_path`
    - record=False: looks up prompt hash in fixtures, returns cached response
    """

    def __init__(
        self,
        fixture_path: Path,
        real_provider: Optional[ModelProvider] = None,
        record: bool = False,
    ):
        super().__init__(model="replay", api_key=None, api_base=None)
        self.fixture_path = fixture_path
        self.real_provider = real_provider
        self.record = record
        self._fixtures: Dict[str, str] = {}
        self._recordings: List[Dict[str, str]] = []

        if fixture_path.exists() and not record:
            data = json.loads(fixture_path.read_text())
            for entry in data:
                self._fixtures[entry["prompt_hash"]] = entry["response"]

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        **kwargs,
    ) -> str:
        prompt = messages[-1]["content"] if messages else ""
        h = _prompt_hash(prompt)

        if self.record:
            if self.real_provider is None:
                raise ValueError("record=True requires a real_provider")
            response = await self.real_provider.chat_completion(
                messages, response_format=response_format, temperature=temperature, **kwargs
            )
            self._fixtures[h] = response
            self._recordings.append({
                "prompt_hash": h,
                "prompt_preview": prompt[:200],
                "response": response,
            })
            return response

        if h not in self._fixtures:
            raise FixtureMissing(
                f"No fixture for prompt hash {h[:16]}... "
                f"Preview: {prompt[:100]}..."
            )
        return self._fixtures[h]

    async def vision_completion(self, messages, **kwargs) -> str:
        return await self.chat_completion(messages, **kwargs)

    def save(self):
        """Write recordings to fixture file."""
        self.fixture_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixture_path.write_text(json.dumps(self._recordings, indent=2))


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary directory for test databases."""
    return str(tmp_path)


@pytest_asyncio.fixture
async def db(tmp_data_dir):
    """Create a fresh SQLite database for each test."""
    database = Database(db_name="test.db", data_directory=tmp_data_dir)
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def store(db):
    """Store wired to the test database."""
    async with db.session() as session:
        yield Store(session)


@pytest.fixture
def replay_provider():
    """Create a replay provider from a fixture file.

    Usage:
        def test_something(replay_provider):
            provider = replay_provider("stage1_obs_to_ops")
    """
    def _make(fixture_name: str) -> RecordReplayProvider:
        path = FIXTURES_DIR / f"{fixture_name}.json"
        return RecordReplayProvider(fixture_path=path, record=False)
    return _make


@pytest.fixture
def mock_provider():
    """Create a mock provider that returns a configurable response."""
    def _make(response: str) -> ModelProvider:
        provider = AsyncMock(spec=ModelProvider)
        provider.model = "mock"
        provider.chat_completion = AsyncMock(return_value=response)
        provider.vision_completion = AsyncMock(return_value=response)
        return provider
    return _make


# ---------------------------------------------------------------------------
# Sample data factories
# ---------------------------------------------------------------------------


def make_operations(
    n: int = 5,
    base_time: Optional[datetime] = None,
    interval_seconds: int = 30,
) -> List[BufferedOperation]:
    """Create a list of BufferedOperations for testing."""
    base = base_time or datetime(2026, 1, 15, 10, 0, 0)
    ops = []
    for i in range(n):
        ops.append(BufferedOperation(
            entity_id=i + 1,
            text=f"Clicked button {i + 1}",
            timestamp=base + timedelta(seconds=i * interval_seconds),
        ))
    return ops


async def seed_operations(store: Store, n: int = 5, base_time: Optional[datetime] = None) -> List[Entity]:
    """Create operation entities in the database and return them."""
    base = base_time or datetime(2026, 1, 15, 10, 0, 0)
    entities = []
    for i in range(n):
        entity = await store.create_operation(
            text=f"Clicked button {i + 1}",
            timestamp=base + timedelta(seconds=i * 30),
            metadata={"confidence": "8", "decay": "3"},
        )
        entities.append(entity)
    return entities


async def seed_actions(store: Store, n: int = 3, base_time: Optional[datetime] = None) -> List[Entity]:
    """Create action entities in the database and return them."""
    base = base_time or datetime(2026, 1, 15, 10, 0, 0)
    entities = []
    for i in range(n):
        entity = await store.create_action(
            text=f"Working on task {i + 1}",
            timestamp_start=base + timedelta(minutes=i * 10),
            timestamp_end=base + timedelta(minutes=i * 10 + 9),
            operation_ids=[],
        )
        entities.append(entity)
    return entities

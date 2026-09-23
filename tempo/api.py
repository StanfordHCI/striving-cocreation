"""Small synchronous public API over Tempo's async query layer."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from tempo.db import Database
from tempo.hierarchy import HierarchyService
from tempo.models import Entity
from tempo.queries import QueryInterface
from tempo.store import Store


@dataclass(frozen=True)
class EntityRecord:
    id: int
    type: str
    text: str
    timestamp_start: datetime
    timestamp_end: datetime | None
    metadata: dict


@dataclass(frozen=True)
class Goal(EntityRecord):
    """A goal returned by :meth:`Tempo.goals`."""


@dataclass(frozen=True)
class SearchResult:
    entity: EntityRecord
    score: float | None


@dataclass(frozen=True)
class TracePath:
    path: list[EntityRecord]
    depth: int


T = TypeVar("T")


def _record(entity: Entity) -> EntityRecord:
    return EntityRecord(
        id=entity.id,
        type=entity.type,
        text=entity.text,
        timestamp_start=entity.timestamp_start,
        timestamp_end=entity.timestamp_end,
        metadata=dict(entity.metadata_dict or {}),
    )


def _goal(entity: Entity) -> Goal:
    return Goal(
        id=entity.id,
        type=entity.type,
        text=entity.text,
        timestamp_start=entity.timestamp_start,
        timestamp_end=entity.timestamp_end,
        metadata=dict(entity.metadata_dict or {}),
    )


class Tempo:
    """Synchronous, notebook-friendly access to a local Tempo database.

    Each call owns a short-lived async database connection, so callers do not
    need to manage an event loop or remember to close resources.
    """

    def __init__(
        self,
        data_dir: str | Path = "~/.cache/tempo",
        db_name: str = "tempo.db",
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.db_name = db_name

    def _run(self, operation: Callable[[Store], Awaitable[T]]) -> T:
        async def execute() -> T:
            database = Database(
                db_name=self.db_name,
                data_directory=str(self.data_dir),
            )
            await database.connect()
            try:
                async with database.session() as session:
                    return await operation(Store(session))
            finally:
                await database.close()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(execute())

        # Notebook kernels already own the calling thread's event loop. Run the
        # short-lived async query on a dedicated thread so the public API stays
        # synchronous without patching or nesting that loop.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tempo-api") as executor:
            return executor.submit(lambda: asyncio.run(execute())).result()

    def goals(self) -> list[Goal]:
        async def load(store: Store) -> list[Goal]:
            return [_goal(entity) for entity in await store.get_goals()]

        return self._run(load)

    def hierarchy(self, goal_id: int | None = None) -> dict:
        async def load(store: Store) -> dict:
            result = await HierarchyService(store.session).get_hierarchy(max_depth=4)
            if goal_id is None:
                return result
            roots = [root for root in result["roots"] if root["id"] == goal_id]
            return {**result, "roots": roots, "count": _hierarchy_count(roots)}

        return self._run(load)

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        if not query.strip():
            return []
        if limit < 1:
            raise ValueError("limit must be at least 1")

        async def load(store: Store) -> list[SearchResult]:
            results = await QueryInterface(store).search_entities(query, top_k=limit)
            return [SearchResult(_record(result.entity), result.score) for result in results]

        return self._run(load)

    def timeline(self, days: int = 7) -> list[EntityRecord]:
        if days < 1:
            raise ValueError("days must be at least 1")

        async def load(store: Store) -> list[EntityRecord]:
            entities = await QueryInterface(store).get_activity_timeline(days=days)
            return [_record(entity) for entity in entities]

        return self._run(load)

    def trace_up(self, entity_id: int) -> list[TracePath]:
        return self._trace(entity_id, direction="up")

    def trace_down(self, entity_id: int) -> list[TracePath]:
        return self._trace(entity_id, direction="down")

    def _trace(self, entity_id: int, direction: str) -> list[TracePath]:
        async def load(store: Store) -> list[TracePath]:
            queries = QueryInterface(store)
            results = (
                await queries.trace_up(entity_id)
                if direction == "up"
                else await queries.trace_down(entity_id)
            )
            return [
                TracePath(path=[_record(entity) for entity in result.path], depth=result.depth)
                for result in results
            ]

        return self._run(load)


def _hierarchy_count(nodes: list[dict]) -> int:
    return sum(1 + _hierarchy_count(node.get("children", [])) for node in nodes)

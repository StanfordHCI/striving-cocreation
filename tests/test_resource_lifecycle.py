"""Resource ownership tests guarding against task and client leaks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tempo.buffer import BufferedOperation, OperationBuffer
from tempo.observers.observer import Observer
from tempo.system import TempoSystem


class WaitingObserver(Observer):
    def __init__(self):
        self.release = asyncio.Event()
        super().__init__(name="waiting")

    async def _worker(self) -> None:
        await self.release.wait()


@pytest.mark.asyncio
async def test_operation_buffer_close_awaits_timer_and_releases_callback():
    callback = AsyncMock()
    buffer = OperationBuffer(on_trigger=callback, time_threshold_seconds=60)
    await buffer.add(
        BufferedOperation(entity_id=1, text="test", timestamp=None)
    )
    timer = buffer._timer_task
    assert timer is not None and not timer.done()

    await buffer.close()

    assert timer.done()
    assert buffer._timer_task is None
    assert buffer.on_trigger is None
    assert buffer.size() == 1


@pytest.mark.asyncio
async def test_operation_buffer_close_collects_superseded_timers():
    buffer = OperationBuffer(time_threshold_seconds=60)
    for entity_id in range(20):
        await buffer.add(
            BufferedOperation(entity_id=entity_id, text="test", timestamp=None)
        )
    owned_timers = list(buffer._timer_tasks)

    await buffer.close()

    assert owned_timers
    assert all(timer.done() for timer in owned_timers)
    assert not buffer._timer_tasks


@pytest.mark.asyncio
async def test_observer_restart_does_not_duplicate_live_worker():
    observer = WaitingObserver()
    original_task = observer._task

    observer.restart()

    assert observer._task is original_task
    await observer.stop()
    assert original_task.done()


@pytest.mark.asyncio
async def test_system_cleanup_closes_each_owned_resource_once():
    system = TempoSystem()
    system.observer = MagicMock()
    system.observer.close = AsyncMock()
    system.db = MagicMock()
    system.db.checkpoint = AsyncMock()
    system.db.close = AsyncMock()
    system.provider = MagicMock()
    system.provider.close = AsyncMock()
    system.stage1_provider = MagicMock()
    system.stage1_provider.close = AsyncMock()

    observer = system.observer
    database = system.db
    provider = system.provider
    stage1_provider = system.stage1_provider
    await system.cleanup()

    observer.close.assert_awaited_once()
    database.close.assert_awaited_once()
    provider.close.assert_awaited_once()
    stage1_provider.close.assert_awaited_once()
    assert system.observer is None
    assert system.db is None
    assert system.provider is None
    assert system.stage1_provider is None


@pytest.mark.asyncio
async def test_system_does_not_double_close_shared_stage_provider():
    system = TempoSystem()
    system.observer = None
    system.db = None
    provider = MagicMock()
    provider.close = AsyncMock()
    system.provider = provider
    system.stage1_provider = provider

    await system.cleanup()

    provider.close.assert_awaited_once()

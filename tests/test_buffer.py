"""Tests for the production OperationBuffer.

Pure logic tests, no LLM needed. Uses short time thresholds to keep tests fast.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List

import pytest
import pytest_asyncio

from tempo.buffer import BufferedOperation, OperationBuffer
from tests.conftest import make_operations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_op(i: int = 1) -> BufferedOperation:
    return BufferedOperation(entity_id=i, text=f"op_{i}", timestamp=datetime.utcnow())


class TriggerTracker:
    """Tracks trigger invocations for assertions."""

    def __init__(self, *, fail_times: int = 0):
        self.calls: List[list] = []
        self._fail_remaining = fail_times

    async def __call__(self, items):
        self.calls.append(list(items))
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("simulated failure")

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_batch(self) -> list:
        return self.calls[-1] if self.calls else []


# ===================================================================
# OperationBuffer tests
# ===================================================================


class TestOperationBufferSizeTrigger:
    """Size trigger fires at max_buffer_size."""

    @pytest.mark.asyncio
    async def test_fires_at_max_size(self):
        tracker = TriggerTracker()
        buf = OperationBuffer(max_buffer_size=3, on_trigger=tracker)

        triggered_1 = await buf.add(_make_op(1))
        triggered_2 = await buf.add(_make_op(2))
        assert not triggered_1
        assert not triggered_2
        assert buf.size() == 2

        triggered_3 = await buf.add(_make_op(3))
        assert triggered_3
        assert tracker.call_count == 1
        assert len(tracker.last_batch) == 3
        # Buffer should be empty after successful trigger
        assert buf.size() == 0

    @pytest.mark.asyncio
    async def test_does_not_fire_below_max(self):
        tracker = TriggerTracker()
        buf = OperationBuffer(max_buffer_size=10, on_trigger=tracker)

        for i in range(9):
            await buf.add(_make_op(i))

        assert tracker.call_count == 0
        assert buf.size() == 9


class TestOperationBufferTimeTrigger:
    """Time trigger fires after inactivity threshold."""

    @pytest.mark.asyncio
    async def test_timer_fires_after_inactivity(self):
        tracker = TriggerTracker()
        buf = OperationBuffer(
            time_threshold_seconds=0.1,
            max_buffer_size=100,
            on_trigger=tracker,
        )

        await buf.add(_make_op(1))
        await buf.add(_make_op(2))
        assert tracker.call_count == 0

        # Wait for the async timer to fire
        await asyncio.sleep(0.3)

        assert tracker.call_count == 1
        assert len(tracker.last_batch) == 2
        assert buf.size() == 0

    @pytest.mark.asyncio
    async def test_check_time_trigger_explicit(self):
        """check_time_trigger() works as a fallback."""
        tracker = TriggerTracker()
        buf = OperationBuffer(
            time_threshold_seconds=0.05,
            max_buffer_size=100,
            on_trigger=tracker,
        )

        await buf.add(_make_op(1))
        # Cancel the async timer so we can test the explicit check path
        if buf._timer_task and not buf._timer_task.done():
            buf._timer_task.cancel()

        await asyncio.sleep(0.1)
        fired = await buf.check_time_trigger()

        assert fired
        assert tracker.call_count == 1
        assert buf.size() == 0


class TestOperationBufferFailedCallback:
    """Failed callback preserves operations in buffer."""

    @pytest.mark.asyncio
    async def test_operations_preserved_on_failure(self):
        tracker = TriggerTracker(fail_times=1)
        buf = OperationBuffer(max_buffer_size=3, on_trigger=tracker)

        await buf.add(_make_op(1))
        await buf.add(_make_op(2))
        await buf.add(_make_op(3))  # triggers, but callback fails

        # Callback was invoked
        assert tracker.call_count == 1
        # Operations should still be in the buffer
        assert buf.size() == 3

    @pytest.mark.asyncio
    async def test_operations_cleared_after_successful_retry(self):
        tracker = TriggerTracker(fail_times=1)
        buf = OperationBuffer(max_buffer_size=3, on_trigger=tracker)

        await buf.add(_make_op(1))
        await buf.add(_make_op(2))
        await buf.add(_make_op(3))  # triggers, fails
        assert buf.size() == 3

        # Force flush — second call should succeed
        await buf.flush()
        assert tracker.call_count == 2
        assert buf.size() == 0


class TestOperationBufferTriggerSuppression:
    """After failure, size-based re-trigger is suppressed."""

    @pytest.mark.asyncio
    async def test_suppressed_after_failure(self):
        tracker = TriggerTracker(fail_times=1)
        buf = OperationBuffer(max_buffer_size=2, on_trigger=tracker)

        await buf.add(_make_op(1))
        await buf.add(_make_op(2))  # triggers, fails
        assert tracker.call_count == 1
        assert buf.size() == 2

        # Adding more should NOT re-trigger because suppression is active
        await buf.add(_make_op(3))
        assert tracker.call_count == 1  # still 1 — suppressed
        assert buf.size() == 3

    @pytest.mark.asyncio
    async def test_flush_bypasses_suppression(self):
        tracker = TriggerTracker(fail_times=1)
        buf = OperationBuffer(max_buffer_size=2, on_trigger=tracker)

        await buf.add(_make_op(1))
        await buf.add(_make_op(2))  # triggers, fails
        assert buf.size() == 2

        # flush() should still work even when suppressed
        await buf.flush()
        assert tracker.call_count == 2
        assert buf.size() == 0


class TestOperationBufferFlush:
    """flush() forces trigger regardless of conditions."""

    @pytest.mark.asyncio
    async def test_flush_with_partial_buffer(self):
        tracker = TriggerTracker()
        buf = OperationBuffer(max_buffer_size=100, on_trigger=tracker)

        await buf.add(_make_op(1))
        assert tracker.call_count == 0

        await buf.flush()
        assert tracker.call_count == 1
        assert len(tracker.last_batch) == 1
        assert buf.size() == 0

    @pytest.mark.asyncio
    async def test_flush_empty_buffer_is_noop(self):
        tracker = TriggerTracker()
        buf = OperationBuffer(max_buffer_size=100, on_trigger=tracker)

        await buf.flush()
        assert tracker.call_count == 0

    @pytest.mark.asyncio
    async def test_flush_no_callback_is_noop(self):
        buf = OperationBuffer(max_buffer_size=100, on_trigger=None)
        await buf.add(_make_op(1))
        await buf.flush()
        assert buf.size() == 1  # nothing consumed without a callback


class TestOperationBufferStateRoundTrip:
    """get_operations returns correct data after add."""

    @pytest.mark.asyncio
    async def test_get_operations_returns_copy(self):
        buf = OperationBuffer(max_buffer_size=100)

        op1 = _make_op(1)
        op2 = _make_op(2)
        await buf.add(op1)
        await buf.add(op2)

        ops = buf.get_operations()
        assert len(ops) == 2
        assert ops[0].entity_id == 1
        assert ops[1].entity_id == 2
        assert ops[0].text == "op_1"

        # Verify it is a copy — mutating returned list does not affect buffer
        ops.pop()
        assert buf.size() == 2

    @pytest.mark.asyncio
    async def test_make_operations_factory(self):
        """The conftest make_operations helper produces valid BufferedOperations."""
        ops = make_operations(n=4, interval_seconds=60)
        buf = OperationBuffer(max_buffer_size=100)

        for op in ops:
            await buf.add(op)

        assert buf.size() == 4
        retrieved = buf.get_operations()
        assert retrieved[0].text == "Clicked button 1"
        assert retrieved[3].text == "Clicked button 4"

    @pytest.mark.asyncio
    async def test_clear_resets_state(self):
        buf = OperationBuffer(max_buffer_size=100)
        await buf.add(_make_op(1))
        await buf.add(_make_op(2))
        assert buf.size() == 2

        buf.clear()
        assert buf.size() == 0
        assert buf.is_empty()
        assert buf.get_operations() == []
        assert buf.last_operation_time is None

# buffer.py

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Callable, Awaitable
from dataclasses import dataclass

from tempo.models import Entity


logger = logging.getLogger(__name__)


@dataclass
class BufferedOperation:
    """An operation in the buffer."""
    entity_id: int
    text: str
    timestamp: datetime


class OperationBuffer:
    """
    Buffer for operations with time and size-based triggers.

    Triggers processing when:
    1. Time threshold exceeded (inactivity)
    2. Buffer size exceeded

    Note: Time-based triggers are checked both via async timer AND
    via explicit check_time_trigger() calls from the main loop for robustness.
    """

    def __init__(
        self,
        time_threshold_seconds: float = 300.0,
        max_buffer_size: int = 20,
        on_trigger: Optional[Callable[[List[BufferedOperation]], Awaitable[None]]] = None,
        debug: bool = False,
    ):
        # Configuration
        self.time_threshold_seconds = time_threshold_seconds
        self.max_buffer_size = max_buffer_size

        # State
        self.operations: List[BufferedOperation] = []
        self.last_operation_time: Optional[datetime] = None

        # Callbacks
        self.on_trigger = on_trigger

        # Timer handle
        self._timer_task: Optional[asyncio.Task] = None
        self._timer_tasks: set[asyncio.Task] = set()

        # Debug flag
        self.debug = debug

        # Serialize trigger callbacks to avoid concurrent writes (SQLite lock issues)
        self._trigger_lock = asyncio.Lock()

        # When a trigger fails and operations stay, suppress re-trigger for a
        # cooldown period to avoid a tight retry loop on every add().
        self._trigger_suppressed_until: Optional[datetime] = None

    async def add(self, operation: BufferedOperation) -> bool:
        """
        Add an operation to the buffer.

        Args:
            operation: The operation to add.

        Returns:
            True if the buffer was triggered, False otherwise.
        """
        triggered = False

        # Add to buffer
        self.operations.append(operation)
        self.last_operation_time = datetime.utcnow()

        # Check for buffer size trigger (skip if suppressed after recent failure)
        if len(self.operations) >= self.max_buffer_size:
            if self._trigger_suppressed_until and datetime.utcnow() < self._trigger_suppressed_until:
                if self.debug:
                    logger.debug("Buffer: Max size reached but trigger suppressed until %s", self._trigger_suppressed_until)
            else:
                if self.debug:
                    logger.info(f"Buffer: Max size ({self.max_buffer_size}) reached, triggering")
                await self._trigger("max_size")
                triggered = True

        # Reset/start timer
        self._reset_timer()

        if self.debug and not triggered:
            logger.debug(f"Buffer: Added operation, size={len(self.operations)}")

        return triggered

    async def check_time_trigger(self) -> bool:
        """
        Check if time-based trigger should fire.

        This should be called periodically from the main loop as a
        fallback to the async timer. Returns True if triggered.
        """
        if not self.operations:
            return False

        if self.last_operation_time is None:
            return False

        elapsed = (datetime.utcnow() - self.last_operation_time).total_seconds()

        if elapsed >= self.time_threshold_seconds:
            if self._trigger_suppressed_until and datetime.utcnow() < self._trigger_suppressed_until:
                return False
            if self.debug:
                logger.info(f"Buffer: Time threshold ({self.time_threshold_seconds}s) exceeded (elapsed={elapsed:.1f}s), triggering")
            await self._trigger("time_threshold")
            return True

        return False

    def _reset_timer(self) -> None:
        """Reset the inactivity timer."""
        # Cancel existing timer
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None

        # Try to start new timer (may fail if no event loop)
        try:
            loop = asyncio.get_running_loop()
            self._timer_task = loop.create_task(self._timer_callback())
            self._timer_tasks.add(self._timer_task)
            self._timer_task.add_done_callback(self._timer_tasks.discard)
        except RuntimeError:
            # No running event loop - that's OK, we have check_time_trigger as fallback
            if self.debug:
                logger.debug("Buffer: No event loop for timer, relying on check_time_trigger()")

    async def _timer_callback(self) -> None:
        """Timer callback for inactivity trigger."""
        try:
            await asyncio.sleep(self.time_threshold_seconds)
            if self.operations:  # Double-check we still have operations
                if self.debug:
                    logger.info(f"Buffer: Timer fired after {self.time_threshold_seconds}s inactivity")
                await self._trigger("timer")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Buffer: Timer callback error: {e}")

    async def _trigger(self, reason: str = "unknown") -> None:
        """Trigger processing of the buffer.

        Operations are only cleared after the callback succeeds. If the
        callback raises (network error, crash during LLM call, etc.) the
        operations remain in the buffer so the periodic state-save preserves
        them and they survive restarts.
        """
        async with self._trigger_lock:
            if not self.operations:
                return

            if self.on_trigger:
                # Snapshot — do NOT clear yet
                operations_to_process = list(self.operations)
                num_ops = len(operations_to_process)

                if self.debug:
                    logger.info(f"Buffer: Triggering with {num_ops} operations (reason: {reason})")

                # Call the trigger callback — only clear on success
                try:
                    await self.on_trigger(operations_to_process)
                except Exception as e:
                    logger.exception(f"Buffer: Trigger callback error, {num_ops} operations kept in buffer: {e}")
                    # Suppress re-trigger for 60s to avoid tight retry loop
                    self._trigger_suppressed_until = datetime.utcnow() + timedelta(seconds=60)
                    return  # operations stay in self.operations

                # Success — only remove the operations we actually processed.
                # Operations added during the callback are preserved.
                self._trigger_suppressed_until = None
                del self.operations[:num_ops]
                if not self.operations:
                    self.last_operation_time = None
                    if (
                        self._timer_task
                        and self._timer_task is not asyncio.current_task()
                        and not self._timer_task.done()
                    ):
                        self._timer_task.cancel()
                    self._timer_task = None

    def clear(self) -> None:
        """Clear the buffer."""
        self.operations = []
        self.last_operation_time = None

        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None

    def size(self) -> int:
        """Get the current buffer size."""
        return len(self.operations)

    def is_empty(self) -> bool:
        """Check if the buffer is empty."""
        return len(self.operations) == 0

    async def flush(self) -> None:
        """Force-trigger the buffer regardless of conditions."""
        if self.debug:
            logger.info(f"Buffer: Flushing {len(self.operations)} operations")
        await self._trigger("flush")

    async def stop_timer(self) -> None:
        """Cancel and await every owned timer without clearing buffered data."""
        tasks = [task for task in self._timer_tasks if task is not asyncio.current_task()]
        self._timer_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._timer_tasks.clear()

    async def close(self) -> None:
        """Release the timer and bound callback held by this buffer."""
        await self.stop_timer()
        self.on_trigger = None

    def get_operations(self) -> List[BufferedOperation]:
        """Get a copy of the current operations."""
        return list(self.operations)

    def get_time_until_trigger(self) -> Optional[float]:
        """
        Get seconds until time-based trigger would fire.

        Returns None if no operations in buffer.
        """
        if not self.operations or not self.last_operation_time:
            return None

        elapsed = (datetime.utcnow() - self.last_operation_time).total_seconds()
        remaining = self.time_threshold_seconds - elapsed
        return max(0, remaining)

    def start_timer_if_needed(self) -> None:
        """
        Start the timer if there are operations and no timer running.

        Call this after restoring state to ensure timer is active.
        """
        if self.operations and (self._timer_task is None or self._timer_task.done()):
            self._reset_timer()

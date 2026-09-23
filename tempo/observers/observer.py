from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import asyncio
import logging


_logger = logging.getLogger(__name__)

# Auto-restart constants
_WORKER_MAX_RESTARTS = 10
_WORKER_RESTART_BASE_SECONDS = 2.0
_WORKER_RESTART_MAX_SECONDS = 60.0


class Observer(ABC):
    """Base class for all observers in the Tempo system.

    This abstract base class defines the interface for all observers that monitor user behavior.
    Observers are responsible for collecting data about user interactions and sending updates
    through an asynchronous queue.

    Args:
        name (Optional[str]): A custom name for the observer. If not provided, the class name will be used.

    Attributes:
        update_queue (asyncio.Queue): Queue for sending updates to the main Tempo system.
        _name (str): The name of the observer.
        _running (bool): Flag indicating if the observer is currently running.
        _task (Optional[asyncio.Task]): Background task handle for the observer's worker.
    """

    _QUEUE_MAX_SIZE = 256  # prevent unbounded memory growth if consumer falls behind

    def __init__(self, name: Optional[str] = None) -> None:
        self.update_queue = asyncio.Queue(maxsize=self._QUEUE_MAX_SIZE)
        self._name = name or self.__class__.__name__

        # running flag + background task handle
        self._running = True
        self._task: asyncio.Task | None = asyncio.create_task(self._worker_wrapper())

    # ─────────────────────────────── abstract worker
    @abstractmethod
    async def _worker(self) -> None:     # subclasses override
        """Main worker method that must be implemented by subclasses.
        
        This method should contain the main logic for the observer, such as monitoring
        user interactions or collecting data. It runs in a background task and should
        continue running until the observer is stopped.
        """
        pass

    # wrapper plugs running flag + exception handling + auto-restart
    async def _worker_wrapper(self) -> None:
        """Wrapper for the worker method with auto-restart on failure.

        Retries the worker with exponential backoff on crash (up to
        _WORKER_MAX_RESTARTS times) so transient failures like sleep/wake
        display errors are absorbed without permanently killing the observer.
        """
        restarts = 0
        while True:
            try:
                await self._worker()
                break  # normal exit (self._running set to False externally)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                restarts += 1
                if restarts > _WORKER_MAX_RESTARTS:
                    _logger.error(
                        "Observer %s: exceeded max restarts (%d), giving up: %s",
                        self._name, _WORKER_MAX_RESTARTS, exc,
                    )
                    break
                delay = min(
                    _WORKER_RESTART_MAX_SECONDS,
                    _WORKER_RESTART_BASE_SECONDS * (2 ** min(restarts - 1, 5)),
                )
                _logger.warning(
                    "Observer %s: worker crashed, restarting in %.1fs (%d/%d): %s",
                    self._name, delay, restarts, _WORKER_MAX_RESTARTS, exc,
                )
                await asyncio.sleep(delay)
                self._running = True
        self._running = False

    # ─────────────────────────────── public API
    @property
    def name(self) -> str:
        """Get the name of the observer.
        
        Returns:
            str: The observer's name.
        """
        return self._name

    async def get_update(self):
        """Get the next update from the queue if available.
        
        Returns:
            Optional[Update]: The next update from the queue, or None if the queue is empty.
        """
        try:
            return self.update_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def stop(self) -> None:
        """Stop the observer and clean up resources.

        This method cancels the worker task and drains the update queue.
        """
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._running = False
        # unblock any awaiters
        while not self.update_queue.empty():
            self.update_queue.get_nowait()

    def restart(self) -> None:
        """Restart the observer worker after a stop. Call before resuming."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._worker_wrapper())


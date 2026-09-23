"""Lifecycle and orchestration for the full Tempo pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from tempo.context import UserContext
from tempo.db import Database
from tempo.debug_logger import DebugLogger
from tempo.pipelines.audit import setup_fts
from tempo.pipelines.tempo_pipeline import TempoPipeline
from tempo.system import TempoSystem
from tempo.utils import is_network_error, report_pipeline_error

logger = logging.getLogger(__name__)

STATE_SAVE_INTERVAL_SECONDS = 60
WAL_CHECKPOINT_INTERVAL_SECONDS = 60 * 60
DISK_CHECK_INTERVAL_SECONDS = 60 * 60
DISK_FREE_WARNING_GB = 10
SLEEP_GAP_THRESHOLD_SECONDS = 60
MAX_OBSERVER_RESTART_ATTEMPTS = 3
PIPELINE_STALL_SECONDS = 60 * 15
PIPELINE_STALL_REPEAT_SECONDS = 60 * 15


class PipelineOrchestrator:
    """Run the full Tempo pipeline against the main database."""

    def __init__(
        self,
        system: TempoSystem,
        user_context: Optional[UserContext] = None,
        state_path: str = "~/.cache/tempo/state.json",
    ):
        self.system = system
        self.user_context = user_context
        self.state_path = Path(state_path).expanduser()
        self.pipeline: Optional[TempoPipeline] = None
        self.running = False
        self._closed = False
        self._started_at: Optional[datetime] = None
        self._last_state_save: Optional[datetime] = None
        self._last_wal_checkpoint: Optional[datetime] = None
        self._last_disk_check: Optional[datetime] = None
        self._background_tasks: set[asyncio.Task] = set()
        self._pipeline_stall_last_alert = 0.0

    async def setup(
        self,
        user_name: str = "the user",
        main_db: Optional[Database] = None,
    ) -> None:
        """Connect the single pipeline to the main DB and restore its state."""
        if self.pipeline is not None:
            return

        db = main_db or self.system.db
        if db is None:
            raise RuntimeError("TempoSystem.setup() must run before orchestrator.setup()")
        await db.connect()
        async with db.session() as session:
            await setup_fts(session)

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        rendered_context = self.user_context.render() if self.user_context else ""
        debug_logger = DebugLogger(
            log_path=self.state_path.parent / "debug.jsonl",
            enabled=self.system.debug,
        )
        self.pipeline = TempoPipeline(
            db=db,
            provider=self.system.provider,
            stage1_provider=self.system.stage1_provider,
            user_name=user_name,
            user_context=rendered_context,
            debug=self.system.debug,
            debug_logger=debug_logger,
            on_critical_state_change=self.save_state,
        )

        if rendered_context and self.system.observer is not None:
            self.system.observer.set_user_context(rendered_context)
        self._restore_state()
        self._closed = False

    async def start(self) -> None:
        """Run until stopped. ``setup`` is intentionally required first."""
        if self.pipeline is None:
            raise RuntimeError("PipelineOrchestrator.setup() must run before start()")
        if self.running:
            return

        self.running = True
        self.pipeline.resume()
        if self._started_at is None:
            self._started_at = datetime.utcnow()
        now = datetime.utcnow()
        self._last_state_save = now
        self._last_wal_checkpoint = now
        self._last_disk_check = now

        observer = self.system.observer
        if observer is None:
            self.running = False
            raise RuntimeError("TempoSystem has no observer")
        if not observer._running:
            observer.restart()
            logger.info("Observer restarted for resume")

        logger.info("Production pipeline starting")
        await self._observation_loop()

    async def _observation_loop(self) -> None:
        assert self.pipeline is not None
        observer = self.system.observer
        assert observer is not None
        last_loop_wall = time.time()
        observer_restart_attempts = 0

        while self.running:
            try:
                now_wall = time.time()
                gap = now_wall - last_loop_wall
                last_loop_wall = now_wall
                if gap > SLEEP_GAP_THRESHOLD_SECONDS:
                    logger.info("Detected probable sleep/wake: %.0fs loop gap", gap)
                    try:
                        await self.pipeline.flush_buffers()
                    except Exception as exc:
                        logger.warning("Post-wake buffer flush failed: %s", exc)
                    now = datetime.utcnow()
                    self._last_state_save = now
                    self._last_wal_checkpoint = now
                    self._last_disk_check = now
                    self.pipeline.last_entity_produced_at = time.time()
                    self._pipeline_stall_last_alert = 0.0

                if not self.running:
                    break
                if not observer._running:
                    if observer_restart_attempts < MAX_OBSERVER_RESTART_ATTEMPTS:
                        observer_restart_attempts += 1
                        logger.warning(
                            "Restarting dead observer (%d/%d)",
                            observer_restart_attempts,
                            MAX_OBSERVER_RESTART_ATTEMPTS,
                        )
                        try:
                            observer.restart()
                        except Exception as exc:
                            logger.error("Observer restart failed: %s", exc)
                        await asyncio.sleep(2)
                        continue
                    if observer_restart_attempts == MAX_OBSERVER_RESTART_ATTEMPTS:
                        observer_restart_attempts += 1
                        logger.error("Observer recovery exhausted")
                else:
                    observer_restart_attempts = 0

                update = await observer.get_update()
                if update:
                    metadata = update.metadata or {}
                    text = update.content
                    # Use personal context in the observation sent to audit
                    # and inference.
                    # Older captures and skipped onboarding still use base text.
                    if self.pipeline.user_context:
                        text = metadata.get("ctx_transcription") or text
                    await self._safe_process(text, metadata)

                await self.pipeline.check_periodic_jobs()
                now = datetime.utcnow()
                if self._elapsed(self._last_state_save, now) >= STATE_SAVE_INTERVAL_SECONDS:
                    self.save_state()
                    self._last_state_save = now
                if self._elapsed(self._last_wal_checkpoint, now) >= WAL_CHECKPOINT_INTERVAL_SECONDS:
                    try:
                        await self.pipeline.db.checkpoint()
                    except Exception as exc:
                        logger.warning("WAL checkpoint failed: %s", exc)
                    self._last_wal_checkpoint = now
                if self._elapsed(self._last_disk_check, now) >= DISK_CHECK_INTERVAL_SECONDS:
                    self._check_disk_usage()
                    self._last_disk_check = now

                self._check_pipeline_stall()
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                self.running = False
                raise
            except Exception as exc:
                if is_network_error(exc):
                    report_pipeline_error("observation_loop", exc)
                    await self._wait_for_connectivity()
                    continue
                logger.error("Observation loop error: %s", exc, exc_info=self.system.debug)

    @staticmethod
    def _elapsed(previous: Optional[datetime], now: datetime) -> float:
        return (now - previous).total_seconds() if previous else 0

    async def _safe_process(self, text: str, metadata: dict) -> None:
        assert self.pipeline is not None
        try:
            await self.pipeline.process_observation(text, metadata)
        except Exception as exc:
            logger.error("Observation processing failed: %s", exc)

    async def get_status(self) -> Dict:
        pipeline_status: Dict = {}
        if self.pipeline is not None:
            try:
                pipeline_status = await self.pipeline.get_status()
            except Exception as exc:
                pipeline_status = {"error": str(exc)}
        elapsed_hours = 0.0
        if self._started_at:
            elapsed_hours = (datetime.utcnow() - self._started_at).total_seconds() / 3600
        return {
            "running": self.running,
            "elapsed_hours": round(elapsed_hours, 2),
            "pipeline": pipeline_status,
        }

    async def stop(self, *, flush_buffer: bool = False) -> None:
        """Pause all work while preserving buffered operations for resume."""
        self.running = False

        tasks = [task for task in self._background_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

        if self.pipeline is not None:
            try:
                self.save_state()
            except Exception as exc:
                logger.warning("State save on stop failed: %s", exc)
            if flush_buffer:
                try:
                    await self.pipeline.flush_buffers()
                except Exception as exc:
                    logger.warning("Buffer flush on stop failed: %s", exc)
            await self.pipeline.pause()

        if self.system.observer is not None:
            try:
                await self.system.observer.stop()
            except Exception as exc:
                logger.warning("Observer stop failed: %s", exc)
        logger.info("Pipeline orchestrator paused")

    async def close(self) -> None:
        """Release orchestrator-owned tasks and pipeline callback references."""
        if self._closed:
            return
        await self.stop()
        if self.pipeline is not None:
            await self.pipeline.close()
            self.pipeline = None
        self._closed = True
        logger.info("Pipeline orchestrator closed")

    def reload_user_context(self, user_context: UserContext) -> None:
        self.user_context = user_context
        rendered = user_context.render()
        if self.pipeline is not None:
            self.pipeline.user_context = rendered
        if self.system.observer is not None:
            self.system.observer.set_user_context(rendered)

    def trigger_goal_resynthesis(self) -> None:
        if self.pipeline is None or not self.running:
            return

        async def _run() -> None:
            assert self.pipeline is not None
            await self.pipeline.trigger_goal_resynthesis()

        self._spawn_background(_run(), name="goal-resynthesis")

    async def _wait_for_connectivity(self) -> None:
        poll_interval = 30
        elapsed = 0
        while self.running:
            try:
                await self.system.provider.chat_completion(
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                elapsed += poll_interval
                logger.warning("Waiting for provider connectivity (%ds)", elapsed)
                await asyncio.sleep(poll_interval)

    def save_state(self) -> None:
        if self.pipeline is None:
            return
        data = self.pipeline.save_state()
        data["_started_at"] = self._started_at.isoformat() if self._started_at else None
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.state_path)

    def _restore_state(self) -> None:
        if self.pipeline is None or not self.state_path.exists():
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.pipeline.restore_state(data)
            if data.get("_started_at"):
                self._started_at = datetime.fromisoformat(data["_started_at"])
            logger.info("Restored production pipeline state")
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Could not restore pipeline state: %s", exc)

    def _spawn_background(self, coro, name: str = "background") -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc:
                report_pipeline_error(f"background/{name}", exc)

        task.add_done_callback(_done)
        return task

    def _check_pipeline_stall(self) -> None:
        if self.pipeline is None:
            return
        now = time.time()
        if now - self._pipeline_stall_last_alert < PIPELINE_STALL_REPEAT_SECONDS:
            return
        stale_seconds = now - self.pipeline.last_entity_produced_at
        if stale_seconds < PIPELINE_STALL_SECONDS:
            return
        observer = self.system.observer
        if observer and (getattr(observer, "_exclusion_paused", False) or not observer._running):
            return
        message = f"Pipeline stall watchdog: no entities produced for {int(stale_seconds)}s"
        logger.error(message)
        report_pipeline_error(
            "pipeline_stall_watchdog",
            RuntimeError(message),
            {"seconds_since_entity": int(stale_seconds)},
        )
        self._pipeline_stall_last_alert = now

    def _check_disk_usage(self) -> None:
        if self.pipeline is None:
            return
        try:
            usage = shutil.disk_usage(self.pipeline.db.data_directory)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < DISK_FREE_WARNING_GB:
                logger.warning("Low disk space: %.1f GB free", free_gb)
        except Exception as exc:
            logger.debug("Disk usage check failed: %s", exc)

"""The full Tempo observation-to-goal pipeline."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from tempo.db import Database
from tempo.models import EntityType
from tempo.providers import ModelProvider
from tempo.store import Store
from tempo.buffer import OperationBuffer, BufferedOperation

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger

from tempo.pipelines.observation_to_operation import ObservationAdapter
from tempo.pipelines.operations_to_actions import ActionBuilder
from tempo.pipelines.action_to_activities import ActivityProposeJob

from tempo.pipelines.audit import AuditPipeline

logger = logging.getLogger(__name__)

# Reconcile and goal-synthesis thresholds for the online pipeline.
RECONCILE_WARMUP_BATCHES = 1       # Defer reconcile for first N buffer triggers (cold start)
RECONCILE_GROUP_SIZE = 1           # Buffer triggers between reconcile runs
GOAL_SYNTHESIS_MIN_NEW_ACTIVITIES = 5
GOAL_SYNTHESIS_INTERVAL_SECONDS = 60 * 60 * 24  # 24-hour ceiling

class TempoPipeline:
    """Convert audited observations into a hierarchy using personal context."""

    def __init__(
        self,
        db: Database,
        provider: ModelProvider,
        user_name: str = "the user",
        user_context: str = "",
        debug: bool = False,
        debug_logger: Optional["DebugLogger"] = None,
        on_critical_state_change: Optional["Callable[[], None]"] = None,
        stage1_provider: Optional[ModelProvider] = None,
    ):
        self.db = db
        self.provider = provider
        # Stage 1 (operation extraction) can run on a cheaper/faster model.
        # Falls through to the main provider when not set.
        self.stage1_provider = stage1_provider or provider
        self.user_name = user_name
        self.user_context = user_context
        self.debug = debug
        self.debug_logger = debug_logger
        # Called after reconcile/synthesis to force immediate state save
        self._on_critical_state_change = on_critical_state_change

        # Statistics
        self.observation_count = 0
        self.error_count = 0
        self.audit_blocked_count = 0
        self.last_entity_produced_at: float = time.time()  # for pipeline stall watchdog

        self.audit_pipeline = AuditPipeline(
            provider=provider,
            db=db,
            user_name=user_name,
            entity_type=EntityType.OPERATION,
            debug=debug,
            debug_logger=debug_logger,
        )

        self._op_buffer = OperationBuffer(
            on_trigger=self._on_buffer_trigger,
            time_threshold_seconds=300.0,
            max_buffer_size=20,
            debug=debug,
        )
        self._batches_processed = 0
        self._warmup_done = False
        self._pending_reconcile_ids: List[int] = []
        self._batches_since_reconcile = 0
        self._new_activities_since_synthesis = 0
        self._last_goal_synthesis: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_observation(self, transcription_text: str, metadata: Optional[dict] = None) -> None:
        """Process a single observation transcription.

        Observations are first audited for privacy compliance. If the audit
        blocks transmission, the observation is skipped.
        """
        self.observation_count += 1

        try:
            # Privacy audit gate
            if self.audit_pipeline:
                should_transmit, audit_result = await self.audit_pipeline.audit(
                    transcription_text, metadata
                )
                if metadata is None:
                    metadata = {}
                metadata["audit_result"] = audit_result

                if not should_transmit:
                    self.audit_blocked_count += 1
                    logger.info(
                        "Observation blocked by audit: %s",
                        audit_result.get("reasoning", "no reason"),
                    )
                    return

            await self._process_observation(transcription_text, metadata)

        except Exception as e:
            self.error_count += 1
            logger.error(
                "Error processing observation: %s", e
            )
            if self.debug:
                logger.exception("Full traceback:")

    async def check_periodic_jobs(self) -> None:
        """Check and run periodic jobs (time-based triggers, goal synthesis)."""
        try:
            await self._op_buffer.check_time_trigger()
            await self._check_goal_synthesis()
        except Exception as e:
            logger.error(
                "Error in periodic jobs: %s", e
            )

    async def get_status(self) -> Dict:
        """Get current pipeline status and metrics."""
        status = {
            "observations": self.observation_count,
            "errors": self.error_count,
            "audit_blocked": self.audit_blocked_count,
        }

        try:
            async with self.db.session() as session:
                store = Store(session)
                status["operations"] = await store.entities.count_by_type(EntityType.OPERATION)
                status["actions"] = await store.entities.count_by_type(EntityType.ACTION)
                status["activities"] = await store.entities.count_by_type(EntityType.ACTIVITY)
                status["goals"] = await store.entities.count_by_type(EntityType.GOAL)
        except Exception as e:
            logger.warning("Status query failed: %s", e)

        return status

    async def flush_buffers(self) -> None:
        """Force-flush all buffers (called on sleep/wake gaps or optional stop).

        This triggers the pipeline callbacks for any buffered items, ensuring
        no data is lost when the system pauses.
        """
        try:
            if not self._op_buffer.is_empty():
                logger.info("Flushing %d buffered operations", self._op_buffer.size())
                await self._op_buffer.flush()
        except Exception as e:
            logger.error("Buffer flush failed: %s", e)

    async def pause(self) -> None:
        """Stop the buffer timer without discarding pending operations."""
        await self._op_buffer.stop_timer()

    def resume(self) -> None:
        """Restart the inactivity timer for restored or paused work."""
        self._op_buffer.start_timer_if_needed()

    async def close(self) -> None:
        """Release pipeline-owned tasks and callback references."""
        await self._op_buffer.close()
        self._on_critical_state_change = None
        if self.debug_logger is not None:
            self.debug_logger.close()
            self.debug_logger = None

    async def trigger_goal_resynthesis(self) -> None:
        """Re-run goal synthesis (called after user edits entities).

        Reconcile pending activity IDs first, then synthesize goals.
        """
        try:
            if self._pending_reconcile_ids:
                logger.info("Re-synthesis: reconciling %d pending IDs first", len(self._pending_reconcile_ids))
                await self._reconcile_and_synthesize(self._pending_reconcile_ids)
                self._pending_reconcile_ids = []
                self._batches_since_reconcile = 0
            else:
                await self._run_goal_synthesis()
            self._force_state_save("user_resynthesis")
        except Exception as e:
            logger.error("Goal re-synthesis failed: %s", e)

    # ------------------------------------------------------------------
    # Production hierarchy
    # ------------------------------------------------------------------

    async def _process_observation(self, transcription_text: str, metadata: Optional[dict] = None) -> None:
        """Stage 1: Transcription → Operations → buffer."""
        screenshot_path = metadata.get("screenshot_path") if metadata else None

        async with self.db.session() as session:
            store = Store(session)
            adapter = ObservationAdapter(
                self.stage1_provider,
                store,
                debug=self.debug,
                user_name=self.user_name,
                user_context=self.user_context,
                debug_logger=self.debug_logger,
            )
            op_ids = await adapter.process_observation(
                transcription_text,
                screenshot_path=screenshot_path,
            )

            if op_ids:
                self.last_entity_produced_at = time.time()

            # Collect operations for buffer
            ops_for_buffer = []
            for op_id in op_ids:
                entity = await store.entities.get(op_id)
                if entity:
                    ops_for_buffer.append(
                        BufferedOperation(
                            entity_id=entity.id,
                            text=entity.text,
                            timestamp=entity.timestamp_start,
                        )
                    )

        # Add to buffer outside session
        for op in ops_for_buffer:
            await self._op_buffer.add(op)

    async def _on_buffer_trigger(self, operations: List[BufferedOperation]) -> None:
        """Stage 2 → Stage 3 → Reconcile → Goal Synthesis.

        Runs the interleaved production stages:
          1. Operations → Actions (Stage 2)
          2. Actions → Activities via propose_only (Stage 3)
          3. Accumulate created activity IDs
          4. After warmup batches: reconcile → goal synthesis
          5. Then every RECONCILE_GROUP_SIZE batches: reconcile → goal synthesis
        """
        # Stage 2: Operations → Actions
        async with self.db.session() as session:
            store = Store(session)
            builder = ActionBuilder(
                self.provider,
                store,
                debug=self.debug,
                user_name=self.user_name,
                user_context=self.user_context,
                debug_logger=self.debug_logger,
            )
            action_ids = await builder.build_actions(operations)

            if action_ids:
                self.last_entity_produced_at = time.time()

            if self.debug:
                logger.info(
                    "Built %d actions from %d operations",
                    len(action_ids),
                    len(operations),
                )

        # Stage 3: Activity assignment
        created_ids = []
        if action_ids:
            try:
                async with self.db.session() as session:
                    store = Store(session)
                    job = ActivityProposeJob(
                        provider=self.provider,
                        store=store,
                        debug=self.debug,
                        user_name=self.user_name,
                        user_context=self.user_context,
                        debug_logger=self.debug_logger,
                    )
                    result = await job.run(action_ids=action_ids)
                    created_ids = result.get("created_activity_ids", [])

                    # Always log propose_only result for diagnostics
                    logger.info(
                        "propose_only result: created=%d created_ids=%s selected=%s note=%s",
                        result.get("created", 0),
                        created_ids,
                        result.get("selected", 0),
                        result.get("note", ""),
                    )
            except Exception as e:
                logger.error("Stage 3 propose failed: %s", e)
                logger.exception("Stage 3 propose traceback:")
                # Continue — actions are already in DB, activities can be assigned next reconcile cycle

        # Accumulate candidate IDs for reconcile
        if created_ids:
            self._pending_reconcile_ids.extend(created_ids)

        # Reconcile + Goal Synthesis (warmup then periodic grouping)
        # Trigger on ANY action production — activity landscape changes via
        # match/revise/merge too, not just new creates.
        if action_ids:
            self._batches_processed += 1
            logger.info(
                "Buffer trigger: action_ids=%d created_ids=%s warmup_done=%s batches=%d/%d pending=%d",
                len(action_ids), created_ids,
                self._warmup_done, self._batches_processed, RECONCILE_WARMUP_BATCHES,
                len(self._pending_reconcile_ids),
            )

            if not self._warmup_done:
                logger.info(
                    "Warmup batch %d/%d (pending_ids=%d)",
                    self._batches_processed,
                    RECONCILE_WARMUP_BATCHES, len(self._pending_reconcile_ids),
                )
                if self._batches_processed >= RECONCILE_WARMUP_BATCHES:
                    logger.info("Warmup complete — triggering reconcile + synthesis")
                    await self._reconcile_and_synthesize(self._pending_reconcile_ids)
                    self._pending_reconcile_ids = []
                    self._warmup_done = True
                    self._force_state_save("warmup_reconcile")
            else:
                self._batches_since_reconcile += 1
                if self._batches_since_reconcile >= RECONCILE_GROUP_SIZE:
                    logger.info("Periodic reconcile triggered")
                    await self._reconcile_and_synthesize(self._pending_reconcile_ids)
                    self._pending_reconcile_ids = []
                    self._batches_since_reconcile = 0
                    self._force_state_save("periodic_reconcile")

    async def _reconcile_and_synthesize(self, candidate_ids: List[int]) -> None:
        """Run ActivityReconcileJob then GoalSynthesisJob.

        Reconcile is skipped when candidate_ids is empty, but goal synthesis
        ALWAYS runs — the activity landscape changes via match/revise/merge
        too, not just new creates.
        """
        from tempo.pipelines.activity_reconcile import ActivityReconcileJob
        from tempo.pipelines.goal_synthesis import GoalSynthesisJob

        # Reconcile (only if there are candidates to integrate)
        if candidate_ids:
            logger.info("Starting reconcile with %d activity IDs: %s", len(candidate_ids), candidate_ids)
            try:
                async with self.db.session() as session:
                    store = Store(session)
                    job = ActivityReconcileJob(
                        provider=self.provider,
                        store=store,
                        user_name=self.user_name,
                        debug=self.debug,
                        skip_probation=True,
                        include_goal_conflicts=True,
                        user_context=self.user_context,
                    )
                    reconcile_result = await job.run(candidate_ids)

                    # Track ALL activity changes for fallback trigger
                    total_changes = sum(
                        reconcile_result.get(k, 0)
                        for k in ("matched", "revised", "created", "merged")
                    )
                    self._new_activities_since_synthesis += total_changes

                    logger.info("Reconcile completed: %s", reconcile_result)
            except Exception as e:
                logger.error("Reconcile failed: %s", e)
                logger.exception("Reconcile traceback:")
        else:
            logger.info("No candidates to reconcile — skipping to goal synthesis")

        # Goal synthesis (always runs — activity landscape may have changed)
        try:
            async with self.db.session() as session:
                store = Store(session)
                job = GoalSynthesisJob(
                    provider=self.provider,
                    store=store,
                    debug=self.debug,
                    mode="incremental",
                    user_name=self.user_name,
                    user_context=self.user_context,
                    debug_logger=self.debug_logger,
                )
                synthesis_result = await job.run()
                self._last_goal_synthesis = datetime.utcnow()
                self._new_activities_since_synthesis = 0

                logger.info("Goal synthesis completed: %s", synthesis_result)
        except Exception as e:
            logger.error("Goal synthesis failed: %s", e)
            logger.exception("Goal synthesis traceback:")

    async def _check_goal_synthesis(self) -> None:
        """Fallback: run goal synthesis on evidence/time ceiling if reconcile hasn't triggered it."""
        now = datetime.utcnow()
        evidence_trigger = self._new_activities_since_synthesis >= GOAL_SYNTHESIS_MIN_NEW_ACTIVITIES
        time_ceiling = (
            self._last_goal_synthesis is None
            or (now - self._last_goal_synthesis).total_seconds() >= GOAL_SYNTHESIS_INTERVAL_SECONDS
        )

        if not (evidence_trigger or time_ceiling):
            return

        await self._run_goal_synthesis()

    async def _run_goal_synthesis(self) -> None:
        """Run a single goal synthesis pass."""
        from tempo.pipelines.goal_synthesis import GoalSynthesisJob

        try:
            async with self.db.session() as session:
                store = Store(session)
                job = GoalSynthesisJob(
                    provider=self.provider,
                    store=store,
                    debug=self.debug,
                    mode="incremental",
                    user_name=self.user_name,
                    user_context=self.user_context,
                    debug_logger=self.debug_logger,
                )
                result = await job.run()
                self._last_goal_synthesis = datetime.utcnow()
                self._new_activities_since_synthesis = 0

                if self.debug:
                    logger.info(
                        "Goal synthesis: %s", result
                    )
        except Exception as e:
            logger.error("Goal synthesis failed: %s", e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _force_state_save(self, reason: str = "unknown") -> None:
        """Request immediate state save after critical state changes (reconcile, synthesis).

        Prevents state loss if the process crashes before the next periodic save.
        """
        logger.info("Requesting immediate state save (reason: %s)", reason)
        if self._on_critical_state_change:
            try:
                self._on_critical_state_change()
            except Exception as e:
                logger.warning("State save callback failed: %s", e)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def save_state(self) -> Dict:
        """Serialize pipeline state for persistence across restarts."""
        state: Dict = {
            "observation_count": self.observation_count,
            "error_count": self.error_count,
            "audit_blocked_count": self.audit_blocked_count,
        }

        ops = [
            {
                "entity_id": op.entity_id,
                "text": op.text,
                "timestamp": op.timestamp.isoformat() if op.timestamp else None,
            }
            for op in self._op_buffer.operations
        ]
        state["buffer"] = {
            "items": ops,
            "last_add_time": (
                self._op_buffer.last_operation_time.isoformat()
                if self._op_buffer.last_operation_time
                else None
            ),
        }
        state["goal_synthesis"] = {
            "new_activities_since": self._new_activities_since_synthesis,
            "last_run": (
                self._last_goal_synthesis.isoformat()
                if self._last_goal_synthesis
                else None
            ),
        }
        state["reconcile"] = {
            "batches_processed": self._batches_processed,
            "warmup_done": self._warmup_done,
            "pending_reconcile_ids": self._pending_reconcile_ids,
            "batches_since_reconcile": self._batches_since_reconcile,
        }

        state["last_saved"] = datetime.utcnow().isoformat()
        return state

    def restore_state(self, data: Dict) -> None:
        """Restore pipeline state from a previously saved dict."""
        self.observation_count = data.get("observation_count", 0)
        self.error_count = data.get("error_count", 0)
        self.audit_blocked_count = data.get("audit_blocked_count", 0)

        buf_data = data.get("buffer", {})
        items = buf_data.get("items", [])

        if items:
            for item in items:
                try:
                    ts = datetime.fromisoformat(item["timestamp"])
                except (ValueError, KeyError, TypeError):
                    ts = datetime.utcnow()
                self._op_buffer.operations.append(
                    BufferedOperation(
                        entity_id=item.get("entity_id"),
                        text=item.get("text", ""),
                        timestamp=ts,
                    )
                )
            if buf_data.get("last_add_time"):
                try:
                    self._op_buffer.last_operation_time = datetime.fromisoformat(
                        buf_data["last_add_time"]
                    )
                except (ValueError, TypeError):
                    pass
            self._op_buffer.start_timer_if_needed()

            gs = data.get("goal_synthesis", {})
            self._new_activities_since_synthesis = gs.get("new_activities_since", 0)
            if gs.get("last_run"):
                try:
                    self._last_goal_synthesis = datetime.fromisoformat(gs["last_run"])
                except (ValueError, TypeError):
                    pass

            rc = data.get("reconcile", {})
            self._batches_processed = rc.get("batches_processed", 0)
            self._warmup_done = rc.get("warmup_done", False)
            # Back-compat: old state files may have warmup_created_ids
            self._pending_reconcile_ids = rc.get("pending_reconcile_ids", [])
            old_warmup_ids = rc.get("warmup_created_ids", [])
            if old_warmup_ids and not self._warmup_done:
                self._pending_reconcile_ids.extend(old_warmup_ids)
            self._batches_since_reconcile = rc.get("batches_since_reconcile", 0)

        restored_items = len(items)
        if restored_items:
            logger.info(
                "Restored state: %d observations, %d buffer items",
                self.observation_count,
                restored_items,
            )

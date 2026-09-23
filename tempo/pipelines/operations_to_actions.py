# operations_to_actions.py

from __future__ import annotations
import json
import time
from datetime import datetime
import asyncio
from typing import List, Optional, TYPE_CHECKING
import os

from tempo.prompts.action_generation import ACTION_GENERATION_PROMPT
from tempo.schemas import ActionItem, SegmentItem, SegmentationWithActionsResult, get_schema
from tempo.models import Entity, EntityType, RelationSubtype
from tempo.buffer import BufferedOperation
from tempo.utils import get_debug_logger, parse_llm_json

SEGMENTATION_WITH_ACTIONS_FORMAT = get_schema(SegmentationWithActionsResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger
    from tempo.providers import ModelProvider
    from tempo.store import Store


class ActionBuilder:
    """
    Pipeline: Operations → Actions
    
    Creates actions from buffered operations.
    Uses LLM to analyze operations and identify coherent goal-directed actions.
    """
    
    def __init__(
        self,
        provider: "ModelProvider",
        store: "Store",
        recent_actions_limit: int = 5,
        debug: bool = False,
        user_name: Optional[str] = None,
        experiment_user_description: Optional[str] = None,
        prompt_override: Optional[str] = None,
        user_context: str = "",
        debug_logger: Optional["DebugLogger"] = None,
    ):
        """
        Initialize the action builder.

        Args:
            provider: LLM provider for action generation.
            store: Database store for persisting actions.
            recent_actions_limit: Number of recent actions to include as context.
            prompt_override: Custom prompt template to use instead of default.
            user_context: Rendered context block for +C study conditions (empty for production).
            debug_logger: Structured JSONL debug logger.
        """
        self.provider = provider
        self.store = store
        self.recent_actions_limit = recent_actions_limit
        self.debug = debug
        self.log = get_debug_logger(self, debug=debug)
        self.user_name = user_name or os.getenv("USER_NAME", "the user")
        self.experiment_user_description = experiment_user_description
        self.prompt_override = prompt_override
        self.user_context = user_context
        self.debug_logger = debug_logger
    
    async def build_actions(
        self,
        operations: List[BufferedOperation],
        *,
        enable_cross_batch_chain: bool = True,
    ) -> List[int]:
        """
        Build actions from a list of buffered operations.
        
        Args:
            operations: List of buffered operations to process.
            
        Returns:
            List of created action IDs.
        """
        if not operations:
            self.log.debug("build_actions: received 0 operations; skipping")
            return []
        self.log.debug("build_actions: received %d operations", len(operations))
        
        prompt_template = self.prompt_override or ACTION_GENERATION_PROMPT
        needs_recent = "{recent_actions}" in prompt_template
        needs_related = "{related_entities}" in prompt_template

        # Get recent actions for context (and for cross-batch chaining if enabled)
        recent_actions = await self._get_recent_actions() if (needs_recent or enable_cross_batch_chain) else []
        self.log.debug("build_actions: recent_actions=%d", len(recent_actions))
        
        # Get related entities from graph
        related_entities = await self._get_related_entities(operations) if needs_related else []
        self.log.debug("build_actions: related_entities=%d", len(related_entities))
        
        # Single-call design: segment + action in one LLM call
        action_items = await self._segment_and_generate_actions(
            operations,
            recent_actions,
            related_entities,
        )
        self.log.debug("build_actions: generated action_items=%d", len(action_items))
        
        # Store actions and create relations
        # Note: recent_actions are ordered by timestamp_start DESC (most recent first)
        action_ids = []
        previous_action_in_batch = None  # Tracks actions created in this batch
        
        # Find the most recent existing action for potential chaining
        latest_existing_action = recent_actions[0] if recent_actions and enable_cross_batch_chain else None
        
        for item in action_items:
            action_id = await self._create_action(
                item, 
                operations, 
                previous_action_in_batch=previous_action_in_batch,
                latest_existing_action=latest_existing_action,
            )
            if action_id:
                action_ids.append(action_id)
                # Update previous_action_in_batch for next iteration (for chaining within batch)
                previous_action_in_batch = await self.store.entities.get(action_id)
                # Clear latest_existing_action after first use (only chain to it once)
                latest_existing_action = None
                self.log.debug("build_actions: created action id=%s", action_id)
        
        # Create CO_OCCURS (same-batch) and OVERLAPS (cross-batch time overlap) relations
        if action_ids:
            await self._create_co_occurs_relations(action_ids)
            await self._create_overlaps_relations(action_ids)

        return action_ids

    async def build_prompt(
        self,
        operations: List[BufferedOperation],
    ) -> str:
        """Build the stage 2 prompt for a batch without calling the LLM."""
        prompt_template = self.prompt_override or ACTION_GENERATION_PROMPT
        needs_recent = "{recent_actions}" in prompt_template
        needs_related = "{related_entities}" in prompt_template
        recent_actions = await self._get_recent_actions() if needs_recent else []
        related_entities = await self._get_related_entities(operations) if needs_related else []
        return await self._build_prompt(operations, recent_actions, related_entities, prompt_template=prompt_template)

    async def _get_current_goals(self) -> str:
        """Fetch current life goals for top-down context in action generation."""
        goals = await self.store.entities.get_by_type(EntityType.GOAL, limit=20)
        if not goals:
            return "No goals established yet."
        return "\n".join(f"- {g.text}" for g in goals)

    async def store_actions_from_items(
        self,
        action_items: List[ActionItem],
        operations: List[BufferedOperation],
        *,
        enable_cross_batch_chain: bool = True,
    ) -> List[int]:
        """Persist pre-generated action items to the DB."""
        if not action_items:
            return []
        action_ids = []
        previous_action_in_batch = None
        latest_existing_action = None
        if enable_cross_batch_chain:
            recent_actions = await self._get_recent_actions()
            latest_existing_action = recent_actions[0] if recent_actions else None
        for item in action_items:
            action_id = await self._create_action(
                item,
                operations,
                previous_action_in_batch=previous_action_in_batch,
                latest_existing_action=latest_existing_action,
            )
            if action_id:
                action_ids.append(action_id)
                previous_action_in_batch = await self.store.entities.get(action_id)
                latest_existing_action = None
        # Create CO_OCCURS and OVERLAPS relations for this batch too
        if action_ids:
            await self._create_co_occurs_relations(action_ids)
            await self._create_overlaps_relations(action_ids)
        return action_ids

    async def _get_recent_actions(self) -> List[Entity]:
        """Get recent actions for context."""
        return await self.store.entities.get_by_type(
            EntityType.ACTION,
            limit=self.recent_actions_limit,
        )
    
    async def _get_related_entities(
        self,
        operations: List[BufferedOperation],
    ) -> List[Entity]:
        """Get entities related to the operations via graph relations."""
        related = []
        
        for op in operations:
            if op.entity_id:
                entities = await self.store.relations.get_related_entities(
                    op.entity_id,
                    direction="both",
                )
                related.extend(entities)
        
        # Deduplicate
        seen = set()
        unique = []
        for e in related:
            if e.id not in seen:
                seen.add(e.id)
                unique.append(e)
        
        return unique[:10]  # Limit context size
    
    async def _segment_and_generate_actions(
        self,
        operations: List[BufferedOperation],
        recent_actions: List[Entity],
        related_entities: List[Entity],
    ) -> List[ActionItem]:
        """Single LLM call: segment + one action per segment."""
        prompt = await self._build_prompt(operations, recent_actions, related_entities)

        self.log.debug("_segment_and_generate_actions: prompt_len=%d ops=%d", len(prompt), len(operations))

        max_attempts = 3
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.monotonic()
                response = await self.provider.chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    response_format=SEGMENTATION_WITH_ACTIONS_FORMAT,
                )
                latency_ms = (time.monotonic() - t0) * 1000
                self.log.debug("_segment_and_generate_actions: response_len=%d", len(response or ""))
                if self.debug_logger:
                    self.debug_logger.log_llm_call(
                        pipeline="operations_to_actions", stage="segment_and_generate",
                        model=self.provider.model,
                        prompt=prompt, response=response,
                        latency_ms=latency_ms, response_format="json_schema",
                    )
                return self._parse_action_items_from_response(response, operations)
            except Exception as exc:
                last_error = exc
                self.log.warning(
                    "_segment_and_generate_actions: parse failed (attempt %d/%d): %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(0.2 * attempt)
        if last_error:
            raise last_error
        return []

    async def _build_prompt(
        self,
        operations: List[BufferedOperation],
        recent_actions: List[Entity],
        related_entities: List[Entity],
        *,
        prompt_template: Optional[str] = None,
    ) -> str:
        # Enrich operations block with selected operation-level metadata (if available)
        # so the LLM can see tool / social / rule context when clustering.
        ops_lines: List[str] = []
        for idx, op in enumerate(operations):
            meta_str = ""
            if op.entity_id:
                try:
                    ent = await self.store.entities.get(op.entity_id)
                except Exception:
                    ent = None
                if ent and ent.metadata_dict:
                    om = ent.metadata_dict
                    tool_kind = om.get("tool_kind")
                    social_target = om.get("social_target")
                    rule_tags = om.get("rule_tags") or []
                    auto_hint = om.get("automaticity_hint")
                    affect = om.get("affect_hint")
                    parts: List[str] = []
                    if tool_kind:
                        parts.append(f"tool_kind={tool_kind}")
                    if social_target:
                        parts.append(f"social_target={social_target}")
                    if rule_tags:
                        # Keep rule tags compact
                        parts.append("rules=" + ",".join(str(r) for r in rule_tags[:5]))
                    if auto_hint:
                        parts.append(f"automaticity={auto_hint}")
                    if affect:
                        parts.append(f"affect={affect}")
                    if parts:
                        meta_str = " | " + " ".join(parts)
            ops_lines.append(
                f"{idx+1}. {op.timestamp.isoformat()} - {op.text}{meta_str}"
            )
        ops_list = "\n".join(ops_lines)

        if recent_actions:
            recent_actions_text = "\n".join([
                f"- (ID: {a.id}) {a.timestamp_start.isoformat()}: {a.text}"
                for a in recent_actions
            ])
        else:
            recent_actions_text = "No recent actions."

        if related_entities:
            related_text = "\n".join([
                f"- [{e.type}] (ID: {e.id}): {e.text[:100]}"
                for e in related_entities
            ])
        else:
            related_text = "No related entities."

        prompt_template = prompt_template or self.prompt_override or ACTION_GENERATION_PROMPT

        # Build format kwargs
        fmt_kwargs = dict(
            operations_list=ops_list,
            recent_actions=recent_actions_text,
            related_entities=related_text,
            user_name=self.user_name,
            user_context=self.user_context,
        )

        # Add current_goals if the template uses it
        if "{current_goals}" in prompt_template:
            fmt_kwargs["current_goals"] = await self._get_current_goals()

        prompt = prompt_template.format(**fmt_kwargs)
        if self.experiment_user_description:
            prompt = f"[User Context: {self.experiment_user_description}]\n\n{prompt}"
        return prompt

    @staticmethod
    def _parse_action_items_from_response(
        response: str,
        operations: List[BufferedOperation],
    ) -> List[ActionItem]:
        try:
            data = parse_llm_json(response)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}
            segments_raw = data.get("segments", [])

            segments: List[SegmentItem] = []
            seg_entries: List[Tuple[int, SegmentItem]] = []
            for raw_idx, seg in enumerate(segments_raw):
                try:
                    item = SegmentItem(
                        label=seg.get("label", ""),
                        start_index=seg.get("start_index"),
                        end_index=seg.get("end_index"),
                    )
                except Exception:
                    continue
                segments.append(item)
                seg_entries.append((raw_idx, item))

            # Validate: non-empty, sorted, non-overlap, full coverage
            if not segments:
                raise ValueError("No valid segments")
            segments = sorted(segments, key=lambda s: s.start_index)
            n = len(operations)
            covered = []
            for s in segments:
                if not (1 <= s.start_index <= s.end_index <= n):
                    raise ValueError("Segment bounds invalid")
                covered.extend(range(s.start_index, s.end_index + 1))
            if len(covered) != n or sorted(covered) != list(range(1, n + 1)):
                raise ValueError("Segments do not fully cover operations or overlap")

            actions: List[ActionItem] = []
            # Map segments to their corresponding raw payload by original index, then sort.
            seg_entries.sort(key=lambda pair: pair[1].start_index)
            for raw_idx, seg in seg_entries:
                seg_ops = operations[seg.start_index - 1: seg.end_index]
                seg_raw = segments_raw[raw_idx] if raw_idx < len(segments_raw) else {}
                action_payload = seg_raw.get("action") or {}
                conf = action_payload.get("confidence")
                dec = action_payload.get("decay")
                metadata = action_payload.get("metadata") or None
                if conf is not None:
                    conf = str(conf)
                if dec is not None:
                    dec = str(dec)
                # Ensure metadata is a dict if present (Gemini may return string for dict fields)
                if metadata is not None and not isinstance(metadata, dict):
                    if isinstance(metadata, str):
                        try:
                            metadata = json.loads(metadata)
                        except (json.JSONDecodeError, ValueError):
                            metadata = {"raw": metadata} if metadata else None
                    else:
                        metadata = None
                actions.append(ActionItem(
                    text=action_payload.get("text", ""),
                    reasoning=action_payload.get("reasoning"),
                    timestamp_start=action_payload.get("timestamp_start", ""),
                    timestamp_end=action_payload.get("timestamp_end", ""),
                    confidence=conf,
                    decay=dec,
                    metadata=metadata,
                    operation_ids=[op.entity_id for op in seg_ops if op.entity_id],
                    goal_hint=action_payload.get("goal_hint"),
                ))
            if actions:
                return actions
            raise ValueError("No actions parsed")
        except Exception:
            # Fallback: single action for all ops
            if not operations:
                return []
            return [ActionItem(
                text=f"Activity during {operations[0].timestamp.strftime('%H:%M')} - {operations[-1].timestamp.strftime('%H:%M')}",
                timestamp_start=operations[0].timestamp.isoformat(),
                timestamp_end=operations[-1].timestamp.isoformat(),
                operation_ids=[op.entity_id for op in operations if op.entity_id],
                goal_hint=None,
            )]
    
    async def _create_action(
        self,
        item: ActionItem,
        operations: List[BufferedOperation],
        previous_action_in_batch: Optional[Entity] = None,
        latest_existing_action: Optional[Entity] = None,
    ) -> Optional[int]:
        """
        Create an action entity and its relations.
        
        Args:
            item: The action item to create.
            operations: All buffered operations.
            previous_action_in_batch: Previous action created in the current batch (for chaining within batch).
            latest_existing_action: Most recent existing action from database (for chaining to history).
            
        Returns:
            The created action ID, or None on failure.
        """
        # Validate operation_ids against known-good buffered IDs to prevent
        # FK constraint failures from hallucinated or stale IDs.
        valid_op_ids = {op.entity_id for op in operations if op.entity_id}
        operation_ids = [
            op_id for op_id in item.operation_ids
            if isinstance(op_id, int) and op_id in valid_op_ids
        ]

        # If no valid IDs, use all operation IDs
        if not operation_ids:
            operation_ids = list(valid_op_ids)
            self.log.error(
                "_create_action: no valid operation_ids in ActionItem (got %s, valid=%s), using all as fallback",
                item.operation_ids, valid_op_ids,
            )

        # Derive action timestamps from the operations that belong to this action.
        op_id_set = set(operation_ids)
        op_timestamps = [
            op.timestamp
            for op in operations
            if op.entity_id in op_id_set and op.timestamp
        ]
        if not op_timestamps:
            op_timestamps = [op.timestamp for op in operations if op.timestamp]
        if op_timestamps:
            timestamp_start = min(op_timestamps)
            timestamp_end = max(op_timestamps)
            if len(op_timestamps) == 1:
                timestamp_end = None
        else:
            timestamp_start = datetime.utcnow()
            timestamp_end = None
        
        # Create action with metadata
        metadata: dict = {}
        # Carry through structured metadata from ActionItem (Activity Theory / behavior-change metadata)
        if item.metadata and isinstance(item.metadata, dict):
            metadata.update(item.metadata)
        if item.goal_hint:
            metadata["goal_hint"] = item.goal_hint
        # Store LLM reasoning and confidence scores
        if item.reasoning:
            metadata["reasoning"] = item.reasoning
        if item.confidence:
            metadata["confidence"] = item.confidence
        if item.decay:
            metadata["decay"] = item.decay
        
        action = await self.store.create_action(
            text=item.text,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            operation_ids=operation_ids,
            metadata=metadata if metadata else None,
        )

        if self.debug_logger:
            self.debug_logger.log_entity_mutation(
                pipeline="operations_to_actions", stage="create_action",
                mutation="create", entity_id=action.id,
                entity_type="action", entity_text=item.text,
                metadata=metadata,
            )

        self.log.debug(
            "_create_action: created action id=%s text_len=%d ops=%d ts=%s op_ids=%s",
            action.id,
            len(item.text),
            len(operation_ids),
            timestamp_start.isoformat(),
            operation_ids,
        )
        # Chain from previous action in current batch to new action (if previous is earlier)
        if previous_action_in_batch and previous_action_in_batch.timestamp_start < timestamp_start:
            # previous_action_in_batch -> new_action (for forward traversal)
            await self.store.add_temporal_relation(
                source_id=previous_action_in_batch.id,  # previous action (earlier)
                target_id=action.id,  # new action (later)
                subtype=RelationSubtype.FOLLOWS,
            )
        
        # Chain from latest existing action to new action (only for first action in batch, if existing is earlier)
        if latest_existing_action and latest_existing_action.timestamp_start < timestamp_start:
            # latest_existing_action -> new_action (for forward traversal)
            await self.store.add_temporal_relation(
                source_id=latest_existing_action.id,  # existing action (earlier)
                target_id=action.id,  # new action (later)
                subtype=RelationSubtype.FOLLOWS,
            )
        
        return action.id

    async def _create_co_occurs_relations(self, action_ids: List[int]) -> None:
        """Create CO_OCCURS relations between all pairs of actions in the same batch.

        Actions created from the same batch of operations share a session/time window,
        so they co-occur even though their individual time ranges are contiguous.
        """
        if len(action_ids) < 2:
            return
        for i in range(len(action_ids)):
            for j in range(i + 1, len(action_ids)):
                try:
                    await self.store.add_temporal_relation(
                        source_id=action_ids[i],
                        target_id=action_ids[j],
                        subtype=RelationSubtype.CO_OCCURS,
                    )
                except Exception:
                    pass  # Ignore duplicate relation errors

    async def _create_overlaps_relations(self, action_ids: List[int]) -> None:
        """Detect and create OVERLAPS relations between new actions and recent existing actions.

        Two actions overlap if their time ranges intersect:
        overlap = (A.start < B.end) AND (B.start < A.end)
        """
        if not action_ids:
            return

        new_actions = []
        for aid in action_ids:
            ent = await self.store.entities.get(aid)
            if ent:
                new_actions.append(ent)

        recent = await self._get_recent_actions()
        new_ids = set(action_ids)

        for new_action in new_actions:
            new_start = new_action.timestamp_start
            new_end = new_action.timestamp_end or new_action.timestamp_start
            for existing in recent:
                if existing.id in new_ids:
                    continue
                ex_start = existing.timestamp_start
                ex_end = existing.timestamp_end or existing.timestamp_start
                if new_start < ex_end and ex_start < new_end:
                    try:
                        await self.store.add_temporal_relation(
                            source_id=min(new_action.id, existing.id),
                            target_id=max(new_action.id, existing.id),
                            subtype=RelationSubtype.OVERLAPS,
                        )
                    except Exception:
                        pass

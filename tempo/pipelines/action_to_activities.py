from __future__ import annotations
import json
import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict, TYPE_CHECKING, Any, Tuple
from dataclasses import dataclass
import os

from tempo.models import Entity, EntityType, RelationType, RelationSubtype
from tempo.schemas import PasProposeResult, get_schema
from tempo.utils import get_debug_logger, parse_llm_json, update_activity_scores, render_prompt_template
from tempo.prompts.activity_generation import PROPOSE_ONLY_PROMPT

PAS_PROPOSE_FORMAT = get_schema(PasProposeResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger
    from tempo.providers import ModelProvider
    from tempo.store import Store


@dataclass
class _PasCandidate:
    cid: str  # e.g., "C1"
    description: str
    example_action_ids: List[int]
    existing_activity_id: Optional[int] = None
    reasoning: Optional[str] = None  # LLM's justification for this candidate
    confidence: Optional[int] = None  # 1-10 confidence score
    action_valences: Optional[List[str]] = None  # parallel array: "supports"/"hinders"/"neutral" per action_id
    # Working sphere metadata (Phase 2)
    purpose: Optional[str] = None
    people: Optional[List[str]] = None
    resources: Optional[List[str]] = None
    temporal_pattern: Optional[str] = None
    engagement_profile: Optional[str] = None
    initiation_profile: Optional[str] = None
    identity_context: Optional[str] = None


@dataclass
class _ValenceEntry:
    """Valence information for an action-activity pair."""
    valence: str  # "supports", "hinders", "neutral", "unclear"
    strength: float  # 0.0 - 1.0
    reasoning: Optional[str] = None  # LLM's justification for this score


class ActivityProposeJob:
    """
    Propose candidate activities from actions, then apply selection.
    - Propose: LLM suggests candidate activities (can reuse existing ones).
    - Apply: Create/update activity entities, link actions, create behavioral relations.
    """

    def __init__(
        self,
        provider: "ModelProvider",
        store: "Store",
        time_horizon_hours: int = 24,
        sample_actions_limit: int = 50,
        debug: bool = False,
        log_stage3_json: bool = False,
        user_name: Optional[str] = None,
        dormant_threshold_days: int = 30,
        retired_threshold_days: int = 90,
        retired_min_usage: int = 5,
        propose_prompt_override: Optional[str] = None,
        experiment_user_description: Optional[str] = None,
        user_context: str = "",
        debug_logger: Optional["DebugLogger"] = None,
    ):
        self.provider = provider
        self.store = store
        self.time_horizon_hours = time_horizon_hours
        self.sample_actions_limit = sample_actions_limit
        self.debug = debug
        self.log_stage3_json = log_stage3_json
        self.log = get_debug_logger(self, debug=debug)
        self.user_name = user_name or os.getenv("USER_NAME", "the user")
        self.dormant_threshold_days = dormant_threshold_days
        self.retired_threshold_days = retired_threshold_days
        self.retired_min_usage = retired_min_usage
        self.propose_prompt_override = propose_prompt_override
        self.experiment_user_description = experiment_user_description
        self.user_context = user_context
        self.debug_logger = debug_logger

    async def run(self, action_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Propose candidate activities from actions and apply selection.

        Uses PROPOSE's action_ids as direct assignments.
        All candidates are treated as new — reconcile handles matching
        to existing activities.

        Args:
            action_ids: Optional list of specific action IDs to process.
                       If None, uses targeted selection.

        Returns:
            Dictionary with selection results.
        """
        if action_ids:
            actions = await self._get_actions_by_ids(action_ids)
        else:
            actions = await self._get_targeted_actions()

        if not actions:
            self.log.debug("run: no actions found")
            return {"selected": 0, "created": 0, "reassigned": 0, "created_activity_ids": [], "note": "no actions"}

        existing_activities = await self._existing_activities()
        self.log.debug("run: actions=%d existing_activities=%d", len(actions), len(existing_activities))
        candidates, context_action_ids = await self._propose_candidates(
            actions, existing_activities, default_prompt=PROPOSE_ONLY_PROMPT,
        )
        if not candidates:
            self.log.debug("run: no candidates proposed")
            return {"selected": 0, "created": 0, "reassigned": 0, "created_activity_ids": [], "note": "no candidates"}

        # In propose-only mode, all candidates are new — force existing_activity_id=None
        # so reconcile handles matching to existing activities instead.
        for cand in candidates:
            cand.existing_activity_id = None

        # Build selection directly from candidates' action_ids (greedy assignment)
        # _apply_selection looks up assignment by positional index, not action_id
        action_id_to_idx = {a.id: idx for idx, a in enumerate(actions)}

        # Expand with context actions (prior/concurrent) so the LLM can reference them
        context_actions = []
        for ctx_id in context_action_ids:
            if ctx_id not in action_id_to_idx:
                ent = await self.store.entities.get(ctx_id)
                if ent and ent.type == EntityType.ACTION:
                    action_id_to_idx[ctx_id] = len(actions) + len(context_actions)
                    context_actions.append(ent)
        all_actions = list(actions) + context_actions
        if context_actions:
            self.log.debug("run: expanded with %d context actions", len(context_actions))

        active_candidates = []
        assignment = {}  # positional index -> [candidate_indices]

        # Build sparse valence matrix from PROPOSE's action_valences
        # valence_matrix[action_idx][cand_idx] = _ValenceEntry
        num_actions = len(all_actions)
        num_candidates = len(candidates)
        valence_matrix: List[List[_ValenceEntry]] = [
            [_ValenceEntry(valence="neutral", strength=0.0) for _ in range(num_candidates)]
            for _ in range(num_actions)
        ]

        for cand_idx, cand in enumerate(candidates):
            valid_ids = [aid for aid in cand.example_action_ids if aid in action_id_to_idx]
            if valid_ids:
                active_candidates.append(cand_idx)
                # Build a map from action_id to its position in cand.example_action_ids
                # so we can look up the parallel valence array
                for pos, aid in enumerate(cand.example_action_ids):
                    if aid not in action_id_to_idx:
                        continue
                    idx = action_id_to_idx[aid]
                    if idx not in assignment:
                        assignment[idx] = []
                    assignment[idx].append(cand_idx)
                    # Set valence for this action-candidate pair
                    valence_str = "supports"
                    if cand.action_valences and pos < len(cand.action_valences):
                        valence_str = cand.action_valences[pos]
                    strength = 0.7 if valence_str in ("supports", "hinders") else 0.0
                    valence_matrix[idx][cand_idx] = _ValenceEntry(
                        valence=valence_str, strength=strength,
                    )

        selection = {"active_candidates": active_candidates, "assignment": assignment}

        try:
            created, reassigned, supports_created, hinders_created, created_activity_ids = await self._apply_selection(
                all_actions, candidates, selection,
                None,  # no membership matrix (greedy assignment from PROPOSE)
                valence_matrix,
            )
        except Exception as e:
            self.log.warning("run: _apply_selection failed (%s)", e)
            return {"selected": 0, "created": 0, "reassigned": 0, "created_activity_ids": [], "note": f"apply_failed: {e}"}

        self.log.debug(
            "run: completed active=%d created=%d reassigned=%d",
            len(active_candidates), created, reassigned,
        )
        return {
            "selected": len(active_candidates),
            "created": created,
            "reassigned": reassigned,
            "supports_created": supports_created,
            "hinders_created": hinders_created,
            "created_activity_ids": created_activity_ids,
        }

    async def _get_actions_by_ids(self, action_ids: List[int]) -> List[Entity]:
        """Get specific actions by their IDs."""
        actions = []
        for action_id in action_ids:
            action = await self.store.entities.get(action_id)
            if action and action.type == EntityType.ACTION:
                actions.append(action)
        # Sort by timestamp_start for consistency
        actions.sort(key=lambda a: a.timestamp_start)
        return actions
    
    async def _get_targeted_actions(self) -> List[Entity]:
        """
        Get actions that need activity assignment using targeted selection:
        - Unassigned actions (no activity)
        - Actions in unstable activities (low usage_count or very recent)
        """
        all_targeted = []
        seen_ids = set()

        # 1. Get unassigned actions (prioritize recent ones)
        since_recent = datetime.utcnow() - timedelta(hours=24)
        unassigned = await self.store.entities.get_unassigned_actions(
            since=since_recent,
            limit=50,
        )
        for action in unassigned:
            if action.id not in seen_ids:
                all_targeted.append(action)
                seen_ids.add(action.id)

        # 2. Get actions in unstable activities
        unstable_actions = await self.store.entities.get_actions_in_unstable_activities(
            max_usage_count=2,
            max_age_hours=24,
            limit=20,
        )
        for action in unstable_actions:
            if action.id not in seen_ids:
                all_targeted.append(action)
                seen_ids.add(action.id)

        # Sort by timestamp_start for consistency
        all_targeted.sort(key=lambda a: a.timestamp_start)

        self.log.debug(
            "get_targeted_actions: unassigned=%d unstable=%d total=%d",
            len(unassigned),
            len(unstable_actions),
            len(all_targeted),
        )

        return all_targeted

    async def _existing_activities(self) -> List[Entity]:
        """
        Get existing activities for PAS consideration.
        
        Returns:
            - All active activities (always considered)
            - Dormant activities from last 15 days (occasionally reused)
            - Excludes retired activities (not used for new assignments)
        """
        from datetime import timedelta
        
        all_activities = await self.store.entities.get_by_type(EntityType.ACTIVITY, limit=200)
        
        # Filter by status
        active = []
        dormant = []
        cutoff_time = datetime.utcnow() - timedelta(days=15)
        
        for activity in all_activities:
            meta = activity.metadata_dict or {}
            status = meta.get("status", "active")  # Default to active if not set
            
            if status == "retired":
                continue  # Skip retired activities
            
            if status == "active":
                active.append(activity)
            elif status == "dormant":
                # Only include dormant activities that were recently used
                last_assigned = meta.get("last_assigned_ts")
                if last_assigned:
                    try:
                        last_ts = datetime.fromisoformat(last_assigned.replace("Z", "+00:00"))
                        if last_ts.replace(tzinfo=None) >= cutoff_time:
                            dormant.append(activity)
                    except (ValueError, AttributeError):
                        pass  # If parsing fails, skip this dormant activity
        
        # Combine: all active + recently-used dormant
        result = active + dormant
        self.log.debug(
            "_existing_activities: total=%d active=%d dormant=%d (recent)",
            len(all_activities),
            len(active),
            len(dormant),
        )
        return result

    def _actions_block(self, actions: List[Entity]) -> str:
        """
        Format actions with selected metadata for PAS context.
        Shows: ID, text, goal_hint, ts, confidence, decay, duration,
        plus key Activity Theory / behavior-change hints.
        """
        lines = []
        for a in actions:
            meta = a.metadata_dict if a.metadata_dict and isinstance(a.metadata_dict, dict) else {}
            goal_hint = meta.get("goal_hint") or ""

            # Quality signals from Stage 2
            confidence = meta.get("confidence")
            decay = meta.get("decay")

            # Compute duration from timestamps
            duration_str = ""
            if a.timestamp_start and a.timestamp_end:
                dur_secs = (a.timestamp_end - a.timestamp_start).total_seconds()
                if dur_secs >= 3600:
                    duration_str = f"{dur_secs / 3600:.1f}h"
                elif dur_secs >= 60:
                    duration_str = f"{int(dur_secs // 60)}m"
                elif dur_secs > 0:
                    duration_str = f"{int(dur_secs)}s"

            # Extract selected action-level metadata fields
            object_label = meta.get("object_label")
            outcome_type = meta.get("outcome_type")
            domain = meta.get("domain")
            community = meta.get("community")
            tension_hint = meta.get("tension_hint")
            engagement_state = meta.get("engagement_state")
            cognitive_mode = meta.get("cognitive_mode")
            initiation = meta.get("initiation")
            social_mode = meta.get("social_mode")

            # Build a compact metadata summary string
            meta_parts: List[str] = []
            if confidence:
                meta_parts.append(f"conf={confidence}/10")
            if decay:
                meta_parts.append(f"decay={decay}/10")
            if duration_str:
                meta_parts.append(f"dur={duration_str}")
            if object_label:
                meta_parts.append(f"object={object_label}")
            if outcome_type:
                meta_parts.append(f"outcome={outcome_type}")
            if domain:
                meta_parts.append(f"domain={domain}")
            if community:
                meta_parts.append(f"community={community}")
            if tension_hint:
                meta_parts.append(f"tension={tension_hint}")
            if engagement_state:
                meta_parts.append(f"engage={engagement_state}")
            if cognitive_mode:
                meta_parts.append(f"cog={cognitive_mode}")
            if initiation:
                meta_parts.append(f"init={initiation}")
            if social_mode:
                meta_parts.append(f"social={social_mode}")

            meta_str = ""
            if meta_parts:
                meta_str = " | " + " ".join(meta_parts)

            lines.append(
                f"- ID:{a.id} | {a.text} | goal_hint:{goal_hint} | ts:{a.timestamp_start}{meta_str}"
            )
        return "\n".join(lines)

    def _temporal_context_block(self, actions: List[Entity]) -> str:
        """Summarize temporal structure (overall span + chronological deltas)."""
        ts_actions = [(a.id, a.timestamp_start, (a.metadata_dict or {}).get("goal_hint") or "") for a in actions if a.timestamp_start]
        if not ts_actions:
            return "None"

        ts_actions.sort(key=lambda x: x[1])
        first_ts = ts_actions[0][1]
        last_ts = ts_actions[-1][1]
        distinct_days = len({t.date() for _, t, _ in ts_actions})
        span_days = (last_ts.date() - first_ts.date()).days + 1

        lines = [
            f"Overall: {first_ts.isoformat()}..{last_ts.isoformat()} | span_days={span_days} | distinct_days={distinct_days} | actions={len(ts_actions)}"
        ]
        lines.append("Chronological actions (use deltas as weak temporal signal):")
        prev_ts: Optional[datetime] = None
        for aid, ts, hint in ts_actions:
            if prev_ts is None:
                delta = "start"
            else:
                delta_minutes = max(0, int((ts - prev_ts).total_seconds() // 60))
                delta = f"+{delta_minutes}m"
            hint_text = hint if hint else "none"
            lines.append(f"- ID:{aid} | ts:{ts.isoformat()} | dt:{delta} | goal_hint:{hint_text}")
            prev_ts = ts

        return "\n".join(lines)

    async def _prior_context_block(self, actions: List[Entity]) -> Tuple[str, List[int]]:
        """
        Build a block of cross-batch predecessor actions via FOLLOWS relations.
        For each action in the current batch, find actions from previous batches
        that directly precede it (source --FOLLOWS--> this_action).

        Returns:
            (formatted_string, list_of_context_action_ids)
        """
        batch_ids = {a.id for a in actions}
        seen_predecessors: Dict[int, Entity] = {}
        # Map predecessor_id -> list of batch action IDs it precedes
        pred_to_successors: Dict[int, List[int]] = {}

        for action in actions:
            rels = await self.store.relations.get_by_target(
                action.id,
                relation_type=RelationType.TEMPORAL,
                relation_subtype=RelationSubtype.FOLLOWS,
            )
            for rel in rels:
                if rel.source_id in batch_ids:
                    continue  # intra-batch, already in temporal_context
                if rel.source_id not in seen_predecessors:
                    ent = await self.store.entities.get(rel.source_id)
                    if ent and ent.type == EntityType.ACTION:
                        seen_predecessors[rel.source_id] = ent
                pred_to_successors.setdefault(rel.source_id, []).append(action.id)

        if not seen_predecessors:
            return "None", []

        context_ids = list(seen_predecessors.keys())
        assignments = await self._get_activity_assignments(context_ids)

        # Sort predecessors by timestamp (most recent first)
        preds = sorted(
            seen_predecessors.values(),
            key=lambda a: a.timestamp_start or datetime.min,
            reverse=True,
        )

        lines = []
        for pred in preds:
            meta = pred.metadata_dict if pred.metadata_dict and isinstance(pred.metadata_dict, dict) else {}
            goal_hint = meta.get("goal_hint") or ""
            object_label = meta.get("object_label") or ""
            domain = meta.get("domain") or ""

            parts = [f"ID:{pred.id}", pred.text or "", f"goal_hint:{goal_hint}", f"ts:{pred.timestamp_start}"]
            if object_label:
                parts.append(f"object={object_label}")
            if domain:
                parts.append(f"domain={domain}")

            # Activity assignment annotation
            action_assignments = assignments.get(pred.id, [])
            if action_assignments:
                assigned_str = ",".join(f"{aid}:{text[:40]}" for aid, text in action_assignments)
                parts.append(f"assigned_to:[{assigned_str}]")
            else:
                parts.append("assigned_to:none")

            successors = pred_to_successors.get(pred.id, [])
            parts.append(f"precedes:{','.join(str(s) for s in successors)}")

            lines.append("- " + " | ".join(parts))

        return "\n".join(lines), context_ids

    async def _concurrent_context_block(self, actions: List[Entity]) -> Tuple[str, List[int]]:
        """
        Build a block of cross-batch concurrent actions via CO_OCCURS and OVERLAPS relations.
        For each action in the current batch, find actions from previous batches
        that co-occurred or overlapped in time.

        Returns:
            (formatted_string, list_of_context_action_ids)
        """
        batch_ids = {a.id for a in actions}
        # Map external_action_id -> (Entity, set of relation types, list of batch action IDs it relates to)
        seen: Dict[int, Entity] = {}
        rel_types: Dict[int, set] = {}
        ext_to_batch: Dict[int, List[int]] = {}

        for action in actions:
            # Check outgoing CO_OCCURS and OVERLAPS
            for subtype in (RelationSubtype.CO_OCCURS, RelationSubtype.OVERLAPS):
                rels_out = await self.store.relations.get_by_source(
                    action.id,
                    relation_type=RelationType.TEMPORAL,
                    relation_subtype=subtype,
                )
                rels_in = await self.store.relations.get_by_target(
                    action.id,
                    relation_type=RelationType.TEMPORAL,
                    relation_subtype=subtype,
                )
                for rel in rels_out:
                    other_id = rel.target_id
                    if other_id in batch_ids:
                        continue  # intra-batch, already visible in temporal_context
                    if other_id not in seen:
                        ent = await self.store.entities.get(other_id)
                        if ent and ent.type == EntityType.ACTION:
                            seen[other_id] = ent
                    rel_types.setdefault(other_id, set()).add(subtype)
                    ext_to_batch.setdefault(other_id, []).append(action.id)
                for rel in rels_in:
                    other_id = rel.source_id
                    if other_id in batch_ids:
                        continue
                    if other_id not in seen:
                        ent = await self.store.entities.get(other_id)
                        if ent and ent.type == EntityType.ACTION:
                            seen[other_id] = ent
                    rel_types.setdefault(other_id, set()).add(subtype)
                    ext_to_batch.setdefault(other_id, []).append(action.id)

        if not seen:
            return "None", []

        context_ids = list(seen.keys())
        assignments = await self._get_activity_assignments(context_ids)

        # Sort by timestamp (most recent first)
        externals = sorted(
            seen.values(),
            key=lambda a: a.timestamp_start or datetime.min,
            reverse=True,
        )

        lines = []
        for ext in externals:
            meta = ext.metadata_dict if ext.metadata_dict and isinstance(ext.metadata_dict, dict) else {}
            goal_hint = meta.get("goal_hint") or ""
            object_label = meta.get("object_label") or ""
            domain = meta.get("domain") or ""
            rtypes = rel_types.get(ext.id, set())
            rtype_str = "+".join(sorted(rtypes))

            parts = [f"ID:{ext.id}", f"rel:{rtype_str}", ext.text or "", f"goal_hint:{goal_hint}", f"ts:{ext.timestamp_start}"]
            if object_label:
                parts.append(f"object={object_label}")
            if domain:
                parts.append(f"domain={domain}")

            # Activity assignment annotation
            action_assignments = assignments.get(ext.id, [])
            if action_assignments:
                assigned_str = ",".join(f"{aid}:{text[:40]}" for aid, text in action_assignments)
                parts.append(f"assigned_to:[{assigned_str}]")
            else:
                parts.append("assigned_to:none")

            related_batch = ext_to_batch.get(ext.id, [])
            # Deduplicate
            related_batch = list(dict.fromkeys(related_batch))
            parts.append(f"concurrent_with:{','.join(str(s) for s in related_batch)}")
            lines.append("- " + " | ".join(parts))

        return "\n".join(lines), context_ids

    def _existing_block(self, activities: List[Entity]) -> str:
        """
        Format existing activities with persistence metadata for LLM context.
        Shows: ID, text, status, usage_count, last_assigned_ts
        """
        lines = []
        for act in activities:
            meta = act.metadata_dict or {}
            status = meta.get("status", "active")
            usage_count = meta.get("usage_count", 0)
            last_assigned = meta.get("last_assigned_ts", "never")
            
            # Format last_assigned timestamp for readability
            if last_assigned != "never":
                try:
                    last_ts = datetime.fromisoformat(last_assigned.replace("Z", "+00:00"))
                    days_ago = (datetime.utcnow() - last_ts.replace(tzinfo=None)).days
                    last_assigned = f"{days_ago}d ago"
                except (ValueError, AttributeError):
                    pass  # Keep original if parsing fails
            
            # Include working sphere metadata if available
            extra_parts = []
            purpose = meta.get("purpose")
            identity_context = meta.get("identity_context")
            if purpose:
                extra_parts.append(f"purpose:{purpose}")
            if identity_context:
                extra_parts.append(f"domain:{identity_context}")
            extra_str = " | " + " | ".join(extra_parts) if extra_parts else ""

            lines.append(
                f"- ID:{act.id} | {act.text} | status:{status} | used:{usage_count}x | last:{last_assigned}{extra_str}"
            )
        return "\n".join(lines) if lines else "None"

    async def _get_user_stated_goals_block(self) -> str:
        """Build a prompt block of user-provided goals + user annotations for PROPOSE context."""
        parts = []

        # User-provided goals
        try:
            user_goals = await self.store.get_user_provided_goals()
        except Exception:
            user_goals = []
        if user_goals:
            header = (
                "########################################\n"
                f"# {self.user_name}'s self-described goals (strong priors)\n"
                "########################################\n"
                f"These goals were provided by {self.user_name} directly. Use them to:\n"
                "- Align candidate descriptions when actions clearly serve a stated goal\n"
                f"- Use {self.user_name}'s own framing and terminology\n"
                "- Note when actions DON'T serve any stated goal — this may indicate\n"
                "  an unstated priority or a gap between intention and behavior\n\n"
            )
            lines = [f"- ID:{g.id} | {g.text}" for g in user_goals]
            parts.append(header + "\n".join(lines))

        # User annotations on goals and activities (confirm/reject feedback)
        if getattr(self, "include_user_annotations", True):
            annotation_lines = []
            try:
                goals = await self.store.get_goals()
                for g in goals:
                    meta = g.metadata_dict or {}
                    for ann in meta.get("user_annotations", []):
                        ann_type = ann.get("type", "note")
                        annotation_lines.append(
                            f"- [goal ID:{g.id}] {ann_type}: \"{ann.get('text', '')}\" ({ann.get('timestamp', '')})"
                        )
            except Exception:
                pass
            if annotation_lines:
                ann_header = (
                    "\n\n########################################\n"
                    f"# {self.user_name}'s feedback (user annotations)\n"
                    "########################################\n"
                    f"{self.user_name} has confirmed or rejected inferences. Respect these:\n"
                    "- 'confirm' = the user agrees with the inference\n"
                    "- 'reject' = the user disagrees — adjust or avoid similar groupings\n\n"
                )
                parts.append(ann_header + "\n".join(annotation_lines))

        return "".join(parts)

    async def _get_system_goals_block(self) -> str:
        """Build a prompt block of system-inferred goals for PROPOSE context."""
        if not getattr(self, "include_system_goals", True):
            return ""
        try:
            goals = await self.store.get_goals()
        except Exception:
            goals = []
        if not goals:
            return ""
        # Filter to active, non-user-provided goals
        system_goals = [
            g for g in goals
            if (g.metadata_dict or {}).get("source") != "user_provided"
            and (g.metadata_dict or {}).get("status", "active") == "active"
        ]
        if not system_goals:
            return ""
        header = (
            "########################################\n"
            f"# System-inferred goals for {self.user_name}\n"
            "########################################\n"
            "These are goals the system has previously inferred from behavior.\n"
            "Use them as context — if actions clearly serve one of these goals,\n"
            "that can inform how you group activities.\n\n"
        )
        lines = []
        for g in system_goals[:15]:  # Cap at 15 to avoid prompt bloat
            meta = g.metadata_dict or {}
            needs = meta.get("needs", [])
            needs_str = f" | needs:{','.join(needs)}" if needs else ""
            lines.append(f"- ID:{g.id} | {g.text}{needs_str}")
        return header + "\n".join(lines)

    async def _get_user_constraints_block(self) -> str:
        """Build a prompt block of user constraints (locked, edited, annotated entities)."""
        constraint_lines = []

        # Check activities for locked/edited/annotated status
        try:
            activities = await self.store.entities.get_by_type(EntityType.ACTIVITY, limit=200)
        except Exception:
            activities = []
        for act in activities:
            meta = act.metadata_dict or {}
            flags = []
            if meta.get("user_locked"):
                flags.append("[locked] — do not reassign, merge, or delete")
            if meta.get("user_edited"):
                flags.append("[user-edited] — preserve exact label")
            if meta.get("user_reassigned"):
                flags.append("[user-reassigned] — respect current goal assignment")
            for ann in meta.get("user_annotations", []):
                ann_type = ann.get("type", "note")
                ann_text = ann.get("text", "")[:80]
                flags.append(f"annotation ({ann_type}): \"{ann_text}\"")
            if flags:
                for flag in flags:
                    constraint_lines.append(f"- Activity ID:{act.id} | {act.text[:60]} | {flag}")

        # Check goals for locked/edited status
        try:
            goals = await self.store.get_goals()
        except Exception:
            goals = []
        for g in goals:
            meta = g.metadata_dict or {}
            flags = []
            if meta.get("user_locked"):
                flags.append("[locked] — do not merge, delete, or substantially alter")
            if meta.get("user_edited"):
                flags.append("[user-edited] — preserve exact label")
            if flags:
                for flag in flags:
                    constraint_lines.append(f"- Goal ID:{g.id} | {g.text[:60]} | {flag}")

        # Actions the user pulled out of a specific activity. Listed so the
        # model does not propose the pairing again; the link step refuses it
        # regardless, but an unexplained rejection wastes a candidate slot.
        try:
            actions = await self.store.entities.get_by_type(EntityType.ACTION, limit=200)
        except Exception:
            actions = []
        for act in actions:
            detached = (act.metadata_dict or {}).get("removed_from_activities") or []
            for activity_id in detached:
                constraint_lines.append(
                    f"- Action ID:{act.id} | {act.text[:60]} | "
                    f"[detached] — do NOT place under Activity ID:{activity_id}"
                )

        if not constraint_lines:
            return ""
        header = (
            "########################################\n"
            f"# User constraints\n"
            "########################################\n"
            f"{self.user_name} has edited, locked, or annotated the following entities.\n"
            "Respect these constraints:\n"
            "- [locked]: Do NOT reassign, merge, delete, or relabel.\n"
            "- [user-edited]: Keep the exact label; you may reassign actions to/from.\n"
            "- [user-reassigned]: Respect the current goal assignment.\n"
            "- [detached]: Do NOT place that action under that activity again.\n"
            "- Annotations provide privileged context — weight above behavioral inference.\n\n"
        )
        return header + "\n".join(constraint_lines)

    async def _propose_candidates(
        self, actions: List[Entity], existing: List[Entity],
        default_prompt: Optional[str] = None,
    ) -> Tuple[List[_PasCandidate], List[int]]:
        """Propose candidate activities from actions.

        Returns:
            (candidates, context_action_ids) where context_action_ids are
            prior/concurrent actions that the LLM may reference in action_ids.
        """
        prompt_template = self.propose_prompt_override or default_prompt or PROPOSE_ONLY_PROMPT
        user_goals_block = await self._get_user_stated_goals_block()
        system_goals_block = await self._get_system_goals_block()
        user_constraints_block = await self._get_user_constraints_block()
        prior_context_block, prior_context_ids = await self._prior_context_block(actions)
        concurrent_context_block, concurrent_context_ids = await self._concurrent_context_block(actions)
        all_context_ids = list(set(prior_context_ids + concurrent_context_ids))
        prompt = render_prompt_template(
            prompt_template,
            existing_activities=self._existing_block(existing),
            actions=self._actions_block(actions),
            temporal_context=self._temporal_context_block(actions),
            prior_context=prior_context_block,
            concurrent_context=concurrent_context_block,
            user_name=self.user_name,
            user_stated_goals=user_goals_block,
            system_goals=system_goals_block,
            user_constraints=user_constraints_block,
            user_context=self.user_context,
        )
        if self.experiment_user_description:
            prompt = f"[User Context: {self.experiment_user_description}]\n\n{prompt}"
        if getattr(self, "debug", False) and os.getenv("TEMPO_LOG_STAGE3_PROMPTS") == "1":
            print("\n[STAGE3][PAS][PROPOSE] Prompt:\n")
            print(prompt)
        self.log.debug("propose_candidates: prompt_len=%d actions=%d existing=%d", len(prompt), len(actions), len(existing))
        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=PAS_PROPOSE_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="action_to_activities", stage="propose",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )
        self.log.debug("propose_candidates: response_len=%d", len(response or ""))
        try:
            data = parse_llm_json(response)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}
            self._log_stage3_json("PROPOSE", data, actions)
            raw_candidates = data.get("candidates", [])
        except Exception as e:
            self.log.warning(f"propose parse failed: {e}")
            return [], []

        candidates: List[_PasCandidate] = []
        existing_ids = {act.id for act in existing}
        for idx, c in enumerate(raw_candidates):
            cid = c.get("candidate_id") or f"C{idx+1}"
            desc = c.get("description") or ""
            examples = c.get("action_ids") or c.get("example_action_ids") or []
            # Robust parsing of action_ids - handle strings and ints
            clean_examples = []
            for x in examples:
                try:
                    clean_examples.append(int(x))
                except (TypeError, ValueError):
                    continue
            existing_id = c.get("existing_activity_id")
            if existing_id and existing_id not in existing_ids:
                existing_id = None
            reasoning = c.get("reasoning")
            confidence = c.get("confidence")
            if confidence is not None:
                try:
                    confidence = int(confidence)
                except (TypeError, ValueError):
                    confidence = None
            # Parse action_valences (parallel array matching action_ids)
            raw_valences = c.get("action_valences") or []
            valid_valences = {"supports", "hinders", "neutral"}
            action_valences = None
            if raw_valences and len(raw_valences) == len(clean_examples):
                action_valences = [
                    v if isinstance(v, str) and v in valid_valences else "supports"
                    for v in raw_valences
                ]
            elif clean_examples:
                # Length mismatch or missing — default all to "supports"
                action_valences = ["supports"] * len(clean_examples)
            # Parse working sphere metadata (Phase 2)
            # Safety net: Gemini may return strings for list/dict fields
            purpose = c.get("purpose")
            people = c.get("people") or []
            if isinstance(people, str):
                try:
                    people = json.loads(people)
                except (json.JSONDecodeError, ValueError):
                    people = [people] if people else []
            resources = c.get("resources") or []
            if isinstance(resources, str):
                try:
                    resources = json.loads(resources)
                except (json.JSONDecodeError, ValueError):
                    resources = [resources] if resources else []
            temporal_pattern = c.get("temporal_pattern")
            engagement_profile = c.get("engagement_profile")
            initiation_profile = c.get("initiation_profile")
            identity_context = c.get("identity_context")

            candidates.append(
                _PasCandidate(
                    cid=cid,
                    description=desc,
                    example_action_ids=clean_examples,
                    existing_activity_id=existing_id,
                    reasoning=reasoning,
                    confidence=confidence,
                    action_valences=action_valences,
                    purpose=purpose,
                    people=people if people else None,
                    resources=resources if resources else None,
                    temporal_pattern=temporal_pattern,
                    engagement_profile=engagement_profile,
                    initiation_profile=initiation_profile,
                    identity_context=identity_context,
                )
            )
            if reasoning:
                self.log.debug("propose_candidates: %s reasoning=%s", cid, reasoning[:100])
        self.log.debug("propose_candidates: produced %d candidates, %d context_ids", len(candidates), len(all_context_ids))
        return candidates, all_context_ids

    def _log_stage3_json(self, label: str, data: Dict[str, Any], actions: List[Entity]) -> None:
        if not (self.log_stage3_json or os.getenv("TEMPO_LOG_STAGE3_JSON") == "1"):
            return
        action_ids = [a.id for a in actions]
        if action_ids:
            id_span = f"{min(action_ids)}..{max(action_ids)}"
        else:
            id_span = "none"
        print(f"\n[STAGE3][PAS][{label}] JSON (actions={len(actions)} ids={id_span}):\n")
        try:
            print(json.dumps(data, indent=2))
        except Exception:
            print(data)

    def _update_activity_scores(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return update_activity_scores(meta)

    async def _apply_selection(
        self,
        actions: List[Entity],
        candidates: List[_PasCandidate],
        selection: Dict[str, Any],
        membership_matrix: Optional[List[List[float]]] = None,
        valence_matrix: Optional[List[List[_ValenceEntry]]] = None,
    ) -> Tuple[int, int, int, int, List[int]]:
        created = 0
        reassigned = 0
        supports_created = 0
        hinders_created = 0
        created_activity_ids: List[int] = []

        active_set = set(selection["active_candidates"])
        proposed_id_map: Dict[int, int] = {}

        # Extract action IDs upfront to avoid accessing detached entities after potential rollbacks
        action_ids = [a.id for a in actions]
        
        # Build a map from candidate index to resolved activity ID (for HINDERS relations)
        # This will be populated as we create/resolve activities
        cand_to_activity_id: Dict[int, int] = {}

        for i, action_id in enumerate(action_ids):
            for j in selection["assignment"].get(i, []):
                if j not in active_set:
                    continue
                cand = candidates[j]
                target_activity_id = cand.existing_activity_id
                if target_activity_id is None:
                    if j not in proposed_id_map:
                        # Create new activity with initial persistence metadata
                        activity_metadata = {
                            "status": "active",  # New activities start as active
                            "usage_count": 0,  # Will be incremented below
                            "last_assigned_ts": datetime.utcnow().isoformat(),
                        }
                        # Store LLM reasoning for this activity creation
                        if cand.reasoning:
                            activity_metadata["reasoning"] = cand.reasoning
                        if cand.confidence is not None:
                            activity_metadata["creation_confidence"] = cand.confidence
                        # Store working sphere metadata (Phase 2)
                        if cand.purpose:
                            activity_metadata["purpose"] = cand.purpose
                        if cand.people:
                            activity_metadata["people"] = cand.people
                        if cand.resources:
                            activity_metadata["resources"] = cand.resources
                        if cand.temporal_pattern:
                            activity_metadata["temporal_pattern"] = cand.temporal_pattern
                        if cand.engagement_profile:
                            activity_metadata["engagement_profile"] = cand.engagement_profile
                        if cand.initiation_profile:
                            activity_metadata["initiation_profile"] = cand.initiation_profile
                        if cand.identity_context:
                            activity_metadata["identity_context"] = cand.identity_context
                        
                        act = await self.store.create_activity(
                            text=cand.description,
                            action_ids=[],
                            embedding=None,
                            metadata=activity_metadata,
                        )
                        proposed_id_map[j] = act.id
                        created_activity_ids.append(act.id)
                        created += 1
                        if self.debug_logger:
                            self.debug_logger.log_entity_mutation(
                                pipeline="action_to_activities", stage="apply_selection",
                                mutation="create", entity_id=act.id,
                                entity_type="activity", entity_text=cand.description,
                                metadata=activity_metadata,
                            )
                    target_activity_id = proposed_id_map[j]
                
                # Track the resolved activity ID for this candidate
                cand_to_activity_id[j] = target_activity_id

                # # Add revision edges from any prior activities for this action to the new/selected activity
                # # Only create revision edges if this is a reassignment (not the first assignment)
                # old_activity_ids = await self._get_action_activities(action_id)
                # # Filter out the target activity to avoid self-loops
                # old_activity_ids = [aid for aid in old_activity_ids if aid != target_activity_id]
                # if old_activity_ids:
                #     await self._add_revision_edges(old_activity_ids, target_activity_id)

                # Get membership score and valence entry for this action-candidate pair
                membership_score = None
                valence_entry = None
                if membership_matrix is not None and i < len(membership_matrix) and j < len(membership_matrix[i]):
                    membership_score = membership_matrix[i][j]
                if valence_matrix is not None and i < len(valence_matrix) and j < len(valence_matrix[i]):
                    valence_entry = valence_matrix[i][j]
                
                linked = await self._link_action(action_id, target_activity_id, membership_score, valence_entry)
                if not linked:
                    continue  # Relation already existed, skip metadata updates
                reassigned += 1

                # Update activity persistence metadata
                activity = await self.store.entities.get(target_activity_id)
                if activity:
                    meta = activity.metadata_dict.copy() if activity.metadata_dict else {}

                    # Initialize status if not set (for older activities)
                    if "status" not in meta:
                        meta["status"] = "active"

                    # Update usage tracking
                    meta["usage_count"] = meta.get("usage_count", 0) + 1
                    meta["last_assigned_ts"] = datetime.utcnow().isoformat()
                    
                    # Auto-update status based on usage patterns
                    # If activity was dormant and is now being used, reactivate it
                    if meta.get("status") == "dormant":
                        meta["status"] = "active"

                    meta = self._update_activity_scores(meta)

                    await self.store.entities.update(
                        target_activity_id,
                        metadata=meta,
                    )
                    if self.debug_logger:
                        self.debug_logger.log_entity_mutation(
                            pipeline="action_to_activities", stage="apply_selection",
                            mutation="update", entity_id=target_activity_id,
                            entity_type="activity", entity_text=activity.text,
                            metadata=meta,
                        )
        
        # Create behavioral relations (SUPPORTS and HINDERS) based on valence matrix
        if valence_matrix:
            supports_created, hinders_created = await self._create_behavioral_relations(
                actions, candidates, selection, valence_matrix, cand_to_activity_id
            )

        self.log.debug(
            "_apply_selection: created=%d reassigned=%d supports=%d hinders=%d", 
            created, reassigned, supports_created, hinders_created,
        )
        return created, reassigned, supports_created, hinders_created, created_activity_ids
    
    async def _create_behavioral_relations(
        self,
        actions: List[Entity],
        candidates: List[_PasCandidate],
        selection: Dict[str, Any],
        valence_matrix: List[List[_ValenceEntry]],
        cand_to_activity_id: Dict[int, int],
    ) -> Tuple[int, int]:
        """
        Create behavioral relations (SUPPORTS and HINDERS) between actions and activities.
        
        For each action, check its valence towards all activities:
        - SUPPORTS: action advances the activity's goal
        - HINDERS: action works against the activity's goal
        
        Returns:
            Tuple of (supports_created, hinders_created).
        """
        supports_created = 0
        hinders_created = 0
        active_set = set(selection["active_candidates"])
        valence_threshold = 0.3  # Minimum valence_strength to create a behavioral relation
        
        for i, action in enumerate(actions):
            # Check all active candidates for valence
            for j, cand in enumerate(candidates):
                if j not in active_set:
                    continue
                if j not in cand_to_activity_id:
                    continue
                    
                activity_id = cand_to_activity_id[j]
                valence_entry = valence_matrix[i][j]
                
                # Skip if valence strength is below threshold
                if valence_entry.strength < valence_threshold:
                    continue
                
                if valence_entry.valence == "supports":
                    # Create SUPPORTS relation: action -> activity
                    try:
                        await self._ensure_behavioral_relation(
                            action.id, 
                            activity_id, 
                            RelationSubtype.SUPPORTS,
                            valence_entry.strength,
                        )
                        supports_created += 1
                        self.log.debug(
                            "_create_behavioral_relations: action %d supports activity %d (strength=%.2f)",
                            action.id, activity_id, valence_entry.strength,
                        )
                    except Exception as e:
                        self.log.warning(
                            "_create_behavioral_relations: failed to create supports relation "
                            "action=%d activity=%d: %s",
                            action.id, activity_id, e,
                        )
                
                elif valence_entry.valence == "hinders":
                    # Create HINDERS relation: action -> activity
                    try:
                        await self._ensure_behavioral_relation(
                            action.id, 
                            activity_id, 
                            RelationSubtype.HINDERS,
                            valence_entry.strength,
                        )
                        hinders_created += 1
                        self.log.debug(
                            "_create_behavioral_relations: action %d hinders activity %d (strength=%.2f)",
                            action.id, activity_id, valence_entry.strength,
                        )
                    except Exception as e:
                        self.log.warning(
                            "_create_behavioral_relations: failed to create hinders relation "
                            "action=%d activity=%d: %s",
                            action.id, activity_id, e,
                        )
        
        return supports_created, hinders_created
    
    async def _ensure_behavioral_relation(
        self, 
        action_id: int, 
        activity_id: int, 
        subtype: str,
        strength: float,
    ) -> None:
        """Create a behavioral relation (SUPPORTS or HINDERS) if it doesn't already exist."""
        existing = await self.store.relations.get_by_source(
            action_id,
            relation_type=RelationType.BEHAVIORAL,
            relation_subtype=subtype,
        )
        for rel in existing:
            if rel.target_id == activity_id:
                # Relation already exists, optionally update confidence
                return
        
        await self.store.relations.create(
            source_id=action_id,
            target_id=activity_id,
            relation_type=RelationType.BEHAVIORAL,
            relation_subtype=subtype,
            confidence=strength,
        )
    
    async def update_activity_lifecycle(self) -> Dict[str, int]:
        """
        Update activity status based on usage patterns.
        
        Rules:
        - Dormant: Not used in last `dormant_threshold_days` days (default 30)
        - Retired: Not used in last `retired_threshold_days` days (default 90) AND usage_count < retired_min_usage (default 5)
        - Active: Currently in use (set when activity is assigned)
        
        Returns:
            Dictionary with counts of status updates: {"dormant": X, "retired": Y, "reactivated": Z}
        """
        from datetime import timedelta
        
        dormant_cutoff = datetime.utcnow() - timedelta(days=self.dormant_threshold_days)
        retired_cutoff = datetime.utcnow() - timedelta(days=self.retired_threshold_days)
        
        all_activities = await self.store.entities.get_by_type(EntityType.ACTIVITY, limit=1000)
        
        updates = {"dormant": 0, "retired": 0, "reactivated": 0}
        
        for activity in all_activities:
            meta = activity.metadata_dict.copy() if activity.metadata_dict else {}
            current_status = meta.get("status", "active")
            last_assigned_str = meta.get("last_assigned_ts")
            
            if not last_assigned_str:
                # No usage history - skip (might be brand new)
                continue
            
            try:
                last_assigned = datetime.fromisoformat(last_assigned_str.replace("Z", "+00:00"))
                last_assigned_naive = last_assigned.replace(tzinfo=None)
                usage_count = meta.get("usage_count", 0)
                
                # Check if should be retired
                if (current_status != "retired" and 
                    last_assigned_naive < retired_cutoff and 
                    usage_count < self.retired_min_usage):
                    meta["status"] = "retired"
                    updates["retired"] += 1
                    await self.store.entities.update(activity.id, metadata=meta)
                    self.log.debug(
                        "update_activity_lifecycle: retired activity id=%d (last_used=%s, usage_count=%d)",
                        activity.id,
                        last_assigned_str,
                        usage_count,
                    )
                
                # Check if should be dormant (but not if already retired)
                elif (current_status == "active" and 
                      last_assigned_naive < dormant_cutoff):
                    meta["status"] = "dormant"
                    updates["dormant"] += 1
                    await self.store.entities.update(activity.id, metadata=meta)
                    self.log.debug(
                        "update_activity_lifecycle: dormant activity id=%d (last_used=%s)",
                        activity.id,
                        last_assigned_str,
                    )
                
            except (ValueError, AttributeError) as e:
                self.log.warning(
                    "update_activity_lifecycle: failed to parse last_assigned_ts for activity id=%d: %s",
                    activity.id,
                    e,
                )
                continue
        
        if any(updates.values()):
            self.log.info(
                "update_activity_lifecycle: updated dormant=%d retired=%d reactivated=%d",
                updates["dormant"],
                updates["retired"],
                updates["reactivated"],
            )
        
        return updates

    async def _link_action(
        self,
        action_id: int,
        activity_id: int,
        membership_score: Optional[float] = None,
        valence_entry: Optional[_ValenceEntry] = None,
    ) -> bool:
        """Link action to activity. Returns True if a new relation was created, False if it already existed.

        Also expands the activity's timestamp_start/timestamp_end to cover
        the linked action's time range, so activity timestamps always reflect
        the actual span of their constituent actions.
        """
        # Respect a detachment the user made. Removing an action from an
        # activity is a judgment about this pairing, not about the action —
        # which stays in the graph and remains free to join other activities.
        # Without this guard the detached action reads as unassigned, becomes a
        # prime candidate on the next run, and is silently re-attached to the
        # very activity it was removed from. The prompt asks for this too, but
        # a prompt cannot be relied on to preserve an explicit user edit.
        action = await self.store.entities.get(action_id)
        if action is not None:
            detached = (action.metadata_dict or {}).get("removed_from_activities") or []
            if activity_id in detached:
                self.log.debug(
                    "Not re-linking action %s to activity %s — the user detached it",
                    action_id, activity_id,
                )
                return False

        # Check if relation already exists to avoid duplicates
        existing = await self.store.relations.get_by_source(
            action_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        for rel in existing:
            if rel.target_id == activity_id:
                # Relation already exists, skip creation
                return False

        # Build metadata with membership/valence info if available
        metadata = None
        if membership_score is not None or valence_entry is not None:
            metadata = {}
            if membership_score is not None:
                metadata["membership_score"] = membership_score
            if valence_entry is not None:
                metadata["valence"] = valence_entry.valence
                metadata["valence_strength"] = valence_entry.strength
                if valence_entry.reasoning:
                    metadata["valence_reasoning"] = valence_entry.reasoning

        await self.store.relations.create(
            source_id=action_id,
            target_id=activity_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
            metadata=metadata,
        )

        # Expand activity timestamps to cover this action's time range
        action = await self.store.entities.get(action_id)
        activity = await self.store.entities.get(activity_id)
        if action and activity:
            action_start = action.timestamp_start
            action_end = action.timestamp_end or action.timestamp_start
            new_start = min(activity.timestamp_start, action_start) if activity.timestamp_start else action_start
            new_end = max(activity.timestamp_end or activity.timestamp_start, action_end)
            if new_start != activity.timestamp_start or new_end != activity.timestamp_end:
                await self.store.entities.update(
                    activity_id,
                    timestamp_start=new_start,
                    timestamp_end=new_end,
                )

        return True

    async def _get_action_activities(self, action_id: int) -> List[int]:
        rels = await self.store.relations.get_by_source(
            action_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        return [r.target_id for r in rels]

    async def _get_activity_assignments(self, action_ids: List[int]) -> Dict[int, List[Tuple[int, str]]]:
        """For each action ID, look up its PART_OF relations to activities.

        Returns:
            {action_id: [(activity_id, activity_label), ...]}
        """
        result: Dict[int, List[Tuple[int, str]]] = {}
        for aid in action_ids:
            rels = await self.store.relations.get_by_source(
                aid,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            assignments: List[Tuple[int, str]] = []
            for rel in rels:
                activity = await self.store.entities.get(rel.target_id)
                if activity and activity.type == EntityType.ACTIVITY:
                    assignments.append((activity.id, activity.text or ""))
            if assignments:
                result[aid] = assignments
        return result

    async def _add_revision_edges(self, old_activity_ids: List[int], new_activity_id: int) -> None:
        for old_id in old_activity_ids:
            if old_id == new_activity_id:
                continue
            await self._ensure_revision_edge(old_id, new_activity_id)

    async def _ensure_revision_edge(self, old_activity_id: int, new_activity_id: int) -> None:
        existing = await self.store.relations.get_by_source(
            old_activity_id,
            relation_type=RelationType.REVISION,
            relation_subtype=RelationSubtype.SUPERSEDES,
        )
        for rel in existing:
            if rel.target_id == new_activity_id:
                return
        await self.store.relations.create(
            source_id=old_activity_id,
            target_id=new_activity_id,
            relation_type=RelationType.REVISION,
            relation_subtype=RelationSubtype.SUPERSEDES,
        )

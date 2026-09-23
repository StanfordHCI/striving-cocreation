from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from tempo.models import Entity, EntityType, RelationType, RelationSubtype
from tempo.prompts.activity_reconcile import RECONCILE_PROMPT
from tempo.schemas import ReconcileMultiResult, get_schema
from tempo.utils import get_debug_logger, parse_llm_json, update_activity_scores, render_prompt_template

RECONCILE_FORMAT = get_schema(ReconcileMultiResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.providers import ModelProvider
    from tempo.store import Store

@dataclass
class ReconcileResult:
    matched: int = 0
    revised: int = 0
    created: int = 0
    merged: int = 0
    relations_created: int = 0
    errors: int = 0
    # Maps each candidate activity ID → the surviving activity ID after reconcile.
    # For match/revise/merge: candidate_id → target_id (candidate was deleted).
    # For new: candidate_id → candidate_id (candidate survived).
    candidate_to_surviving: Dict[int, int] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.candidate_to_surviving is None:
            self.candidate_to_surviving = {}


class ActivityReconcileJob:
    """
    LLM-only reconcile step that maps new activity candidates to a stable activity repository.
    It can match, revise, create, fork, or merge activities, and create activity relations.
    """

    def __init__(
        self,
        provider: "ModelProvider",
        store: "Store",
        *,
        user_name: Optional[str] = None,
        experiment_user_description: Optional[str] = None,
        debug: bool = False,
        max_actions_per_candidate: int = 10,
        probation_min_actions: int = 3,
        probation_min_batches: int = 2,
        probation_confidence: int = 6,
        skip_probation: bool = False,
        include_goal_conflicts: bool = True,
        user_context: str = "",
    ) -> None:
        self.provider = provider
        self.store = store
        self.user_name = user_name or os.getenv("USER_NAME", "the user")
        self.experiment_user_description = experiment_user_description
        self.debug = debug
        self.log = get_debug_logger(self, debug=debug)
        self.max_actions_per_candidate = max_actions_per_candidate
        self.probation_min_actions = probation_min_actions
        self.probation_min_batches = probation_min_batches
        self.probation_confidence = probation_confidence
        self.skip_probation = skip_probation
        self.include_goal_conflicts = include_goal_conflicts
        self.llm_timeout_seconds = self._get_timeout_seconds()
        self.user_context = user_context

    @staticmethod
    def _get_timeout_seconds() -> Optional[float]:
        raw = os.getenv("TEMPO_STAGE3_RECONCILE_TIMEOUT_SECONDS", "600").strip()
        if not raw:
            return None
        try:
            val = float(raw)
        except ValueError:
            return None
        if val <= 0:
            return None
        return val

    async def _get_goal_conflicts_block(self) -> str:
        """Build prompt block of known goal-goal conflicts for reconcile context."""
        if not self.include_goal_conflicts:
            return ""
        try:
            from tempo.models import EntityType as ET
            goals = await self.store.entities.get_by_type(ET.GOAL)
        except Exception:
            return ""
        if not goals:
            return ""
        conflict_lines = []
        goals_by_id = {g.id: g for g in goals}
        for g in goals:
            try:
                rels = await self.store.relations.get_by_source(g.id, relation_type="behavioral")
            except Exception:
                continue
            for rel in (rels or []):
                rel_meta = rel.metadata_dict or {}
                if rel_meta.get("goal_relation") == "conflict" or rel.relation_subtype == "competes":
                    target = goals_by_id.get(rel.target_id)
                    if target:
                        evidence = rel_meta.get("evidence", "")[:100]
                        resolution = rel_meta.get("resolution", {})
                        res_str = ""
                        if resolution:
                            res_str = f" | resolution: {resolution.get('strategy', 'none')}"
                        conflict_lines.append(
                            f"- CONFLICT: \"{g.text[:60]}\" (ID:{g.id}) vs \"{target.text[:60]}\" (ID:{target.id})"
                            + (f" | evidence: {evidence}" if evidence else "")
                            + res_str
                        )
        if not conflict_lines:
            return ""
        header = (
            "############################\n"
            "# Known goal conflicts\n"
            "############################\n"
            "These conflicts between goals have been detected. Do NOT merge activities\n"
            "that serve conflicting goals — keep them separate to preserve the tension.\n\n"
        )
        return header + "\n".join(conflict_lines)

    async def _get_user_constraints_block(self) -> str:
        """Build a prompt block of user constraints for reconcile context."""
        constraint_lines = []

        # Check activities for locked/edited/reassigned status
        try:
            activities = await self.store.entities.get_by_type(EntityType.ACTIVITY, limit=200)
        except Exception:
            activities = []
        for act in activities:
            meta = act.metadata_dict or {}
            flags = []
            if meta.get("user_locked"):
                flags.append("[locked] — do not merge, delete, or reassign")
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

        if not constraint_lines:
            return ""
        header = (
            "############################\n"
            "# User constraints\n"
            "############################\n"
            f"{self.user_name} has edited, locked, or annotated the following entities.\n"
            "Respect these constraints:\n"
            "- [locked]: Do NOT merge, delete, reassign, or relabel.\n"
            "- [user-edited]: Keep the exact label; you may reassign actions to/from.\n"
            "- [user-reassigned]: Respect the current goal assignment.\n"
            "- Annotations provide privileged context — weight above behavioral inference.\n\n"
        )
        return header + "\n".join(constraint_lines)

    async def run(
        self,
        candidate_activity_ids: Sequence[int],
        *,
        batch_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = ReconcileResult()
        if not candidate_activity_ids:
            return result.__dict__

        # Pull a snapshot of all activities for candidate filtering.
        all_activities = await self.store.entities.get_by_type(EntityType.ACTIVITY)
        activities_by_id = {a.id: a for a in all_activities}
        goal_conflicts_block = await self._get_goal_conflicts_block()
        user_constraints_block = await self._get_user_constraints_block()

        candidate_ids_set = {int(cid) for cid in candidate_activity_ids}

        # Build candidate entries first (needed for connected-activity lookup)
        candidate_entries: List[Tuple[Entity, List[Entity]]] = []
        for candidate_id in candidate_activity_ids:
            candidate = activities_by_id.get(candidate_id) or await self.store.entities.get(candidate_id)
            if not candidate or candidate.type != EntityType.ACTIVITY:
                continue
            # Skip user-reassigned activities — respect manual placement
            cand_meta = candidate.metadata_dict or {}
            if cand_meta.get("user_reassigned"):
                self.log.debug("reconcile: skipping user-reassigned candidate=%s", candidate.id)
                continue
            try:
                actions = await self._get_actions_for_activity(candidate.id)
                candidate_entries.append((candidate, actions))
            except Exception:
                result.errors += 1
                self.log.warning("reconcile: failed candidate=%s", candidate_id)
                continue

        if not candidate_entries:
            return result.__dict__

        # Three-tier selection: connected → backfill (recency) → summary
        others = [act for act in all_activities if act.id not in candidate_ids_set]
        now = datetime.utcnow()

        connected_ids = await self._find_connected_activities(candidate_entries, candidate_ids_set)
        connected = [a for a in others if a.id in connected_ids]
        remaining = [a for a in others if a.id not in connected_ids]

        # Backfill: active-status only, sorted by recency-first then usage
        def _score_backfill(act: Entity) -> Tuple[int, int]:
            meta = act.metadata_dict or {}
            usage = int(meta.get("usage_count", 0) or 0)
            last_ts_str = meta.get("last_assigned_ts")
            recency_days = 999
            if last_ts_str:
                try:
                    last_ts = datetime.fromisoformat(str(last_ts_str).replace("Z", "+00:00")).replace(tzinfo=None)
                    recency_days = max(0, (now - last_ts).days)
                except Exception:
                    pass
            return (-recency_days, usage)

        active_remaining = [a for a in remaining if (a.metadata_dict or {}).get("status", "active") == "active"]
        active_remaining.sort(key=_score_backfill, reverse=True)
        slots_left = max(0, 25 - len(connected))
        backfill = active_remaining[:slots_left]

        detailed = connected + backfill
        detailed_ids = {a.id for a in detailed}
        summary_only = [a for a in others if a.id not in detailed_ids]
        existing_block = self._existing_block(detailed, summary_only)
        self.log.debug(
            "reconcile: selection connected=%d backfill=%d summary=%d",
            len(connected), len(backfill), len(summary_only),
        )

        blocks: List[str] = []
        for candidate, actions in candidate_entries:
            meta_line = self._candidate_metadata_block(candidate)
            block_parts = [f"ID: {candidate.id}", f"Label: {candidate.text}"]
            if meta_line:
                block_parts.append(meta_line)
            block_parts.append(
                "Evidence actions (ID | action | goal_hint | ts | conf | decay | dur | engage | cog | init | social):\n"
                + self._actions_block(actions)
            )
            blocks.append("\n".join(block_parts))
        candidate_goals_block = "\n\n---\n\n".join(blocks)

        prompt = render_prompt_template(
            RECONCILE_PROMPT,
            candidate_goals=candidate_goals_block,
            existing_goals=existing_block,
            user_name=self.user_name,
            goal_conflicts=goal_conflicts_block,
            user_constraints=user_constraints_block,
            user_context=self.user_context,
        )
        if self.experiment_user_description:
            prompt = f"[User Context: {self.experiment_user_description}]\n\n{prompt}"
        if self.debug and os.getenv("TEMPO_LOG_STAGE3_PROMPTS") == "1":
            print("\n[STAGE3][RECONCILE] Prompt:\n")
            print(prompt)
        self.log.debug(
            "reconcile: candidates=%d prompt_len=%d existing=%d+%d",
            len(candidate_entries),
            len(prompt),
            len(detailed),
            len(summary_only),
        )

        async def _call_llm() -> str:
            return await self.provider.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format=RECONCILE_FORMAT,
            )

        try:
            if self.llm_timeout_seconds:
                self.log.debug("reconcile: calling LLM (timeout=%ss)", self.llm_timeout_seconds)
            else:
                self.log.debug("reconcile: calling LLM (timeout=disabled)")
            started_at = datetime.utcnow()
            response = await asyncio.wait_for(_call_llm(), timeout=self.llm_timeout_seconds) if self.llm_timeout_seconds else await _call_llm()
            elapsed = (datetime.utcnow() - started_at).total_seconds()
            self.log.debug("reconcile: LLM returned in %.2fs", elapsed)
            self.log.debug("reconcile: response_len=%d", len(response or ""))
            data = self._parse_json_response(response)
        except Exception:
            result.errors += len(candidate_entries)
            self.log.warning("reconcile: failed to get response")
            return result.__dict__

        if not data:
            result.errors += len(candidate_entries)
            self.log.warning("reconcile: parse failed")
            return result.__dict__

        if isinstance(data, list):
            decisions_raw = data
        else:
            decisions_raw = data.get("decisions")
            if not isinstance(decisions_raw, list):
                decisions_raw = [data] if isinstance(data, dict) else []

        decision_by_id: Dict[int, Dict[str, Any]] = {}
        for dec in decisions_raw:
            if not isinstance(dec, dict):
                continue
            cid = dec.get("candidate_id")
            try:
                cid_int = int(cid)
            except Exception:
                continue
            decision_by_id[cid_int] = dec

        actions_by_id: Dict[int, List[Entity]] = {c.id: actions for c, actions in candidate_entries}

        for candidate, actions in candidate_entries:
            data_for_candidate = decision_by_id.get(candidate.id)
            if not data_for_candidate:
                result.errors += 1
                self.log.warning("reconcile: missing decision for candidate=%s", candidate.id)
                continue
            await self._apply_decision(
                candidate=candidate,
                actions=actions_by_id.get(candidate.id, actions),
                data=data_for_candidate,
                result=result,
                batch_id=batch_id,
            )
            self.log.debug("reconcile: applied decision for candidate=%s", candidate.id)

        await self._capture_snapshot(batch_id, result.__dict__)
        return result.__dict__

    async def _capture_snapshot(
        self,
        batch_id: Optional[int],
        result: Dict[str, Any],
    ) -> None:
        """Capture a batch snapshot for stability tracking (best-effort)."""
        if batch_id is None:
            return
        try:
            await self.store.capture_snapshot(
                batch_id=batch_id,
                stage="stage3",
                result=result,
            )
            self.log.debug("reconcile: captured snapshot batch_id=%s", batch_id)
        except Exception as e:
            self.log.warning("reconcile: snapshot capture failed: %s", e)

    @staticmethod
    def _extract_working_sphere(data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract working-sphere fields from reconcile LLM output."""
        ws: Dict[str, Any] = {}
        if data.get("purpose"):
            ws["purpose"] = data["purpose"]
        if data.get("people"):
            people = data["people"]
            # Safety net: Gemini may return strings for list fields
            if isinstance(people, str):
                try:
                    people = json.loads(people)
                except (json.JSONDecodeError, ValueError):
                    people = [people] if people else []
            ws["people"] = people
        if data.get("resources"):
            resources = data["resources"]
            if isinstance(resources, str):
                try:
                    resources = json.loads(resources)
                except (json.JSONDecodeError, ValueError):
                    resources = [resources] if resources else []
            ws["resources"] = resources
        if data.get("temporal_pattern"):
            ws["temporal_pattern"] = data["temporal_pattern"]
        if data.get("engagement_profile"):
            ws["engagement_profile"] = data["engagement_profile"]
        if data.get("initiation_profile"):
            ws["initiation_profile"] = data["initiation_profile"]
        if data.get("identity_context"):
            ws["identity_context"] = data["identity_context"]
        return ws

    @staticmethod
    def _merge_working_sphere(meta: Dict[str, Any], ws: Dict[str, Any]) -> Dict[str, Any]:
        """Merge working-sphere fields into activity metadata, unioning list fields."""
        for key in ("purpose", "temporal_pattern", "engagement_profile", "initiation_profile", "identity_context"):
            if ws.get(key):
                meta[key] = ws[key]
        for list_key in ("people", "resources"):
            new_vals = ws.get(list_key) or []
            old_vals = meta.get(list_key) or []
            if new_vals:
                merged = list(dict.fromkeys(old_vals + new_vals))  # union preserving order
                meta[list_key] = merged
        return meta

    async def _apply_decision(
        self,
        *,
        candidate: Entity,
        actions: List[Entity],
        data: Dict[str, Any],
        result: ReconcileResult,
        batch_id: Optional[int],
    ) -> None:
        decision = str(data.get("decision", "")).strip().lower()
        # Treat legacy "fork" as "new" for backwards compatibility
        if decision == "fork":
            decision = "new"
        target_goal_id = data.get("target_goal_id")
        new_goal_label = data.get("new_goal_label")
        revised_label = data.get("revised_label")
        goal_summary = data.get("goal_summary")
        scope_tags = data.get("scope_tags") or []
        # Safety net: Gemini may return strings for list fields
        if isinstance(scope_tags, str):
            try:
                scope_tags = json.loads(scope_tags)
            except (json.JSONDecodeError, ValueError):
                scope_tags = [scope_tags] if scope_tags else []
        merge_goal_ids = data.get("merge_goal_ids") or []
        if isinstance(merge_goal_ids, str):
            try:
                merge_goal_ids = json.loads(merge_goal_ids)
            except (json.JSONDecodeError, ValueError):
                merge_goal_ids = []
        relations = data.get("relations") or []
        if isinstance(relations, str):
            try:
                relations = json.loads(relations)
            except (json.JSONDecodeError, ValueError):
                relations = []
        confidence = self._coerce_int(data.get("confidence"))
        ws = self._extract_working_sphere(data)

        if decision in ("match", "revise", "merge"):
            if not target_goal_id or target_goal_id == candidate.id:
                # Self-reference or missing target — treat as "new" (keep candidate)
                decision = "new"
            target = await self.store.entities.get(target_goal_id)
            if not target:
                self.log.warning(
                    "reconcile: target_goal_id=%s not found for candidate=%s (decision=%s), skipping",
                    target_goal_id, candidate.id, decision,
                )
                return

            # Respect locked constraints: don't merge with locked targets
            cand_meta = candidate.metadata_dict or {}
            target_meta = target.metadata_dict or {}
            if decision == "merge" and (cand_meta.get("user_locked") or target_meta.get("user_locked")):
                self.log.debug(
                    "reconcile: refusing merge (locked entity involved), candidate=%s target=%s",
                    candidate.id, target.id,
                )
                decision = "match"  # Downgrade to match instead

            moved = await self._move_actions(candidate.id, target.id)
            meta = target.metadata_dict.copy() if target.metadata_dict else {}
            if revised_label and decision in ("revise", "merge"):
                # Respect user-edited and locked labels — don't overwrite them
                if not meta.get("user_edited") and not meta.get("user_locked"):
                    await self.store.entities.update(target.id, text=revised_label)
                    meta["revision_count"] = int(meta.get("revision_count", 0) or 0) + 1
            if goal_summary:
                meta["evidence_summary"] = goal_summary
            if scope_tags:
                meta["scope_tags"] = scope_tags
            meta = self._merge_working_sphere(meta, ws)
            self._bump_usage(meta, moved)
            meta = self._apply_probation(meta, moved, batch_id, confidence)
            meta = self._mark_batch_seen(meta, batch_id)
            if decision == "merge":
                meta["merge_count"] = int(meta.get("merge_count", 0) or 0) + 1
            meta = self._update_activity_scores(meta)
            await self.store.entities.update(target.id, metadata=meta)

            # Merge additional existing goals if requested.
            if merge_goal_ids:
                for mid in merge_goal_ids:
                    if mid == target.id or mid == candidate.id:
                        continue
                    await self._merge_goal_into_target(mid, target.id)
                if decision == "merge":
                    result.merged += 1

            await self._apply_relations(target.id, relations, result)
            await self._delete_activity(candidate.id)
            result.candidate_to_surviving[candidate.id] = target.id
            result.matched += 1 if decision == "match" else 0
            result.revised += 1 if decision == "revise" else 0
            return

        if decision == "new":
            if new_goal_label:
                await self.store.entities.update(candidate.id, text=new_goal_label)

            meta = candidate.metadata_dict.copy() if candidate.metadata_dict else {}
            if goal_summary:
                meta["evidence_summary"] = goal_summary
            if scope_tags:
                meta["scope_tags"] = scope_tags
            meta = self._merge_working_sphere(meta, ws)
            if self.skip_probation:
                meta["status"] = "active"
            else:
                meta = self._apply_probation(meta, len(actions), batch_id, confidence, force_probation=True)
            meta = self._mark_batch_seen(meta, batch_id)
            meta = self._update_activity_scores(meta)
            await self.store.entities.update(candidate.id, metadata=meta)

            await self._apply_relations(candidate.id, relations, result)
            result.candidate_to_surviving[candidate.id] = candidate.id
            result.created += 1
            return

    async def _merge_goal_into_target(self, source_goal_id: int, target_goal_id: int) -> None:
        # Check if either entity is locked — refuse merge if so
        source = await self.store.entities.get(source_goal_id)
        target = await self.store.entities.get(target_goal_id)
        if source:
            s_meta = source.metadata_dict or {}
            if s_meta.get("user_locked") or s_meta.get("user_edited"):
                self.log.debug("reconcile: refusing merge — source %s is user-protected", source_goal_id)
                return
        if target:
            t_meta = target.metadata_dict or {}
            if t_meta.get("user_locked"):
                self.log.debug("reconcile: refusing merge — target %s is locked", target_goal_id)
                return

        # Union working sphere metadata (people, resources) from source into target
        if source and target:
            s_meta = source.metadata_dict or {}
            t_meta = target.metadata_dict.copy() if target.metadata_dict else {}
            for list_field in ("people", "resources"):
                s_vals = s_meta.get(list_field) or []
                t_vals = t_meta.get(list_field) or []
                merged = list(dict.fromkeys(t_vals + s_vals))  # union preserving order
                if merged:
                    t_meta[list_field] = merged
            await self.store.entities.update(target_goal_id, metadata=t_meta)
        await self._move_actions(source_goal_id, target_goal_id)
        await self._delete_activity(source_goal_id)

    async def _move_actions(self, source_goal_id: int, target_goal_id: int) -> int:
        relations = await self.store.relations.get_by_target(
            source_goal_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        if not relations:
            relations = []
        existing = await self.store.relations.get_by_target(
            target_goal_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        existing_sources = {rel.source_id for rel in existing}

        moved = 0
        for rel in relations:
            if rel.source_id in existing_sources:
                continue
            await self.store.relations.create(
                source_id=rel.source_id,
                target_id=target_goal_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
                metadata=rel.metadata_dict if rel.metadata_dict else None,
            )
            moved += 1

        # Move behavioral action->activity relations as well (supports/hinders).
        behavioral = await self.store.relations.get_by_target(
            source_goal_id,
            relation_type=RelationType.BEHAVIORAL,
        )
        existing_behavioral = await self.store.relations.get_by_target(
            target_goal_id,
            relation_type=RelationType.BEHAVIORAL,
        )
        existing_beh_pairs = {(rel.source_id, rel.relation_subtype) for rel in existing_behavioral}

        for rel in behavioral:
            if (rel.source_id, rel.relation_subtype) in existing_beh_pairs:
                continue
            await self.store.relations.create(
                source_id=rel.source_id,
                target_id=target_goal_id,
                relation_type=RelationType.BEHAVIORAL,
                relation_subtype=rel.relation_subtype,
                confidence=rel.confidence,
                metadata=rel.metadata_dict if rel.metadata_dict else None,
            )

        # Remove old relations to avoid duplicates after merge.
        for rel in relations + behavioral:
            try:
                await self.store.relations.delete(rel.id)
            except Exception:
                continue

        # Refresh target activity timestamps from all linked actions
        if moved > 0:
            await self._refresh_activity_timestamps(target_goal_id)

        return moved

    async def _refresh_activity_timestamps(self, activity_id: int) -> None:
        """Recompute activity timestamp_start/end from all linked actions."""
        rels = await self.store.relations.get_by_target(
            activity_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        if not rels:
            return
        ts_start = None
        ts_end = None
        for rel in rels:
            action = await self.store.entities.get(rel.source_id)
            if not action:
                continue
            a_start = action.timestamp_start
            a_end = action.timestamp_end or action.timestamp_start
            if ts_start is None or a_start < ts_start:
                ts_start = a_start
            if ts_end is None or a_end > ts_end:
                ts_end = a_end
        if ts_start:
            await self.store.entities.update(
                activity_id,
                timestamp_start=ts_start,
                timestamp_end=ts_end,
            )

    async def _delete_activity(self, activity_id: int) -> None:
        try:
            entity = await self.store.entities.get(activity_id)
            entity_text = entity.text[:80] if entity else "unknown"
            entity_type = entity.type if entity else "unknown"
            await self.store.entities.delete(activity_id)
            self.log.info(
                "reconcile: deleted entity id=%s type=%s text=%s",
                activity_id, entity_type, entity_text,
            )
        except Exception as e:
            self.log.warning("reconcile: failed to delete entity id=%s: %s", activity_id, e)
            return

    async def _apply_relations(
        self,
        source_goal_id: int,
        relations: Sequence[Dict[str, Any]],
        result: ReconcileResult,
    ) -> None:
        if not relations:
            return

        behavioral_existing = await self.store.relations.get_by_source(
            source_goal_id,
            relation_type=RelationType.BEHAVIORAL,
        )
        temporal_existing = await self.store.relations.get_by_source(
            source_goal_id,
            relation_type=RelationType.TEMPORAL,
        )
        existing_pairs = {(rel.target_id, rel.relation_type, rel.relation_subtype) for rel in behavioral_existing + temporal_existing}

        for rel in relations:
            other_id = rel.get("other_goal_id")
            relation = str(rel.get("relation", "")).strip().lower()
            confidence = self._coerce_float(rel.get("confidence"))
            rationale = rel.get("rationale")
            if not other_id or other_id == source_goal_id:
                continue

            if relation == "supports":
                subtype = RelationSubtype.SUPPORTS
                if (other_id, RelationType.BEHAVIORAL, subtype) in existing_pairs:
                    continue
                await self.store.relations.create(
                    source_id=source_goal_id,
                    target_id=other_id,
                    relation_type=RelationType.BEHAVIORAL,
                    relation_subtype=subtype,
                    confidence=confidence,
                    metadata={"rationale": rationale} if rationale else None,
                )
                result.relations_created += 1
            elif relation == "conflicts":
                subtype = RelationSubtype.HINDERS
                if (other_id, RelationType.BEHAVIORAL, subtype) not in existing_pairs:
                    await self.store.relations.create(
                        source_id=source_goal_id,
                        target_id=other_id,
                        relation_type=RelationType.BEHAVIORAL,
                        relation_subtype=subtype,
                        confidence=confidence,
                        metadata={"rationale": rationale} if rationale else None,
                    )
                    result.relations_created += 1
                # Create symmetric conflict link if missing.
                back_links = await self.store.relations.get_by_source(
                    other_id,
                    relation_type=RelationType.BEHAVIORAL,
                    relation_subtype=RelationSubtype.HINDERS,
                )
                if not any(bl.target_id == source_goal_id for bl in back_links):
                    await self.store.relations.create(
                        source_id=other_id,
                        target_id=source_goal_id,
                        relation_type=RelationType.BEHAVIORAL,
                        relation_subtype=subtype,
                        confidence=confidence,
                        metadata={"rationale": rationale} if rationale else None,
                    )
                    result.relations_created += 1
            elif relation in ("overlaps", "co_occurs", "competes"):
                if relation == "overlaps":
                    subtype = RelationSubtype.OVERLAPS
                elif relation == "co_occurs":
                    subtype = RelationSubtype.CO_OCCURS
                else:
                    subtype = RelationSubtype.COMPETES
                if (other_id, RelationType.TEMPORAL, subtype) not in existing_pairs:
                    await self.store.relations.create(
                        source_id=source_goal_id,
                        target_id=other_id,
                        relation_type=RelationType.TEMPORAL,
                        relation_subtype=subtype,
                        confidence=confidence,
                        metadata={"rationale": rationale} if rationale else None,
                    )
                    result.relations_created += 1
                # Temporal relations are symmetric; add reverse if missing.
                back_links = await self.store.relations.get_by_source(
                    other_id,
                    relation_type=RelationType.TEMPORAL,
                    relation_subtype=subtype,
                )
                if not any(bl.target_id == source_goal_id for bl in back_links):
                    await self.store.relations.create(
                        source_id=other_id,
                        target_id=source_goal_id,
                        relation_type=RelationType.TEMPORAL,
                        relation_subtype=subtype,
                        confidence=confidence,
                        metadata={"rationale": rationale} if rationale else None,
                    )
                    result.relations_created += 1

    async def _get_actions_for_activity(self, activity_id: int) -> List[Entity]:
        rels = await self.store.relations.get_by_target(
            activity_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        actions: List[Entity] = []
        for rel in rels:
            ent = await self.store.entities.get(rel.source_id)
            if ent and ent.type == EntityType.ACTION:
                actions.append(ent)
        actions.sort(key=lambda a: a.timestamp_start or datetime.min)
        if len(actions) > self.max_actions_per_candidate:
            actions = actions[-self.max_actions_per_candidate :]
        return actions

    def _actions_block(self, actions: Sequence[Entity]) -> str:
        if not actions:
            return "None"
        lines = []
        for a in actions:
            meta = a.metadata_dict if a.metadata_dict and isinstance(a.metadata_dict, dict) else {}
            goal_hint = meta.get("goal_hint") or ""
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

            # Behavioral state fields from Stage 2
            engagement_state = meta.get("engagement_state")
            cognitive_mode = meta.get("cognitive_mode")
            initiation = meta.get("initiation")
            social_mode = meta.get("social_mode")

            extra_parts = []
            if confidence:
                extra_parts.append(f"conf={confidence}/10")
            if decay:
                extra_parts.append(f"decay={decay}/10")
            if duration_str:
                extra_parts.append(f"dur={duration_str}")
            if engagement_state:
                extra_parts.append(f"engage={engagement_state}")
            if cognitive_mode:
                extra_parts.append(f"cog={cognitive_mode}")
            if initiation:
                extra_parts.append(f"init={initiation}")
            if social_mode:
                extra_parts.append(f"social={social_mode}")
            extra_str = " | " + " ".join(extra_parts) if extra_parts else ""

            lines.append(
                f"- ID:{a.id} | {a.text} | goal_hint:{goal_hint} | ts:{a.timestamp_start}{extra_str}"
            )
        return "\n".join(lines)

    async def _find_connected_activities(
        self,
        candidate_entries: List[Tuple[Entity, List[Entity]]],
        exclude_ids: set,
    ) -> set:
        """Find existing activities that are graph-neighbors of candidates' actions.

        Traverses: candidate actions → temporal relations (FOLLOWS, CO_OCCURS,
        OVERLAPS) → neighbor actions → PART_OF → parent activities.

        Returns set of connected activity IDs (excluding candidates and exclude_ids).
        """
        neighbor_action_ids: set = set()
        all_candidate_action_ids: set = set()

        for _candidate, actions in candidate_entries:
            for action in actions:
                all_candidate_action_ids.add(action.id)
                for subtype in (RelationSubtype.FOLLOWS, RelationSubtype.CO_OCCURS, RelationSubtype.OVERLAPS):
                    rels_out = await self.store.relations.get_by_source(
                        action.id,
                        relation_type=RelationType.TEMPORAL,
                        relation_subtype=subtype,
                    )
                    for rel in rels_out:
                        neighbor_action_ids.add(rel.target_id)
                    rels_in = await self.store.relations.get_by_target(
                        action.id,
                        relation_type=RelationType.TEMPORAL,
                        relation_subtype=subtype,
                    )
                    for rel in rels_in:
                        neighbor_action_ids.add(rel.source_id)

        # Remove candidate actions themselves
        neighbor_action_ids -= all_candidate_action_ids

        # Follow PART_OF to parent activities
        connected_activity_ids: set = set()
        for aid in neighbor_action_ids:
            rels = await self.store.relations.get_by_source(
                aid,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            for rel in rels:
                if rel.target_id not in exclude_ids:
                    connected_activity_ids.add(rel.target_id)

        return connected_activity_ids

    @staticmethod
    def _candidate_metadata_block(candidate: Entity) -> str:
        """Build a working-sphere metadata block for a candidate activity."""
        meta = candidate.metadata_dict if candidate.metadata_dict and isinstance(candidate.metadata_dict, dict) else {}
        parts = []
        if meta.get("purpose"):
            parts.append(f"purpose={meta['purpose']}")
        people = meta.get("people") or []
        if people:
            parts.append(f"people={', '.join(str(p) for p in people)}")
        resources = meta.get("resources") or []
        if resources:
            parts.append(f"resources={', '.join(str(r) for r in resources)}")
        if meta.get("temporal_pattern"):
            parts.append(f"temporal={meta['temporal_pattern']}")
        if meta.get("engagement_profile"):
            parts.append(f"engage={meta['engagement_profile']}")
        if meta.get("initiation_profile"):
            parts.append(f"init={meta['initiation_profile']}")
        if meta.get("identity_context"):
            parts.append(f"domain={meta['identity_context']}")
        if not parts:
            return ""
        return "Working sphere: " + " | ".join(parts)

    def _existing_block(
        self,
        detailed: Sequence[Entity],
        summary_only: Sequence[Entity] = (),
    ) -> str:
        if not detailed and not summary_only:
            return "None"
        lines = []
        if detailed:
            lines.append("### Primary candidates (full detail):")
            for act in detailed:
                meta = act.metadata_dict if act.metadata_dict and isinstance(act.metadata_dict, dict) else {}
                status = meta.get("status", "active")
                usage = meta.get("usage_count", 0)
                last = meta.get("last_assigned_ts", "never")
                confidence = meta.get("activity_confidence")
                stability = meta.get("activity_stability")

                extra_parts = []
                if confidence is not None:
                    extra_parts.append(f"conf={confidence}/10")
                if stability is not None:
                    extra_parts.append(f"stability={stability}/10")
                # Mark constraint flags
                if meta.get("user_locked"):
                    extra_parts.append("[locked]")
                if meta.get("user_edited"):
                    extra_parts.append("[user-edited]")
                # Working-sphere fields (replace evidence_summary)
                if meta.get("purpose"):
                    extra_parts.append(f"purpose:{meta['purpose']}")
                people = meta.get("people") or []
                if people:
                    extra_parts.append(f"people:{', '.join(str(p) for p in people)}")
                resources = meta.get("resources") or []
                if resources:
                    extra_parts.append(f"resources:{', '.join(str(r) for r in resources)}")
                if meta.get("identity_context"):
                    extra_parts.append(f"domain:{meta['identity_context']}")
                extra_str = " | " + " | ".join(extra_parts) if extra_parts else ""

                lines.append(
                    f"- ID:{act.id} | {act.text} | status:{status} | used:{usage}x | last:{last}{extra_str}"
                )
        if summary_only:
            lines.append("\n### Other known activities (title only — you may still match/merge with these):")
            for act in summary_only:
                lines.append(f"- ID:{act.id} | {act.text}")
        return "\n".join(lines)

    def _apply_probation(
        self,
        meta: Dict[str, Any],
        action_count: int,
        batch_id: Optional[int],
        confidence: Optional[int],
        *,
        force_probation: bool = False,
    ) -> Dict[str, Any]:
        status = meta.get("status")
        if force_probation and status != "probation":
            status = "probation"
        if status != "probation":
            meta["status"] = status or "active"
            return meta

        meta["status"] = "probation"
        meta["probation_action_count"] = meta.get("probation_action_count", 0) + action_count
        if batch_id is not None:
            last_batch = meta.get("probation_last_batch_id")
            if last_batch != batch_id:
                meta["probation_batches_seen"] = meta.get("probation_batches_seen", 0) + 1
                meta["probation_last_batch_id"] = batch_id
        if (
            meta.get("probation_action_count", 0) >= self.probation_min_actions
            or meta.get("probation_batches_seen", 0) >= self.probation_min_batches
            or (confidence is not None and confidence >= self.probation_confidence)
        ):
            meta["status"] = "active"
        return meta

    def _mark_batch_seen(self, meta: Dict[str, Any], batch_id: Optional[int]) -> Dict[str, Any]:
        if batch_id is None:
            return meta
        last_batch = meta.get("last_batch_id")
        if last_batch != batch_id:
            meta["batches_seen"] = int(meta.get("batches_seen", 0) or 0) + 1
            meta["last_batch_id"] = batch_id
        return meta

    def _update_activity_scores(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return update_activity_scores(meta)

    def _bump_usage(self, meta: Dict[str, Any], moved: int) -> None:
        usage = int(meta.get("usage_count", 0) or 0)
        meta["usage_count"] = usage + moved
        meta["last_assigned_ts"] = datetime.utcnow().isoformat()

    def _parse_json_response(self, response: str) -> Optional[Dict[str, Any]]:
        try:
            data = parse_llm_json(response)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}
            return data
        except Exception as e:
            if self.debug:
                snippet = (response or "").strip().replace("\n", " ")[:200]
                self.log.warning("reconcile: json parse error: %s | snippet=%s", e, snippet)
            return None

    def _coerce_int(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except Exception:
            return None

    def _coerce_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

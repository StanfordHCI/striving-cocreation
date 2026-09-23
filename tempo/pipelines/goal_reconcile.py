"""GoalReconcileJob: reconcile candidate goals against the existing repository.

Takes candidate goal entities (from GoalProposeJob) and integrates them
with existing goals via match/revise/new/merge decisions.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from tempo.models import Entity, EntityType, RelationType, RelationSubtype
from tempo.prompts.goal_reconcile import GOAL_RECONCILE_PROMPT
from tempo.schemas import GoalProposeCandidate, GoalReconcileResult, GoalReconcileDecisionItem, get_schema
from tempo.utils import get_debug_logger, parse_llm_json

GOAL_RECONCILE_FORMAT = get_schema(GoalReconcileResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger
    from tempo.providers import ModelProvider
    from tempo.store import Store


class GoalReconcileJob:
    """Reconcile candidate goals against the existing goal repository."""

    def __init__(
        self,
        provider: "ModelProvider",
        store: "Store",
        *,
        user_name: Optional[str] = None,
        debug: bool = False,
        user_context: str = "",
        debug_logger: Optional["DebugLogger"] = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.user_name = user_name or os.getenv("USER_NAME", "the user")
        self.debug = debug
        self.user_context = user_context
        self.log = get_debug_logger(self, debug=debug)
        self.debug_logger = debug_logger

    @staticmethod
    def _render_prompt(template: str, **values: Any) -> str:
        rendered = template
        for key, val in values.items():
            rendered = rendered.replace("{" + key + "}", str(val))
        return rendered.replace("{{", "{").replace("}}", "}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        candidate_entities: List[Entity],
        propose_by_db_id: Dict[int, GoalProposeCandidate],
        *,
        all_activities: Optional[List[Entity]] = None,
    ) -> Dict[str, Any]:
        """Reconcile proposed candidates against existing goals.

        Args:
            candidate_entities: Provisional goal entities from GoalProposeJob.
            propose_by_db_id: Mapping from entity DB ID to the original
                GoalProposeCandidate.
            all_activities: Optional pre-fetched list of all activities.
                If not provided, loaded from DB.

        Returns:
            Dict with created/updated/matched/merged counts.
        """
        if not candidate_entities:
            return {"created": 0, "updated": 0, "matched": 0, "merged": 0}

        existing_goals = await self.store.get_goals()

        if all_activities is None:
            all_activities = await self.store.entities.get_by_type(EntityType.ACTIVITY)

        user_constraints_block = await self._user_constraints_block()

        # RECONCILE: integrate candidates with existing goal repository
        decisions = await self._reconcile_goals(
            candidate_entities, propose_by_db_id, existing_goals,
            user_constraints_block,
        )
        if not decisions:
            self.log.debug("goal_reconcile: reconcile returned no decisions — cleaning up candidates")
            for entity in candidate_entities:
                await self._delete_candidate(entity.id)
            return {"created": 0, "updated": 0, "matched": 0, "merged": 0}

        # Apply decisions
        result = await self._apply_decisions(
            decisions, candidate_entities, propose_by_db_id,
            existing_goals, all_activities,
        )

        self.log.debug("goal_reconcile: done %s", result)
        return result

    # ------------------------------------------------------------------
    # Reconcile LLM call
    # ------------------------------------------------------------------

    async def _reconcile_goals(
        self,
        candidate_entities: List[Entity],
        propose_by_db_id: Dict[int, GoalProposeCandidate],
        existing_goals: List[Entity],
        user_constraints_block: str,
    ) -> List[GoalReconcileDecisionItem]:
        # Build candidate block using real DB IDs from provisional entities
        candidate_blocks = []
        for entity in candidate_entities:
            cand = propose_by_db_id.get(entity.id)
            meta = entity.metadata_dict or {}
            parts = [
                f"ID: {entity.id}",
                f"Text: {entity.text}",
            ]
            orientation = meta.get("orientation")
            if orientation:
                parts.append(f"Orientation: {orientation}")
            needs = meta.get("needs")
            if needs:
                parts.append(f"Needs: {', '.join(needs)}")
            if cand and cand.activity_ids:
                parts.append(f"Activity IDs: {cand.activity_ids}")
            evidence = meta.get("evidence_summary")
            if evidence:
                parts.append(f"Evidence: {evidence}")
            meta_parts = []
            people = meta.get("people")
            if people:
                meta_parts.append(f"People: {', '.join(str(p) for p in people)}")
            domains = meta.get("domains")
            if domains:
                meta_parts.append(f"Domains: {', '.join(domains)}")
            asp = meta.get("aspiration_vs_obligation")
            if asp:
                meta_parts.append(f"Aspiration/Obligation: {asp}")
            aut = meta.get("autonomy")
            if aut:
                meta_parts.append(f"Autonomy: {aut}")
            ident = meta.get("identity_link")
            if ident:
                meta_parts.append(f"Identity link: {ident}")
            feared = meta.get("feared_self")
            if feared:
                meta_parts.append(f"Feared self: {feared}")
            if meta_parts:
                parts.append(" | ".join(meta_parts))
            candidate_blocks.append("\n".join(parts))

        candidate_goals_block = "\n\n---\n\n".join(candidate_blocks)

        # Build existing goals block (two-tier), including linked activity labels
        detailed, summary_only = self._select_existing_goals(existing_goals)
        all_activities_now = await self.store.entities.get_by_type(EntityType.ACTIVITY)
        act_text_by_id = {a.id: (a.text or "")[:80] for a in all_activities_now}
        goal_activity_map: Dict[int, List[str]] = {}
        for g in list(detailed) + list(summary_only):
            rels = await self.store.relations.get_by_target(
                g.id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            act_labels = []
            for rel in (rels or []):
                label = act_text_by_id.get(rel.source_id)
                if label:
                    act_labels.append(label)
            if act_labels:
                goal_activity_map[g.id] = act_labels
        existing_goals_block = self._goals_block_two_tier(
            detailed, summary_only, goal_activity_map=goal_activity_map,
        )

        # Build goal conflicts block
        goal_conflicts_block = await self._goal_conflicts_block()

        user_context_block = ""
        if self.user_context:
            user_context_block = (
                "########################################\n"
                "# User context\n"
                "########################################\n"
                f"{self.user_context}\n"
            )

        prompt = self._render_prompt(
            GOAL_RECONCILE_PROMPT,
            user_name=self.user_name,
            candidate_goals=candidate_goals_block,
            existing_goals=existing_goals_block,
            existing_goal_count=str(len(existing_goals)),
            goal_conflicts=goal_conflicts_block,
            user_constraints=user_constraints_block,
            user_context=user_context_block,
        )

        self.log.debug(
            "goal_reconcile: candidates=%d existing=%d prompt_len=%d",
            len(candidate_entities), len(existing_goals), len(prompt),
        )

        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=GOAL_RECONCILE_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="goal_reconcile", stage="reconcile",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )

        return self._parse_reconcile_response(response)

    def _parse_reconcile_response(self, response: str) -> List[GoalReconcileDecisionItem]:
        try:
            data = self._parse_json(response)
            if not data:
                return []

            decisions_raw = data.get("decisions", [])
            if not isinstance(decisions_raw, list):
                if isinstance(data, dict) and data.get("decision"):
                    decisions_raw = [data]
                else:
                    return []

            decisions = []
            for d in decisions_raw:
                if not isinstance(d, dict):
                    continue
                raw_cid = d.get("candidate_id")
                try:
                    cid = int(raw_cid) if raw_cid is not None else None
                except (ValueError, TypeError):
                    cid = None
                decisions.append(GoalReconcileDecisionItem(
                    candidate_id=cid,
                    decision=d.get("decision", "new"),
                    target_goal_id=d.get("target_goal_id"),
                    revised_label=d.get("revised_label"),
                    goal_summary=d.get("goal_summary"),
                    needs=d.get("needs"),
                    orientation=d.get("orientation"),
                    aspiration_vs_obligation=d.get("aspiration_vs_obligation"),
                    autonomy_dim=d.get("autonomy_dim"),
                    identity_link=d.get("identity_link"),
                    feared_self=d.get("feared_self"),
                    people=d.get("people"),
                    domains=d.get("domains"),
                    engagement_signature=d.get("engagement_signature"),
                    initiation_signature=d.get("initiation_signature"),
                    temporal_context=d.get("temporal_context"),
                    evidence_summary=d.get("evidence_summary"),
                    merge_goal_ids=d.get("merge_goal_ids"),
                    relations=d.get("relations"),
                    confidence=d.get("confidence"),
                    decision_reasoning=d.get("decision_reasoning"),
                ))
            return decisions
        except Exception as e:
            self.log.warning("goal_reconcile: parse error (%s)", e)
            return []

    # ------------------------------------------------------------------
    # Apply decisions
    # ------------------------------------------------------------------

    async def _apply_decisions(
        self,
        decisions: List[GoalReconcileDecisionItem],
        candidate_entities: List[Entity],
        propose_by_db_id: Dict[int, GoalProposeCandidate],
        existing_goals: List[Entity],
        all_activities: List[Entity],
    ) -> Dict[str, Any]:
        result = {"created": 0, "updated": 0, "matched": 0, "merged": 0}
        existing_by_id = {g.id: g for g in existing_goals}
        candidates_by_id = {e.id: e for e in candidate_entities}
        activities_by_id = {a.id: a for a in all_activities}
        referenced_candidate_ids: set = set()

        for dec in decisions:
            candidate_entity = candidates_by_id.get(dec.candidate_id)
            propose_cand = propose_by_db_id.get(dec.candidate_id)
            if not candidate_entity or not propose_cand:
                self.log.warning("goal_reconcile: candidate_id=%s not found", dec.candidate_id)
                continue

            referenced_candidate_ids.add(dec.candidate_id)
            decision = str(dec.decision).strip().lower()

            try:
                if decision == "match":
                    await self._apply_match(dec, candidate_entity, propose_cand, existing_by_id, activities_by_id)
                    result["matched"] += 1

                elif decision == "revise":
                    await self._apply_revise(dec, candidate_entity, propose_cand, existing_by_id, activities_by_id)
                    result["updated"] += 1

                elif decision == "new":
                    await self._apply_new(dec, candidate_entity, propose_cand)
                    result["created"] += 1

                elif decision == "merge":
                    await self._apply_merge(dec, candidate_entity, propose_cand, existing_by_id, activities_by_id)
                    result["merged"] += 1

                else:
                    self.log.warning("goal_reconcile: unknown decision=%s", decision)

                goal_id = candidate_entity.id if decision == "new" else dec.target_goal_id
                if goal_id and dec.relations:
                    await self._apply_relations(goal_id, dec.relations)

            except Exception as e:
                self.log.warning(
                    "goal_reconcile: failed applying decision=%s candidate=%s: %s",
                    decision, dec.candidate_id, e,
                )

        # Clean up any unreferenced candidates (LLM skipped them)
        for entity in candidate_entities:
            if entity.id not in referenced_candidate_ids:
                self.log.debug("goal_reconcile: cleaning up unreferenced candidate %s", entity.id)
                await self._delete_candidate(entity.id)

        return result

    async def _apply_match(
        self,
        dec: GoalReconcileDecisionItem,
        candidate_entity: Entity,
        propose_cand: GoalProposeCandidate,
        existing_by_id: Dict[int, Entity],
        activities_by_id: Dict[int, Entity],
    ) -> None:
        """Match: move candidate's activities to existing goal, delete candidate."""
        target = existing_by_id.get(dec.target_goal_id)
        if not target:
            self.log.warning("goal_reconcile: match target %s not found", dec.target_goal_id)
            return

        await self._link_activities_to_goal(propose_cand.activity_ids, target.id)

        meta = target.metadata_dict.copy() if target.metadata_dict else {}
        meta = self._update_goal_metadata(meta, dec, propose_cand)
        meta = self._merge_screenshot_metadata(meta, candidate_entity.metadata_dict or {})
        meta = self._update_goal_scores(meta)
        await self.store.entities.update(target.id, metadata=meta)

        await self._refresh_goal_timestamps(target.id, activities_by_id)
        await self._delete_candidate(candidate_entity.id)

        if self.debug_logger:
            self.debug_logger.log_entity_mutation(
                pipeline="goal_reconcile", stage="apply_match",
                mutation="update", entity_id=target.id,
                entity_type="goal", entity_text=target.text,
                metadata=meta,
            )

    async def _apply_revise(
        self,
        dec: GoalReconcileDecisionItem,
        candidate_entity: Entity,
        propose_cand: GoalProposeCandidate,
        existing_by_id: Dict[int, Entity],
        activities_by_id: Dict[int, Entity],
    ) -> None:
        """Revise: move activities to target, update label/metadata, delete candidate."""
        target = existing_by_id.get(dec.target_goal_id)
        if not target:
            self.log.warning("goal_reconcile: revise target %s not found", dec.target_goal_id)
            return

        await self._link_activities_to_goal(propose_cand.activity_ids, target.id)

        meta = target.metadata_dict.copy() if target.metadata_dict else {}
        new_text = target.text
        if dec.revised_label and not meta.get("user_edited") and not meta.get("user_locked"):
            new_text = dec.revised_label
            meta["revision_count"] = int(meta.get("revision_count", 0) or 0) + 1

        meta = self._update_goal_metadata(meta, dec, propose_cand)
        meta = self._merge_screenshot_metadata(meta, candidate_entity.metadata_dict or {})
        meta = self._update_goal_scores(meta)

        prev_text = target.text
        await self.store.entities.update(target.id, text=new_text, metadata=meta)
        await self._refresh_goal_timestamps(target.id, activities_by_id)
        await self._delete_candidate(candidate_entity.id)

        if self.debug_logger:
            self.debug_logger.log_entity_mutation(
                pipeline="goal_reconcile", stage="apply_revise",
                mutation="update", entity_id=target.id,
                entity_type="goal", entity_text=new_text,
                metadata=meta, prev_text=prev_text,
            )

    async def _apply_new(
        self,
        dec: GoalReconcileDecisionItem,
        candidate_entity: Entity,
        propose_cand: GoalProposeCandidate,
    ) -> None:
        """New: promote provisional candidate to active goal."""
        goal_text = dec.revised_label or candidate_entity.text

        meta = candidate_entity.metadata_dict.copy() if candidate_entity.metadata_dict else {}
        meta["status"] = "active"
        meta = self._update_goal_metadata(meta, dec, propose_cand)
        meta = self._update_goal_scores(meta)

        await self.store.entities.update(candidate_entity.id, text=goal_text, metadata=meta)

        if self.debug_logger:
            self.debug_logger.log_entity_mutation(
                pipeline="goal_reconcile", stage="apply_new",
                mutation="create", entity_id=candidate_entity.id,
                entity_type="goal", entity_text=goal_text,
                metadata=meta,
            )

    async def _apply_merge(
        self,
        dec: GoalReconcileDecisionItem,
        candidate_entity: Entity,
        propose_cand: GoalProposeCandidate,
        existing_by_id: Dict[int, Entity],
        activities_by_id: Dict[int, Entity],
    ) -> None:
        """Merge: combine existing goals + candidate into target, delete candidate."""
        target = existing_by_id.get(dec.target_goal_id)
        if not target:
            self.log.warning("goal_reconcile: merge target %s not found", dec.target_goal_id)
            return

        target_meta = target.metadata_dict or {}
        if target_meta.get("user_locked"):
            self.log.debug("goal_reconcile: refusing merge — target %s is locked", target.id)
            await self._apply_match(dec, candidate_entity, propose_cand, existing_by_id, activities_by_id)
            return

        await self._link_activities_to_goal(propose_cand.activity_ids, target.id)

        merge_ids = dec.merge_goal_ids or []
        for mid in merge_ids:
            if mid == target.id or mid == candidate_entity.id:
                continue
            source = existing_by_id.get(mid)
            if not source:
                continue
            source_meta = source.metadata_dict or {}
            if source_meta.get("user_locked") or source_meta.get("user_edited"):
                self.log.debug("goal_reconcile: refusing merge — source %s is user-protected", mid)
                continue
            await self._merge_goal_into_target(mid, target.id)

        meta = target.metadata_dict.copy() if target.metadata_dict else {}
        if dec.revised_label and not meta.get("user_edited") and not meta.get("user_locked"):
            await self.store.entities.update(target.id, text=dec.revised_label)
        meta["merge_count"] = int(meta.get("merge_count", 0) or 0) + 1
        meta = self._update_goal_metadata(meta, dec, propose_cand)
        meta = self._merge_screenshot_metadata(meta, candidate_entity.metadata_dict or {})
        meta = self._update_goal_scores(meta)
        await self.store.entities.update(target.id, metadata=meta)
        await self._refresh_goal_timestamps(target.id, activities_by_id)
        await self._delete_candidate(candidate_entity.id)

        if self.debug_logger:
            self.debug_logger.log_entity_mutation(
                pipeline="goal_reconcile", stage="apply_merge",
                mutation="update", entity_id=target.id,
                entity_type="goal", entity_text=target.text,
                metadata=meta,
            )

    # ------------------------------------------------------------------
    # Helpers: activity → goal linking
    # ------------------------------------------------------------------

    async def _link_activities_to_goal(
        self,
        activity_ids: List[int],
        goal_id: int,
    ) -> None:
        """Create PART_OF relations from activities to goal (skip duplicates)."""
        existing_rels = await self.store.relations.get_by_target(
            goal_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        existing_sources = {rel.source_id for rel in (existing_rels or [])}

        for aid in activity_ids:
            if aid in existing_sources:
                continue
            await self.store.relations.create(
                source_id=aid,
                target_id=goal_id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )

    async def _merge_goal_into_target(self, source_id: int, target_id: int) -> None:
        """Move all activity links from source goal to target, then delete source."""
        source = await self.store.entities.get(source_id)
        target = await self.store.entities.get(target_id)
        if source and (
            (source.metadata_dict or {}).get("user_locked")
            or (source.metadata_dict or {}).get("user_edited")
        ):
            self.log.debug("goal_reconcile: refusing merge — source %s is user-protected", source_id)
            return
        if target and (target.metadata_dict or {}).get("user_locked"):
            self.log.debug("goal_reconcile: refusing merge — target %s is locked", target_id)
            return

        rels = await self.store.relations.get_by_target(
            source_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        existing_rels = await self.store.relations.get_by_target(
            target_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        existing_sources = {r.source_id for r in (existing_rels or [])}

        for rel in (rels or []):
            if rel.source_id not in existing_sources:
                await self.store.relations.create(
                    source_id=rel.source_id,
                    target_id=target_id,
                    relation_type=RelationType.STRUCTURAL,
                    relation_subtype=RelationSubtype.PART_OF,
                )

        beh_rels = await self.store.relations.get_by_target(
            source_id,
            relation_type=RelationType.BEHAVIORAL,
        )
        existing_beh = await self.store.relations.get_by_target(
            target_id,
            relation_type=RelationType.BEHAVIORAL,
        )
        existing_beh_pairs = {(r.source_id, r.relation_subtype) for r in (existing_beh or [])}

        for rel in (beh_rels or []):
            if (rel.source_id, rel.relation_subtype) not in existing_beh_pairs:
                await self.store.relations.create(
                    source_id=rel.source_id,
                    target_id=target_id,
                    relation_type=RelationType.BEHAVIORAL,
                    relation_subtype=rel.relation_subtype,
                    confidence=rel.confidence,
                    metadata=rel.metadata_dict if rel.metadata_dict else None,
                )

        if source and target:
            s_meta = source.metadata_dict or {}
            t_meta = target.metadata_dict.copy() if target.metadata_dict else {}
            for list_field in ("people", "domains", "observation_screenshots", "context_screenshots", "new_screenshots"):
                s_vals = s_meta.get(list_field) or []
                t_vals = t_meta.get(list_field) or []
                merged = list(dict.fromkeys(t_vals + s_vals))
                if merged:
                    t_meta[list_field] = merged
            await self.store.entities.update(target_id, metadata=t_meta)

        for rel in (rels or []) + (beh_rels or []):
            try:
                await self.store.relations.delete(rel.id)
            except Exception:
                continue

        try:
            await self.store.entities.delete(source_id)
            self.log.info("goal_reconcile: merged goal %s into %s", source_id, target_id)
        except Exception as e:
            self.log.warning("goal_reconcile: failed to delete merged goal %s: %s", source_id, e)

    async def _delete_candidate(self, entity_id: int) -> None:
        """Delete a provisional candidate entity and its relations."""
        rels = await self.store.relations.get_by_target(
            entity_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        for rel in (rels or []):
            try:
                await self.store.relations.delete(rel.id)
            except Exception:
                pass

        out_rels = await self.store.relations.get_by_source(entity_id)
        for rel in (out_rels or []):
            try:
                await self.store.relations.delete(rel.id)
            except Exception:
                pass

        try:
            await self.store.entities.delete(entity_id)
        except Exception as e:
            self.log.warning(
                "goal_reconcile: failed to delete candidate %s: %s",
                entity_id, e,
            )

    # ------------------------------------------------------------------
    # Helpers: goal-goal relations
    # ------------------------------------------------------------------

    async def _apply_relations(
        self,
        goal_id: int,
        relations: Sequence[Dict[str, Any]],
    ) -> None:
        """Create goal-goal relations from reconcile output."""
        if not relations:
            return
        if isinstance(relations, str):
            try:
                relations = json.loads(relations)
            except (json.JSONDecodeError, ValueError):
                return

        for rel in relations:
            if not isinstance(rel, dict):
                continue
            other_id = rel.get("other_goal_id")
            relation = str(rel.get("relation", "")).strip().lower()
            confidence = self._coerce_float(rel.get("confidence"))
            rationale = rel.get("rationale")

            if not other_id or other_id == goal_id:
                continue

            if relation == "supports":
                subtype = RelationSubtype.SUPPORTS
                rel_type = RelationType.BEHAVIORAL
            elif relation == "conflicts":
                subtype = RelationSubtype.HINDERS
                rel_type = RelationType.BEHAVIORAL
            elif relation in ("overlaps", "co_occurs", "competes"):
                subtype = {
                    "overlaps": RelationSubtype.OVERLAPS,
                    "co_occurs": RelationSubtype.CO_OCCURS,
                    "competes": RelationSubtype.COMPETES,
                }[relation]
                rel_type = RelationType.TEMPORAL
            else:
                continue

            existing = await self.store.relations.get_by_source(
                goal_id, relation_type=rel_type, relation_subtype=subtype,
            )
            if any(r.target_id == other_id for r in (existing or [])):
                continue

            await self.store.relations.create(
                source_id=goal_id,
                target_id=other_id,
                relation_type=rel_type,
                relation_subtype=subtype,
                confidence=confidence,
                metadata={"rationale": rationale} if rationale else None,
            )

            if relation in ("conflicts", "overlaps", "co_occurs", "competes"):
                back = await self.store.relations.get_by_source(
                    other_id, relation_type=rel_type, relation_subtype=subtype,
                )
                if not any(r.target_id == goal_id for r in (back or [])):
                    await self.store.relations.create(
                        source_id=other_id,
                        target_id=goal_id,
                        relation_type=rel_type,
                        relation_subtype=subtype,
                        confidence=confidence,
                        metadata={"rationale": rationale} if rationale else None,
                    )

    # ------------------------------------------------------------------
    # Helpers: metadata
    # ------------------------------------------------------------------

    def _update_goal_metadata(
        self,
        meta: Dict[str, Any],
        dec: GoalReconcileDecisionItem,
        candidate: GoalProposeCandidate,
    ) -> Dict[str, Any]:
        """Update goal metadata from reconcile decision + propose candidate."""
        meta["last_synthesized_ts"] = datetime.utcnow().isoformat()

        existing_usage = int(meta.get("usage_count", 0) or 0)
        meta["usage_count"] = existing_usage + len(candidate.activity_ids)

        if dec.confidence is not None:
            meta["goal_confidence"] = dec.confidence
        if dec.evidence_summary:
            meta["evidence_summary"] = dec.evidence_summary
        elif candidate.evidence_summary:
            meta["evidence_summary"] = candidate.evidence_summary

        needs = dec.needs or candidate.needs
        if needs:
            meta["needs"] = needs
        orientation = dec.orientation or candidate.orientation
        if orientation:
            meta["orientation"] = orientation
        asp = dec.aspiration_vs_obligation or candidate.aspiration_vs_obligation
        if asp:
            meta["aspiration_vs_obligation"] = asp
        aut = dec.autonomy_dim or candidate.autonomy
        if aut:
            meta["autonomy"] = aut
        ident = dec.identity_link or candidate.identity_link
        if ident:
            meta["identity_link"] = ident
        feared = dec.feared_self or candidate.feared_self
        if feared:
            meta["feared_self"] = feared

        for list_field in ("people", "domains"):
            new_vals = getattr(dec, list_field, None) or getattr(candidate, list_field, None) or []
            old_vals = meta.get(list_field) or []
            if new_vals:
                merged = list(dict.fromkeys(old_vals + new_vals))
                meta[list_field] = merged

        for str_field in ("engagement_signature", "initiation_signature", "temporal_context"):
            val = getattr(dec, str_field, None) or getattr(candidate, str_field, None)
            if val:
                meta[str_field] = val

        return meta

    def _update_goal_scores(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Compute goal confidence and stability scores."""
        import math

        usage = int(meta.get("usage_count", 0) or 0)
        batches_seen = int(meta.get("batches_seen", 0) or 0) + 1
        meta["batches_seen"] = batches_seen

        supporting_norm = min(1.0, math.log1p(usage) / math.log1p(20))
        batches_norm = min(1.0, batches_seen / 5.0)

        recency_score = 0.2
        last_seen = meta.get("last_synthesized_ts")
        if last_seen:
            try:
                last_ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
                days = max(0.0, (datetime.utcnow() - last_ts.replace(tzinfo=None)).days)
                if days <= 1:
                    recency_score = 1.0
                elif days <= 7:
                    recency_score = 0.8
                elif days <= 30:
                    recency_score = 0.5
            except Exception:
                pass

        churn_events = int(meta.get("merge_count", 0) or 0) + int(meta.get("revision_count", 0) or 0)
        churn_rate = min(1.0, churn_events / max(1.0, float(batches_seen)))
        stability = max(0.0, 1.0 - churn_rate)

        confidence_raw = (
            0.4 * supporting_norm
            + 0.3 * batches_norm
            + 0.2 * recency_score
            + 0.1 * stability
        )
        meta["goal_confidence_score"] = max(1, min(10, int(round(1 + 9 * confidence_raw))))
        meta["goal_stability_score"] = max(1, min(10, int(round(1 + 9 * stability))))
        return meta

    def _merge_screenshot_metadata(
        self, meta: Dict[str, Any], source_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge screenshot list fields from *source_meta* into *meta*."""
        for field in ("observation_screenshots", "context_screenshots", "new_screenshots"):
            src_vals = source_meta.get(field) or []
            tgt_vals = meta.get(field) or []
            if src_vals:
                meta[field] = list(dict.fromkeys(tgt_vals + src_vals))
        return meta

    async def _refresh_goal_timestamps(
        self,
        goal_id: int,
        activities_by_id: Dict[int, Entity],
    ) -> None:
        """Recompute goal timestamp_start/end from constituent activities."""
        rels = await self.store.relations.get_by_target(
            goal_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        if not rels:
            return
        ts_start = None
        ts_end = None
        for rel in rels:
            act = activities_by_id.get(rel.source_id)
            if not act:
                act = await self.store.entities.get(rel.source_id)
            if not act:
                continue
            a_start = act.timestamp_start
            a_end = act.timestamp_end or act.timestamp_start
            if ts_start is None or a_start < ts_start:
                ts_start = a_start
            if ts_end is None or a_end > ts_end:
                ts_end = a_end
        if ts_start:
            await self.store.entities.update(
                goal_id, timestamp_start=ts_start, timestamp_end=ts_end,
            )

    # ------------------------------------------------------------------
    # Helpers: prompt formatting
    # ------------------------------------------------------------------

    def _select_existing_goals(
        self,
        existing_goals: List[Entity],
    ) -> Tuple[List[Entity], List[Entity]]:
        """Two-tier selection: recent/high-usage detailed, rest summary-only."""
        if not existing_goals:
            return [], []

        now = datetime.utcnow()

        def _score(g: Entity) -> Tuple[int, int]:
            meta = g.metadata_dict or {}
            usage = int(meta.get("usage_count", 0) or 0)
            last_ts_str = meta.get("last_synthesized_ts")
            recency_days = 999
            if last_ts_str:
                try:
                    last_ts = datetime.fromisoformat(str(last_ts_str).replace("Z", "+00:00")).replace(tzinfo=None)
                    recency_days = max(0, (now - last_ts).days)
                except Exception:
                    pass
            return (-recency_days, usage)

        sorted_goals = sorted(existing_goals, key=_score, reverse=True)
        detailed = sorted_goals[:15]
        summary_only = sorted_goals[15:]
        return detailed, summary_only

    def _goals_block_two_tier(
        self,
        detailed: Sequence[Entity],
        summary_only: Sequence[Entity] = (),
        *,
        goal_activity_map: Optional[Dict[int, List[str]]] = None,
    ) -> str:
        """Format existing goals in two tiers: full detail + title-only."""
        if not detailed and not summary_only:
            return "None"
        gam = goal_activity_map or {}
        lines = []
        if detailed:
            lines.append("### Primary goals (full detail):")
            for g in detailed:
                meta = g.metadata_dict or {}
                flags = []
                if meta.get("user_locked"):
                    flags.append("[locked]")
                if meta.get("user_edited"):
                    flags.append("[user-edited]")
                if meta.get("user_provided"):
                    flags.append("[user-provided]")
                flag_str = " ".join(flags)

                extra_parts = []
                usage = int(meta.get("usage_count", 0) or 0)
                extra_parts.append(f"used:{usage}x")
                conf = meta.get("goal_confidence_score")
                stab = meta.get("goal_stability_score")
                if conf is not None:
                    extra_parts.append(f"conf={conf}/10")
                if stab is not None:
                    extra_parts.append(f"stability={stab}/10")
                if meta.get("needs"):
                    extra_parts.append(f"needs:{','.join(meta['needs'][:4])}")
                if meta.get("orientation"):
                    extra_parts.append(f"orient:{meta['orientation']}")
                if meta.get("people"):
                    extra_parts.append(f"people:{','.join(str(p) for p in meta['people'][:5])}")
                if meta.get("domains"):
                    extra_parts.append(f"domains:{','.join(meta['domains'])}")
                act_labels = gam.get(g.id)
                if act_labels:
                    extra_parts.append(f"activities:[{'; '.join(act_labels)}]")
                extra_str = " | " + " | ".join(extra_parts) if extra_parts else ""

                lines.append(f"- ID:{g.id} | {g.text} {flag_str}{extra_str}".strip())

        if summary_only:
            lines.append("\n### Other known goals (title only — you may still match/merge with these):")
            for g in summary_only:
                lines.append(f"- ID:{g.id} | {g.text}")
        return "\n".join(lines)

    async def _goal_conflicts_block(self) -> str:
        """Build prompt block of known goal-goal conflicts."""
        try:
            goals = await self.store.entities.get_by_type(EntityType.GOAL)
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
                        conflict_lines.append(
                            f"- CONFLICT: \"{g.text[:60]}\" (ID:{g.id}) vs \"{target.text[:60]}\" (ID:{target.id})"
                            + (f" | evidence: {evidence}" if evidence else "")
                        )
        if not conflict_lines:
            return ""
        header = (
            "########################################\n"
            "# Known goal conflicts\n"
            "########################################\n"
            "These conflicts have been detected. Preserve tension — do NOT merge conflicting goals.\n\n"
        )
        return header + "\n".join(conflict_lines)

    async def _user_constraints_block(self) -> str:
        """Build prompt block of user constraints."""
        constraint_lines = []

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
                flags.append("[user-edited] — preserve exact label verbatim")
            for ann in meta.get("user_annotations", []):
                ann_type = ann.get("type", "note")
                ann_text = ann.get("text", "")[:80]
                flags.append(f"annotation ({ann_type}): \"{ann_text}\"")
            if flags:
                for flag in flags:
                    constraint_lines.append(f"- Goal ID:{g.id} | {g.text[:60]} | {flag}")

        try:
            activities = await self.store.entities.get_by_type(EntityType.ACTIVITY, limit=200)
        except Exception:
            activities = []
        for act in activities:
            meta = act.metadata_dict or {}
            flags = []
            if meta.get("user_locked"):
                flags.append("[locked] — do not reassign to a different goal")
            if meta.get("user_reassigned"):
                flags.append("[user-reassigned] — respect current goal assignment")
            if flags:
                for flag in flags:
                    constraint_lines.append(f"- Activity ID:{act.id} | {act.text[:60]} | {flag}")

        if not constraint_lines:
            return ""
        header = (
            "########################################\n"
            "# User constraints\n"
            "########################################\n"
            f"{self.user_name} has edited, locked, or annotated the following entities.\n"
            "Respect these constraints:\n"
            "- [locked] goals: Do NOT merge, delete, or substantially alter.\n"
            "- [user-edited] goals: Keep the exact label verbatim.\n"
            "- [user-reassigned] activities: Keep with their assigned goal.\n"
            "- Annotations provide privileged context — weight above behavioral inference.\n\n"
        )
        return header + "\n".join(constraint_lines)

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    def _parse_json(self, response: str) -> Optional[Dict[str, Any]]:
        try:
            data = parse_llm_json(response)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}
            return data
        except Exception as e:
            self.log.warning("goal_reconcile: json parse error: %s", e)
            return None

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

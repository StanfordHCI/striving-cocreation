"""GoalProposeJob: propose candidate life goals (unbiased).

The LLM sees activities (or raw observations) but NOT existing goals,
so it proposes fresh candidate strivings without anchoring to what
already exists.  The subsequent GoalReconcileJob integrates these
candidates with the existing goal repository.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from tempo.models import Entity, EntityType, RelationType, RelationSubtype
from tempo.prompts.goal_propose import GOAL_PROPOSE_PROMPT, GOAL_PROPOSE_OBSERVATION_PROMPT
from tempo.schemas import GoalProposeResult, GoalProposeCandidate, get_schema
from tempo.utils import get_debug_logger, parse_llm_json

GOAL_PROPOSE_FORMAT = get_schema(GoalProposeResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger
    from tempo.providers import ModelProvider
    from tempo.store import Store


class GoalProposeJob:
    """Propose candidate life goals from activities or observations."""

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
        changed_activity_ids: Optional[Sequence[int]] = None,
        *,
        observation_texts: Optional[List[str]] = None,
        new_observation_texts: Optional[List[str]] = None,
    ) -> Tuple[List[Entity], Dict[int, GoalProposeCandidate]]:
        """Propose candidate goals and write them to DB as provisional entities.

        Two modes:
          1. Activity mode (default): changed_activity_ids provided.
             Loads activities from DB, proposes goals from activities.
          2. Observation mode: observation_texts provided.
             Proposes goals directly from raw screen observations (flat condition).

        Returns:
            (candidate_entities, propose_by_db_id) — provisional goal entities
            written to DB, keyed by their DB IDs.  Pass these to
            GoalReconcileJob.run() for integration.
        """
        observation_mode = observation_texts is not None

        if observation_mode:
            if not observation_texts and not new_observation_texts:
                return [], {}
        else:
            if not changed_activity_ids:
                return [], {}

            all_activities = await self.store.entities.get_by_type(EntityType.ACTIVITY)
            activities_by_id = {a.id: a for a in all_activities}
            changed_id_set = {
                aid for aid in changed_activity_ids
                if aid in activities_by_id
            }

            if not changed_id_set:
                self.log.debug("goal_propose: no changed activities found")
                return [], {}

        # Load existing goals to expand changed set (activity mode only)
        existing_goals = await self.store.get_goals()

        if not observation_mode and existing_goals:
            # Expand changed set: find existing goals linked to changed
            # activities, then include ALL sibling activities under those
            # goals.  This ensures the propose step sees the full evidence
            # base of goals whose activities were just touched by activity
            # reconcile (merged / revised / deleted).
            affected_goal_ids: set = set()
            for aid in list(changed_id_set):
                rels = await self.store.relations.get_by_source(
                    aid,
                    relation_type=RelationType.STRUCTURAL,
                    relation_subtype=RelationSubtype.PART_OF,
                )
                for rel in (rels or []):
                    if rel.target_id in {g.id for g in existing_goals}:
                        affected_goal_ids.add(rel.target_id)

            if affected_goal_ids:
                for gid in affected_goal_ids:
                    sibling_rels = await self.store.relations.get_by_target(
                        gid,
                        relation_type=RelationType.STRUCTURAL,
                        relation_subtype=RelationSubtype.PART_OF,
                    )
                    for rel in (sibling_rels or []):
                        if rel.source_id in activities_by_id:
                            changed_id_set.add(rel.source_id)

                self.log.debug(
                    "goal_propose: expanded changed set via %d affected goals → %d activities",
                    len(affected_goal_ids), len(changed_id_set),
                )

        # Build user constraints
        user_constraints_block = await self._user_constraints_block()

        # PROPOSE: generate candidate goals (unbiased — no existing goals visible)
        if observation_mode:
            candidates = await self._propose_goals_from_observations(
                new_observation_texts or observation_texts,
                observation_texts,
                user_constraints_block,
            )
        else:
            changed_activities = [activities_by_id[aid] for aid in changed_id_set]
            candidates = await self._propose_goals(
                changed_activities, all_activities, user_constraints_block,
            )
        if not candidates:
            self.log.debug("goal_propose: no candidates generated")
            return [], {}

        self.log.debug("goal_propose: proposed %d candidates", len(candidates))

        # Write candidates to DB as provisional goal entities.
        # This gives them real DB IDs so the reconcile LLM only works with
        # integer IDs, avoiding the candidate-ID-as-string bug in relations.
        candidate_entities = []
        propose_by_db_id: Dict[int, GoalProposeCandidate] = {}
        for cand in candidates:
            entity = await self._write_candidate_to_db(cand)
            candidate_entities.append(entity)
            propose_by_db_id[entity.id] = cand

        self.log.debug(
            "goal_propose: wrote %d candidates to DB (ids=%s)",
            len(candidate_entities),
            [e.id for e in candidate_entities],
        )

        return candidate_entities, propose_by_db_id

    # ------------------------------------------------------------------
    # Propose from activities (hierarchical condition)
    # ------------------------------------------------------------------

    async def _propose_goals(
        self,
        changed_activities: List[Entity],
        all_activities: List[Entity],
        user_constraints_block: str,
    ) -> List[GoalProposeCandidate]:
        changed_block = self._activities_block(changed_activities)
        all_block = self._activities_block(all_activities)

        user_context_block = ""
        if self.user_context:
            user_context_block = (
                "########################################\n"
                "# User context\n"
                "########################################\n"
                f"{self.user_context}\n"
            )

        prompt = self._render_prompt(
            GOAL_PROPOSE_PROMPT,
            user_name=self.user_name,
            changed_activities=changed_block,
            all_activities=all_block,
            user_constraints=user_constraints_block,
            user_context=user_context_block,
        )

        self.log.debug(
            "goal_propose: changed=%d all=%d prompt_len=%d",
            len(changed_activities), len(all_activities), len(prompt),
        )

        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=GOAL_PROPOSE_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="goal_propose", stage="propose",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )

        return self._parse_propose_response(response)

    # ------------------------------------------------------------------
    # Propose from observations (flat condition)
    # ------------------------------------------------------------------

    async def _propose_goals_from_observations(
        self,
        new_observations: List[str],
        all_observations: List[str],
        user_constraints_block: str,
    ) -> List[GoalProposeCandidate]:
        """Propose goals from raw screen observations (no Activity Theory hierarchy)."""
        new_block = self._observations_block(new_observations)
        all_block = self._observations_block(all_observations)

        user_context_block = ""
        if self.user_context:
            user_context_block = (
                "########################################\n"
                "# User context\n"
                "########################################\n"
                f"{self.user_context}\n"
            )

        prompt = self._render_prompt(
            GOAL_PROPOSE_OBSERVATION_PROMPT,
            user_name=self.user_name,
            new_observations=new_block,
            all_observations=all_block,
            user_constraints=user_constraints_block,
            user_context=user_context_block,
        )

        self.log.debug(
            "goal_propose_obs: new=%d all=%d prompt_len=%d",
            len(new_observations), len(all_observations), len(prompt),
        )

        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=GOAL_PROPOSE_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="goal_propose", stage="propose_obs",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )

        return self._parse_propose_response(response)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_propose_response(self, response: str) -> List[GoalProposeCandidate]:
        try:
            data = self._parse_json(response)
            if not data:
                return []
            candidates_raw = data.get("candidates", [])
            if not isinstance(candidates_raw, list):
                return []
            candidates = []
            for c in candidates_raw:
                if not isinstance(c, dict):
                    continue
                candidates.append(GoalProposeCandidate(
                    candidate_id=c.get("candidate_id"),
                    text=c.get("text", ""),
                    needs=c.get("needs"),
                    orientation=c.get("orientation"),
                    aspiration_vs_obligation=c.get("aspiration_vs_obligation"),
                    autonomy=c.get("autonomy"),
                    identity_link=c.get("identity_link"),
                    feared_self=c.get("feared_self"),
                    activity_ids=c.get("activity_ids", []),
                    reasoning=c.get("reasoning"),
                    synthesis_evidence=c.get("synthesis_evidence"),
                    confidence=c.get("confidence"),
                    evidence_summary=c.get("evidence_summary"),
                    people=c.get("people"),
                    domains=c.get("domains"),
                    engagement_signature=c.get("engagement_signature"),
                    initiation_signature=c.get("initiation_signature"),
                    temporal_context=c.get("temporal_context"),
                ))
            return candidates
        except Exception as e:
            self.log.warning("goal_propose: parse error (%s)", e)
            return []

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _write_candidate_to_db(self, candidate: GoalProposeCandidate) -> Entity:
        """Write a proposed candidate to DB as a provisional goal entity."""
        metadata: Dict[str, Any] = {
            "status": "candidate",
            "source": "system_inferred",
            "user_edited": False,
            "user_provided": False,
            "last_synthesized_ts": datetime.utcnow().isoformat(),
            "usage_count": len(candidate.activity_ids),
        }
        if candidate.needs:
            metadata["needs"] = candidate.needs
        if candidate.orientation:
            metadata["orientation"] = candidate.orientation
        if candidate.aspiration_vs_obligation:
            metadata["aspiration_vs_obligation"] = candidate.aspiration_vs_obligation
        if candidate.autonomy:
            metadata["autonomy"] = candidate.autonomy
        if candidate.identity_link:
            metadata["identity_link"] = candidate.identity_link
        if candidate.feared_self:
            metadata["feared_self"] = candidate.feared_self
        if candidate.evidence_summary:
            metadata["evidence_summary"] = candidate.evidence_summary
        if candidate.people:
            metadata["people"] = candidate.people
        if candidate.domains:
            metadata["domains"] = candidate.domains
        if candidate.engagement_signature:
            metadata["engagement_signature"] = candidate.engagement_signature
        if candidate.initiation_signature:
            metadata["initiation_signature"] = candidate.initiation_signature
        if candidate.temporal_context:
            metadata["temporal_context"] = candidate.temporal_context
        if candidate.confidence is not None:
            metadata["goal_confidence"] = candidate.confidence

        goal = await self.store.create_goal(
            text=candidate.text,
            activity_ids=candidate.activity_ids,
            metadata=metadata,
        )
        return goal

    # ------------------------------------------------------------------
    # Prompt formatting helpers
    # ------------------------------------------------------------------

    def _activities_block(self, activities: Sequence[Entity]) -> str:
        """Format activities for the prompt."""
        if not activities:
            return "None"
        lines = []
        for act in activities:
            meta = act.metadata_dict or {}
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
            purpose = meta.get("purpose")
            identity_context = meta.get("identity_context")
            people = meta.get("people")
            engagement_profile = meta.get("engagement_profile")
            initiation_profile = meta.get("initiation_profile")
            summary = meta.get("evidence_summary", "")
            if purpose:
                extra_parts.append(f"purpose:{purpose[:80]}")
            if identity_context:
                extra_parts.append(f"domain:{identity_context}")
            if people:
                extra_parts.append(f"people:{','.join(str(p) for p in people[:5])}")
            if engagement_profile:
                extra_parts.append(f"engage:{engagement_profile}")
            if initiation_profile:
                extra_parts.append(f"init:{initiation_profile}")
            if summary:
                extra_parts.append(f"summary:{summary[:120]}")
            extra_str = " | " + " | ".join(extra_parts) if extra_parts else ""

            lines.append(
                f"- ID:{act.id} | {act.text} | status:{status} | used:{usage}x | last:{last}{extra_str}"
            )
        return "\n".join(lines)

    def _observations_block(self, observation_texts: List[str]) -> str:
        """Format raw observation texts for the prompt."""
        if not observation_texts:
            return "None"
        lines = []
        for i, text in enumerate(observation_texts):
            truncated = text[:500] if len(text) > 500 else text
            lines.append(f"- [{i+1}] {truncated}")
        return "\n".join(lines)

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
            self.log.warning("goal_propose: json parse error: %s", e)
            return None

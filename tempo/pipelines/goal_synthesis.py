"""
GoalSynthesisJob: synthesize life goals from activities.

Sees ALL activities at once and groups them into 5-15 broad life goals.
Optionally runs a self-refine pass for higher quality.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from tempo.models import EntityType, RelationType, RelationSubtype
from tempo.prompts.goal_synthesis import (
    SYNTHESIZE_PROMPT,
    SYNTHESIZE_OBSERVATION_PROMPT,
    SELF_REFINE_PROMPT,
    SELF_REFINE_OBSERVATION_PROMPT,
    CONTRAST_SECTION,
    CONTRAST_OUTPUT_SCHEMA,
)
from tempo.schemas import GoalSynthesisResult, GoalItem, GoalRelation, DroppedGoal, ContrastAnalysis, ContrastEntry, NeedLandscape, get_schema
from tempo.utils import get_debug_logger

GOAL_SYNTHESIS_FORMAT = get_schema(GoalSynthesisResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger
    from tempo.providers import ModelProvider
    from tempo.store import Store


class GoalSynthesisJob:
    """Synthesize life goals from activities."""

    def __init__(
        self,
        provider: "ModelProvider",
        store: "Store",
        *,
        user_name: Optional[str] = None,
        debug: bool = False,
        enable_self_refine: bool = True,
        mode: str = "full",
        user_context: str = "",
        debug_logger: Optional["DebugLogger"] = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.user_name = user_name or os.getenv("USER_NAME", "the user")
        self.debug = debug
        self.enable_self_refine = enable_self_refine
        self.mode = mode  # "full" (offline/experiment) or "incremental" (online)
        self.log = get_debug_logger(self, debug=debug)
        self.user_context = user_context
        self.debug_logger = debug_logger

    @staticmethod
    def _render_prompt(template: str, **values: Any) -> str:
        rendered = template
        for key, val in values.items():
            rendered = rendered.replace("{" + key + "}", str(val))
        return rendered.replace("{{", "{").replace("}}", "}")

    async def run(
        self,
        activity_cutoff: Optional[datetime] = None,
        observation_texts: Optional[List[str]] = None,
        new_observations: Optional[List[str]] = None,
        context_observations: Optional[List[str]] = None,
        observation_screenshot_paths: Optional[List[str]] = None,
        context_screenshot_paths: Optional[List[str]] = None,
        new_screenshot_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Run goal synthesis.

        Args:
            activity_cutoff: If set, only include activities whose timestamp_start
                is on or before this time. Used for periodic synthesis to simulate
                "what goals existed at this point in time."
            observation_texts: If set, run in observation mode — synthesize goals
                directly from raw observations (transcription texts) instead of
                activities. Used by the flat ablation condition.
            new_observations: The new batch of observations to make decisions on.
                If provided alongside context_observations, the prompt splits into
                context (rolling window) vs new (current batch).
            context_observations: Previously processed observations for context.
                Acts as the FC equivalent of "all activities" — compressed history
                the LLM can reference but doesn't re-decide on.

        Returns:
            Dict with created/updated/total counts and optional contrast.
        """
        # Determine observation mode: either split (new + context) or legacy (single list)
        # Compute combined screenshot paths from split lists (if provided)
        if context_screenshot_paths is not None or new_screenshot_paths is not None:
            observation_screenshot_paths = list(dict.fromkeys(
                (context_screenshot_paths or []) + (new_screenshot_paths or [])
            ))
        if new_observations is not None:
            observation_mode = True
            all_obs = (context_observations or []) + new_observations
            observation_texts = all_obs  # for compat
        else:
            observation_mode = observation_texts is not None

        if observation_mode:
            # Flat condition: raw observations instead of activities
            activities = []
            if not observation_texts and not new_observations:
                self.log.debug("goal_synthesis: empty observation_texts")
                return {"created": 0, "updated": 0, "total": 0}
            if new_observations is not None:
                activities_block = self._split_observations_block(
                    context_observations or [], new_observations,
                )
            else:
                activities_block = self._observations_block(observation_texts)
        else:
            # Hierarchical condition: load activities from DB
            activities = await self.store.entities.get_by_type(
                EntityType.ACTIVITY, until=activity_cutoff
            )
            if not activities:
                self.log.debug("goal_synthesis: no activities found")
                return {"created": 0, "updated": 0, "total": 0}
            activities_block = self._activities_block(activities)

        # 2. Load existing goals
        existing_goals = await self.store.get_goals()

        # 3. Load user-provided goals
        user_provided_goals = await self.store.get_user_provided_goals()


        # 5. Build existing goals block
        existing_goals_block = self._goals_block(existing_goals)

        # 6. Build user goals section + contrast section
        user_goals_section = ""
        contrast_section = ""
        contrast_output_schema = ""
        if user_provided_goals:
            user_stated_goals = "\n".join(
                f"- ID:{g.id} | {g.text}" for g in user_provided_goals
            )
            user_goals_section = (
                "########################################\n"
                f"# {self.user_name}'s self-described goals (strong priors)\n"
                "########################################\n"
                f"{user_stated_goals}"
            )
            contrast_section = CONTRAST_SECTION.replace(
                "{user_name}", self.user_name
            ).replace("{user_stated_goals}", user_stated_goals)
            contrast_output_schema = CONTRAST_OUTPUT_SCHEMA

        # 7. Build user constraints block
        user_constraints_block = await self._user_constraints_block()

        # 8. Build and call LLM
        synth_template = SYNTHESIZE_OBSERVATION_PROMPT if observation_mode else SYNTHESIZE_PROMPT
        prompt = self._render_prompt(
            synth_template,
            user_name=self.user_name,
            activities=activities_block,
            existing_goals=existing_goals_block,
            user_goals_section=user_goals_section,
            user_constraints=user_constraints_block,
            contrast_section=contrast_section,
            contrast_output_schema=contrast_output_schema,
            user_context=self.user_context,
        )

        self.log.debug(
            "goal_synthesis: activities=%d existing_goals=%d user_goals=%d prompt_len=%d",
            len(activities),
            len(existing_goals),
            len(user_provided_goals),
            len(prompt),
        )

        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=GOAL_SYNTHESIS_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="goal_synthesis", stage="synthesize",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )

        # 8. Parse response (retry once on failure)
        result = self._parse_response(response)
        if not result or not result.goals:
            self.log.warning("goal_synthesis: parse failed or empty, retrying")
            t0 = time.monotonic()
            response = await self.provider.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format=GOAL_SYNTHESIS_FORMAT,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            if self.debug_logger:
                self.debug_logger.log_llm_call(
                    pipeline="goal_synthesis", stage="synthesize_retry",
                    model=self.provider.model,
                    prompt=prompt, response=response,
                    latency_ms=latency_ms, response_format="json_schema",
                )
            result = self._parse_response(response)
            if not result or not result.goals:
                self.log.error("goal_synthesis: parse failed after retry")
                return {"created": 0, "updated": 0, "total": 0}

        # 9. Compute need saturation from initial synthesis (for self-refine context)
        need_saturation = self._compute_need_saturation(result.goals, activities)
        need_saturation_ctx = self._format_need_saturation_context(need_saturation)

        # 10. Optional self-refine (with need saturation context)
        if self.enable_self_refine and result.goals:
            self.log.debug("goal_synthesis: running self-refine")
            refined = await self._self_refine(
                result, activities_block, user_goals_section,
                need_saturation_context=need_saturation_ctx,
                observation_mode=observation_mode,
            )
            if refined and refined.goals:
                result = refined

        # 11. Persist goals (with goal-goal relations)
        created, updated, idx_to_goal_id = await self._persist_goals(
            result.goals, existing_goals, activities,
            goal_relations=result.goal_relations,
            observation_mode=observation_mode,
            dropped_goals=result.dropped_goals,
            observation_screenshot_paths=observation_screenshot_paths,
            context_screenshot_paths=context_screenshot_paths,
            new_screenshot_paths=new_screenshot_paths,
        )

        # 12a. Materialize screenshot paths on each goal's metadata.
        # Hierarchical: traverse goal → activities → actions → operations.
        # Observation mode: already handled via observation_screenshot_paths in _persist_goals.
        if not observation_mode:
            await self._materialize_goal_screenshots(idx_to_goal_id)

        # 12. Compute and persist multifinality scores on activities
        # (skip in observation mode — no activity entities exist)
        if not observation_mode:
            await self._compute_and_persist_multifinality(result.goals, activities)

        # 13. Persist need_landscape on goals metadata (if present)
        if result.need_landscape:
            await self._persist_need_landscape(result.need_landscape, need_saturation)

        summary = {
            "created": created,
            "updated": updated,
            "total": len(result.goals),
        }

        # 14. Store contrast if present
        if result.contrast:
            contrast_data = {
                "supported": [e.model_dump() for e in result.contrast.supported],
                "unsupported": [e.model_dump() for e in result.contrast.unsupported],
                "unstated": [e.model_dump() for e in result.contrast.unstated],
            }
            summary["contrast"] = {
                "supported": len(result.contrast.supported),
                "unsupported": len(result.contrast.unsupported),
                "unstated": len(result.contrast.unstated),
            }
            # Persist full contrast on all user-provided goals metadata
            for g in (await self.store.get_user_provided_goals()):
                meta = g.metadata_dict.copy() if g.metadata_dict else {}
                meta["contrast"] = contrast_data
                await self.store.entities.update(g.id, metadata=meta)

        self.log.debug("goal_synthesis: done %s", summary)
        return summary

    def _activities_block(self, activities) -> str:
        lines = []
        for act in activities:
            meta = act.metadata_dict or {}
            status = meta.get("status", "active")
            usage = meta.get("usage_count", 0)
            last = meta.get("last_assigned_ts", "never")
            confidence = meta.get("activity_confidence")
            stability = meta.get("activity_stability")
            summary = meta.get("evidence_summary", "")

            extra_parts = []
            if confidence is not None:
                extra_parts.append(f"conf={confidence}/10")
            if stability is not None:
                extra_parts.append(f"stability={stability}/10")
            # Working sphere metadata (Phase 2)
            purpose = meta.get("purpose")
            identity_context = meta.get("identity_context")
            people = meta.get("people")
            engagement_profile = meta.get("engagement_profile")
            initiation_profile = meta.get("initiation_profile")
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
            # User annotations on activities
            annotations = meta.get("user_annotations", [])
            if annotations:
                ann_strs = [f"{a.get('type','note')}:\"{a.get('text','')[:60]}\"" for a in annotations[-3:]]
                extra_parts.append(f"user_feedback:[{'; '.join(ann_strs)}]")
            if meta.get("user_reassigned"):
                extra_parts.append("user_reassigned")
            extra_str = " | " + " | ".join(extra_parts) if extra_parts else ""

            lines.append(
                f"- ID:{act.id} | {act.text} | status:{status} | used:{usage}x | last:{last}{extra_str}"
            )
        return "\n".join(lines) if lines else "None"

    def _observations_block(self, observation_texts: List[str]) -> str:
        """Format raw observation texts for the prompt (observation mode)."""
        lines = []
        for i, text in enumerate(observation_texts):
            # Truncate very long observations to keep prompt manageable
            # truncated = text[:500] if len(text) > 500 else text
            lines.append(f"- [{i+1}] {text}")
        return "\n".join(lines)

    def _split_observations_block(
        self, context_observations: List[str], new_observations: List[str],
    ) -> str:
        """Format observations split into context (rolling window) and new (current batch).

        The context observations serve as compressed history (analogous to
        'all activities' in the hierarchical condition). The new observations
        are what the LLM should make goal decisions based on.
        """
        lines = []

        if context_observations:
            lines.append("## Previous observations (context — for pattern recognition)")
            lines.append("These are previously processed observations. Use them to understand")
            lines.append("behavioral patterns and continuity, but do NOT re-decide on them.")
            lines.append("")
            for i, text in enumerate(context_observations):
                # truncated = text[:500] if len(text) > 500 else text
                lines.append(f"- [ctx-{i+1}] {text}")
            lines.append("")

        lines.append("## New observations (make decisions based on these)")
        lines.append("These are new observations since the last synthesis. Update, revise,")
        lines.append("or create goals based on what these reveal about the user's pursuits.")
        lines.append("")
        for i, text in enumerate(new_observations):
            # truncated = text[:500] if len(text) > 500 else text
            lines.append(f"- [new-{i+1}] {text}")

        return "\n".join(lines)

    def _goals_block(self, goals) -> str:
        if not goals:
            return "None"
        lines = []
        for g in goals:
            meta = g.metadata_dict or {}
            flags = []
            if meta.get("user_locked"):
                flags.append("[locked]")
            if meta.get("user_edited"):
                flags.append("[user-edited]")
            if meta.get("user_provided"):
                flags.append("[user-provided]")
            # Include user annotations as flags
            annotations = meta.get("user_annotations", [])
            for ann in annotations[-2:]:
                ann_type = ann.get("type", "note")
                ann_text = ann.get("text", "")[:50]
                flags.append(f"[user-{ann_type}: \"{ann_text}\"]")
            flag_str = " ".join(flags)

            # Aggregated metadata (Phase 3b)
            extra_parts = []
            if meta.get("people"):
                extra_parts.append(f"people:{','.join(str(p) for p in meta['people'][:5])}")
            if meta.get("domains"):
                extra_parts.append(f"domains:{','.join(meta['domains'])}")
            if meta.get("engagement_signature"):
                extra_parts.append(f"engage:{meta['engagement_signature'][:60]}")
            if meta.get("initiation_signature"):
                extra_parts.append(f"init:{meta['initiation_signature'][:60]}")
            if meta.get("temporal_context"):
                extra_parts.append(f"temporal:{meta['temporal_context'][:60]}")
            extra_str = " | " + " | ".join(extra_parts) if extra_parts else ""

            lines.append(f"- ID:{g.id} | {g.text} {flag_str}{extra_str}".strip())
        return "\n".join(lines)

    async def _user_constraints_block(self) -> str:
        """Build a prompt block of user constraints for goal synthesis."""
        constraint_lines = []

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
                flags.append("[user-edited] — preserve exact label verbatim")
            for ann in meta.get("user_annotations", []):
                ann_type = ann.get("type", "note")
                ann_text = ann.get("text", "")[:80]
                flags.append(f"annotation ({ann_type}): \"{ann_text}\"")
            if flags:
                for flag in flags:
                    constraint_lines.append(f"- Goal ID:{g.id} | {g.text[:60]} | {flag}")

        # Check activities for locked/reassigned status
        try:
            activities = await self.store.entities.get_by_type(EntityType.ACTIVITY)
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
            f"# User constraints\n"
            "########################################\n"
            f"{self.user_name} has edited, locked, or annotated the following entities.\n"
            "Respect these constraints:\n"
            "- [locked] goals: Do NOT merge, delete, or substantially alter.\n"
            "- [user-edited] goals: Keep the exact label verbatim.\n"
            "- [user-reassigned] activities: Keep with their assigned goal.\n"
            "- Annotations provide privileged context — weight above behavioral inference.\n\n"
        )
        return header + "\n".join(constraint_lines)

    def _parse_response(self, response: str) -> Optional[GoalSynthesisResult]:
        try:
            from tempo.utils import parse_llm_json
            data = parse_llm_json(response)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}
            goals = []
            for g in data.get("goals", []):
                goals.append(GoalItem(
                    text=g.get("text", ""),
                    activity_ids=g.get("activity_ids", []),
                    reasoning=g.get("reasoning"),
                    confidence=g.get("confidence"),
                    matches_goal_id=g.get("matches_goal_id"),
                    evidence_summary=g.get("evidence_summary"),
                    # Psychological dimensions (Phase 3)
                    needs=g.get("needs"),
                    orientation=g.get("orientation"),
                    aspiration_vs_obligation=g.get("aspiration_vs_obligation"),
                    autonomy=g.get("autonomy"),
                    identity_link=g.get("identity_link"),
                    feared_self=g.get("feared_self"),
                    # Aggregated activity metadata (Phase 3b)
                    people=g.get("people"),
                    domains=g.get("domains"),
                    engagement_signature=g.get("engagement_signature"),
                    initiation_signature=g.get("initiation_signature"),
                    temporal_context=g.get("temporal_context"),
                ))

            # Parse goal-goal relations
            goal_relations = []
            for r in data.get("goal_relations", []):
                try:
                    goal_relations.append(GoalRelation(
                        goal_a_idx=r.get("goal_a_idx", 0),
                        goal_b_idx=r.get("goal_b_idx", 0),
                        relation=r.get("relation", "conflict"),
                        evidence=r.get("evidence"),
                    ))
                except Exception:
                    continue

            contrast = None
            if "contrast" in data and data["contrast"]:
                c = data["contrast"]
                # Safety net: Gemini may return strings for list fields
                def _coerce_list(val):
                    if isinstance(val, str):
                        try:
                            val = json.loads(val)
                        except (json.JSONDecodeError, ValueError):
                            val = []
                    return val if isinstance(val, list) else []
                contrast = ContrastAnalysis(
                    supported=[ContrastEntry(**e) for e in _coerce_list(c.get("supported", []))],
                    unsupported=[ContrastEntry(**e) for e in _coerce_list(c.get("unsupported", []))],
                    unstated=[ContrastEntry(**e) for e in _coerce_list(c.get("unstated", []))],
                )

            # Parse need_landscape if present (from self-refine)
            need_landscape = None
            if "need_landscape" in data and data["need_landscape"]:
                nl = data["need_landscape"]
                try:
                    # Safety net: Gemini may return strings for list-of-dict fields
                    def _coerce_list_of_dicts(val):
                        if isinstance(val, str):
                            try:
                                val = json.loads(val)
                            except (json.JSONDecodeError, ValueError):
                                val = []
                        return val if isinstance(val, list) else []
                    need_landscape = NeedLandscape(
                        congruence_insights=_coerce_list_of_dicts(nl.get("congruence_insights", [])),
                        incongruence_insights=_coerce_list_of_dicts(nl.get("incongruence_insights", [])),
                        leverage_points=_coerce_list_of_dicts(nl.get("leverage_points", [])),
                    )
                except Exception:
                    pass  # Non-critical — don't fail the whole parse

            return GoalSynthesisResult(
                goals=goals,
                goal_relations=goal_relations,
                contrast=contrast,
                need_landscape=need_landscape,
            )
        except Exception as e:
            self.log.warning("goal_synthesis: parse error (%s)", e)
            return None

    async def _self_refine(
        self, result: GoalSynthesisResult, activities_block: str,
        user_goals_section: str = "",
        need_saturation_context: str = "",
        observation_mode: bool = False,
    ) -> Optional[GoalSynthesisResult]:
        prev_data = {"goals": [g.model_dump() for g in result.goals]}
        if result.goal_relations:
            prev_data["goal_relations"] = [r.model_dump() for r in result.goal_relations]
        previous_output = json.dumps(prev_data, indent=2)
        refine_template = SELF_REFINE_OBSERVATION_PROMPT if observation_mode else SELF_REFINE_PROMPT
        prompt = self._render_prompt(
            refine_template,
            user_name=self.user_name,
            previous_output=previous_output,
            activities=activities_block,
            user_goals_section=user_goals_section,
            need_saturation_context=need_saturation_context,
            user_context=self.user_context,
        )

        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=GOAL_SYNTHESIS_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="goal_synthesis", stage="self_refine",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )

        refined = self._parse_response(response)
        if refined and refined.goals:
            # Safety net: recover activity_ids if self-refine dropped them.
            # Build lookup from original goals by matches_goal_id and by index.
            orig_aids_by_id: Dict[Optional[int], List[int]] = {}
            for g in result.goals:
                if g.matches_goal_id is not None:
                    orig_aids_by_id[g.matches_goal_id] = g.activity_ids
            orig_aids_by_idx = {i: g.activity_ids for i, g in enumerate(result.goals)}

            orig_total = set()
            for g in result.goals:
                orig_total.update(g.activity_ids)
            refined_total = set()
            for g in refined.goals:
                refined_total.update(g.activity_ids)

            if orig_total and not refined_total:
                # Self-refine dropped ALL activity_ids — recover from originals
                self.log.warning(
                    "self_refine: LLM dropped all activity_ids (%d total), recovering from original",
                    len(orig_total),
                )
                for rg in refined.goals:
                    # Match by matches_goal_id first
                    if rg.matches_goal_id is not None and rg.matches_goal_id in orig_aids_by_id:
                        rg.activity_ids = orig_aids_by_id[rg.matches_goal_id]
                    elif len(refined.goals) == len(result.goals):
                        # Same count — match by position
                        idx = refined.goals.index(rg)
                        rg.activity_ids = orig_aids_by_idx.get(idx, [])

                # Any activity_ids still unassigned? Distribute to first goal.
                recovered = set()
                for rg in refined.goals:
                    recovered.update(rg.activity_ids)
                orphaned = orig_total - recovered
                if orphaned and refined.goals:
                    refined.goals[0].activity_ids = list(set(refined.goals[0].activity_ids) | orphaned)

            # Safety net: recover people/domains from original if self-refine dropped them
            for rg in refined.goals:
                orig_match = None
                if rg.matches_goal_id is not None:
                    for og in result.goals:
                        if og.matches_goal_id == rg.matches_goal_id:
                            orig_match = og
                            break
                if not rg.people and orig_match and orig_match.people:
                    rg.people = orig_match.people
                if not rg.domains and orig_match and orig_match.domains:
                    rg.domains = orig_match.domains
                if not rg.engagement_signature and orig_match and orig_match.engagement_signature:
                    rg.engagement_signature = orig_match.engagement_signature
                if not rg.initiation_signature and orig_match and orig_match.initiation_signature:
                    rg.initiation_signature = orig_match.initiation_signature
                if not rg.temporal_context and orig_match and orig_match.temporal_context:
                    rg.temporal_context = orig_match.temporal_context

            # Preserve contrast from original if refine didn't include it
            if result.contrast and not refined.contrast:
                refined.contrast = result.contrast
            return refined
        return None

    # ── Need Taxonomy (must match SYNTHESIZE_PROMPT) ──
    NEED_TAXONOMY = [
        "competence", "autonomy", "relatedness", "status_recognition",
        "self_coherence", "understanding", "order_predictability",
        "nurturance", "safety_security", "growth", "stimulation",
        "purpose_meaning",
    ]

    def _compute_need_saturation(
        self,
        goal_items: List[GoalItem],
        activities: list,
    ) -> Dict[str, Any]:
        """Compute per-need saturation scores from goals + their activities.

        Pure Python — no LLM call.  Returns a dict::

            {
              "per_need": {
                "competence": {
                  "goal_count": 2,
                  "activity_count": 5,
                  "avg_confidence": 7.0,
                  "engagement_modes": ["deep_focus", "routine"],
                  "initiation_modes": ["self_initiated", "habitual"],
                  "saturation": "high"  # high / moderate / low / absent
                },
                ...
              },
              "starved": ["safety_security"],
              "saturated": ["competence", "autonomy"],
            }
        """
        activities_by_id = {a.id: a for a in activities}

        # Collect per-need evidence
        per_need: Dict[str, Dict[str, Any]] = {}
        for need in self.NEED_TAXONOMY:
            per_need[need] = {
                "goal_count": 0,
                "activity_ids": set(),
                "confidences": [],
                "engagement_modes": [],
                "initiation_modes": [],
            }

        for item in goal_items:
            if not item.needs:
                continue
            for need in item.needs:
                if need not in per_need:
                    continue
                bucket = per_need[need]
                bucket["goal_count"] += 1
                bucket["confidences"].append(item.confidence or 5)
                # Collect constituent activities
                for aid in item.activity_ids:
                    bucket["activity_ids"].add(aid)
                    act = activities_by_id.get(aid)
                    if act:
                        meta = act.metadata_dict or {}
                        eng = meta.get("engagement_profile")
                        ini = meta.get("initiation_profile")
                        if eng and eng not in bucket["engagement_modes"]:
                            bucket["engagement_modes"].append(eng)
                        if ini and ini not in bucket["initiation_modes"]:
                            bucket["initiation_modes"].append(ini)

        # Compute saturation levels
        result: Dict[str, Any] = {"per_need": {}, "starved": [], "saturated": []}
        for need, bucket in per_need.items():
            activity_count = len(bucket["activity_ids"])
            goal_count = bucket["goal_count"]
            confs = bucket["confidences"]
            avg_conf = sum(confs) / len(confs) if confs else 0.0

            # Saturation heuristic
            if goal_count == 0:
                saturation = "absent"
            elif goal_count == 1 and activity_count <= 1:
                saturation = "low"
            elif goal_count >= 2 or activity_count >= 4:
                saturation = "high"
            else:
                saturation = "moderate"

            result["per_need"][need] = {
                "goal_count": goal_count,
                "activity_count": activity_count,
                "avg_confidence": round(avg_conf, 1),
                "engagement_modes": bucket["engagement_modes"][:4],
                "initiation_modes": bucket["initiation_modes"][:4],
                "saturation": saturation,
            }

            if saturation in ("absent", "low"):
                result["starved"].append(need)
            elif saturation == "high":
                result["saturated"].append(need)

        return result

    def _format_need_saturation_context(self, saturation: Dict[str, Any]) -> str:
        """Format computed need saturation as a prompt context block for self-refine."""
        if not saturation or not saturation.get("per_need"):
            return ""

        lines = [
            "########################################",
            "# Computed Need Saturation (from behavioral data)",
            "########################################",
            "The following need saturation was computed from goal-activity mappings.",
            "Use this to identify motive congruence/incongruence and produce a need_landscape.",
            "",
        ]

        per_need = saturation["per_need"]
        # Show each need with its metrics
        for need, data in per_need.items():
            sat = data["saturation"]
            gc = data["goal_count"]
            ac = data["activity_count"]
            conf = data["avg_confidence"]
            marker = ""
            if sat == "absent":
                marker = " ⚠ ABSENT"
            elif sat == "low":
                marker = " ⚠ LOW"
            elif sat == "high":
                marker = " ✓ HIGH"
            lines.append(
                f"- {need}: goals={gc} activities={ac} conf={conf}{marker}"
            )

        starved = saturation.get("starved", [])
        saturated = saturation.get("saturated", [])
        if starved:
            lines.append(f"\nStarved needs (absent/low): {', '.join(starved)}")
            lines.append(
                "Consider: Are these needs genuinely unmet, or is the system "
                "not capturing activities that serve them?"
            )
        if saturated:
            lines.append(f"Saturated needs (high): {', '.join(saturated)}")

        return "\n".join(lines)

    def _store_psych_dimensions(self, meta: dict, item: GoalItem) -> dict:
        """Store psychological dimension fields from GoalItem into metadata."""
        if item.needs:
            meta["needs"] = item.needs
        if item.orientation:
            meta["orientation"] = item.orientation
        if item.aspiration_vs_obligation:
            meta["aspiration_vs_obligation"] = item.aspiration_vs_obligation
        if item.autonomy:
            meta["autonomy"] = item.autonomy
        if item.identity_link:
            meta["identity_link"] = item.identity_link
        if item.feared_self:
            meta["feared_self"] = item.feared_self
        # Aggregated activity metadata (Phase 3b)
        if item.people:
            meta["people"] = item.people
        if item.domains:
            meta["domains"] = item.domains
        if item.engagement_signature:
            meta["engagement_signature"] = item.engagement_signature
        if item.initiation_signature:
            meta["initiation_signature"] = item.initiation_signature
        if item.temporal_context:
            meta["temporal_context"] = item.temporal_context
        return meta

    async def _compute_and_persist_multifinality(
        self,
        goal_items: List[GoalItem],
        activities: list,
    ) -> None:
        """Compute multifinality score per activity and persist on metadata.

        Multifinality (Kruglanski) = one activity serving multiple goals/needs.
        Score = goal_count * need_diversity.  Higher = more leverage.
        """
        activities_by_id = {a.id: a for a in activities}

        # Build activity → {goal_indices, needs_served}
        activity_goals: Dict[int, Dict[str, Any]] = {}
        for idx, item in enumerate(goal_items):
            for aid in item.activity_ids:
                if aid not in activity_goals:
                    activity_goals[aid] = {"goal_indices": set(), "needs": set()}
                activity_goals[aid]["goal_indices"].add(idx)
                for need in (item.needs or []):
                    activity_goals[aid]["needs"].add(need)

        for aid, info in activity_goals.items():
            entity = activities_by_id.get(aid)
            if not entity:
                continue
            goal_count = len(info["goal_indices"])
            need_diversity = len(info["needs"])
            # Only meaningful when serving 2+ goals
            if goal_count < 2:
                continue
            score = goal_count * need_diversity

            # Safe read-copy-write
            meta = entity.metadata_dict.copy() if entity.metadata_dict else {}
            meta["multifinality_score"] = score
            meta["multifinality_goal_count"] = goal_count
            meta["multifinality_needs_served"] = sorted(info["needs"])
            try:
                await self.store.entities.update(aid, metadata=meta)
            except Exception as e:
                self.log.warning(
                    "goal_synthesis: failed to persist multifinality for activity %d: %s",
                    aid, e,
                )

    async def _persist_need_landscape(
        self,
        landscape: "NeedLandscape",
        saturation: Dict[str, Any],
    ) -> None:
        """Persist the need landscape analysis on a synthetic metadata record.

        Stores on each goal's metadata (under 'need_landscape_snapshot')
        so the frontend can read it from any goal.
        """
        landscape_data = {
            "saturation": saturation,
            "congruence_insights": landscape.congruence_insights,
            "incongruence_insights": landscape.incongruence_insights,
            "leverage_points": landscape.leverage_points,
            "computed_at": datetime.utcnow().isoformat(),
        }
        # Persist on all current goals
        try:
            goals = await self.store.get_goals()
        except Exception:
            goals = []
        for g in goals:
            meta = g.metadata_dict.copy() if g.metadata_dict else {}
            meta["need_landscape_snapshot"] = landscape_data
            try:
                await self.store.entities.update(g.id, metadata=meta)
            except Exception as e:
                self.log.warning(
                    "goal_synthesis: failed to persist need_landscape on goal %d: %s",
                    g.id, e,
                )

    async def _persist_goals(
        self,
        goal_items: List[GoalItem],
        existing_goals: list,
        all_activities: list,
        goal_relations: Optional[List[GoalRelation]] = None,
        observation_mode: bool = False,
        dropped_goals: Optional[List[DroppedGoal]] = None,
        observation_screenshot_paths: Optional[List[str]] = None,
        context_screenshot_paths: Optional[List[str]] = None,
        new_screenshot_paths: Optional[List[str]] = None,
    ) -> tuple:
        """Persist goal items. Returns (created, updated)."""
        created = 0
        updated = 0
        existing_by_id = {g.id: g for g in existing_goals}
        activities_by_id = {a.id: a for a in all_activities}
        referenced_ids = set()
        # Map from goal index in goal_items → persisted goal ID (for relations)
        idx_to_goal_id: Dict[int, int] = {}

        for idx, item in enumerate(goal_items):
            if item.matches_goal_id and item.matches_goal_id in existing_by_id:
                # Update existing goal
                goal = existing_by_id[item.matches_goal_id]
                referenced_ids.add(goal.id)
                idx_to_goal_id[idx] = goal.id

                # Preserve text the user owns. A lock is a hard constraint, not
                # a hint: `HierarchyService.update_entity` refuses to edit a
                # locked goal's text, and the drop guard below already honours
                # `user_locked` — so synthesis must not relabel one either.
                # The prompt asks for this too, but a model can talk itself out
                # of a prompt; it cannot talk itself past this line.
                meta = goal.metadata_dict.copy() if goal.metadata_dict else {}
                text_is_user_owned = meta.get("user_edited") or meta.get("user_locked")
                new_text = goal.text if text_is_user_owned else item.text

                if not observation_mode:
                    # Diff-based update: only remove/add changed relations to preserve IDs
                    old_rels = await self.store.relations.get_by_target(
                        goal.id,
                        relation_type=RelationType.STRUCTURAL,
                        relation_subtype=RelationSubtype.PART_OF,
                    )
                    old_by_source = {rel.source_id: rel for rel in (old_rels or [])}
                    new_activity_set = set(item.activity_ids)

                    # Delete relations for activities no longer in this goal
                    for src_id, rel in old_by_source.items():
                        if src_id not in new_activity_set:
                            await self.store.relations.delete(rel.id)

                    # Create relations only for newly added activities
                    for activity_id in new_activity_set:
                        if activity_id not in old_by_source:
                            existing_check = await self.store.relations.get_by_source(
                                activity_id,
                                relation_type=RelationType.STRUCTURAL,
                                relation_subtype=RelationSubtype.PART_OF,
                            )
                            if any(r.target_id == goal.id for r in existing_check):
                                continue
                            await self.store.relations.create(
                                source_id=activity_id,
                                target_id=goal.id,
                                relation_type=RelationType.STRUCTURAL,
                                relation_subtype=RelationSubtype.PART_OF,
                            )

                    # Recompute timestamps from constituent activities
                    ts_start = None
                    ts_end = None
                    for aid in item.activity_ids:
                        act = activities_by_id.get(aid)
                        if not act:
                            continue
                        a_start = act.timestamp_start
                        a_end = act.timestamp_end or act.timestamp_start
                        if ts_start is None or a_start < ts_start:
                            ts_start = a_start
                        if ts_end is None or a_end > ts_end:
                            ts_end = a_end
                else:
                    # Observation mode: no activities, keep existing timestamps
                    ts_start = goal.timestamp_start
                    ts_end = goal.timestamp_end

                # Update metadata
                meta["last_synthesized_ts"] = datetime.utcnow().isoformat()
                meta["usage_count"] = len(item.activity_ids)
                if item.evidence_summary:
                    meta["evidence_summary"] = item.evidence_summary
                if item.confidence is not None:
                    meta["goal_confidence"] = item.confidence
                meta = self._store_psych_dimensions(meta, item)

                # Observation mode: merge screenshot paths
                if observation_mode and observation_screenshot_paths:
                    existing_paths = meta.get("observation_screenshots", [])
                    merged = list(dict.fromkeys(existing_paths + observation_screenshot_paths))
                    meta["observation_screenshots"] = merged
                if observation_mode and context_screenshot_paths is not None:
                    existing_ctx = meta.get("context_screenshots", [])
                    meta["context_screenshots"] = list(dict.fromkeys(existing_ctx + context_screenshot_paths))
                if observation_mode and new_screenshot_paths is not None:
                    existing_new = meta.get("new_screenshots", [])
                    meta["new_screenshots"] = list(dict.fromkeys(existing_new + new_screenshot_paths))

                prev_text = goal.text
                await self.store.entities.update(
                    goal.id, text=new_text, metadata=meta,
                    timestamp_start=ts_start,
                    timestamp_end=ts_end,
                )
                updated += 1
                if self.debug_logger:
                    self.debug_logger.log_entity_mutation(
                        pipeline="goal_synthesis", stage="persist_goals",
                        mutation="update", entity_id=goal.id,
                        entity_type="goal", entity_text=new_text,
                        metadata=meta, prev_text=prev_text,
                    )
            else:
                # Create new goal
                metadata = {
                    "status": "active",
                    "usage_count": len(item.activity_ids),
                    "last_synthesized_ts": datetime.utcnow().isoformat(),
                    "user_edited": False,
                    "user_provided": False,
                    "source": "system_inferred",
                    "goal_confidence": item.confidence,
                }
                if item.evidence_summary:
                    metadata["evidence_summary"] = item.evidence_summary
                if observation_mode and observation_screenshot_paths:
                    metadata["observation_screenshots"] = list(dict.fromkeys(observation_screenshot_paths))
                if observation_mode and context_screenshot_paths:
                    metadata["context_screenshots"] = list(dict.fromkeys(context_screenshot_paths))
                if observation_mode and new_screenshot_paths:
                    metadata["new_screenshots"] = list(dict.fromkeys(new_screenshot_paths))
                metadata = self._store_psych_dimensions(metadata, item)

                goal = await self.store.create_goal(
                    text=item.text,
                    activity_ids=item.activity_ids,
                    metadata=metadata,
                )
                referenced_ids.add(goal.id)
                idx_to_goal_id[idx] = goal.id
                created += 1
                if self.debug_logger:
                    self.debug_logger.log_entity_mutation(
                        pipeline="goal_synthesis", stage="persist_goals",
                        mutation="create", entity_id=goal.id,
                        entity_type="goal", entity_text=item.text,
                        metadata=metadata,
                    )

        # Create goal-goal BEHAVIORAL relations from goal_relations
        # First, delete existing goal-goal behavioral relations to avoid duplicates
        all_goal_ids = set(idx_to_goal_id.values())
        for gid in all_goal_ids:
            old_behavioral = await self.store.relations.get_by_source(
                gid,
                relation_type=RelationType.BEHAVIORAL,
            )
            for rel in (old_behavioral or []):
                if rel.target_id in all_goal_ids:
                    await self.store.relations.delete(rel.id)

        if goal_relations:
            # Map relation types to subtypes
            relation_subtype_map = {
                "conflict": RelationSubtype.COMPETES,
                "facilitation": RelationSubtype.SUPPORTS,
                "instrumental": RelationSubtype.SUPPORTS,
            }
            for gr in goal_relations:
                a_id = idx_to_goal_id.get(gr.goal_a_idx)
                b_id = idx_to_goal_id.get(gr.goal_b_idx)
                if a_id and b_id and a_id != b_id:
                    subtype = relation_subtype_map.get(gr.relation, RelationSubtype.SUPPORTS)
                    existing_rels = await self.store.relations.get_by_source(
                        a_id,
                        relation_type=RelationType.BEHAVIORAL,
                        relation_subtype=subtype,
                    )
                    if any(r.target_id == b_id for r in existing_rels):
                        continue
                    await self.store.relations.create(
                        source_id=a_id,
                        target_id=b_id,
                        relation_type=RelationType.BEHAVIORAL,
                        relation_subtype=subtype,
                        metadata={"goal_relation": gr.relation, "evidence": gr.evidence},
                    )

        # Handle orphaned goals (not referenced by synthesis output).
        # Two paths:
        #   1. Explicitly dropped by LLM (in dropped_goals with rationale) → delete immediately
        #   2. Silently missed (not referenced, not in dropped_goals) → miss counter with grace period
        # In observation mode the sliding window is lossy, so the grace period
        # is important — but explicit drops and the miss counter both apply.
        ORPHAN_MISS_THRESHOLD = 3
        explicitly_dropped_ids = {dg.goal_id for dg in (dropped_goals or [])}
        dropped_reasons = {dg.goal_id: dg for dg in (dropped_goals or [])}

        if existing_goals:
            for goal in existing_goals:
                if goal.id in referenced_ids:
                    # Reset miss counter on successful reference
                    meta = goal.metadata_dict or {}
                    if meta.get("synthesis_miss_count", 0) > 0:
                        meta["synthesis_miss_count"] = 0
                        await self.store.entities.update(goal.id, metadata=meta)
                    continue
                meta = (goal.metadata_dict or {}).copy()
                if meta.get("user_edited") or meta.get("user_provided") or meta.get("user_locked"):
                    continue

                if goal.id in explicitly_dropped_ids:
                    # LLM explicitly dropped this goal with a reason — delete immediately
                    dg = dropped_reasons[goal.id]
                    try:
                        await self.store.entities.delete(goal.id)
                        self.log.info(
                            "goal_synthesis: explicitly dropped goal %s reason=%s: %s",
                            goal.id, dg.reason, dg.explanation,
                        )
                        if self.debug_logger:
                            self.debug_logger.log_entity_mutation(
                                pipeline="goal_synthesis", stage="persist_goals",
                                mutation="delete", entity_id=goal.id,
                                entity_type="goal", entity_text=goal.text,
                                metadata={"drop_reason": dg.reason, "drop_explanation": dg.explanation},
                            )
                    except Exception:
                        self.log.warning("goal_synthesis: failed to delete dropped goal %s", goal.id)
                else:
                    # Silently missed — increment miss counter, delete after threshold
                    miss_count = meta.get("synthesis_miss_count", 0) + 1
                    if miss_count >= ORPHAN_MISS_THRESHOLD:
                        try:
                            await self.store.entities.delete(goal.id)
                            self.log.info(
                                "goal_synthesis: deleting goal %s after %d consecutive misses",
                                goal.id, miss_count,
                            )
                            if self.debug_logger:
                                self.debug_logger.log_entity_mutation(
                                    pipeline="goal_synthesis", stage="persist_goals",
                                    mutation="delete", entity_id=goal.id,
                                    entity_type="goal", entity_text=goal.text,
                                    metadata={"synthesis_miss_count": miss_count},
                                )
                        except Exception:
                            self.log.warning("goal_synthesis: failed to delete orphaned goal %s", goal.id)
                    else:
                        meta["synthesis_miss_count"] = miss_count
                        await self.store.entities.update(goal.id, metadata=meta)
                        self.log.info(
                            "goal_synthesis: goal %s not referenced (miss %d/%d), keeping",
                            goal.id, miss_count, ORPHAN_MISS_THRESHOLD,
                        )

        return created, updated, idx_to_goal_id

    async def _materialize_goal_screenshots(self, idx_to_goal_id: Dict[int, int]) -> None:
        """Store the full list of contributing screenshot paths on each goal.

        Traverses goal → activities → actions → operations and collects
        screenshot_path from operation metadata.  The result is written to
        ``goal.metadata["observation_screenshots"]`` so every condition
        exposes the same field for comparison.
        """
        for goal_id in set(idx_to_goal_id.values()):
            try:
                # activity IDs linked to this goal
                act_rels = await self.store.relations.get_by_target(
                    goal_id,
                    relation_type=RelationType.STRUCTURAL,
                    relation_subtype=RelationSubtype.PART_OF,
                )
                screenshots: list[str] = []
                for ar in (act_rels or []):
                    # action IDs linked to this activity
                    action_rels = await self.store.relations.get_by_target(
                        ar.source_id,
                        relation_type=RelationType.STRUCTURAL,
                        relation_subtype=RelationSubtype.PART_OF,
                    )
                    for acr in (action_rels or []):
                        # operation IDs linked to this action
                        op_rels = await self.store.relations.get_by_target(
                            acr.source_id,
                            relation_type=RelationType.STRUCTURAL,
                            relation_subtype=RelationSubtype.PART_OF,
                        )
                        for opr in (op_rels or []):
                            op = await self.store.entities.get(opr.source_id)
                            if op and op.metadata_dict:
                                sp = op.metadata_dict.get("screenshot_path")
                                if sp:
                                    screenshots.append(sp)

                # Deduplicate while preserving order
                seen: set[str] = set()
                unique: list[str] = []
                for s in screenshots:
                    if s not in seen:
                        seen.add(s)
                        unique.append(s)

                goal = await self.store.entities.get(goal_id)
                if goal:
                    meta = (goal.metadata_dict or {}).copy()
                    meta["observation_screenshots"] = unique
                    await self.store.entities.update(goal_id, metadata=meta)
            except Exception as e:
                self.log.warning(
                    "goal_synthesis: failed to materialize screenshots for goal %d: %s",
                    goal_id, e,
                )

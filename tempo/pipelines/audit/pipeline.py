"""AuditPipeline — privacy gate using contextual integrity.

Before an observation enters the pipeline, the audit checks whether
the data should be transmitted based on privacy norms inferred from
the user's own model.

Each condition gets its own AuditPipeline instance that queries
that condition's DB for context.
"""

from __future__ import annotations

import json
import logging
import time

from tempo.utils import parse_llm_json
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from tempo.db import Database
from tempo.models import EntityType
from tempo.providers import ModelProvider
from tempo.store import Store
from .bm25 import bm25_search
from .prompt import AUDIT_PROMPT
from tempo.schemas import AuditDecisionResult, get_schema

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger

logger = logging.getLogger(__name__)

AUDIT_DECISION_FORMAT = get_schema(AuditDecisionResult.model_json_schema())


class AuditPipeline:
    """Privacy audit gate using contextual integrity.

    Queries the condition's own DB for recent entities to build
    a privacy profile, then asks the LLM whether the new observation
    should be transmitted for processing.
    """

    def __init__(
        self,
        provider: ModelProvider,
        db: Database,
        user_name: str = "the user",
        entity_type: str = EntityType.GOAL,
        debug: bool = False,
        debug_logger: Optional["DebugLogger"] = None,
    ):
        self.provider = provider
        self.db = db
        self.user_name = user_name
        self.entity_type = entity_type
        self.debug = debug
        self.debug_logger = debug_logger

    async def audit(
        self,
        transcription_text: str,
        metadata: Optional[dict] = None,
    ) -> Tuple[bool, Dict]:
        """Audit an observation for privacy compliance.

        Args:
            transcription_text: Raw transcription to audit.
            metadata: Optional metadata (screenshot_path, etc.).

        Returns:
            Tuple of (should_transmit, audit_result_dict).
            On error, defaults to (True, {"error": ...}) — non-blocking.
        """
        try:
            past_interaction = await self._build_past_interaction(
                transcription_text
            )

            prompt = (
                AUDIT_PROMPT
                .replace("{user_name}", self.user_name)
                .replace("{past_interaction}", past_interaction)
                .replace("{user_input}", transcription_text)
            )

            t0 = time.monotonic()
            response_text = await self.provider.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format=AUDIT_DECISION_FORMAT,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            data = parse_llm_json(response_text)
            # Gemini may return a JSON array instead of object
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}

            should_transmit = data.get("transmit_data", True)

            if self.debug_logger:
                self.debug_logger.log_llm_call(
                    pipeline="audit", stage="audit",
                    model=self.provider.model,
                    prompt=prompt, response=response_text,
                    latency_ms=latency_ms, response_format="json_schema",
                )
                self.debug_logger.log_audit_decision(
                    decision="allow" if should_transmit else "block",
                    data_type=data.get("data_type"),
                    reasoning=data.get("reasoning"),
                    transmit_data=should_transmit,
                    past_interaction_summary=f"{len(past_interaction)} chars, searched {self.entity_type}",
                    observation_text_preview=transcription_text,
                )

            if self.debug:
                logger.info(
                    "[Audit] transmit=%s, data_type=%s, reasoning=%s",
                    should_transmit,
                    data.get("data_type", "unknown"),
                    data.get("reasoning", ""),
                )

            return should_transmit, data

        except Exception as e:
            logger.error("[Audit] Audit failed (non-blocking): %s", e)
            return True, {"error": str(e)}

    async def _build_past_interaction(self, query_text: str) -> str:
        """Build past interaction context from the condition's DB.

        Searches for recent entities matching the query text to
        establish privacy norms.
        """
        try:
            async with self.db.session() as session:
                with session.no_autoflush:
                    hits = await bm25_search(
                        session,
                        query_text,
                        entity_type=self.entity_type,
                        limit=10,
                        mode="OR",
                        enable_mmr=False,
                        enable_decay=True,
                    )

            if not hits:
                return "*None*"

            chunks = []
            for entity, score in hits:
                meta = entity.metadata_dict or {}
                lines = [f"- {entity.text}"]
                reasoning = meta.get("reasoning", "")
                if reasoning:
                    lines.append(f"  Reasoning: {reasoning}")
                confidence = meta.get("confidence")
                if confidence is not None:
                    lines.append(f"  Confidence: {confidence}")
                lines.append(f"  Relevance: {score:.2f}")
                chunks.append("\n".join(lines))

            return "\n\n".join(chunks)

        except Exception as e:
            logger.debug("[Audit] Past interaction query failed: %s", e)
            return "*None*"

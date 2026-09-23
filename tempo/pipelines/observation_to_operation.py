# observation_to_operation.py

from __future__ import annotations
import json
import time
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING
import os
from pathlib import Path

from tempo.prompts.operation_extraction import OPERATION_EXTRACTION_PROMPT
from tempo.schemas import OperationItem, OperationExtractionResult, get_schema
from tempo.utils import get_debug_logger, parse_llm_json

OPERATION_EXTRACTION_FORMAT = get_schema(OperationExtractionResult.model_json_schema())

if TYPE_CHECKING:
    from tempo.debug_logger import DebugLogger
    from tempo.providers import ModelProvider
    from tempo.store import Store


def _file_mtime_utc(path: Path) -> Optional[datetime]:
    """Return file modification time as a naive UTC datetime, or None."""
    try:
        if path.exists():
            return datetime.utcfromtimestamp(path.stat().st_mtime)
    except Exception:
        pass
    return None


def _timestamp_from_screenshot_path(path: Optional[str]) -> Optional[datetime]:
    if not path:
        return None
    p = Path(path).expanduser()
    # Try to parse capture-time epoch embedded in the filename
    try:
        epoch_str = p.stem.split("_", 1)[0]
        epoch = float(epoch_str)
        if epoch > 0:
            return datetime.utcfromtimestamp(epoch)
    except Exception:
        pass
    # Fallback: use file modification time (OS always knows when it was written)
    return _file_mtime_utc(p)


def _timestamp_from_transcription(source_transcription: Optional[str]) -> Optional[datetime]:
    if not source_transcription:
        return None
    try:
        json_path = Path(source_transcription).expanduser()
    except Exception:
        return None
    if not json_path.exists() or not json_path.is_file():
        return None
    try:
        data = json.loads(json_path.read_text())
    except Exception:
        return None
    # Try screenshot filename first (most precise — embeds time.time() at capture)
    screenshot_name = data.get("screenshot_path")
    if screenshot_name:
        candidate = (json_path.parent / screenshot_name).expanduser()
        ts = _timestamp_from_screenshot_path(str(candidate))
        if ts:
            return ts
    # Try JSON timestamp/created_at fields
    for key in ("timestamp", "created_at"):
        ts_str = data.get(key)
        if ts_str:
            try:
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue
    # Last resort: use the JSON file's own mtime
    return _file_mtime_utc(json_path)


class ObservationAdapter:
    """
    Pipeline: Observation → Operation
    
    Converts raw observations from the screen observer
    into atomic operations stored in the database.
    """
    
    def __init__(
        self,
        provider: "ModelProvider",
        store: "Store",
        debug: bool = False,
        user_name: Optional[str] = None,
        user_context: str = "",
        debug_logger: Optional["DebugLogger"] = None,
    ):
        """
        Initialize the observation adapter.

        Args:
            provider: LLM provider for operation extraction.
            store: Database store for persisting operations.
            debug: Enable detailed logging of prompts/responses and created entities.
            user_name: Name used in prompts; defaults to $USER_NAME or 'the user'.
            user_context: Rendered context block for +C study conditions (empty for production).
            debug_logger: Structured JSONL debug logger.
        """
        self.provider = provider
        self.store = store
        self.debug = debug
        self.log = get_debug_logger(self, debug=debug)
        self.user_name = user_name or os.getenv("USER_NAME", "the user")
        self.user_context = user_context
        self.debug_logger = debug_logger
    
    async def process_observation(
        self,
        observation_text: str,
        observation_timestamp: Optional[datetime] = None,
        screenshot_path: Optional[str] = None,
        source_transcription: Optional[str] = None,
    ) -> List[int]:
        """
        Process a raw observation and extract operations.
        
        Args:
            observation_text: The observation text from the screen observer.
            observation_timestamp: Timestamp of the observation (defaults to now).
            
        Returns:
            List of created operation IDs.
        """
        resolved_timestamp = (
            _timestamp_from_screenshot_path(screenshot_path)
            or _timestamp_from_transcription(source_transcription)
        )
        if resolved_timestamp:
            observation_timestamp = resolved_timestamp
        if observation_timestamp is None:
            observation_timestamp = datetime.utcnow()

        self.log.debug(
            "process_observation: received text (len=%d) at %s",
            len(observation_text or ""),
            observation_timestamp.isoformat(),
        )
        
        # Extract operations using LLM
        operations = await self._extract_operations(
            observation_text, 
            observation_timestamp
        )

        self.log.debug("process_observation: extracted %d operations", len(operations))
        
        # Store operations in database
        operation_ids = []
        for op in operations:
            timestamp = observation_timestamp
            
            # Build metadata including reasoning and scores
            metadata = op.context.copy() if op.context else {}
            if op.reasoning:
                metadata["reasoning"] = op.reasoning
            if op.confidence:
                metadata["confidence"] = op.confidence
            if op.decay:
                metadata["decay"] = op.decay
            if screenshot_path:
                metadata["screenshot_path"] = screenshot_path
            if source_transcription:
                metadata["source_transcription"] = source_transcription
            
            # Create operation entity
            entity = await self.store.create_operation(
                text=op.text,
                timestamp=timestamp,
                metadata=metadata if metadata else None,
            )
            operation_ids.append(entity.id)

            if self.debug_logger:
                self.debug_logger.log_entity_mutation(
                    pipeline="observation_to_operation", stage="store",
                    mutation="create", entity_id=entity.id,
                    entity_type="operation", entity_text=op.text,
                    metadata=metadata,
                )

            self.log.debug(
                "process_observation: stored operation id=%s text=%s ts=%s",
                entity.id,
                op.text[:200],
                timestamp.isoformat(),
            )
        
        return operation_ids
    
    async def _extract_operations(
        self,
        observation_text: str,
        observation_timestamp: datetime,
    ) -> List[OperationItem]:
        """
        Use LLM to extract operations from observation text.
        
        Args:
            observation_text: The observation text.
            observation_timestamp: Timestamp of the observation.
            
        Returns:
            List of extracted OperationItems.
        """
        # Build prompt
        prompt = OPERATION_EXTRACTION_PROMPT.format(
            observation_text=observation_text,
            user_name=self.user_name,
            user_context=self.user_context,
        )

        self.log.debug("extract_operations: prompt length=%d", len(prompt))
        
        # Call LLM
        t0 = time.monotonic()
        response = await self.provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            response_format=OPERATION_EXTRACTION_FORMAT,
        )
        latency_ms = (time.monotonic() - t0) * 1000

        if self.debug_logger:
            self.debug_logger.log_llm_call(
                pipeline="observation_to_operation", stage="extract",
                model=self.provider.model,
                prompt=prompt, response=response,
                latency_ms=latency_ms, response_format="json_schema",
            )

        self.log.debug(
            "extract_operations: raw response length=%d", len(response or "")
        )
        
        # Parse response
        try:
            data = parse_llm_json(response)
            operations = []

            # Handle both formats:
            # 1. {"operations": [...]} - dict with operations key
            # 2. [...] - direct list (some models return this)
            if isinstance(data, list):
                ops_list = data
            elif isinstance(data, dict):
                ops_list = data.get("operations", [])
            else:
                ops_list = []
            
            for op_data in ops_list:
                # Convert confidence/decay to strings (LLM may return int or string)
                conf = op_data.get("confidence")
                dec = op_data.get("decay")
                if conf is not None:
                    conf = str(conf)
                if dec is not None:
                    dec = str(dec)
                
                # Coerce context to dict — Gemini may return a string
                # when the schema converts bare object → string type
                ctx = op_data.get("context")
                if isinstance(ctx, str):
                    try:
                        ctx = json.loads(ctx)
                    except (json.JSONDecodeError, ValueError):
                        ctx = {"raw": ctx} if ctx else None

                operations.append(OperationItem(
                    text=op_data.get("text", ""),
                    timestamp=op_data.get("timestamp", observation_timestamp.isoformat()),
                    reasoning=op_data.get("reasoning"),
                    confidence=conf,
                    decay=dec,
                    context=ctx,
                ))
            
            self.log.debug(
                "extract_operations: parsed %d operations", len(operations)
            )
            return operations
            
        except (json.JSONDecodeError, KeyError) as e:
            self.log.warning("extract_operations: parse failed (%s), returning fallback op", e)
            # If parsing fails, create a single operation with the full text
            return [OperationItem(
                text=f"[Observation] {observation_text[:500]}",
                timestamp=observation_timestamp.isoformat(),
                context={"parse_error": str(e)},
            )]

"""Structured JSONL debug logging for production pipeline events."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class DebugLogger:
    """Append-only JSONL logger for pipeline debug events.

    Each pipeline component receives the same production logger instance.

    Usage::

        dl = DebugLogger(Path("~/.cache/tempo/debug.jsonl"))
        dl.log_llm_call(pipeline="gum_pipeline", stage="propose", ...)
        dl.close()
    """

    MAX_FILE_SIZE_MB = 500

    def __init__(
        self,
        log_path: Path,
        enabled: bool = True,
    ):
        self.log_path = Path(log_path)
        self.enabled = enabled
        self._file = None

        if enabled:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _ensure_file(self):
        if self._file is None or self._file.closed:
            self._file = open(self.log_path, "a", encoding="utf-8")
        try:
            size_mb = self.log_path.stat().st_size / (1024 * 1024)
            if size_mb > self.MAX_FILE_SIZE_MB:
                self._rotate()
        except OSError:
            pass

    def _rotate(self):
        if self._file and not self._file.closed:
            self._file.close()
        suffix = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        rotated = self.log_path.with_suffix(f".{suffix}.jsonl")
        try:
            self.log_path.rename(rotated)
        except OSError:
            pass
        self._file = open(self.log_path, "a", encoding="utf-8")

    def _write(self, record: Dict[str, Any]):
        if not self.enabled:
            return
        record["ts"] = datetime.utcnow().isoformat()
        self._ensure_file()
        line = json.dumps(record, default=str, ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def log_llm_call(
        self,
        pipeline: str,
        stage: str,
        model: str,
        prompt: str,
        response: Optional[str],
        latency_ms: float,
        response_format: Optional[str] = None,
        error: Optional[str] = None,
    ):
        self._write({
            "event": "llm_call",
            "pipeline": pipeline,
            "stage": stage,
            "model": model,
            "prompt": prompt,
            "response": response,
            "latency_ms": round(latency_ms, 1),
            "response_format": response_format,
            "error": error,
        })

    def log_entity_mutation(
        self,
        pipeline: str,
        stage: str,
        mutation: str,
        entity_id: int,
        entity_type: str,
        entity_text: Optional[str] = None,
        metadata: Optional[dict] = None,
        prev_text: Optional[str] = None,
        prev_metadata: Optional[dict] = None,
    ):
        self._write({
            "event": "entity_mutation",
            "pipeline": pipeline,
            "stage": stage,
            "mutation": mutation,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "entity_text": entity_text,
            "metadata": metadata,
            "prev_text": prev_text,
            "prev_metadata": prev_metadata,
        })

    def log_audit_decision(
        self,
        decision: str,
        data_type: Optional[str] = None,
        reasoning: Optional[str] = None,
        transmit_data: bool = True,
        past_interaction_summary: Optional[str] = None,
        observation_text_preview: Optional[str] = None,
    ):
        self._write({
            "event": "audit_decision",
            "pipeline": "audit",
            "decision": decision,
            "data_type": data_type,
            "reasoning": reasoning,
            "transmit_data": transmit_data,
            "past_interaction_summary": past_interaction_summary,
            "observation_text_preview": observation_text_preview[:500] if observation_text_preview else None,
        })

    def close(self):
        if self._file and not self._file.closed:
            self._file.close()

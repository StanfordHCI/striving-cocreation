import logging
import math
import os
import re
from datetime import datetime
from typing import Any, Dict

# Match ALL backslash sequences atomically so that valid \\ pairs are consumed
# as a unit and the second \ is never mistaken for the start of a new escape.
#   Group 1 = valid escape  (keep as-is)
#   Group 2 = invalid char  (double the backslash)
_ESCAPE_RE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrtu])|\\(.)')


def _fix_escape(m: re.Match) -> str:
    if m.group(1):          # valid escape — leave untouched
        return m.group(0)
    return '\\\\' + m.group(2)  # invalid escape — double the backslash


def fix_json_escapes(text: str) -> str:
    """Replace invalid JSON escape sequences (e.g. \\T, \\s, \\url) with double-backslash.

    Processes escape sequences atomically so that already-escaped backslashes
    (``\\\\``) are not falsely flagged.
    """
    return _ESCAPE_RE.sub(_fix_escape, text)


def strip_json_fences(text: str) -> str:
    """Strip markdown code fences from an LLM response."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    elif text.startswith("```"):
        text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return text


def parse_llm_json(text: str) -> dict | list:
    """Best-effort JSON parse: strip fences, fix escapes, retry on failure.

    Handles common LLM failure modes:
    - Markdown code fences
    - Invalid escape sequences
    - Concatenated JSON objects ("Extra data")
    - Non-ASCII control characters
    """
    import json

    cleaned = strip_json_fences(text)
    fixed = fix_json_escapes(cleaned)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        # Concatenated JSON objects — extract just the first one
        if "Extra data" in str(e):
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(fixed)
            return obj
        # Last resort: strip all non-ASCII control chars and retry
        sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', fixed)
        try:
            return json.loads(sanitized)
        except json.JSONDecodeError as e2:
            if "Extra data" in str(e2):
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(sanitized)
                return obj
            raise


def render_prompt_template(template: str, **values: Any) -> str:
    """Render prompt templates without requiring brace-escaping in overrides."""
    rendered = template
    for key, val in values.items():
        rendered = rendered.replace("{" + key + "}", str(val))
    return rendered.replace("{{", "{").replace("}}", "}")


def is_network_error(exc: Exception) -> bool:
    """Return True for connectivity issues (wait & retry), False for auth/validation (fail fast)."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    msg = str(exc).lower()
    # Fail fast on auth/billing/validation — these won't resolve by waiting
    non_retry = ("api key", "authentication", "permission", "invalid")
    if any(m in msg for m in non_retry):
        return False
    network_markers = (
        "connection refused", "name resolution", "network unreachable",
        "connect timeout", "read timeout", "ssl", "eof", "broken pipe",
        "reset by peer", "temporary failure", "server disconnected",
    )
    if any(m in msg for m in network_markers):
        return True
    # Default: treat unknown errors as potentially network-related
    return True


def update_activity_scores(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Compute activity_confidence and activity_stability from metadata counters.

    Reads: usage_count, batches_seen, last_assigned_ts, merge_count, fork_count, revision_count.
    Writes: activity_confidence (1-10), activity_stability (1-10).
    """
    usage = int(meta.get("usage_count", 0) or 0)
    batches_seen = int(meta.get("batches_seen", 0) or 0)
    last_seen = meta.get("last_assigned_ts")

    supporting_norm = min(1.0, math.log1p(usage) / math.log1p(20))
    batches_norm = min(1.0, batches_seen / 5.0)

    recency_score = 0.2
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
            else:
                recency_score = 0.2
        except Exception:
            recency_score = 0.2

    churn_events = (
        int(meta.get("merge_count", 0) or 0)
        + int(meta.get("fork_count", 0) or 0)
        + int(meta.get("revision_count", 0) or 0)
    )
    churn_rate = min(1.0, churn_events / max(1.0, float(batches_seen)))
    stability = max(0.0, 1.0 - churn_rate)

    confidence_raw = (
        0.4 * supporting_norm
        + 0.3 * batches_norm
        + 0.2 * recency_score
        + 0.1 * stability
    )
    activity_confidence = max(1, min(10, int(round(1 + 9 * confidence_raw))))
    activity_stability = max(1, min(10, int(round(1 + 9 * stability))))

    meta["activity_confidence"] = activity_confidence
    meta["activity_stability"] = activity_stability
    return meta


def report_pipeline_error(stage: str, error: Exception, context: dict = None):
    """Log a structured pipeline error to the local application log."""
    err_logger = logging.getLogger("tempo.errors")
    err_logger.error(
        "Pipeline error in %s: %s",
        stage, error,
        exc_info=error,
        extra={"stage": stage, "context": context or {}},
    )


def get_debug_logger(
    instance,
    debug: bool = True,
    log_errors_to_file: bool | None = True,
) -> logging.Logger:
    """Create and return a logger with DEBUG level."""
    log = logging.getLogger(str(instance.__class__.__name__))
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    if not log.hasHandlers():
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        log.addHandler(h)
    _attach_error_file_handler(log, log_errors_to_file)
    return log


def _attach_error_file_handler(
    log: logging.Logger,
    log_errors_to_file: bool | None,
) -> None:
    if log_errors_to_file is None:
        log_errors_to_file = os.getenv("TEMPO_LOG_ERRORS_TO_FILE", "1") == "1"
    if not log_errors_to_file:
        return
    log_path = os.getenv("TEMPO_ERROR_LOG_PATH", "~/.cache/tempo/errors.log")
    log_path = os.path.expanduser(log_path)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handler_id = f"error_file:{log_path}"
    for handler in log.handlers:
        if getattr(handler, "name", None) == handler_id:
            return
    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    file_handler.name = handler_id
    log.addHandler(file_handler)

"""Bounded personal context and explicit feedback shared by both assistants."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tempo.prompts.context_block import CONTEXT_BLOCK_TEMPLATE

ONBOARDING_FIELDS = (
    "roles", "typical_day", "main_concerns", "stressors", "recent_changes",
    "work", "relationships", "personal_growth", "health", "finances",
    "education", "additional_context",
)
MAX_PERSON_CONTEXT = 12000


def _read_onboarding(data_directory: str) -> tuple[str, bool]:
    path = Path(data_directory) / "onboarding.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "", False
    # Invalid saved context must not silently turn into missing personal context.
    if not isinstance(data, dict) or not isinstance(data.get("responses", {}), dict):
        raise ValueError("Invalid onboarding context")
    responses = data.get("responses", {})
    answers = {}
    truncated = False
    for field in ONBOARDING_FIELDS:
        value = responses.get(field, "")
        if not isinstance(value, str):
            raise ValueError("Invalid onboarding answer")
        # Reserve room for every answer; long early answers must not hide later ones.
        truncated |= len(value) > 800
        answers[field] = value[:800] or "Not provided"
    name = data.get("user_name") or "the user"
    if not isinstance(name, str):
        raise ValueError("Invalid onboarding name")
    rendered = CONTEXT_BLOCK_TEMPLATE.format(user_name=name[:200], **answers)
    return rendered if any(responses.get(field) for field in ONBOARDING_FIELDS) else "", truncated


async def personal_context(data_directory: str, override: str | None) -> tuple[str, bool]:
    if override is not None:
        return override[:MAX_PERSON_CONTEXT], len(override) > MAX_PERSON_CONTEXT
    return await asyncio.to_thread(_read_onboarding, data_directory)


class SuggestionHistoryItem(BaseModel):
    """Explicit suggestion state from the runtime, never inferred from behavior."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    suggestion_id: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=600)
    status: Literal["shown", "dismissed", "completed", "discussed"]
    updated_at: datetime


def recent_suggestions(items: list[SuggestionHistoryItem], now: datetime) -> list[dict]:
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    now = utc(now)
    cutoff = now - timedelta(days=30)
    latest: dict[str, SuggestionHistoryItem] = {}
    for item in sorted(items, key=lambda item: utc(item.updated_at)):
        if cutoff <= utc(item.updated_at) <= now:
            latest[item.suggestion_id] = item
    return [
        {**item.model_dump(mode="json"), "updated_at": utc(item.updated_at).isoformat()}
        for item in sorted(latest.values(), key=lambda item: (utc(item.updated_at), item.suggestion_id), reverse=True)[:20]
    ]

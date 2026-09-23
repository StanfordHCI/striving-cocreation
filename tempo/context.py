"""UserContext: loads onboarding interview data and formats context blocks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from tempo.prompts.context_block import CONTEXT_BLOCK_TEMPLATE


class UserContext:
    """Loads onboarding JSON and renders context blocks for +C pipeline variants."""

    def __init__(self, onboarding_path: str, user_name: str = "the user"):
        self.user_name = user_name
        self.participant_id: str = "unknown"
        self.responses: Dict[str, str] = {}
        self._rendered: Optional[str] = None
        self._load(onboarding_path)

    def _load(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Onboarding file not found: {p}")
        data = json.loads(p.read_text())
        self.participant_id = data.get("participant_id", "unknown")
        self.responses = data.get("responses", {})

    def render(self) -> str:
        """Render the full context block for prompt injection."""
        if self._rendered is None:
            self._rendered = CONTEXT_BLOCK_TEMPLATE.format(
                user_name=self.user_name,
                roles=self.responses.get("roles", "Not provided"),
                typical_day=self.responses.get("typical_day", "Not provided"),
                main_concerns=self.responses.get("main_concerns", "Not provided"),
                stressors=self.responses.get("stressors", "Not provided"),
                recent_changes=self.responses.get("recent_changes", "Not provided"),
                work=self.responses.get("work", "Not provided"),
                relationships=self.responses.get("relationships", "Not provided"),
                personal_growth=self.responses.get("personal_growth", "Not provided"),
                health=self.responses.get("health", "Not provided"),
                finances=self.responses.get("finances", "Not provided"),
                education=self.responses.get("education", "Not provided"),
                additional_context=self.responses.get("additional_context", "Not provided"),
            )
        return self._rendered

    @staticmethod
    def empty() -> str:
        """Return empty string for non-context conditions."""
        return ""

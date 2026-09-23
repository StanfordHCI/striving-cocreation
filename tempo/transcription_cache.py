"""Utilities for inspecting and backfilling the transcription cache."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class CachedTranscription:
    screenshot_path: str
    timestamp: datetime
    transcription: str
    summary: str
    app_context: Optional[dict]
    model_used: str
    created_at: datetime

    @classmethod
    def from_json_file(cls, json_path: Path) -> "CachedTranscription":
        data = json.loads(json_path.read_text())
        return cls(
            screenshot_path=str(json_path.parent / data["screenshot_path"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            transcription=data["transcription"],
            summary=data["summary"],
            app_context=data.get("app_context"),
            model_used=data["model_used"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )


class TranscriptionCache:
    """Query and iterate over cached transcriptions."""

    def __init__(self, screenshots_dir: str = "~/.cache/tempo/screenshots"):
        self.screenshots_dir = Path(screenshots_dir).expanduser()

    def list_all(self) -> List[CachedTranscription]:
        """List all cached transcriptions, sorted by timestamp."""
        transcriptions = []
        for json_file in self.screenshots_dir.glob("*.json"):
            # Skip .meta.json (capture metadata) and _ctx.json (context-enriched)
            name = json_file.name
            if name.endswith(".meta.json") or name.endswith("_ctx.json"):
                continue
            try:
                transcriptions.append(CachedTranscription.from_json_file(json_file))
            except (json.JSONDecodeError, KeyError) as e:
                # Skip malformed files
                continue
        return sorted(transcriptions, key=lambda t: t.timestamp)

    def get_range(self, start: int = 0, end: Optional[int] = None) -> List[CachedTranscription]:
        """Get transcriptions by index range."""
        all_transcriptions = self.list_all()
        return all_transcriptions[start:end]

    def get_by_time(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None
    ) -> List[CachedTranscription]:
        """Get transcriptions within a time range."""
        all_transcriptions = self.list_all()
        result = []
        for t in all_transcriptions:
            if since and t.timestamp < since:
                continue
            if until and t.timestamp > until:
                continue
            result.append(t)
        return result

    def count(self) -> int:
        """Count total cached transcriptions."""
        return sum(
            1 for f in self.screenshots_dir.glob("*.json")
            if not f.name.endswith(".meta.json") and not f.name.endswith("_ctx.json")
        )

    def has_transcription(self, screenshot_path: str) -> bool:
        """Check if a screenshot has a cached transcription."""
        json_path = Path(screenshot_path).with_suffix('.json')
        return json_path.exists()

    def list_screenshots_without_cache(self) -> List[str]:
        """List all screenshots that don't have a cached transcription.

        Skips screenshots that have a .failed marker (permanently broken).
        """
        screenshots = sorted(self.screenshots_dir.glob("*.jpg"))
        missing = []
        for jpg_path in screenshots:
            json_path = jpg_path.with_suffix('.json')
            failed_path = Path(str(jpg_path).rsplit('.', 1)[0] + '.failed')
            if not json_path.exists() and not failed_path.exists():
                missing.append(str(jpg_path))
        return missing

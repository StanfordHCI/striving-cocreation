"""Prompts for LLM calls in Tempo."""

from pathlib import Path
from functools import lru_cache

_PROMPTS_DIR = Path(__file__).parent / "txt"

@lru_cache(maxsize=None)
def load_prompt(module: str, name: str) -> str:
    """Load a prompt template from a .txt file."""
    path = _PROMPTS_DIR / module / f"{name}.txt"
    return path.read_text()


from .screen import TRANSCRIPTION_PROMPT, SUMMARY_PROMPT
from .operation_extraction import OPERATION_EXTRACTION_PROMPT
from .action_generation import ACTION_GENERATION_PROMPT
from .activity_generation import PROPOSE_ONLY_PROMPT
from .goal_propose import GOAL_PROPOSE_PROMPT, GOAL_PROPOSE_OBSERVATION_PROMPT
from .goal_reconcile import GOAL_RECONCILE_PROMPT

__all__ = [
    "load_prompt",
    "TRANSCRIPTION_PROMPT",
    "SUMMARY_PROMPT",
    "OPERATION_EXTRACTION_PROMPT",
    "ACTION_GENERATION_PROMPT",
    "PROPOSE_ONLY_PROMPT",
    "GOAL_PROPOSE_PROMPT",
    "GOAL_PROPOSE_OBSERVATION_PROMPT",
    "GOAL_RECONCILE_PROMPT",
]

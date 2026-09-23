"""Prompt for jointly reasoning over recent context, goals, and their graph."""
from tempo.prompts import load_prompt

SYSTEM_PROMPT = load_prompt("contextual_assistant", "SYSTEM_PROMPT")

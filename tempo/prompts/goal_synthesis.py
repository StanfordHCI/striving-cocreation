"""Prompts for GoalSynthesis (Stage 4): synthesize life goals from activities."""

from tempo.prompts import load_prompt

SYNTHESIZE_PROMPT = load_prompt("goal_synthesis", "SYNTHESIZE_PROMPT")
CONTRAST_SECTION = load_prompt("goal_synthesis", "CONTRAST_SECTION")
CONTRAST_OUTPUT_SCHEMA = load_prompt("goal_synthesis", "CONTRAST_OUTPUT_SCHEMA")
SYNTHESIZE_OBSERVATION_PROMPT = load_prompt("goal_synthesis", "SYNTHESIZE_OBSERVATION_PROMPT")
SELF_REFINE_OBSERVATION_PROMPT = load_prompt("goal_synthesis", "SELF_REFINE_OBSERVATION_PROMPT")
SELF_REFINE_PROMPT = load_prompt("goal_synthesis", "SELF_REFINE_PROMPT")

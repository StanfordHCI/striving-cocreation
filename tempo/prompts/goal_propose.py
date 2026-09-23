"""Prompt for Goal Propose step (B1): propose candidate life goals from recently changed activities.

This is the UNBIASED step — the LLM sees activities but NOT existing goals,
so it proposes fresh candidate strivings without anchoring to what already exists.
The subsequent goal reconcile step will integrate these candidates with the existing
goal repository.
"""

from tempo.prompts import load_prompt

GOAL_PROPOSE_PROMPT = load_prompt("goal_propose", "GOAL_PROPOSE_PROMPT")
GOAL_PROPOSE_OBSERVATION_PROMPT = load_prompt("goal_propose", "GOAL_PROPOSE_OBSERVATION_PROMPT")

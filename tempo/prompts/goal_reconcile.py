"""Prompt for Goal Reconcile step (B1): reconcile candidate goals against existing goal repository.

This is the RECONCILE step — the LLM sees candidate goals (from the propose step)
alongside the existing goal repository, and decides how to integrate each candidate:
match, revise, new, or merge. The system should maintain 5–15 life goals total.
"""

from tempo.prompts import load_prompt

GOAL_RECONCILE_PROMPT = load_prompt("goal_reconcile", "GOAL_RECONCILE_PROMPT")
GOAL_RECONCILE_MULTI_PROMPT = GOAL_RECONCILE_PROMPT

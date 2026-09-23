"""MI prompt adaptation from YouBeAnything, branch shardul/ui.

Source: c80cdb57405b51b43b61d7854db3f1e6c00e517f, tempo/prompts/coaching.py.
The original credits GPTCoach (Stanford HCI, CHI 2025).
"""
from tempo.prompts import load_prompt

SYSTEM_PROMPT = load_prompt("assistant_chat", "SYSTEM_PROMPT")
STRATEGY_PROMPT = load_prompt("assistant_chat", "STRATEGY_PROMPT")
STRATEGIES = {
    "Question": "Ask one open question that helps the person explore their own perspective.",
    "Reflect": "Reflect what the person actually expressed, without assigning unspoken feelings or priorities.",
    "Affirm": "Recognize a specific effort, value, or strength supported by the conversation. Avoid generic praise.",
    "Facilitate": "Briefly acknowledge what the person said and make room for them to continue.",
    "Summarize": "Connect the main themes already expressed, then check whether you understood them.",
    "Advise with Permission": "If the person asked for suggestions, give one to three concrete options directly. Their request is permission; do not ask again. Otherwise ask before introducing unsolicited advice.",
    "Giving Information": "Share one relevant, grounded observation and leave room for the person's interpretation.",
    "Reframe": "Offer a tentative alternative interpretation without dismissing the person's concern.",
    "Support": "Acknowledge the person's experience with empathy; do not rush to solve it.",
    "Raise Concern": "Tentatively name an evidence-supported tension, then invite the person's perspective. Do not infer neglect from absent computer activity.",
    "Offer Analysis": "Offer to examine relevant activity when that would help. An explicit request to explain a card or inspect evidence already authorizes that analysis.",
}

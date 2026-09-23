"""Privacy audit prompt for contextual integrity gate."""

AUDIT_PROMPT = """You are a data privacy compliance assistant for a user modeling system.

Here are some past interactions {user_name} had with the system:

## Past Interactions

{past_interaction}

## Task

{user_name} currently is looking at the following:

User Input
---
{user_input}
---

Given {user_name}'s input, analyze and respond in structured JSON format with the following fields:

1. **is_new_information** (bool): Does this input contain new information compared to the past interactions?
2. **data_type** (str): What type of data is being disclosed? (e.g., "banking_credentials", "health_information", "personal_communications", "work_activity", "browsing_activity", "general_activity", "none")
3. **subject** (str): Who is the primary subject of the disclosed data?
4. **recipient** (str): Who or what is the recipient of the information?
5. **transmit_data** (bool): Based on how {user_name} handles privacy in their past interactions, should this data be transmitted to the model? Block transmission for sensitive categories like banking credentials, health records, personal authentication, and private communications unless the user has a clear pattern of sharing such data.
6. **reasoning** (str): Brief explanation of your decision.

Response format:
{{
  "is_new_information": true,
  "data_type": "work_activity",
  "subject": "{user_name}",
  "recipient": "An AI model that generates inferences about the user to help in downstream tasks.",
  "transmit_data": true,
  "reasoning": "This observation shows typical work activity consistent with past interactions."
}}"""

import json
import logging
import re

from config import Config
from gemini_service import generate_planner_response


logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {
    "PUNCH_IN",
    "PUNCH_OUT",
    "GET_ATTENDANCE",
    "GET_LEAVE_BALANCE",
    "GET_LEAVE_HISTORY",
    "APPLY_LEAVE",
    "GET_EXPENSE_HISTORY",
    "APPLY_EXPENSE",
    "GET_PAYROLL",
    "GET_HR_SUMMARY",
    "GET_POLICY_ADVICE",
    "CANCEL_WORKFLOW",
    "CONTINUE_WORKFLOW",
    "CLOSE_CONVERSATION",
    "GENERAL_HR_QUERY",
}


def _parse_json(text):
    cleaned = str(text or "").strip().replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Planner did not return JSON")
    return json.loads(cleaned[start : end + 1])


def _normalise_action(action):
    action = re.sub(r"[^A-Z_]", "", str(action or "").upper())
    return action if action in ALLOWED_ACTIONS else "GENERAL_HR_QUERY"


def normalise_plan(raw):
    if not isinstance(raw, dict):
        return None
    raw_actions = raw.get("actions") or [raw.get("action")]
    actions = []
    for raw_action in raw_actions:
        action = _normalise_action(raw_action)
        if action not in actions:
            actions.append(action)
    if not actions:
        actions = ["GENERAL_HR_QUERY"]
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    return {
        "actions": actions[:3],
        "entities": {str(key): value for key, value in entities.items() if value not in (None, "", "UNKNOWN")},
        "reply_hint": str(raw.get("reply_hint") or "").strip(),
        "confidence": str(raw.get("confidence") or "medium").lower(),
    }


def plan_conversation(message, employee_context=None, active_workflow=None, last_topic=None):
    if not Config.GEMINI_PLANNER_ENABLED:
        return None
    employee_context = employee_context or {}
    active_workflow = active_workflow or {}
    prompt = f"""
You are the planning layer for an enterprise HR assistant. Understand natural language, spelling mistakes, shorthand, indirect requests, follow-up answers, and multiple actions in one message.

Return ONLY JSON using this exact shape:
{{
  "actions": ["ACTION"],
  "entities": {{"date_reference": "", "from_date": "", "to_date": "", "leave_type": "", "duration": "", "reason": "", "amount": "", "expense_type": "", "description": ""}},
  "reply_hint": "",
  "confidence": "high|medium|low"
}}

Allowed actions:
- PUNCH_IN, PUNCH_OUT, GET_ATTENDANCE
- GET_LEAVE_BALANCE, GET_LEAVE_HISTORY, APPLY_LEAVE
- GET_EXPENSE_HISTORY, APPLY_EXPENSE
- GET_PAYROLL, GET_HR_SUMMARY, GET_POLICY_ADVICE
- CANCEL_WORKFLOW, CONTINUE_WORKFLOW, CLOSE_CONVERSATION, GENERAL_HR_QUERY

Planning rules:
1. Prefer retrieval for questions. Do not start a workflow for "did I take leave", attendance logs, salary questions, expense status, or policy questions.
2. A message may require multiple actions. Example: "punch me in and apply leave tomorrow" means [PUNCH_IN, APPLY_LEAVE].
3. Active workflow context does not override a new attendance, history, payroll, policy, or other retrieval request.
4. Short follow-ups inherit the last topic. Example: after leave history, "in may?" means GET_LEAVE_HISTORY for May.
5. Sensitive situations, workplace harassment, manager-unavailable approvals, benefits, escalation, or "what should I do" are GET_POLICY_ADVICE unless the employee explicitly asks to submit an action.
6. "no", "nothing", "no thanks", and similar closing messages are CLOSE_CONVERSATION unless they clearly answer a required field.
7. Do not invent dates, policy rights, employee records, manager data, or contact details.

Employee context: {employee_context}
Last topic: {last_topic or 'none'}
Active workflow: {active_workflow or 'none'}
Employee message: {message}
"""
    response = generate_planner_response(prompt)
    if not response:
        return None
    try:
        return normalise_plan(_parse_json(response))
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Gemini planner response could not be parsed: %s", exc)
        return None

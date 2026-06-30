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
    "GET_TEAM_ATTENDANCE",
    "GET_ATTENDANCE_CORRECTIONS",
    "APPLY_ATTENDANCE_CORRECTION",
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

INTENT_ACTION_MAP = {
    "attendance_history": "GET_ATTENDANCE",
    "attendance_status": "GET_ATTENDANCE",
    "attendance_comparison": "GET_ATTENDANCE",
    "attendance_metric": "GET_ATTENDANCE",
    "manager_attendance": "GET_TEAM_ATTENDANCE",
    "team_attendance": "GET_TEAM_ATTENDANCE",
    "attendance_correction": "APPLY_ATTENDANCE_CORRECTION",
    "attendance_correction_status": "GET_ATTENDANCE_CORRECTIONS",
    "leave_balance": "GET_LEAVE_BALANCE",
    "leave_history": "GET_LEAVE_HISTORY",
    "apply_leave": "APPLY_LEAVE",
    "expense_history": "GET_EXPENSE_HISTORY",
    "apply_expense": "APPLY_EXPENSE",
    "payroll": "GET_PAYROLL",
    "hr_summary": "GET_HR_SUMMARY",
    "policy_advice": "GET_POLICY_ADVICE",
    "cancel_workflow": "CANCEL_WORKFLOW",
    "continue_workflow": "CONTINUE_WORKFLOW",
    "close_conversation": "CLOSE_CONVERSATION",
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


def _action_from_intent(intent):
    key = re.sub(r"[^a-z_]", "", str(intent or "").lower().replace(" ", "_"))
    return INTENT_ACTION_MAP.get(key)


def normalise_plan(raw):
    if not isinstance(raw, dict):
        return None
    raw_actions = raw.get("actions") if raw.get("actions") is not None else [raw.get("action")]
    intent_action = _action_from_intent(raw.get("intent"))
    if intent_action:
        raw_actions = [intent_action] + list(raw_actions or [])
    raw_actions = [action for action in raw_actions if action]
    actions = []
    for raw_action in raw_actions:
        action = _normalise_action(raw_action)
        if action not in actions:
            actions.append(action)
    if not actions:
        actions = ["GENERAL_HR_QUERY"]
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    structured_entity_keys = (
        "workflow_action",
        "date_phrase",
        "date_reference",
        "start_date",
        "end_date",
        "comparison_requested",
        "period_1_phrase",
        "period_2_phrase",
        "start_date_1",
        "end_date_1",
        "start_date_2",
        "end_date_2",
        "leave_type",
        "expense_category",
        "expense_type",
        "attendance_date",
        "punch_in",
        "punch_out",
        "correction_type",
        "reason",
        "other_entities",
    )
    merged_entities = dict(entities)
    for key in structured_entity_keys:
        if key in raw and raw.get(key) not in (None, "", "UNKNOWN"):
            merged_entities[key] = raw.get(key)
    return {
        "actions": actions[:3],
        "intent": str(raw.get("intent") or "").strip(),
        "workflow_action": str(raw.get("workflow_action") or "").strip(),
        "entities": {str(key): value for key, value in merged_entities.items() if value not in (None, "", "UNKNOWN")},
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

Return ONLY JSON using this exact shape. Keep the original phrase and normalized dates when you can infer them:
{{
  "intent": "attendance_history|attendance_correction|attendance_correction_status|manager_attendance|leave_balance|leave_history|apply_leave|expense_history|apply_expense|payroll|hr_summary|policy_advice|cancel_workflow|continue_workflow|close_conversation|general_hr_query",
  "workflow_action": "start|continue|confirm|cancel|retrieve|none",
  "date_phrase": "",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "comparison_requested": false,
  "period_1_phrase": "",
  "period_2_phrase": "",
  "start_date_1": "YYYY-MM-DD",
  "end_date_1": "YYYY-MM-DD",
  "start_date_2": "YYYY-MM-DD",
  "end_date_2": "YYYY-MM-DD",
  "leave_type": "",
  "expense_category": "",
  "other_entities": {{}},
  "actions": ["ACTION"],
  "entities": {{"date_reference": "", "from_date": "", "to_date": "", "leave_type": "", "duration": "", "reason": "", "amount": "", "expense_type": "", "description": "", "attendance_date": "", "punch_in": "", "punch_out": "", "correction_type": ""}},
  "reply_hint": "",
  "confidence": "high|medium|low"
}}

Allowed actions:
- PUNCH_IN, PUNCH_OUT, GET_ATTENDANCE
- GET_TEAM_ATTENDANCE, GET_ATTENDANCE_CORRECTIONS, APPLY_ATTENDANCE_CORRECTION
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
8. For attendance date phrases, normalize today, yesterday, day before yesterday, last weekdays, this/last week, this/last month, explicit dates, and explicit ranges into start_date/end_date.
9. "I forgot to punch in/out", "I forgot to mark my attendance last Thursday", "missed my attendance yesterday", "mark me present yesterday", "fix my attendance", and "I worked yesterday from 9:30 AM to 6:30 PM" are APPLY_ATTENDANCE_CORRECTION, not direct attendance updates.
10. Manager team queries such as "who is absent today", "who has not punched in", and "show my team's attendance" are GET_TEAM_ATTENDANCE.

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

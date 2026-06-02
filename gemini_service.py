import json

import google.generativeai as genai

from config import Config, require_config


require_config("GEMINI_API_KEY")

genai.configure(api_key=Config.GEMINI_API_KEY)
model = genai.GenerativeModel(Config.GEMINI_MODEL)

ALLOWED_INTENTS = {
    "PUNCH_IN",
    "PUNCH_OUT",
    "CHECK_LEAVE_BALANCE",
    "APPLY_LEAVE",
    "CONFIRM_LEAVE",
    "APPLY_EXPENSE",
    "CONFIRM_EXPENSE",
    "CANCEL_WORKFLOW",
    "GENERAL_HR_QUERY",
    "OUT_OF_SCOPE",
}

REQUIRED_FIELDS = {
    "intent": "OUT_OF_SCOPE",
    "leave_type": "UNKNOWN",
    "from_date": "UNKNOWN",
    "to_date": "UNKNOWN",
    "duration": "UNKNOWN",
    "reason": "UNKNOWN",
    "amount": "UNKNOWN",
    "expense_type": "UNKNOWN",
    "description": "UNKNOWN",
    "reply": "I can help with HR tasks like attendance, leave, approvals, payroll, and company policies.",
}


def _fallback_result(reply=None):
    result = REQUIRED_FIELDS.copy()
    if reply:
        result["reply"] = reply
    return result


def _extract_json(text):
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def _normalize_result(raw_result):
    if not isinstance(raw_result, dict):
        return _fallback_result("I could not understand that request. Please rephrase it as an HR task.")

    normalized = REQUIRED_FIELDS.copy()
    for field in REQUIRED_FIELDS:
        value = raw_result.get(field, REQUIRED_FIELDS[field])
        if value is None or str(value).strip() == "":
            value = "UNKNOWN" if field != "reply" else REQUIRED_FIELDS[field]
        normalized[field] = str(value).strip()

    intent = normalized["intent"].upper()
    if intent not in ALLOWED_INTENTS:
        intent = "OUT_OF_SCOPE"
    normalized["intent"] = intent
    if normalized["reply"].strip().lower() in {"unknown", "none", "null", "n/a"}:
        normalized["reply"] = REQUIRED_FIELDS["reply"]

    return normalized


def process_hr_request(user_message, employee_context=None, active_workflow=None):
    employee_context = employee_context or {}
    active_workflow = active_workflow or {}

    workflow_hint = "No active workflow."
    if active_workflow:
        workflow_hint = (
            f"Active workflow type: {active_workflow.get('workflow_type')}; "
            f"current step: {active_workflow.get('step')}; "
            f"known payload: {active_workflow.get('payload')}"
        )

    prompt = f"""
You are an enterprise AI HR assistant. Classify the employee message into a strict HR intent and extract entities.

Return ONLY valid JSON. Do not include markdown, comments, or explanations.

Enterprise rules:
1. Only support company HR topics and HR workflows.
2. Reject jokes, general knowledge, coding, medical/legal advice, and open-domain questions as OUT_OF_SCOPE.
3. Do not invent missing fields. Use UNKNOWN.
4. If an active workflow exists, interpret short replies in that workflow context.
5. If the user confirms an active leave workflow, use CONFIRM_LEAVE.
6. If the user confirms an active expense workflow, use CONFIRM_EXPENSE.
7. If the user cancels/stops an active workflow, use CANCEL_WORKFLOW.
8. Never return UNKNOWN in reply. If unsure, ask a clear HR follow-up question.
9. When the active workflow step is reason, treat the employee message as the leave reason.
10. For reimbursement, expense, claim, bill, receipt, or travel/food/hotel/software claim requests, use APPLY_EXPENSE.

Allowed intents:
- PUNCH_IN
- PUNCH_OUT
- CHECK_LEAVE_BALANCE
- APPLY_LEAVE
- CONFIRM_LEAVE
- APPLY_EXPENSE
- CONFIRM_EXPENSE
- CANCEL_WORKFLOW
- GENERAL_HR_QUERY
- OUT_OF_SCOPE

Return this exact JSON shape:
{{
  "intent": "",
  "leave_type": "",
  "from_date": "",
  "to_date": "",
  "duration": "",
  "reason": "",
  "amount": "",
  "expense_type": "",
  "description": "",
  "reply": ""
}}

Employee context:
{employee_context}

Workflow context:
{workflow_hint}

Employee message:
{user_message}
"""

    try:
        response = model.generate_content(prompt)
        raw_result = _extract_json(response.text)
        return _normalize_result(raw_result)
    except Exception:
        return _fallback_result("I could not process that safely. Please rephrase your HR request.")

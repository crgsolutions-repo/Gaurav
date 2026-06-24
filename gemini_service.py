import json
import logging
import time

from config import Config, require_config


require_config("GEMINI_API_KEY")

logger = logging.getLogger(__name__)

try:
    from google import genai as modern_genai
except ImportError:  # Transitional support until environments install google-genai.
    modern_genai = None

if modern_genai:
    from google.genai import types as modern_types

    client = modern_genai.Client(api_key=Config.GEMINI_API_KEY)
    model = None
    GEMINI_SDK = "google.genai"
else:
    import google.generativeai as legacy_genai

    legacy_genai.configure(api_key=Config.GEMINI_API_KEY)
    model = legacy_genai.GenerativeModel(Config.GEMINI_MODEL)
    client = None
    GEMINI_SDK = "google.generativeai"

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


def _usage_value(usage, *names):
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return value
    return 0


def _generate_text(prompt, purpose, response_mime_type=None):
    started = time.perf_counter()
    logger.info(
        "Gemini request started: purpose=%s model=%s sdk=%s",
        purpose,
        Config.GEMINI_MODEL,
        GEMINI_SDK,
    )
    try:
        if client:
            config = modern_types.GenerateContentConfig(response_mime_type=response_mime_type) if response_mime_type else None
            response = client.models.generate_content(model=Config.GEMINI_MODEL, contents=prompt, config=config)
        else:
            response = model.generate_content(
                prompt,
                request_options={"timeout": Config.GEMINI_TIMEOUT_SECONDS},
            )
        text = str(getattr(response, "text", "") or "").strip()
        usage = getattr(response, "usage_metadata", None)
        logger.info(
            "Gemini request completed: purpose=%s model=%s latency_ms=%d prompt_tokens=%s output_tokens=%s total_tokens=%s",
            purpose,
            Config.GEMINI_MODEL,
            round((time.perf_counter() - started) * 1000),
            _usage_value(usage, "prompt_token_count", "prompt_tokens"),
            _usage_value(usage, "candidates_token_count", "candidate_token_count", "output_tokens"),
            _usage_value(usage, "total_token_count", "total_tokens"),
        )
        if not text:
            raise ValueError("Gemini returned an empty response")
        return text
    except Exception:
        logger.exception(
            "Gemini request failed: purpose=%s model=%s latency_ms=%d",
            purpose,
            Config.GEMINI_MODEL,
            round((time.perf_counter() - started) * 1000),
        )
        raise


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
        raw_result = _extract_json(_generate_text(prompt, "intent_classification"))
        return _normalize_result(raw_result)
    except Exception:
        return _fallback_result("I could not process that safely. Please rephrase your HR request.")


def generate_copilot_response(user_message, employee_context=None, policy_context=""):
    employee_context = employee_context or {}
    grounded_context = policy_context.strip() or "No matching company policy was retrieved."
    prompt = f"""
You are a thoughtful enterprise HR copilot. Respond like an experienced, humane HR partner, not a form or policy search engine.

Behavior:
1. Answer the employee's actual question first.
2. For grief, harassment, illness, safety, or other sensitive situations, acknowledge the person before discussing process.
3. Do not assume that a life event automatically means the employee wants to submit leave or another request. Offer a relevant action gently.
4. Use only the supplied company-policy context for policy claims. If it is insufficient, say what you cannot verify.
5. Never invent approvals, manager availability, escalation rights, balances, records, deadlines, or reporting contacts.
   If `manager_id` is missing or `manager` is absent from Employee context, manager ownership and delegation data are not configured: do not say you can check them, and explain that verified routing data is unavailable.
6. Protect coworker privacy. You may discuss the employee's own data and minimal manager availability only when supplied in context.
7. Distinguish advice from action. Do not claim an HR action was performed unless the context says it was.
8. Keep the answer concise, natural, and specific. Ask at most one useful follow-up question.
9. For possible harassment or immediate danger, prioritize safety, preserve evidence, explain reporting options, and mention anti-retaliation protections when grounded by policy.
10. When policy sources are supplied, finish with a short 'Sources:' line listing their titles.

Employee context:
{employee_context}

Relevant company policy:
{grounded_context}

Employee message:
{user_message}
"""
    try:
        return _generate_text(prompt, "hr_copilot")
    except Exception:
        return None


def generate_planner_response(prompt):
    try:
        return _generate_text(prompt, "conversation_planner", response_mime_type="application/json")
    except Exception:
        return None

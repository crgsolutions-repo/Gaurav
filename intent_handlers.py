import re
from datetime import date, datetime, timedelta

from dateutil import parser as date_parser
from flask import jsonify

from supabase_client import supabase
from workflow_store import CANCELLED_STATUS, finish_workflow, get_active_workflow, upsert_workflow


UNKNOWN_VALUES = {"", "unknown", "none", "null", "not specified", "n/a"}
LEAVE_WORKFLOW = "leave_request"
TOMORROW_WORDS = {"tomorrow", "tommorow", "tomorow", "tmrw", "tmr"}
DAY_AFTER_TOMORROW_PATTERN = re.compile(
    r"\b(day\s+after\s+tom+m?or+ow|day\s+after\s+tmr?w?|overmorrow)\b"
)
DATE_EXPRESSION = (
    r"day\s+after\s+tom+m?or+ow|day\s+after\s+tmr?w?|overmorrow|"
    r"today|tom+m?or+ow|tmrw|tmr|"
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
)
DATE_RANGE_PATTERN = re.compile(
    rf"\b(?:from\s+)?(?P<start>{DATE_EXPRESSION})\s+(?:to|till|until|through)\s+(?P<end>{DATE_EXPRESSION})\b",
    re.IGNORECASE,
)


def json_reply(reply, status=200, **extra):
    payload = {"reply": reply}
    payload.update(extra)
    return jsonify(payload), status


def is_unknown(value):
    return value is None or str(value).strip().lower() in UNKNOWN_VALUES


def clean_value(value):
    return "UNKNOWN" if is_unknown(value) else str(value).strip()


def is_meaningful_reply(value):
    return not is_unknown(value)


def log_conversation(employee_id, user_message, bot_response):
    try:
        supabase.table("conversations").insert(
            {
                "employee_id": employee_id,
                "user_message": user_message,
                "bot_response": bot_response,
            }
        ).execute()
    except Exception:
        pass


def normalize_duration(duration):
    if is_unknown(duration):
        return None

    lowered = str(duration).lower()
    if "half" in lowered:
        return "Half Day"
    if "full" in lowered or "whole day" in lowered or re.search(r"\b(1|one)\s+day\b", lowered):
        return "Full Day"
    return None


def parse_hr_date(value):
    if is_unknown(value):
        return None

    text = str(value).strip().lower()
    today = date.today()

    if text == "today":
        return today
    if DAY_AFTER_TOMORROW_PATTERN.search(text):
        return today + timedelta(days=2)
    if text in TOMORROW_WORDS:
        return today + timedelta(days=1)

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    try:
        return date_parser.parse(text, fuzzy=True, dayfirst=True).date()
    except (ValueError, TypeError, OverflowError):
        return None


def infer_date_from_message(message):
    lowered = str(message or "").lower()
    words = set(re.findall(r"[a-z]+", lowered))

    if "today" in words:
        return date.today()
    if DAY_AFTER_TOMORROW_PATTERN.search(lowered):
        return date.today() + timedelta(days=2)
    if words & TOMORROW_WORDS:
        return date.today() + timedelta(days=1)

    date_match = re.search(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", lowered)
    if date_match:
        return parse_hr_date(date_match.group(0))

    iso_match = re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", lowered)
    if iso_match:
        return parse_hr_date(iso_match.group(0))

    return None


def infer_date_range_from_message(message):
    match = DATE_RANGE_PATTERN.search(str(message or ""))
    if not match:
        return None, None

    return parse_hr_date(match.group("start")), parse_hr_date(match.group("end"))


def infer_requested_days(message):
    match = re.search(r"\b(\d+)\s+(?:working\s+)?days?\b", str(message or "").lower())
    if not match:
        return None

    requested_days = int(match.group(1))
    return requested_days if requested_days > 0 else None


def get_leave_balances(employee_id):
    response = (
        supabase.table("employee_leave_balance")
        .select("*")
        .eq("employee_id", employee_id)
        .execute()
    )
    return response.data or []


def normalize_leave_type(leave_type, allowed_types):
    if is_unknown(leave_type):
        return None

    aliases = {
        "casual": "Casual Leave",
        "cl": "Casual Leave",
        "privilege": "Privilege Leave",
        "pl": "Privilege Leave",
        "sick": "Sick Leave",
        "sl": "Sick Leave",
        "maternity": "Maternity Leave",
        "paternity": "Paternity Leave",
    }

    cleaned = str(leave_type).strip().lower()
    candidate = aliases.get(cleaned, str(leave_type).strip())
    for allowed in allowed_types:
        if candidate.lower() == allowed.lower():
            return allowed
        if cleaned == allowed.lower().replace(" leave", ""):
            return allowed

    return None


def infer_leave_type_from_message(message):
    lowered = str(message or "").lower()
    aliases = {
        "casual": "Casual Leave",
        "cl": "Casual Leave",
        "privilege": "Privilege Leave",
        "pl": "Privilege Leave",
        "sick": "Sick Leave",
        "sl": "Sick Leave",
        "maternity": "Maternity Leave",
        "paternity": "Paternity Leave",
        "bereavement": "Bereavement Leave",
        "marriage": "Marriage Leave",
        "optional holiday": "Optional Holidays",
        "public holiday": "Public Holidays",
        "comp off": "Compensatory Off",
        "compensatory": "Compensatory Off",
        "unpaid": "Unpaid Leave",
        "adoption": "Adoption Leave",
        "miscarriage": "Miscarriage Leave",
    }

    for alias, leave_type in aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return leave_type

    return None


def merge_message_entities(payload, user_message):
    message = str(user_message or "").strip()
    lowered = message.lower()

    inferred_leave_type = infer_leave_type_from_message(message)
    if inferred_leave_type:
        payload["leave_type"] = inferred_leave_type

    requested_days = infer_requested_days(message)
    if requested_days:
        payload["requested_days"] = str(requested_days)
        if requested_days > 1:
            payload["duration"] = "Full Day"

    range_start, range_end = infer_date_range_from_message(message)
    if range_start or range_end:
        if range_start:
            payload["from_date"] = range_start.isoformat()
        if range_end:
            payload["to_date"] = range_end.isoformat()
        if range_start and range_end and range_start != range_end:
            payload["duration"] = "Full Day"
    else:
        inferred_date = infer_date_from_message(lowered)
        if inferred_date:
            formatted_date = inferred_date.isoformat()
            payload["from_date"] = formatted_date
            if requested_days and requested_days > 1:
                payload["to_date"] = (inferred_date + timedelta(days=requested_days - 1)).isoformat()
            elif not payload.get("requested_days"):
                payload["to_date"] = formatted_date

    if payload.get("requested_days") and not is_unknown(payload.get("from_date")):
        requested_days = int(payload["requested_days"])
        from_date = parse_hr_date(payload.get("from_date"))
        if from_date and requested_days > 1:
            payload["to_date"] = (from_date + timedelta(days=requested_days - 1)).isoformat()
            payload["duration"] = "Full Day"

    duration = normalize_duration(message)
    if duration:
        payload["duration"] = duration

    return payload


def apply_start_date_to_payload(payload, message):
    inferred_date = infer_date_from_message(message)
    if inferred_date:
        payload["from_date"] = inferred_date.isoformat()
        requested_days = payload.get("requested_days")
        if requested_days and int(requested_days) > 1:
            payload["to_date"] = (inferred_date + timedelta(days=int(requested_days) - 1)).isoformat()
            payload["duration"] = "Full Day"
        else:
            payload["to_date"] = inferred_date.isoformat()
    else:
        payload["from_date"] = message
        payload["to_date"] = message

    return payload


def calculate_leave_days(from_date, to_date, duration):
    if duration == "Half Day":
        return 0.5
    return (to_date - from_date).days + 1


def format_leave_amount(value):
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def format_leave_balances(balances):
    lines = ["Available leave balances:"]
    for leave in balances:
        lines.append(f"{leave['leave_type']}: {format_leave_amount(leave.get('remaining_leaves', 0))}")
    return "\n".join(lines)


def insufficient_balance_message(leave_type, requested_days, remaining, balances):
    requested = format_leave_amount(requested_days)
    available = format_leave_amount(remaining)
    return (
        f"You requested {requested} days of {leave_type} but only {available} days are available.\n\n"
        f"{format_leave_balances(balances)}\n\n"
        "Please reduce the duration or choose another leave type."
    )


def find_overlapping_leave(employee_id, from_date, to_date):
    response = (
        supabase.table("leave_requests")
        .select("*")
        .eq("employee_id", employee_id)
        .execute()
    )

    for leave in response.data or []:
        status = str(leave.get("status", "")).lower()
        if status not in {"pending", "approved"}:
            continue

        existing_from = parse_hr_date(leave.get("from_date"))
        existing_to = parse_hr_date(leave.get("to_date")) or existing_from
        if not existing_from or not existing_to:
            continue

        if existing_from <= to_date and from_date <= existing_to:
            return leave

    return None


def validate_leave_payload(employee_id, payload, require_reason=True):
    balances = get_leave_balances(employee_id)
    allowed_types = [row["leave_type"] for row in balances if row.get("leave_type")]

    leave_type = normalize_leave_type(payload.get("leave_type"), allowed_types)
    from_date = parse_hr_date(payload.get("from_date"))
    to_date = parse_hr_date(payload.get("to_date")) or from_date
    duration = normalize_duration(payload.get("duration"))
    reason = clean_value(payload.get("reason"))

    if not allowed_types:
        return None, "No leave balances are configured for your employee profile."
    if not leave_type:
        return None, (
            "Please choose a valid leave type from your available leave balances: "
            f"{', '.join(allowed_types)}."
        )
    if not from_date:
        return None, "Please provide a valid leave start date."
    if from_date < date.today():
        return None, "Leave cannot be applied for a past date."
    if not to_date:
        return None, "Please provide a valid leave end date."
    if to_date < from_date:
        return None, "Leave end date cannot be before the start date."
    if not duration:
        return None, "Please specify whether this is a Full Day or Half Day leave."
    if duration == "Half Day" and from_date != to_date:
        return None, "Half Day leave can only be applied for a single date."
    if require_reason and is_unknown(reason):
        return None, "Please provide a short reason for the leave request."

    deduction = calculate_leave_days(from_date, to_date, duration)
    balance_row = next(
        (row for row in balances if str(row.get("leave_type", "")).lower() == leave_type.lower()),
        None,
    )
    remaining = float(balance_row.get("remaining_leaves", 0))
    if remaining < deduction:
        return None, insufficient_balance_message(leave_type, deduction, remaining, balances)

    if find_overlapping_leave(employee_id, from_date, to_date):
        return None, "You already have a pending or approved leave request for these dates."

    return {
        "leave_type": leave_type,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "duration": duration,
        "reason": reason,
        "deduction": deduction,
    }, None


def merge_leave_payload(existing_payload, ai_result):
    payload = dict(existing_payload or {})
    for field in ("leave_type", "from_date", "to_date", "duration", "reason", "requested_days"):
        value = ai_result.get(field)
        if not is_unknown(value):
            payload[field] = str(value).strip()

    if not payload.get("to_date") and payload.get("from_date"):
        payload["to_date"] = payload["from_date"]

    return payload


def merge_workflow_step_reply(payload, current_step, user_message):
    message = str(user_message or "").strip()
    if is_unknown(message):
        return payload

    if current_step == "leave_type" and is_unknown(payload.get("leave_type")):
        payload["leave_type"] = message
    elif current_step in {"from_date", "to_date"}:
        range_start, range_end = infer_date_range_from_message(message)
        if range_start or range_end:
            if range_start:
                payload["from_date"] = range_start.isoformat()
            if range_end:
                payload["to_date"] = range_end.isoformat()
            if range_start and range_end and range_start != range_end:
                payload["duration"] = "Full Day"
        elif current_step == "to_date":
            inferred_date = infer_date_from_message(message)
            payload["to_date"] = inferred_date.isoformat() if inferred_date else message
        else:
            payload = apply_start_date_to_payload(payload, message)
    elif current_step == "duration" and is_unknown(payload.get("duration")):
        duration = normalize_duration(message)
        if duration:
            payload["duration"] = duration
    elif current_step == "reason" and is_unknown(payload.get("reason")):
        payload["reason"] = message

    return payload


def validation_failure_step(error):
    lowered = str(error or "").lower()
    if "valid leave type" in lowered:
        return "leave_type"
    if (
        "start date" in lowered
        or "past date" in lowered
        or "end date" in lowered
        or "pending or approved leave request" in lowered
    ):
        return "from_date"
    if "full day" in lowered or "half day" in lowered:
        return "duration"
    if "reason" in lowered:
        return "reason"
    return None


def reset_failed_payload_step(payload, failed_step):
    if not failed_step:
        return payload

    payload[failed_step] = "UNKNOWN"
    if failed_step == "from_date":
        payload["to_date"] = "UNKNOWN"
    return payload


def validate_known_leave_type(employee_id, payload):
    if is_unknown(payload.get("leave_type")):
        return payload, None

    balances = get_leave_balances(employee_id)
    allowed_types = [row["leave_type"] for row in balances if row.get("leave_type")]
    leave_type = normalize_leave_type(payload.get("leave_type"), allowed_types)

    if leave_type:
        payload["leave_type"] = leave_type
        return payload, None

    payload["leave_type"] = "UNKNOWN"
    if not allowed_types:
        return payload, "No leave balances are configured for your employee profile."

    return payload, (
        "Please choose a valid leave type from your available leave balances: "
        f"{', '.join(allowed_types)}."
    )


def next_leave_step(payload):
    if is_unknown(payload.get("leave_type")):
        return "leave_type"
    if is_unknown(payload.get("from_date")):
        return "from_date"
    if is_unknown(payload.get("duration")):
        return "duration"
    if is_unknown(payload.get("reason")):
        return "reason"
    return "confirm"


def leave_step_prompt(step):
    prompts = {
        "leave_type": "Which leave type would you like to apply for?",
        "from_date": "For which date or date range should I apply the leave?",
        "to_date": "What is the leave end date?",
        "duration": "Is this a Full Day or Half Day leave?",
        "reason": "Please provide a short reason for this leave request.",
    }
    return prompts.get(step, "Please confirm if you want me to submit this leave request.")


def format_leave_confirmation(employee_name, payload):
    leave_type = clean_value(payload.get("leave_type"))
    from_date = clean_value(payload.get("from_date"))
    to_date = clean_value(payload.get("to_date")) or from_date
    duration = clean_value(payload.get("duration"))
    reason = clean_value(payload.get("reason"))

    return (
        f"{employee_name}, please confirm this leave request:\n"
        f"Leave Type: {leave_type}\n"
        f"From: {from_date}\n"
        f"To: {to_date}\n"
        f"Duration: {duration}\n"
        f"Reason: {reason}\n\n"
        "Reply with confirm to submit, or cancel to discard it."
    )


def handle_punch_in(employee_id, employee_name, ai_result):
    today = date.today().isoformat()
    existing_record = (
        supabase.table("attendance")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("date", today)
        .execute()
    )

    if existing_record.data:
        return json_reply(f"{employee_name}, you already punched in today.")

    current_time = datetime.now().time().strftime("%H:%M:%S")
    supabase.table("attendance").insert(
        {
            "employee_id": employee_id,
            "date": today,
            "punch_in": current_time,
            "status": "Present",
        }
    ).execute()

    return json_reply(f"{employee_name}, your punch in has been recorded.")


def handle_punch_out(employee_id, employee_name, ai_result):
    today = date.today().isoformat()
    existing_record = (
        supabase.table("attendance")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("date", today)
        .execute()
    )

    if not existing_record.data:
        return json_reply(f"{employee_name}, you have not punched in today.")

    if existing_record.data[0].get("punch_out"):
        return json_reply(f"{employee_name}, you already punched out today.")

    current_time = datetime.now().time().strftime("%H:%M:%S")
    attendance_id = existing_record.data[0]["id"]
    supabase.table("attendance").update({"punch_out": current_time}).eq("id", attendance_id).execute()

    return json_reply(f"{employee_name}, your punch out has been recorded.")


def handle_leave_balance(employee_id, employee_name):
    balances = get_leave_balances(employee_id)
    if not balances:
        return json_reply(f"{employee_name}, no leave balances are configured for your profile.")

    lines = [f"{employee_name}, here is your leave balance:"]
    for leave in balances:
        lines.append(
            f"{leave['leave_type']}: {leave['remaining_leaves']} remaining, "
            f"{leave.get('used_leaves', 0)} used"
        )

    return json_reply("\n".join(lines))


def handle_apply_leave(employee_id, employee_name, ai_result, user_message=""):
    workflow = get_active_workflow(employee_id, LEAVE_WORKFLOW)
    current_step = workflow.get("step") if workflow else None
    payload = merge_leave_payload(workflow.get("payload") if workflow else {}, ai_result)
    if current_step != "reason":
        payload = merge_message_entities(payload, user_message)
    payload = merge_workflow_step_reply(payload, current_step, user_message)
    payload, leave_type_error = validate_known_leave_type(employee_id, payload)
    if leave_type_error:
        upsert_workflow(employee_id, LEAVE_WORKFLOW, "leave_type", payload)
        return json_reply(leave_type_error)

    step = next_leave_step(payload)
    workflow = upsert_workflow(employee_id, LEAVE_WORKFLOW, step, payload)

    if step != "confirm":
        return json_reply(leave_step_prompt(step))

    valid_payload, error = validate_leave_payload(employee_id, payload)
    if error:
        failed_step = validation_failure_step(error)
        if failed_step:
            payload = reset_failed_payload_step(payload, failed_step)
            upsert_workflow(employee_id, LEAVE_WORKFLOW, failed_step, payload)
        return json_reply(error)

    workflow = upsert_workflow(employee_id, LEAVE_WORKFLOW, "confirm", valid_payload)
    return json_reply(format_leave_confirmation(employee_name, workflow["payload"]))


def handle_confirm_leave(employee_id, employee_name):
    workflow = get_active_workflow(employee_id, LEAVE_WORKFLOW)
    if not workflow:
        return json_reply("No active leave request is waiting for confirmation.")

    payload = workflow.get("payload") or {}
    step = next_leave_step(payload)
    if step != "confirm":
        upsert_workflow(employee_id, LEAVE_WORKFLOW, step, payload)
        return json_reply(leave_step_prompt(step))

    valid_payload, error = validate_leave_payload(employee_id, payload)
    if error:
        failed_step = validation_failure_step(error)
        if failed_step:
            payload = reset_failed_payload_step(payload, failed_step)
            upsert_workflow(employee_id, LEAVE_WORKFLOW, failed_step, payload)
        return json_reply(error)

    supabase.table("leave_requests").insert(
        {
            "employee_id": employee_id,
            "leave_type": valid_payload["leave_type"],
            "from_date": valid_payload["from_date"],
            "to_date": valid_payload["to_date"],
            "leave_duration": valid_payload["duration"],
            "reason": valid_payload["reason"],
            "status": "Pending",
        }
    ).execute()

    finish_workflow(workflow["id"])
    return json_reply(f"{employee_name}, your leave request has been submitted for manager approval.")


def handle_cancel_workflow(employee_id):
    workflow = get_active_workflow(employee_id)
    if not workflow:
        return json_reply("There is no active workflow to cancel.")

    finish_workflow(workflow["id"], CANCELLED_STATUS)
    return json_reply("I cancelled the active workflow.")


def handle_general_hr_query(ai_result):
    reply = ai_result.get("reply") if is_meaningful_reply(ai_result.get("reply")) else ""
    reply = reply or (
        "I can help with HR tasks such as attendance, leave balance, leave requests, "
        "approvals, payroll, and company policies."
    )
    return json_reply(reply)

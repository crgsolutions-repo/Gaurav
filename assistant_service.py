import re
from calendar import monthrange
from collections import Counter
from datetime import date, datetime, timedelta

from dateutil import parser as date_parser
from flask import has_request_context, jsonify, session

import attendance_service as attendance_module
from config import Config
from intent_handlers import calculate_leave_days, format_leave_amount, parse_hr_date
from payroll_service import (
    get_employee_salary_records,
    money as payroll_money,
    month_display,
    payslip_parts,
    salary_month_date,
)
from supabase_client import supabase


MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def json_reply(reply, status=200):
    return jsonify({"reply": reply}), status


def normalized(message):
    return re.sub(r"\s+", " ", str(message or "").strip().lower())


def is_global_cancel_message(message):
    text = normalized(message)
    if not text:
        return False
    exact = {
        "cancel",
        "no",
        "cancel it",
        "stop",
        "stop it",
        "discard",
        "discard it",
        "abort",
        "abort it",
        "never mind",
        "nevermind",
        "leave it",
        "forget it",
        "drop it",
        "nothing else",
        "nothing else thanks",
        "nothing",
        "no thanks",
        "no thank you",
        "not now",
        "maybe later",
        "that's all",
        "thats all",
        "thanks",
        "thank you",
        "leave it",
        "ignore that",
        "skip it",
        "not interested",
        "no leave",
        "no reimbursement",
        "no expense claim",
    }
    if text in exact:
        return True
    return any(
        phrase in text
        for phrase in (
            "cancel this",
            "cancel the request",
            "cancel my request",
            "stop this",
            "forget this",
            "forget the request",
            "leave this request",
            "i changed my mind",
            "my plans changed",
            "i won't be taking leave",
            "i wont be taking leave",
            "i don't need this anymore",
            "i dont need this anymore",
            "never mind that",
            "ignore that",
            "let's skip it",
            "lets skip it",
            "nothing else thanks",
            "thanks, i'm good",
            "thanks i am good",
            "thanks im good",
            "no thanks",
        )
    )


def is_explicit_resume_message(message):
    text = normalized(message)
    return text in {
        "continue",
        "resume",
        "continue it",
        "resume it",
        "continue leave request",
        "resume leave request",
        "continue my leave request",
        "continue reimbursement request",
        "resume reimbursement request",
        "continue expense request",
        "resume expense request",
        "continue my expense request",
    }


def attendance_action(message):
    text = normalized(message)
    if re.search(r"\b(how|explain|process|guide|steps|policy)\b", text):
        return None
    correction_phrases = (
        "forgot to punch in",
        "forgot punch in",
        "forgot to punch out",
        "forgot punch out",
        "forgot to mark attendance",
        "forgot to mark my attendance",
        "forgot mark attendance",
        "forgot mark my attendance",
        "missed attendance",
        "missed my attendance",
        "attendance correction",
        "correct my attendance",
        "fix my attendance",
        "attendance is incorrect",
        "incorrect attendance",
        "worked yesterday from",
        "i worked yesterday from",
    )
    if any(phrase in text for phrase in correction_phrases):
        return None
    if any(
        phrase in text
        for phrase in (
            "punch in time",
            "punch-in time",
            "punch out time",
            "punch-out time",
            "make punch in",
            "make punch out",
            "set punch in",
            "set punch out",
            "change punch in",
            "change punch out",
        )
    ):
        return None
    status_words = (
        "did you",
        "did i",
        "have you",
        "have i",
        "am i",
        "was i",
        "status",
        "already",
    )
    if any(text.startswith(prefix) for prefix in status_words):
        return None
    if "mark attendance" in text and any(token in text for token in ("yesterday", "day before", "last ", " for ", " on ")):
        return None
    if "mark me present" in text and any(token in text for token in ("yesterday", "day before", "last ", " for ", " on ")):
        return None
    if text in {"present", "i am present", "im present", "i am in office", "i am at office"}:
        return "PUNCH_IN"
    if any(
        phrase in text
        for phrase in (
            "punch me in",
            "punch in",
            "clock me in",
            "clock in",
            "check me in",
            "check in",
            "start my day",
            "start my shift",
            "mark attendance",
            "mark my attendance",
            "mark me present today",
            "mark present today",
            "present today",
        )
    ):
        return "PUNCH_IN"
    if any(
        phrase in text
        for phrase in (
            "punch me out",
            "punch out",
            "clock me out",
            "clock out",
            "check me out",
            "check out",
            "end my day",
            "end my shift",
            "leaving office",
            "leaving office now",
            "leaving now",
            "leaving for the day",
            "done for the day",
            "done with work",
        )
    ):
        return "PUNCH_OUT"
    return None


def is_retrieval_like_message(message):
    text = normalized(message)
    return any(
        phrase in text
        for phrase in (
            "show",
            "view",
            "list",
            "history",
            "status",
            "latest",
            "last",
            "most recent",
            "what was",
            "how many",
            "balance",
            "download",
            "compare",
            "explain",
            "how do",
            "should i",
            "will my manager",
            "policy",
            "recommend",
            "worth",
            "did i take",
            "have i taken",
            "leave taken",
        )
    )


def is_switch_confirmation(message):
    text = normalized(message)
    return text in {
        "switch",
        "switch it",
        "switch context",
        "switch to leave",
        "switch to reimbursement",
        "switch to expense",
        "start reimbursement",
        "start expense",
        "start leave",
        "yes switch",
    }


def is_expense_start_message(message):
    text = normalized(message)
    phrase_match = any(
        phrase in text
        for phrase in (
            "submit expense",
            "add expense",
            "expense claim",
            "expense to claim",
            "reimbursement",
            "need to claim",
            "claim expenses",
            "claim reimbursement",
            "bill to claim",
            "claim a bill",
            "claim this bill",
            "spent money",
            "spent on",
            "paid for",
            "paid money",
            "bought",
            "purchased",
            "travel expense",
            "food expense",
            "upload bill",
            "upload receipt",
            "have a bill",
            "i have a bill",
            "submit a receipt",
            "expense approval",
            "company reimburse",
            "reimburse this",
            "client dinner",
            "travel bill",
        )
    )
    semantic_bill_match = re.search(r"\b(?:have|got|submit|claim)\b.{0,30}\b(?:bill|receipt|expense)\b", text)
    return phrase_match or semantic_bill_match is not None


def is_leave_start_message(message):
    text = normalized(message)
    if is_leave_history_query(message):
        return False
    phrase_match = any(
        phrase in text
        for phrase in (
            "apply leave",
            "need leave",
            "want leave",
            "take leave",
            "leave tomorrow",
            "personal leave",
            "emergency leave",
            "holiday",
            "time off",
            "day off",
            "taking tomorrow off",
            "won't be coming",
            "wont be coming",
            "not coming tomorrow",
            "not available tomorrow",
            "absent tomorrow",
            "make it tomorrow",
            "vacation",
            "long vacation",
            "break tomorrow",
            "days off",
        )
    )
    semantic_leave_match = re.search(
        r"\b(?:need|want|apply|take|request|planning)\b.{0,35}\b(?:leave|holiday|vacation|time off|day off)\b",
        text,
    )
    return phrase_match or semantic_leave_match is not None


def workflow_switch_target(active_workflow_type, message):
    if is_retrieval_like_message(message):
        return None
    if active_workflow_type == "leave_request" and is_expense_start_message(message):
        return "expense_request"
    if active_workflow_type == "expense_request" and is_leave_start_message(message):
        return "leave_request"
    return None


def workflow_switch_prompt(active_workflow_type, target_workflow_type, message):
    if target_workflow_type == "expense_request":
        return (
            "It sounds like you would like to submit an expense claim.\n\n"
            "Reimbursements go to your manager for approval. If the amount is above INR 200, a receipt is required for OCR validation.\n\n"
            "You already have a leave request in progress. Would you like to switch to reimbursement or continue the leave request?\n"
            "Reply switch to reimbursement, or continue leave request."
        )
    if target_workflow_type == "leave_request":
        return (
            "It sounds like you would like to apply for leave.\n\n"
            "I can help prepare the leave request, check your balance, and send it for manager approval.\n\n"
            "You already have an expense claim in progress. Would you like to switch to leave or continue the reimbursement request?\n"
            "Reply switch to leave, or continue reimbursement request."
        )
    return ""


def rupees(value):
    number = float(value or 0)
    return f"INR {number:,.2f}"


def fetch_rows(table_name, employee_id):
    response = supabase.table(table_name).select("*").eq("employee_id", employee_id).execute()
    return response.data or []


def copilot_employee_context(employee_id, employee_name):
    expenses = fetch_rows("expenses", employee_id)
    leaves = fetch_rows("leave_requests", employee_id)
    context = {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "pending_expenses": [
            {
                "id": row.get("id"),
                "amount": row.get("amount"),
                "expense_type": row.get("expense_type"),
                "status": row.get("status"),
            }
            for row in expenses
            if str(row.get("status", "")).lower() == "pending"
        ],
        "pending_leave_requests": [
            {
                "id": row.get("id"),
                "leave_type": row.get("leave_type"),
                "from_date": row.get("from_date"),
                "to_date": row.get("to_date"),
                "status": row.get("status"),
            }
            for row in leaves
            if str(row.get("status", "")).lower() == "pending"
        ],
    }

    try:
        employee_response = (
            supabase.table("employees").select("*").eq("employee_id", employee_id).limit(1).execute()
        )
        employee = (employee_response.data or [{}])[0]
        manager_id = employee.get("manager_id")
        context["manager_id"] = manager_id
        if manager_id:
            manager_response = (
                supabase.table("employees").select("employee_id,name").eq("employee_id", manager_id).limit(1).execute()
            )
            manager = (manager_response.data or [{}])[0]
            manager_leaves = fetch_rows("leave_requests", manager_id)
            manager_on_leave = any(
                str(row.get("status", "")).lower() == "approved"
                and (parse_hr_date(row.get("from_date")) or date.max) <= date.today()
                and (parse_hr_date(row.get("to_date")) or parse_hr_date(row.get("from_date")) or date.min) >= date.today()
                for row in manager_leaves
            )
            context["manager"] = {
                "employee_id": manager_id,
                "name": manager.get("name") or "Manager",
                "availability": "on approved leave" if manager_on_leave else "no approved leave found today",
            }
    except Exception:
        context["manager_id"] = None
    return context


def row_date(row, *fields):
    for field in fields:
        parsed = parse_hr_date(row.get(field))
        if parsed:
            return parsed
    return date.min


def parse_natural_date(message):
    text = str(message or "")
    parsed = parse_hr_date(text)
    if parsed:
        return parsed
    try:
        parsed = date_parser.parse(text, fuzzy=True, dayfirst=True, default=datetime(date.today().year, 1, 1)).date()
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.year == 1900:
        return parsed.replace(year=date.today().year)
    return parsed


def attendance_month_range(message):
    text = normalized(message)
    today = date.today()
    if "last month" in text or "previous month" in text:
        first_this_month = today.replace(day=1)
        previous_month_end = first_this_month - timedelta(days=1)
        start = previous_month_end.replace(day=1)
        return start, previous_month_end
    if "this month" in text or "current month" in text:
        return today.replace(day=1), today

    for name, month in MONTH_NAMES.items():
        if re.search(rf"\b{name}\b", text):
            year_match = re.search(r"\b(20\d{2})\b", text)
            year = int(year_match.group(1)) if year_match else today.year
            start = date(year, month, 1)
            end = date(year, month, monthrange(year, month)[1])
            if year == today.year and month == today.month:
                end = min(end, today)
            return start, end

    if "attendance" in text and "history" not in text and "range" not in text:
        return today.replace(day=1), today
    return today - timedelta(days=14), today


def named_month_ranges(message):
    text = normalized(message)
    today = date.today()
    ranges = []

    for name, month in MONTH_NAMES.items():
        for match in re.finditer(rf"\b{name}\b", text):
            year_match = re.search(rf"\b{name}\s+(20\d{{2}})\b", text[match.start():])
            year = int(year_match.group(1)) if year_match else today.year
            start = date(year, month, 1)
            end = date(year, month, monthrange(year, month)[1])
            if year == today.year and month == today.month:
                end = min(end, today)
            ranges.append((match.start(), start.strftime("%B %Y"), start, end))

    this_match = re.search(r"\b(this|current) month\b", text)
    if this_match:
        ranges.append((this_match.start(), today.strftime("%B %Y"), today.replace(day=1), today))

    last_match = re.search(r"\b(last|previous) month\b", text)
    if last_match:
        first_this_month = today.replace(day=1)
        previous_month_end = first_this_month - timedelta(days=1)
        start = previous_month_end.replace(day=1)
        ranges.append((last_match.start(), start.strftime("%B %Y"), start, previous_month_end))

    ordered = []
    seen = set()
    for _pos, label, start, end in sorted(ranges, key=lambda item: item[0]):
        key = (start.year, start.month)
        if key not in seen:
            ordered.append((label, start, end))
            seen.add(key)
    return ordered


def attendance_specific_date(message):
    text = normalized(message)
    if "yesterday" in text:
        return date.today() - timedelta(days=1)
    if "today" in text:
        return date.today()
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", text):
        return parse_natural_date(message)
    month_pattern = "|".join(MONTH_NAMES)
    if re.search(rf"\b\d{{1,2}}\s+({month_pattern})\b|\b({month_pattern})\s+\d{{1,2}}\b", text):
        return parse_natural_date(message)
    return None


def is_conversation_close_message(message):
    text = normalized(message)
    return text in {
        "no",
        "nope",
        "nothing",
        "nothing else",
        "no thanks",
        "no thank you",
        "no nothing thank you",
        "no nothing thanks",
        "thanks",
        "thank you",
        "ok thank you",
        "okay thank you",
    }


def is_hr_contact_message(message):
    text = normalized(message)
    return ("contact hr" in text or "reach hr" in text or "talk to hr" in text) and any(
        word in text for word in ("how", "mail", "email", "message", "channel", "contact")
    )


def hr_contact_response():
    contacts = []
    if Config.HR_CONTACT_EMAIL:
        contacts.append(f"Email: {Config.HR_CONTACT_EMAIL}")
    if Config.HR_CONTACT_CHANNEL:
        contacts.append(f"Internal channel: {Config.HR_CONTACT_CHANNEL}")
    if contacts:
        return "You can contact HR through:\n" + "\n".join(contacts)
    return (
        "I do not have verified HR contact details configured for your company, so I do not want to invent an address or channel.\n\n"
        "Please use your company directory, HR portal, or internal communication tool to find the official HR contact. "
        "For an urgent safety concern, contact workplace security or local emergency services first."
    )


def status_filter(message):
    text = normalized(message)
    for status in ("pending", "approved", "rejected"):
        if status in text:
            return status
    return None


def help_response():
    return (
        "I can help you with:\n"
        "- Attendance\n"
        "- Leave Management\n"
        "- Leave Balances\n"
        "- Leave History\n"
        "- Expense Reimbursements\n"
        "- Expense History\n"
        "- Payroll\n"
        "- Payslips\n"
        "- Salary History\n"
        "- Attendance History\n"
        "- HR Summary and recommendations\n\n"
        "Examples:\n"
        "- Apply leave tomorrow\n"
        "- Show my leave balance\n"
        "- Show my leave requests\n"
        "- Submit reimbursement\n"
        "- Show my expense claims\n"
        "- Show my attendance history\n"
        "- Generate payslip\n"
        "- Compare salary with last month\n"
        "- Show my HR summary"
    )


def leave_process_response():
    return (
        "To apply for leave:\n"
        "1. Tell me the leave type.\n"
        "2. Provide the date or date range.\n"
        "3. Specify Full Day or Half Day if needed.\n"
        "4. Provide a short reason.\n"
        "5. Review the summary.\n"
        "6. Confirm the request.\n\n"
        "You can start by saying: I want to apply for leave."
    )


def expense_process_response():
    return (
        "To submit an expense reimbursement:\n"
        "1. Enter the expense amount.\n"
        "2. Select a category: Travel, Food, Accommodation, or Software / Tools.\n"
        "3. Provide a short description.\n"
        "4. If the amount exceeds INR 200, upload a receipt.\n"
        "5. OCR will validate the receipt where possible.\n"
        "6. The request will be sent to your manager for approval.\n"
        "7. Once approved, it can be added to payroll.\n\n"
        "You can start by saying: I want to submit an expense."
    )


def payslip_process_response():
    return (
        "To view or download your payslip:\n"
        "1. Ask for your latest payslip or a specific month.\n"
        "2. I will fetch your salary record from payroll.\n"
        "3. Use the View link to open it in the dashboard.\n"
        "4. Use the Download PDF link to download it.\n\n"
        "Try: Download my payslip for May."
    )


def attendance_process_response():
    return (
        "For today's attendance:\n"
        "1. Say: Punch me in, Check me in, Start my day, or Mark my attendance.\n"
        "2. To end the day, say: Punch me out or Check me out.\n\n"
        "To view attendance:\n"
        "1. Ask for attendance history, this month's attendance, or a specific date.\n"
        "2. I will check attendance records and approved leave records.\n"
        "3. I will show whether you were Present, Absent, on Leave, or on Half Day Leave.\n\n"
        "For past-date mistakes, say something like: I forgot to punch out yesterday."
    )


def payroll_brief_response(employee_name, employee_id, message):
    records = get_employee_salary_records(employee_id)
    if not records:
        return f"{employee_name}, no payroll records were found."

    requested = None
    ranges = named_month_ranges(message)
    if ranges:
        requested = ranges[0][1]
    record = None
    if requested:
        requested_date = salary_month_date(requested)
        record = next((row for row in records if salary_month_date(row.get("salary_month")) == requested_date), None)
    record = record or records[0]
    parts = payslip_parts(record)
    return (
        f"{employee_name}, salary for {month_display(record.get('salary_month'))}:\n"
        f"Basic: INR {payroll_money(parts['basic'])}\n"
        f"HRA: INR {payroll_money(parts['hra'])}\n"
        f"Allowances: INR {payroll_money(parts['allowances'])}\n"
        f"Reimbursements: INR {payroll_money(parts['reimbursement'])}\n"
        f"Deductions: INR {payroll_money(parts['deductions'])}\n"
        f"Net Salary: INR {payroll_money(parts['net_salary'])}"
    )


def payroll_latest_payslip_response(employee_name, employee_id):
    records = get_employee_salary_records(employee_id)
    if not records:
        return f"{employee_name}, no payslip record was found."
    record = records[0]
    month = str(record.get("salary_month") or "")
    from urllib.parse import quote

    encoded_month = quote(month)
    return (
        f"{employee_name}, your payslip for {month_display(month)} is ready.\n"
        f"View: /payslip?month={encoded_month}\n"
        f"Download PDF: /payslip/download?month={encoded_month}"
    )


def message_topics(message):
    text = normalized(message)
    topics = set()
    if is_attendance_message(message):
        topics.add("attendance")
    if any(word in text for word in ("salary", "payroll", "payslip", "net pay", "earnings", "earned", "deduction", "hra")):
        topics.add("payroll")
    if is_leave_balance_message(message) or "what leaves do i have" in text or "leaves do i have" in text:
        topics.add("leave_balance")
    if "pending request" in text or "pending requests" in text or "show my requests" in text or "approved leaves" in text:
        topics.add("requests")
    if re.search(r"\b(how|explain|process|guide|steps|use them|policy)\b", text):
        if "leave" in text or "leaves" in text or "holiday" in text:
            topics.add("leave_guidance")
        if "reimbursement" in text or "expense" in text or "claim" in text:
            topics.add("expense_guidance")
        if "payslip" in text or "salary slip" in text or "payroll" in text:
            topics.add("payroll_guidance")
        if "attendance" in text:
            topics.add("attendance_guidance")
    return topics


def multi_intent_response(employee_id, employee_name, message):
    text = normalized(message)
    topics = message_topics(message)
    multi_signal = " and " in text or "?" in str(message or "").strip().rstrip("?") or "," in text
    if len(topics) < 2 or not multi_signal:
        return None

    sections = []
    if "leave_balance" in topics:
        sections.append(("Leave Balances", format_leave_balance(employee_id, employee_name)))
    if "requests" in topics:
        sections.append(("Requests", format_all_requests(employee_id, message)))
    if "attendance" in topics:
        sections.append(("Attendance", attendance_response(employee_id, employee_name, message)))
    if "payroll" in topics:
        if "payslip" in text:
            sections.append(("Payslip", payroll_latest_payslip_response(employee_name, employee_id)))
        else:
            sections.append(("Payroll", payroll_brief_response(employee_name, employee_id, message)))

    guidance_sections = []
    if "leave_guidance" in topics:
        guidance_sections.append(("Leave Process", leave_process_response()))
    if "expense_guidance" in topics:
        guidance_sections.append(("Reimbursement Process", expense_process_response()))
    if "payroll_guidance" in topics:
        guidance_sections.append(("Payroll and Payslips", payslip_process_response()))
    if "attendance_guidance" in topics:
        guidance_sections.append(("Attendance Process", attendance_process_response()))
    sections.extend(guidance_sections)

    if len(sections) < 2:
        return None
    return "\n\n".join(f"{title}:\n{body}" for title, body in sections)


def is_help_message(message):
    text = normalized(message)
    return text in {"help", "what can you do", "what can you do?", "show available features"} or any(
        phrase in text
        for phrase in (
            "what services do you provide",
            "what can i ask",
            "available features",
            "show features",
        )
    )


def guidance_response(message):
    text = normalized(message)
    if not re.search(r"\b(how|explain|process|guide|steps|policy)\b", text):
        return None

    sections = []
    if "leave" in text or "holiday" in text:
        sections.append(("Leave Process", leave_process_response()))
    if "reimbursement" in text or "expense" in text or "claim" in text:
        sections.append(("Reimbursement Process", expense_process_response()))
    if "payslip" in text or "salary slip" in text or "payroll" in text:
        sections.append(("Payslip Process", payslip_process_response()))
    if "attendance" in text or "present" in text:
        sections.append(("Attendance Process", attendance_process_response()))

    if len(sections) > 1:
        return "\n\n".join(f"{title}:\n{body}" for title, body in sections)
    if sections:
        return sections[0][1]
    return None


def expense_advice_response(message):
    text = normalized(message)
    if any(word in text for word in ("show", "view", "list", "history", "latest", "last", "pending", "approved", "rejected", "status")):
        return None
    if not any(word in text for word in ("claim", "reimbursement", "expense", "bill", "receipt", "manager approve", "reimburse", "reimbursable")):
        return None
    advice_signal = any(
        word in text
        for word in ("should", "can", "will", "approve", "eligible", "worth", "policy", "reimbursable")
    )
    missing_receipt_signal = any(
        phrase in text
        for phrase in ("no bill", "don't have a bill", "dont have a bill", "no receipt", "without a receipt")
    )
    if not advice_signal and not missing_receipt_signal:
        return None

    lines = []
    if "no bill" in text or "don't have a bill" in text or "dont have a bill" in text or "no receipt" in text:
        lines.append("If the expense is INR 200 or below, you can submit it without a receipt.")
        lines.append("If it is above INR 200, a receipt is required before it can go for manager approval.")
    else:
        lines.append("If this was a work-related expense, it may be reimbursable after manager approval.")
        lines.append("For claims above INR 200, keep the receipt because OCR validation is required.")
    if "manager" in text or "approve" in text:
        lines.append("Manager approval usually depends on business purpose, category, amount, and receipt quality.")
    lines.append("")
    lines.append("You can continue the claim when you are ready, or cancel it if this is not worth submitting.")
    return "\n".join(lines)


def leave_advice_response(message):
    text = normalized(message)
    if not any(word in text for word in ("leave", "holiday", "time off", "day off", "absent", "vacation")):
        return None
    if not any(word in text for word in ("should", "can", "recommend", "worth", "eligible", "which", "what should")):
        return None

    return (
        "If you will be unavailable, applying leave is usually better than leaving attendance unresolved.\n\n"
        "For short personal time off, Casual Leave is usually the first option. For planned longer time off, Privilege Leave is usually better. "
        "For specific events such as marriage, childbirth, bereavement, or adoption, use the matching leave type if it is available in your balance.\n\n"
        "I can check your balances, suggest the best leave type, or prepare the request."
    )


def advisory_question_response(message):
    return expense_advice_response(message) or leave_advice_response(message)


def format_leave_balance(employee_id, employee_name):
    balances = fetch_rows("employee_leave_balance", employee_id)
    if not balances:
        return f"{employee_name}, no leave balances are configured for your profile."
    lines = [f"{employee_name}, here are your leave balances:"]
    lines.append("Leave Type | Remaining | Used")
    for row in balances:
        lines.append(
            f"{row.get('leave_type')}: {format_leave_amount(row.get('remaining_leaves', 0))} remaining, "
            f"{format_leave_amount(row.get('used_leaves', 0))} used"
        )
    low = [
        row
        for row in balances
        if float(row.get("remaining_leaves") or 0) <= 2
    ]
    if low:
        lines.append("")
        lines.append("Note: You are running low on " + ", ".join(row.get("leave_type", "leave") for row in low) + ".")
    lines.append("")
    lines.append("Would you like to apply leave or view leave requests?")
    return "\n".join(lines)


def is_leave_balance_message(message):
    text = normalized(message)
    return (
        "leave balance" in text
        or "leave balances" in text
        or "leave entitlement" in text
        or "how many leaves" in text
        or "leaves do i have" in text
        or "leaves can i take" in text
    )


def is_leave_history_query(message):
    text = normalized(message)
    has_leave = re.search(r"\bleaves?\b|\btime off\b|\bholiday\b", text) is not None
    if not has_leave:
        return False
    return any(
        phrase in text
        for phrase in (
            "did i take",
            "have i taken",
            "leaves have i taken",
            "leave have i taken",
            "leave taken",
            "my leave in",
            "my leaves in",
            "leave this month",
            "leaves this month",
        )
    )


def leave_history_period_response(employee_id, employee_name, message):
    text = normalized(message)
    start, end = attendance_month_range(message)
    approved = []
    for row in fetch_rows("leave_requests", employee_id):
        if str(row.get("status", "")).lower() != "approved":
            continue
        leave_start = parse_hr_date(row.get("from_date"))
        leave_end = parse_hr_date(row.get("to_date")) or leave_start
        if leave_start and leave_end and leave_start <= end and leave_end >= start:
            approved.append(row)

    period = start.strftime("%B %Y") if start.month == end.month and start.year == end.year else f"{start} to {end}"
    if not approved:
        return f"{employee_name}, I found no approved leave taken in {period}."

    lines = [f"{employee_name}, approved leave taken in {period}:"]
    total = 0.0
    for row in sort_leave_requests(approved):
        leave_start = max(parse_hr_date(row.get("from_date")), start)
        leave_end = min(parse_hr_date(row.get("to_date")) or leave_start, end)
        days = calculate_leave_days(leave_start, leave_end, row.get("leave_duration"))
        total += days
        lines.append(
            f"{row.get('leave_type', 'Leave')}: {leave_start.isoformat()} to {leave_end.isoformat()} "
            f"({format_leave_amount(days)} day(s))"
        )
    lines.append(f"Total approved leave: {format_leave_amount(total)} day(s).")
    return "\n".join(lines)


def sort_leave_requests(rows):
    return sorted(rows, key=lambda row: (row_date(row, "from_date", "created_at"), str(row.get("id", ""))), reverse=True)


def format_leave_requests(employee_id, message):
    rows = sort_leave_requests(fetch_rows("leave_requests", employee_id))
    status = status_filter(message)
    if status:
        rows = [row for row in rows if str(row.get("status", "")).lower() == status]
    if "last approved leave" in normalized(message):
        rows = [row for row in rows if str(row.get("status", "")).lower() == "approved"][:1]
    elif "last" in normalized(message) or "latest" in normalized(message) or "most recent" in normalized(message):
        rows = rows[:1]
    if not rows:
        status_text = f" {status}" if status else ""
        return f"No{status_text} leave requests were found."
    lines = ["Leave requests:"]
    for row in rows[:8]:
        lines.append(
            f"{row.get('status', 'Unknown')}: {row.get('leave_type', 'Leave')} "
            f"from {row.get('from_date')} to {row.get('to_date') or row.get('from_date')} "
            f"({row.get('leave_duration', 'Full Day')})"
        )
        if str(row.get("status", "")).lower() == "rejected" and row.get("rejection_reason"):
            lines.append(f"Reason: {row.get('rejection_reason')}")
    lines.append("")
    lines.append("Would you like to view leave balances, apply leave, or check attendance?")
    return "\n".join(lines)


def format_expense_requests(employee_id, message):
    rows = fetch_rows("expenses", employee_id)
    rows = sorted(rows, key=lambda row: str(row.get("created_at") or row.get("submitted_at") or row.get("id") or ""), reverse=True)
    status = status_filter(message)
    if status:
        rows = [row for row in rows if str(row.get("status", "")).lower() == status]
    if "last" in normalized(message) or "latest" in normalized(message) or "most recent" in normalized(message):
        rows = rows[:1]
    if not rows:
        status_text = f" {status}" if status else ""
        return f"No{status_text} expense claims were found."
    lines = ["Expense claims:"]
    for row in rows[:8]:
        lines.append(
            f"{row.get('status', 'Unknown')}: {row.get('expense_type', 'Expense')} "
            f"{rupees(row.get('amount'))} - {row.get('description') or 'No description'}"
        )
        if row.get("ocr_amount") and abs(float(row.get("amount") or 0) - float(row.get("ocr_amount") or 0)) > 0.01:
            lines.append(f"Manager amount check required: OCR read {rupees(row.get('ocr_amount'))}.")
        if str(row.get("status", "")).lower() == "rejected" and row.get("rejection_reason"):
            lines.append(f"Reason: {row.get('rejection_reason')}")
    lines.append("")
    lines.append("Would you like to submit a claim, view claims, or check reimbursement status?")
    return "\n".join(lines)


def is_history_message(message):
    text = normalized(message)
    if is_leave_history_query(message):
        return True
    request_words = ("request", "requests", "claim", "claims", "reimbursement", "reimbursements", "expense", "expenses", "leave", "leaves")
    return any(word in text for word in request_words) and any(
        phrase in text
        for phrase in ("show", "view", "list", "history", "previous", "last", "latest", "most recent", "pending", "approved", "rejected", "all")
    )


def latest_request_response(employee_id):
    leaves = [
        {
            "kind": "Leave",
            "label": f"{row.get('status', 'Unknown')}: {row.get('leave_type', 'Leave')} from {row.get('from_date')} to {row.get('to_date') or row.get('from_date')}",
            "date": row_date(row, "from_date", "created_at"),
        }
        for row in fetch_rows("leave_requests", employee_id)
    ]
    expenses = [
        {
            "kind": "Expense",
            "label": f"{row.get('status', 'Unknown')}: {row.get('expense_type', 'Expense')} {rupees(row.get('amount'))} - {row.get('description') or 'No description'}",
            "date": row_date(row, "submitted_at", "created_at", "bill_date"),
        }
        for row in fetch_rows("expenses", employee_id)
    ]
    rows = sorted(leaves + expenses, key=lambda row: row["date"], reverse=True)
    if not rows:
        return "No leave or reimbursement requests were found."
    latest = rows[0]
    return f"Most recent request:\n{latest['kind']}: {latest['label']}"


def format_all_requests(employee_id, message):
    text = normalized(message)
    if "latest" in text or "last" in text or "most recent" in text:
        return latest_request_response(employee_id)
    leave = format_leave_requests(employee_id, message)
    expense = format_expense_requests(employee_id, message)
    return f"{leave}\n\n{expense}"


def approved_leave_on(employee_id, target):
    leaves = fetch_rows("leave_requests", employee_id)
    for row in leaves:
        if str(row.get("status", "")).lower() != "approved":
            continue
        start = parse_hr_date(row.get("from_date"))
        end = parse_hr_date(row.get("to_date")) or start
        if start and end and start <= target <= end:
            duration = str(row.get("leave_duration") or "Full Day")
            if "half" in duration.lower():
                return "Half Day Leave"
            return "Leave"
    return None


def attendance_status_for_date(employee_id, target):
    leave_status = approved_leave_on(employee_id, target)
    if leave_status:
        return leave_status
    rows = fetch_rows("attendance", employee_id)
    row = next((item for item in rows if parse_hr_date(item.get("date")) == target), None)
    if row:
        return row.get("status") or ("Present" if row.get("punch_in") else "Absent")
    return "Absent" if target < date.today() else "No record yet"


def attendance_response(employee_id, employee_name, message):
    text = normalized(message)
    if "compare" in text and len(named_month_ranges(message)) >= 2:
        return compare_attendance_response(employee_id, employee_name, message)
    if ("latest" in text or "last" in text or "most recent" in text) and "record" in text:
        return latest_attendance_record_response(employee_id, employee_name)

    target = attendance_specific_date(message)
    if target:
        return (
            f"{employee_name}, attendance for {target.isoformat()}: {attendance_status_for_date(employee_id, target)}.\n\n"
            "Would you like to view attendance history, check another date, or view leave records?"
        )

    start, end = attendance_month_range(message)

    day_count = (end - start).days + 1
    statuses = []
    for offset in range(day_count):
        current = start + timedelta(days=offset)
        statuses.append((current, attendance_status_for_date(employee_id, current)))

    counts = Counter(status for _day, status in statuses)
    lines = [f"{employee_name}, attendance from {start.isoformat()} to {end.isoformat()}:"]
    for status, count in counts.items():
        lines.append(f"{status}: {count}")
    lines.append("")
    lines.append("Recent records:")
    for current, status in statuses[-8:]:
        lines.append(f"{current.isoformat()}: {status}")
    lines.append("")
    lines.append("Would you like to view attendance history, check a specific date, or view leave records?")
    return "\n".join(lines)


def attendance_counts_for_range(employee_id, start, end):
    statuses = []
    for offset in range((end - start).days + 1):
        current = start + timedelta(days=offset)
        statuses.append(attendance_status_for_date(employee_id, current))
    return Counter(statuses)


def compare_attendance_response(employee_id, employee_name, message):
    ranges = named_month_ranges(message)[:2]
    if len(ranges) < 2:
        return f"{employee_name}, please mention two months to compare attendance."

    first_label, first_start, first_end = ranges[0]
    second_label, second_start, second_end = ranges[1]
    first_counts = attendance_counts_for_range(employee_id, first_start, first_end)
    second_counts = attendance_counts_for_range(employee_id, second_start, second_end)
    present_delta = second_counts.get("Present", 0) - first_counts.get("Present", 0)
    direction = "increase" if present_delta >= 0 else "decrease"

    lines = [f"{employee_name}, attendance comparison:"]
    lines.append(f"{first_label}: Present {first_counts.get('Present', 0)}, Leave {first_counts.get('Leave', 0)}, Absent {first_counts.get('Absent', 0)}")
    lines.append(f"{second_label}: Present {second_counts.get('Present', 0)}, Leave {second_counts.get('Leave', 0)}, Absent {second_counts.get('Absent', 0)}")
    lines.append(f"Difference: {abs(present_delta)} present day(s) {direction}.")
    lines.append("")
    lines.append("Would you like to view detailed attendance history or check a specific date?")
    return "\n".join(lines)


def latest_attendance_record_response(employee_id, employee_name):
    rows = fetch_rows("attendance", employee_id)
    rows = sorted(rows, key=lambda row: row_date(row, "date", "created_at"), reverse=True)
    if not rows:
        return f"{employee_name}, I could not find any attendance records yet."
    row = rows[0]
    status = row.get("status") or ("Present" if row.get("punch_in") else "No status")
    return (
        f"{employee_name}, your latest attendance record is {row.get('date')}: {status}.\n"
        f"Punch In: {row.get('punch_in') or 'Not recorded'}\n"
        f"Punch Out: {row.get('punch_out') or 'Not recorded'}\n\n"
        "Would you like to view attendance history or check a specific date?"
    )


def is_attendance_message(message):
    text = normalized(message)
    return (
        "attendance" in text
        or "was i present" in text
        or "present on" in text
        or "did i attend" in text
        or "attend on" in text
        or "did you punch" in text
        or "did i punch" in text
        or "was i punched" in text
        or "was i punch" in text
        or "punch status" in text
        or "am i punched" in text
        or "recorded present" in text
        or "attendance log" in text
        or "attendance logs" in text
        or "punch-in record" in text
        or "punch out record" in text
    )


def hr_summary(employee_id, employee_name):
    attendance_rows = fetch_rows("attendance", employee_id)
    leaves = fetch_rows("leave_requests", employee_id)
    balances = fetch_rows("employee_leave_balance", employee_id)
    expenses = fetch_rows("expenses", employee_id)
    salaries = get_employee_salary_records(employee_id)

    today = date.today()
    month_attendance = [row for row in attendance_rows if (parse_hr_date(row.get("date")) or date.min).month == today.month]
    present_count = sum(1 for row in month_attendance if str(row.get("status") or "").lower() == "present" or row.get("punch_in"))
    approved_leave_days = 0
    for row in leaves:
        if str(row.get("status", "")).lower() != "approved":
            continue
        start = parse_hr_date(row.get("from_date"))
        end = parse_hr_date(row.get("to_date")) or start
        if start and end and (start.month == today.month or end.month == today.month):
            approved_leave_days += calculate_leave_days(start, end, row.get("leave_duration"))

    leave_status_counts = Counter(str(row.get("status", "Unknown")).title() for row in leaves)
    expense_totals = Counter()
    for row in expenses:
        expense_totals[str(row.get("status", "Unknown")).title()] += float(row.get("amount") or 0)
    latest_salary = payslip_parts(salaries[0])["net_salary"] if salaries else 0
    latest_month = month_display(salaries[0].get("salary_month")) if salaries else "Not available"

    lines = [f"HR Summary - {date.today().strftime('%B %Y')}"]
    lines.append(f"Employee: {employee_name}")
    lines.append("Attendance:")
    lines.append(f"Present this month: {present_count}")
    lines.append(f"Absent this month: {max(date.today().day - present_count - int(approved_leave_days), 0)}")
    lines.append(f"Approved leave days this month: {format_leave_amount(approved_leave_days)}")
    lines.append("")
    lines.append("Leave Balances:")
    if balances:
        for row in balances:
            lines.append(f"{row.get('leave_type')}: {format_leave_amount(row.get('remaining_leaves', 0))} remaining")
    else:
        lines.append("No balances configured")
    lines.append("")
    lines.append("Requests:")
    lines.append(f"Pending: {leave_status_counts.get('Pending', 0)}")
    lines.append(f"Approved: {leave_status_counts.get('Approved', 0)}")
    lines.append("")
    lines.append("Expenses:")
    lines.append(f"Pending: {rupees(expense_totals.get('Pending', 0))}")
    lines.append(f"Approved: {rupees(expense_totals.get('Approved', 0))}")
    lines.append("")
    lines.append("Payroll:")
    lines.append(f"Latest Salary: {rupees(latest_salary)}")
    lines.append(f"Latest Payslip: {latest_month}")
    lines.append("")
    lines.append("Recent Activity:")
    latest_leave = sort_leave_requests(leaves)[:1]
    latest_expense = sorted(expenses, key=lambda row: str(row.get("created_at") or row.get("submitted_at") or row.get("id") or ""), reverse=True)[:1]
    lines.append(
        "Last Leave Request: "
        + (
            f"{latest_leave[0].get('status')} {latest_leave[0].get('leave_type')} from {latest_leave[0].get('from_date')}"
            if latest_leave
            else "None"
        )
    )
    lines.append(
        "Last Expense Claim: "
        + (
            f"{latest_expense[0].get('status')} {rupees(latest_expense[0].get('amount'))}"
            if latest_expense
            else "None"
        )
    )
    lines.append(f"Latest Payroll Record: {latest_month}")
    insights = []
    low_balances = [
        row
        for row in balances
        if float(row.get("remaining_leaves") or 0) <= 2
    ]
    for row in low_balances:
        insights.append(f"Your {row.get('leave_type')} balance is running low.")

    if expenses:
        top_category = Counter(str(row.get("expense_type") or "Uncategorized") for row in expenses).most_common(1)[0]
        insights.append(f"Your most common reimbursement category is {top_category[0]}.")

    if len(salaries) >= 2:
        latest_parts = payslip_parts(salaries[0])
        previous_parts = payslip_parts(salaries[1])
        salary_delta = latest_parts["net_salary"] - previous_parts["net_salary"]
        reimbursement_delta = latest_parts["reimbursement"] - previous_parts["reimbursement"]
        if salary_delta < -0.01 and reimbursement_delta < -0.01:
            insights.append("Your latest salary decreased partly because reimbursements were lower than the previous payroll record.")
        elif salary_delta > 0.01 and reimbursement_delta > 0.01:
            insights.append("Your latest salary increased partly because reimbursements were higher than the previous payroll record.")

    lines.append("")
    lines.append("Insights:")
    if insights:
        lines.extend(insights)
    else:
        lines.append("No urgent HR issues stand out from the available records.")
    return "\n".join(lines)


def balance_value(balances, leave_name):
    leave_name = leave_name.lower()
    for row in balances:
        if leave_name in str(row.get("leave_type", "")).lower():
            return float(row.get("remaining_leaves") or 0)
    return 0


def balance_line(balances, leave_name):
    return f"{leave_name}: {format_leave_amount(balance_value(balances, leave_name))} days"


def most_common_expense_category(employee_id):
    rows = fetch_rows("expenses", employee_id)
    categories = Counter(str(row.get("expense_type") or "Uncategorized") for row in rows)
    return categories.most_common(1)[0] if categories else None


def most_used_leave_type(employee_id):
    rows = [
        row
        for row in fetch_rows("leave_requests", employee_id)
        if str(row.get("status", "")).lower() == "approved"
    ]
    categories = Counter(str(row.get("leave_type") or "Leave") for row in rows)
    return categories.most_common(1)[0] if categories else None


def attendance_correction_response(employee_id):
    target = date.today() - timedelta(days=1)
    rows = fetch_rows("attendance", employee_id)
    row = next((item for item in rows if parse_hr_date(item.get("date")) == target), None)
    if row and row.get("punch_in") and not row.get("punch_out"):
        return (
            f"I found an incomplete attendance record for {target.isoformat()}: punch in was recorded, but punch out is missing.\n\n"
            "This usually needs an attendance correction request or manager/HR update.\n\n"
            "Would you like me to show your attendance history or explain what details to provide for correction?"
        )
    return (
        f"I do not see an incomplete punch-out record for {target.isoformat()} in the current attendance data.\n\n"
        "If the record is missing or incorrect, share the date and approximate punch-out time so HR can review it."
    )


def recommendation_response(employee_id, employee_name, message):
    text = normalized(message)
    if not any(
        phrase in text
        for phrase in (
            "married",
            "marriage",
            "wedding",
            "wife",
            "spouse",
            "pregnant",
            "due next month",
            "expecting",
            "child",
            "baby",
            "birth",
            "born",
            "father",
            "mother",
            "parent",
            "passed away",
            "died",
            "death",
            "bereavement",
            "medical",
            "surgery",
            "illness",
            "sick",
            "travelling",
            "traveling",
            "client meeting",
            "client visit",
            "forgot to punch out",
            "forgot punch out",
            "which leave",
            "what leaves should i use",
            "best leave",
            "running low",
            "leaves have i taken",
            "leave taken",
            "reimbursement categories",
            "reimbursement category",
            "categories do i use",
            "category do i use",
            "most often",
            "frequently used",
            "highest salary",
            "long vacation",
            "planning vacation",
            "relocating",
            "family situation",
            "recommend",
            "what should i do",
        )
    ):
        return None
    balances = fetch_rows("employee_leave_balance", employee_id)
    balance_map = {str(row.get("leave_type", "")).lower(): float(row.get("remaining_leaves") or 0) for row in balances}

    if "family situation is complicated" in text or "family situation" in text and "complicated" in text:
        return (
            "That may involve different leave types depending on the situation.\n\n"
            "Could you share whether this is for medical care, bereavement, childcare, marriage, or general personal time off? "
            "I can recommend the safest option once I know the context."
        )

    if "forgot to punch out" in text or "forgot punch out" in text:
        return attendance_correction_response(employee_id)

    if any(phrase in text for phrase in ("wife is due", "spouse is due", "pregnant", "expecting", "due next month")):
        paternity = balance_value(balances, "Paternity Leave")
        return (
            "You may be eligible for Paternity Leave.\n\n"
            f"Current balance: {format_leave_amount(paternity)} days.\n\n"
            "Would you like me to view the policy, check your leave balances, or prepare a leave request?"
        )

    if ("child" in text and ("born" in text or "birth" in text)) or "baby born" in text or "baby was born" in text:
        paternity = balance_value(balances, "Paternity Leave")
        return (
            "Congratulations.\n\n"
            "You may be eligible for Paternity Leave.\n\n"
            f"Current balance: {format_leave_amount(paternity)} days.\n\n"
            "Would you like assistance creating a request?"
        )

    if "married" in text or "marriage" in text or "wedding" in text:
        marriage = next((row for row in balances if "marriage" in str(row.get("leave_type", "")).lower()), None)
        casual = balance_map.get("casual leave", 0)
        privilege = balance_map.get("privilege leave", 0)
        lines = ["You may be eligible for Marriage Leave."]
        if marriage:
            lines.append(f"Marriage Leave balance: {format_leave_amount(marriage.get('remaining_leaves', 0))} days.")
        lines.append(f"Casual Leave: {format_leave_amount(casual)} days.")
        lines.append(f"Privilege Leave: {format_leave_amount(privilege)} days.")
        lines.append("")
        lines.append("Recommended option: use Marriage Leave first, then Casual or Privilege Leave if you need extra days.")
        lines.append("Reason: Marriage Leave is specifically intended for this situation and preserves your Privilege Leave balance for future planned time off.")
        lines.append("")
        lines.append("Would you like me to prepare the request?")
        return "\n".join(lines)

    if any(phrase in text for phrase in ("passed away", "died", "death", "bereavement")) and any(word in text for word in ("father", "mother", "parent", "family", "relative", "grandfather", "grandmother")):
        bereavement = balance_value(balances, "Bereavement Leave")
        return (
            "I am sorry to hear that.\n\n"
            "You may be eligible for Bereavement Leave.\n\n"
            f"Current balance: {format_leave_amount(bereavement)} days.\n\n"
            "Would you like me to explain the policy, check balances, or create a leave request?"
        )

    if "medical" in text or "surgery" in text or "health" in text or re.search(r"\b(ill|illness|sick)\b", text):
        casual = balance_value(balances, "Casual Leave")
        privilege = balance_value(balances, "Privilege Leave")
        return (
            "For medical time off, the right leave type depends on your configured policy and documentation requirements.\n\n"
            f"Casual Leave: {format_leave_amount(casual)} days.\n"
            f"Privilege Leave: {format_leave_amount(privilege)} days.\n\n"
            "If this is urgent or short, Casual Leave may work. If it is planned or longer, Privilege Leave may be safer. "
            "Please check whether your company has a dedicated medical/sick leave policy before submitting."
        )

    if "travelling" in text or "traveling" in text or "client meeting" in text or "client visit" in text or "travelling for work" in text or "traveling for work" in text:
        return (
            "You may be eligible for Travel Reimbursement.\n\n"
            "Keep the receipt or invoice. If the amount is above INR 200, upload the receipt so OCR can validate it before manager approval.\n\n"
            "Would you like information about the reimbursement process or do you want to submit an expense now?"
        )

    if "relocating" in text or "relocation" in text:
        return (
            "Relocation can involve leave, travel, or reimbursement depending on company policy, so I do not want to guess.\n\n"
            "Is this mainly time off, travel expenses, accommodation, or a payroll/benefits question?"
        )

    if "long vacation" in text or "planning vacation" in text or "vacation" in text:
        privilege = balance_value(balances, "Privilege Leave")
        casual = balance_value(balances, "Casual Leave")
        return (
            "For a planned vacation, Privilege Leave is usually the best first option.\n\n"
            f"Privilege Leave: {format_leave_amount(privilege)} days.\n"
            f"Casual Leave: {format_leave_amount(casual)} days.\n\n"
            "Recommended plan: use Privilege Leave for the main vacation and preserve Casual Leave for short unplanned absences."
        )

    if "which leave" in text or "what leaves should i use" in text or "best leave" in text:
        if not balances:
            return "No leave balances are configured, so I cannot recommend a leave type yet."
        best = max(balances, key=lambda row: float(row.get("remaining_leaves") or 0))
        most_used = most_used_leave_type(employee_id)
        lines = [
            f"Based on your current balances, {best.get('leave_type')} has the highest availability "
            f"({format_leave_amount(best.get('remaining_leaves', 0))} days)."
        ]
        if most_used:
            lines.append(f"Historically, your most used approved leave type is {most_used[0]} ({most_used[1]} request(s)).")
        lines.append("For personal short absences, use Casual Leave. For planned longer absences, use Privilege Leave. For specific life events, use the matching leave type if available.")
        return "\n\n".join(lines)

    if "running low" in text and "leave" in text:
        low = [row for row in balances if float(row.get("remaining_leaves") or 0) <= 2]
        if not low:
            return "You are not currently running low on configured leave balances."
        return "You are running low on: " + ", ".join(f"{row.get('leave_type')} ({format_leave_amount(row.get('remaining_leaves', 0))})" for row in low)

    if (
        "reimbursement categories" in text
        or "reimbursement category" in text
        or "categories do i use" in text
        or "category do i use" in text
        or ("reimbursement" in text and "most often" in text)
    ):
        top = most_common_expense_category(employee_id)
        if not top:
            return "I do not see reimbursement history yet, so I cannot identify your most used category."
        return (
            f"Your most frequently used reimbursement category is {top[0]} ({top[1]} claim(s)).\n\n"
            "That may be useful when reviewing recurring work expenses or planning what receipts to keep."
        )

    if "frequently used" in text and "leave" in text:
        top = most_used_leave_type(employee_id)
        if not top:
            return "I do not see approved leave history yet, so I cannot identify your most used leave type."
        return f"Your most frequently used approved leave type is {top[0]} ({top[1]} request(s))."

    if "leaves have i taken" in text or "leave taken" in text:
        year = date.today().year
        total = 0
        for row in fetch_rows("leave_requests", employee_id):
            if str(row.get("status", "")).lower() != "approved":
                continue
            start = parse_hr_date(row.get("from_date"))
            end = parse_hr_date(row.get("to_date")) or start
            if start and start.year == year:
                total += calculate_leave_days(start, end, row.get("leave_duration"))
        return f"You have taken {format_leave_amount(total)} approved leave days in {year}."

    if "recommend" in text or "what should i do" in text:
        return (
            "I can recommend the next HR action, but I need a little more context.\n\n"
            "Is this about leave, attendance, reimbursement, payroll, or a life event?"
        )

    return None


def payroll_insight_response(employee_id, message):
    text = normalized(message)
    if not any(
        phrase in text
        for phrase in (
            "salary have i earned",
            "earned this year",
            "highest salary",
            "highest net salary",
            "which month had my highest salary",
            "highest reimbursement",
            "reimbursement this year",
            "reimbursement have i received",
        )
    ):
        return None
    salaries = get_employee_salary_records(employee_id)
    if not salaries:
        return None
    current_year = date.today().year
    year_rows = [row for row in salaries if salary_month_date(row.get("salary_month")).year == current_year]
    if "salary have i earned" in text or "earned this year" in text:
        total = sum(payslip_parts(row)["net_salary"] for row in year_rows)
        return f"Your total net salary recorded in {current_year} is {rupees(total)}."
    if "highest salary" in text or "highest net salary" in text or "which month had my highest salary" in text:
        highest = max(salaries, key=lambda row: payslip_parts(row)["net_salary"])
        parts = payslip_parts(highest)
        return f"Your highest net salary was {rupees(parts['net_salary'])} in {month_display(highest.get('salary_month'))}."
    if "highest reimbursement" in text:
        highest = max(salaries, key=lambda row: payslip_parts(row)["reimbursement"])
        parts = payslip_parts(highest)
        return f"Your highest reimbursement was {rupees(parts['reimbursement'])} in {month_display(highest.get('salary_month'))}."
    if "reimbursement" in text and "year" in text:
        total = sum(payslip_parts(row)["reimbursement"] for row in year_rows)
        return f"Your total payroll reimbursement in {current_year} is {rupees(total)}."
    return None


def smart_followup_response(message):
    text = normalized(message)
    followup_text = text.strip("?.!,;: ")
    topic = session.get("last_hr_topic") if has_request_context() else None
    if topic == "leave_history" and re.fullmatch(r"(?:in\s+)?(?:" + "|".join(MONTH_NAMES) + r")(?:\s+\d{4})?", followup_text):
        return "__LEAVE_HISTORY_MONTH_FOLLOWUP__"
    if topic == "attendance" and "compare" in text:
        return "__ATTENDANCE_COMPARISON_FOLLOWUP__"
    if topic == "attendance" and followup_text in {"attendance log", "attendance logs", "check log", "check logs", "log", "logs"}:
        return "__ATTENDANCE_LOG_FOLLOWUP__"
    if text not in {"ok", "okay", "okok", "thanks", "thank you", "hmm"}:
        return None
    topic = session.get("last_hr_topic")
    if topic == "leave_balance":
        return "Would you like to apply leave, view leave requests, or check attendance?"
    if topic == "expense_guidance":
        return "Would you like to submit an expense claim now?"
    if topic == "payroll":
        return "Would you like salary history, payslip download, or salary comparison?"
    if topic == "attendance":
        return "Would you like this month's attendance history or a specific date check?"
    if topic == "help":
        return "Tell me what you want to do: attendance, leave, expenses, payroll, or HR summary."
    return None


def set_topic(topic):
    if not has_request_context():
        return
    session["last_hr_topic"] = topic
    session.modified = True


def handle_advisory_message(employee_id, employee_name, message):
    text = normalized(message)

    if is_conversation_close_message(message):
        return json_reply("You're welcome. Take care, and message me whenever you need HR help.")

    followup = smart_followup_response(message)
    if followup == "__LEAVE_HISTORY_MONTH_FOLLOWUP__":
        return json_reply(leave_history_period_response(employee_id, employee_name, f"Did I take leave {message}?"))
    if followup == "__ATTENDANCE_COMPARISON_FOLLOWUP__":
        return json_reply(attendance_response(employee_id, employee_name, f"attendance {message}"))
    if followup == "__ATTENDANCE_LOG_FOLLOWUP__":
        return json_reply(attendance_response(employee_id, employee_name, "attendance this month"))
    if followup:
        return json_reply(followup)

    if is_hr_contact_message(message):
        set_topic("hr_contact")
        return json_reply(hr_contact_response())

    if is_help_message(message):
        set_topic("help")
        return json_reply(help_response())

    multi = multi_intent_response(employee_id, employee_name, message)
    if multi:
        set_topic("multi")
        return json_reply(multi)

    if is_leave_history_query(message):
        set_topic("leave_history")
        return json_reply(leave_history_period_response(employee_id, employee_name, message))

    if is_leave_balance_message(message):
        set_topic("leave_balance")
        return json_reply(format_leave_balance(employee_id, employee_name))

    if attendance_module.is_attendance_comparison_message(message):
        set_topic("attendance")
        return json_reply(attendance_response(employee_id, employee_name, message))

    if attendance_module.is_absent_dates_query(message):
        set_topic("attendance")
        return json_reply(attendance_response(employee_id, employee_name, message))

    guidance = guidance_response(message)
    if guidance:
        if "expense" in text or "reimbursement" in text or "claim" in text:
            set_topic("expense_guidance")
        elif "attendance" in text:
            set_topic("attendance")
        elif "payslip" in text:
            set_topic("payroll")
        else:
            set_topic("leave_guidance")
        return json_reply(guidance)

    if any(phrase in text for phrase in ("summary", "hr summary", "dashboard", "employee summary", "monthly summary", "total summary")):
        set_topic("summary")
        return json_reply(hr_summary(employee_id, employee_name))

    insight = payroll_insight_response(employee_id, message)
    if insight:
        set_topic("payroll")
        return json_reply(insight)

    if is_attendance_correction_status_message(message):
        set_topic("attendance")
        return json_reply(attendance_correction_status_response(employee_id, employee_name, message))

    if is_attendance_correction_message(message):
        set_topic("attendance_correction")
        return handle_apply_attendance_correction(employee_id, employee_name, {}, message)

    recommendation = recommendation_response(employee_id, employee_name, message)
    if recommendation:
        set_topic("recommendation")
        return json_reply(recommendation)

    advice = advisory_question_response(message)
    if advice:
        set_topic("advice")
        return json_reply(advice)

    if is_attendance_message(message):
        set_topic("attendance")
        return json_reply(attendance_response(employee_id, employee_name, message))

    if is_history_message(message):
        if "all my requests" in text or "show my requests" in text or ("request" in text and "leave" not in text and "expense" not in text and "claim" not in text and "reimbursement" not in text):
            set_topic("requests")
            return json_reply(format_all_requests(employee_id, message))
        if "leave" in text or "holiday" in text:
            set_topic("leave_history")
            return json_reply(format_leave_requests(employee_id, message))
        if any(word in text for word in ("expense", "claim", "claims", "reimbursement", "reimbursements")):
            set_topic("expense_history")
            return json_reply(format_expense_requests(employee_id, message))

    return None


def _sync_attendance_service():
    attendance_module.supabase = supabase


def attendance_month_range(message):
    _sync_attendance_service()
    period = attendance_module.attendance_period(message)
    if period and period.start and period.end:
        return period.start, period.end
    today = date.today()
    return today.replace(day=1), today


def named_month_ranges(message):
    _sync_attendance_service()
    return attendance_module.named_month_ranges(message)


def attendance_specific_date(message):
    _sync_attendance_service()
    period = attendance_module.extract_specific_date(message)
    return period.start if period else None


def attendance_status_for_date(employee_id, target):
    _sync_attendance_service()
    return attendance_module.attendance_status_for_date(employee_id, target)


def attendance_counts_for_range(employee_id, start, end):
    _sync_attendance_service()
    return attendance_module.attendance_counts_for_range(employee_id, start, end)


def compare_attendance_response(employee_id, employee_name, message):
    _sync_attendance_service()
    return attendance_module.compare_attendance_response(employee_id, employee_name, message)


def latest_attendance_record_response(employee_id, employee_name):
    _sync_attendance_service()
    return attendance_module.latest_attendance_record_response(employee_id, employee_name)


def attendance_response(employee_id, employee_name, message):
    _sync_attendance_service()
    return attendance_module.attendance_response(employee_id, employee_name, message)


def is_attendance_message(message):
    return attendance_module.is_attendance_message(message)


def attendance_correction_response(employee_id):
    _sync_attendance_service()
    return attendance_module.attendance_correction_status_response(employee_id, "Employee", "show pending correction requests")


def is_attendance_correction_message(message):
    return attendance_module.is_attendance_correction_message(message)


def is_attendance_correction_status_message(message):
    return attendance_module.is_attendance_correction_status_message(message)


def attendance_correction_status_response(employee_id, employee_name, message):
    _sync_attendance_service()
    return attendance_module.attendance_correction_status_response(employee_id, employee_name, message)


def handle_apply_attendance_correction(employee_id, employee_name, entities=None, user_message=""):
    _sync_attendance_service()
    return attendance_module.handle_apply_attendance_correction(employee_id, employee_name, entities or {}, user_message)

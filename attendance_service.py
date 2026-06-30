import re
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from dateutil import parser as date_parser
from flask import flash, has_request_context, jsonify, redirect, render_template, request, session

from config import Config
from intent_handlers import parse_hr_date
from supabase_client import supabase
from workflow_store import finish_workflow, get_active_workflow, upsert_workflow


ATTENDANCE_CORRECTION_WORKFLOW = "attendance_correction"

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

COMMON_DATE_TYPOS = {
    "thrusday": "thursday",
    "thurday": "thursday",
    "thursdayday": "thursday",
    "weak": "week",
}

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


@dataclass
class AttendancePeriod:
    label: str
    start: date | None
    end: date | None
    kind: str


def json_reply(reply, status=200):
    return jsonify({"reply": reply}), status


def normalized(message):
    text = re.sub(r"\s+", " ", str(message or "").strip().lower())
    for typo, replacement in COMMON_DATE_TYPOS.items():
        text = re.sub(rf"\b{typo}\b", replacement, text)
    return text


def parse_time_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return datetime.strptime(text.upper(), fmt).time()
        except ValueError:
            continue
    try:
        return date_parser.parse(text, fuzzy=True).time()
    except (ValueError, TypeError, OverflowError):
        return None


def parse_config_time(value, fallback):
    return parse_time_value(value) or parse_time_value(fallback)


def time_to_hours(value):
    parsed = parse_time_value(value)
    if not parsed:
        return None
    return parsed.hour + parsed.minute / 60 + parsed.second / 3600


def worked_hours(punch_in, punch_out):
    start = time_to_hours(punch_in)
    end = time_to_hours(punch_out)
    if start is None or end is None:
        return 0.0
    if end < start:
        end += 24
    return round(max(0.0, end - start), 2)


def attendance_metrics(row):
    hours = worked_hours(row.get("punch_in"), row.get("punch_out"))
    office_start = parse_config_time(getattr(Config, "OFFICE_START_TIME", None), "09:30")
    office_end = parse_config_time(getattr(Config, "OFFICE_END_TIME", None), "18:30")
    overtime_threshold = float(getattr(Config, "OVERTIME_THRESHOLD_HOURS", 9) or 9)
    half_day_threshold = float(getattr(Config, "HALF_DAY_THRESHOLD_HOURS", 4) or 4)
    punch_in = parse_time_value(row.get("punch_in"))
    punch_out = parse_time_value(row.get("punch_out"))
    status = str(row.get("status") or "").strip()

    is_leave = status.lower() == "leave"
    is_half_day = bool(hours and hours < half_day_threshold and not is_leave)
    late = bool(punch_in and office_start and punch_in > office_start)
    early = bool(punch_out and office_end and punch_out < office_end)
    overtime = round(max(0.0, hours - overtime_threshold), 2)

    if is_leave:
        display_status = "Leave"
    elif is_half_day:
        display_status = "Half Day"
    elif row.get("punch_in") or status.lower() == "present":
        display_status = "Present"
    elif status:
        display_status = status
    else:
        display_status = "Absent"

    return {
        "status": display_status,
        "worked_hours": hours,
        "late_arrival": late,
        "early_departure": early,
        "overtime_hours": overtime,
        "attendance_type": "Half Day" if is_half_day else display_status,
    }


def fetch_employee_rows(table_name, employee_id):
    response = supabase.table(table_name).select("*").eq("employee_id", employee_id).execute()
    return response.data or []


def fetch_all_rows(table_name):
    response = supabase.table(table_name).select("*").execute()
    return response.data or []


def row_date(row, *fields):
    for field in fields:
        parsed = parse_hr_date(row.get(field))
        if parsed:
            return parsed
    return None


def parse_date_fragment(fragment):
    text = str(fragment or "").strip(" .,:;")
    if not text:
        return None
    lower = normalized(text)
    today = date.today()
    if lower in {"today", "now"}:
        return today
    if lower == "yesterday":
        return today - timedelta(days=1)
    if lower in {"day before yesterday", "the day before yesterday"}:
        return today - timedelta(days=2)
    weekday_match = re.fullmatch(r"(?:last|previous)\s+(" + "|".join(WEEKDAYS) + r")", lower)
    if weekday_match:
        target = WEEKDAYS[weekday_match.group(1)]
        days_back = (today.weekday() - target) % 7 or 7
        return today - timedelta(days=days_back)
    parsed = parse_hr_date(text)
    if parsed:
        return parsed
    try:
        default = datetime(today.year, 1, 1)
        parsed_dt = date_parser.parse(text, fuzzy=True, dayfirst=True, default=default)
        parsed_date = parsed_dt.date()
        if parsed_date.year == 1900:
            parsed_date = parsed_date.replace(year=today.year)
        return parsed_date
    except (ValueError, TypeError, OverflowError):
        return None


def extract_explicit_range(message):
    text = normalized(message)
    patterns = [
        r"\bfrom\s+(.+?)\s+(?:to|till|until|through)\s+(.+)$",
        r"\bbetween\s+(.+?)\s+and\s+(.+)$",
    ]
    trim_words = (
        "attendance",
        "show",
        "my",
        "please",
        "history",
        "records",
        "record",
        "for",
        "on",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        start_text, end_text = match.group(1), match.group(2)
        for word in trim_words:
            start_text = re.sub(rf"\b{word}\b", " ", start_text)
            end_text = re.sub(rf"\b{word}\b", " ", end_text)
        start = parse_date_fragment(start_text)
        end = parse_date_fragment(end_text)
        if start and end:
            if end < start:
                start, end = end, start
            return AttendancePeriod(f"{start.isoformat()} to {end.isoformat()}", start, end, "range")
    return None


def extract_specific_date(message):
    text = normalized(message)
    if "day before yesterday" in text:
        target = date.today() - timedelta(days=2)
        return AttendancePeriod(target.isoformat(), target, target, "day")
    if "yesterday" in text:
        target = date.today() - timedelta(days=1)
        return AttendancePeriod(target.isoformat(), target, target, "day")
    for weekday in WEEKDAYS:
        if re.search(rf"\b(?:last|previous)\s+{weekday}\b", text):
            target = parse_date_fragment(f"last {weekday}")
            return AttendancePeriod(target.isoformat(), target, target, "day")
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", text):
        target = parse_date_fragment(text)
        if target:
            return AttendancePeriod(target.isoformat(), target, target, "day")
    month_pattern = "|".join(MONTH_NAMES)
    if re.search(rf"\b\d{{1,2}}\s+({month_pattern})\b|\b({month_pattern})\s+\d{{1,2}}\b", text):
        target = parse_date_fragment(text)
        if target:
            return AttendancePeriod(target.isoformat(), target, target, "day")
    if "today" in text and not any(token in text for token in ("this week", "this month")):
        target = date.today()
        return AttendancePeriod(target.isoformat(), target, target, "day")
    return None


def extract_week_period(message):
    text = normalized(message)
    today = date.today()
    if "last week" in text or "previous week" in text:
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        end = start + timedelta(days=6)
        return AttendancePeriod("last week", start, end, "week")
    if "this week" in text or "current week" in text:
        start = today - timedelta(days=today.weekday())
        return AttendancePeriod("this week", start, today, "week")
    return None


def extract_month_period(message):
    text = normalized(message)
    today = date.today()
    if "last month" in text or "previous month" in text:
        previous_month_end = today.replace(day=1) - timedelta(days=1)
        start = previous_month_end.replace(day=1)
        return AttendancePeriod(previous_month_end.strftime("%B %Y"), start, previous_month_end, "month")
    if "this month" in text or "current month" in text:
        return AttendancePeriod(today.strftime("%B %Y"), today.replace(day=1), today, "month")
    for name, month in MONTH_NAMES.items():
        if re.search(rf"\b{name}\b", text):
            year_match = re.search(rf"\b{name}\s+(20\d{{2}})\b|\b(20\d{{2}})\s+{name}\b", text)
            year = int(next(group for group in year_match.groups() if group)) if year_match else today.year
            start = date(year, month, 1)
            end = date(year, month, monthrange(year, month)[1])
            if year == today.year and month == today.month:
                end = min(end, today)
            return AttendancePeriod(start.strftime("%B %Y"), start, end, "month")
    return None


def attendance_period(message):
    text = normalized(message)
    if re.search(r"\b(all|complete|full)\s+attendance\b|\battendance\s+(history|records?)\b", text):
        if "this month" not in text and "last month" not in text and "previous month" not in text:
            return AttendancePeriod("complete attendance history", None, None, "all")
    for extractor in (extract_explicit_range, extract_specific_date, extract_week_period, extract_month_period):
        period = extractor(message)
        if period:
            return period
    if "attendance" in text:
        today = date.today()
        return AttendancePeriod(today.strftime("%B %Y"), today.replace(day=1), today, "month")
    return None


def named_month_ranges(message):
    text = normalized(message)
    today = date.today()
    ranges = []
    for name, month in MONTH_NAMES.items():
        for match in re.finditer(rf"\b{name}\b", text):
            year_match = re.search(rf"\b{name}\s+(20\d{{2}})\b", text[match.start() :])
            year = int(year_match.group(1)) if year_match else today.year
            start = date(year, month, 1)
            end = date(year, month, monthrange(year, month)[1])
            if year == today.year and month == today.month:
                end = min(end, today)
            ranges.append((match.start(), start.strftime("%B %Y"), start, end))
    if re.search(r"\b(this|current) month\b", text):
        ranges.append((text.find("this month") if "this month" in text else text.find("current month"), today.strftime("%B %Y"), today.replace(day=1), today))
    if re.search(r"\b(last|previous) month\b", text):
        previous_month_end = today.replace(day=1) - timedelta(days=1)
        start = previous_month_end.replace(day=1)
        ranges.append((text.find("last month") if "last month" in text else text.find("previous month"), start.strftime("%B %Y"), start, previous_month_end))
    ordered = []
    seen = set()
    for _pos, label, start, end in sorted(ranges, key=lambda item: item[0]):
        key = (start.year, start.month)
        if key not in seen:
            ordered.append((label, start, end))
            seen.add(key)
    return ordered


def comparison_periods(message):
    ranges = named_month_ranges(message)
    if len(ranges) >= 2:
        return ranges[:2]
    text = normalized(message)
    if (
        "compared to last month" in text
        or "compare it with last month" in text
        or "compare with last month" in text
        or "compare this month and last month" in text
        or "compared to previous month" in text
        or "compare it with previous month" in text
        or "compare with previous month" in text
        or "compare this month with last month" in text
        or "previous month" in text and "perform" in text
    ):
        today = date.today()
        current = (today.strftime("%B %Y"), today.replace(day=1), today)
        previous_end = today.replace(day=1) - timedelta(days=1)
        previous = (previous_end.strftime("%B %Y"), previous_end.replace(day=1), previous_end)
        return [current, previous]
    return []


def attendance_rows_for_employee(employee_id):
    return fetch_employee_rows("attendance", employee_id)


def leave_status_for_date(employee_id, target):
    for row in fetch_employee_rows("leave_requests", employee_id):
        if str(row.get("status", "")).lower() != "approved":
            continue
        start = parse_hr_date(row.get("from_date"))
        end = parse_hr_date(row.get("to_date")) or start
        if start and end and start <= target <= end:
            return "Leave"
    return None


def attendance_row_for_date(employee_id, target):
    for row in attendance_rows_for_employee(employee_id):
        if parse_hr_date(row.get("date")) == target:
            return row
    return None


def attendance_status_for_date(employee_id, target):
    leave_status = leave_status_for_date(employee_id, target)
    if leave_status:
        return leave_status
    row = attendance_row_for_date(employee_id, target)
    if not row:
        return "Absent"
    return attendance_metrics(row)["status"]


def daily_attendance_detail(employee_id, target):
    row = attendance_row_for_date(employee_id, target)
    leave_status = leave_status_for_date(employee_id, target)
    if leave_status and not row:
        return {
            "date": target,
            "status": "Leave",
            "punch_in": None,
            "punch_out": None,
            "worked_hours": 0,
            "late_arrival": False,
            "early_departure": False,
            "overtime_hours": 0,
        }
    if not row:
        return {
            "date": target,
            "status": "Absent",
            "punch_in": None,
            "punch_out": None,
            "worked_hours": 0,
            "late_arrival": False,
            "early_departure": False,
            "overtime_hours": 0,
        }
    metrics = attendance_metrics(row)
    return {
        "date": target,
        "status": metrics["status"],
        "punch_in": row.get("punch_in"),
        "punch_out": row.get("punch_out"),
        "worked_hours": metrics["worked_hours"],
        "late_arrival": metrics["late_arrival"],
        "early_departure": metrics["early_departure"],
        "overtime_hours": metrics["overtime_hours"],
    }


def iter_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def attendance_details_for_range(employee_id, start, end):
    return [daily_attendance_detail(employee_id, current) for current in iter_dates(start, end)]


def attendance_summary(details):
    total = len(details)
    counts = Counter(detail["status"] for detail in details)
    late = sum(1 for detail in details if detail["late_arrival"])
    early = sum(1 for detail in details if detail["early_departure"])
    overtime = round(sum(float(detail["overtime_hours"] or 0) for detail in details), 2)
    present_weight = counts.get("Present", 0) + counts.get("Half Day", 0) * 0.5
    percentage = round((present_weight / total) * 100, 2) if total else 0.0
    return {
        "Present": counts.get("Present", 0),
        "Absent": counts.get("Absent", 0),
        "Leave": counts.get("Leave", 0),
        "Half Day": counts.get("Half Day", 0),
        "Late Arrivals": late,
        "Early Departures": early,
        "Overtime Hours": overtime,
        "Attendance Percentage": percentage,
    }


def format_daily_detail(detail):
    parts = [f"{detail['date'].isoformat()} : {detail['status']}"]
    if detail.get("punch_in"):
        parts.append(f"In {detail['punch_in']}")
    if detail.get("punch_out"):
        parts.append(f"Out {detail['punch_out']}")
    flags = []
    if detail.get("late_arrival"):
        flags.append("Late")
    if detail.get("early_departure"):
        flags.append("Early departure")
    if detail.get("overtime_hours"):
        flags.append(f"Overtime {detail['overtime_hours']:.2f}h")
    if flags:
        parts.append("(" + ", ".join(flags) + ")")
    return " ".join(parts)


def format_summary_block(summary):
    return [
        "Summary:",
        f"Present: {summary['Present']}",
        f"Absent: {summary['Absent']}",
        f"Leave: {summary['Leave']}",
        f"Half Day: {summary['Half Day']}",
        f"Late Arrivals: {summary['Late Arrivals']}",
        f"Early Departures: {summary['Early Departures']}",
        f"Overtime Hours: {summary['Overtime Hours']:.2f}",
        f"Attendance Percentage: {summary['Attendance Percentage']:.2f}%",
    ]


def attendance_counts_for_range(employee_id, start, end):
    return Counter(detail["status"] for detail in attendance_details_for_range(employee_id, start, end))


def all_attendance_response(employee_id, employee_name):
    rows = sorted(attendance_rows_for_employee(employee_id), key=lambda row: row_date(row, "date") or date.min)
    if not rows:
        return f"{employee_name}, I could not find any attendance records yet."
    lines = [f"{employee_name}, here is your complete attendance history:"]
    display_rows = rows[-90:]
    if len(rows) > len(display_rows):
        lines.append(f"Showing latest {len(display_rows)} of {len(rows)} records.")
    for row in display_rows:
        target = parse_hr_date(row.get("date"))
        if not target:
            continue
        lines.append(format_daily_detail(daily_attendance_detail(employee_id, target)))
    return "\n".join(lines)


def attendance_response(employee_id, employee_name, message):
    if is_attendance_comparison_message(message):
        return compare_attendance_response(employee_id, employee_name, message)
    if is_latest_attendance_message(message):
        return latest_attendance_record_response(employee_id, employee_name)
    if is_manager_attendance_message(message):
        return team_attendance_response(employee_id, employee_name, message)
    if is_attendance_metric_question(message):
        return attendance_metric_response(employee_id, employee_name, message)
    if is_absent_dates_query(message):
        return absent_dates_response(employee_id, employee_name, message)

    period = attendance_period(message)
    if not period:
        today = date.today()
        period = AttendancePeriod(today.strftime("%B %Y"), today.replace(day=1), today, "month")
    if period.kind == "all":
        remember_attendance_context(period)
        return all_attendance_response(employee_id, employee_name)
    if period.kind == "day" or (period.kind == "range" and period.start == period.end):
        remember_attendance_context(period)
        detail = daily_attendance_detail(employee_id, period.start)
        return (
            f"{employee_name}, attendance for {period.start.isoformat()}: {detail['status']}.\n"
            f"Punch In: {detail['punch_in'] or 'Not recorded'}\n"
            f"Punch Out: {detail['punch_out'] or 'Not recorded'}\n"
            f"Worked Hours: {detail['worked_hours']:.2f}\n"
            f"Late Arrival: {'Yes' if detail['late_arrival'] else 'No'}\n"
            f"Early Departure: {'Yes' if detail['early_departure'] else 'No'}\n"
            f"Overtime: {detail['overtime_hours']:.2f} hours\n\n"
            "Would you like to view attendance history, check another date, or view leave records?"
        )

    details = attendance_details_for_range(employee_id, period.start, period.end)
    summary = attendance_summary(details)
    remember_attendance_context(period)
    lines = [f"{employee_name}, attendance from {period.start.isoformat()} to {period.end.isoformat()} for {period.label}:"]
    lines.extend(format_summary_block(summary))
    lines.append("")
    lines.append("Daily breakdown:")
    for detail in details:
        lines.append(format_daily_detail(detail))
    lines.append("")
    lines.append("Would you like to compare this with another period or check a specific date?")
    return "\n".join(lines)


def is_absent_dates_query(message):
    text = normalized(message)
    return "absent" in text and any(
        phrase in text
        for phrase in (
            "which date",
            "which dates",
            "what date",
            "what dates",
            "dates i was absent",
            "when was i absent",
            "absent dates",
        )
    )


def absent_dates_response(employee_id, employee_name, message):
    period = attendance_period(message)
    if not period or not period.start or not period.end:
        today = date.today()
        period = AttendancePeriod(today.strftime("%B %Y"), today.replace(day=1), today, "month")
    if period.kind == "all":
        rows = sorted(attendance_rows_for_employee(employee_id), key=lambda row: row_date(row, "date") or date.min)
        if rows:
            period = AttendancePeriod(
                "complete attendance history",
                row_date(rows[0], "date"),
                row_date(rows[-1], "date"),
                "range",
            )
        else:
            return f"{employee_name}, I could not find any attendance records yet."
    details = attendance_details_for_range(employee_id, period.start, period.end)
    absent_dates = [detail["date"].isoformat() for detail in details if detail["status"] == "Absent"]
    remember_attendance_context(period, "absent")
    if not absent_dates:
        return f"{employee_name}, I found no absent dates from {period.start.isoformat()} to {period.end.isoformat()}."
    lines = [f"{employee_name}, absent dates from {period.start.isoformat()} to {period.end.isoformat()}:"]
    lines.extend(absent_dates)
    lines.append("")
    lines.append(f"Total Absent Days: {len(absent_dates)}")
    return "\n".join(lines)


def is_attendance_comparison_message(message):
    text = normalized(message)
    if has_request_context() and session.get("last_hr_topic") == "attendance" and "compare" in text:
        return True
    if "how did i perform" in text or "perform compared" in text:
        return "previous month" in text or "last month" in text or "compared" in text
    return "attendance" in text and ("compare" in text or "compared to" in text or "comparison" in text)


def compare_attendance_response(employee_id, employee_name, message):
    periods = comparison_periods(message)
    if len(periods) < 2:
        return f"{employee_name}, please mention two months or periods to compare attendance."
    first_label, first_start, first_end = periods[0]
    second_label, second_start, second_end = periods[1]
    first_summary = attendance_summary(attendance_details_for_range(employee_id, first_start, first_end))
    second_summary = attendance_summary(attendance_details_for_range(employee_id, second_start, second_end))
    percentage_delta = first_summary["Attendance Percentage"] - second_summary["Attendance Percentage"]
    lines = [f"{employee_name}, attendance comparison:"]
    lines.append(f"{first_label}: Present {first_summary['Present']}, Leave {first_summary['Leave']}, Absent {first_summary['Absent']}, Attendance {first_summary['Attendance Percentage']:.2f}%")
    lines.append(f"{second_label}: Present {second_summary['Present']}, Leave {second_summary['Leave']}, Absent {second_summary['Absent']}, Attendance {second_summary['Attendance Percentage']:.2f}%")
    lines.append(f"Difference: {percentage_delta:+.2f} percentage points.")
    lines.append(f"Late arrivals changed by {first_summary['Late Arrivals'] - second_summary['Late Arrivals']:+d}.")
    lines.append(f"Overtime changed by {first_summary['Overtime Hours'] - second_summary['Overtime Hours']:+.2f} hours.")
    lines.append("Would you like to view detailed attendance history or check a specific date?")
    return "\n".join(lines)


def is_latest_attendance_message(message):
    text = normalized(message)
    return "latest attendance" in text or "last attendance record" in text or "most recent attendance" in text


def latest_attendance_record_response(employee_id, employee_name):
    rows = sorted(attendance_rows_for_employee(employee_id), key=lambda row: row_date(row, "date") or date.min, reverse=True)
    if not rows:
        return f"{employee_name}, I could not find any attendance records yet."
    row = rows[0]
    target = parse_hr_date(row.get("date"))
    detail = daily_attendance_detail(employee_id, target) if target else {"status": row.get("status") or "No status", "punch_in": row.get("punch_in"), "punch_out": row.get("punch_out")}
    return (
        f"{employee_name}, your latest attendance record is {row.get('date')}: {detail['status']}.\n"
        f"Punch In: {detail.get('punch_in') or 'Not recorded'}\n"
        f"Punch Out: {detail.get('punch_out') or 'Not recorded'}\n\n"
        "Would you like to view attendance history or check a specific date?"
    )


def is_attendance_metric_question(message):
    text = normalized(message)
    metric_question = any(
        phrase in text
        for phrase in (
            "how many days was i late",
            "how many days were i late",
            "how many times was i late",
            "how often was i late",
            "late last month",
            "late this month",
            "leave early",
            "left early",
            "early departure",
            "early departures",
            "overtime",
            "half days",
            "half day",
        )
    )
    return metric_question


def attendance_metric_response(employee_id, employee_name, message):
    period = attendance_period(message) or extract_month_period("this month")
    if not period or not period.start:
        today = date.today()
        period = AttendancePeriod(today.strftime("%B %Y"), today.replace(day=1), today, "month")
    details = attendance_details_for_range(employee_id, period.start, period.end)
    summary = attendance_summary(details)
    text = normalized(message)
    if "overtime" in text:
        remember_attendance_context(period, "overtime")
        return f"{employee_name}, your overtime for {period.label} is {summary['Overtime Hours']:.2f} hours."
    if "late" in text:
        remember_attendance_context(period, "late")
        return f"{employee_name}, you had {summary['Late Arrivals']} late arrival(s) in {period.label}."
    if "early" in text or "leave early" in text or "left early" in text:
        remember_attendance_context(period, "early_departure")
        if period.kind == "day":
            detail = details[0]
            return f"{employee_name}, early departure on {detail['date'].isoformat()}: {'Yes' if detail['early_departure'] else 'No'}."
        return f"{employee_name}, you had {summary['Early Departures']} early departure(s) in {period.label}."
    if "half" in text:
        remember_attendance_context(period, "half_day")
        return f"{employee_name}, you had {summary['Half Day']} half day(s) in {period.label}."
    return attendance_response(employee_id, employee_name, message)


def remember_attendance_context(period, metric=None):
    if not has_request_context() or not period:
        return
    session["last_hr_topic"] = "attendance"
    if period.start and period.end:
        session["last_attendance_period"] = {
            "label": period.label,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "kind": period.kind,
        }
    if metric:
        session["last_attendance_metric"] = metric
    session.modified = True


def is_attendance_message(message):
    text = normalized(message)
    return (
        "attendance" in text
        or "present" in text
        or "absent" in text
        or "did you punch" in text
        or "did i punch" in text
        or "was i punched" in text
        or "was i punch" in text
        or "punch status" in text
        or "am i punched" in text
        or "attendance log" in text
        or "attendance logs" in text
        or "punch-in record" in text
        or "punch out record" in text
        or "not punched in" in text
        or "not punched out" in text
        or "team's attendance" in text
        or "team attendance" in text
        or is_attendance_comparison_message(message)
        or is_attendance_metric_question(message)
    )


def is_attendance_correction_message(message):
    text = normalized(message)
    if "mark me present" in text:
        return any(token in text for token in ("yesterday", "day before", "last ", " for ", " on ")) and "today" not in text
    return any(
        phrase in text
        for phrase in (
            "forgot to punch in",
            "forgot punch in",
            "forgot to punch out",
            "forgot punch out",
            "forgot to mark attendance",
            "forgot to mark my attendance",
            "forgot mark attendance",
            "forgot mark my attendance",
            "missed punch in",
            "missed punch out",
            "missed my punch in",
            "missed my punch out",
            "missed attendance",
            "missed my attendance",
            "attendance is incorrect",
            "attendance is wrong",
            "my attendance is wrong",
            "fix my attendance",
            "attendance correction",
            "correct my attendance",
            "worked yesterday from",
            "i worked yesterday from",
        )
    )


def is_attendance_correction_status_message(message):
    text = normalized(message)
    return "correction" in text and any(word in text for word in ("status", "pending", "request", "requests", "show", "latest"))


def parse_times_from_message(message):
    text = re.sub(r"\b\d{4}-\d{1,2}-\d{1,2}\b", " ", str(message or ""))
    text = re.sub(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", " ", text)
    pattern = r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)\b"
    values = []
    for match in re.finditer(pattern, text):
        raw = match.group(1).strip()
        if re.fullmatch(r"\d{4}", raw):
            continue
        parsed = parse_time_value(raw)
        if parsed:
            values.append(parsed.strftime("%H:%M:%S"))
    return values


def usable_time_value(value):
    text = str(value or "").strip()
    if not text or normalized(text) in {"now", "current time", "right now"}:
        return None
    parsed = parse_time_value(text)
    return parsed.strftime("%H:%M:%S") if parsed else None


def infer_punch_out_time(value, message):
    parsed = parse_time_value(value)
    if not parsed:
        return None
    raw = str(value or "")
    if re.search(r"\b(am|pm)\b", raw, re.IGNORECASE):
        return parsed.strftime("%H:%M:%S")
    text = normalized(message)
    if "punch out" in text or "punch-out" in text or "checkout" in text or "check out" in text:
        if parsed.hour < 12:
            parsed = time(parsed.hour + 12, parsed.minute, parsed.second)
    return parsed.strftime("%H:%M:%S")


def infer_correction_payload(message, existing=None, entities=None):
    payload = dict(existing or {})
    entities = entities or {}
    text = normalized(message)

    for target_key, source_key in (("attendance_date", "attendance_date"), ("attendance_date", "start_date"), ("attendance_date", "from_date"), ("punch_in", "punch_in"), ("punch_out", "punch_out"), ("reason", "reason")):
        value = entities.get(source_key)
        if not value:
            continue
        if target_key == "punch_in":
            parsed = usable_time_value(value)
            if parsed:
                payload[target_key] = parsed
        elif target_key == "punch_out":
            parsed = infer_punch_out_time(value, message)
            if parsed:
                payload[target_key] = parsed
        else:
            payload[target_key] = str(value)

    period = attendance_period(message)
    if period and period.start and period.kind == "day":
        payload["attendance_date"] = period.start.isoformat()
    elif "yesterday" in text and "day before yesterday" not in text:
        payload["attendance_date"] = (date.today() - timedelta(days=1)).isoformat()
    elif "day before yesterday" in text:
        payload["attendance_date"] = (date.today() - timedelta(days=2)).isoformat()

    if "forgot to punch in" in text or "forgot punch in" in text or "missed punch in" in text or "missed my punch in" in text:
        payload["correction_type"] = "missing_punch_in"
    elif "forgot to punch out" in text or "forgot punch out" in text or "missed punch out" in text or "missed my punch out" in text:
        payload["correction_type"] = "missing_punch_out"
    elif (
        "forgot to mark attendance" in text
        or "forgot to mark my attendance" in text
        or "forgot mark attendance" in text
        or "forgot mark my attendance" in text
        or "missed attendance" in text
        or "missed my attendance" in text
    ):
        payload["correction_type"] = "mark_present"
    elif "mark me present" in text:
        payload["correction_type"] = "mark_present"
    elif "incorrect" in text or "fix my attendance" in text or "correct my attendance" in text:
        payload["correction_type"] = "incorrect_record"
    elif "worked" in text and "from" in text:
        payload["correction_type"] = "worked_hours"

    times = parse_times_from_message(message)
    explicit_punch_out_update = "punch out" in text or "punch-out" in text or "checkout" in text or "check out" in text
    if len(times) >= 1 and (not payload.get("punch_in") or payload.get("punch_in") == "now") and not explicit_punch_out_update:
        payload["punch_in"] = times[0]
    if len(times) >= 2 and (not payload.get("punch_out") or payload.get("punch_out") == "now"):
        payload["punch_out"] = infer_punch_out_time(times[1], message) or times[1]
    elif len(times) >= 1 and explicit_punch_out_update:
        payload["punch_out"] = infer_punch_out_time(times[0], message) or times[0]

    if not payload.get("reason") and is_attendance_correction_message(message):
        payload["reason"] = str(message).strip()
    return payload


def next_correction_step(payload):
    if not payload.get("attendance_date"):
        return "attendance_date"
    if not payload.get("correction_type"):
        return "correction_type"
    if payload.get("correction_type") in {"missing_punch_in", "mark_present", "worked_hours", "incorrect_record"} and not payload.get("punch_in"):
        return "punch_in"
    if payload.get("correction_type") in {"missing_punch_out", "mark_present", "worked_hours", "incorrect_record"} and not payload.get("punch_out"):
        return "punch_out"
    if not payload.get("reason"):
        return "reason"
    return "confirm"


def attendance_correction_step_prompt(step):
    prompts = {
        "attendance_date": "For which date should I raise the attendance correction request?",
        "correction_type": "What needs correction: missing punch in, missing punch out, mark present, or incorrect record?",
        "punch_in": "Please share the correct punch-in time for this attendance correction, for example 09:30 AM.",
        "punch_out": "Please share the correct punch-out time for this attendance correction, for example 06:30 PM.",
        "reason": "Please share a short reason or comment for your manager.",
        "confirm": "Please confirm if you want me to submit this attendance correction request.",
    }
    return prompts.get(step, "Please provide the missing attendance correction detail.")


def format_correction_confirmation(employee_name, payload):
    return (
        f"{employee_name}, please confirm this attendance correction request:\n"
        f"Date: {payload.get('attendance_date')}\n"
        f"Correction Type: {payload.get('correction_type')}\n"
        f"Punch In: {payload.get('punch_in') or 'Not requested'}\n"
        f"Punch Out: {payload.get('punch_out') or 'Not requested'}\n"
        f"Reason: {payload.get('reason')}\n\n"
        "Reply with confirm to submit, or cancel to discard it."
    )


def handle_apply_attendance_correction(employee_id, employee_name, entities=None, user_message=""):
    workflow = get_active_workflow(employee_id, ATTENDANCE_CORRECTION_WORKFLOW)
    payload = infer_correction_payload(user_message, workflow.get("payload") if workflow else {}, entities)
    step = next_correction_step(payload)
    workflow = upsert_workflow(employee_id, ATTENDANCE_CORRECTION_WORKFLOW, step, payload)
    if step != "confirm":
        return json_reply(attendance_correction_step_prompt(step))
    return json_reply(format_correction_confirmation(employee_name, workflow["payload"]))


def handle_confirm_attendance_correction(employee_id, employee_name):
    workflow = get_active_workflow(employee_id, ATTENDANCE_CORRECTION_WORKFLOW)
    if not workflow:
        return json_reply("No active attendance correction request is waiting for confirmation.")
    payload = workflow.get("payload") or {}
    step = next_correction_step(payload)
    if step != "confirm":
        upsert_workflow(employee_id, ATTENDANCE_CORRECTION_WORKFLOW, step, payload)
        return json_reply(attendance_correction_step_prompt(step))
    employee = get_employee(employee_id)
    insert_payload = {
        "employee_id": employee_id,
        "manager_id": employee.get("manager_id"),
        "attendance_date": payload.get("attendance_date"),
        "requested_punch_in": payload.get("punch_in"),
        "requested_punch_out": payload.get("punch_out"),
        "correction_type": payload.get("correction_type"),
        "reason": payload.get("reason"),
        "status": "Pending",
    }
    try:
        supabase.table("attendance_correction_requests").insert(insert_payload).execute()
    except Exception:
        compatible_payload = dict(insert_payload)
        compatible_payload.pop("manager_id", None)
        try:
            supabase.table("attendance_correction_requests").insert(compatible_payload).execute()
        except Exception:
            return json_reply(
                "I could not submit the attendance correction because the database schema is not ready. Please apply the latest schema.sql and try again.",
                500,
            )
    try:
        finish_workflow(workflow["id"])
    except Exception:
        return json_reply(
            "Your attendance correction was saved, but I could not close the workflow state. Please refresh the chat before starting another correction.",
            500,
        )
    return json_reply(f"{employee_name}, your attendance correction request has been submitted for manager approval.")


def attendance_correction_status_response(employee_id, employee_name, message):
    rows = fetch_employee_rows("attendance_correction_requests", employee_id)
    status = "pending" if "pending" in normalized(message) else None
    if status:
        rows = [row for row in rows if str(row.get("status", "")).lower() == status]
    rows = sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)
    if not rows:
        return f"{employee_name}, I could not find any attendance correction requests matching that status."
    lines = [f"{employee_name}, here are your attendance correction requests:"]
    for row in rows[:10]:
        lines.append(
            f"#{row.get('id')} - {row.get('attendance_date')} - {row.get('correction_type')} - {row.get('status')}"
            + (f" ({row.get('manager_comments')})" if row.get("manager_comments") else "")
        )
    return "\n".join(lines)


def get_employee(employee_id):
    response = supabase.table("employees").select("*").eq("employee_id", employee_id).limit(1).execute()
    return (response.data or [{}])[0]


def team_members(manager_id):
    rows = fetch_all_rows("employees")
    return [row for row in rows if str(row.get("manager_id")) == str(manager_id)]


def is_manager_attendance_message(message):
    text = normalized(message)
    return any(
        phrase in text
        for phrase in (
            "team attendance",
            "team's attendance",
            "who is absent",
            "who was absent",
            "not punched in",
            "not punched out",
            "my team",
        )
    )


def team_attendance_response(manager_id, manager_name, message):
    members = team_members(manager_id)
    if not members:
        return f"{manager_name}, I could not find direct reports mapped to you."
    period = attendance_period(message)
    target = period.start if period and period.kind == "day" else date.today()
    text = normalized(message)
    rows = []
    for member in members:
        detail = daily_attendance_detail(member.get("employee_id"), target)
        rows.append((member, detail))
    if "not punched in" in text:
        rows = [(member, detail) for member, detail in rows if not detail.get("punch_in")]
        title = f"Employees who have not punched in on {target.isoformat()}"
    elif "not punched out" in text:
        rows = [(member, detail) for member, detail in rows if detail.get("punch_in") and not detail.get("punch_out")]
        title = f"Employees who have not punched out on {target.isoformat()}"
    elif "absent" in text:
        rows = [(member, detail) for member, detail in rows if detail["status"] == "Absent"]
        title = f"Employees absent on {target.isoformat()}"
    else:
        title = f"Team attendance for {target.isoformat()}"
    if not rows:
        return f"{manager_name}, no matching team attendance records found for {target.isoformat()}."
    lines = [f"{manager_name}, {title}:"]
    for member, detail in rows:
        lines.append(f"{member.get('name') or member.get('employee_id')}: {detail['status']} (In: {detail.get('punch_in') or 'Not recorded'}, Out: {detail.get('punch_out') or 'Not recorded'})")
    return "\n".join(lines)


def correction_id_value(row):
    return str(row.get("id", ""))


def get_seen_pending_correction_ids():
    return set(str(item) for item in session.get("seen_pending_attendance_correction_ids", []))


def mark_pending_corrections_seen(rows):
    seen = get_seen_pending_correction_ids()
    seen.update(correction_id_value(row) for row in rows if correction_id_value(row))
    session["seen_pending_attendance_correction_ids"] = sorted(seen)
    session.modified = True


def get_attendance_corrections_by_status(status):
    response = supabase.table("attendance_correction_requests").select("*").eq("status", status).execute()
    return response.data or []


def employee_name_lookup():
    return {row.get("employee_id"): row.get("name") for row in fetch_all_rows("employees") if row.get("employee_id")}


def enrich_correction_rows(rows):
    names = employee_name_lookup()
    enriched = []
    for row in rows:
        item = dict(row)
        item["employee_name"] = names.get(row.get("employee_id"), row.get("employee_id"))
        enriched.append(item)
    return enriched


def get_pending_attendance_corrections(mark_seen=False):
    rows = get_attendance_corrections_by_status("Pending")
    seen = get_seen_pending_correction_ids() if has_request_context() else set()
    for row in rows:
        row["_is_new"] = correction_id_value(row) not in seen
    rows = sorted(rows, key=lambda row: (correction_id_value(row) in seen, row.get("created_at") or ""), reverse=False)
    if mark_seen and has_request_context():
        mark_pending_corrections_seen(rows)
    return rows


def get_unseen_pending_attendance_correction_count():
    rows = get_attendance_corrections_by_status("Pending")
    seen = get_seen_pending_correction_ids() if has_request_context() else set()
    return sum(1 for row in rows if correction_id_value(row) not in seen)


def render_attendance_corrections():
    if session.get("role", "").lower() != "manager":
        return redirect("/")
    view = request.args.get("view", "pending").lower()
    if view not in {"pending", "approved", "rejected"}:
        view = "pending"
    rows = get_attendance_corrections_by_status(view.title()) if view != "pending" else get_pending_attendance_corrections(mark_seen=True)
    rows = enrich_correction_rows(rows)
    return render_template(
        "manager_attendance.html",
        corrections=rows,
        active_view=view,
        pending_count=len(get_attendance_corrections_by_status("Pending")),
        approved_count=len(get_attendance_corrections_by_status("Approved")),
        rejected_count=len(get_attendance_corrections_by_status("Rejected")),
    )


def redirect_to_attendance_corrections(message=None, category="info"):
    if message:
        flash(message, category)
    return redirect("/manager/attendance")


def approve_attendance_correction(correction_id):
    if session.get("role", "").lower() != "manager":
        return redirect("/")
    response = (
        supabase.table("attendance_correction_requests")
        .select("*")
        .eq("id", correction_id)
        .eq("status", "Pending")
        .limit(1)
        .execute()
    )
    if not response.data:
        return redirect_to_attendance_corrections("This attendance correction is no longer pending.", "error")
    correction = response.data[0]
    attendance_date = correction.get("attendance_date")
    employee_id = correction.get("employee_id")
    existing = (
        supabase.table("attendance")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("date", attendance_date)
        .limit(1)
        .execute()
    )
    payload = {
        "employee_id": employee_id,
        "date": attendance_date,
        "punch_in": correction.get("requested_punch_in"),
        "punch_out": correction.get("requested_punch_out"),
        "status": "Present",
    }
    metrics = attendance_metrics(payload)
    payload.update(
        {
            "worked_hours": metrics["worked_hours"],
            "late_arrival": metrics["late_arrival"],
            "early_departure": metrics["early_departure"],
            "overtime_hours": metrics["overtime_hours"],
            "attendance_type": metrics["attendance_type"],
        }
    )
    if existing.data:
        supabase.table("attendance").update(payload).eq("id", existing.data[0]["id"]).execute()
    else:
        supabase.table("attendance").insert(payload).execute()
    supabase.table("attendance_correction_requests").update(
        {
            "status": "Approved",
            "manager_comments": request.form.get("comments", "").strip() or None,
            "resolved_by": session.get("employee_id"),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", correction_id).eq("status", "Pending").execute()
    return redirect_to_attendance_corrections("Attendance correction approved and attendance record updated.", "success")


def reject_attendance_correction(correction_id):
    if session.get("role", "").lower() != "manager":
        return redirect("/")
    reason = request.form.get("reason", "").strip()
    if not reason:
        return redirect_to_attendance_corrections("A rejection reason is required.", "error")
    response = (
        supabase.table("attendance_correction_requests")
        .update(
            {
                "status": "Rejected",
                "manager_comments": reason,
                "resolved_by": session.get("employee_id"),
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", correction_id)
        .eq("status", "Pending")
        .execute()
    )
    if not response.data:
        return redirect_to_attendance_corrections("This attendance correction was already processed.", "error")
    return redirect_to_attendance_corrections("Attendance correction rejected.", "success")

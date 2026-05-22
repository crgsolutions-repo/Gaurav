# =========================
# intent_handlers.py
# =========================

from flask import jsonify, session

from datetime import date, datetime, timedelta

from supabase_client import supabase


# =========================
# NORMALIZATION FUNCTIONS
# =========================

def normalize_leave_type(leave_type):

    leave_map = {

        "casual": "Casual Leave",
        "casual leave": "Casual Leave",

        "privilege": "Privilege Leave",
        "privilege leave": "Privilege Leave",

        "sick": "Sick Leave",
        "sick leave": "Sick Leave",

        "maternity": "Maternity Leave",
        "maternity leave": "Maternity Leave",

        "paternity": "Paternity Leave",
        "paternity leave": "Paternity Leave"
    }

    leave_type = leave_type.lower().strip()

    return leave_map.get(leave_type, leave_type.title())


def normalize_duration(duration):

    duration = duration.lower().strip()

    if "half" in duration:

        return "Half Day"

    return "Full Day"


def normalize_date(date_text):

    date_text = date_text.lower().strip()

    today = date.today()

    if date_text == "today":

        return str(today)

    if date_text == "tomorrow":

        return str(today + timedelta(days=1))

    return date_text


# =========================
# PUNCH IN
# =========================

def handle_punch_in(employee_id, employee_name, ai_result):

    today = str(date.today())

    existing_record = supabase.table("attendance") \
        .select("*") \
        .eq("employee_id", employee_id) \
        .eq("date", today) \
        .execute()

    if existing_record.data:

        return jsonify({
            "reply": f"{employee_name}, you already punched in today."
        })

    current_time = datetime.now().time().strftime("%H:%M:%S")

    supabase.table("attendance").insert({
        "employee_id": employee_id,
        "date": today,
        "punch_in": current_time,
        "status": "Present"
    }).execute()

    return jsonify({
        "reply": ai_result["reply"]
    })


# =========================
# PUNCH OUT
# =========================

def handle_punch_out(employee_id, employee_name, ai_result):

    today = str(date.today())

    existing_record = supabase.table("attendance") \
        .select("*") \
        .eq("employee_id", employee_id) \
        .eq("date", today) \
        .execute()

    if not existing_record.data:

        return jsonify({
            "reply": f"{employee_name}, you haven't punched in today."
        })

    if existing_record.data[0]["punch_out"]:

        return jsonify({
            "reply": f"{employee_name}, you already punched out today."
        })

    current_time = datetime.now().time().strftime("%H:%M:%S")

    attendance_id = existing_record.data[0]["id"]

    supabase.table("attendance").update({
        "punch_out": current_time
    }).eq("id", attendance_id).execute()

    return jsonify({
        "reply": ai_result["reply"]
    })


# =========================
# LEAVE BALANCE
# =========================

def handle_leave_balance(employee_id, employee_name):

    leave_data = supabase.table("employee_leave_balance") \
        .select("*") \
        .eq("employee_id", employee_id) \
        .execute()

    leave_summary = ""

    for leave in leave_data.data:

        leave_summary += f"""
Leave Type: {leave['leave_type']}
Remaining Leaves: {leave['remaining_leaves']}
"""

    return jsonify({
        "reply": leave_summary
    })


# =========================
# APPLY LEAVE
# =========================

def handle_apply_leave(ai_result):

    session["pending_leave"] = ai_result

    return jsonify({
        "reply": ai_result["reply"]
    })


# =========================
# CONFIRM LEAVE
# =========================

def handle_confirm_leave(employee_id, employee_name):

    pending_leave = session.get("pending_leave")

    if not pending_leave:

        return jsonify({
            "reply": "No pending leave request found."
        })

    # =========================
    # NORMALIZATION
    # =========================

    leave_type = normalize_leave_type(
        pending_leave["leave_type"]
    )

    from_date = normalize_date(
        pending_leave["from_date"]
    )

    to_date = normalize_date(
        pending_leave["to_date"]
    )

    duration = normalize_duration(
        pending_leave["duration"]
    )

    # =========================
    # VALIDATION
    # =========================

    if leave_type == "Unknown":

        return jsonify({
            "reply": "Please specify leave type."
        })

    if from_date == "unknown":

        return jsonify({
            "reply": "Please specify leave date."
        })

    # =========================
    # LEAVE BALANCE CHECK
    # =========================

    balance_data = supabase.table("employee_leave_balance") \
        .select("*") \
        .eq("employee_id", employee_id) \
        .eq("leave_type", leave_type) \
        .execute()

    if not balance_data.data:

        return jsonify({
            "reply": f"{leave_type} balance not found."
        })

    remaining_leaves = float(
        balance_data.data[0]["remaining_leaves"]
    )

    # =========================
    # DEDUCTION LOGIC
    # =========================

    deduction = 1

    if duration == "Half Day":

        deduction = 0.5

    if remaining_leaves < deduction:

        return jsonify({
            "reply": f"Insufficient {leave_type} balance."
        })

    # =========================
    # DUPLICATE CHECK
    # =========================

    existing_leave = supabase.table("leave_requests") \
        .select("*") \
        .eq("employee_id", employee_id) \
        .eq("from_date", from_date) \
        .execute()

    if existing_leave.data:

        return jsonify({
            "reply": "You already have a leave request for this date."
        })

    # =========================
    # INSERT LEAVE REQUEST
    # =========================

    supabase.table("leave_requests").insert({

        "employee_id": employee_id,
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "leave_duration": duration,
        "status": "Pending"

    }).execute()

    # =========================
    # UPDATE LEAVE BALANCE
    # =========================

    used_leaves = float(
    balance_data.data[0]["used_leaves"]
    )

    new_remaining = remaining_leaves - deduction

    new_used = used_leaves + deduction

    supabase.table("employee_leave_balance").update({

    "remaining_leaves": new_remaining,
    "used_leaves": new_used

    }).eq("employee_id", employee_id) \
    .eq("leave_type", leave_type) \
    .execute()

    # =========================
    # CLEAR SESSION
    # =========================

    session.pop("pending_leave", None)

    return jsonify({
        "reply": f"{employee_name}, your leave request has been submitted successfully."
    })
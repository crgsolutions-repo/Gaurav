from datetime import date, datetime, timezone

from flask import flash, redirect, render_template, request, session

from intent_handlers import calculate_leave_days, parse_hr_date
from supabase_client import supabase


def require_manager():
    return session.get("role", "").lower() == "manager"


def redirect_to_approvals(message=None, category="info"):
    if message:
        flash(message, category)
    return redirect("/manager/leaves")


def leave_id_value(leave):
    return str(leave.get("id", ""))


def get_seen_pending_leave_ids():
    return set(str(leave_id) for leave_id in session.get("seen_pending_leave_ids", []))


def mark_pending_leaves_seen(leaves):
    seen_ids = get_seen_pending_leave_ids()
    seen_ids.update(leave_id_value(leave) for leave in leaves if leave_id_value(leave))
    session["seen_pending_leave_ids"] = sorted(seen_ids)
    session.modified = True


def parse_created_at(leave):
    raw_value = leave.get("created_at") or leave.get("updated_at") or ""
    if not raw_value:
        return 0

    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def numeric_leave_id(leave):
    try:
        return int(leave.get("id"))
    except (TypeError, ValueError):
        return -1


def sort_pending_leaves(leaves, seen_ids=None):
    seen_ids = seen_ids or set()

    return sorted(
        leaves,
        key=lambda leave: (
            leave_id_value(leave) in seen_ids,
            -parse_created_at(leave),
            -numeric_leave_id(leave),
            parse_hr_date(leave.get("from_date")) or date.max,
        ),
    )


def sort_approved_leaves(leaves):
    return sorted(
        leaves,
        key=lambda leave: (
            parse_hr_date(leave.get("from_date")) or date.min,
            parse_created_at(leave),
        ),
        reverse=True,
    )


def get_leave_requests_by_status(status):
    response = (
        supabase.table("leave_requests")
        .select("*")
        .eq("status", status)
        .execute()
    )
    return response.data or []


def get_pending_leave_requests(mark_seen=False):
    leaves = get_leave_requests_by_status("Pending")
    seen_ids = get_seen_pending_leave_ids()

    for leave in leaves:
        leave["_is_new"] = leave_id_value(leave) not in seen_ids

    sorted_leaves = sort_pending_leaves(leaves, seen_ids)
    if mark_seen:
        mark_pending_leaves_seen(sorted_leaves)

    return sorted_leaves


def get_approved_leave_requests():
    return sort_approved_leaves(get_leave_requests_by_status("Approved"))


def get_unseen_pending_leave_count():
    pending_leaves = get_leave_requests_by_status("Pending")
    seen_ids = get_seen_pending_leave_ids()
    return sum(1 for leave in pending_leaves if leave_id_value(leave) not in seen_ids)


def selected_leave_date():
    selected = parse_hr_date(request.args.get("leave_date"))
    return selected or date.today()


def employee_name_lookup():
    response = supabase.table("employees").select("employee_id,name").execute()
    return {
        row.get("employee_id"): row.get("name")
        for row in (response.data or [])
        if row.get("employee_id")
    }


def enrich_employee_names(leaves):
    names = employee_name_lookup()
    enriched_leaves = []
    for leave in leaves:
        enriched_leave = dict(leave)
        employee_id = enriched_leave.get("employee_id")
        enriched_leave["employee_name"] = names.get(employee_id, employee_id)
        enriched_leaves.append(enriched_leave)
    return enriched_leaves


def get_employees_on_leave(target_date):
    leaves = get_leave_requests_by_status("Approved")
    on_leave = []

    for leave in enrich_employee_names(leaves):
        from_date = parse_hr_date(leave.get("from_date"))
        to_date = parse_hr_date(leave.get("to_date")) or from_date
        if not from_date or not to_date:
            continue
        if from_date <= target_date <= to_date:
            enriched_leave = dict(leave)
            on_leave.append(enriched_leave)

    return sorted(on_leave, key=lambda leave: str(leave.get("employee_name") or ""))


def get_employee_leave_history(employee_id):
    response = (
        supabase.table("leave_requests")
        .select("*")
        .eq("employee_id", employee_id)
        .execute()
    )
    leaves = response.data or []
    return sorted(
        leaves,
        key=lambda leave: (
            parse_hr_date(leave.get("from_date")) or date.min,
            parse_created_at(leave),
            numeric_leave_id(leave),
        ),
        reverse=True,
    )


def render_employee_leave_history():
    if "employee_id" not in session:
        return redirect("/login")

    return render_template(
        "employee_leaves.html",
        leaves=get_employee_leave_history(session["employee_id"]),
        employee_name=session.get("employee_name", "Employee"),
    )


def render_leave_approvals():
    if not require_manager():
        return redirect("/")

    view = request.args.get("view", "pending").lower()
    if view not in {"pending", "approved"}:
        view = "pending"

    target_date = selected_leave_date()
    leaves = get_approved_leave_requests() if view == "approved" else get_pending_leave_requests(mark_seen=True)
    leaves = enrich_employee_names(leaves)

    return render_template(
        "manager_leaves.html",
        leaves=leaves,
        active_view=view,
        pending_count=len(get_pending_leave_requests()),
        approved_count=len(get_approved_leave_requests()),
        selected_date=target_date.isoformat(),
        employees_on_leave=get_employees_on_leave(target_date),
    )


def approve_leave_request(leave_id):
    if not require_manager():
        return redirect("/")

    leave_response = (
        supabase.table("leave_requests")
        .select("*")
        .eq("id", leave_id)
        .eq("status", "Pending")
        .limit(1)
        .execute()
    )
    if not leave_response.data:
        return redirect_to_approvals("This leave request is no longer pending.", "error")

    leave = leave_response.data[0]
    balance_response = (
        supabase.table("employee_leave_balance")
        .select("*")
        .eq("employee_id", leave["employee_id"])
        .eq("leave_type", leave["leave_type"])
        .limit(1)
        .execute()
    )
    if not balance_response.data:
        return redirect_to_approvals("No leave balance was found for this employee and leave type.", "error")

    balance = balance_response.data[0]
    from_date = parse_hr_date(leave.get("from_date"))
    to_date = parse_hr_date(leave.get("to_date")) or from_date
    if not from_date or not to_date:
        return redirect_to_approvals("This leave request has invalid dates and cannot be approved.", "error")

    deduction = calculate_leave_days(from_date, to_date, leave.get("leave_duration"))
    remaining = float(balance.get("remaining_leaves", 0))
    used = float(balance.get("used_leaves", 0))
    if remaining < deduction:
        return redirect_to_approvals("Insufficient leave balance to approve this request.", "error")

    status_response = (
        supabase.table("leave_requests")
        .update({"status": "Approved"})
        .eq("id", leave_id)
        .eq("status", "Pending")
        .execute()
    )
    if not status_response.data:
        return redirect_to_approvals("This leave request was already processed.", "error")

    try:
        supabase.table("employee_leave_balance").update(
            {
                "remaining_leaves": remaining - deduction,
                "used_leaves": used + deduction,
            }
        ).eq("employee_id", leave["employee_id"]).eq("leave_type", leave["leave_type"]).execute()
    except Exception:
        supabase.table("leave_requests").update({"status": "Pending"}).eq("id", leave_id).eq(
            "status", "Approved"
        ).execute()
        return redirect_to_approvals("Approval failed while updating leave balance. Please try again.", "error")

    return redirect_to_approvals("Leave request approved.", "success")


def reject_leave_request(leave_id):
    if not require_manager():
        return redirect("/")

    reason = request.form.get("reason", "").strip()
    if not reason:
        return redirect_to_approvals("A rejection reason is required.", "error")

    response = (
        supabase.table("leave_requests")
        .update(
            {
                "status": "Rejected",
                "rejection_reason": reason,
                "rejected_by": session.get("employee_name") or session.get("employee_id"),
                "rejected_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", leave_id)
        .eq("status", "Pending")
        .execute()
    )
    if not response.data:
        return redirect_to_approvals("This leave request was already processed.", "error")
    return redirect_to_approvals("Leave request rejected.", "success")

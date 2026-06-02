from datetime import date, datetime, timezone

from flask import flash, redirect, render_template, request, session

from expense_service import money
from supabase_client import supabase


def require_manager():
    return session.get("role", "").lower() == "manager"


def redirect_to_expenses(message=None, category="info"):
    if message:
        flash(message, category)
    return redirect("/manager/expenses")


def parse_created_at(expense):
    raw_value = expense.get("submitted_at") or expense.get("uploaded_at") or expense.get("created_at") or ""
    if not raw_value:
        return 0
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def expense_id_value(expense):
    return str(expense.get("id", ""))


def get_seen_pending_expense_ids():
    return set(str(expense_id) for expense_id in session.get("seen_pending_expense_ids", []))


def mark_pending_expenses_seen(expenses):
    seen_ids = get_seen_pending_expense_ids()
    seen_ids.update(expense_id_value(expense) for expense in expenses if expense_id_value(expense))
    session["seen_pending_expense_ids"] = sorted(seen_ids)
    session.modified = True


def get_expenses_by_status(status):
    response = supabase.table("expenses").select("*").eq("status", status).execute()
    return response.data or []


def sort_expenses(expenses, seen_ids=None):
    seen_ids = seen_ids or set()
    return sorted(
        expenses,
        key=lambda expense: (
            expense_id_value(expense) in seen_ids,
            -parse_created_at(expense),
            -int(expense.get("id") or 0) if str(expense.get("id") or "").isdigit() else 0,
        ),
    )


def get_pending_expenses(mark_seen=False):
    expenses = get_expenses_by_status("Pending")
    seen_ids = get_seen_pending_expense_ids()
    for expense in expenses:
        expense["_is_new"] = expense_id_value(expense) not in seen_ids
    sorted_expenses = sort_expenses(expenses, seen_ids)
    if mark_seen:
        mark_pending_expenses_seen(sorted_expenses)
    return sorted_expenses


def get_unseen_pending_expense_count():
    pending_expenses = get_expenses_by_status("Pending")
    seen_ids = get_seen_pending_expense_ids()
    return sum(1 for expense in pending_expenses if expense_id_value(expense) not in seen_ids)


def employee_name_lookup():
    response = supabase.table("employees").select("employee_id,name").execute()
    return {
        row.get("employee_id"): row.get("name")
        for row in (response.data or [])
        if row.get("employee_id")
    }


def enrich_employee_names(expenses):
    names = employee_name_lookup()
    enriched = []
    for expense in expenses:
        row = dict(expense)
        employee_id = row.get("employee_id")
        row["employee_name"] = names.get(employee_id, employee_id)
        if row.get("amount") is not None:
            row["amount_display"] = money(row["amount"])
        if row.get("ocr_amount") is not None:
            row["ocr_amount_display"] = money(row["ocr_amount"])
        enriched.append(row)
    return enriched


def render_expense_approvals():
    if not require_manager():
        return redirect("/")

    view = request.args.get("view", "pending").lower()
    if view not in {"pending", "approved", "rejected"}:
        view = "pending"

    if view == "approved":
        expenses = get_expenses_by_status("Approved")
    elif view == "rejected":
        expenses = get_expenses_by_status("Rejected")
    else:
        expenses = get_pending_expenses(mark_seen=True)

    expenses = enrich_employee_names(sort_expenses(expenses))
    return render_template(
        "manager_expenses.html",
        expenses=expenses,
        active_view=view,
        pending_count=len(get_pending_expenses()),
        approved_count=len(get_expenses_by_status("Approved")),
        rejected_count=len(get_expenses_by_status("Rejected")),
    )


def current_salary_month_values():
    today = date.today()
    return [today.strftime("%Y-%m"), today.strftime("%B %Y"), today.strftime("%b %Y")]


def add_expense_to_payroll(employee_id, amount):
    for salary_month in current_salary_month_values():
        response = (
            supabase.table("salary_records")
            .select("*")
            .eq("employee_id", employee_id)
            .eq("salary_month", salary_month)
            .limit(1)
            .execute()
        )
        if response.data:
            salary = response.data[0]
            reimbursement = float(salary.get("reimbursement") or 0) + float(amount)
            net_salary = float(salary.get("net_salary") or 0) + float(amount)
            update_response = (
                supabase.table("salary_records")
                .update({"reimbursement": reimbursement, "net_salary": net_salary})
                .eq("id", salary["id"])
                .execute()
            )
            return bool(update_response.data)
    return False


def approve_expense_request(expense_id):
    if not require_manager():
        return redirect("/")

    expense_response = (
        supabase.table("expenses")
        .select("*")
        .eq("id", expense_id)
        .eq("status", "Pending")
        .limit(1)
        .execute()
    )
    if not expense_response.data:
        return redirect_to_expenses("This expense request is no longer pending.", "error")

    expense = expense_response.data[0]
    status_response = (
        supabase.table("expenses")
        .update(
            {
                "status": "Approved",
                "approved_by": session.get("employee_name") or session.get("employee_id"),
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", expense_id)
        .eq("status", "Pending")
        .execute()
    )
    if not status_response.data:
        return redirect_to_expenses("This expense request was already processed.", "error")

    payroll_updated = add_expense_to_payroll(expense["employee_id"], expense["amount"])
    if payroll_updated:
        return redirect_to_expenses("Expense request approved and added to this month's payroll.", "success")
    return redirect_to_expenses(
        "Expense request approved. No current-month salary record was found to update.",
        "info",
    )


def reject_expense_request(expense_id):
    if not require_manager():
        return redirect("/")

    reason = request.form.get("reason", "").strip()
    if not reason:
        return redirect_to_expenses("A rejection reason is required.", "error")

    response = (
        supabase.table("expenses")
        .update(
            {
                "status": "Rejected",
                "rejection_reason": reason,
                "rejected_by": session.get("employee_name") or session.get("employee_id"),
                "rejected_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", expense_id)
        .eq("status", "Pending")
        .execute()
    )
    if not response.data:
        return redirect_to_expenses("This expense request was already processed.", "error")
    return redirect_to_expenses("Expense request rejected.", "success")

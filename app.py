import logging

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session

from config import Config, require_config
from expense_service import (
    EXPENSE_WORKFLOW,
    handle_apply_expense,
    handle_confirm_expense,
    receipt_path,
    save_expense_receipt,
)
from gemini_service import process_hr_request
from ocr_service import log_ocr_diagnostics
from payroll_service import (
    download_payslip_pdf,
    handle_payroll_query,
    is_payroll_message,
    render_payslip,
)
from intent_handlers import (
    LEAVE_WORKFLOW,
    handle_apply_leave,
    handle_cancel_workflow,
    handle_confirm_leave,
    handle_general_hr_query,
    handle_leave_balance,
    handle_punch_in,
    handle_punch_out,
    json_reply,
    log_conversation,
)
from manager_expenses import (
    approve_expense_request,
    get_unseen_pending_expense_count,
    reject_expense_request,
    render_expense_approvals,
)
from manager_approval import (
    approve_leave_request,
    get_unseen_pending_leave_count,
    reject_leave_request,
    render_employee_leave_history,
    render_leave_approvals,
)
from supabase_client import supabase
from workflow_store import WorkflowStoreError, clear_active_workflows, get_active_workflow


require_config("FLASK_SECRET_KEY")

app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
logging.basicConfig(level=logging.INFO)
log_ocr_diagnostics()


def current_employee_context():
    context = {
        "employee_id": session.get("employee_id"),
        "employee_name": session.get("employee_name"),
        "role": session.get("role"),
    }
    if str(context.get("role", "")).lower() == "manager":
        try:
            context["unseen_leave_approval_count"] = get_unseen_pending_leave_count()
        except Exception:
            context["unseen_leave_approval_count"] = 0
        try:
            context["unseen_expense_approval_count"] = get_unseen_pending_expense_count()
        except Exception:
            context["unseen_expense_approval_count"] = 0
    return context


def finalize_chat_response(employee_id, user_message, result):
    response = result[0] if isinstance(result, tuple) else result
    data = response.get_json(silent=True) or {}
    bot_reply = data.get("reply", "")
    if bot_reply:
        log_conversation(employee_id, user_message, bot_reply)
    return result


def local_workflow_result(user_message, workflow_type=LEAVE_WORKFLOW):
    message = user_message.strip().lower()
    cancel_phrases = {"cancel", "cancel it", "stop", "discard", "discard it"}
    confirm_phrases = {"confirm", "yes", "yes confirm", "yes submit", "submit", "submit it", "proceed"}

    if message in cancel_phrases:
        intent = "CANCEL_WORKFLOW"
    elif message in confirm_phrases:
        intent = "CONFIRM_EXPENSE" if workflow_type == EXPENSE_WORKFLOW else "CONFIRM_LEAVE"
    else:
        intent = "APPLY_EXPENSE" if workflow_type == EXPENSE_WORKFLOW else "APPLY_LEAVE"

    return {
        "intent": intent,
        "leave_type": "UNKNOWN",
        "from_date": "UNKNOWN",
        "to_date": "UNKNOWN",
        "duration": "UNKNOWN",
        "reason": "UNKNOWN",
        "amount": "UNKNOWN",
        "expense_type": "UNKNOWN",
        "description": "UNKNOWN",
        "reply": "",
    }


@app.route("/")
def home():
    if "employee_id" not in session:
        return redirect("/login")

    try:
        clear_active_workflows(session["employee_id"])
    except WorkflowStoreError:
        pass

    return render_template("index.html", employee=current_employee_context())


@app.route("/chat", methods=["POST"])
def chat():
    if "employee_id" not in session:
        return jsonify({"reply": "Please login first."}), 401

    receipt_filename = None
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        user_message = str(request.form.get("message", "")).strip()
        receipt_filename, upload_error = save_expense_receipt(request.files.get("receipt"))
        if upload_error:
            return jsonify({"reply": upload_error}), 400
    else:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get("message", "")).strip()

    if not user_message and not receipt_filename:
        return jsonify({"reply": "Please type a message first."}), 400

    employee_id = session["employee_id"]
    employee_name = session["employee_name"]

    active_workflow = None
    workflow_setup_error = None
    try:
        active_workflow = get_active_workflow(employee_id)
    except WorkflowStoreError as exc:
        workflow_setup_error = str(exc)

    if not receipt_filename and is_payroll_message(user_message):
        result = handle_payroll_query(employee_id, employee_name, user_message)
        return finalize_chat_response(employee_id, user_message, result)
    if active_workflow and active_workflow.get("workflow_type") in {LEAVE_WORKFLOW, EXPENSE_WORKFLOW}:
        ai_result = local_workflow_result(user_message, active_workflow.get("workflow_type"))
    elif receipt_filename and not user_message:
        ai_result = local_workflow_result(user_message, EXPENSE_WORKFLOW)
    else:
        ai_result = process_hr_request(
            user_message,
            employee_context=current_employee_context(),
            active_workflow=active_workflow,
        )
    intent = ai_result["intent"]
    if (
        active_workflow
        and active_workflow.get("workflow_type") == LEAVE_WORKFLOW
        and intent in {"GENERAL_HR_QUERY", "OUT_OF_SCOPE"}
    ):
        intent = "APPLY_LEAVE"
    if (
        active_workflow
        and active_workflow.get("workflow_type") == EXPENSE_WORKFLOW
        and intent in {"GENERAL_HR_QUERY", "OUT_OF_SCOPE"}
    ):
        intent = "APPLY_EXPENSE"
    if receipt_filename and intent in {"GENERAL_HR_QUERY", "OUT_OF_SCOPE"}:
        intent = "APPLY_EXPENSE"

    try:
        if intent == "PUNCH_IN":
            result = handle_punch_in(employee_id, employee_name, ai_result)
        elif intent == "PUNCH_OUT":
            result = handle_punch_out(employee_id, employee_name, ai_result)
        elif intent == "CHECK_LEAVE_BALANCE":
            result = handle_leave_balance(employee_id, employee_name)
        elif intent == "APPLY_LEAVE":
            if workflow_setup_error:
                result = json_reply(workflow_setup_error, 500)
            else:
                result = handle_apply_leave(employee_id, employee_name, ai_result, user_message)
        elif intent == "APPLY_EXPENSE":
            if workflow_setup_error:
                result = json_reply(workflow_setup_error, 500)
            else:
                result = handle_apply_expense(
                    employee_id,
                    employee_name,
                    ai_result,
                    user_message,
                    receipt_filename,
                )
        elif intent == "CONFIRM_LEAVE":
            if workflow_setup_error:
                result = json_reply(workflow_setup_error, 500)
            else:
                result = handle_confirm_leave(employee_id, employee_name)
        elif intent == "CONFIRM_EXPENSE":
            if workflow_setup_error:
                result = json_reply(workflow_setup_error, 500)
            else:
                result = handle_confirm_expense(employee_id, employee_name)
        elif intent == "CANCEL_WORKFLOW":
            if workflow_setup_error:
                result = json_reply(workflow_setup_error, 500)
            else:
                result = handle_cancel_workflow(employee_id)
        elif intent == "GENERAL_HR_QUERY":
            result = handle_general_hr_query(ai_result)
        else:
            result = json_reply(
                "I can only help with company HR topics such as attendance, leave, approvals, payroll, and policies."
            )
    except WorkflowStoreError as exc:
        result = json_reply(str(exc), 500)
    except Exception:
        result = json_reply("Something went wrong while processing your HR request. Please try again.", 500)

    return finalize_chat_response(employee_id, user_message, result)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template("login.html", error="Email and password are required.")

        response = (
            supabase.table("employees")
            .select("*")
            .eq("email", email)
            .eq("password", password)
            .limit(1)
            .execute()
        )

        if response.data:
            employee = response.data[0]
            session["employee_id"] = employee["employee_id"]
            session["employee_name"] = employee["name"]
            session["role"] = employee["role"]
            try:
                clear_active_workflows(employee["employee_id"])
            except WorkflowStoreError:
                pass
            return redirect("/")

        return render_template("login.html", error="Invalid credentials.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/leaves")
def employee_leaves():
    return render_employee_leave_history()


@app.route("/payslip")
def employee_payslip():
    return render_payslip()


@app.route("/payslip/download")
def employee_payslip_download():
    return download_payslip_pdf()


@app.route("/manager/leaves")
def manager_leaves():
    return render_leave_approvals()


@app.route("/manager/leaves/<leave_id>/approve", methods=["POST"])
def manager_leave_approve(leave_id):
    return approve_leave_request(leave_id)


@app.route("/manager/leaves/<leave_id>/reject", methods=["POST"])
def manager_leave_reject(leave_id):
    return reject_leave_request(leave_id)


@app.route("/manager/expenses")
def manager_expenses():
    return render_expense_approvals()


@app.route("/manager/expenses/<expense_id>/approve", methods=["POST"])
def manager_expense_approve(expense_id):
    return approve_expense_request(expense_id)


@app.route("/manager/expenses/<expense_id>/reject", methods=["POST"])
def manager_expense_reject(expense_id):
    return reject_expense_request(expense_id)


@app.route("/expense-receipts/<filename>")
def expense_receipt(filename):
    if "employee_id" not in session:
        return redirect("/login")
    if not receipt_path(filename).exists():
        return redirect("/")
    return send_from_directory(Config.EXPENSE_UPLOAD_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True)

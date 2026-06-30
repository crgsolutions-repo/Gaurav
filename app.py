import logging

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session

from config import Config, require_config
from assistant_service import (
    copilot_employee_context,
    attendance_action,
    attendance_response,
    attendance_correction_status_response,
    is_attendance_correction_message,
    is_attendance_correction_status_message,
    handle_advisory_message,
    format_expense_requests,
    format_leave_requests,
    hr_summary,
    is_conversation_close_message,
    leave_history_period_response,
    is_expense_start_message,
    is_explicit_resume_message,
    is_global_cancel_message,
    is_leave_start_message,
    is_retrieval_like_message,
    is_switch_confirmation,
    workflow_switch_prompt,
    workflow_switch_target,
)
from attendance_service import (
    ATTENDANCE_CORRECTION_WORKFLOW,
    approve_attendance_correction,
    attendance_correction_step_prompt,
    get_unseen_pending_attendance_correction_count,
    handle_apply_attendance_correction,
    handle_confirm_attendance_correction,
    render_attendance_corrections,
    reject_attendance_correction,
    team_attendance_response,
)
from expense_service import (
    EXPENSE_WORKFLOW,
    expense_step_prompt,
    handle_apply_expense,
    handle_confirm_expense,
    receipt_path,
    save_expense_receipt,
)
from gemini_service import generate_copilot_response, process_hr_request
from conversation_planner import plan_conversation
from ocr_service import log_ocr_diagnostics
from policy_service import retrieve_policy_context, should_use_copilot
from payroll_service import (
    download_payslip_pdf,
    handle_payroll_query,
    is_payroll_followup,
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
    leave_step_prompt,
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
        try:
            context["unseen_attendance_correction_count"] = get_unseen_pending_attendance_correction_count()
        except Exception:
            context["unseen_attendance_correction_count"] = 0
    return context


def finalize_chat_response(employee_id, user_message, result):
    response, status = result if isinstance(result, tuple) else (result, 200)
    data = response.get_json(silent=True) or {}
    intent_debug = data.get("intent_debug")
    if Config.INTENT_DEBUG_ENABLED and intent_debug and data.get("reply"):
        marker = f"[Intent: {intent_debug}]"
        if marker not in data["reply"]:
            data["reply"] = f"{data['reply']}\n\n{marker}"
        result = jsonify(data), status
    bot_reply = data.get("reply", "")
    if bot_reply:
        log_conversation(employee_id, user_message, bot_reply)
    return result


def with_intent(result, intent):
    if not intent:
        return result
    response, status = result if isinstance(result, tuple) else (result, 200)
    data = response.get_json(silent=True) or {}
    data["intent_debug"] = str(intent)
    return jsonify(data), status


def combine_chat_replies(*results):
    replies = []
    intents = []
    status = 200
    for result in results:
        response, result_status = result if isinstance(result, tuple) else (result, 200)
        data = response.get_json(silent=True) or {}
        reply = str(data.get("reply") or "").strip()
        if reply:
            replies.append(reply)
        intent_debug = str(data.get("intent_debug") or "").strip()
        if intent_debug and intent_debug not in intents:
            intents.append(intent_debug)
        status = max(status, result_status)
    payload = {"reply": "\n\n".join(replies)}
    if intents:
        payload["intent_debug"] = " + ".join(intents)
    return jsonify(payload), status


def planner_ai_result(action, entities):
    result = {
        "intent": action,
        "leave_type": "UNKNOWN",
        "from_date": "UNKNOWN",
        "to_date": "UNKNOWN",
        "duration": "UNKNOWN",
        "reason": "UNKNOWN",
        "amount": "UNKNOWN",
        "expense_type": "UNKNOWN",
        "description": "UNKNOWN",
        "attendance_date": "UNKNOWN",
        "punch_in": "UNKNOWN",
        "punch_out": "UNKNOWN",
        "correction_type": "UNKNOWN",
        "reply": "",
    }
    for field in (
        "leave_type",
        "from_date",
        "to_date",
        "duration",
        "reason",
        "amount",
        "expense_type",
        "expense_category",
        "description",
        "attendance_date",
        "punch_in",
        "punch_out",
        "correction_type",
        "start_date",
        "end_date",
        "date_phrase",
    ):
        if entities.get(field):
            result[field] = str(entities[field])
    if result.get("expense_type") == "UNKNOWN" and entities.get("expense_category"):
        result["expense_type"] = str(entities["expense_category"])
    if result.get("from_date") == "UNKNOWN" and entities.get("start_date"):
        result["from_date"] = str(entities["start_date"])
    if result.get("to_date") == "UNKNOWN" and entities.get("end_date"):
        result["to_date"] = str(entities["end_date"])
    return result


def planner_message_with_date(user_message, entities):
    start_date = str(entities.get("start_date") or "").strip()
    end_date = str(entities.get("end_date") or "").strip()
    if start_date and end_date and start_date not in user_message and end_date not in user_message:
        return f"{user_message} from {start_date} to {end_date}"
    if start_date and start_date not in user_message:
        return f"{user_message} {start_date}"
    date_reference = str(entities.get("date_reference") or "").strip()
    if date_reference and date_reference.lower() not in user_message.lower():
        return f"{user_message} {date_reference}"
    date_phrase = str(entities.get("date_phrase") or "").strip()
    if date_phrase and date_phrase.lower() not in user_message.lower():
        return f"{user_message} {date_phrase}"
    return user_message


def execute_planner_actions(employee_id, employee_name, user_message, plan, active_workflow, workflow_setup_error):
    actions = sanitize_planner_actions(user_message, plan.get("actions") or [])
    entities = plan.get("entities") or {}
    results = []
    retrieval_actions = {
        "GET_ATTENDANCE",
        "GET_LEAVE_BALANCE",
        "GET_LEAVE_HISTORY",
        "GET_EXPENSE_HISTORY",
        "GET_PAYROLL",
        "GET_HR_SUMMARY",
        "GET_POLICY_ADVICE",
        "GET_ATTENDANCE_CORRECTIONS",
        "GET_TEAM_ATTENDANCE",
    }

    for action in actions:
        message_with_date = planner_message_with_date(user_message, entities)
        if action == "PUNCH_IN":
            results.append(with_intent(handle_punch_in(employee_id, employee_name, planner_ai_result(action, entities)), action))
        elif action == "PUNCH_OUT":
            results.append(with_intent(handle_punch_out(employee_id, employee_name, planner_ai_result(action, entities)), action))
        elif action == "GET_ATTENDANCE":
            session["last_hr_topic"] = "attendance"
            session.modified = True
            results.append(with_intent(json_reply(attendance_response(employee_id, employee_name, message_with_date)), action))
        elif action == "GET_TEAM_ATTENDANCE":
            session["last_hr_topic"] = "attendance"
            session.modified = True
            results.append(with_intent(json_reply(team_attendance_response(employee_id, employee_name, message_with_date)), action))
        elif action == "GET_ATTENDANCE_CORRECTIONS":
            session["last_hr_topic"] = "attendance"
            session.modified = True
            results.append(with_intent(json_reply(attendance_correction_status_response(employee_id, employee_name, message_with_date)), action))
        elif action == "GET_LEAVE_BALANCE":
            results.append(with_intent(handle_leave_balance(employee_id, employee_name), action))
        elif action == "GET_LEAVE_HISTORY":
            results.append(with_intent(json_reply(leave_history_period_response(employee_id, employee_name, message_with_date)), action))
        elif action == "GET_EXPENSE_HISTORY":
            results.append(with_intent(json_reply(format_expense_requests(employee_id, message_with_date)), action))
        elif action == "GET_PAYROLL":
            session["last_hr_topic"] = "payroll"
            session.modified = True
            results.append(with_intent(handle_payroll_query(employee_id, employee_name, message_with_date), action))
        elif action == "GET_HR_SUMMARY":
            results.append(with_intent(json_reply(hr_summary(employee_id, employee_name)), action))
        elif action == "GET_POLICY_ADVICE":
            employee_context = copilot_employee_context(employee_id, employee_name)
            employee_context.update(current_employee_context())
            policy_context = retrieve_policy_context(user_message)
            reply = generate_copilot_response(user_message, employee_context, policy_context)
            if reply:
                results.append(with_intent(json_reply(reply), action))
        elif action == "APPLY_LEAVE":
            if workflow_setup_error:
                results.append(with_intent(json_reply(workflow_setup_error, 500), action))
            else:
                results.append(
                    with_intent(
                        handle_apply_leave(
                            employee_id,
                            employee_name,
                            planner_ai_result(action, entities),
                            user_message,
                        ),
                        action,
                    )
                )
        elif action == "APPLY_EXPENSE":
            if workflow_setup_error:
                results.append(with_intent(json_reply(workflow_setup_error, 500), action))
            else:
                results.append(
                    with_intent(
                        handle_apply_expense(
                            employee_id,
                            employee_name,
                            planner_ai_result(action, entities),
                            user_message,
                            None,
                        ),
                        action,
                    )
                )
        elif action == "APPLY_ATTENDANCE_CORRECTION":
            if workflow_setup_error:
                results.append(with_intent(json_reply(workflow_setup_error, 500), action))
            else:
                results.append(with_intent(handle_apply_attendance_correction(employee_id, employee_name, entities, user_message), action))
        elif action == "CANCEL_WORKFLOW":
            results.append(with_intent(cancel_active_workflow(employee_id, active_workflow), action))
        elif action == "CONTINUE_WORKFLOW" and active_workflow:
            results.append(with_intent(resume_active_workflow(active_workflow), action))
        elif action == "CLOSE_CONVERSATION":
            results.append(with_intent(json_reply("You're welcome. Take care, and message me whenever you need HR help."), action))

    if not results:
        return None
    combined = combine_chat_replies(*results)
    if active_workflow and any(action in retrieval_actions for action in actions):
        combined = append_workflow_resume_prompt(combined, active_workflow)
    return combined


def sanitize_planner_actions(user_message, actions):
    """Keep Gemini planning useful while refusing unsafe attendance subtype collisions."""
    cleaned = [action for action in actions if action]
    lowered = user_message.strip().lower()
    guidance_question = any(word in lowered for word in ("how do i", "how to", "explain", "process", "steps", "guide"))
    hr_guidance_topic = any(
        word in lowered
        for word in (
            "attendance",
            "present",
            "punch",
            "check in",
            "bill",
            "expense",
            "reimbursement",
            "leave",
            "payslip",
            "salary",
        )
    )
    if guidance_question and hr_guidance_topic:
        return ["GET_POLICY_ADVICE"]

    direct_attendance_action = attendance_action(user_message)
    if direct_attendance_action:
        sanitized = [
            action
            for action in cleaned
            if action
            not in {
                "APPLY_ATTENDANCE_CORRECTION",
                "PUNCH_IN",
                "PUNCH_OUT",
                "GET_ATTENDANCE",
                "GET_ATTENDANCE_CORRECTIONS",
                "GET_TEAM_ATTENDANCE",
            }
        ]
        return [direct_attendance_action] + sanitized

    looks_like_attendance_query = any(
        phrase in lowered
        for phrase in (
            "was i present",
            "was i absent",
            "did i attend",
            "show attendance",
            "attendance for",
            "attendance last",
            "attendance this",
            "day before yesterday",
            "how many days was i late",
            "how many times was i late",
            "overtime hours",
            "half days",
        )
    )
    if looks_like_attendance_query and "APPLY_ATTENDANCE_CORRECTION" in cleaned and not is_attendance_correction_message(user_message):
        cleaned = ["GET_ATTENDANCE" if action == "APPLY_ATTENDANCE_CORRECTION" else action for action in cleaned]
    return cleaned


def infer_advisory_intent(user_message):
    text = str(user_message or "").strip().lower()
    if is_conversation_close_message(user_message):
        return "CLOSE_CONVERSATION"
    if is_attendance_correction_status_message(user_message):
        return "GET_ATTENDANCE_CORRECTIONS"
    if is_attendance_correction_message(user_message):
        return "APPLY_ATTENDANCE_CORRECTION"
    if attendance_action(user_message):
        return attendance_action(user_message)
    if any(
        phrase in text
        for phrase in (
            "attendance",
            "was i present",
            "was i absent",
            "did i attend",
            "late",
            "overtime",
            "half day",
        )
    ):
        return "GET_ATTENDANCE"
    if "leave balance" in text or "leaves do i have" in text:
        return "GET_LEAVE_BALANCE"
    if "leave" in text and any(word in text for word in ("history", "taken", "approved", "pending", "last", "latest", "request")):
        return "GET_LEAVE_HISTORY"
    if any(word in text for word in ("expense", "reimbursement", "claim")) and any(
        word in text for word in ("history", "status", "pending", "approved", "latest", "last", "show")
    ):
        return "GET_EXPENSE_HISTORY"
    if "summary" in text or "dashboard" in text:
        return "GET_HR_SUMMARY"
    if is_payroll_message(user_message) or is_payroll_followup(user_message):
        return "GET_PAYROLL"
    if is_expense_start_message(user_message):
        return "APPLY_EXPENSE"
    if is_leave_start_message(user_message):
        return "APPLY_LEAVE"
    if is_retrieval_like_message(user_message):
        return "RETRIEVAL"
    if should_use_copilot(user_message):
        return "GET_POLICY_ADVICE"
    return "GENERAL_HR_QUERY"


def workflow_resume_prompt(active_workflow):
    if not active_workflow:
        return ""
    workflow_type = active_workflow.get("workflow_type")
    if workflow_type == LEAVE_WORKFLOW:
        return "\n\nWould you like to continue your leave request? Reply continue to resume it, or cancel to discard it."
    if workflow_type == EXPENSE_WORKFLOW:
        return "\n\nWould you like to continue your expense request? Reply continue to resume it, or cancel to discard it."
    if workflow_type == ATTENDANCE_CORRECTION_WORKFLOW:
        return "\n\nWould you like to continue your attendance correction request? Reply continue to resume it, or cancel to discard it."
    return ""


def append_workflow_resume_prompt(result, active_workflow):
    prompt = workflow_resume_prompt(active_workflow)
    if not prompt:
        return result
    response, status = result if isinstance(result, tuple) else (result, 200)
    data = response.get_json(silent=True) or {}
    reply = data.get("reply", "")
    if reply and prompt.strip() not in reply:
        data["reply"] = reply + prompt
        return jsonify(data), status
    return result


def resume_active_workflow(active_workflow):
    workflow_type = active_workflow.get("workflow_type")
    step = active_workflow.get("step")
    if workflow_type == LEAVE_WORKFLOW:
        return json_reply(leave_step_prompt(step))
    if workflow_type == EXPENSE_WORKFLOW:
        return json_reply(expense_step_prompt(step))
    if workflow_type == ATTENDANCE_CORRECTION_WORKFLOW:
        return json_reply(attendance_correction_step_prompt(step))
    return json_reply("There is no active workflow to continue.")


def cancel_active_workflow(employee_id, active_workflow):
    result = handle_cancel_workflow(employee_id)
    workflow_type = active_workflow.get("workflow_type") if active_workflow else None
    if workflow_type == LEAVE_WORKFLOW:
        return json_reply("No problem. The leave request has been cancelled.\n\nLet me know if you need anything else.")
    if workflow_type == EXPENSE_WORKFLOW:
        return json_reply("No problem. The reimbursement request has been cancelled.\n\nLet me know if you need anything else.")
    if workflow_type == ATTENDANCE_CORRECTION_WORKFLOW:
        return json_reply("No problem. The attendance correction request has been cancelled.\n\nLet me know if you need anything else.")
    return result


def local_workflow_result(user_message, workflow_type=LEAVE_WORKFLOW):
    message = user_message.strip().lower()
    confirm_phrases = {"confirm", "yes", "yes confirm", "yes submit", "submit", "submit it", "proceed"}

    if is_global_cancel_message(message):
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

    immediate_attendance_action = attendance_action(user_message)
    # Fast-path attendance handling remains only for an AI outage or an explicitly disabled planner.
    if immediate_attendance_action and not Config.GEMINI_PLANNER_ENABLED:
        if active_workflow and active_workflow.get("workflow_type") == ATTENDANCE_CORRECTION_WORKFLOW:
            cancel_active_workflow(employee_id, active_workflow)
            active_workflow = None
        if immediate_attendance_action == "PUNCH_OUT":
            attendance_result = handle_punch_out(employee_id, employee_name, {})
        else:
            attendance_result = handle_punch_in(employee_id, employee_name, {})
        attendance_result = with_intent(attendance_result, immediate_attendance_action)

        if not active_workflow and is_leave_start_message(user_message):
            leave_result = handle_apply_leave(
                employee_id,
                employee_name,
                local_workflow_result(user_message, LEAVE_WORKFLOW),
                user_message,
            )
            result = combine_chat_replies(attendance_result, leave_result)
        else:
            result = append_workflow_resume_prompt(attendance_result, active_workflow)
        return finalize_chat_response(employee_id, user_message, result)

    pending_switch = session.get("pending_workflow_switch") or {}
    if active_workflow and pending_switch and is_switch_confirmation(user_message):
        target_workflow = pending_switch.get("target")
        original_message = pending_switch.get("message") or user_message
        cancel_active_workflow(employee_id, active_workflow)
        session.pop("pending_workflow_switch", None)
        session.modified = True
        if target_workflow == EXPENSE_WORKFLOW:
            ai_result = local_workflow_result(original_message, EXPENSE_WORKFLOW)
            result = handle_apply_expense(employee_id, employee_name, ai_result, original_message, receipt_filename)
            result = with_intent(result, "APPLY_EXPENSE")
        elif target_workflow == LEAVE_WORKFLOW:
            ai_result = local_workflow_result(original_message, LEAVE_WORKFLOW)
            result = handle_apply_leave(employee_id, employee_name, ai_result, original_message)
            result = with_intent(result, "APPLY_LEAVE")
        else:
            result = json_reply("I could not switch workflows. Please tell me what HR task you want to start.")
            result = with_intent(result, "GENERAL_HR_QUERY")
        return finalize_chat_response(employee_id, user_message, result)

    if active_workflow and is_global_cancel_message(user_message):
        session.pop("pending_workflow_switch", None)
        session.modified = True
        result = with_intent(cancel_active_workflow(employee_id, active_workflow), "CANCEL_WORKFLOW")
        return finalize_chat_response(employee_id, user_message, result)

    if active_workflow and is_explicit_resume_message(user_message):
        session.pop("pending_workflow_switch", None)
        session.modified = True
        result = with_intent(resume_active_workflow(active_workflow), "CONTINUE_WORKFLOW")
        return finalize_chat_response(employee_id, user_message, result)

    if active_workflow:
        switch_target = workflow_switch_target(active_workflow.get("workflow_type"), user_message)
        if switch_target:
            session["pending_workflow_switch"] = {"target": switch_target, "message": user_message}
            session.modified = True
            result = with_intent(
                json_reply(workflow_switch_prompt(active_workflow.get("workflow_type"), switch_target, user_message)),
                "WORKFLOW_SWITCH_PROMPT",
            )
            return finalize_chat_response(employee_id, user_message, result)

    if (
        not receipt_filename
        and immediate_attendance_action
        and active_workflow
        and active_workflow.get("workflow_type") == ATTENDANCE_CORRECTION_WORKFLOW
    ):
        cancel_active_workflow(employee_id, active_workflow)
        active_workflow = None
        if immediate_attendance_action == "PUNCH_OUT":
            attendance_result = handle_punch_out(employee_id, employee_name, {})
        else:
            attendance_result = handle_punch_in(employee_id, employee_name, {})
        attendance_result = with_intent(attendance_result, immediate_attendance_action)
        result = append_workflow_resume_prompt(attendance_result, active_workflow)
        return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename and active_workflow and active_workflow.get("workflow_type") == ATTENDANCE_CORRECTION_WORKFLOW:
        confirm_phrases = {"confirm", "yes", "yes confirm", "submit", "submit it", "proceed"}
        if user_message.strip().lower() in confirm_phrases:
            result = with_intent(handle_confirm_attendance_correction(employee_id, employee_name), "CONFIRM_ATTENDANCE_CORRECTION")
            session.pop("pending_workflow_switch", None)
            session.modified = True
            return finalize_chat_response(employee_id, user_message, result)
        if not is_retrieval_like_message(user_message):
            result = with_intent(
                handle_apply_attendance_correction(employee_id, employee_name, {}, user_message),
                "APPLY_ATTENDANCE_CORRECTION",
            )
            return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename and Config.GEMINI_PLANNER_ENABLED:
        planner_context = copilot_employee_context(employee_id, employee_name)
        planner_context.update(current_employee_context())
        plan = plan_conversation(
            user_message,
            employee_context=planner_context,
            active_workflow=active_workflow,
            last_topic=session.get("last_hr_topic"),
        )
        if plan:
            result = execute_planner_actions(
                employee_id,
                employee_name,
                user_message,
                plan,
                active_workflow,
                workflow_setup_error,
            )
            if result:
                return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename and immediate_attendance_action:
        if immediate_attendance_action == "PUNCH_OUT":
            attendance_result = handle_punch_out(employee_id, employee_name, {})
        else:
            attendance_result = handle_punch_in(employee_id, employee_name, {})
        attendance_result = with_intent(attendance_result, immediate_attendance_action)
        result = append_workflow_resume_prompt(attendance_result, active_workflow)
        return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename and is_attendance_correction_status_message(user_message):
        result = with_intent(
            json_reply(attendance_correction_status_response(employee_id, employee_name, user_message)),
            "GET_ATTENDANCE_CORRECTIONS",
        )
        result = append_workflow_resume_prompt(result, active_workflow)
        return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename and is_attendance_correction_message(user_message):
        result = with_intent(
            handle_apply_attendance_correction(employee_id, employee_name, {}, user_message),
            "APPLY_ATTENDANCE_CORRECTION",
        )
        return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename and should_use_copilot(user_message):
        employee_context = copilot_employee_context(employee_id, employee_name)
        employee_context.update(current_employee_context())
        policy_context = retrieve_policy_context(user_message)
        copilot_reply = generate_copilot_response(user_message, employee_context, policy_context)
        if copilot_reply:
            result = with_intent(json_reply(copilot_reply), "GET_POLICY_ADVICE")
            result = append_workflow_resume_prompt(result, active_workflow)
            return finalize_chat_response(employee_id, user_message, result)

    if not receipt_filename:
        advisory_result = handle_advisory_message(employee_id, employee_name, user_message)
        if advisory_result:
            advisory_result = with_intent(advisory_result, infer_advisory_intent(user_message))
            advisory_result = append_workflow_resume_prompt(advisory_result, active_workflow)
            return finalize_chat_response(employee_id, user_message, advisory_result)

    payroll_followup = session.get("last_hr_topic") == "payroll" and is_payroll_followup(user_message)
    if not receipt_filename and (is_payroll_message(user_message) or payroll_followup):
        session["last_hr_topic"] = "payroll"
        result = with_intent(handle_payroll_query(employee_id, employee_name, user_message), "GET_PAYROLL")
        result = append_workflow_resume_prompt(result, active_workflow)
        return finalize_chat_response(employee_id, user_message, result)
    if active_workflow and active_workflow.get("workflow_type") in {LEAVE_WORKFLOW, EXPENSE_WORKFLOW}:
        ai_result = local_workflow_result(user_message, active_workflow.get("workflow_type"))
    elif receipt_filename:
        ai_result = local_workflow_result(user_message, EXPENSE_WORKFLOW)
    elif is_expense_start_message(user_message):
        ai_result = local_workflow_result(user_message, EXPENSE_WORKFLOW)
    elif is_leave_start_message(user_message):
        ai_result = local_workflow_result(user_message, LEAVE_WORKFLOW)
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

    if intent in {"CONFIRM_LEAVE", "CONFIRM_EXPENSE", "CANCEL_WORKFLOW"}:
        session.pop("pending_workflow_switch", None)
        session.modified = True

    result = with_intent(result, intent)
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


@app.route("/manager/attendance")
def manager_attendance():
    return render_attendance_corrections()


@app.route("/manager/attendance/<correction_id>/approve", methods=["POST"])
def manager_attendance_approve(correction_id):
    return approve_attendance_correction(correction_id)


@app.route("/manager/attendance/<correction_id>/reject", methods=["POST"])
def manager_attendance_reject(correction_id):
    return reject_attendance_correction(correction_id)


@app.route("/expense-receipts/<filename>")
def expense_receipt(filename):
    if "employee_id" not in session:
        return redirect("/login")
    if not receipt_path(filename).exists():
        return redirect("/")
    return send_from_directory(Config.EXPENSE_UPLOAD_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True)

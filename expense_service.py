import re
from datetime import date
from pathlib import Path
from uuid import uuid4

from flask import jsonify
from werkzeug.utils import secure_filename

from config import Config
from ocr_service import OcrServiceError, run_tesseract
from supabase_client import supabase
from workflow_store import finish_workflow, get_active_workflow, upsert_workflow


EXPENSE_WORKFLOW = "expense_request"
EXPENSE_TYPES = ("Travel", "Food", "Accommodation", "Software / Tools")
SMALL_EXPENSE_LIMIT = 200
ALLOWED_RECEIPT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
UNKNOWN_VALUES = {"", "unknown", "none", "null", "not specified", "n/a"}


def json_reply(reply, status=200, **extra):
    payload = {"reply": reply}
    payload.update(extra)
    return jsonify(payload), status


def is_unknown(value):
    return value is None or str(value).strip().lower() in UNKNOWN_VALUES


def money(value):
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def normalize_amount(value):
    if is_unknown(value):
        return None
    cleaned = str(value).replace(",", "")
    match = re.search(r"(\d+(?:\.\d{1,2})?)", cleaned)
    if not match:
        return None
    amount = float(match.group(1))
    return amount if amount > 0 else None


def infer_amount_from_message(message):
    text = str(message or "").lower().replace(",", "")
    patterns = [
        r"(?:₹|rs\.?|inr)\s*(\d+(?:\.\d{1,2})?)",
        r"\b(\d+(?:\.\d{1,2})?)\s*(?:rupees?|rs\.?|inr)\b",
        r"\bamount\s+(?:is|should\s+be|was)\s+(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d{1,2})?)\b",
        r"\b(\d+(?:\.\d{1,2})?)\s*(?:grand\s+total|total|net|payable)\b",
        r"\b(?:amount|claim|expense|reimbursement|for|of)\s+(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d{1,2})?)\b",
        r"^\s*(\d+(?:\.\d{1,2})?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_amount(match.group(1))
    return None


def normalize_expense_type(value):
    if is_unknown(value):
        return None

    cleaned = str(value).strip().lower()
    number_map = {
        "1": "Travel",
        "2": "Food",
        "3": "Accommodation",
        "4": "Software / Tools",
    }
    aliases = {
        "travel": "Travel",
        "cab": "Travel",
        "taxi": "Travel",
        "flight": "Travel",
        "train": "Travel",
        "food": "Food",
        "meal": "Food",
        "meals": "Food",
        "lunch": "Food",
        "dinner": "Food",
        "accommodation": "Accommodation",
        "hotel": "Accommodation",
        "stay": "Accommodation",
        "software": "Software / Tools",
        "tool": "Software / Tools",
        "tools": "Software / Tools",
        "software tools": "Software / Tools",
        "software / tools": "Software / Tools",
    }

    if cleaned in number_map:
        return number_map[cleaned]
    if cleaned in aliases:
        return aliases[cleaned]

    for alias, expense_type in aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", cleaned):
            return expense_type
    return None


def infer_expense_type_from_message(message):
    text = str(message or "")
    for value in ("1", "2", "3", "4"):
        if re.search(rf"\b{value}\b", text):
            return normalize_expense_type(value)
    return normalize_expense_type(text)


def expense_type_prompt():
    return "Please choose an expense type: 1. Travel, 2. Food, 3. Accommodation, 4. Software / Tools."


def save_expense_receipt(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None

    original_name = secure_filename(file_storage.filename)
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_RECEIPT_EXTENSIONS:
        return None, "Please upload a receipt image file such as PNG, JPG, WEBP, BMP, or TIFF."

    upload_dir = Path(Config.EXPENSE_UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid4().hex}{extension}"
    file_storage.save(upload_dir / stored_name)
    return stored_name, None


def receipt_path(filename):
    return Path(Config.EXPENSE_UPLOAD_DIR) / str(filename)


def merge_expense_payload(existing_payload, ai_result, user_message, current_step=None):
    payload = dict(existing_payload or {})
    current_step = current_step or ""

    if current_step not in {"expense_type", "description"} and not is_unknown(ai_result.get("amount")):
        payload["amount"] = ai_result["amount"]
    if not is_unknown(ai_result.get("expense_type")):
        payload["expense_type"] = ai_result["expense_type"]
    if not is_unknown(ai_result.get("description")):
        payload["description"] = ai_result["description"]

    amount = infer_amount_from_message(user_message) if current_step in {"", "amount"} else None
    if amount and is_unknown(payload.get("amount")):
        payload["amount"] = str(amount)

    expense_type = infer_expense_type_from_message(user_message)
    if expense_type:
        payload["expense_type"] = expense_type

    return payload


def merge_expense_step_reply(payload, current_step, user_message):
    message = str(user_message or "").strip()
    if is_unknown(message):
        return payload

    if current_step == "amount" and is_unknown(payload.get("amount")):
        amount = normalize_amount(message)
        payload["amount"] = str(amount) if amount else message
    elif current_step == "expense_type" and is_unknown(payload.get("expense_type")):
        payload["expense_type"] = message
    elif current_step == "description" and is_unknown(payload.get("description")):
        payload["description"] = message
    return payload


def ocr_fields_from_receipt(filename):
    try:
        ocr_result = run_tesseract(receipt_path(filename))
    except OcrServiceError as exc:
        return None, f"Receipt OCR failed: {exc}"

    if not ocr_result.text and ocr_result.amount is None and not ocr_result.bill_date and not ocr_result.invoice_number:
        return None, (
            "I could not recognize this as an expense receipt. "
            "Please upload a clear bill or tell me what HR action you want to take with the image."
        )

    if ocr_result.amount is None:
        return None, "I could not detect a receipt amount from this image. Please upload a clearer bill or type the amount you want to claim."

    invoice_number = ocr_result.invoice_number.strip().upper() if ocr_result.invoice_number else None
    return {
        "ocr_text": ocr_result.text,
        "ocr_amount": ocr_result.amount,
        "bill_date": ocr_result.bill_date.isoformat() if ocr_result.bill_date else None,
        "invoice_number": invoice_number,
        "vendor_name": ocr_result.vendor_name,
        "amount": str(ocr_result.amount),
        "receipt_ocr_done": "true",
    }, None


def next_expense_step(payload):
    amount = normalize_amount(payload.get("amount"))
    if not amount:
        return "amount"
    if not normalize_expense_type(payload.get("expense_type")):
        return "expense_type"
    if amount > SMALL_EXPENSE_LIMIT and is_unknown(payload.get("description")):
        return "description"
    if amount > SMALL_EXPENSE_LIMIT and is_unknown(payload.get("receipt_filename")):
        return "receipt"
    return "confirm"


def expense_step_prompt(step):
    prompts = {
        "amount": "It sounds like you would like to submit a reimbursement claim. Expenses above INR 200 need a receipt for OCR validation before manager approval. What amount would you like to claim?",
        "expense_type": expense_type_prompt(),
        "description": "Please provide a short description for this expense so your manager can understand the business purpose.",
        "receipt": "Please attach the receipt image with your message so I can validate it.",
    }
    return prompts.get(step, "Please confirm if you want me to submit this expense request.")


def format_ocr_context(payload):
    amount = normalize_amount(payload.get("ocr_amount") or payload.get("amount"))
    claimed_amount = normalize_amount(payload.get("amount"))
    lines = []
    if amount and claimed_amount and abs(float(claimed_amount) - float(amount)) > 0.01:
        lines.append(f"Claimed Amount: ₹{money(claimed_amount)}")
        lines.append(f"OCR Amount: ₹{money(amount)}")
        lines.append("Amount Validation: Manager review required")
        if not is_unknown(payload.get("vendor_name")):
            lines.append(f"Vendor: {payload['vendor_name']}")
        if not is_unknown(payload.get("invoice_number")):
            lines.append(f"Invoice: {payload['invoice_number']}")
        if not is_unknown(payload.get("bill_date")):
            lines.append(f"Receipt Date: {payload['bill_date']}")
        return "\n".join(lines)
    if amount:
        lines.append(f"Amount: ₹{money(amount)}")
    if not is_unknown(payload.get("vendor_name")):
        lines.append(f"Vendor: {payload['vendor_name']}")
    if not is_unknown(payload.get("invoice_number")):
        lines.append(f"Invoice: {payload['invoice_number']}")
    if not is_unknown(payload.get("bill_date")):
        lines.append(f"Receipt Date: {payload['bill_date']}")
    return "\n".join(lines)


def format_expense_followup(step, payload):
    prompt = expense_step_prompt(step)
    if step == "amount":
        expense_type = normalize_expense_type(payload.get("expense_type"))
        if expense_type:
            return (
                f"It sounds like you would like to submit a {expense_type.lower()} reimbursement.\n\n"
                "Reimbursements are sent to your manager for approval. If the amount is above INR 200, a receipt will be required for OCR validation.\n\n"
                "What amount would you like to claim?"
            )
    if not is_unknown(payload.get("receipt_filename")) and not is_unknown(payload.get("receipt_ocr_done")):
        context = format_ocr_context(payload)
        if context:
            return f"I read the uploaded receipt as:\n{context}\n\n{prompt}"
    return prompt


def format_expense_confirmation(employee_name, payload):
    amount = money(normalize_amount(payload.get("amount")))
    expense_type = normalize_expense_type(payload.get("expense_type"))
    description = payload.get("description") if not is_unknown(payload.get("description")) else "Not provided"
    receipt_status = "Attached" if not is_unknown(payload.get("receipt_filename")) else "Not required"
    warning = ""
    ocr_amount = normalize_amount(payload.get("ocr_amount"))
    claimed_amount = normalize_amount(payload.get("amount"))
    if ocr_amount and claimed_amount and abs(float(claimed_amount) - float(ocr_amount)) > 0.01:
        warning = (
            "\nOCR read a different amount, so the manager will manually validate this bill amount.\n"
            f"OCR Amount: ₹{money(ocr_amount)}\n"
        )

    return (
        f"{employee_name}, please confirm this reimbursement request:\n"
        f"Amount: ₹{amount}\n"
        f"Expense Type: {expense_type}\n"
        f"Description: {description}\n"
        f"Receipt: {receipt_status}\n"
        f"{warning}\n"
        "Reply with confirm to submit, or cancel to discard it."
    )


def duplicate_invoice_exists(invoice_number):
    if is_unknown(invoice_number):
        return False
    response = (
        supabase.table("expenses")
        .select("id")
        .eq("invoice_number", str(invoice_number).strip().upper())
        .limit(1)
        .execute()
    )
    return bool(response.data)


def validate_ocr_payload(payload):
    amount = normalize_amount(payload.get("amount"))
    filename = payload.get("receipt_filename")
    if not filename:
        return None, "Receipt image is required for expenses above ₹200."

    try:
        ocr_result = run_tesseract(receipt_path(filename))
    except OcrServiceError as exc:
        return None, f"Receipt OCR failed: {exc}"

    if ocr_result.amount is None:
        return None, "I could not detect the receipt amount. Please upload a clearer receipt."
    amount_validation_required = abs(float(amount) - float(ocr_result.amount)) > 0.01

    if not ocr_result.bill_date:
        return None, "I could not detect the receipt date. Please upload a clearer receipt."
    today = date.today()
    if ocr_result.bill_date.year != today.year or ocr_result.bill_date.month != today.month:
        return None, "Only bills from the current month are eligible for reimbursement."

    if not ocr_result.invoice_number:
        return None, "I could not detect the invoice or bill number. Please upload a clearer receipt."
    invoice_number = ocr_result.invoice_number.strip().upper()
    if duplicate_invoice_exists(invoice_number):
        return None, "This receipt appears to have already been submitted."

    return {
        "ocr_text": ocr_result.text,
        "ocr_amount": ocr_result.amount,
        "bill_date": ocr_result.bill_date.isoformat(),
        "invoice_number": invoice_number,
        "vendor_name": ocr_result.vendor_name,
        "amount_validation_required": "true" if amount_validation_required else None,
    }, None


def validate_ocr_payload_with_cache(payload):
    amount = normalize_amount(payload.get("amount"))
    filename = payload.get("receipt_filename")
    if not filename:
        return None, "Receipt image is required for expenses above ₹200."

    if not is_unknown(payload.get("receipt_ocr_done")):
        ocr_amount = normalize_amount(payload.get("ocr_amount"))
        bill_date = date.fromisoformat(payload["bill_date"]) if not is_unknown(payload.get("bill_date")) else None
        invoice_number = str(payload.get("invoice_number") or "").strip().upper() or None
        ocr_fields = {
            "ocr_text": payload.get("ocr_text"),
            "ocr_amount": ocr_amount,
            "bill_date": bill_date.isoformat() if bill_date else None,
            "invoice_number": invoice_number,
            "vendor_name": payload.get("vendor_name"),
        }
    else:
        ocr_fields, error = ocr_fields_from_receipt(filename)
        if error:
            return None, error

        ocr_amount = normalize_amount(ocr_fields.get("ocr_amount"))
        bill_date = date.fromisoformat(ocr_fields["bill_date"]) if ocr_fields.get("bill_date") else None
        invoice_number = ocr_fields.get("invoice_number")

    if ocr_amount is None:
        return None, "I could not detect the receipt amount. Please upload a clearer receipt."
    amount_validation_required = abs(float(amount) - float(ocr_amount)) > 0.01

    if not bill_date:
        return None, "I could not detect the receipt date. Please upload a clearer receipt."
    today = date.today()
    if bill_date.year != today.year or bill_date.month != today.month:
        return None, "Only bills from the current month are eligible for reimbursement."

    if not invoice_number:
        return None, "I could not detect the invoice or bill number. Please upload a clearer receipt."
    if duplicate_invoice_exists(invoice_number):
        return None, "This receipt appears to have already been submitted."

    return {
        "ocr_text": ocr_fields.get("ocr_text"),
        "ocr_amount": ocr_amount,
        "bill_date": bill_date.isoformat(),
        "invoice_number": invoice_number,
        "vendor_name": ocr_fields.get("vendor_name"),
        "amount_validation_required": "true" if amount_validation_required else None,
    }, None


def validate_expense_payload(payload, perform_ocr=False):
    amount = normalize_amount(payload.get("amount"))
    expense_type = normalize_expense_type(payload.get("expense_type"))
    description = "" if is_unknown(payload.get("description")) else str(payload.get("description")).strip()

    if not amount:
        return None, "Please provide a valid expense amount."
    if not expense_type:
        return None, expense_type_prompt()
    if amount > SMALL_EXPENSE_LIMIT and not description:
        return None, "Description is required for expenses above ₹200."
    if amount <= SMALL_EXPENSE_LIMIT:
        return {
            "amount": amount,
            "expense_type": expense_type,
            "description": description,
            "bill_image": None,
            "ocr_text": None,
            "ocr_amount": None,
            "bill_date": None,
            "invoice_number": None,
            "vendor_name": None,
        }, None
    if not payload.get("receipt_filename"):
        return None, "Receipt image is required for expenses above ₹200."

    ocr_fields = {}
    if perform_ocr:
        ocr_fields, error = validate_ocr_payload_with_cache(payload)
        if error:
            return None, error

    return {
        "amount": amount,
        "expense_type": expense_type,
        "description": description,
        "bill_image": payload.get("receipt_filename"),
        "ocr_text": ocr_fields.get("ocr_text"),
        "ocr_amount": ocr_fields.get("ocr_amount"),
        "bill_date": ocr_fields.get("bill_date"),
        "invoice_number": ocr_fields.get("invoice_number"),
        "vendor_name": ocr_fields.get("vendor_name"),
        "amount_validation_required": ocr_fields.get("amount_validation_required"),
    }, None


def handle_apply_expense(employee_id, employee_name, ai_result, user_message="", receipt_filename=None):
    workflow = get_active_workflow(employee_id, EXPENSE_WORKFLOW)
    current_step = workflow.get("step") if workflow else None
    payload = merge_expense_payload(workflow.get("payload") if workflow else {}, ai_result, user_message, current_step)
    payload = merge_expense_step_reply(payload, current_step, user_message)
    claimed_amount_from_message = infer_amount_from_message(user_message)

    if receipt_filename:
        payload["receipt_filename"] = receipt_filename
        if current_step != "description":
            payload["description"] = "UNKNOWN"
        if is_unknown(payload.get("receipt_ocr_done")):
            ocr_fields, ocr_error = ocr_fields_from_receipt(receipt_filename)
            if ocr_error and not workflow and not normalize_amount(payload.get("amount")):
                return json_reply(ocr_error)
            if ocr_error and current_step == "receipt":
                upsert_workflow(employee_id, EXPENSE_WORKFLOW, "receipt", payload)
                return json_reply(ocr_error)
            if ocr_fields:
                payload.update({key: value for key, value in ocr_fields.items() if value is not None})
        if claimed_amount_from_message:
            payload["amount"] = str(claimed_amount_from_message)

    normalized_type = normalize_expense_type(payload.get("expense_type"))
    if normalized_type:
        payload["expense_type"] = normalized_type

    step = next_expense_step(payload)
    workflow = upsert_workflow(employee_id, EXPENSE_WORKFLOW, step, payload)

    if step != "confirm":
        return json_reply(format_expense_followup(step, payload))

    valid_payload, error = validate_expense_payload(payload, perform_ocr=False)
    if error:
        return json_reply(error)

    workflow = upsert_workflow(employee_id, EXPENSE_WORKFLOW, "confirm", valid_payload | payload)
    return json_reply(format_expense_confirmation(employee_name, workflow["payload"]))


def handle_confirm_expense(employee_id, employee_name):
    workflow = get_active_workflow(employee_id, EXPENSE_WORKFLOW)
    if not workflow:
        return json_reply("No active expense request is waiting for confirmation.")

    payload = workflow.get("payload") or {}
    step = next_expense_step(payload)
    if step != "confirm":
        upsert_workflow(employee_id, EXPENSE_WORKFLOW, step, payload)
        return json_reply(expense_step_prompt(step))

    valid_payload, error = validate_expense_payload(payload, perform_ocr=True)
    if error:
        finish_workflow(workflow["id"])
        return json_reply(error)

    supabase.table("expenses").insert(
        {
            "employee_id": employee_id,
            "amount": valid_payload["amount"],
            "description": valid_payload["description"],
            "bill_image": valid_payload["bill_image"],
            "ocr_text": valid_payload["ocr_text"],
            "expense_type": valid_payload["expense_type"],
            "status": "Pending",
            "ocr_amount": valid_payload["ocr_amount"],
            "bill_date": valid_payload["bill_date"],
            "invoice_number": valid_payload["invoice_number"],
            "vendor_name": valid_payload["vendor_name"],
        }
    ).execute()

    finish_workflow(workflow["id"])
    if valid_payload.get("amount_validation_required"):
        return json_reply(
            f"{employee_name}, your reimbursement request has been sent to manager review for ₹{money(valid_payload['amount'])}.\n"
            f"OCR read ₹{money(valid_payload['ocr_amount'])}, so the manager will validate the bill amount before approval."
        )
    return json_reply(f"{employee_name}, your reimbursement request has been submitted for manager approval.")

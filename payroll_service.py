import re
from datetime import date, datetime
from io import BytesIO
from urllib.parse import quote

from flask import Response, jsonify, redirect, render_template, request, session

from supabase_client import supabase


def money(value):
    number = float(value or 0)
    return f"{number:,.2f}"


def salary_month_date(value):
    raw = str(value or "").strip()
    for fmt in ("%B %Y", "%b %Y", "%Y-%m", "%Y/%m"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return date(parsed.year, parsed.month, 1)
        except ValueError:
            continue
    return date.min


def month_display(value):
    parsed = salary_month_date(value)
    if parsed == date.min:
        return str(value or "")
    return parsed.strftime("%B %Y")


def extract_salary_month(message):
    text = str(message or "")
    iso_match = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b", text)
    if iso_match:
        return f"{iso_match.group(1)}-{int(iso_match.group(2)):02d}"

    month_names = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    month_pattern = "|".join(month_names)
    match = re.search(rf"\b({month_pattern})\s+(20\d{{2}})\b", text, re.IGNORECASE)
    if match:
        return f"{match.group(1).title()} {match.group(2)}"

    month_only = re.search(rf"\b({month_pattern})\b", text, re.IGNORECASE)
    if month_only:
        return f"{month_only.group(1).title()} {date.today().year}"
    return None


def normalize_payroll_text(message):
    lowered = str(message or "").lower()
    return re.sub(r"\breimbur\w*\b", "reimbursement", lowered)


def payslip_links(record):
    month = str(record.get("salary_month") or "")
    encoded_month = quote(month)
    return f"/payslip?month={encoded_month}", f"/payslip/download?month={encoded_month}"


def get_employee_salary_records(employee_id):
    response = (
        supabase.table("salary_records")
        .select("*")
        .eq("employee_id", employee_id)
        .execute()
    )
    return sorted(response.data or [], key=lambda row: salary_month_date(row.get("salary_month")), reverse=True)


def get_salary_record(employee_id, salary_month=None):
    records = get_employee_salary_records(employee_id)
    if not records:
        return None
    if not salary_month:
        return records[0]

    requested = str(salary_month).strip().lower()
    requested_date = salary_month_date(salary_month)
    for record in records:
        record_month = record.get("salary_month", "")
        if str(record_month).strip().lower() == requested:
            return record
        if requested_date != date.min and salary_month_date(record_month) == requested_date:
            return record
    return None


def payslip_parts(record):
    basic = float(record.get("basic_salary") or 0)
    hra = float(record.get("hra") or 0)
    allowances = float(record.get("allowances") or 0)
    reimbursement = float(record.get("reimbursement") or 0)
    deductions = float(record.get("deductions") or 0)
    total_earnings = basic + hra + allowances + reimbursement
    expected_net = total_earnings - deductions
    net_salary = float(record.get("net_salary") if record.get("net_salary") is not None else expected_net)
    return {
        "basic": basic,
        "hra": hra,
        "allowances": allowances,
        "reimbursement": reimbursement,
        "deductions": deductions,
        "total_earnings": total_earnings,
        "total_deductions": deductions,
        "net_salary": net_salary,
    }


def render_payslip():
    if "employee_id" not in session:
        return redirect("/login")

    employee_id = session["employee_id"]
    salary_month = request.args.get("month")
    record = get_salary_record(employee_id, salary_month)
    records = get_employee_salary_records(employee_id)
    return render_template(
        "payslip.html",
        employee_id=employee_id,
        employee_name=session.get("employee_name", "Employee"),
        record=record,
        records=records,
        parts=payslip_parts(record) if record else None,
        requested_month=salary_month,
        generated_on=date.today().isoformat(),
    )


def pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_simple_pdf(lines):
    stream_lines = ["BT", "/F1 11 Tf", "72 760 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            stream_lines.append("T*")
        stream_lines.append(f"({pdf_escape(line)}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = BytesIO()
    pdf.write(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f"{index} 0 obj\n".encode("ascii"))
        pdf.write(obj)
        pdf.write(b"\nendobj\n")
    xref = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.write(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode("ascii"))
    return pdf.getvalue()


def download_payslip_pdf():
    if "employee_id" not in session:
        return redirect("/login")

    record = get_salary_record(session["employee_id"], request.args.get("month"))
    if not record:
        return Response("No payroll record was found.", status=404)

    parts = payslip_parts(record)
    lines = [
        "Enterprise HR Assistant - Payslip",
        f"Employee ID: {session['employee_id']}",
        f"Employee Name: {session.get('employee_name', 'Employee')}",
        f"Payroll Month: {record.get('salary_month')}",
        f"Generated On: {date.today().isoformat()}",
        "",
        "Earnings",
        f"Basic Salary: INR {money(parts['basic'])}",
        f"HRA: INR {money(parts['hra'])}",
        f"Allowances: INR {money(parts['allowances'])}",
        f"Reimbursements: INR {money(parts['reimbursement'])}",
        f"Total Earnings: INR {money(parts['total_earnings'])}",
        "",
        "Deductions",
        f"Deductions: INR {money(parts['deductions'])}",
        f"Total Deductions: INR {money(parts['total_deductions'])}",
        "",
        "Summary",
        f"Net Salary: INR {money(parts['net_salary'])}",
    ]
    pdf = build_simple_pdf(lines)
    filename = f"payslip-{session['employee_id']}-{record.get('salary_month')}.pdf".replace(" ", "-")
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def handle_payroll_query(employee_id, employee_name, message):
    lowered = normalize_payroll_text(message)
    requested_month = extract_salary_month(message)
    record = get_salary_record(employee_id, requested_month)
    if not record:
        month_text = f" for {requested_month}" if requested_month else ""
        return jsonify({"reply": f"No payroll record was found{month_text}."}), 200

    parts = payslip_parts(record)
    if "compare" in lowered or "change" in lowered or "last month" in lowered:
        records = get_employee_salary_records(employee_id)
        if len(records) < 2:
            return jsonify({"reply": "I need at least two payroll records to compare salary months."}), 200
        current = payslip_parts(records[0])
        previous = payslip_parts(records[1])
        difference = current["net_salary"] - previous["net_salary"]
        direction = "increase" if difference >= 0 else "decrease"
        reasons = []
        for label, key in (
            ("Reimbursement", "reimbursement"),
            ("Deductions", "deductions"),
            ("Allowances", "allowances"),
            ("HRA", "hra"),
            ("Basic salary", "basic"),
        ):
            delta = current[key] - previous[key]
            if abs(delta) >= 0.01:
                reasons.append(f"{label}: INR {money(abs(delta))} {'higher' if delta > 0 else 'lower'}")
        if not reasons:
            reasons.append("No component-level change was found.")
        reply = (
            f"{employee_name}, salary comparison:\n"
            f"Previous Month ({month_display(records[1].get('salary_month'))}): INR {money(previous['net_salary'])}\n"
            f"Current Month ({month_display(records[0].get('salary_month'))}): INR {money(current['net_salary'])}\n"
            f"Difference: INR {money(abs(difference))} {direction}\n\n"
            "Reason:\n"
            + "\n".join(reasons)
        )
        return jsonify({"reply": reply}), 200

    wants_history = (
        "history" in lowered
        or "previous salaries" in lowered
        or "trend" in lowered
        or "all months" in lowered
        or "all month" in lowered
        or "every month" in lowered
        or "month wise" in lowered
    )
    if wants_history:
        records = get_employee_salary_records(employee_id)
        lines = [f"{employee_name}, here is your salary history:"]
        for row in records:
            row_parts = payslip_parts(row)
            lines.append(f"{month_display(row.get('salary_month'))}: INR {money(row_parts['net_salary'])}")
        return jsonify({"reply": "\n".join(lines)}), 200

    if "highest" in lowered:
        records = get_employee_salary_records(employee_id)
        if "reimbursement" in lowered:
            highest = max(records, key=lambda row: payslip_parts(row)["reimbursement"])
            highest_parts = payslip_parts(highest)
            return jsonify({"reply": f"Your highest reimbursement was INR {money(highest_parts['reimbursement'])} in {month_display(highest.get('salary_month'))}."}), 200
        highest = max(records, key=lambda row: payslip_parts(row)["net_salary"])
        highest_parts = payslip_parts(highest)
        return jsonify({"reply": f"Your highest net salary was INR {money(highest_parts['net_salary'])} in {month_display(highest.get('salary_month'))}."}), 200

    if "lowest" in lowered:
        records = get_employee_salary_records(employee_id)
        if "reimbursement" in lowered:
            lowest = min(records, key=lambda row: payslip_parts(row)["reimbursement"])
            lowest_parts = payslip_parts(lowest)
            return jsonify({"reply": f"Your lowest reimbursement was INR {money(lowest_parts['reimbursement'])} in {month_display(lowest.get('salary_month'))}."}), 200
        lowest = min(records, key=lambda row: payslip_parts(row)["net_salary"])
        lowest_parts = payslip_parts(lowest)
        return jsonify({"reply": f"Your lowest net salary was INR {money(lowest_parts['net_salary'])} in {month_display(lowest.get('salary_month'))}."}), 200

    if "reimbursement" in lowered and ("year" in lowered or "total" in lowered):
        current_year = date.today().year
        total = sum(
            payslip_parts(row)["reimbursement"]
            for row in get_employee_salary_records(employee_id)
            if salary_month_date(row.get("salary_month")).year == current_year
        )
        return jsonify({"reply": f"{employee_name}, total reimbursements in {current_year} are INR {money(total)}."}), 200

    if "deduction" in lowered:
        reply = f"{employee_name}, deductions for {month_display(record.get('salary_month'))} were INR {money(parts['deductions'])}."
    elif "reimbursement" in lowered:
        reply = f"{employee_name}, reimbursements for {month_display(record.get('salary_month'))} were INR {money(parts['reimbursement'])}."
    elif "hra" in lowered:
        reply = f"{employee_name}, HRA for {month_display(record.get('salary_month'))} was INR {money(parts['hra'])}."
    elif "breakdown" in lowered or "payslip" in lowered or "download" in lowered or "generate" in lowered:
        view_link, download_link = payslip_links(record)
        reply = (
            f"{employee_name}, your payslip for {month_display(record.get('salary_month'))} is ready.\n"
            f"View: {view_link}\n"
            f"Download PDF: {download_link}"
        )
    else:
        reply = (
            f"{employee_name}, your salary for {month_display(record.get('salary_month'))}:\n"
            f"Basic: INR {money(parts['basic'])}\n"
            f"HRA: INR {money(parts['hra'])}\n"
            f"Allowances: INR {money(parts['allowances'])}\n"
            f"Reimbursements: INR {money(parts['reimbursement'])}\n"
            f"Deductions: INR {money(parts['deductions'])}\n"
            f"Net Salary: INR {money(parts['net_salary'])}"
        )

    return jsonify({"reply": reply}), 200


def is_payroll_message(message):
    lowered = normalize_payroll_text(message)
    expense_context = ("expense", "claim", "receipt", "bill", "submit", "upload", "reimburse me")
    if any(keyword in lowered for keyword in expense_context):
        return False

    keywords = (
        "salary",
        "payroll",
        "payslip",
        "net salary",
        "deduction",
        "hra",
        "salary history",
        "earn",
        "earned",
    )
    reimbursement_query = (
        "reimbursement" in lowered
        and any(keyword in lowered for keyword in ("salary", "payroll", "payslip", "received", "added", "year", "month", "total"))
    )
    return any(keyword in lowered for keyword in keywords) or reimbursement_query

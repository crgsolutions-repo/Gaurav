import argparse
import csv
import json
import os
import re
import time
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SCENARIO_FILE = PROJECT_ROOT / "conversation_scenarios.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "test_results"
MIN_REQUEST_INTERVAL_SECONDS = 6.0


def load_scenarios(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    decoder = json.JSONDecoder()
    position = 0
    scenarios = []

    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        value, position = decoder.raw_decode(text, position)
        if isinstance(value, list):
            scenarios.extend(value)
        elif isinstance(value, dict):
            scenarios.append(value)
        else:
            raise ValueError("Scenario data must contain JSON objects or arrays of objects.")

    normalized = []
    for index, scenario in enumerate(scenarios, start=1):
        name = str(scenario.get("name") or f"scenario_{index:03d}").strip()
        messages = scenario.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Scenario '{name}' must contain a non-empty messages list.")
        normalized.append({"name": name, "messages": [str(message).strip() for message in messages]})
    return normalized


def parse_count(value, total):
    if str(value).strip().lower() == "all":
        return total
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--count must be 25, 50, 100, all, or another positive number.") from exc
    if count <= 0:
        raise argparse.ArgumentTypeError("--count must be positive.")
    return min(count, total)


class RateLimiter:
    def __init__(self, delay_seconds):
        self.interval = max(float(delay_seconds), MIN_REQUEST_INTERVAL_SECONDS)
        self.last_request_at = None

    def wait(self):
        if self.last_request_at is not None:
            remaining = self.interval - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()


class MemoryQuery:
    def __init__(self, database, table_name):
        self.database = database
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.row_limit = None
        self.order_field = None
        self.order_desc = False

    def select(self, *_args):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def order(self, field, desc=False):
        self.order_field = field
        self.order_desc = desc
        return self

    def execute(self):
        rows = self.database.tables.setdefault(self.table_name, [])
        if self.operation == "insert":
            values = dict(self.payload)
            values.setdefault("id", str(len(rows) + 1))
            rows.append(values)
            return SimpleNamespace(data=[dict(values)])

        indexes = [
            index
            for index, row in enumerate(rows)
            if all(str(row.get(field)) == str(value) for field, value in self.filters)
        ]
        if self.operation == "update":
            updated = []
            for index in indexes:
                rows[index].update(self.payload)
                updated.append(dict(rows[index]))
            return SimpleNamespace(data=updated[: self.row_limit] if self.row_limit else updated)

        selected = [dict(rows[index]) for index in indexes]
        if self.order_field:
            selected.sort(key=lambda row: str(row.get(self.order_field) or ""), reverse=self.order_desc)
        if self.row_limit:
            selected = selected[: self.row_limit]
        return SimpleNamespace(data=selected)


class MemoryDatabase:
    def __init__(self, employee_id):
        self.employee_id = employee_id
        self.seed = self._seed_tables()
        self.tables = deepcopy(self.seed)

    def _seed_tables(self):
        today = date.today()
        current_month = today.strftime("%B %Y")
        previous_month_end = today.replace(day=1) - timedelta(days=1)
        previous_month = previous_month_end.strftime("%B %Y")
        april = date(today.year, 4, 1).strftime("%B %Y")
        yesterday = today - timedelta(days=1)
        return {
            "employees": [
                {
                    "employee_id": self.employee_id,
                    "name": "QA Employee",
                    "email": "qa@example.com",
                    "password": "qa-only",
                    "role": "employee",
                }
            ],
            "employee_leave_balance": [
                {"employee_id": self.employee_id, "leave_type": "Casual Leave", "remaining_leaves": 2, "used_leaves": 3},
                {"employee_id": self.employee_id, "leave_type": "Privilege Leave", "remaining_leaves": 8, "used_leaves": 1},
                {"employee_id": self.employee_id, "leave_type": "Paternity Leave", "remaining_leaves": 10, "used_leaves": 0},
                {"employee_id": self.employee_id, "leave_type": "Marriage Leave", "remaining_leaves": 5, "used_leaves": 0},
                {"employee_id": self.employee_id, "leave_type": "Bereavement Leave", "remaining_leaves": 5, "used_leaves": 0},
                {"employee_id": self.employee_id, "leave_type": "Unpaid Leave", "remaining_leaves": 30, "used_leaves": 0},
            ],
            "leave_requests": [
                {
                    "id": "leave-1",
                    "employee_id": self.employee_id,
                    "leave_type": "Casual Leave",
                    "from_date": previous_month_end.replace(day=5).isoformat(),
                    "to_date": previous_month_end.replace(day=5).isoformat(),
                    "leave_duration": "Full Day",
                    "reason": "Personal",
                    "status": "Approved",
                    "created_at": previous_month_end.replace(day=1).isoformat(),
                },
                {
                    "id": "leave-2",
                    "employee_id": self.employee_id,
                    "leave_type": "Privilege Leave",
                    "from_date": (today + timedelta(days=14)).isoformat(),
                    "to_date": (today + timedelta(days=15)).isoformat(),
                    "leave_duration": "Full Day",
                    "reason": "Planned",
                    "status": "Pending",
                    "created_at": today.isoformat(),
                },
            ],
            "attendance": [
                {
                    "id": "attendance-1",
                    "employee_id": self.employee_id,
                    "date": yesterday.isoformat(),
                    "punch_in": "09:00:00",
                    "punch_out": None,
                    "status": "Present",
                },
                {
                    "id": "attendance-2",
                    "employee_id": self.employee_id,
                    "date": today.replace(day=1).isoformat(),
                    "punch_in": "09:10:00",
                    "punch_out": "18:05:00",
                    "status": "Present",
                },
                {
                    "id": "attendance-3",
                    "employee_id": self.employee_id,
                    "date": previous_month_end.replace(day=2).isoformat(),
                    "punch_in": "09:05:00",
                    "punch_out": "18:00:00",
                    "status": "Present",
                },
            ],
            "expenses": [
                {
                    "id": "expense-1",
                    "employee_id": self.employee_id,
                    "expense_type": "Travel",
                    "amount": 4500,
                    "description": "Client travel",
                    "status": "Approved",
                    "created_at": previous_month_end.replace(day=10).isoformat(),
                },
                {
                    "id": "expense-2",
                    "employee_id": self.employee_id,
                    "expense_type": "Food",
                    "amount": 800,
                    "description": "Client dinner",
                    "status": "Pending",
                    "created_at": today.isoformat(),
                },
            ],
            "salary_records": [
                {
                    "id": "salary-current",
                    "employee_id": self.employee_id,
                    "salary_month": current_month,
                    "basic_salary": 50000,
                    "hra": 10000,
                    "allowances": 5000,
                    "reimbursement": 0,
                    "deductions": 2000,
                    "net_salary": 63000,
                },
                {
                    "id": "salary-previous",
                    "employee_id": self.employee_id,
                    "salary_month": previous_month,
                    "basic_salary": 50000,
                    "hra": 10000,
                    "allowances": 5000,
                    "reimbursement": 4500,
                    "deductions": 2000,
                    "net_salary": 67500,
                },
                {
                    "id": "salary-april",
                    "employee_id": self.employee_id,
                    "salary_month": april,
                    "basic_salary": 50000,
                    "hra": 10000,
                    "allowances": 5000,
                    "reimbursement": 3000,
                    "deductions": 2000,
                    "net_salary": 66000,
                },
            ],
            "conversation_workflows": [],
            "conversations": [],
        }

    def reset(self):
        self.tables = deepcopy(self.seed)

    def table(self, table_name):
        return MemoryQuery(self, table_name)


def offline_fallback(message, **_kwargs):
    text = str(message or "").lower()
    if any(word in text for word in ("help", "guide", "not sure")):
        reply = "I can help with leave, attendance, reimbursements, payroll, payslips, and HR summaries. Which area do you need?"
    else:
        reply = "Could you clarify whether this is about leave, attendance, reimbursement, payroll, or another HR request?"
    return {
        "intent": "GENERAL_HR_QUERY",
        "leave_type": "UNKNOWN",
        "from_date": "UNKNOWN",
        "to_date": "UNKNOWN",
        "duration": "UNKNOWN",
        "reason": "UNKNOWN",
        "amount": "UNKNOWN",
        "expense_type": "UNKNOWN",
        "description": "UNKNOWN",
        "reply": reply,
    }


def offline_copilot(message, *_args, **_kwargs):
    text = str(message or "").lower()
    if "wife" in text and any(word in text for word in ("due", "pregnant", "expecting")):
        return "You may be eligible for Paternity Leave. I can explain the policy or help prepare a request when you are ready."
    if "passed away" in text or "died" in text:
        return "I am sorry for your loss. Bereavement Leave may be available, and I can help when you are ready."
    if "married" in text or "marriage" in text or "wedding" in text:
        return "Marriage Leave may be available. I can check your balance or explain the policy."
    if "punch out" in text:
        return "This may need an attendance correction. I can help review the attendance record and required details."
    if "harass" in text:
        return "I am sorry you are dealing with this. You can report workplace harassment to HR, another manager, or the confidential ethics channel."
    if "manager" in text and "leave" in text and any(word in text for word in ("expense", "claim", "approval")):
        return "I can check whether an authorized delegate or approval escalation is available for your pending expense."
    return offline_fallback(message)["reply"]


class InProcessTransport:
    def __init__(self, employee_id, employee_name, role):
        import app as app_module
        import assistant_service
        import expense_service
        import intent_handlers
        import manager_approval
        import manager_expenses
        import payroll_service
        import workflow_store

        self.app_module = app_module
        self.employee_id = employee_id
        self.employee_name = employee_name
        self.role = role
        self.database = MemoryDatabase(employee_id)
        self.patchers = [
            patch.object(app_module, "supabase", self.database),
            patch.object(app_module, "process_hr_request", side_effect=offline_fallback),
            patch.object(app_module, "plan_conversation", return_value=None),
            patch.object(app_module, "generate_copilot_response", side_effect=offline_copilot),
            patch.object(assistant_service, "supabase", self.database),
            patch.object(expense_service, "supabase", self.database),
            patch.object(intent_handlers, "supabase", self.database),
            patch.object(manager_approval, "supabase", self.database),
            patch.object(manager_expenses, "supabase", self.database),
            patch.object(payroll_service, "supabase", self.database),
            patch.object(workflow_store, "supabase", self.database),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = None

    def close(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def start_scenario(self, _scenario):
        self.database.reset()
        self.client = self.app_module.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["employee_id"] = self.employee_id
            browser_session["employee_name"] = self.employee_name
            browser_session["role"] = self.role

    def send(self, message):
        response = self.client.post("/chat", json={"message": message})
        data = response.get_json(silent=True) or {}
        return data.get("reply") or response.get_data(as_text=True), response.status_code

    def active_workflow(self):
        rows = [
            row
            for row in self.database.tables.get("conversation_workflows", [])
            if row.get("employee_id") == self.employee_id and row.get("status") == "active"
        ]
        return rows[-1] if rows else None


class HttpTransport:
    def __init__(self, base_url, email=None, password=None):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.session = requests.Session()
        if email and password:
            response = self.session.post(
                f"{self.base_url}/login",
                data={"email": email, "password": password},
                timeout=30,
            )
            response.raise_for_status()

    def close(self):
        self.session.close()

    def start_scenario(self, _scenario):
        response = self.session.get(f"{self.base_url}/", timeout=30)
        if response.status_code >= 400:
            response.raise_for_status()

    def send(self, message):
        response = self.session.post(f"{self.base_url}/chat", json={"message": message}, timeout=60)
        data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
        return data.get("reply") or response.text, response.status_code

    def active_workflow(self):
        return None


CANCEL_WORDS = (
    "changed my mind",
    "not now",
    "maybe later",
    "forget it",
    "never mind",
    "leave it",
    "nothing",
    "no thanks",
    "don't need",
    "dont need",
    "ignore that",
    "skip it",
)
WORKFLOW_PROMPTS = (
    "which leave type",
    "date or date range",
    "full day or half day",
    "short reason",
    "amount would you like to claim",
    "choose an expense type",
    "attach the receipt",
)
GENERIC_RESPONSES = (
    "could you clarify",
    "i can only help with company hr topics",
    "something went wrong",
    "no active workflow",
)


def expected_keywords(scenario_name, message):
    text = message.lower()
    rules = []
    if "wife" in text and "due" in text or "expecting a child" in text or "baby is expected" in text:
        rules.append(("copilot_failure", ("paternity leave",)))
    if "child was born" in text or "became a father" in text:
        rules.append(("copilot_failure", ("paternity leave",)))
    if "married" in text or "wedding" in text or "my marriage" in text:
        rules.append(("copilot_failure", ("marriage leave",)))
    if "passed away" in text or "death in my family" in text or "lost a close family" in text:
        rules.append(("copilot_failure", ("bereavement leave",)))
    if "forgot to punch out" in text or "forgot checkout" in text or "attendance seems incomplete" in text:
        rules.append(("copilot_failure", ("attendance", "correction", "incomplete")))
    if "compare" in text:
        rules.append(("comparison_failure", ("comparison", "difference")))
    if "highest salary" in text or "earn the most" in text:
        rules.append(("recommendation_failure", ("highest", "salary")))
    if "running low" in text:
        rules.append(("recommendation_failure", ("low", "leave")))
    if "category do i use most" in text or "expenses do i usually claim" in text:
        rules.append(("recommendation_failure", ("category", "travel", "claim")))
    if scenario_name.startswith("summary_"):
        rules.append(("copilot_failure", ("summary", "attendance", "payroll")))
    return rules


def validate_turn(scenario, turn_index, message, response, status_code, history, active_workflow):
    scenario_name = scenario["name"]
    text = message.lower()
    reply = str(response or "").strip()
    lowered_reply = reply.lower()
    categories = []
    notes = []
    suspicious = False

    if status_code >= 400:
        categories.append("request_error")
        notes.append(f"HTTP/status error {status_code}")
    if not reply:
        categories.append("unanswered_question")
        notes.append("Empty response")
    elif len(reply) < 12:
        suspicious = True
        notes.append("Very short response")

    is_cancel = any(phrase in text for phrase in CANCEL_WORDS)
    if is_cancel:
        if "cancel" not in lowered_reply and "no problem" not in lowered_reply:
            categories.append("cancellation_failure")
            notes.append("Cancellation was not acknowledged")
        if active_workflow:
            categories.append("cancellation_failure")
            notes.append("Workflow remained active after cancellation")

    is_resume = text.strip() in {"continue", "resume", "continue leave request", "continue reimbursement request"}
    if is_resume and ("no active workflow" in lowered_reply or "could you clarify" in lowered_reply):
        categories.append("workflow_resume_issue")
        notes.append("Explicit resume did not continue the active workflow")

    retrieval = any(
        phrase in text
        for phrase in ("show", "what was", "latest", "most recent", "history", "salary", "payroll", "attendance", "compare")
    )
    if retrieval and any(prompt in lowered_reply for prompt in WORKFLOW_PROMPTS):
        categories.append("workflow_contamination")
        notes.append("Retrieval/comparison response was intercepted by a workflow prompt")

    if turn_index > 0 and retrieval:
        previous_text = history[-1]["user_message"].lower()
        previous_was_workflow = any(word in previous_text for word in ("leave", "expense", "reimbursement", "bill"))
        if previous_was_workflow and not any(word in lowered_reply for word in ("salary", "payroll", "attendance", "request", "comparison", "reimbursement")):
            categories.append("context_switching_failure")
            notes.append("New topic was not answered after workflow interruption")

    for category, keywords in expected_keywords(scenario_name, message):
        if not any(keyword in lowered_reply for keyword in keywords):
            categories.append(category)
            notes.append("Expected one of: " + ", ".join(keywords))

    if "?" in message or text.startswith(("should", "will", "is ", "what ", "how ", "can ")):
        if any(prompt in lowered_reply for prompt in WORKFLOW_PROMPTS) and not any(
            phrase in lowered_reply for phrase in ("if the expense", "may be", "usually", "policy", "recommend")
        ):
            categories.append("unanswered_question")
            notes.append("Question received a form-style workflow prompt")

    if any(phrase in lowered_reply for phrase in GENERIC_RESPONSES):
        suspicious = True
        notes.append("Generic or fallback response")

    categories = sorted(set(categories))
    return not categories, categories, "; ".join(dict.fromkeys(notes)), suspicious


def write_results(output_dir, records, scenario_count, interval):
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "results.json"
    csv_path = output_dir / "results.csv"
    report_path = output_dir / "summary_report.txt"

    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    fieldnames = [
        "scenario_name",
        "turn",
        "user_message",
        "bot_response",
        "timestamp",
        "status_code",
        "pass",
        "notes",
        "failure_categories",
        "suspicious",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    passed = sum(1 for record in records if record["pass"])
    failed = len(records) - passed
    suspicious = sum(1 for record in records if record["suspicious"])
    categories = Counter(
        category
        for record in records
        for category in record["failure_categories"]
    )
    lines = [
        "Conversational QA Summary",
        "=========================",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Scenarios executed: {scenario_count}",
        f"Total requests: {len(records)}",
        f"Passed: {passed}",
        f"Failed: {failed}",
        f"Suspicious responses: {suspicious}",
        f"Effective delay: {interval:.1f} seconds",
        "",
        "Common failure categories:",
    ]
    if categories:
        lines.extend(f"- {category}: {count}" for category, count in categories.most_common())
    else:
        lines.append("- None")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, csv_path, report_path


def build_parser():
    parser = argparse.ArgumentParser(description="Run conversational QA scenarios against the HR assistant.")
    parser.add_argument("--count", default="25", help="Number of scenarios to run: 25, 50, 100, all, or another positive number.")
    parser.add_argument("--delay", type=float, default=10.0, help="Requested delay between requests. Effective delay is never below 6 seconds.")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIO_FILE), help="Scenario dataset path.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for JSON, CSV, and summary outputs.")
    parser.add_argument("--transport", choices=("flask", "http"), default="flask", help="Safe in-process Flask transport or a dedicated HTTP QA server.")
    parser.add_argument("--base-url", default=os.getenv("QA_BASE_URL", "http://127.0.0.1:5000"))
    parser.add_argument("--email", default=os.getenv("QA_EMAIL"))
    parser.add_argument("--password", default=os.getenv("QA_PASSWORD"))
    parser.add_argument("--employee-id", default=os.getenv("QA_EMPLOYEE_ID", "EMP101"))
    parser.add_argument("--employee-name", default=os.getenv("QA_EMPLOYEE_NAME", "QA Employee"))
    parser.add_argument("--role", default=os.getenv("QA_ROLE", "employee"))
    return parser


def main():
    args = build_parser().parse_args()
    scenarios = load_scenarios(args.scenarios)
    count = parse_count(args.count, len(scenarios))
    selected = scenarios[:count]
    limiter = RateLimiter(args.delay)

    if args.transport == "http":
        transport = HttpTransport(args.base_url, args.email, args.password)
    else:
        transport = InProcessTransport(args.employee_id, args.employee_name, args.role)

    records = []
    try:
        for scenario in selected:
            transport.start_scenario(scenario)
            history = []
            for turn_index, message in enumerate(scenario["messages"], start=1):
                limiter.wait()
                timestamp = datetime.now(timezone.utc).isoformat()
                try:
                    response, status_code = transport.send(message)
                except Exception as exc:
                    response = f"{type(exc).__name__}: {exc}"
                    status_code = 599
                active_workflow = transport.active_workflow()
                passed, categories, notes, suspicious = validate_turn(
                    scenario,
                    turn_index - 1,
                    message,
                    response,
                    status_code,
                    history,
                    active_workflow,
                )
                record = {
                    "scenario_name": scenario["name"],
                    "turn": turn_index,
                    "user_message": message,
                    "bot_response": response,
                    "timestamp": timestamp,
                    "status_code": status_code,
                    "pass": passed,
                    "notes": notes,
                    "failure_categories": categories,
                    "suspicious": suspicious,
                }
                records.append(record)
                history.append(record)
                print(f"[{'PASS' if passed else 'FAIL'}] {scenario['name']} #{turn_index}: {message}")
    finally:
        transport.close()

    paths = write_results(Path(args.output_dir), records, len(selected), limiter.interval)
    failed = sum(1 for record in records if not record["pass"])
    print(f"\nExecuted {len(selected)} scenarios / {len(records)} requests. Failures: {failed}")
    for path in paths:
        print(path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

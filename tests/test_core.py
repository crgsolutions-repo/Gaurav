import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, get_flashed_messages, session

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("RAG_ENABLED", "false")
os.environ.setdefault("GEMINI_PLANNER_ENABLED", "false")

import intent_handlers
import assistant_service
import expense_service
import manager_expenses
import manager_approval
import ocr_service
import payroll_service
import policy_service
import conversation_planner
import stress_test
import workflow_store
import app as app_module
from workflow_store import CANCELLED_STATUS


class FakeQuery:
    def __init__(self, supabase, table_name):
        self.supabase = supabase
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
        rows = self.supabase.tables.setdefault(self.table_name, [])
        if self.operation == "insert":
            inserted = dict(self.payload)
            inserted.setdefault("id", str(len(rows) + 1))
            rows.append(inserted)
            return SimpleNamespace(data=[dict(inserted)])

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
            selected.sort(key=lambda row: row.get(self.order_field), reverse=self.order_desc)
        if self.row_limit:
            selected = selected[: self.row_limit]
        return SimpleNamespace(data=selected)


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def table(self, table_name):
        return FakeQuery(self, table_name)


class CoreStabilizationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def patch_supabase(self, fake):
        return patch.multiple(
            intent_handlers,
            supabase=fake,
        )

    def test_attendance_punch_in_out_validation(self):
        fake = FakeSupabase({"attendance": []})
        with patch.object(intent_handlers, "supabase", fake):
            response, status = intent_handlers.handle_punch_in("EMP101", "Gaurav", {})
            self.assertEqual(status, 200)
            self.assertIn("recorded", response.get_json()["reply"])
            self.assertEqual(len(fake.tables["attendance"]), 1)

            response, _ = intent_handlers.handle_punch_in("EMP101", "Gaurav", {})
            self.assertIn("already punched in", response.get_json()["reply"])

            response, _ = intent_handlers.handle_punch_out("EMP101", "Gaurav", {})
            self.assertIn("punch out has been recorded", response.get_json()["reply"])

            response, _ = intent_handlers.handle_punch_out("EMP101", "Gaurav", {})
            self.assertIn("already punched out", response.get_json()["reply"])

    def test_leave_payload_validation_success_and_duplicate_detection(self):
        future = (date.today() + timedelta(days=3)).isoformat()
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "remaining_leaves": 4,
                        "used_leaves": 1,
                    }
                ],
                "leave_requests": [],
            }
        )

        payload = {
            "leave_type": "casual",
            "from_date": future,
            "to_date": future,
            "duration": "full day",
            "reason": "Family function",
        }
        with patch.object(intent_handlers, "supabase", fake):
            valid_payload, error = intent_handlers.validate_leave_payload("EMP101", payload)
            self.assertIsNone(error)
            self.assertEqual(valid_payload["leave_type"], "Casual Leave")
            self.assertEqual(valid_payload["deduction"], 1)

            fake.tables["leave_requests"].append(
                {
                    "employee_id": "EMP101",
                    "from_date": future,
                    "to_date": future,
                    "status": "Pending",
                }
            )
            _valid_payload, error = intent_handlers.validate_leave_payload("EMP101", payload)
            self.assertIn("already have", error)

    def test_insufficient_leave_balance_returns_detailed_message_without_insert(self):
        start_date = date.today() + timedelta(days=3)
        end_date = start_date + timedelta(days=4)
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "remaining_leaves": 2,
                        "used_leaves": 3,
                    },
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Privilege Leave",
                        "remaining_leaves": 8,
                        "used_leaves": 0,
                    },
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Unpaid Leave",
                        "remaining_leaves": 20,
                        "used_leaves": 0,
                    },
                ],
                "leave_requests": [],
            }
        )
        workflow = {
            "id": "wf1",
            "step": "confirm",
            "payload": {
                "leave_type": "Casual Leave",
                "from_date": start_date.isoformat(),
                "to_date": end_date.isoformat(),
                "duration": "Full Day",
                "reason": "Family function",
            },
        }

        with (
            patch.object(intent_handlers, "supabase", fake),
            patch.object(intent_handlers, "get_active_workflow", return_value=workflow),
        ):
            response, _ = intent_handlers.handle_confirm_leave("EMP101", "Gaurav")

        reply = response.get_json()["reply"]
        self.assertIn("You requested 5 days of Casual Leave but only 2 days are available.", reply)
        self.assertIn("Casual Leave: 2", reply)
        self.assertIn("Privilege Leave: 8", reply)
        self.assertIn("Unpaid Leave: 20", reply)
        self.assertEqual(fake.tables["leave_requests"], [])

    def test_common_tomorrow_typos_and_explicit_date_are_understood(self):
        tomorrow = date.today() + timedelta(days=1)
        explicit_tomorrow = tomorrow.strftime("%d-%m-%Y")

        self.assertEqual(intent_handlers.parse_hr_date("tommorow"), tomorrow)
        self.assertEqual(intent_handlers.infer_date_from_message("apply leave for tommorow"), tomorrow)
        self.assertEqual(intent_handlers.infer_date_from_message(f"tommorow {explicit_tomorrow}"), tomorrow)
        self.assertEqual(intent_handlers.infer_date_from_message(explicit_tomorrow), tomorrow)
        self.assertEqual(intent_handlers.infer_date_from_message("then day after tommorow"), date.today() + timedelta(days=2))

    def test_date_range_and_requested_days_are_preserved(self):
        start_date = date.today() + timedelta(days=2)
        end_date = start_date + timedelta(days=10)
        payload = intent_handlers.merge_message_entities(
            {},
            f"want leave from {start_date.strftime('%d-%m-%Y')} to {end_date.strftime('%d-%m-%Y')}",
        )

        self.assertEqual(payload["from_date"], start_date.isoformat())
        self.assertEqual(payload["to_date"], end_date.isoformat())
        self.assertEqual(payload["duration"], "Full Day")

        payload = intent_handlers.merge_message_entities({}, "i want to apply for 30 days leave")
        payload = intent_handlers.merge_message_entities(payload, start_date.isoformat())

        self.assertEqual(payload["requested_days"], "30")
        self.assertEqual(payload["from_date"], start_date.isoformat())
        self.assertEqual(payload["to_date"], (start_date + timedelta(days=29)).isoformat())
        self.assertEqual(payload["duration"], "Full Day")

    def test_week_leave_from_next_monday_calculates_end_date(self):
        next_monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
        payload = intent_handlers.merge_message_entities({}, "i need paternity leave for a week from next monday")

        self.assertEqual(payload["leave_type"], "Paternity Leave")
        self.assertEqual(payload["from_date"], next_monday.isoformat())
        self.assertEqual(payload["to_date"], (next_monday + timedelta(days=6)).isoformat())
        self.assertEqual(payload["duration"], "Full Day")

    def test_week_request_during_duration_step_updates_existing_start_date(self):
        start_date = date.today() + timedelta(days=8)
        payload = {
            "leave_type": "Paternity Leave",
            "from_date": start_date.isoformat(),
            "to_date": start_date.isoformat(),
            "duration": "UNKNOWN",
            "reason": "UNKNOWN",
        }

        payload = intent_handlers.merge_message_entities(payload, "i need for at least a week")
        payload = intent_handlers.merge_workflow_step_reply(payload, "duration", "i need for at least a week")

        self.assertEqual(payload["to_date"], (start_date + timedelta(days=6)).isoformat())
        self.assertEqual(payload["duration"], "Full Day")

    def test_multiday_range_reaches_reason_step_after_leave_type(self):
        start_date = date.today() + timedelta(days=2)
        end_date = start_date + timedelta(days=3)
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "remaining_leaves": 10,
                        "used_leaves": 0,
                    }
                ],
                "leave_requests": [],
            }
        )
        workflow = {
            "id": "wf1",
            "step": "leave_type",
            "payload": {
                "leave_type": "UNKNOWN",
                "from_date": start_date.isoformat(),
                "to_date": end_date.isoformat(),
                "duration": "Full Day",
                "reason": "UNKNOWN",
            },
        }
        saved_workflows = []

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(intent_handlers, "supabase", fake),
            patch.object(intent_handlers, "get_active_workflow", return_value=workflow),
            patch.object(intent_handlers, "upsert_workflow", side_effect=fake_upsert),
        ):
            response, _ = intent_handlers.handle_apply_leave(
                "EMP101",
                "Gaurav",
                {
                    "leave_type": "UNKNOWN",
                    "from_date": "UNKNOWN",
                    "to_date": "UNKNOWN",
                    "duration": "UNKNOWN",
                    "reason": "UNKNOWN",
                },
                "casual",
            )

        self.assertIn("short reason", response.get_json()["reply"])
        self.assertEqual(saved_workflows[-1]["step"], "reason")
        self.assertEqual(saved_workflows[-1]["payload"]["to_date"], end_date.isoformat())

    def test_date_validation_failure_clears_stale_end_date(self):
        payload = {"from_date": "2026-05-30", "to_date": "2026-05-29"}
        failed_step = intent_handlers.validation_failure_step("Leave end date cannot be before the start date.")

        payload = intent_handlers.reset_failed_payload_step(payload, failed_step)

        self.assertEqual(payload["from_date"], "UNKNOWN")
        self.assertEqual(payload["to_date"], "UNKNOWN")

    def test_duplicate_leave_validation_returns_to_date_step(self):
        payload = {"from_date": "2026-05-30", "to_date": "2026-05-30", "reason": "Family function"}
        failed_step = intent_handlers.validation_failure_step(
            "You already have a pending or approved leave request for these dates."
        )

        payload = intent_handlers.reset_failed_payload_step(payload, failed_step)

        self.assertEqual(failed_step, "from_date")
        self.assertEqual(payload["from_date"], "UNKNOWN")
        self.assertEqual(payload["to_date"], "UNKNOWN")

    def test_leave_workflow_reason_step_recovers_to_confirmation(self):
        future = (date.today() + timedelta(days=5)).isoformat()
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "remaining_leaves": 4,
                        "used_leaves": 1,
                    }
                ],
                "leave_requests": [],
            }
        )
        workflow = {
            "id": "wf1",
            "step": "reason",
            "payload": {
                "leave_type": "Casual Leave",
                "from_date": future,
                "to_date": future,
                "duration": "Full Day",
                "reason": "UNKNOWN",
            },
        }
        saved_workflows = []

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(intent_handlers, "supabase", fake),
            patch.object(intent_handlers, "get_active_workflow", return_value=workflow),
            patch.object(intent_handlers, "upsert_workflow", side_effect=fake_upsert),
        ):
            response, _ = intent_handlers.handle_apply_leave(
                "EMP101",
                "Gaurav",
                {
                    "leave_type": "UNKNOWN",
                    "from_date": "UNKNOWN",
                    "to_date": "UNKNOWN",
                    "duration": "UNKNOWN",
                    "reason": "UNKNOWN",
                },
                "Family function",
            )

        self.assertIn("please confirm", response.get_json()["reply"].lower())
        self.assertEqual(saved_workflows[-1]["step"], "confirm")
        self.assertEqual(saved_workflows[-1]["payload"]["reason"], "Family function")

    def test_confirm_and_cancel_workflows(self):
        future = (date.today() + timedelta(days=6)).isoformat()
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "remaining_leaves": 4,
                        "used_leaves": 1,
                    }
                ],
                "leave_requests": [],
            }
        )
        workflow = {
            "id": "wf1",
            "step": "confirm",
            "payload": {
                "leave_type": "Casual Leave",
                "from_date": future,
                "to_date": future,
                "duration": "Full Day",
                "reason": "Family function",
            },
        }
        finished = []

        with (
            patch.object(intent_handlers, "supabase", fake),
            patch.object(intent_handlers, "get_active_workflow", return_value=workflow),
            patch.object(intent_handlers, "finish_workflow", side_effect=lambda workflow_id, status="completed": finished.append((workflow_id, status))),
        ):
            response, _ = intent_handlers.handle_confirm_leave("EMP101", "Gaurav")

        self.assertIn("submitted", response.get_json()["reply"])
        self.assertEqual(fake.tables["leave_requests"][0]["status"], "Pending")
        self.assertEqual(finished, [("wf1", "completed")])

        with (
            patch.object(intent_handlers, "get_active_workflow", return_value=workflow),
            patch.object(intent_handlers, "finish_workflow", side_effect=lambda workflow_id, status="completed": finished.append((workflow_id, status))),
        ):
            response, _ = intent_handlers.handle_cancel_workflow("EMP101")

        self.assertIn("cancelled", response.get_json()["reply"])
        self.assertEqual(finished[-1], ("wf1", CANCELLED_STATUS))

    def test_clear_active_workflows_cancels_only_employee_active_rows(self):
        fake = FakeSupabase(
            {
                "conversation_workflows": [
                    {"id": "wf1", "employee_id": "EMP101", "status": "active", "workflow_type": "expense_request"},
                    {"id": "wf2", "employee_id": "EMP101", "status": "completed", "workflow_type": "leave_request"},
                    {"id": "wf3", "employee_id": "EMP102", "status": "active", "workflow_type": "expense_request"},
                ]
            }
        )

        with patch.object(workflow_store, "supabase", fake):
            workflow_store.clear_active_workflows("EMP101")

        self.assertEqual(fake.tables["conversation_workflows"][0]["status"], CANCELLED_STATUS)
        self.assertEqual(fake.tables["conversation_workflows"][1]["status"], "completed")
        self.assertEqual(fake.tables["conversation_workflows"][2]["status"], "active")

    def test_manager_approve_only_deducts_once_and_rejects_pending_only(self):
        future = (date.today() + timedelta(days=2)).isoformat()
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {
                        "id": "1",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": future,
                        "to_date": future,
                        "leave_duration": "Full Day",
                        "status": "Pending",
                    },
                    {
                        "id": "2",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": future,
                        "to_date": future,
                        "leave_duration": "Full Day",
                        "status": "Pending",
                    },
                ],
                "employee_leave_balance": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "remaining_leaves": 3,
                        "used_leaves": 1,
                    }
                ],
            }
        )

        with patch.object(manager_approval, "supabase", fake):
            with self.app.test_request_context("/manager/leaves/1/approve", method="POST"):
                session["role"] = "manager"
                manager_approval.approve_leave_request("1")
                messages = get_flashed_messages(with_categories=True)
                self.assertEqual(messages[-1], ("success", "Leave request approved."))

            self.assertEqual(fake.tables["leave_requests"][0]["status"], "Approved")
            self.assertEqual(fake.tables["employee_leave_balance"][0]["remaining_leaves"], 2)
            self.assertEqual(fake.tables["employee_leave_balance"][0]["used_leaves"], 2)

            with self.app.test_request_context("/manager/leaves/1/approve", method="POST"):
                session["role"] = "manager"
                manager_approval.approve_leave_request("1")
                messages = get_flashed_messages(with_categories=True)
                self.assertEqual(messages[-1][0], "error")

            self.assertEqual(fake.tables["employee_leave_balance"][0]["remaining_leaves"], 2)
            self.assertEqual(fake.tables["employee_leave_balance"][0]["used_leaves"], 2)

            with self.app.test_request_context("/manager/leaves/2/reject", method="POST", data={"reason": "Team capacity is low."}):
                session["role"] = "manager"
                session["employee_name"] = "Manager One"
                manager_approval.reject_leave_request("2")
                messages = get_flashed_messages(with_categories=True)
                self.assertEqual(messages[-1], ("success", "Leave request rejected."))

            self.assertEqual(fake.tables["leave_requests"][1]["status"], "Rejected")
            self.assertEqual(fake.tables["leave_requests"][1]["rejection_reason"], "Team capacity is low.")
            self.assertEqual(fake.tables["leave_requests"][1]["rejected_by"], "Manager One")
            self.assertTrue(fake.tables["leave_requests"][1]["rejected_at"])

    def test_manager_reject_requires_reason(self):
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {
                        "id": "1",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": "2026-06-02",
                        "to_date": "2026-06-02",
                        "leave_duration": "Full Day",
                        "status": "Pending",
                    }
                ]
            }
        )

        with patch.object(manager_approval, "supabase", fake):
            with self.app.test_request_context("/manager/leaves/1/reject", method="POST", data={"reason": ""}):
                session["role"] = "manager"
                manager_approval.reject_leave_request("1")
                messages = get_flashed_messages(with_categories=True)

        self.assertEqual(messages[-1], ("error", "A rejection reason is required."))
        self.assertEqual(fake.tables["leave_requests"][0]["status"], "Pending")

    def test_manager_unread_pending_leaves_are_tagged_and_marked_seen(self):
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {
                        "id": "1",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": "2026-06-02",
                        "to_date": "2026-06-02",
                        "leave_duration": "Full Day",
                        "status": "Pending",
                        "created_at": "2026-05-28T09:00:00+00:00",
                    },
                    {
                        "id": "2",
                        "employee_id": "EMP102",
                        "leave_type": "Unpaid Leave",
                        "from_date": "2026-06-01",
                        "to_date": "2026-06-01",
                        "leave_duration": "Full Day",
                        "status": "Pending",
                        "created_at": "2026-05-29T09:00:00+00:00",
                    },
                ]
            }
        )

        with patch.object(manager_approval, "supabase", fake):
            with self.app.test_request_context("/manager/leaves"):
                session["role"] = "manager"
                session["seen_pending_leave_ids"] = ["1"]

                self.assertEqual(manager_approval.get_unseen_pending_leave_count(), 1)
                leaves = manager_approval.get_pending_leave_requests(mark_seen=True)

                self.assertEqual([leave["id"] for leave in leaves], ["2", "1"])
                self.assertTrue(leaves[0]["_is_new"])
                self.assertFalse(leaves[1]["_is_new"])
                self.assertEqual(set(session["seen_pending_leave_ids"]), {"1", "2"})

    def test_manager_employees_on_leave_filters_approved_overlapping_dates(self):
        target = date.today().isoformat()
        next_week = (date.today() + timedelta(days=7)).isoformat()
        fake = FakeSupabase(
            {
                "employees": [
                    {"employee_id": "EMP101", "name": "Gaurav Patil"},
                    {"employee_id": "EMP102", "name": "Asha Rao"},
                ],
                "leave_requests": [
                    {
                        "id": "1",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": target,
                        "to_date": target,
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    },
                    {
                        "id": "2",
                        "employee_id": "EMP102",
                        "leave_type": "Unpaid Leave",
                        "from_date": next_week,
                        "to_date": next_week,
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    },
                    {
                        "id": "3",
                        "employee_id": "EMP103",
                        "leave_type": "Casual Leave",
                        "from_date": target,
                        "to_date": target,
                        "leave_duration": "Full Day",
                        "status": "Pending",
                    },
                ],
            }
        )

        with patch.object(manager_approval, "supabase", fake):
            on_leave = manager_approval.get_employees_on_leave(date.today())

        self.assertEqual(len(on_leave), 1)
        self.assertEqual(on_leave[0]["employee_name"], "Gaurav Patil")

    def test_small_expense_submits_without_receipt(self):
        fake = FakeSupabase({"expenses": []})
        workflow = {
            "id": "wf-expense-1",
            "step": "confirm",
            "payload": {
                "amount": "150",
                "expense_type": "Food",
                "description": "Snacks",
            },
        }
        finished = []

        with (
            patch.object(expense_service, "supabase", fake),
            patch.object(expense_service, "get_active_workflow", return_value=workflow),
            patch.object(expense_service, "finish_workflow", side_effect=lambda workflow_id, status="completed": finished.append((workflow_id, status))),
        ):
            response, _ = expense_service.handle_confirm_expense("EMP101", "Gaurav")

        self.assertIn("submitted", response.get_json()["reply"])
        self.assertEqual(fake.tables["expenses"][0]["status"], "Pending")
        self.assertIsNone(fake.tables["expenses"][0]["bill_image"])
        self.assertEqual(finished, [("wf-expense-1", "completed")])

    def test_expense_amount_step_accepts_bare_number_after_receipt_upload(self):
        workflow = {
            "id": "wf-expense-1",
            "step": "amount",
            "payload": {
                "amount": "UNKNOWN",
                "receipt_filename": "receipt.png",
            },
        }
        saved_workflows = []

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf-expense-1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(expense_service, "get_active_workflow", return_value=workflow),
            patch.object(expense_service, "upsert_workflow", side_effect=fake_upsert),
        ):
            response, _ = expense_service.handle_apply_expense(
                "EMP101",
                "Gaurav",
                {
                    "amount": "UNKNOWN",
                    "expense_type": "UNKNOWN",
                    "description": "UNKNOWN",
                },
                "5445.30",
            )

        self.assertIn("expense type", response.get_json()["reply"].lower())
        self.assertEqual(saved_workflows[-1]["step"], "expense_type")
        self.assertEqual(saved_workflows[-1]["payload"]["amount"], "5445.3")

    def test_expense_amount_parser_accepts_rupees_after_amount(self):
        self.assertEqual(expense_service.infer_amount_from_message("186 rupees for food"), 186)
        self.assertEqual(expense_service.infer_amount_from_message("5445.30"), 5445.30)

    def test_expense_type_number_does_not_overwrite_existing_amount(self):
        workflow = {
            "id": "wf-expense-1",
            "step": "expense_type",
            "payload": {
                "amount": "5445.30",
                "expense_type": "UNKNOWN",
                "description": "UNKNOWN",
                "receipt_filename": "receipt.png",
                "receipt_ocr_done": "true",
                "ocr_amount": 5445.30,
                "bill_date": date.today().isoformat(),
                "invoice_number": "INV-200",
                "vendor_name": "Cafe",
            },
        }
        saved_workflows = []

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf-expense-1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(expense_service, "get_active_workflow", return_value=workflow),
            patch.object(expense_service, "upsert_workflow", side_effect=fake_upsert),
        ):
            response, _ = expense_service.handle_apply_expense(
                "EMP101",
                "Gaurav",
                {
                    "amount": "UNKNOWN",
                    "expense_type": "UNKNOWN",
                    "description": "UNKNOWN",
                },
                "2",
            )

        self.assertIn("description", response.get_json()["reply"].lower())
        self.assertEqual(saved_workflows[-1]["step"], "description")
        self.assertEqual(saved_workflows[-1]["payload"]["amount"], "5445.30")
        self.assertEqual(saved_workflows[-1]["payload"]["expense_type"], "Food")

    def test_direct_receipt_upload_prefills_ocr_amount_and_asks_expense_type(self):
        saved_workflows = []
        ocr_result = SimpleNamespace(
            text="Cafe Bill\nInvoice INV-201\nTotal 5445.30\n",
            amount=5445.30,
            bill_date=date.today(),
            invoice_number="INV-201",
            vendor_name="Cafe Bill",
        )

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf-expense-1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(expense_service, "get_active_workflow", return_value=None),
            patch.object(expense_service, "upsert_workflow", side_effect=fake_upsert),
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
        ):
            response, _ = expense_service.handle_apply_expense(
                "EMP101",
                "Gaurav",
                {
                    "amount": "UNKNOWN",
                    "expense_type": "UNKNOWN",
                    "description": "UNKNOWN",
                },
                "upload this bill in expense",
                "receipt.png",
            )

        reply = response.get_json()["reply"]
        self.assertIn("I read the uploaded receipt as:", reply)
        self.assertIn("Amount:", reply)
        self.assertIn("expense type", reply.lower())
        self.assertEqual(saved_workflows[-1]["step"], "expense_type")
        self.assertEqual(saved_workflows[-1]["payload"]["amount"], "5445.3")

    def test_direct_receipt_upload_does_not_use_upload_phrase_as_description(self):
        saved_workflows = []
        ocr_result = SimpleNamespace(
            text="Cafe Bill\nInvoice INV-301\nTotal 5445.30\n",
            amount=5445.30,
            bill_date=date.today(),
            invoice_number="INV-301",
            vendor_name="Cafe Bill",
        )

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf-expense-1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(expense_service, "get_active_workflow", return_value=None),
            patch.object(expense_service, "upsert_workflow", side_effect=fake_upsert),
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
        ):
            response, _ = expense_service.handle_apply_expense(
                "EMP101",
                "Gaurav",
                {
                    "amount": "UNKNOWN",
                    "expense_type": "Food",
                    "description": "bill upload",
                },
                "bill upload",
                "receipt.png",
            )

        self.assertIn("description", response.get_json()["reply"].lower())
        self.assertEqual(saved_workflows[-1]["step"], "description")
        self.assertEqual(saved_workflows[-1]["payload"]["description"], "UNKNOWN")

    def test_receipt_reupload_with_claimed_amount_keeps_employee_amount_for_manager_review(self):
        workflow = {
            "id": "wf-expense-1",
            "step": "expense_type",
            "payload": {
                "amount": "812",
                "expense_type": "UNKNOWN",
                "description": "UNKNOWN",
            },
        }
        saved_workflows = []
        ocr_result = SimpleNamespace(
            text="Restaurant\nBill 7767\nTotal 812\n",
            amount=812,
            bill_date=date.today(),
            invoice_number="7767",
            vendor_name="Bhagini",
        )

        def fake_upsert(_employee_id, _workflow_type, step, payload):
            saved = {"id": "wf-expense-1", "step": step, "payload": dict(payload)}
            saved_workflows.append(saved)
            return saved

        with (
            patch.object(expense_service, "get_active_workflow", return_value=workflow),
            patch.object(expense_service, "upsert_workflow", side_effect=fake_upsert),
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
        ):
            response, _ = expense_service.handle_apply_expense(
                "EMP101",
                "Gaurav",
                {
                    "amount": "UNKNOWN",
                    "expense_type": "UNKNOWN",
                    "description": "UNKNOWN",
                },
                "no again read it carefully amount is 3150 total",
                "receipt.png",
            )

        reply = response.get_json()["reply"]
        self.assertIn("Claimed Amount", reply)
        self.assertIn("3150", reply)
        self.assertIn("812", reply)
        self.assertEqual(saved_workflows[-1]["payload"]["amount"], "3150.0")
        self.assertEqual(saved_workflows[-1]["payload"]["ocr_amount"], 812)

    def test_direct_rubbish_image_upload_is_rejected_without_workflow(self):
        ocr_result = SimpleNamespace(
            text="",
            amount=None,
            bill_date=None,
            invoice_number=None,
            vendor_name=None,
        )

        with (
            patch.object(expense_service, "get_active_workflow", return_value=None),
            patch.object(expense_service, "upsert_workflow") as upsert,
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
        ):
            response, _ = expense_service.handle_apply_expense(
                "EMP101",
                "Gaurav",
                {
                    "amount": "UNKNOWN",
                    "expense_type": "UNKNOWN",
                    "description": "UNKNOWN",
                },
                "",
                "image.png",
            )

        self.assertIn("could not recognize this as an expense receipt", response.get_json()["reply"])
        upsert.assert_not_called()

    def test_large_expense_amount_mismatch_goes_to_manager_review(self):
        fake = FakeSupabase({"expenses": []})
        workflow = {
            "id": "wf-expense-1",
            "step": "confirm",
            "payload": {
                "amount": "6000",
                "expense_type": "Travel",
                "description": "Client visit",
                "receipt_filename": "receipt.png",
            },
        }
        ocr_result = SimpleNamespace(
            text="Invoice INV-100\nTotal 5000\n",
            amount=5000,
            bill_date=date.today(),
            invoice_number="INV-100",
            vendor_name="Travel Co",
        )

        with (
            patch.object(expense_service, "supabase", fake),
            patch.object(expense_service, "get_active_workflow", return_value=workflow),
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
            patch.object(expense_service, "finish_workflow"),
        ):
            response, _ = expense_service.handle_confirm_expense("EMP101", "Gaurav")

        reply = response.get_json()["reply"]
        self.assertIn("manager review", reply)
        self.assertIn("6000", reply)
        self.assertIn("5000", reply)
        self.assertEqual(fake.tables["expenses"][0]["status"], "Pending")
        self.assertEqual(fake.tables["expenses"][0]["amount"], 6000)
        self.assertEqual(fake.tables["expenses"][0]["ocr_amount"], 5000)

    def test_large_expense_rejects_old_receipt_month(self):
        fake = FakeSupabase({"expenses": []})
        old_date = date.today().replace(day=1) - timedelta(days=1)
        payload = {
            "amount": "600",
            "expense_type": "Food",
            "description": "Team lunch",
            "receipt_filename": "receipt.png",
        }
        ocr_result = SimpleNamespace(
            text="Invoice INV-101\nTotal 600\n",
            amount=600,
            bill_date=old_date,
            invoice_number="INV-101",
            vendor_name="Cafe",
        )

        with (
            patch.object(expense_service, "supabase", fake),
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
        ):
            valid_payload, error = expense_service.validate_expense_payload(payload, perform_ocr=True)

        self.assertIsNone(valid_payload)
        self.assertEqual(error, "Only bills from the current month are eligible for reimbursement.")

    def test_large_expense_rejects_duplicate_invoice(self):
        fake = FakeSupabase({"expenses": [{"id": "99", "invoice_number": "INV-102"}]})
        payload = {
            "amount": "600",
            "expense_type": "Food",
            "description": "Team lunch",
            "receipt_filename": "receipt.png",
        }
        ocr_result = SimpleNamespace(
            text="Invoice INV-102\nTotal 600\n",
            amount=600,
            bill_date=date.today(),
            invoice_number="INV-102",
            vendor_name="Cafe",
        )

        with (
            patch.object(expense_service, "supabase", fake),
            patch.object(expense_service, "run_tesseract", return_value=ocr_result),
        ):
            valid_payload, error = expense_service.validate_expense_payload(payload, perform_ocr=True)

        self.assertIsNone(valid_payload)
        self.assertEqual(error, "This receipt appears to have already been submitted.")

    def test_manager_expense_approval_updates_payroll_once(self):
        salary_month = date.today().strftime("%Y-%m")
        fake = FakeSupabase(
            {
                "expenses": [
                    {
                        "id": "1",
                        "employee_id": "EMP101",
                        "amount": 600,
                        "expense_type": "Travel",
                        "status": "Pending",
                    }
                ],
                "salary_records": [
                    {
                        "id": "sal1",
                        "employee_id": "EMP101",
                        "salary_month": salary_month,
                        "reimbursement": 100,
                        "net_salary": 50100,
                    }
                ],
            }
        )

        with patch.object(manager_expenses, "supabase", fake):
            with self.app.test_request_context("/manager/expenses/1/approve", method="POST"):
                session["role"] = "manager"
                session["employee_name"] = "Manager One"
                manager_expenses.approve_expense_request("1")
                messages = get_flashed_messages(with_categories=True)
                self.assertEqual(messages[-1][0], "success")

            self.assertEqual(fake.tables["expenses"][0]["status"], "Approved")
            self.assertEqual(fake.tables["expenses"][0]["approved_by"], "Manager One")
            self.assertEqual(fake.tables["salary_records"][0]["reimbursement"], 700)
            self.assertEqual(fake.tables["salary_records"][0]["net_salary"], 50700)

    def test_manager_expense_enrichment_flags_amount_mismatch_for_review(self):
        fake = FakeSupabase({"employees": [{"employee_id": "EMP101", "name": "Gaurav Patil"}]})

        with patch.object(manager_expenses, "supabase", fake):
            enriched = manager_expenses.enrich_employee_names(
                [
                    {
                        "employee_id": "EMP101",
                        "amount": 3150,
                        "ocr_amount": 812,
                    }
                ]
            )

        self.assertTrue(enriched[0]["_amount_not_validated"])

    def test_help_message_lists_available_features(self):
        response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "What can you do?")

        reply = response.get_json()["reply"]
        self.assertIn("Attendance", reply)
        self.assertIn("Expense Reimbursements", reply)
        self.assertIn("Generate payslip", reply)

    def test_reimbursement_how_to_explains_process_without_workflow(self):
        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=None),
                patch.object(app_module, "process_hr_request") as process_hr_request,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "How do I submit reimbursement?"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("To submit an expense reimbursement", response.get_json()["reply"])
        process_hr_request.assert_not_called()

    def test_leave_balance_variations_are_consistent(self):
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {"employee_id": "EMP101", "leave_type": "Casual Leave", "remaining_leaves": 4, "used_leaves": 1},
                    {"employee_id": "EMP101", "leave_type": "Privilege Leave", "remaining_leaves": 8, "used_leaves": 0},
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "How many leaves do I have?")

        reply = response.get_json()["reply"]
        self.assertIn("Casual Leave: 4 remaining, 1 used", reply)
        self.assertIn("Privilege Leave: 8 remaining, 0 used", reply)

    def test_leave_and_expense_request_history_filters_status(self):
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": "2026-06-10",
                        "to_date": "2026-06-11",
                        "leave_duration": "Full Day",
                        "status": "Pending",
                    },
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Paternity Leave",
                        "from_date": "2026-05-01",
                        "to_date": "2026-05-05",
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    },
                ],
                "expenses": [
                    {
                        "employee_id": "EMP101",
                        "expense_type": "Food",
                        "amount": 300,
                        "description": "Dinner",
                        "status": "Approved",
                    },
                    {
                        "employee_id": "EMP101",
                        "expense_type": "Travel",
                        "amount": 1200,
                        "description": "Taxi",
                        "status": "Pending",
                    },
                ],
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            leave_response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "show pending leave requests")
            expense_response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "show approved expense claims")

        self.assertIn("Pending: Casual Leave", leave_response.get_json()["reply"])
        self.assertNotIn("Paternity", leave_response.get_json()["reply"])
        self.assertIn("Approved: Food", expense_response.get_json()["reply"])
        self.assertNotIn("Travel", expense_response.get_json()["reply"])

    def test_attendance_history_considers_approved_leave(self):
        target = date.today().replace(day=1)
        fake = FakeSupabase(
            {
                "attendance": [
                    {"employee_id": "EMP101", "date": target.isoformat(), "status": "Present", "punch_in": "09:00:00"},
                ],
                "leave_requests": [
                    {
                        "employee_id": "EMP101",
                        "from_date": (target + timedelta(days=1)).isoformat(),
                        "to_date": (target + timedelta(days=1)).isoformat(),
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    }
                ],
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "show attendance for this month")

        reply = response.get_json()["reply"]
        self.assertIn("Present", reply)
        self.assertIn("Leave", reply)

    def test_hr_summary_combines_employee_records(self):
        fake = FakeSupabase(
            {
                "attendance": [{"employee_id": "EMP101", "date": date.today().isoformat(), "status": "Present"}],
                "leave_requests": [
                    {"employee_id": "EMP101", "from_date": date.today().isoformat(), "to_date": date.today().isoformat(), "leave_duration": "Full Day", "status": "Approved"}
                ],
                "employee_leave_balance": [
                    {"employee_id": "EMP101", "leave_type": "Casual Leave", "remaining_leaves": 4, "used_leaves": 1}
                ],
                "expenses": [{"employee_id": "EMP101", "amount": 1200, "status": "Pending"}],
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 63000}
                ],
            }
        )

        with (
            patch.object(assistant_service, "supabase", fake),
            patch.object(payroll_service, "supabase", fake),
        ):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "Show my HR summary")

        reply = response.get_json()["reply"]
        self.assertIn("Attendance:", reply)
        self.assertIn("Leave Balances:", reply)
        self.assertIn("Pending: INR 1,200.00", reply)
        self.assertIn("Latest Salary: INR 63,000.00", reply)

    def test_recommendation_for_marriage_uses_balances(self):
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {"employee_id": "EMP101", "leave_type": "Marriage Leave", "remaining_leaves": 5, "used_leaves": 0},
                    {"employee_id": "EMP101", "leave_type": "Casual Leave", "remaining_leaves": 4, "used_leaves": 1},
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "I am getting married next month. What leaves can I apply for?")

        reply = response.get_json()["reply"]
        self.assertIn("Marriage Leave", reply)
        self.assertIn("Recommended option", reply)

    def test_data_request_bypasses_active_leave_workflow_and_offers_resume(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {
                "leave_type": "Casual Leave",
                "from_date": (date.today() + timedelta(days=1)).isoformat(),
                "to_date": (date.today() + timedelta(days=1)).isoformat(),
                "duration": "Full Day",
                "reason": "UNKNOWN",
            },
        }
        fake = FakeSupabase(
            {
                "attendance": [{"employee_id": "EMP101", "date": date.today().isoformat(), "status": "Present"}],
                "leave_requests": [],
            }
        )

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(assistant_service, "supabase", fake),
                patch.object(app_module, "process_hr_request") as process_hr_request,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "Show my attendance"})

        reply = response.get_json()["reply"]
        self.assertEqual(response.status_code, 200)
        self.assertIn("attendance from", reply)
        self.assertIn("continue your leave request", reply)
        process_hr_request.assert_not_called()

    def test_continue_resumes_active_leave_workflow_step(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {},
        }

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "continue"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("short reason", response.get_json()["reply"])

    def test_payroll_followup_bypasses_active_leave_workflow(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {},
        }
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "April 2026", "net_salary": 66000},
                ]
            }
        )

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"
                browser_session["last_hr_topic"] = "payroll"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(payroll_service, "supabase", fake),
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "What about April?"})

        reply = response.get_json()["reply"]
        self.assertEqual(response.status_code, 200)
        self.assertIn("your salary for April 2026", reply)
        self.assertIn("continue your leave request", reply)

    def test_named_month_salary_comparison_uses_requested_months(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {
                        "employee_id": "EMP101",
                        "salary_month": "May 2026",
                        "reimbursement": 4500,
                        "deductions": 2000,
                        "net_salary": 67500,
                    },
                    {
                        "employee_id": "EMP101",
                        "salary_month": "June 2026",
                        "reimbursement": 0,
                        "deductions": 2500,
                        "net_salary": 63000,
                    },
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "Compare May and June")

        reply = response.get_json()["reply"]
        self.assertIn("May 2026: INR 67,500.00", reply)
        self.assertIn("June 2026: INR 63,000.00", reply)
        self.assertIn("Difference: INR 4,500.00 decrease", reply)
        self.assertIn("Reimbursement", reply)
        self.assertIn("Deductions", reply)

    def test_attendance_for_named_month_returns_month_summary(self):
        fake = FakeSupabase(
            {
                "attendance": [
                    {"employee_id": "EMP101", "date": "2026-05-03", "status": "Present", "punch_in": "09:00:00"},
                ],
                "leave_requests": [
                    {
                        "employee_id": "EMP101",
                        "from_date": "2026-05-04",
                        "to_date": "2026-05-04",
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    }
                ],
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "Show my attendance for May")

        reply = response.get_json()["reply"]
        self.assertIn("2026-05-01 to 2026-05-31", reply)
        self.assertIn("Present", reply)
        self.assertIn("Leave", reply)

    def test_last_approved_leave_is_direct_retrieval(self):
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {"employee_id": "EMP101", "leave_type": "Casual Leave", "from_date": "2026-04-01", "to_date": "2026-04-01", "leave_duration": "Full Day", "status": "Approved"},
                    {"employee_id": "EMP101", "leave_type": "Paternity Leave", "from_date": "2026-05-01", "to_date": "2026-05-05", "leave_duration": "Full Day", "status": "Approved"},
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "What was my last approved leave?")

        reply = response.get_json()["reply"]
        self.assertIn("Paternity Leave", reply)
        self.assertNotIn("Casual Leave", reply)

    def test_summary_variants_are_first_class_intents(self):
        fake = FakeSupabase(
            {
                "attendance": [{"employee_id": "EMP101", "date": date.today().isoformat(), "status": "Present"}],
                "leave_requests": [],
                "employee_leave_balance": [{"employee_id": "EMP101", "leave_type": "Casual Leave", "remaining_leaves": 4}],
                "expenses": [],
                "salary_records": [{"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 63000}],
            }
        )

        with (
            patch.object(assistant_service, "supabase", fake),
            patch.object(payroll_service, "supabase", fake),
        ):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "summary")

        reply = response.get_json()["reply"]
        self.assertIn("HR Summary -", reply)
        self.assertIn("Recent Activity:", reply)
        self.assertIn("Latest Salary: INR 63,000.00", reply)

    def test_global_cancel_phrase_cancels_active_leave_workflow(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "from_date",
            "payload": {},
        }

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(app_module, "handle_cancel_workflow", return_value=(app_module.jsonify({"reply": "cancelled"}), 200)) as cancel_workflow,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "nothing else thanks"})

        reply = response.get_json()["reply"]
        self.assertEqual(response.status_code, 200)
        self.assertIn("leave request has been cancelled", reply)
        cancel_workflow.assert_called_once_with("EMP101")

    def test_new_expense_intent_during_leave_workflow_offers_switch(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {},
        }

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(app_module, "process_hr_request") as process_hr_request,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "I have a bill to claim"})

        reply = response.get_json()["reply"]
        self.assertEqual(response.status_code, 200)
        self.assertIn("submit an expense claim", reply)
        self.assertIn("switch to reimbursement", reply)
        process_hr_request.assert_not_called()

    def test_switch_confirmation_starts_pending_expense_workflow(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {},
        }

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"
                browser_session["pending_workflow_switch"] = {
                    "target": expense_service.EXPENSE_WORKFLOW,
                    "message": "I spent money on travel",
                }

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(app_module, "handle_cancel_workflow", return_value=(app_module.jsonify({"reply": "cancelled"}), 200)),
                patch.object(app_module, "handle_apply_expense", return_value=(app_module.jsonify({"reply": "expense started"}), 200)) as apply_expense,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "switch to reimbursement"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("expense started", response.get_json()["reply"])
        apply_expense.assert_called_once()

    def test_multi_question_guidance_answers_leave_and_reimbursement(self):
        response, _ = assistant_service.handle_advisory_message(
            "EMP101",
            "Gaurav",
            "How do I apply leave? How does reimbursement work?",
        )

        reply = response.get_json()["reply"]
        self.assertIn("Leave Process:", reply)
        self.assertIn("Reimbursement Process:", reply)

    def test_indirect_expense_start_routes_to_expense_workflow(self):
        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=None),
                patch.object(app_module, "handle_apply_expense", return_value=(app_module.jsonify({"reply": "travel reimbursement started"}), 200)) as apply_expense,
                patch.object(app_module, "process_hr_request") as process_hr_request,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "I spent money on travel"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("travel reimbursement started", response.get_json()["reply"])
        apply_expense.assert_called_once()
        process_hr_request.assert_not_called()

    def test_latest_reimbursement_retrieval_during_leave_workflow_does_not_switch(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {},
        }
        fake = FakeSupabase(
            {
                "expenses": [
                    {
                        "employee_id": "EMP101",
                        "expense_type": "Travel",
                        "amount": 1200,
                        "description": "Taxi",
                        "status": "Approved",
                        "created_at": "2026-06-03",
                    }
                ]
            }
        )

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(assistant_service, "supabase", fake),
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "Show my latest reimbursement"})

        reply = response.get_json()["reply"]
        self.assertEqual(response.status_code, 200)
        self.assertIn("Approved: Travel", reply)
        self.assertIn("continue your leave request", reply)
        self.assertNotIn("switch to reimbursement", reply)

    def test_attendance_comparison_between_months(self):
        fake = FakeSupabase(
            {
                "attendance": [
                    {"employee_id": "EMP101", "date": "2026-05-01", "status": "Present"},
                    {"employee_id": "EMP101", "date": "2026-05-02", "status": "Present"},
                    {"employee_id": "EMP101", "date": "2026-06-01", "status": "Present"},
                ],
                "leave_requests": [],
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "Compare attendance for May and June")

        reply = response.get_json()["reply"]
        self.assertIn("attendance comparison", reply)
        self.assertIn("May 2026", reply)
        self.assertIn("June 2026", reply)
        self.assertIn("Difference", reply)

    def test_latest_attendance_record_is_direct_retrieval(self):
        fake = FakeSupabase(
            {
                "attendance": [
                    {"employee_id": "EMP101", "date": "2026-05-01", "status": "Present", "punch_in": "09:00:00"},
                    {"employee_id": "EMP101", "date": "2026-06-01", "status": "Present", "punch_in": "09:15:00"},
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "Show my latest attendance record")

        reply = response.get_json()["reply"]
        self.assertIn("latest attendance record", reply)
        self.assertIn("2026-06-01", reply)

    def test_reimbursement_comparison_between_named_months(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "May 2026", "reimbursement": 4500, "net_salary": 67500},
                    {"employee_id": "EMP101", "salary_month": "June 2026", "reimbursement": 0, "net_salary": 63000},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "Compare reimbursements between May and June")

        reply = response.get_json()["reply"]
        self.assertIn("reimbursement comparison", reply)
        self.assertIn("May 2026: INR 4,500.00", reply)
        self.assertIn("June 2026: INR 0.00", reply)
        self.assertIn("Difference: INR 4,500.00 decrease", reply)

    def test_latest_payslip_is_direct_retrieval(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 63000},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "Show my latest payslip")

        reply = response.get_json()["reply"]
        self.assertIn("your payslip for June 2026 is ready", reply)
        self.assertIn("Download PDF", reply)

    def test_semantic_cancellation_variations_are_recognized(self):
        phrases = [
            "I changed my mind",
            "I won't be taking leave",
            "Leave it",
            "Forget it",
            "Nothing",
            "No leave",
            "Not now",
            "Maybe later",
            "Thanks, I'm good",
            "That's all",
            "I don't need this anymore",
            "Never mind that",
            "Ignore that",
            "Let's skip it",
            "Not interested",
            "Forget the request",
            "No reimbursement",
            "No expense claim",
            "No thanks",
        ]

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(assistant_service.is_global_cancel_message(phrase))

    def test_advisory_question_during_expense_workflow_answers_without_continuing(self):
        active_expense = {
            "id": "wf-expense-1",
            "workflow_type": expense_service.EXPENSE_WORKFLOW,
            "step": "amount",
            "payload": {},
        }

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_expense),
                patch.object(app_module, "handle_apply_expense") as apply_expense,
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post(
                    "/chat",
                    json={"message": "Should I even claim this? I don't have a bill. Will my manager approve it?"},
                )

        reply = response.get_json()["reply"]
        self.assertEqual(response.status_code, 200)
        self.assertIn("INR 200 or below", reply)
        self.assertIn("manager approval", reply.lower())
        self.assertIn("continue your expense request", reply)
        apply_expense.assert_not_called()

    def test_multi_question_retrieval_answers_attendance_and_salary(self):
        fake = FakeSupabase(
            {
                "attendance": [{"employee_id": "EMP101", "date": date.today().isoformat(), "status": "Present"}],
                "leave_requests": [],
                "salary_records": [
                    {
                        "employee_id": "EMP101",
                        "salary_month": "June 2026",
                        "basic_salary": 50000,
                        "hra": 10000,
                        "allowances": 5000,
                        "deductions": 2000,
                        "net_salary": 63000,
                    }
                ],
            }
        )

        with (
            patch.object(assistant_service, "supabase", fake),
            patch.object(payroll_service, "supabase", fake),
        ):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "Show my attendance and salary")

        reply = response.get_json()["reply"]
        self.assertIn("Attendance:", reply)
        self.assertIn("Payroll:", reply)
        self.assertIn("Net Salary: INR 63,000.00", reply)

    def test_multi_question_leave_balance_and_pending_requests(self):
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {"employee_id": "EMP101", "leave_type": "Casual Leave", "remaining_leaves": 2, "used_leaves": 3}
                ],
                "leave_requests": [
                    {"employee_id": "EMP101", "leave_type": "Casual Leave", "from_date": "2026-06-10", "to_date": "2026-06-10", "leave_duration": "Full Day", "status": "Pending"}
                ],
                "expenses": [],
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "Show my leave balance and pending requests")

        reply = response.get_json()["reply"]
        self.assertIn("Leave Balances:", reply)
        self.assertIn("Requests:", reply)
        self.assertIn("Pending: Casual Leave", reply)

    def test_comparison_intent_during_leave_workflow_uses_payroll_not_switching(self):
        active_leave = {
            "id": "wf-leave-1",
            "workflow_type": intent_handlers.LEAVE_WORKFLOW,
            "step": "reason",
            "payload": {},
        }
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "May 2026", "reimbursement": 4500, "net_salary": 67500},
                    {"employee_id": "EMP101", "salary_month": "June 2026", "reimbursement": 0, "net_salary": 63000},
                ]
            }
        )

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_leave),
                patch.object(payroll_service, "supabase", fake),
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "Compare reimbursements for May and June"})

        reply = response.get_json()["reply"]
        self.assertIn("reimbursement comparison", reply)
        self.assertIn("continue your leave request", reply)
        self.assertNotIn("switch to reimbursement", reply)

    def test_life_event_paternity_due_recognition(self):
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {"employee_id": "EMP101", "leave_type": "Paternity Leave", "remaining_leaves": 10}
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "My wife is due next month")

        reply = response.get_json()["reply"]
        self.assertIn("Paternity Leave", reply)
        self.assertIn("10 days", reply)

    def test_life_event_bereavement_recognition(self):
        fake = FakeSupabase(
            {
                "employee_leave_balance": [
                    {"employee_id": "EMP101", "leave_type": "Bereavement Leave", "remaining_leaves": 5}
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "My father passed away")

        reply = response.get_json()["reply"]
        self.assertIn("sorry", reply.lower())
        self.assertIn("Bereavement Leave", reply)

    def test_forgot_punch_out_gives_correction_guidance(self):
        yesterday = date.today() - timedelta(days=1)
        fake = FakeSupabase(
            {
                "attendance": [
                    {"employee_id": "EMP101", "date": yesterday.isoformat(), "punch_in": "09:00:00", "punch_out": None, "status": "Present"}
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "I forgot to punch out yesterday")

        reply = response.get_json()["reply"]
        self.assertIn("incomplete attendance record", reply)
        self.assertIn("correction", reply)

    def test_recommendation_uses_reimbursement_history(self):
        fake = FakeSupabase(
            {
                "expenses": [
                    {"employee_id": "EMP101", "expense_type": "Travel", "amount": 1000},
                    {"employee_id": "EMP101", "expense_type": "Travel", "amount": 800},
                    {"employee_id": "EMP101", "expense_type": "Food", "amount": 400},
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "What reimbursement categories do I use most often?")

        reply = response.get_json()["reply"]
        self.assertIn("Travel", reply)
        self.assertIn("2 claim", reply)

    def test_hr_summary_includes_insights(self):
        fake = FakeSupabase(
            {
                "attendance": [{"employee_id": "EMP101", "date": date.today().isoformat(), "status": "Present"}],
                "leave_requests": [],
                "employee_leave_balance": [{"employee_id": "EMP101", "leave_type": "Casual Leave", "remaining_leaves": 1}],
                "expenses": [{"employee_id": "EMP101", "expense_type": "Travel", "amount": 1200, "status": "Approved"}],
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "June 2026", "reimbursement": 0, "net_salary": 63000},
                    {"employee_id": "EMP101", "salary_month": "May 2026", "reimbursement": 4500, "net_salary": 67500},
                ],
            }
        )

        with (
            patch.object(assistant_service, "supabase", fake),
            patch.object(payroll_service, "supabase", fake),
        ):
            response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "HR summary")

        reply = response.get_json()["reply"]
        self.assertIn("Insights:", reply)
        self.assertIn("Casual Leave balance is running low", reply)
        self.assertIn("salary decreased", reply)

    def test_adversarial_intent_variations_classify_without_gemini(self):
        leave_variations = [
            "Need leave tomorrow",
            "Need a day off",
            "Won't be coming tomorrow",
            "Taking tomorrow off",
            "Can I be on leave tomorrow?",
            "Not available tomorrow",
            "Need personal leave",
            "Need emergency leave",
            "Need a break tomorrow",
            "I won't make it tomorrow",
            "I'll be absent tomorrow",
            "Need holiday next week",
            "Need 3 days off",
            "Need leave from Monday",
            "Planning vacation",
        ]
        reimbursement_variations = [
            "I have a bill",
            "Need reimbursement",
            "Need to claim expenses",
            "Spent money on travel",
            "Can company reimburse this?",
            "I paid for a client dinner",
            "Bought software for work",
            "Need expense approval",
            "Want to submit a receipt",
            "I have a travel bill",
        ]
        payroll_variations = [
            "Show salary",
            "Payslip",
            "Net pay",
            "Earnings",
            "Salary breakdown",
            "How much did I earn?",
            "Show payroll",
            "Show June salary",
        ]
        history_variations = [
            "Show my requests",
            "Show pending requests",
            "Show approved leaves",
            "Latest reimbursement",
            "Most recent leave",
        ]

        for phrase in leave_variations:
            with self.subTest(leave=phrase):
                self.assertTrue(assistant_service.is_leave_start_message(phrase))
        for phrase in reimbursement_variations:
            with self.subTest(reimbursement=phrase):
                self.assertTrue(assistant_service.is_expense_start_message(phrase))
        for phrase in payroll_variations:
            with self.subTest(payroll=phrase):
                self.assertTrue(payroll_service.is_payroll_message(phrase))
        for phrase in history_variations:
            with self.subTest(history=phrase):
                self.assertTrue(assistant_service.is_history_message(phrase))

    def test_payslip_parts_calculates_salary_breakdown(self):
        parts = payroll_service.payslip_parts(
            {
                "basic_salary": 50000,
                "hra": 10000,
                "allowances": 5000,
                "reimbursement": 4500,
                "deductions": 2000,
                "net_salary": 67500,
            }
        )

        self.assertEqual(parts["total_earnings"], 69500)
        self.assertEqual(parts["total_deductions"], 2000)
        self.assertEqual(parts["net_salary"], 67500)

    def test_payroll_history_uses_only_logged_in_employee_records(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "May 2026", "net_salary": 67500},
                    {"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 68000},
                    {"employee_id": "EMP102", "salary_month": "June 2026", "net_salary": 91000},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "show salary history")

        reply = response.get_json()["reply"]
        self.assertIn("May 2026", reply)
        self.assertIn("June 2026", reply)
        self.assertIn("68,000.00", reply)
        self.assertNotIn("91,000.00", reply)

    def test_all_months_salary_is_treated_as_history(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "April 2026", "net_salary": 66000},
                    {"employee_id": "EMP101", "salary_month": "May 2026", "net_salary": 67500},
                    {"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 63000},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "show my all months salary")

        reply = response.get_json()["reply"]
        self.assertIn("salary history", reply)
        self.assertIn("April 2026: INR 66,000.00", reply)
        self.assertIn("May 2026: INR 67,500.00", reply)
        self.assertIn("June 2026: INR 63,000.00", reply)

    def test_payroll_comparison_reports_difference_and_reasons(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {
                        "employee_id": "EMP101",
                        "salary_month": "May 2026",
                        "basic_salary": 50000,
                        "hra": 10000,
                        "allowances": 5000,
                        "reimbursement": 4500,
                        "deductions": 2000,
                        "net_salary": 67500,
                    },
                    {
                        "employee_id": "EMP101",
                        "salary_month": "June 2026",
                        "basic_salary": 50000,
                        "hra": 10000,
                        "allowances": 5000,
                        "reimbursement": 1000,
                        "deductions": 2500,
                        "net_salary": 63500,
                    },
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "compare this month with last month")

        reply = response.get_json()["reply"]
        self.assertIn("Difference: INR 4,000.00 decrease", reply)
        self.assertIn("Reimbursement", reply)
        self.assertIn("Deductions", reply)

    def test_previous_month_comparison_does_not_fall_back_to_history(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "May 2026", "net_salary": 67500},
                    {"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 63000},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "compare it with previous month salary")

        reply = response.get_json()["reply"]
        self.assertIn("salary comparison", reply)
        self.assertIn("Difference: INR 4,500.00 decrease", reply)
        self.assertNotIn("here is your salary history", reply)

    def test_month_only_misspelled_reimbursement_query_uses_payroll(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "April 2026", "reimbursement": 3500},
                    {"employee_id": "EMP101", "salary_month": "June 2026", "reimbursement": 0},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "show my reimburesements for april month")

        self.assertIn("reimbursements for April 2026 were INR 3,500.00", response.get_json()["reply"])

    def test_download_payslip_reply_uses_encoded_direct_links(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {"employee_id": "EMP101", "salary_month": "June 2026", "net_salary": 63000},
                ]
            }
        )

        with patch.object(payroll_service, "supabase", fake):
            response, _ = payroll_service.handle_payroll_query("EMP101", "Gaurav", "download my payslip")

        reply = response.get_json()["reply"]
        self.assertIn("View: /payslip?month=June%202026", reply)
        self.assertIn("Download PDF: /payslip/download?month=June%202026", reply)

    def test_payroll_message_detection_does_not_steal_expense_claims(self):
        self.assertFalse(payroll_service.is_payroll_message("I want to claim reimbursement for a bill"))
        self.assertTrue(payroll_service.is_payroll_message("How much reimbursement was added to my salary?"))
        self.assertTrue(payroll_service.is_payroll_message("show my reimburesements for april month"))

    def test_payroll_query_bypasses_active_expense_workflow(self):
        active_expense = {
            "id": "wf-expense-1",
            "workflow_type": expense_service.EXPENSE_WORKFLOW,
            "step": "amount",
            "payload": {"amount": "UNKNOWN"},
        }

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"

            with (
                patch.object(app_module, "get_active_workflow", return_value=active_expense),
                patch.object(app_module, "handle_payroll_query", return_value=(app_module.jsonify({"reply": "payroll answer"}), 200)),
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "download my payslip"})

        self.assertEqual(response.status_code, 200)
        reply = response.get_json()["reply"]
        self.assertIn("payroll answer", reply)
        self.assertIn("continue your expense request", reply)

    def test_month_only_payroll_followup_uses_last_payroll_context(self):
        fake = FakeSupabase(
            {
                "salary_records": [
                    {
                        "employee_id": "EMP101",
                        "salary_month": "April 2026",
                        "basic_salary": 50000,
                        "hra": 10000,
                        "allowances": 5000,
                        "reimbursement": 3000,
                        "deductions": 2000,
                        "net_salary": 66000,
                    }
                ]
            }
        )

        with app_module.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["employee_id"] = "EMP101"
                browser_session["employee_name"] = "Gaurav"
                browser_session["role"] = "employee"
                browser_session["last_hr_topic"] = "payroll"

            with (
                patch.object(app_module, "get_active_workflow", return_value=None),
                patch.object(payroll_service, "supabase", fake),
                patch.object(app_module, "log_conversation"),
            ):
                response = client.post("/chat", json={"message": "What about April?"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("your salary for April 2026", response.get_json()["reply"])

    def test_payslip_pdf_bytes_are_generated(self):
        pdf = payroll_service.build_simple_pdf(["Enterprise HR Assistant - Payslip", "Net Salary: INR 67,500.00"])

        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"Net Salary", pdf)

    def test_ocr_tessdata_prefix_resolves_to_tessdata_directory(self):
        diagnostics = ocr_service.ocr_diagnostics()

        self.assertTrue(diagnostics["tessdata_prefix"].endswith("Tesseract-OCR\\tessdata"))
        self.assertTrue(diagnostics["eng_traineddata_exists"])

    def test_ocr_amount_prefers_final_total_over_subtotal(self):
        text = """Sunrise Foods Pvt Ltd
Name: Pooja Iyer Invoice No: INV-2026-0423
Item Price Qty Total
Sub-Total: 25,186.00
CGST: 2.5% 129.65
SGST: 2.5% 129.65
Mode: card Total: =5,445.30
GSTIN: 30XICTI5508S8Z5"""

        self.assertEqual(ocr_service._extract_amount(text), 5445.30)

    def test_ocr_amount_prefers_grand_total_over_other_totals(self):
        text = """Restaurant Bill
Invoice 7767
Item Total 812.00
Total 812.00
Grand Total 3,150.00"""

        self.assertEqual(ocr_service._extract_amount(text), 3150.00)

    def test_ocr_amount_falls_back_to_last_amount_not_largest_number(self):
        text = """Restaurant Bill
Invoice 7767
Service 812.00
3,150.00"""

        self.assertEqual(ocr_service._extract_amount(text), 3150.00)

    def test_stress_dataset_loader_supports_concatenated_arrays(self):
        scenarios = stress_test.load_scenarios(Path(app_module.app.root_path) / "conversation_scenarios.json")

        self.assertEqual(len(scenarios), 120)
        self.assertEqual(scenarios[0]["name"], "leave_interrupt_salary_01")
        self.assertEqual(scenarios[-1]["name"], "ambiguous_03")

    def test_stress_count_parser_supports_all_and_limits(self):
        self.assertEqual(stress_test.parse_count("all", 120), 120)
        self.assertEqual(stress_test.parse_count("25", 120), 25)
        self.assertEqual(stress_test.parse_count("500", 120), 120)

    def test_stress_validator_flags_missing_life_event_recommendation(self):
        scenario = {"name": "copilot_paternity_01", "messages": ["My wife is due next month"]}

        passed, categories, notes, suspicious = stress_test.validate_turn(
            scenario,
            0,
            scenario["messages"][0],
            "Please provide more information.",
            200,
            [],
            None,
        )

        self.assertFalse(passed)
        self.assertIn("copilot_failure", categories)
        self.assertIn("paternity leave", notes)
        self.assertFalse(suspicious)

    def test_stress_inprocess_transport_executes_real_chat_route(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "copilot_paternity_01", "messages": ["My wife is due next month"]})
            response, status = transport.send("My wife is due next month")
        finally:
            transport.close()

        self.assertEqual(status, 200)
        self.assertIn("Paternity Leave", response)

    def test_stress_report_writes_all_output_formats(self):
        records = [
            {
                "scenario_name": "sample",
                "turn": 1,
                "user_message": "Hello",
                "bot_response": "HR response",
                "timestamp": "2026-06-06T00:00:00+00:00",
                "status_code": 200,
                "pass": True,
                "notes": "",
                "failure_categories": [],
                "suspicious": False,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = stress_test.write_results(Path(temporary_directory), records, 1, 10.0)
            contents = [path.read_text(encoding="utf-8-sig") for path in paths]

        self.assertEqual(len(paths), 3)
        self.assertIn("sample", contents[0])
        self.assertIn("scenario_name", contents[1])
        self.assertIn("Scenarios executed: 1", contents[2])

    def test_expense_claim_phrase_starts_workflow_instead_of_fallback(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "expense_switch", "messages": []})
            first_response, first_status = transport.send("I have an expense to claim")
            salary_response, salary_status = transport.send("Show my salary")
            resume_response, resume_status = transport.send("continue")
        finally:
            transport.close()

        self.assertEqual(first_status, 200)
        self.assertIn("amount would you like to claim", first_response.lower())
        self.assertEqual(salary_status, 200)
        self.assertIn("continue your expense request", salary_response.lower())
        self.assertEqual(resume_status, 200)
        self.assertIn("amount would you like to claim", resume_response.lower())

    def test_plain_bill_statement_is_not_misclassified_as_advice(self):
        self.assertIsNone(assistant_service.expense_advice_response("I have a bill to claim"))
        self.assertIn("work-related expense", assistant_service.expense_advice_response("Is this reimbursable"))

    def test_semantic_workflow_start_patterns_allow_intervening_words(self):
        self.assertTrue(assistant_service.is_leave_start_message("I need paternity leave for a week from next Monday"))
        self.assertTrue(assistant_service.is_expense_start_message("I have a food bill"))

    def test_singular_reimbursement_category_recommendation(self):
        fake = FakeSupabase(
            {
                "employee_leave_balance": [],
                "expenses": [
                    {"employee_id": "EMP101", "expense_type": "Travel", "amount": 1000},
                    {"employee_id": "EMP101", "expense_type": "Travel", "amount": 800},
                ],
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message(
                "EMP101",
                "Gaurav",
                "What reimbursement category do I use most",
            )

        self.assertIn("Travel", response.get_json()["reply"])

    def test_leave_history_question_does_not_start_leave_workflow(self):
        message = "Did I take any leave in the month of June?"

        self.assertTrue(assistant_service.is_leave_history_query(message))
        self.assertFalse(assistant_service.is_leave_start_message(message))

    def test_monthly_leave_history_returns_only_approved_requested_month(self):
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {
                        "id": "1",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": "2026-06-08",
                        "to_date": "2026-06-09",
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    },
                    {
                        "id": "2",
                        "employee_id": "EMP101",
                        "leave_type": "Privilege Leave",
                        "from_date": "2026-06-15",
                        "to_date": "2026-06-15",
                        "leave_duration": "Full Day",
                        "status": "Rejected",
                    },
                    {
                        "id": "3",
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": "2026-05-04",
                        "to_date": "2026-05-04",
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    },
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message(
                "EMP101", "Gaurav", "Did I take any leave in June?"
            )

        reply = response.get_json()["reply"]
        self.assertIn("Casual Leave", reply)
        self.assertIn("Total approved leave: 2", reply)
        self.assertNotIn("Privilege Leave", reply)
        self.assertNotIn("2026-05-04", reply)

    def test_local_policy_retrieval_finds_harassment_reporting_policy(self):
        policy_service.local_policy_chunks.cache_clear()

        context = policy_service.retrieve_policy_context(
            "What should I do if my colleague is harassing me?"
        )

        self.assertIn("Code of Conduct and Employee Relations Policy", context)
        self.assertIn("Reporting harassment or misconduct", context)

    def test_copilot_routes_sensitive_and_approval_escalation_questions(self):
        self.assertTrue(policy_service.should_use_copilot("My colleague is harassing me"))
        self.assertTrue(
            policy_service.should_use_copilot(
                "My manager is on leave and my expense is pending. What should I do?"
            )
        )
        self.assertFalse(policy_service.should_use_copilot("Show my salary for June"))

    def test_combined_punch_in_and_leave_request_runs_punch_first(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "punch_and_leave", "messages": []})
            response, status = transport.send("Punch me in today and apply for leave tomorrow")
        finally:
            transport.close()

        self.assertEqual(status, 200)
        self.assertIn("punch in has been recorded", response.lower())
        self.assertIn("which leave type", response.lower())

    def test_attendance_questions_and_actions_override_active_leave_workflow(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "attendance_interrupt", "messages": []})
            transport.send("I need leave tomorrow")
            status_response, status = transport.send("Did you punch me in today?")
            punch_response, punch_status = transport.send("bro punch in")
        finally:
            transport.close()

        self.assertEqual(status, 200)
        self.assertIn("attendance for", status_response.lower())
        self.assertNotIn("valid leave type", status_response.lower())
        self.assertEqual(punch_status, 200)
        self.assertIn("punch in has been recorded", punch_response.lower())
        self.assertIn("continue your leave request", punch_response.lower())

    def test_yesterday_punch_question_is_attendance_retrieval_not_leave_input(self):
        yesterday = date.today() - timedelta(days=1)
        fake = FakeSupabase(
            {
                "attendance": [
                    {
                        "employee_id": "EMP101",
                        "date": yesterday.isoformat(),
                        "punch_in": "09:05:00",
                        "punch_out": "18:03:00",
                        "status": "Present",
                    }
                ]
            }
        )

        with patch.object(assistant_service, "supabase", fake):
            response, _ = assistant_service.handle_advisory_message(
                "EMP101", "Gaurav", "Was I punched in yesterday?"
            )

        reply = response.get_json()["reply"]
        self.assertIn(yesterday.isoformat(), reply)
        self.assertIn("Present", reply)

    def test_leave_history_month_followup_uses_previous_topic(self):
        fake = FakeSupabase(
            {
                "leave_requests": [
                    {
                        "employee_id": "EMP101",
                        "leave_type": "Casual Leave",
                        "from_date": "2026-05-20",
                        "to_date": "2026-05-20",
                        "leave_duration": "Full Day",
                        "status": "Approved",
                    }
                ]
            }
        )

        with self.app.test_request_context("/"):
            session["last_hr_topic"] = "leave_history"
            with patch.object(assistant_service, "supabase", fake):
                response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "in May?")

        self.assertIn("Casual Leave", response.get_json()["reply"])

    def test_month_only_leave_history_followup_wins_over_active_leave_workflow(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "leave_history_followup", "messages": []})
            transport.send("I need leave tomorrow")
            june_response, june_status = transport.send("Did I take any leave in June?")
            may_response, may_status = transport.send("in May?")
        finally:
            transport.close()

        self.assertEqual(june_status, 200)
        self.assertIn("approved leave", june_response.lower())
        self.assertEqual(may_status, 200)
        self.assertIn("May", may_response)
        self.assertNotIn("valid leave type", may_response.lower())
        self.assertIn("continue your leave request", may_response.lower())

    def test_hr_contact_response_never_invents_contact_details(self):
        with patch.object(assistant_service.Config, "HR_CONTACT_EMAIL", None), patch.object(
            assistant_service.Config, "HR_CONTACT_CHANNEL", None
        ):
            response, _ = assistant_service.handle_advisory_message(
                "EMP101", "Gaurav", "How should I contact HR through mail or message?"
            )

        reply = response.get_json()["reply"]
        self.assertIn("do not have verified HR contact details", reply)
        self.assertNotIn("hr@company.com", reply)

    def test_no_without_active_workflow_closes_conversation_naturally(self):
        response, _ = assistant_service.handle_advisory_message("EMP101", "Gaurav", "no nothing thank you")

        self.assertIn("Take care", response.get_json()["reply"])

    def test_conversation_planner_normalises_multiple_actions_and_entities(self):
        plan = conversation_planner.normalise_plan(
            {
                "actions": ["punch_in", "apply_leave", "not_a_real_action"],
                "entities": {"date_reference": "tomorrow", "leave_type": "Casual Leave"},
                "confidence": "high",
            }
        )

        self.assertEqual(plan["actions"], ["PUNCH_IN", "APPLY_LEAVE", "GENERAL_HR_QUERY"])
        self.assertEqual(plan["entities"]["date_reference"], "tomorrow")

    def test_gemini_planner_tool_result_beats_active_leave_workflow_for_typo_attendance_request(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "planner_typo_attendance", "messages": []})
            transport.send("I need leave tomorrow")
            plan = {"actions": ["GET_ATTENDANCE"], "entities": {"date_reference": "yesterday"}}
            with patch.object(app_module, "plan_conversation", return_value=plan), patch.object(
                app_module.Config, "GEMINI_PLANNER_ENABLED", True
            ):
                response, status = transport.send("attandence log yesterdy")
        finally:
            transport.close()

        self.assertEqual(status, 200)
        self.assertIn("attendance for", response.lower())
        self.assertIn("continue your leave request", response.lower())

    def test_gemini_planner_runs_multi_action_without_keyword_patterns(self):
        transport = stress_test.InProcessTransport("EMP101", "QA Employee", "employee")
        try:
            transport.start_scenario({"name": "planner_multi_action", "messages": []})
            plan = {"actions": ["PUNCH_IN", "APPLY_LEAVE"], "entities": {"date_reference": "tomorrow"}}
            with patch.object(app_module, "plan_conversation", return_value=plan), patch.object(
                app_module.Config, "GEMINI_PLANNER_ENABLED", True
            ):
                response, status = transport.send("mark me at office and i need off tomorow")
        finally:
            transport.close()

        self.assertEqual(status, 200)
        self.assertIn("punch in has been recorded", response.lower())
        self.assertIn("which leave type", response.lower())


if __name__ == "__main__":
    unittest.main()

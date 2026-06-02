import os
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, get_flashed_messages, session

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

import intent_handlers
import expense_service
import manager_expenses
import manager_approval
import ocr_service
import payroll_service
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

    def test_large_expense_rejects_amount_mismatch_without_insert(self):
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
        self.assertIn("Claim amount does not match receipt amount.", reply)
        self.assertIn("Claimed Amount: ₹6000", reply)
        self.assertIn("Receipt Amount: ₹5000", reply)
        self.assertEqual(fake.tables["expenses"], [])

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
        self.assertNotIn("salary history", reply)

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
        self.assertEqual(response.get_json()["reply"], "payroll answer")

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


if __name__ == "__main__":
    unittest.main()

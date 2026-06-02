# HR ASSISTANT PROJECT - MASTER HANDOFF

## Purpose

This document is the permanent project specification.

It defines:

* Project vision
* Architecture
* Business rules
* Database expectations
* Long-term roadmap

This file should remain relatively stable.

Recent development history belongs in:

`session_updates.md`

---

# Instructions For Future Codex Sessions

Before making changes:

1. Read the repository.
2. Read `MASTER_HANDOFF.md`.
3. Read `session_updates.md`.
4. Compare repository state against both documents.
5. Explain:

   * What is implemented
   * What is partially implemented
   * What is missing
   * What technical debt exists
6. Provide an implementation plan.
7. Wait for approval before making code changes.

---

# Project Overview

Enterprise HR Assistant built using:

Backend:

* Flask

Database:

* Supabase PostgreSQL

AI:

* Gemini API

Frontend:

* HTML
* CSS
* JavaScript

Future:

* RAG
* Embeddings
* Semantic Retrieval

Goal:

Build an enterprise-grade HR operating assistant that supports:

* Attendance
* Leave Management
* Expense Reimbursements
* Payroll
* Payslips
* Manager Approvals
* HR Policy Retrieval

The assistant should behave like a real HR system, not a toy chatbot.

---

# Core Design Principles

## 1. HR-Only Assistant

Allowed topics:

* Attendance
* Leave
* Approvals
* Payroll
* Payslips
* Reimbursements
* Holidays
* HR Policies
* Employee Information

Not allowed:

* Jokes
* Trivia
* Open-domain questions
* General internet queries

If outside HR scope:

Politely refuse.

---

## 2. Semantic Intent Understanding

The assistant should not rely on exact keywords.

Examples:

All should map to:

PUNCH_IN

Examples:

* I came to office
* Mark attendance
* Start my shift
* Punch me in

Similarly:

APPLY_LEAVE

Examples:

* I need leave tomorrow
* Apply casual leave
* Can I take leave tomorrow

Intent understanding should remain semantic.

---

## 3. Conversational Workflows

Workflows should support:

* Multi-step conversations
* Persistence
* Session recovery
* Context retention

Example:

User:
I need leave tomorrow

Bot:
What leave type?

User:
Casual Leave

Bot:
Reason?

User:
Family Function

Bot:
Please confirm.

---

## 4. Enterprise Validation

Never trust user input.

Always validate:

* Dates
* Leave balances
* Attendance state
* Workflow state
* Duplicate requests
* Approval permissions

The system must never crash due to malformed input.

---

# Current Database Schema

## employees

Purpose:

Authentication and employee context.

Columns:

* employee_id
* name
* email
* password
* role

---

## attendance

Purpose:

Attendance tracking.

Columns:

* employee_id
* date
* punch_in
* punch_out
* status

Rules:

* No duplicate punch-in
* No punch-out before punch-in
* No duplicate punch-out

---

## employee_leave_balance

Purpose:

Leave balance management.

Columns:

* employee_id
* leave_type
* remaining_leaves
* used_leaves

---

## leave_requests

Purpose:

Leave workflow.

Columns:

* employee_id
* leave_type
* from_date
* to_date
* leave_duration
* reason
* status

Status:

* Pending
* Approved
* Rejected

Business Rule:

Leave balance deduction occurs only after manager approval.

---

## expenses

Purpose:

Expense reimbursement workflow.

Columns:

* employee_id
* amount
* description
* bill_image
* ocr_text
* expense_type
* status

Future OCR fields may include:

* bill_number
* vendor_name
* bill_date
* ocr_amount

---

## salary_records

Purpose:

Payroll and payslip generation.

Columns:

* employee_id
* salary_month
* basic_salary
* hra
* allowances
* deductions
* reimbursement
* net_salary

---

## conversations

Purpose:

Audit trail and future memory.

Columns:

* employee_id
* user_message
* bot_response
* created_at

---

## conversation_workflows

Purpose:

Workflow persistence.

Columns:

* employee_id
* workflow_type
* status
* step
* payload
* created_at
* updated_at

---

# Leave Workflow Rules

Leave requests must validate:

* Leave type exists
* Leave balance available
* Dates valid
* No overlaps
* No duplicate requests

If balance is insufficient:

* Reject immediately
* Do not create request
* Do not notify manager

Bot should display available leave balances.

Manager rejection should only happen for:

* Business reasons
* Team capacity
* Policy violations
* Missing documentation

Store rejection reason.

Employees should see rejection reason.

---

# Expense Workflow Rules

Supported categories:

1. Travel
2. Food
3. Accommodation
4. Software / Tools

Expenses ≤ ₹200:

Required:

* Expense type

Optional:

* Description

No receipt required.

Send directly for manager approval.

Expenses > ₹200:

Required:

* Expense type
* Description
* Receipt image

OCR validation required.

---

## OCR Rules

Project contains local Tesseract installation:

tesseract/

Expected OCR extraction:

* Bill amount
* Bill date
* Invoice number
* Vendor name

Validation:

### Amount Match

Claimed amount must equal OCR amount.

If mismatch:

Reject before manager approval.

---

### Current Month Validation

Only bills from current month allowed.

Older receipts rejected.

---

### Duplicate Bill Detection

Store invoice number.

If invoice already exists:

Reject immediately.

---

# Manager Approval Rules

Managers should see:

* Employee Name
* Expense Type
* Description
* Amount
* OCR Amount
* Vendor Name
* Invoice Number
* Bill Date

Managers can:

* Approve
* Reject

Rejection requires a reason.

---

# Payroll Rules

Employees may only view their own payroll.

No cross-employee payroll access.

---

## Payslip Requirements

Display:

Employee Information

Earnings:

* Basic Salary
* HRA
* Allowances
* Reimbursements

Deductions:

* Deductions

Net Salary

---

## PDF Payslip

Generate professional downloadable PDF.

Include:

* Employee information
* Earnings section
* Deductions section
* Reimbursement section
* Net salary

---

## Payroll Questions

Examples:

* What is my salary this month?
* What deductions were applied?
* What reimbursement was added?
* Show my payslip.
* Compare my salary with last month.
* What is my net salary?

Answers must come from salary_records.

---

# Security Requirements

Never hardcode:

* API keys
* Database credentials
* Secrets

Use:

* .env
* config.py

Employees:

* Can view only their own data.

Managers:

* Can only perform authorized approval actions.

---

# Testing Requirements

New features should include tests where practical.

Priority:

* Workflow tests
* Validation tests
* Route tests
* Approval tests

---

# Long-Term Roadmap

High Priority:

* Expense Workflow
* OCR Integration
* Payroll
* Payslip PDF
* Better Workflow Orchestration
* Better Semantic Understanding

Medium Priority:

* Notifications
* Email Alerts
* Dashboard Improvements
* Audit Enhancements

Future:

* RAG-based HR Policy Assistant
* Embeddings
* Vector Search
* Company Policy Retrieval

---

# Session History

Do not store session history in this file.

Use:

session_updates.md

for all ongoing development updates.

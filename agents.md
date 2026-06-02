# AGENTS.md

## Project Rules

Before making changes:

1. Read:

   * MASTER_HANDOFF.md
   * session_updates.md

2. Compare repository state against project documentation.

3. Provide:

   * current state assessment
   * missing features
   * technical debt
   * implementation plan

4. Wait for approval before coding.

---

## Development Rules

* Never hardcode secrets.
* Use .env and config.py.
* Preserve existing functionality when adding features.
* Prefer small, incremental changes.
* Add tests where practical.
* Avoid unnecessary schema changes.
* Explain database impact before modifying schema.

---

## Leave Workflow Rules

* Insufficient balance requests must be rejected before manager approval.
* Display available leave balances when balance is insufficient.
* Leave balance deduction occurs only after manager approval.
* Manager rejection must include a business or policy reason.
* Employees should be able to see rejection reasons.

---

## Expense Workflow Rules

Expense categories:

* Travel
* Food
* Accommodation
* Software / Tools

Expenses <= ₹200:

* Receipt not required.

Expenses > ₹200:

* Receipt required.
* OCR validation required.

OCR validations:

* Amount match
* Current month receipt
* Duplicate invoice detection

Failed OCR validation must prevent manager approval.

---

## Payroll Rules

Employees may only access their own payroll data.

Approved reimbursements contribute to payroll reimbursement totals.

Payslips should support:

* View
* Download
* PDF generation

---

## Session Update Rules

session_updates.md is the project history file.

Rules:

1. Never overwrite previous entries.
2. Append new entries only.
3. Do not update after every prompt.
4. Update when:

   * a feature is completed,
   * a meaningful bug is fixed,
   * a schema/database change occurs,
   * a development session is ending,
   * context limit is becoming low.
5. Keep entries concise and factual.
6. Do not include logs or chain-of-thought.
7. Future sessions should review recent entries before making changes.

---

## End Of Session Requirement

Before ending a significant development session:

1. Generate a structured handoff.
2. Append it to session_updates.md.
3. Include:

   * features implemented
   * bugs fixed
   * files modified
   * schema changes
   * known issues
   * next recommended tasks

This should happen automatically whenever a significant implementation milestone is completed or the session is ending.

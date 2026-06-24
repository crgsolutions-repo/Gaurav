# Expense, Travel, and Approval Policy
Category: Expenses and Travel
Version: 1.0-demo

This demonstration policy reflects the rules configured in the HR Assistant project and requires company review before adoption.

## Reimbursable expenses

Reasonable work-related Travel, Food, Accommodation, and Software or Tools expenses may be submitted for reimbursement. Personal expenses, duplicate claims, altered receipts, and unsupported business expenses are not reimbursable.

Claims of INR 200 or less require an expense type and may include an optional description. A receipt is not required. Claims above INR 200 require an expense type, description, and receipt.

## Receipt validation

Claims above INR 200 use OCR to extract the total, bill date, invoice number, and vendor. The receipt should be from the current month. Duplicate invoice numbers are rejected. If OCR cannot confidently validate the amount, the claim may be flagged for manager amount review rather than represented as automatically validated.

## Manager approval

The approver reviews business purpose, category, amount, receipt, policy compliance, and available budget. Rejected claims require a reason. Only approved reimbursements may flow into payroll.

## Manager unavailable or on leave

An employee should not submit a duplicate claim merely because the assigned manager is unavailable. The system should first check whether an active approval delegation exists. If a valid delegate exists, the claim may be routed to that delegate with an audit record.

If no delegate exists and the request has exceeded the configured service time, HR or the finance operations owner may route it to the manager's manager or another authorized approver. Escalation must be offered, not silently performed. The employee should see who currently owns the approval and whether escalation was requested.

## Business travel

Employees should obtain required pre-approval before booking travel, use reasonable transport and accommodation, retain itemized receipts, and submit claims promptly. Safety concerns during travel take priority over cost optimization.


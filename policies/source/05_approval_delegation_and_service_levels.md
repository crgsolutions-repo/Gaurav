# Approval Delegation and Service Levels Policy
Category: Approvals and Escalations
Version: 1.0-demo

This is a demonstration policy. Delegation limits and financial authority must be configured by the company before use.

## Approval ownership

Leave and expense requests are normally assigned to the employee's configured manager. The application must store the responsible approver and preserve an audit trail when ownership changes.

## Planned manager absence

Before planned leave, a manager may appoint an authorized delegate for leave approvals, expense approvals, or both. A delegation requires a start time, end time, scope, and delegate identity. Delegation does not increase the delegate's financial authority beyond configured limits.

## Escalation

When an assigned approver is unavailable and no valid delegate exists, an employee may request escalation. The system should check the manager hierarchy and route only to an authorized skip-level manager, HR operations owner, or finance approver.

The assistant must explain the current status and ask for confirmation before creating an escalation. It must never claim that a request has been escalated merely because escalation was discussed.

## Service levels

Routine leave and expense approvals should be reviewed within two working days. Urgent leave should be acknowledged as soon as practical. Expense escalation may be offered after two working days or sooner when payroll cutoff or business travel creates a documented urgency.

## Conflicts and sensitive requests

If the assigned manager is the subject of a grievance, harassment report, or conflict of interest, the request must bypass that manager and route to HR, the skip-level manager, or the ethics owner. Sensitive reports must not be included in ordinary manager approval queues.

## Audit requirements

Every delegation or escalation must record the request, employee, previous approver, new approver, reason, timestamp, initiator, and outcome. Duplicate active escalations for the same request are prohibited.


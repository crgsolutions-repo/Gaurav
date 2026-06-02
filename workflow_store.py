from datetime import datetime, timezone

from supabase_client import supabase


ACTIVE_STATUS = "active"
COMPLETED_STATUS = "completed"
CANCELLED_STATUS = "cancelled"


class WorkflowStoreError(RuntimeError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _execute(query):
    try:
        return query.execute()
    except Exception as exc:
        raise WorkflowStoreError(
            "Workflow storage is not ready. Create the conversation_workflows table from schema.sql."
        ) from exc


def get_active_workflow(employee_id, workflow_type=None):
    query = (
        supabase.table("conversation_workflows")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("status", ACTIVE_STATUS)
    )
    if workflow_type:
        query = query.eq("workflow_type", workflow_type)

    response = _execute(query.order("updated_at", desc=True).limit(1))
    return response.data[0] if response.data else None


def upsert_workflow(employee_id, workflow_type, step, payload):
    existing = get_active_workflow(employee_id, workflow_type)
    values = {
        "employee_id": employee_id,
        "workflow_type": workflow_type,
        "status": ACTIVE_STATUS,
        "step": step,
        "payload": payload,
        "updated_at": _now_iso(),
    }

    if existing:
        response = _execute(
            supabase.table("conversation_workflows")
            .update(values)
            .eq("id", existing["id"])
        )
    else:
        values["created_at"] = _now_iso()
        response = _execute(
            supabase.table("conversation_workflows").insert(values)
        )

    return response.data[0] if response.data else values


def finish_workflow(workflow_id, status=COMPLETED_STATUS):
    if status not in {COMPLETED_STATUS, CANCELLED_STATUS}:
        raise ValueError("Workflow status must be completed or cancelled.")

    _execute(
        supabase.table("conversation_workflows")
        .update({"status": status, "updated_at": _now_iso()})
        .eq("id", workflow_id)
    )


def clear_active_workflows(employee_id, status=CANCELLED_STATUS):
    if status not in {COMPLETED_STATUS, CANCELLED_STATUS}:
        raise ValueError("Workflow status must be completed or cancelled.")

    _execute(
        supabase.table("conversation_workflows")
        .update({"status": status, "updated_at": _now_iso()})
        .eq("employee_id", employee_id)
        .eq("status", ACTIVE_STATUS)
    )

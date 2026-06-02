-- Run this in the Supabase SQL editor before using database-backed workflows.

create table if not exists public.conversation_workflows (
    id uuid primary key default gen_random_uuid(),
    employee_id text not null,
    workflow_type text not null,
    status text not null default 'active',
    step text not null,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists one_active_workflow_per_employee_type
on public.conversation_workflows (employee_id, workflow_type)
where status = 'active';

alter table public.leave_requests
add column if not exists reason text;

alter table public.leave_requests
add column if not exists rejection_reason text;

alter table public.leave_requests
add column if not exists rejected_by text;

alter table public.leave_requests
add column if not exists rejected_at timestamptz;

alter table public.expenses
add column if not exists ocr_amount numeric;

alter table public.expenses
add column if not exists bill_date date;

alter table public.expenses
add column if not exists invoice_number text;

alter table public.expenses
add column if not exists vendor_name text;

alter table public.expenses
add column if not exists rejection_reason text;

alter table public.expenses
add column if not exists rejected_by text;

alter table public.expenses
add column if not exists rejected_at timestamptz;

alter table public.expenses
add column if not exists approved_by text;

alter table public.expenses
add column if not exists approved_at timestamptz;

create unique index if not exists unique_expense_invoice_number
on public.expenses (invoice_number)
where invoice_number is not null and invoice_number <> '';

create index if not exists expenses_status_idx
on public.expenses (status);

create index if not exists expenses_employee_id_idx
on public.expenses (employee_id);

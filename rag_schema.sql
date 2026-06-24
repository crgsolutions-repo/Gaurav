-- Paste this entire block into the Supabase SQL editor once.
-- Policy content remains private and is accessed only by the Flask server's service-role client.

create extension if not exists vector with schema extensions;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'hr-policies',
    'hr-policies',
    false,
    26214400,
    array['application/pdf', 'text/plain', 'text/markdown', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
)
on conflict (id) do update set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create table if not exists public.hr_policy_documents (
    id uuid primary key default gen_random_uuid(),
    title text not null,
    category text not null,
    storage_path text,
    source_filename text not null,
    source_hash text not null unique,
    version text not null default '1.0',
    effective_date date,
    review_date date,
    status text not null default 'draft' check (status in ('draft', 'published', 'archived')),
    audience text not null default 'all_employees',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.hr_policy_chunks (
    id uuid primary key default gen_random_uuid(),
    document_id uuid not null references public.hr_policy_documents(id) on delete cascade,
    chunk_index integer not null,
    section_title text,
    content text not null,
    token_count integer,
    source_page integer,
    embedding extensions.vector(768) not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (document_id, chunk_index)
);

create index if not exists hr_policy_documents_status_idx
on public.hr_policy_documents (status, category);

create index if not exists hr_policy_chunks_document_idx
on public.hr_policy_chunks (document_id, chunk_index);

create index if not exists hr_policy_chunks_embedding_hnsw_idx
on public.hr_policy_chunks using hnsw (embedding extensions.vector_cosine_ops);

create or replace function public.match_hr_policy_chunks(
    query_embedding extensions.vector(768),
    match_count integer default 5,
    match_threshold double precision default 0.45
)
returns table (
    chunk_id uuid,
    document_id uuid,
    document_title text,
    category text,
    section_title text,
    content text,
    source_filename text,
    version text,
    similarity double precision
)
language sql
stable
security definer
set search_path = public, extensions
as $$
    select
        c.id,
        d.id,
        d.title,
        d.category,
        c.section_title,
        c.content,
        d.source_filename,
        d.version,
        1 - (c.embedding <=> query_embedding) as similarity
    from public.hr_policy_chunks c
    join public.hr_policy_documents d on d.id = c.document_id
    where d.status = 'published'
      and 1 - (c.embedding <=> query_embedding) >= match_threshold
    order by c.embedding <=> query_embedding
    limit greatest(1, least(match_count, 20));
$$;

alter table public.hr_policy_documents enable row level security;
alter table public.hr_policy_chunks enable row level security;

revoke all on public.hr_policy_documents from anon, authenticated;
revoke all on public.hr_policy_chunks from anon, authenticated;
revoke all on function public.match_hr_policy_chunks(extensions.vector, integer, double precision) from public, anon, authenticated;
grant all on public.hr_policy_documents to service_role;
grant all on public.hr_policy_chunks to service_role;
grant execute on function public.match_hr_policy_chunks(extensions.vector, integer, double precision) to service_role;

alter table public.employees add column if not exists manager_id text;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'employees_manager_id_fkey'
    ) then
        alter table public.employees
        add constraint employees_manager_id_fkey
        foreign key (manager_id) references public.employees(employee_id) on delete set null;
    end if;
end $$;

create table if not exists public.approval_delegations (
    id uuid primary key default gen_random_uuid(),
    manager_id text not null references public.employees(employee_id),
    delegate_id text not null references public.employees(employee_id),
    approval_type text not null default 'all' check (approval_type in ('all', 'leave', 'expense')),
    starts_at timestamptz not null,
    ends_at timestamptz not null,
    reason text,
    active boolean not null default true,
    created_at timestamptz not null default now(),
    check (manager_id <> delegate_id),
    check (ends_at > starts_at)
);

create table if not exists public.approval_escalations (
    id uuid primary key default gen_random_uuid(),
    employee_id text not null references public.employees(employee_id),
    request_type text not null check (request_type in ('leave', 'expense')),
    request_id text not null,
    from_approver_id text references public.employees(employee_id),
    to_approver_id text not null references public.employees(employee_id),
    reason text not null,
    status text not null default 'pending' check (status in ('pending', 'accepted', 'rejected', 'cancelled')),
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    unique (request_type, request_id, status)
);

alter table public.approval_delegations enable row level security;
alter table public.approval_escalations enable row level security;
revoke all on public.approval_delegations from anon, authenticated;
revoke all on public.approval_escalations from anon, authenticated;
grant all on public.approval_delegations to service_role;
grant all on public.approval_escalations to service_role;

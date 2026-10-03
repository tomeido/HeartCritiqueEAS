-- Durable private discovery backlog and immutable capture artifact references.
-- Run before deploying collector changes when using Supabase. Local databases
-- receive this additive schema automatically through services/localdb.py.
create table if not exists public.discovery_queue (
  id uuid primary key default gen_random_uuid(),
  url text not null unique,
  source text not null,
  feed text not null,
  guid text,
  title text,
  summary text,
  priority integer not null default 0,
  status text not null default 'queued'
    check (status in ('queued','capturing','retry','captured','deleted')),
  attempts integer not null default 0,
  cooldown boolean not null default true,
  first_seen timestamptz not null default now(),
  last_attempt_at timestamptz,
  next_attempt_at timestamptz default now(),
  last_error text
);
alter table public.discovery_queue
  add column if not exists cooldown boolean not null default true;
create index if not exists idx_discovery_source_due
  on public.discovery_queue (source, status, next_attempt_at, first_seen);
create index if not exists idx_discovery_source_attempt
  on public.discovery_queue (source, last_attempt_at desc);
alter table public.discovery_queue enable row level security;
drop policy if exists "discovery_queue_svc" on public.discovery_queue;
create policy "discovery_queue_svc" on public.discovery_queue for all
  using (auth.role() = 'service_role');

alter table public.captured_posts
  add column if not exists capture_manifest_path text,
  add column if not exists capture_manifest_sha256 text,
  add column if not exists capture_state text;

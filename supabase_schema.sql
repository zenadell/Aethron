-- Aethron cloud schema — run once in the Supabase SQL editor.
-- The whole backend is: built-in Auth (accounts) + one events table.
-- No file storage: users' template data stays on their own machines.

-- ── telemetry events ────────────────────────────────────────────────
create table if not exists public.events (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  event      text not null,
  props      jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists events_user_time on public.events (user_id, created_at desc);
create index if not exists events_name_time on public.events (event, created_at desc);

-- Row-level security: a user may only INSERT and READ their OWN events.
-- (Analytics/admin reads happen server-side with the service-role key,
-- which bypasses RLS — never ship that key in the desktop app.)
alter table public.events enable row level security;

drop policy if exists events_insert_own on public.events;
create policy events_insert_own on public.events
  for insert to authenticated
  with check (user_id = auth.uid());

drop policy if exists events_select_own on public.events;
create policy events_select_own on public.events
  for select to authenticated
  using (user_id = auth.uid());

-- ── (optional) lightweight profile / plan, for later monetization ───
create table if not exists public.profiles (
  id         uuid primary key references auth.users (id) on delete cascade,
  plan       text not null default 'free',
  created_at timestamptz not null default now()
);
alter table public.profiles enable row level security;

drop policy if exists profiles_self on public.profiles;
create policy profiles_self on public.profiles
  for select to authenticated using (id = auth.uid());

-- auto-create a profile row when a user signs up
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.profiles (id) values (new.id) on conflict do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ─── agent run records (aethron_runlog.py) ─────────────────────────────
-- What agents driving Aethron over MCP did, uploaded from a machine that
-- switched it on (Settings → Agent runs). The app's PUBLIC key may only
-- ADD rows; nobody reads them except the service role (a debugging
-- session holding the service key in its own secrets). Keys and tokens
-- are removed before a record leaves the machine.
create table if not exists public.agent_runs (
  id          bigint generated always as identity primary key,
  received_at timestamptz not null default now(),
  machine     text not null check (length(machine) <= 64),
  caller      text not null check (length(caller) <= 80),
  run         text not null check (length(run) <= 80),
  n           int,
  at          text check (length(at) <= 32),
  tool        text check (length(tool) <= 80),
  project     text check (length(project) <= 80),
  status      text check (length(status) <= 16),
  ms          int,
  record      jsonb not null check (pg_column_size(record) <= 131072)
);
create index if not exists agent_runs_caller_run on public.agent_runs (caller, run, n);
create index if not exists agent_runs_received on public.agent_runs (received_at desc);

alter table public.agent_runs enable row level security;
drop policy if exists agent_runs_insert on public.agent_runs;
create policy agent_runs_insert on public.agent_runs
  for insert to anon, authenticated with check (true);
-- no select/update/delete policy: with RLS on, the public key cannot read,
-- change or remove any row; the service role bypasses RLS to read.
revoke select, update, delete on public.agent_runs from anon, authenticated;

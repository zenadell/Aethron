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

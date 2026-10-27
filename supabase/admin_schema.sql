-- Aethron admin & control — run AFTER supabase_schema.sql. Idempotent:
-- running it again changes nothing that is already in place.
--
-- WHO MAY TOUCH WHAT
--   control        everyone may READ (the app checks it); only the
--                  service role (the edge function) writes.
--   user_controls  a signed-in user may read THEIR OWN row; only the
--                  service role writes.
--   everything else: no policies at all, so the public key can neither
--   read nor write it. The edge function holds the service role.

-- ── the switches every copy of Aethron obeys ─────────────────────────
create table if not exists public.control (
  id                 int primary key default 1 check (id = 1),
  paused             boolean not null default false,
  pause_message      text not null default '',
  banner             text not null default '',
  banner_level       text not null default 'info'
                       check (banner_level in ('info', 'warn', 'critical')),
  ai_mode            text not null default 'byok'
                       check (ai_mode in ('byok', 'managed', 'off')),
  managed_provider   text not null default 'gemini',
  managed_model      text not null default '',
  features           jsonb not null default
                       '{"agent":true,"mcp":true,"migrate":true,"screenshot":true,"publish":true,"signup":true}'::jsonb,
  min_version        text not null default '',
  default_daily_usd  numeric not null default 0.50,
  global_daily_usd   numeric not null default 10,
  prices             jsonb not null default '{}'::jsonb,
  updated_at         timestamptz not null default now(),
  updated_by         text not null default ''
);
insert into public.control (id) values (1) on conflict (id) do nothing;
alter table public.control enable row level security;
drop policy if exists control_read on public.control;
create policy control_read on public.control
  for select to anon, authenticated using (true);
revoke insert, update, delete on public.control from anon, authenticated;

-- ── per-user overrides ──────────────────────────────────────────────
create table if not exists public.user_controls (
  user_id        uuid primary key references auth.users (id) on delete cascade,
  blocked        boolean not null default false,
  block_reason   text not null default '',
  daily_usd      numeric,             -- null = control.default_daily_usd
  ai_mode        text check (ai_mode in ('byok', 'managed', 'off')),
  notice         text not null default '',
  signout_after  timestamptz,
  updated_at     timestamptz not null default now()
);
alter table public.user_controls enable row level security;
drop policy if exists user_controls_self on public.user_controls;
create policy user_controls_self on public.user_controls
  for select to authenticated using (user_id = auth.uid());
revoke insert, update, delete on public.user_controls from anon, authenticated;

-- admin notes stay apart: a user can read their own user_controls row
create table if not exists public.user_notes (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  note       text not null default '',
  updated_at timestamptz not null default now(),
  updated_by text not null default ''
);
alter table public.user_notes enable row level security;

-- ── every installed copy: who, where (country, never the address), what
create table if not exists public.devices (
  machine      text primary key check (length(machine) <= 64),
  user_id      uuid references auth.users (id) on delete cascade,
  email        text not null default '',
  token_hash   text unique,
  first_seen   timestamptz not null default now(),
  last_seen    timestamptz not null default now(),
  version      text not null default '',
  os           text not null default '',
  arch         text not null default '',
  tz           text not null default '',
  ip_hash      text not null default '',
  country      text not null default '',
  region       text not null default '',
  activity     jsonb not null default '{}'::jsonb,
  revoked_at   timestamptz
);
create index if not exists devices_user on public.devices (user_id);
create index if not exists devices_seen on public.devices (last_seen desc);
alter table public.devices enable row level security;

-- ── what the managed AI spent, request by request ───────────────────
create table if not exists public.usage_ledger (
  id             bigint generated always as identity primary key,
  at             timestamptz not null default now(),
  user_id        uuid references auth.users (id) on delete set null,
  machine        text not null default '',
  provider       text not null default '',
  model          text not null default '',
  purpose        text not null default '',
  input_tokens   int not null default 0,
  output_tokens  int not null default 0,
  cached_tokens  int not null default 0,
  cost_usd       numeric not null default 0,
  status         text not null default 'ok',
  ms             int not null default 0,
  estimated      boolean not null default false
);
create index if not exists usage_user_time on public.usage_ledger (user_id, at desc);
create index if not exists usage_time on public.usage_ledger (at desc);
alter table public.usage_ledger enable row level security;

-- ── provider keys and settings only the function may read ───────────
create table if not exists public.secrets (
  name        text primary key check (length(name) <= 80),
  value       text not null,
  updated_at  timestamptz not null default now(),
  updated_by  text not null default ''
);
alter table public.secrets enable row level security;
revoke all on public.secrets from anon, authenticated;

-- ── who runs the admin ──────────────────────────────────────────────
create table if not exists public.admins (
  email     text primary key,
  role      text not null default 'admin' check (role in ('owner', 'admin', 'viewer')),
  added_at  timestamptz not null default now(),
  added_by  text not null default ''
);
alter table public.admins enable row level security;
revoke all on public.admins from anon, authenticated;

-- ── API keys for coding agents (only a hash is kept) ────────────────
create table if not exists public.admin_keys (
  id          uuid primary key default gen_random_uuid(),
  label       text not null default '',
  prefix      text not null,
  hash        text not null unique,
  scopes      text[] not null default '{read}',
  created_at  timestamptz not null default now(),
  created_by  text not null default '',
  last_used   timestamptz,
  revoked_at  timestamptz
);
alter table public.admin_keys enable row level security;
revoke all on public.admin_keys from anon, authenticated;

-- ── every admin action, by a person, a key or the admin AI ──────────
create table if not exists public.audit_log (
  id       bigint generated always as identity primary key,
  at       timestamptz not null default now(),
  actor    text not null default '',
  via      text not null default 'ui',
  action   text not null,
  target   text not null default '',
  details  jsonb not null default '{}'::jsonb
);
create index if not exists audit_time on public.audit_log (at desc);
alter table public.audit_log enable row level security;
revoke all on public.audit_log from anon, authenticated;

-- ── the admin AI's conversations ────────────────────────────────────
create table if not exists public.admin_ai_messages (
  id       bigint generated always as identity primary key,
  thread   text not null,
  at       timestamptz not null default now(),
  actor    text not null default '',
  role     text not null,
  content  jsonb not null
);
create index if not exists admin_ai_thread on public.admin_ai_messages (thread, id);
alter table public.admin_ai_messages enable row level security;
revoke all on public.admin_ai_messages from anon, authenticated;

-- ── announcements: a card every copy shows once, with an illustration ──
create table if not exists public.announcements (
  id          text primary key,
  title       text not null,
  body        text not null default '',
  level       text not null default 'info' check (level in ('info', 'warn', 'critical')),
  art         text not null default '',
  art_source  text not null default 'none',
  cta_label   text not null default '',
  cta_url     text not null default '',
  created_by  text not null default '',
  created_at  timestamptz not null default now()
);
alter table public.announcements enable row level security;
revoke all on public.announcements from anon, authenticated;
alter table public.control add column if not exists announcement_id text not null default '';

-- the users list reads plans from profiles; keep updated_at for the admin
alter table public.profiles add column if not exists updated_at timestamptz;

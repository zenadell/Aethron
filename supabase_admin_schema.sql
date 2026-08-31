-- Aethron Admin — durable content state (run once in Supabase SQL Editor).
-- Holds the editable copy_map so a stateless host (Render free) never
-- loses edits when its disk is wiped. Only the admin server (service_role,
-- server-side) touches this — RLS is enabled with NO policies, so anon and
-- signed-in users get zero access; service_role bypasses RLS.

create table if not exists public.site_content (
  project    text primary key,
  copy_map   jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.site_content enable row level security;
-- (no policies on purpose → locked to service_role only)

-- keep updated_at fresh on every upsert
create or replace function public.touch_site_content()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_touch_site_content on public.site_content;
create trigger trg_touch_site_content
  before update on public.site_content
  for each row execute function public.touch_site_content();

-- The Storage bucket "site-assets" (private) is created automatically by
-- the admin on boot; no SQL needed. If you'd rather make it here:
--   insert into storage.buckets (id, name, public)
--   values ('site-assets','site-assets', false)
--   on conflict (id) do nothing;

# Cloud setup — accounts + telemetry

Aethron ships as a **local desktop tool**: users' template data never leaves
their machine. The cloud layer adds only two things — **login** (so every user
has an account) and **privacy-respecting telemetry** (so you can see usage and
failures and keep improving the product). It is **dormant by default**: with no
configuration, the studio never shows a login screen and sends nothing.

Because no files are stored server-side, the backend is tiny — Supabase's **free
tier** is plenty.

## What telemetry sends (and never sends)

**Sends:** event names (`project_created`, `run_step`, `heal`, `login`), the
platform (`framer`/`webflow`), small counts (pages, healed, stuck), durations,
and short error categories.

**Never sends:** the user's copy, brand names, images, links, URLs, or any
template content. The allow-list is enforced in code (`aethron_cloud._sanitize`),
not by policy — anything not on the list is dropped before it can leave.

## 1. Create the Supabase project

1. Sign up at supabase.com and create a new project (free tier).
2. In **SQL Editor**, paste and run [`supabase_schema.sql`](../supabase_schema.sql).
   It creates the `events` table with row-level security (each user can only
   read/write their own rows) and an optional `profiles` table with a `plan`
   column for later monetization.
3. In **Project Settings → API**, copy:
   - the **Project URL** (`https://<ref>.supabase.co`)
   - the **anon / public** key (safe to ship in the desktop app — RLS protects
     the data; never ship the *service_role* key)
4. **Authentication → Providers**: keep Email on. For a smooth desktop signup,
   you may turn *off* "Confirm email" during beta (users get a session
   immediately); leave it on for production and they confirm by email first.

## 2. Point the app at it

Set two environment variables (the packaged desktop build bakes these in):

```bash
export AETHRON_SUPABASE_URL="https://<ref>.supabase.co"
export AETHRON_SUPABASE_ANON_KEY="<anon-key>"
python3 studio.py
```

Now the studio requires sign-in, sessions are held server-side (the Supabase
token never reaches the browser), and telemetry flows to your `events` table.

## 3. Test it locally without any Supabase project

```bash
export AETHRON_CLOUD_DEBUG="$PWD/events.jsonl"   # dry-run
python3 studio.py
```

The login gate turns on, **any** email/password is accepted, and every event is
written to `events.jsonl` instead of the network — so you can see exactly what
would be sent before you ever create a project.

## 4. Reading your telemetry

In the Supabase dashboard, query the `events` table, or build a small internal
dashboard against it with the **service_role** key (server-side only — it
bypasses RLS so you can read across all users). Example:

```sql
select event, props->>'platform' as platform, count(*)
from events where created_at > now() - interval '7 days'
group by 1, 2 order by 3 desc;
```

## Notes

- **Local/offline is untouched.** With neither env var set, none of this
  activates — the desktop app works fully offline, no account needed. (You'll
  likely require login in the shipped build; that's a one-line policy choice.)
- **The anon key is meant to be public.** Security comes from row-level security
  + the fact that the app only ever inserts the signed-in user's own events.
- Auth sessions live in memory, so a studio restart signs users out — fine for a
  desktop app the user relaunches and logs into.

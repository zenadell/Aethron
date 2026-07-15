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

## 1b. Add "Continue with Google" (the branded consent screen)

The "**Aethron wants to use your Google Account**" screen is free — it comes
from Google Cloud's OAuth consent screen, and Supabase just forwards to it.

1. **Google Cloud Console** (console.cloud.google.com) → create/select a project.
2. **APIs & Services → OAuth consent screen**: choose *External*, set the **App
   name** to `Aethron`, upload a logo, set your support email. (For beta you can
   stay in "Testing" with your own test users; publish later. Basic
   email/profile scopes don't need heavy verification.)
3. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Web application**. Under **Authorized redirect URIs** add your Supabase
   callback: `https://<ref>.supabase.co/auth/v1/callback`. Copy the **Client ID**
   and **Client secret**.
4. **Supabase → Authentication → Providers → Google**: enable it, paste the
   Client ID + secret, save.
5. **Supabase → Authentication → URL Configuration → Redirect URLs**: add
   `http://127.0.0.1:8899/auth/callback` (the desktop app's local callback; keep
   it on port 8899). Add other ports/domains if you ever change them.

That's it — the login page's "Continue with Google" button now shows the
branded consent screen and drops the user straight into the app.

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

## Beta → paid: locking out free users

During beta, leave billing enforcement **off** — every signed-in account gets
full access. When you're ready to charge:

1. Set `AETHRON_ENFORCE_BILLING=1` on the app (env var) and restart.
2. From that moment, only accounts whose `profiles.plan` is `pro` or `studio`
   can use the app. **Every free account that was testing during the beta is
   locked out on its next login**, shown "Your free beta access has ended —
   upgrade to Pro." (Active sessions are re-checked too.)
3. To grant access, set a user's plan: `update profiles set plan='pro' where
   id='<user-uuid>';` — or, once Stripe is wired, flip it automatically on a
   successful payment webhook.

Test it locally with the dry-run: `AETHRON_CLOUD_DEBUG=… AETHRON_ENFORCE_BILLING=1
AETHRON_CLOUD_DEBUG_PLAN=free python3 studio.py` → login is blocked;
set `AETHRON_CLOUD_DEBUG_PLAN=pro` → login succeeds.

## Notes

- **Local/offline is untouched.** With neither env var set, none of this
  activates — the desktop app works fully offline, no account needed. (You'll
  likely require login in the shipped build; that's a one-line policy choice.)
- **The anon key is meant to be public.** Security comes from row-level security
  + the fact that the app only ever inserts the signed-in user's own events.
- Auth sessions live in memory, so a studio restart signs users out — fine for a
  desktop app the user relaunches and logs into.

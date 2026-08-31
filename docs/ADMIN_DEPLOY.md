# Aethron Admin — deploy the always-on console

The admin (`aethron_admin.py` + `admin_ui/`) is a single stdlib service:

- **Content** — edit team/CEO images, names, roles → `copy_map` → `forge
  build` → publish to Netlify.
- **Operations** — users, signups, telemetry, one-click plan changes.

Content edits live in **Supabase** (a `site_content` table + a `site-assets`
Storage bucket), so a stateless free host that wipes its disk never loses
them. A fresh instance rebuilds the exact latest state from Supabase on boot.

---

## 1. Supabase (one time)

1. **SQL Editor** → run [`supabase_admin_schema.sql`](../supabase_admin_schema.sql).
   Creates `site_content` with RLS locked to `service_role`.
2. **Settings → API Keys** → reveal the **`service_role`** secret and copy it.
   (Bypasses row-level security — server-side only, never in a browser or git.)
3. The `site-assets` Storage bucket is **created automatically** by the admin
   on first boot — nothing to do.

## 2. Verify it locally before hosting

Create `aethron_admin_config.json` (gitignored) from the example:

```json
{
  "admin_emails": "you@jomiez.com",
  "supabase_service_role": "eyJ...service_role...",
  "netlify_token": "nfp_...",
  "netlify_site_id": "incandescent-longma-2957f0"
}
```

(The Supabase **URL + anon key** come from `aethron_config.json`, already set.)

Round-trip check — never sends the key anywhere, cleans up after itself:

```bash
python3 aethron_admin.py --selftest
#   table   write/read : PASS
#   storage put/get    : PASS
#   SELFTEST: ALL PASS — Supabase durable state is ready
```

Then run it for real and log in at http://127.0.0.1:8790 :

```bash
python3 aethron_admin.py
```

## 3. Put the deploy files in the (private) repo

The host needs the read-only template files. `projects/` is gitignored, so
force-add just what the admin needs (content isn't secret; the repo is
private, and this doubles as a durable backup):

```bash
git add -f projects/aethron-site/pristine \
           projects/aethron-site/forge.json \
           projects/aethron-site/copy_map.json
git add aethron_admin.py admin_ui aethron_admin_config.example.json \
        supabase_admin_schema.sql render.admin.yaml docs/ADMIN_DEPLOY.md
git commit -m "Aethron Admin console + Supabase durable state"
git push
```

Uploaded images and copy_map edits do **not** need committing — they live in
Supabase.

## 4. Deploy on Render (free)

**New +→ Web Service → connect your private repo**, then:

| Field | Value |
|---|---|
| Runtime | Python 3 |
| Build command | `pip install fonttools brotli || true` |
| Start command | `python3 aethron_admin.py` |
| Instance type | Free |

**Environment → add these (mark as secret):**

```
AETHRON_SUPABASE_URL       https://bnmiretrggdnoaskasxj.supabase.co
AETHRON_SUPABASE_ANON_KEY  eyJ...anon...
AETHRON_SERVICE_ROLE       eyJ...service_role...
AETHRON_ADMIN_EMAILS       you@jomiez.com
AETHRON_NETLIFY_TOKEN      nfp_...
AETHRON_NETLIFY_SITE       incandescent-longma-2957f0
AETHRON_LIVE_URL           https://aethron.jomiez.com
```

Render injects `$PORT` automatically — the admin binds to it, no port var
needed. Deploy; the boot log should end with `state : Supabase (durable)`.

## 5. Keep it awake (kills the free-tier cold start)

Render free sleeps after 15 min idle. Add a free uptime ping:

- **UptimeRobot** → New monitor → HTTP(s) → your Render URL → every 5–10 min.
- (or **cron-job.org**, same idea.)

Keeping one service awake 24/7 uses most of Render's monthly free allowance,
so make this your single always-on free service.

## 6. Custom domain — `admin.jomiez.com`

1. Render → your service → **Settings → Custom Domains → Add** `admin.jomiez.com`.
   Render shows a target hostname (`something.onrender.com`).
2. Hostinger DNS → add a **CNAME**: name `admin`, value = that
   `*.onrender.com` host. Render auto-issues HTTPS once it resolves.

## 7. Netlify token (for Publish)

Netlify → **User settings → Applications → Personal access tokens → New** →
copy into `AETHRON_NETLIFY_TOKEN`. Site ID is `incandescent-longma-2957f0`
(Netlify → Site configuration → Site ID). Without these, editing still works
but the **Publish** button is disabled.

---

### Security recap
- `service_role` + Netlify token live **only** as server env vars. Never
  shipped to a browser, never committed.
- Login uses Supabase auth **and** an email allow-list — a valid Aethron
  account that isn't in `AETHRON_ADMIN_EMAILS` is refused.
- The admin talks to Supabase/Netlify server-side; the browser only ever
  reaches the admin's own API.

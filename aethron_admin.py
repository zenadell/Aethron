#!/usr/bin/env python3
"""Aethron Admin — the unified, always-on control center.

Two faces behind a single admin login (allow-listed emails only):

  • CONTENT  — a structured editor for the marketing site (team/CEO
    images, names, roles, hero, pricing copy). Every change flows
    through copy_map.json + `forge build` (all invariants hold), then
    one click deploys the rebuilt site/ to Netlify.

  • OPS      — a business dashboard reading Supabase: signups, active
    users, usage/failure telemetry, plan breakdown, and one-click
    "upgrade this user to Pro".

SECURITY MODEL (enforced here, not by policy):
  - The Supabase SERVICE_ROLE key, the Netlify token, and the admin
    allow-list live ONLY on this server. The browser talks solely to
    this admin's own API; no privileged key is ever sent to a client.
  - Login uses Supabase auth, but a valid Aethron account is NOT
    enough — the email must be in ADMIN_EMAILS or the session is
    refused. This is the owner's private console, not a user feature.

Single stdlib file, same zero-dependency ethos as forge/studio. It
imports forge (the build engine) and aethron_cloud (auth) as libraries.

Config: env vars first, then aethron_admin_config.json (gitignored).
Run:    python3 aethron_admin.py         # → http://127.0.0.1:8790
"""
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_cloud as cloud  # noqa: E402  (SUPABASE_URL, ANON_KEY, login)


# ─────────────────────────── config ──────────────────────────────────

def _admin_cfg():
    """env FIRST, then aethron_admin_config.json alongside this file /
    AETHRON_HOME. Missing values just disable the feature that needs
    them (e.g. no Netlify token → publish is disabled, editing still
    works)."""
    cfg = {}
    for base in (ROOT, Path(os.environ.get("AETHRON_HOME", "."))):
        f = base / "aethron_admin_config.json"
        if f.is_file():
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
                break
            except (ValueError, OSError):
                pass

    def pick(env, key, default=""):
        return os.environ.get(env) or cfg.get(key) or default

    admins = pick("AETHRON_ADMIN_EMAILS", "admin_emails")
    if isinstance(admins, str):
        admins = [e.strip().lower() for e in admins.split(",") if e.strip()]
    return {
        "service_role": pick("AETHRON_SERVICE_ROLE", "supabase_service_role"),
        "admin_emails": set(admins or []),
        "project_dir": pick("AETHRON_ADMIN_PROJECT", "project_dir",
                            str(ROOT / "projects" / "aethron-site")),
        "netlify_token": pick("AETHRON_NETLIFY_TOKEN", "netlify_token"),
        "netlify_site": pick("AETHRON_NETLIFY_SITE", "netlify_site_id"),
        "live_url": pick("AETHRON_LIVE_URL", "live_url",
                         "https://aethron.jomiez.com"),
        # $PORT is the PaaS standard (Render/Railway/Fly inject it); fall
        # back to our own var, then the config file, then a local default.
        "port": int(os.environ.get("PORT") or os.environ.get("AETHRON_ADMIN_PORT")
                    or cfg.get("port") or 8790),
    }


CFG = _admin_cfg()
PROJECT = Path(CFG["project_dir"])
PROJECT_NAME = PROJECT.name
FROZEN = getattr(sys, "frozen", False)
SESSIONS = {}   # opaque cookie token -> {email, uid, token}

# When a service_role key is present, the EDITABLE state (copy_map +
# uploaded images) lives in Supabase, not on local disk — so a stateless
# host (Render free) that wipes its disk on redeploy/recycle never loses
# edits. pristine/ + forge ride along read-only in the deploy image; the
# built site/ is regenerated on boot and on publish.
SUPA_STATE = bool(cloud.SUPABASE_URL and CFG["service_role"])
BUCKET = "site-assets"


def forge_argv(*args):
    if FROZEN:
        return [sys.executable, "--forge", *map(str, args)]
    return [sys.executable, str(ROOT / "forge.py"), *map(str, args)]


# ───────────────────── Supabase (service_role) ───────────────────────
# All privileged reads/writes happen here, server-side. service_role
# bypasses RLS, so it must NEVER leave this process.

def _sb(path, method="GET", body=None, service=True):
    key = CFG["service_role"] if service else cloud.ANON_KEY
    if not cloud.SUPABASE_URL or not key:
        raise RuntimeError("Supabase not configured")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(cloud.SUPABASE_URL + path, data=data,
                                 method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    if method in ("PATCH", "POST"):
        req.add_header("Prefer", "return=representation")
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else []


def ops_overview():
    """Business snapshot. Degrades gracefully if service_role is unset."""
    out = {"configured": bool(CFG["service_role"]),
           "users": [], "events": [], "plan_counts": {},
           "signups_7d": 0, "total_users": 0, "error_categories": {}}
    if not out["configured"]:
        return out
    try:
        users = _sb("/auth/v1/admin/users?per_page=200")
        users = users.get("users", users) if isinstance(users, dict) else users
    except Exception as e:
        out["error"] = f"user list failed: {e}"
        users = []
    # plans from profiles
    plans = {}
    try:
        for row in _sb("/rest/v1/profiles?select=id,plan"):
            plans[row.get("id")] = row.get("plan") or "free"
    except Exception:
        pass
    now = time.time()
    slim = []
    for u in users:
        uid = u.get("id")
        created = u.get("created_at", "")
        plan = plans.get(uid, "free")
        out["plan_counts"][plan] = out["plan_counts"].get(plan, 0) + 1
        # signups in last 7 days (created_at is ISO)
        try:
            t = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
            if now - t < 7 * 86400:
                out["signups_7d"] += 1
        except Exception:
            pass
        slim.append({"id": uid, "email": u.get("email", ""),
                     "created": created[:10], "plan": plan,
                     "last_sign_in": (u.get("last_sign_in_at") or "")[:10]})
    slim.sort(key=lambda x: x["created"], reverse=True)
    out["users"] = slim
    out["total_users"] = len(slim)
    # recent telemetry
    try:
        events = _sb("/rest/v1/events?select=event,props,created_at"
                     "&order=created_at.desc&limit=100")
        out["events"] = events
        for e in events:
            cat = (e.get("props") or {}).get("error_category")
            if cat:
                out["error_categories"][cat] = \
                    out["error_categories"].get(cat, 0) + 1
    except Exception:
        pass
    return out


def set_user_plan(uid, plan):
    if plan not in ("free", "pro", "studio"):
        raise ValueError("plan must be free/pro/studio")
    # upsert into profiles
    return _sb(f"/rest/v1/profiles?id=eq.{uid}", "PATCH", {"plan": plan})


# ─────────────── Supabase-backed durable state (stateless-host safe) ──

def _sb_raw(url, method="GET", data=None, headers=None, timeout=40):
    req = urllib.request.Request(url, data=data, method=method)
    k = CFG["service_role"]
    req.add_header("apikey", k)
    req.add_header("Authorization", "Bearer " + k)
    for h, v in (headers or {}).items():
        req.add_header(h, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _content_get():
    """copy_map from the site_content table, or None if not stored yet."""
    try:
        _, body = _sb_raw(cloud.SUPABASE_URL +
            f"/rest/v1/site_content?project=eq.{PROJECT_NAME}&select=copy_map")
        rows = json.loads(body or b"[]")
        return rows[0]["copy_map"] if rows else None
    except Exception:
        return None


def _content_put(cm):
    _sb_raw(cloud.SUPABASE_URL + "/rest/v1/site_content", "POST",
            json.dumps({"project": PROJECT_NAME, "copy_map": cm}).encode(),
            {"Content-Type": "application/json",
             "Prefer": "resolution=merge-duplicates"})


def _bucket_ensure():
    try:
        _sb_raw(cloud.SUPABASE_URL + "/storage/v1/bucket", "POST",
                json.dumps({"id": BUCKET, "name": BUCKET,
                            "public": False}).encode(),
                {"Content-Type": "application/json"})
    except urllib.error.HTTPError:
        pass  # already exists


_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "webp": "image/webp", "avif": "image/avif", "gif": "image/gif",
         "svg": "image/svg+xml"}


def _mime(name):
    return _MIME.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def _storage_put(name, raw, ctype):
    _sb_raw(f"{cloud.SUPABASE_URL}/storage/v1/object/{BUCKET}/"
            f"{PROJECT_NAME}/{name}", "POST", raw,
            {"Content-Type": ctype, "x-upsert": "true"})


def _storage_list():
    try:
        _, b = _sb_raw(f"{cloud.SUPABASE_URL}/storage/v1/object/list/{BUCKET}",
                       "POST",
                       json.dumps({"prefix": PROJECT_NAME + "/",
                                   "limit": 1000}).encode(),
                       {"Content-Type": "application/json"})
        return json.loads(b or b"[]")
    except Exception:
        return []


def _storage_get(name):
    _, b = _sb_raw(f"{cloud.SUPABASE_URL}/storage/v1/object/{BUCKET}/"
                   f"{PROJECT_NAME}/{name}")
    return b


def hydrate_from_supabase():
    """Make local disk reflect Supabase truth. Called on boot and before
    every publish, so a fresh (wiped) instance rebuilds the exact latest
    state from durable storage."""
    if not SUPA_STATE:
        return
    cm = _content_get()
    if cm is not None:
        (PROJECT / "copy_map.json").write_text(
            json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")
    adir = PROJECT / "assets"
    adir.mkdir(parents=True, exist_ok=True)
    for obj in _storage_list():
        nm = obj.get("name")
        if not nm or obj.get("id") is None:   # skip folder placeholders
            continue
        try:
            (adir / nm).write_bytes(_storage_get(nm))
        except Exception:
            pass


# ──────────────────────── content: the site ──────────────────────────

def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _copy_map():
    if SUPA_STATE:
        cm = _content_get()
        if cm is None:                       # first run — seed from repo copy
            cm = json.loads(_read(PROJECT / "copy_map.json") or "{}")
            _content_put(cm)
        (PROJECT / "copy_map.json").write_text(   # mirror for build
            json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")
        return cm
    return json.loads(_read(PROJECT / "copy_map.json") or "{}")


def _forge_json():
    return json.loads(_read(PROJECT / "forge.json") or "{}")


def _localized_inverse():
    """served path (/assets/r/<basename>) -> original CDN url, so an
    <img src> in the built page can be mapped back to its copy_map
    image entry (whose key is the original url)."""
    inv = {}
    for url, f in (_forge_json().get("localized") or {}).items():
        inv["/assets/r/" + Path(f).name] = url
    return inv


IMG_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"[^>]*>', re.I)
# team-name is a PREFIX of team-name-area (the empty wrapper) — require a
# quote or whitespace right after so we hit the text element, not the wrap.
NAME_RE = re.compile(r'class="team-name["\s][^>]*>([^<]*)<', re.I)
DESG_RE = re.compile(r'class="team-designation["\s][^>]*>([^<]*)<', re.I)


def parse_team():
    """Structured team/CEO model from the built about page. Returns
    editable cards: name, role, current image src, and the resolved
    copy_map keys so edits route through the pipeline."""
    html = _read(PROJECT / "site" / "about.html")
    if not html:
        return {"page": "about", "cards": [], "note": "about.html not built"}
    import html as _html
    inv = _localized_inverse()
    cards = []
    # split on team-card boundaries (regex-scan, stdlib only)
    for chunk in re.split(r'class="team-card', html)[1:]:
        block = chunk[:4000]
        nm, dg = NAME_RE.search(block), DESG_RE.search(block)
        name = _html.unescape(nm.group(1)).strip() if nm else ""
        desg = _html.unescape(dg.group(1)).strip() if dg else ""
        img = IMG_RE.search(block)
        src = img.group(1) if img else ""
        cards.append({
            "kind": "team",
            "name": name, "role": desg, "src": src,
            "orig_url": inv.get(src, src),
        })
    # founder image (single hero portrait)
    founder = re.search(r'class="founder-image[^"]*"[^>]*\bsrc="([^"]+)"'
                        r'|<img\b[^>]*class="founder-image[^"]*"[^>]*>', html, re.I)
    fsrc = ""
    if founder:
        m = IMG_RE.search(html[max(0, founder.start()):founder.start() + 500])
        fsrc = founder.group(1) if founder.lastindex else (m.group(1) if m else "")
    if fsrc:
        cards.insert(0, {"kind": "founder", "name": "Founder portrait",
                         "role": "", "src": fsrc,
                         "orig_url": inv.get(fsrc, fsrc)})
    return {"page": "about", "cards": cards,
            "live": CFG["live_url"].rstrip("/") + "/about"}


def _find_string_entry(cm, current):
    """the copy_map string entry currently rendering `current` (its
    'new' if filled, else 'old')."""
    for e in cm.get("strings", []):
        eff = e.get("new") or e.get("old")
        if eff == current:
            return e
    return None


def set_text(current, new_text):
    """Change a rendered string. Guardrails (byte budget, backticks)
    are enforced by forge at build; we do the obvious checks here too."""
    if "`" in new_text or "${" in new_text:
        raise ValueError("text may not contain ` or ${")
    cm = _copy_map()
    e = _find_string_entry(cm, current)
    if not e:
        raise ValueError(f"couldn't locate the text {current!r} to edit")
    mb = e.get("max_bytes")
    if mb and len(new_text.encode("utf-8")) > mb:
        raise ValueError(f"too long: {len(new_text.encode())}/{mb} bytes")
    e["new"] = new_text
    _write_copy_map(cm)
    return True


def set_image(orig_url, asset_path):
    """Point an image entry (keyed by its original url) at a new local
    asset. `forge build` expands srcset variants; `forge heal` mops up
    any mangled/mismatched pick, so this is safe even if the pick is
    imperfect."""
    cm = _copy_map()
    hit = None
    for e in cm.get("images", []):
        if orig_url and orig_url in (e.get("old") or ""):
            hit = e
            break
    if not hit:  # create the entry so the change is tracked
        hit = {"old": orig_url, "new": ""}
        cm.setdefault("images", []).append(hit)
    hit["new"] = asset_path
    _write_copy_map(cm)
    return True


def _write_copy_map(cm):
    (PROJECT / "copy_map.json").write_text(
        json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")
    if SUPA_STATE:
        _content_put(cm)                     # durable source of truth


def save_asset(filename, raw):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', filename) or "upload"
    dest = PROJECT / "assets"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / safe).write_bytes(raw)
    if SUPA_STATE:
        _storage_put(safe, raw, _mime(safe))  # durable copy in Storage
    return "/assets/" + safe


# ─────────────────── build + publish to Netlify ──────────────────────

def build_project(heal=True):
    r = subprocess.run(forge_argv("build"), cwd=str(PROJECT),
                       capture_output=True, text=True)
    log = r.stdout + r.stderr
    if heal:
        h = subprocess.run(forge_argv("heal"), cwd=str(PROJECT),
                           capture_output=True, text=True)
        log += "\n--- heal ---\n" + h.stdout + h.stderr
        # heal may adopt/repair; rebuild to bake it in
        subprocess.run(forge_argv("build"), cwd=str(PROJECT),
                       capture_output=True, text=True)
    return r.returncode == 0, log


def _zip_site():
    site = PROJECT / "site"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in site.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(site).as_posix())
    return buf.getvalue()


def netlify_deploy():
    """Deploy site/ as a zip to Netlify. Returns (ok, message)."""
    tok, sid = CFG["netlify_token"], CFG["netlify_site"]
    if not tok or not sid:
        return False, "Netlify not configured (token/site id missing)"
    zdata = _zip_site()
    req = urllib.request.Request(
        f"https://api.netlify.com/api/v1/sites/{sid}/deploys",
        data=zdata, method="POST")
    req.add_header("Content-Type", "application/zip")
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
        return True, resp.get("deploy_ssl_url") or resp.get("ssl_url") \
            or CFG["live_url"]
    except urllib.error.HTTPError as e:
        return False, f"Netlify {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)


def publish():
    hydrate_from_supabase()   # ensure disk == durable truth before build
    ok, log = build_project()
    if not ok:
        return {"ok": False, "stage": "build", "log": log[-1500:]}
    dok, msg = netlify_deploy()
    return {"ok": dok, "stage": "deploy", "message": msg, "log": log[-800:]}


# ─────────────────────────── HTTP layer ──────────────────────────────

def _tmpl(path):
    return (ROOT / "admin_ui" / path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # -- helpers ------------------------------------------------------
    def _user(self):
        c = self.headers.get("Cookie", "")
        m = re.search(r"admin_sess=([A-Za-z0-9_-]+)", c)
        return SESSIONS.get(m.group(1)) if m else None

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    # -- routing ------------------------------------------------------
    def do_GET(self):
        p = self.path.split("?")[0]
        u = self._user()
        if p == "/":
            page = "admin.html" if u else "login.html"
            return self._send(200, _read(_tmpl(page)), "text/html; charset=utf-8")
        if p == "/api/me":
            return self._send(200, {"email": u["email"]} if u else {})
        if p.startswith("/api/") and not u:
            return self._send(401, {"error": "auth required"})
        if p == "/api/site/team":
            return self._send(200, parse_team())
        if p == "/api/ops/overview":
            return self._send(200, ops_overview())
        if p == "/api/config":
            return self._send(200, {
                "netlify": bool(CFG["netlify_token"] and CFG["netlify_site"]),
                "service_role": bool(CFG["service_role"]),
                "live_url": CFG["live_url"],
                "project": str(PROJECT)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        # login is the only unauthenticated POST
        if p == "/api/login":
            b = self._json_body()
            email = (b.get("email") or "").strip().lower()
            try:
                res = cloud.login(email, b.get("password") or "")
                usr = cloud.user_of(res)
            except Exception as e:
                return self._send(401, {"error": str(e)})
            if email not in CFG["admin_emails"]:
                return self._send(403, {"error":
                    "this account is not an admin of this console"})
            tok = secrets.token_urlsafe(24)
            SESSIONS[tok] = {"email": email, "uid": usr["id"],
                             "token": usr["token"]}
            return self._send(200, {"email": email}, extra=[
                ("Set-Cookie",
                 f"admin_sess={tok}; HttpOnly; SameSite=Lax; Path=/")])
        if p == "/api/logout":
            c = self.headers.get("Cookie", "")
            m = re.search(r"admin_sess=([A-Za-z0-9_-]+)", c)
            if m:
                SESSIONS.pop(m.group(1), None)
            return self._send(200, {"ok": True})

        u = self._user()
        if not u:
            return self._send(401, {"error": "auth required"})

        if p == "/api/site/text":
            b = self._json_body()
            try:
                set_text(b["current"], b["new"])
                return self._send(200, {"ok": True})
            except (KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})

        if p == "/api/site/image":
            # multipart: filename + orig_url + file bytes (base64 in JSON
            # to stay single-file / no multipart parser)
            b = self._json_body()
            try:
                import base64
                raw = base64.b64decode(b["data_b64"])
                asset = save_asset(b.get("filename", "upload"), raw)
                set_image(b.get("orig_url", ""), asset)
                return self._send(200, {"ok": True, "asset": asset})
            except (KeyError, ValueError, Exception) as e:
                return self._send(400, {"error": str(e)})

        if p == "/api/publish":
            return self._send(200, publish())

        if p == "/api/ops/plan":
            b = self._json_body()
            try:
                set_user_plan(b["uid"], b["plan"])
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(400, {"error": str(e)})

        return self._send(404, {"error": "not found"})


def selftest():
    """Verify the Supabase durable-state round-trip end to end. Uses an
    isolated key/prefix so it never touches real content, and cleans up
    after itself. Run on the box that holds the config:
        python3 aethron_admin.py --selftest"""
    if not SUPA_STATE:
        print("Supabase state OFF — need Supabase URL + service_role key.")
        print("(Editing still works locally; edits just won't survive a "
              "stateless host's redeploy without this.)")
        return
    key = PROJECT_NAME + "__selftest"
    ok = True
    try:  # table write → read → delete
        _sb_raw(cloud.SUPABASE_URL + "/rest/v1/site_content", "POST",
                json.dumps({"project": key, "copy_map": {"ping": 1}}).encode(),
                {"Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates"})
        _, b = _sb_raw(cloud.SUPABASE_URL +
            f"/rest/v1/site_content?project=eq.{key}&select=copy_map")
        rows = json.loads(b or b"[]")
        good = bool(rows) and rows[0]["copy_map"].get("ping") == 1
        print(f"  table   write/read : {'PASS' if good else 'FAIL'}")
        ok &= good
        _sb_raw(cloud.SUPABASE_URL +
                f"/rest/v1/site_content?project=eq.{key}", "DELETE")
    except Exception as e:
        print(f"  table   FAIL: {e}"); ok = False
    try:  # storage put → get → delete
        _bucket_ensure()
        _sb_raw(f"{cloud.SUPABASE_URL}/storage/v1/object/{BUCKET}/{key}/ping.txt",
                "POST", b"hello",
                {"Content-Type": "text/plain", "x-upsert": "true"})
        _, b = _sb_raw(
            f"{cloud.SUPABASE_URL}/storage/v1/object/{BUCKET}/{key}/ping.txt")
        print(f"  storage put/get    : {'PASS' if b == b'hello' else 'FAIL'}")
        ok &= (b == b"hello")
        _sb_raw(f"{cloud.SUPABASE_URL}/storage/v1/object/{BUCKET}/{key}/ping.txt",
                "DELETE")
    except Exception as e:
        print(f"  storage FAIL: {e}"); ok = False
    print("SELFTEST:", "ALL PASS — Supabase durable state is ready"
          if ok else "SOME FAILED — check the schema ran + bucket perms")


def main():
    port = CFG["port"]
    if SUPA_STATE:                 # fresh/wiped instance: rebuild from truth
        try:
            _bucket_ensure()
            hydrate_from_supabase()
            build_project(heal=False)
        except Exception as e:
            print(f"boot hydrate/build warning: {e}")
    print(f"Aethron Admin → http://0.0.0.0:{port}/")
    print(f"  project : {PROJECT}")
    print(f"  admins  : {sorted(CFG['admin_emails']) or '(none set — no one can log in!)'}")
    print(f"  netlify : {'configured' if CFG['netlify_token'] else 'not set'}")
    print(f"  state   : {'Supabase (durable)' if SUPA_STATE else 'local disk'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()

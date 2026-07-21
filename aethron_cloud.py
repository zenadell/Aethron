"""Aethron cloud layer — account auth + privacy-respecting telemetry.

DORMANT BY DEFAULT. Offline/local use and the test battery run with no
cloud configured: every function here is a no-op and the studio never
gates on login. It activates only when both env vars are set (the
packaged desktop build ships them, or a developer exports them):

    AETHRON_SUPABASE_URL       https://<ref>.supabase.co
    AETHRON_SUPABASE_ANON_KEY  the project's anon/public key

For local testing without a real project, set AETHRON_CLOUD_DEBUG to a
file path — auth is stubbed and telemetry is written there as JSONL, so
the whole flow is exercisable with zero infrastructure.

PRIVACY (enforced here, not by policy): telemetry carries USAGE + ERRORS
only — event name, platform (framer/webflow), small counts, durations,
and short error categories. It NEVER carries the user's template
content, copy, images, links, or brand. Migration data stays entirely
on the user's machine. `_sanitize` is a strict allow-list; anything not
on it is dropped.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_config():
    """Config resolution, env FIRST (dev/override), then a bundled
    aethron_config.json (how the packaged desktop app ships its keys —
    users have no env vars). Search: alongside the frozen bundle, the
    module dir, then AETHRON_HOME. Missing/garbage file = dormant."""
    cfg = {}
    here = Path(getattr(sys, "_MEIPASS", "")) if getattr(
        sys, "frozen", False) else Path(__file__).resolve().parent
    for base in (here, Path(__file__).resolve().parent,
                 Path(os.environ.get("AETHRON_HOME", "."))):
        f = base / "aethron_config.json"
        if f.is_file():
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
                break
            except (ValueError, OSError):
                pass

    def pick(env, key):
        return os.environ.get(env) or cfg.get(key) or ""
    return (pick("AETHRON_SUPABASE_URL", "supabase_url").rstrip("/"),
            pick("AETHRON_SUPABASE_ANON_KEY", "supabase_anon_key"),
            os.environ.get("AETHRON_CLOUD_DEBUG", "") or cfg.get("debug_log", ""),
            os.environ.get("AETHRON_ENFORCE_BILLING")
            or cfg.get("enforce_billing"))


SUPABASE_URL, ANON_KEY, DEBUG_LOG, _ENFORCE = _load_config()

# REAL  = a live Supabase project (network auth + telemetry).
# DRY   = AETHRON_CLOUD_DEBUG only: gate is on, auth accepts any creds,
#         telemetry goes to the JSONL file — the whole flow, zero infra.
# ENABLED gates the studio on login. Default (no env) = fully dormant:
#         no gate, no network, local/desktop-offline use is untouched.
REAL = bool(SUPABASE_URL and ANON_KEY)
DRY = bool(DEBUG_LOG and not REAL)
ENABLED = bool(REAL or DRY)

# BILLING ENFORCEMENT (the beta -> paid switch).
# During beta, leave AETHRON_ENFORCE_BILLING unset: every signed-in
# account gets full access regardless of plan. When you're ready to
# charge, set AETHRON_ENFORCE_BILLING=1 and restart — from that moment
# only paid plans (pro/studio) get in, and every 'free' account that
# was using it during the beta is locked out on its next login, shown
# an upgrade prompt. Wiring Stripe later just means flipping a user's
# profiles.plan to 'pro' on successful payment.
ENFORCE_BILLING = bool(_ENFORCE)
PAID_PLANS = {"pro", "studio"}


def entitled(plan: str) -> bool:
    return (not ENFORCE_BILLING) or (plan in PAID_PLANS)


def plan_of(token: str, user_id: str) -> str:
    """The signed-in user's plan (free/pro/studio) from the profiles
    table. Fails safe to 'free' so a lookup error never grants access."""
    if DRY:
        return os.environ.get("AETHRON_CLOUD_DEBUG_PLAN", "free")
    if not REAL:
        return "free"
    try:
        req = urllib.request.Request(
            SUPABASE_URL + f"/rest/v1/profiles?id=eq.{user_id}&select=plan",
            method="GET")
        req.add_header("apikey", ANON_KEY)
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read().decode())
        return (rows[0].get("plan") if rows else "free") or "free"
    except Exception:
        return "free"

# telemetry keys that are safe to leave the machine. Everything else is
# dropped. Values are coerced to bounded scalars — no free-form content.
_ALLOWED = {
    "platform": str, "ok": bool, "count": int, "pages": int,
    "chunks": int, "strings": int, "images": int, "links": int,
    "healed": int, "stuck": int, "dropped": int, "ms": int,
    "step": str, "source": str, "error_category": str,
    "app_version": str, "os": str,
}


def _sanitize(props: dict) -> dict:
    out = {}
    for k, typ in _ALLOWED.items():
        if k not in props or props[k] is None:
            continue
        v = props[k]
        try:
            if typ is bool:
                out[k] = bool(v)
            elif typ is int:
                out[k] = int(v)
            else:  # str — bounded, single line, no content leakage
                out[k] = str(v).replace("\n", " ")[:80]
        except (ValueError, TypeError):
            continue
    return out


def _post(path: str, body: dict, token: str = "", timeout: int = 8):
    """POST JSON to Supabase. Raises urllib errors on failure."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(SUPABASE_URL + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("apikey", ANON_KEY)
    req.add_header("Authorization", "Bearer " + (token or ANON_KEY))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


# ─────────────────────────── auth ────────────────────────────────────
# Supabase GoTrue REST. The studio holds the returned access_token in a
# server-side session keyed by an opaque cookie; the token never reaches
# the browser.

def _auth(path: str, email: str, password: str) -> dict:
    if DRY:                            # dry-run: accept any credentials
        return {"access_token": "debug-token",
                "user": {"id": "debug-user", "email": email}}
    if not REAL:
        raise RuntimeError("cloud auth is not configured")
    try:
        return _post(path, {"email": email, "password": password})
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        try:
            detail = json.loads(detail).get("msg") or \
                json.loads(detail).get("error_description") or detail
        except ValueError:
            pass
        raise RuntimeError(detail or f"auth failed ({e.code})")


def signup(email: str, password: str) -> dict:
    return _auth("/auth/v1/signup", email, password)


def login(email: str, password: str) -> dict:
    return _auth("/auth/v1/token?grant_type=password", email, password)


def user_of(auth_result: dict):
    u = auth_result.get("user") or {}
    return {"id": u.get("id"), "email": u.get("email"),
            "token": auth_result.get("access_token", "")}


# ── Google (or any provider) sign-in ────────────────────────────────
# The studio redirects the browser to Supabase's authorize endpoint;
# Supabase bounces through Google and back to our localhost callback
# with the session. The branded "Aethron wants to use your Google
# Account" screen is configured (free) in Google Cloud's OAuth consent
# screen — Supabase just forwards to it.

def oauth_url(provider: str, redirect_to: str):
    """Supabase authorize URL, or None in dry/dormant mode."""
    if not REAL:
        return None
    import urllib.parse
    q = urllib.parse.urlencode({"provider": provider,
                                "redirect_to": redirect_to})
    return f"{SUPABASE_URL}/auth/v1/authorize?{q}"


def user_from_token(access_token: str) -> dict:
    """Resolve an access token to {id,email,token} (post-OAuth)."""
    if DRY:
        return {"id": "debug-user", "email": "google@dry.local",
                "token": access_token or "debug-token"}
    if not REAL:
        raise RuntimeError("cloud auth is not configured")
    req = urllib.request.Request(SUPABASE_URL + "/auth/v1/user", method="GET")
    req.add_header("apikey", ANON_KEY)
    req.add_header("Authorization", "Bearer " + access_token)
    with urllib.request.urlopen(req, timeout=8) as r:
        u = json.loads(r.read().decode())
    return {"id": u.get("id"), "email": u.get("email"), "token": access_token}


# ───────────────────────── telemetry ─────────────────────────────────
# Fire-and-forget: never blocks a request, never raises into the app.

def track(event: str, token: str = "", **props):
    rec = {"event": str(event)[:60], "ts": round(time.time(), 3),
           "props": _sanitize(props)}
    if DEBUG_LOG:
        try:
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass
    if not REAL:
        return

    def _send():
        try:
            _post("/rest/v1/events",
                  {"event": rec["event"], "props": rec["props"]},
                  token=token, timeout=6)
        except Exception:
            pass       # telemetry must never break the app
    threading.Thread(target=_send, daemon=True).start()

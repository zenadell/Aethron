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
import threading
import time
import urllib.error
import urllib.request

SUPABASE_URL = os.environ.get("AETHRON_SUPABASE_URL", "").rstrip("/")
ANON_KEY = os.environ.get("AETHRON_SUPABASE_ANON_KEY", "")
DEBUG_LOG = os.environ.get("AETHRON_CLOUD_DEBUG", "")  # JSONL path (dry-run)

# REAL  = a live Supabase project (network auth + telemetry).
# DRY   = AETHRON_CLOUD_DEBUG only: gate is on, auth accepts any creds,
#         telemetry goes to the JSONL file — the whole flow, zero infra.
# ENABLED gates the studio on login. Default (no env) = fully dormant:
#         no gate, no network, local/desktop-offline use is untouched.
REAL = bool(SUPABASE_URL and ANON_KEY)
DRY = bool(DEBUG_LOG and not REAL)
ENABLED = bool(REAL or DRY)

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

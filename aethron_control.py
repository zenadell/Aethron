#!/usr/bin/env python3
"""What the Aethron team's switches say, for this copy of the app.

The admin (supabase/functions/aethron) holds the switches: a kill switch,
a banner, features on/off, who powers the AI (people's own keys, or
Aethron's managed key), per-person holds, allowances and sign-outs, and a
minimum version. Every copy checks in about once a minute (`ping`) and
keeps the answer in <AETHRON_HOME>/control.json, so EVERY process — the
studio, the agent's bridge, the MCP server an outside agent drives — obeys
the same answer, and an answer from a minute ago still holds when the
network does not.

`why(action)` is the one question: "" when the action may run, else the
sentence to show. Actions: agent, ai, mcp, migrate, screenshot, publish.

HONEST LIMITS, said here so nobody assumes more:
  * with people's own keys (byok), stopping a copy is the copy obeying;
    a person who cuts it off from the network keeps the last answer they
    saw. With the managed AI the refusal is ALSO made on the server,
    which nobody can switch off from their own machine.
  * a copy that has never checked in has no answer, and is not stopped.

Dormant when the cloud is not configured (no Supabase address): every
answer is "go", nothing is sent anywhere.
"""
import json
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STALE_S = 120          # older than this, a process that can refresh does
TIMEOUT_S = 6
# A full check-in (/ping) is an edge-function call, and the free plan has a
# monthly allowance of those. Between full check-ins a copy only asks the
# control table when the team last changed a switch: a plain database read,
# which is not a function call. A changed switch still reaches every copy
# within a minute; who is online and per-person holds within FULL_S.
FULL_S = 600

LABELS = {"agent": "The coding agent", "ai": "Aethron's AI", "mcp": "Aethron's MCP tools",
          "migrate": "Migrating and building", "screenshot": "Screenshot to code",
          "publish": "Publishing"}


def home() -> Path:
    return Path(os.environ.get("AETHRON_HOME", ROOT)).resolve()


def state_file() -> Path:
    return home() / "control.json"


def base_url() -> str:
    """The admin function's address, or "" when the cloud is not set up."""
    if os.environ.get("AETHRON_CONTROL_URL"):
        return os.environ["AETHRON_CONTROL_URL"].rstrip("/")
    try:
        sys.path.insert(0, str(ROOT))
        import aethron_cloud as cloud
        if cloud.SUPABASE_URL and cloud.ANON_KEY:
            return cloud.SUPABASE_URL.rstrip("/") + "/functions/v1/aethron"
    except Exception:
        pass
    return ""


def _anon() -> str:
    try:
        import aethron_cloud as cloud
        return cloud.ANON_KEY or ""
    except Exception:
        return ""


def machine_id() -> str:
    try:
        import aethron_runlog
        return aethron_runlog.machine_id()
    except Exception:
        return "m-unknown0"


def version() -> str:
    try:
        import aethron_update
        return aethron_update.VERSION
    except Exception:
        return ""


# ── the answer on disk ──────────────────────────────────────────────

_MEM = {"mtime": None, "data": {}}
_LOCK = threading.Lock()


def state() -> dict:
    """The last answer, read once per change of the file."""
    f = state_file()
    try:
        m = f.stat().st_mtime
    except OSError:
        return {}
    if _MEM["mtime"] == m:
        return _MEM["data"]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    _MEM.update(mtime=m, data=data)
    return data


def _save(data: dict) -> None:
    f = state_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)          # it holds this copy's device token
        tmp.replace(f)
        _MEM.update(mtime=None)
    except OSError:
        pass


def device_token() -> str:
    return str(state().get("device_token") or "")


# ── talking to the admin ────────────────────────────────────────────

def _request(method: str, path: str, body=None, token: str = "", timeout: float = 0) -> tuple:
    url = base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    anon = _anon()
    if anon:
        req.add_header("apikey", anon)
    req.add_header("Authorization", "Bearer " + (token or anon))
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def ping(token: str = "", activity: dict = None) -> dict:
    """Check in. `token` is a person's access token, used only until this
    copy holds a device token of its own. Returns the saved state, with
    `error` set when the admin could not be reached (the old answer stays)."""
    if not base_url():
        return {}
    with _LOCK:
        old = dict(state())
        dev = str(old.get("device_token") or "")
        body = {"machine": machine_id(), "version": version(),
                "os": f"{platform.system()} {platform.release()}"[:60],
                "arch": platform.machine()[:20], "tz": time.strftime("%Z"),
                "activity": activity or {}, "rotate": not dev}
        try:
            code, j = _request("POST", "/ping", body, dev or token)
            # a device token that was revoked: try once more as the person
            # (an access token from before an admin's sign-out is refused too)
            if code == 401 and dev and token:
                code, j = _request("POST", "/ping", body, token)
        except Exception as e:                       # offline: keep the last answer
            old["error"] = str(e)[:200]
            old["error_at"] = time.time()
            _save(old)
            return old
        new = dict(old)
        new.pop("error", None)
        new["at"] = time.time()
        if j.get("control"):
            new["control"] = j["control"]
        if code == 401 and j.get("signed_out"):
            new.pop("device_token", None)
            new["me"] = {**(old.get("me") or {}), "signed_out": True}
        elif code == 200:
            if j.get("device_token"):
                new["device_token"] = j["device_token"]
            if "me" in j:
                new["me"] = j["me"]
            elif not token and not dev:
                new.pop("me", None)
        else:
            new["error"] = f"admin answered {code}: {str(j)[:160]}"
        _save(new)
        return new


def _rest_url() -> str:
    """The database's own address, when the function's address is the usual one."""
    b = base_url()
    tail = "/functions/v1/aethron"
    return b[: -len(tail)] + "/rest/v1" if b.endswith(tail) and _anon() else ""


def switches_changed() -> bool | None:
    """Did the team change a switch since our last full answer? One database
    read, no function call. None when it cannot be asked."""
    rest = _rest_url()
    if not rest:
        return None
    req = urllib.request.Request(rest + "/control?select=updated_at&id=eq.1")
    req.add_header("apikey", _anon())
    req.add_header("Authorization", "Bearer " + _anon())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            rows = json.loads(r.read().decode() or "[]")
    except Exception:
        return None
    now = str((rows[0] if rows else {}).get("updated_at") or "")
    had = str((state().get("control") or {}).get("updated_at") or "")
    s = dict(state())
    s["looked_at"] = time.time()
    _save(s)
    return _same_time(now, had) is False


def _same_time(a: str, b: str) -> bool | None:
    """Two timestamps from the database, written in slightly different forms."""
    if not a or not b:
        return False
    def norm(t):
        t = t.replace("Z", "+00:00").replace(" ", "T")
        try:
            from datetime import datetime
            return datetime.fromisoformat(t).timestamp()
        except ValueError:
            return t
    return norm(a) == norm(b)


def check(token: str = "", activity: dict = None, full_every: float = FULL_S) -> dict:
    """What a heartbeat does: a full check-in when one is due or a switch
    changed, otherwise only the cheap question."""
    s = state()
    if not base_url():
        return s
    if time.time() - float(s.get("at") or 0) >= full_every or not s.get("control"):
        return ping(token, activity)
    changed = switches_changed()
    if changed is None or changed:
        return ping(token, activity)
    return state()


def refresh(max_age: float = STALE_S, token: str = "") -> dict:
    """Ask again only if the answer on disk is older than max_age."""
    s = state()
    seen = max(float(s.get("at") or 0), float(s.get("looked_at") or 0))
    if not base_url() or time.time() - seen <= max_age:
        return s
    # offline: do not make every call wait out a timeout; try again in a minute
    if time.time() - float(s.get("error_at") or 0) < 60:
        return s
    return check(token)


def forget_device() -> None:
    """Signed out on this machine: drop the token and the person's state."""
    s = dict(state())
    s.pop("device_token", None)
    s.pop("me", None)
    _save(s)


# ── the one question ────────────────────────────────────────────────

def control() -> dict:
    return state().get("control") or {}


def _vtuple(v: str) -> tuple:
    out = []
    for part in str(v or "").strip().split("."):
        try:
            out.append(int(part))
        except ValueError:
            return ()
    return tuple(out)


def needs_update() -> bool:
    """Decided HERE, from the version that is actually running. The admin's
    answer was computed for whatever version last checked in, so after an
    update it stays "too old" until the next ping; this copy knows better."""
    need, have = _vtuple(control().get("min_version")), _vtuple(version())
    if not need or not have:
        return bool((state().get("me") or {}).get("update_required"))
    n = max(len(need), len(have))
    return have + (0,) * (n - len(have)) < need + (0,) * (n - len(need))


def me() -> dict:
    m = dict(state().get("me") or {})
    m["update_required"] = needs_update()
    return m


def ai_mode() -> str:
    m = me().get("ai_mode") or control().get("ai_mode") or "byok"
    return m if m in ("byok", "managed", "off") else "byok"


def why(action: str = "any") -> str:
    """"" when `action` may run; else what to tell the person."""
    if not base_url():
        return ""
    c, m = control(), me()
    if c.get("paused"):
        return c.get("pause_message") or "Aethron is paused by its team for a moment. Please try again later."
    if m.get("signed_out"):
        return "You were signed out of Aethron. Sign in again to continue."
    if m.get("blocked"):
        r = m.get("block_reason") or ""
        return "Your Aethron account is on hold" + (f": {r}" if r else ".")
    if needs_update():
        return (f"This version of Aethron is too old (the team needs {c.get('min_version')} or newer). "
                "Update from the top bar to continue.")
    feats = c.get("features") or {}
    if action in feats and feats.get(action) is False:
        return f"{LABELS.get(action, action)} is switched off by the Aethron team for now."
    if action in ("ai", "agent") and ai_mode() == "off":
        return "Aethron's AI is switched off by the team for now."
    return ""


def managed() -> dict:
    """The managed AI's endpoint for the brain, or {} when people use their
    own keys. The device token is the key: the admin's real key never
    leaves the admin function."""
    if not base_url() or ai_mode() != "managed":
        return {}
    tok = device_token()
    return {"base_url": base_url() + "/v1", "api_key": tok,
            "model": control().get("managed_model") or "",
            "ready": bool(tok),
            "why": "" if tok else "sign in to Aethron to use its AI"}


def announcement_art(aid: str) -> bytes:
    """The live announcement's illustration: fetched once by id, kept in
    <home>/announcements/, and only ever an SVG picture. b"" when there is
    none or it cannot be fetched (the card then shows without a picture)."""
    import re
    if not re.fullmatch(r"an_[A-Za-z0-9_-]{6,40}", aid or "") or not base_url():
        return b""
    p = home() / "announcements" / (aid + ".svg")
    if p.is_file():
        return p.read_bytes()
    req = urllib.request.Request(base_url() + "/announcement/art?id=" + aid)
    anon = _anon()
    if anon:
        req.add_header("apikey", anon)
        req.add_header("Authorization", "Bearer " + anon)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            kind = r.headers.get("Content-Type", "")
            body = r.read(64_000)
    except Exception:
        return b""
    low = body.lower()
    if "image/svg+xml" not in kind or b"<svg" not in low or b"<script" in low:
        return b""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    except OSError:
        pass
    return body


def summary() -> dict:
    """What the studio's screen needs, with no token in it."""
    s = state()
    m = me()
    return {"configured": bool(base_url()), "control": s.get("control") or {}, "me": m,
            "ai_mode": ai_mode(), "managed": bool(managed()), "checked_at": s.get("at"),
            "error": s.get("error") or "", "has_device": bool(s.get("device_token")),
            "blocked_why": why("any")}


# ── a heartbeat for long-running processes ──────────────────────────

class Heartbeat:
    """Pings every `every` seconds on a daemon thread. `token()` and
    `activity()` are called each time, so the newest sign-in and what the
    app is doing right now go with every check-in."""

    def __init__(self, token=lambda: "", activity=lambda: {}, every: float = 60.0,
                 on_change=None):
        self.token, self.activity, self.every = token, activity, every
        self.on_change = on_change
        self._stop = threading.Event()
        self.thread = None

    def beat(self) -> dict:
        before = json.dumps(state().get("control"), sort_keys=True), json.dumps(state().get("me"), sort_keys=True)
        try:
            tok = self.token() or ""
        except Exception:
            tok = ""
        try:
            act = self.activity() or {}
        except Exception:
            act = {}
        s = check(tok, act)
        after = json.dumps(s.get("control"), sort_keys=True), json.dumps(s.get("me"), sort_keys=True)
        if self.on_change and after != before:
            try:
                self.on_change(s)
            except Exception:
                pass
        return s

    def start(self):
        if not base_url() or (self.thread and self.thread.is_alive()):
            return self

        def run():
            while not self._stop.is_set():
                try:
                    self.beat()
                except Exception:
                    pass
                self._stop.wait(self.every)
        self.thread = threading.Thread(target=run, name="aethron-heartbeat", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()


if __name__ == "__main__":
    if "--status" in sys.argv:
        print(json.dumps(summary(), indent=1))
    elif "--ping" in sys.argv:
        print(json.dumps({k: v for k, v in ping().items() if k != "device_token"}, indent=1))
    else:
        print("usage: aethron_control.py --status | --ping")

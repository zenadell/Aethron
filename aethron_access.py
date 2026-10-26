"""Who may use Aethron's MCP from their own coding agent.

The MCP is part of the Studio plan. It runs from the person's own app
(`Aethron --mcp`), so the check happens here, on their Mac, when an
outside agent calls a tool:

  * billing switch OFF (the beta, today) -> everyone may use it;
  * Aethron's own agent inside the app    -> always (it is not an outside
    agent; it proves that with a secret only the app can read);
  * otherwise the signed-in account must be on Studio. The plan is
    confirmed online when possible, and a confirmation is trusted
    offline for `mcp_offline_days` (default 3, set in aethron_config.json
    or AETHRON_MCP_OFFLINE_DAYS).

A refusal is an answer, not a dropped connection: the agent is told the
MCP needs Studio and where to upgrade, so it can tell its person.

This stops ordinary use; a program on someone's own computer can always
be taken apart. Serving the MCP from Aethron's servers is the step that
makes the lock hard, and it is planned.
"""
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path

MCP_PLANS = {"studio"}
UPGRADE_URL = "https://aethron.jomiez.com/pricing"
INTERNAL_ENV = "AETHRON_MCP_INTERNAL"


def home() -> Path:
    return Path(os.environ.get("AETHRON_HOME")
                or Path(__file__).resolve().parent)


def _cloud():
    import aethron_cloud
    return aethron_cloud


def enforced() -> bool:
    """One switch for all paid features: AETHRON_ENFORCE_BILLING."""
    try:
        return bool(_cloud().ENFORCE_BILLING)
    except Exception:
        return False


def offline_days() -> float:
    v = os.environ.get("AETHRON_MCP_OFFLINE_DAYS")
    if not v:
        try:
            for base in (home(), Path(__file__).resolve().parent):
                f = base / "aethron_config.json"
                if f.is_file():
                    v = json.loads(f.read_text()).get("mcp_offline_days")
                    if v is not None:
                        break
        except Exception:
            v = None
    try:
        return max(0.0, float(v)) if v is not None else 3.0
    except (TypeError, ValueError):
        return 3.0


# ── the app's own agent ───────────────────────────────────────────────
def internal_secret(where=None) -> str:
    """A secret only this app's data folder holds; the app hands it to
    the agent it starts, so that agent is never asked for a plan. `where`
    is the data folder the MCP server will be started with."""
    f = Path(where or home()) / ".mcp_internal"
    try:
        s = f.read_text().strip()
        if len(s) >= 24:
            return s
    except OSError:
        pass
    s = secrets.token_urlsafe(32)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(s)
        os.chmod(f, 0o600)
    except OSError:
        pass
    return s


def _is_internal(env) -> bool:
    given = (env or {}).get(INTERNAL_ENV, "")
    if not given:
        return False
    try:
        have = (home() / ".mcp_internal").read_text().strip()
    except OSError:
        return False
    return bool(have) and secrets.compare_digest(given, have)


# ── the plan, as last confirmed ───────────────────────────────────────
def record_plan(plan: str, user_id: str = "") -> None:
    """Called whenever the app learns the account's plan (sign-in, a live
    check). The MCP trusts this record for `offline_days`."""
    try:
        f = home() / ".plan.json"
        f.write_text(json.dumps({"plan": plan or "free", "user": user_id,
                                 "at": time.time()}))
        os.chmod(f, 0o600)
    except OSError:
        pass


def _recorded():
    try:
        r = json.loads((home() / ".plan.json").read_text())
        return r.get("plan") or "free", float(r.get("at") or 0)
    except Exception:
        return None, 0.0


def _signed_in():
    """(token, user_id) of the most recently used sign-in on this Mac."""
    try:
        raw = json.loads((home() / ".sessions.json").read_text())
    except Exception:
        return None
    best = None
    for rec in (raw or {}).values():
        u = (rec or {}).get("user") or {}
        if u.get("token") and u.get("id"):
            seen = float(rec.get("seen") or 0)
            if best is None or seen > best[0]:
                best = (seen, u["token"], u["id"])
    return best[1:] if best else None


def _live_plan(token, user_id):
    """The plan from the account itself, or None when it could not be
    asked (offline, expired sign-in). None is not 'free'."""
    c = _cloud()
    if getattr(c, "DRY", False):
        return os.environ.get("AETHRON_CLOUD_DEBUG_PLAN", "free")
    if not getattr(c, "REAL", False):
        return None
    try:
        req = urllib.request.Request(
            c.SUPABASE_URL + f"/rest/v1/profiles?id=eq.{user_id}&select=plan")
        req.add_header("apikey", c.ANON_KEY)
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status != 200:
                return None
            rows = json.loads(r.read().decode())
        return (rows[0].get("plan") if rows else "free") or "free"
    except Exception:
        return None


def mcp_access(env=None, now=None):
    """(allowed, message). The message is what the agent is told."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    if not enforced():
        return True, "beta: the MCP is open to everyone"
    if _is_internal(env):
        return True, "Aethron's own agent"
    plan, at = _recorded()
    if plan in MCP_PLANS and now - at <= offline_days() * 86400:
        return True, "Studio (confirmed)"
    who = _signed_in()
    if who:
        live = _live_plan(*who)
        if live is not None:
            record_plan(live, who[1])
            if live in MCP_PLANS:
                return True, "Studio"
            return False, (
                "Aethron's MCP is part of the Studio plan, and this account "
                f"is on {live!r}. Upgrade at {UPGRADE_URL}, then try again.")
    if plan in MCP_PLANS:
        return False, (
            "Aethron could not confirm this account's Studio plan for "
            f"{offline_days():g} days. Open the Aethron app while online "
            "(it confirms the plan when it starts), then try again.")
    if not who:
        return False, (
            "Aethron's MCP is part of the Studio plan. Open the Aethron app "
            "and sign in with your Studio account, then try again. "
            f"Plans: {UPGRADE_URL}")
    return False, (
        "Aethron could not confirm this account's plan. Open the Aethron "
        "app while online and sign in again, then try again.")


def selftest():
    import tempfile
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print(("PASS " if cond else "FAIL ") + name)

    c = _cloud()
    saved = (c.ENFORCE_BILLING, getattr(c, "REAL", False),
             getattr(c, "DRY", False), os.environ.get("AETHRON_HOME"))
    with tempfile.TemporaryDirectory() as d:
        os.environ["AETHRON_HOME"] = d
        try:
            c.ENFORCE_BILLING = False
            check("beta: open to everyone", mcp_access({})[0])
            c.ENFORCE_BILLING, c.REAL, c.DRY = True, False, False
            a, m = mcp_access({})
            check("enforced, nobody signed in: refused", not a and "sign in" in m)
            check("the app's own agent is never refused",
                  mcp_access({INTERNAL_ENV: internal_secret()})[0])
            check("a wrong secret is not the app",
                  not mcp_access({INTERNAL_ENV: "x" * 40})[0])
            record_plan("studio", "u1")
            check("Studio confirmed: allowed", mcp_access({})[0])
            check("Studio confirmed 10 days ago, offline: refused",
                  not mcp_access({}, now=time.time() + 10 * 86400)[0])
            record_plan("pro", "u1")
            a, m = mcp_access({})
            check("Pro is not Studio", not a and "Studio" in m)
            (Path(d) / ".sessions.json").write_text(json.dumps(
                {"t": {"user": {"id": "u1", "token": "tok"}, "seen": 1}}))
            c.DRY = True
            os.environ["AETHRON_CLOUD_DEBUG_PLAN"] = "studio"
            record_plan("free", "u1")
            check("upgraded online: the live plan wins", mcp_access({})[0])
            check("…and is recorded", _recorded()[0] == "studio")
            os.environ["AETHRON_CLOUD_DEBUG_PLAN"] = "free"
            record_plan("free", "u1")
            a, m = mcp_access({})
            check("free account online: refused, told where to upgrade",
                  not a and UPGRADE_URL in m)
        finally:
            c.ENFORCE_BILLING, c.REAL, c.DRY = saved[:3]
            os.environ.pop("AETHRON_CLOUD_DEBUG_PLAN", None)
            if saved[3] is None:
                os.environ.pop("AETHRON_HOME", None)
            else:
                os.environ["AETHRON_HOME"] = saved[3]
    print(f"\n{ok} passed, {fail} failed")
    return fail == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(mcp_access())

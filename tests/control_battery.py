#!/usr/bin/env python3
"""The app obeys the Aethron team's switches — measured, not assumed.

The REAL edge function runs locally over a stand-in Supabase
(tests/cloud/serve_fake.ts). This battery is one installed copy of
Aethron (a throwaway AETHRON_HOME) and an admin (the master key):

  check-in    a signed-in copy gets a device token, kept 0600 and never
              shown; the server sees what the studio is doing
  kill switch the studio refuses agent/build/AI work with the team's own
              words, the bridge stops a running agent at its next model
              call, the MCP refuses outside agents — and looking (publish
              status, settings) still works
  features    MCP off stops outside agents but not Aethron's own agent
  managed AI  one admin switch and the brain runs on Aethron's gateway
              with the device token; the admin's key reaches the provider,
              the person's saved key never does; their spend is metered;
              saving a key of their own is refused while it is on
  people      a hold, a sign-out (sessions on this copy end), an update
              requirement
  offline     the last answer still holds; a dead admin is not re-asked
              on every call

Needs bun; without it reports SKIPPED, never PASS.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, flush=True)


bun = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
if not Path(bun).exists():
    print("VERDICT: SKIPPED — no bun to run the edge function")
    sys.exit(0)

HOME = Path(tempfile.mkdtemp(prefix="ae-control-"))
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
BASE = f"http://127.0.0.1:{port}"
FN = BASE + "/aethron"
os.environ.update(AETHRON_HOME=str(HOME), AETHRON_CONTROL_URL=FN, AETHRON_REPORTS="off", AETHRON_RUNLOG="off")
for k in ("AETHRON_AI_PROVIDER", "AETHRON_AI_KEY", "AETHRON_AI_MODEL", "AETHRON_AI_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

srv = subprocess.Popen([bun, str(ROOT / "tests/cloud/serve_fake.ts"), str(port)], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
MASTER = "master-key-0123456789abcdefghij"


def admin(tool, **args):
    r = urllib.request.Request(FN + "/admin/call", data=json.dumps({"tool": tool, "args": args}).encode(),
                               headers={"Authorization": "Bearer " + MASTER, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())["result"]


def state():
    return json.loads(urllib.request.urlopen(BASE + "/__state", timeout=5).read())


try:
    if "ready" not in srv.stdout.readline():
        print("VERDICT: SKIPPED — the function did not start")
        sys.exit(0)
    import aethron_control as C
    import aethron_brain as B
    import aethron_bridge as BR
    import forge_mcp as M
    import studio

    me = json.loads(urllib.request.urlopen(BASE + "/__user?email=una@example.com", timeout=5).read())
    # this copy's person has their OWN key saved — it must never be used while managed
    B.save({"provider": "deepseek", "api_key": "sk-PERSONS-OWN-KEY-999", "model": "deepseek-flash"})

    # ── check-in ──
    st = C.ping(me["token"], {"job": "build", "project": "acme"})
    tok = st.get("device_token", "")
    check("a signed-in copy gets a device token of its own", tok.startswith("adt_"))
    check("kept owner-only on disk", oct((HOME / "control.json").stat().st_mode)[-3:] == "600")
    check("and never shown to the screen", tok not in json.dumps(C.summary()))
    dev = [d for d in state()["devices"] if d["machine"] == C.machine_id()][0]
    check("the team sees what this copy is doing", dev["activity"].get("project") == "acme" and dev["email"] == "una@example.com")
    check("nothing is stopped while the switches are off", C.why("agent") == "" and studio.control_refusal("/api/code/send", {}) == "")

    # the studio, really serving
    sport_s = socket.socket()
    sport_s.bind(("127.0.0.1", 0))
    sport = sport_s.getsockname()[1]
    sport_s.close()
    httpd = studio.ThreadingHTTPServer(("127.0.0.1", sport), studio.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path, body):
        r = urllib.request.Request(f"http://127.0.0.1:{sport}{path}", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(r, timeout=20) as x:
                return x.status, json.loads(x.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{sport}{path}", timeout=20) as x:
            return json.loads(x.read())

    # ── the kill switch ──
    admin("pause_all", message="Back in ten minutes — the Aethron team")
    C.ping()
    msg = "Back in ten minutes — the Aethron team"
    check("the copy hears the kill switch on its next check-in", C.why("agent") == msg)
    code, body = post("/api/code/send", {"key": "x", "text": "hello"})
    check("the studio refuses an agent message, in the team's words (423)", code == 423 and body.get("error") == msg)
    code, body = post("/api/run", {"project": "x", "cmd": "build"})
    check("and a build", code == 423)
    code, _ = post("/api/publish", {"action": "status"})
    check("but looking is never stopped (publish status still answers)", code != 423)
    check("the screen is told", get("/api/control")["blocked_why"] == msg and get("/api/control")["control"]["paused"])
    check("a running agent stops at its next model call (the bridge refuses)", BR._control_why() == msg)
    reply = M.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}})
    check("an outside agent's MCP call is refused", reply["result"].get("isError")
          and "STOPPED by the Aethron team" in reply["result"]["content"][0]["text"])
    admin("resume_all")
    C.ping()
    check("resume lifts it everywhere", C.why("agent") == "" and post("/api/code/send", {"key": "nope"})[0] != 423)

    # ── one part off ──
    admin("set_feature", feature="mcp", on=False)
    C.ping()
    out = M.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}})
    check("MCP off: outside agents are refused", out["result"].get("isError") and "MCP" in out["result"]["content"][0]["text"])
    (HOME / ".mcp_internal").write_text("internal-secret-abc")
    os.environ["AETHRON_MCP_INTERNAL"] = "internal-secret-abc"
    out = M.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}})
    check("but Aethron's own agent keeps working", not out["result"].get("isError"))
    os.environ.pop("AETHRON_MCP_INTERNAL")
    admin("set_feature", feature="mcp", on=True)

    # ── the managed AI ──
    check("before: the person's own provider runs", B.resolve()["provider"] == "deepseek")
    admin("set_provider_key", provider="gemini", keys="AIza-THE-TEAMS-KEY-777", base_url="https://prov.test/v1")
    admin("set_ai_mode", mode="managed", provider="gemini", model="gemini-3.6-flash")
    C.ping()
    r = B.resolve()
    check("one admin switch: the brain runs on Aethron's gateway", r["provider"] == "aethron" and r["base"] == FN + "/v1"
          and r["ready"])
    check("with this copy's device token as its key", r["key"] == tok)
    text = B.text_call("Say hello")
    sent = state()["sent"][-1]
    check("a model call goes through the gateway and comes back", "people on Aethron" in str(text))
    check("the provider got the TEAM's key, never the person's", sent["auth"] == "Bearer AIza-THE-TEAMS-KEY-777"
          and "PERSONS-OWN" not in json.dumps(state()["sent"]))
    check("and the team's model", json.loads(sent["body"])["model"] == "gemini-3.6-flash")
    usage = [u for u in state()["usage"] if u.get("user_id") == me["id"]]
    check("the person's request is on their ledger", len(usage) >= 1 and usage[-1]["purpose"])
    code, body = post("/api/ai/settings", {"ai": {"provider": "openai", "api_key": "sk-new"}})
    check("saving a key of their own is refused while it is on", code == 423 and "Aethron's own AI" in body.get("error", ""))
    check("the settings screen says who powers the AI", get("/api/ai/settings")["control"]["managed"] is True)
    check("the local spend window does not stand in the server's way", B.session_check() == "")
    admin("set_ai_mode", mode="byok")
    C.ping()
    check("switched back: the person's own key returns, untouched", B.resolve()["provider"] == "deepseek"
          and B.resolve()["key"] == "sk-PERSONS-OWN-KEY-999")

    # ── people ──
    admin("block_user", user="una@example.com", reason="unpaid invoice")
    C.ping()
    check("a hold stops this copy, with the reason", C.why("migrate") == "Your Aethron account is on hold: unpaid invoice")
    admin("unblock_user", user="una@example.com")
    admin("set_min_version", version="99.0.0")
    C.ping()
    check("an old version is told to update", "too old" in C.why("agent"))
    admin("set_min_version", version="")
    C.ping()
    studio.SESSIONS["sess-test"] = {"id": me["id"], "email": "una@example.com", "token": me["token"], "_seen": time.time()}
    hb = C.Heartbeat(token=studio._latest_token, activity=studio._activity, on_change=studio._on_control_change)
    hb.beat()
    admin("sign_out_user", user="una@example.com")
    time.sleep(1.1)                      # the token below was issued before the sign-out
    hb.beat()
    if os.environ.get("AE_DEBUG"):
        print("DEBUG", {k: v for k, v in C.state().items() if k != "device_token"})
    check("a sign-out reaches this copy", C.me().get("signed_out") is True and "signed out" in C.why("agent"))
    check("and every session on it ends", not studio.SESSIONS)
    check("its device token is dropped", not C.device_token())
    fresh = json.loads(urllib.request.urlopen(BASE + "/__user?email=una@example.com&fresh=1", timeout=5).read())
    time.sleep(2.2)
    C.ping(fresh["token"])
    check("signing in again brings it back", C.device_token().startswith("adt_") and C.why("agent") == "")

    # ── events on the device token ──
    n0 = len(state()["events"])
    code, _ = C._request("POST", "/event", {"events": [{"event": "build", "props": {"ok": True}, "ts": time.time()}]},
                         C.device_token())
    check("activity events go in on the device token (no expiring login)", code == 200 and len(state()["events"]) == n0 + 1)

    # ── offline ──
    admin("pause_all", message="paused for the offline test")
    C.ping()
    os.environ["AETHRON_CONTROL_URL"] = "http://127.0.0.1:9"     # nothing listens there
    st = C.ping()
    check("offline: the last answer still holds", C.why("agent") == "paused for the offline test" and st.get("error"))
    t0 = time.time()
    C.refresh(max_age=0)
    C.refresh(max_age=0)
    check("and a dead admin is not asked again on every call", time.time() - t0 < 1.0)
    os.environ["AETHRON_CONTROL_URL"] = FN
    admin("resume_all")
    httpd.shutdown()
finally:
    srv.terminate()
    shutil.rmtree(HOME, ignore_errors=True)

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

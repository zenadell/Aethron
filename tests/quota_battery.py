#!/usr/bin/env python3
"""Supabase's free plan counts every edge-function call. A copy must not spend
one per minute just to learn nothing changed: between full check-ins it asks
the control table (a database read), and pings only when a switch changed or
a full check-in is due. Fake Supabase, no network."""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="quota-"))
CALLS = {"ping": 0, "rest": 0}
CTRL = {"updated_at": "2026-11-04T10:00:00+00:00", "paused": False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/rest/v1/control"):
            CALLS["rest"] += 1
            # the database writes its own form of the time
            return self._send([{"updated_at": CTRL["updated_at"].replace("T", " ").replace("+00:00", "+00")}])
        self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if self.path == "/functions/v1/aethron/ping":
            CALLS["ping"] += 1
            return self._send({"control": dict(CTRL), "device_token": "dt-1", "me": {}})
        self.send_response(404); self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(AETHRON_HOME=str(HOME), AETHRON_REPORTS="off", AETHRON_RUNLOG="off",
                  AETHRON_SUPABASE_URL=f"http://127.0.0.1:{srv.server_port}",
                  AETHRON_SUPABASE_ANON_KEY="anon-test")
os.environ.pop("AETHRON_CONTROL_URL", None)
sys.path.insert(0, str(ROOT))
import aethron_control as C

ok = bad = 0
def check(name, cond, extra=""):
    global ok, bad
    print(("  PASS " if cond else "  FAIL ") + name + (f" — {extra}" if extra and not cond else ""))
    ok += bool(cond); bad += (not cond)

C.check()
check("the first check-in is a full one", CALLS["ping"] == 1, str(CALLS))
for _ in range(30):                       # thirty minutes of a copy left open
    C.check()
check("nothing changed: no further function calls", CALLS["ping"] == 1, str(CALLS))
check("it asked the database instead", CALLS["rest"] == 30, str(CALLS))
check("the database's own time form counts as the same time", not C.switches_changed())

CTRL.update(updated_at="2026-11-04T10:05:00+00:00", paused=True)
s = C.check()
check("a changed switch brings a full check-in at once", CALLS["ping"] == 2, str(CALLS))
check("and the copy now knows it is paused", (s.get("control") or {}).get("paused") is True)

st = json.loads((HOME / "control.json").read_text()); st["at"] = time.time() - C.FULL_S - 1
(HOME / "control.json").write_text(json.dumps(st)); C._MEM.update(mtime=None)
C.check()
check("a full check-in still happens every FULL_S", CALLS["ping"] == 3, str(CALLS))

before = dict(CALLS)
C.refresh(30); C.refresh(30)
check("refresh soon after a look asks nothing", CALLS == before, f"{before} -> {CALLS}")

srv.shutdown()
os.environ["AETHRON_SUPABASE_URL"] = "http://127.0.0.1:9"
st = json.loads((HOME / "control.json").read_text()); st["looked_at"] = 0; st["at"] = time.time() - 100
(HOME / "control.json").write_text(json.dumps(st)); C._MEM.update(mtime=None)
t = time.time(); s = C.check()
check("offline: the last answer is kept", (s.get("control") or {}).get("paused") is True)

print(f"\n{ok}/{ok+bad} passed")
sys.exit(1 if bad else 0)

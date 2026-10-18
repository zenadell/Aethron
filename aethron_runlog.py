#!/usr/bin/env python3
"""Run records — what every agent that drives Aethron actually did.

Other agents drive Aethron over MCP (the Jomiez lead agent today, other
people's agents later). When something goes wrong nobody could see which
agent called which tool, with what, what Aethron answered, how long it
took, and what broke or went missing. The Progrex preview lost its hero
image and it could not be told whether Aethron or the agent did it.

One file per caller and session, under <AETHRON_HOME>/runs/<caller>/:
    <started>-<session>.jsonl
line 1  {"kind":"session", caller, client, transport, started, …}
then    {"kind":"call", t, tool, project, args, status, ms, result,
         findings[]}
Runs never mix: a session is one MCP connection (stdio: the process; HTTP:
the Mcp-Session-Id the server hands out at initialize).

NEVER A SECRET. Argument keys that name a key/token/secret/password, and
string values shaped like one, are written as "[hidden]". Long strings
(page HTML, block markup) are cut. Writing is one appended line per call.
AETHRON_RUNLOG=off turns it off.

    python3 aethron_runlog.py                 list callers and their runs
    python3 aethron_runlog.py <caller> [n]    the nth newest run, as a timeline
    python3 aethron_runlog.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEEP = 200                       # sessions kept per caller
CUT = 400                        # longest string kept in args/results

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|passwd|"
                         r"authori[sz]ation|cookie|bearer|credential)", re.I)
_SECRET_VAL = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|Bearer\s+\S{12,})")
# what a reader of a run wants to find first
_FINDING = re.compile(r"^\s*(?:.*\b(?:FAIL|FAILED|REJECTED|ERROR|STUCK|"
                      r"NOT ACCEPTED|IMAGE DID NOT LOAD|MISSING)\b.*|⚠.*)$",
                      re.M)


def home() -> Path:
    return Path(os.environ.get("AETHRON_HOME", ROOT)).resolve()


def runs_dir() -> Path:
    return home() / "runs"


def enabled() -> bool:
    return os.environ.get("AETHRON_RUNLOG", "").lower() not in (
        "off", "0", "no", "false")


def slug(s) -> str:
    return re.sub(r"[^\w.-]+", "-", str(s or "").strip()).strip("-.")[:60] \
        or "unknown"


def clean(v, depth=0):
    """A copy safe to write: no secrets, no novels."""
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {k: ("[hidden]" if _SECRET_KEY.search(str(k)) else
                    clean(x, depth + 1)) for k, x in list(v.items())[:60]}
    if isinstance(v, (list, tuple)):
        out = [clean(x, depth + 1) for x in list(v)[:40]]
        if len(v) > 40:
            out.append(f"…(+{len(v) - 40} more)")
        return out
    if isinstance(v, str):
        v = _SECRET_VAL.sub("[hidden]", v)
        return v if len(v) <= CUT else v[:CUT] + f"…(+{len(v) - CUT} chars)"
    return v


def summarize(text: str, data=None) -> dict:
    """What Aethron answered, and the lines a reader looks for first."""
    text = str(text or "")
    out = {}
    if data is not None:
        out["data"] = clean(data)
    t = _SECRET_VAL.sub("[hidden]", text)
    out["text"] = t if len(t) <= 900 else t[:500] + "\n…\n" + t[-400:]
    found, lines = [], t.splitlines()
    for i, ln in enumerate(lines):
        if not _FINDING.match(ln):
            continue
        if ln.rstrip().endswith(":"):  # a header: its reasons are the "- " lines
            items = [x.strip()[2:] for x in lines[i + 1:i + 6]
                     if x.strip().startswith("- ")]
            found += [f"{ln.strip()[:60]} {x}" for x in items] or [ln.strip()]
        else:
            found.append(ln.strip())
    out["findings"] = [f[:300] for f in found][:12]
    return out


def status_of(text: str, is_error: bool, data=None) -> str:
    if is_error:
        return "error"
    head = str(text or "").lstrip()[:40].upper()
    verdict = str((data or {}).get("verdict", "")).upper() \
        if isinstance(data, dict) else ""
    if head.startswith(("FAILED", "PROBLEMS")) or verdict in ("FAIL", "REFUSED"):
        return "failed"
    if isinstance(data, dict) and data.get("rejected"):
        return "partial"
    return "ok"


class Session:
    """One caller's one connection. Cheap: one appended line per call."""

    def __init__(self, transport: str, caller: str = "", sid: str = ""):
        self.transport = transport
        self.caller = caller          # a label we trust (HTTP token)
        self.client = {}              # what the agent says it is
        self.sid = sid or secrets.token_hex(6)
        self.started = time.time()
        self.path = None
        self.calls = 0
        self._lock = threading.Lock()

    def name(self) -> str:
        c = self.caller or self.client.get("name") or ""
        return slug(c or f"{self.transport}-client")

    def hello(self, params: dict):
        info = params.get("clientInfo") or {}
        self.client = {"name": str(info.get("name", ""))[:80],
                       "version": str(info.get("version", ""))[:40]}

    def _open(self):
        if self.path:
            return
        d = runs_dir() / self.name()
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
        self.path = d / f"{stamp}-{self.sid}.jsonl"
        head = {"kind": "session", "session": self.sid,
                "caller": self.name(), "client": self.client,
                "transport": self.transport,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(self.started)),
                "pid": os.getpid()}
        self.path.write_text(json.dumps(head) + "\n", encoding="utf-8")
        old = sorted(d.glob("*.jsonl"))[:-KEEP]
        for f in old:
            try:
                f.unlink()
            except OSError:
                pass

    def call(self, tool: str, args: dict, text: str, is_error: bool,
             ms: float, data=None, extra=None):
        if not enabled():
            return
        try:
            with self._lock:
                self._open()
                self.calls += 1
                a = args if isinstance(args, dict) else {}
                rec = {"kind": "call", "n": self.calls,
                       "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "tool": tool,
                       "project": str(a.get("project") or a.get("name")
                                      or "")[:80],
                       "args": clean(a),
                       "status": status_of(text, is_error, data),
                       "ms": int(ms),
                       "result": summarize(text, data)}
                if extra:
                    rec.update(clean(extra))
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if upload_on():
                UPLOAD.send(self, rec)
        except Exception as e:       # a record must never break a call
            sys.stderr.write(f"run record not written: {e}\n")


# ─────────────────────── upload to Supabase (opt-in) ───────────────────
# So a session that is NOT on this computer (a cloud session debugging the
# keeper) can read what happened. OFF unless this machine says so: records
# hold the copy agents write, and the app ships to everyone.
#   on:  AETHRON_RUN_UPLOAD=1, or {"upload": true} in <home>/runs/settings.json
#        (the studio's Settings toggle writes it)
# WRITE with the project's public (anon) key into public.agent_runs, which
# that key can INSERT into and never read (supabase_schema.sql). READ only
# with the service key, held in the reading session's secrets, never here.
# Every row goes to runs/.outbox.jsonl FIRST and leaves it only once the
# server has taken it — offline, a crash, a stdio agent hanging up: nothing
# is lost, the next call or the next run sends it.
OUTBOX_CAP = 5_000_000


def _settings_file() -> Path:
    return runs_dir() / "settings.json"


def upload_on() -> bool:
    env = os.environ.get("AETHRON_RUN_UPLOAD", "").strip().lower()
    if env:
        return env in ("1", "on", "yes", "true")
    try:
        return bool(json.loads(_settings_file().read_text())
                    .get("upload"))
    except (OSError, ValueError):
        return False


def set_upload(on: bool) -> dict:
    runs_dir().mkdir(parents=True, exist_ok=True)
    cur = {}
    try:
        cur = json.loads(_settings_file().read_text())
    except (OSError, ValueError):
        pass
    cur["upload"] = bool(on)
    _settings_file().write_text(json.dumps(cur, indent=1))
    return upload_status()


def _cloud() -> tuple:
    """(url, anon key) — the same project the app logs in with."""
    url = os.environ.get("AETHRON_SUPABASE_URL", "")
    key = os.environ.get("AETHRON_SUPABASE_ANON_KEY", "")
    if not (url and key):
        try:
            import aethron_cloud as C
            url, key = url or C.SUPABASE_URL, key or C.ANON_KEY
        except Exception:
            pass
    if not (url and key):
        # run from a checkout (the keeper's terminal-started --http server):
        # the release's own shareable settings carry the public key
        try:
            rc = json.loads((ROOT / "release_config.json").read_text())
            url = url or rc.get("supabase_url", "")
            key = key or rc.get("supabase_anon_key", "")
        except (OSError, ValueError):
            pass
    return url.rstrip("/"), key


def machine_id() -> str:
    """A random id for this computer, made once — never its hostname."""
    f = runs_dir() / ".machine"
    try:
        return f.read_text().strip()[:64]
    except OSError:
        runs_dir().mkdir(parents=True, exist_ok=True)
        mid = "m-" + secrets.token_hex(6)
        f.write_text(mid)
        return mid


def upload_status() -> dict:
    url, key = _cloud()
    out = runs_dir() / ".outbox.jsonl"
    try:
        waiting = sum(1 for _ in out.open(encoding="utf-8"))
    except OSError:
        waiting = 0
    return {"on": upload_on(), "configured": bool(url and key),
            "machine": machine_id(), "waiting": waiting,
            "last_error": UPLOAD.last_error, "sent": UPLOAD.sent}


class _Uploader:
    def __init__(self):
        self.lock = threading.Lock()      # guards the outbox file
        self.sending = threading.Lock()   # ONE flush at a time: two flushers
        self.wake = threading.Event()     # read the same rows and sent each
                                          # twice (caught by the selftest)
        self.thread = None
        self.last_error = ""
        self.sent = 0

    def send(self, sess: "Session", rec: dict):
        row = {"machine": machine_id(), "caller": sess.name(),
               "run": sess.path.stem if sess.path else sess.sid,
               "n": rec.get("n"), "at": rec.get("t", ""),
               "tool": rec.get("tool", "")[:80],
               "project": rec.get("project", "")[:80],
               "status": rec.get("status", ""), "ms": rec.get("ms", 0),
               "record": dict(rec, session={
                   "client": sess.client, "transport": sess.transport,
                   "session": sess.sid})}
        out = runs_dir() / ".outbox.jsonl"
        with self.lock:
            if out.exists() and out.stat().st_size > OUTBOX_CAP:
                self.last_error = "outbox full — oldest rows kept, new dropped"
                return
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            import atexit
            atexit.register(self.flush, 5)
        self.wake.set()

    def _loop(self):
        while True:
            self.wake.wait(30)
            self.wake.clear()
            self.flush()

    def flush(self, timeout=20) -> bool:
        """Send everything waiting. True when the outbox is empty. The
        network is never touched under the outbox lock: an agent's call
        that adds a row must not wait on a slow upload."""
        if not self.sending.acquire(timeout=timeout + 5):
            return False
        try:
            return self._flush(timeout)
        finally:
            self.sending.release()

    def _flush(self, timeout) -> bool:
        import urllib.request
        url, key = _cloud()
        out = runs_dir() / ".outbox.jsonl"
        if not (url and key):
            self.last_error = "no Supabase project configured"
            return False
        with self.lock:
            try:
                lines = [x for x in out.read_text(encoding="utf-8")
                         .splitlines() if x.strip()]
            except OSError:
                lines = []
        if not lines:
            return True
        done = 0
        for i in range(0, len(lines), 50):
            batch = []
            for x in lines[i:i + 50]:
                try:
                    batch.append(json.loads(x))
                except ValueError:
                    pass                    # a torn line: drop it
            req = urllib.request.Request(
                url + "/rest/v1/agent_runs", method="POST",
                data=json.dumps(batch).encode(),
                headers={"apikey": key, "Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "Prefer": "return=minimal"})
            try:
                with urllib.request.urlopen(req, timeout=timeout):
                    pass
            except Exception as e:
                detail = ""
                try:
                    detail = e.read().decode()[:200]   # type: ignore
                except Exception:
                    pass
                self.last_error = f"{e} {detail}".strip()[:300]
                if "PGRST205" in detail or "agent_runs" in detail and \
                        "find the table" in detail:
                    self.last_error = (
                        "the agent_runs table does not exist yet — run the "
                        "agent_runs part of supabase_schema.sql in Supabase's "
                        "SQL editor; records wait here until then")
                break
            done = i + len(lines[i:i + 50])
            self.sent += len(batch)
        if done:
            with self.lock:                 # rows added meanwhile stay
                try:
                    now = [x for x in out.read_text(encoding="utf-8")
                           .splitlines() if x.strip()]
                except OSError:
                    now = []
                out.write_text("".join(x + "\n" for x in now[done:]),
                               encoding="utf-8")
        if done == len(lines):
            self.last_error = ""
            return True
        return False


UPLOAD = _Uploader()


def cloud_rows(query: str) -> list:
    """Read uploaded rows. Needs the SERVICE key in the environment
    (SUPABASE_SERVICE_KEY) — the public key cannot read this table."""
    import urllib.request
    url = (os.environ.get("AETHRON_SUPABASE_URL") or _cloud()[0]).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not (url and key):
        raise SystemExit("set AETHRON_SUPABASE_URL and SUPABASE_SERVICE_KEY "
                         "(the service key lives in this environment's "
                         "secrets, never in the repo)")
    req = urllib.request.Request(url + "/rest/v1/agent_runs?" + query,
                                 headers={"apikey": key,
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def cloud_callers(days: int = 14) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() - days * 86400))
    rows = cloud_rows("select=machine,caller,run,status,project,at&"
                      f"received_at=gte.{since}&order=id.desc&limit=5000")
    runs = {}
    for r in rows:
        k = (r["caller"], r["run"])
        x = runs.setdefault(k, {"caller": r["caller"], "run": r["run"],
                                "machine": r["machine"], "calls": 0,
                                "problems": 0, "projects": set(),
                                "last": r["at"]})
        x["calls"] += 1
        x["problems"] += r["status"] in ("error", "failed", "partial")
        if r["project"]:
            x["projects"].add(r["project"])
    out = {}
    for x in runs.values():
        x["projects"] = sorted(x["projects"])
        out.setdefault(x["caller"], []).append(x)
    return [{"caller": c, "runs": sorted(v, key=lambda r: r["run"],
                                         reverse=True)}
            for c, v in sorted(out.items())]


def cloud_run(caller: str, run: str = "") -> dict:
    import urllib.parse as up
    if not run or run.isdigit():
        k = int(run or 0)
        names = sorted({r["run"] for r in cloud_rows(
            f"select=run&caller=eq.{up.quote(caller)}&order=id.desc"
            "&limit=5000")}, reverse=True)
        if k >= len(names):
            raise ValueError(f"no uploaded run {k} for {caller!r}")
        run = names[k]
    rows = cloud_rows(f"select=record&caller=eq.{up.quote(caller)}"
                      f"&run=eq.{up.quote(run)}&order=n.asc")
    calls = [r["record"] for r in rows]
    s = (calls[0].get("session") if calls else {}) or {}
    return {"run": run, "calls": calls,
            "session": {"caller": caller, "client": s.get("client", {}),
                        "transport": s.get("transport", ""),
                        "started": run[:15]}}


# ─────────────────────────────── reading ──────────────────────────────

def _read(f: Path):
    head, calls = {}, []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "session":
                head = r
            elif r.get("kind") == "call":
                calls.append(r)
    except OSError:
        pass
    return head, calls


def callers() -> list:
    """Every caller with its runs, newest first, each run summarised."""
    out = []
    d = runs_dir()
    if not d.is_dir():
        return out
    for c in sorted(p for p in d.iterdir() if p.is_dir()):
        runs = []
        for f in sorted(c.glob("*.jsonl"), reverse=True):
            head, calls = _read(f)
            bad = [x for x in calls if x["status"] in ("error", "failed",
                                                       "partial")]
            runs.append({"run": f.stem, "caller": c.name,
                         "started": head.get("started", ""),
                         "client": head.get("client", {}),
                         "transport": head.get("transport", ""),
                         "calls": len(calls), "problems": len(bad),
                         "findings": sum(len(x["result"].get("findings", []))
                                         for x in calls),
                         "projects": sorted({x["project"] for x in calls
                                             if x["project"]}),
                         "last": calls[-1]["t"] if calls else ""})
        if runs:
            out.append({"caller": c.name, "runs": runs})
    out.sort(key=lambda c: c["runs"][0]["last"] or c["runs"][0]["started"],
             reverse=True)
    return out


def get_run(caller: str, run: str = "") -> dict:
    """One run in full. run "" (or an index like "0") = newest."""
    d = runs_dir() / slug(caller)
    files = sorted(d.glob("*.jsonl"), reverse=True) if d.is_dir() else []
    if not files:
        raise ValueError(f"no runs recorded for caller {caller!r}")
    if not run or run.isdigit():
        k = int(run or 0)
        if k >= len(files):
            raise ValueError(f"{caller!r} has {len(files)} run(s)")
        f = files[k]
    else:
        f = d / (slug(run) + ".jsonl")
        if f not in files:
            raise ValueError(f"no run {run!r} for {caller!r}")
    head, calls = _read(f)
    return {"run": f.stem, "session": head, "calls": calls}


def timeline(r: dict) -> str:
    h = r["session"]
    cl = h.get("client") or {}
    lines = [f"run {r['run']} — {h.get('caller', '?')} "
             f"({cl.get('name', '') or 'client unnamed'}"
             f"{' ' + cl.get('version', '') if cl.get('version') else ''}, "
             f"{h.get('transport', '?')}) started {h.get('started', '?')}"]
    mark = {"ok": "  ok ", "partial": "  ~~ ", "failed": "FAIL ",
            "error": " ERR "}
    for c in r["calls"]:
        lines.append(f"{mark.get(c['status'], '  ?  ')}{c['t'][11:]} "
                     f"{c['tool']}"
                     + (f" [{c['project']}]" if c['project'] else "")
                     + f" {c['ms'] / 1000:.1f}s")
        for f in c["result"].get("findings", [])[:6]:
            lines.append(f"        ↳ {f[:160]}")
    return "\n".join(lines)


# ────────────────────────────── selftest ──────────────────────────────

def socket_name() -> str:
    import socket
    return socket.gethostname()


def selftest() -> int:
    import tempfile
    ok = fail = 0

    def check(name, cond, info=""):
        nonlocal ok, fail
        ok += bool(cond)
        fail += not cond
        print(("  ok  " if cond else "  FAIL") + f" {name}"
              + (f" — {info}" if info and not cond else ""))

    with tempfile.TemporaryDirectory() as td:
        os.environ["AETHRON_HOME"] = td
        s = Session("stdio")
        s.hello({"clientInfo": {"name": "keeper agent", "version": "2"}})
        s.call("set_content_bulk", {"project": "crux", "api_key": "abc",
                                    "entries": [{"new": "x" * 2000}],
                                    "note": "key sk-ABCDEFGHIJKLMNOPQRST"},
               "applied 1, rejected 1\nREJECTED (fix):\n- image not used",
               False, 120, {"applied": 1, "rejected": [{"old": "a"}]})
        s.call("export_preview", {"project": "crux"},
               "browser check: FAIL\n  FAIL index.html: IMAGE DID NOT LOAD",
               False, 9000, {"verdict": "FAIL"})
        s.call("nope", {}, "ERROR: unknown tool 'nope'", True, 1)
        raw = s.path.read_text()
        check("one file per caller and session",
              s.path.parent.name == "keeper-agent" and s.sid in s.path.name)
        check("no key is written", "abc" not in raw.replace("abcdef", "")
              and "sk-ABCDEFG" not in raw and "[hidden]" in raw)
        check("long strings are cut", "+1600 chars" in raw)
        r = get_run("keeper-agent")
        st = [c["status"] for c in r["calls"]]
        check("statuses read", st == ["partial", "failed", "error"], str(st))
        check("findings pulled out",
              any("IMAGE DID NOT LOAD" in f
                  for f in r["calls"][1]["result"]["findings"]))
        cs = callers()
        check("callers listed", cs and cs[0]["caller"] == "keeper-agent"
              and cs[0]["runs"][0]["problems"] == 3, json.dumps(cs)[:200])
        s2 = Session("http", caller="keeper")
        s2.call("list_projects", {}, "no projects yet", False, 3)
        check("a second session never mixes into the first",
              s2.path != s.path and len(get_run("keeper")["calls"]) == 1)
        check("timeline reads", "FAIL" in timeline(r) and "↳" in timeline(r))
        os.environ["AETHRON_RUNLOG"] = "off"
        s3 = Session("stdio", caller="silent")
        s3.call("list_projects", {}, "x", False, 1)
        check("AETHRON_RUNLOG=off writes nothing",
              not (runs_dir() / "silent").exists())
        os.environ.pop("AETHRON_RUNLOG")

        # ── upload, against a fake Supabase that holds rows like the real
        # table: the public key may add, only the service key may read
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import urllib.parse as up
        table, state = [], {"down": False, "keys": []}

        class Fake(BaseHTTPRequestHandler):
            def log_message(s_, *a):
                pass

            def _reply(s_, code, body=b""):
                s_.send_response(code)
                s_.send_header("Content-Length", str(len(body)))
                s_.end_headers()
                s_.wfile.write(body)

            def do_POST(s_):
                state["keys"].append(s_.headers.get("apikey"))
                n = int(s_.headers.get("Content-Length") or 0)
                body = json.loads(s_.rfile.read(n))
                if state["down"]:
                    return s_._reply(503, b"down")
                table.extend(body)
                s_._reply(201)

            def do_GET(s_):
                if s_.headers.get("apikey") != "SERVICE":
                    return s_._reply(200, b"[]")   # RLS: anon sees nothing
                q = up.parse_qs(up.urlparse(s_.path).query)
                rows = [r for r in table if all(
                    r.get(k) == v[0][3:] for k, v in q.items()
                    if v[0].startswith("eq."))]
                if q.get("order", [""])[0].startswith("n."):
                    rows.sort(key=lambda r: r["n"])
                s_._reply(200, json.dumps(rows).encode())
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["AETHRON_SUPABASE_URL"] = \
            f"http://127.0.0.1:{srv.server_address[1]}"
        os.environ["AETHRON_SUPABASE_ANON_KEY"] = "PUBLIC"
        try:
            s4 = Session("stdio", caller="up-off")
            s4.call("list_projects", {}, "x", False, 1)
            UPLOAD.flush()
            check("nothing leaves the machine unless switched on",
                  not table and not upload_on())
            set_upload(True)
            s5 = Session("http", caller="keeper")
            s5.call("export_preview", {"project": "crux", "token": "SECRETX"},
                    "browser check: FAIL\n  FAIL index.html: IMAGE DID NOT "
                    "LOAD", False, 900, {"verdict": "FAIL"})
            UPLOAD.flush()
            check("switched on, the row reaches Supabase with the public key",
                  len(table) == 1 and state["keys"][-1] == "PUBLIC"
                  and table[0]["caller"] == "keeper"
                  and table[0]["status"] == "failed", table)
            check("no secret is uploaded", "SECRETX" not in json.dumps(table))
            check("the machine is a random id, not its name",
                  table[0]["machine"].startswith("m-")
                  and socket_name() not in json.dumps(table))
            state["down"] = True
            s5.call("get_content", {"project": "crux"}, "ok", False, 3)
            UPLOAD.flush()
            check("offline, the row waits on disk",
                  upload_status()["waiting"] == 1 and len(table) == 1)
            state["down"] = False
            UPLOAD.flush()
            check("…and goes once the server answers",
                  upload_status()["waiting"] == 0 and len(table) == 2)
            try:
                os.environ.pop("SUPABASE_SERVICE_KEY", None)
                cloud_rows("select=*")
                check("reading without the service key is refused", False)
            except SystemExit:
                check("reading without the service key is refused", True)
            os.environ["SUPABASE_SERVICE_KEY"] = "SERVICE"
            r = cloud_run("keeper")
            check("a cloud session reads the run back as a timeline",
                  [c["tool"] for c in r["calls"]] ==
                  ["export_preview", "get_content"]
                  and "IMAGE DID NOT LOAD" in timeline(r), timeline(r))
            check("…and lists the caller",
                  cloud_callers()[0]["caller"] == "keeper")
        finally:
            srv.shutdown()
            for k in ("AETHRON_SUPABASE_URL", "AETHRON_SUPABASE_ANON_KEY",
                      "SUPABASE_SERVICE_KEY"):
                os.environ.pop(k, None)
    print(f"\n{ok}/{ok + fail} run-record checks passed")
    return 0 if not fail else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--cloud" in sys.argv:            # what the Mac uploaded
        rest = [a for a in sys.argv[1:] if a != "--cloud"]
        if rest:
            print(timeline(cloud_run(rest[0], rest[1] if len(rest) > 1
                                     else "")))
        else:
            for c in cloud_callers():
                print(c["caller"])
                for r in c["runs"][:10]:
                    print(f"  {r['run']}  {r['calls']} call(s), "
                          f"{r['problems']} problem(s)  "
                          f"{', '.join(r['projects'])}  [{r['machine']}]")
        sys.exit(0)
    if "--upload" in sys.argv:           # on | off | now
        k = sys.argv.index("--upload")
        what = sys.argv[k + 1] if len(sys.argv) > k + 1 else "status"
        if what in ("on", "off"):
            set_upload(what == "on")
        elif what == "now":
            UPLOAD.flush()
        print(json.dumps(upload_status(), indent=1))
        sys.exit(0)
    if len(sys.argv) > 1:
        print(timeline(get_run(sys.argv[1], sys.argv[2]
                               if len(sys.argv) > 2 else "")))
    else:
        for c in callers():
            print(c["caller"])
            for r in c["runs"][:10]:
                print(f"  {r['run']}  {r['calls']} call(s), "
                      f"{r['problems']} problem(s)  "
                      f"{', '.join(r['projects'])}")

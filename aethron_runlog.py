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
        except Exception as e:       # a record must never break a call
            sys.stderr.write(f"run record not written: {e}\n")


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
    print(f"\n{ok}/{ok + fail} run-record checks passed")
    return 0 if not fail else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
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

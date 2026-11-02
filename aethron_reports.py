"""Failure reports: when something goes wrong for a person, we get told.

Whenever a step fails in someone's copy of Aethron (a fetch, build, verify
or browser check, an agent tool that failed, an agent turn that ended in
an error, a crash, an error on the screen), this writes a report and sends
it to Aethron's Supabase project, so the cause can be found and fixed
without waiting for the person to describe it.

A report carries what is needed to reproduce the failure:
  * what failed (kind, step, error category) and the exact FAIL/ERROR lines;
  * the end of the step's log and any traceback;
  * the template: its public address, platform, page count;
  * the app version, macOS version and architecture;
  * a random id for the computer (never its name), and a fingerprint, so
    the same failure on many machines is counted as one problem.
It never carries API keys or tokens (they are hidden by the same rules as
the agent run records), and home-folder names are replaced by "~".

Reports are always on in the released app. Running from source (tests,
development) they are off unless AETHRON_REPORTS=on; AETHRON_REPORTS=off
turns them off anywhere. Each report is kept on the computer
(<AETHRON_HOME>/reports/, the last 200) and queued in an outbox that is
sent when the computer is online, so nothing is lost offline.

Reading them needs the SERVICE key (the app's public key can only add):
    python3 aethron_reports.py --cloud [days]        grouped by problem
    python3 aethron_reports.py --cloud --html out.html
    python3 aethron_reports.py --show <fingerprint>  every report of one
"""
import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import aethron_runlog as RL

TABLE = "failure_reports"
KEEP = 200                  # reports kept on the computer
THROTTLE = 600              # the same problem is sent once per 10 minutes
TAIL = 120                  # log lines kept
CUT = 16000                 # longest log/trace kept, in characters
FINDING = re.compile(
    r"(\bFAIL(?:ED)?\b|\bERROR\b|\bREJECTED\b|\bSTUCK\b|Traceback|"
    r"\w*Error\b:|NOT ACCEPTED|IMAGE DID NOT LOAD|PICTURE MISSING|"
    r"timed out|could not|refused)", re.I)
_LAST = {}                  # fingerprint -> time last sent
_LOCK = threading.Lock()


def enabled() -> bool:
    v = os.environ.get("AETHRON_REPORTS", "").strip().lower()
    if v in ("0", "off", "no", "false"):
        return False
    if v in ("1", "on", "yes", "true"):
        return True
    return bool(getattr(sys, "frozen", False))


def home() -> Path:
    return RL.home()


def reports_dir() -> Path:
    return home() / "reports"


UPLOAD = RL._Uploader(TABLE, lambda: reports_dir() / ".outbox.jsonl")


def _app_version() -> str:
    try:
        import aethron_update
        return str(aethron_update.VERSION)
    except Exception:
        return "?"


def _os() -> str:
    try:
        if sys.platform == "darwin":
            return "macOS " + platform.mac_ver()[0]
        return platform.platform(terse=True)
    except Exception:
        return sys.platform


def _scrub(s: str) -> str:
    """Hide secrets and the person's home-folder name."""
    s = str(s or "")
    s = re.sub(r"/Users/[^/\s'\"]+", "~", s)
    s = re.sub(r"/home/[^/\s'\"]+", "~", s)
    s = re.sub(r"C:\\Users\\[^\\\s'\"]+", "~", s)
    return RL._SECRET_VAL.sub("[hidden]", s)


def findings(log: str, limit=40) -> list:
    out = []
    for line in str(log or "").splitlines():
        line = line.strip()
        if line.startswith("$ "):        # the command that was run
            continue
        if line and FINDING.search(line) and line not in out:
            out.append(line[:400])
        if len(out) >= limit:
            break
    return out


def category(text: str) -> str:
    """A short, stable name for a failure: numbers, addresses and quoted
    words removed, so the same fault in two projects reads the same."""
    t = str(text or "").strip()
    t = re.sub(r"https?://\S+", "<url>", t)
    t = re.sub(r"(['\"`]).*?\1", "<…>", t)
    t = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", t)
    t = re.sub(r"\d+(\.\d+)?", "N", t)
    t = re.sub(r"\s+", " ", t)
    return t[:160]


def template_of(project) -> dict:
    """The template behind a project: its public address and shape."""
    if not project:
        return {}
    d = Path(project)
    out = {"project": d.name}
    try:
        cfg = json.loads((d / "forge.json").read_text(encoding="utf-8"))
        out.update({
            "source_url": cfg.get("source_url") or "",
            "platform": cfg.get("platform") or "",
            "pages": len(cfg.get("pages") or []),
            "own_hosts": list(cfg.get("own_hosts") or [])[:4],
        })
    except (OSError, ValueError):
        pass
    try:
        cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
        st = cm.get("strings") or []
        out["lines"] = len(st)
        out["filled"] = sum(1 for e in st if e.get("new"))
    except (OSError, ValueError):
        pass
    return out


def report(kind: str, step: str = "", project=None, log: str = "",
           error: str = "", trace: str = "", extra: dict = None,
           force: bool = False) -> dict:
    """Record one failure. Never raises: a failure report must not become
    a second failure. Returns the report (or {} when reports are off)."""
    try:
        if not (enabled() or force):
            return {}
        log = _scrub(log)
        found = findings(log) or ([_scrub(error)[:400]] if error else [])
        first = found[0] if found else (_scrub(error) or step or kind)
        cat = category(first)
        fp = hashlib.sha1(f"{kind}|{step}|{cat}".encode()).hexdigest()[:12]
        rec = {
            "fingerprint": fp, "kind": kind[:40], "step": str(step)[:80],
            "category": cat, "error": _scrub(error)[:2000],
            "findings": found,
            "log_tail": "\n".join(log.splitlines()[-TAIL:])[-CUT:],
            "trace": _scrub(trace)[-CUT:],
            "template": template_of(project),
            "app_version": _app_version(), "os": _os(),
            "arch": platform.machine(), "machine": RL.machine_id(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "extra": RL.clean(extra or {}),
        }
        d = reports_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rec['at'].replace(':', '')}-{fp}-{os.urandom(2).hex()}.json").write_text(
            json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        olds = sorted(d.glob("*.json"))
        for f in olds[:-KEEP]:
            f.unlink(missing_ok=True)
        with _LOCK:
            last = _LAST.get(fp, 0)
            if not force and time.time() - last < THROTTLE:
                return rec            # kept locally; already sent recently
            _LAST[fp] = time.time()
        UPLOAD.enqueue({
            "machine": rec["machine"], "fingerprint": fp, "kind": rec["kind"],
            "step": rec["step"], "category": cat[:160],
            "app_version": rec["app_version"][:40],
            "source_url": str(rec["template"].get("source_url", ""))[:400],
            "record": rec})
        return rec
    except Exception:
        return {}


def report_exception(kind: str, step: str = "", project=None,
                     extra: dict = None) -> dict:
    """Call inside an `except` block."""
    e = sys.exc_info()[1]
    # the other side hung up (a tab reloaded while a file was being sent):
    # not a fault, and it was most of what came in
    if kind == "crash" and isinstance(e, (BrokenPipeError,
                                          ConnectionResetError,
                                          ConnectionAbortedError)):
        return {}
    return report(kind, step, project, error=f"{type(e).__name__}: {e}",
                  trace=traceback.format_exc(), extra=extra)


def status() -> dict:
    out = reports_dir() / ".outbox.jsonl"
    try:
        waiting = sum(1 for _ in out.open(encoding="utf-8"))
    except OSError:
        waiting = 0
    return {"on": enabled(), "waiting": waiting, "sent": UPLOAD.sent,
            "last_error": UPLOAD.last_error,
            "kept": len(list(reports_dir().glob("*.json")))
            if reports_dir().exists() else 0}


# ──────────────────────────── reading them ───────────────────────────

def cloud_rows(query: str) -> list:
    import urllib.request
    url = (os.environ.get("AETHRON_SUPABASE_URL") or RL._cloud()[0]).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not (url and key):
        raise SystemExit("set AETHRON_SUPABASE_URL and SUPABASE_SERVICE_KEY "
                         "(the service key lives in this environment's "
                         "secrets, never in the repo)")
    req = urllib.request.Request(url + f"/rest/v1/{TABLE}?" + query,
                                 headers={"apikey": key,
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def problems(rows: list) -> list:
    """Reports grouped by fingerprint, most machines first."""
    g = {}
    for r in rows:
        rec = r.get("record") or {}
        x = g.setdefault(r["fingerprint"], {
            "fingerprint": r["fingerprint"], "kind": r.get("kind", ""),
            "step": r.get("step", ""), "category": r.get("category", ""),
            "count": 0, "machines": set(), "versions": set(),
            "templates": set(), "first": r.get("received_at", ""),
            "last": r.get("received_at", ""), "example": rec})
        x["count"] += 1
        x["machines"].add(r.get("machine", ""))
        x["versions"].add(r.get("app_version", ""))
        if r.get("source_url"):
            x["templates"].add(r["source_url"])
        ts = r.get("received_at", "")
        if ts and ts < x["first"]:
            x["first"] = ts
        if ts and ts > x["last"]:
            x["last"], x["example"] = ts, rec
    out = []
    for x in g.values():
        for k in ("machines", "versions", "templates"):
            x[k] = sorted(v for v in x[k] if v)
        out.append(x)
    return sorted(out, key=lambda x: (-len(x["machines"]), -x["count"]))


def cloud_problems(days: int = 14) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() - days * 86400))
    return problems(cloud_rows(
        "select=received_at,machine,fingerprint,kind,step,category,"
        f"app_version,source_url,record&received_at=gte.{since}"
        "&order=id.desc&limit=5000"))


def html_page(probs: list, days: int) -> str:
    import html as H
    rows = []
    for p in probs:
        ex = p["example"] or {}
        f = "".join(f"<li>{H.escape(x)}</li>" for x in ex.get("findings", [])[:8])
        t = "".join(f"<li><a href='{H.escape(u)}'>{H.escape(u)}</a></li>"
                    for u in p["templates"][:6])
        rows.append(f"""<details class=p><summary><b>{len(p['machines'])}</b>
<span>machines · {p['count']} reports</span><code>{H.escape(p['kind'])}/{H.escape(p['step'])}</code>
<em>{H.escape(p['category'])}</em></summary>
<div class=b><p>Versions {H.escape(', '.join(p['versions']))} · first {H.escape(p['first'][:16])} · last {H.escape(p['last'][:16])} · <code>{p['fingerprint']}</code></p>
<h4>What failed</h4><ul>{f or '<li>—</li>'}</ul>
<h4>Templates</h4><ul>{t or '<li>—</li>'}</ul>
<h4>Last log</h4><pre>{H.escape(ex.get('log_tail', '')[-4000:])}</pre>
{f"<h4>Trace</h4><pre>{H.escape(ex.get('trace', '')[-3000:])}</pre>" if ex.get('trace') else ''}
</div></details>""")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Aethron failure reports</title><style>
body{{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;background:#fafafa;color:#111}}
main{{max-width:1000px;margin:0 auto;padding:32px 16px}}h1{{letter-spacing:-.03em}}
.p{{background:#fff;border:1px solid #e6e6e6;border-radius:14px;margin:10px 0}}
summary{{cursor:pointer;padding:14px 16px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}}
summary b{{font-size:22px}}summary span{{color:#777}}summary em{{flex-basis:100%;color:#333;font-style:normal}}
code{{font:12.5px ui-monospace,Menlo,monospace;background:#f1f1f1;padding:2px 6px;border-radius:6px}}
.b{{padding:0 16px 16px}}pre{{background:#151515;color:#eee;padding:12px;border-radius:10px;overflow:auto;font-size:12px}}
h4{{margin:14px 0 4px}}</style><main><h1>Failure reports</h1>
<p>Last {days} days · {len(probs)} problems · {sum(p['count'] for p in probs)} reports. Grouped by cause; most machines first.</p>
{''.join(rows) or '<p>No reports. 🎉</p>'}</main>"""


def selftest() -> int:
    import tempfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print(("PASS " if cond else "FAIL ") + name)

    got = []

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if self.path.endswith("/" + TABLE) and \
                    self.headers.get("apikey") == "anon":
                got.extend(json.loads(self.rfile.read(n)))
                self.send_response(201)
            else:
                self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    saved = {k: os.environ.get(k) for k in
             ("AETHRON_HOME", "AETHRON_REPORTS", "AETHRON_SUPABASE_URL",
              "AETHRON_SUPABASE_ANON_KEY")}
    with tempfile.TemporaryDirectory() as d:
        try:
            os.environ.update({
                "AETHRON_HOME": d,
                "AETHRON_SUPABASE_URL": f"http://127.0.0.1:{srv.server_port}",
                "AETHRON_SUPABASE_ANON_KEY": "anon"})
            os.environ["AETHRON_REPORTS"] = "off"
            check("off when told: nothing written",
                  report("job", "build", log="FAIL x") == {}
                  and not reports_dir().exists())
            os.environ.pop("AETHRON_REPORTS")
            check("off by default when run from source", not enabled())
            os.environ["AETHRON_REPORTS"] = "on"
            p = Path(d) / "projects" / "demo"
            p.mkdir(parents=True)
            (p / "forge.json").write_text(json.dumps({
                "source_url": "https://kaviyan.framer.website/",
                "platform": "framer", "pages": ["index.html", "about.html"]}))
            (p / "copy_map.json").write_text(json.dumps(
                {"strings": [{"old": "a", "new": "b"}, {"old": "c"}]}))
            log = ("$ build\nbuilding 2 pages\n"
                   "  FAIL about.html: renders blank (12 of 5400 chars)\n"
                   "using key sk-abcdefghijklmnopqrstuvwxyz123456\n"
                   "File /Users/temple/Library/Application Support/x\n")
            r = report("job", "probe", p, log=log)
            check("the template's address travels with the report",
                  r["template"]["source_url"] == "https://kaviyan.framer.website/"
                  and r["template"]["pages"] == 2 and r["template"]["filled"] == 1)
            check("the FAIL line is the finding",
                  r["findings"][0].startswith("FAIL about.html"))
            body = json.dumps(r)
            check("no key leaves the computer",
                  "sk-abcdefghijklmnopqrstuvwxyz" not in body)
            check("the home-folder name is hidden",
                  "/Users/temple" not in body and "~/Library" in body)
            r2 = report("job", "probe", p, log=log.replace("12 of 5400", "9 of 6100"))
            check("the same fault in other numbers is one problem",
                  r2["fingerprint"] == r["fingerprint"])
            check("kept on the computer", len(list(reports_dir().glob("*.json"))) == 2)
            check("sent at most once per 10 minutes (one queued)",
                  status()["waiting"] == 1)
            UPLOAD.flush(5)
            check("sent to the reports table with the public key",
                  len(got) == 1 and got[0]["fingerprint"] == r["fingerprint"]
                  and got[0]["source_url"].startswith("https://kaviyan"))
            check("outbox empty once sent", status()["waiting"] == 0)
            try:
                raise ValueError("boom 42")
            except ValueError:
                r3 = report_exception("crash", "studio")
            check("a crash carries its trace", "ValueError: boom" in r3["trace"]
                  and r3["fingerprint"] != r["fingerprint"])
            g = problems([{"fingerprint": "a", "machine": "m1", "record": {},
                           "received_at": "2026-10-26T10"},
                          {"fingerprint": "a", "machine": "m2", "record": {},
                           "received_at": "2026-10-26T11"},
                          {"fingerprint": "b", "machine": "m1", "record": {},
                           "received_at": "2026-10-26T12"}])
            check("grouped by problem, most machines first",
                  g[0]["fingerprint"] == "a" and len(g[0]["machines"]) == 2)
            check("the admin page renders", "Failure reports" in html_page(g, 14))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            srv.shutdown()
    print(f"\n{ok}/{ok + fail} failure-report checks passed")
    return 1 if fail else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a:
        sys.exit(selftest())
    if "--cloud" in a:
        nums = [x for x in a if x.isdigit()]
        days = int(nums[0]) if nums else 14
        probs = cloud_problems(days)
        if "--html" in a:
            out = Path(a[a.index("--html") + 1])
            out.write_text(html_page(probs, days), encoding="utf-8")
            print(f"wrote {out} ({len(probs)} problems)")
        else:
            for p in probs:
                print(f"{len(p['machines']):>3} machines {p['count']:>4}x  "
                      f"{p['kind']}/{p['step']}  {p['category']}  "
                      f"[{p['fingerprint']}]")
        sys.exit(0)
    if "--show" in a:
        fp = a[a.index("--show") + 1]
        for r in cloud_rows(f"select=received_at,record&fingerprint=eq.{fp}"
                            "&order=id.desc&limit=20"):
            print(json.dumps(r, indent=1, ensure_ascii=False))
        sys.exit(0)
    print(json.dumps(status(), indent=1))

#!/usr/bin/env python3
"""Play the lead agent ("the keeper") against the REAL MCP server and read
back what the run record caught.

    python3 tests/keeper_battery.py [template-project-or-path]

The case that asked for this: a lead preview made from Progrex lost its
hero image and nobody could tell whether Aethron or the agent did it. So
the keeper here does what an agent plausibly does — swaps the hero for a
photo of the lead's business — and the photo then stops loading (an
expiring Google Places address does exactly that). Aethron must:
  * refuse an image address that does not load, at fill time;
  * FAIL the preview when a picture dies after the fill, naming it;
  * record every call, per caller and session, with no secret in it;
  * hand the record back through list_runs / get_run.
The HTTP leg needs no template: a label per token, a run per session.

Template: a prepared project (copy_map.json present). Default
projects/agero; with none, the template half reports SKIPPED.
"""
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = sys.argv[1] if len(sys.argv) > 1 else "agero"
checks = []


def _png(w=64, h=40):
    """A real PNG, built here: a hand-typed one had a bad checksum, the
    browser refused it, and the check was right while the test was wrong."""
    import struct
    import zlib

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    raw = b"".join(b"\x00" + b"\xd9\x77\x57" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png()


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name
          + ("" if ok else f"   {str(detail)[:400]}"), flush=True)


class Stdio:
    """An agent's side of the stdio MCP connection."""

    def __init__(self, env, client):
        self.p = subprocess.Popen([sys.executable, str(ROOT / "forge_mcp.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True,
                                  env=env, bufsize=1)
        self.replies, self.n = {}, 0
        threading.Thread(target=self._read, daemon=True).start()
        self.rpc("initialize", {"protocolVersion": "2024-11-05",
                                "clientInfo": client})

    def _read(self):
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m:
                self.replies[m["id"]] = m

    def rpc(self, method, params, timeout=900):
        self.n += 1
        i = self.n
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i,
                                       "method": method,
                                       "params": params}) + "\n")
        self.p.stdin.flush()
        t0 = time.time()
        while i not in self.replies:
            if time.time() - t0 > timeout:
                raise TimeoutError(f"{method}: no answer in {timeout}s")
            time.sleep(0.1)
        return self.replies.pop(i).get("result") or {}

    def call(self, tool, args, timeout=900):
        r = self.rpc("tools/call", {"name": tool, "arguments": args}, timeout)
        text = " ".join(c.get("text", "") for c in r.get("content", []))
        return not r.get("isError"), text, r.get("structuredContent")

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()


def hero_entries(tpl: Path) -> list:
    """The template's biggest picture on its home page, as copy-map olds."""
    html = (tpl / "pristine" / "index.html").read_text(errors="ignore")
    best, area = None, 0
    for m in re.finditer(r"<img\b[^>]*>", html):
        tag = m.group(0)
        w = re.search(r'\bwidth="(\d+)"', tag)
        h = re.search(r'\bheight="(\d+)"', tag)
        src = re.search(r'\bsrc="([^"]+)"', tag)
        if w and h and src and int(w[1]) * int(h[1]) > area:
            best, area = src[1].split("?")[0], int(w[1]) * int(h[1])
    cm = json.loads((tpl / "copy_map.json").read_text())
    return [e["old"] for e in cm["images"] if best and
            e["old"].split("?")[0] == best]


def template_half(home: Path, env: dict):
    src = Path(TEMPLATE) if "/" in TEMPLATE else ROOT / "projects" / TEMPLATE
    if not (src / "copy_map.json").exists():
        print(f"SKIPPED — no prepared template at {src} (the template half "
              "is UNVERIFIED, not passed)")
        return False
    sys.path.insert(0, str(ROOT))
    import forge
    if not forge._find_browser():
        print("SKIPPED — no browser: the preview check cannot run "
              "(UNVERIFIED, not passed)")
        return False
    shutil.copytree(src, home / "projects" / "tpl",
                    ignore=shutil.ignore_patterns("site", ".history",
                                                  "convert-*", "export-*"),
                    copy_function=lambda a, b: os.link(a, b)
                    if "pristine" in Path(a).parts else shutil.copy2(a, b))
    hero = hero_entries(home / "projects" / "tpl")
    ck("the template's hero picture is found in its copy map", hero, hero)

    # the lead's "business photo", served while the keeper fills…
    class Img(BaseHTTPRequestHandler):
        def do_GET(s):
            if s.path.startswith("/lead-photo.png"):
                s.send_response(200)
                s.send_header("Content-Type", "image/png")
                s.send_header("Content-Length", str(len(PNG)))
                s.end_headers()
                s.wfile.write(PNG)
            else:
                s.send_response(404)
                s.send_header("Content-Length", "0")
                s.end_headers()

        def log_message(s, *a):
            pass
    img = ThreadingHTTPServer(("127.0.0.1", 0), Img)
    threading.Thread(target=img.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{img.server_address[1]}"

    k = Stdio(env, {"name": "keeper scenario", "version": "1"})
    try:
        ok, out, _ = k.call("make_preview", {"template": "tpl",
                                             "name": "Crux Climbing",
                                             "pages": ["home"],
                                             "api_key": "AIza" + "x" * 35})
        ck("make_preview", ok, out)
        ok, out, data = k.call("set_content_bulk", {
            "project": "crux-climbing", "build": False,
            "entries": [{"section": "images", "old": hero[0],
                         "new": base + "/gone-already.jpg"}]})
        ck("a hero address that does not load is refused, with the reason",
           ok and data and data["applied"] == 0 and data["rejected"]
           and "404" in data["rejected"][0]["reason"], out)
        ok, out, data = k.call("set_content_bulk", {
            "project": "crux-climbing", "build": False,
            "entries": [{"section": "images", "old": o,
                         "new": base + "/lead-photo.png"} for o in hero]})
        ck("a hero address that loads is taken", ok and data
           and data["applied"] == len(hero), out)
        ok, out, data = k.call("export_preview", {"project": "crux-climbing"})
        ck("while the photo loads, the preview passes",
           ok and data and data["verdict"] == "PASS", out)
        img.shutdown()                 # …and then the photo stops loading
        img.server_close()
        ok, out, data = k.call("export_preview", {"project": "crux-climbing"})
        dead = [im for v in (data or {}).get("pages", {}).values()
                for im in v.get("dead_images", [])]
        ck("when the photo dies the preview FAILS, naming the picture",
           data and data["verdict"] == "FAIL" and dead
           and "lead-photo.png" in dead[0]["src"]
           and "IMAGE DID NOT LOAD" in out, out)
        ck("…and says it is the biggest picture on the page",
           "the biggest on the page" in out, out)
    finally:
        k.close()

    runs = home / "runs"
    files = list(runs.glob("keeper-scenario/*.jsonl"))
    ck("the run is recorded under the caller's own name", len(files) == 1,
       [str(f) for f in runs.rglob("*.jsonl")])
    if not files:
        return True
    raw = files[0].read_text()
    recs = [json.loads(x) for x in raw.splitlines()]
    calls = [r for r in recs if r["kind"] == "call"]
    ck("every call is in it, in order",
       [c["tool"] for c in calls] == ["make_preview", "set_content_bulk",
                                      "set_content_bulk", "export_preview",
                                      "export_preview"],
       [c["tool"] for c in calls])
    ck("no key reaches the record", "AIza" not in raw and "[hidden]" in raw)
    ck("the refused fill is a problem with its reason",
       calls[1]["status"] == "partial"
       and "404" in json.dumps(calls[1]["result"]), calls[1])
    ck("the dead picture is a FINDING on the failed export",
       calls[4]["status"] == "failed" and any(
           "IMAGE DID NOT LOAD" in f for f in calls[4]["result"]["findings"]),
       calls[4])
    ck("time taken is recorded", all(c["ms"] >= 0 for c in calls)
       and calls[3]["ms"] > 500, [c["ms"] for c in calls])

    # an agent reads it back
    k2 = Stdio(env, {"name": "the next session"})
    try:
        ok, out, data = k2.call("list_runs", {})
        ck("list_runs names the caller and counts its problems",
           ok and "keeper-scenario" in out and data and any(
               r["problems"] >= 2 for c in data["callers"]
               for r in c["runs"]), out)
        ok, out, data = k2.call("get_run", {"caller": "keeper-scenario"})
        ck("get_run gives the timeline with the finding",
           ok and "FAIL" in out and "IMAGE DID NOT LOAD" in out, out)
    finally:
        k2.close()
    return True


def http_half(home: Path, env: dict):
    keep, other = secrets.token_hex(16), secrets.token_hex(16)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    e = dict(env, AETHRON_MCP_TOKENS=f"keeper={keep},somebody={other}")
    e.pop("AETHRON_MCP_TOKEN", None)
    srv = subprocess.Popen([sys.executable, str(ROOT / "forge_mcp.py"),
                            "--http", str(port)], env=e,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"

    def post(tok, body, sid=""):
        h = {"Content-Type": "application/json",
             "Authorization": f"Bearer {tok}"}
        if sid:
            h["Mcp-Session-Id"] = sid
        req = urllib.request.Request(url, json.dumps(body).encode(), h)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.headers.get("Mcp-Session-Id", ""), json.loads(
                r.read() or b"null")
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                       timeout=2)
                break
            except Exception:
                time.sleep(0.2)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "claims to be Aethron"}}}
        sid1, _ = post(keep, init)
        ck("initialize hands out a session id", len(sid1) >= 8, sid1)
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_projects",
                           "arguments": {"token": keep}}}
        post(keep, call, sid1)
        post(keep, call, sid1)
        sid2, _ = post(keep, init)
        post(keep, call, sid2)
        sid3, _ = post(other, init)
        post(other, call, sid3)
        k = sorted((home / "runs" / "keeper").glob("*.jsonl"))
        o = list((home / "runs" / "somebody").glob("*.jsonl"))
        ck("the caller is the token's label, not what the agent claims",
           k and o and not (home / "runs" / "claims-to-be-Aethron").exists(),
           [str(p) for p in (home / "runs").rglob("*")])
        counts = sorted(sum(1 for x in f.read_text().splitlines()
                            if '"kind": "call"' in x) for f in k)
        ck("one run per session, never mixed", counts == [1, 2], counts)
        raw = "".join(f.read_text() for f in k + o)
        ck("no token reaches the record", keep not in raw and other not in raw)
    finally:
        srv.terminate()
        srv.wait(10)


def main():
    home = Path(tempfile.mkdtemp(prefix="ae-keeper-"))
    (home / "projects").mkdir()
    env = dict(os.environ, AETHRON_HOME=str(home))
    env.pop("AETHRON_RUNLOG", None)
    try:
        print("── HTTP: callers and sessions")
        http_half(home, env)
        print("── the keeper loses a hero image")
        ran = template_half(home, env)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} keeper checks passed"
          + ("" if ran else " — template half SKIPPED (UNVERIFIED)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

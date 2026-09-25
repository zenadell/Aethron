#!/usr/bin/env python3
"""Migrating a REGULAR site — not Framer, not Webflow — from its live URL.

    python3 tests/site_battery.py

A fake site is served on this machine with every ingredient the owner's
own site had when it was first migrated (2026-09-25) and came out
unstyled with empty sections:

  * a stylesheet on the site's own server, not a template CDN
  * content that arrives from the site's own /api/ AFTER the page opens,
    drawn by the site's own script
  * code split into an on-demand chunk whose NAME the script computes
    (no scan of the markup can see it)
  * a private endpoint the page calls on load (a token)
  * a contact form that only POSTs
  * a file the original site itself is missing

and the migration must: copy what belongs to the site, record the data so
the site's own script draws it unchanged, never copy the private one,
name what needs a server, make the data's words editable, and land an
edit to them on screen. No network, no key.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORGE = ROOT / "forge.py"
sys.path.insert(0, str(ROOT))
checks = []


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))


PAGE = """<!doctype html><html><head><title>Northwind Studio</title>
<link rel="stylesheet" href="/css/site.css">
<link rel="canonical" href="{origin}/"></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<h1>Northwind Studio builds calm software</h1>
<section id="services"><div class="skeleton">loading…</div></section>
<form id="contact"><input name="email"><button>Send</button></form>
<img src="/uploads/missing-on-origin-too.jpg" alt="">
<script src="/js/app.js"></script></body></html>"""
ABOUT = PAGE.replace("builds calm software", "was founded in a small room")
CSS = "body{font-family:sans-serif;background:#f4efe6}h1{color:#223}"
APP = """
(function(){
  // the chunk name is COMPUTED — no markup scan can find it
  var s=document.createElement('script');
  s.src='/js/'+['app','lazy','7f3a9c21'].join('.')+'.js';
  document.head.appendChild(s);
  fetch('/api/session-token').catch(function(){});
  fetch('/api/services').then(function(r){return r.json()}).then(function(d){
    var box=document.getElementById('services'); box.innerHTML='';
    d.items.forEach(function(it){
      var h=document.createElement('h3'); h.textContent=it.title;
      box.appendChild(h);});
  });
  document.getElementById('contact').onsubmit=function(e){
    e.preventDefault(); fetch('/api/contact',{method:'POST'});};
})();"""
LAZY = "window.__lazyLoaded = true;"
SERVICES = {"items": [{"slug": "brand-work", "title": "Brand identity for founders"},
                      {"slug": "web-apps", "title": "Web applications that stay fast"}]}
hits = []


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        hits.append(self.path)
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(200, PAGE.format(origin=origin), "text/html")
        if p == "/about":
            return self._send(200, ABOUT.format(origin=origin), "text/html")
        if p == "/css/site.css":
            return self._send(200, CSS, "text/css")
        if p == "/js/app.js":
            return self._send(200, APP, "application/javascript")
        if p == "/js/app.lazy.7f3a9c21.js":
            return self._send(200, LAZY, "application/javascript")
        if p == "/api/services":
            return self._send(200, json.dumps(SERVICES), "application/json")
        if p == "/api/session-token":
            return self._send(200, '{"token":"sk-SECRET-must-never-be-copied"}',
                              "application/json")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        self._send(405, "", "text/plain")


def forge(cwd, *args, timeout=900):
    r = subprocess.run([sys.executable, str(FORGE), *args], cwd=cwd,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def main():
    import forge as F
    if not F._find_browser():
        print("SKIPPED — no browser: runtime learning cannot be exercised "
              "(UNVERIFIED, not passed)")
        return 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    work = Path(tempfile.mkdtemp(prefix="aethron-site-"))
    try:
        code, out = forge(work, "init", url, "--name", "northwind")
        ck("init takes a regular site from its URL",
           code == 0 and "STATIC" in out, out[-300:])
        proj = work / "northwind"
        code, out = forge(proj, "fetch")
        ck("fetch downloads the site's own stylesheet",
           any(fn.endswith(".css") for fn in json.loads(
               (proj / "forge.json").read_text())["localized"].values()),
           out[-400:])
        cfg = json.loads((proj / "forge.json").read_text())
        ck("fetch leaves no stand-in copy map behind (inventory writes it)",
           not (proj / "copy_map.json").exists())
        # it may land in the download map (next to a localized script) or
        # as a site file at its own path — either way it must be SHIPPED
        ck("the on-demand chunk whose name is computed was learned",
           "app.lazy.7f3a9c21.js" in cfg["localized"].values()
           or (proj / "pristine" / "js" / "app.lazy.7f3a9c21.js").is_file(),
           str(cfg["localized"]))
        ck("the API data was recorded as data",
           "api/services" in cfg.get("recorded_data", []),
           str(cfg.get("recorded_data")))
        secret = (proj / "pristine" / "api" / "session-token")
        ck("the private token endpoint was NEVER copied",
           not secret.exists() and not any("sk-SECRET" in p.read_text(
               errors="ignore") for p in (proj / "pristine").rglob("*")
               if p.is_file() and p.stat().st_size < 2_000_000),
           str(secret))
        ck("…and is named as needing a server",
           "/api/session-token" in cfg.get("backend_only", []),
           str(cfg.get("backend_only")))
        ck("the POST-only contact endpoint is named as needing a server",
           "/api/contact" in cfg.get("backend_only", []),
           str(cfg.get("backend_only")))
        ck("a file the original is missing too is recorded as such",
           "/uploads/missing-on-origin-too.jpg" in cfg.get(
               "missing_upstream", []), str(cfg.get("missing_upstream")))

        code, out = forge(proj, "inventory")
        cm = json.loads((proj / "copy_map.json").read_text())
        olds = {e["old"] for e in cm["strings"]}
        ck("the data's words are editable copy",
           "Brand identity for founders" in olds, sorted(olds)[:20])
        ck("the data's lookup keys are NOT offered as copy",
           "brand-work" not in olds and "web-apps" not in olds)

        for e in cm["strings"]:
            if e["old"] == "Brand identity for founders":
                e["new"] = "Naming and identity for new companies"
        (proj / "copy_map.json").write_text(json.dumps(cm, indent=1))
        code, out = forge(proj, "build")
        shipped = (proj / "site" / "api" / "services").read_text()
        ck("an edit to the data lands in the shipped data",
           "Naming and identity for new companies" in shipped
           and "Brand identity for founders" not in shipped, shipped[:200])

        code, out = forge(proj, "verify")
        ck("verify: nothing missing but what the original lacks too",
           "carried, not caused" in out
           and "referenced asset(s) missing" not in out, out[-600:])

        code, out = forge(proj, "probe", "--all")
        ck("probe: the site's own script draws the EDITED data on screen",
           code == 0 or "FAIL" not in out.split("VERDICT")[0]
           .replace("NOTE", ""), out[-900:])
        dom = F._render_page(F._find_browser(),
                             _serve(proj / "site") + "/index.html",
                             budget_ms=6000).get("dom", "")
        ck("…and it reads the new words, not the skeleton",
           "Naming and identity for new companies" in dom
           and "loading…" not in dom, dom[:300])
        ck("probe names the server-only endpoints instead of failing them",
           "need the original site's server" in out, out[-600:])
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


_SERVERS = []


def _serve(site: Path) -> str:
    import forge as F
    H = F._site_handler(site, "static", quiet=True)
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    _SERVERS.append(s)
    return f"http://127.0.0.1:{s.server_address[1]}"


if __name__ == "__main__":
    sys.exit(main())

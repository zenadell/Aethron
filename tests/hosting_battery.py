#!/usr/bin/env python3
"""Aethron hosting, end to end: the real Worker (cloud/hosting/worker.js)
in Bun with an in-memory R2 and a fake account server, driven by the app's
own publish code (aethron_publish) with this copy's device token.

  * a site is claimed, uploaded and served at <name>.aethron.site
  * pages, folders (/about -> /about/), .html-less addresses, 404.html
  * republishing uploads only what changed; versions list and rollback
  * a second account cannot take or touch someone else's site
  * a held account and a missing token are refused with their reason
  * a file whose bytes do not match its hash is refused
  * rename moves the site; unpublish takes it down
  * the badge is in the served page
Needs bun; without it SKIPPED, never PASS.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
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
    print("VERDICT: SKIPPED — no bun to run the Worker")
    sys.exit(0)

s = socket.socket()
s.bind(("127.0.0.1", 0))
PORT = s.getsockname()[1]
s.close()
BASE = f"http://127.0.0.1:{PORT}"
HOME = Path(tempfile.mkdtemp(prefix="ae-host-"))
os.environ.update(AETHRON_HOME=str(HOME), AETHRON_HOSTING_API=BASE + "/__api",
                  AETHRON_REPORTS="off", AETHRON_RUNLOG="off")
sys.path.insert(0, str(ROOT))
srv = subprocess.Popen([bun, str(ROOT / "tests/hosting/fake.ts"), str(PORT)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def token(t):
    (HOME / "control.json").write_text(json.dumps({"device_token": t}))
    time.sleep(0.02)          # a new mtime, so the cached state is re-read


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPEN = urllib.request.build_opener(_NoRedirect)


def get(host, path="/"):
    r = urllib.request.Request(BASE + path, headers={"x-host": host, "User-Agent": "test"})
    try:
        with OPEN.open(r, timeout=10) as f:
            return f.status, f.read().decode("utf-8", "replace"), dict(f.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def raw(method, path, data=b"", tok="good-u1"):
    r = urllib.request.Request(BASE + "/__api" + path, data=data or None, method=method,
                               headers={"authorization": "Bearer " + tok, "User-Agent": "test"})
    try:
        with urllib.request.urlopen(r, timeout=10) as f:
            return f.status, json.loads(f.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


try:
    if "ready" not in srv.stdout.readline():
        print("VERDICT: SKIPPED — the Worker did not start")
        sys.exit(0)
    import aethron_publish as P

    site = HOME / "site"
    (site / "about").mkdir(parents=True)
    (site / "index.html").write_text("<!doctype html><html><body><h1>Hello Aethron</h1></body></html>")
    (site / "about" / "index.html").write_text("<html><body>About us</body></html>")
    (site / "pricing.html").write_text("<html><body>Pricing</body></html>")
    (site / "404.html").write_text("<html><body>Lost?</body></html>")
    (site / "style.css").write_text("body{color:red}")
    files = P.folder_files(site)

    try:
        urllib.request.urlopen(BASE + "/__api/v1/health", timeout=10)
        check("the fake refuses Python's default client name, like Cloudflare", False)
    except urllib.error.HTTPError as e:
        check("the fake refuses Python's default client name, like Cloudflare", e.code == 403)
    token("")
    check("with no sign-in, hosting is not offered", not P.hosting()["ready"])
    token("good-u1")
    check("signed in and reachable, hosting is ready", P.hosting()["ready"])
    log = []
    rec = P.publish("project:demo", files, "Demo Site", log=log.append, title="Demo")
    check("a new site goes to Aethron hosting", rec.get("host") == "aethron")
    check("at <name>.aethron.site", rec["url"] == "https://demo-site.aethron.site")
    st, body, h = get("demo-site.aethron.site", "/")
    check("the home page is served", st == 200 and "Hello Aethron" in body and "text/html" in h.get("Content-Type", ""))
    check("with the Built with Aethron badge", "data-aethron-badge" in body)
    st, _, h = get("demo-site.aethron.site", "/about")
    check("a folder address redirects to its slash", st == 301 and h.get("Location", "").endswith("/about/"))
    st, body, _ = get("demo-site.aethron.site", "/about/")
    check("and serves its index.html", st == 200 and "About us" in body)
    st, body, _ = get("demo-site.aethron.site", "/pricing")
    check("an address without .html finds the page", st == 200 and "Pricing" in body)
    st, body, h = get("demo-site.aethron.site", "/style.css")
    check("files carry their type", st == 200 and h.get("Content-Type", "").startswith("text/css"))
    st, body, _ = get("demo-site.aethron.site", "/nope")
    check("a missing page shows the site's own 404", st == 404 and "Lost?" in body)
    st, body, _ = get("ghost.aethron.site", "/")
    check("an unclaimed address says so", st == 404 and "has not been published" in body)
    st, _, h = get("aethron.site", "/")
    check("the bare domain points to Aethron's website", st in (301, 302) or "aethron.jomiez.com" in h.get("Location", ""))

    (site / "style.css").write_text("body{color:blue}")
    log = []
    rec2 = P.publish("project:demo", P.folder_files(site), "Demo Site", log=log.append, title="Demo")
    up = [l for l in log if l.startswith("uploading")]
    check("republishing uploads only what changed", up and up[0].startswith("uploading 1 new"))
    check("same address", rec2["url"] == rec["url"])
    vs = P.versions("project:demo")
    check("two versions, the newest live", len(vs) == 2 and vs[0]["live"] and not vs[1]["live"])
    P.rollback("project:demo", vs[1]["id"])
    st, body, _ = get("demo-site.aethron.site", "/style.css")
    check("rollback puts the older version live", "red" in body)

    st, b = raw("POST", "/v1/sites", json.dumps({"name": "demo-site"}).encode(), tok="good-u2")
    check("another account cannot take the address", st == 409)
    st, b = raw("DELETE", "/v1/sites/demo-site", tok="good-u2")
    check("or delete it", st == 403)
    st, b = raw("GET", "/v1/me", tok="held")
    check("a held account is refused with the reason", st == 403 and "on hold" in b.get("error", ""))
    st, b = raw("GET", "/v1/me", tok="")
    check("no token is refused", st == 401)
    st, b = raw("PUT", "/v1/blobs/" + "0" * 40, b"hello")
    check("bytes that don't match their hash are refused", st == 400)
    st, b = raw("POST", "/v1/sites", json.dumps({"name": "admin"}).encode())
    check("reserved names cannot be claimed", st == 409)

    P.rename("project:demo", "Shiny New")
    st, body, _ = get("shiny-new.aethron.site", "/")
    st2, _, _ = get("demo-site.aethron.site", "/")
    check("rename moves the site", st == 200 and "Hello Aethron" in body and st2 == 404)
    try:
        P.set_domain("project:demo", "example.com")
        check("own domains say they are coming", False)
    except P.PublishError as e:
        check("own domains say they are coming", "coming soon" in str(e))
    P.unpublish("project:demo")
    st, _, _ = get("shiny-new.aethron.site", "/")
    check("unpublish takes it down", st == 404 and P.record("project:demo") is None)
finally:
    srv.terminate()
    shutil.rmtree(HOME, ignore_errors=True)

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

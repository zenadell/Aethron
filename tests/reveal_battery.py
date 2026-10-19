#!/usr/bin/env python3
"""A page that starts hidden and is revealed by a script whose address the
page BUILDS in code — then the brand fill renames that address.

    python3 tests/reveal_battery.py

Found live on 2026-10-19 (trymeridian.com, a Webflow site): the page ships
`main{opacity:0}` and loads its reveal script from
    `${PRODUCTION_URL}/main.js`   on   meridian-custom-code-….vercel.app
No scan of the markup sees that URL. The migration worked until the owner
filled the brand, which renamed the host too; the script 404'd and the
browser showed the nav, the footer and nothing in between.

A fake site and a fake "author's code host" are served on this machine.
The migration must own the reveal script, keep working after the brand is
filled, heal a project learned before this fix on its next build, and the
probe must FAIL (naming the script) when the script is gone. No network,
no key.
"""
import json
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


PARA = ("Kestrel helps small teams see how their brand shows up when people "
        "ask an assistant for advice, and what to change so it shows up more. ")
PAGE = """<!doctype html><html><head><title>Kestrel | Answers that name you</title>
<link rel="canonical" href="{origin}/">
<style>main{{opacity:0}}</style></head><body>
<nav><a href="/">Home</a></nav>
<main><h1>Kestrel makes your brand the answer</h1><p>{para}</p>
<p>{para}</p><p>{para}</p></main>
<footer>© 2026 Kestrel</footer>
<script>
const CODE_HOST = `{code}/kestrel-custom-code`
function addScript(src) {{ const s = document.createElement("script");
  s.setAttribute("src", src); document.body.appendChild(s); }}
addScript(`${{CODE_HOST}}/main.js`);
</script></body></html>"""
REVEAL = "document.querySelector('main').style.opacity='1';"


def handler(kind):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = self.path.split("?")[0]
            if kind == "code":
                if p == "/kestrel-custom-code/main.js":
                    return self._send(200, REVEAL, "application/javascript")
                return self._send(404, "nope", "text/plain")
            if p in ("/", "/index.html"):
                origin = f"http://127.0.0.1:{self.server.server_address[1]}"
                return self._send(200, PAGE.format(origin=origin, code=CODE,
                                                   para=PARA), "text/html")
            return self._send(404, "nope", "text/plain")
    return H


CODE = ""


def forge(cwd, *args, env=None, timeout=900):
    import os
    e = dict(os.environ, **(env or {}))
    r = subprocess.run([sys.executable, str(FORGE), *args], cwd=cwd,
                       capture_output=True, text=True, timeout=timeout, env=e)
    return r.returncode, r.stdout + r.stderr


def fill_brand(proj):
    cm = json.loads((proj / "copy_map.json").read_text())
    for e in cm["strings"]:
        if e["old"] == "Kestrel":
            e["new"] = "Jomiez"
        if e["old"] == "kestrel":
            e["new"] = "jomiez"
    (proj / "copy_map.json").write_text(json.dumps(cm, indent=1))


def main():
    global CODE
    import forge as F
    if not F._find_browser():
        print("SKIPPED — no browser: a reveal script cannot be exercised "
              "(UNVERIFIED, not passed)")
        return 0
    code_srv = ThreadingHTTPServer(("127.0.0.1", 0), handler("code"))
    threading.Thread(target=code_srv.serve_forever, daemon=True).start()
    CODE = f"http://127.0.0.1:{code_srv.server_address[1]}"
    site_srv = ThreadingHTTPServer(("127.0.0.1", 0), handler("site"))
    threading.Thread(target=site_srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{site_srv.server_address[1]}/"
    reveal_url = f"{CODE}/kestrel-custom-code/main.js"
    work = Path(tempfile.mkdtemp(prefix="aethron-reveal-"))
    try:
        forge(work, "init", url, "--name", "kestrel")
        proj = work / "kestrel"
        code, out = forge(proj, "fetch")
        cfg = json.loads((proj / "forge.json").read_text())
        ck("fetch keeps the script whose address the page builds in code",
           reveal_url in cfg.get("runtime_urls", [])
           and reveal_url in cfg.get("localized", {}), out[-500:])
        forge(proj, "inventory")
        cm = json.loads((proj / "copy_map.json").read_text())
        ck("the brand token is offered for filling",
           any(e["old"] == "kestrel" for e in cm["strings"]))
        fill_brand(proj)
        code, out = forge(proj, "build")
        html = (proj / "site" / "index.html").read_text()
        ck("the fill renamed the address in the page (ownership)",
           "jomiez-custom-code" in html and "kestrel-custom-code" not in
           html.split("data-aethron-runtime-map")[-1].split("</script>", 1)[1],
           html[:400])
        ck("…and the page maps the renamed address to its own copy",
           f"{CODE}/jomiez-custom-code/main.js" in html
           and "/assets/r/" in html.split("data-aethron-runtime-map")[1]
           .split("</script>")[0])
        code, out = forge(proj, "probe", "--all")
        ck("probe: the filled page is painted, not blank",
           "NOT ON SCREEN" not in out and "VERDICT: CLEAN" in out, out[-700:])

        # the author's host going away must not matter any more
        code_srv.shutdown()
        code_srv.server_close()
        code, out = forge(proj, "probe", "--all")
        ck("…even with the author's code host offline (owned, not rented)",
           "NOT ON SCREEN" not in out, out[-700:])

        # a project learned BEFORE this fix: no runtime_urls, no local copy
        cfg = json.loads((proj / "forge.json").read_text())
        cfg.pop("runtime_urls", None)
        cfg["localized"].pop(reveal_url, None)
        (proj / "forge.json").write_text(json.dumps(cfg, indent=1))
        code, out = forge(proj, "build", env={"AETHRON_NO_RELEARN": "1"})
        code, out = forge(proj, "probe", "--all")
        ck("probe FAILs the blank page the old migration shipped",
           "NOT ON SCREEN" in out and "VERDICT: CLEAN" not in out, out[-700:])
        ck("…and names the script that did not load",
           "a script the page needs did not load" in out
           and "jomiez-custom-code/main.js" in out, out[-700:])

        # the next normal build heals it (the author's host is back)
        code_srv2 = ThreadingHTTPServer(
            ("127.0.0.1", int(CODE.rsplit(":", 1)[1])), handler("code"))
        threading.Thread(target=code_srv2.serve_forever, daemon=True).start()
        code, out = forge(proj, "build")
        cfg = json.loads((proj / "forge.json").read_text())
        ck("an old project heals on its next build",
           reveal_url in cfg.get("runtime_urls", [])
           and reveal_url in cfg.get("localized", {}), out[-500:])
        cm2 = json.loads((proj / "copy_map.json").read_text())
        ck("…and keeps every fill it had",
           any(e["old"] == "kestrel" and e["new"] == "jomiez"
               for e in cm2["strings"]))
        code, out = forge(proj, "probe", "--all")
        ck("…and the page is painted again",
           "NOT ON SCREEN" not in out and "VERDICT: CLEAN" in out, out[-700:])
        code_srv2.shutdown()
        code_srv2.server_close()
    finally:
        site_srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

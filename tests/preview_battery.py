#!/usr/bin/env python3
"""Personalised previews, driven over MCP the way a lead agent drives them.

    python3 tests/preview_battery.py [template-project] [Aethron.app]

With an app, the MCP server is the PACKAGED one (`Aethron --mcp`): a tool
proven only from source is not proven in what people run.

A template migrated ONCE is copied per lead with only the chosen pages,
filled, given extras (a map, reviews), exported to live at
/preview/<slug> and checked in a browser AT THAT ADDRESS. Offline: the
template is a project already on this machine (default projects/agero, an
11-page Framer template); its original files are linked, not copied.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = sys.argv[1] if len(sys.argv) > 1 else "agero"
APP = Path(sys.argv[2]) if len(sys.argv) > 2 else None
checks = []


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {str(detail)[:400]}"))


def main():
    src = Path(TEMPLATE) if "/" in TEMPLATE else ROOT / "projects" / TEMPLATE
    if not (src / "copy_map.json").exists():
        print(f"SKIPPED — no prepared template at projects/{TEMPLATE} "
              "(UNVERIFIED, not passed)")
        return 0
    sys.path.insert(0, str(ROOT))
    import forge
    if not forge._find_browser():
        print("SKIPPED — no browser: the preview check cannot run "
              "(UNVERIFIED, not passed)")
        return 0
    home = Path(tempfile.mkdtemp(prefix="ae-preview-"))
    (home / "projects").mkdir()
    # the prepared template, sharing its big original files with the repo's
    shutil.copytree(src, home / "projects" / "tpl",
                    ignore=shutil.ignore_patterns("site", ".history"),
                    copy_function=lambda a, b: os.link(a, b)
                    if "pristine" in Path(a).parts else shutil.copy2(a, b))
    env = dict(os.environ, AETHRON_HOME=str(home))
    cmd = [str(APP / "Contents" / "MacOS" / "Aethron"), "--mcp"] if APP \
        else [sys.executable, str(ROOT / "forge_mcp.py")]
    p = subprocess.Popen(cmd,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env,
                         bufsize=1)
    replies = {}

    def reader():
        for line in p.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m:
                replies[m["id"]] = m
    threading.Thread(target=reader, daemon=True).start()
    n = [0]

    def call(tool, args, timeout=600):
        n[0] += 1
        i = n[0]
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i,
                                  "method": "tools/call",
                                  "params": {"name": tool,
                                             "arguments": args}}) + "\n")
        p.stdin.flush()
        t0 = time.time()
        while i not in replies:
            if time.time() - t0 > timeout:
                return False, f"no answer in {timeout}s"
            time.sleep(0.1)
        r = replies.pop(i)
        res = r.get("result") or {}
        text = " ".join(c.get("text", "") for c in res.get("content", []))
        if "error" in r:
            text = json.dumps(r["error"])
        return not (res.get("isError") or "error" in r), text

    try:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 0,
                                  "method": "initialize", "params": {}}) + "\n")
        p.stdin.flush()
        time.sleep(4 if APP else 1)

        ok, out = call("make_preview", {"template": "tpl", "name": "Lead Acme",
                                        "pages": ["home", "about", "nope"]})
        ck("an unknown page is refused, naming the pages that exist",
           not ok and "nope" in out and "about" in out, out)

        t0 = time.time()
        ok, out = call("make_preview", {"template": "tpl", "name": "Lead Acme",
                                        "pages": ["home", "about", "contact"],
                                        "notice": "Preview only — the rest is coming."})
        took = time.time() - t0
        ck("a lead's preview is made from the template in seconds",
           ok and took < 20 and "/preview/lead-acme" in out, f"{took:.1f}s {out}")
        m = re.search(r"(\d+) of the template's (\d+) lines", out)
        on, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        ck("only the chosen pages' lines are handed over", 0 < on < total, out)
        lead = home / "projects" / "lead-acme"
        prist = next((lead / "pristine").glob("*.html"))
        ck("the template's original files are shared, not duplicated",
           os.stat(prist).st_nlink > 1 and not os.path.samefile(
               lead / "pristine" / ".forge-manifest.json",
               home / "projects" / "tpl" / "pristine" / ".forge-manifest.json")
           if (lead / "pristine" / ".forge-manifest.json").exists()
           else os.stat(prist).st_nlink > 1, prist)

        ok, out = call("get_content", {"project": "lead-acme", "limit": 200,
                                       "brand_notes": False})
        listed = json.loads(out[out.index("["):]) if "[" in out else []
        ck("get_content lists just those pages' lines",
           ok and "preview — only the lines on" in out and 0 < len(listed) < total,
           out[:300])
        olds = [e["old"] for e in listed if len(e["old"].split()) >= 2
                and not e.get("max_bytes")][:2]
        fills = [{"old": o, "new": f"Acme Furniture line {i + 1}"}
                 for i, o in enumerate(olds)]
        ok, out = call("set_content_bulk", {"project": "lead-acme",
                                            "build": False, "entries": fills})
        ck("the agent's lines are written", ok and f"applied {len(fills)}" in out, out)

        page = (lead / "pristine" / "index.html").read_text(errors="ignore")
        anchor = next((e["old"] for e in listed
                       if 2 <= len(e["old"].split()) <= 6
                       and e["old"] in page and e["old"] not in olds), "")
        ok, out = call("add_block", {"project": "lead-acme", "kind": "reviews",
                                     "anchor": anchor, "data": {"items": []}})
        ck("reviews with no real review text are refused", not ok, out)
        ok, out = call("add_block", {"project": "lead-acme", "kind": "html",
                                     "anchor": anchor,
                                     "data": {"html": "<div onclick=x()>hi</div>"}})
        ck("a custom section cannot carry a script or event handler", not ok, out)
        ok, out = call("add_block", {"project": "lead-acme", "kind": "map",
                                     "anchor": "words that are not on any page",
                                     "data": {"query": "Lagos"}})
        ck("an anchor that is not on the pages is refused", not ok, out)
        ok, out = call("add_block", {"project": "lead-acme", "kind": "map",
                                     "anchor": anchor, "page": "home",
                                     "data": {"query": "Acme Furniture, Lagos",
                                              "title": "Visit us"}})
        ck("a map is added next to a section on the home page", ok, out)
        ok, out = call("add_block", {"project": "lead-acme", "kind": "reviews",
                                     "anchor": anchor, "page": "home",
                                     "position": "before",
                                     "data": {"rating": 4.8, "count": 12,
                                              "items": [{"author": "A. B.",
                                                         "text": "Test review text.",
                                                         "stars": 5}]}})
        ck("reviews are added with the caller's own review text", ok, out)

        ok, out = call("export_preview", {"project": "lead-acme"}, timeout=900)
        ck("the export's browser check PASSES at the preview address",
           ok and "browser check at /preview/lead-acme: PASS" in out, out)
        outdir = home / "previews" / "lead-acme"
        ck("only the chosen pages are exported",
           sorted(x.name for x in outdir.glob("*.html"))
           == ["about.html", "contact.html", "index.html"],
           sorted(x.name for x in outdir.glob("*.html")))
        idx = (outdir / "index.html").read_text()
        ck("no page still asks for ./assets/ (it would 404 with no trailing /)",
           not re.search(r'["\'( ,]\./assets/', idx), "relative refs left")
        ck("the agent's lines are in the exported page",
           all(f["new"] in idx for f in fills), [f["new"] for f in fills])

        # BEHAVIOUR, in a browser, at the preview address: a click on a page
        # that was not built opens the notice and goes nowhere; the extras
        # are on the page they were given to
        probe = idx.replace("</body>", """<script>
window.addEventListener('load',function(){setTimeout(function(){
 var C=window.__AE_PREVIEW__, keep=C.pages, a=[].slice.call(
   document.querySelectorAll('a[href]')).filter(function(x){
   var u=new URL(x.getAttribute('href'),location.href);
   if(u.origin!==location.origin) return false;
   var r=u.pathname.replace(C.base,'').replace(/^\\/+|\\/+$/g,'').replace(/\\.html$/,'');
   return r && r!=='index' && !keep.hasOwnProperty(r); })[0];
 var res={found:!!a, before:location.pathname};
 if(a){ a.click(); }
 setTimeout(function(){
  res.after=location.pathname; res.note=document.getElementById('ae-pv-note').className;
  res.blocks=[].map.call(document.querySelectorAll('[data-ae-block]'),function(b){return b.className;});
  var o=document.createElement('pre'); o.id='ae-test'; o.textContent=JSON.stringify(res);
  document.body.appendChild(o); },400); },2500);});
</script></body>""", 1)
        (outdir / "zz-test.html").write_text(probe)
        import aethron_preview as P
        import http.server
        base = "/preview/lead-acme"

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(s, *a, **k):
                super().__init__(*a, directory=str(outdir), **k)

            def translate_path(s, path):
                q = path.split("?")[0]
                return str(outdir / (q[len(base) + 1:] if q.startswith(base + "/")
                                     else "missing"))

            def log_message(s, *a):
                pass
        import mimetypes
        mimetypes.add_type("text/javascript", ".mjs")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        got = forge._render_page(forge._find_browser(),
                                 f"http://127.0.0.1:{srv.server_address[1]}{base}/zz-test.html",
                                 budget_ms=9000)
        srv.shutdown()
        m = re.search(r'<pre id="ae-test">(.*?)</pre>', got.get("dom") or "")
        res = json.loads(m.group(1).replace("&quot;", '"')) if m else {}
        ck("a link to a page that was not built opens the notice and stays put",
           res.get("found") and res.get("note") == "on"
           and res.get("after") == res.get("before"), res or got.get("error"))
        ck("the extras are placed on the page", set(res.get("blocks") or [])
           >= {"ae-pv-block ae-pv-map", "ae-pv-block ae-pv-rev"}, res)
    finally:
        p.kill()
        shutil.rmtree(home, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

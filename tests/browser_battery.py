#!/usr/bin/env python3
"""Aethron's own browser, driven the way the agent drives it — over MCP.

The case it exists for (the owner's Essentia migration): a shop widget
draws its cart AFTER the page loads, and its "Contact" button sends the
visitor to a Stripe checkout with window.open in a new tab. Aethron
changed the words, said the link was fixed, and never pressed it. Here:

  * the browser opens the built project, opens the cart, presses the
    button and reports the NEW TAB it landed on — FAIL against the
    address the person asked for;
  * set_link points the address elsewhere — the address is built in the
    widget's code, so it must be followed at click time — and hide_text
    hides the "FRAMESHIP" tag the widget draws;
  * the same clicks then PASS, the tag is not on screen, the answer
    carries a picture for the agent and a marker for the chat.

Runs the real forge_mcp over stdio in a throwaway AETHRON_HOME, against
two local servers — no network. No browser -> SKIPPED, never PASS.
"""
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ok = fail = 0


def check(name, cond, info=""):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name +
          ("" if cond else f"  — {str(info)[:600]}"))
    ok, fail = ok + bool(cond), fail + (not cond)


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main():
    import forge
    if not forge._find_browser():
        print("VERDICT: SKIPPED — no browser to drive")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="ae-brb-"))
    other = tmp / "other"
    other.mkdir()
    (other / "stripe.html").write_text("<title>Checkout</title>Pay here")
    (other / "contact.html").write_text("<title>Contact</title>Say hello")
    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              functools.partial(Quiet, directory=str(other)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    o = f"http://127.0.0.1:{srv.server_address[1]}"
    src = tmp / "export"
    src.mkdir()
    (src / "index.html").write_text(f"""<!doctype html><html><head>
<title>Essentia</title></head><body><h1>Software with deep roots</h1>
<button id=bag>Open bag</button><div id=cart></div>
<script>
// a widget: it builds its address in code and draws after load
var PAY = ['{o}', 'stripe.html'].join('/');
document.getElementById('bag').onclick = function () {{
  setTimeout(function () {{
    var c = document.getElementById('cart');
    c.innerHTML = '<div class=card><b>Start a project</b>' +
      '<a href="#" id=go>Contact Jomiez</a><span><i></i>FRAMESHIP</span></div>';
    document.getElementById('go').onclick = function (e) {{
      e.preventDefault(); window.open(PAY, '_blank'); }};
  }}, 200);
}};
</script></body></html>""")
    home = tmp / "home"
    env = dict(os.environ, AETHRON_HOME=str(home), AETHRON_REPORTS="off",
               AETHRON_RUNLOG="off")
    p = subprocess.Popen([sys.executable, str(ROOT / "forge_mcp.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env,
                         cwd=str(ROOT))
    n = [0]

    def call(tool, **args):
        n[0] += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0],
                                  "method": "tools/call",
                                  "params": {"name": tool,
                                             "arguments": args}}) + "\n")
        p.stdin.flush()
        r = json.loads(p.stdout.readline())["result"]
        text = "\n".join(c.get("text", "") for c in r["content"]
                         if c["type"] == "text")
        return text, r

    try:
        call_init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                     "params": {"clientInfo": {"name": "battery"}}}
        p.stdin.write(json.dumps(call_init) + "\n")
        p.stdin.flush()
        p.stdout.readline()
        t, r = call("create_project", name="shop", source_path=str(src))
        check("a project is made", not r.get("isError"), t)
        t, r = call("inventory", project="shop")
        t, r = call("build", project="shop")
        check("it builds", not r.get("isError") and "FAIL" not in t[:200], t)

        t, r = call("browser_open", project="shop")
        check("the browser opens the built project",
              "Software with deep roots" in t and "Open bag" in t, t)
        check("the answer shows the screen to the agent",
              any(c["type"] == "image" for c in r["content"]), r["content"][-1:])
        check("and marks a picture for the chat", "aethron-shot: " in t, t)
        call("browser_click", text="Open bag")
        t, r = call("browser_click", text="Contact Jomiez",
                    expect=o + "/contact.html")
        check("pressing the widget's button FAILS against the asked address",
              t.startswith("FAIL") and (r.get("structuredContent") or {})
              .get("verdict") == "FAIL", t)
        check("and names the checkout it really opened in a new tab",
              "NEW TAB" in t and "stripe.html" in t, t)

        t, r = call("set_link", project="shop", old=o + "/stripe.html",
                    new=o + "/contact.html")
        check("set_link takes an address built in code", not r.get("isError")
              and "build: OK" in t, t)
        t, r = call("hide_text", project="shop", text="FRAMESHIP")
        check("hide_text saves and builds", "build: OK" in t, t)

        call("browser_open", project="shop")
        call("browser_click", text="Open bag")
        t, r = call("browser_find", text="FRAMESHIP")
        check("the tag the widget draws is not on screen",
              "Nothing visible" in t, t)
        t, r = call("browser_find", text="Contact Jomiez")
        check("the button itself still is", "Contact Jomiez" in t, t)
        t, r = call("browser_click", text="Contact Jomiez",
                    expect=o + "/contact.html")
        check("the same click now PASSES", t.startswith("PASS"), t)
        check("in the new tab", "contact.html" in
              (r.get("structuredContent") or {}).get("landed", ""), t)
        t, r = call("browser_read")
        check("and the page it reads is the contact page",
              "Say hello" in t, t)
        t, r = call("browser_click", text="Nothing like this")
        check("a click on words that are not there is an error, not a pass",
              r.get("isError") and "nothing visible" in t.lower(), t)
        shots = list((home / "uploads").glob("*.jpg"))
        check("pictures are kept where the chat can show them", len(shots) >= 3,
              shots)
        js = (home / "projects" / "shop" / "site" / "index.html").read_text()
        check("the build carries the live fix, not a hand edit",
              "data-aethron-live" in js)
    finally:
        try:
            p.stdin.close()
            p.wait(20)
        except Exception:
            p.kill()
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok}/{ok + fail} checks passed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

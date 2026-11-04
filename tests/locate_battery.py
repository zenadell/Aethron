"""A screenshot of part of the site is found on the site (aethron_locate).

A person crops a piece of their site and asks for a change. Aethron must say
which page, where, and which element in the code — not guess from words.

  A. the module: a card on the SECOND page, captured at 2x, is found on the
     right page and is the right card, not its identical-looking twin; a
     plain picture is never "found"
  B. the studio route: /api/locate answers the same, names the line, and
     refuses a crafted image name or an unknown project
  C. the agent's tool: locate_screenshot over MCP answers the same
  D. the chat: a picture attached in a project is located BEFORE it is sent,
     and the agent is given what was found
No browser -> SKIPPED, never PASS.
"""
import base64, json, os, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS = FAIL = 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f" — {str(detail)[:300]}"))


def post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def main():
    import forge
    print("D. the chat locates a picture before sending it")
    src = (ROOT / "studio.py").read_text()
    send = src[src.index("async function sendCode(){"):]
    send = send[:send.index("\n}\n")]
    ck("sendCode locates attached pictures, then sends what was found",
       "locateShots(pics)" in send and send.index("locateShots(pics)")
       < send.index("/api/code/send") and "text:sendText" in send, send[-400:])
    ck("the finding is shown in the chat", "e.type==='sysnote'" in src)
    ck("the agent is told to use it", "locate_screenshot" in
       (ROOT / "aethron_code.py").read_text())
    if not forge._find_browser():
        print("SKIPPED — no browser; nothing was located")
        print(f"\nlocate battery: {PASS} passed, {FAIL} failed (browser part SKIPPED)")
        return 1 if FAIL else 0

    print("A. the module")
    r = subprocess.run([sys.executable, "aethron_locate.py", "--selftest"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    ck("aethron_locate --selftest", r.returncode == 0 and "6/6" in r.stdout, r.stdout[-600:])

    tmp = Path(tempfile.mkdtemp(prefix="ae-locbat-"))
    home = tmp / "home"
    proj = home / "projects" / "plans"
    (proj / "site").mkdir(parents=True)
    (home / "uploads").mkdir(parents=True)
    (proj / "forge.json").write_text('{"platform":"static"}')
    css = ("body{margin:0;font:16px sans-serif;background:#fbfaf6}"
           ".row{display:flex;gap:24px;padding:60px}"
           ".card{width:300px;height:220px;border-radius:18px;padding:20px}")
    cards = "".join(f"<div class=\"card\" data-framer-name=\"Tier {i}\" "
                    f"style=\"background:hsl({i*90+20},55%,72%)\"><b>Tier {i}</b>"
                    f"<p>{'More ' * (i+1)}</p></div>" for i in range(3))
    (proj / "site/index.html").write_text(f"<style>{css}</style><h1 style=\"padding:60px\">Home</h1>")
    (proj / "site/plans.html").write_text(f"<style>{css}</style><h1 style=\"padding:60px\">Plans</h1>"
                                          f"<div class=\"row\">{cards}</div>")
    import aethron_browser as AB
    b = AB.Browser(width=1240, height=900); b.start()
    b.page("Emulation.setDeviceMetricsOverride", {"width": 1240, "height": 900,
           "deviceScaleFactor": 2, "mobile": False})
    b.open(AB.serve_site(proj / "site", "static") + "plans.html")
    rc = b.eval("(()=>{const b=document.querySelector('[data-framer-name=\"Tier 2\"]')"
                ".getBoundingClientRect();return [b.left,b.top,b.width,b.height]})()")
    png = base64.b64decode(b.page("Page.captureScreenshot", {"format": "png", "clip": {
        "x": rc[0] - 10, "y": rc[1] - 10, "width": rc[2] + 20, "height": rc[3] + 20, "scale": 1}})["data"])
    b.close()
    name = "0123456789abcdef.png"
    (home / "uploads" / name).write_bytes(png)

    print("B. the studio route")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(os.environ, AETHRON_HOME=str(home), BROWSER="true", AETHRON_REPORTS="off",
               AETHRON_RUNLOG="off")
    for k in list(env):
        if k.startswith("AETHRON_SUPABASE"):
            env.pop(k)
    srv = subprocess.Popen([sys.executable, "studio.py", str(port)], cwd=ROOT, env=env,
                           stdout=open(tmp / "studio.log", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2); break
            except Exception:
                time.sleep(0.5)
        code, r = post(port, "/api/locate", {"project": "plans", "image": name, "width": 1240})
        el = r.get("element") or {}
        ck("found, with confidence", code == 200 and r.get("ok"), r)
        ck("on the right page", r.get("page") == "plans.html", r.get("page"))
        ck("the right card, not its twin", el.get("frname") == "Tier 2", el)
        ck("with its line in the file", r.get("line") and r.get("file") == "site/plans.html", r)
        ck("the agent's text names it", "Tier 2" in (r.get("text") or ""), r.get("text"))
        ck("a crafted image name is refused",
           post(port, "/api/locate", {"project": "plans", "image": "../../etc/passwd"})[0] == 400)
        ck("an unknown project is refused",
           post(port, "/api/locate", {"project": "../x", "image": name})[0] == 404)
    finally:
        srv.terminate()

    print("C. the agent's tool")
    prog = ("import json,forge_mcp as M;r=M.HANDLERS['locate_screenshot']("
            f"{{'project':'plans','image':{json.dumps(name)},'width':1240}});"
            "print(json.dumps(getattr(r,'data',None) or {}))")
    out = subprocess.run([sys.executable, "-c", prog], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=600)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        d = {}
    ck("locate_screenshot names the same card",
       d.get("ok") and (d.get("element") or {}).get("frname") == "Tier 2",
       out.stdout[-300:] + out.stderr[-300:])
    print(f"\nlocate battery: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

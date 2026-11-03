"""The Home chat, as the owner hit it on 2026-11-03 (1.1.82):

  * "New" opened the SAME Home conversation again (Home reopens its last
    chat, and New only went Home);
  * with a chat on screen, the projects gallery in the right rail was
    drawn hidden from the start, so after a restart it never showed;
  * after a dropped connection Aethron told the agent only "the
    connection dropped", the model answered "No response requested." and
    the run never continued — and that message was drawn as if the
    person had written it.

Real studio, real browser, throwaway AETHRON_HOME with one built project.
SKIPPED without playwright or a browser — never PASS.
"""
import json, os, sys, time, socket, shutil, subprocess, tempfile, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("VERDICT: SKIPPED — playwright is not installed"); sys.exit(0)

ok = True
def check(n, c):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n, flush=True)

# 1. the transcript reader never draws Aethron's resume note as the person's words
import aethron_code as AC
SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-chatui-")); HOME = SP / "home"
tr = SP / "t.jsonl"
tr.write_text("\n".join(json.dumps(m) for m in [
    {"type": "user", "message": {"content": "Migrate mkvision.studio for Jomiez"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "On it."}]}},
    {"type": "user", "message": {"content": [{"type": "text", "text":
        "<aethron-resume>The connection to the model dropped …</aethron-resume>"}]}},
]) + "\n")
_tp = AC.transcript_path
AC.transcript_path = lambda sid: tr
evs = AC.transcript_events("x")
AC.transcript_path = _tp
yous = [e["text"] for e in evs if e["type"] == "you"]
check("a restored chat shows the person's request", yous == ["Migrate mkvision.studio for Jomiez"])
check("and not Aethron's resume note", not any("aethron-resume" in y for y in yous))

(HOME / "projects").mkdir(parents=True)
work = SP / "work"; work.mkdir()
subprocess.run([sys.executable, str(ROOT / "forge.py"), "init", str(ROOT / "tests/pages/site.html")],
               cwd=work, capture_output=True, timeout=300)
proj = next(work.iterdir()); shutil.move(str(proj), HOME / "projects" / proj.name)
proj = HOME / "projects" / proj.name
for step in ("inventory", "build"):
    subprocess.run([sys.executable, str(ROOT / "forge.py"), step], cwd=proj,
                   capture_output=True, timeout=300)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"): env.pop(k)
srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)

def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); srv.terminate(); sys.exit(0)

try:
    time.sleep(4)
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1500, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
        pg.goto("http://127.0.0.1:%d/" % PORT)
        pg.wait_for_selector("#hrail", timeout=15000)
        pg.wait_for_timeout(1500)
        # a Home conversation on screen, as after a restart
        pg.evaluate("""()=>{CODE.owner='';CODE.events=[{type:'you',text:'Migrate mkvision.studio for Jomiez'},
                       {type:'text',text:'Working on it.'}];CODE.known=true;renderTab();}""")
        pg.wait_for_timeout(800)
        check("a chat is on screen", "Working on it." in pg.inner_text("body"))
        check("the projects gallery is in the right rail during the chat",
              pg.evaluate("()=>{const g=document.getElementById('hgal');"
                          "return !!g&&!g.hidden&&g.getBoundingClientRect().height>0}"))
        # the reconnect asks for the work, carrying the person's request
        sent = pg.evaluate("""async()=>{const real=api, seen=[];
            window.api=async(path,body)=>{seen.push([path,body]);
              if(path==='/api/code/start')return {key:'k1'};return {};};
            try{await reconnectRun();}finally{window.api=real;if(CODE.poll){clearInterval(CODE.poll);CODE.poll=0;}}
            return seen;}""")
        txt = next((b["text"] for pth, b in sent if pth == "/api/code/send"), "")
        check("a reconnect sends the person's last request", "Migrate mkvision.studio for Jomiez" in txt)
        check("as a task to continue, tagged so it is never drawn as theirs",
              txt.startswith("<aethron-resume>") and "Continue the work" in txt)
        check("nothing new is drawn in the chat by the reconnect",
              pg.evaluate("()=>CODE.events.filter(e=>e.type==='you').length") == 1)
        # New starts a fresh chat
        calls = pg.evaluate("""async()=>{const real=api, seen=[];
            window.api=async(path,body)=>{seen.push([path,body]);return real(path,body);};
            document.querySelector('.newbtn').click();
            await new Promise(r=>setTimeout(r,1200)); window.api=real; return seen;}""")
        check("New clears the conversation on screen",
              pg.evaluate("()=>CODE.events.length") == 0 and "Working on it." not in pg.inner_text("body"))
        check("New tells the server to start Home afresh",
              any(pth == "/api/code/forget" and (bd or {}).get("console") for pth, bd in calls))
        check("the welcome is back", pg.evaluate("()=>{const w=document.getElementById('welcome');return !!w&&!w.hidden}"))
        # going Home (the logo) does NOT wipe a chat
        pg.evaluate("""()=>{CODE.events=[{type:'you',text:'Keep this'}];renderTab();}""")
        pg.evaluate("()=>goHome()"); pg.wait_for_timeout(800)
        check("the logo goes Home without wiping the chat",
              pg.evaluate("()=>CODE.events.length") == 1)
        check("no page errors", not errs)
        if errs: print("  ", errs[:3])
        b.close()
finally:
    srv.terminate()
print("\n%s" % ("all chat-ui checks passed" if ok else "SOME CHAT-UI CHECKS FAILED"))
sys.exit(0 if ok else 1)

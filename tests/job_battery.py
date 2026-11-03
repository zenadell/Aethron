"""The background migration job (aethron_jobs) — does a request become a
finished project without anyone watching, and does it survive being killed?

A real export (tests/pages/site.html) and a fake AI provider served on this
machine. The fake answers the brief and the fill prompts the way a model
does, records how many requests run at once and which lines it was asked
for, and can be told to answer badly or slowly.

Proves:
  * one start() runs create -> fetch -> inventory -> plan -> fill -> build
    -> check with nobody polling it, and every line is written;
  * the copy is written in PARALLEL batches (several requests at once);
  * an answer that is not JSON is split and asked again, not lost;
  * KILLED mid-fill, the watchdog resumes it and lines already written are
    not asked for again;
  * no brief and no request -> the job WAITS with a question, never guesses;
  * stop() stops it; start() continues it;
  * the MCP tool starts it and says it runs in the background.
"""
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


class Fake:
    delay = 0.0
    garbage_first = True
    lock = threading.Lock()
    live = 0
    peak = 0
    asked = []          # every fill line asked for, in order
    calls = 0


class Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        prompt = body["messages"][-1]["content"]
        if isinstance(prompt, list):
            prompt = " ".join(p.get("text", "") for p in prompt
                              if isinstance(p, dict))
        with Fake.lock:
            Fake.live += 1
            Fake.peak = max(Fake.peak, Fake.live)
            Fake.calls += 1
            first = Fake.calls
        try:
            if "Owner's rough plan" in prompt:
                out = ("Brand: Northwind\nDomain: northwind.no\nWhat it is: a "
                       "coffee roaster in Oslo.\nTone: warm, plain.\n")
            else:
                time.sleep(Fake.delay)
                i = prompt.rfind("\n{")
                data = json.loads(prompt[i + 1:].split("\n\nThese were "
                                                       "REFUSED")[0])
                for sec, items in data.items():
                    for e in items:
                        with Fake.lock:
                            Fake.asked.append(e["old"])
                        if sec != "strings":
                            continue
                        new = "Northwind " + e["old"]
                        mb = e.get("max_bytes")
                        if mb:
                            new = new.encode()[:mb].decode("utf-8", "ignore")
                        e["new"] = new
                out = json.dumps(data)
                if Fake.garbage_first and len(data.get("strings", [])) > 5:
                    Fake.garbage_first = False
                    out = "Sorry, I cannot do that."   # not JSON at all
        finally:
            with Fake.lock:
                Fake.live -= 1
        resp = {"id": "x", "object": "chat.completion", "model": "fake",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": out}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        b = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except OSError:          # a stopped runner hung up — that is the test
            pass


def wait(J, name, statuses=("done", "failed", "waiting", "stopped"),
         limit=900):
    t = time.time()
    while time.time() - t < limit:
        s = J.read(name)
        if s.get("status") in statuses:
            return s
        time.sleep(1)
    return J.read(name)


def export_dir(tmp: Path) -> Path:
    src = tmp / "export"
    src.mkdir()
    shutil.copy(ROOT / "tests/pages/site.html", src / "index.html")
    if (ROOT / "tests/pages/assets").is_dir():
        shutil.copytree(ROOT / "tests/pages/assets", src / "assets")
    return src


def main():
    tmp = Path(tempfile.mkdtemp(prefix="aejob-"))
    home = tmp / "home"
    (home / "projects").mkdir(parents=True)
    os.environ.update(AETHRON_HOME=str(home), AETHRON_BROWSER="none",
                      AETHRON_JOB_AGENT="0", AETHRON_REPORTS="off",
                      AETHRON_RUNLOG="off", AETHRON_JOB_PARALLEL="4")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sys.path.insert(0, str(ROOT))
    import aethron_brain as brain
    brain.save({"provider": "custom", "model": "fake", "api_key": "k",
                "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1"})
    import aethron_jobs as J
    src = export_dir(tmp)

    print("A. one request, nobody watching")
    os.environ["AETHRON_JOB_BATCH"] = "8"       # a small export: several
    Fake.delay = 0.5                            # batches, overlapping
    t0 = time.time()
    J.start("north", request="Northwind, a coffee roaster in Oslo",
            url=str(src))
    st = wait(J, "north")
    secs = time.time() - t0
    ck("the job finished", st.get("status") == "done",
       f"{st.get('status')}: {st.get('error')} | "
       + " / ".join(J.log_tail("north", 15)))
    ck("every step is recorded done",
       all(st.get("steps", {}).get(s, {}).get("status") == "done"
           for s in J.STEPS), json.dumps(st.get("steps"))[:400])
    d = home / "projects" / "north"
    cm = json.loads((d / "copy_map.json").read_text()) \
        if (d / "copy_map.json").exists() else {}
    strings = cm.get("strings", [])
    filled = sum(1 for e in strings if e.get("new"))
    from forge import word_fragments
    frag = word_fragments(d, cm) if cm else {}
    want = sum(1 for e in strings if e["old"] not in frag)
    ck("every line is written", strings and filled >= want,
       f"{filled} of {want}")
    site = "".join(p.read_text(errors="ignore")
                   for p in (d / "site").glob("*.html")) \
        if (d / "site").is_dir() else ""
    ck("the built site carries the new copy", "Northwind" in site)
    ck("batches ran in parallel", Fake.peak >= 2, f"peak {Fake.peak}")
    ck("an answer that was not JSON was split and asked again",
       not Fake.garbage_first and filled >= want)
    ck("the plan was written from the request",
       "Northwind" in (d / "project_plan.md").read_text())
    ck("a result is measured", bool(st.get("result", {}).get("verdict")))
    ck("it took minutes, not hours", secs < 600, f"{secs:.0f}s")
    print(f"    ({len(strings)} lines, {Fake.calls} model calls, "
          f"{secs:.0f}s)")

    Fake.delay = 0.0
    print("B. killed mid-fill, resumed by the watchdog")
    Fake.asked.clear()
    Fake.delay = 2.0
    os.environ["AETHRON_JOB_PARALLEL"] = "1"
    os.environ["AETHRON_JOB_BATCH"] = "10"
    J.start("north2", request="Northwind, a coffee roaster in Oslo",
            url=str(src))
    t = time.time()
    while time.time() - t < 300:
        s = J.read("north2")
        if (s.get("progress") or {}).get("done", 0) >= 10:
            break
        time.sleep(0.5)
    s = J.read("north2")
    ck("the job reached the fill and saved lines",
       (s.get("progress") or {}).get("done", 0) >= 10,
       json.dumps(s.get("progress")))
    pid = s.get("pid")
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except OSError:
        pass
    time.sleep(1)
    ck("the runner is gone", not J.alive(pid))
    before = len(Fake.asked)
    J.update("north2", heartbeat=time.time() - 30)
    acted = J.supervise()
    ck("the watchdog resumed it", ("north2", "resumed") in acted, str(acted))
    Fake.delay = 0.0
    s = wait(J, "north2")
    ck("the resumed job finished", s.get("status") == "done",
       f"{s.get('status')}: {s.get('error')}")
    dup = len(Fake.asked) - len(set(Fake.asked))
    ck("lines already written were not asked again",
       dup <= 10, f"{dup} asked twice (one batch in flight may be)")
    ck("it resumed at the fill, not from the start",
       before > 0 and "resuming at 'fill'" in "\n".join(
           J.log_tail("north2", 200)), "\n".join(J.log_tail("north2", 30)))

    print("C. no brief, no request: it asks")
    J.start("north3", url=str(src))
    s = wait(J, "north3")
    ck("the job waits for the person", s.get("status") == "waiting",
       f"{s.get('status')}: {s.get('error')}")
    ck("with one concrete question", "brand" in (s.get("question") or ""))
    J.start("north3", request="Northwind, a coffee roaster in Oslo")
    s = wait(J, "north3")
    ck("answered, it carries on and finishes", s.get("status") == "done",
       f"{s.get('status')}: {s.get('error')}")

    print("D. stop and continue")
    Fake.delay = 3.0
    J.start("north4", request="Northwind", url=str(src))
    t = time.time()
    while time.time() - t < 120 and J.read("north4").get("step") != "fill":
        time.sleep(0.5)
    st = J.stop("north4")
    time.sleep(1)
    ck("stopped", J.read("north4").get("status") == "stopped")
    ck("its process is gone", not J.alive(st.get("pid") or
                                          J.read("north4").get("pid")))
    Fake.delay = 0.0
    J.start("north4")
    s = wait(J, "north4")
    ck("start() continues it to the end", s.get("status") == "done",
       f"{s.get('status')}: {s.get('error')}")

    print("E. the agent's tool")
    import forge_mcp
    r = forge_mcp.t_start_migration({"project": "north5",
                                     "url": str(src),
                                     "request": "Northwind coffee"})
    ck("start_migration starts it and says it runs in the background",
       "background" in r and getattr(r, "data", {}).get("status"))
    s = wait(J, "north5")
    ck("and it finishes", s.get("status") == "done", s.get("error"))
    ck("migration_status reads the result",
       "DONE" in forge_mcp.t_migration_status({"project": "north5"}))

    print("F. the card in the app")
    ui(home, src, J)

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\njob battery: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def ui(home, src, J):
    import socket
    import subprocess
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIPPED — playwright is not installed (not a pass)")
        return
    so = socket.socket()
    so.bind(("127.0.0.1", 0))
    port = so.getsockname()[1]
    so.close()
    env = dict(os.environ, BROWSER="true")
    for k in list(env):
        if k.startswith("AETHRON_SUPABASE"):
            env.pop(k)
    log = open(home / "studio.log", "w")
    st = subprocess.Popen([sys.executable, str(ROOT / "studio.py"), str(port)],
                          cwd=str(ROOT), env=env, stdout=log,
                          stderr=subprocess.STDOUT)
    try:
        time.sleep(4)
        with sync_playwright() as p:
            b = None
            for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
                try:
                    b = p.chromium.launch(**kw)
                    break
                except Exception:
                    pass
            if b is None:
                print("  SKIPPED — no browser playwright can start")
                return
            pg = b.new_page(viewport={"width": 1500, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
            base = f"http://127.0.0.1:{port}"
            pg.goto(base + "/")
            pg.wait_for_selector("#hrail", timeout=20000)
            pg.evaluate("select('north5')")
            try:
                pg.wait_for_selector(".jobc.done", timeout=15000)
                txt = pg.inner_text(".jobc.done")
            except Exception:
                txt = ""
            ck("a finished job shows its card in the project", "Finished" in txt
               and "Open north5" in txt, repr(txt[:300]))
            ck("with every step ticked",
               pg.evaluate("()=>document.querySelectorAll('.jobc-steps .done')"
                           ".length") >= 7)
            # a job that needs the person: answered in the card
            pg.evaluate("""async()=>{await api('/api/job/start',
                {project:'north6',url:%s});}""" % json.dumps(str(src)))
            wait(J, "north6", ("waiting", "done", "failed"))
            pg.evaluate("select('north6')")
            try:
                pg.wait_for_selector(".jobc.waiting input", timeout=15000)
                asked = True
            except Exception:
                asked = False
            ck("a waiting job asks its question in the card", asked,
               f"job {J.read('north6').get('status')}; card: "
               + repr(pg.evaluate("()=>[...document.querySelectorAll('.jobc')]"
                                  ".map(e=>e.className+': '+e.innerText.slice(0,120))")))
            if asked:
                pg.fill(".jobc.waiting input", "Northwind, coffee in Oslo")
                pg.click(".jobc.waiting .btn.primary")
                try:
                    pg.wait_for_selector(".jobc.done", timeout=180000)
                    done = True
                except Exception:
                    done = False
                ck("answered there, it goes on and finishes", done,
                   J.read("north6").get("status"))
            # a start_migration step in the chat draws the card under it
            pg.evaluate("""()=>{S.cur=null;CODE.owner='';CODE.known=true;
                CODE.events=[{type:'you',text:'Migrate it for Northwind'},
                 {type:'tool',id:'t1',name:'mcp__aethron__start_migration',
                  input:{project:'north5'}},
                 {type:'tool_result',id:'t1',ok:true,text:'started'},
                 {type:'text',text:'It is running.'}];renderTab();}""")
            try:
                pg.wait_for_selector('.jobc[data-job="north5"]', timeout=10000)
                drawn = True
            except Exception:
                drawn = False
            ck("a start_migration step in the chat shows the job's card", drawn)
            ck("no page errors", not errs, "; ".join(errs[:3]))
            b.close()
    finally:
        st.terminate()


if __name__ == "__main__":
    sys.exit(main())

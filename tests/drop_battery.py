"""Dropping and pasting an image works anywhere in the window, and shows it.

  A  the studio's native routes, with a stand-in for the Mac's pasteboards:
     a clipboard image is stored and named; a drop the page already handled
     is never handed back by a later empty drop; off a Mac they say so
  B  the studio in a real browser:
       - a file dragged over ANY part of the window lights up the whole
         window, names what will happen, and blocks the window from
         navigating to the file
       - dropped, the picture flies to the chat box and lands as a thumbnail
       - a file that is not an image is named and refused, never attached
       - a paste of an image into the box attaches it
       - an empty paste, Cmd+V with nothing focused, and a drop with no
         files in it (a screenshot dragged from its floating thumbnail)
         all ask the Mac's own pasteboards
       - a drag that started inside the page is not a file drop
       - with reduced motion nothing flies
     (SKIPPED without playwright or a browser, never PASS)

usage: python3 tests/drop_battery.py
"""
import base64, hashlib, json, os, pathlib, shutil, socket, struct, subprocess, \
    sys, tempfile, threading, time, urllib.request, zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
ok = True
SKIPPED_B = False


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n)


def png(w=40, h=24, rgb=(217, 119, 87)):
    raw = zlib.compress((b"\x00" + bytes(rgb) * w) * h)
    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
        ">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw)
        + chunk(b"IEND", b""))


SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-drop-"))
HOME = SP / "home"
(HOME / "projects").mkdir(parents=True)
PIC = png()
B64 = base64.b64encode(PIC).decode()
NATIVE_PIC = png(30, 20, (40, 90, 200))
NATIVE_NAME = hashlib.sha1(NATIVE_PIC).hexdigest()[:16] + ".png"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


# ── A: the native routes, in-process, with a stand-in pasteboard ───────
os.environ["AETHRON_HOME"] = str(HOME)
for k in list(os.environ):
    if k.startswith("AETHRON_SUPABASE"):
        os.environ.pop(k)
import studio  # noqa: E402


class FakeNative:
    def __init__(self):
        self.drag = {"count": 7, "images": [{"name": "Screenshot.png", "data": NATIVE_PIC}],
                     "others": []}
        self.general = {"count": 3, "images": [{"name": "pasted image", "data": NATIVE_PIC}],
                        "others": []}
        self.reads = []

    def change_count(self, which):
        return (self.drag if which == "drag" else self.general)["count"]

    def read(self, which):
        self.reads.append(which)
        return dict(self.drag if which == "drag" else self.general)


port = free_port()
srv = studio.ThreadingHTTPServer(("127.0.0.1", port), studio.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(path, body):
    rq = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(rq, timeout=20).read())


r = post("/api/native/paste", {})
check("in a browser the native routes say they are not the Mac app",
      r.get("native") is False and r.get("images") == [])
fake = FakeNative()
studio.NATIVE = fake
r = post("/api/native/paste", {})
check("an image on the Mac's clipboard is stored and named",
      r.get("images") and r["images"][0]["name"] == NATIVE_NAME
      and (HOME / "uploads" / NATIVE_NAME).read_bytes() == NATIVE_PIC)
r = post("/api/native/drop", {})
check("an image in the last drag is stored and named",
      r.get("images") and r["images"][0]["label"] == "Screenshot.png")
r = post("/api/native/drop", {})
check("the same drag is never handed back twice", r.get("images") == [])
fake.drag["count"] = 8
post("/api/native/drop", {"discard": True})
r = post("/api/native/drop", {})
check("a drop the page already handled is marked seen, not re-read",
      r.get("images") == [] and fake.reads.count("drag") == 1)
fake.drag.update(count=9, images=[], others=["notes.pdf"])
r = post("/api/native/drop", {})
check("a dropped file that is not an image is named, not stored",
      r.get("images") == [] and r.get("others") == ["notes.pdf"])
studio.NATIVE = None
srv.shutdown()

# ── B: the studio in a browser ────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
if not sync_playwright:
    print("SKIP playwright is not installed — B UNVERIFIED")
    SKIPPED_B = True
else:
    PORT = free_port()
    env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
    proc = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                            stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
    DROP_JS = """([b64,name,type,where,x,y,kind])=>{
      const bin=atob(b64),u=new Uint8Array(bin.length);
      for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
      const dt=new DataTransfer();
      if(name)dt.items.add(new File([u],name,{type}));
      else dt.setData('public.file-url','file:///Users/x/Desktop/shot.png');
      const el=document.querySelector(where)||document.body;
      const ev=new DragEvent(kind,{bubbles:true,cancelable:true,dataTransfer:dt,clientX:x,clientY:y});
      el.dispatchEvent(ev);
      return ev.defaultPrevented;
    }"""
    try:
        time.sleep(4)
        with sync_playwright() as p:
            b = None
            for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
                try:
                    b = p.chromium.launch(**kw); break
                except Exception:
                    pass
            if not b:
                print("SKIP no browser playwright can start — B UNVERIFIED")
                SKIPPED_B = True
            else:
                pg = b.new_page(viewport={"width": 1300, "height": 860})
                errs, native = [], []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.add_init_script("try{localStorage.forge_tour='done';"
                                   "localStorage.forge_news='done'}catch(e){}")
                # the Mac's pasteboards, stood in for: each answers with the
                # picture section A stored
                def nat(route):
                    native.append(route.request.url.rsplit("/", 1)[1])
                    body = json.loads(route.request.post_data or "{}")
                    imgs = [] if body.get("discard") else [
                        {"name": NATIVE_NAME, "label": "Screenshot.png"}]
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"native": True, "images": imgs, "others": []}))
                pg.route("**/api/native/*", nat)
                pg.goto(f"http://127.0.0.1:{PORT}/")
                pg.wait_for_selector("#npurl", timeout=15000)
                pg.wait_for_timeout(800)

                def thumbs():
                    return pg.locator(".composer .cthumb img").count()

                def clear():
                    pg.evaluate("()=>{CODE.pending=[];renderPending()}")

                # over the headline, far from the box
                prevented = pg.evaluate(DROP_JS, [B64, "hero.png", "image/png", "h1,.hero,main",
                                                  300, 200, "dragenter"])
                pg.evaluate(DROP_JS, [B64, "hero.png", "image/png", "h1,.hero,main",
                                      310, 210, "dragover"])
                pg.wait_for_timeout(450)
                on = pg.evaluate("()=>{const d=document.querySelector('.ae-drop');"
                                 "return d&&d.classList.contains('on')&&getComputedStyle(d).opacity}")
                check("a file over any part of the window lights up the whole window",
                      on and float(on) > 0.5 or print("overlay:", on))
                check("and the window will not navigate to the file", prevented)
                check("it says what will happen",
                      "Drop your image here" in pg.inner_text(".ae-drop"))
                box = pg.evaluate("()=>{const r=document.querySelector('.ae-drop').getBoundingClientRect();"
                                  "return [r.width,r.height]}")
                check("the drop zone covers the window", box == [1300, 860])
                flown = pg.evaluate("""()=>{window.__fly=0;new MutationObserver(m=>m.forEach(x=>
                    x.addedNodes.forEach(n=>{if(n.classList&&n.classList.contains('ae-flyer'))window.__fly++})))
                    .observe(document.body,{childList:true});return true}""")
                pg.evaluate(DROP_JS, [B64, "hero.png", "image/png", "h1,.hero,main",
                                      310, 210, "drop"])
                pg.wait_for_selector(".composer .cthumb img", timeout=10000)
                pg.wait_for_timeout(1100)
                check("dropped, the picture flies to the chat box", pg.evaluate("window.__fly") == 1)
                check("and lands there as a thumbnail",
                      pg.eval_on_selector(".composer .cthumb img", "i=>i.complete&&i.naturalWidth") == 40
                      and pg.locator(".ae-flyer").count() == 0)
                check("the window goes back to normal",
                      not pg.evaluate("()=>document.querySelector('.ae-drop').classList.contains('on')"))
                check("the page told the Mac the drop was handled", "drop" in native)
                clear()

                pg.evaluate(DROP_JS, [B64, "notes.pdf", "application/pdf", "main", 300, 300, "dragenter"])
                pg.wait_for_timeout(150)
                check("a file that is not an image is called out before it is dropped",
                      pg.evaluate("()=>document.querySelector('.ae-drop').classList.contains('bad')"))
                pg.evaluate(DROP_JS, [B64, "notes.pdf", "application/pdf", "main", 300, 300, "drop"])
                pg.wait_for_timeout(500)
                check("and after the drop it is named and refused, not attached",
                      thumbs() == 0 and "notes.pdf is not something Aethron can open"
                      in pg.inner_text("#toasts"))

                pg.focus("#npurl")
                pg.evaluate("""([b64])=>{const bin=atob(b64),u=new Uint8Array(bin.length);
                  for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
                  const dt=new DataTransfer();dt.items.add(new File([u],'image.png',{type:'image/png'}));
                  document.getElementById('npurl').dispatchEvent(new ClipboardEvent('paste',
                    {bubbles:true,cancelable:true,clipboardData:dt}));}""", [B64])
                pg.wait_for_selector(".composer .cthumb img", timeout=10000)
                pg.wait_for_timeout(400)
                check("an image pasted into the box is attached", thumbs() == 1)
                check("a pasted screenshot is called a pasted image, not image.png",
                      pg.get_attribute(".composer .cthumb", "title") == "pasted image")
                clear()
                n0 = native.count("paste")
                pg.evaluate("""()=>{const dt=new DataTransfer();
                  document.getElementById('npurl').dispatchEvent(new ClipboardEvent('paste',
                    {bubbles:true,cancelable:true,clipboardData:dt}));}""")
                pg.wait_for_timeout(700)
                check("a paste the web view hands over empty asks the Mac's clipboard",
                      native.count("paste") == n0 + 1 and thumbs() == 1)
                clear()

                pg.evaluate("()=>document.activeElement&&document.activeElement.blur()")
                n0 = native.count("paste")
                pg.keyboard.press("Meta+V")
                pg.wait_for_timeout(700)
                check("Cmd+V with nothing focused still pastes the image",
                      native.count("paste") == n0 + 1 and thumbs() == 1)
                clear()

                n0 = native.count("drop")
                pg.evaluate(DROP_JS, [B64, "", "", "main", 400, 300, "dragenter"])
                pg.evaluate(DROP_JS, [B64, "", "", "main", 400, 300, "drop"])
                pg.wait_for_timeout(700)
                check("a drop with no files in it reads the Mac's drag instead",
                      native.count("drop") == n0 + 1 and thumbs() == 1)
                clear()

                pg.evaluate("""()=>{const dt=new DataTransfer();dt.setData('text/plain','hi');
                  document.body.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
                  document.body.dispatchEvent(new DragEvent('dragenter',{bubbles:true,cancelable:true,dataTransfer:dt}));}""")
                pg.wait_for_timeout(200)
                check("a drag that started inside the page is not a file drop",
                      not pg.evaluate("()=>document.querySelector('.ae-drop').classList.contains('on')"))
                pg.evaluate("()=>document.body.dispatchEvent(new DragEvent('dragend',{bubbles:true}))")

                pg.evaluate(DROP_JS, [B64, "hero.png", "image/png", "main", 300, 300, "dragenter"])
                pg.wait_for_timeout(1400)
                check("a drag that goes quiet (it left the window) puts the window back",
                      not pg.evaluate("()=>document.querySelector('.ae-drop').classList.contains('on')"))

                pg.emulate_media(reduced_motion="reduce")
                pg.evaluate("window.__fly=0")
                pg.evaluate(DROP_JS, [B64, "hero.png", "image/png", "main", 300, 300, "drop"])
                pg.wait_for_selector(".composer .cthumb img", timeout=10000)
                pg.wait_for_timeout(500)
                check("with reduced motion the picture is attached without flying",
                      thumbs() == 1 and pg.evaluate("window.__fly") == 0)
                check("no page errors", not errs or print(errs))
                b.close()
    finally:
        proc.terminate()
shutil.rmtree(SP, ignore_errors=True)
if ok and SKIPPED_B:
    print("VERDICT: SKIPPED — the browser half could not run, so drop and paste are UNVERIFIED")
    sys.exit(0)
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

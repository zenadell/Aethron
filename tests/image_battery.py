"""An attached image is SEEN: shown in the composer and the message, and
handed to the model as real image input — never a path in the text and
never "[image omitted]".

  A  the bridge turns Anthropic image blocks (in a message or returned by
     a tool) into OpenAI image_url parts; a model that refuses images is
     asked again without them, so the turn goes on
  B  the REAL agent engine, through the REAL bridge, into a recording
     OpenAI-shaped upstream: the picture arrives as a data URL, and a
     restored conversation still knows the message had a picture
  C  the studio in a real browser: paste -> thumbnail in the composer,
     send -> the picture in the sent message and its name in the request
     (SKIPPED without playwright or a browser, never PASS)
"""
import base64, json, os, pathlib, shutil, socket, struct, subprocess, sys, \
    tempfile, threading, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import aethron_bridge as B
import aethron_code as C

ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n)


def png(w=40, h=24, rgb=(217, 119, 87)):
    row = b"\x00" + bytes(rgb) * w
    raw = zlib.compress(row * h)
    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
        ">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw)
        + chunk(b"IEND", b""))


SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-img-"))
PIC = SP / "0123456789abcdef.png"
PIC.write_bytes(png())
import hashlib
STORED = hashlib.sha1(PIC.read_bytes()).hexdigest()[:16] + ".png"
B64 = base64.b64encode(PIC.read_bytes()).decode()

# ── A: the translation ────────────────────────────────────────────────
blocks = C.image_blocks([str(PIC)])
check("an attached image becomes an image block plus a note naming its path",
      blocks[0]["type"] == "image" and blocks[0]["source"]["data"] == B64
      and str(PIC) in blocks[-1]["text"])
req = B.to_openai({"model": "gpt-4.1-mini", "messages": [
    {"role": "user", "content": blocks + [
        {"type": "text", "text": "build this"}]}]}, "gpt-4.1-mini")
last = req["messages"][-1]
parts = last["content"] if isinstance(last["content"], list) else []
check("the model is handed the picture as an image_url data URL",
      any(p.get("type") == "image_url" and p["image_url"]["url"]
          == "data:image/png;base64," + B64 for p in parts))
check("and the words with it", any(p.get("type") == "text"
                                   and "build this" in p["text"] for p in parts))
check("nothing says the image was omitted", "omitted" not in json.dumps(req))
req2 = B.to_openai({"model": "m", "messages": [
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                       "name": "Read", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": [blocks[0]]}]}]}, "m")
roles = [m["role"] for m in req2["messages"]]
check("a picture a tool returned follows its tool message as input",
      roles[-2:] == ["tool", "user"] and B.has_images(req2))
blind = B.strip_images(req)
check("a model without eyes gets the same request with a note instead",
      not B.has_images(blind) and "cannot see images" in json.dumps(blind)
      and "build this" in json.dumps(blind))
said = C.attached_paths(blocks[-1]["text"])
check("the saved path is read back out of the note", said == [str(PIC)])

# when measuring cannot run, the agent is told to build, not to ask
import forge_mcp as FM
import aethron_vision as V
_orig = V.ocr
V.ocr = lambda *a, **k: (V.OCR_WHY.__setitem__(0, "test: no helper"), None)[1]
try:
    msg = FM.t_screenshot_to_page({"image": str(PIC), "outdir": str(SP / "rb")})
finally:
    V.ocr = _orig
check("a skipped measurement names why and says to build from the image now",
      msg.startswith("SKIPPED") and "test: no helper" in msg
      and "build the page from it yourself NOW" in msg
      and "Do not ask" in msg)
check("the attachment note says the same",
      "do not stop to ask" in blocks[-1]["text"])

# ── B: the real engine, real bridge, recording upstream ───────────────
SEEN, REFUSE = [], {"on": False}


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        SEEN.append(body)
        if REFUSE["on"] and B.has_images(body):
            out = json.dumps({"error": {"message": "Invalid content type. "
                              "image_url is only supported by certain models."}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        txt = "I can see an orange image." if B.has_images(body) \
            else "I see no image."
        chunks = [{"choices": [{"index": 0, "delta": {"content": txt}}]},
                  {"choices": [{"index": 0, "delta": {},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}}]
        if not body.get("stream"):
            out = json.dumps({"choices": [{"index": 0, "message": {
                "role": "assistant", "content": txt},
                "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def turn(refuse):
    REFUSE["on"] = refuse
    del SEEN[:]
    up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    bsrv, burl = B.start(B.BridgeConfig(
        f"http://127.0.0.1:{up.server_address[1]}", "sk-fake", "gpt-4.1-mini"))
    ws = pathlib.Path(tempfile.mkdtemp(prefix="ae-img-ws-"))
    s = C.CodeSession(ws, {"provider": "custom", "base_url": burl,
                           "token": "aethron", "mode": "bridge",
                           "permission_policy": "automatic",
                           "aethron_tools": False, "isolate": True})
    text, err = [], ""
    try:
        s.start()
        s.send("what is in this picture?", images=[str(PIC)])
        for ev in s.events(timeout=150, idle=120):
            if ev["type"] == "text":
                text.append(ev["text"])
            elif ev["type"] == "done":
                err = ev["text"] if ev.get("error") else ""
                break
        sid, replay = s.id, s.replay()
    finally:
        s.close()
        bsrv.shutdown()
        up.shutdown()
    return "".join(text), err, list(SEEN), sid, replay


if shutil.which("claude"):
    said_text, err, seen, sid, replay = turn(False)
    check("the agent engine sends the picture through the bridge as image input",
          any(B.has_images(b) for b in seen))
    check("and the model's answer comes back", "orange" in said_text)
    you = [e for e in replay if e.get("type") == "you"]
    check("a screen re-attaching to the run gets the picture with the words",
          you and you[0].get("images") == [str(PIC)])
    hist = [e for e in C.transcript_events(sid) if e.get("type") == "you"]
    check("a restored conversation draws the picture, not the path",
          hist and hist[0].get("images") == [str(PIC)]
          and "<aethron-attached>" not in hist[0].get("text", ""))
    said_text, err, seen, _, _ = turn(True)
    check("a model that refuses images is asked again without them",
          any(B.has_images(b) for b in seen)
          and any(not B.has_images(b) and "cannot see images" in json.dumps(b)
                  for b in seen))
    check("and the turn still finishes", "no image" in said_text and not err)
else:
    print("SKIP the agent engine is not installed — B UNVERIFIED")

# ── C: the studio in a browser ────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
if not sync_playwright:
    print("SKIP playwright is not installed — C UNVERIFIED")
else:
    HOME = SP / "home"
    (HOME / "projects").mkdir(parents=True)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
    env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
    for k in list(env):
        if k.startswith("AETHRON_SUPABASE"):
            env.pop(k)
    srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                           stdout=open(SP / "studio.log", "w"),
                           stderr=subprocess.STDOUT)
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
                print("SKIP no browser playwright can start — C UNVERIFIED")
            else:
                pg = b.new_page(viewport={"width": 1300, "height": 860})
                errs, sent = [], []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.add_init_script("try{localStorage.forge_tour='done';"
                                   "localStorage.forge_news='done'}catch(e){}")
                # the agent is not what this half tests: answer start/send
                # here and keep what the page asked to send
                pg.route("**/api/code/start", lambda r: r.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"key": "k1"})))

                def on_send(r):
                    sent.append(json.loads(r.request.post_data or "{}"))
                    r.fulfill(status=200, content_type="application/json",
                              body='{"ok":true}')
                pg.route("**/api/code/send", on_send)
                pg.goto(f"http://127.0.0.1:{PORT}/")
                pg.wait_for_selector("#npurl", timeout=15000)
                pg.wait_for_timeout(800)
                pg.set_input_files(".composer .cattach-in", str(PIC))
                pg.wait_for_selector(".composer .cthumb img", timeout=10000)
                nat = pg.eval_on_selector(".composer .cthumb img",
                                          "i=>i.complete&&i.naturalWidth")
                check("an attached image shows as a thumbnail in the composer",
                      nat == 40)
                check("no file path is typed into the message",
                      pg.input_value("#npurl") == "")
                pg.fill("#npurl", "rebuild this in react")
                pg.keyboard.press("Enter")
                pg.wait_for_selector(".turn.you .youpics img", timeout=10000)
                pg.wait_for_timeout(600)
                shown = pg.eval_on_selector(".turn.you .youpics img",
                                            "i=>i.complete&&i.naturalWidth")
                check("the sent message shows the same picture", shown == 40)
                check("the request carries the image and the words",
                      sent and sent[-1].get("images") == [STORED]
                      and sent[-1].get("text") == "rebuild this in react")
                check("the composer is empty again",
                      pg.locator(".composer .cthumb").count() == 0)
                check("no page errors", not errs or print(errs))
                b.close()
    finally:
        srv.terminate()
shutil.rmtree(SP, ignore_errors=True)
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

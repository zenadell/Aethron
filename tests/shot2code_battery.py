"""A screenshot becomes real HTML and CSS, never a picture of the screenshot.

1.1.27 handed the owner the screenshot pasted in as the page's background
with two lines of text on top, and called it a rebuild. This proves the
new path cannot do that, and that it keeps the best real page it makes.

  A  offline: the reuse check, likeness maths, id stamping
  B  the model's request carries the picture in each wire's own shape,
     and a model with no eyes is asked again without it
  C  in a real browser (SKIPPED without one, never PASS):
     - the 1.1.27-style page (the screenshot as an <img>) FAILS grade_page
     - a hand-written CSS page of the same design PASSES
     - a page that hangs off a phone is caught
     - the loop refuses a round that reuses the screenshot, keeps the
       best real round over a worse later one, sends the model the
       screenshot and its own render, and stamps ids for point-and-edit
     - Remove works on a stamped page
     - a model that cannot be reached is NOT ASKED and writes no page
"""
import base64, json, os, pathlib, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-s2c-b-"))
os.environ["AETHRON_HOME"] = str(SP / "home")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import aethron_shot2code as S
import aethron_build as B
import aethron_figma_grade as G

ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n, flush=True)


REF = ROOT / "tests" / "pages" / "recognito.png"
REAL = (ROOT / "tests" / "pages" / "recognito-real.html").read_text()
# the same page built the way a developer ships it: tokens, <main>, a working
# phone menu, hover and focus states, a description and share card
CRAFTED = (ROOT / "tests" / "pages" / "recognito-crafted.html").read_text()
CARRY = ("<!doctype html><html><body style='margin:0'><img style='display:block' "
         "src='data:image/png;base64," + base64.b64encode(REF.read_bytes()).decode()
         + "'><h1 style='position:absolute;top:230px;width:100%;text-align:center;"
         "color:#fff'>Meet! Recognito</h1></body></html>")
BLACK = "<!doctype html><html><body style='margin:0;background:#000'><a href=#>x</a></body></html>"

# ── A ────────────────────────────────────────────────────────────────
r = subprocess.run([sys.executable, "aethron_shot2code.py", "--selftest"],
                   capture_output=True, text=True, timeout=120)
check("the module's own checks pass offline", r.returncode == 0 and "VERDICT: PASS" in r.stdout)
check("the 1.1.27-style page is caught from its source: it IS the screenshot",
      any("IS the screenshot" in f for f in S.reuse(CARRY, SP, REF)))
check("the hand-written page carries no picture at all", S.reuse(REAL, SP, REF) == [])

# ── B ────────────────────────────────────────────────────────────────
pic = [("image/png", b"\x89PNG fake")]
oa = B._request({"wire": "openai", "base": "http://x/v1", "model": "gpt-4.1-mini",
                 "provider": "openai"}, "k", "hello", 100, 0.2, pic)
body = json.loads(oa.data)
parts = body["messages"][0]["content"]
check("OpenAI-style: the picture goes as an image_url data URL, then the words",
      parts[0]["type"] == "image_url"
      and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
      and parts[-1] == {"type": "text", "text": "hello"})
an = B._request({"wire": "anthropic", "base": "http://x", "model": "m",
                 "provider": "anthropic"}, "k", "hello", 100, 0.2, pic)
ab = json.loads(an.data)["messages"][0]["content"]
check("Anthropic: the picture goes as a base64 image block",
      ab[0]["type"] == "image" and ab[0]["source"]["media_type"] == "image/png")
check("with no picture the request is plain text, as before",
      json.loads(B._request({"wire": "openai", "base": "http://x/v1", "model": "m",
                             "provider": "openai"}, "k", "hi", 10, 0.2).data)
      ["messages"][0]["content"] == "hi")

SEEN = []


class Blind(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = d["messages"][0]["content"]
        SEEN.append(content)
        if isinstance(content, list):
            out = json.dumps({"error": {"message": "This model does not support image input"}}).encode()
            self.send_response(400)
        else:
            out = json.dumps({"choices": [{"message": {"content": "```html\n" + REAL + "\n```"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 10, "total_tokens": 20}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = HTTPServer(("127.0.0.1", 0), Blind)
threading.Thread(target=srv.serve_forever, daemon=True).start()
_act = B._active
B._active = lambda model=None: {"wire": "openai", "provider": "custom", "model": "blind",
                                "base": f"http://127.0.0.1:{srv.server_port}/v1",
                                "local": True, "label": "test"}
try:
    led = {}
    txt = B.gemini_text("build it", led, 0.1, images=pic, max_tokens=500)
finally:
    B._active = _act
    srv.shutdown()
check("a model with no eyes is asked again with the words alone, and says so",
      len(SEEN) == 2 and isinstance(SEEN[0], list) and isinstance(SEEN[1], str)
      and "could not be sent" in SEEN[1] and led.get("no_images")
      and "Apply for Beta" in txt)

# ── C ────────────────────────────────────────────────────────────────
if not G.find_browser():
    print("SKIP no browser — C UNVERIFIED")
else:
    def page(name, html):
        p = SP / name
        p.write_text(html)
        return p

    g = S.grade(REF, page("carry.html", CARRY))
    check("grade_page FAILS the 1.1.27-style page, naming the reuse",
          g["verdict"] == "FAIL" and g["reused"])
    g2 = S.grade(REF, page("crafted.html", CRAFTED))
    check(f"grade_page PASSES the page built like a website ({g2.get('likeness', 0):.0%} alike)",
          g2["verdict"] == "PASS" and g2["likeness"] >= S.LIKENESS_FLOOR
          or print(g2.get("findings")))
    check("and every website check on it is ticked",
          g2.get("craft") and all(c["ok"] for c in g2["craft"]) and len(g2["craft"]) >= 8)
    check("and it found the real nav links and buttons",
          g2["elements"]["a"] >= 6 and g2["elements"]["button"] >= 2)
    gr = S.grade(REF, page("real.html", REAL))
    check("a page that only LOOKS right is refused: no <main>, nav gone on phones",
          gr["verdict"] == "FAIL" and gr["likeness"] >= S.LIKENESS_FLOOR
          and any("<main>" in f for f in gr["findings"])
          and any("nav links disappear" in f for f in gr["findings"]))
    gf = S.grade(REF, page("real-fin.html", S.finish_site(REAL, accent="#040201")))
    check("Aethron adds the head, focus and hover states itself, and the look does not move",
          not any(k in " ".join(gf["findings"]) for k in ("<head>", ":hover", ":focus-visible"))
          and abs(gf["likeness"] - gr["likeness"]) < 0.002)
    dead_menu = CRAFTED.split("<script>")[0] + "</main>\n</body>"
    gm = S.grade(REF, page("deadmenu.html", dead_menu))
    check("a phone menu button that opens nothing is caught",
          any("phone menu button does not work" in f for f in gm["findings"])
          or print(gm["findings"]))
    faint = CRAFTED.replace(".sub{color:var(--muted);", ".sub{color:#2a2523;")
    gc = S.grade(REF, page("faint.html", faint))
    check("text too faint to read is caught, with its contrast",
          any("too faint" in f and ":1" in f for f in gc["findings"]))
    noname = CRAFTED.replace('aria-label="Open menu">', '>')
    noname = noname.replace("&#9776;", "<svg aria-hidden='true' width='16' height='16'></svg>")
    gn = S.grade(REF, page("noname.html", noname))
    check("an icon-only button with no name is caught (screen readers say 'button')",
          any("no accessible name" in f for f in gn["findings"]) or print(gn["findings"]))
    g3 = S.grade(REF, page("black.html", BLACK))
    check("a black page FAILS for not looking like the screenshot",
          g3["verdict"] == "FAIL" and g3["likeness"] < S.LIKENESS_FLOOR)
    wide = CRAFTED.replace("</body>", "<div style='width:1460px;height:4px'></div></body>")
    g4 = S.grade(REF, page("wide.html", wide))
    check("a page that hangs off a phone is caught",
          g4["phones_checked"] and any("390px" in f for f in g4["findings"]))

    calls = []
    script = ["```html\n" + CARRY + "\n```", "```html\n" + CRAFTED + "\n```",
              "```html\n" + BLACK + "\n```"]

    def writer(prompt, images):
        calls.append((prompt, [m for m, _ in images]))
        return script[len(calls) - 1]

    rep = S.build(REF, SP / "out", writer=writer, rounds=3, log=lambda *a: None)
    site = SP / "out" / "site.html"
    rows = rep.get("rounds", [])
    check("round 1 (the screenshot as a picture) is refused by the reuse check",
          rows and rows[0]["verdict"] == "FAIL"
          and any("screenshot" in f for f in rows[0]["findings"]))
    check("round 2 is told exactly why it was refused",
          len(calls) >= 2 and "IS the screenshot" in calls[1][0])
    check("round 3 is shown the screenshot beside its own render, with what differs",
          len(calls) == 3 and calls[2][1] == ["image/png", "image/png"]
          and "looks 5" in calls[2][0] and "should be -> is" in calls[2][0])
    check("round 1 was given the screenshot", calls and calls[0][1] == ["image/png"])
    check("the best real round is kept, not the worse last one",
          rep["verdict"] == "PASS" and site.is_file()
          and "Apply for Beta" in site.read_text())
    check("the page handed over shows no picture of the screenshot",
          S.reuse(site.read_text(), site.parent, REF) == [])
    handed = site.read_text()
    check("the page handed over carries a description, share card and skip link",
          'name="description"' in handed and "og:title" in handed and "Skip to content" in handed)
    check("every element carries an id, for point-and-edit",
          site.read_text().count('data-ae-id="e') >= 20)
    check("the report says how alike, and how many rounds",
          json.loads((SP / "out" / "report.json").read_text())["likeness"] >= S.LIKENESS_FLOOR
          and len(rows) == 3)
    import aethron_edit as AE
    h = site.read_text()
    import re
    eid = re.search(r'<button data-ae-id="(e\d+)" class="primary"', h).group(1)
    h2 = AE.apply(h, [{"id": eid, "set": {"hidden": True}}])
    h2 = h2[0] if isinstance(h2, tuple) else h2
    check("Remove hides a stamped element on a model-written page",
          h2 != h and re.search(rf'data-ae-id="{eid}"[^>]*(display:\s*none|visibility:\s*hidden)', h2))

    def dead(prompt, images):
        raise B.BudgetExhausted("no key can serve this today")

    rep2 = S.build(REF, SP / "out2", writer=dead, rounds=2, log=lambda *a: None)
    check("a model that cannot be reached is NOT ASKED and writes no page",
          rep2["verdict"] == "NOT ASKED" and "no key" in rep2["why"]
          and not (SP / "out2" / "site.html").exists())

shutil.rmtree(SP, ignore_errors=True)
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

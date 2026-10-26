#!/usr/bin/env python3
"""The replacement engine may change COPY and nothing else.

    python3 tests/engine_battery.py

Found live on 2026-09-26 (the jomiez migration): a fill of "me" -> "us",
right for "Send me an email", also renamed fourteen VARIABLES named `me`
inside Framer's own runtime and turned the Telegram share link `t.me`
into `t.us`. Once another fill touched the CMS, the home page went blank
with `e.flatMap is not a function`. The engine already had a guard — in a
chunk, only text inside string literals is copy — but its scanner knew
nothing of `${…}` code inside templates, regex literals or comments. On
Framer's runtime its mistakes happened to cancel out, so it reported the
file as trusted while reading code as text.

Every case below is a shape that broke the old scanner. No network, no
browser, no key.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import forge as F  # noqa: E402

checks = []


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))


def text_at(s, needle, nth=0):
    """Is the nth occurrence of needle read as TEXT by the scanner?"""
    mask, trusted = F._js_literal_mask(s)
    i = -1
    for _ in range(nth + 1):
        i = s.index(needle, i + 1)
    return bool(mask[i]), trusted


def pair(old, new, brand=False):
    return {"old": old, "new": new, "scope": "all", "in_cms": False,
            "cms_over": False, "flex": False, "brand": brand}


# ── the scanner ──────────────────────────────────────────────────────────
s = "a={className:`${y(me,`framer-x`,p)} framer-1`};b=me;"
ck("code inside ${…} is not text",
   text_at(s, "me")[0] is False, s)
ck("…a template nested inside it is text",
   text_at(s, "framer-x")[0] is True)
ck("…and the outer template's text after the }` is text again",
   text_at(s, " framer-1")[0] is True)
ck("…and the code after the whole template is code",
   text_at(s, "me", 1)[0] is False and text_at(s, "me", 1)[1])

s = "q=/['\"]/.test(x);me(1);t=\"say me\";"
ck("a regex holding quotes does not flip the scanner",
   text_at(s, "me(")[0] is False and text_at(s, "say me")[0] is True
   and text_at(s, "me(")[1], s)

s = "// don't stop\nme=1;/* it's */t='hi me';"
ck("comments holding apostrophes do not flip the scanner",
   text_at(s, "me=")[0] is False and text_at(s, "hi me")[0] is True, s)

s = "a=b/2;c=\"me\";d=e/f;g=me;"
ck("division is not mistaken for a regex",
   text_at(s, "\"me\"")[0] is True and text_at(s, "g=me")[0] is False
   and F._js_literal_mask(s)[1], s)

s = "x=`a ${`b ${c} d`} e`;me();"
ck("templates nested two deep close where the language closes them",
   text_at(s, " e`")[0] is True and text_at(s, "me()")[0] is False
   and F._js_literal_mask(s)[1], s)

# the exact shape from Framer's runtime that shipped broken
s = ("function Vu(e){return e===`a`?`span`:Te(e)&&me(e)===`a`?P.span:e}"
     "let o=`${d}${k}`,F=u[fe],me=F?.path;")
m, trusted = F._js_literal_mask(s)
ck("the runtime fragment that broke jomiez: no `me` read as text",
   trusted and not any(m[i] for i in range(len(s))
                       if s.startswith("me", i)), s)

ck("an unterminated string is reported untrusted, not guessed",
   F._js_literal_mask("a='oops\nb=1;")[1] is False)
ck("an unclosed ${ is reported untrusted",
   F._js_literal_mask("a=`x ${b")[1] is False)

# ── the engine ───────────────────────────────────────────────────────────
chunk = ("function Vu(e){return Te(e)&&me(e)===`a`}"
         "let F=u[fe],me=F?.path;"
         "c:`${y(me,`framer-x`,p)} framer-1`,"
         "t:`Send me an email`,"
         "share:`https://t.me/share/url?url=${r}`,bare:`t.me/joinchat`")
out = F._apply(chunk, [pair("me", "us")], code=True)
ck("me -> us rewrites the sentence in the chunk",
   "Send us an email" in out, out)
ck("…renames no variable",
   "&&me(e)" in out and "me=F?.path" in out and "y(me," in out, out)
ck("…and leaves the Telegram address alone",
   "https://t.me/share" in out and "t.me/joinchat" in out, out)

html = ('<a href="https://t.me/jomiez">Contact me</a>'
        '<p>Email hello@me.com or visit me.dev today</p>')
out = F._apply(html, [pair("me", "us")])
ck("in a page: the copy changes, the address does not",
   "Contact us" in out and 'href="https://t.me/jomiez"' in out
   and "hello@me.com" in out and "me.dev" in out, out)

out = F._apply('<a href="https://lewius.framer.website/">Lewius</a>',
               [pair("lewius", "jomiez", brand=True),
                pair("Lewius", "Jomiez", brand=True)])
ck("the brand is the exception: it goes everywhere, addresses too",
   "lewius" not in out.lower(), out)

ck("the address test works on CMS bytes the same way it does on text",
   F._in_url(b"\x05https://t.me/x\x00", 11, 13)
   and not F._in_url(b"\x05Send me\x00", 6, 8))

ck("a slash between words is not an address (UI/UX stays copy)",
   "Code/UX" in F._apply("<p>Design/UX</p>", [pair("Design", "Code")]))

# ── a slug or a path is an address too ─────────────────────────────────
# "the" -> "our" turned `./the-role-of-typography-…` into a link to a page
# that does not exist, and "concept" -> "design" turned a CMS slug into
# `from-design -to-…` (space-padded to its byte slot).
out = F._apply('<a href="./the-role-of-typography">Read the post</a>'
               '<p>A behind-the-scenes look</p>', [pair("the", "our")],
               escaped=True)
ck("a word inside a relative link is left alone, the sentence changes",
   'href="./the-role-of-typography"' in out and "Read our post" in out,
   out)
ck("…and a hyphenated compound is one term, not three words",
   "behind-the-scenes" in out, out)
out = F._apply("s={slug:`from-concept-to-execution`,t:`concept`}",
               [pair("concept", "design")], code=True)
ck("a slug in the code keeps its words; the copy beside it changes",
   "from-concept-to-execution" in out and "t:`design`" in out, out)
ck("the slug rule reads a CMS string past a printable length byte",
   F._in_url(b"\x05*the-role-of-typography\x00", 2, 5))

# ── a replacement is never read again ───────────────────────────────────
# Found on the same project the same morning: "Design System" -> "Design
# Systems" shipped as "Design Systemss" in the code and "Design
# Systemssss" in the pages, because every pass re-read the previous
# pass's output and the new text contains the old one.
ds = pair("Design System", "Design Systems")
ds["flex"] = True
page = F._apply("<p>Design System</p><p>Design\n  System</p>", [ds],
                escaped=True)
ck("a new text containing its old one lands once in a page",
   page == "<p>Design Systems</p><p>Design Systems</p>", page)
code = F._apply("a={c:`Design System`,d:`Design\\nSystem`};", [ds],
                code=True)
ck("…and once in the code, so page and code still agree",
   code == "a={c:`Design Systems`,d:`Design Systems`};", code)
st = {}
F._apply("<p>Design System</p>", [ds], escaped=True, stats=st)
ck("…and it is counted once, not once per pass",
   st.get("Design System") == 1, str(st))

out = F._apply("<p>Income &amp; expense tracking</p>",
               [pair("Income & expense tracking", "Invoices & payments"),
                pair("Income", "Images")], escaped=True)
ck("in a page, the longer line wins over a short one inside it, "
   "even when the page spells & as &amp;",
   out == "<p>Invoices &amp; payments</p>", out)

# ── a word of an animated heading is not a line ─────────────────────────
# Framer puts each word of an animated heading in its own span. Inventory
# offered every word as a line, the agent rewrote headings word by word,
# and each one-word fill applied site-wide: "the" -> "our" in 217 places.
import json as _json      # noqa: E402
import tempfile           # noqa: E402
W = '<span style="display:inline-block;opacity:0.001">{}</span>'
heading = " ".join(W.format(w) for w in "Over the years".split())
second = " ".join(W.format(w) for w in "Building brands through design".split())
proj = Path(tempfile.mkdtemp(prefix="aethron-words-"))
(proj / "pristine").mkdir()
(proj / "pristine" / "index.html").write_text(
    f"<h2>{heading}</h2><h2>{second}</h2>"
    "<p>We read the brief and design the plan.</p><div>Research</div>",
    encoding="utf-8")
cm = {"strings": [
    {"old": "Over the years", "new": ""},
    {"old": "Building brands through design",
     "new": "Engineering software that scales"},
    {"old": "the", "new": "our"}, {"old": "years", "new": "decades"},
    {"old": "design", "new": "and AI"},
    {"old": "We read the brief and design the plan.", "new": ""},
    {"old": "Research", "new": "Discovery"}],
    "images": [], "links": []}
(proj / "copy_map.json").write_text(_json.dumps(cm), encoding="utf-8")
(proj / "forge.json").write_text("{}", encoding="utf-8")
frag = F.word_fragments(proj)
ck("the words of animated headings are recognised as words, not lines",
   {"the", "years", "design"} <= set(frag) and "Research" not in frag,
   str(sorted(frag)))
pairs = F._pairs_from_map(proj, [])
olds = {p["old"]: p["new"] for p in pairs}
ck("a heading word's fill no longer applies across the site",
   not {"the", "years", "design"} & set(olds), str(olds))
ck("…its fill is kept, inside its own heading only",
   olds.get("Over the years") == "Over our decades", str(olds))
ck("a heading written as a whole line wins over its word fills",
   olds.get("Building brands through design")
   == "Engineering software that scales", str(olds))
ck("a one-word LABEL (its own text node) is still a line",
   olds.get("Research") == "Discovery", str(olds))
page = (proj / "pristine" / "index.html").read_text(encoding="utf-8")
t, held = F._regen_word_runs(page, pairs)
t = F._apply(t, pairs, escaped=True)
for i, run in enumerate(held):
    t = t.replace(f"\x00W{i}\x00", run)
ck("the page rebuilds each heading word by word from its new line",
   " ".join(W.format(w) for w in "Engineering software that scales".split())
   in t and " ".join(W.format(w) for w in "Over our decades".split()) in t,
   t[:300])
ck("…and a sentence that was never rewritten keeps its own words",
   "We read the brief and design the plan." in t, t)
# the agent's tools, on the same project
import os as _os          # noqa: E402
home = Path(tempfile.mkdtemp(prefix="aethron-home-"))
(home / "projects").mkdir()
import shutil as _sh      # noqa: E402
_sh.copytree(proj, home / "projects" / "words")
_os.environ["AETHRON_HOME"] = str(home)
import forge_mcp as M     # noqa: E402
listing = M.t_get_content({"project": "words", "brand_notes": False})
ck("the agent's content list does not offer heading words as lines",
   '"old": "the"' not in listing and '"old": "Over the years"' in listing
   and "write the whole line" in listing, listing[:400])
got = M.t_set_content_bulk({"project": "words", "build": False, "entries": [
    {"old": "the", "new": "a"},
    {"old": "We read the brief and design the plan.",
     "new": "We scope the work and plan the build."}]})
ck("a write to a heading word is refused, naming its heading",
   "applied 1, rejected 1" in got and "Over the years" in got, got)
_sh.rmtree(home, ignore_errors=True)
_sh.rmtree(proj, ignore_errors=True)

# ── a number is not a word (2026-10-21, the aethron-web site) ────────────
# A fill "18" -> "0" rewrote SVG path data ("M18 6") and broke every icon;
# "100%" -> "0" rewrote width:100% across the chunks and squashed pages.
out = F._apply('<p>118 and 1.18 and 18.5 and 18</p>', [pair("18", "40")])
ck("a number fill never lands inside another number",
   out == '<p>118 and 1.18 and 18.5 and 40</p>', out)

# ── a changed build gets new addresses (2026-10-26, the aethron-web site) ─
# The build rewrites chunks without renaming them; a browser holding last
# week's copy kept running the broken one after the fix had shipped.
import re, tempfile as _tf
from pathlib import Path as _P
def _site(body):
    d = _P(_tf.mkdtemp()); (d / "assets/chunks").mkdir(parents=True)
    (d / "assets/cms").mkdir()
    (d / "assets/chunks/a.X1.mjs").write_text(
        'import"./b.X2.mjs";new URL(`./c-0.framercms`,`${location.origin}/assets/cms/c.js`);' + body)
    (d / "assets/chunks/b.X2.mjs").write_text("export const b=1")
    (d / "assets/cms/c-0.framercms").write_bytes(b"data")
    (d / "index.html").write_text('<script type="module" src="./assets/chunks/a.X1.mjs"></script>'
                                  '<link rel="modulepreload" href="./assets/chunks/b.X2.mjs">')
    F._stamp_versions(d); return d
s1, s2, s3 = _site("1"), _site("1"), _site("2")
h1 = (s1 / "index.html").read_text(); a1 = (s1 / "assets/chunks/a.X1.mjs").read_text()
v = re.search(r"\?v=([0-9a-f]+)", h1)
ck("every module and CMS address carries the build's version",
   v and h1.count("?v=" + v.group(1)) == 2
   and a1.count("?v=" + v.group(1)) == 3, h1 + a1)
ck("an unchanged build keeps its addresses, a changed one gets new ones",
   h1 == (s2 / "index.html").read_text() and h1 != (s3 / "index.html").read_text())
for _d in (s1, s2, s3): _sh.rmtree(_d, ignore_errors=True)

# ── a link to the owner's own page is followed, not swallowed (2026-10-26) ─
# "mailto:" -> "/start": Framer's router took the click and went nowhere.
_cfg = {"platform": "framer", "routes": {"": "index.html", "projects": "projects.html"}}
_t = F._inject_link_routes("<head></head>", _cfg, [
    pair("mailto:a@b.co", "/start#000000"), pair("x", "/projects"),
    pair("y", "/assets/a.png"), pair("z", "https://jomiez.com")])
_m = re.search(r"\}\)\((\{.*?\})\);", _t)
_R = _json.loads(_m.group(1)) if _m else {}
ck("a link to a page outside the template is loaded, without its padding",
   _R == {"start": "/start"}, _t[-200:])

bad = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
sys.exit(1 if bad else 0)

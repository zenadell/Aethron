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

bad = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
sys.exit(1 if bad else 0)

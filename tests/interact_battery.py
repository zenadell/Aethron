#!/usr/bin/env python3
"""Can Aethron add something that OPENS — a pop-up on a button — and prove it works for a person?

The owner's example: "make the Mac OS button, or the iOS button, or all four, pop up a smaller
box with options when I hover on it or click on it", and get it right without breaking anything.
A pop-up can look finished and still fail a person in a dozen ways, so every scenario here is a
model's reply played against a real page in a real browser, and Aethron USES the result: hovers,
carries the pointer onto what opened, leaves, clicks, presses Escape, clicks outside.

Honest replies must land. Replies that show the pop-up before anyone acts, lose it on the way
from the button, hide it behind the card, cut it off, put it far away, never close it, break the
button's own toggle, change how the button looks, or leave an invisible layer over the next
button must be refused with the measured reason — and the page left exactly as it was.

No key, no network: the page is built here, and the model is scripted.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_change as C            # noqa: E402
import aethron_edit as E              # noqa: E402
import aethron_figma_grade as GR      # noqa: E402
import aethron_gradient as G          # noqa: E402

OK = FAIL = 0
W, H = 900, 560
MODEL = {"base": (2, 2, 4), "layers": [
    {"cx": 120, "cy": 520, "rx": 520, "ry": 330, "c": (48, 78, 214), "a0": 1.0, "a1": 0.6, "s": 0.4},
    {"cx": 760, "cy": 420, "rx": 460, "ry": 360, "c": (90, 150, 225), "a0": 0.95, "a1": 0.5,
     "s": 0.35, "ease": "smooth"}]}


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   ({detail})" if detail else ""), flush=True)


def chip(eid, x, w, lx, label):
    return (f'<button type="button" aria-pressed="false" class="sf" data-ae-id="{eid}" style="left:{x}px;top:373px;'
            f'width:{w}px;height:38px;border-radius:19px;background:#DBE3F3"><span class="t" style="left:{lx}px;'
            f'top:9px;font-size:16.00px;color:#848B9C">{label}</span></button>')


def page():
    still = ('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0}'
             f'.page{{position:relative;width:{W}px;height:{H}px;overflow:hidden;'
             f'font-family:Helvetica,Arial,sans-serif;background:{G.css(MODEL, W, H, units="%")}}}'
             '.t{position:absolute;white-space:nowrap;line-height:1.2;margin:0}'
             '.sf{position:absolute;box-sizing:border-box}'
             'button.sf{padding:0;margin:0;border:0;font:inherit;color:inherit;cursor:pointer}'
             'button.sf:hover{filter:brightness(1.12)}'
             '.prompt{background:transparent;border:0;outline:none;resize:none;padding:0;font-family:inherit}'
             '</style></head><body><main class="page" data-ae-id="bg">'
             '<h1 class="t" data-ae-id="t00" style="left:80px;top:40px;font-size:40.00px;color:#FFFFFF">'
             'Build Apps</h1>'
             '<div class="sf" data-ae-id="s00" style="left:150px;top:190px;width:600px;height:240px;'
             'border-radius:40px;background:rgba(240,239,240,0.8)"></div>'
             '<textarea class="t prompt" data-ae-id="t01" placeholder="Type something to generate" '
             'style="left:180px;top:220px;width:540px;height:110px;font-size:18.00px;color:#858795"></textarea>'
             '<button type="submit" class="sf" data-ae-id="s01" style="left:590px;top:370px;width:130px;'
             'height:44px;border-radius:22px;background:#101014"><span class="t" style="left:30px;top:12px;'
             'font-size:17.00px;color:#A6A1B0">Generate</span></button>'
             + chip("s02", 180, 96, 18, "Android") + chip("s03", 290, 96, 16, "Mac OS") + chip("s04", 400, 70, 18, "IOS")
             + '<p class="t" data-ae-id="t02" style="left:330px;top:500px;font-size:18.00px;color:#9CA1A4">'
             'Launch app 10x faster</p></main>'
             '<script>document.querySelectorAll("button[aria-pressed]").forEach(function(b){'
             'b.addEventListener("click",function(){b.setAttribute("aria-pressed",'
             'b.getAttribute("aria-pressed")!=="true");});});</script></body></html>')
    alive, written = E.animate_background(still, "drift+breathe", 10, 0.5)
    return alive if written else None


MAC = 'color:#848B9C">Mac OS</span></button>'
CARD = '<div class="sf" data-ae-id="s00"'
POP = ('<div class="ae-pop" data-ae-id="p03" role="menu"><a href="#" role="menuitem">Download for Mac</a>'
       '<a href="#" role="menuitem">Apple silicon</a><a href="#" role="menuitem">Intel</a></div>')
HIDDEN = "opacity:0;visibility:hidden;transition:opacity .15s,visibility .15s"
OPEN_RULE = '.ae-pop.open,.ae-pop:hover,[data-ae-id="s03"]:hover+.ae-pop{opacity:1;visibility:visible}'
CSS = ('.ae-pop{position:absolute;left:290px;top:415px;width:170px;padding:6px;box-sizing:border-box;'
       'background:#FFFFFF;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,0.25);z-index:20;display:grid;'
       'gap:2px;' + HIDDEN + '}' + OPEN_RULE +
       '.ae-pop a{display:block;padding:6px 8px;color:#15171C;font-size:14px;line-height:1.2;'
       'text-decoration:none;border-radius:6px;font-family:Helvetica,Arial,sans-serif}')
TOGGLE = "t.addEventListener('click',function(){set(!p.classList.contains('open'));});"
CLOSERS = ("document.addEventListener('click',function(e){if(!p.contains(e.target)&&!t.contains(e.target))set(false);});"
           "document.addEventListener('keydown',function(e){if(e.key==='Escape')set(false);});")
JS = ("(function(){var t=document.querySelector('[data-ae-id=\"s03\"]'),p=document.querySelector('[data-ae-id=\"p03\"]');"
      "if(!t||!p)return;t.setAttribute('aria-haspopup','menu');t.setAttribute('aria-expanded','false');"
      "function set(o){p.classList.toggle('open',o);t.setAttribute('aria-expanded',String(o));}"
      + TOGGLE + CLOSERS + "})();")
HOVER = {"kind": "appears_on", "id": "p03", "trigger": "s03", "on": "hover", "items": 3}
CLICK = dict(HOVER, on="click")
BOTH = "Give the Mac OS button a small pop-up with options when I hover on it or click on it"
ON_HOVER = "Show a pop-up with options when I hover the Mac OS button"
ON_CLICK = "Show a pop-up with options when I click the Mac OS button"


def plan(claims, css=CSS, js=JS, find=MAC, before=False):
    return {"patches": [{"find": find, "replace": POP + find if before else find + POP}], "css": css, "js": js,
            "touches": ["s03", "p03"], "expect": list(claims)}


def scripted(*plans):
    it = iter(plans)
    prompts = []

    def call(prompt):
        prompts.append(prompt)
        return json.dumps(next(it))
    call.prompts = prompts
    return call


def run(html, work, request, *plans, retries=0):
    t = time.time()
    r = C.change(html, request, work, call=scripted(*plans), retries=retries)
    r["seconds"] = round(time.time() - t, 1)
    return r


def said(r, phrase):
    return any(phrase in p for p in r["problems"])


def main():
    if not GR.find_browser():
        print("VERDICT: SKIPPED — no browser to use the page with; nothing here is proven")
        return 0
    work = Path(tempfile.mkdtemp(prefix="ae-interact-battery-"))
    html = page()
    check("the fixture has four buttons, a toggle on the chips and a moving background",
          html is not None and C._alive_block(html) and html.count('aria-pressed="false"') == 3)
    if not html:
        return 1

    print("\n── a pop-up that works for a person lands")
    r = run(html, work, BOTH, plan((HOVER, CLICK)))
    check("hover + click pop-up on Mac OS, hidden until used, is APPLIED", r["verdict"] == "APPLIED", C.report(r))
    check("  ...the pointer can travel onto it and it stays open",
          any("stays open with the pointer on it" in m for m in r["measured"]), str(r["measured"]))
    check("  ...it closes when the pointer leaves, and with a click outside",
          any("closes when the pointer leaves" in m for m in r["measured"])
          and any("closes with" in m and "a click outside" in m for m in r["measured"]), str(r["measured"]))
    check("  ...its three options are read off the page",
          any("3 option(s): 'Download for Mac'" in m for m in r["measured"]), str(r["measured"]))
    check("  ...and at rest nothing on screen changed", any("nothing outside the named elements changed" in m
                                                          for m in r["measured"]), str(r["measured"]))

    print("\n── a pop-up that fails a person is refused, with what was measured")
    r = run(html, work, ON_HOVER, plan((HOVER,), css=CSS.replace(HIDDEN, "")))
    check("already showing before anyone hovers", r["verdict"] == "REFUSED" and said(r, "already shows"),
          str(r["problems"]))
    unreachable = (CSS.replace("top:415px", "top:423px").replace("display:grid;gap:2px;" + HIDDEN, "display:none;gap:2px")
                   .replace("{opacity:1;visibility:visible}", "{display:grid}"))
    r = run(html, work, ON_HOVER, plan((HOVER,), css=unreachable))
    check("gone the moment the pointer leaves the button, so it can never be reached",
          r["verdict"] == "REFUSED" and said(r, "cannot reach"), str(r["problems"]))
    stuck_js = JS.replace(TOGGLE, "t.addEventListener('click',function(){set(true);});").replace(CLOSERS, "")
    r = run(html, work, ON_CLICK, plan((CLICK,), css=CSS.replace(OPEN_RULE, ".ae-pop.open{opacity:1;visibility:visible}"),
                                       js=stuck_js))
    check("opens on click and can never be closed", r["verdict"] == "REFUSED" and said(r, "cannot be closed"),
          str(r["problems"]))
    stop_js = JS.replace(TOGGLE, "t.addEventListener('click',function(e){set(!p.classList.contains('open'));"
                                 "e.stopImmediatePropagation();},true);")
    r = run(html, work, ON_CLICK, plan((CLICK,), js=stop_js))
    check("opens, but swallows the click the Mac OS toggle needed",
          r["verdict"] == "REFUSED" and said(r, "no longer does what it did: aria-pressed"), str(r["problems"]))
    r = run(html, work, ON_HOVER, plan((HOVER,), css=CSS.replace("top:415px", "top:530px")))
    check("cut off by the bottom of the page", r["verdict"] == "REFUSED" and said(r, "cut off"), str(r["problems"]))
    behind = CSS.replace("z-index:20;", "").replace("left:290px;top:415px", "left:470px;top:330px")
    r = run(html, work, ON_CLICK, plan((CLICK,), css=behind, find=CARD, before=True))
    check("opens behind the glass card", r["verdict"] == "REFUSED" and said(r, "covered by s00"), str(r["problems"]))
    ghost = (CSS.replace("left:290px;top:415px", "left:380px;top:360px")
             .replace(HIDDEN, "opacity:0;transition:opacity .15s").replace("{opacity:1;visibility:visible}", "{opacity:1}"))
    r = run(html, work, ON_HOVER, plan((HOVER,), css=ghost))
    check("hidden only by opacity, so it sits invisibly over the IOS button and eats its clicks",
          r["verdict"] == "REFUSED" and said(r, "s04 can no longer be clicked"), str(r["problems"]))
    r = run(html, work, ON_HOVER, plan((HOVER,), css=CSS.replace("left:290px;top:415px", "left:600px;top:60px")))
    check("opens far from its button", r["verdict"] == "REFUSED" and said(r, "away from s03"), str(r["problems"]))
    r = run(html, work, ON_HOVER, plan((HOVER,), css=CSS + 'button.sf[data-ae-id="s03"]:hover{filter:none}'))
    check("works, but the Mac OS button no longer brightens on hover",
          r["verdict"] == "REFUSED" and said(r, "no longer looks as it did"), str(r["problems"]))

    print("\n── the claims must match the words")
    r = run(html, work, "Give all four buttons a pop-up with options when I hover them", plan((HOVER,)))
    check("'all four buttons' done on one is refused before rendering",
          said(r, "covers 4") and not r["measured"], str(r["problems"]))
    r = run(html, work, "Show a pop-up with options when I hover the IOS button", plan((HOVER,)))
    check("the IOS button asked for, the Mac OS button given, is refused before rendering",
          said(r, "names the 'IOS' button") and not r["measured"], str(r["problems"]))

    print("\n── a look that changes on hover is measured while hovering")
    # !important is not decoration: the button carries its background INLINE, and an inline
    # declaration outranks any stylesheet rule. Without it a real mouse sees no change either —
    # which Aethron measured, and refused, the first time this test was run.
    white = 'button.sf[data-ae-id="s01"]:hover{background:#FFFFFF!important}'
    look = {"kind": "style_on", "id": "s01", "on": "hover", "property": "background", "equals": "#FFFFFF"}
    r = run(html, work, "Make the Generate button turn white when I hover it",
            {"css": white, "touches": ["s01"], "expect": [look]})
    check("Generate turning white on hover is APPLIED", r["verdict"] == "APPLIED", C.report(r))
    check("  ...measured while hovering, white only then", any("-> rgb(255, 255, 255)" in m for m in r["measured"]),
          str(r["measured"]))
    r = run(html, work, "Make the Generate button turn white when I hover it",
            {"css": white.replace("#FFFFFF", "#22CC66"), "touches": ["s01"], "expect": [look]})
    check("a claimed white that is really green is refused", r["verdict"] == "REFUSED", str(r["problems"]))
    print(f"\ninteract battery: {OK} ok, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

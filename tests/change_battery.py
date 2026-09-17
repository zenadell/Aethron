#!/usr/bin/env python3
"""Can Aethron change anything it built — and does it catch a change that is not the one asked?

The owner's standard, in two halves that pull against each other. An agent that cannot
change its output at the user's command is rubbish; an agent that changes it and gets it
wrong, or does something else, is worse. So every scenario here is a MODEL'S REPLY played
against a real page in a real browser: honest replies must land, and replies that lie,
overreach, go the wrong way or break something must be refused with the measured reason,
leaving the page exactly as it was.

No key, no network: the page is built here with local fonts, and the model is scripted.
"""
import json
import re
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
     "s": 0.35, "ease": "smooth"},
    {"cx": 450, "cy": 600, "rx": 380, "ry": 170, "c": (240, 246, 252), "a0": 1.0, "a1": 0.7, "s": 0.3}]}


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   ({detail})" if detail else ""), flush=True)


def page():
    still = ('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0}'
             f'.page{{position:relative;width:{W}px;height:{H}px;overflow:hidden;'
             f'font-family:Helvetica,Arial,sans-serif;background:{G.css(MODEL, W, H, units="%")}}}'
             '.t{position:absolute;white-space:nowrap;line-height:1.2;margin:0}'
             '.sf{position:absolute;box-sizing:border-box}'
             'button.sf{padding:0;margin:0;border:0;font:inherit;color:inherit}'
             '.prompt{background:transparent;border:0;outline:none;resize:none;padding:0;font-family:inherit}'
             '.prompt::placeholder{color:var(--ph);opacity:1}'
             '</style></head><body><main class="page" data-ae-id="bg">'
             '<h1 class="t" data-ae-id="t00" style="left:80px;top:40px;font-size:40.00px;color:#FFFFFF">'
             'Build Apps</h1>'
             '<div class="sf" data-ae-id="s00" style="left:150px;top:190px;width:600px;height:240px;'
             'border-radius:40px;background:rgba(240,239,240,0.8);box-shadow:0 10px 20px rgba(0,0,0,0.3)"></div>'
             '<textarea class="t prompt" data-ae-id="t01" placeholder="Type something to generate" '
             'style="left:180px;top:220px;width:540px;height:140px;font-size:18.00px;color:#858795;--ph:#858795">'
             '</textarea>'
             '<button type="submit" class="sf" data-ae-id="s01" style="left:590px;top:370px;width:130px;'
             'height:44px;border-radius:22px;background:#101014"><span class="t" style="left:30px;top:12px;'
             'font-size:17.00px;color:#A6A1B0">Generate</span></button>'
             '<button type="button" class="sf" data-ae-id="s02" style="left:180px;top:373px;width:96px;'
             'height:38px;border-radius:19px;background:#DBE3F3"><span class="t" style="left:18px;top:9px;'
             'font-size:16.00px;color:#848B9C">Android</span></button>'
             '<p class="t" data-ae-id="t02" style="left:330px;top:500px;font-size:18.00px;color:#9CA1A4">'
             'Launch app 10x faster</p>'
             '</main></body></html>')
    alive, written = E.animate_background(still, "drift+breathe", 10, 0.5)
    return alive if written else None


def scripted(*plans):
    it = iter(plans)
    prompts = []

    def call(prompt):
        prompts.append(prompt)
        return json.dumps(next(it))
    call.prompts = prompts
    return call


HEIGHT = "Can you reduce this chat box height a little bit?"
TYPING = "Make the chat box automatically type different texts, one after another"
ORANGE = "Change the background animation to orange blending with black"
SHORTER = {"kind": "size", "id": "s00", "dimension": "height", "change": "-10%"}
CARD = {"find": "width:600px;height:240px", "replace": "width:600px;height:216px"}
FOLLOW = [{"find": "left:590px;top:370px", "replace": "left:590px;top:346px"},
          {"find": "left:180px;top:373px", "replace": "left:180px;top:349px"}]
# The text box reaches y=360; buttons moved up to y=346 would sit inside its lower edge.
TEXTBOX = {"find": "width:540px;height:140px", "replace": "width:540px;height:116px"}
GOOD = {"patches": [CARD] + FOLLOW + [TEXTBOX], "touches": ["s00", "t01"], "expect": [SHORTER]}
TYPER = ("(function(){var t=document.querySelector('[data-ae-id=\"t01\"]');if(!t)return;"
         "if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;"
         "var words=['Build me a habit tracker','Design a recipe app for two','Ship a budget planner'],"
         "w=0,i=0,del=false;function tick(){var s=words[w];if(!del){i++;if(i>s.length){del=true;"
         "setTimeout(tick,1100);return;}}else{i--;if(i<=0){del=false;i=0;w=(w+1)%words.length;}}"
         "t.setAttribute('placeholder',s.slice(0,i));setTimeout(tick,del?30:65);}tick();})();")


def recolour(src, everything=False, flatten=False):
    """The background's colours become orange at their OWN lightness (Aethron's tone map); white and
    black keep their light. Counted, so both copies of the gradient change together.
    `flatten=True` turns every glow — the white core too — into full orange, the way a model did
    live; `everything=True` is the careless version that also recolours the glass card."""
    out = []
    scope = src if everything else (C._page_rule(src) or "")
    mapping = C.tone_map(scope, "orange")
    for token in sorted(set(re.findall(r"rgba\((\d+),(\d+),(\d+),", scope))):
        r, g, b = map(int, token)
        if max(r, g, b) < 20:
            continue
        new = (min(255, 150 + r // 2), 60 + g // 5, 10) if (flatten or everything) else mapping.get((r, g, b))
        if not new or new == (r, g, b):
            continue
        find = f"rgba({r},{g},{b},"
        out.append({"find": find, "replace": f"rgba({new[0]},{new[1]},{new[2]},", "count": src.count(find)})
    return out


def slim_first_glow(src):
    """Rebuild the first glow layer with only its first and last stops, in both copies — the shape
    change a model made live when asked only to recolour."""
    patches = []
    for block in (C._page_rule(src), C._alive_block(src)):
        m = re.search(r"radial-gradient\(", block)
        depth, end = 0, None
        for j in range(m.end() - 1, len(block)):
            depth += (block[j] == "(") - (block[j] == ")")
            if depth == 0:
                end = j + 1
                break
        layer = block[m.start():end]
        args = C._split_top(layer[len("radial-gradient("):-1])
        slim = "radial-gradient(" + ", ".join([args[0], args[1], args[-1]]) + ")"
        patches.append({"find": block, "replace": block.replace(layer, slim, 1)})
    return patches


def run(html, work, request, *plans, retries=0):
    call = scripted(*plans)
    t = time.time()
    r = C.change(html, request, work, call=call, retries=retries)
    r["seconds"] = round(time.time() - t, 1)
    r["prompts"] = call.prompts
    return r


def main():
    if not GR.find_browser():
        print("VERDICT: SKIPPED — no browser to measure with; nothing here is proven")
        return 0
    work = Path(tempfile.mkdtemp(prefix="ae-change-battery-"))
    html = page()
    check("the fixture page has a moving background to protect", html is not None and C._alive_block(html))
    if not html:
        return 1

    print("\n── 'reduce this chat box height a little bit'")
    r = run(html, work, HEIGHT, {"patches": [CARD] + FOLLOW, "touches": ["s00"], "expect": [SHORTER]})
    check("buttons that follow the card up INTO the text box are REFUSED, with both edges",
          r["verdict"] == "REFUSED" and any("now overlaps t01" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, HEIGHT, GOOD)
    check("an honest -10% — buttons follow, text box shortened to clear them — is APPLIED",
          r["verdict"] == "APPLIED", C.report(r))
    check("  ...measured on screen, nothing outside it changed",
          any("nothing outside the named elements changed" in m for m in r["measured"]), str(r["measured"]))
    check("  ...in well under a minute", r["seconds"] < 60, str(r["seconds"]))
    check("  ...and the report carries the measured heights", any("240.0 -> 216.0" in m for m in r["measured"]),
          str(r["measured"]))
    check("  ...and names what changed by what it holds", any("s00 (holding" in t for t in r["targets"]),
          str(r["targets"]))
    r = run(html, work, HEIGHT, dict(GOOD, touches=GOOD["touches"] + ["bg", "t00"]))
    check("the same change, naming the background and the headline too, is REFUSED — nothing measures them",
          r["verdict"] == "REFUSED" and any("bg, t00 were named" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, HEIGHT, {"patches": [{"find": "height:240px", "replace": "height:240px"}],
                                 "touches": ["s00"], "expect": [SHORTER]})
    check("a reply that CLAIMS -10% and changes nothing is REFUSED", r["verdict"] == "REFUSED")
    check("  ...with the measurement that proves it", any("240.0 -> 240.0" in p for p in r["problems"]),
          str(r["problems"]))
    check("  ...and the page is left exactly as it was", r["html"] == html)
    r = run(html, work, HEIGHT, {"patches": [CARD], "touches": ["s00"], "expect": [SHORTER]})
    check("a shorter card that leaves its buttons hanging off it is REFUSED",
          r["verdict"] == "REFUSED" and any("hangs outside" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, HEIGHT, {"patches": [{"find": "width:600px;height:240px", "replace": "width:600px;height:264px"}],
                                 "touches": ["s00"], "expect": [dict(SHORTER, change="+10%")]})
    check("a TALLER card, honestly claimed, is REFUSED — the user said reduce",
          r["verdict"] == "REFUSED" and any("reduce" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, HEIGHT, {"patches": [{"find": "width:600px;height:240px", "replace": "width:600px;height:144px"},
                                             {"find": "left:590px;top:370px", "replace": "left:590px;top:274px"},
                                             {"find": "left:180px;top:373px", "replace": "left:180px;top:277px"}],
                                 "touches": ["s00", "t01"],
                                 "expect": [dict(SHORTER, change="-40%")]})
    check("'a little' done as -40% is REFUSED", any("a little" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, HEIGHT, {"patches": [{"find": "width:600px;height:240px", "replace": "width:560px;height:216px"}]
                                 + FOLLOW, "touches": ["s00"], "expect": [SHORTER]})
    check("only the height was asked for; a card that also got narrower is REFUSED",
          any("width also changed" in p for p in r["problems"]), str(r["problems"]))

    print("\n── 'make the chat box automatically type different texts'")
    r = run(html, work, TYPING, {"js": TYPER, "touches": ["t01"],
                                 "expect": [{"kind": "changes_over_time", "id": "t01"}]})
    check("a real typing script is APPLIED", r["verdict"] == "APPLIED", C.report(r))
    check("  ...followed over time: different texts, typed letter by letter, again and again",
          any("typed letter by letter" in m and m.startswith("ok") for m in r["measured"]), str(r["measured"]))
    check("  ...and a person typing into the box keeps their words",
          any("typing is kept" in m for m in r["measured"]), str(r["measured"]))
    once = ("(function(){var t=document.querySelector('[data-ae-id=\"t01\"]'),s='Build me a habit tracker',i=0;"
            "function tick(){i++;t.setAttribute('placeholder',s.slice(0,i));if(i<s.length)setTimeout(tick,65);}"
            "tick();})();")
    r = run(html, work, TYPING, {"js": once, "touches": ["t01"],
                                 "expect": [{"kind": "changes_over_time", "id": "t01"}]})
    check("a script that types ONE text once and stops is REFUSED — different texts were asked",
          r["verdict"] == "REFUSED" and any("fewer than two" in p for p in r["problems"]), str(r["problems"]))
    clobber = TYPER.replace("t.setAttribute('placeholder',s.slice(0,i))", "t.value=s.slice(0,i)")
    r = run(html, work, TYPING, {"js": clobber, "touches": ["t01"],
                                 "expect": [{"kind": "changes_over_time", "id": "t01"}]})
    check("typing into the field itself — so a person's own words get overwritten — is REFUSED",
          r["verdict"] == "REFUSED" and any("loses their words" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, TYPING, {"patches": [{"find": 'placeholder="Type something to generate"',
                                              "replace": 'placeholder="Build me a habit tracker"'}],
                                 "touches": ["t01"], "expect": [{"kind": "changes_over_time", "id": "t01"}]})
    check("one fixed new placeholder, CLAIMED as typing, is REFUSED", r["verdict"] == "REFUSED", str(r["problems"]))
    r = run(html, work, TYPING, {"patches": [{"find": 'placeholder="Type something to generate"',
                                              "replace": 'placeholder="Build me a habit tracker"'}],
                                 "touches": ["t01"], "expect": [{"kind": "text", "id": "t01",
                                                                 "equals": "Build me a habit tracker"}]})
    check("  ...and honestly claimed as a fixed text, it is REFUSED before rendering — typing was asked",
          any("changes_over_time" in p for p in r["problems"]) and not r["measured"], str(r["problems"]))
    r = run(html, work, TYPING, {"js": "document.querySelector('[data-ae-id=\"t01\"]').nosuch.x=1;",
                                 "touches": ["t01"], "expect": [{"kind": "changes_over_time", "id": "t01"}]})
    check("a script that throws is REFUSED with the error", any("script errors" in p for p in r["problems"]),
          str(r["problems"]))

    print("\n── 'change the background animation to orange blending with black'")
    claim = [{"kind": "colors", "region": "background", "families": ["orange", "black", "white"], "min_share": 0.6}]
    r = run(html, work, ORANGE, {"patches": recolour(html), "touches": ["bg"], "expect": claim})
    check("both copies recoloured at their own lightness and still moving is APPLIED", r["verdict"] == "APPLIED",
          C.report(r))
    check("  ...every glow layer kept its shape", any("every glow layer kept" in m for m in r["measured"]),
          str(r["measured"]))
    check("  ...and the light of the sky is kept band by band", any("light of the sky is kept" in m
                                                                   for m in r["measured"]), str(r["measured"]))
    check("  ...with the colours measured off the render",
          any("colours measured" in m and m.startswith("ok") for m in r["measured"]), str(r["measured"]))
    check("  ...and the motion measured", any("moving background: PASS" in m for m in r["measured"]),
          str(r["measured"]))
    r = run(html, work, ORANGE, {"patches": recolour(html, flatten=True), "touches": ["bg"], "expect": claim})
    check("a white core turned full orange — the live model's mistake — is REFUSED for losing the light",
          r["verdict"] == "REFUSED" and any("changed the LIGHT" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, ORANGE, {"patches": slim_first_glow(html), "touches": ["bg"], "expect": claim})
    check("a glow rebuilt with fewer stops is REFUSED for changing the sky's shape",
          r["verdict"] == "REFUSED" and any("reshaped glow layer 1" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, ORANGE, {"patches": recolour(html), "touches": ["bg"],
                                 "expect": [dict(claim[0], families=["orange", "black", "blue"])]})
    check("a colour claim that counts a hue nobody asked for is REFUSED",
          any("counts blue" in p for p in r["problems"]), str(r["problems"]))
    rule = C._page_rule(html)
    orange_rule = rule
    for p in recolour(html):
        orange_rule = orange_rule.replace(p["find"], p["replace"])
    r = run(html, work, ORANGE, {"patches": [{"find": rule, "replace": orange_rule}], "touches": ["bg"],
                                 "expect": claim})
    check("the still copy recoloured but the moving copy left blue is REFUSED, before rendering",
          r["verdict"] == "REFUSED" and any("moving copy" in p for p in r["problems"]) and not r["measured"],
          str(r["problems"]))
    alive = C._alive_block(html)
    rest = html.replace(alive, "")
    r = run(html, work, ORANGE, {"patches": [{"find": alive, "replace": ""}] + recolour(rest), "touches": ["bg"],
                                 "expect": claim})
    check("orange, but the animation deleted, is REFUSED — nobody asked it to stop",
          any("no longer is" in p for p in r["problems"]), str(r["problems"]))
    r = run(html, work, ORANGE, {"patches": recolour(html, everything=True), "touches": ["bg"], "expect": claim})
    check("a careless recolour that also turns the glass card orange is REFUSED, naming the card",
          r["verdict"] == "REFUSED" and any(p.startswith("s00 bg changed") for p in r["problems"]),
          str(r["problems"]))
    r = run(html, work, ORANGE, {"patches": recolour(html), "touches": ["bg"],
                                 "expect": [dict(claim[0], families=["orange"])]})
    check("a colour claim that leaves out the black the user named is REFUSED",
          any("black" in p for p in r["problems"]), str(r["problems"]))

    print("\n── everything not named stays exactly as it was")
    r = run(html, work, 'Make the "Generate" label white',
            {"patches": [{"find": "color:#A6A1B0", "replace": "color:#FFFFFF"},
                         {"find": "font-size:40.00px;color:#FFFFFF", "replace": "font-size:40.00px;color:#FF3B30"}],
             "touches": ["s01"], "expect": [{"kind": "color", "id": "s01", "property": "text", "equals": "#FFFFFF"}]})
    check("a label turned white AND a headline quietly turned red is REFUSED, naming the headline",
          any(p.startswith("t00 color") for p in r["problems"]), str(r["problems"]))
    r = run(html, work, 'Make the "Generate" label white',
            {"patches": [{"find": "color:#A6A1B0", "replace": "color:#FFFFFF"}],
             "touches": ["s01"], "expect": [{"kind": "color", "id": "s01", "property": "text", "equals": "#FFFFFF"}]})
    check("  ...the same change without the extra is APPLIED", r["verdict"] == "APPLIED", C.report(r))
    r = run(html, work, "Log a message when Generate is clicked",
            {"js": "document.querySelector('button').onclick=function(){fetch('/log')}", "touches": ["s01"],
             "expect": [{"kind": "text", "id": "s01", "equals": "Generate"}]})
    check("a script that reaches the network is REFUSED", any("fetch" in p for p in r["problems"]), str(r["problems"]))

    print("\n── a refusal is a lesson, not the end")
    r = run(html, work, HEIGHT, {"patches": [CARD], "touches": ["s00"], "expect": [SHORTER]}, GOOD, retries=1)
    check("the first reply is refused and the second APPLIED", r["verdict"] == "APPLIED" and len(r["attempts"]) == 2,
          C.report(r))
    check("  ...because the second prompt carried the measured failure",
          len(r["prompts"]) == 2 and "hangs outside" in r["prompts"][1])
    r = run(html, work, HEIGHT, {"patches": [CARD], "touches": ["s00"], "expect": [SHORTER]},
            {"patches": [CARD], "touches": ["s00"], "expect": [SHORTER]}, retries=1)
    check("two failures leave the page unchanged", r["verdict"] == "REFUSED" and r["html"] == html)
    print("\n── a loader is WATCHED: it has no words to read")
    LOADER = "When I click Generate, show a spinning loader inside the button for about 2 seconds"
    SPIN = ('<i data-ae-id="ld0" style="position:absolute;left:8px;top:14px;width:16px;height:16px;'
            'border-radius:50%;border:2px solid #FFF;border-top-color:transparent;display:none"></i>')
    ADD_SPIN = {"find": ">Generate</span>", "replace": ">Generate</span>" + SPIN, "count": 1}
    # !important, because the spinner's own inline display:none outranks any stylesheet rule —
    # the same lesson a hover test learned here once already.
    CSS = ("@keyframes aeSpin{to{transform:rotate(360deg)}}"
           "[data-ae-id=\"ld0\"].on{display:block!important;animation:aeSpin .6s linear infinite}")
    JS = ("(function(){var b=document.querySelector('[data-ae-id=\"s01\"]'),"
          "l=document.querySelector('[data-ae-id=\"ld0\"]');if(!b||!l)return;"
          "b.addEventListener('click',function(){l.classList.add('on');"
          "setTimeout(function(){l.classList.remove('on');},2000);});})();")
    MOVES = {"kind": "moves", "id": "ld0", "trigger": "s01", "on": "click", "for_ms": 2000,
             "property": "rotate"}
    r = run(html, work, LOADER, {"patches": [ADD_SPIN], "css": CSS, "js": JS,
                                 "touches": ["ld0", "s01"], "expect": [MOVES]})
    check("a spinner that turns on click and stops after about 2s is APPLIED", r["verdict"] == "APPLIED",
          C.report(r)[:400])
    endless = JS.replace("setTimeout(function(){l.classList.remove('on');},2000);", "")
    r = run(html, work, LOADER, {"patches": [ADD_SPIN], "css": CSS, "js": endless,
                                 "touches": ["ld0", "s01"], "expect": [MOVES]})
    check("  ...one that never stops is REFUSED, with when it was still moving",
          r["verdict"] == "REFUSED" and any("never stopped" in p for p in r["problems"]), str(r["problems"])[:220])
    still = JS.replace("l.classList.add('on');", "l.style.display='block';")
    r = run(html, work, LOADER, {"patches": [ADD_SPIN], "css": CSS, "js": still,
                                 "touches": ["ld0", "s01"], "expect": [MOVES]})
    check("  ...and one that appears but does not turn is REFUSED",
          r["verdict"] == "REFUSED" and any("did not move" in p for p in r["problems"]), str(r["problems"])[:220])

    fade = ("@keyframes aeFade{50%{opacity:.2}}"
            "[data-ae-id=\"ld0\"].on{display:block!important;animation:aeFade .6s linear infinite}")
    r = run(html, work, LOADER, {"patches": [ADD_SPIN], "css": fade, "js": JS,
                                 "touches": ["ld0", "s01"], "expect": [MOVES]})
    check("  ...and one that only fades when a SPIN was asked for is REFUSED",
          r["verdict"] == "REFUSED" and any("animates rotate" in p for p in r["problems"]),
          str(r["problems"])[:200])

    print("\n── a colour that is painted over is not a colour anyone can see")
    GREEN = {"kind": "color", "id": "s01", "property": "background", "equals": "#008000"}
    buried = {"patches": [{"find": "border-radius:22px;background:#101014",
                           "replace": "border-radius:22px;background-color:rgb(0, 128, 0);"
                                      "background-image:linear-gradient(90deg,#000100 0%,#463D5F 100%)",
                           "count": 1}],
              "touches": ["s01"], "expect": [GREEN]}
    r = run(html, work, "Make the Generate button green", buried)
    check("green set UNDER a gradient is REFUSED — the property says green, the screen does not",
          r["verdict"] == "REFUSED" and any("nobody sees" in p or "painted over" in p for p in r["problems"]),
          str(r["problems"])[:220])
    honest = {"patches": [{"find": "border-radius:22px;background:#101014",
                           "replace": "border-radius:22px;background:rgb(0, 128, 0)", "count": 1}],
              "touches": ["s01"], "expect": [GREEN]}
    r = run(html, work, "Make the Generate button green", honest)
    check("  ...and a button that really is green is APPLIED", r["verdict"] == "APPLIED", C.report(r)[:300])

    print(f"\nchange battery: {OK} ok, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""IS THIS A WEBSITE, OR A PICTURE OF ONE?

THE OWNER'S CORRECTION, AND IT CHANGES THE OBJECTIVE. Everything built
so far optimises FAITHFULNESS TO A SCREENSHOT: every check asks "does
this render the same pixels?", and the rebuild answers yes. But a
screenshot is a lossy photograph of a website, and a rebuild that
matches it perfectly has faithfully reproduced its compression
artefacts, its soft small type, and — worst — its complete absence of
behaviour.

    "We are not repainting something. We're rebuilding something into a
     website, a really interactive and functional website."

Three things follow, and none of them is visible to a pixel referee:

1. NOTHING IS CLICKABLE. The rebuild emits absolutely-positioned divs.
   A button in the original becomes a div that LOOKS like a button:
   no hover, no focus, no keyboard, no cursor, nothing to tab to, and
   nothing a screen reader will call a button. A pixel score of 100%
   is compatible with a page that does nothing at all.

2. THE BLUR IS BAKED IN ON PURPOSE. Where OCR cannot read a line, the
   pipeline carries the original's pixels — which is exactly right for
   a logo and exactly wrong for a label. A JPEG screenshot degrades
   small type; carrying it ships that degradation to a client who
   wanted crisp text. The original is EVIDENCE OF what the page said,
   not the page.

3. THERE IS NO STRUCTURE. Everything is a <div> at an (x, y). Nothing
   can reflow, nothing is a heading, nothing groups, nothing is
   selectable as a list or a card.

This module does not fix any of that. It MEASURES it, because this
project's rule is that the instrument comes first — every capability
here that works was built after something could report it honestly, and
every capability that silently rotted did so where nothing was looking.

Run it on a rebuilt page and it says, in numbers, how much of what you
are holding is a website and how much is a photograph.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_edit as AE            # noqa: E402
import aethron_vision as V           # noqa: E402

# affordances() lives with the measurement, because that is what
# it is; re-exported here so the audit reads as one piece.
affordances = V.affordances


# ────────────────────── what SHOULD be interactive ───────────────────

# ──────────────────────────── the audit ──────────────────────────────

INTERACTIVE_RE = re.compile(r"<(button|a|input|select|textarea|summary)\b",
                            re.I)
SEMANTIC_RE = re.compile(r"<(nav|main|header|footer|section|article|aside|"
                         r"h[1-6]|ul|ol|li|p|button|a|form|label|input)\b",
                         re.I)


def audit_page(html, image, rep=None, lines=None, carried=()):
    """How much of this page is a website, and how much is a photograph?

    Every number here is a fact about the emitted file measured against
    the original image. None of them is an average, and none of them
    can be satisfied by getting the pixels right — that is the point.
    """
    shot = image if hasattr(image, "w") else V.load(image)
    rep = rep or V.measure(image)
    lines = lines if lines is not None else (V.ocr(image) or [])
    man = AE.manifest(html)
    canvas = man["canvas"]
    area = max(1, (canvas.get("w") or shot.w) * (canvas.get("h") or shot.h))

    els = man["elements"]
    pics = [e for e in els if e["kind"] == "picture"]
    texts = [e for e in els if e["kind"] == "text"]

    # 1. HOW MUCH IS A PHOTOGRAPH. The carried background is excluded —
    #    it is the page's own colour and belongs there. What counts is
    #    the crops laid ON it, because each one is a rectangle of the
    #    page that can never be edited, translated, selected or
    #    restyled.
    carried_area = 0
    for e in pics:
        b = e.get("box") or []
        if len(b) == 4 and b[2] and b[3]:
            carried_area += b[2] * b[3]

    # 2. HOW MUCH OF THE PAGE'S TEXT IS REAL TEXT. A line baked into a
    #    crop is not text: it cannot be selected, searched, translated,
    #    read aloud, or rewritten by the edit layer.
    want = [ln["text"].strip() for ln in lines
            if ln.get("confidence", 1) >= 0.6 and ln["text"].strip()]
    # TEXT IS TEXT WHEREVER IT LIVES. The first version read only the
    # rebuild's own `class="t"` divs, so a page that had been written
    # PROPERLY — <h1>, <p>, <button>, <a> — scored zero lines of real
    # text and was reported as a photograph. A check that fails the
    # thing it is supposed to be steering towards is worse than no
    # check, and the selftest caught it on its first run.
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", html,
                  flags=re.I | re.S)
    have = [t.strip() for t in re.split(r"<[^>]+>", body) if t.strip()]
    have = [AE._unescape(t) for t in have]
    import difflib
    as_type = 0
    for w in want:
        if any(difflib.SequenceMatcher(None, w.lower(), g.lower()).ratio()
               >= 0.72 for g in have):
            as_type += 1

    # 3. WHAT SHOULD BE INTERACTIVE, AND WHAT IS. This is the one the
    #    pixel referee is blind to by construction.
    afford = affordances(shot, rep, lines)
    expect = [a for a in afford if a["kind"] in ("button", "navlink")]
    found = INTERACTIVE_RE.findall(html)
    # WHICH ONES ARE ACTUALLY MISSING, not "all of the ones expected".
    # The first version listed every affordance under "not interactive,
    # and should be" even when the page had just been given a real
    # <button> for each — a report that makes a fixed thing look broken
    # is a report people stop believing.
    labelled = " ".join(
        AE._unescape(re.sub(r"<[^>]+>", " ", m))
        for m in re.findall(r"<(?:button|a)\b[^>]*>(.*?)</(?:button|a)>",
                            html, re.I | re.S))
    missing = [a for a in expect
               if a["label"] and a["label"].lower() not in labelled.lower()]

    # 4. IS THERE ANY STRUCTURE AT ALL?
    tags = SEMANTIC_RE.findall(html)
    divs = len(re.findall(r"<div\b", html, re.I))

    # 5. CAN ANYONE REACH IT WITHOUT A MOUSE?
    focusable = len(re.findall(r"tabindex=|<button\b|<a [^>]*href=", html,
                               re.I))

    findings = []

    def note(kind, what, got, want_, fix):
        findings.append({"kind": kind, "what": what, "got": got,
                         "want": want_, "fix": fix})

    if expect and not found:
        note("NOTHING IS CLICKABLE",
             f"{len(expect)} thing(s) a person would try to use",
             "0 interactive elements in the page",
             "a real <button> or <a> for each",
             "emit buttons as <button> and nav items as <a href>")
    elif missing:
        note("NOT ENOUGH IS CLICKABLE",
             f"{len(expect)} affordance(s) measured",
             f"{len(expect) - len(missing)} of them are real elements",
             f"{len(expect)}",
             "the ones still inert are listed above")

    if want and as_type < len(want):
        note("TEXT SHIPPED AS PIXELS",
             f"{len(want)} line(s) read from the original",
             f"{as_type} of them are real text",
             "every line selectable",
             "read the line rather than carrying its crop; a screenshot "
             "is evidence of what the page SAID, not the page")

    if carried_area / area > 0.02:
        note("PART OF THE PAGE IS A PHOTOGRAPH",
             f"{len(pics)} carried crop(s)",
             f"{carried_area / area * 100:.1f}% of the canvas",
             "logos and photographs only",
             "anything with a readable label belongs in code")

    if not tags:
        note("NO STRUCTURE",
             f"{divs} <div> and nothing else",
             "0 semantic element(s)",
             "nav / main / headings / buttons / lists",
             "a div at an (x,y) cannot reflow, group, or be understood")

    if not focusable:
        note("NOT REACHABLE BY KEYBOARD", "no focusable element",
             "0", "every control focusable",
             "a page nobody can tab through is not finished")

    return {
        "verdict": "PASS" if not findings else "FAIL",
        "canvas": canvas,
        "photograph_share": round(carried_area / area, 4),
        "carried_crops": len(pics),
        "text_lines": len(want),
        "text_as_type": as_type,
        "affordances_expected": len(expect),
        "interactive_found": len(found),
        "semantic_tags": len(tags),
        "divs": divs,
        "focusable": focusable,
        "interactive_wired": len(expect) - len(missing),
        "missing": missing[:20],
        "findings": findings,
    }


def report(res, say=print):
    say(f"canvas {res['canvas'].get('w')}x{res['canvas'].get('h')}")
    say(f"  text that is real text .......... {res['text_as_type']}"
        f" of {res['text_lines']}")
    say(f"  page that is a photograph ....... "
        f"{res['photograph_share'] * 100:.1f}%"
        f"  ({res['carried_crops']} crop(s))")
    say(f"  things a person would use ....... "
        f"{res['affordances_expected']}")
    say(f"  things they actually can ........ "
        f"{res.get('interactive_wired', 0)}"
        f"   ({res['interactive_found']} interactive element(s))")
    say(f"  semantic elements ............... {res['semantic_tags']}"
        f"   (of {res['divs']} div(s))")
    say(f"  focusable ....................... {res['focusable']}")
    if res["missing"]:
        say("\n  not interactive, and should be:")
        for a in res["missing"][:12]:
            say(f"     {a['kind']:8s} {a['label'][:34]!r:38s} {a['evidence']}")
    if res["findings"]:
        say("")
        for f in res["findings"]:
            say(f"  [{f['kind']}] {f['what']}")
            say(f"      got {f['got']}  ·  want {f['want']}")
            say(f"      -> {f['fix']}")
    say("\nVERDICT: " + res["verdict"])


def _selftest():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}   {detail}")

    shot = V.Shot(800, 600, bytearray(800 * 600 * 4))
    rep = {"boxes": [
        {"x": 60, "y": 200, "w": 180, "h": 44, "radius": 8},     # button
        {"x": 60, "y": 300, "w": 300, "h": 120, "radius": 8},    # card
    ]}
    lines = [
        {"text": "Get started", "x": 90, "y": 212, "w": 120, "h": 16,
         "confidence": 1.0},
        {"text": "My balance", "x": 80, "y": 320, "w": 100, "h": 14,
         "confidence": 1.0},
        {"text": "$28,520.30", "x": 80, "y": 350, "w": 140, "h": 20,
         "confidence": 1.0},
        {"text": "Features", "x": 300, "y": 30, "w": 60, "h": 12,
         "confidence": 1.0},
        {"text": "Pricing", "x": 400, "y": 30, "w": 50, "h": 12,
         "confidence": 1.0},
        {"text": "Wezzi", "x": 40, "y": 28, "w": 50, "h": 16,
         "confidence": 1.0},
    ]

    print("── what a person would expect to be able to use")
    aff = affordances(shot, rep, lines)
    kinds = {}
    for a in aff:
        kinds.setdefault(a["kind"], []).append(a["label"])
    check("a filled box with one short line is a button",
          kinds.get("button") == ["Get started"], str(kinds))
    check("a filled box with several lines is a card, not a button",
          kinds.get("card") == ["My balance"], str(kinds))
    check("short top-band lines level with each other are nav links",
          sorted(kinds.get("navlink", [])) == ["Features", "Pricing", "Wezzi"],
          str(kinds))
    lonely = affordances(shot, {"boxes": []},
                         [{"text": "Wezzi", "x": 40, "y": 28, "w": 50,
                           "h": 16, "confidence": 1.0}])
    check("  ...but ONE word at the top is a logo, not a menu",
          lonely == [], str(lonely))

    print("\n── the audit of a page that is only a picture")
    page = ('<style>html,body{width:800px;height:600px}</style><body>'
            '<div class="r" data-ae-id="ground" style="left:0;top:0;'
            'width:800px;height:600px;z-index:0"></div>'
            '<div class="r" data-ae-id="f00" style="left:60px;top:200px;'
            'width:180px;height:44px;z-index:2;background:#FFF"></div>'
            '<div class="t" data-ae-id="t00" style="left:90px;top:212px;'
            'font-size:14px;z-index:4">Get started</div>'
            '<div class="t" data-ae-id="t01" style="left:300px;top:30px;'
            'font-size:12px;z-index:4">Features</div>'
            '<img data-ae-id="p00" class="r" style="left:60px;top:320px;'
            'width:300px;height:60px;z-index:5" src="data:image/png;base64,x">'
            '</body>')
    res = audit_page(page, shot, rep=rep, lines=lines)
    kinds = {f["kind"] for f in res["findings"]}
    check("it FAILS — because it is a picture", res["verdict"] == "FAIL")
    check("it says nothing is clickable",
          "NOTHING IS CLICKABLE" in kinds, str(kinds))
    check("  ...and names what should be",
          any(a["label"] == "Get started" for a in res["missing"]),
          str(res["missing"]))
    check("it says text was shipped as pixels",
          "TEXT SHIPPED AS PIXELS" in kinds, str(kinds))
    check("  ...counting only the lines that really are type",
          res["text_as_type"] == 2 and res["text_lines"] == 6,
          f"{res['text_as_type']}/{res['text_lines']}")
    check("it measures how much is a photograph",
          abs(res["photograph_share"] - (300 * 60) / (800 * 600)) < 1e-6,
          str(res["photograph_share"]))
    check("it says there is no structure", "NO STRUCTURE" in kinds, str(kinds))
    check("it says nobody can tab through it",
          "NOT REACHABLE BY KEYBOARD" in kinds, str(kinds))

    print("\n── and it PASSES a page that is actually a website")
    good = ('<style>html,body{width:800px;height:600px}</style><body>'
            '<nav><a href="#f">Features</a><a href="#p">Pricing</a>'
            '<a href="#w">Wezzi</a></nav>'
            '<main><h1>My balance</h1><p>$28,520.30</p>'
            '<button type="button">Get started</button></main></body>')
    res2 = audit_page(good, shot, rep={"boxes": []}, lines=lines)
    check("no findings on a real page", res2["verdict"] == "PASS",
          str(res2["findings"]))
    check("  ...it counts the interactive elements",
          res2["interactive_found"] >= 4, str(res2["interactive_found"]))
    check("  ...and the semantic ones", res2["semantic_tags"] >= 6,
          str(res2["semantic_tags"]))

    print(f"\nweb selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: aethron_web.py <page.html> <original.png>")
        print("       aethron_web.py --selftest")
        print("       Asks whether a rebuilt page is a WEBSITE or a")
        print("       picture of one. Nothing here is a pixel score.")
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    html = Path(argv[0]).read_text()
    res = audit_page(html, argv[1])
    if "--json" in argv:
        print(json.dumps(res, indent=1))
        return 0
    report(res)
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

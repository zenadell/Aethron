#!/usr/bin/env python3
"""THE MEASUREMENT WRITES THE SPEC. A MODEL WRITES THE WEBSITE.

THE OWNER'S CORRECTION, AND THE ARCHITECTURE THAT FOLLOWS FROM IT:

    "We are not repainting something. We're rebuilding something into a
     website... Do you not see V0? Do you not see Lovable? Do you not
     see Bolt?"

Everything before this module treated the screenshot as an ASSET —
carry its background, crop its logos, lay measured text on top. That
produces a picture of a website. What a client needs is a website, and
the screenshot is the SPECIFICATION for one.

So the two halves finally do what each is actually good at:

    MEASUREMENT reads the pixels. Every coordinate, colour, corner
    radius, gradient and font size in the spec is read, not guessed.
    A multimodal model asked to recover a font size that breaks the
    surrounding pattern is right 7.89% of the time; measurement either
    reads it or says it could not.

    THE MODEL reads the MEANING. Is that a button or a badge? Is that
    a search field? What does that half-legible label actually say?
    Which of these boxes are one card repeated three times? Those are
    judgements about what a thing IS, and no amount of pixel
    arithmetic answers them.

THE RULE THE PROMPT ENFORCES, because it is the whole design:
NUMBERS COME FROM THE SPEC, MEANING COMES FROM THE IMAGE. A model that
invents a coordinate has done the one job it is measurably worst at.

And the result is graded twice, because one referee taught this project
a hard lesson. `identical` says it looks right. `aethron_web` says it
IS a website — real text, real buttons, real structure, reachable by
keyboard. A page can score 100% on the first and zero on the second;
that is precisely how the previous design shipped a photograph.
"""
import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_vision as V           # noqa: E402
import aethron_boxes as B            # noqa: E402
import aethron_web as W              # noqa: E402
import aethron_figma_grade as GR     # noqa: E402


# ─────────────────────────── the spec ────────────────────────────────

def spec(image, verbose=True):
    """Everything measurable about this screenshot, as words and numbers.

    No pixels leave this function except the genuine picture assets —
    a logo, an avatar, an icon. Everything else is a number somebody
    read off the image, which is why the model cannot get it wrong.
    """
    def say(*a):
        if verbose:
            print(*a)

    image = Path(image)
    shot = V.load(image)
    rep = V.measure(image)
    lines = V.ocr(image) or []
    dark = sum(rep["background"]["rgb"]) < 384
    field = V.Field(shot, dark=dark)
    field.refine(V.ink_mask(shot, field))
    mask = V.ink_mask(shot, field)

    # THE BOX TREE — the elements, found by their boundaries. Three
    # tests, each of which the others need; see aethron_boxes.
    boxes = []
    for b in B.styled(shot, B.rectangles(shot), mask=mask):
        if not (B.stands_out(shot, b) and B.sharp_edges(shot, b)
                and B.closed_boundary(shot, b)):
            continue
        if V._in_any(rep.get("gradients", []), b["x"], b["y"],
                     b["w"], b["h"]):
            continue
        boxes.append(b)
    # the measurement's own fill pass finds small pills the edge pass
    # misses (a 39x21 button has little boundary to measure)
    for b in rep.get("boxes", []):
        if not any(abs(b["x"] - o["x"]) < 6 and abs(b["y"] - o["y"]) < 6
                   for o in boxes):
            boxes.append({**b, "css": b["fill"], "quality": 1.0})
    boxes.sort(key=lambda b: -(b["w"] * b["h"]))

    named = [ln for ln in lines if ln.get("confidence", 1) >= 0.6]
    aff = V.affordances(shot, {"boxes": boxes}, lines)
    regs = V.raster_regions(shot, field, lines, fills=boxes)

    def enclosing(x, y, w, h, skip=None):
        """The smallest measured box this thing sits inside.

        `skip` is the box asking, because without it every box reports
        itself as its own parent — box01 came back `"inside": "box01"`,
        which is not a tree, it is a loop.
        """
        best = None
        for i, b in enumerate(boxes):
            if i == skip:
                continue
            if (b["x"] - 3 <= x and b["y"] - 3 <= y
                    and x + w <= b["x"] + b["w"] + 3
                    and y + h <= b["y"] + b["h"] + 3):
                if best is None or b["w"] * b["h"] < boxes[best]["w"] * \
                        boxes[best]["h"]:
                    best = i
        return f"box{best:02d}" if best is not None else None

    out = {
        "canvas": {"w": rep["width"], "h": rep["height"]},
        "page_background": _bg_css(rep),
        "font_family_guess": None,
        "boxes": [],
        "text": [],
        "rules": [],
        "pictures": [],
        "affordances": [],
    }
    for i, b in enumerate(boxes):
        out["boxes"].append({
            "id": f"box{i:02d}",
            "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
            "background": b.get("css") or b.get("fill"),
            "border_radius": b.get("radius", 0),
            "inside": enclosing(b["x"] + 1, b["y"] + 1, b["w"] - 2,
                                b["h"] - 2, skip=i),
        })
    for i, ln in enumerate(named):
        lo, hi = ln["h"] * 1.05, ln["h"] * 1.45
        out["text"].append({
            "id": f"t{i:02d}",
            "text": ln["text"],
            "x": ln["x"], "y": ln["y"], "w": ln["w"],
            "ink_height": ln["h"],
            "font_size_px": round((lo + hi) / 2, 1),
            "colour": V._ink_color(shot, mask, field, ln["y"],
                                   ln["y"] + ln["h"]) or "#000000",
            "inside": enclosing(ln["x"], ln["y"], ln["w"], ln["h"]),
        })
    for r in rep.get("rules", []):
        out["rules"].append({"axis": r["axis"], "at": r["at"],
                             "lit_px": r.get("present")})
    for i, r in enumerate(regs):
        out["pictures"].append({"id": f"pic{i:02d}", "file": f"pic{i:02d}.png",
                                "x": r["x"], "y": r["y"],
                                "w": r["w"], "h": r["h"],
                                "note": "ink the OCR could not read — a "
                                        "logo, an icon or a photograph"})
    for a in aff:
        out["affordances"].append({"kind": a["kind"], "label": a["label"],
                                   "box": a["box"], "why": a["evidence"]})
    # THE BACKGROUND IS ONE ASSET, because on a real site it IS one.
    # Handed only "#0B0B0B" the model invented a flat dark page and the
    # design's entire light bloom vanished — it had no way to build a
    # volumetric render in CSS and neither does anyone else. A designer
    # ships that as a PNG. So do we; everything ELSE stays code.
    lift = V.dilate(mask, shot.w, shot.h, 2)
    out["background_image"] = "assets/ground.png"
    out["_ground_px"] = V.ground_plate(shot, lift)
    say(f"  {len(out['boxes'])} element(s), {len(out['text'])} line(s), "
        f"{len(out['rules'])} rule(s), {len(out['pictures'])} picture(s), "
        f"{len(out['affordances'])} affordance(s)")
    return out, shot, regs


def _skel_from_ir(ir):
    """The corrected page as plain absolute markup, assets by path."""
    c = ir["canvas"]
    out = [f'<div id="page" style="position:relative;width:{c["w"]}px;'
           f'height:{c["h"]}px;overflow:hidden;'
           f'background:{ir["background"]}">']
    for e in ir["elements"]:
        st = ";".join(f"{k}:{v}" for k, v in e["style"].items())
        st = "position:absolute;" + st.replace("/assets/", "assets/")
        if e["kind"] == "picture":
            src = (e.get("src") or "").replace("/assets/", "assets/")
            out.append(f'<img id="{e["id"]}" alt="" src="{src}" '
                       f'style="{st}">')
        else:
            txt = (e.get("text", "").replace("&", "&amp;")
                   .replace("<", "&lt;").replace(">", "&gt;"))
            out.append(f'<div id="{e["id"]}" style="{st};'
                       f'white-space:nowrap;line-height:1">{txt}</div>')
    out.append("</div>")
    return "\n".join(out)


def skeleton(sp):
    """The measured page, as absolute divs. THE MODEL'S STARTING POINT.

    THE FIRST ATTEMPT HANDED OVER ONLY NUMBERS AND ASKED FOR A PAGE.
    Gemini built a real, semantic, clickable website — and IGNORED the
    coordinates completely: its own centred flow layout, its own type
    scale, 52.9% identical. Given exact measurements it still reached
    for the pattern, which is the documented failure and the reason
    this project stopped asking models to place things.

    So do not ask. PLACE EVERYTHING, and hand the model a page whose
    geometry is already right. Its job becomes the one it is genuinely
    good at: turning divs into a document — real buttons, real
    headings, real structure, correct words — without moving anything.
    Rewriting beats composing, and it is checkable: the pixels must
    not move, and the semantics must improve.
    """
    c = sp["canvas"]
    out = [f'<div id="page" style="position:relative;width:{c["w"]}px;'
           f'height:{c["h"]}px;overflow:hidden;'
           f'background:url(assets/ground.png) 0 0/100% 100% no-repeat,'
           f'{sp["page_background"]}">']
    for b in sp["boxes"]:
        out.append(f'<div id="{b["id"]}" style="position:absolute;'
                   f'left:{b["x"]}px;top:{b["y"]}px;width:{b["w"]}px;'
                   f'height:{b["h"]}px;background:{b["background"]};'
                   f'border-radius:{b["border_radius"]}px"></div>')
    for p_ in sp["pictures"]:
        out.append(f'<img id="{p_["id"]}" src="assets/{p_["file"]}" alt="" '
                   f'style="position:absolute;left:{p_["x"]}px;'
                   f'top:{p_["y"]}px;width:{p_["w"]}px;'
                   f'height:{p_["h"]}px">')
    for t in sp["text"]:
        esc = (t["text"].replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        out.append(f'<div id="{t["id"]}" style="position:absolute;'
                   f'left:{t["x"]}px;top:{t["y"]}px;white-space:nowrap;'
                   f'line-height:1;font-size:{t["font_size_px"]}px;'
                   f'color:{t["colour"]}">{esc}</div>')
    out.append("</div>")
    return "\n".join(out)


def _bg_css(rep):
    layers = [g["css"] for g in rep.get("gradients", []) if g.get("css")]
    return ",".join(layers + [rep["background"]["hex"]])


# ─────────────────────────── the brief ───────────────────────────────

PROMPT = """You are rebuilding a design as a REAL, RESPONSIVE WEBSITE.

You get a MEASURED LAYOUT — a working page whose every position, size,
colour, radius and font size was read off the original design's pixels
— and the design's SCREENSHOT.

THE MEASURED LAYOUT IS A SPECIFICATION, NOT THE ANSWER. It is absolute
and frozen at one width, which is a poster, not a site. Your job is to
turn those measurements into a layout that actually works.

WHAT "MEASURED" MEANS AND WHY IT MATTERS. Those numbers are facts. A
model asked to recover a size from an image is right 7.89% of the time,
which is why you are being handed them instead of asked for them. So do
not re-invent them — CONVERT them:

    an element at left:138 on a 1200px canvas   ->  the content column
    and another at left:1062                        starts at 138 and is
                                                    924 wide: a centred
                                                    max-width container

    three boxes at x=363, 586, 809, each 205     ->  a flex row,
    wide, 18px apart                                 gap:18px

    a line at y=362 and the next at y=384        ->  margin, not top

    a 205x112 box with radius 6 and that exact   ->  ONE .card class
    gradient, three times over                       used three times

HOW TO BUILD IT

1. RESPONSIVE BY CONSTRUCTION. The page fills the viewport width. A
   centred container holds the content at the measured content width as
   its max-width. Rows are flex or grid with the measured gaps. Vertical
   spacing is margin and padding derived from the measured gaps. Type
   keeps its measured px sizes. Add media queries so rows stack and the
   container padding shrinks on narrow screens. NOTHING may overflow
   horizontally at 400px wide.

2. IT MUST STILL BE THE DESIGN. Rendered at the design's own width, the
   page must look like the screenshot: same text on the same lines, same
   sizes, same colours, same order. That is checked automatically, line
   by line, and reported back to you. Use `position:absolute` only where
   the design genuinely is absolute — a badge pinned to a card, a
   decorative glow.

3. MAKE IT WORK. Anything clickable is a real <button type="button"> or
   <a href="#">, with :hover and :focus-visible. A search field is a
   real <input> with a placeholder. Use <header>, <nav>, <main>,
   <section>, <footer>, <h1>-<h3>, <p>, <ul>/<li>, <article>.

4. FIX THE WORDS. The text came from OCR of a lossy screenshot. Where it
   is mangled, write what the page actually said — "See detas" is "See
   details". Read the screenshot. You are restoring a site, not copying
   a photograph's mistakes.

5. THE PICTURES in assets/ are the only bitmaps: logos, icons, avatars,
   and the hero's light render. Keep their paths exactly. Everything
   else — every fill, gradient, rule, corner — is CSS. Do not add any
   image of your own.

6. NO DUPLICATES. Each piece of text and each element appears ONCE. If
   the measured layout has a box and a text line that are really one
   button, emit one button — not a box and a label on top of it.

OUTPUT: one complete HTML file — <!DOCTYPE html>, <head> with a <title>
and the <style>, semantic <body>. No markdown fence, no explanation.
"""


def _call(spec_json, image, key, model, max_tokens=32000,
          effort="low", extra=""):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT + extra},
            {"type": "text", "text": "THE MEASURED PAGE — keep every "
                                     "number in it exactly:\n"
                                     + spec_json["_skeleton"]},
            {"type": "text", "text": "WHAT THE MEASUREMENT FOUND (which "
                                     "of these are buttons and links):\n"
                                     + json.dumps(
                                         spec_json["affordances"], indent=1)},
            {"type": "image_url", "image_url": {"url":
                "data:image/png;base64," + base64.b64encode(
                    Path(image).read_bytes()).decode()}},
        ]}],
        "max_tokens": max_tokens, "temperature": 0.2,
        "reasoning_effort": effort,
    }).encode()
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/openai"
        "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    # RETRY THE PROVIDER'S OWN BACK-PRESSURE. A 429 is the API asking
    # for a pause, not a failure of the work — and an unhandled one
    # threw away a whole run mid-flight.
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise SystemExit(f"provider said {e.code}: "
                                 f"{e.read()[:300].decode(errors='replace')}")
            wait = 8 * (attempt + 1)
            print(f"   provider said {e.code}; waiting {wait}s")
            time.sleep(wait)
    u = d.get("usage") or {}
    txt = (d["choices"][0].get("message") or {}).get("content") or ""
    if not txt:
        raise SystemExit(f"empty reply "
                         f"({d['choices'][0].get('finish_reason')}) {u}")
    return txt, u


def _clean(txt):
    t = txt.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t[:4].lower() == "html":
            t = t[4:]
    return t.strip()


# ────────────────────────────── run it ───────────────────────────────

def generate(image, outdir, key=None, model=None, rounds=1, verbose=True):
    """Measure, hand the spec to the model, render, and grade it twice."""
    def say(*a):
        if verbose:
            print(*a)

    cfg = json.loads((ROOT / "aethron_config.json").read_text())["ai"]
    key = key or cfg["api_key"]
    model = model or cfg.get("model") or "gemini-3.6-flash"
    image = Path(image)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    if image.suffix.lower() != ".png":
        png = outdir / "original.png"
        import subprocess
        subprocess.run(["sips", "-s", "format", "png", str(image),
                        "--out", str(png)], capture_output=True)
        if png.is_file():
            image = png

    say(f"measuring {image.name}")
    sp, shot, regs = spec(image, verbose)
    sp.pop("_ground_px", None)

    # THE SKELETON IS THE *CORRECTED* PAGE, NOT A FRESH GUESS.
    #
    # The first version built the skeleton straight from OCR, sizing
    # each line as ink_height x 1.25. That is 42% too large — the
    # measured pipeline starts at x0.88 and then RENDERS, RE-READS and
    # corrects, which is the only reason it reaches 22 of 22 lines. My
    # skeleton skipped the correction, so the headline overran, the
    # button's label wrapped out of its own pill, and the model was
    # blamed for obeying the numbers it was given.
    #
    # rebuild() already does that loop. Use its output. page_ir() then
    # pulls the inlined base64 out into real files, so the model is
    # handed markup instead of 400KB of data URI.
    import aethron_screen as SC
    say("  refining the layout (render, re-read, correct)")
    v = V.rebuild(image, outdir / "measured", verbose=verbose)
    if v.get("verdict") == "SKIPPED":
        return {"verdict": "FAILED", "why": v.get("why", "no OCR")}
    ir = SC.page_ir(Path(v["page"]).read_text(), outdir.name)
    (outdir / "assets").mkdir(exist_ok=True)
    for a in ir["assets"]:
        (outdir / "assets" / a["file"]).write_bytes(a["bytes"])
    sp["_skeleton"] = SC.emit_html.__wrapped__(ir) if hasattr(
        SC.emit_html, "__wrapped__") else _skel_from_ir(ir)
    say(f"  layout corrected: {v.get('lines_correct')}/"
        f"{v.get('lines_expected')} lines")
    (outdir / "skeleton.html").write_text(sp["_skeleton"])
    (outdir / "spec.json").write_text(json.dumps(
        {k: v for k, v in sp.items() if k != "_skeleton"}, indent=1))
    for i, r in enumerate(regs):
        b64 = V.crop_b64(shot, r["x"], r["y"], r["w"], r["h"])
        (outdir / "assets").mkdir(exist_ok=True)
        (outdir / "assets" / f"pic{i:02d}.png").write_bytes(
            base64.b64decode(b64))

    spent = {"in": 0, "out": 0}
    best = None
    for rnd in range(rounds):
        say(f"round {rnd + 1}: {model} writes the site")
        t0 = time.time()
        fix = ""
        if best is not None and best.get("findings"):
            # HAND BACK WHAT THE CHECKER READ, NAMED BY ITS WORDS.
            # An earlier correction loop failed because it said "text at
            # y224, move it +10px" — a coordinate in the ORIGINAL that
            # the writer cannot map to its own markup. A line's TEXT is
            # the same in both documents, so it is the one handle that
            # always resolves.
            fix = ("\n\nYOUR LAST ATTEMPT WAS CHECKED AGAINST THE DESIGN "
                   "LINE BY LINE. These lines are wrong — fix exactly "
                   "these and change nothing else:\n"
                   + json.dumps(best["findings"], indent=1))
        txt, u = _call(sp, image, key, model, extra=fix)
        spent["in"] += u.get("prompt_tokens", 0)
        spent["out"] += max(u.get("completion_tokens", 0),
                            u.get("total_tokens", 0)
                            - u.get("prompt_tokens", 0))
        html = _clean(txt)
        page = outdir / f"r{rnd + 1}.html"
        page.write_text(html)
        png = outdir / f"r{rnd + 1}.png"
        GR.shoot(page, sp["canvas"]["w"], sp["canvas"]["h"], png)
        if not png.is_file() or not png.stat().st_size:
            say("   the page did not render at all")
            continue
        ident = GR.compare(png, image)["identical"]
        web = W.audit_page(html, image)
        # THE CHECKLIST DECIDES, NOT THE PERCENTAGE. The dense page came
        # back at 97.09% identical with its HEADLINE MISSING, its
        # paragraph scrambled and one button rendered twice — because
        # that page is mostly dark ground and glow, so wrecked text
        # barely moves the total. Selecting on it chose a broken page.
        # verify_rebuild reads both renders and checks every line by its
        # own words: there, in the right place, at the right size.
        # A WRAPPER THAT REPOSITIONS ITS CHILDREN IS A SILENT KILLER,
        # so name it rather than let it show up as 41 missing lines.
        bad_wrap = re.findall(
            r"<(?:header|nav|main|section|footer|article|div)\b[^>]*"
            r"style=\"[^\"]*(?:position:\s*(?:relative|absolute|fixed|"
            r"sticky)|transform:|filter:|will-change:)", html, re.I)
        if bad_wrap:
            say(f"   ({len(bad_wrap)} wrapper(s) create a positioning "
                f"context — children will move)")
        # DOES IT REFLOW? A responsive page must survive a narrow
        # viewport without spilling sideways — that is the whole
        # difference between a website and a poster, and it is one
        # measurement, not an opinion.
        narrow = outdir / f"r{rnd + 1}-400.png"
        GR.shoot(page, 400, 900, narrow)
        flow = _flow(page, 400)
        if flow is None:
            # A CHECK THAT CANNOT RUN REPORTS SKIPPED, NEVER PASS.
            say("   at 400px wide: UNMEASURED (no browser reading)")
        elif flow["spills"]:
            worst = ", ".join(f"{e['t']}.{e['c'].split()[0]}"
                              if e["c"] else e["t"]
                              for e in flow["worst"][:3])
            say(f"   at {flow['vw']}px wide: {flow['spills']} element(s) "
                f"spill past the edge (scrollWidth {flow['scroll']}) — "
                f"{worst}")
        else:
            say(f"   at {flow['vw']}px wide: reflows cleanly")
        chk = V.verify_rebuild(image, png)
        ok = chk.get("lines_correct", 0)
        tot = max(1, chk.get("lines_expected", 1))
        say(f"   {time.time() - t0:.0f}s · {ok}/{tot} lines correct · "
            f"{ident * 100:.2f}% identical · "
            f"{web['text_as_type']}/{web['text_lines']} real text · "
            f"{web.get('interactive_wired', 0)}/"
            f"{web['affordances_expected']} clickable · "
            f"{web['semantic_tags']} semantic · "
            f"{web['photograph_share'] * 100:.1f}% photograph")
        cand = {"html": html, "page": str(page), "png": str(png),
                "identical": ident, "web": web, "lines": [ok, tot],
                "flow": flow,
                "reflows": bool(flow) and not flow["spills"],
                "findings": chk.get("findings", [])[:24]}
        if best is None or _score(cand) > _score(best):
            best = cand

    if best is None:
        return {"verdict": "FAILED", "why": "no round produced a page"}
    (outdir / "page.html").write_text(best["html"])
    GR.shoot(outdir / "page.html", sp["canvas"]["w"], sp["canvas"]["h"],
             outdir / "page.png")
    cost = spent["in"] / 1e6 * 0.30 + spent["out"] / 1e6 * 2.50
    best["cost"] = round(cost, 4)
    best["tokens"] = spent
    (outdir / "result.json").write_text(json.dumps(
        {k: v for k, v in best.items() if k != "html"}, indent=1))
    say(f"\n  ${cost:.4f} · {spent['in']} in / {spent['out']} out")
    return best


FLOW_JS = """
<script id="__ae_flow">
(function () {
  function report() {
    var d = document.documentElement, out = [];
    var vw = window.innerWidth;
    var els = document.body ? document.body.querySelectorAll('*') : [];
    for (var i = 0; i < els.length; i++) {
      var r = els[i].getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      var cs = getComputedStyle(els[i]);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      // A FIXED element parked off-canvas is a drawer, not a spill.
      if (cs.position === 'fixed') continue;
      if (r.right > vw + 1 || r.left < -1) {
        out.push({ t: els[i].tagName.toLowerCase(),
                   c: (els[i].className || '').toString().slice(0, 40),
                   x: Math.round(r.left), r: Math.round(r.right),
                   w: Math.round(r.width) });
      }
    }
    out.sort(function (a, b) { return b.r - a.r; });
    d.setAttribute('data-ae-flow', JSON.stringify({
      vw: vw,
      scroll: Math.round(d.scrollWidth),
      spills: out.length,
      worst: out.slice(0, 6)
    }));
  }
  if (document.readyState === 'complete') report();
  else window.addEventListener('load', report);
  setTimeout(report, 600);
})();
</script>
"""


def _flow(page, width=400):
    """What does the page do at phone width? Ask the BROWSER.

    Returns a dict, or None when it could not be measured — and None is
    NOT a pass. The previous version of this function greped
    `width:NNNpx` out of the markup while its own docstring claimed it
    asked the browser, and it never ran once: Chrome DOES NOT EXIT after
    --dump-dom (documented in this project since the probe was built),
    so the plain subprocess.run hung its full timeout, threw, and
    returned None on every round of every run.

    The page reports on ITSELF, the way the motion runtime does with
    data-ae-stats: a script stamps the measurement onto <html> and we
    read the attribute out of the dump. That turns "it looks frozen"
    into a list of the elements actually hanging off the edge.
    """
    b = GR.find_browser()
    if not b:
        return None
    import subprocess, tempfile, shutil, threading
    src = Path(page).read_text()
    probe = Path(page).with_suffix(".flow.html")
    probe.write_text(src.replace("</body>", FLOW_JS + "</body>")
                     if "</body>" in src else src + FLOW_JS)
    prof = tempfile.mkdtemp(prefix="ae-flow-")
    proc = subprocess.Popen(
        [b, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--user-data-dir={prof}", f"--window-size={width},900",
         "--virtual-time-budget=5000", "--dump-dom",
         probe.resolve().as_uri()],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    dom, done = [], threading.Event()

    def pump():
        try:
            for line in proc.stdout:
                dom.append(line)
                if "</html>" in line:
                    break
        except Exception:
            pass
        done.set()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    done.wait(60)
    try:
        proc.kill()
    except Exception:
        pass
    shutil.rmtree(prof, ignore_errors=True)
    probe.unlink(missing_ok=True)
    m = re.search(r'data-ae-flow="([^"]*)"', "".join(dom))
    if not m:
        return None
    try:
        import html as _h
        return json.loads(_h.unescape(m.group(1)))
    except Exception:
        return None


def _score(c):
    """A candidate is judged on BOTH referees, not on pixels alone.

    Weighted towards being a website, deliberately: a page that looks
    perfect and does nothing is the failure this whole module exists
    to stop, and the pixel score cannot see it.
    """
    w = c["web"]
    # Lines-correct first and heavily: a page whose headline is missing
    # is broken however good the rest of it looks, and it is the only
    # one of these numbers that can see that.
    ok, tot = c.get("lines", [0, 1])
    # A page that does not reflow is a poster. It can still win on
    # lines, so the penalty has to be real rather than decorative.
    flow = 1.0 if c.get("reflows") else 0.0
    site = (
        (w["text_as_type"] / max(1, w["text_lines"])) * 0.4
        + (w.get("interactive_wired", 0)
           / max(1, w["affordances_expected"])) * 0.3
        + min(1.0, w["semantic_tags"] / 12) * 0.2
        + (1.0 - min(1.0, w["photograph_share"] * 10)) * 0.1
    )
    return ((ok / max(1, tot)) * 0.45 + c["identical"] * 0.15
            + site * 0.25 + flow * 0.15)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: aethron_generate.py <screenshot> <outdir> "
              "[--rounds N] [--model NAME]")
        return 0
    rounds = int(argv[argv.index("--rounds") + 1]) if "--rounds" in argv else 1
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    r = generate(argv[0], argv[1], model=model, rounds=rounds)
    if r.get("verdict") == "FAILED":
        print("FAILED: " + r["why"])
        return 1
    print("\n" + "─" * 62)
    W.report(r["web"])
    print(f"\n{r['identical'] * 100:.2f}% identical to the original")
    print(r["page"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

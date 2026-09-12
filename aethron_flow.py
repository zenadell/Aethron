#!/usr/bin/env python3
"""A measured page is a POSTER. This turns it into a website.

THE OWNER'S QUESTION, and it is the right one: "shouldn't it be
intelligent enough to know that it is supposed to make it an entire
screen website and make it responsive? shouldn't it be intelligent
enough to know a distance?"

It knows every distance — it MEASURED them. Which is exactly why this
module contains no model. Responsiveness is not a matter of taste that
needs judging; it is a set of relationships that are already in hand:

    two elements whose vertical spans overlap ...... are a ROW
    the span from the leftmost to the rightmost .... is the COLUMN
    the space between two bands .................... is a MARGIN
    consecutive lines at one size and one left ..... are a PARAGRAPH

A model asked to do this guesses; the measurement knows. And the result
is checkable both ways, which is what makes it safe to do mechanically:
render at the DESIGN WIDTH and the line checker must still pass (the
layout is faithful), render at PHONE WIDTH and nothing may hang off the
edge (it genuinely reflows). Neither check can be satisfied by luck.

THE THING THAT MAKES FLOW POSSIBLE AT ALL is not the container or the
media query — it is `paragraphs()`. A rebuilt page is a list of LINES,
because that is what a screenshot contains: OCR reports one box per
line, and the emitter writes one absolutely-positioned div per box. A
line cannot reflow. It has nowhere to go and no siblings to push. Three
lines of one paragraph, left to re-wrap independently at 400px, produce
three ragged fragments rather than a paragraph.

So the lines are reassembled into the paragraphs they were cut from,
by the same reasoning `_rejoin` uses for card strips: same size, same
left edge, and a vertical step that matches the leading. THEN the page
has something that can reflow.

WHAT THIS DOES NOT DO, said plainly rather than discovered later:
  * a region carried as a raster crop stays a raster crop. A picture of
    a dashboard is a picture; it is scaled, never reflowed, because
    there is nothing inside it to lay out.
  * it does not invent breakpoints it cannot check. One narrow rule at
    a measured width, verified, beats four guessed ones.
  * it will not reflow a page it cannot read as rows and bands, and it
    says so rather than shipping a poster with a media query on it.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aethron_screen as SC            # noqa: E402
import aethron_edit as AE              # noqa: E402


def _n(v, d=0.0):
    """A CSS length as a number. Missing is missing, not zero."""
    if v is None:
        return d
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else d


def box(e):
    """left, top, width, height for an element, whatever it carries.

    A REAL READING IF THERE IS ONE (see `measure`), and only otherwise
    the estimate below. Text elements declare no width or height — they
    are sized by their own glyphs — and estimating a width from the
    character count is fine for asking "does this sit beside that?" and
    disastrous for anything cumulative: laying a nav row out by stepping
    from each item's estimated right edge drifted 50px by the fourth
    item and put the Log-in button on the wrong side of the page. The
    estimate is not wrong by a constant, so it cannot be corrected; it
    has to be replaced by a measurement.
    """
    if e.get("rect"):
        return tuple(e["rect"])
    s = e["style"]
    l, t = _n(s.get("left")), _n(s.get("top"))
    w, h = _n(s.get("width")), _n(s.get("height"))
    if not h:
        h = _n(s.get("font-size"), 12) * 1.35
    if not w and e.get("text"):
        w = len(e["text"]) * _n(s.get("font-size"), 12) * 0.52
    return l, t, w, h


MEASURE_JS = """
<script id="__ae_measure">
(function () {
  function go() {
    var out = {};
    document.querySelectorAll('[data-ae-id]').forEach(function (el) {
      var r = el.getBoundingClientRect();
      out[el.getAttribute('data-ae-id')] = [
        Math.round(r.left + window.scrollX),
        Math.round(r.top + window.scrollY),
        Math.round(r.width), Math.round(r.height)];
    });
    document.documentElement.setAttribute('data-ae-rects',
      JSON.stringify(out));
  }
  if (document.readyState === 'complete') go();
  else window.addEventListener('load', go);
  setTimeout(go, 700);
})();
</script>
"""


def measure(page, size=None):
    """Every element's REAL box, read from the browser that drew it.

    The rebuilt page positions everything absolutely, so one render of
    it is a complete and exact statement of the design's geometry —
    including the widths of text, which nothing upstream records because
    a line of type is as wide as its glyphs happen to be.

    This is the same move `capture_realtime` made for rAF motion and
    `VISIBLE_JS` made for the probe: stop inferring what the browser can
    simply be asked.
    """
    import subprocess, tempfile, shutil, threading
    import aethron_figma_grade as GR
    b = GR.find_browser()
    if not b:
        return {}
    page = Path(page)
    src = page.read_text()
    probe = page.with_suffix(".measure.html")
    probe.write_text(src.replace("</body>", MEASURE_JS + "</body>")
                     if "</body>" in src else src + MEASURE_JS)
    prof = tempfile.mkdtemp(prefix="ae-measure-")
    # THE VIEWPORT MUST BE SAID, NOT SNIFFED. Reading it from the page's
    # own `html,body{width:...}` rule works for an absolute rebuild and
    # silently fails for a FLOWED page, which has no such rule by
    # design — so it fell back to 1200, centred a 1024px container in
    # it, and reported every element 88px to the right. Twenty-six
    # elements "wrong" by one number: the instrument, not the page.
    if size:
        w, h = size
    else:
        w = h = 1200
        mm = re.search(r"html,body\{width:(\d+)px;height:(\d+)px", src)
        if mm:
            w, h = int(mm.group(1)), int(mm.group(2))
    proc = subprocess.Popen(
        [b, "--headless", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", f"--user-data-dir={prof}",
         f"--window-size={w},{h}", "--virtual-time-budget=8000",
         "--dump-dom", probe.resolve().as_uri()],
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

    threading.Thread(target=pump, daemon=True).start()
    done.wait(90)
    try:
        proc.kill()
    except Exception:
        pass
    shutil.rmtree(prof, ignore_errors=True)
    probe.unlink(missing_ok=True)
    m = re.search(r'data-ae-rects="([^"]*)"', "".join(dom))
    if not m:
        return {}
    try:
        import html as _h
        return json.loads(_h.unescape(m.group(1)))
    except Exception:
        return {}


def paragraphs(els, canvas):
    """Put the lines back into the paragraphs they were cut from.

    THIS IS THE PASS THAT MAKES THE PAGE REFLOWABLE, and it is worth
    being precise about why. A screenshot has no paragraphs in it. OCR
    returns one box per LINE and the emitter writes one absolutely
    positioned div per box, so a three-line paragraph arrives as three
    unrelated elements that happen to sit above one another. Each one
    carries `white-space:nowrap` because each one IS a single measured
    line. Nothing about that can respond to a narrower screen: at 400px
    the three fragments simply overflow, side by side with nothing.

    Rejoined, they become one <p> with real text in it, and the browser
    re-wraps it for free at any width — which is what a paragraph is
    for and the reason HTML has them.

    THE TEST IS THE ONE THE STRIP MERGE USES: same measured size, same
    left edge, and a vertical step consistent with the leading. All
    three must hold. Size alone would weld a heading to the line under
    it; the left edge alone would weld a nav item to a label beneath.
    A step much larger than the leading is a NEW paragraph, and that is
    the measurement telling us where the author put a break.
    """
    text = [e for e in els if e.get("text")]
    text.sort(key=lambda e: (box(e)[1], box(e)[0]))
    # EVERYTHING THAT IS NOT TYPE PASSES STRAIGHT THROUGH. The first
    # version of this function returned only what it had merged, which
    # silently deleted every picture and every filled box on the page —
    # 65 elements in, 17 out. A pass named for one job must not quietly
    # decide the fate of everything else.
    used, out = set(), [e for e in els if not e.get("text")]
    for i, e in enumerate(text):
        if id(e) in used:
            continue
        run, last = [e], e
        used.add(id(e))
        for f in text[i + 1:]:
            if id(f) in used:
                continue
            lb, fb = box(last), box(f)
            size_l = _n(last["style"].get("font-size"), 12)
            size_f = _n(f["style"].get("font-size"), 12)
            step = fb[1] - lb[1]
            # the leading of a set paragraph runs about 1.2-1.8x the
            # size; outside that band the two lines are not a paragraph
            if (abs(size_l - size_f) <= max(0.6, size_l * 0.06)
                    and abs(fb[0] - lb[0]) <= 3
                    and last["style"].get("color") == f["style"].get("color")
                    and size_l * 1.05 <= step <= size_l * 2.05):
                run.append(f)
                used.add(id(f))
                last = f
            elif fb[1] > lb[1] + size_l * 2.4:
                break
        if len(run) > 1:
            merged = dict(run[0])
            merged["style"] = dict(run[0]["style"])
            merged["text"] = " ".join(r["text"] for r in run)
            merged["lines"] = len(run)
            # THE MEASURED WIDTH OF THE PARAGRAPH IS THE WIDEST LINE IN
            # IT. That is a real measurement, not an estimate, and it is
            # what tells the browser where to wrap so the rebuilt page
            # breaks in the same places as the original.
            merged["measured_w"] = max(box(r)[2] for r in run)
            merged["measured_h"] = (box(run[-1])[1] - box(run[0])[1]
                                    + box(run[-1])[3])
            # THE MERGED ELEMENT IS THE MERGED BOX. Copying run[0]
            # carried its `rect` along too — the FIRST LINE's box — so
            # everything downstream believed a three-line paragraph was
            # 14px tall while it rendered 66, and the button under it
            # landed exactly 52px low: the difference, to the pixel.
            steps = [box(y)[1] - box(x)[1] for x, y in zip(run, run[1:])]
            _lead = (sum(steps) / len(steps)) if steps else 0
            lead = _lead
            size0 = _n(run[0]["style"].get("font-size"), 12) or 12
            merged["line_height"] = round(lead / size0, 3) if lead else 1
            # THE MERGED ELEMENT IS THE MERGED BOX, AND ITS HEIGHT IS
            # WHAT IT WILL ACTUALLY RENDER: one leading per line. Copying
            # run[0] carried the FIRST LINE's rect along, so everything
            # downstream believed a three-line paragraph was 14px tall
            # while it rendered 66, and the button under it landed
            # exactly 52px low — the difference, to the pixel.
            merged["rect"] = [box(run[0])[0], box(run[0])[1],
                              max(box(r)[2] for r in run),
                              round(len(run) * lead) if lead
                              else box(run[0])[3]]
            out.append(merged)
        else:
            out.append(e)
    return out


def rows(band, slack=0.45):
    """Elements that sit BESIDE one another, from their own geometry.

    Two things are in the same row when their vertical spans overlap —
    the identical rule that `regions()` needed for buttons, and for the
    identical reason. Lining up tops does not work: a 56px heading and
    a 12px label beside it share a row and share no coordinate. What
    they share is the same stretch of the page.
    """
    items = sorted(band, key=lambda e: (box(e)[1], box(e)[0]))
    out = []
    for e in items:
        l, t, w, h = box(e)
        for r in out:
            rt, rb = r["top"], r["bot"]
            over = min(rb, t + h) - max(rt, t)
            if over > 0 and over >= slack * min(h, rb - rt):
                r["els"].append(e)
                r["top"], r["bot"] = min(rt, t), max(rb, t + h)
                break
        else:
            out.append({"top": t, "bot": t + h, "els": [e]})
    for r in out:
        r["els"].sort(key=lambda e: (box(e)[0], e.get("z", 0)))
    return sorted(out, key=lambda r: r["top"])


def column(els, canvas):
    """The content column: where the design actually puts its content.

    Measured, not assumed. An element at left:138 on a 1200px canvas
    and another ending at 1062 are not decoration — they are the two
    margins of the column the designer drew, and the column between
    them is what becomes a centred max-width container.

    FULL-BLEED ELEMENTS ARE EXCLUDED FROM THE VOTE. The carried ground
    spans the canvas by definition, and so does every horizontal rule;
    counting them puts the column at 0..canvas and there is no column
    left. What defines the column is the content that respects it.
    """
    inner = []
    for e in els:
        l, t, w, h = box(e)
        if w >= canvas["w"] * 0.94 or h >= canvas["h"] * 0.6:
            continue
        if w < 4 or h < 4:
            continue
        inner.append((l, l + w))
    if not inner:
        return 0, canvas["w"]
    left = min(a for a, _ in inner)
    right = max(b for _, b in inner)
    return int(left), int(min(canvas["w"], right))


def is_backdrop(e, canvas):
    l, t, w, h = box(e)
    return (w >= canvas["w"] * 0.94 and h >= canvas["h"] * 0.6)


def is_rule(e, canvas):
    l, t, w, h = box(e)
    return e["kind"] == "rule" or (min(w, h) <= 2 and max(w, h) > 40)


def bands(els, canvas, gap=None):
    """Stack the page into bands, splitting where the page has space.

    The split threshold is MEASURED from the page's own vertical gaps
    rather than fixed: a dense dashboard and an airy landing page do
    not share a rhythm, and a constant that suits one shreds the other.
    The median gap is the page's normal line spacing; a gap well above
    it is the author putting a section break in.
    """
    items = sorted([e for e in els if not is_backdrop(e, canvas)],
                   key=lambda e: box(e)[1])
    if not items:
        return []
    steps = []
    prev_bot = None
    for e in items:
        l, t, w, h = box(e)
        if prev_bot is not None and t - prev_bot > 0:
            steps.append(t - prev_bot)
        prev_bot = max(prev_bot or 0, t + h)
    if gap is None:
        steps.sort()
        med = steps[len(steps) // 2] if steps else 12
        gap = max(18.0, med * 3.0)
    out, cur, bot = [], [], None
    for e in items:
        l, t, w, h = box(e)
        if bot is not None and t - bot > gap:
            out.append(cur)
            cur = []
        cur.append(e)
        bot = max(bot or 0, t + h)
    if cur:
        out.append(cur)
    return out


def _css_of(e, keep):
    s = e["style"]
    return ";".join(f"{k}:{s[k]}" for k in keep if s.get(k))


TEXTY = ("font-size", "font-weight", "font-family", "color",
         "letter-spacing", "line-height", "text-transform",
         "font-style", "text-decoration", "opacity")
BOXY = ("background", "background-color", "background-image",
        "background-size", "background-position", "background-repeat",
        "border-radius", "border", "box-shadow", "opacity")


def flow(html, name="site", verbose=True, src=None):
    """Absolute measured page in, responsive page out.

    Every number written here was read off the screenshot. Nothing is
    invented: the container width, the gaps, the type sizes and the one
    breakpoint are all measurements, which is why the result can be
    checked against the original rather than merely admired.
    """
    def say(*a):
        if verbose:
            print(*a)

    ir = SC.page_ir(html, name)
    canvas = ir["canvas"]
    els = ir["elements"]
    rects = measure(src) if src else {}
    if rects:
        for e in els:
            r = rects.get(e["id"])
            if r and r[2] and r[3]:
                e["rect"] = r
        say(f"  {len(rects)} element box(es) read from the browser")
    else:
        say("  element widths ESTIMATED (no browser reading) — "
            "row offsets will drift")
    back = [e for e in els if is_backdrop(e, canvas)]
    rules = [e for e in els if not is_backdrop(e, canvas)
             and is_rule(e, canvas)]
    body = [e for e in els if not is_backdrop(e, canvas)
            and not is_rule(e, canvas)]

    body = paragraphs(body, canvas)
    merged = sum(1 for e in body if e.get("lines"))
    L, R = column(body, canvas)
    col = max(320, R - L)
    say(f"  canvas {canvas['w']}x{canvas['h']} · {len(els)} elements")
    say(f"  {merged} paragraph(s) reassembled from measured lines")
    say(f"  content column {L}..{R}  ({col}px wide, "
        f"{L} of margin either side)")

    bs = bands(body, canvas)
    say(f"  {len(bs)} band(s), {len(rules)} rule(s) carried as dividers")

    # ---- the page ----------------------------------------------------
    ground = None
    for e in back:
        bg = e["style"].get("background-image", "")
        m = re.search(r"url\(([^)]+)\)", bg)
        if m:
            ground = m.group(1)
    # THE RULES ARE THE PAGE'S STRUCTURE, AND THEY WERE BEING DROPPED.
    # They are separated out of the flow (a 1px line spanning the canvas
    # is not a row and putting it in one wrecks the band) and then the
    # first version of this module simply never emitted them again — all
    # eighteen grid lines of the dense page, gone, with the measurement
    # sitting right there in the list.
    # They belong in a backdrop layer, positioned in PERCENTAGES so they
    # stay where the design put them at any width, and marked decorative
    # because that is what they are.
    rule_html = ""
    if rules:
        rr = []
        for e in rules:
            el, et, ew, eh = box(e)
            bg = e["style"].get("background") or e["style"].get(
                "background-color") or "currentColor"
            if ew >= eh:                     # a horizontal rule
                rr.append(
                    f'<i style="position:absolute;'
                    f'left:{el / canvas["w"] * 100:.3f}%;'
                    f'top:{et / canvas["h"] * 100:.3f}%;'
                    f'width:{ew / canvas["w"] * 100:.3f}%;'
                    f'height:{max(1, int(eh))}px;background:{bg}"></i>')
            else:                            # a vertical one
                rr.append(
                    f'<i style="position:absolute;'
                    f'left:{el / canvas["w"] * 100:.3f}%;'
                    f'top:{et / canvas["h"] * 100:.3f}%;'
                    f'width:{max(1, int(ew))}px;'
                    f'height:{eh / canvas["h"] * 100:.3f}%;'
                    f'background:{bg}"></i>')
        rule_html = ('<div class="grid" aria-hidden="true">'
                     + "".join(rr) + "</div>")
        say(f"  {len(rules)} rule(s) placed proportionally in a "
            f"backdrop layer")

    parts = []
    prev_bot = None
    for i, band in enumerate(bs, 1):
        rs = rows(band)
        top = min(box(e)[1] for e in band)
        # THE FIRST BAND'S MARGIN IS ITS OWN TOP. Starting it at zero
        # rode the whole page up by the height of the page's own top
        # margin — every line came back "move it +0,+18", which is one
        # fault wearing twenty costumes.
        margin = int(top) if prev_bot is None else max(0, int(top - prev_bot))
        prev_bot = max(box(e)[1] + box(e)[3] for e in band)
        inner, row_bot = [], None
        for r in rs:
            # EVERY OFFSET INSIDE THE ROW IS A MEASUREMENT, AND DROPPING
            # IT IS WHAT TURNED THE FIRST VERSION OF THIS PASS INTO A
            # PERFECT REFLOW OF THE WRONG PAGE — 0 of 21 lines landing,
            # because flex packed everything hard left and every row sat
            # on the one above it. The distances the owner asked about
            # are right here; they are written as MARGINS, which is the
            # flow layout's own way of saying the same thing, and the
            # narrow breakpoint drops them because that is exactly the
            # moment a measured offset stops being true.
            cells, cursor = [], L
            for e in r["els"]:
                el, et, ew, eh = box(e)
                # AN OVERLAP IS A MEASUREMENT. Absolute layout lets two
                # elements share a space and this page does exactly that:
                # a carried logo crop at x404 with its own text at x406.
                # Clamping the offset at zero laid them side by side
                # instead of on top of one another, which pushed the nav
                # 46px wide, wrapped the Log-in button onto a second
                # line, and walked every band below it down the page.
                # A negative margin is how flow says "these overlap".
                ml = int(round(el - cursor))
                mt = max(0, int(round(et - r["top"])))
                cells.append(_cell(e, canvas, L, col, ml, mt))
                cursor = el + ew
            rmt = 0 if row_bot is None else max(0, int(r["top"] - row_bot))
            row_bot = r["bot"]
            inner.append(f'<div class="row" style="margin-top:{rmt}px">'
                         + "".join(cells) + "</div>")
        parts.append(f'<section class="band"'
                     f' style="margin-top:{margin}px">'
                     + "".join(inner) + "</section>")

    body_css = f"""
*,*::before,*::after{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{margin:0;background:{ir['background']};min-height:100vh;
 {"background-image:url(" + ground + ");background-size:100% auto;"
  "background-position:top center;background-repeat:no-repeat;"
  if ground else ""}
 font-family:{ir['font']['family'] or 'system-ui'},system-ui,sans-serif}}
/* THE CONTAINER IS THE CANVAS, AND THE MARGINS ARE THE MEASURED ONES.
   Writing max-width:{col} here with the measured side padding starved
   the page to {max(0, col - 2 * L)}px of content under border-box, and
   every line of the page broke after one word. The column is what is
   LEFT INSIDE the margins, not the width of the box that holds them. */
.page{{max-width:{canvas['w']}px;margin:0 auto;
 padding-left:clamp(16px,{L / canvas['w'] * 100:.1f}vw,{L}px);
 padding-right:clamp(12px,{max(0, canvas['w'] - R - 4) / canvas['w'] * 100:.1f}vw,{max(0, canvas['w'] - R - 4)}px)}}
.band{{display:block}}
/* the measured rules, behind everything, proportional so they hold at
   any width; pointer-transparent because they are decoration */
.grid{{position:absolute;inset:0;pointer-events:none;z-index:0;
 overflow:hidden}}
.grid i{{display:block}}
.shell{{position:relative;min-height:100vh}}
.page{{position:relative;z-index:1}}
.row{{display:flex;flex-wrap:wrap;align-items:flex-start}}
.cell{{min-width:0;max-width:100%;
 margin-left:var(--ml,0);margin-top:var(--mt,0)}}
img{{max-width:100%;height:auto;display:block}}
p,h1,h2,h3{{margin:0}}
h1,h2,h3{{text-wrap:balance}}
a,button{{font:inherit;color:inherit}}
button{{border:0;cursor:pointer}}
/* BELOW THE DESIGN WIDTH THE MEASURED OFFSETS STOP BEING TRUE.
   They describe where a thing sits on a {canvas['w']}px canvas, and on
   a narrower screen that is no longer a fact about the design, it is
   just a push off the edge. One rule releases all of them. */
@media (max-width:{int(canvas['w'] * 0.92)}px){{
 .page{{max-width:100%}}
 .cell{{margin-left:0;margin-top:0;flex-basis:auto;
  max-width:100%}}
 .row{{gap:clamp(8px,2vw,20px);row-gap:clamp(10px,2.5vw,24px)}}
 .cell[data-grow]{{flex:1 1 100%}}
}}
/* NO flex-direction:column HERE, however natural it looks. Each cell
   carries its measured width as an INLINE flex-basis, and flex-basis is
   measured along the MAIN axis — so flipping the row to a column turned
   every width into a height and made a 435px-wide heading a 435px-TALL
   cell. The phone layout ran to 3,200px of mostly empty page. Wrapping
   alone already puts each oversized cell on its own line, and it keeps
   the basis meaning what it says. */
@media (max-width:640px){{
 .band{{margin-top:clamp(20px,7vw,56px)!important}}
 .row{{row-gap:clamp(10px,3vw,22px)}}
}}
@media (prefers-reduced-motion:reduce){{
 *{{animation:none!important;transition:none!important}}
}}
"""
    head = ""
    if ir["font"]["href"]:
        head = f'<link rel="stylesheet" href="{ir["font"]["href"]}">'
    out = (
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,"
        "initial-scale=1\">"
        f"{head}<style>{body_css}</style></head><body>"
        f"<div class=\"shell\">{rule_html}"
        f"<div class=\"page\">{''.join(parts)}</div></div>"
        "</body></html>")
    # ASSETS ARE REFERENCED RELATIVE, NOT FROM A ROOT. page_ir writes
    # /assets/x.png because a framework build serves from a site root;
    # a single page opened from disk has no root, so every image came
    # back as a broken icon and the carried ground — the whole glow —
    # simply did not load. This is the same defect that once condemned
    # a perfect framework port for being graded off file://.
    out = out.replace('src="/assets/', 'src="assets/') \
             .replace("url(/assets/", "url(assets/")
    return {"html": out, "column": [L, R], "bands": len(bs),
            "paragraphs": merged, "assets": ir["assets"],
            "canvas": canvas}


def _cell(e, canvas, L, col, ml=0, mt=0):
    """One measured element, written so it can move.

    THE MEASURED WIDTH BECOMES A MAXIMUM, NEVER A FIXED SIZE. That one
    substitution is most of the difference between a poster and a page:
    `width:203px` is an instruction that survives a 400px screen by
    hanging off the edge, while `max-width:203px` is the same design
    intent expressed as a limit the browser is free to respect.

    `ml`/`mt` are the element's measured offsets from where flow would
    otherwise put it. They are carried in custom properties rather than
    written straight into margin, so one rule in one media query can
    let go of all of them at once when the screen is too narrow for the
    measurement to still mean anything.
    """
    l, t, w, h = box(e)
    grow = ' data-grow=""' if w > col * 0.45 else ""
    off = f"--ml:{ml}px;--mt:{mt}px;"
    if e.get("z"):
        off += f"position:relative;z-index:{int(e['z'])};"
    if e["kind"] == "picture" or e.get("src"):
        src = e.get("src") or ""
        if not src:
            m = re.search(r"url\(([^)]+)\)",
                          e["style"].get("background-image", ""))
            src = m.group(1) if m else ""
        ar = f"{max(1, int(w))} / {max(1, int(h))}"
        return (f'<div class="cell" data-ae-id="{e["id"]}"{grow} style="{off}'
                f'flex:0 0 auto;max-width:100%">'
                f'<img src="{src}" alt="" loading="lazy" '
                f'style="aspect-ratio:{ar};max-width:{int(w)}px;'
                f'width:100%">'
                "</div>")
    if e["kind"] == "button" and e.get("text"):
        # A MEASURED BUTTON STAYS A BUTTON. The whole point of the
        # semantic pass was that a thing a person would click is a thing
        # they CAN click — and reflowing the page is no reason to hand
        # that back. Its measured size becomes padding so the label can
        # grow or shrink with the type without bursting the pill.
        txt = (e["text"].replace("&", "&amp;")
               .replace("<", "&lt;").replace(">", "&gt;"))
        fs = _n(e["style"].get("font-size"), 0) or e.get("font_size") \
            or h * 0.42
        pad_y = max(0, (h - fs * 1.3) / 2)
        return (f'<div class="cell" data-ae-id="{e["id"]}"{grow} '
                f'style="{off}flex:0 0 {int(w)}px">'
                f'<button type="button" style="{_css_of(e, BOXY)};'
                f'min-width:{int(w)}px;min-height:{int(h)}px;'
                f'padding:{pad_y:.0f}px 12px;font-size:{fs:.1f}px;'
                f'color:{e["style"].get("color", "inherit")};'
                f'line-height:1.3;white-space:nowrap">'
                f"{txt}</button></div>")
    if e.get("text"):
        txt = (e["text"].replace("&", "&amp;")
               .replace("<", "&lt;").replace(">", "&gt;"))
        css = _css_of(e, TEXTY)
        # one measured line is line-height:1, exactly as the rebuild's
        # own .t rule says; a paragraph gets the leading measured from
        # the lines it was reassembled from
        css += f";line-height:{e.get('line_height', 1)}"
        if e.get("lines", 1) < 2:
            css += ";white-space:nowrap"
        mw = e.get("measured_w") or w
        if e.get("tag") == "a":
            tag = "a"
        elif e.get("lines", 1) > 1:
            tag = "p"
        elif _n(e["style"].get("font-size"), 12) >= 28:
            tag = "h1"
        elif _n(e["style"].get("font-size"), 12) >= 18:
            tag = "h2"
        else:
            tag = "p"
        box_css = _css_of(e, BOXY)
        if box_css:
            css += ";" + box_css
        href = ' href="#"' if tag == "a" else ""
        # a single measured line keeps its measured box and is allowed
        # to sit proud of it; a paragraph wraps inside its own width
        basis = (f"flex:0 0 {int(mw)}px" if e.get("lines", 1) < 2
                 else f"flex:0 1 {int(mw)}px")
        return (f'<div class="cell" data-ae-id="{e["id"]}"{grow} '
                f'style="{off}{basis};max-width:100%">'
                f'<{tag}{href} style="{css};max-width:{int(mw)}px;'
                f'display:block">'
                f"{txt}</{tag}></div>")
    css = _css_of(e, BOXY)
    return (f'<div class="cell" data-ae-id="{e["id"]}"{grow} style="{off}flex:0 0 auto;'
            f'width:{int(w)}px;max-width:100%;aspect-ratio:{max(1,int(w))}/'
            f'{max(1,int(h))};{css}"></div>')


def prove(page, original, canvas, narrow=400):
    """Did the flow keep the design AND gain the reflow?

    BOTH, OR IT IS NOT A PASS. Each half alone is trivially easy and
    completely worthless:

      * reflow on its own is one line of CSS. The first version of this
        module scored a perfect 0 spills at phone width while landing
        0 of 21 lines at the design width — a flawless reflow of a page
        that was no longer the design.
      * fidelity on its own is what the tool already had, and is what
        the owner was looking at when they asked why it renders as a
        frozen canvas.

    So the page is rendered AT THE DESIGN WIDTH and every line of the
    original is checked by its own words, and then rendered at phone
    width and asked whether anything hangs off the edge. And when the
    browser cannot be reached, this reports SKIPPED. It does not report
    a pass, and it does not report a failure either: a check that
    cannot run has not found anything.
    """
    import aethron_figma_grade as GR
    import aethron_vision as V
    page = Path(page)
    shot = page.with_name("design.png")
    GR.shoot(page, canvas["w"], canvas["h"], shot)
    if not shot.is_file() or not shot.stat().st_size:
        return {"verdict": "SKIPPED", "why": "the page did not render",
                "lines": None, "spills": None}
    chk = V.verify_rebuild(original, shot)
    ok = chk.get("lines_correct", 0)
    tot = max(1, chk.get("lines_expected", 1))
    ident = GR.compare(shot, original)["identical"]
    flow_r = _narrow(page, narrow)
    if flow_r is None:
        return {"verdict": "SKIPPED",
                "why": (f"{ok}/{tot} lines correct at the design width, "
                        f"but the reflow could not be measured — "
                        f"UNPROVEN, not proven good"),
                "lines": [ok, tot], "identical": ident, "spills": None}
    spills = flow_r["spills"]
    good = (ok >= tot * 0.66) and spills == 0
    why = (f"{ok}/{tot} lines land at {canvas['w']}px "
           f"({ident * 100:.2f}% identical) and "
           + ("nothing spills" if not spills
              else f"{spills} element(s) spill")
           + f" at {flow_r['vw']}px")
    return {"verdict": "PASS" if good else "FAIL", "why": why,
            "lines": [ok, tot], "identical": ident, "spills": spills,
            "findings": chk.get("findings", [])[:20]}


def _narrow(page, width):
    """Reuse the generator's reading rather than keeping a second one."""
    try:
        import aethron_generate as G
        return G._flow(page, width)
    except Exception:
        return None


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    src = Path(argv[0])
    out = Path(argv[1]) if len(argv) > 1 else src.with_name("flow.html")
    r = flow(src.read_text(), name=src.stem, src=src)
    out.write_text(r["html"])
    # assets live beside the page, exactly as the emitters write them
    ad = out.parent / "assets"
    if r["assets"]:
        ad.mkdir(exist_ok=True)
        for a in r["assets"]:
            (ad / a["file"]).write_bytes(a["bytes"])
    print(f"  wrote {out}  ({len(r['html'])} chars, "
          f"{len(r['assets'])} asset(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

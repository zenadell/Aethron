#!/usr/bin/env python3
"""THE EYE. Every coding agent shipping today is blind; this is the fix.

Cursor, Claude Code, Copilot and the rest write front-end code and never
look at it. Lovable, v0 and Bolt render a preview and ask a HUMAN to
look. The industry's own write-ups say it plainly — the 2026 complaint
is "the code compiles, unit tests pass, and the UI is wrong", and the
standard workaround is that "agents capture screenshots for humans to
validate rather than agents validating themselves".

The published state of the art does close the loop: UI2Code^N treats
UI-to-code as interactive visual optimisation and reaches 88.6% on
Design2Code-HARD with a 9B model by drafting, rendering, inspecting and
refining. But its judge is a vision model, and its own paper concedes
the two costs of that — "VLMs are much better comparators than
evaluators", and polishing an already-good UI produces OSCILLATIONS,
changes that alter without improving.

Both costs come from the same place: THEY ARE ASKING FOR AN OPINION.
A rendered page is not a matter of opinion. It is a few thousand exact
numbers, and this project has spent months building the instruments that
read them.

So the eye does not judge. It MEASURES, and it reports three things a
model can act on:

    WHAT IS WRONG     named by the words the element contains, so the
                      builder can find it in its own source
    WHERE IT IS       a real CSS selector taken from the built page's
                      own DOM, not a coordinate in someone else's image
    WHAT TO CHANGE    a property and a value, not a complaint

THE THREE RULES THIS FILE EXISTS TO ENFORCE, each bought with a failure
recorded in AETHRON.md:

 1. A CHECKLIST, NOT A PERCENTAGE. A page that had lost its entire
    navigation scored 95.6% identical, because the page was mostly
    gradient and the gradient was perfect. Percentages cannot fail a
    page that is wrong where it matters. Every line is checked by its
    own words: present, in the right place, at the right size.

 2. NEVER ONE WIDTH. A page graded only at its design width shipped
    rendering every element twice — the one width where a carried
    background lines up with the content on top of it. The industry
    names the same failure: "agents test UI work at one screen width,
    so everything narrower ships unchecked."

 3. MONOTONE OR IT DOES NOT COUNT. Every revision is scored on the
    checklist it is trying to satisfy, and a revision that scores worse
    is thrown away. That is the deterministic answer to the oscillation
    UI2Code^N reports, and it is the property that matters when nobody
    is watching: a model's revision is a coin flip; a change kept only
    when it measures better cannot lose.

WHAT MAKES THE FINDINGS USABLE, and it is the whole difference between
this and the audit that came before it: the candidate is a LIVE PAGE we
are allowed to instrument, so every finding carries a selector the
builder can act on. The previous version compared two PNGs and said
"text at y224 -> move it +10px", naming a run in an image the builder
has never seen. Measured: three separate correction strategies driven by
those findings all scored WORSE than leaving the page alone.
"""
import json
import math
import re
import subprocess
import sys
import tempfile
import shutil
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_figma_grade as GR        # noqa: E402

# The widths a page is judged at. NOT ONE — see rule 2. These are a
# phone, a small tablet, a laptop and a wide desktop; the narrow end is
# where the industry says agent-written UI actually breaks.
WIDTHS = (390, 768, 1280, 1920)

# How close counts as right. Position in CSS pixels, size as a fraction.
POS_TOL = 6
SIZE_TOL = 0.12
# CIEDE2000 units. ~2.3 is the "just noticeable difference"; 5 is a
# colour a designer would call wrong.
COLOR_TOL = 5.0


# ───────────────────────────── reading the page ──────────────────────

PROBE_JS = r"""
<script id="__ae_eye">
(function () {
  function selectorOf(el) {
    // A SELECTOR THE BUILDER CAN USE, preferring what the builder
    // itself wrote. An id is the author's own name for the thing; a
    // data-testid is even better because it exists to be addressed.
    // An nth-child chain is the last resort, because it is the only
    // one that says nothing about intent.
    if (el.id) return '#' + CSS.escape(el.id);
    var t = el.getAttribute('data-testid') || el.getAttribute('data-ae-id');
    if (t) return '[data-testid="' + t + '"]';
    var parts = [], node = el, depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      var part = node.tagName.toLowerCase();
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      var cls = (node.className || '').toString().trim().split(/\s+/)
        .filter(function (c) { return c && !/^(ng-|css-|sc-)/.test(c); });
      if (cls.length) part += '.' + cls.slice(0, 2).map(CSS.escape).join('.');
      var p = node.parentElement;
      if (p) {
        var same = Array.prototype.filter.call(p.children, function (s) {
          return s.tagName === node.tagName;
        });
        if (same.length > 1) {
          part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
      node = p; depth++;
    }
    return parts.join(' > ');
  }

  function ownText(el) {
    // The text THIS element contributes, not its descendants'. A <div>
    // wrapping the whole page "contains" every word on it, and matching
    // on that would name the body for every finding on the page.
    var s = '';
    for (var i = 0; i < el.childNodes.length; i++) {
      var n = el.childNodes[i];
      if (n.nodeType === 3) s += n.nodeValue;
    }
    return s.replace(/\s+/g, ' ').trim();
  }

  function go() {
    var out = [], seen = 0;
    var all = document.body ? document.body.querySelectorAll('*') : [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i], r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      var cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      if (parseFloat(cs.opacity) < 0.05) continue;
      seen++;
      var txt = ownText(el);
      var bg = cs.backgroundColor;
      var hasBg = bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent';
      // WHAT IS ACTUALLY BEHIND THIS TEXT. Contrast is a property of a
      // PAIR, and an element's own background is usually transparent —
      // the colour a reader sees behind a heading belongs to some
      // ancestor. Walking up is the only way to know it, and without it
      // a contrast check is just a guess about the body colour.
      var eff = null, up = el;
      while (up) {
        var ub = getComputedStyle(up).backgroundColor;
        if (ub && ub !== 'rgba(0, 0, 0, 0)' && ub !== 'transparent'
            && !/rgba\(.*,\s*0\)$/.test(ub)) { eff = ub; break; }
        up = up.parentElement;
      }
      var hasImg = cs.backgroundImage && cs.backgroundImage !== 'none';
      // A CARD DEFINED BY ITS BORDER IS STILL A CARD. The filter used
      // to keep only elements with text, a background or an image — so
      // a pricing tier drawn as `border:1px solid` with its text in
      // children was dropped completely, and "the page's largest group
      // of repeated elements is 0" was reported about a page with three
      // identical tiers plainly on it. Structure is what you count
      // repeated things WITH.
      var bw = parseFloat(cs.borderTopWidth) || 0;
      var rad = parseFloat(cs.borderTopLeftRadius) || 0;
      var shadow = cs.boxShadow && cs.boxShadow !== 'none';
      var boxish = (r.width >= 60 && r.height >= 40)
        && ((bw > 0 && cs.borderTopStyle !== 'none') || rad > 0 || shadow);
      if (!txt && !hasBg && !hasImg && !boxish && el.tagName !== 'IMG'
          && el.tagName !== 'SVG') continue;
      out.push({
        sel: selectorOf(el),
        tag: el.tagName.toLowerCase(),
        text: txt.slice(0, 120),
        x: Math.round(r.left + window.scrollX),
        y: Math.round(r.top + window.scrollY),
        w: Math.round(r.width),
        h: Math.round(r.height),
        fs: Math.round(parseFloat(cs.fontSize) * 10) / 10,
        fw: cs.fontWeight,
        color: cs.color,
        bg: hasBg ? bg : null,
        effbg: eff,
        mono: /mono|courier|consol/i.test(cs.fontFamily) || null,
        display: cs.display,
        pad: cs.padding,
        radius: cs.borderTopLeftRadius,
        boxish: boxish || null,
        img: el.tagName === 'IMG' ? (el.currentSrc || el.src || '') : null,
        // did the browser actually decode it? a dead <img> has a box
        // and no picture, and no markup scan can tell the difference
        broke: el.tagName === 'IMG' && el.complete
               && el.naturalWidth === 0
      });
    }
    var d = document.documentElement;
    d.setAttribute('data-ae-eye', JSON.stringify({
      vw: window.innerWidth,
      scroll: Math.round(d.scrollWidth),
      height: Math.round(d.scrollHeight),
      elements: out.length,
      considered: seen,
      items: out
    }));
  }
  if (document.readyState === 'complete') go();
  else window.addEventListener('load', go);
  setTimeout(go, 600);
})();
</script>
"""


def read_page(target, width, height=1400, timeout=90):
    """What the browser actually drew, as numbers. None if it could not.

    Works on a local file or a live URL, because a coding agent's output
    is a dev server far more often than it is a file — and a target the
    user names is usually a URL too. A URL cannot be edited to inject
    the probe, so it rides in on the same trick the probe already uses:
    a temporary copy for files, and for URLs the script is appended
    through a data-less wrapper that navigates and then reports.

    Returns None rather than an empty reading, because a check that
    cannot run reports SKIPPED, never PASS, and never a clean sheet.
    """
    b = GR.find_browser()
    if not b:
        return None
    target = str(target)
    is_url = target.startswith(("http://", "https://"))
    leaked = None
    tmp = Path(tempfile.mkdtemp(prefix="ae-eye-"))
    try:
        if is_url:
            url = target
            # A page we do not own cannot be edited, so the probe is
            # handed to Chrome instead of to the document.
            src = tmp / "probe.js"
            src.write_text(PROBE_JS.split(">", 1)[1].rsplit("<", 1)[0])
            extra = []
        else:
            p = Path(target)
            if p.is_dir():
                for cand in ("index.html", "dist/index.html",
                             "out/index.html", "build/index.html"):
                    if (p / cand).is_file():
                        p = p / cand
                        break
            if not p.is_file():
                return None
            html = p.read_text(errors="replace")
            shot = p.with_suffix(".eye.html")
            shot.write_text(html.replace("</body>", PROBE_JS + "</body>")
                            if "</body>" in html else html + PROBE_JS)
            url = shot.resolve().as_uri()
            extra = []
            leaked = shot
        prof = tmp / "prof"
        cmd = [b, "--headless", "--disable-gpu", "--hide-scrollbars",
               "--force-device-scale-factor=1",
               f"--user-data-dir={prof}",
               f"--window-size={width},{height}",
               "--virtual-time-budget=9000", "--dump-dom", *extra, url]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
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
        done.wait(timeout)
        # CHROME DOES NOT EXIT AFTER --dump-dom. Documented in this
        # project since the probe was built, and re-learned the hard way
        # by a reflow check that hung its whole timeout on every round of
        # every run and reported the reassuring answer when it threw.
        try:
            proc.kill()
        except Exception:
            pass
        blob = "".join(dom)
        # DELETE THE FILE WE ACTUALLY WROTE. This used to recompute the
        # path from `target`, but when `target` is a DIRECTORY — the
        # primary documented use, "point it at your project" — the probe
        # copy is written to <dir>/index.eye.html while the cleanup
        # deleted <dir>.eye.html, which is a different path that never
        # existed. The instrument would have left a stray file inside
        # every project it was ever pointed at.
        if leaked is not None:
            leaked.unlink(missing_ok=True)
        m = re.search(r'data-ae-eye="([^"]*)"', blob)
        if not m:
            return None
        import html as _h
        r = json.loads(_h.unescape(m.group(1)))
        r["width"] = width
        return r
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_image(path):
    """A reference that is a picture, read the only way a picture can be.

    OCR gives ONE BOX PER LINE, which is the fact that settles element
    identity — a paragraph measured as ink is one 32px run, and the
    words are what let a finding name something the builder recognises.
    """
    import aethron_vision as V
    lines = V.ocr(path)
    if lines is None:
        return None
    shot = V.load(path)
    items = []
    for ln in lines:
        if ln.get("confidence", 1) < 0.35:
            continue
        items.append({"sel": None, "tag": "text",
                      "text": (ln.get("text") or "").strip(),
                      "x": ln["x"], "y": ln["y"],
                      "w": ln["w"], "h": ln["h"],
                      "fs": None, "color": None, "bg": None,
                      "img": None, "broke": False})
    return {"vw": shot.w, "height": shot.h, "width": shot.w,
            "scroll": shot.w, "elements": len(items),
            "considered": len(items), "items": items, "image": str(path)}


# ───────────────────────────── comparing ─────────────────────────────

def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _sim(a, b):
    """Sørensen-Dice on character bigrams — the benchmark's own text
    metric, and the right one here for the same reason: a rebuild set in
    a face we do not have reads back as 'Dacs' for 'Docs', and a checker
    that demands an exact string reports six correct lines as missing.
    """
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 1.0 if a == b else 0.0
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    A = [a[i:i + 2] for i in range(len(a) - 1)]
    B = [b[i:i + 2] for i in range(len(b) - 1)]
    from collections import Counter
    ca, cb = Counter(A), Counter(B)
    inter = sum((ca & cb).values())
    return 2 * inter / (len(A) + len(B))


def _lab(rgb):
    r, g, b = [c / 255 for c in rgb]

    def f(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = f(r), f(g), f(b)
    x = (r * .4124 + g * .3576 + b * .1805) / .95047
    y = (r * .2126 + g * .7152 + b * .0722)
    z = (r * .0193 + g * .1192 + b * .9505) / 1.08883

    def h(t):
        return t ** (1 / 3) if t > .008856 else 7.787 * t + 16 / 116
    x, y, z = h(x), h(y), h(z)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


def delta_e(c1, c2):
    """CIEDE2000 — the perceptual difference, and the metric Design2Code
    itself scores colour with. Worth the forty lines: RGB distance calls
    two dark greys further apart than a designer ever would, and calls
    two bright yellows identical when they are not.
    """
    L1, a1, b1 = _lab(c1)
    L2, a2, b2 = _lab(c2)
    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7))) if Cb else 0
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (b1 or a1p) else 0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (b2 or a2p) else 0
    dLp = L2 - L1
    dCp = C2p - C1p
    dhp = 0.0
    if C1p * C2p:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp) / 2)
    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    hbp = h1p + h2p
    if C1p * C2p:
        if abs(h1p - h2p) > 180:
            hbp = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else \
                  (h1p + h2p - 360) / 2
        else:
            hbp = (h1p + h2p) / 2
    T = (1 - 0.17 * math.cos(math.radians(hbp - 30))
         + 0.24 * math.cos(math.radians(2 * hbp))
         + 0.32 * math.cos(math.radians(3 * hbp + 6))
         - 0.20 * math.cos(math.radians(4 * hbp - 63)))
    dTh = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7)) if Cbp else 0
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / math.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -math.sin(math.radians(2 * dTh)) * Rc
    return math.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                     + Rt * (dCp / Sc) * (dHp / Sh))


def _rgb(css):
    if not css:
        return None
    m = re.findall(r"[\d.]+", css)
    if len(m) < 3:
        return None
    if len(m) >= 4 and float(m[3]) < 0.05:
        return None
    return tuple(int(float(v)) for v in m[:3])


def _reading_order(items):
    """Down the page, then across each row — the order a person reads.

    NOT (y, x). A nav sits at y = 22, 24, 24, 26, 26, 26, and sorting by
    the raw coordinate puts those six items in an order that depends on
    two pixels of noise. The reference and the built page then produce
    DIFFERENT PERMUTATIONS of the same row, and a monotone alignment can
    only resolve that by dropping matches — which is exactly how the one
    Log-in button on the page came back reported as MISSING and as EXTRA
    at the same time.

    Elements whose vertical spans overlap are one row, the same rule
    that finally found the buttons. Within a row, read left to right.
    """
    rows = []
    for e in sorted(items, key=lambda e: e["y"]):
        for r in rows:
            over = min(r["bot"], e["y"] + e["h"]) - max(r["top"], e["y"])
            if over > 0.45 * min(e["h"] or 1, r["bot"] - r["top"] or 1):
                r["els"].append(e)
                r["top"] = min(r["top"], e["y"])
                r["bot"] = max(r["bot"], e["y"] + e["h"])
                break
        else:
            rows.append({"top": e["y"], "bot": e["y"] + e["h"],
                         "els": [e]})
    out = []
    for r in sorted(rows, key=lambda r: r["top"]):
        out.extend(sorted(r["els"], key=lambda e: e["x"]))
    return out


def align(ref_items, cand_items, floor=0.62):
    """Pair reference elements with built ones BY THEIR WORDS.

    Text settles identity exactly, and every attempt to infer it from
    order and geometry has lost: measured, four of them scored 41.4%
    (no change at all), defeated, 22.3% and 19.5%. "Get started for
    free" is the same thing in both pages because it is the same string.

    The pairing is MONOTONIC — both lists run down the page, so a match
    may not cross an earlier one. Without that rule one element slightly
    out of place steals its neighbour's partner and every comparison
    after it compares the wrong two things, which is how an audit came
    to advise "multiply this font-size by 0.400" about a heading it had
    mistaken for a nav item.
    """
    ref = _reading_order([r for r in ref_items if _norm(r.get("text"))])
    cand = _reading_order([c for c in cand_items if _norm(c.get("text"))])
    n, m = len(ref), len(cand)
    # longest-common-subsequence alignment on text similarity: forbids
    # crossings by construction
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            s = _sim(ref[i]["text"], cand[j]["text"])
            take = (s + best[i + 1][j + 1]) if s >= floor else -1
            best[i][j] = max(best[i + 1][j], best[i][j + 1], take)
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        s = _sim(ref[i]["text"], cand[j]["text"])
        take = (s + best[i + 1][j + 1]) if s >= floor else -1
        if take >= best[i + 1][j] and take >= best[i][j + 1] and take >= 0:
            pairs.append((ref[i], cand[j], s))
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    matched_r = {id(a) for a, _, _ in pairs}
    matched_c = {id(b) for _, b, _ in pairs}
    return (pairs,
            [r for r in ref if id(r) not in matched_r],
            [c for c in cand if id(c) not in matched_c])


def compare(ref, cand, pos_tol=POS_TOL, size_tol=SIZE_TOL,
            color_tol=COLOR_TOL, scale=None, ref_is_ink=True):
    """Findings a builder can act on. Not a score — a list of repairs.

    Every finding names the WORDS of the thing, a SELECTOR in the built
    page, and the CHANGE to make. That triple is the whole difference
    between a report a human reads with both files open and a report the
    thing holding the code can use.
    """
    if scale is None:
        scale = (cand["vw"] / ref["vw"]) if ref.get("vw") else 1.0
    pairs, missing, extra = align(ref["items"], cand["items"])
    out = []
    for r, c, s in pairs:
        want_x, want_y = r["x"] * scale, r["y"] * scale
        dx, dy = c["x"] - want_x, c["y"] - want_y
        if max(abs(dx), abs(dy)) > pos_tol:
            out.append({
                "kind": "MISPLACED", "text": r["text"][:60],
                "selector": c["sel"], "tag": c["tag"],
                "want": [round(want_x), round(want_y)],
                "got": [c["x"], c["y"]],
                "fix": (f"move it {abs(dx):.0f}px "
                        f"{'left' if dx > 0 else 'right'}"
                        if abs(dx) > pos_tol else "")
                       + (" and " if abs(dx) > pos_tol and abs(dy) > pos_tol
                          else "")
                       + (f"{abs(dy):.0f}px "
                          f"{'up' if dy > 0 else 'down'}"
                          if abs(dy) > pos_tol else ""),
            })
        # COMPARE LIKE WITH LIKE, OR DO NOT COMPARE. OCR reports where
        # the INK is; a <button>'s box is that ink plus its padding, and
        # measuring one against the other told us to shrink a perfectly
        # correct 10.6px label by 0.46. Where the built element is a
        # tight line box the heights are the same measurement and can be
        # compared directly; where it is padded, the only honest
        # comparison is against its font-size — and then only outside
        # the band that cap height and descenders genuinely span, which
        # is why this reports a RANGE rather than inventing a number.
        want_h = (r.get("h") or 0) * scale
        if not ref_is_ink and r.get("fs") and c.get("fs"):
            # BOTH SIDES ARE LIVE PAGES, so there is nothing to infer:
            # compare the font-size to the font-size and be exact.
            # Treating the reference's BOX height as ink height told us
            # a correct 13px button label — 31px tall with its padding —
            # needed to be 40px, on a page being compared WITH ITSELF.
            # A referee that fires on an identical page is one people
            # switch off, and the probe learned that lesson once
            # already.
            want_fs = r["fs"] * scale
            if abs(c["fs"] - want_fs) / max(1.0, want_fs) > size_tol:
                out.append({
                    "kind": "WRONG SIZE", "text": r["text"][:60],
                    "selector": c["sel"], "tag": c["tag"],
                    "want": round(want_fs, 1), "got": c["fs"],
                    "fix": f"set font-size to {want_fs:.1f}px "
                           f"(it is {c['fs']}px)",
                })
        elif want_h > 2:
            tight = c.get("fs") and c["h"] <= c["fs"] * 1.8
            if tight and abs(c["h"] - want_h) / want_h > size_tol:
                f = want_h / max(1.0, c["h"])
                out.append({
                    "kind": "WRONG SIZE", "text": r["text"][:60],
                    "selector": c["sel"], "tag": c["tag"],
                    "want": round(want_h, 1), "got": c["h"],
                    "fix": (f"scale its font-size by {f:.2f}"
                            + (f" (about {c['fs'] * f:.1f}px)"
                               if c.get("fs") else "")),
                })
            elif not tight and c.get("fs"):
                lo, hi = want_h / 0.95, want_h / 0.66
                if not (lo <= c["fs"] <= hi):
                    out.append({
                        "kind": "WRONG SIZE", "text": r["text"][:60],
                        "selector": c["sel"], "tag": c["tag"],
                        "want": f"{lo:.1f}-{hi:.1f}px", "got": c["fs"],
                        "fix": (f"set font-size into "
                                f"{lo:.1f}-{hi:.1f}px (it is {c['fs']}px; "
                                f"the reference ink is {want_h:.0f}px "
                                f"tall)"),
                    })
        rc, cc = _rgb(r.get("color")), _rgb(c.get("color"))
        if rc and cc:
            de = delta_e(rc, cc)
            if de > color_tol:
                out.append({
                    "kind": "WRONG COLOUR", "text": r["text"][:60],
                    "selector": c["sel"], "tag": c["tag"],
                    "want": "#%02X%02X%02X" % rc,
                    "got": "#%02X%02X%02X" % cc,
                    "fix": f"set color to #%02X%02X%02X (off by "
                           f"{de:.1f} CIEDE2000)" % rc,
                })
    pics = [c for c in cand["items"] if c.get("img") is not None
            or c.get("tag") in ("img", "svg", "canvas")]
    for r in missing:
        # A WORDMARK IS A PICTURE, AND OCR READS IT ANYWAY. The
        # reference is an image, so OCR happily returns the letters
        # inside a logo the built page ships as an <img> — and calling
        # that MISSING sends the builder off to set a logotype as type,
        # which is precisely how a row of wordmarks once shipped as
        # "VIVUVIYOIIII". If the words fall inside a picture on the
        # built page, they are present; they are just not type.
        rx, ry = r["x"] * scale, r["y"] * scale
        if any(p["x"] - 4 <= rx <= p["x"] + p["w"] + 4
               and p["y"] - 4 <= ry <= p["y"] + p["h"] + 4
               for p in pics):
            continue
        out.append({"kind": "MISSING", "text": r["text"][:60],
                    "selector": None, "tag": None,
                    "want": [r["x"], r["y"]], "got": None,
                    "fix": f"this text is not on the built page at all — "
                           f"add it near ({round(r['x'] * scale)},"
                           f"{round(r['y'] * scale)})"})
    for c in extra:
        if len(_norm(c["text"])) < 3:
            continue
        out.append({"kind": "EXTRA", "text": c["text"][:60],
                    "selector": c["sel"], "tag": c["tag"],
                    "want": None, "got": [c["x"], c["y"]],
                    "fix": "this text is not in the reference — remove it "
                           "or check it is not placeholder copy"})
    for c in cand["items"]:
        if c.get("broke"):
            out.append({"kind": "BROKEN IMAGE", "text": "",
                        "selector": c["sel"], "tag": "img",
                        "want": None, "got": c.get("img"),
                        "fix": "this <img> has a box but the browser "
                               "could not decode it — fix the src"})
    return {"findings": out, "matched": len(pairs),
            "expected": len(pairs) + len(missing)}


# ─────────────────────────────── the verdict ─────────────────────────

def look(candidate, reference, widths=WIDTHS, height=1400, verbose=True):
    """Render the built thing beside the target and report the repairs.

    THE VERDICT IS A CHECKLIST. `ok/expected` counts elements that are
    present, placed and sized correctly — a page missing its navigation
    fails here however good it looks by area, which is exactly what a
    percentage could not do.
    """
    def say(*a):
        if verbose:
            print(*a)

    ref_is_image = (not str(reference).startswith(("http://", "https://"))
                    and Path(str(reference)).suffix.lower()
                    in (".png", ".jpg", ".jpeg", ".webp"))
    rounds, worst = [], None
    for w in widths:
        cand = read_page(candidate, w, height)
        if cand is None:
            say(f"  {w:>5}px  UNMEASURED — the page did not render")
            rounds.append({"width": w, "skipped": True})
            continue
        if ref_is_image:
            ref = read_image(reference)
            # A picture has ONE width. Judging a phone render against a
            # desktop screenshot is not a comparison, it is a category
            # error — so the reference only grades its own width, and
            # the others are judged on reflow alone.
            grade_here = abs(ref["vw"] - w) <= max(40, ref["vw"] * 0.06)
        else:
            ref = read_page(reference, w, height)
            grade_here = ref is not None
        if ref is None or not grade_here:
            spill = sum(1 for it in cand["items"]
                        if it["x"] + it["w"] > cand["vw"] + 2)
            say(f"  {w:>5}px  reflow only — "
                + (f"{spill} element(s) spill past the edge" if spill
                   else "nothing spills"))
            rounds.append({"width": w, "reflow_only": True,
                           "spills": spill,
                           "scroll": cand["scroll"], "vw": cand["vw"]})
            continue
        r = compare(ref, cand, ref_is_ink=ref_is_image)
        bad = {f["text"] for f in r["findings"]
               if f["kind"] in ("MISPLACED", "WRONG SIZE", "WRONG COLOUR",
                                "MISSING")}
        ok = max(0, r["expected"] - len(bad))
        spill = sum(1 for it in cand["items"]
                    if it["x"] + it["w"] > cand["vw"] + 2)
        row = {"width": w, "ok": ok, "expected": r["expected"],
               "findings": r["findings"], "spills": spill,
               "scroll": cand["scroll"], "vw": cand["vw"]}
        rounds.append(row)
        say(f"  {w:>5}px  {ok}/{r['expected']} elements correct · "
            f"{len(r['findings'])} finding(s) · "
            + (f"{spill} spill" if spill else "no spill"))
        if worst is None or (ok / max(1, r["expected"])
                             < worst["ok"] / max(1, worst["expected"])):
            worst = row

    graded = [r for r in rounds if "ok" in r]
    total_ok = sum(r["ok"] for r in graded)
    total_ex = sum(r["expected"] for r in graded)
    spills = sum(r.get("spills", 0) for r in rounds if not r.get("skipped"))
    if not graded:
        return {"verdict": "SKIPPED",
                "why": "nothing could be graded — no render, or the "
                       "reference never matched a width",
                "rounds": rounds, "score": None}
    score = total_ok / max(1, total_ex)
    verdict = "PASS" if (score >= 0.95 and spills == 0) else "FAIL"
    return {"verdict": verdict, "score": score,
            "ok": total_ok, "expected": total_ex, "spills": spills,
            "rounds": rounds,
            "findings": (worst or graded[0]).get("findings", []),
            "why": (f"{total_ok}/{total_ex} elements correct across "
                    f"{len(graded)} width(s), {spills} spill(s)")}


def with_design(report, page, widths=None, spec=None):
    """Fold the design audit into a fidelity report.

    TWO QUESTIONS, ONE ANSWER. "Does it match the target" and "is it a
    good interface" are different, and a page can pass either while
    failing the other — a pixel-perfect replica of a badly designed
    mock is still badly designed, and a beautifully systematic page
    that is not the design you asked for is the wrong page.
    """
    try:
        import aethron_design as D
    except Exception:
        return report
    d = D.look(page, widths=tuple(widths or (390, 1280)), spec=spec,
               verbose=False)
    report = dict(report)
    report["design"] = {k: d[k] for k in ("verdict", "why", "system")
                        if k in d}
    report["findings"] = (report.get("findings", [])
                          + d.get("findings", []))
    if d["verdict"] == "FAIL" and report.get("verdict") == "PASS":
        report["verdict"] = "FAIL"
        report["why"] = (report.get("why", "")
                         + f"; matches the target, but {d['why']}")
    return report


def brief(report, limit=25):
    """The report as the builder should receive it: repairs, in order of
    how much they matter, each naming a selector it can act on.
    """
    if report.get("verdict") == "SKIPPED":
        return "SKIPPED — " + report.get("why", "")
    rank = {"MISSING": 0, "BROKEN IMAGE": 1, "LOW CONTRAST": 2,
            "MISPLACED": 3, "TAP TARGET": 4, "WRONG SIZE": 5,
            "WRONG COLOUR": 6, "NO TYPE SCALE": 7, "OFF SCALE": 8,
            "NOT ALIGNED": 9, "OFF GRID": 10, "EXTRA": 11}
    fs = sorted(report.get("findings", []),
                key=lambda f: rank.get(f["kind"], 9))
    lines = [f"{report['verdict']} — {report['why']}", ""]
    for f in fs[:limit]:
        who = f.get("selector") or "(not on the page)"
        txt = f'"{f["text"]}"' if f.get("text") else f.get("tag") or ""
        lines.append(f"  {f['kind']:<13} {txt}")
        lines.append(f"                {who}")
        if f.get("fix"):
            lines.append(f"                -> {f['fix']}")
    if len(fs) > limit:
        lines.append(f"  … and {len(fs) - limit} more")
    return "\n".join(lines)


# ───────────────────────────── closing the loop ──────────────────────

SAFE = ("WRONG SIZE", "WRONG COLOUR")


def patch(findings):
    """Turn findings into CSS, but ONLY the ones that are arithmetic.

    A finding carries a real selector in the built page, so a repair can
    be written as a rule rather than as a request. But only where the
    change is LOCAL AND PROVABLE: a font-size or a colour affects the
    element and nothing else, while moving something in flow layout
    pushes everything after it — a "fix" that is really a new guess, and
    guesses are what this whole architecture exists to remove.

    So the tool repairs what it can prove and hands the rest to the
    model. That division is the same one that makes copy_map work.
    """
    rules, done = [], set()
    for f in findings:
        sel = f.get("selector")
        if not sel or f["kind"] not in SAFE:
            continue
        key = (sel, f["kind"])
        if key in done:
            continue
        if f["kind"] == "WRONG SIZE":
            m = re.search(r"about ([\d.]+)px", f.get("fix", ""))
            if not m:
                m = re.search(r"into ([\d.]+)-([\d.]+)px", f.get("fix", ""))
                if not m:
                    continue
                lo, hi = float(m.group(1)), float(m.group(2))
                val = (lo + hi) / 2
            else:
                val = float(m.group(1))
            if not (4 <= val <= 400):
                continue
            # !important, AND IT IS NOT LAZINESS. Generated pages set
            # type INLINE, and an inline declaration outranks every
            # stylesheet rule — so the first version of this patch
            # applied five rules, changed nothing, and the loop
            # correctly reported no improvement for a repair that had
            # never actually happened. A repair layer that cannot reach
            # the thing it names is not a repair layer.
            rules.append(f"{sel}{{font-size:{val:.1f}px!important}}")
        elif f["kind"] == "WRONG COLOUR":
            want = f.get("want")
            if isinstance(want, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}",
                                                      want):
                rules.append(f"{sel}{{color:{want}!important}}")
        done.add(key)
    return rules


def apply_patch(html, rules):
    block = ("<style data-ae-eye-patch>" + "".join(rules) + "</style>")
    html = re.sub(r"<style data-ae-eye-patch>.*?</style>", "", html,
                  flags=re.S)
    return (html.replace("</head>", block + "</head>") if "</head>" in html
            else html.replace("<body", block + "<body", 1))


def type_scale(findings, floor=3, spread=0.10):
    """One fault wearing many costumes: the whole type scale is off.

    The first repair pass applied five independent font-size rules and
    took the page from 75% to 40%, because IN FLOW LAYOUT A FONT-SIZE IS
    NOT LOCAL — enlarging one line pushes everything below it down, and
    every element after it becomes misplaced. The guard threw the change
    away, correctly, but the diagnosis was wrong before the repair was.

    Look at what the findings actually said: 1.17, 1.15, 1.17, 1.15.
    Four elements do not independently agree to that precision. That is
    ONE fault — the page's type scale — and the repair for it is a
    single factor applied to every size at once, which moves nothing
    relative to anything else.
    """
    fs = []
    for f in findings:
        if f["kind"] != "WRONG SIZE":
            continue
        m = re.search(r"scale its font-size by ([\d.]+)", f.get("fix", ""))
        if m:
            fs.append(float(m.group(1)))
            continue
        m = re.search(r"into ([\d.]+)-([\d.]+)px \(it is ([\d.]+)px",
                      f.get("fix", ""))
        if m:
            lo, hi, got = (float(m.group(1)), float(m.group(2)),
                           float(m.group(3)))
            if got:
                fs.append(((lo + hi) / 2) / got)
    if len(fs) < floor:
        return None
    fs.sort()
    mid = fs[len(fs) // 2]
    # they must actually AGREE. A median over four numbers that disagree
    # is just a number, and applying it would be the guess this exists
    # to avoid.
    if not all(abs(v - mid) <= spread * mid for v in fs):
        return None
    return mid if abs(mid - 1) > 0.02 else None


def scale_rules(items, factor):
    """Every measured size multiplied together, so nothing moves
    relative to anything else."""
    rules, seen = [], set()
    for it in items:
        if not it.get("fs") or not it.get("sel") or not it.get("text"):
            continue
        if it["sel"] in seen:
            continue
        seen.add(it["sel"])
        rules.append(f"{it['sel']}{{font-size:"
                     f"{it['fs'] * factor:.1f}px!important}}")
    return rules


def refine(page, reference, rounds=4, widths=None, verbose=True):
    """Look, repair, look again — and KEEP ONLY WHAT MEASURES BETTER.

    This is the deterministic answer to the oscillation UI2Code^N
    reports when polishing an already-good UI. Their loop asks a vision
    model whether the new version is better, and a model's opinion of a
    small change is close to a coin flip. Here every round is scored on
    the checklist it is trying to satisfy, and a round that scores worse
    is thrown away and the loop stops. It cannot regress — which is the
    only property that matters when nobody is watching.

    And it needs no model at all. The findings already carry the
    selector and the number.
    """
    def say(*a):
        if verbose:
            print(*a)

    page = Path(page)
    widths = widths or WIDTHS
    original = page.read_text()
    best_html, trail = original, []
    r = look(page, reference, widths=widths, verbose=False)
    if r.get("score") is None:
        return {"verdict": "SKIPPED", "why": r.get("why"), "trail": []}
    best = r
    trail.append(round(r["score"], 4))
    say(f"  round 0  {r['ok']}/{r['expected']} "
        f"({r['score'] * 100:.1f}%)  {len(r.get('findings', []))} finding(s)")
    try:
        for i in range(1, rounds + 1):
            # PROPOSALS, TRIED ONE AT A TIME AND KEPT ONLY IF BETTER.
            # A batch of independent repairs is a batch of independent
            # risks, and in flow layout they interact.
            props = []
            sc = type_scale(best.get("findings", []))
            if sc:
                probe = read_page(page, (widths or WIDTHS)[0])
                if probe:
                    props.append((f"type scale x{sc:.2f}",
                                  scale_rules(probe["items"], sc)))
            for f in best.get("findings", []):
                one = patch([f])
                if one:
                    props.append((f'{f["kind"].lower()} on '
                                  f'"{f.get("text", "")[:24]}"', one))
            props = [(n, r) for n, r in props if r][:8]
            if not props:
                say(f"  round {i}  nothing left that can be repaired "
                    f"mechanically")
                break
            won, gain = None, best["score"]
            for name, rules in props:
                page.write_text(apply_patch(best_html, rules))
                t = look(page, reference, widths=widths, verbose=False)
                if t.get("score") is not None and t["score"] > gain + 1e-6:
                    won, gain = (name, rules, t), t["score"]
            if won is None:
                say(f"  round {i}  tried {len(props)} repair(s), none "
                    f"measured better — stopping")
                break
            name, rules, r = won
            page.write_text(apply_patch(best_html, rules))
            trail.append(round(r["score"], 4))
            say(f"  round {i}  {r['ok']}/{r['expected']} "
                f"({r['score'] * 100:.1f}%)  kept: {name} "
                f"({len(rules)} rule(s), tried {len(props)})")
            best, best_html = r, page.read_text()
    finally:
        page.write_text(best_html)
    return {"verdict": best["verdict"], "score": best["score"],
            "ok": best["ok"], "expected": best["expected"],
            "why": best["why"], "trail": trail,
            "findings": best.get("findings", [])}


def main(argv):
    if len(argv) < 2 or {"-h", "--help"} & set(argv):
        print(__doc__.split("\n\n")[0])
        print("\nusage: aethron_eye.py <built> <reference> "
              "[--widths 390,1280]")
        print("  <built>      a URL, an .html file, or a project dir")
        print("  <reference>  a URL or a screenshot")
        return 0
    widths = WIDTHS
    if "--widths" in argv:
        widths = tuple(int(x) for x in
                       argv[argv.index("--widths") + 1].split(","))
    r = look(argv[0], argv[1], widths=widths)
    print()
    print(brief(r))
    return 0 if r["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

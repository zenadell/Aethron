#!/usr/bin/env python3
"""Aethron Convert — the pixel-perfect port. No model, no runtime, no
dependency on the platform the template came from.

WHY THIS EXISTS, AND WHY IT IS NOT THE AI PORT: asking a model to
rewrite a template produces an approximation — measured on a real
Framer template: 75% of the copy and a design that merely resembles
the original. This does the opposite of rewriting. It CARRIES what
already exists, exactly as editor mode does, and that is why it can be
pixel-perfect.

WHAT THE EVIDENCE SAID (agero, a real Framer template):

    every <script> stripped  ->  6,296 chars of text, 152 images,
                                 26/26 headings, 99% identical

The design is entirely in the CSS Framer emits. The runtime contributes
nothing to layout, type, colour or spacing. It only does one visible
thing:

    49 elements sit at inline `opacity: 0`, waiting to be animated in.

Both ends of those animations are already in the DOM — the start is
written in the style attribute, the end is the CSS default. So the
entrance is recovered mechanically, not generated: strip the parked
initial state, hand it to a ~20-line IntersectionObserver that ships as
readable source in the user's own repo. No Framer runtime is carried,
and nothing is a black box.

    python3 aethron_convert.py <project> --framework astro|next|vite
"""
import html as html_mod
import json
import re
import shutil
import subprocess
import sys
import threading
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE = ROOT / "forge.py"
FRAMEWORKS = ("astro", "next", "vite")

# The entrance runtime. Twenty lines the owner can read, delete or
# rewrite — the point of the whole exercise.
MOTION_TAG = '\n<script src="/aethron-motion.js" defer></script>\n'


def anim_tag(doc) -> str:
    """The original's own animation definitions AND the recording of what
    it actually does, carried into the port."""
    out = ""
    # the engine's own data tags first, verbatim, under their real ids
    for tag in (doc.get("engine_data") or []):
        out += "\n" + tag
    spec = doc.get("spec") or {}
    if spec.get("anims") and not doc.get("engine_data"):
        out += ('\n<script type="application/json" id="__ae_anim">'
                + json.dumps(spec) + "</script>")
    # The original engine runs FIRST — it owns every element carrying a
    # data-framer-appear-id. Our runtime then handles only what it does
    # not: recovered blur states and anything without a spec.
    if doc.get("keep_runtime"):
        return ""          # the original's code is already in the body
    for src in (doc.get("engine") or []):
        out += "\n<script>" + src + "</script>"
    tl = doc.get("timeline") or {}
    # NOT mutually exclusive with the engine: the engine owns elements
    # carrying data-framer-appear-id, the recording owns everything else,
    # and the runtime already skips the overlap. Gating one on the other
    # silently dropped the recording on every build.
    if tl.get("entries"):
        out += ('\n<script type="application/json" id="__ae_timeline">'
                + json.dumps(tl) + "</script>")
    # The measured animations. These outrank both of the above: they are
    # the browser's own animation objects, read back with the exact
    # keyframes and timing the original was given.
    ent = compress_entrance(doc.get("entrance") or {})
    if ent.get("anims"):
        out += ('\n<script type="application/json" id="__ae_entrance">'
                + json.dumps(ent, separators=(",", ":")) + "</script>")
    cont = doc.get("continuous") or {}
    if cont.get("rotate") or cont.get("loop"):
        out += ('\n<script type="application/json" id="__ae_continuous">'
                + json.dumps({"rotate": cont.get("rotate") or [],
                              "loop": cont.get("loop") or []},
                             separators=(",", ":")) + "</script>")
    scr = doc.get("scroll") or {}
    wh = doc.get("wheel") or {}
    if wh.get("available"):
        out += ('\n<script type="application/json" id="__ae_wheel">'
                + json.dumps({"container": wh.get("container") or "",
                              "via": wh.get("container_via") or "",
                              "cycle": wh.get("cycle") or 0,
                              "stops": wh.get("stops") or [],
                              "elements": wh.get("elements") or []},
                             separators=(",", ":")) + "</script>")
    if scr.get("counters") or scr.get("tracks"):
        out += ('\n<script type="application/json" id="__ae_scroll">'
                + json.dumps({"counters": scr.get("counters") or [],
                              "tracks": scr.get("tracks") or [],
                              # one-shots too. Leaving these out shipped
                              # a build where 55 animations were
                              # classified, measured, and then silently
                              # never emitted.
                              "oneshots": scr.get("oneshots") or [],
                              "height": scr.get("height") or 0},
                             separators=(",", ":")) + "</script>")
    return out + MOTION_TAG


def restore_consumed_scripts(dom: str, source_html: str) -> str:
    """Put back scripts the runtime ate before we photographed the page.

    We capture the POST-JS DOM, which is what makes the port look right.
    But a runtime that CONSUMES its inputs leaves holes: Webflow's
    commerce code reads its `data-wf-template-id` list templates and
    removes them, so the capture has no trace of them — and a carry-mode
    port then re-runs that same runtime with its inputs missing, and the
    lists it should build never appear.

    Anything the source shipped and the capture lacks is restored, keyed
    by content so nothing is duplicated.
    """
    src_scripts = re.findall(r'(?is)<script\b(?![^>]*\bsrc=)[^>]*>(.*?)'
                             r'</script\s*>', source_html)
    if not src_scripts:
        return dom
    have = set()
    for body in re.findall(r'(?is)<script\b(?![^>]*\bsrc=)[^>]*>(.*?)'
                           r'</script\s*>', dom):
        have.add(body.strip()[:400])
    missing = []
    seen = set()
    for body in src_scripts:
        key = body.strip()[:400]
        if not key or key in have or key in seen:
            continue
        seen.add(key)
        # find the ORIGINAL tag so its attributes (type, template id)
        # come back with it — a bare <script> would lose the binding
        m = re.search(r'(?is)(<script\b(?![^>]*\bsrc=)[^>]*>)'
                      + re.escape(body[:120]), source_html)
        open_tag = m.group(1) if m else "<script>"
        missing.append(open_tag + body + "</script>")
    if not missing:
        return dom
    print(f"── restored {len(missing)} script(s) the runtime consumed "
          f"before capture")
    block = "\n".join(missing)
    if "</body>" in dom:
        return dom.replace("</body>", block + "</body>", 1)
    return dom + block


def astro_markup(html: str) -> str:
    """Make raw template HTML safe to paste into an .astro component.

    Two things Astro does to markup that a carried page cannot survive:

    1. `{` starts an expression. Braces become numeric entities, which
       render and decode identically in markup and attribute values, so a
       GraphQL query inside a data attribute comes through intact.

    2. IT PROCESSES <script> AND <style>. Astro hoists component scripts
       into ES modules and scopes component styles. Hoisting turned
       Framer's `var animator = …` into a module-scoped variable, so the
       global its engine looks for no longer existed and the appear
       animations stopped — the script was in the source, absent from
       every bundle, and nothing but a parity check would have noticed.
       Scoping would rewrite the template's own selectors the same way.
       `is:inline` tells Astro to emit both exactly as written.

    Neither transformation is wrong; they are for code you authored. This
    is code we are carrying, and it has to arrive unchanged.
    """
    def mark(m):
        tag, attrs = m.group(1), m.group(2)
        if "is:inline" in attrs:
            return m.group(0)
        return f"<{tag}{attrs} is:inline>"

    parts = re.split(r'(?is)(<(?:style|script)\b[^>]*>.*?</(?:style|script)\s*>)',
                     html)
    out = []
    for i, chunk in enumerate(parts):
        if i % 2:
            # raw-text element: entities are NOT decoded inside it, so its
            # contents must be left byte-for-byte alone
            out.append(re.sub(r'(?is)^<(style|script)((?:[^>"\']|"[^"]*"|\'[^\']*\')*)>',
                              mark, chunk, count=1))
        else:
            out.append(chunk.replace("{", "&#123;").replace("}", "&#125;"))
    return "".join(out)


def clean_urls(html: str) -> str:
    """Rewrite ./page.html links to the clean paths the original served.

    The migration writes local links as ./about.html because it ships
    plain files. A framework port builds real routes, so it should serve
    /about — which is what the scraped site served in the first place.
    Shipping /about.html made the port match neither the original nor the
    framework's own conventions.
    """
    def sub(m):
        stem = m.group("p")
        tail = m.group("t") or ""
        if stem in ("index", "home"):
            return 'href="/' + tail + '"'
        return 'href="/' + stem + tail + '"'
    return re.sub(r'href="\./(?P<p>[\w-]+)\.html(?P<t>[#?][^"]*)?"',
                  sub, html)


def strip_instrumentation(dom: str) -> str:
    """Remove Aethron's own measuring apparatus from a captured page.

    The capture rides into the page as an injected script, and it leaves
    its findings behind as DOM nodes — that is how the results get back
    out. A carry-mode port keeps every script it finds, so all of it
    shipped: the recorder itself (which scrolls the page in steps to take
    readings, on the user's machine, forever), its 487KB of timeline
    data, and a replay that then fought the platform's own runtime for
    control of the same elements. Cards stopped mid-travel and hover
    effects vanished because two systems were driving them.

    Nothing here belongs in the output. The instrument is not the result.
    """
    n = 0
    # the injected probe, by its marker
    dom, k = re.subn(r'(?is)<script\b[^>]*\bdata-aethron-probe\b[^>]*>'
                     r'.*?</script\s*>', "", dom)
    n += k
    # and the data nodes it wrote into the page
    dom, k = re.subn(r'(?is)<script\b[^>]*\bid="__ae_[a-z]+"[^>]*>'
                     r'.*?</script\s*>', "", dom)
    n += k
    if n:
        print(f"── stripped {n} instrumentation node(s) from the capture")
    return dom


def compress_entrance(ent: dict) -> dict:
    """Deduplicate the recording before it ships.

    A flattened spring easing is a sampled curve — `linear(0 0%, 0.024
    2.56%, …)` runs about 5KB — and a staggered entrance repeats that
    identical curve once per character. Written out naively the
    recording was 1.34MB on one page, 74% of the whole document, to say
    six different things 238 times. Shapes go in a table; each animation
    keeps only what actually distinguishes it: its element and its
    delay. 1342KB -> 35KB, with nothing rounded or dropped."""
    anims = ent.get("anims") or []
    if not anims:
        return {"shapes": [], "anims": []}
    shapes, index, out = [], {}, []
    for a in anims:
        shape = {k: a[k] for k in ("frames", "duration", "easing",
                                   "iterations", "direction")
                 if a.get(k) is not None}
        key = json.dumps(shape, sort_keys=True)
        if key not in index:
            index[key] = len(shapes)
            shapes.append(shape)
        out.append({"i": a["id"], "s": index[key], "d": a.get("delay") or 0,
                    **({"a": 1} if a.get("appear") else {})})
    return {"shapes": shapes, "anims": out, "meta": ent.get("meta") or {}}

MOTION_JS = r"""// Aethron motion — replayed from the original's OWN animation spec.
//
// The entrance definitions ship with the page (Framer writes them into
// __framer__appearAnimationsContent, keyed by data-framer-appear-id),
// so nothing here is guessed: exact delay, exact duration, exact
// easing curve, exact spring physics, chosen per breakpoint.
//
// Seven of ten transitions on a real template are SPRINGS. A spring is
// not an easing curve — it is a damped harmonic oscillator — so it is
// integrated properly below. Approximating it with `ease` is the
// difference between identical and merely similar.
(function () {
  // If the original engine came across, it owns every element carrying
  // a data-framer-appear-id — identical by construction. Ours then
  // handles only the remainder: recovered blur/transform states that
  // belong to no spec.
  var ENGINE = (typeof animator !== 'undefined');
  var specTag = document.getElementById('__ae_anim');
  var SPEC = specTag ? JSON.parse(specTag.textContent) : { anims: {}, breakpoints: [] };
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- which breakpoint variant applies right now -------------------
  var active = {};
  (SPEC.breakpoints || []).forEach(function (b) {
    if (b.mediaQuery && matchMedia(b.mediaQuery).matches) active[b.hash] = 1;
  });
  function variantFor(id) {
    var v = SPEC.anims[id];
    if (!v) return null;
    for (var hash in v) if (hash !== 'default' && active[hash]) return v[hash];
    return v['default'] || null;
  }

  // ---- the curves ---------------------------------------------------
  function bezier(p1x, p1y, p2x, p2y) {          // cubic-bezier(t)
    function A(a, b) { return 1 - 3 * b + 3 * a; }
    function B(a, b) { return 3 * b - 6 * a; }
    function C(a) { return 3 * a; }
    function calc(t, a, b) { return ((A(a, b) * t + B(a, b)) * t + C(a)) * t; }
    function slope(t, a, b) { return 3 * A(a, b) * t * t + 2 * B(a, b) * t + C(a); }
    return function (x) {
      if (p1x === p1y && p2x === p2y) return x;   // linear
      var t = x;
      for (var i = 0; i < 8; i++) {
        var sl = slope(t, p1x, p2x);
        if (!sl) break;
        t -= (calc(t, p1x, p2x) - x) / sl;
      }
      return calc(t, p1y, p2y);
    };
  }

  function spring(stiffness, damping, mass) {
    // Damped harmonic oscillator, solved analytically. Returns
    // progress 0..1 for a time in SECONDS, plus the point at which it
    // has settled — a spring has no duration of its own.
    var k = stiffness || 100, c = damping || 10, m = mass || 1;
    var w0 = Math.sqrt(k / m), z = c / (2 * Math.sqrt(k * m));
    var f;
    if (z < 1) {                                   // underdamped: overshoots
      var wd = w0 * Math.sqrt(1 - z * z);
      f = function (t) {
        return 1 - Math.exp(-z * w0 * t) *
          (Math.cos(wd * t) + (z * w0 / wd) * Math.sin(wd * t));
      };
    } else if (z === 1) {                          // critically damped
      f = function (t) { return 1 - Math.exp(-w0 * t) * (1 + w0 * t); };
    } else {                                       // overdamped
      var r1 = -w0 * (z - Math.sqrt(z * z - 1));
      var r2 = -w0 * (z + Math.sqrt(z * z - 1));
      f = function (t) {
        return 1 - (r1 * Math.exp(r2 * t) - r2 * Math.exp(r1 * t)) / (r1 - r2);
      };
    }
    var settle = 0.05;
    for (var t = 0.05; t < 12; t += 0.05) {
      if (Math.abs(1 - f(t)) < 0.001) { settle = t; break; }
      settle = t;
    }
    return { at: f, duration: settle };
  }

  // ---- turning spec numbers into styles ------------------------------
  var TRANSFORMS = ['x', 'y', 'scale', 'rotate', 'rotateX', 'rotateY',
                    'skewX', 'skewY'];
  function transformOf(v) {
    var out = '';
    if (v.x || v.y) out += 'translate(' + (v.x || 0) + 'px,' + (v.y || 0) + 'px) ';
    if (v.rotate) out += 'rotate(' + v.rotate + 'deg) ';
    if (v.rotateX) out += 'rotateX(' + v.rotateX + 'deg) ';
    if (v.rotateY) out += 'rotateY(' + v.rotateY + 'deg) ';
    if (v.skewX) out += 'skewX(' + v.skewX + 'deg) ';
    if (v.skewY) out += 'skewY(' + v.skewY + 'deg) ';
    if (v.scale !== undefined && v.scale !== 1) out += 'scale(' + v.scale + ') ';
    return out.trim();
  }
  function apply(el, from, to, p) {
    var v = {}, i;
    for (i = 0; i < TRANSFORMS.length; i++) {
      var key = TRANSFORMS[i];
      var a = from[key], b = to[key];
      if (a === undefined && b === undefined) continue;
      a = a === undefined ? (key === 'scale' ? 1 : 0) : a;
      b = b === undefined ? (key === 'scale' ? 1 : 0) : b;
      v[key] = a + (b - a) * p;
    }
    var t = transformOf(v);
    el.style.transform = t || '';
    if (from.opacity !== undefined || to.opacity !== undefined) {
      var oa = from.opacity === undefined ? 1 : from.opacity;
      var ob = to.opacity === undefined ? 1 : to.opacity;
      el.style.opacity = oa + (ob - oa) * p;
    }
  }

  function play(el, variant) {
    var from = variant.initial || {}, to = variant.animate || {};
    var tr = to.transition || {};
    var curve, dur;
    if (tr.type === 'spring') {
      var sp = spring(tr.stiffness, tr.damping, tr.mass);
      curve = sp.at; dur = sp.duration;
    } else {
      var e = tr.ease || [0.44, 0, 0.56, 1];
      var b = bezier(e[0], e[1], e[2], e[3]);
      dur = tr.duration === undefined ? 0.4 : tr.duration;
      curve = function (t) { return b(dur ? Math.min(1, t / dur) : 1); };
    }
    var delay = (tr.delay || 0) * 1000;
    apply(el, from, to, 0);
    setTimeout(function () {
      var t0 = performance.now();
      (function frame(now) {
        var t = (now - t0) / 1000;
        var p = Math.min(1, curve(t));
        apply(el, from, to, tr.type === 'spring' ? curve(t) : p);
        if (t < dur) requestAnimationFrame(frame);
        else { apply(el, from, to, 1); el.style.transform = ''; el.style.opacity = ''; }
      })(performance.now());
    }, delay);
  }

  // ---- wire it up ----------------------------------------------------
  var byId = [].slice.call(document.querySelectorAll('[data-framer-appear-id]'));
  var generic = [].slice.call(document.querySelectorAll('[data-ae]'));

  if (reduce) { return; }

  // ---- recorded traces: replay what the original ACTUALLY did -------
  // Each entry is one element's own measured keyframes. A per-character
  // heading therefore staggers itself: every character carries its own
  // timing, so nothing has to know what a "stagger" is.
  var tlTag = document.getElementById('__ae_timeline');
  var TL = tlTag ? JSON.parse(tlTag.textContent) : { entries: {} };

  function applyFrame(el, s) {
    el.style.opacity = (s.opacity === undefined) ? '' : s.opacity;
    el.style.transform = s.transform || '';
    el.style.filter = s.filter || '';
  }
  function playTrace(el, frames) {
    var t0 = performance.now(), i = 0;
    (function step() {
      var now = performance.now() - t0;
      while (i < frames.length && frames[i][0] <= now) {
        applyFrame(el, frames[i][1]); i++;
      }
      if (i < frames.length) requestAnimationFrame(step);
      else { el.style.opacity = ''; el.style.transform = ''; el.style.filter = ''; }
    })();
  }

  // ---- recorded animations: the browser's OWN objects ---------------
  // Not a reconstruction and not an approximation. getAnimations() hands
  // back the exact keyframes and timing the original was given, and
  // el.animate() hands them straight back to the browser — so a spring
  // the platform had already flattened into a linear() easing replays as
  // that same easing, and nothing here has to know what a spring is.
  //
  // These are only visible if you look EARLY. One measurement of the
  // hero page saw 1 animation at t=0, 19 at t=60ms and 7 by t=140ms:
  // entrances finish and are collected, which is why the port used to
  // ship those characters settled and inert.
  // ---- continuous motion: rotations and loops that never stop -------
  // These are driven by requestAnimationFrame writing inline style, so
  // there is no animation object to read and no entrance to recover —
  // a spinning badge simply shipped frozen. They were measured in REAL
  // time (a virtual clock reads them 120x slow) and are addressed by
  // their framer-* classes, because that measurement is a different
  // page load from the one this DOM came from.
  var contTag = document.getElementById('__ae_continuous');
  var CONT = contTag ? JSON.parse(contTag.textContent)
                     : { rotate: [], loop: [] };
  function pick(item) {
    if (!item.sel) return null;
    var all;
    try { all = document.querySelectorAll(item.sel); } catch (e) { return null; }
    return all[item.idx || 0] || null;
  }
  (CONT.rotate || []).forEach(function (r) {
    var el = pick(r);
    if (!el) return;
    var a = r.from, b = r.from + (r.clockwise ? 360 : -360);
    try {
      el.animate([{ transform: 'rotate(' + a + 'deg)' },
                  { transform: 'rotate(' + b + 'deg)' }],
                 { duration: r.duration, iterations: Infinity,
                   easing: 'linear' });
      el.dataset.aeDone = '1';
    } catch (e) { }
  });
  (CONT.loop || []).forEach(function (l) {
    var el = pick(l);
    if (!el || !l.frames || l.frames.length < 2) return;
    try {
      el.animate(l.frames, { duration: l.duration, iterations: Infinity,
                             easing: 'linear' });
      el.dataset.aeDone = '1';
    } catch (e) { }
  });

  // ---- scroll-linked tracks and counters ----------------------------
  // Style as a function of the element's own progress through the
  // viewport. The cards that shrink as you scroll (1424x794 -> 462x309)
  // animate WIDTH AND HEIGHT and touch neither transform nor opacity,
  // so every earlier recorder called them static and the port snapped
  // them to the end size.
  var scrTag = document.getElementById('__ae_scroll');
  var SCR = scrTag ? JSON.parse(scrTag.textContent)
                   : { tracks: [], counters: [] };

  // One interpolator for every property. Rather than a case per type —
  // matrix, rgb, blur, px, WxH — split each value into its numbers and
  // the text between them: if two values have the same shape, the
  // numbers can be walked. That covers the properties this page
  // animates and the ones it does not yet.
  var NUM = /-?\d*\.?\d+(?:e[-+]?\d+)?/gi;
  function shapeOf(s) { return String(s).replace(NUM, ' '); }
  function lerpStr(a, b, f) {
    if (a === b) return a;
    if (shapeOf(a) !== shapeOf(b)) return f < 0.5 ? a : b;
    var na = String(a).match(NUM) || [], nb = String(b).match(NUM) || [];
    if (na.length !== nb.length) return f < 0.5 ? a : b;
    var i = 0;
    return String(a).replace(NUM, function () {
      var x = parseFloat(na[i]), y = parseFloat(nb[i]);
      i++;
      return String(Math.round((x + (y - x) * f) * 1000) / 1000);
    });
  }

  var scrollTracks = [], oneShots = [], claimedEls = [];
  var SCALE = 1;
  try {
    var recH = SCR.height || 0;
    var nowH = Math.max(document.body.scrollHeight,
                        document.documentElement.scrollHeight);
    if (recH > 0 && nowH > 0) SCALE = recH / nowH;
  } catch (e) { }
  // ONE ELEMENT, ONE DRIVER. Two selectors can resolve to the same node
  // (".framer-x[0]" and ".framer-x div[1]" are different strings and the
  // same element), and two curves on one box fight: measured, a card
  // snapped back to its natural 1424x794 mid-scroll because a second
  // driver overwrote the first. First claim wins.
  function claim(el) {
    for (var i = 0; i < claimedEls.length; i++)
      if (claimedEls[i] === el) return false;
    claimedEls.push(el);
    return true;
  }
  (SCR.tracks || []).forEach(function (t) {
    var el = pick(t);
    if (!el || !t.rows || t.rows.length < 2) return;
    if (!claim(el)) return;
    var r = el.getBoundingClientRect();
    var sy0 = window.scrollY || window.pageYOffset || 0;
    // Progress must be a PURE FUNCTION OF SCROLL. Computing it from the
    // element's live rect fed back into itself: this replay changes the
    // box, which moves the rect, which changes the progress that
    // decided the box. Freezing the height was not enough because top
    // still moves as the layout reflows — measured, a card oscillated
    // 406 -> 462 -> 1424 -> 462 -> 1424 going DOWN the page, which is
    // what stacked three cards on screen at once. Anchor to the
    // document position captured before any replay, and the curve is
    // monotonic like the original's.
    scrollTracks.push({ el: el, props: t.props, rows: t.rows,
                        h0: r.height || 1, docTop: r.top + sy0 });
  });

  function applyScroll() {
    for (var i = 0; i < scrollTracks.length; i++) {
      var t = scrollTracks[i], el = t.el;
      if (!el.isConnected) continue;
      var sy = window.scrollY || window.pageYOffset || 0;
      var p = (sy + innerHeight - t.docTop) / (innerHeight + t.h0);
      p = p < 0 ? 0 : (p > 1 ? 1 : p);
      var rows = t.rows, a = rows[0], b = rows[rows.length - 1];
      for (var j = 0; j < rows.length - 1; j++) {
        if (p >= rows[j][0] && p <= rows[j + 1][0]) {
          a = rows[j]; b = rows[j + 1]; break;
        }
      }
      var span = (b[0] - a[0]) || 1;
      var f = (p - a[0]) / span;
      f = f < 0 ? 0 : (f > 1 ? 1 : f);
      for (var k = 0; k < t.props.length; k++) {
        var prop = t.props[k], v = lerpStr(a[1 + k], b[1 + k], f);
        if (!v) continue;
        if (prop === 'box') {
          var wh = String(v).split('x');
          if (wh.length === 2) {
            el.style.width = wh[0] + 'px';
            el.style.height = wh[1] + 'px';
          }
        } else {
          try { el.style[prop] = v; } catch (e) { }
        }
      }
    }
  }
  var scrBusy = false, osBusy = false;
  if (scrollTracks.length) {
    addEventListener('scroll', function () {
      if (scrBusy) return;
      scrBusy = true;
      requestAnimationFrame(function () {
        scrBusy = false; applyScroll(); checkOneShots();
      });
    }, { passive: true });
    applyScroll();
  }

  // ---- one-shots: play once, at the position the ORIGINAL fires -----
  // These are NOT scroll-linked. Dragging them by scroll position is
  // what made every animation feel scroll-driven, and made them start
  // the instant the element peeked into view instead of where the
  // original starts them. Each carries the progress it fired at and the
  // frames and duration measured by dwelling at that stop.
  (SCR.oneshots || []).forEach(function (o) {
    var el = pick(o);
    if (!el) return;
    if (!claim(el)) return;
    var trig = (typeof o.trigger === 'number') ? o.trigger : 0.25;
    var fired = false;

    function park() {
      if (!o.frames || !o.frames.length) return;
      applyRow(el, o.props, o.frames[0], 1);
    }
    function play() {
      if (fired) return;
      fired = true;
      // The original element may be REMOVED when its one-shot finishes.
      // Its recording therefore ends mid-flight, and holding that pose
      // leaves a half-animated element sitting on the page — a loading
      // mask frozen across the viewport, in the case that produced this.
      // Play what was recorded, then let it go, exactly as the original
      // does.
      function retire() {
        if (!el || !el.style) return;
        if (o.gone) {
          el.style.setProperty('display', 'none', 'important');
          return;
        }
        // DID THE REPLAY ACTUALLY GET THERE?
        //
        // A recorded end state is not always reachable element by
        // element. fiber's loading panel is a flex item with
        // `flex: 1 0 0px`, so its height belongs to its container: the
        // original collapses it 813px -> 2px by changing an ancestor,
        // and setting height on the panel — even !important — computes
        // right back to 813px. Left there, the panel freezes half-way
        // and lies across the page, which is worse than not animating
        // at all. So check the result, and if the end state was not
        // reached, take the element out of the way as the original does.
        var want = null, props = o.props || [];
        for (var i = 0; i < props.length; i++) {
          if (props[i] === 'box') { want = o.frames[o.frames.length - 1][1 + i]; }
        }
        if (!want) return;
        var wh = String(want).split('x');
        if (wh.length !== 2) return;
        var got = el.getBoundingClientRect().height;
        var target = parseFloat(wh[1]);
        if (Math.abs(got - target) > Math.max(8, target * 0.5)) {
          el.style.setProperty('display', 'none', 'important');
        }
      }
      if (o.frames && o.frames.length > 1 && o.duration) {
        var t0 = performance.now();
        (function step() {
          var dt = performance.now() - t0;
          var a = o.frames[0], b = o.frames[o.frames.length - 1], i;
          for (i = 0; i < o.frames.length - 1; i++) {
            if (o.frames[i][0] <= dt && dt <= o.frames[i + 1][0]) {
              a = o.frames[i]; b = o.frames[i + 1]; break;
            }
          }
          var span = (b[0] - a[0]) || 1;
          var f = (dt - a[0]) / span;
          f = f < 0 ? 0 : (f > 1 ? 1 : f);
          applyRow(el, o.props, a, 1, b, f);
          if (dt < o.duration) requestAnimationFrame(step);
          else {
            applyRow(el, o.props, o.frames[o.frames.length - 1], 1);
            retire();
          }
        })();
        return;
      } else if (o.end) {
        // no measured timing: land on the end state rather than
        // inventing a speed, and say so in the spec
        applyRow(el, o.props, [0].concat(o.end), 1);
      }
    }
    // Park ONLY what the reader cannot see yet. A parked element whose
    // trigger never fires sits frozen at its opening frame, which is
    // worse than no animation at all — the element is simply wrong on
    // screen. Anything already in view plays now instead; anything
    // below the fold parks and waits, where being parked is invisible.
    var r0 = el.getBoundingClientRect();
    if (r0.top < innerHeight && r0.bottom > 0) {
      // fire when the ORIGINAL fired, not at load
      setTimeout(play, typeof o.delay === "number" ? o.delay : 60);
    } else {
      park();
      var rr = el.getBoundingClientRect();
      var sy1 = window.scrollY || window.pageYOffset || 0;
      oneShots.push({ el: el, trig: trig, play: play,
                      h0: rr.height || 1, docTop: rr.top + sy1,
                      fired: function () { return fired; } });
    }
  });

  function applyRow(el, props, a, _u, b, f) {
    for (var k = 0; k < props.length; k++) {
      var v = b ? lerpStr(a[1 + k], b[1 + k], f) : a[1 + k];
      if (!v) continue;
      if (props[k] === 'box') {
        var wh = String(v).split('x');
        if (wh.length === 2) {
          // !important, because the template's own stylesheet carries it.
          // Measured: the inline height WAS set to 2px and the computed
          // height stayed 813px, so the loading panel never collapsed and
          // sat across the page looking like a broken layout.
          el.style.setProperty('width', wh[0] + 'px', 'important');
          el.style.setProperty('height', wh[1] + 'px', 'important');
        }
      } else {
        try { el.style.setProperty(props[k], v, 'important'); }
        catch (e) { try { el.style[props[k]] = v; } catch (e2) { } }
      }
    }
  }

  function checkOneShots() {
    for (var i = 0; i < oneShots.length; i++) {
      var o = oneShots[i];
      if (o.fired() || !o.el.isConnected) continue;
      // Never drive an element the entrance spec owns.
      var aeid = o.el.getAttribute && o.el.getAttribute('data-ae-id');
      if (aeid && recorded && recorded[aeid]) continue;
      var sy2 = window.scrollY || window.pageYOffset || 0;
      var p = (sy2 + innerHeight - o.docTop) / (innerHeight + o.h0);
      if (p >= o.trig) o.play();
    }
  }

  if (oneShots.length) {
    addEventListener('scroll', function () {
      if (osBusy) return;
      osBusy = true;
      requestAnimationFrame(function () { osBusy = false; checkOneShots(); });
    }, { passive: true });
    setTimeout(checkOneShots, 160);
  }

  function setCounter(el, c, val) {
    // Write into the element's OWN text node when the number sits
    // beside a child element — a unit span, for instance. Assigning
    // textContent here would delete that child, so the recorder marks
    // these and the number goes back exactly where it came from.
    if (!c.own) { el.textContent = val; return; }
    for (var i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 3) {
        el.childNodes[i].nodeValue = val;
        return;
      }
    }
    el.insertBefore(document.createTextNode(val), el.firstChild);
  }

  // Counters: they fire ONCE when first seen, each on its own timing.
  (SCR.counters || []).forEach(function (c) {
    var el = pick(c);
    if (!el || !c.frames || c.frames.length < 2) return;
    var fired = false;
    function play() {
      if (fired) return;
      fired = true;
      c.frames.forEach(function (fr) {
        setTimeout(function () { setCounter(el, c, fr[1]); }, fr[0]);
      });
    }
    var io2 = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { play(); io2.disconnect(); } });
    }, { threshold: 0.05 });
    io2.observe(el);
    // in view at load, or an observer that never fires
    var rr = el.getBoundingClientRect();
    if (rr.top < innerHeight && rr.bottom > 0) setTimeout(play, 120);
  });

  var recTag = document.getElementById('__ae_entrance');
  // ---- wheel-driven motion -------------------------------------------
  // Some components animate from the viewer's wheel rather than from
  // time or scroll position: fiber's hero accumulates deltaY into one
  // number, springs it, and derives every layer's depth, scale, opacity
  // and blur from it. There is no timeline to replay, so what ships is
  // the sampled FUNCTION and the port re-derives the motion from the
  // viewer's own input — the same way the original does.
  var whTag = document.getElementById('__ae_wheel');
  var WH = whTag ? JSON.parse(whTag.textContent) : null;
  if (WH && WH.elements && WH.elements.length) {
    var wc = WH.container ? document.querySelector(WH.container) : null;
    if (!wc && WH.via) {
      // the component's own container usually carries no class; it is
      // identified as the parent of a child that does
      var probe = document.querySelector(WH.via);
      if (probe) { wc = probe.parentElement; }
    }
    var wels = [];
    for (var wi = 0; wi < WH.elements.length; wi++) {
      var we = WH.elements[wi];
      var wlist = document.querySelectorAll(we.sel);
      if (wlist[we.idx]) {
        wels.push({ el: wlist[we.idx], st: we.states,
                    jumps: we.jumps || [] });
      }
    }
    if (wc && wels.length) {
      var wstops = WH.stops, wcycle = WH.cycle || 0;
      var wlast = wstops[wstops.length - 1];
      var wtarget = 0, wcur = 0, wraf = 0;

      function wapply(v) {
        var x = v;
        if (wcycle > 0) { x = ((v % wcycle) + wcycle) % wcycle; }
        else if (x < wstops[0]) { x = wstops[0]; }
        else if (x > wlast) { x = wlast; }
        var i = 0;
        while (i < wstops.length - 2 && wstops[i + 1] <= x) { i++; }
        var a = wstops[i], b = wstops[i + 1];
        var f0 = b > a ? (x - a) / (b - a) : 0;
        for (var k = 0; k < wels.length; k++) {
          var s1 = wels[k].st[i], s2 = wels[k].st[i + 1];
          if (!s1 || !s2) { continue; }
          var f = f0;
          // Do not interpolate ACROSS a wrap. An infinite scroller sends
          // its front layer back to the rear in one frame; blending that
          // interval produces a layer sliding backwards through the
          // whole scene, which is the one thing the original never does.
          if (wels[k].jumps && wels[k].jumps.indexOf(i) !== -1) {
            f = f0 < 0.5 ? 0 : 1;
          }
          var style = wels[k].el.style;
          if (s1[0] || s2[0]) {
            style.setProperty('transform', lerpStr(s1[0], s2[0], f),
                              'important');
          }
          style.setProperty('opacity', lerpStr(s1[1], s2[1], f), 'important');
          if (s1[2] || s2[2]) {
            style.setProperty('filter', lerpStr(s1[2], s2[2], f), 'important');
          }
          var z1 = parseFloat(s1[3]), z2 = parseFloat(s2[3]);
          if (!isNaN(z1) && !isNaN(z2)) {
            style.setProperty('z-index',
                              String(Math.round(z1 + (z2 - z1) * f)),
                              'important');
          }
        }
      }
      function wtick() {
        // The original springs its input (stiffness 100, damping 30), so
        // the motion glides on after the wheel stops. An instant jump to
        // the target would read as a different component.
        wcur += (wtarget - wcur) * 0.11;
        wapply(wcur);
        if (Math.abs(wtarget - wcur) > 0.4) {
          wraf = requestAnimationFrame(wtick);
        } else { wcur = wtarget; wapply(wcur); wraf = 0; }
      }
      wc.addEventListener('wheel', function (e) {
        wtarget += e.deltaY;
        e.preventDefault();
        if (!wraf) { wraf = requestAnimationFrame(wtick); }
      }, { passive: false });
      wapply(0);
    }
  }

  var REC = recTag ? JSON.parse(recTag.textContent) : { anims: [] };
  var recorded = {}, recPending = [];

  function timingOf(a) {
    return {
      duration: a.duration || 0, delay: a.delay || 0,
      easing: a.easing || 'linear',
      // backwards, never forwards: during the delay the element holds
      // the recorded start pose, and when the animation ends it releases
      // to the settled style the page already carries.
      fill: 'backwards',
      iterations: a.iterations === 'infinite' ? Infinity : (a.iterations || 1),
      direction: a.direction || 'normal'
    };
  }
  function parkRecorded(entry) {
    // Remember exactly what the page carried, because releasing must
    // RESTORE it rather than clear it. transform is the trap: an
    // element centred with translate(-50%,-50%) that also animates
    // would be shoved out of place by a blanket reset, and the layout
    // moves while every content check still reads 100%.
    entry.was = { opacity: entry.el.style.opacity,
                  transform: entry.el.style.transform,
                  filter: entry.el.style.filter };
    entry.list.forEach(function (a) {
      var f = a.frames[0] || {};
      if (f.opacity !== undefined) entry.el.style.opacity = f.opacity;
      if (f.transform !== undefined) entry.el.style.transform = f.transform;
      if (f.filter !== undefined) entry.el.style.filter = f.filter;
    });
  }
  function restoreRecorded(entry) {
    var w = entry.was || { opacity: '', transform: '', filter: '' };
    entry.el.style.opacity = w.opacity;
    entry.el.style.transform = w.transform;
    entry.el.style.filter = w.filter;
  }
  function playRecorded(entry) {
    // Put the carried values back first, or the animation would finish
    // and revert straight into the parked pose it just came out of.
    restoreRecorded(entry);
    entry.list.forEach(function (a) {
      try { entry.el.animate(a.frames, timingOf(a)); } catch (e) { }
    });
    entry.el.dataset.aeDone = '1';
  }

  // The recording ships as a shape table plus one row per element; a
  // staggered entrance is six curves, not two hundred copies of six.
  function expand(r) {
    var s = (REC.shapes || [])[r.s] || {};
    return { id: r.i, delay: r.d || 0, appear: !!r.a, frames: s.frames || [],
             duration: s.duration || 0, easing: s.easing,
             iterations: s.iterations, direction: s.direction };
  }
  (REC.anims || []).map(expand).forEach(function (a) {
    var el = document.querySelector('[data-ae-id="' + a.id + '"]');
    if (!el || !a.frames.length) return;
    // When the original engine came across it owns its own elements;
    // two systems animating one element fight over the same style.
    if (ENGINE && a.appear) return;
    if (a.iterations === 'infinite') {
      // marquees do not wait for anything and never end
      try { el.animate(a.frames, timingOf(a)); } catch (e) { }
      el.dataset.aeDone = '1';
      return;
    }
    (recorded[a.id] = recorded[a.id] || { el: el, list: [] }).list.push(a);
  });
  Object.keys(recorded).forEach(function (id) {
    parkRecorded(recorded[id]);
    recPending.push(recorded[id]);
  });

  var traced = [];
  Object.keys(TL.entries || {}).forEach(function (id) {
    var el = document.querySelector('[data-ae-id="' + id + '"]');
    if (!el) return;
    if (recorded[id]) return;      // a measured animation beats a sampled one
    var frames = TL.entries[id].frames || [];
    if (frames.length < 2) return;
    applyFrame(el, frames[0][1]);              // park at the recorded start
    traced.push({ el: el, frames: frames });
  });

  // exact-spec elements
  var pending = [];
  (ENGINE ? [] : byId).forEach(function (el) {
    var v = variantFor(el.getAttribute('data-framer-appear-id'));
    if (!v) return;
    apply(el, v.initial || {}, v.animate || {}, 0);
    pending.push({ el: el, v: v });
  });

  // everything else keeps the recovered start state (blur, etc.)
  generic = generic.filter(function (el) {
    // an element whose real animation was recorded is driven by that
    return !recorded[el.getAttribute('data-ae-id')];
  });
  generic.forEach(function (el) {
    if (ENGINE && el.hasAttribute('data-framer-appear-id')) return;
    if (!ENGINE && el.hasAttribute('data-framer-appear-id')) return;
    el.setAttribute('style', (el.getAttribute('style') || '') + ';' + el.dataset.ae);
  });

  function releaseGeneric(el) {
    el.style.transition = 'opacity .6s ease, transform .6s cubic-bezier(.44,0,.56,1), filter .6s ease';
    el.style.opacity = ''; el.style.transform = ''; el.style.filter = '';
    el.dataset.aeDone = '1';
  }

  // Wiring must not depend on requestAnimationFrame alone. Measured
  // three times in a row, two runs played the full staggered entrance
  // and the third played nothing — the double rAF simply never fired,
  // leaving every parked element invisible. A throttled frame callback
  // is normal in a background tab or on a low-power device, so the
  // timer is not a test crutch: without it the content guarantee is the
  // only thing standing between a user and a blank hero.
  var wired = false;
  function wire() {
    if (wired) return;
    wired = true;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var hit = null, tr = null, rec = null, i;
        rec = recorded[e.target.getAttribute('data-ae-id')] || null;
        for (i = 0; i < pending.length; i++)
          if (pending[i].el === e.target) { hit = pending[i]; break; }
        for (i = 0; i < traced.length; i++)
          if (traced[i].el === e.target) { tr = traced[i]; break; }
        // measured first: it is the original's own animation object
        if (rec) playRecorded(rec);
        else if (hit) { play(hit.el, hit.v); hit.el.dataset.aeDone = '1'; }
        else if (tr) { playTrace(tr.el, tr.frames); tr.el.dataset.aeDone = '1'; }
        else releaseGeneric(e.target);
        io.unobserve(e.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.01 });
    // Anything already on screen plays NOW. The original does not wait
    // for an observer to notice its own hero, and the observer is the
    // part that proved unreliable: the same page measured twice gave a
    // full staggered entrance once and nothing at all the next time,
    // purely on whether the callback had fired yet. Only what is still
    // below the fold has any reason to wait.
    recPending = recPending.filter(function (x) {
      if (x.el.dataset.aeDone) return false;
      var r = x.el.getBoundingClientRect();
      if (r.top < innerHeight && r.bottom > -1) { playRecorded(x); return false; }
      return true;
    });
    recPending.forEach(function (x) { if (!x.el.dataset.aeDone) io.observe(x.el); });
    pending.forEach(function (x) { io.observe(x.el); });
    traced.forEach(function (x) { if (!x.el.dataset.aeDone) io.observe(x.el); });
    generic.forEach(function (el) { if (!el.dataset.aeDone) io.observe(el); });

    // THE CONTENT GUARANTEE. Motion is second; content is first.
    // Whatever parks an element — our runtime, a recording, or the
    // carried engine parking it and never firing — nothing may stay
    // invisible. A missing animation is a defect; missing content is a
    // broken site, and 33 elements sat at opacity 0.001 before this.
    // Only rescue what is ON SCREEN and still hidden. An element parked
    // below the fold is not broken, it is waiting its turn — and a
    // blanket sweep at 2.6s used to force every one of them visible,
    // which quietly destroyed every below-the-fold entrance in the
    // name of protecting content. In view and invisible is a defect;
    // out of view and invisible is the animation working.
    function guarantee() {
      var all = document.querySelectorAll(
        '[data-ae-id],[data-ae],[data-framer-appear-id]');
      [].forEach.call(all, function (el) {
        if (el.dataset.aeDone === 'released') return;
        // Mid-flight is not stuck. An element two thirds through a
        // staggered entrance is legitimately near zero opacity, and
        // clearing it here would destroy the animation this function
        // exists to protect.
        try { if ((el.getAnimations() || []).length) return; } catch (e) { }
        var r = el.getBoundingClientRect();
        var onScreen = r.top < innerHeight && r.bottom > 0;
        if (!onScreen && (r.width || r.height)) return;
        var cs = getComputedStyle(el);
        var blur = /blur\(([\d.]+)px\)/.exec(cs.filter || '');
        if (parseFloat(cs.opacity) >= 0.05 &&
            !(blur && parseFloat(blur[1]) > 0.5)) return;
        el.style.transition = 'opacity .4s ease, filter .4s ease';
        var known = recorded[el.getAttribute('data-ae-id')];
        if (known) {
          restoreRecorded(known);       // put back what the page carried
        } else {
          el.style.opacity = '';
          el.style.filter = '';
          el.style.transform = '';
        }
        el.dataset.aeDone = 'released';
      });
    }
    // a few passes, then whenever the reader moves
    [2600, 4200, 6000].forEach(function (ms) { setTimeout(guarantee, ms); });
    addEventListener('scroll', function () {
      clearTimeout(window.__aeGuard);
      window.__aeGuard = setTimeout(guarantee, 700);
    }, { passive: true });

    // zero-area boxes never trigger an observer
    setTimeout(function () {
      generic.concat(pending.map(function (x) { return x.el; })).forEach(function (el) {
        var r = el.getBoundingClientRect();
        if ((!r.width || !r.height) && !el.dataset.aeDone) {
          el.style.opacity = ''; el.style.transform = ''; el.style.filter = '';
          el.dataset.aeDone = '1'; io.unobserve(el);
        }
      });
    }, 900);

    // clipped elements never intersect either — sweep by position
    var busy = false;
    function sweep() {
      busy = false;
      var left = 0;
      recPending.forEach(function (x) {
        if (x.el.dataset.aeDone) return;
        if (x.el.getBoundingClientRect().top < innerHeight * 0.92) {
          playRecorded(x); io.unobserve(x.el);
        } else left++;
      });
      pending.forEach(function (x) {
        if (x.el.dataset.aeDone) return;
        if (x.el.getBoundingClientRect().top < innerHeight * 0.92) {
          play(x.el, x.v); x.el.dataset.aeDone = '1'; io.unobserve(x.el);
        } else left++;
      });
      traced.forEach(function (x) {
        if (x.el.dataset.aeDone) return;
        if (x.el.getBoundingClientRect().top < innerHeight * 0.92) {
          playTrace(x.el, x.frames); x.el.dataset.aeDone = '1'; io.unobserve(x.el);
        } else left++;
      });
      generic.forEach(function (el) {
        if (el.dataset.aeDone) return;
        if (el.getBoundingClientRect().top < innerHeight * 0.92) {
          releaseGeneric(el); io.unobserve(el);
        } else left++;
      });
      if (!left) removeEventListener('scroll', onScroll);
    }
    function onScroll() { if (!busy) { busy = true; requestAnimationFrame(sweep); } }
    addEventListener('scroll', onScroll, { passive: true });

    // Scroll tracks and counters wire themselves at parse time, above,
    // so they do not depend on this block running at all. Resize does
    // matter here: the recorded curve is keyed on each element's
    // progress through the viewport, and a resize moves that for every
    // element at once.
    if (scrollTracks.length)
      addEventListener('resize', applyScroll, { passive: true });
  }
  requestAnimationFrame(function () { requestAnimationFrame(wire); });
  setTimeout(wire, 150);
})();
"""
# An animation's PARKED START, never the design.
#
# THE RULE THAT MATTERS: a transform alone is NOT an entrance. Framer
# uses `translate(-50%,-50%)` to centre things and `scale()` for design
# effects — strip those and the layout moves, which is the one thing
# this converter exists to prevent. An entrance is only recognised when
# the element is also HIDDEN (opacity 0), because that is what an
# element waiting to animate in looks like. Caught by counting: a first
# cut reported 197 "entrances" on a page that has 49 hidden elements.
OPACITY = re.compile(r"(?i)(?:^|;)\s*opacity\s*:\s*([0-9]*\.?[0-9]+)\s*(?=;|$)")
HIDDEN_MAX = 0.05        # above this it is a design choice, not a parked state


def is_hidden(style: str) -> bool:
    """Parked mid-entrance, or deliberately faded?

    Framer parks elements at opacity 0 AND at 0.001 — and it also uses
    0.06 and 0.18 as real design values. A regex that pattern-matches
    digits cannot tell 0.001 from 0.18 (both are "0.something"), so the
    value is parsed and compared. Forcing a deliberately faded element
    to full opacity is exactly the kind of silent wrongness a content
    score would never catch."""
    m = OPACITY.search(style)
    try:
        return bool(m) and float(m.group(1)) <= HIDDEN_MAX
    except ValueError:
        return False
# `filter: blur()` is the THIRD leg of a Framer entrance — a word fades
# in, slides up AND unblurs. Recovering only opacity and transform left
# 94 per-word spans at full opacity and still blurred, which is exactly
# what the owner saw in the hero heading.
#
# The lookbehind matters more than it looks: `backdrop-filter: blur()`
# CONTAINS the substring "filter: blur(" and is frosted glass on the
# sticky header — 18 elements of pure design. Stripping it would be the
# translate(-50%) mistake all over again.
START_STATE = re.compile(
    r"(?i)(?:^|;)\s*((?<![-\w])opacity\s*:\s*0(?:\.\d+)?|"
    r"(?<![-\w])transform\s*:\s*[^;]*(?:translate|scale|rotate|perspective)[^;]*|"
    r"(?<![-\w])filter\s*:\s*[^;]*blur\([^;]*)\s*(?=;|$)")


def strip_scripts(dom: str) -> tuple:
    """Remove every script. The template's runtime does not come with
    us — that is the entire promise of this converter."""
    n = len(re.findall(r"(?i)<script\b", dom))
    dom = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", dom)
    dom = re.sub(r"(?is)<script\b[^>]*/?>", "", dom)
    return dom, n


def recover_entrances(dom: str) -> tuple:
    """Move parked start states out of the inline style and onto
    data-ae, so the element renders at its FINAL appearance and the
    entrance can be replayed by our own observer."""
    count = [0]

    def fix(m):
        tag, style = m.group(0), m.group(1)
        # only a hidden element is mid-entrance; everything else keeps
        # its inline style exactly as the template wrote it
        if "data-framer-appear-id" in tag:
            # The carried engine owns this element and writes its own
            # start state from the spec. Touching it makes two systems
            # fight over the same style attribute.
            return tag
        if not is_hidden(style):
            return tag
        starts = [s.strip() for s in START_STATE.findall(style)]
        if not starts:
            return tag
        rest = START_STATE.sub("", style).strip(" ;")
        count[0] += 1
        keep = f' style="{rest}"' if rest else ""
        tag = re.sub(r'\s*style="[^"]*"', keep, tag, count=1)
        # the attribute belongs INSIDE the tag; appending after the
        # closing bracket turns it into page text
        attr = ' data-ae="' + ";".join(starts).replace('"', "&quot;") + '"'
        close = "/>" if tag.rstrip().endswith("/>") else ">"
        return tag.rstrip()[:-len(close)].rstrip() + attr + close

    dom = re.sub(r'(?i)<[a-z][^>]*\sstyle="([^"]*)"[^>]*>', fix, dom)
    return dom, count[0]


# Links that point back at the platform the template came from. The
# rendered DOM carries them (editor mode only hides them with CSS), and
# a site that is "completely yours" cannot ship a marketplace link to
# the shop it was bought from.
PLATFORM_LINKS = re.compile(
    r"(?i)https?://(?:www\.)?(?:framer\.com|framer\.website|webflow\.com|"
    r"webflow\.io|buy\.polar\.sh|[a-z0-9-]+\.lemonsqueezy\.com|"
    r"gumroad\.com)[^\"\']*")

# Hints that phone home. A preconnect is harmless to render and still
# tells the browser to open a socket to the vendor.
#
# Attribute ORDER is not guaranteed: this tag ships as
# `<link href="…" rel="preconnect">`, so a pattern expecting rel first
# silently matched nothing and left the last leak in place.
HINT_HOSTS = ("website-files", "framerusercontent", "framer.com",
              "cloudfront.net", "webflow")


def strip_hints(dom: str) -> tuple:
    n = 0

    def drop(m):
        nonlocal n
        tag = m.group(0)
        if re.search(r'(?i)rel="(?:preconnect|dns-prefetch)"', tag) and \
                any(h in tag.lower() for h in HINT_HOSTS):
            n += 1
            return ""
        return tag

    return re.sub(r"(?is)<link\b[^>]*>", drop, dom), n

# THE BADGE. Editor mode can only hide it with CSS because the platform
# runtime re-creates the element. A converted port has no runtime, so it
# can simply be deleted — element, images, CDN requests and all.
BADGES = (
    r'(?is)<a\b[^>]*class="[^"]*w-webflow-badge[^"]*".*?</a\s*>',
    r'(?is)<div\b[^>]*id="__framer-badge-container".*?</div\s*>',
    r'(?is)<a\b[^>]*href="[^"]*framer\.com/r/badge[^"]*".*?</a\s*>',
)


def cut_element(dom: str, start: int) -> str:
    """Remove the element beginning at `start`, honouring nesting.

    A non-greedy regex cannot do this: `<div id="badge"><div/></div>`
    matches to the FIRST </div> and leaves a stray closing tag, which
    unbalances the document. Nothing about the CONTENT changes, so every
    text and image metric still reads 100% — while the section splitter
    silently collapses to a single blob."""
    m = TAG.match(dom, start)
    if not m:
        return dom
    if m.group(2).lower() in VOID or m.group(4):
        return dom[:start] + dom[m.end():]
    name = m.group(2).lower()
    if name == "a":
        # Anchors cannot nest — the spec forbids it and no generator
        # emits it — so the first </a> IS the close. Depth-counting an
        # anchor over a regex tag stream walked straight past it and
        # swallowed the carousel that followed: 20,144 bytes and 18
        # images deleted to remove a promo whose entire text was
        # "NEW TEMPLATES".
        close = re.search(r"(?is)</a\s*>", dom[start:])
        return dom[:start] + dom[start + close.end():] if close else dom
    depth = 0
    for t in TAG.finditer(dom, start):
        closing, tag, _, selfclose = t.groups()
        if tag.lower() in VOID or selfclose:
            continue
        depth += -1 if closing else 1
        if depth == 0:
            return dom[:start] + dom[t.end():]
    return dom


def strip_badges(dom: str) -> tuple:
    """Delete platform badges outright — element, images and the CDN
    requests they fire. Only a port can do this: with the runtime gone,
    nothing re-creates them."""
    n = 0
    for opener in (r'(?is)<a\b[^>]*class="[^"]*w-webflow-badge[^"]*"[^>]*>',
                   r'(?is)<div\b[^>]*id="__framer-badge-container"[^>]*>',
                   r'(?is)<a\b[^>]*href="[^"]*framer\.com/r/badge[^"]*"[^>]*>'):
        while True:
            m = re.search(opener, dom)
            if not m:
                break
            after = cut_element(dom, m.start())
            if after == dom:
                break
            dom, n = after, n + 1
    return dom, n


def strip_platform(dom: str, keep_preloads=False) -> tuple:
    """Dead-end every link back to the platform, and drop the preloads
    for a runtime that no longer exists. 30 modulepreload tags were
    making the browser fetch 5.5MB of chunks that nothing runs."""
    n_pre = 0
    if not keep_preloads:
        n_pre = len(re.findall(
            r'(?is)<link[^>]*rel="(?:modulepreload|prefetch)"[^>]*>', dom))
        dom = re.sub(r'(?is)<link[^>]*rel="(?:modulepreload|prefetch)"[^>]*>',
                     "", dom)
    dom, n_hint = strip_hints(dom)
    dom, n_badge = strip_badges(dom)
    # DELETE anchors that point at the platform/marketplace, before the
    # href is rewritten — neutralising the link to "#" left the promo
    # card ("NEW TEMPLATES") sitting on the page looking native.
    n_promo = 0
    while True:
        # ONLY the badge/marketplace promo. Matching any framer.com
        # link deleted 18 portfolio cards and their images with it —
        # a plain link back to the platform should be defused (href ->
        # "#"), never taken out along with the content around it.
        m = re.search(r'(?is)<a\b[^>]*href="[^"]*'
                      r'(?:framer\.com/r/badge|tab=marketplace|'
                      r'framer\.com/marketplace|buy\.polar\.sh|'
                      r'lemonsqueezy\.com|gumroad\.com)'
                      r'[^"]*"[^>]*>', dom)
        if not m:
            break
        after = cut_element(dom, m.start())
        if after == dom:
            break
        dom, n_promo = after, n_promo + 1
    n_link = len(set(PLATFORM_LINKS.findall(dom))) + n_promo
    dom = PLATFORM_LINKS.sub("#", dom)
    return dom, n_pre + n_hint, n_link + n_badge


def _outside_raw_text(dom: str, pattern: str):
    """First match of `pattern` that is NOT inside a <script>, <style>,
    or HTML comment.

    A tag name written in script text or a comment is still just text,
    and avenlo's own head script has this in a comment:

        // … so a backdrop parked on <body>

    and head comments can also reference markup:

        <!-- layout note: the sticky <body> wrapper owns the scroll -->

    <body> matched there first, so the page body was taken from the
    middle of that script or comment: the component began three thousand
    characters into the JavaScript or inside a comment, without its opening
    tag, and the brace escaper then treated the rest of the code as markup.
    Astro refused to compile `window.scrollTo(&#123; top: 0 …`, which is
    the right answer to markup that was never markup.
    """
    spans = [(m.start(), m.end()) for m in re.finditer(
        r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>|<!--.*?-->", dom)]
    for m in re.finditer(pattern, dom, re.I | re.S):
        if not any(a <= m.start() < b for a, b in spans):
            return m
    return None


def _body_of(dom: str):
    """(inner html, attrs) of the real <body>, or (None, "").

    Located as the OPENING TAG first and sliced afterwards: searching for
    the whole <body>…</body> at once lets one bad candidate swallow the
    document — the match that starts inside a script runs to the closing
    tag, and the scan resumes past it with no candidate left.
    """
    open_m = _outside_raw_text(dom, r"<body\b[^>]*>")
    if not open_m:
        return None, ""
    close_m = _outside_raw_text(dom, r"</body\s*>")
    if close_m and close_m.start() >= open_m.end():
        end = close_m.start()
    else:
        end = dom.rfind("</body")
        if end < open_m.end():
            end = len(dom)
    attrs = re.match(r"(?is)<body\b([^>]*)>", open_m.group(0))
    return dom[open_m.end():end], (attrs.group(1) if attrs else "")


def split_document(dom: str) -> tuple:
    head_open = _outside_raw_text(dom, r"<head\b[^>]*>")
    head_close = _outside_raw_text(dom, r"</head\s*>")
    body_open = _outside_raw_text(dom, r"<body\b[^>]*>")
    if head_open:
        if head_close and head_close.start() >= head_open.end():
            head = dom[head_open.end():head_close.start()]
        elif body_open and body_open.start() >= head_open.end():
            head = dom[head_open.end():body_open.start()]
        else:
            head = ""
    else:
        head_m = re.search(r"(?is)<head\b[^>]*>(.*?)</head\s*>", dom)
        head = head_m.group(1) if head_m else ""

    inner, body_attrs = _body_of(dom)
    lang_m = _outside_raw_text(dom, r'<html\b[^>]*\blang="([^"]*)"') or \
        re.search(r'(?is)<html\b[^>]*\blang="([^"]*)"', dom)
    lang = lang_m.group(1) if lang_m else "en"
    return (head,
            dom if inner is None else inner,
            body_attrs.strip(),
            lang)


def route_of(page: str) -> str:
    return "index" if page == "index.html" else page[:-5] if \
        page.endswith(".html") else page




# ─────────────── the animation spec, read from the source ────────────
# THE CORRECTION THAT MATTERED: motion was being inferred from outside —
# sampling the rendered page and guessing a curve. But the page SHIPS
# its own animation definitions, and Aethron owns them:
#
#   <script type="framer/appear" id="__framer__appearAnimationsContent">
#   {"1n7k6km": {"default": {
#      "initial": {"opacity":0.001,"y":0,...},
#      "animate": {"opacity":1,...,
#                  "transition":{"type":"spring","stiffness":200,
#                                "damping":60,"mass":1,"delay":0.6}}}}}
#
# Keyed by data-framer-appear-id — the same attribute the elements
# carry. So the entrance is not estimated at all: exact delay, exact
# duration, exact easing, exact spring physics, per breakpoint.
#
# Measured on one real template: SEVEN of ten transitions are springs,
# with delays from 0.2s to 1.6s. Replaying that as one hardcoded
# "0.6s ease" is precisely the replica-not-identical the owner refused.

def extract_appear_spec(html: str) -> dict:
    """-> {"anims": {...}, "breakpoints": [...]} straight from the page."""
    out = {"anims": {}, "breakpoints": []}
    m = re.search(r'(?is)<script[^>]*id="__framer__appearAnimationsContent"'
                  r'[^>]*>(.*?)</script\s*>', html)
    if m:
        try:
            out["anims"] = json.loads(m.group(1))
        except ValueError:
            pass
    b = re.search(r'(?is)<script[^>]*id="__framer__breakpoints"[^>]*>'
                  r'(.*?)</script\s*>', html)
    if b:
        try:
            out["breakpoints"] = json.loads(b.group(1))
        except ValueError:
            pass
    return out



# ─────────── carry the original's animation engine, verbatim ─────────
# THE CORRECTION: recreating motion was the wrong instinct twice over —
# first a hand-rolled spring integrator, then replayed keyframes. The
# page already ships its animation engine as ~11KB of SELF-CONTAINED
# inline script (it imports nothing from the chunks), reading the same
# __framer__appearAnimationsContent we carry and driving the same
# data-framer-appear-id the DOM already has.
#
# Carrying it makes the animation identical BY CONSTRUCTION — same
# engine, same maths — instead of an imitation that merely looks close.
#
# This is not the 5.5MB chunk runtime that draws the whole page as a
# black box. The structure and CSS stay real framework source the owner
# can edit; only the animation utility comes across, self-hosted, with
# nothing phoning home.

ENGINE_MARKERS = ("startOptimizedAppearAnimation", "animateAppearEffects",
                  "data-framer-appear-id")


def extract_engine_data(html: str) -> list:
    """The engine's DATA tags, carried with their ORIGINAL id and type.

    The engine looks the spec up by Framer's own id
    (__framer__appearAnimationsContent). Renaming it to something of
    ours meant the engine loaded, found no spec, and animated nothing —
    the elements just sat there. Carrying it as-is is the whole point of
    using the original rather than reimplementing it."""
    return [m.group(0) for m in
            re.finditer(r'(?is)<script[^>]*type="framer/appear"[^>]*>'
                        r'.*?</script\s*>', html)]


def extract_motion_engine(html: str) -> list:
    """The inline scripts that implement the original's animations, in
    document order. Only self-contained ones — anything importing from
    the chunks would drag the app runtime with it."""
    out = []
    for m in re.finditer(r'(?is)<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script\s*>',
                         html):
        attrs, body = m.group(1), m.group(2)
        if "application/json" in attrs or "framer/appear" in attrs:
            continue                      # that is the DATA, carried already
        if not any(k in body for k in ENGINE_MARKERS):
            continue
        if re.search(r'\bimport\s+[\w{*]|from\s+["\']\./assets', body):
            continue                      # not self-contained: skip it
        out.append(body)
    return out


# ─────────────── organising the output like a project ────────────────
# A 600KB body blob is not something a developer can work with. The
# original is already authored in sections — Framer names them
# (data-framer-name), Webflow gives them classes — so we cut on those
# boundaries and emit one component per section. Concatenating the
# sections reproduces the body byte-for-byte, which is what keeps this
# faithful: the split is structural, never a rewrite.

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}
TAG = re.compile(r"(?is)<(/?)([a-z][a-z0-9-]*)\b([^>]*?)(/?)>")


def top_level_spans(html: str) -> list:
    """[(start, end, attrs)] for each direct child ELEMENT, in order."""
    out, depth, begin, battrs = [], 0, None, ""
    for m in TAG.finditer(html):
        closing, tag, attrs, selfclose = m.groups()
        tag = tag.lower()
        if tag in VOID or selfclose:
            if depth == 0:
                out.append((m.start(), m.end(), attrs))
            continue
        if not closing:
            if depth == 0:
                begin, battrs = m.start(), attrs
            depth += 1
        else:
            depth -= 1
            if depth == 0 and begin is not None:
                out.append((begin, m.end(), battrs))
                begin = None
            if depth < 0:
                return []
    return out if depth == 0 else []


def section_name(attrs: str, index: int, used: set) -> str:
    """A human name for the file, from what the designer actually
    called the section."""
    for pat in (r'data-framer-name="([^"]+)"', r'\bid="([^"]+)"',
                r'aria-label="([^"]+)"'):
        m = re.search(pat, attrs, re.I)
        if m:
            raw = m.group(1)
            break
    else:
        m = re.search(r'class="([^"]*)"', attrs, re.I)
        raw = ""
        for c in (m.group(1).split() if m else []):
            if not re.match(r"(?i)^(framer-[a-z0-9]+|w-|css-)", c):
                raw = c
                break
    words = re.findall(r"[A-Za-z0-9]+", raw or "")
    name = "".join(w[:1].upper() + w[1:] for w in words)[:40]
    if not name or name[0].isdigit():
        name = "Section" + name
    base, n = name, 2
    while name in used:
        name, n = f"{base}{n}", n + 1
    used.add(name)
    return name


def split_sections(body: str, min_parts=3, max_depth=4):
    """-> (prefix, [(name, html)], suffix)

    INVARIANT, asserted by the caller: prefix + ''.join(html) + suffix
    reproduces `body` byte-for-byte. Anything between elements (text,
    whitespace, comments) is carried on the following part, so nothing
    can be silently dropped — an earlier version split only on element
    spans and lost the gaps between them."""
    prefix, suffix, inner = "", "", body
    best = None                 # deepest level that actually splits well
    for _ in range(max_depth):
        spans = top_level_spans(inner)
        if not spans:
            break
        if len(spans) >= min_parts:
            best = (prefix, inner, suffix)
        # Follow the CONTENT, not the element count. A page is usually
        # one <main> holding 95% of the bytes beside a couple of tiny
        # siblings; splitting at that level yields "Main: 402KB", which
        # is no split at all.
        sizes = [(b - a) for a, b, _ in spans]
        biggest = max(range(len(spans)), key=lambda i: sizes[i])
        dominant = sizes[biggest] > 0.6 * len(inner)
        if not dominant and len(spans) >= min_parts:
            break
        if not dominant:
            break
        a, b, _ = spans[biggest]
        open_m = TAG.match(inner, a)
        close_m = list(TAG.finditer(inner[:b]))[-1]
        child = inner[open_m.end():close_m.start()]
        if len(top_level_spans(child)) < 2:
            break                      # nothing gained by going deeper
        # descending is a gamble: the level below may be a single chain.
        # `best` holds the last good one so a bad descent cannot cost us
        # the split entirely (removing the badge once collapsed a
        # 13-section page to one).
        prefix += inner[:open_m.end()]
        suffix = inner[close_m.start():] + suffix
        inner = child
    spans = top_level_spans(inner)
    if len(spans) < 2 and best:
        prefix, inner, suffix = best
        spans = top_level_spans(inner)
    if len(spans) < 2:
        return prefix, [("Page", inner)], suffix
    used, parts, cursor = set(), [], 0
    for i, (a, b, attrs) in enumerate(spans, 1):
        html = inner[cursor:b]          # gap + element, nothing lost
        cursor = b
        parts.append((section_name(attrs, i, used), html))
    if cursor < len(inner):             # trailing text belongs somewhere
        parts[-1] = (parts[-1][0], parts[-1][1] + inner[cursor:])
    return prefix, parts, suffix


def extract_styles(head: str):
    """Inline <style> out of the head into a real stylesheet the user
    can open and edit. Order is preserved, and a <link> takes its place
    at the position of the first block, so cascade order is unchanged."""
    blocks = re.findall(r"(?is)<style\b[^>]*>(.*?)</style\s*>", head)
    if not blocks:
        return head, ""
    css = "\n\n".join(b.strip() for b in blocks if b.strip())
    first = re.search(r"(?is)<style\b[^>]*>.*?</style\s*>", head)
    head = head[:first.start()] + \
        '<link rel="stylesheet" href="/styles/site.css">' + \
        head[first.end():]
    head = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", "", head)
    return head, css



# ─────────────── HTML -> real JSX, mechanically ──────────────────────
# React cannot inject markup without a host element, so the obvious
# route (dangerouslySetInnerHTML per section) silently adds a <div> the
# original never had — which breaks `.parent > .child` selectors and
# flex/grid layouts while every text metric still reads 100%. So the
# markup is transformed into actual JSX instead: same tree, same
# attributes, no extra nodes.
#
# The mapping is the documented one (class -> className, for -> htmlFor,
# hyphenated -> camelCase except data-/aria-, style string -> object).

JSX_ATTR = {"class": "className", "for": "htmlFor", "srcset": "srcSet",
            "tabindex": "tabIndex", "readonly": "readOnly",
            "maxlength": "maxLength", "cellpadding": "cellPadding",
            "cellspacing": "cellSpacing", "colspan": "colSpan",
            "rowspan": "rowSpan", "contenteditable": "contentEditable",
            "crossorigin": "crossOrigin", "datetime": "dateTime",
            "enctype": "encType", "formaction": "formAction",
            "frameborder": "frameBorder", "hreflang": "hrefLang",
            "inputmode": "inputMode", "keyparams": "keyParams",
            "marginwidth": "marginWidth", "marginheight": "marginHeight",
            "novalidate": "noValidate", "playsinline": "playsInline",
            "referrerpolicy": "referrerPolicy", "spellcheck": "spellCheck",
            "usemap": "useMap", "autoplay": "autoPlay",
            "autocomplete": "autoComplete", "autofocus": "autoFocus",
            "accept-charset": "acceptCharset", "http-equiv": "httpEquiv"}
# SVG ELEMENT names are camelCase in JSX too, and the parser lowercases
# them exactly as it does attributes: <feFlood> arrives as <feflood>,
# which React rejects outright.
SVG_TAGS = {t.lower(): t for t in (
    "feFlood", "feBlend", "feColorMatrix", "feComponentTransfer",
    "feComposite", "feConvolveMatrix", "feDiffuseLighting",
    "feDisplacementMap", "feDistantLight", "feDropShadow", "feFuncA",
    "feFuncB", "feFuncG", "feFuncR", "feGaussianBlur", "feImage",
    "feMerge", "feMergeNode", "feMorphology", "feOffset", "fePointLight",
    "feSpecularLighting", "feSpotLight", "feTile", "feTurbulence",
    "linearGradient", "radialGradient", "clipPath", "textPath",
    "foreignObject", "animateMotion", "animateTransform", "glyphRef",
    "altGlyph", "altGlyphDef", "altGlyphItem")}


def _jsx_tag(tag: str) -> str:
    return SVG_TAGS.get(tag.lower(), tag)


SVG_CAMEL = {a.lower(): a for a in (
    "viewBox", "preserveAspectRatio", "baseProfile", "patternUnits",
    "patternContentUnits", "patternTransform", "gradientUnits",
    "gradientTransform", "spreadMethod", "markerWidth", "markerHeight",
    "markerUnits", "refX", "refY", "textLength", "lengthAdjust",
    "startOffset", "pathLength", "clipPathUnits", "maskUnits",
    "maskContentUnits", "primitiveUnits", "filterUnits", "stdDeviation",
    "tableValues", "xChannelSelector", "yChannelSelector", "numOctaves",
    "baseFrequency", "stitchTiles", "diffuseConstant", "specularConstant",
    "specularExponent", "surfaceScale", "kernelMatrix", "kernelUnitLength",
    "limitingConeAngle", "pointsAtX", "pointsAtY", "pointsAtZ",
    "attributeName", "attributeType", "repeatCount", "repeatDur",
    "keyTimes", "keySplines", "calcMode", "requiredExtensions",
    "systemLanguage", "edgeMode", "targetX", "targetY", "order")}

# React types these as numbers; HTML writes every attribute as text.
NUMERIC_ATTR = {"tabindex", "colspan", "rowspan", "span", "start", "size",
                "maxlength", "minlength", "cols", "rows", "high", "low",
                "optimum", "marginwidth", "marginheight"}

BOOLEAN_ATTR = {"checked", "disabled", "selected", "readonly", "multiple",
                "controls", "autoplay", "loop", "muted", "playsinline",
                "required", "novalidate", "open", "hidden", "async",
                "defer", "reversed", "itemscope", "default", "inert"}


def _camel(name: str) -> str:
    parts = name.split("-")
    return parts[0] + "".join(w[:1].upper() + w[1:] for w in parts[1:])


def _jsx_attr_name(name: str) -> str:
    low = name.lower()
    if low in JSX_ATTR:
        return JSX_ATTR[low]
    if low in SVG_CAMEL:
        return SVG_CAMEL[low]
    if low.startswith(("data-", "aria-")):
        return low                      # these stay hyphenated in JSX
    if ":" in low:                      # xlink:href -> xlinkHref
        a, b = low.split(":", 1)
        return a + b[:1].upper() + b[1:]
    if "-" in low:
        return _camel(low)
    return low


def _style_object(css: str) -> str:
    out = []
    for decl in css.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop, val = prop.strip(), val.strip()
        if not prop or not val:
            continue
        key = prop if prop.startswith("--") else _camel(prop.lower())
        if not prop.startswith("--"):
            key = re.sub(r"^Webkit|^Moz|^Ms", lambda m: m.group(0), key)
        quoted = json.dumps(val)
        out.append(f"{json.dumps(key)}: {quoted}")
    return "{" + ", ".join(out) + "}"


def _jsx_text(text: str) -> str:
    """Braces are JSX syntax; < and > would open tags."""
    if not text.strip():
        return text if "\n" not in text else " "
    # ONE pass: replacing { then } re-escapes the braces the first
    # replacement just introduced ({a} -> {\'{\'{\'}\'}a...).
    safe = re.sub(r"[{}<>]", lambda m: {
        "{": "{'{'}", "}": "{'}'}", "<": "&lt;", ">": "&gt;"}[m.group(0)],
        text)
    return safe


class _JsxWriter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.out.append(self._open(tag, attrs, self_close=tag in VOID))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.out.append(self._open(tag, attrs, self_close=True))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.out.append(f"</{_jsx_tag(tag)}>")
        elif tag in self.stack:            # implicitly closed children
            while self.stack and self.stack[-1] != tag:
                self.out.append(f"</{_jsx_tag(self.stack.pop())}>")
            if self.stack:
                self.stack.pop()
                self.out.append(f"</{_jsx_tag(tag)}>")

    def handle_data(self, data):
        self.out.append(_jsx_text(data))

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")

    def handle_comment(self, data):
        pass                               # comments carry nothing visual

    def _open(self, tag, attrs, self_close):
        bits = []
        for k, v in attrs:
            name = _jsx_attr_name(k)
            if k.lower() in BOOLEAN_ATTR:
                # HTML spells these `loop`, `loop=""` or `loop="loop"`;
                # React types them as booleans, so a string fails to
                # compile. Only the literal "false" means false.
                falsey = (v or "").strip().lower() == "false"
                bits.append(f"{name}={{{'false' if falsey else 'true'}}}")
                continue
            if v is None:
                bits.append(f'{name}=""')
                continue
            if k.lower() in NUMERIC_ATTR and \
                    re.fullmatch(r"-?\d+(?:\.\d+)?", (v or "").strip()):
                bits.append(f"{name}={{{v.strip()}}}")
                continue
            if name == "style":
                # `as AeStyle` — Framer's design IS custom properties
                # (--framer-*, --token-*), and React's CSSProperties
                # type has no room for them, so strict TS rejects every
                # styled element without the cast.
                bits.append(f"style={{{_style_object(v)} as AeStyle}}")
            else:
                bits.append(f"{name}={{{json.dumps(html_mod.unescape(v))}}}")
        attr_s = (" " + " ".join(bits)) if bits else ""
        return f"<{_jsx_tag(tag)}{attr_s}{' />' if self_close else '>'}"

    def close_all(self):
        while self.stack:
            self.out.append(f"</{_jsx_tag(self.stack.pop())}>")
        return "".join(self.out)


def to_jsx(html: str) -> str:
    w = _JsxWriter()
    w.feed(html)
    return w.close_all()


# ─────────────────────────── emitters ────────────────────────────────

def emit_astro(dest: Path, pages: dict, name: str):
    """Astro is a superset of HTML and `set:html` writes markup out
    verbatim, so the built page is what we carried in — but split into
    one component per authored section, which is what makes it a
    project someone can actually work in."""
    _w(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "astro dev", "build": "astro build",
                    "preview": "astro preview"},
        "dependencies": {"astro": "5.14.1"}}, indent=2))
    _w(dest / "astro.config.mjs",
       "import { defineConfig } from 'astro/config';\n"
       "// `directory` format emits /about/index.html so the site\n"
       "// serves /about — the clean URLs the ORIGINAL used. `file`\n"
       "// gave /about.html, which matched nothing: the scraped site\n"
       "// had no .html in its links and neither should its port.\n"
       "export default defineConfig({ build: { format: 'directory' },\n"
       "  devToolbar: { enabled: false } });\n")
    _w(dest / "tsconfig.json",
       json.dumps({"extends": "astro/tsconfigs/strict"}, indent=2))

    css_all = []
    for page, doc in pages.items():
        r = route_of(page)
        head, css = extract_styles(doc["head"])
        if css:
            css_all.append(f"/* ---- {page} ---- */\n{css}")
        prefix, parts, suffix = split_sections(doc["body"])
        assert prefix + "".join(h for _, h in parts) + suffix == doc["body"], \
            f"{page}: section split is not byte-exact"

        _w(dest / f"src/html/{r}.head.html", clean_urls(head))
        imports, uses = [], []
        for i, (sec, html) in enumerate(parts, 1):
            comp = f"{i:02d}-{sec}"
            _w(dest / f"src/components/{r}/{comp}.astro",
               f"---\n// {sec} — from {page}. Edit this markup directly.\n"
               f"---\n" + astro_markup(clean_urls(html)) + "\n")
            imports.append(f"import {sec} from "
                           f"'../components/{r}/{comp}.astro';")
            uses.append(f"    <{sec} />")
        nl = chr(10)
        _w(dest / f"src/pages/{r}.astro", f"""---
// {page} — {len(parts)} section(s), each its own component.
import head from '../html/{r}.head.html?raw';
const anim = {json.dumps(anim_tag(doc))};
{nl.join(imports)}
---
<html lang="{doc['lang']}">
  <head set:html={{head}} />
  <body{(' ' + doc['battrs']) if doc['battrs'] else ''}>
{astro_markup(clean_urls(prefix))}
{nl.join(uses)}
{astro_markup(clean_urls(suffix))}
    <Fragment set:html={{anim}} />
  </body>
</html>
""")
    if css_all:
        _w(dest / "public/styles/site.css", (nl := chr(10)) and
           (nl * 2).join(css_all))
    _w(dest / "public/aethron-motion.js", MOTION_JS)
    _w(dest / "README.md", f"""# {name}

Converted from the original template by Aethron. Nothing from the
source platform ships here: no runtime, no CDN, no tracking.

    src/pages/          one file per page
    src/components/     one component per authored section
    public/styles/      the site's CSS, extracted and editable
    public/assets/      images, fonts and video — all local
    public/aethron-motion.js
                        ~20 lines that replay the entrance animations
                        recovered from the original. Delete it and the
                        content simply stays visible.

    npm install && npm run build
""")


def emit_next(dest: Path, pages: dict, name: str):
    """Next.js App Router, TypeScript, real JSX — no wrapper elements.

    Sections become components by cutting the body at their boundaries
    and leaving a placeholder element in the shell; the shell is then
    converted as one tree, so the DOM the browser builds is exactly the
    original's. That is what makes it safe to call pixel-perfect."""
    _w(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0",
        "scripts": {"dev": "next dev", "build": "next build",
                    "start": "next start"},
        "dependencies": {"next": "15.5.4", "react": "19.1.1",
                         "react-dom": "19.1.1"},
        "devDependencies": {"typescript": "5.9.2",
                            "@types/react": "19.1.9",
                            "@types/react-dom": "19.1.7",
                            "@types/node": "22.15.3"}}, indent=2))
    _w(dest / "next.config.mjs",
       "/** @type {import('next').NextConfig} */\n"
       "export default { output: 'export', images: { unoptimized: true },\n"
       "  trailingSlash: true };\n")
    _w(dest / "tsconfig.json", json.dumps({
        "compilerOptions": {"target": "ES2022",
                            "lib": ["dom", "dom.iterable", "esnext"],
                            "allowJs": True, "skipLibCheck": True,
                            "strict": True, "noEmit": True,
                            "esModuleInterop": True, "module": "esnext",
                            "moduleResolution": "bundler",
                            "resolveJsonModule": True,
                            "isolatedModules": True, "jsx": "preserve",
                            "incremental": True,
                            "plugins": [{"name": "next"}]},
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx",
                    ".next/types/**/*.ts"],
        "exclude": ["node_modules"]}, indent=2))

    css_all = []
    first = next(iter(pages.values()))
    for page, doc in pages.items():
        r = route_of(page)
        head, css = extract_styles(doc["head"])
        if css:
            css_all.append(f"/* ---- {page} ---- */\n{css}")
        prefix, parts, suffix = split_sections(doc["body"])
        assert prefix + "".join(h for _, h in parts) + suffix == doc["body"], \
            f"{page}: section split is not byte-exact"

        # placeholder elements survive the JSX conversion, so the shell
        # can be converted as ONE tree and the slots swapped for
        # components afterwards
        slots = "".join(f"<ae-slot-{i}></ae-slot-{i}>"
                        for i in range(1, len(parts) + 1))
        shell = to_jsx(prefix + slots + suffix)
        imports = []
        for i, (sec, html) in enumerate(parts, 1):
            comp = f"{i:02d}-{sec}"
            _w(dest / f"app/components/{r}/{comp}.tsx",
               f"// {sec} — carried from the original build, as JSX.\n"
               f"export default function {sec}() {{\n  return (<>\n"
               f"{to_jsx(html)}\n  </>);\n}}\n")
            imports.append(f"import {sec} from "
                           f"'../components/{r}/{comp}';"
                           if r != "index" else
                           f"import {sec} from './components/{r}/{comp}';")
            shell = shell.replace(f"<ae-slot-{i}></ae-slot-{i}>",
                                  f"<{sec} />")
        nl = chr(10)
        page_dir = "app" if r == "index" else f"app/{r}"
        _w(dest / f"{page_dir}/page.tsx", f"""// {page} — {len(parts)} section(s), each its own component.
{nl.join(imports)}

export default function Page() {{
  return (<>
{shell}
  </>);
}}
""")

    head_jsx = to_jsx(extract_styles(first["head"])[0])
    spec_json = json.dumps(json.dumps(first.get("spec") or
                                      {"anims": {}, "breakpoints": []}))
    tl_json = json.dumps(json.dumps(first.get("timeline") or {"entries": {}}))
    _w(dest / "app/layout.tsx", f"""// The original document head, carried as real elements. React hoists
// link/meta/title from anywhere in the tree, so they land in <head>.
export default function RootLayout(
  {{ children }}: {{ children: React.ReactNode }}) {{
  return (
    <html lang="{first['lang']}">
      <head>
{head_jsx}
      </head>
      <body>
        {{children}}
        <script type="application/json" id="__ae_anim"
          dangerouslySetInnerHTML={{{{ __html: {spec_json} }}}} />
        <script type="application/json" id="__ae_timeline"
          dangerouslySetInnerHTML={{{{ __html: {tl_json} }}}} />
        <script src="/aethron-motion.js" defer />
      </body>
    </html>
  );
}}
""")
    # The original stylesheet ships VERBATIM from public/. Routing it
    # through Next's CSS pipeline rewrites vendor CSS and can simply
    # reject it — a Webflow sheet failed with `Unexpected "&" found`.
    # extract_styles already left a <link> to this path in the head.
    _w(dest / "public/styles/site.css",
       (chr(10) * 2).join(css_all) or "/* none */")
    # a global alias: no file needs to import it
    # TWO files on purpose: a .d.ts with a top-level import/export is a
    # MODULE, and its declarations stop being global. The `import()`
    # TYPE syntax below does not trigger that; a bare `import 'react'`
    # does — which is what made AeStyle vanish.
    _w(dest / "app/aethron.d.ts",
       "// CSS custom properties (--framer-*, --token-*) ARE this design.\n"
       "// React sets them fine at runtime; its CSSProperties type has\n"
       "// no slot for them. Global on purpose — no file should have to\n"
       "// import it.\n"
       "declare type AeStyle = import('react').CSSProperties &\n"
       "  Record<string, string | number>;\n")
    _w(dest / "app/react-attrs.d.ts",
       "// The markup carries the original's authoring attributes\n"
       "// (parentsize, _constraints, rotation, shadows...). Nothing in\n"
       "// the CSS selects on them, so they could be dropped — but\n"
       "// dropping anything from a port that promises to be identical\n"
       "// is the wrong instinct. The DOM is kept; the types widen.\n"
       "import 'react';\n\n"
       "declare module 'react' {\n"
       "  interface HTMLAttributes<T> { [attr: string]: unknown }\n"
       "  interface SVGAttributes<T> { [attr: string]: unknown }\n"
       "}\n")
    _w(dest / "public/aethron-motion.js", MOTION_JS)
    _w(dest / "README.md", f"""# {name}

Converted from the original template by Aethron — real JSX, no wrapper
elements, nothing from the source platform.

    app/page.tsx           the home page
    app/components/        one component per authored section
    app/globals.css        the site's CSS, extracted and editable
    public/assets/         images, fonts and video — all local
    public/aethron-motion.js
                           ~20 lines that replay the entrance animations
                           recovered from the original

    npm install && npm run build
""")


def _head_tags(head: str) -> list:
    """<style> and <link> out of the original head, as data React can
    render as real elements."""
    out = []
    for m in re.finditer(r"(?is)<style\b[^>]*>(.*?)</style\s*>", head):
        out.append({"style": m.group(1)})
    for m in re.finditer(r"(?is)<link\b([^>]*)>", head):
        attrs = dict(re.findall(r'([a-zA-Z-]+)="([^"]*)"', m.group(1)))
        ren = {"class": "className", "crossorigin": "crossOrigin",
               "referrerpolicy": "referrerPolicy"}
        out.append({"attrs": {ren.get(k, k): v for k, v in attrs.items()}})
    return out


def emit_vite(dest: Path, pages: dict, name: str):
    _w(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "vite", "build": "vite build",
                    "preview": "vite preview"},
        "devDependencies": {"vite": "7.1.5"}}, indent=2))
    _w(dest / "vite.config.ts",
       "import { defineConfig } from 'vite';\n"
       "export default defineConfig({ appType: 'mpa' });\n")
    for page, doc in pages.items():
        r = route_of(page)
        doc = {**doc, "body": doc["body"] + MOTION_TAG}
        _w(dest / f"{r}.html",
           f"<!doctype html>\n<html lang=\"{doc['lang']}\">\n<head>"
           f"{doc['head']}</head>\n<body{(' ' + doc['battrs']) if doc['battrs'] else ''}>"
           f"{doc['body']}\n<script src=\"/aethron-motion.js\" defer></script>"
           f"</body>\n</html>\n")
    _w(dest / "public/aethron-motion.js", MOTION_JS)


EMITTERS = {"astro": emit_astro, "next": emit_next, "vite": emit_vite}
OUTDIR = {"astro": "dist", "next": "out", "vite": "dist"}


def _w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")



def _entrance_owned(docs: dict) -> dict:
    """Selectors the ENTRANCE recorder actually captured, for this page.

    Handed to the scroll recorder so it excludes exactly those and
    nothing else. Inferring ownership from "this element has a Web
    Animation" excluded 13 elements the entrance pass had in fact
    missed, leaving them driven by neither."""
    owned = {}
    for doc in docs.values():
        for k in (doc.get("entrance") or {}).get("owned") or {}:
            owned[k] = 1
    return owned

def _ship_animation_source(project: Path, dest: Path, say) -> dict:
    """Put the template's ORIGINAL animation code inside the port.

    Framer serves a source map for every chunk it built, so the authored
    source of the template's own components is recoverable exactly —
    real names, real numbers, the actual useScroll offsets and spring
    configs. A developer who opens this port should not have to reverse
    a minified bundle to find out what an element was supposed to do.

    Only the TEMPLATE AUTHOR'S modules travel. Framer's own runtime
    bundles are excluded: they are the vendor's code, the port does not
    depend on them, and the engine underneath is framer-motion — a
    public package, not something to smuggle out of a CDN."""
    try:
        import aethron_source as source
    except Exception:
        return {"available": False, "reason": "recovery module unavailable"}
    index = source.recover(project, quiet=True)
    if not index.get("available"):
        say("stage", "animation source: none served for this template "
                     f"({index.get('reason', 'no maps')}) — port ships "
                     "without it")
        return index
    doc = source.build_map(project, quiet=True)
    if not doc.get("available"):
        return doc

    out = dest / "ANIMATIONS"
    out.mkdir(parents=True, exist_ok=True)
    src_dir = project / "pristine" / "sources"
    shipped = 0
    for comp in doc["components"]:
        f = src_dir / comp["module"]
        if f.exists():
            (out / comp["origin"]).write_text(f.read_text(errors="replace"))
            shipped += 1
    (out / "animations.json").write_text(json.dumps(doc, indent=2))
    (out / "README.md").write_text(_ANIM_README % {
        "n": shipped,
        "sites": doc["totals"]["animation_sites"],
        "api": ", ".join(list(doc["framer_package_api"])[:12]),
    })
    say("stage", f"animation source: {shipped} component(s) recovered from "
                 f"the template's own source maps → ANIMATIONS/ "
                 f"({doc['totals']['animation_sites']} animation sites)")
    return {"available": True, "components": shipped,
            "sites": doc["totals"]["animation_sites"]}


_ANIM_README = """# The template's original animation code

`%(n)d` component(s) recovered from the source maps the original site
served. This is the authored source — real identifier names, real
numbers — not a reconstruction, and not the minified bundle.

`animations.json` indexes every one: what it animates, the literal
config at each of the %(sites)d animation sites (durations, easings,
spring settings, scroll offsets), and which elements on which pages it
renders, matched by class.

## Reading it

Each file is plain React. The motion primitives (`useScroll`,
`useTransform`, `useSpring`, `useInView`, `motion.*`) come from
**framer-motion**, which is a public npm package — `npm i framer-motion`
and these components run.

## The one thing that is not plug-and-play

Framer components also import from a package called `framer`:

    %(api)s …

That package is proprietary and is deliberately NOT included here. Most
of its surface is inert outside the Framer editor (property controls
exist only for the canvas); the rest is small and mechanical — `cx` is
class joining, `Link`/`Image` are `<a>`/`<img>`, `RichText` is a
wrapper. Shim what you need.

## Why this is here

The port renders the design and replays the entrance animations on its
own. This directory is the ground truth for everything beyond that: if
an interaction does not feel right, the code that defines it is here,
with the original values, and you can port it deliberately instead of
guessing at it.
"""


# ─────────────────────────── the pipeline ────────────────────────────

def convert(project, framework="astro", pages=None, on_event=None,
            install=True, build=True, keep_runtime=True):
    """keep_runtime=True is IDENTICAL BY CONSTRUCTION.

    Every other approach reproduces the motion: a hand-rolled spring
    integrator, replayed keyframes, or reading each component's config
    out of minified chunks and driving the real library. All of them are
    only as accurate as the reading, and a component missed is a
    deviation.

    Carrying the original's own code cannot deviate, because it IS the
    original. What that costs is hand-editable page-render source; what
    it keeps is the framework project around it — the build, the routes,
    the assets, the content, the CSS — and the animation exactly as the
    designer made it.

    Nothing is given up on ownership: the runtime is localized to the
    user's own server, no CDN, no telemetry, badges deleted. The
    platform could vanish and the site still runs."""
    project = Path(project).resolve()
    if framework not in FRAMEWORKS:
        raise SystemExit(f"--framework must be one of {FRAMEWORKS}")
    site = project / "site"
    if not site.is_dir():
        raise SystemExit("no site/ — migrate and build the project first")
    say = on_event or (lambda k, t: print(f"── {t}"))
    sys.path.insert(0, str(ROOT))
    import forge
    from http.server import ThreadingHTTPServer

    cfg = json.loads((project / "forge.json").read_text(encoding="utf-8"))
    want = pages or [p for p in cfg.get("pages", []) if (site / p).exists()]
    browser = forge._find_browser()
    if not browser:
        raise SystemExit("conversion needs a headless browser to read the "
                         "original as it truly renders")

    dest = project / f"convert-{framework}"
    if dest.exists():
        # keep node_modules — reinstalling a toolchain on every run is
        # minutes of nothing, and on a locked-down machine it can fail
        for child in dest.iterdir():
            if child.name in ("node_modules", "package-lock.json"):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    import aethron_motion as motion
    # The entrance recorder runs FIRST and from the very first frame:
    # these animations are short-lived, and by the time the timeline
    # sampler has scrolled anywhere they have finished and been
    # collected. Both stamp data-ae-id and both reuse an existing one,
    # so they agree on element identity.
    tl_js = (motion.ENTRANCE_JS % {"watch": 4200}
             + motion.TIMELINE_JS % {"samples": 26, "every": 55,
                                     "step": 0.75})
    # One pass: the injected recorder walks the page, stamps data-ae-id on
    # every element it sees move, and leaves the keyframes in the DOM. The
    # capture and the conversion therefore see the SAME page state.
    H = motion._injecting_handler(site, cfg.get("platform", "static"), tl_js)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    docs, total_ae = {}, 0
    try:
        for page in want:
            say("stage", f"reading {page} as the browser renders it…")
            got = forge._render_page(browser,
                                     f"http://127.0.0.1:{port}/{page}",
                                     budget_ms=90000, timeout=180)
            if not got["dom"]:
                # The virtual clock wedged on this page. Read it in real
                # time instead — the recorders publish their timeline and
                # the page hands back its own DOM. Same injected scripts,
                # same window size; only the clock differs.
                say("stage", f"{page}: virtual clock never settled — "
                             f"reading it in real time instead")
                rt = motion.capture_dom_realtime(
                    site, cfg.get("platform", "static"), page, tl_js,
                    wait_s=120)
                if rt.get("dom"):
                    got = {"dom": rt["dom"], "console": [],
                           "error": "", "mode": "realtime"}
                    if not rt.get("settled"):
                        say("warn", f"{page}: recorders had not finished "
                                    f"— motion may be under-captured")
                else:
                    say("error", f"{page} did not render — skipped")
                    continue
            timeline = {"entries": {}}
            tlm = re.search(r'(?is)<script[^>]*id="__ae_timeline"[^>]*>'
                            r'(.*?)</script\s*>', got["dom"])
            if tlm:
                try:
                    timeline = json.loads(tlm.group(1))
                except ValueError:
                    pass
            # STRIP OUR OWN CAPTURE ORIGIN.
            #
            # The DOM is read from a page served on an ephemeral local
            # port, and a runtime that rewrites links against
            # location.origin bakes that address into the markup:
            # Webflow's tab script turned href="#w-tabs-0" into
            # href="http://127.0.0.1:64802/index.html#w-tabs-0", which
            # shipped, 404'd for every visitor, and failed the referee.
            # Anything pointing at loopback is ours, never the template's.
            if got.get("dom"):
                got["dom"] = re.sub(r'https?://127\.0\.0\.1:\d+', '',
                                    got["dom"])
                got["dom"] = strip_instrumentation(got["dom"])
                if keep_runtime:
                    # only carry mode re-runs the platform's code, so
                    # only carry mode needs the inputs that code eats
                    got["dom"] = restore_consumed_scripts(
                        got["dom"],
                        (site / page).read_text(encoding="utf-8",
                                                errors="ignore"))
            if keep_runtime:
                # THE SOURCE HTML, NOT THE PHOTOGRAPH.
                #
                # Carry mode re-runs the platform's own code, which
                # rebuilds the page from scratch — so it needs the page
                # the platform shipped, not a snapshot of that page
                # mid-flight. Using the post-JS DOM baked in whatever
                # state each element happened to be in when the camera
                # fired: elements below the fold that had not revealed
                # yet kept opacity:0 forever (5 footer elements stayed
                # invisible), and a card stack that had already advanced
                # shipped starting from the wrong card. It also lost the
                # scripts the runtime consumes and gained the recorder we
                # injected. Every one of those problems is the snapshot,
                # and none of them exists if we ship the source.
                dom = (site / page).read_text(encoding="utf-8",
                                              errors="ignore")
                dom = re.sub(r'https?://127\.0\.0\.1:\d+', '', dom)
                n_scripts = 0
                dom, n_pre, n_link = strip_platform(dom, keep_preloads=True)
                n_ae = 0
            else:
                dom, n_scripts = strip_scripts(got["dom"])
                dom, n_pre, n_link = strip_platform(dom)
                dom, n_ae = recover_entrances(dom)
            total_ae += n_ae
            head, body, battrs, lang = split_document(dom)
            # The animation spec must come from the SOURCE page: by the
            # time the emitters see the DOM every script is stripped,
            # including the one carrying the definitions.
            source_html = (site / page).read_text(encoding="utf-8",
                                                   errors="ignore")
            spec = extract_appear_spec(source_html)
            engine = extract_motion_engine(source_html)
            engine_data = extract_engine_data(source_html)
            entrance = motion.entrance_spec(got["dom"])
            docs[page] = {"head": head, "body": body, "battrs": battrs,
                          "lang": lang, "spec": spec, "timeline": timeline,
                          "entrance": entrance,
                          "engine": engine, "engine_data": engine_data,
                          "keep_runtime": keep_runtime}
            if entrance.get("anims"):
                uncovered = sum(1 for a in entrance["anims"]
                                if not a.get("appear"))
                say("page", f"  {len(entrance['anims'])} animation(s) "
                            f"measured from the live runtime, {uncovered} of "
                            f"them outside the appear engine's reach")
            if spec["anims"]:
                say("page", f"  {len(spec['anims'])} animation(s) from the "
                            f"page's own spec; engine carried verbatim "
                            f"({sum(len(e) for e in engine)} bytes, "
                            f"{len(engine)} script(s))")
            say("page", f"{page}: {n_scripts} script(s) + {n_pre} preload(s) "
                        f"dropped, {n_link} platform link(s) cut, "
                        f"{n_ae} entrance(s) recovered, "
                        f"{len(forge._visible_text(dom))} chars carried")
    finally:
        srv.shutdown()
    if not docs:
        raise SystemExit("nothing rendered — cannot convert")

    # A SECOND pass, in real time. The capture above runs under a virtual
    # clock so --dump-dom knows when the page has settled, and that clock
    # makes rAF-driven motion unreadable: a badge rotating at 71.9 deg/s
    # measured 0.60 deg/s under it. So anything that animates itself
    # forever gets its own load, with real seconds, reporting back over
    # HTTP instead of through the DOM dump.
    if not keep_runtime:
        for page in list(docs):
            say("stage", f"measuring continuous motion on {page} "
                         f"(real time, ~20s)…")
            cap = motion.capture_realtime(
                site, cfg.get("platform", "static"), page,
                # The window must be at least TWICE the longest period
                # worth finding: a period is confirmed by matching the
                # series against a shifted copy of itself, so only lags
                # up to half the window can be tested. At 9s these same
                # three elements reported "no period found" — their real
                # cycle is 6s, which a 4.5s search ceiling cannot reach.
                motion.CONTINUOUS_JS % {"span": 15000, "delay": 3500,
                                        "post": "/__ae_capture"},
                wait_s=75)
            found = motion.analyse_continuous(cap)
            docs[page]["continuous"] = found
            if not found.get("available"):
                say("page", f"  continuous motion UNMEASURED "
                            f"({found.get('reason')}) — not proven absent")
                continue
            n_r, n_l = len(found["rotate"]), len(found["loop"])
            lost = (cap.get("unaddressable") or 0) + len(found["unhandled"])
            found["lost"] = lost
            skipped = cap.get("in_removed") or 0
            say("page", f"  {n_r} rotation(s) + {n_l} loop(s) reproduced"
                        + (f"; {lost} not reproduced (reported, not guessed)"
                           if lost else "")
                        + (f"; {skipped} inside removed platform content "
                           f"(correctly absent)" if skipped else ""))
            for u in found["unhandled"]:
                say("page", f"    UNHANDLED {u['id']}: {u['why']}")

            # Same real-time load requirement: counters rewrite their own
            # text and scroll effects are written from rAF, so neither is
            # visible under a virtual clock.
            say("stage", f"measuring counters + scroll-linked motion on "
                         f"{page} (real time)…")
            scap = motion.capture_realtime(
                site, cfg.get("platform", "static"), page,
                motion.SCROLLREC_JS % {"stops": 14, "settle": 420,
                                       "delay": 900,
                                       "post": "/__ae_capture",
                                       "max": 110000,
                                       # every observable property, not
                                       # the three that missed the cards
                                       "props": json.dumps(
                                           list(motion.WATCHED)),
                                       "owned": json.dumps(
                                           _entrance_owned(docs))},
                wait_s=120)
            scr = motion.analyse_scroll(scap, found)
            # One-shots found by the continuous pass ride the same replay
            # path as the scroll pass's: play once, at load, from their
            # recorded frames. Before the recorder saw the first frames
            # these simply did not exist, and fiber's progress bar and
            # loading reveal shipped frozen.
            if found.get("oneshot"):
                # DEDUPE BY ELEMENT. The runtime enforces one driver per
                # element and the FIRST claim wins, so appending blindly
                # let a thinner recording win over a better one: fiber's
                # loading reveal had a 9-frame entry from the scroll pass
                # and an 18-frame entry from this one, and the 9-frame
                # entry claimed the element and left it visibly static.
                merged = {}
                for o in (scr.get("oneshots") or []) + found["oneshot"]:
                    key = (o.get("sel"), o.get("idx"))
                    prev = merged.get(key)
                    if prev is None or len(o.get("frames") or []) > \
                            len(prev.get("frames") or []):
                        merged[key] = o
                scr["oneshots"] = list(merged.values())
                scr["available"] = True
            docs[page]["scroll"] = scr
            if not scr.get("available"):
                say("page", f"  counters/scroll UNMEASURED "
                            f"({scr.get('reason')}) — not proven absent")
            else:
                say("page", f"  {len(scr['counters'])} counter(s) and "
                            f"{len(scr['tracks'])} scroll-linked "
                            f"element(s) recorded")
                for c in scr["counters"]:
                    v = [f[1] for f in c["frames"]]
                    say("page", f"    {v[0]} -> {v[-1]} in {c['duration']}ms "
                                f"({len(v)} steps)")

            # Input-driven motion. Neither a timeline nor a scroll
            # position: the viewer's wheel is the input, so what is
            # recorded is the function and the port re-derives it.
            say("stage", f"measuring wheel-driven motion on {page} "
                         f"(real time)…")
            wcap = motion.capture_realtime(
                site, cfg.get("platform", "static"), page,
                motion.WHEELREC_JS % {"step": 600, "steps": 40,
                                      "settle": 700, "delay": 7000,
                                      "post": "/__ae_capture"},
                wait_s=150)
            wh = motion.analyse_wheel(wcap)
            docs[page]["wheel"] = wh
            if wh.get("available"):
                say("page", f"  wheel-driven: {len(wh['elements'])} "
                            f"element(s) over {len(wh['stops'])} stops, "
                            f"cycle {wh.get('cycle') or 'none (bounded)'}")
            else:
                say("page", f"  no wheel-driven motion "
                            f"({wh.get('reason')})")

    say("stage", f"emitting {framework}")
    EMITTERS[framework](dest, docs, cfg.get("name", "site"))
    # Copy EVERY asset directory the build ships, not just assets/.
    # A Webflow project keeps its localized files in remote-assets/, so
    # hardcoding "assets" copied 5 files out of 160 and the port quietly
    # fell back to the vendor's CDN for the rest.
    #
    # chunks/ and cms/ are the original RUNTIME's files: the content they
    # rendered is already baked into the carried HTML, so shipping them
    # is dead weight from a platform this port no longer depends on.
    SKIP_DIRS = set() if keep_runtime else {"chunks", "cms"}
    SKIP_FILES = {"serve.py", "README.txt", "DEPLOY.md", "_redirects",
                  "vercel.json", ".forge-probe.json", ".forge-report.json"}
    n_assets = 0
    for child in sorted(site.iterdir()):
        if child.name in SKIP_DIRS or child.name in SKIP_FILES:
            continue
        if child.is_dir():
            ignore = (None if keep_runtime else
                      shutil.ignore_patterns(*SKIP_DIRS, "*.mjs",
                                             "*.framercms"))
            shutil.copytree(child, dest / "public" / child.name,
                            ignore=ignore)
            n_assets += sum(1 for _ in (dest / "public" / child.name)
                            .rglob("*") if _.is_file())
        elif child.suffix.lower() not in (".html", ".py", ".md", ".txt",
                                          ".json"):
            shutil.copy2(child, dest / "public" / child.name)
            n_assets += 1
    say("stage", f"{n_assets} local asset(s) copied — no CDN, no platform")

    anim = _ship_animation_source(project, dest, say)

    # SHIPPING THE RUNTIME IS NOT WIRING IT — and this is the second
    # time that distinction has cost a release. The first time the
    # motion runtime was written to public/ and never REFERENCED, so
    # 181 entrances sat inert. This time it was referenced correctly
    # and there was NOTHING FOR IT TO DO: the entrance recorder
    # returned zero, convert reported success, and the port shipped
    # every element at its final pose with no motion at all. Nobody was
    # told, because nothing had failed.
    #
    # An entrance count of zero is not proof there are no entrances; it
    # is equally consistent with the recorder never having run. So it
    # is reported as a PROBLEM rather than passed over in silence, and
    # the caller is told which of the two it must check.
    if total_ae == 0 and not keep_runtime:
        # Only a problem on the path where entrance RECOVERY is the
        # mechanism. With keep_runtime the original's own engine is
        # carried instead, so zero recovered entrances is expected and
        # warning about it would cry wolf on every port — which is how
        # a check earns being ignored.
        say("warn",
            "NO ENTRANCE ANIMATIONS WERE RECOVERED. Either this design "
            "genuinely has none, or the recorder did not run — those "
            "look identical from here, and only one of them is fine. "
            "The port will render every element at its final pose. "
            "Check the original in a browser before shipping this.")

    result = {"ok": True, "dir": str(dest), "pages": list(docs),
              "entrances": total_ae, "assets": n_assets, "animations": anim,
              "motion_recovered": total_ae > 0}
    if not build:
        return result
    say("stage", "npm install…")
    r = subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                       cwd=dest, capture_output=True, text=True, timeout=1800)
    if r.returncode:
        say("stage", "retrying install without postinstall scripts…")
        r = subprocess.run(["npm", "install", "--ignore-scripts",
                            "--no-audit", "--no-fund"], cwd=dest,
                           capture_output=True, text=True, timeout=1800)
    if r.returncode and install:
        return {**result, "ok": False, "stage": "install",
                "log": (r.stdout + r.stderr)[-1500:]}
    say("stage", "building…")
    r = subprocess.run(["npm", "run", "build"], cwd=dest,
                       capture_output=True, text=True, timeout=1800)
    if r.returncode:
        return {**result, "ok": False, "stage": "build",
                "log": (r.stdout + r.stderr)[-2500:]}
    out = dest / OUTDIR[framework]
    say("stage", f"grading {out.name}/ against the original…")
    home_page = "index.html" if "index.html" in docs else want[0]
    g = subprocess.run([sys.executable, str(FORGE), "probe",
                        f"--page={home_page}", f"--against={out}"],
                       cwd=project, capture_output=True, text=True,
                       timeout=1800)
    verdict = [l for l in (g.stdout + g.stderr).splitlines()
               if l.startswith(("PASS ", "FAIL ", "       missing",
                                "       NOT OWNED", "         ",
                                "       the port"))]
    say("referee", "\n".join(verdict) or (g.stdout + g.stderr)[-800:])
    # Judge the PORT on the comparison, not on the probe's exit code:
    # that code also folds in the ORIGINAL template's own runtime health,
    # so a template that ships two console errors would condemn a
    # perfect port for a defect it faithfully inherited.
    graded = [l for l in verdict if l.startswith(("PASS ", "FAIL "))
              and "identical" in l]
    ok = bool(graded) and all(l.startswith("PASS ") for l in graded)

    # MOTION IS NOT GRADED BY THE REFEREE. It compares text, headings and
    # images, so it once stamped PIXEL-PERFECT on a build whose loading
    # counter never left 0 and whose every page therefore sat behind a
    # white cover. Content identical is a real result; it is not the same
    # claim as motion reproduced, and the two must not share a verdict.
    gaps = []
    for page, doc in docs.items():
        con, scr = doc.get("continuous") or {}, doc.get("scroll") or {}
        if con and not con.get("available"):
            gaps.append(f"{page}: continuous motion UNMEASURED "
                        f"({con.get('reason')}) — not proven absent")
        elif con.get("lost"):
            gaps.append(f"{page}: {con['lost']} continuous animation(s) "
                        f"seen but not reproduced")
        if scr and not scr.get("available"):
            gaps.append(f"{page}: counters/scroll UNMEASURED "
                        f"({scr.get('reason')}) — not proven absent")
    return {**result, "ok": ok, "stage": "graded", "motion_gaps": gaps,
            "out": str(out), "verdict": "\n".join(verdict)}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    fw = "astro"
    if "--framework" in argv:
        fw = argv[argv.index("--framework") + 1]
    pages = None
    if "--pages" in argv:
        pages = argv[argv.index("--pages") + 1].split(",")
    res = convert(Path(argv[0]).expanduser(), fw, pages,
                  build="--no-build" not in argv,
                  keep_runtime="--rebuild-motion" not in argv)
    print()
    gaps = res.get("motion_gaps") or []
    if not res.get("ok"):
        print("NOT ACCEPTED: " + res.get("dir", ""))
    elif gaps:
        print("CONTENT IDENTICAL, MOTION INCOMPLETE: " + res.get("dir", ""))
        for g in gaps[:12]:
            print("   " + g)
        if len(gaps) > 12:
            print(f"   … and {len(gaps) - 12} more")
    else:
        print("PIXEL-PERFECT PORT READY: " + res.get("dir", ""))
    if res.get("verdict"):
        print(res["verdict"])
    if res.get("log"):
        print(res["log"])
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

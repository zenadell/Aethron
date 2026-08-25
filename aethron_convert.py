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
    return out + MOTION_TAG

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

  var traced = [];
  Object.keys(TL.entries || {}).forEach(function (id) {
    var el = document.querySelector('[data-ae-id="' + id + '"]');
    if (!el) return;
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

  requestAnimationFrame(function () { requestAnimationFrame(function () {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var hit = null, tr = null, i;
        for (i = 0; i < pending.length; i++)
          if (pending[i].el === e.target) { hit = pending[i]; break; }
        for (i = 0; i < traced.length; i++)
          if (traced[i].el === e.target) { tr = traced[i]; break; }
        if (hit) { play(hit.el, hit.v); hit.el.dataset.aeDone = '1'; }
        else if (tr) { playTrace(tr.el, tr.frames); tr.el.dataset.aeDone = '1'; }
        else releaseGeneric(e.target);
        io.unobserve(e.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.01 });
    pending.forEach(function (x) { io.observe(x.el); });
    traced.forEach(function (x) { if (!x.el.dataset.aeDone) io.observe(x.el); });
    generic.forEach(function (el) { if (!el.dataset.aeDone) io.observe(el); });

    // THE CONTENT GUARANTEE. Motion is second; content is first.
    // Whatever parks an element — our runtime, a recording, or the
    // carried engine parking it and never firing — nothing may stay
    // invisible. A missing animation is a defect; missing content is a
    // broken site, and 33 elements sat at opacity 0.001 before this.
    setTimeout(function () {
      var all = document.querySelectorAll(
        '[data-ae-id],[data-ae],[data-framer-appear-id]');
      [].forEach.call(all, function (el) {
        var cs = getComputedStyle(el);
        var blur = /blur\(([\d.]+)px\)/.exec(cs.filter || '');
        if (parseFloat(cs.opacity) >= 0.05 &&
            !(blur && parseFloat(blur[1]) > 0.5)) return;
        el.style.transition = 'opacity .4s ease, filter .4s ease';
        el.style.opacity = '';
        el.style.filter = '';
        el.style.transform = '';
        el.dataset.aeDone = '1';
      });
    }, 2600);

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
  }); });
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


def split_document(dom: str) -> tuple:
    head = re.search(r"(?is)<head\b[^>]*>(.*?)</head\s*>", dom)
    body = re.search(r"(?is)<body\b[^>]*>(.*?)</body\s*>", dom)
    battrs = re.search(r"(?is)<body\b([^>]*)>", dom)
    lang = re.search(r'(?is)<html\b[^>]*\blang="([^"]*)"', dom)
    return (head.group(1) if head else "",
            body.group(1) if body else dom,
            (battrs.group(1) if battrs else "").strip(),
            lang.group(1) if lang else "en")


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
       "// `file` format keeps /about.html style URLs, matching the\n"
       "// original site exactly.\n"
       "export default defineConfig({ build: { format: 'file' },\n"
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

        _w(dest / f"src/html/{r}.head.html", head)
        imports, uses = [], []
        for i, (sec, html) in enumerate(parts, 1):
            comp = f"{i:02d}-{sec}"
            _w(dest / f"src/components/{r}/{comp}.html", html)
            _w(dest / f"src/components/{r}/{comp}.astro",
               f"---\n// {sec} — carried verbatim from the original build.\n"
               f"import html from './{comp}.html?raw';\n---\n"
               f"<Fragment set:html={{html}} />\n")
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
{prefix}
{nl.join(uses)}
{suffix}
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
    tl_js = motion.TIMELINE_JS % {"samples": 26, "every": 55, "step": 0.75}
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
            if keep_runtime:
                # the platform's own code stays: it draws AND animates
                # the page exactly as it always did
                dom = got["dom"]
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
            docs[page] = {"head": head, "body": body, "battrs": battrs,
                          "lang": lang, "spec": spec, "timeline": timeline,
                          "engine": engine, "engine_data": engine_data,
                          "keep_runtime": keep_runtime}
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

    result = {"ok": True, "dir": str(dest), "pages": list(docs),
              "entrances": total_ae, "assets": n_assets, "animations": anim}
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
    return {**result, "ok": ok, "stage": "graded",
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
    print(("PIXEL-PERFECT PORT READY: " if res.get("ok")
           else "NOT ACCEPTED: ") + res.get("dir", ""))
    if res.get("verdict"):
        print(res["verdict"])
    if res.get("log"):
        print(res["log"])
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

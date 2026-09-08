#!/usr/bin/env python3
"""Aethron Motion — capture what a page ACTUALLY does, then prove a port
does the same.

THE PROBLEM THIS SOLVES: everything else in the converter reads one
snapshot of the DOM. An entrance animation survives that because it has
exactly two states and both are in the markup. Nothing else does. A
marquee is a function of time. A card that rotates as you scroll is a
function of scroll position. One frame of a function tells you nothing
about the function — which is why a captured card froze at scale 1.25,
frame one of an animation, with no way to know it was frame one.

So motion is MEASURED instead:

    walk the page in steps ─┬─ record every element's state at each stop
                            │      -> the scroll-linked curves
                            └─ dwell and record again at each stop
                                   -> whatever moves on its own

TWO RULES THAT KEEP IT HONEST

1. DETECT BY OBSERVATION, NEVER BY CATALOGUE. This does not look for
   "a marquee" or "a counter". It samples transform, opacity, filter,
   colour, text, background-position, clip-path and size, and calls
   anything that changes animated. A detector that knows the names of
   animations will miss the one nobody has built yet; a detector that
   only knows "this pixel differs" cannot.

2. WHAT CANNOT BE CAPTURED MUST BE REPORTED, NEVER DROPPED. Random
   scrambles and cursor-following motion are not reproducible from
   outside — they depend on a seed or on input. Those are named in the
   report rather than silently shipped as a still image.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import forge  # noqa: E402

# Everything observable about an element. Adding a property here widens
# what can be detected — no other code needs to know about it.
WATCHED = ("transform", "opacity", "filter", "backdropFilter", "color",
           "backgroundColor", "backgroundPosition", "backgroundSize",
           "clipPath", "borderRadius", "width", "height", "left", "top",
           "letterSpacing", "strokeDashoffset", "maskPosition")

CAPTURE_JS = """
(function () {
  var W = %(watched)s;
  var STEP = %(step).2f, DWELL = %(dwell)d, SETTLE = %(settle)d;
  var out = { stops: [], meta: {} };
  var els = [].slice.call(document.querySelectorAll('*'));

  function path(el) {                  // a selector stable across builds
    var parts = [], n = el, depth = 0;
    while (n && n.nodeType === 1 && depth++ < 6) {
      var p = n.parentElement;
      if (!p) break;
      parts.unshift([].indexOf.call(p.children, n));
      n = p;
    }
    return parts.join('-');
  }
  function state(el) {
    var s = getComputedStyle(el), o = {};
    for (var i = 0; i < W.length; i++) {
      var v = s[W[i]];
      if (v && v !== 'none' && v !== 'auto' && v !== 'normal') o[W[i]] = v;
    }
    if (!el.children.length && el.textContent && el.textContent.trim())
      o.text = el.textContent.trim().slice(0, 40);
    return o;
  }
  function snapshot() { return els.map(state); }
  function differs(a, b) {
    for (var k in a) if (a[k] !== b[k]) return true;
    for (var k2 in b) if (a[k2] !== b[k2]) return true;
    return false;
  }

  var H = document.body.scrollHeight;
  var ys = [];
  for (var y = 0; y <= H; y += Math.round(innerHeight * STEP)) ys.push(y);
  var i = 0;

  function finish() {
    out.meta.elements = els.length;
    out.meta.height = H;
    out.meta.viewport = innerHeight;
    var tag = document.createElement('script');
    tag.type = 'application/json';
    tag.id = '__ae_motion';
    tag.textContent = JSON.stringify(out);
    document.body.appendChild(tag);
  }

  function step() {
    if (i >= ys.length) return finish();
    var y = ys[i++];
    scrollTo(0, y);
    setTimeout(function () {
      var a = snapshot();
      setTimeout(function () {
        var b = snapshot();          // same scroll position, later in time
        var moved = [];
        for (var k = 0; k < els.length; k++) {
          if (differs(a[k], b[k]))
            moved.push({ p: path(els[k]), from: a[k], to: b[k] });
        }
        out.stops.push({ y: y, at: a.map(function (s, k) {
          return Object.keys(s).length ? { p: path(els[k]), s: s } : null;
        }).filter(Boolean), moving: moved });
        step();
      }, DWELL);
    }, SETTLE);
  }
  step();
})();
"""


def _injecting_handler(site: Path, platform: str, script: str):
    """Serve the site with the capture script injected before </body>.

    Headless Chrome's --dump-dom cannot run our own JS, but it WILL run
    the page's. So the measurement rides in with the page and leaves its
    results in a DOM node, which the dump then hands back."""
    base = forge._site_handler(site, platform, quiet=True)

    class H(base):
        def send_head(self):
            path = self.translate_path(self.path.split("?")[0])
            if not path.endswith((".html", "/")) or not Path(path).is_file():
                return super().send_head()
            html = Path(path).read_text(encoding="utf-8", errors="ignore")
            # MARKED so it can be found and removed again. The capture
            # runs inside the page, and a carry-mode port keeps every
            # script it finds — so without a marker our own recorder
            # ships to the user, scrolls their page in steps taking
            # measurements nobody asked for, and fights the platform's
            # runtime for control of the same elements.
            tag = ('<script data-aethron-probe="1">' + script
                   + "</script>")
            html = (html.replace("</body>", tag + "</body>", 1)
                    if "</body>" in html else html + tag)
            body = html.encode("utf-8", "replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            import io
            return io.BytesIO(body)

    return H


def capture(site: Path, page="index.html", platform="static", step=0.6,
            dwell=600, settle=400, budget_ms=120000) -> dict:
    """-> {'stops': [...], 'meta': {...}} — the page's motion, measured."""
    browser = forge._find_browser()
    if not browser:
        raise SystemExit("motion capture needs a headless browser")
    js = CAPTURE_JS % {"watched": json.dumps(list(WATCHED)), "step": step,
                       "dwell": dwell, "settle": settle}
    handler = _injecting_handler(Path(site), platform, js)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = forge._render_page(
            browser, f"http://127.0.0.1:{srv.server_address[1]}/{page}",
            budget_ms=budget_ms, timeout=max(90, budget_ms // 1000 + 30))
    finally:
        srv.shutdown()
    m = re.search(
        r'(?is)<script[^>]*id="__ae_motion"[^>]*>(.*?)</script\s*>',
        got.get("dom") or "")
    if not m:
        return {"stops": [], "meta": {"error": "capture did not report — "
                                               "the page may not have run it"}}
    return json.loads(m.group(1))



# ─────────────── recording the curve, not the config ─────────────────
# THE GENERAL MECHANISM. Reading each component's props means learning
# Framer's internals one component at a time — Text Effect, Ticker,
# counters — and still missing whatever ships next year. Recording what
# the page ACTUALLY does covers all of them with one mechanism:
#
#   element -> [(t, {opacity, transform, filter}), ...]
#
# The stagger of a per-character heading does not need to be extracted:
# if every character is recorded separately, the stagger IS the data.
# A counter is text keyframes. A ticker is a long transform trace. None
# of them need a name.
#
# rAF does not fire reliably under headless virtual time; setTimeout
# does. That cost two failed capture attempts before it was pinned.

TIMELINE_JS = r"""
(function () {
  var SAMPLES = %(samples)d, EVERY = %(every)d, STEP = %(step).2f;
  var out = { entries: {}, meta: {} };

  // Paths, not references. Framer's runtime REPLACES DOM nodes as it
  // re-renders, so a held reference detaches and getComputedStyle
  // returns empty — which shows up as an element that "animates" from a
  // value to nothing. A first run reported 1040 of 3848 elements
  // animated; almost all of it was that artefact.
  function pathOf(el) {
    var parts = [], n = el, d = 0;
    while (n && n.parentElement && d++ < 14) {
      parts.unshift([].indexOf.call(n.parentElement.children, n));
      n = n.parentElement;
    }
    return parts.join('.');
  }
  function resolve(path) {
    // pathOf stops at <html> (no parentElement), so parts[0] is already
    // an index INTO documentElement. Starting at 1 skipped a level and
    // every lookup resolved to the wrong node — 1040 false positives
    // became 0 true ones.
    if (!path) return null;
    var n = document.documentElement, parts = path.split('.');
    for (var i = 0; i < parts.length; i++) {
      if (!n) return null;
      n = n.children[+parts[i]];
    }
    return n || null;
  }
  function read(el) {
    if (!el || !el.isConnected) return null;
    var s = getComputedStyle(el), o = {};
    if (s.opacity !== '' && s.opacity !== '1') o.opacity = s.opacity;
    if (s.transform && s.transform !== 'none') o.transform = s.transform;
    if (s.filter && s.filter !== 'none') o.filter = s.filter;
    if (!el.children.length) {
      var t = (el.textContent || '').trim();
      if (t && t.length <= 24) o.text = t;
    }
    return o;
  }
  function same(a, b) {
    if (!a || !b) return true;          // a detached read proves nothing
    for (var k in a) if (a[k] !== b[k]) return false;
    for (var j in b) if (a[j] !== b[j]) return false;
    return true;
  }

  var stamped = 0;
  var stops = [], H = document.body.scrollHeight;
  for (var y = 0; y <= H; y += Math.round(innerHeight * STEP)) stops.push(y);
  var si = 0;

  function atStop() {
    if (si >= stops.length) return finish();
    var y = stops[si++];
    scrollTo(0, y);
    setTimeout(function () {
      // candidates: anything currently displaced, faded or blurred —
      // i.e. anything that could be mid-animation. Bounded so the
      // sampling stays affordable.
      var cands = [];
      var all = document.querySelectorAll('*');
      for (var i = 0; i < all.length && cands.length < 500; i++) {
        var v = read(all[i]);
        if (v && (v.opacity !== undefined || v.transform || v.filter))
          cands.push(pathOf(all[i]));
      }
      var frames = [], n = 0, t0 = performance.now();
      (function tick() {
        var row = {};
        for (var c = 0; c < cands.length; c++)
          row[cands[c]] = read(resolve(cands[c]));
        frames.push([Math.round(performance.now() - t0), row]);
        if (++n < SAMPLES) setTimeout(tick, EVERY);
        else { collect(y, cands, frames); atStop(); }
      })();
    }, 120);
  }

  function collect(y, cands, frames) {
    for (var c = 0; c < cands.length; c++) {
      var p = cands[c], track = [], moved = false;
      for (var f = 0; f < frames.length; f++) {
        var v = frames[f][1][p];
        if (!v) continue;               // skip detached samples entirely
        if (track.length && !same(v, track[track.length - 1][1])) moved = true;
        track.push([frames[f][0], v]);
      }
      if (moved && track.length > 2 && !out.entries[p]) {
        // Stamp a STABLE id. Paths shift the moment conversion strips
        // scripts or deletes a badge, so the port must be keyed by
        // something that travels with the element, not by position.
        var el = resolve(p);
        if (el) {
          var id = el.getAttribute('data-ae-id');
          if (!id) { id = 'a' + (++stamped); el.setAttribute('data-ae-id', id); }
          out.entries[id] = { y: y, frames: track };
        }
      }
    }
  }

  function finish() {
    out.meta.stops = stops.length;
    out.meta.height = H;
    var tag = document.createElement('script');
    tag.type = 'application/json';
    tag.id = '__ae_timeline';
    tag.textContent = JSON.stringify(out);
    document.body.appendChild(tag);
  }
  atStop();
})();
"""


ENTRANCE_JS = r"""
(function () {
  // Framer's entrance animations are Web Animations, so the browser can
  // be asked what they are instead of being watched and guessed at. The
  // catch is that they are SHORT: a single getAnimations() call a second
  // after load returns 5 marquees and nothing else, because every
  // entrance has already finished and been collected. One measurement
  // of this page saw 1 animation at t=0, 19 at t=60ms, and 7 by t=140ms.
  // So accumulate — poll from the first frame and keep everything ever
  // seen, rather than sampling an instant and calling it an inventory.
  var seen = {}, out = { anims: [], meta: {} }, stamped = 0;

  function idOf(el) {
    var id = el.getAttribute('data-ae-id');
    if (!id) { id = 'e' + (++stamped); el.setAttribute('data-ae-id', id); }
    return id;
  }
  function num(v) { return typeof v === 'number' ? Math.round(v * 100) / 100
                                                 : v; }

  // A STAMPED ID NAMES AN ELEMENT IN A DOCUMENT NOBODY KEEPS.
  //
  // data-ae-id is written into the CAPTURE. A carried-runtime port
  // ships the SSR html instead, so every id lookup returned null and
  // 141 measured animations found nothing to play — the port shipped
  // 496 parked elements and no motion at all.
  //
  // So record WHERE the element is as well as what it is called:
  // the nearest ancestor carrying framer-* classes (which survive
  // hydration and are how the rest of this tool addresses elements),
  // plus a child-index path down from that anchor. Per-character
  // spans carry no classes of their own, which is exactly why the
  // class-only selectorOf used by the continuous pass is not enough
  // here.
  function classSel(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ''));
    var f = cls.split(/\s+/).filter(function (c) {
      return /^framer-[A-Za-z0-9]{4,}$/.test(c);
    });
    if (f.length) return '.' + f.join('.');
    var n = el.getAttribute && el.getAttribute('data-framer-name');
    if (n) return '[data-framer-name="' + n.replace(/"/g, '\\"') + '"]';
    return '';
  }
  function pathOf(el) {
    var steps = [], node = el, guard = 0;
    while (node && node.nodeType === 1 && guard++ < 12) {
      var sel = classSel(node);
      if (sel) {
        // Only trust an anchor that is unambiguous in the document.
        try {
          if (document.querySelectorAll(sel).length === 1)
            return steps.length ? sel + ' > ' + steps.join(' > ') : sel;
        } catch (e) { }
      }
      var parent = node.parentElement;
      if (!parent) break;
      var i = 1, sib = node;
      while ((sib = sib.previousElementSibling)) i++;
      steps.unshift('*:nth-child(' + i + ')');
      node = parent;
    }
    return '';
  }

  function collect() {
    var list;
    try { list = document.getAnimations(); } catch (e) { return; }
    for (var i = 0; i < list.length; i++) {
      var a = list[i], eff = a.effect;
      if (!eff || !eff.target || !eff.target.getAttribute) continue;
      var t, kf;
      try { t = eff.getComputedTiming(); kf = eff.getKeyframes(); }
      catch (e) { continue; }
      if (!kf || kf.length < 2) continue;
      var id = idOf(eff.target);
      // One element can carry several animations (opacity and transform
      // arrive as separate ones). Key on what the animation IS, so the
      // same one polled twice is not recorded twice.
      var props = Object.keys(kf[0]).concat(Object.keys(kf[kf.length - 1]))
        .filter(function (k) {
          return k !== 'offset' && k !== 'computedOffset' &&
                 k !== 'easing' && k !== 'composite';
        }).sort().join(',');
      var key = id + '|' + props + '|' + Math.round(t.duration || 0) +
                '|' + Math.round(t.delay || 0);
      if (seen[key]) continue;
      seen[key] = 1;
      var frames = [];
      for (var j = 0; j < kf.length; j++) {
        var f = {}, src = kf[j];
        for (var k in src) {
          if (k === 'composite' || k === 'computedOffset') continue;
          if (src[k] === null || src[k] === undefined) continue;
          f[k] = src[k];
        }
        if (f.offset === undefined || f.offset === null)
          f.offset = src.computedOffset;
        frames.push(f);
      }
      var own = null;
      try {
        var raw = eff.target.className;
        var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                          : (raw || ''));
        var fr2 = cls.split(/\s+/).filter(function (c) {
          return /^framer-[A-Za-z0-9]{4,}$/.test(c);
        });
        if (fr2.length) {
          var sel2 = '.' + fr2.join('.');
          var all2 = [].slice.call(document.querySelectorAll(sel2));
          var ix2 = all2.indexOf(eff.target);
          if (ix2 >= 0) own = sel2 + '|' + ix2;
        }
      } catch (e) { }
      if (own) (out.owned = out.owned || {})[own] = 1;
      out.anims.push({
        id: id, path: pathOf(eff.target),
        props: props, duration: num(t.duration),
        delay: num(t.delay), easing: t.easing,
        iterations: t.iterations === Infinity ? 'infinite' : t.iterations,
        direction: t.direction, fill: t.fill, frames: frames,
        appear: eff.target.hasAttribute('data-framer-appear-id')
      });
    }
  }

  var t0 = Date.now(), frames = 0;
  function tick() {
    collect();
    frames++;
    if (Date.now() - t0 < %(watch)d) requestAnimationFrame(tick);
  }
  tick();
  // rAF alone is not trustworthy under a virtual-time budget, so back it
  // with timers; both paths call the same idempotent collector.
  var every = setInterval(collect, 16);
  setTimeout(function () {
    clearInterval(every);
    collect();
    out.meta.watched_ms = Date.now() - t0;
    out.meta.frames = frames;
    out.meta.total = out.anims.length;
    var tag = document.createElement('script');
    tag.type = 'application/json';
    tag.id = '__ae_entrance';
    tag.textContent = JSON.stringify(out);
    document.body.appendChild(tag);
  }, %(watch)d + 120);
})();
"""


SCROLL_JS = r"""
(function () {
  // The third mechanism. getAnimations() sees Web Animations; the
  // timeline sampler sees things that move on their own clock. Neither
  // sees an effect that framer-motion drives on requestAnimationFrame
  // from the scroll position — useScroll/useTransform writes inline
  // style directly, so there is no animation object to read and no
  // timeline to replay. Measured on one page, twelve elements moved in
  // the original and in neither recording: a sticky header, several
  // scroll-linked transforms, and three counters.
  //
  // Two things are recorded here, because two different things are
  // happening:
  //   tracks — style as a function of the element's OWN progress
  //            through the viewport, which is exactly the quantity
  //            useScroll({target}) computes;
  //   text   — elements whose TEXT changes once, when first seen. A
  //            counter animates by rewriting itself, so no style read
  //            will ever notice it.
  //
  // Progress, not scrollY: the port legitimately differs in height (it
  // deletes badges and marketplace promos), so absolute positions do
  // not carry across. Progress does.
  var STOPS = %(stops)d, HOLD = %(hold)d, EVERY = %(every)d;
  var out = { tracks: [], text: [], meta: {} }, stamped = 0;

  function idOf(el) {
    var id = el.getAttribute('data-ae-id');
    if (!id) { id = 'e' + (++stamped); el.setAttribute('data-ae-id', id); }
    return id;
  }
  function progressOf(el) {
    var r = el.getBoundingClientRect();
    var p = (innerHeight - r.top) / (innerHeight + r.height);
    return Math.max(0, Math.min(1, p));
  }
  function styleOf(el) {
    var s = getComputedStyle(el);
    return [s.transform === 'none' ? '' : s.transform,
            s.opacity === '1' ? '' : s.opacity,
            s.filter === 'none' ? '' : s.filter];
  }
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && (r.width || r.height);
  }
  // a leaf whose text carries digits is a counter candidate — but a
  // CLOCK is live data, not an animation. One capture recorded
  // '15:22:53' -> '15:22:54' and would have baked a frozen fake time
  // into the port.
  function ownText(el) {
    var s = '', c = el.childNodes;
    for (var i = 0; i < c.length; i++) {
      if (c[i].nodeType === 3) { s += c[i].nodeValue; }
    }
    return s.trim();
  }
  function counterValue(el) {
    // THE NUMBER IS NOT ALWAYS THE ELEMENT'S WHOLE CONTENT. fiber's
    // preloader writes it as an element's OWN text node sitting beside
    // a child span holding the unit sign: children.length is 1, so a
    // childless-only test rejected the digits, while the unit span is a
    // leaf with no digit and was rejected too. The counter was not
    // mis-measured, it was structurally invisible — and the port
    // shipped it frozen at zero behind a full-screen cover while every
    // file-level check passed. Read an element's own text nodes.
    return el.children.length ? ownText(el) : (el.textContent || '').trim();
  }
  function counterish(el) {
    var t = counterValue(el);
    if (!t || t.length > 16 || !/\d/.test(t)) return false;
    if (/^\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?$/i.test(t)) return false;
    if (/^\d{1,2}\/\d{1,2}(\/\d{2,4})?$/.test(t)) return false;
    return true;
  }

  // Settle the Web Animations BEFORE measuring scroll, deterministically
  // rather than by waiting. A first run recorded 158 "scroll tracks"
  // whose top entries were the hero characters sitting at
  // matrix(1,0,0,1,0,10) — the PARKED entrance pose. Under a virtual
  // time budget the animation clock barely advances, so no amount of
  // waiting finishes an entrance; asking each animation to finish does.
  // Marquees never finish (infinite), so they are pinned at frame 0
  // instead, which stops them adding noise to every sample.
  function settleAnimations() {
    var list = [];
    try { list = document.getAnimations() || []; } catch (e) { return; }
    for (var i = 0; i < list.length; i++) {
      var a = list[i], ct = {};
      try { ct = a.effect.getComputedTiming(); } catch (e) { }
      try {
        if (ct.iterations === Infinity || ct.iterations > 1e6) {
          a.pause(); a.currentTime = 0;
        } else {
          a.finish();
        }
      } catch (e) { }
    }
  }

  var all = [].slice.call(document.querySelectorAll('*')).filter(function (el) {
    return el.getClientRects().length;
  });
  var samples = {}, texts = {}, watched = {}, firedText = {};

  function record() {
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!el.isConnected) continue;
      var st = styleOf(el);
      if (!st[0] && !st[1] && !st[2]) continue;   // nothing set: skip
      var id = idOf(el);
      (samples[id] = samples[id] || []).push(
        [Math.round(progressOf(el) * 1000) / 1000, st[0], st[1], st[2]]);
    }
  }

  // Catch a counter at the moment it first appears: hold the scroll and
  // sample its text. Scrolling back later cannot work — these fire once
  // (useInView with once), so a second look shows only the final value,
  // which is exactly why the port shipped them frozen.
  function catchText(done) {
    var fresh = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!el.isConnected || firedText[idOf(el)]) continue;
      if (!counterish(el) || !visible(el)) continue;
      firedText[idOf(el)] = 1;
      fresh.push(el);
    }
    if (!fresh.length) { done(); return; }
    // Time is recorded on the ANIMATION timeline, not the wall clock.
    // Headless runs under a virtual time budget where the two diverge
    // badly — measured, document.timeline advanced 289ms while
    // setTimeout advanced 1800ms. A hold measured in wall-clock ms
    // therefore captured a couple of hundred ms of a one second count
    // and produced two frames: the start and the end, with the whole
    // animation missing in between. Holding until the TIMELINE has
    // moved far enough is correct in both worlds.
    function now() {
      var t = null;
      try { t = document.timeline.currentTime; } catch (e) { }
      return typeof t === 'number' ? t : Date.now();
    }
    var t0 = now(), wall0 = Date.now();
    fresh.forEach(function (el) { texts[idOf(el)] = []; });
    (function tick() {
      var dt = now() - t0;
      fresh.forEach(function (el) {
        var id = idOf(el), arr = texts[id];
        var v = (el.textContent || '').trim();
        if (!arr.length || arr[arr.length - 1][1] !== v)
          arr.push([Math.round(dt), v]);
      });
      // stop on timeline progress, with a wall-clock backstop so a
      // frozen timeline can never hang the capture
      if (dt < HOLD && Date.now() - wall0 < HOLD * 12) setTimeout(tick, EVERY);
      else done();
    })();
  }

  var H = Math.max(document.body.scrollHeight,
                   document.documentElement.scrollHeight);
  var step = Math.max(1, Math.floor((H - innerHeight) / Math.max(1, STOPS - 1)));
  var at = 0, n = 0;

  function stop() {
    window.scrollTo(0, at);
    setTimeout(function () {
      record();
      catchText(function () {
        record();                       // again: text hold moved time on
        n++; at += step;
        if (n < STOPS) { setTimeout(stop, 60); return; }
        finish();
      });
    }, 70);
  }

  var done = false;
  function finish() {
    // Idempotent and watchdogged. A capture that runs out of budget
    // mid-walk must still hand back what it has: the first version
    // emitted nothing at all, and "no tag" is indistinguishable from
    // "nothing animates" — a silent, total loss of the measurement.
    if (done) return;
    done = true;
    window.scrollTo(0, 0);
    for (var id in samples) {
      var rows = samples[id];
      var distinct = {};
      for (var i = 0; i < rows.length; i++)
        distinct[rows[i][1] + '|' + rows[i][2] + '|' + rows[i][3]] = 1;
      if (Object.keys(distinct).length < 2) continue;   // never moved
      rows.sort(function (a, b) { return a[0] - b[0]; });
      out.tracks.push({ id: id, samples: rows });
    }
    for (var tid in texts) {
      if ((texts[tid] || []).length > 1)
        out.text.push({ id: tid, frames: texts[tid] });
    }
    out.plays = plays;
    out.height = H;
    out.meta = { stops: n, of: STOPS, height: H,
                 waapi_owned: Object.keys(waapiOwned).length,
                 tracked: out.tracks.length, counters: out.text.length,
                 complete: n >= STOPS - 1 };
    var tag = document.createElement('script');
    tag.type = 'application/json';
    tag.id = '__ae_scroll';
    tag.textContent = JSON.stringify(out);
    document.body.appendChild(tag);
  }
  setTimeout(function () { settleAnimations(); stop(); }, %(settle)d);
  setTimeout(finish, %(max)d);          // watchdog: always emit something
})();
"""


CONTINUOUS_JS = r"""
(function () {
  // The animations nothing else can see: driven by requestAnimationFrame
  // writing inline style, on their own clock, forever. A rotating badge
  // is the classic one — no Web Animation to read, no entrance to
  // recover, no scroll position to key on. Measured on one page:
  // transform:rotate(Ndeg) advancing 71.86 deg/s, a five second turn.
  //
  // This MUST run in real time. Under a virtual time budget the same
  // element measured 0.60 deg/s — 120x slow, and not off by any
  // constant that could be corrected for, because the rAF loop is
  // starved relative to the clock. So the result is posted back to the
  // server rather than waited for by --dump-dom.
  // Start AFTER the entrances are over. An entrance is a one-shot that
  // also changes style every frame, so sampling from t=0 records
  // matrix(1,0,0,1,0,40) -> '' and calls a finished slide a continuous
  // animation. Waiting is legitimate here because this pass runs in
  // real time — unlike under a virtual clock, entrances actually finish.
  var SPAN = %(span)d, DELAY = %(delay)d, POST = "%(post)s";
  var els = [].slice.call(document.querySelectorAll('*')).filter(function (el) {
    return el.getClientRects().length;
  });
  var seen = {}, stamped = 0;

  function idOf(el) {
    var id = el.getAttribute('data-ae-id');
    if (!id) { id = 'c' + (++stamped); el.setAttribute('data-ae-id', id); }
    return id;
  }
  // This pass is a SEPARATE page load from the one the port is built
  // from — it has to be, because it needs real time and that load needs
  // a virtual clock. So a stamped id is useless here: it would name an
  // element in a document nobody keeps. Address them the way the rest
  // of the tool does, by their framer-* classes.
  function selectorOf(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ''));
    var framer = cls.split(/\s+/).filter(function (c) {
      return /^framer-[A-Za-z0-9]{4,}$/.test(c);
    });
    if (framer.length) return '.' + framer.join('.');
    var name = el.getAttribute('data-framer-name');
    if (name) return '[data-framer-name="' + name.replace(/"/g, '\\"') + '"]';
    return '';
  }
  // Content the PORT deliberately deletes: marketplace promos and
  // platform badges. Motion inside them is not a gap — reporting it as
  // one sends somebody hunting for an animation that is correctly
  // absent. One carousel cost exactly that detour before this existed.
  function inRemovedContent(el) {
    for (var n = el; n; n = n.parentElement) {
      if (n.tagName === 'A') {
        var href = n.getAttribute('href') || '';
        var promo = ['framer.com/r/badge', 'tab=marketplace',
                     'framer.com/marketplace', 'buy.polar.sh',
                     'lemonsqueezy.com', 'gumroad.com'];
        for (var k = 0; k < promo.length; k++)
          if (href.indexOf(promo[k]) >= 0) return true;
      }
      var c = String(n.className || '');
      if (/w-webflow-badge|__framer-badge/.test(c)) return true;
      if (n.id === '__framer-badge-container') return true;
    }
    return false;
  }
  function locate(el) {
    var sel = selectorOf(el);
    if (!sel) return null;
    var all;
    try { all = [].slice.call(document.querySelectorAll(sel)); }
    catch (e) { return null; }
    var idx = all.indexOf(el);
    return idx < 0 ? null : { sel: sel, idx: idx, of: all.length };
  }
  function waapi(el) {
    try { return (el.getAnimations() || []).length > 0; } catch (e) { return false; }
  }
  function read(el) {
    // SIZE COUNTS. Framer reveals a loading screen by collapsing its
    // height 813px -> 2px, using a scaleY transform only to smooth the
    // reflow. Watching transform/opacity/filter alone recorded the
    // smoothing and missed the animation: the port kept the panel at
    // full height forever, leaving it stranded across the page.
    var s = getComputedStyle(el);
    return [s.transform === 'none' ? '' : s.transform,
            s.opacity === '1' ? '' : s.opacity,
            s.filter === 'none' ? '' : s.filter,
            parseFloat(s.width) + 'x' + parseFloat(s.height)];
  }

  var sizeBase = {};
  var t0 = 0, n = 0;
  // START AT THE FIRST FRAME, NOT AFTER THE DELAY.
  //
  // This waited DELAY (3500ms) before it began, so that entrance
  // animations would not be misread as continuous motion. On a template
  // with an intro sequence that is not a safeguard, it is a blindfold:
  // fiber's preloader — progress bar, counter, and the cover lifting
  // away — is over before the recorder opens its eyes, and all three
  // shipped frozen. DELAY is now a boundary used at ANALYSIS time to
  // tell a one-shot from a loop, which is what it was always for.
  setTimeout(function () { t0 = performance.now(); tick(); }, 120);
  function tick() {
    var dt = performance.now() - t0;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el.isConnected || waapi(el)) continue;   // WAAPI is handled
      var v = read(el);
      var id = idOf(el);
      // AN ELEMENT WITH NO TRANSFORM IS NOT A STILL ELEMENT.
      //
      // Skipping anything without transform/opacity/filter dropped the
      // loading panel entirely: at load it has none of the three, so it
      // was ignored until 3.1s — by which time it had already collapsed
      // 813px -> 2px, and the recording opened on the END of its
      // animation. The port then held that pose and left the panel
      // stranded across the page.
      //
      // So remember every element's size the first time it is seen, and
      // start recording when EITHER a visual property appears OR its
      // size moves off that baseline — replaying the baseline first, so
      // the animation still has a beginning.
      var base = sizeBase[id];
      if (base === undefined) { sizeBase[id] = base = v[3]; }
      var sized = v[3] !== base;
      if (!v[0] && !v[1] && !v[2] && !sized) continue;
      var rec = seen[id] || (seen[id] = { rows: [], el: el });
      if (!rec.rows.length && sized) {
        rec.rows.push([Math.round(dt), '', '', '', base]);
      }
      var last = rec.rows[rec.rows.length - 1];
      if (!last || last[1] !== v[0] || last[2] !== v[1] ||
          last[3] !== v[2] || last[4] !== v[3])
        rec.rows.push([Math.round(dt), v[0], v[1], v[2], v[3]]);
    }
    n++;
    if (dt < SPAN) { requestAnimationFrame(tick); return; }

    var out = { spans: SPAN, frames: n, items: [], unaddressable: 0,
                in_removed: 0 };
    for (var id in seen) {
      var rec = seen[id];
      if (rec.rows.length < 3) continue;      // one change is not a loop
      if (rec.el && inRemovedContent(rec.el)) { out.in_removed++; continue; }
      var where = rec.el ? locate(rec.el) : null;
      if (!where) { out.unaddressable++; continue; }
      out.items.push({ id: id, sel: where.sel, idx: where.idx,
                       // An element the original REMOVES stops being
                       // sampled mid-animation, so its last recorded
                       // value is not its end state. Replaying that as a
                       // final pose froze fiber's loading mask across the
                       // page. Say whether it was still there at the end.
                       gone: !(rec.el && rec.el.isConnected),
                       startedAt: rec.rows[0][0],
                       of: where.of, rows: rec.rows });
    }
    try {
      fetch(POST, { method: 'POST',
                    body: JSON.stringify(out) });
    } catch (e) { }
  }
})();
"""


def capture_realtime(site: Path, platform: str, page: str, script: str,
                     wait_s: int = 25, win: str = "1440,2400") -> dict:
    """Run a page in REAL time and let it post its findings back.

    --dump-dom needs --virtual-time-budget to know when the page is
    settled, and that budget is exactly what makes rAF-driven motion
    unmeasurable. So this drops the budget, lets real time pass, and
    takes the result over HTTP instead of over stdout.

    `win` matters more than it looks. The 1440x2400 default puts as much
    of a page in view as possible, but a page SHORTER than the window
    cannot scroll at all: fiber measures 2313px tall against a 2313px
    viewport — scrollable distance ZERO — so every scroll-driven reading
    came back empty, and the port shipped with its scroll motion dead,
    while a viewer at 900px has 1400px of scroll. Measure scroll-linked
    motion at a viewport a person would actually use."""
    import http.server
    import subprocess
    import tempfile
    import threading
    browser = forge._find_browser()
    if not browser:
        return {"available": False, "reason": "no browser"}

    got, ready = {}, threading.Event()
    base = _injecting_handler(site, platform, script)

    class H(base):
        def do_POST(self):
            if self.path == "/__ae_capture":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    got.update(json.loads(self.rfile.read(n) or b"{}"))
                except Exception:
                    pass
                self.send_response(204)
                self.end_headers()
                ready.set()
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    tmp = tempfile.mkdtemp(prefix="forge-rt-")
    proc = None
    try:
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--mute-audio",
             f"--window-size={win}", "--hide-scrollbars",
             f"--user-data-dir={tmp}/profile",
             # the point of this runner: NO virtual time budget
             f"http://127.0.0.1:{port}/{page}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready.wait(wait_s)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    if not got:
        return {"available": False,
                "reason": f"the page reported nothing within {wait_s}s"}
    got["available"] = True
    return got



DOM_POST_JS = r"""
(function () {
  // Hand the finished DOM back over HTTP instead of over stdout.
  //
  // --dump-dom needs a virtual clock to know when a page has settled,
  // and that clock can wedge: fiber's desktop variant renders in 3s of
  // real time and produced NOTHING under every budget tried, 3s through
  // 90s. Dropping the budget fixes the render but moves the dump to the
  // load event, which is too early here — the recorders need their full
  // watch window before they publish. So wait for the timeline they
  // write, then post the DOM ourselves.
  var POST = "%(post)s", DEADLINE = Date.now() + %(max)d, sent = false;
  function send() {
    if (sent) { return; }
    sent = true;
    try {
      fetch(POST, {
        method: 'POST',
        body: JSON.stringify({
          dom: document.documentElement.outerHTML,
          waited: Date.now() - START,
          settled: !!document.getElementById('__ae_timeline')
        })
      });
    } catch (e) { }
  }
  var START = Date.now();
  (function poll() {
    if (document.getElementById('__ae_timeline') || Date.now() > DEADLINE) {
      send();
      return;
    }
    setTimeout(poll, 200);
  })();
})();
"""


def capture_dom_realtime(site: Path, platform: str, page: str,
                         script: str = "", wait_s: int = 120) -> dict:
    """The post-JS DOM, read in real time. -> {dom, settled, available}.

    Same contract as forge._render_page's `dom`, for the pages where the
    virtual clock never expires. `settled` is False when the recorders
    had not published within the window: the DOM is still real, but it
    is a weaker measurement and the caller should say so rather than
    quietly treat it as equivalent."""
    js = script + DOM_POST_JS % {"post": "/__ae_capture",
                                 "max": max(1, wait_s - 15) * 1000}
    got = capture_realtime(site, platform, page, js, wait_s=wait_s)
    if not got.get("available"):
        return got
    return {"available": True, "dom": got.get("dom") or "",
            "settled": bool(got.get("settled")),
            "waited_ms": got.get("waited")}


WHEELREC_JS = r"""
(function () {
  // INPUT-DRIVEN MOTION. Some templates animate from the wheel rather
  // than from time or from window scroll. fiber's hero is a 3D layer
  // scroller: its component accumulates deltaY into a motion value,
  // springs it, and derives every layer's translateZ, scale, opacity,
  // blur and z-index from that one number. Nothing about it is a
  // timeline, so an entrance or continuous recorder captures nothing at
  // all and the port ships the whole hero inert.
  //
  // But it IS a function. Drive the accumulator to a value, let the
  // spring settle, and the layer states are reproducible to within the
  // settling error. So sweep the input, sample the result, and ship
  // f(input) — the port re-derives it from the viewer's own wheel.
  //
  // The listener is bound to the component's OWN container with
  // {passive:false}; wheel bubbles upward, so events sent to window or
  // document never reach it. Dispatch on the container.
  var STEP = %(step)d, STEPS = %(steps)d, SETTLE = %(settle)d,
      POST = "%(post)s";
  var out = { container: null, containerVia: null, step: STEP,
              samples: [], watched: [], available: false, reason: "" };

  function selectorOf(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ''));
    var f = cls.split(/\s+/).filter(function (c) {
      return /^framer-[A-Za-z0-9]{4,}$/.test(c) || c === 'motion-layer';
    });
    if (f.length) { return '.' + f.join('.'); }
    var n = el.getAttribute('data-framer-name');
    return n ? '[data-framer-name="' + n.replace(/"/g, '\\"') + '"]' : '';
  }
  function indexOf(el, sel) {
    var all = document.querySelectorAll(sel);
    for (var i = 0; i < all.length; i++) { if (all[i] === el) { return i; } }
    return -1;
  }
  function readEl(el) {
    var s = getComputedStyle(el);
    return [s.transform === 'none' ? '' : s.transform,
            (+s.opacity).toFixed(3),
            s.filter === 'none' ? '' : s.filter,
            s.zIndex];
  }
  function snapshot(list) {
    var m = [];
    for (var i = 0; i < list.length; i++) { m.push(readEl(list[i])); }
    return m;
  }
  function differs(a, b) {
    for (var i = 0; i < a.length; i++) {
      for (var j = 0; j < a[i].length; j++) {
        if (a[i][j] !== b[i][j]) { return true; }
      }
    }
    return false;
  }
  function wheel(el, dy, times) {
    for (var i = 0; i < times; i++) {
      el.dispatchEvent(new WheelEvent('wheel', {
        deltaY: dy, deltaMode: 0, bubbles: true, cancelable: true }));
    }
  }

  function finish(reason) {
    out.reason = reason || "";
    try { fetch(POST, { method: 'POST', body: JSON.stringify(out) }); }
    catch (e) { }
  }

  setTimeout(function () {
    // 1. Which container responds to a wheel? Test the plausible ones:
    //    an element large enough to be a stage, with enough children to
    //    be worth animating. Smallest responder wins, so we bind to the
    //    component itself and not to some ancestor that merely contains
    //    it.
    var all = document.querySelectorAll('div, section, main');
    var cands = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i], r = el.getBoundingClientRect();
      if (el.children.length >= 3 && r.width >= innerWidth * 0.5 &&
          r.height >= innerHeight * 0.4) { cands.push(el); }
    }
    if (!cands.length) { finish("no candidate container"); return; }

    var everything = document.querySelectorAll('*');
    var probe = [];
    for (var k = 0; k < everything.length; k++) {
      var e2 = everything[k];
      if (e2.getClientRects().length && selectorOf(e2)) { probe.push(e2); }
    }
    var before = snapshot(probe), winner = null;

    (function tryNext(ci) {
      if (ci >= cands.length) {
        if (!winner) { finish("nothing responded to a wheel"); return; }
        return;
      }
      var c = cands[cands.length - 1 - ci];        // smallest first
      wheel(c, STEP, 4);
      setTimeout(function () {
        var after = snapshot(probe);
        if (differs(before, after)) {
          winner = c;
          wheel(c, -STEP, 4);                       // put it back
          setTimeout(function () { sweep(c, probe); }, SETTLE);
        } else {
          tryNext(ci + 1);
        }
      }, 420);
    })(0);

    // 2. Sweep the input and record the result at each stop.
    function sweep(c, probe) {
      out.container = selectorOf(c);
      if (!out.container) {
        // The component's own container often carries no class at all.
        // Identify it by a child that does: the port resolves it as
        // "the parent of the first X", which survives a rebuild.
        for (var q = 0; q < c.children.length; q++) {
          var cs = selectorOf(c.children[q]);
          if (cs) { out.containerVia = cs; break; }
        }
      }
      var base = snapshot(probe);
      // only elements that actually move are worth shipping
      var moving = [], movingEls = [];
      wheel(c, STEP, 8);
      setTimeout(function () {
        var mid = snapshot(probe);
        for (var i = 0; i < probe.length; i++) {
          var d = false;
          for (var j = 0; j < base[i].length; j++) {
            if (base[i][j] !== mid[i][j]) { d = true; }
          }
          if (d) {
            var sel = selectorOf(probe[i]);
            var idx = indexOf(probe[i], sel);
            if (idx >= 0) { moving.push({ sel: sel, idx: idx });
                            movingEls.push(probe[i]); }
          }
        }
        wheel(c, -STEP, 8);
        out.watched = moving;
        if (!moving.length) { finish("container responded but nothing moved");
                              return; }
        setTimeout(function () {
          var n = 0, acc = 0;
          (function stop() {
            out.samples.push({ d: acc, states: snapshot(movingEls) });
            if (++n > STEPS) { out.available = true; finish(""); return; }
            wheel(c, STEP, 1);
            acc += STEP;
            setTimeout(stop, SETTLE);
          })();
        }, SETTLE);
      }, 420);
    }
  }, %(delay)d);
})();
"""


def analyse_wheel(capture: dict) -> dict:
    """Turn a wheel sweep into a replayable f(input) spec.

    The port cannot replay this as a timeline — the viewer decides how
    far and how fast it goes — so what ships is the sampled function
    plus the container to bind to. Elements that never moved are
    dropped: shipping a constant is just weight."""
    if not capture.get("available"):
        return {"available": False,
                "reason": capture.get("reason") or "no wheel capture"}
    watched = capture.get("watched") or []
    samples = capture.get("samples") or []
    if len(samples) < 3 or not watched:
        return {"available": False, "reason": "sweep too short"}

    # keep only elements whose state actually varies across the sweep
    keep, tracks = [], []
    for i, w in enumerate(watched):
        series = [s["states"][i] for s in samples if i < len(s["states"])]
        if len(series) < 3:
            continue
        if all(v == series[0] for v in series):
            continue
        keep.append(w)
        tracks.append(series)

    if not keep:
        return {"available": False, "reason": "nothing varied across the sweep"}

    stops = [s["d"] for s in samples]
    cyc = _wheel_cycle(stops, tracks)
    return {"available": True,
            "container": capture.get("container"),
            "container_via": capture.get("containerVia"),
            "step": capture.get("step"),
            "cycle": cyc,
            "stops": stops,
            "elements": [{"sel": w["sel"], "idx": w["idx"],
                          "states": tracks[n],
                          # intervals the element WRAPS across. Without
                          # these the port glides 1.4 -> 0.6 where the
                          # original snaps, and that one layer reads 127%
                          # wrong while every other layer is within 3%.
                          "jumps": _wrap_intervals(tracks[n])}
                         for n, w in enumerate(keep)]}


def _wrap_intervals(states):
    """Indices of sample intervals containing a discontinuity."""
    import re as _re
    import statistics as _st

    def first_num(s):
        m = _re.match(r"[a-z3d]*\(\s*(-?\d+\.?\d*)", s or "")
        return float(m.group(1)) if m else None

    vals = [first_num(s[0]) for s in states]
    if any(v is None for v in vals) or len(vals) < 4:
        return []
    diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
    nz = [abs(d) for d in diffs if d]
    if not nz:
        return []
    typical = _st.median(nz)
    return [i for i, d in enumerate(diffs) if abs(d) > typical * 5]


def _wheel_cycle(stops, tracks):
    """The period of an infinite scroller, in input units, or None.

    NOT by autocorrelation. Mean-difference autocorrelation on a slowly
    varying sweep is minimised by the SMALLEST lag — consecutive samples
    are nearly identical — so it confidently returned 600 for a cycle
    measured at 9600. (The same trap, in a different shape, once fitted
    a 3s carousel at 240ms.)

    Measure what is actually visible instead. Each layer ramps steadily
    and then jumps back: fiber's layer 0 climbs 0.646 -> 1.396 and wraps.
    One wrap is enough — the period is the full travel divided by the
    per-unit slope, so a sweep only has to be long enough to show the
    range, not long enough to repeat."""
    import re as _re
    import statistics as _st

    def first_num(state):
        m = _re.match(r"[a-z3d]*\(\s*(-?\d+\.?\d*)", state or "")
        return float(m.group(1)) if m else None

    step = (stops[-1] - stops[0]) / max(1, len(stops) - 1)
    if step <= 0:
        return None
    # PREFERRED: the spacing between wraps, measured directly. A layer
    # that reaches the front reappears at the back, and that jump is
    # unmistakable — the wraps landed at 10800, 20400, 30000, 39600 and
    # 49200, i.e. every 9600 exactly. This needs a sweep long enough to
    # show two of them; the ramp estimate below covers the case where it
    # is not, at about 9% error.
    spacings = []
    for tr in tracks:
        vals = [first_num(s[0]) for s in tr]
        vals = [v for v in vals if v is not None]
        if len(vals) < 8:
            continue
        diffs = [vals[k] - vals[k - 1] for k in range(1, len(vals))]
        nz = [abs(d) for d in diffs if d]
        if not nz:
            continue
        typical = _st.median(nz)
        jumps = [k for k, d in enumerate(diffs) if abs(d) > typical * 5]
        for a, b in zip(jumps, jumps[1:]):
            spacings.append((b - a) * step)
    if len(spacings) >= 3:
        cyc = _st.median(spacings)
        if _st.median([abs(s - cyc) for s in spacings]) < cyc * 0.1:
            return int(round(cyc / step) * step)

    ests = []
    for tr in tracks:
        vals = [first_num(s[0]) for s in tr]
        vals = [v for v in vals if v is not None]
        if len(vals) < 6:
            continue
        diffs = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
        small = [abs(d) for d in diffs if d]
        if not small:
            continue
        typical = _st.median(small)
        # the ramp: differences that are not the wrap-around jump
        ramp = [d for d in diffs if abs(d) <= typical * 4]
        if len(ramp) < 4:
            continue
        slope = abs(_st.median(ramp))
        span = max(vals) - min(vals)
        if slope <= 0 or span <= 0:
            continue
        ests.append(span / slope * step)
    if len(ests) < 3:
        return None
    cyc = _st.median(ests)
    spread = _st.median([abs(e - cyc) for e in ests])
    # agreement across independent layers is the check that this is a
    # real period and not one element's noise
    if cyc <= step or spread > cyc * 0.25:
        return None
    return int(round(cyc / step) * step)


MATRIX_RE = re.compile(r"matrix\(([^)]*)\)")


def _matrix(value: str):
    m = MATRIX_RE.match(value or "")
    if not m:
        return None
    try:
        parts = [float(x) for x in m.group(1).split(",")]
    except ValueError:
        return None
    return parts if len(parts) == 6 else None


def _resample(rows, step=16):
    """Uniform time grid — the browser hands back frames at whatever
    interval it managed, and comparing a series against a shifted copy
    of itself needs even spacing to mean anything."""
    if len(rows) < 4:
        return [], 0
    t0, t1 = rows[0][0], rows[-1][0]
    grid, i = [], 0
    t = t0
    while t <= t1:
        while i + 1 < len(rows) and rows[i + 1][0] <= t:
            i += 1
        grid.append(rows[i][1])
        t += step
    return grid, step


def _period_of(grid, step, min_ms=250, max_ms=None):
    """Smallest lag at which the series repeats itself, or None.

    Autocorrelation rather than looking for a jump: a loop that eases
    has no discontinuity to find, and a linear one wraps somewhere the
    sampling may never land on."""
    n = len(grid)
    if n < 8:
        return None, 1.0
    max_ms = max_ms or (n * step) // 2
    width = max(abs(v) for row in grid for v in row) or 1.0

    # The whole motion has to happen INSIDE one period. Without this a
    # series that rests at one value between short bursts scores well at
    # any tiny lag — the flat stretches dominate the average and the
    # bursts are lost in it. One real element travelled 300px on a ~3s
    # cycle and was fitted at 240ms, which would have shipped a visible
    # jitter in place of a slow drift.
    span_of = [max(r[i] for r in grid) - min(r[i] for r in grid)
               for i in range(len(grid[0]))]
    total = max(span_of) or 0.0

    def covers(lag):
        """Does SOME window of this length contain the motion?

        Not the first window: an element can rest at one value for
        seconds before it moves, and judging only grid[:lag] rejected a
        genuine ~3s cycle because its opening stretch was flat."""
        if total <= 0:
            return True
        stride = max(1, lag // 4)
        for s in range(0, max(1, n - lag), stride):
            window = grid[s:s + lag]
            if len(window) < 2:
                break
            got = max(max(r[i] for r in window) - min(r[i] for r in window)
                      for i in range(len(grid[0])))
            if got >= 0.6 * total:
                return True
        return False

    best, best_err = None, 1e9
    for lag in range(max(2, min_ms // step), min(n // 2, max_ms // step) + 1):
        overlap = n - lag
        if overlap < 6:
            break
        if not covers(lag):
            continue
        err = 0.0
        for i in range(overlap):
            a, b = grid[i], grid[i + lag]
            err += sum(abs(x - y) for x, y in zip(a, b)) / len(a)
        err /= overlap * width
        if err < best_err:
            best, best_err = lag * step, err
    return best, best_err


def analyse_continuous(capture: dict, frames_per_loop=24) -> dict:
    """Turn a real-time capture into animations the port can replay.

    Three outcomes, and the third one matters as much as the others: a
    motion whose period cannot be established is REPORTED, not guessed
    at. Emitting a loop at the wrong period looks worse than emitting
    nothing, and unlike nothing it hides the fact that it is wrong."""
    out = {"rotate": [], "loop": [], "unhandled": [], "oneshot": []}
    span_total = capture.get("spans") or 0
    if not capture.get("available"):
        return {**out, "available": False,
                "reason": capture.get("reason", "no capture")}
    for item in capture.get("items", []):
        rows = [(r[0], _matrix(r[1])) for r in item["rows"] if _matrix(r[1])]
        # An element whose ONLY change is size has no matrix series to
        # analyse, but it is still a real animation — the loading panel
        # collapsing is exactly this. Send it straight to the one-shot
        # path rather than discarding it as "not enough samples".
        if len(rows) < 8:
            raw0 = item["rows"]
            sizes = {r[4] for r in raw0 if len(r) > 4}
            if len(raw0) >= 3 and len(sizes) > 1:
                out["oneshot"].append({
                    "id": item["id"], "sel": item.get("sel"),
                    "idx": item.get("idx"),
                    "props": ["transform", "opacity", "filter", "box"],
                    "frames": [[r[0] - raw0[0][0], r[1], r[2], r[3],
                                (r[4] if len(r) > 4 else "")] for r in raw0],
                    "duration": raw0[-1][0] - raw0[0][0],
                    "delay": int(item.get("startedAt") or raw0[0][0]),
                    "gone": bool(item.get("gone")), "trigger": 0.0})
                continue
        if len(rows) < 8:
            out["unhandled"].append({"id": item["id"],
                                     "why": "not enough matrix samples"})
            continue
        span = rows[-1][0] - rows[0][0]
        varies = {i for i in range(6)
                  if len({round(r[1][i], 4) for r in rows}) > 1}

        # A pure rotation is the common case (spinning badges) and can
        # be expressed exactly instead of sampled: scale is constant, so
        # the angle alone describes it.
        scales = {round((r[1][0] ** 2 + r[1][1] ** 2) ** 0.5, 3)
                  for r in rows}
        if varies <= {0, 1, 2, 3} and len(scales) == 1 and span:
            import math
            degs = [math.degrees(math.atan2(r[1][1], r[1][0])) for r in rows]
            un = [degs[0]]
            for k in range(1, len(degs)):
                d = degs[k] - degs[k - 1]
                d += 360 if d < -180 else (-360 if d > 180 else 0)
                un.append(un[-1] + d)
            rate = (un[-1] - un[0]) / (span / 1000.0)
            if abs(rate) > 2:
                out["rotate"].append({
                    "id": item["id"], "sel": item.get("sel"),
                    "idx": item.get("idx"), "from": round(degs[0], 2),
                    "duration": round(abs(360.0 / rate) * 1000),
                    "clockwise": rate > 0,
                    "scale": scales.pop()})
                continue

        grid, step = _resample(rows)
        period, err = _period_of(grid, step)
        if period and err < 0.02:
            # one full loop, evenly sampled
            frames = []
            for k in range(frames_per_loop + 1):
                t = rows[0][0] + period * k / frames_per_loop
                idx = min(range(len(rows)), key=lambda j: abs(rows[j][0] - t))
                frames.append({"offset": round(k / frames_per_loop, 4),
                               "transform": "matrix("
                                            + ", ".join(str(round(v, 4))
                                                        for v in rows[idx][1])
                                            + ")"})
            out["loop"].append({"id": item["id"], "sel": item.get("sel"),
                                "idx": item.get("idx"),
                                "duration": round(period),
                                "frames": frames, "fit": round(err, 4)})
        else:
            # Say WHICH kind of failure. A drifting value that never
            # comes back is an auto-advancing carousel whose cycle is
            # longer than the capture; a value that returns but does not
            # line up is something genuinely aperiodic. They need
            # different answers, and "no period" hides that.
            first = [r[1] for r in rows[:max(2, len(rows) // 10)]]
            last = [r[1] for r in rows[-max(2, len(rows) // 10):]]
            drift = max(abs(sum(c[i] for c in last) / len(last)
                            - sum(c[i] for c in first) / len(first))
                        for i in range(6))
            rng = max(max(r[1][i] for r in rows) - min(r[1][i] for r in rows)
                      for i in range(6)) or 1.0
            # A ONE-SHOT: it moved, then stopped, and never came back.
            # Not a loop and not a rotation, so this used to be reported
            # as "no period found" and dropped — which is how fiber's
            # progress bar and its loading reveal ended up static in the
            # port. It has a beginning, an end and a duration; that is
            # everything needed to replay it.
            raw = item["rows"]
            quiet = span_total - raw[-1][0] if raw else 0
            if raw and len(raw) >= 3 and quiet >= max(600, span_total * 0.25):
                out["oneshot"].append({
                    "id": item["id"], "sel": item.get("sel"),
                    "idx": item.get("idx"),
                    "props": ["transform", "opacity", "filter", "box"],
                    "frames": [[r[0] - raw[0][0], r[1], r[2], r[3],
                                (r[4] if len(r) > 4 else "")]
                               for r in raw],
                    "duration": raw[-1][0] - raw[0][0],
                    # WHEN it fired. Rebasing to zero and playing at load
                    # made the loading mask start before the counter had
                    # run; the original waits 3.1s.
                    "delay": int(item.get("startedAt") or raw[0][0]),
                    # and whether the original still had it at the end
                    "gone": bool(item.get("gone")),
                    "trigger": 0.0})
                continue
            if drift > 0.7 * rng:
                why = (f"advances without returning ({round(drift, 1)} of "
                       f"{round(rng, 1)} over {span}ms) — a stepped or "
                       f"looping carousel whose full cycle is longer than "
                       f"the capture window")
            else:
                why = f"no period found in {span}ms (best fit {round(err, 3)})"
            out["unhandled"].append({"id": item["id"], "why": why})
    out["available"] = True
    return out


def analyse_scroll(capture: dict, continuous: dict = None,
                   max_samples=14) -> dict:
    """Turn a real-time scroll capture into replayable specs.

    Two products: counters (text over time, replayed on first view) and
    scroll tracks (style as a function of the element's own progress
    through the viewport, replayed on scroll).

    Whatever the CONTINUOUS pass already owns is subtracted here. A
    rotating badge changes while you scroll past it, so it lands in this
    capture too — replaying it from both would put two systems on one
    transform, and the scroll one would be wrong because the motion is a
    function of time, not position."""
    out = {"counters": [], "tracks": [], "available": False}
    if not capture.get("available"):
        return {**out, "reason": capture.get("reason", "no capture")}
    claimed = set()
    for kind in ("rotate", "loop"):
        for item in (continuous or {}).get(kind, []) or []:
            claimed.add((item.get("sel"), item.get("idx")))

    for t in capture.get("text", []) or []:
        frames = t.get("frames") or []
        if len(frames) < 3:
            continue
        # Rebase to the moment the count STARTS, not to page load. Only
        # changes are recorded, so the opening frame is the resting
        # value stamped at t=0 and the next is the first tick seconds
        # later. Measuring from t=0 reported a 1.2s count as 5.2s — the
        # dead air before the trigger replayed as a stall.
        first = next((i for i in range(1, len(frames))
                      if frames[i][1] != frames[0][1]), None)
        if first is None:
            continue
        t0 = frames[first][0]
        seq = ([[0, frames[0][1]]]
               + [[f[0] - t0, f[1]] for f in frames[first:]])
        out["counters"].append({
            "sel": t["sel"], "idx": t["idx"], "frames": seq,
            # Carry `own` to the port. Without it the replay assigns
            # textContent, which deletes the unit element sitting beside
            # the number — fiber's counter would count up and lose its
            # percent sign on the first tick.
            "own": bool(t.get("own")),
            "duration": seq[-1][0]})

    names = list(WATCHED) + ["box"]
    # keyed the same way the recorder writes parent references: "sel|idx"
    by_key = {f'{t.get("sel")}|{t.get("idx")}': t
              for t in capture.get("tracks", []) or []}
    dropped_cascade = 0

    # ONLY these actually cascade. A parent's size determines its
    # children's rendered size, so a child that merely shrinks with its
    # parent must be left to layout. Nothing else works that way: a
    # parent's transform does NOT set a child's transform, and a
    # parent's opacity does not set a child's opacity — each element
    # holds its own. Treating them as inherited discarded five elements
    # that had their own motion and left them driven by nothing at all.
    LAYOUT_CASCADES = {"box", "width", "height"}

    def cascades(tr, kept):
        """Is this element merely following an ancestor that IS driven?"""
        mine = set(tr.get("varies") or [])
        if not mine:
            return False
        # anything that is not a layout consequence is its own animation
        if any(names[i] not in LAYOUT_CASCADES for i in mine):
            return False
        for pk in (tr.get("parents") or []):
            if pk not in kept:
                continue            # that ancestor is not driven either
            parent = by_key.get(pk)
            if parent and mine <= set(parent.get("varies") or []):
                return True
        return False

    # Which tracks will actually be EMITTED? A child may only be left to
    # layout if the ancestor it would follow is really going to be
    # driven. Dropping it because an ancestor merely *has* motion left
    # elements following a parent that was itself dropped, so nothing
    # drove either — a div growing 1209x526 -> 1349x724 sat static.
    kept = set()
    for tr in capture.get("tracks", []) or []:
        if (tr.get("sel"), tr.get("idx")) in claimed:
            continue
        rows0 = tr.get("rows") or []
        if len({tuple(r[1:]) for r in rows0}) < 2:
            continue
        kept.add(f'{tr.get("sel")}|{tr.get("idx")}')

    for tr in capture.get("tracks", []) or []:
        if (tr.get("sel"), tr.get("idx")) in claimed:
            continue
        if cascades(tr, kept):
            dropped_cascade += 1
            continue
        rows = tr.get("rows") or []
        if len({tuple(r[1:]) for r in rows}) < 2:
            continue
        rows = sorted(rows, key=lambda r: r[0])
        # thin to a bounded set of stops, keeping the extremes
        if len(rows) > max_samples:
            step = len(rows) / float(max_samples)
            keep = [rows[min(len(rows) - 1, int(i * step))]
                    for i in range(max_samples)]
            keep[-1] = rows[-1]
            rows = keep
        # rows become [scrollY, ...varying values] — keyed on absolute
        # scroll, which no amount of layout reflow can destabilise
        keep = sorted(tr.get("varies") or [])
        # Rows carry ONLY the varying columns. They used to carry every
        # column while `props` listed only the varying ones, so the
        # runtime's props[k] <-> row[1+k] pairing read the wrong
        # property — `box` was being fed `filter`.
        keyed = [[r[0]] + [r[1 + i] for i in keep] for r in rows]
        keyed.sort(key=lambda r: r[0])
        deduped = []
        for r in keyed:
            if deduped and deduped[-1][0] == r[0]:
                deduped[-1] = r          # last sample wins at a position
            else:
                deduped.append(r)
        if len(deduped) < 2:
            continue
        # SIZE REPLAY IS DISABLED. Driving width/height from a recorded
        # curve produced cards at 213x426 where the original settles at
        # 338x259 — portrait instead of landscape, visibly broken. An
        # element left at its natural size is merely missing an
        # animation; one driven to the wrong size is a defect on screen.
        # Until the size curve can be reproduced correctly, transform
        # and opacity are replayed and size is left to layout.
        props_kept = [names[i] for i in keep]
        drop = [k for k, nm in enumerate(props_kept)
                if nm in ("box", "width", "height")]
        if drop:
            props_kept = [nm for k, nm in enumerate(props_kept)
                          if k not in drop]
            deduped = [[r[0]] + [v for k, v in enumerate(r[1:])
                                 if k not in drop] for r in deduped]
        if not props_kept or len({tuple(r[1:]) for r in deduped}) < 2:
            continue
        entry = {"sel": tr["sel"], "idx": tr["idx"],
                 "props": props_kept,
                 "rows": deduped}
        kind = tr.get("kind") or "unknown"
        if kind == "scroll-linked":
            # follows the scrollbar, both ways
            out["tracks"].append(entry)
        else:
            # Plays ONCE, on its own clock, when it reaches the position
            # where the original started it. Driving these by scroll
            # progress is what made them start the instant the element
            # peeked into view instead of when it should, and made every
            # animation feel scroll-dragged.
            entry["trigger"] = tr.get("trigger")
            entry["retraced"] = tr.get("retraced")
            entry["end"] = rows[-1][1:]
            # Real frames and a real duration, measured by dwelling at
            # the stop where it fired. Without these a one-shot can only
            # be replayed at a guessed speed.
            play = (capture.get("plays") or {}).get(
                f'{tr.get("sel")}|{tr.get("idx")}')
            if play and play.get("duration"):
                keep = sorted(set(tr.get("varies") or []))
                entry["frames"] = [[f[0]] + [f[1 + i] for i in keep]
                                   for f in play["frames"]]
                entry["duration"] = play["duration"]
            out.setdefault("oneshots", []).append(entry)
    out["available"] = True
    out["dropped_cascade"] = dropped_cascade
    out.setdefault("oneshots", [])
    out["height"] = capture.get("height") or 0
    out["kinds"] = {"scroll_linked": len(out["tracks"]),
                    "one_shot": len(out["oneshots"])}
    return out


def scroll_spec(dom: str) -> dict:
    """The scroll-linked tracks and counter text, out of the dumped DOM."""
    m = re.search(r'(?is)<script[^>]*id="__ae_scroll"[^>]*>(.*?)</script\s*>',
                  dom or "")
    if not m:
        return {"tracks": [], "text": [], "meta": {}}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {"tracks": [], "text": [], "meta": {}}


def entrance_spec(dom: str) -> dict:
    """Pull the recorder's findings back out of the dumped DOM."""
    m = re.search(r'(?is)<script[^>]*id="__ae_entrance"[^>]*>(.*?)</script\s*>',
                  dom or "")
    if not m:
        return {"anims": [], "meta": {}}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {"anims": [], "meta": {}}


def capture_timeline(site: Path, page="index.html", platform="static",
                     samples=26, every=55, step=0.75, budget_ms=180000):
    """Record every element's animation as real keyframes."""
    browser = forge._find_browser()
    if not browser:
        raise SystemExit("timeline capture needs a headless browser")
    js = TIMELINE_JS % {"samples": samples, "every": every, "step": step}
    handler = _injecting_handler(Path(site), platform, js)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = forge._render_page(
            browser, f"http://127.0.0.1:{srv.server_address[1]}/{page}",
            budget_ms=budget_ms, timeout=max(120, budget_ms // 1000 + 60))
    finally:
        srv.shutdown()
    m = re.search(r'(?is)<script[^>]*id="__ae_timeline"[^>]*>(.*?)</script\s*>',
                  got.get("dom") or "")
    if not m:
        return {"entries": {}, "meta": {"error": "no timeline reported"}}
    return json.loads(m.group(1))

def summarise(cap: dict) -> dict:
    """What moves, and in which way — by observation, not by name."""
    time_driven, scroll_driven = {}, {}
    prev = None
    for stop in cap.get("stops", []):
        for mv in stop.get("moving", []):
            time_driven.setdefault(mv["p"], []).append(
                {"y": stop["y"], "from": mv["from"], "to": mv["to"]})
        now = {e["p"]: e["s"] for e in stop.get("at", [])}
        if prev is not None:
            for p, s in now.items():
                was = prev.get(p)
                if was and was != s:
                    scroll_driven.setdefault(p, []).append(
                        {"y": stop["y"], "s": s})
        prev = now
    return {"time_driven": time_driven, "scroll_driven": scroll_driven,
            "elements": cap.get("meta", {}).get("elements", 0),
            "stops": len(cap.get("stops", []))}


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    site = Path(argv[0]).expanduser()
    page = argv[1] if len(argv) > 1 else "index.html"
    cap = capture(site, page,
                  platform="framer" if (site.parent / "forge.json").exists()
                  else "static")
    if cap.get("meta", {}).get("error"):
        print("FAILED:", cap["meta"]["error"])
        return 1
    s = summarise(cap)
    print(f"scanned {s['elements']} elements over {s['stops']} scroll stops")
    print(f"  moving on their own (time-driven) : {len(s['time_driven'])}")
    print(f"  changing with scroll              : {len(s['scroll_driven'])}")
    for p, frames in list(s["time_driven"].items())[:5]:
        k = list(frames[0]["from"].keys())[:3]
        print(f"    time  [{p}] {k} across {len(frames)} stop(s)")
    for p, frames in list(s["scroll_driven"].items())[:5]:
        print(f"    scroll[{p}] {len(frames)} keyframe(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

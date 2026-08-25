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
            tag = f"<script>{script}</script>"
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
      out.anims.push({
        id: id, props: props, duration: num(t.duration),
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
  function counterish(el) {
    if (el.children.length) return false;
    var t = (el.textContent || '').trim();
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
    out.meta = { stops: n, of: STOPS, height: H,
                 tracked: out.tracks.length, counters: out.text.length,
                 complete: n >= STOPS };
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
    var s = getComputedStyle(el);
    return [s.transform === 'none' ? '' : s.transform,
            s.opacity === '1' ? '' : s.opacity,
            s.filter === 'none' ? '' : s.filter];
  }

  var t0 = 0, n = 0;
  setTimeout(function () { t0 = performance.now(); tick(); }, DELAY);
  function tick() {
    var dt = performance.now() - t0;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el.isConnected || waapi(el)) continue;   // WAAPI is handled
      var v = read(el);
      if (!v[0] && !v[1] && !v[2]) continue;
      var id = idOf(el);
      var rec = seen[id] || (seen[id] = { rows: [], el: el });
      var last = rec.rows[rec.rows.length - 1];
      if (!last || last[1] !== v[0] || last[2] !== v[1] || last[3] !== v[2])
        rec.rows.push([Math.round(dt), v[0], v[1], v[2]]);
    }
    n++;
    if (dt < SPAN) { requestAnimationFrame(tick); return; }

    var out = { spans: SPAN, frames: n, items: [], unaddressable: 0 };
    for (var id in seen) {
      var rec = seen[id];
      if (rec.rows.length < 3) continue;      // one change is not a loop
      var where = rec.el ? locate(rec.el) : null;
      if (!where) { out.unaddressable++; continue; }
      out.items.push({ id: id, sel: where.sel, idx: where.idx,
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
                     wait_s: int = 25) -> dict:
    """Run a page in REAL time and let it post its findings back.

    --dump-dom needs --virtual-time-budget to know when the page is
    settled, and that budget is exactly what makes rAF-driven motion
    unmeasurable. So this drops the budget, lets real time pass, and
    takes the result over HTTP instead of over stdout."""
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
             "--window-size=1440,2400", "--hide-scrollbars",
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
    out = {"rotate": [], "loop": [], "unhandled": []}
    if not capture.get("available"):
        return {**out, "available": False,
                "reason": capture.get("reason", "no capture")}
    for item in capture.get("items", []):
        rows = [(r[0], _matrix(r[1])) for r in item["rows"] if _matrix(r[1])]
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

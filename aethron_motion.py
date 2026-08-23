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
      if (moved && track.length > 2 && !out.entries[p])
        out.entries[p] = { y: y, frames: track };
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

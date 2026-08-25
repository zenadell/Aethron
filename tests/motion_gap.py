#!/usr/bin/env python3
"""What does the ORIGINAL move that the PORT does not?

WHAT THIS TOOL CAN AND CANNOT DECIDE — read before believing a number.

It is reliable for PERSISTENT motion: anything that keeps moving is
moving whenever you look, so an element that never changes in the port
while changing in the original is a genuine defect. That is how a
spinning badge frozen by the converter was found.

It CANNOT judge one-shot animations. An entrance plays once, and the
two builds are separate page loads with their own timing, so the same
element is routinely caught mid-flight in one and already finished in
the other. That shows up here as a "missing" move and means nothing:
checked directly, every element on one such list — a header, a founders
block, all ten characters of a split heading — carried a recorded
entrance animation in the port and played it correctly.

For entrances use tests/motion_battery.py, which seeks the animations
and reads what a viewer would see at a given instant instead of hoping
to sample the right moment.


The entrance recorder covers Web Animations. Everything framer-motion
drives on requestAnimationFrame — scroll-linked transforms, counters
whose text ticks upward — is invisible to getAnimations() and therefore
invisible to that recorder. This finds those, by the only method that
cannot miss one: scroll both builds through the same positions and diff
what actually changed on screen.

Elements are keyed by what survives the port: tag + framer-* classes +
a text prefix. Positional paths are useless here because the port
deliberately deletes things (badges, marketplace promos), which shifts
every index after them.

    python3 tests/motion_gap.py [project] [--pages index.html]
"""
import json
import re
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import forge                      # noqa: E402
import aethron_motion as motion    # noqa: E402

# Sample the page at a spread of scroll positions and record, per
# element, every distinct rendered value. An element that reads the same
# at every position is not animating; one that changes is.
SCAN_JS = r"""
(function () {
  var STOPS = %(stops)d;
  var out = { moved: {}, stops: 0, height: 0 };

  function key(el) {
    var cls = (el.className && el.className.baseVal !== undefined
               ? el.className.baseVal : el.className) || '';
    var framer = String(cls).split(/\s+/).filter(function (c) {
      return /^framer-[A-Za-z0-9]{4,}$/.test(c);
    }).sort().join('.');
    var name = el.getAttribute('data-framer-name') || '';
    var txt = (el.textContent || '').trim().slice(0, 18);
    return el.tagName + '|' + framer + '|' + name + '|' + txt;
  }
  function read(el) {
    var s = getComputedStyle(el);
    var t = s.transform === 'none' ? '' : s.transform;
    var o = s.opacity === '1' ? '' : s.opacity;
    var f = s.filter === 'none' ? '' : s.filter;
    var c = s.color || '';
    // leaf text matters: a counter animates by rewriting its text, and
    // no style read will ever see that
    var txt = el.children.length ? '' : (el.textContent || '').trim().slice(0, 24);
    return t + '~' + o + '~' + f + '~' + c + '~' + txt;
  }

  // Finish the ONE-SHOT animations before measuring. An entrance parks
  // its element and then releases it, so scanning mid-entrance counts
  // the parked pose as a scroll state — that is how this tool reported
  // a sticky header and three static blocks as scroll-linked, sending
  // me off to reproduce animations that do not exist.
  //
  // Infinite ones are LEFT RUNNING on purpose: marquees and spinning
  // badges are exactly what the port has to prove it reproduces, and
  // pausing them here would hide both a working port and a broken one.
  function settleFinite() {
    var list = [];
    try { list = document.getAnimations() || []; } catch (e) { return; }
    for (var i = 0; i < list.length; i++) {
      var t = {};
      try { t = list[i].effect.getComputedTiming(); } catch (e) { }
      if (t.iterations === Infinity || t.iterations > 1e6) continue;
      try { list[i].finish(); } catch (e) { }
    }
  }

  var els = [].slice.call(document.querySelectorAll('*')).filter(function (el) {
    return el.offsetParent !== null || el.getClientRects().length;
  });
  var seen = {};
  function snap() {
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el.isConnected) continue;
      var k = key(el), v = read(el);
      (seen[k] = seen[k] || {})[v] = 1;
    }
  }

  var H = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  out.height = H;
  var step = Math.max(1, Math.floor((H - innerHeight) / Math.max(1, STOPS - 1)));
  var at = 0, n = 0;
  function tick() {
    window.scrollTo(0, at);
    // Settle at EVERY stop, not just once at the start. Scrolling down
    // brings new elements into view and fires their entrances, so a
    // single settle at t=0 leaves every below-the-fold entrance to be
    // caught mid-flight and counted as a scroll state. That is what
    // kept a static header and two static blocks on the missing list
    // after the real gap was already fixed.
    settleFinite();
    snap();
    n++;
    at += step;
    // Real seconds now, so give entrances time to actually play out
    // between stops instead of assuming they have.
    if (n < STOPS) { setTimeout(tick, 550); return; }
    window.scrollTo(0, 0);
    setTimeout(function () {
      snap();
      out.stops = n;
      for (var k in seen) {
        var vals = Object.keys(seen[k]);
        if (vals.length > 1) out.moved[k] = vals.length;
      }
      try {
        fetch("/__ae_capture", { method: 'POST',
                                 body: JSON.stringify(out) });
      } catch (e) { }
    }, 600);
  }
  setTimeout(function () { settleFinite(); tick(); }, 900);
})();
"""


def scan(root: Path, platform: str, page: str, stops: int = 12) -> dict:
    """Scan in REAL time. This is not an optimisation — it is the only
    environment where the ORIGINAL behaves.

    Under a virtual clock the original's appear engine never fires for
    below-the-fold elements: measured directly, a header and a notch sat
    at opacity 0.001 and a block at translateY(160px) for an entire
    twelve-stop scan. Compared against a port that renders them
    correctly, that reports the PORT as broken — the comparison inverts.
    So the scan posts its findings back over HTTP, the way the
    continuous recorder does, and no virtual clock is involved."""
    return motion.capture_realtime(root, platform, page,
                                   SCAN_JS % {"stops": stops},
                                   wait_s=90)


def describe(key: str) -> str:
    tag, framer, name, txt = (key.split("|") + ["", "", ""])[:4]
    bits = [tag.lower()]
    if name:
        bits.append(f'"{name}"')
    if txt:
        bits.append(f'text={txt!r}')
    if framer:
        bits.append(framer.split(".")[0])
    return "  ".join(bits)


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    project = Path(args[0]).resolve() if args else ROOT / "projects/agero"
    page = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--page=")), "index.html")
    cfg = json.loads((project / "forge.json").read_text())
    if not forge._find_browser():
        print("SKIPPED — no browser, the gap is UNMEASURED (not proven small)")
        return 0

    print(f"scanning ORIGINAL  {project.name}/site/{page} …")
    orig = scan(project / "site", cfg.get("platform", "static"), page)
    port_dir = project / "convert-astro/dist"
    if not port_dir.is_dir():
        print("no built port — convert first")
        return 1
    print(f"scanning PORT      {port_dir.relative_to(project)}/{page} …")
    port = scan(port_dir, "static", page)
    if not orig or not port:
        print("one of the scans produced nothing — cannot compare")
        return 1

    o, p = orig.get("moved", {}), port.get("moved", {})
    print(f"\n  original: {len(o)} element(s) change while scrolling "
          f"({orig.get('stops')} stops, {orig.get('height')}px)")
    print(f"  port:     {len(p)} element(s) change "
          f"({port.get('stops')} stops, {port.get('height')}px)")

    missing = sorted(((v, k) for k, v in o.items() if k not in p),
                     reverse=True)
    extra = sorted(((v, k) for k, v in p.items() if k not in o), reverse=True)
    print(f"\n  MOVES IN THE ORIGINAL BUT NOT IN THE PORT: {len(missing)}")
    for v, k in missing[:25]:
        print(f"     {v:>3} distinct state(s)   {describe(k)}")
    if len(missing) > 25:
        print(f"     … and {len(missing) - 25} more")
    if extra:
        print(f"\n  moves in the port but not the original: {len(extra)}")
        for v, k in extra[:8]:
            print(f"     {v:>3}   {describe(k)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Does the PORT actually move, and does it move like the original?

The referee grades what a reader sees: text, headings, images. A page
can score 100% on all of it and still be dead, which is exactly what
kept happening — the port shipped a hero whose characters were baked at
their final opacity, and every content check called it perfect.

So this battery measures motion directly:

  1. the recorder reads the original's own animation objects, with the
     per-character stagger intact;
  2. the port creates those same animations;
  3. seeking them reproduces a staggered wave rather than a flat page;
  4. the marquees run, at the original's exact durations;
  5. the wiring does not depend on requestAnimationFrame alone.

(5) earns its place: the same page measured three times played the full
entrance twice and nothing at all the third time, purely on whether a
double rAF had fired. A throttled frame callback is ordinary in a
background tab, and without the timer fallback that leaves a user
looking at a blank hero.

A missing port reports SKIPPED. It never reports PASS for a check that
could not run.

    python3 tests/motion_battery.py [project_dir]
"""
import json
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import forge                       # noqa: E402
import aethron_motion as motion    # noqa: E402
import aethron_convert as convert  # noqa: E402

PROJ = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
    else ROOT / "projects/agero"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   {detail}" if detail and not cond else ""))


def scenario(t):
    print(f"\n── {t}")


def render(root: Path, platform: str, probe: str, page="index.html",
           budget=9000):
    """Serve a directory and run one page with a script injected."""
    handler = motion._injecting_handler(root, platform, probe)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = forge._render_page(
            forge._find_browser(),
            f"http://127.0.0.1:{srv.server_address[1]}/{page}",
            budget_ms=budget, timeout=180)
    finally:
        srv.shutdown()
    return got.get("dom") or ""


def json_tag(dom: str, tag_id: str):
    m = re.search(r'(?is)<script[^>]*id="%s"[^>]*>(.*?)</script\s*>' % tag_id,
                  dom)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


SEEK = r"""
(function () {
  setTimeout(function () {
    var h = document.querySelector('h1');
    var out = { n: 0, withAnims: 0, profile: [], marquee: [] };
    if (h) {
      var cs = [].slice.call(h.querySelectorAll('span')).filter(function (s) {
        return (s.textContent || '').length === 1;
      });
      out.n = cs.length;
      cs.forEach(function (el) {
        if ((el.getAnimations() || []).length) out.withAnims++;
      });
      // Seek every animation to a common time and read what a viewer
      // would see at that instant. This measures the animations
      // themselves, without depending on the clock advancing — which
      // under a virtual-time budget it barely does.
      [0, 250, 350, 450, 600, 800, 1100].forEach(function (T) {
        var row = { T: T, o: [] };
        cs.forEach(function (el) {
          (el.getAnimations() || []).forEach(function (a) {
            try { a.currentTime = T; } catch (e) { }
          });
          row.o.push(+(+getComputedStyle(el).opacity).toFixed(3));
        });
        out.profile.push(row);
      });
    }
    (document.getAnimations() || []).forEach(function (a) {
      var ct = {};
      try { ct = a.effect.getComputedTiming(); } catch (e) { }
      if (ct.iterations === Infinity || ct.iterations > 1e6)
        out.marquee.push(Math.round(ct.duration));
    });
    var t = document.createElement('script');
    t.type = 'application/json'; t.id = '__ae_seek';
    t.textContent = JSON.stringify(out);
    document.body.appendChild(t);
  }, 1500);
})();
"""


def main():
    if not (PROJ / "forge.json").exists():
        print(f"SKIPPED — {PROJ} is not a project")
        return 0
    platform = json.loads((PROJ / "forge.json").read_text()).get(
        "platform", "static")
    browser = forge._find_browser()

    scenario("the payload the browser will run")
    js = ROOT / "aethron_motion.py"
    node = subprocess.run(["node", "--version"], capture_output=True)
    if node.returncode == 0:
        import tempfile
        for name, src in (("ENTRANCE_JS", motion.ENTRANCE_JS % {"watch": 100}),
                          ("MOTION_JS", convert.MOTION_JS)):
            f = Path(tempfile.mkstemp(suffix=".js")[1])
            f.write_text(src)
            r = subprocess.run(["node", "--check", str(f)],
                               capture_output=True, text=True)
            check(f"{name} is valid JavaScript", r.returncode == 0,
                  r.stderr[:200])
    else:
        print("  SKIPPED node --check (no node)")
    check("the runtime does not depend on rAF alone",
          "setTimeout(wire," in convert.MOTION_JS)
    check("marquees are not gated on an observer",
          "marquees do not wait" in convert.MOTION_JS)

    scenario("the recording survives being shrunk for shipping")
    # A sampled spring easing is ~5KB and a stagger repeats it once per
    # character, so the recording ships as a shape table. Shrinking it
    # must not quietly round a delay or drop a curve — that would be a
    # silently wrong animation rather than a missing one.
    sample = {"anims": [
        {"id": "e1", "delay": 200, "duration": 400, "easing": "linear(0 0%, 1 100%)",
         "iterations": 1, "direction": "normal", "appear": False,
         "frames": [{"opacity": "0.001", "offset": 0},
                    {"opacity": "1", "offset": 1}]},
        {"id": "e2", "delay": 250, "duration": 400, "easing": "linear(0 0%, 1 100%)",
         "iterations": 1, "direction": "normal", "appear": False,
         "frames": [{"opacity": "0.001", "offset": 0},
                    {"opacity": "1", "offset": 1}]},
        {"id": "m1", "delay": 0, "duration": 59280, "easing": "linear",
         "iterations": "infinite", "direction": "normal", "appear": True,
         "frames": [{"transform": "translateX(0px)", "offset": 0},
                    {"transform": "translateX(-2964px)", "offset": 1}]},
    ], "meta": {}}
    comp = convert.compress_entrance(sample)
    check("identical curves collapse to one shape",
          len(comp["shapes"]) == 2, f"{len(comp['shapes'])}")

    def expand(rec):                      # mirrors expand() in MOTION_JS
        out = []
        for r in rec["anims"]:
            s = rec["shapes"][r["s"]]
            out.append({"id": r["i"], "delay": r.get("d", 0),
                        "appear": bool(r.get("a")), **s})
        return out

    back = expand(comp)
    check("every animation comes back", len(back) == len(sample["anims"]))
    check("nothing is lost or rounded in the round trip",
          all(b["id"] == a["id"] and b["delay"] == a["delay"]
              and b["appear"] == a["appear"] and b["frames"] == a["frames"]
              and b["duration"] == a["duration"] and b["easing"] == a["easing"]
              and b["iterations"] == a["iterations"]
              for a, b in zip(sample["anims"], back)))
    check("an empty recording stays empty (no phantom tag)",
          convert.compress_entrance({}).get("anims") == [])

    if not browser:
        print("\nVERDICT: SKIPPED — no browser, motion is UNVERIFIED "
              "(not proven good)")
        return 0

    scenario("reading the original's own animations")
    dom = render(PROJ / "site", platform,
                 motion.ENTRANCE_JS % {"watch": 3000})
    spec = motion.entrance_spec(dom)
    anims = spec.get("anims") or []
    check("the recorder found animations", len(anims) > 10,
          f"got {len(anims)}")
    uncovered = [a for a in anims if not a.get("appear")]
    check("it sees what the appear engine does not", len(uncovered) > 0,
          f"{len(uncovered)}")
    delays = sorted({a["delay"] for a in anims
                     if a.get("duration") and a["duration"] < 2000})
    check("staggered delays recovered, not one lump", len(delays) > 4,
          f"{delays[:8]}")
    loops = [a for a in anims if a.get("iterations") == "infinite"]
    check("marquees recovered as infinite animations", len(loops) > 0,
          f"{len(loops)}")

    dist = PROJ / "convert-astro/dist"
    if not dist.is_dir():
        print("\nVERDICT: SKIPPED — no built port to measure "
              "(convert first); the port side is UNVERIFIED")
        return 0 if all(c for _, c in results) else 1

    # A VERDICT MUST NOT BE OLDER THAN WHAT IT JUDGES.
    #
    # This battery grades a dist/ it does not build. A fix to the
    # converter was made, this suite was run, the same four checks
    # failed, and the obvious reading was "the fix did not work" — but
    # dist/ was two hours older than the change and the fix had never
    # been in it. That is the STALE rule from aethron_audit, met in the
    # wild by the person who wrote it. Measuring a stale artifact does
    # not produce a weaker result; it produces a WRONG one, delivered
    # with full confidence.
    conv = ROOT / "aethron_convert.py"
    if conv.is_file() and dist.stat().st_mtime < conv.stat().st_mtime:
        import datetime as _dt

        def _t(p):
            return _dt.datetime.fromtimestamp(p).strftime("%H:%M:%S")
        print(f"\nVERDICT: SKIPPED — the built port is STALE. dist/ was "
              f"built at {_t(dist.stat().st_mtime)} but "
              f"aethron_convert.py changed at "
              f"{_t(conv.stat().st_mtime)}.\n"
              f"          Grading it would report on code that is not in "
              f"it. Re-run:\n"
              f"          python3 forge.py convert {PROJ} "
              f"--framework astro\n"
              f"          The port side is UNVERIFIED — not proven good, "
              f"and not proven bad either.")
        return 0 if all(c for _, c in results) else 1

    scenario("the port, measured three times")
    runs = []
    for _ in range(3):
        d = json_tag(render(dist, "static", SEEK), "__ae_seek")
        runs.append(d or {})
    good = [r for r in runs if r.get("n")]
    check("the port has a per-character heading", len(good) == 3)
    check("every run animates every character",
          all(r.get("withAnims") == r.get("n") and r.get("n")
              for r in good),
          f"withAnims={[r.get('withAnims') for r in good]} "
          f"n={[r.get('n') for r in good]}")

    if good:
        prof = good[0]["profile"]
        first, last = prof[0]["o"], prof[-1]["o"]
        check("nothing is visible at the start", all(v < 0.05 for v in first))
        check("everything has arrived by the end",
              all(v > 0.95 for v in last))
        check("opacity never travels backwards",
              all(all(b >= a - 1e-6 for a, b in zip(p["o"], q["o"]))
                  for p, q in zip(prof, prof[1:])))
        # the wave: mid-flight, earlier characters lead later ones
        # a genuinely mid-flight frame: something has arrived and
        # something has not
        mid = next((r["o"] for r in prof
                    if max(r["o"]) > 0.2 and min(r["o"]) < 0.05), None)
        check("mid-entrance the characters are staggered, not in lockstep",
              mid is not None and mid[0] > mid[-1] + 0.2,
              f"{mid}")
        original_loops = sorted(a["duration"] for a in loops)
        port_loops = sorted(good[0].get("marquee") or [])
        # Directional on purpose. Every marquee the original runs must
        # appear in the port — but the port legitimately runs MORE, and
        # that is the point: motion the original drives on rAF (a badge
        # rotating by inline style) is re-expressed as a real animation,
        # so it shows up here while being invisible to getAnimations()
        # on the original. Asserting equality failed the port for doing
        # its job.
        check("every marquee the original runs is in the port",
              original_loops and all(
                  any(abs(p - o) <= 3 for p in port_loops)
                  for o in original_loops),
              f"port={port_loops} original={[round(x) for x in original_loops]}")
        extra = [p for p in port_loops
                 if not any(abs(p - o) <= 3 for o in original_loops)]
        check("continuous rAF motion is re-expressed as real animations",
              len(extra) > 0, f"extra={extra}")

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} green")
    if passed != len(results):
        print("FAILED: " + ", ".join(n for n, c in results if not c))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

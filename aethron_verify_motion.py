#!/usr/bin/env python3
"""Does the PORT move exactly like the ORIGINAL? Every element, both.

This exists because the wrong person was finding the defects. The
referee graded text, headings and images and said "99% identical" on a
build whose cards were frozen, whose counters were dead and whose
one-shots were being dragged by the scrollbar. Every one of those was
found by a human looking at the page.

So: watch EVERY element in both builds through the same load and the
same scroll, and diff what they do. Not a sample, not the named ones —
all of them. An element that reaches eight states in the original and
two in the port is a defect, and it should be this file that says so.

    python3 aethron_verify_motion.py <project> [--port=astro] [--page=]
                                     [--dir=<a specific build to grade>]

Exit code is 0 only when nothing diverges.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_motion as motion    # noqa: E402

PROBE = r"""
(function () {
  // Same script in both builds, same scroll positions, same dwells.
  // Elements are keyed by their framer-* classes so the key survives
  // the port's deliberate deletions (badges, marketplace promos) which
  // shift every positional index after them.
  var out = { items: {}, meta: {} }, done = false;

  function key(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ''));
    var f = cls.split(/\s+/).filter(function (c) {
      return /^framer-[A-Za-z0-9]{4,}$/.test(c);
    }).sort();
    var name = el.getAttribute && el.getAttribute('data-framer-name');
    if (!f.length && !name) return null;
    return el.tagName + '|' + f.join('.') + '|' + (name || '');
  }
  function read(el) {
    var s = getComputedStyle(el), r = el.getBoundingClientRect();
    return [s.transform === 'none' ? '' : s.transform,
            (+s.opacity).toFixed(2),
            s.filter === 'none' ? '' : s.filter,
            s.color,
            Math.round(r.width) + 'x' + Math.round(r.height),
            el.children.length ? ''
              : (el.textContent || '').trim().slice(0, 20)].join('~');
  }
  function snap() {
    var all = document.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!el.getClientRects().length) continue;
      var k = key(el);
      if (!k) continue;
      var rec = out.items[k] || (out.items[k] = { n: 0, states: {} });
      rec.states[read(el)] = 1;
    }
  }

  // phase 1: load, in place — entrances and anything time-driven
  var t0 = Date.now();
  (function inPlace() {
    snap();
    if (Date.now() - t0 < 5000) { setTimeout(inPlace, 260); return; }
    scrollPhase();
  })();

  // phase 2: the same scroll positions in both builds, with a dwell so
  // one-shots triggered by arriving actually get to play
  function scrollPhase() {
    var H = Math.max(document.body.scrollHeight,
                     document.documentElement.scrollHeight);
    var n = 0, STOPS = 14;
    (function step() {
      window.scrollTo(0, Math.round((H - innerHeight) * n / (STOPS - 1)));
      setTimeout(function () {
        snap();
        setTimeout(function () {
          snap();
          n++;
          if (n < STOPS) step(); else finish();
        }, 420);
      }, 340);
    })();
  }
  function finish() {
    if (done) return;
    done = true;
    for (var k in out.items) {
      out.items[k].n = Object.keys(out.items[k].states).length;
      delete out.items[k].states;
    }
    try { fetch("/__ae_capture", { method: 'POST',
                                   body: JSON.stringify(out) }); } catch (e) { }
  }
  setTimeout(finish, 120000);
})();
"""


def run(project, port="astro", page="index.html", dist_dir=None) -> dict:
    proj = Path(project).resolve()
    cfg = json.loads((proj / "forge.json").read_text(encoding="utf-8"))
    plat = cfg.get("platform", "static")
    dist = {"astro": "convert-astro/dist", "next": "convert-next/out",
            "vite": "convert-vite/dist"}[port]
    # An explicit directory lets two builds of the SAME framework be
    # compared against one original — carry-the-runtime against
    # rebuilt-motion, say. Carry is identical by construction, so
    # measuring it gives this checker's own noise floor, without which
    # a mismatch count from the other mode cannot be read.
    port_dir = Path(dist_dir).resolve() if dist_dir else proj / dist
    if not (port_dir / page).is_file():
        return {"available": False, "reason": f"no {port} build"}

    a = motion.capture_realtime(proj / "site", plat, page, PROBE, wait_s=180)
    b = motion.capture_realtime(port_dir, "static", page, PROBE, wait_s=180)
    if not a.get("available") or not b.get("available"):
        return {"available": False,
                "reason": a.get("reason") or b.get("reason")}

    A = {k: v["n"] for k, v in (a.get("items") or {}).items()}
    B = {k: v["n"] for k, v in (b.get("items") or {}).items()}

    frozen, missing, extra, ok = [], [], [], 0
    for k, na in A.items():
        nb = B.get(k)
        if nb is None:
            missing.append({"key": k, "original": na})
        elif na > 1 and nb <= 1:
            # moves in the original, static in the port — the defect
            # class that shipped frozen cards and dead counters
            frozen.append({"key": k, "original": na, "port": nb})
        elif na > 1 and nb < na * 0.6:
            frozen.append({"key": k, "original": na, "port": nb,
                           "partial": True})
        else:
            ok += 1
    for k, nb in B.items():
        if k not in A and nb > 1:
            extra.append({"key": k, "port": nb})

    frozen.sort(key=lambda d: -(d["original"] - d["port"]))
    return {"available": True, "matched": ok,
            "frozen": frozen, "missing_elements": missing, "extra": extra,
            "original_elements": len(A), "port_elements": len(B)}


def describe(key: str) -> str:
    tag, cls, name = (key.split("|") + ["", ""])[:3]
    bits = [tag.lower()]
    if name:
        bits.append(f'"{name}"')
    if cls:
        bits.append(cls.split(".")[0])
    return " ".join(bits)


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    project = args[0] if args else "projects/agero"
    port = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--port=")), "astro")
    page = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--page=")), "index.html")
    dist_dir = next((a.split("=", 1)[1] for a in argv
                     if a.startswith("--dir=")), None)
    r = run(project, port, page, dist_dir)
    if not r.get("available"):
        print("UNMEASURED:", r.get("reason"))
        return 2

    print(f"elements compared: original {r['original_elements']}, "
          f"port {r['port_elements']}, matching motion {r['matched']}")
    print(f"\nMOVES IN THE ORIGINAL, NOT (OR BARELY) IN THE PORT: "
          f"{len(r['frozen'])}")
    for d in r["frozen"][:40]:
        tag = " (partial)" if d.get("partial") else ""
        print(f"   {describe(d['key'])[:52]:54} "
              f"original {d['original']:>3} -> port {d['port']:>3}{tag}")
    if len(r["frozen"]) > 40:
        print(f"   … and {len(r['frozen']) - 40} more")
    if r["missing_elements"]:
        print(f"\nELEMENTS ABSENT FROM THE PORT: {len(r['missing_elements'])}")
        for d in r["missing_elements"][:10]:
            print(f"   {describe(d['key'])[:52]}")
    print(f"\nVERDICT: "
          + ("IDENTICAL — nothing moves in the original that does not "
             "move in the port" if not r["frozen"] else
             f"{len(r['frozen'])} element(s) do not match"))
    return 0 if not r["frozen"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

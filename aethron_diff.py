#!/usr/bin/env python3
"""Find defects nobody wrote a check for. The original is the spec.

WHY THIS EXISTS

Every other check in Aethron looks for a failure someone already met.
That is useful and it is not enough: a template can break in a way no
check looks for, finish "clean", and be visibly wrong when you open it.

But a checklist was never how these were actually found. They were found
by DIFFING — put the original beside the copy, drive both identically,
and ask what is different. The footer was found by comparing how many
elements stayed invisible. The card stack by comparing how many states
each element reached. The promo card by comparing what was visible. None
of those needed prior knowledge of the failure; they needed a comparison.

So this compares EVERYTHING. Every element, every computed property the
browser exposes — not a curated list of twenty — at several moments and
under the same scripted interactions. Anything the copy does differently
is a candidate defect, named, with the property that differs.

THE NOISE PROBLEM, and why this is trustworthy: two renders of the SAME
page differ slightly — animations land at different phases, timers read
different values. So the reference build is captured TWICE and diffed
against itself first. That is the noise floor. Only differences that
exceed it are reported. Without that step this would drown you in false
positives and you would stop reading it, which is worse than not having
it.

    python3 aethron_diff.py <project>
    python3 aethron_diff.py <project> --pair=migration:port --page=about.html
    python3 aethron_diff.py <project> --top=40
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_motion as motion                     # noqa: E402

BUILDS = {
    "original": ("pristine", None),
    "migration": ("site", None),
    "port": ("convert-astro/dist", "static"),
    "port-next": ("convert-next/out", "static"),
}

# Properties that differ between two renders of the same page for reasons
# that are not defects. Everything else the browser exposes is compared.
VOLATILE = {
    "perspectiveOrigin", "transformOrigin", "webkitLocale",
    "blockSize", "inlineSize", "webkitLogicalHeight", "webkitLogicalWidth",
}

PROBE = r"""
(function () {
  /* Record EVERY computed property of EVERY element, at several moments
     and under the same scripted scroll. Sent as a per-element hash plus
     the properties themselves for a bounded sample, because the full
     matrix for a real page is tens of megabytes. */
  var STOPS = %(stops)d, SETTLE = %(settle)d, POST = "%(post)s";
  var out = { els: {}, order: [], meta: {}, err: "" };
  var counts = {};

  function keyOf(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ""));
    var stable = cls.split(/\s+/).filter(function (c) {
      return c && !/^(w--current|is-active|w-condition-invisible)$/.test(c);
    }).sort().slice(0, 4).join(".");
    var own = "";
    for (var i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 3) { own += el.childNodes[i].nodeValue; }
    }
    own = own.trim().replace(/\s+/g, " ").slice(0, 20);
    var name = (el.getAttribute && (el.getAttribute("data-framer-name")
                || el.getAttribute("data-w-id") || el.id)) || "";
    var base = el.tagName + "|" + stable + "|" + name + "|" + own;
    counts[base] = (counts[base] || 0) + 1;
    return base + "#" + counts[base];
  }

  function props(el) {
    var s = getComputedStyle(el), o = {};
    for (var i = 0; i < s.length; i++) {
      var p = s[i];
      o[p] = s.getPropertyValue(p);
    }
    var r = el.getBoundingClientRect();
    o["--rect"] = Math.round(r.width) + "x" + Math.round(r.height);
    o["--text"] = el.children.length ? ""
                : (el.textContent || "").trim().slice(0, 30);
    return o;
  }

  function hash(o) {
    var s = "", k;
    var keys = Object.keys(o).sort();
    for (var i = 0; i < keys.length; i++) { s += keys[i] + "=" + o[keys[i]] + ";"; }
    var h = 5381;
    for (var j = 0; j < s.length; j++) { h = ((h * 33) ^ s.charCodeAt(j)) >>> 0; }
    return h.toString(36);
  }

  function sweep(tag) {
    var all = document.querySelectorAll("body *");
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName)) { continue; }
      var r = el.getBoundingClientRect();
      if (r.width < 1 && r.height < 1) { continue; }
      var k = el.__aeKey || (el.__aeKey = keyOf(el));
      var rec = out.els[k];
      if (!rec) {
        rec = out.els[k] = { h: {}, p: null };
        out.order.push(k);
      }
      var o = props(el);
      rec.h[tag] = hash(o);
      /* keep the full property set from the settled moment only: it is
         what a person would compare, and it bounds the payload */
      if (tag === "settled") { rec.p = o; }
    }
  }

  try {
    var t0 = Date.now();
    (function early() {
      sweep("t" + Math.round((Date.now() - t0) / 500));
      if (Date.now() - t0 < SETTLE) { setTimeout(early, 500); return; }
      sweep("settled");
      scrollPhase();
    })();
  } catch (e) {
    out.err = String(e && e.message).slice(0, 150);
    post();
  }

  function scrollPhase() {
    var H = Math.max(document.body.scrollHeight,
                     document.documentElement.scrollHeight) - innerHeight;
    var n = 0;
    (function step() {
      window.scrollTo(0, Math.round(H * n / Math.max(1, STOPS - 1)));
      setTimeout(function () {
        try { sweep("s" + n); } catch (e) { }
        if (++n < STOPS) { step(); return; }
        window.scrollTo(0, 0);
        setTimeout(post, 400);
      }, 420);
    })();
  }

  function post() {
    out.meta.elements = out.order.length;
    try { fetch(POST, { method: "POST", body: JSON.stringify(out) }); }
    catch (e) { }
  }
})();
"""


def capture(root: Path, platform: str, page: str, win="1440,900",
            wait_s=300) -> dict:
    js = PROBE % {"stops": 8, "settle": 5000, "post": "/__ae_capture"}
    return motion.capture_realtime(root, platform, page, js,
                                   wait_s=wait_s, win=win)


def differences(a: dict, b: dict):
    """Elements whose behaviour differs, and the properties that differ."""
    A, B = a.get("els") or {}, b.get("els") or {}
    changed, missing = {}, []
    for key, ra in A.items():
        rb = B.get(key)
        if rb is None:
            missing.append(key)
            continue
        # which moments disagree
        tags = [t for t in ra["h"] if t in rb["h"] and ra["h"][t] != rb["h"][t]]
        if not tags:
            continue
        pa, pb = ra.get("p") or {}, rb.get("p") or {}
        props = sorted(k for k in set(pa) | set(pb)
                       if k not in VOLATILE and pa.get(k) != pb.get(k))
        changed[key] = {"moments": tags, "props": props,
                        "a": {k: pa.get(k) for k in props[:6]},
                        "b": {k: pb.get(k) for k in props[:6]}}
    extra = [k for k in B if k not in A]
    return changed, missing, extra


def describe(key: str) -> str:
    tag, cls, name, text = (key.split("|") + ["", "", ""])[:4]
    bits = [tag.lower()]
    if name:
        bits.append(f'"{name.split("#")[0]}"')
    elif text.split("#")[0]:
        bits.append(f'"{text.split("#")[0][:26]}"')
    elif cls:
        bits.append("." + cls.split(".")[0])
    return " ".join(bits)


def resolve(proj: Path, name: str):
    if name not in BUILDS:
        return None, None
    sub, plat = BUILDS[name]
    root = proj / sub
    if not root.is_dir():
        return None, None
    if plat is None:
        cfg = json.loads((proj / "forge.json").read_text(encoding="utf-8"))
        plat = cfg.get("platform", "static")
    return root, plat


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 2
    proj = Path(args[0]).resolve()
    page = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--page=")), "index.html")
    pair = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--pair=")), "migration:port")
    top = int(next((a.split("=", 1)[1] for a in argv
                    if a.startswith("--top=")), "25"))
    ref_name, copy_name = pair.split(":", 1)

    ref_root, ref_plat = resolve(proj, ref_name)
    copy_root, copy_plat = resolve(proj, copy_name)
    if not ref_root or not copy_root:
        print(f"missing build: {ref_name} or {copy_name}")
        return 2
    for r in (ref_root, copy_root):
        if not (r / page).is_file():
            print(f"no {page} in {r}")
            return 2

    print(f"comparing EVERY property of EVERY element\n"
          f"  reference: {ref_name}    copy: {copy_name}    page: {page}\n")

    print("  reading reference (twice — the second run measures noise) …")
    ref1 = capture(ref_root, ref_plat, page)
    ref2 = capture(ref_root, ref_plat, page)
    print("  reading copy …")
    cpy = capture(copy_root, copy_plat, page)
    for nm, c in (("reference", ref1), ("reference#2", ref2), ("copy", cpy)):
        if not c.get("available"):
            print(f"  UNMEASURED ({nm}): {c.get('reason')} — NOT proven equal")
            return 2

    noise, noise_missing, _ = differences(ref1, ref2)
    signal, missing, extra = differences(ref1, cpy)

    # subtract the noise floor: an element that differs from ITSELF between
    # two runs cannot be used as evidence about the copy
    real = {k: v for k, v in signal.items() if k not in noise}
    noisy_props = set()
    for v in noise.values():
        noisy_props |= set(v["props"])
    for k in list(real):
        real[k]["props"] = [p for p in real[k]["props"] if p not in noisy_props]
        if not real[k]["props"] and not real[k]["moments"]:
            del real[k]

    print(f"\n  elements: {len(ref1.get('els') or {})} reference, "
          f"{len(cpy.get('els') or {})} copy")
    print(f"  noise floor: {len(noise)} element(s) differ between two runs "
          f"of the SAME build")
    print(f"  DIFFERENCES beyond noise: {len(real)}")
    print(f"  missing from the copy: {len(missing)}"
          f"   (noise: {len(noise_missing)})")

    if missing:
        print("\n  ELEMENTS THE COPY DOES NOT HAVE:")
        for k in missing[:top]:
            print(f"     {describe(k)}")
        if len(missing) > top:
            print(f"     … and {len(missing) - top} more")

    # A hash that differs at one moment while every settled property
    # agrees is an animation caught at a different phase, not a defect.
    # Measured: 11 such candidates on qourvac2, every one a single
    # character of a split-text heading. Reporting those as findings
    # would bury the real ones.
    unstable = {k: v for k, v in real.items() if not v["props"]}
    real = {k: v for k, v in real.items() if v["props"]}
    if unstable:
        print(f"\n  UNSTABLE (differ at a moment, identical when settled): "
              f"{len(unstable)} — animation phase, not a defect")
        for k in list(unstable)[:5]:
            print(f"     {describe(k)}")

    if real:
        print("\n  ELEMENTS THAT BEHAVE DIFFERENTLY:")
        ranked = sorted(real.items(), key=lambda kv: -len(kv[1]["props"]))
        for k, v in ranked[:top]:
            print(f"     {describe(k):44} {len(v['props'])} propert"
                  f"{'y' if len(v['props']) == 1 else 'ies'}, "
                  f"{len(v['moments'])} moment(s)")
            for p in v["props"][:4]:
                print(f"          {p}: {str(v['a'].get(p))[:34]!r} -> "
                      f"{str(v['b'].get(p))[:34]!r}")
        if len(ranked) > top:
            print(f"     … and {len(ranked) - top} more")

    ok = not real and not missing
    if unstable and ok:
        print(f"\n  ({len(unstable)} element(s) were unstable between runs "
              f"of the same build and are not counted)")
    print(f"\n  VERDICT: " + ("IDENTICAL — nothing the copy does differs "
                              "from the original beyond measurement noise"
                              if ok else
                              f"{len(real) + len(missing)} difference(s) "
                              f"no check was looking for"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

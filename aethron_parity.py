#!/usr/bin/env python3
"""Does the copy do everything the original does? Element by element.

WHY THIS EXISTS

Every check Aethron had asked a summary question — how much text, how
many images, how many headings — and a port can pass all of them while
losing things a person notices in ten seconds. Measured on a real
migration that scored "text 100% identical, headings 40/40": a hover
card that follows the cursor was gone, hero cards stopped half way
through their travel, footer elements were missing, and text that
animated in the original sat still. Nothing in the report mentioned any
of it, because nothing was counting elements or their behaviour.

So this counts both. For every element in the original it asks:

  1. Is it THERE?              (presence — catches missing tabs, footers)
  2. Does it LOOK the same?    (computed style — catches design drift)
  3. Does it REACT the same?   (hover — catches lost interactions)
  4. Does it MOVE the same?    (load + scroll — catches lost animation)

A gap in any of the four is a defect, reported with the element that has
it, so the fix has an address instead of a hunch.

    python3 aethron_parity.py <project> [--page=index.html]
                              [--pair=original:migration]
                              [--pair=migration:port] [--json=out.json]

Exit code is 0 only when nothing is missing and nothing lost behaviour.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_motion as motion    # noqa: E402

# The properties that decide whether an element LOOKS the same. Deliberately
# not every property: shorthand and vendor-expanded ones differ harmlessly
# between two renders of the same CSS and would drown the real signal.
STYLE_PROPS = [
    "display", "position", "fontFamily", "fontSize", "fontWeight",
    "lineHeight", "letterSpacing", "color", "backgroundColor",
    "borderRadius", "textAlign", "textTransform", "justifyContent",
    "alignItems", "flexDirection", "gap", "overflow", "objectFit",
    "opacity", "transform", "zIndex",
]

PROBE = r"""
(function () {
  var STYLE_PROPS = %(props)s;
  var HOVER_WAIT = %(hover)d, SETTLE = %(settle)d, STOPS = %(stops)d;
  var POST = "%(post)s";
  var out = { items: {}, order: [], meta: {}, err: "" };

  /* ---- identity -----------------------------------------------------
     An element must be recognisable in BOTH builds, so the key cannot
     use anything a rebuild reshuffles: no nth-child (deleted badges
     shift every sibling after them), no generated ids. Tag + stable
     classes + a little of its own text is durable, and a running count
     disambiguates repeats. */
  var counts = {};
  function keyOf(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ""));
    var stable = cls.split(/\s+/).filter(function (c) {
      return c && !/^(w-condition-invisible|w--current|is-active)$/.test(c);
    }).sort().slice(0, 4).join(".");
    var own = "";
    for (var i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 3) { own += el.childNodes[i].nodeValue; }
    }
    own = own.trim().replace(/\s+/g, " ").slice(0, 24);
    var name = (el.getAttribute && (el.getAttribute("data-framer-name") ||
                el.getAttribute("data-w-id") || el.getAttribute("id"))) || "";
    var base = el.tagName + "|" + stable + "|" + name + "|" + own;
    counts[base] = (counts[base] || 0) + 1;
    return base + "#" + counts[base];
  }

  function styleOf(el) {
    var s = getComputedStyle(el), v = [];
    for (var i = 0; i < STYLE_PROPS.length; i++) {
      v.push(String(s[STYLE_PROPS[i]]));
    }
    return v;
  }
  function motionSig(el) {
    var s = getComputedStyle(el);
    return s.transform + "|" + s.opacity + "|" + s.filter + "|" +
           s.backgroundColor + "|" + s.color + "|" +
           Math.round(el.getBoundingClientRect().height);
  }
  function visible(el) {
    var r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" &&
           s.display !== "none";
  }

  /* interactive = worth hover-testing. Testing every node is too slow
     and mostly meaningless; these are the ones a person points at. */
  function interactive(el) {
    if (/^(A|BUTTON|SUMMARY|SELECT|LABEL)$/.test(el.tagName)) { return true; }
    if (el.getAttribute && el.getAttribute("data-w-id")) { return true; }
    var s = getComputedStyle(el);
    if (s.cursor === "pointer") { return true; }
    var cls = String(el.className || "");
    return /button|btn|card|item|link|tab|nav/i.test(cls);
  }

  function walk() {
    var all = document.querySelectorAll("body *");
    var list = [];
    for (var i = 0; i < all.length; i++) {
      if (!visible(all[i])) { continue; }
      if (/^(SCRIPT|STYLE|NOSCRIPT)$/.test(all[i].tagName)) { continue; }
      list.push(all[i]);
    }
    return list;
  }

  function snapshotAll(list) {
    for (var i = 0; i < list.length; i++) {
      var el = list[i], k = out.order[i];
      if (!k) { continue; }
      var rec = out.items[k];
      rec.motion[motionSig(el)] = 1;
    }
  }

  try {
    /* ---- 1. inventory + design ------------------------------------ */
    var list = walk();
    for (var i = 0; i < list.length; i++) {
      var el = list[i], k = keyOf(el);
      out.order.push(k);
      var r = el.getBoundingClientRect();
      out.items[k] = {
        tag: el.tagName,
        text: (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 40),
        style: styleOf(el),
        box: Math.round(r.width) + "x" + Math.round(r.height),
        inter: interactive(el) ? 1 : 0,
        hover: 0,          /* how many things change when hovered */
        motion: {}         /* distinct visual states seen over time  */
      };
    }
    out.meta.elements = list.length;

    /* ---- 2. what MOVES on its own --------------------------------- */
    var t0 = Date.now();
    (function settle() {
      snapshotAll(list);
      if (Date.now() - t0 < SETTLE) { setTimeout(settle, 120); return; }
      hoverPhase();
    })();

    /* ---- 3. what REACTS to a pointer -------------------------------
       The defect that started this: a card whose "view details" overlay
       follows the cursor simply did not exist in the port, and every
       content check passed. So hover each candidate and count how many
       elements change — zero where the original had many is a lost
       interaction. */
    function hoverPhase() {
      var cand = [];
      for (var i = 0; i < list.length; i++) {
        if (out.items[out.order[i]].inter) { cand.push(i); }
      }
      var n = 0;
      (function next() {
        if (n >= cand.length) { scrollPhase(); return; }
        var idx = cand[n++], el = list[idx];
        var before = [];
        for (var j = 0; j < list.length; j++) { before.push(motionSig(list[j])); }
        var r = el.getBoundingClientRect();
        var x = r.left + r.width / 2, y = r.top + r.height / 2;
        ["pointerover", "pointerenter", "mouseover", "mouseenter",
         "mousemove"].forEach(function (type) {
          try {
            el.dispatchEvent(new MouseEvent(type, {
              bubbles: true, cancelable: true, clientX: x, clientY: y }));
          } catch (e) { }
        });
        setTimeout(function () {
          var changed = 0;
          for (var j = 0; j < list.length; j++) {
            if (motionSig(list[j]) !== before[j]) { changed++; }
          }
          out.items[out.order[idx]].hover = changed;
          ["pointerout", "pointerleave", "mouseout", "mouseleave"]
            .forEach(function (type) {
              try {
                el.dispatchEvent(new MouseEvent(type, {
                  bubbles: true, cancelable: true }));
              } catch (e) { }
            });
          setTimeout(next, 20);
        }, HOVER_WAIT);
      })();
    }

    /* ---- 4. what MOVES on scroll ---------------------------------- */
    function scrollPhase() {
      var H = Math.max(document.body.scrollHeight,
                       document.documentElement.scrollHeight);
      var n = 0;
      (function step() {
        window.scrollTo(0, Math.round((H - innerHeight) * n / (STOPS - 1)));
        setTimeout(function () {
          snapshotAll(list);
          if (++n < STOPS) { step(); return; }
          finish();
        }, 260);
      })();
    }

    function finish() {
      for (var k in out.items) {
        out.items[k].states = Object.keys(out.items[k].motion).length;
        delete out.items[k].motion;
      }
      try { fetch(POST, { method: "POST", body: JSON.stringify(out) }); }
      catch (e) { }
    }
  } catch (e) {
    out.err = String(e && e.message).slice(0, 200);
    try { fetch(POST, { method: "POST", body: JSON.stringify(out) }); }
    catch (e2) { }
  }
})();
"""


def capture(root: Path, platform: str, page: str, win="1440,900",
            wait_s=240) -> dict:
    js = PROBE % {"props": json.dumps(STYLE_PROPS), "hover": 90,
                  "settle": 4000, "stops": 10, "post": "/__ae_capture"}
    return motion.capture_realtime(root, platform, page, js,
                                   wait_s=wait_s, win=win)


def compare(a: dict, b: dict) -> dict:
    """a = reference (what SHOULD happen), b = the copy."""
    A, B = a.get("items") or {}, b.get("items") or {}
    missing, style_gaps, hover_gaps, motion_gaps, extra = [], [], [], [], []

    for k, ref in A.items():
        got = B.get(k)
        if got is None:
            missing.append({"key": k, "tag": ref["tag"], "text": ref["text"],
                            "box": ref["box"]})
            continue
        diffs = [STYLE_PROPS[i] for i in range(min(len(ref["style"]),
                                                   len(got["style"])))
                 # transform/opacity are motion, judged separately: an
                 # element caught mid-animation is not a design defect
                 if ref["style"][i] != got["style"][i]
                 and STYLE_PROPS[i] not in ("transform", "opacity")]
        if diffs:
            style_gaps.append({"key": k, "text": ref["text"], "props": diffs})
        if ref.get("hover", 0) >= 2 and got.get("hover", 0) == 0:
            hover_gaps.append({"key": k, "tag": ref["tag"],
                               "text": ref["text"],
                               "original": ref["hover"], "port": 0})
        rs, gs = ref.get("states", 1), got.get("states", 1)
        if rs > 2 and gs <= 1:
            motion_gaps.append({"key": k, "text": ref["text"],
                                "original": rs, "port": gs, "dead": True})
        elif rs > 3 and gs < rs * 0.5:
            motion_gaps.append({"key": k, "text": ref["text"],
                                "original": rs, "port": gs, "dead": False})
    for k, got in B.items():
        if k not in A:
            extra.append({"key": k, "tag": got["tag"], "text": got["text"]})

    return {"reference_elements": len(A), "copy_elements": len(B),
            "missing": missing, "style_gaps": style_gaps,
            "hover_gaps": hover_gaps, "motion_gaps": motion_gaps,
            "extra": extra,
            "ok": not (missing or hover_gaps or motion_gaps)}


BUILDS = {
    "original": ("pristine", None),
    "migration": ("site", None),
    "port": ("convert-astro/dist", "static"),
    "port-next": ("convert-next/out", "static"),
}


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


def describe(d: dict) -> str:
    t = (d.get("text") or "").strip()
    tag = d.get("key", "").split("|")[0].lower()
    return f'{tag} "{t[:34]}"' if t else f'{tag} {d.get("box", "")}'


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 2
    proj = Path(args[0]).resolve()
    page = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--page=")), "index.html")
    pairs = [a.split("=", 1)[1] for a in argv if a.startswith("--pair=")]
    if not pairs:
        pairs = ["original:migration"]
        if (proj / "convert-astro/dist").is_dir():
            pairs.append("migration:port")

    seen, caps, worst = {}, {}, 0
    for pair in pairs:
        ref_name, copy_name = pair.split(":", 1)
        for name in (ref_name, copy_name):
            if name in caps:
                continue
            root, plat = resolve(proj, name)
            if not root:
                print(f"SKIPPED {name}: no such build")
                caps[name] = None
                continue
            if not (root / page).is_file():
                print(f"SKIPPED {name}: no {page}")
                caps[name] = None
                continue
            print(f"measuring {name} ({root.name}/{page}) …")
            caps[name] = capture(root, plat, page)

        a, b = caps.get(ref_name), caps.get(copy_name)
        if not a or not b or not a.get("available") or not b.get("available"):
            print(f"UNMEASURED {pair} — cannot compare, NOT proven equal")
            worst = max(worst, 2)
            continue

        r = compare(a, b)
        seen[pair] = r
        print(f"\n{'=' * 62}\n{ref_name.upper()}  ->  {copy_name.upper()}"
              f"   ({page})\n{'=' * 62}")
        print(f"elements: {r['reference_elements']} in {ref_name}, "
              f"{r['copy_elements']} in {copy_name}")

        def show(title, items, fmt):
            if not items:
                print(f"  {title}: none")
                return
            print(f"  {title}: {len(items)}")
            for d in items[:12]:
                print("     " + fmt(d))
            if len(items) > 12:
                print(f"     … and {len(items) - 12} more")

        show("MISSING ELEMENTS", r["missing"], describe)
        show("LOST HOVER BEHAVIOUR", r["hover_gaps"],
             lambda d: f"{describe(d)} — original changed "
                       f"{d['original']} element(s) on hover, copy 0")
        show("LOST MOTION", r["motion_gaps"],
             lambda d: f"{describe(d)} — {d['original']} states -> "
                       f"{d['port']}" + ("  (frozen)" if d["dead"] else ""))
        show("DESIGN DRIFT", r["style_gaps"],
             lambda d: f"{describe(d)} — {', '.join(d['props'][:4])}")
        if r["extra"]:
            print(f"  extra in {copy_name}: {len(r['extra'])}")
        print(f"\n  VERDICT: " + ("PARITY" if r["ok"] else "GAPS FOUND"))
        worst = max(worst, 0 if r["ok"] else 1)

    out = next((a.split("=", 1)[1] for a in argv if a.startswith("--json=")),
               None)
    if out:
        Path(out).write_text(json.dumps(seen, indent=1))
        print(f"\nwritten: {out}")
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

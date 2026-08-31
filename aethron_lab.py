#!/usr/bin/env python3
"""Aethron Lab — the instruments, shipped.

WHY THIS FILE EXISTS. Every animation defect found in this project was
found by a throwaway script: a side-by-side that noticed a card showed
eight states in the original and two in the port; a seek that read what
a viewer sees at t=350ms; a scroll-up that proved a size change was
reversible. Those scripts lived in a scratch directory and died with the
session. The capture and replay machinery shipped; the instruments that
judged it did not.

That asymmetry is the whole problem. A tool that can record and replay
an animation but cannot tell you its replay is WRONG needs a human
watching it. The referee graded text, headings and images, and reported
"99% identical" on a build whose cards were frozen and whose counters
were dead — every content check passing while the motion was broken.

So the instruments live here, permanently, and anything can drive them:
the CLI, an MCP agent, or the self-healer deciding whether its own fix
worked.

    python3 aethron_lab.py <tool> <project> [options]

    inventory    what animates on this page, by which mechanism
    properties   which CSS properties animate anywhere on the page
    compare      the SAME elements in the original and the port
    filmstrip    one element, frame by frame — for "it starts then hangs"
    reversible   is a motion scroll-linked, or a one-shot?
    seek         what a viewer sees at t=0,150,350… (entrances)
    trace        everything known about one selector
    vendor       fetch framer-motion's own source for reference

THE RULES THESE FOLLOW, inherited from aethron_motion:

1. OBSERVE, DO NOT CATALOGUE. Nothing here looks for "a marquee" or
   "a counter". It samples every observable property and calls whatever
   changes animated. Three recorders once narrowed to transform/opacity/
   filter and a whole category — cards animating WIDTH AND HEIGHT — was
   invisible to all of them.

2. REAL TIME, OR NOTHING. Under a virtual clock a badge rotating at
   71.86 deg/s measures 0.60, the appear engine never fires below the
   fold, and a counter's 1.2s count reads as an 8ms jump. Every
   instrument here runs in real seconds and posts its findings back.

3. WHAT CANNOT BE MEASURED IS REPORTED, NEVER ASSUMED. No browser means
   UNMEASURED, not "fine".
"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import forge                       # noqa: E402
import aethron_motion as motion    # noqa: E402

# ─────────────────────────── shared JS ───────────────────────────────

# Every instrument needs these three things, so they are written once
# and prepended. selectorOf/locate match aethron_motion's addressing so
# a finding here names an element the same way a capture does.
COMMON_JS = r"""
  function selectorOf(el) {
    var raw = el.className;
    var cls = String(raw && raw.baseVal !== undefined ? raw.baseVal
                                                      : (raw || ''));
    var framer = cls.split(/\s+/).filter(function (c) {
      return /^framer-[A-Za-z0-9]{4,}$/.test(c);
    });
    if (framer.length) return '.' + framer.join('.');
    var name = el.getAttribute && el.getAttribute('data-framer-name');
    if (name) return '[data-framer-name="' + name.replace(/"/g, '\\"') + '"]';
    for (var n = el.parentElement, up = 0; n && up < 6;
         n = n.parentElement, up++) {
      var praw = n.className;
      var pcls = String(praw && praw.baseVal !== undefined ? praw.baseVal
                                                           : (praw || ''));
      var pf = pcls.split(/\s+/).filter(function (c) {
        return /^framer-[A-Za-z0-9]{4,}$/.test(c);
      });
      if (pf.length) return '.' + pf.join('.') + ' ' + el.tagName.toLowerCase();
    }
    return '';
  }
  function locate(el) {
    var sel = selectorOf(el);
    if (!sel) return null;
    var all;
    try { all = [].slice.call(document.querySelectorAll(sel)); }
    catch (e) { return null; }
    var i = all.indexOf(el);
    return i < 0 ? null : { sel: sel, idx: i };
  }
  function post(o) {
    try { fetch("/__ae_capture", { method: 'POST',
                                   body: JSON.stringify(o) }); } catch (e) { }
  }
  function elements() {
    return [].slice.call(document.querySelectorAll('*')).filter(
      function (el) { return el.getClientRects().length; });
  }
"""


def _run(root: Path, platform: str, page: str, body: str, wait_s=90) -> dict:
    """Run one instrument against a build, in real time."""
    return motion.capture_realtime(root, platform, page,
                                   "(function(){" + COMMON_JS + body + "})();",
                                   wait_s=wait_s)


def _project(project) -> tuple:
    p = Path(project).resolve()
    cfg = json.loads((p / "forge.json").read_text(encoding="utf-8"))
    return p, cfg.get("platform", "static")


def _build_dirs(project: Path) -> dict:
    """Where the original and each port live."""
    out = {"original": project / "site"}
    for name, rel in (("astro", "convert-astro/dist"),
                      ("next", "convert-next/out"),
                      ("vite", "convert-vite/dist")):
        if (project / rel / "index.html").is_file():
            out[name] = project / rel
    return out


# ─────────────────────────── instruments ─────────────────────────────

INVENTORY_JS = r"""
  // Everything that moves, and by WHICH mechanism — the question
  // "is this animation missing" cannot be answered without it.
  var PROPS = %(props)s;
  var out = { items: [], meta: {} }, seen = {};
  function styleOf(el) {
    var s = getComputedStyle(el), v = [];
    for (var i = 0; i < PROPS.length; i++) {
      var x = s[PROPS[i]];
      v.push(x === 'none' || x === 'normal' || x === 'auto' ? '' : (x || ''));
    }
    var r = el.getBoundingClientRect();
    v.push(Math.round(r.width * 10) / 10 + 'x' + Math.round(r.height * 10) / 10);
    if (!el.children.length) v.push((el.textContent || '').trim().slice(0, 24));
    else v.push('');
    return v;
  }
  function key(el) { var w = locate(el); return w ? w.sel + '|' + w.idx : null; }

  function snap(tag) {
    var all = elements();
    for (var i = 0; i < all.length; i++) {
      var el = all[i], k = key(el);
      if (!k) continue;
      var rec = seen[k] || (seen[k] = { sel: k.split('|')[0],
                                        idx: +k.split('|')[1],
                                        vals: {}, waapi: 0, tags: {} });
      var v = styleOf(el).join('');
      rec.vals[v] = 1;
      rec.tags[tag] = 1;
      try { if ((el.getAnimations() || []).length) rec.waapi = 1; } catch (e) { }
    }
  }

  // three phases, because three mechanisms
  var t0 = Date.now();
  (function inPlace() {                       // entrances + time-driven
    snap('t' + (Date.now() - t0));
    if (Date.now() - t0 < 5000) setTimeout(inPlace, 220);
    else scrollPhase();
  })();
  function scrollPhase() {
    var H = Math.max(document.body.scrollHeight,
                     document.documentElement.scrollHeight);
    var n = 0, STOPS = 12;
    (function step() {
      window.scrollTo(0, Math.round((H - innerHeight) * n / (STOPS - 1)));
      setTimeout(function () {
        snap('y' + n);
        setTimeout(function () { snap('y' + n + 'b'); n++;
          if (n < STOPS) step(); else finish(); }, 240);
      }, 300);
    })();
  }
  function finish() {
    var names = PROPS.concat(['box', 'text']);
    for (var k in seen) {
      var r = seen[k], vals = Object.keys(r.vals);
      if (vals.length < 2) continue;
      // which columns moved?
      var cols = {}, split = vals.map(function (v) { return v.split(''); });
      for (var c = 0; c < names.length; c++) {
        var u = {};
        for (var i = 0; i < split.length; i++) u[split[i][c]] = 1;
        if (Object.keys(u).length > 1) cols[names[c]] = 1;
      }
      out.items.push({ sel: r.sel, idx: r.idx, states: vals.length,
                       props: Object.keys(cols), waapi: r.waapi });
    }
    out.meta.total = out.items.length;
    post(out);
  }
  setTimeout(finish, 90000);
"""


def inventory(project, page="index.html", build="original") -> dict:
    """What animates on this page, and by which mechanism."""
    proj, platform = _project(project)
    dirs = _build_dirs(proj)
    if build not in dirs:
        return {"available": False, "reason": f"no {build} build"}
    js = INVENTORY_JS % {"props": json.dumps(list(motion.WATCHED))}
    res = _run(dirs[build], platform if build == "original" else "static",
               page, js, wait_s=150)
    if not res.get("available"):
        return res
    items = res.get("items", [])
    # classify: WAAPI (entrance/marquee) vs rAF-driven vs scroll-only
    for it in items:
        it["mechanism"] = ("web-animation" if it.get("waapi")
                           else "javascript (rAF or scroll)")
    return {"available": True, "count": len(items), "items": items}


PROPERTIES_JS = r"""
  // Which CSS properties animate ANYWHERE on this page. The answer is
  // how you discover a category you were not watching: 'color' on 47
  // elements was a scroll-driven per-character ramp that every recorder
  // had been blind to.
  var PROPS = %(props)s;
  var out = { props: {}, meta: {} }, prev = {}, names = PROPS.concat(['box']);
  function styleOf(el) {
    var s = getComputedStyle(el), v = [];
    for (var i = 0; i < PROPS.length; i++) {
      var x = s[PROPS[i]];
      v.push(x === 'none' || x === 'normal' || x === 'auto' ? '' : (x || ''));
    }
    var r = el.getBoundingClientRect();
    v.push(Math.round(r.width * 10) / 10 + 'x' + Math.round(r.height * 10) / 10);
    return v;
  }
  function snap() {
    var all = elements();
    for (var i = 0; i < all.length; i++) {
      var el = all[i], w = locate(el);
      if (!w) continue;
      var k = w.sel + '|' + w.idx, v = styleOf(el), p = prev[k];
      if (p) {
        for (var c = 0; c < v.length; c++)
          if (v[c] !== p[c]) {
            var n = names[c];
            (out.props[n] = out.props[n] || { count: 0, examples: [] });
            if (!out.props[n].seen) out.props[n].seen = {};
            if (!out.props[n].seen[k]) {
              out.props[n].seen[k] = 1;
              out.props[n].count++;
              if (out.props[n].examples.length < 5)
                out.props[n].examples.push({ sel: w.sel, idx: w.idx,
                                             from: p[c], to: v[c] });
            }
          }
      }
      prev[k] = v;
    }
  }
  var t0 = Date.now();
  (function tick() {
    snap();
    if (Date.now() - t0 < 5000) { setTimeout(tick, 200); return; }
    var H = Math.max(document.body.scrollHeight,
                     document.documentElement.scrollHeight);
    var n = 0;
    (function step() {
      window.scrollTo(0, Math.round((H - innerHeight) * n / 11));
      setTimeout(function () {
        snap();
        setTimeout(function () { snap(); n++;
          if (n < 12) step(); else done(); }, 220);
      }, 300);
    })();
  })();
  function done() {
    for (var k in out.props) delete out.props[k].seen;
    post(out);
  }
  setTimeout(done, 90000);
"""


def properties(project, page="index.html", build="original") -> dict:
    """Which CSS properties animate anywhere on this page."""
    proj, platform = _project(project)
    dirs = _build_dirs(proj)
    if build not in dirs:
        return {"available": False, "reason": f"no {build} build"}
    js = PROPERTIES_JS % {"props": json.dumps(list(motion.WATCHED))}
    return _run(dirs[build], platform if build == "original" else "static",
                page, js, wait_s=150)


COMPARE_JS = r"""
  // The instrument that found every defect so far: watch the SAME
  // elements in two builds through the same load and the same scroll,
  // and count the distinct states each reaches. A card that reaches
  // eight states in one and two in the other is not the same card.
  var WANT = %(want)s;
  var out = { items: [], meta: {} }, rows = {}, watched = null;

  function targets() {
    var t = [];
    WANT.forEach(function (sel) {
      var els;
      try { els = document.querySelectorAll(sel); } catch (e) { return; }
      for (var i = 0; i < els.length && i < 4; i++) {
        t.push({ key: sel + '[' + i + ']', el: els[i] });
        var imgs = els[i].querySelectorAll('img');
        for (var j = 0; j < imgs.length && j < 2; j++)
          t.push({ key: sel + '[' + i + '] img' + j, el: imgs[j] });
      }
    });
    return t;
  }
  function read(el) {
    var s = getComputedStyle(el), r = el.getBoundingClientRect();
    return [s.transform === 'none' ? '' : s.transform, s.opacity,
            s.filter === 'none' ? '' : s.filter, s.color,
            Math.round(r.width) + 'x' + Math.round(r.height),
            el.children.length ? '' : (el.textContent || '').trim().slice(0, 20)
           ].join(' | ');
  }
  function snap(tag) {
    if (!watched) {
      watched = targets();
      watched.forEach(function (w) { rows[w.key] = []; });
    }
    watched.forEach(function (w) {
      if (!w.el.isConnected) return;
      var v = read(w.el), seq = rows[w.key];
      if (!seq.length || seq[seq.length - 1][1] !== v) seq.push([tag, v]);
    });
  }
  var t0 = Date.now();
  (function inPlace() {
    snap('t' + (Date.now() - t0));
    if (Date.now() - t0 < 5000) setTimeout(inPlace, 200);
    else scrollPhase();
  })();
  function scrollPhase() {
    var H = Math.max(document.body.scrollHeight,
                     document.documentElement.scrollHeight);
    var n = 0, STOPS = 14;
    (function step() {
      var y = Math.round((H - innerHeight) * n / (STOPS - 1));
      window.scrollTo(0, y);
      setTimeout(function () {
        snap('y' + y);
        setTimeout(function () { snap('y' + y + 'b'); n++;
          if (n < STOPS) step(); else finish(); }, 240);
      }, 300);
    })();
  }
  function finish() {
    for (var k in rows) if (rows[k].length) out.items.push({ key: k, seq: rows[k] });
    post(out);
  }
  setTimeout(finish, 90000);
"""


def compare(project, selectors, page="index.html", port="astro") -> dict:
    """The same elements in the original and a port, side by side.

    This is the instrument that found the frozen cards. It is the one
    that most needed to stop being a scratch file."""
    proj, platform = _project(project)
    dirs = _build_dirs(proj)
    if port not in dirs:
        return {"available": False, "reason": f"no {port} build to compare"}
    js = COMPARE_JS % {"want": json.dumps(list(selectors))}
    a = _run(dirs["original"], platform, page, js, wait_s=150)
    b = _run(dirs[port], "static", page, js, wait_s=150)
    if not a.get("available") or not b.get("available"):
        return {"available": False,
                "reason": a.get("reason") or b.get("reason") or "capture failed"}
    A = {i["key"]: i["seq"] for i in a.get("items", [])}
    B = {i["key"]: i["seq"] for i in b.get("items", [])}
    diffs = []
    for k in sorted(set(A) | set(B)):
        na, nb = len(A.get(k, [])), len(B.get(k, []))
        diffs.append({"key": k, "original_states": na, "port_states": nb,
                      "differs": na != nb,
                      "original": A.get(k, [])[:10],
                      "port": B.get(k, [])[:10]})
    return {"available": True, "port": port,
            "differing": sum(1 for d in diffs if d["differs"]),
            "items": diffs}


FILMSTRIP_JS = r"""
  // One element, every frame. For "it starts and then hangs": a
  // sequence that advances and stops names the frame it stopped on,
  // which a start/end comparison never can.
  var SEL = %(sel)s, IDX = %(idx)d, MS = %(ms)d, SCROLL = %(scroll)d;
  var out = { frames: [], meta: { sel: SEL, idx: IDX } };
  var el = null;
  try { el = document.querySelectorAll(SEL)[IDX]; } catch (e) { }
  if (!el) { out.meta.error = 'element not found'; post(out); }
  else {
    if (SCROLL >= 0) window.scrollTo(0, SCROLL);
    var t0 = performance.now();
    (function tick() {
      var s = getComputedStyle(el), r = el.getBoundingClientRect();
      var v = { t: Math.round(performance.now() - t0),
                transform: s.transform === 'none' ? '' : s.transform,
                opacity: s.opacity,
                filter: s.filter === 'none' ? '' : s.filter,
                color: s.color,
                box: Math.round(r.width) + 'x' + Math.round(r.height),
                text: el.children.length ? ''
                      : (el.textContent || '').trim().slice(0, 24) };
      var last = out.frames[out.frames.length - 1];
      if (!last || last.transform !== v.transform || last.opacity !== v.opacity
          || last.filter !== v.filter || last.box !== v.box
          || last.text !== v.text || last.color !== v.color)
        out.frames.push(v);
      if (performance.now() - t0 < MS) requestAnimationFrame(tick);
      else { out.meta.frames = out.frames.length; post(out); }
    })();
  }
"""


def filmstrip(project, selector, idx=0, ms=6000, scroll=-1,
              page="index.html", build="original") -> dict:
    """One element frame by frame — the instrument for "it hangs"."""
    proj, platform = _project(project)
    dirs = _build_dirs(proj)
    if build not in dirs:
        return {"available": False, "reason": f"no {build} build"}
    js = FILMSTRIP_JS % {"sel": json.dumps(selector), "idx": idx,
                         "ms": ms, "scroll": scroll}
    return _run(dirs[build], platform if build == "original" else "static",
                page, js, wait_s=max(60, ms // 1000 + 40))


REVERSIBLE_JS = r"""
  // Scroll-linked, or one-shot? The single question that decides how a
  // motion must be replayed. Scroll down through it, then back up the
  // same way: a scroll-linked element retraces its values, a one-shot
  // stays where it finished. Guessing produces animation that is half
  // right, which reads as broken.
  var SEL = %(sel)s, IDX = %(idx)d, FROM = %(from)d, TO = %(to)d;
  var out = { down: [], up: [], meta: { sel: SEL, idx: IDX } };
  var el = null;
  try { el = document.querySelectorAll(SEL)[IDX]; } catch (e) { }
  if (!el) { out.meta.error = 'element not found'; post(out); }
  else {
    function read() {
      var s = getComputedStyle(el), r = el.getBoundingClientRect();
      return { transform: s.transform === 'none' ? '' : s.transform,
               opacity: s.opacity, color: s.color,
               box: Math.round(r.width) + 'x' + Math.round(r.height) };
    }
    var ys = [], i;
    for (i = 0; i <= 8; i++) ys.push(Math.round(FROM + (TO - FROM) * i / 8));
    for (i = 7; i >= 0; i--) ys.push(Math.round(FROM + (TO - FROM) * i / 8));
    var n = 0;
    (function step() {
      window.scrollTo(0, ys[n]);
      setTimeout(function () {
        (n <= 8 ? out.down : out.up).push({ y: ys[n], v: read() });
        n++;
        if (n < ys.length) step();
        else {
          // Does the way up retrace the way down? WITH TOLERANCE: an
          // eased scroll effect lags slightly, so the same position
          // reads 1424x794 going down and 1419x792 coming back. Exact
          // string equality called that a one-shot at 25 percent
          // retraced — a wrong verdict on the one question that decides
          // how a motion must be replayed.
          //
          // Never write a bare percent sign in this payload: the whole
          // template is substituted by Python and a stray one breaks
          // it. The comment that first explained this contained two of
          // them and broke the very template it was warning about.
          function nums(s) {
            return (String(s).match(/-?\d*\.?\d+/g) || []).map(parseFloat);
          }
          function close(a, b) {
            var x = nums(a), y = nums(b);
            if (x.length !== y.length) return false;
            for (var i = 0; i < x.length; i++) {
              var scale = Math.max(1, Math.abs(x[i]), Math.abs(y[i]));
              if (Math.abs(x[i] - y[i]) / scale > 0.05) return false;
            }
            return true;
          }
          var same = 0, total = 0;
          out.up.forEach(function (u) {
            out.down.forEach(function (d) {
              if (d.y === u.y) {
                total++;
                if (close(d.v.box, u.v.box) && close(d.v.transform, u.v.transform))
                  same++;
              }
            });
          });
          out.meta.retraced = total ? Math.round(same / total * 100) : 0;
          out.meta.verdict = total === 0 ? 'inconclusive'
            : (same / total > 0.7 ? 'scroll-linked (reversible)'
                                  : 'one-shot (does not retrace)');
          post(out);
        }
      }, 420);
    })();
  }
"""


def reversible(project, selector, idx=0, y_from=0, y_to=6000,
               page="index.html", build="original") -> dict:
    """Is this motion scroll-linked, or a one-shot?"""
    proj, platform = _project(project)
    dirs = _build_dirs(proj)
    if build not in dirs:
        return {"available": False, "reason": f"no {build} build"}
    js = REVERSIBLE_JS % {"sel": json.dumps(selector), "idx": idx,
                          "from": y_from, "to": y_to}
    return _run(dirs[build], platform if build == "original" else "static",
                page, js, wait_s=120)


SEEK_JS = r"""
  // What a viewer SEES at a given moment. Entrances cannot be sampled
  // by waiting — two page loads have their own timing and the same
  // element is routinely mid-flight in one and finished in the other.
  // Seeking the animations reads the same instant in both.
  var SEL = %(sel)s, TIMES = %(times)s;
  var out = { profile: [], meta: { sel: SEL } };
  setTimeout(function () {
    var els;
    try { els = [].slice.call(document.querySelectorAll(SEL)); }
    catch (e) { els = []; }
    out.meta.found = els.length;
    var withAnims = 0;
    els.forEach(function (el) {
      try { if ((el.getAnimations() || []).length) withAnims++; } catch (e) { }
    });
    out.meta.withAnimations = withAnims;
    TIMES.forEach(function (T) {
      var row = { t: T, opacity: [], transform: [] };
      els.forEach(function (el) {
        try {
          (el.getAnimations() || []).forEach(function (a) {
            try { a.currentTime = T; } catch (e) { }
          });
        } catch (e) { }
        var s = getComputedStyle(el);
        row.opacity.push(+(+s.opacity).toFixed(3));
        row.transform.push(s.transform === 'none' ? '' : s.transform);
      });
      out.profile.push(row);
    });
    post(out);
  }, 1800);
"""


def seek(project, selector, times=(0, 150, 350, 600, 900, 1400),
         page="index.html", build="astro") -> dict:
    """Seek the animations and read what a viewer sees at each time."""
    proj, platform = _project(project)
    dirs = _build_dirs(proj)
    if build not in dirs:
        return {"available": False, "reason": f"no {build} build"}
    js = SEEK_JS % {"sel": json.dumps(selector), "times": json.dumps(list(times))}
    return _run(dirs[build], platform if build == "original" else "static",
                page, js, wait_s=90)


def trace(project, selector, idx=0, page="index.html") -> dict:
    """Everything known about one element, from every source at once.

    The question "why is this not animating" has five possible answers
    and they live in five places: the entrance recording, the continuous
    analysis, the scroll tracks, the recovered component source, and
    what the two builds actually do. Answering it by hand meant opening
    all five."""
    proj, _ = _project(project)
    found = {"selector": selector, "idx": idx}

    # 1. what the port shipped for it
    port = proj / "convert-astro/dist" / page
    if port.is_file():
        import re
        html = port.read_text(errors="replace")
        for tag, label in (("__ae_entrance", "entrance"),
                           ("__ae_continuous", "continuous"),
                           ("__ae_scroll", "scroll")):
            m = re.search(r'(?is)<script[^>]*id="%s"[^>]*>(.*?)</script\s*>'
                          % tag, html)
            if not m:
                continue
            try:
                spec = json.loads(m.group(1))
            except ValueError:
                continue
            hits = []
            for key in ("anims", "rotate", "loop", "tracks", "counters"):
                for item in (spec.get(key) or []):
                    if item.get("sel") == selector and item.get("idx", 0) == idx:
                        hits.append({"kind": key, **{k: v for k, v in
                                                     item.items()
                                                     if k != "frames"}})
            found[label] = hits or "not covered"

    # 2. which recovered component renders it
    anim = proj / "animations.json"
    if anim.is_file():
        doc = json.loads(anim.read_text(encoding="utf-8"))
        cls = selector.lstrip(".").split(".")[0].split(" ")[0]
        owners = [c["origin"] for c in doc.get("components", [])
                  if any(cls in k for k in (c.get("elements") or {}))]
        found["source_components"] = owners or "none matched"
    return found


# ───────────────────── the reference library ─────────────────────────

def vendor(dest: Path = None, version: str = "") -> dict:
    """Fetch framer-motion's own source, for reference.

    Aethron already recovers the TEMPLATE's authored components from
    source maps. This adds the ENGINE those components call — so a
    question like "what does a spring with bounce:0 actually do" has an
    answer in the repository instead of in someone's memory. MIT, so it
    can sit here; it is reference material, never shipped into a port."""
    dest = Path(dest or (ROOT / "reference"))
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(
                "https://registry.npmjs.org/framer-motion", timeout=30) as r:
            meta = json.loads(r.read())
        ver = version or meta["dist-tags"]["latest"]
        tarball = meta["versions"][ver]["dist"]["tarball"]
        lic = meta["versions"][ver].get("license", "?")
    except Exception as exc:
        return {"ok": False, "why": f"could not reach npm: {exc}"}

    out = dest / f"framer-motion-{ver}"
    if out.is_dir():
        return {"ok": True, "path": str(out), "version": ver,
                "license": lic, "note": "already present"}
    import io
    import tarfile
    try:
        with urllib.request.urlopen(tarball, timeout=120) as r:
            data = r.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and not m.name.startswith("/")
                       and ".." not in m.name]
            out.mkdir(parents=True, exist_ok=True)
            for m in members:
                m.name = m.name.split("package/", 1)[-1]
                tf.extract(m, out)
    except Exception as exc:
        return {"ok": False, "why": f"could not unpack: {exc}"}
    files = sum(1 for _ in out.rglob("*") if _.is_file())
    return {"ok": True, "path": str(out), "version": ver, "license": lic,
            "files": files}


# ───────────────────────────── CLI ───────────────────────────────────

def _print(obj, limit=40):
    print(json.dumps(obj, indent=1)[:20000])


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    tool = argv[0]
    rest = argv[1:]
    project = rest[0] if rest and not rest[0].startswith("-") else "."
    opt = {}
    for a in rest:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opt[k] = v

    if tool == "vendor":
        _print(vendor(version=opt.get("version", "")))
        return 0
    if tool == "inventory":
        r = inventory(project, opt.get("page", "index.html"),
                      opt.get("build", "original"))
        if r.get("available"):
            print(f"{r['count']} element(s) animate")
            for it in sorted(r["items"], key=lambda x: -x["states"])[:30]:
                print(f"  {it['sel'][:48]}[{it['idx']}]  {it['states']} states"
                      f"  {','.join(it['props'])}  [{it['mechanism']}]")
        else:
            print("UNMEASURED:", r.get("reason"))
        return 0
    if tool == "properties":
        r = properties(project, opt.get("page", "index.html"),
                       opt.get("build", "original"))
        if not r.get("available"):
            print("UNMEASURED:", r.get("reason"))
            return 1
        print("properties that animate on this page:")
        for name, d in sorted((r.get("props") or {}).items(),
                              key=lambda kv: -kv[1]["count"]):
            print(f"  {name:20} {d['count']:>4} element(s)")
            for e in d["examples"][:2]:
                print(f"       {e['sel'][:44]}[{e['idx']}]  "
                      f"{str(e['from'])[:28]} -> {str(e['to'])[:28]}")
        return 0
    if tool == "compare":
        sels = [s for s in opt.get("selectors", "").split(",") if s]
        if not sels:
            print("need --selectors=.a,.b")
            return 1
        r = compare(project, sels, opt.get("page", "index.html"),
                    opt.get("port", "astro"))
        if not r.get("available"):
            print("UNMEASURED:", r.get("reason"))
            return 1
        print(f"{r['differing']} of {len(r['items'])} differ "
              f"(original vs {r['port']})")
        for d in r["items"]:
            mark = "  <-- DIFFERENT" if d["differs"] else ""
            print(f"  {d['key'][:44]:46} original={d['original_states']:>3}"
                  f"  port={d['port_states']:>3}{mark}")
        return 0
    if tool == "filmstrip":
        r = filmstrip(project, opt.get("sel", ""), int(opt.get("idx", 0)),
                      int(opt.get("ms", 6000)), int(opt.get("scroll", -1)),
                      opt.get("page", "index.html"),
                      opt.get("build", "original"))
        if not r.get("available"):
            print("UNMEASURED:", r.get("reason"))
            return 1
        fr = r.get("frames", [])
        print(f"{len(fr)} distinct frame(s)")
        for f in fr[:40]:
            print(f"  t={f['t']:>6}  {f['box']:>12}  op={f['opacity']:<6}"
                  f"  {f['transform'][:38]}  {f['text'][:16]}")
        return 0
    if tool == "reversible":
        r = reversible(project, opt.get("sel", ""), int(opt.get("idx", 0)),
                       int(opt.get("from", 0)), int(opt.get("to", 6000)),
                       opt.get("page", "index.html"),
                       opt.get("build", "original"))
        if not r.get("available"):
            print("UNMEASURED:", r.get("reason"))
            return 1
        print("verdict:", r.get("meta", {}).get("verdict"),
              f"({r.get('meta', {}).get('retraced')}% retraced)")
        for d in r.get("down", []):
            print(f"  down y={d['y']:>6}  {d['v']['box']:>12}")
        for u in r.get("up", []):
            print(f"  up   y={u['y']:>6}  {u['v']['box']:>12}")
        return 0
    if tool == "seek":
        r = seek(project, opt.get("sel", ""),
                 [int(x) for x in opt.get("times", "0,150,350,600,900,1400")
                  .split(",")],
                 opt.get("page", "index.html"), opt.get("build", "astro"))
        if not r.get("available"):
            print("UNMEASURED:", r.get("reason"))
            return 1
        m = r.get("meta", {})
        print(f"{m.get('found')} element(s), "
              f"{m.get('withAnimations')} carrying animations")
        for row in r.get("profile", []):
            print(f"  t={row['t']:>5}  " +
                  "".join(f"{v:>6.2f}" for v in row["opacity"][:12]))
        return 0
    if tool == "trace":
        _print(trace(project, opt.get("sel", ""), int(opt.get("idx", 0)),
                     opt.get("page", "index.html")))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

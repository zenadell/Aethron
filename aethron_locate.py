#!/usr/bin/env python3
"""Find the part of a site a screenshot shows.

A person crops a piece of their site and says "make this bigger". Point-and-
edit can only name what was clicked; a picture can name anything, including
things a click cannot reach (a card under an overlay, a gap between two
sections, a whole row). This module answers: which page, where on it, and
which element in the code.

THE MATCH IS MEASURED, NOT GUESSED. Each built page is loaded in a real
browser, scrolled once so entrance animations have played, and photographed
whole. The crop is then searched for on that photograph by normalised
correlation, at the sizes a screenshot can come in (a Retina capture is 2x,
a preview is narrower than the window). The search runs in the browser's own
JavaScript, which is ~50x faster than Python for this. The element is the
one whose box overlaps the found area best.

    python3 aethron_locate.py <project-dir> <image> [--page about.html]
    python3 aethron_locate.py --selftest
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FOUND = 0.62       # below this the best place is reported, never trusted

# The matcher. Runs in the page after it was photographed: `crop` and `shot`
# are data URLs. Returns {score, x, y, w, h, k} in page CSS pixels.
MATCH_JS = r"""
(async (crop, shot, W, quick) => {
  const img = u => new Promise((ok, no) => { const i = new Image();
    i.onload = () => ok(i); i.onerror = () => no(new Error('image')); i.src = u; });
  const [C, P] = await Promise.all([img(crop), img(shot)]);
  const gray = (im, w, h) => { const c = document.createElement('canvas');
    c.width = w; c.height = h; const x = c.getContext('2d', {willReadFrequently: true});
    x.imageSmoothingQuality = 'high'; x.drawImage(im, 0, 0, w, h);
    const d = x.getImageData(0, 0, w, h).data, g = new Float32Array(w * h);
    for (let i = 0; i < w * h; i++) g[i] = .299*d[4*i] + .587*d[4*i+1] + .114*d[4*i+2];
    return g; };
  const pw = P.naturalWidth, ph = P.naturalHeight;
  const page = gray(P, pw, ph);
  const shrink = (g, w, h, f) => { const W2 = Math.floor(w / f), H2 = Math.floor(h / f),
      o = new Float32Array(W2 * H2);
    for (let y = 0; y < H2; y++) for (let x = 0; x < W2; x++) { let s = 0;
      for (let j = 0; j < f; j++) for (let i = 0; i < f; i++) s += g[(y*f+j)*w + x*f+i];
      o[y*W2+x] = s / (f*f); }
    return [o, W2, H2]; };
  const integral = (g, w, h) => { const S = new Float64Array((w+1)*(h+1)),
      Q = new Float64Array((w+1)*(h+1));
    for (let y = 0; y < h; y++) { let rs = 0, rq = 0;
      for (let x = 0; x < w; x++) { const v = g[y*w+x]; rs += v; rq += v*v;
        S[(y+1)*(w+1)+x+1] = S[y*(w+1)+x+1] + rs; Q[(y+1)*(w+1)+x+1] = Q[y*(w+1)+x+1] + rq; } }
    return [S, Q]; };
  const box = (T, w, x, y, a, b) => T[(y+b)*(w+1)+x+a] - T[y*(w+1)+x+a] - T[(y+b)*(w+1)+x] + T[y*(w+1)+x];
  // NCC of crop c (cw x ch) at every position of g (w x h), sampled
  const scan = (g, w, h, c, cw, ch, keep) => {
    const pts = [], step = Math.max(1, Math.floor(Math.sqrt(cw*ch / 600)));
    for (let y = 0; y < ch; y += step) for (let x = 0; x < cw; x += step) pts.push([x, y]);
    let mc = 0; for (const [x, y] of pts) mc += c[y*cw+x]; mc /= pts.length;
    let sc = 0; const cv = new Float32Array(pts.length), off = new Int32Array(pts.length);
    pts.forEach(([x, y], i) => { cv[i] = c[y*cw+x] - mc; sc += cv[i]*cv[i]; off[i] = y*w + x; });
    sc = Math.sqrt(sc / pts.length);
    if (sc < 2) return [];                       // a flat crop names nothing
    // mean and spread over the SAME points the crop is sampled at, or the
    // score is not a correlation (it read 1.5 on a plain card)
    const best = [], N = cv.length;
    for (let y = 0; y + ch <= h; y++) for (let x = 0; x + cw <= w; x++) {
      let s = 0, a = 0, q = 0; const b0 = y*w + x;
      for (let i = 0; i < N; i++) { const v = g[b0 + off[i]]; a += v; q += v*v; s += v * cv[i]; }
      const m = a / N, v = q / N - m*m;
      if (v < 4) continue;
      const r = s / (N * Math.sqrt(v) * sc);
      if (best.length < keep || r > best[best.length-1][0]) {
        best.push([r, x, y]); best.sort((a, b) => b[0] - a[0]);
        if (best.length > keep * 4) best.length = keep * 4; } }
    const out = [];                              // spread out, not one peak 10x
    for (const b of best) if (!out.some(o => Math.abs(o[1]-b[1]) < cw/2 && Math.abs(o[2]-b[2]) < ch/2)) {
      out.push(b); if (out.length >= keep) break; }
    return out; };
  const Cw = C.naturalWidth, Ch = C.naturalHeight, cand = [];
  // a screenshot comes in at 1x or 2x, and the page it shows may have been
  // narrower or wider than ours: sweep the sizes that can occur
  // the usual sizes first (Retina 2x, 1.5x, 1x); a sweep only when asked
  const ks = quick ? [0.5, 1, 2/3, 0.75, 0.8, 0.4] : [];
  if (!quick) for (let k = 0.3; k <= 1.25; k *= 1.05) ks.push(k);
  for (const k of ks) {
    const cw = Math.round(Cw * k), ch = Math.round(Ch * k);
    if (cw < 24 || ch < 12 || cw > pw || ch > ph) continue;
    const f = Math.max(1, Math.min(8, Math.floor(Math.min(cw, ch) / 14)));
    const [pg, gw, gh] = shrink(page, pw, ph, f);
    const cg = gray(C, Math.max(3, Math.floor(cw / f)), Math.max(3, Math.floor(ch / f)));
    for (const [r, x, y] of scan(pg, gw, gh, cg, Math.max(3, Math.floor(cw/f)),
                                 Math.max(3, Math.floor(ch/f)), 3))
      cand.push({r, x: x*f, y: y*f, cw, ch, k, f});
  }
  cand.sort((a, b) => b.r - a.r);
  let top = {score: -1};
  for (const c of cand.slice(0, 12)) {           // refine at full size
    const cg = gray(C, c.cw, c.ch);
    const x0 = Math.max(0, c.x - c.f), y0 = Math.max(0, c.y - c.f);
    const w = Math.min(pw - x0, c.cw + 2*c.f + 1), h = Math.min(ph - y0, c.ch + 2*c.f + 1);
    if (w < c.cw || h < c.ch) continue;
    const sub = new Float32Array(w*h);
    for (let y = 0; y < h; y++) sub.set(page.subarray((y0+y)*pw + x0, (y0+y)*pw + x0 + w), y*w);
    for (const [r, x, y] of scan(sub, w, h, cg, c.cw, c.ch, 1))
      if (r > top.score) top = {score: r, x: x0 + x, y: y0 + y, w: c.cw, h: c.ch, k: c.k};
  }
  return top;
})
"""

# The element at the found area: the one whose box overlaps it best.
ELEMENT_JS = r"""
((X, Y, w, h) => {
  const iou = r => { const ix = Math.max(0, Math.min(X+w, r.x+r.w) - Math.max(X, r.x)),
      iy = Math.max(0, Math.min(Y+h, r.y+r.h) - Math.max(Y, r.y)), i = ix*iy;
    return i / (w*h + r.w*r.h - i); };
  let best = null, bs = 0;
  for (const el of document.body.querySelectorAll('*')) {
    if (/^(SCRIPT|STYLE|LINK|META|NOSCRIPT|BR)$/.test(el.tagName)) continue;
    const b = el.getBoundingClientRect();
    if (b.width < 2 || b.height < 2) continue;
    const r = {x: b.left + scrollX, y: b.top + scrollY, w: b.width, h: b.height};
    const s = iou(r);
    if (s > bs + 0.02 || (s >= bs - 0.02 && best && best.contains(el) && s > 0.3)) {
      best = el; bs = Math.max(bs, s); } }
  if (!best) return null;
  const cls = [...best.classList].filter(c => !/^__forge|hover|active/.test(c)).slice(0, 8);
  const path = []; for (let e = best; e && e !== document.body && path.length < 6; e = e.parentElement) {
    let s = e.tagName.toLowerCase();
    if (e.id) { s += '#' + e.id; path.unshift(s); break; }
    const p = e.parentElement; if (p) { const sib = [...p.children].filter(x => x.tagName === e.tagName);
      if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(e) + 1})`; }
    path.unshift(s); }
  const name = e => e.getAttribute('data-framer-name') || '';
  const anc = []; for (let e = best.parentElement; e && anc.length < 4; e = e.parentElement)
    if (name(e) || e.id) anc.push({frname: name(e), id: e.id || ''});
  const open = best.outerHTML.slice(0, best.outerHTML.indexOf('>') + 1);
  const same = name(best) ? [...document.querySelectorAll(`[data-framer-name="${CSS.escape(name(best))}"]`)] : [];
  return {tag: best.tagName.toLowerCase(), open: open.slice(0, 400), path: path.join(' > '),
          frname: name(best), aeid: best.getAttribute('data-ae-id') || '', id: best.id || '',
          classes: cls, index: Math.max(0, same.indexOf(best)), overlap: +bs.toFixed(2),
          text: (best.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 240),
          ancestors: anc};
})
"""

REVEAL_JS = r"""
(async () => { const H = () => document.documentElement.scrollHeight;
  for (let y = 0; y < H() && y < 30000; y += innerHeight * 0.7) {
    scrollTo(0, y); await new Promise(r => setTimeout(r, 160)); }
  scrollTo(0, H()); await new Promise(r => setTimeout(r, 300));
  scrollTo(0, 0); await new Promise(r => setTimeout(r, 500));
  return H(); })()
"""


def _data_url(path: Path) -> str:
    raw = path.read_bytes()
    mime = "image/png" if raw[:4] == b"\x89PNG" else \
        "image/jpeg" if raw[:2] == b"\xff\xd8" else \
        "image/webp" if raw[8:12] == b"WEBP" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _pages(site: Path, first: str | None) -> list[str]:
    pages = sorted(str(p.relative_to(site)) for p in site.rglob("*.html")
                   if p.name not in ("404.html",) and "/assets/" not in
                   "/" + str(p.relative_to(site)))
    order = []
    for p in ([first] if first else []) + ["index.html"]:
        p = (p or "").lstrip("/")
        if p and not p.endswith(".html"):
            p = p.rstrip("/") + ".html"
        if p in pages and p not in order:
            order.append(p)
    return order + [p for p in pages if p not in order]


def _source_line(f: Path, el: dict) -> dict:
    try:
        src = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for how, needle in (("tag", el.get("open")),
                        ("framer name", el.get("frname") and
                         f'data-framer-name="{el["frname"]}"'),
                        ("id", el.get("aeid") and f'data-ae-id="{el["aeid"]}"'),
                        ("id", el.get("id") and f'id="{el["id"]}"')):
        if needle and len(needle) > 6:
            # the browser writes attributes in double quotes; the file may not
            pat = re.escape(needle).replace('"', "[\"']")
            hits = [m.start() for _, m in zip(range(50), re.finditer(pat, src))]
            if hits:
                at = hits[min(int(el.get("index") or 0), len(hits) - 1)
                          if how == "framer name" else 0]
                return {"line": src.count("\n", 0, at) + 1, "matched_by": how}
    return {}


def locate(project, image, page: str | None = None, width: int | None = None,
           limit_pages: int = 30) -> dict:
    """{ok, page, rect, score, element, file, line} — or {ok: False, why}."""
    sys.path.insert(0, str(ROOT))
    import aethron_browser as AB
    project, image = Path(project), Path(image)
    site = project / "site"
    if not image.is_file():
        return {"ok": False, "why": f"no image at {image}"}
    if not (site / "index.html").exists() and not list(site.glob("*.html")):
        return {"ok": False, "why": "the site is not built yet — build it first"}
    try:
        platform = json.loads((project / "forge.json").read_text()).get(
            "platform", "static")
    except (OSError, ValueError):
        platform = "static"
    base = AB.serve_site(site, platform)
    crop = _data_url(image)
    widths = [w for w in dict.fromkeys([width or 0, 1440, 1280]) if w]
    b = AB.Browser(width=widths[0], height=900)
    best = {"score": -1}
    STRONG = 0.8

    def match(W, p, shot, quick):
        nonlocal best
        m = b.eval(f"({MATCH_JS})({json.dumps(crop)},{json.dumps(shot)},"
                   f"{W},{'true' if quick else 'false'})", timeout=300) or {}
        if m.get("score", -1) > best["score"]:
            if b.url() != base + p:
                b.open(base + p)
                b.eval(REVEAL_JS, timeout=60)
            el = b.eval(f"({ELEMENT_JS})({m['x']},{m['y']},"
                        f"{m['w']},{m['h']})") if m.get("w") else None
            best = {**m, "page": p, "width": W, "element": el}
        return m.get("score", -1)
    try:
        b.start()
        for W in widths:
            b.page("Emulation.setDeviceMetricsOverride",
                   {"width": W, "height": 900, "deviceScaleFactor": 1,
                    "mobile": False})
            b.size = (W, 900)
            shots = {}
            # a quick look at every page (the one open in the preview first)
            for p in _pages(site, page)[:limit_pages]:
                b.open(base + p)
                b.eval(REVEAL_JS, timeout=60)
                shots[p] = ("data:image/png;base64," + base64.b64encode(
                    b.screenshot(full=True)).decode(), 0)
                shots[p] = (shots[p][0], match(W, p, shots[p][0], True))
                if best["score"] >= STRONG:
                    break
            # then every size, on the pages that came closest
            if best["score"] < STRONG:
                for p, (shot, _) in sorted(shots.items(),
                                           key=lambda kv: -kv[1][1])[:3]:
                    match(W, p, shot, False)
                    if best["score"] >= STRONG:
                        break
            if best["score"] >= FOUND:
                break
    except AB.BrowserError as e:
        return {"ok": False, "why": f"no browser could load the site: {e}"}
    finally:
        b.close()
    if best["score"] < 0:
        return {"ok": False, "why": "the picture is too small or too plain "
                                    "to find (crop a bit more around it)"}
    el = best.get("element") or {}
    out = {"ok": best["score"] >= FOUND, "score": round(best["score"], 3),
           "page": best["page"], "width": best["width"],
           "rect": {k: round(best[k]) for k in ("x", "y", "w", "h")},
           "element": el, "file": f"site/{best['page']}"}
    out.update(_source_line(site / best["page"], el))
    if not out["ok"]:
        out["why"] = (f"best place found is only {out['score']:.0%} alike — "
                      "it may be on a page not built yet, or from another site")
    return out


def describe(r: dict) -> str:
    """The lines the agent is given."""
    if not r.get("ok") and not r.get("element"):
        return f"Could not find the screenshot on the site: {r.get('why')}"
    el = r.get("element") or {}
    lines = [("The screenshot shows this part of the site"
              if r.get("ok") else "Best guess (NOT certain — "
              + str(r.get("why")) + ")") + f" ({r['score']:.0%} alike):",
             f"- Page: {r['page']} (at {r['rect']['x']},{r['rect']['y']}, "
             f"{r['rect']['w']}x{r['rect']['h']} px in a {r['width']}px window)",
             f"- File: {r['file']}" + (f", line {r['line']}" if r.get('line') else "")]
    if el.get("open"):
        lines.append("- Opening tag: `" + el["open"][:300].replace("`", "'") + "`")
    if el.get("path"):
        lines.append(f"- CSS path: {el['path']}")
    if el.get("frname"):
        lines.append(f'- Framer name: "{el["frname"]}"')
    anc = [a.get("frname") or ("#" + a["id"] if a.get("id") else "")
           for a in el.get("ancestors") or []]
    if any(anc):
        lines.append("- Inside: " + " › ".join(a for a in anc if a))
    if el.get("text"):
        lines.append(f"- Words in it: \"{el['text'][:200]}\"")
    return "\n".join(lines)


def _selftest() -> int:
    """Build a small site, photograph a card on its SECOND page at 2x, and
    find it — the right page, the right card, not its twin."""
    import shutil
    import tempfile
    sys.path.insert(0, str(ROOT))
    import aethron_browser as AB
    import forge
    if not forge._find_browser():
        print("SKIPPED — no browser")
        return 0
    ok = []

    def check(name, cond, info=""):
        ok.append(bool(cond))
        print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f" — {info}" if info and not cond else ""))
    d = Path(tempfile.mkdtemp(prefix="ae-locate-"))
    try:
        (d / "site").mkdir()
        (d / "forge.json").write_text('{"platform":"static"}')
        css = ("body{margin:0;font:16px sans-serif;background:#f6f6f2}"
               ".row{display:flex;gap:24px;padding:60px}"
               ".card{width:300px;height:220px;border-radius:18px;padding:20px}")
        (d / "site/index.html").write_text(
            f"<style>{css}</style><h1 style='padding:60px'>Home</h1>"
            "<div class=row><div class=card style='background:#cde'>A</div></div>")
        cards = "".join(f"<div class=card data-framer-name='Card {i}' "
                        f"style='background:hsl({i*70},60%,70%)'><b>Plan {i}</b>"
                        f"<p>{'Fast ' * (i+1)}</p></div>" for i in range(3))
        (d / "site/pricing.html").write_text(
            f"<style>{css}</style><h1 style='padding:60px'>Pricing</h1>"
            f"<div class=row data-framer-name=Plans>{cards}</div>"
            "<div style='height:1400px'></div>")
        b = AB.Browser(width=1200, height=900)
        b.start()
        b.page("Emulation.setDeviceMetricsOverride", {"width": 1200, "height": 900,
               "deviceScaleFactor": 2, "mobile": False})
        base = AB.serve_site(d / "site", "static")
        b.open(base + "pricing.html")
        r = b.eval("(()=>{const b=document.querySelector('[data-framer-name=\"Card 1\"]')"
                   ".getBoundingClientRect();return [b.left,b.top,b.width,b.height]})()")
        png = base64.b64decode(b.page("Page.captureScreenshot", {
            "format": "png", "clip": {"x": r[0] - 6, "y": r[1] - 6, "width": r[2] + 12,
                                      "height": r[3] + 12, "scale": 1}})["data"])
        b.close()
        (d / "crop.png").write_bytes(png)
        res = locate(d, d / "crop.png", width=1200)
        el = res.get("element") or {}
        check("found with confidence", res.get("ok"), json.dumps(res)[:300])
        check("on the right page", res.get("page") == "pricing.html", res.get("page"))
        check("the right card, not its twin", el.get("frname") == "Card 1", el.get("frname"))
        check("its line in the source", res.get("line"), res)
        check("described for the agent", "Card 1" in describe(res))
        (d / "flat.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nO3NMQEAAAjDMMC/"
            "52GDL4FAm1rTQgICAgICAgICAgICAgICAvgBpHMBVHTQRhsAAAAASUVORK5CYII="))
        flat = locate(d, d / "flat.png", width=1200)
        check("a plain picture is not 'found'", not flat.get("ok"), flat)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print(f"locate selftest {sum(ok)}/{len(ok)}")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "--selftest":
        sys.exit(_selftest())
    pg = a[a.index("--page") + 1] if "--page" in a else None
    r = locate(a[0], a[1], page=pg)
    print(describe(r))
    sys.exit(0 if r.get("ok") else 1)

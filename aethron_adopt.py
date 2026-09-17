#!/usr/bin/env python3
"""
ADOPT ANY PAGE — the piece that was missing between "Aethron can change the page it built"
and "Aethron can change anything".

Every measured-change path in this project reads `[data-ae-id]`: the probe, the timeline, the
simulated person, the AGM. Aethron stamps those ids only on pages IT built. Measured
2026-09-16 on the real projects already in this repo:

    agero       3,655 renderable elements     0 data-ae-id
    sadewa      4,605                          0
    acme-demo   2,028                          0
    test-2      1,386                          0

So on every page a user actually owns, `probe()` returned `els: {}` — nothing measurable,
therefore nothing changeable. Not a missing feature per request shape; one missing seam.

adopt() stamps stable ids onto ANY html file and PROVES the stamping changed nothing.

THE HARD PART IS THE MAPPING, AND IT IS NOT ASSUMED. A browser builds DOM the source never
spelled — an implied <tbody>, nodes written by script — so "the Nth element in the DOM is the
Nth tag in the file" is a guess, and this project has been burned by exactly that class of
guess before. Instead the browser returns the tag of EVERY element in document order, Python
reads the same sequence out of the source text, and the two are ALIGNED with difflib. An
element that does not fall inside an aligned block is SKIPPED AND COUNTED, never guessed at.

The ids go into the SOURCE, so they survive every later render, every rebuild, and are stable
across runs: re-adopting a page keeps the ids it already has and only numbers what is new.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

VIEWPORT_SLACK = 200        # --dump-dom's viewport is 87px shorter than the window asked for
MAX_ELEMENTS = 400          # the AGM records every computed style per element; this is the cap
MIN_BOX = 4                 # anything smaller than 4x4 is not a thing a person names
MIN_SURFACE = 400           # a painted box is only a surface if it has some area

# Elements that render nothing, and SVG internals (a picture is one thing, not 300 paths).
SKIP_TAGS = {"script", "style", "link", "meta", "title", "head", "html", "base", "noscript",
             "template", "br", "wbr", "source", "track", "param", "col", "colgroup"}

SELECT_TAIL = """<script>(function(){
var REPL=/^(A|BUTTON|INPUT|TEXTAREA|SELECT|LABEL|IMG|SVG|VIDEO|CANVAS|IFRAME|SUMMARY|PICTURE)$/;
var SKIP=/^(SCRIPT|STYLE|LINK|META|TITLE|HEAD|HTML|BASE|NOSCRIPT|TEMPLATE|BR|WBR|SOURCE|TRACK|PARAM|COL|COLGROUP)$/;
function own(el){var s='',c=el.childNodes,i;for(i=0;i<c.length;i++)if(c[i].nodeType===3)s+=c[i].nodeValue;
return s.replace(/\\s+/g,' ').trim();}
function run(){var all=document.querySelectorAll('*'),tags=[],pick=[],body=-1,i;
var SH=Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0),
SW=Math.max(document.documentElement.scrollWidth,document.body?document.body.scrollWidth:0);
for(i=0;i<all.length;i++){var el=all[i],T=el.tagName.toUpperCase();tags.push(el.tagName.toLowerCase());
if(T==='BODY'){body=i;continue;}
if(SKIP.test(T))continue;
try{if(T!=='SVG'&&el.closest&&el.closest('svg'))continue;}catch(e){}
var r=el.getBoundingClientRect();if(r.width<__MINBOX__||r.height<__MINBOX__)continue;
// PARKED OFF-SCREEN IS NOT ON THE PAGE. Measured on a real template: a hidden panel and its
// images sit at y≈122,000 on a page 20,651px tall — six times past its own end. Nobody sees
// them, nothing names them, and their geometry never settles, so they were the bulk of what
// made the page look unmeasurable. An element wholly outside the page's own scroll area is
// not a thing to measure; it is somewhere the design put things it is not showing.
var top=r.top+(window.scrollY||0),left=r.left+(window.scrollX||0);
if(top>SH||top+r.height<0||left>SW||left+r.width<0)continue;
var cs=getComputedStyle(el);
if(cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity||'1')<0.05)continue;
var area=r.width*r.height,why=null;
if(REPL.test(T))why='control';
else if(own(el))why='words';
else{var bc=cs.backgroundColor||'',paints=(bc&&bc!=='rgba(0, 0, 0, 0)'&&bc!=='transparent')
||(cs.backgroundImage&&cs.backgroundImage!=='none')
||(parseFloat(cs.borderTopWidth||'0')>0&&cs.borderTopStyle!=='none')
||(parseFloat(cs.borderLeftWidth||'0')>0&&cs.borderLeftStyle!=='none')
||(cs.boxShadow&&cs.boxShadow!=='none');
if(paints&&area>=__MINSURF__)why='surface';}
if(why)pick.push({i:i,tag:el.tagName.toLowerCase(),why:why,a:Math.round(area),
id:el.getAttribute('data-ae-id')||null,
x:Math.round(left),y:Math.round(top),
w:Math.round(r.width),h:Math.round(r.height),
t:(el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,60)});}
var links=document.querySelectorAll('link[rel~="stylesheet"]').length,sheets=0;
try{for(var s=0;s<document.styleSheets.length;s++){var ss=document.styleSheets[s];
try{if(ss.href&&ss.cssRules&&ss.cssRules.length)sheets++;}catch(e){sheets++;}}}catch(e){}
var o={tags:tags,pick:pick,body:body,n:all.length,links:links,sheets:sheets,
sh:Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0),
sw:document.documentElement.scrollWidth};
var d=document.createElement('script');d.type='application/json';d.id='__ae_adopt';
d.textContent=JSON.stringify(o);document.body.appendChild(d);}
// READ IT WHEN IT HAS STOPPED ARRIVING. The same rule the probe learned: a reading taken on
// a fixed timer catches a different stage of the page's loading every time, and then the
// verdict about which element an id sits on flips between runs on a page nobody touched.
function settled(cb){var last=performance.now(),obs=null;
try{obs=new MutationObserver(function(){last=performance.now();});
obs.observe(document.documentElement,{subtree:true,childList:true,attributes:true,characterData:true});}catch(e){}
function imgs(){var l=document.images;for(var i=0;i<l.length;i++){if(!l[i].complete)return false;}return true;}
function tick(){var now=performance.now();
var f=(!document.fonts)||document.fonts.status==='loaded';
if((f&&imgs()&&(now-last)>__QUIET__)||now>__CAP__){try{obs&&obs.disconnect();}catch(e){}cb();return;}
setTimeout(tick,50);}
setTimeout(tick,120);}
settled(function(){setTimeout(run,__AT__);});})();</script>"""


# ---------------------------------------------------------------- the browser's reading

def _render(path: Path, w, h, at_ms=1500):
    """Render the page IN ITS OWN DIRECTORY (so every relative asset resolves the way it does
    for a real visitor) and read back the tag of every element plus the ones worth naming."""
    import aethron_figma_grade as GR
    b = GR.find_browser()
    if not b:
        return None, "no browser"
    text = path.read_text(errors="ignore")
    import aethron_change as _C
    tail = (SELECT_TAIL.replace("__AT__", str(int(at_ms)))
            .replace("__QUIET__", str(_C.SETTLE_QUIET))
            .replace("__CAP__", str(_C.SETTLE_CAP))
            .replace("__MINBOX__", str(MIN_BOX)).replace("__MINSURF__", str(MIN_SURFACE)))
    page = _append_to_body(text, tail)
    tmp = path.with_name("__ae_adopt_probe.html")
    tmp.write_text(page)
    # A page whose assets are ROOT-ABSOLUTE (/assets/…) renders unstyled under file://.
    # Serving its folder is what a visitor's browser does, and it is the only way the
    # measurement is of the real page.
    import aethron_change as C
    origin = C.serve_assets(path.parent)
    url = f"{origin}/{tmp.name}" if origin else tmp.resolve().as_uri()
    prof = tempfile.mkdtemp(prefix="ae-adopt-")
    try:
        proc = subprocess.Popen(
            [b, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--user-data-dir={prof}", f"--window-size={w},{h + VIEWPORT_SLACK}",
             f"--virtual-time-budget={int(at_ms) + _C.SETTLE_CAP + 2500}", "--dump-dom", url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        dom, done = [], threading.Event()

        def pump():
            # Chrome does not exit after --dump-dom: read until </html>, then kill it.
            try:
                for line in proc.stdout:
                    dom.append(line)
                    if "</html>" in line:
                        break
            except Exception:
                pass
            done.set()
        threading.Thread(target=pump, daemon=True).start()
        done.wait(120)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    finally:
        shutil.rmtree(prof, ignore_errors=True)
        try:
            tmp.unlink()
        except OSError:
            pass
    m = re.search(r'<script type="application/json" id="__ae_adopt">(.*?)</script>',
                  "".join(dom), re.S)
    if not m:
        return None, "the browser returned nothing"
    try:
        return json.loads(m.group(1)), ""
    except ValueError:
        return None, "the browser's reading could not be read"


def _append_to_body(text, tail):
    """Append at the very END of the body. Anything inserted earlier would shift every element
    index after it, and the index is what the source mapping is built on."""
    low = text.lower()
    i = low.rfind("</body>")
    if i < 0:
        i = low.rfind("</html>")
    return text[:i] + tail + text[i:] if i >= 0 else text + tail


# ---------------------------------------------------------------- the source's reading

class _Tags(HTMLParser):
    """Every open tag in the file, in document order, with the exact offset at which an
    attribute can be inserted (just after `<tagname`) and any id it already carries."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text = text
        self.starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.starts.append(i + 1)
        self.items = []

    def _off(self):
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        self.items.append({"tag": tag, "at": self._off() + 1 + len(tag),
                           "id": dict(attrs).get("data-ae-id")})

    # <img/> reports as a start-end tag; it is still one element and one insertion point.
    handle_startendtag = handle_starttag


def source_tags(text):
    p = _Tags(text)
    p.feed(text)
    p.close()
    return p.items


def align(dom_tags, src_items):
    """Which source tag is each DOM element? difflib, not arithmetic: a browser invents
    elements the file never spelled. autojunk MUST be off — with thousands of <div>s the
    popular-element heuristic throws away exactly the anchors the alignment needs."""
    src_tags = [it["tag"] for it in src_items]
    sm = difflib.SequenceMatcher(None, dom_tags, src_tags, autojunk=False)
    out = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            out[a + k] = b + k
    return out


# ---------------------------------------------------------------- stamping

def _free_ids(taken, n):
    out, i = [], 0
    while len(out) < n:
        name = f"a{i:03d}"
        i += 1
        if name not in taken:
            out.append(name)
            taken.add(name)
    return out


def choose(pick, cap=MAX_ELEMENTS):
    """Controls and words always; painted surfaces fill what is left, largest first. A cap is
    a real constraint (the AGM records every computed style of every stamped element), so what
    it drops is returned rather than quietly lost."""
    keep = [p for p in pick if p["why"] in ("control", "words")]
    rest = sorted([p for p in pick if p["why"] == "surface"], key=lambda p: -p["a"])
    room = max(0, cap - len(keep))
    chosen = keep + rest[:room]
    dropped = rest[room:]
    if len(chosen) > cap:                       # more controls+words than the cap allows
        chosen.sort(key=lambda p: -p["a"])
        dropped = dropped + chosen[cap:]
        chosen = chosen[:cap]
    chosen.sort(key=lambda p: p["i"])
    return chosen, dropped


MAX_CANVAS_H = 20000        # a render taller than this costs more than it tells anyone


def canvas_of(dom, w, h):
    """THE CANVAS IS A MEASUREMENT, NOT A CSS RULE. Every measured path asks the page how big
    it is, and until now the answer came from `html,body{width:NNNpx;height:NNNpx}` — a marker
    only Aethron's OWN generated pages carry. A real page has no such rule, so the whole change
    path stopped at "the page's canvas could not be read". A real page's size is simply how
    much of it there is, which the browser already knows."""
    full = int(dom.get("sh") or h)
    capped = min(max(full, h), MAX_CANVAS_H)
    return {"w": int(w), "h": capped, "page_height": full,
            "capped": capped < full}


def _meta_tag(canvas):
    return f'<meta name="ae-canvas" content="{canvas["w"]}x{canvas["h"]}">'


def stamp(text, dom, cap=MAX_ELEMENTS, canvas=None):
    """Write data-ae-id into the source. Returns (new_text, report)."""
    src = source_tags(text)
    dom_tags = dom["tags"]
    m = align(dom_tags, src)
    taken = {it["id"] for it in src if it["id"]}
    chosen, dropped = choose(dom["pick"], cap)

    edits, named, unmapped, already = [], [], [], []
    body_i = dom.get("body", -1)
    if "bg" not in taken and body_i >= 0 and body_i in m:
        it = src[m[body_i]]
        if not it["id"]:
            edits.append((it["at"], ' data-ae-id="bg"'))
            taken.add("bg")
            named.append({"id": "bg", "tag": it["tag"], "why": "ground", "text": ""})

    need = []
    for p in chosen:
        if p["i"] not in m:
            unmapped.append(p)
            continue
        it = src[m[p["i"]]]
        if it["tag"] != p["tag"]:               # aligned, but not to the same kind of thing
            unmapped.append(p)
            continue
        if it["id"]:
            already.append(it["id"])
            continue
        need.append((p, it))

    for (p, it), name in zip(need, _free_ids(taken, len(need))):
        edits.append((it["at"], f' data-ae-id="{name}"'))
        named.append({"id": name, "tag": p["tag"], "why": p["why"], "text": p["t"],
                      "box": [p.get("x", 0), p.get("y", 0), p.get("w", 0), p.get("h", 0)]})

    out = text
    for at, ins in sorted(edits, key=lambda e: -e[0]):     # from the end, so offsets hold
        out = out[:at] + ins + out[at:]

    # The measured canvas travels WITH the page. Added after the ids are placed, so it cannot
    # disturb the offsets they were resolved against, and a <meta> draws nothing — the pixel
    # proof below still has to agree.
    if canvas and not re.search(r'name=["\']ae-canvas', out, re.I):
        m = re.search(r"<head\b[^>]*>", out, re.I)
        if m:
            out = out[:m.end()] + _meta_tag(canvas) + out[m.end():]
        else:
            mh = re.search(r"<html\b[^>]*>", out, re.I)
            out = ((out[:mh.end()] + "<head>" + _meta_tag(canvas) + "</head>" + out[mh.end():])
                   if mh else _meta_tag(canvas) + out)

    return out, {
        "canvas": canvas,
        "elements_in_dom": dom.get("n", len(dom_tags)),
        "worth_naming": len(dom["pick"]),
        "stamped": len(named),
        "already_had_an_id": len(already),
        "over_the_cap": len(dropped),
        "could_not_be_placed_in_the_source": len(unmapped),
        "named": named,
        "unmapped": [{"tag": p["tag"], "why": p["why"], "text": p["t"]} for p in unmapped[:20]],
    }


# ---------------------------------------------------------------- the proof

def _shoot(text, path: Path, w, h, name):
    import aethron_figma_grade as GR
    import aethron_vision as V
    f = path.with_name(f"__ae_adopt_{name}.html")
    png = path.with_name(f"__ae_adopt_{name}.png")
    f.write_text(text)
    try:
        if png.exists():
            png.unlink()
        GR.shoot(f, w, h, png)
        if not png.exists():
            return None
        return V.load(png)
    finally:
        for q in (f,):
            try:
                q.unlink()
            except OSError:
                pass


def _differs(a, b):
    """How many pixels differ, comparing EVERY pixel. The buffers are raw RGBA, so equality is
    one comparison; only a failure pays for the count."""
    if a.px == b.px:
        return 0
    n, w, h = 0, min(a.w, b.w), min(a.h, b.h)
    for y in range(h):
        ra, rb = y * a.w * 4, y * b.w * 4
        for x in range(w):
            i, j = ra + x * 4, rb + x * 4
            if (a.px[i] != b.px[j] or a.px[i + 1] != b.px[j + 1] or a.px[i + 2] != b.px[j + 2]):
                n += 1
    return n


def confirm(after, path: Path, w, h, named, at_ms=1500):
    """DID THE IDS LAND ON THE RIGHT THINGS? The pixel proof cannot answer that — writing an
    invisible attribute onto the wrong element is invisible too. So the stamped page is read
    BACK, and every id must be carrying the same kind of element, with the same words, that
    the browser picked it for."""
    tmp = path.with_name("__ae_adopt_confirm.html")
    tmp.write_text(after)
    try:
        dom, why = _render(tmp, w, h, at_ms)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if dom is None:
        return {"verdict": "UNPROVEN", "why": why}
    back = {p["id"]: p for p in dom["pick"] if p.get("id")}
    wrong, missing, drifting = [], [], []
    for n in named:
        if n["id"] == "bg":
            continue
        got = back.get(n["id"])
        if not got:
            missing.append(n)
            continue
        want, have = (n.get("text") or "").strip(), (got.get("t") or "").strip()
        if got["tag"] != n["tag"]:
            wrong.append({"id": n["id"], "expected": f"{n['tag']} {want[:40]}",
                          "found": f"{got['tag']} {have[:40]}"})
            continue
        if want == have or (want and have and (want in have or have in want)):
            continue
        # WORDS THAT CHANGE BY THEMSELVES ARE NOT A DIFFERENT ELEMENT — and WHICH element it
        # is, is a question about WHERE it is, not about how alike two strings are. Measured
        # on a real template: a heading with a typing caret read "Jomie█" on one pass and
        # "Jomie▓" on the next; a similarity threshold called that the same element on one
        # run and a different one on the next, so the whole verdict flipped between runs on a
        # page nobody had touched. A box in the same place, at the same size, with the same
        # tag, IS the same element whatever its words are doing — and a shuffled id lands
        # somewhere else entirely, which is exactly what the attack proves.
        # ITS ORIGIN, NOT ITS SIZE. A headline that types itself keeps its top-left and GROWS
        # as characters arrive, so comparing width and height reports the same element as a
        # different one — which is what made this verdict flip between runs on an untouched
        # page. Where a thing starts is its identity; how wide it is right now is what it is
        # doing. A shuffled id still lands at a different origin, which is what the attack
        # proves and the battery asserts.
        same_place = (n.get("box") and all(abs(n["box"][k] - got.get(j, -1e9)) <= 4
                                           for k, j in ((0, "x"), (1, "y"))))
        near = difflib.SequenceMatcher(None, want, have).ratio() if (want and have) else 0.0
        if same_place or near >= 0.75:
            drifting.append({"id": n["id"], "was": want[:40], "now": have[:40]})
        else:
            wrong.append({"id": n["id"], "expected": f"{n['tag']} {want[:40]}",
                          "found": f"{got['tag']} {have[:40]}"})
    why = (f"{len(wrong)} id(s) landed on a different element than the one they were picked for"
           if wrong else "every id read back as the element it was picked for")
    if not wrong and drifting:
        why += (f"; {len(drifting)} carry words that change by themselves and read slightly "
                f"differently each time")
    if not wrong and missing:
        why += (f"; {len(missing)} were not among the elements worth naming on the second "
                f"reading")
    return {"verdict": "REFUSED" if wrong else ("PROVEN" if not (missing or drifting)
                                                else "PROVEN (with gaps)"),
            "checked": len(named) - 1, "on_the_wrong_element": len(wrong),
            "not_found_when_read_back": len(missing), "words_change_by_themselves": len(drifting),
            "wrong": wrong[:8], "drifting": drifting[:5], "why": why}


def prove(before, after, path: Path, w, h):
    """Stamping an attribute must be invisible. Proven by rendering, not by reasoning — and a
    page that MOVES BY ITSELF is reported UNPROVEN, never passed and never failed, because two
    renders of it differ with nothing changed at all."""
    a1 = _shoot(before, path, w, h, "a1")
    if a1 is None:
        return {"verdict": "UNPROVEN", "why": "the page could not be rendered"}
    b = _shoot(after, path, w, h, "b")
    if b is None:
        return {"verdict": "UNPROVEN", "why": "the stamped page could not be rendered"}
    moved = _differs(a1, b)
    if moved == 0:
        return {"verdict": "PROVEN", "pixels_changed": 0,
                "why": "every pixel compared; the stamped page is the same picture"}
    a2 = _shoot(before, path, w, h, "a2")
    drift = _differs(a1, a2) if a2 is not None else None
    if drift is None:
        return {"verdict": "UNPROVEN", "pixels_changed": moved,
                "why": "the page could not be rendered a second time to check itself"}
    if drift >= moved / 5:
        return {"verdict": "UNPROVEN", "pixels_changed": moved, "own_drift": drift,
                "why": (f"the page moves by itself — two renders of the ORIGINAL differ by "
                        f"{drift} pixels, so a {moved}-pixel reading proves nothing")}
    return {"verdict": "CHANGED THE PAGE", "pixels_changed": moved, "own_drift": drift,
            "why": "stamping an attribute must be invisible and this was not"}


# ---------------------------------------------------------------- the whole thing

def _adopt(page, w=1414, h=900, cap=MAX_ELEMENTS, write=True, at_ms=1500, log=print,
           keep_serving=False):
    path = Path(page).resolve()
    if not path.is_file():
        return {"verdict": "SKIPPED", "why": f"{path} is not a file"}
    text = path.read_text(errors="ignore")
    have = len(re.findall(r'data-ae-id\s*=', text))

    log(f"reading {path.name} in a browser ({w}x{h}) …")
    dom, why = _render(path, w, h, at_ms)
    if dom is None:
        return {"verdict": "SKIPPED", "why": why, "had_ids": have}
    # A PAGE MEASURED UNSTYLED IS A DIFFERENT PAGE. Said out loud rather than measured
    # silently: this is exactly how a whole run can be correct about a document nobody
    # will ever see.
    links, sheets = int(dom.get("links") or 0), int(dom.get("sheets") or 0)
    dressed = {"stylesheets_declared": links, "stylesheets_that_loaded": sheets}
    if links and sheets < links:
        dressed["warning"] = (f"{links - sheets} of {links} stylesheet(s) did not load, so "
                              f"this page was measured UNSTYLED — what is measured is not "
                              f"what a visitor sees")
        log(f"  WARNING: {dressed['warning']}")

    canvas = canvas_of(dom, w, h)
    log(f"  {dom.get('n', 0)} elements, {len(dom['pick'])} worth naming, "
        f"canvas {canvas['w']}x{canvas['h']}"
        + (f" (the page is {canvas['page_height']}px tall; measuring the top "
           f"{canvas['h']}px)" if canvas["capped"] else ""))

    out, rep = stamp(text, dom, cap, canvas)
    rep["had_ids"] = have
    rep["styling"] = dressed
    if rep["stamped"] == 0:
        rep["verdict"] = "ALREADY ADOPTED" if have else "NOTHING TO NAME"
        rep["why"] = ("every element a person could name already carries an id" if have
                      else "the browser found nothing on this page worth naming")
        return rep
    log(f"  stamping {rep['stamped']} …")

    # Proven over the WHOLE page, not just the first screen — a stamp that moved something
    # below the fold would otherwise be invisible to its own proof.
    rep["proof"] = prove(text, out, path, w, canvas["h"])
    if rep["proof"]["verdict"] == "CHANGED THE PAGE":
        rep["verdict"] = "REFUSED"
        rep["why"] = rep["proof"]["why"]
        return rep

    rep["landed"] = confirm(out, path, w, h, rep["named"], at_ms)
    if rep["landed"]["verdict"] == "REFUSED":
        rep["verdict"] = "REFUSED"
        rep["why"] = rep["landed"]["why"]
        return rep

    if write:
        keep = path.with_name(path.name + ".before-adopt")
        if not keep.exists():
            keep.write_text(text)
        path.write_text(out)
        rep["written"] = str(path)
        rep["kept"] = str(keep)
    else:
        rep["html"] = out
    rep["verdict"] = "ADOPTED" if rep["proof"]["verdict"] == "PROVEN" else "ADOPTED (UNPROVEN)"
    rep["why"] = rep["proof"]["why"]
    return rep


def adopt(page, w=1414, h=900, cap=MAX_ELEMENTS, write=True, at_ms=1500, log=print,
          keep_serving=False):
    """Adopt a page, and never leave the machinery in a state the next render inherits."""
    import aethron_change as _C
    was = _C.ASSET_ORIGIN
    try:
        return _adopt(page, w, h, cap, write, at_ms, log, keep_serving)
    finally:
        if not keep_serving and not was:
            _C.stop_serving()


def needs_adopting(text):
    return len(re.findall(r'data-ae-id\s*=', text)) == 0


def report(rep, out=print):
    out(f"VERDICT: {rep.get('verdict')} — {rep.get('why', '')}")
    for k in ("elements_in_dom", "worth_naming", "stamped", "already_had_an_id",
              "over_the_cap", "could_not_be_placed_in_the_source"):
        if k in rep:
            out(f"  {k.replace('_', ' ')}: {rep[k]}")
    st = rep.get("styling") or {}
    if st.get("warning"):
        out(f"  WARNING: {st['warning']}")
    elif st.get("stylesheets_declared"):
        out(f"  styling: all {st['stylesheets_declared']} stylesheet(s) loaded")
    p = rep.get("proof")
    if p:
        out(f"  same picture: {p['verdict']} — {p.get('why', '')}")
    ld = rep.get("landed")
    if ld:
        out(f"  landed right:  {ld['verdict']} — {ld.get('why', '')}")
        for x in ld.get("wrong", []):
            out(f"    {x['id']}: picked {x['expected']} / found {x['found']}")
    for n in rep.get("named", [])[:12]:
        out(f"    {n['id']:>5}  {n['tag']:<8} {n['why']:<8} {n.get('text', '')[:44]}")
    if len(rep.get("named", [])) > 12:
        out(f"    … and {len(rep['named']) - 12} more")
    if rep.get("unmapped"):
        out(f"  NOT placed in the source ({rep['could_not_be_placed_in_the_source']}):")
        for u in rep["unmapped"][:5]:
            out(f"    {u['tag']:<8} {u['why']:<8} {u.get('text', '')[:40]}")


# ---------------------------------------------------------------- selftest (no browser)

def _selftest():
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL  {name}")

    src = ('<!doctype html><html><head><title>t</title></head><body>'
           '<div class="card"><h1>Hello</h1><img src="a.png"><button>Go</button></div>'
           '</body></html>')
    items = source_tags(src)
    tags = [i["tag"] for i in items]
    check("every open tag is read, in order",
          tags == ["html", "head", "title", "body", "div", "h1", "img", "button"])
    check("the insertion point is just after the tag name",
          src[items[4]["at"] - 4:items[4]["at"]] == "<div")

    dom = {"n": 8, "body": 3,
           "tags": ["html", "head", "title", "body", "div", "h1", "img", "button"],
           "pick": [{"i": 4, "tag": "div", "why": "surface", "a": 9000, "id": None, "t": ""},
                    {"i": 5, "tag": "h1", "why": "words", "a": 900, "id": None, "t": "Hello"},
                    {"i": 6, "tag": "img", "why": "control", "a": 400, "id": None, "t": ""},
                    {"i": 7, "tag": "button", "why": "control", "a": 400, "id": None, "t": "Go"}]}
    out, rep = stamp(src, dom)
    check("the ground is named bg", 'data-ae-id="bg"' in out and "<body data-ae-id" in out)
    check("four elements plus the ground were stamped", rep["stamped"] == 5)
    check("ids land on the right tags",
          '<div data-ae-id="a000" class="card">' in out and '<h1 data-ae-id="a001">' in out)
    check("the stamped page still parses to the same tags",
          [i["tag"] for i in source_tags(out)] == tags)

    again, rep2 = stamp(out, dom)
    check("adopting twice changes nothing", again == out and rep2["stamped"] == 0)
    check("it says so rather than renumbering", rep2["already_had_an_id"] == 4)

    # A browser element the file never spelled: an implied <tbody>. Everything after it
    # shifts by one, and arithmetic would stamp the wrong elements.
    cv = canvas_of({"sh": 4200}, 1414, 900)
    check("the canvas is the page's real height, not the window's", cv["h"] == 4200)
    check("and an absurd height is capped, and says so",
          canvas_of({"sh": 90000}, 1414, 900)["capped"] is True)
    out_c, _ = stamp(src, dom, canvas=cv)
    check("the measured canvas travels with the page",
          'name="ae-canvas" content="1414x4200"' in out_c)
    import aethron_edit as _AE
    check("and the manifest reads it back", _AE.manifest(out_c)["canvas"] == {"w": 1414, "h": 4200})
    check("re-stamping does not add a second canvas",
          stamp(out_c, dom, canvas=cv)[0].count("ae-canvas") == 1)

    src2 = '<html><head></head><body><table><tr><td>A</td></tr></table></body></html>'
    dom2 = {"n": 8, "body": 2,
            "tags": ["html", "head", "body", "table", "tbody", "tr", "td"],
            "pick": [{"i": 6, "tag": "td", "why": "words", "a": 500, "id": None, "t": "A"}]}
    out2, rep2b = stamp(src2, dom2)
    check("an element the source never spelled does not shift the mapping",
          "<td data-ae-id=" in out2 and rep2b["could_not_be_placed_in_the_source"] == 0)

    dom3 = {"n": 4, "body": 1, "tags": ["html", "body", "aside", "span"],
            "pick": [{"i": 2, "tag": "aside", "why": "words", "a": 500, "id": None, "t": "x"}]}
    out3, rep3 = stamp("<html><body><div>x</div></body></html>", dom3)
    check("an element that cannot be placed is skipped and counted",
          rep3["could_not_be_placed_in_the_source"] == 1 and "aside" not in out3)
    check("and nothing was stamped on the wrong tag — only the ground", rep3["stamped"] == 1)

    many = [{"i": 10 + k, "tag": "div", "why": "surface", "a": 1000 - k, "id": None, "t": ""}
            for k in range(20)]
    words = [{"i": 5, "tag": "p", "why": "words", "a": 10, "id": None, "t": "w"}]
    chosen, dropped = choose(words + many, cap=6)
    check("words are kept before surfaces when the cap bites",
          any(c["why"] == "words" for c in chosen) and len(chosen) == 6)
    check("the biggest surfaces are the ones kept",
          chosen[1]["a"] == 1000 and len(dropped) == 15)
    check("what the cap dropped is reported, not lost", len(chosen) + len(dropped) == 21)

    check("a page with no ids is seen to need adopting", needs_adopting("<p>x</p>"))
    check("a page with ids is not", not needs_adopting('<p data-ae-id="t0">x</p>'))

    big_src = ["<html><body>"] + [f"<div>{i}</div>" for i in range(1200)] + ["</body></html>"]
    big = "".join(big_src)
    big_dom = {"n": 1203, "body": 1,
               "tags": ["html", "body"] + ["div"] * 1200,
               "pick": [{"i": 2 + k, "tag": "div", "why": "words", "a": 100, "id": None,
                         "t": str(k)} for k in range(1200)]}
    o4, r4 = stamp(big, big_dom, cap=400)
    check("a real-sized page aligns without the junk heuristic eating it",
          r4["could_not_be_placed_in_the_source"] == 0)
    check("and the cap holds — 400 named elements, plus the ground",
          r4["stamped"] == 401 and r4["over_the_cap"] == 800)
    ids4 = re.findall(r'data-ae-id="([^"]+)"', o4)
    check("every stamped id is unique", len(set(ids4)) == len(ids4) == 401)

    print(f"adopt selftest: {ok} ok, {fail} failed")
    return fail == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stamp measurable ids onto any page, and prove "
                                             "the stamping changed nothing.")
    ap.add_argument("page", nargs="?")
    ap.add_argument("--width", type=int, default=1414)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--cap", type=int, default=MAX_ELEMENTS)
    ap.add_argument("--dry-run", action="store_true", help="do not write the file")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if _selftest() else 1
    if not a.page:
        ap.error("a page is required")
    rep = adopt(a.page, a.width, a.height, a.cap, write=not a.dry_run)
    report(rep)
    return 0 if rep.get("verdict", "").startswith(("ADOPTED", "ALREADY")) else 1


if __name__ == "__main__":
    sys.exit(main())

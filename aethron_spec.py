#!/usr/bin/env python3
"""Aethron does what nobody built it to do — and proves it did.

The owner, after watching each new kind of request need its own hand-built check: "if a user
comes up with something Aethron hasn't been trained or implemented to do yet, how would it do
it? It would reject the request, because it doesn't even know how to understand it." A fixed
menu of checks is not an agent. An agent works out what "done" means for a request it has never
seen, and proves it got there.

So a request is turned into TESTS before any code exists, the way a strong engineer works:

  1. TESTS FIRST. A model reads the request and the page's measured elements and writes
     scenarios in a small language of what a person does (hover, click, type, press a key,
     scroll, wait, drag) and what they would see (visible, hidden, words, colour or any CSS
     property, position, relation to another element, count, attributes, on top, inside the
     page, readable). No code is written in this step.
  2. THE TESTS ARE HELD TO THE WORDS. Every requirement quotes the request; together they must
     cover it; every requirement has a test; quoted words must be checked exactly; a test may
     only touch elements on the page or ones the change declares it will create.
  3. THE TESTS MUST FAIL ON THE PAGE AS IT IS. A test that already passes proves nothing.
  4. AN INDEPENDENT REVIEWER sees only the request and the tests — never the code — and names
     anything asked for that the tests do not check.
  5. THEN CODE, in a separate call that cannot change the tests.
  6. EVERY TEST MUST PASS on the changed page, with no script errors.
  7. EVERY PART OF THE CHANGE MUST BE NEEDED: Aethron removes each part in turn (the CSS, the
     script, each patch) and some test must fail. A part no test needs is either untested or
     something nobody asked for.
  8. NOTHING ELSE BREAKS, which needs no understanding of the request at all: every element not
     named measures exactly as before, nothing clickable is covered, no untagged structure, the
     page at rest is unchanged on screen, and anything the tests only act on still does and
     looks what it did.

Failures go back to the model with what was measured. Nothing is kept that has not passed all of it.
"""
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_change as C     # noqa: E402
import aethron_edit as AE      # noqa: E402

# The person already exists in aethron_change: pointer, clicks, keys, visibility, contrast.
PERSON_JS = C.INTERACT_TAIL[len("<script>(function(){"):C.INTERACT_TAIL.index("async function run(){try{")]

SCENARIO_RUN = """
var steps=SPEC.steps||[],res=[],pageErr=[];
addEventListener('error',function(e){pageErr.push(String((e&&e.message)||'script error'));});
function R(r){if(!r)return null;if(r.id)return q(r.id);try{var l=document.querySelectorAll(r.sel);return l[r.nth||0]||null;}catch(x){return null;}}
function nm(r){return r?(r.id||(r.sel+(r.nth?'['+r.nth+']':''))):'?';}
function toRgb(v){if(v===undefined||v===null)return null;var d=document.createElement('i');d.style.color=String(v);
if(!d.style.color)return null;document.body.appendChild(d);var c=getComputedStyle(d).color;d.parentNode.removeChild(d);
var m=c.match(/rgba?\\(([^)]+)\\)/);return m?m[1].split(',').map(parseFloat):null;}
function readText(el){if(el.tagName==='TEXTAREA'||el.tagName==='INPUT')return el.value||el.getAttribute('placeholder')||'';
return (el.innerText!==undefined?el.innerText:el.textContent)||'';}
function norm(s){return String(s===null||s===undefined?'':s).replace(/\\s+/g,' ').trim();}
function num(v){var m=String(v).match(/-?\\d+(\\.\\d+)?/);return m?parseFloat(m[0]):NaN;}
function boxOf(el){var b=el.getBoundingClientRect();return {x:b.left,y:b.top,w:b.width,h:b.height,right:b.right,bottom:b.bottom};}
function within(got,s,tol){if(isNaN(got))return false;if(s.equals!==undefined&&Math.abs(got-num(s.equals))>tol)return false;
if(s.min!==undefined&&got<num(s.min)-tol)return false;if(s.max!==undefined&&got>num(s.max)+tol)return false;return true;}
function typeIn(el,text){press(el);var v=('value' in el);for(var i=0;i<text.length;i++){var ch=text.charAt(i);
el.dispatchEvent(new KeyboardEvent('keydown',{key:ch,bubbles:true,cancelable:true}));
if(v){el.value=(el.value||'')+ch;}else if(el.isContentEditable){el.textContent=(el.textContent||'')+ch;}
try{el.dispatchEvent(new InputEvent('input',{bubbles:true,data:ch,inputType:'insertText'}));}catch(x){el.dispatchEvent(new Event('input',{bubbles:true}));}
el.dispatchEvent(new KeyboardEvent('keyup',{key:ch,bubbles:true,cancelable:true}));}
el.dispatchEvent(new Event('change',{bubbles:true}));}
function dragBy(el,dx,dy){var b=el.getBoundingClientRect(),x=b.left+b.width/2,y=b.top+b.height/2;point(el);
function ev(t,n,X,Y){var o={bubbles:true,cancelable:true,clientX:X,clientY:Y,view:window,buttons:1};
try{n.dispatchEvent(t.indexOf('pointer')===0?new PointerEvent(t,Object.assign({pointerType:'mouse',isPrimary:true,pointerId:1},o)):new MouseEvent(t,o));}
catch(x){n.dispatchEvent(new MouseEvent(t,o));}}
ev('pointerdown',el,x,y);ev('mousedown',el,x,y);
for(var i=1;i<=8;i++){var X=x+dx*i/8,Y=y+dy*i/8,t=document.elementFromPoint(X,Y)||document.body;ev('pointermove',t,X,Y);ev('mousemove',t,X,Y);}
var u=document.elementFromPoint(x+dx,y+dy)||document.body;ev('pointerup',u,x+dx,y+dy);ev('mouseup',u,x+dx,y+dy);}
async function act(s){var el=R(s.ref),n=nm(s.ref);
if(s.do==='wait'){await wait(Math.min(15000,Math.max(0,s.ms||0)));settle();return {ok:true};}
if(s.do==='leave'){point(far([]));await wait(350);settle();return {ok:true};}
if(s.do==='key'){key(String(s.key||'Enter'));await wait(350);settle();return {ok:true};}
if(s.do==='scroll'){if(el){el.scrollTop=s.y||0;}else{window.scrollTo(0,s.y||0);}await wait(350);settle();return {ok:true};}
if(!el)return {ok:false,why:'cannot '+s.do+' '+n+': it is not on the page'};
if(s.do==='hover')point(el);else if(s.do==='click')press(el);else if(s.do==='type')typeIn(el,String(s.text||''));
else if(s.do==='clear'){press(el);if('value' in el){el.value='';el.dispatchEvent(new Event('input',{bubbles:true}));}}
else if(s.do==='focus'){try{el.focus({preventScroll:true});}catch(x){}}else if(s.do==='blur'){try{el.blur();}catch(x){}}
else if(s.do==='drag')dragBy(el,s.dx||0,s.dy||0);
else if(s.do==='select'){el.value=String(s.value||'');el.dispatchEvent(new Event('change',{bubbles:true}));}
else return {ok:false,why:'unknown action '+s.do};
await wait(350);settle();return {ok:true};}
function check(s){var el=R(s.ref),n=nm(s.ref),e=s.expect;
if(e==='exists')return {ok:!!el,got:!!el};if(e==='missing')return {ok:!el,got:!!el};
if(e==='count'){var l=[];try{l=Array.prototype.slice.call(document.querySelectorAll(s.sel));}catch(x){return {ok:false,why:'bad selector '+s.sel};}
if(s.visible)l=l.filter(function(z){return opn(vis(z));});return {ok:within(l.length,s,0),got:l.length};}
if(!el)return {ok:false,why:n+' is not on the page'};
if(e==='visible'){var v=vis(el);return {ok:opn(v),got:{shown:v.shown,op:v.op,w:v.w,h:v.h,frac:v.frac}};}
if(e==='hidden'){var v2=vis(el);return {ok:hid(v2),got:{shown:v2.shown,op:v2.op,w:v2.w,h:v2.h}};}
if(e==='text'||e==='value'){var t=norm(e==='value'?el.value:readText(el)),lo=t.toLowerCase();
if(s.equals!==undefined&&t!==norm(s.equals))return {ok:false,got:t};
if(s.contains!==undefined&&lo.indexOf(norm(s.contains).toLowerCase())<0)return {ok:false,got:t};
if(s.not_contains!==undefined&&lo.indexOf(norm(s.not_contains).toLowerCase())>=0)return {ok:false,got:t};
if(s.matches!==undefined){try{if(!(new RegExp(s.matches)).test(t))return {ok:false,got:t};}catch(x){return {ok:false,why:'bad pattern'};}}
return {ok:true,got:t};}
if(e==='style'){var g=getComputedStyle(el).getPropertyValue(s.prop||''),tol=s.tol===undefined?1.5:s.tol;
if(s.equals!==undefined){var a=toRgb(s.equals),b=toRgb(g);
if(a&&b&&/(colou?r|background|border|fill|stroke|outline)/.test(s.prop||'')){
return {ok:Math.abs(a[0]-b[0])+Math.abs(a[1]-b[1])+Math.abs(a[2]-b[2])<=12,got:g};}
if(/^-?[\\d.]+(px|%|em|rem|s|ms|deg)?$/.test(String(s.equals).trim())&&!isNaN(num(g)))return {ok:Math.abs(num(g)-num(s.equals))<=tol,got:g};
return {ok:norm(g)===norm(s.equals),got:g};}
if(s.not_equals!==undefined)return {ok:norm(g)!==norm(s.not_equals),got:g};
return {ok:within(num(g),s,tol),got:g};}
if(e==='attr'){var at=el.getAttribute(s.name||'');if(s.exists)return {ok:at!==null,got:at};if(s.missing)return {ok:at===null,got:at};
return {ok:at!==null&&norm(at)===norm(s.equals),got:at};}
if(e==='box'){var bx=boxOf(el),gv=bx[s.prop||'y'];return {ok:within(gv,s,s.tol===undefined?2:s.tol),got:Math.round(gv*10)/10};}
if(e==='relation'){var o=R(s.to);if(!o)return {ok:false,why:nm(s.to)+' is not on the page'};
var A=boxOf(el),B=boxOf(o),tl=s.tol===undefined?2:s.tol,gx=Math.max(B.x-A.right,A.x-B.right,0),gy=Math.max(B.y-A.bottom,A.y-B.bottom,0),
gap=Math.max(gx,gy),ok2;
if(s.is==='below')ok2=A.y>=B.bottom-tl;else if(s.is==='above')ok2=A.bottom<=B.y+tl;else if(s.is==='left_of')ok2=A.right<=B.x+tl;
else if(s.is==='right_of')ok2=A.x>=B.right-tl;else if(s.is==='inside')ok2=A.x>=B.x-tl&&A.y>=B.y-tl&&A.right<=B.right+tl&&A.bottom<=B.bottom+tl;
else if(s.is==='overlaps')ok2=!(A.right<=B.x||A.x>=B.right||A.bottom<=B.y||A.y>=B.bottom);else if(s.is==='near')ok2=gap<=(s.gap_max===undefined?24:s.gap_max);
else return {ok:false,why:'unknown relation '+s.is};if(s.gap_max!==undefined&&gap>s.gap_max)ok2=false;
return {ok:ok2,got:{gap:Math.round(gap*10)/10,el:[Math.round(A.x),Math.round(A.y),Math.round(A.w),Math.round(A.h)],to:[Math.round(B.x),Math.round(B.y),Math.round(B.w),Math.round(B.h)]}};}
if(e==='on_top'||e==='clickable'){var vb=vis(el);return {ok:opn(vb)&&vb.onTop&&(e!=='clickable'||vb.pe!=='none'),got:{onTop:vb.onTop,cover:vb.cover,shown:vb.shown,op:vb.op}};}
if(e==='inside_page'){var vp=vis(el),pg=q('bg'),inb=true;if(pg){var P=boxOf(pg),A2=boxOf(el);
inb=A2.x>=P.x-1&&A2.y>=P.y-1&&A2.right<=P.right+1&&A2.bottom<=P.bottom+1;}return {ok:opn(vp)&&vp.frac>=0.95&&inb,got:{frac:vp.frac,clip:vp.clip}};}
if(e==='focused'){var ae=document.activeElement;return {ok:!!ae&&(ae===el||el.contains(ae)),got:ae?(ae.getAttribute('data-ae-id')||ae.tagName.toLowerCase()):null};}
if(e==='readable'){var it=items(el),cr=contrast(it.length?it[0]:el,el);return {ok:cr!==null&&cr>=(s.min||4.5),got:cr};}
return {ok:false,why:'unknown check '+e};}
async function run(){try{
for(var i=0;i<document.styleSheets.length;i++){try{emulate(document.styleSheets[i].cssRules,document.styleSheets[i]);}catch(x){}}
point(far([]));await wait(250);settle();
for(var k=0;k<steps.length;k++){var s=steps[k],r;
try{r=s.do?await act(s):check(s);}catch(x){r={ok:false,why:'threw: '+String((x&&x.message)||x)};}
res.push({i:k,ok:!!r.ok,got:r.got===undefined?null:r.got,why:r.why||null});}
}catch(x){out.errors.push(String((x&&x.message)||x));}
finally{out.results=res;out.pageErrors=pageErr;var d=document.createElement('script');d.type='application/json';d.id='__ae_scenario';
d.textContent=JSON.stringify(out);document.body.appendChild(d);}}
setTimeout(run,400);"""

SCENARIO_TAIL = "<script>(function(){" + PERSON_JS + SCENARIO_RUN + "})();</script>"

ACTIONS = {"hover", "leave", "click", "type", "clear", "key", "focus", "blur", "scroll", "wait", "drag", "select"}
CHECKS = {"exists", "missing", "visible", "hidden", "text", "value", "count", "style", "attr", "box", "relation",
          "on_top", "clickable", "inside_page", "focused", "readable"}
# Checks that say an existing element's own state is part of the change. visible / on_top /
# clickable / exists say it must still be there and usable, which licenses nothing.
CHANGING = {"text", "value", "style", "attr", "box", "hidden", "missing"}
LIBRARY = ("size", "position", "text", "color", "colors", "removed", "added", "changes_over_time", "appears_on",
           "style_on", "moves")
STOP = set((
    "a an the and or but if then else when while to of on in at by for with from into onto it its this that these "
    "those i im me my mine you your we our they them he she his her is are was were be been being am do does did "
    "done can could would should will shall may might must please kindly just maybe like um uh uhm er hey okay ok so "
    "also too very really some something thing things stuff etc actually basically make made let have has had get got "
    "want wanted need needed there here how what which who whom whose where why yes aethron atron etron ytron trunk "
    "able possible sure way know mean means understand see look lets going gonna").split())


# ───────────────────────────── running a scenario ─────────────────────────────

def _ref(step, key="el"):
    v = step.get(key)
    if isinstance(v, str) and v:
        return {"id": v}
    if isinstance(v, dict) and (v.get("id") or v.get("sel")):
        return {k: v[k] for k in ("id", "sel", "nth") if k in v}
    if key == "el" and isinstance(step.get("sel"), str) and step["sel"] and step.get("expect") != "count":
        return {"sel": step["sel"], "nth": int(step.get("nth") or 0)}
    return None


_RUNS = {}


def run_scenario(html, workdir, w, h, scenario):
    """One scenario, in its own render: a person does each step; each check is read."""
    steps = [dict(s, ref=_ref(s), to=_ref(s, "to")) for s in scenario.get("steps") or [] if isinstance(s, dict)]
    spec = {"steps": steps}
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h, json.dumps(spec, sort_keys=True))
    if key in _RUNS:
        return json.loads(_RUNS[key])
    waits = sum(min(15000, max(0, int(s.get("ms") or 0))) for s in steps if s.get("do") == "wait")
    budget = min(60000, 2500 + waits + 500 * len(steps))
    tail = SCENARIO_TAIL.replace("__SPEC__", json.dumps(spec).replace("</", "<\\/"))
    page = html.replace("</body>", tail + "</body>", 1) if "</body>" in html else html + tail
    got = C._dump(page, workdir, w, h, budget, "scenario", "__ae_scenario")
    if got is not None:
        _RUNS[key] = json.dumps(got)
    return got


def say_step(s):
    ref = s.get("el") if isinstance(s.get("el"), str) else (s.get("el") or {}).get("sel") if isinstance(
        s.get("el"), dict) else s.get("sel")
    if s.get("do"):
        extra = {"type": f" {s.get('text')!r}", "wait": f" {s.get('ms')}ms", "key": f" {s.get('key')}",
                 "drag": f" by {s.get('dx', 0)},{s.get('dy', 0)}", "scroll": f" to y {s.get('y')}"}.get(s["do"], "")
        return f"{s['do']} {ref or ''}".strip() + extra
    bits = [f"expect {s.get('expect')} {ref or ''}".strip()]
    for k in ("prop", "is", "to", "name", "equals", "contains", "not_contains", "matches", "min", "max", "gap_max",
              "sel"):
        if k in s and not (k == "sel" and s.get("expect") != "count"):
            bits.append(f"{k}={s[k]!r}")
    return " ".join(bits)


def outcome(scenario, run, known_errors=()):
    """-> {'passed', 'failures': [words], 'ran'} or None when it could not run.

    `known_errors` are the scripts errors the page ALREADY threw before anything was changed.
    A real template ships them — test-2 refused a correct recolour four times over
    `gsap is not defined`, which is the migration's own pre-existing fault and has nothing to
    do with the request. Blaming a change for a fault that predates it is the same error as
    passing a change that caused one: this project's oldest rule is to compare against the
    untouched original rather than against zero."""
    if run is None:
        return None
    steps = [s for s in scenario.get("steps") or [] if isinstance(s, dict)]
    results = run.get("results") or []
    failures = []
    for r in results:
        if not r.get("ok") and r.get("i", -1) < len(steps):
            s = steps[r["i"]]
            got = r.get("why") or f"got {json.dumps(r.get('got'))[:160]}"
            failures.append(f"step {r['i'] + 1} ({say_step(s)}): {got}")
    if len(results) < len(steps):
        failures.append(f"only {len(results)} of {len(steps)} steps ran")
    fresh = [e for e in (run.get("pageErrors") or []) if e not in set(known_errors)]
    for e in fresh[:2]:
        failures.append(f"the page threw: {e}")
    for e in (run.get("errors") or [])[:2]:
        failures.append(f"the test runner failed: {e}")
    return {"passed": not failures, "failures": failures}


# ───────────────────────────── holding the tests to the words ─────────────────────────────

def _stem(w):
    return w[:5] if len(w) > 5 else w


def content_words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 1]


def validate_spec(spec, request, pb):
    """Are these tests about THIS request, and can they run? Checked before anything renders."""
    if not isinstance(spec, dict):
        return ["the reply was not the JSON asked for"]
    problems = []
    reqs = spec.get("requirements") if isinstance(spec.get("requirements"), list) else []
    scs = spec.get("scenarios") if isinstance(spec.get("scenarios"), list) else []
    claims = [c for c in spec.get("claims") or [] if isinstance(c, dict)]
    new_ids = [i for i in spec.get("new_ids") or [] if isinstance(i, str)]
    existing = set((pb or {}).get("els") or {})
    if not reqs:
        problems.append("no requirements were written")
    if not scs and not claims:
        problems.append("no tests were written")
    if len(scs) > 12:
        problems.append(f"{len(scs)} tests is more than 12 — test what was asked, not everything")
    for i in new_ids:
        if i in existing:
            problems.append(f"new id {i} already exists on the page — choose an id that does not")
    words = {}
    for w in content_words(C._unquoted(request)):
        words.setdefault(_stem(w), w)
    covered = set()
    for n, r in enumerate(reqs):
        says = str((r or {}).get("says") or "") if isinstance(r, dict) else ""
        stems = {_stem(w) for w in content_words(says)}
        if not stems:
            problems.append(f"requirement {n + 1} does not quote the request")
            continue
        foreign = sorted(stems - set(words))
        if len(foreign) > 0.25 * len(stems):
            problems.append(f"requirement {n + 1} says {says!r}, but these words are not in the request: "
                            + ", ".join(foreign))
        covered |= stems & set(words)
    missing = [words[s] for s in words if s not in covered]
    if words and len(missing) > 0.15 * len(words):
        problems.append("the requirements leave parts of the request out: " + ", ".join(missing))
    texts = []
    for sc in scs:
        for s in (sc or {}).get("steps") or [] if isinstance(sc, dict) else []:
            if isinstance(s, dict):
                texts += [str(s.get(k)) for k in ("equals", "contains", "matches") if s.get(k) is not None]
    for c in claims:
        texts += [str(c.get(k)) for k in ("equals",) if c.get(k) is not None]
    for quoted in re.findall(r'"([^"]+)"|“([^”]+)”', request or ""):
        q = next(x for x in quoted if x)
        if not any(q.strip().lower() in t.lower() for t in texts):
            problems.append(f"the request gives the exact words {q!r}, and no test checks them")
    tested = set()
    for n, sc in enumerate(scs):
        if not isinstance(sc, dict):
            problems.append(f"test {n + 1} is not an object")
            continue
        name = sc.get("name") or f"test {n + 1}"
        steps = sc.get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append(f"'{name}' has no steps")
            continue
        if len(steps) > 40:
            problems.append(f"'{name}' has {len(steps)} steps — at most 40")
        ri = sc.get("requirement")
        if not sc.get("keeps"):
            if not isinstance(ri, int) or isinstance(ri, bool) or not 0 <= ri < len(reqs):
                problems.append(f"'{name}' does not name the requirement it tests")
            else:
                tested.add(ri)
        if not any(isinstance(s, dict) and s.get("expect") for s in steps):
            problems.append(f"'{name}' checks nothing")
        for s in steps:
            if not isinstance(s, dict) or not (s.get("do") or s.get("expect")):
                problems.append(f"'{name}' has a step that is neither an action nor a check: {json.dumps(s)[:80]}")
                continue
            if s.get("do") and s["do"] not in ACTIONS:
                problems.append(f"'{name}' uses an unknown action {s['do']!r}")
            if s.get("expect") and s["expect"] not in CHECKS:
                problems.append(f"'{name}' uses an unknown check {s['expect']!r}")
            if s.get("do") == "wait" and not 0 <= int(s.get("ms") or 0) <= 15000:
                problems.append(f"'{name}' waits longer than 15 seconds")
            for key in ("el", "to"):
                v = s.get(key)
                if isinstance(v, str) and v not in existing and v not in new_ids:
                    problems.append(f"'{name}' refers to {v}, which is not on the page and not in new_ids")
    for c in claims:
        if c.get("kind") not in LIBRARY:
            problems.append(f"unknown claim kind {c.get('kind')!r}")
        if isinstance(c.get("requirement"), int) and 0 <= c["requirement"] < len(reqs):
            tested.add(c["requirement"])
    for n, r in enumerate(reqs):
        if n not in tested:
            problems.append(f"requirement {n + 1} ({str((r or {}).get('says'))[:60]!r}) has no test")
    return problems


MEASURABLE_CLAIMS = ("size", "position", "colors", "removed", "added", "color", "text", "changes_over_time")


def measure_claims(page, claims, pb, workdir, w, h, request="", name="claims"):
    """Each claim that can be measured by looking — not by acting on the page — measured on `page`
    against the page as it was (`pb`): [(claim, ok, words)]. A claim that cannot be read is left out,
    never counted as passing or failing."""
    use = [c for c in claims or () if isinstance(c, dict) and c.get("kind") in MEASURABLE_CLAIMS]
    if not use or not pb:
        return []
    pm = C.probe(page, workdir, w, h, 1200)
    if not pm or not pm.get("els"):
        return []
    png = pl = None
    if any(c.get("kind") == "colors" for c in use):
        import aethron_figma_grade as GR
        f, out = Path(workdir) / f"_{name}.html", Path(workdir) / f"_{name}.png"
        f.write_text(page)
        if out.exists():
            out.unlink()
        GR.shoot(f, w, h, out)
        png = out if out.exists() else None
    watch = sorted({c["id"] for c in use if c.get("kind") == "changes_over_time" and isinstance(c.get("id"), str)})
    if watch:
        pl = C.timeline(page, workdir, w, h, watch)
    got = []
    for c in use:
        if (c.get("kind") == "colors" and png is None) or (c.get("kind") == "changes_over_time" and not pl):
            continue
        try:
            ok, words = C.check_claim(c, pb, pm, pl, png, w, h, request)
        except Exception:
            continue
        got.append((c, ok, words))
    return got


def red(before, spec, workdir, w, h, request="", pb=None):
    """Run the tests on the page AS IT IS. New-behaviour tests must fail; keep-tests must pass.
    A measured claim is a test too, and must fail today. A test that already passes today proves
    nothing — it is refused, unless a claim that FAILS today proves the same requirement, in which case
    the empty test is left out. Measured live: a correct colour claim for "orange blending with black"
    was refused three times because the model also added "the background is visible"."""
    notes, problems = [], []
    failing = {}
    for c, ok, words in measure_claims(before, spec.get("claims"), pb, workdir, w, h, request, "red_before"):
        if ok:
            continue
        notes.append(f"the {c.get('kind')} claim fails on the page as it is, as it should: {words[:200]}")
        req = c.get("requirement")
        if isinstance(req, int) and not isinstance(req, bool):
            failing.setdefault(req, c)
    kept = []
    # WHAT THE PAGE ALREADY THROWS IS NOT THE CHANGE'S FAULT. Seeded from the page's own
    # measured reading rather than from whatever a scenario happens to trip: script errors
    # are TIMING-DEPENDENT, so the same broken template threw `gsap is not defined` during
    # the changed run and not during the unchanged one, and a correct change was refused for
    # a fault that is on both sides. Anything genuinely NEW still fails.
    already = set((pb or {}).get("errors") or [])
    for n, sc in enumerate(spec.get("scenarios") or []):
        name = sc.get("name") or f"test {n + 1}"
        run = run_scenario(before, workdir, w, h, sc)
        if run:
            already.update(run.get("pageErrors") or [])
        out = outcome(sc, run, already)
        if out is None:
            problems.append(f"'{name}' could not run in a browser — UNVERIFIED")
        elif sc.get("keeps"):
            if out["passed"]:
                notes.append(f"'{name}' (keeps) passes on the page as it is")
            else:
                problems.append(f"'{name}' is meant to keep something that works, but already fails on the page as it "
                                f"is: {out['failures'][0]}")
        elif out["passed"]:
            cover = failing.get(sc.get("requirement"))
            if cover is not None:
                notes.append(f"'{name}' already passes on the page as it is, so it proves nothing and is left out — "
                             f"requirement {sc['requirement'] + 1} is proven by its {cover.get('kind')} claim, "
                             "which fails today")
                continue
            problems.append(f"'{name}' already passes on the page as it is, so it tests nothing the change adds")
        else:
            notes.append(f"'{name}' fails on the page as it is, as it should: {out['failures'][0]}")
        kept.append(sc)
    if isinstance(spec.get("scenarios"), list):
        spec["scenarios"] = kept
    if already:
        notes.append("the page already throws, before anything is changed, and these are not "
                     "blamed on the change: " + "; ".join(sorted(already)[:3]))
    return notes, problems, already


def critic_problems(reply, request, spec):
    """An independent reviewer's gaps, kept only when they quote the request and name real tests."""
    got = C._parse_plan(reply) if isinstance(reply, str) else reply
    if not isinstance(got, dict):
        return None
    words = {_stem(w) for w in content_words(request)}
    names = {str(sc.get("name")) for sc in spec.get("scenarios") or [] if isinstance(sc, dict)}
    out = []
    for g in got.get("gaps") or []:
        if not isinstance(g, dict):
            continue
        stems = {_stem(w) for w in content_words(str(g.get("words") or ""))}
        if stems and len(stems & words) >= 0.6 * len(stems):
            out.append(f"the reviewer says no test checks {g.get('words')!r}: {str(g.get('why') or '')[:200]}")
    for x in got.get("wrong") or []:
        if isinstance(x, dict) and str(x.get("test")) in names:
            out.append(f"the reviewer says '{x.get('test')}' tests the wrong thing: {str(x.get('why') or '')[:200]}")
    return out


def mutants(plan):
    """Every part of the change, taken away in turn."""
    out = []
    if plan.get("css"):
        out.append(("the CSS", dict(plan, css="")))
    if plan.get("js"):
        out.append(("the script", dict(plan, js="")))
    patches = plan.get("patches") or []
    for i, p in enumerate(patches):
        rest = [q for j, q in enumerate(patches) if j != i]
        if rest or plan.get("css") or plan.get("js"):
            find = str((p or {}).get("find", ""))[:48] if isinstance(p, dict) else ""
            out.append((f"patch {i + 1} ({find!r})", dict(plan, patches=rest)))
    return out


def needed(before, plan, spec, workdir, w, h, request="", pb=None, known_errors=()):
    """-> (notes, problems). A part of the change that no test — and no measured claim — needs is refused."""
    notes, problems = [], []
    tests = [sc for sc in spec.get("scenarios") or [] if not sc.get("keeps")]
    claims = [c for c in spec.get("claims") or [] if isinstance(c, dict)]
    for label, mplan in mutants(plan):
        page, errs = C.apply_plan(before, mplan)
        if errs:
            notes.append(f"without {label} the change does not apply at all")
            continue
        killer = None
        for sc in tests:
            out = outcome(sc, run_scenario(page, workdir, w, h, sc), known_errors)
            if out is None:
                problems.append(f"without {label}, the tests could not run — UNVERIFIED")
                killer = "?"
                break
            if not out["passed"]:
                killer = f"'{sc.get('name')}'"
                break
        if killer is None and claims:
            # A MEASURED CLAIM IS A TEST TOO: a recolour proven only by its colour claim would otherwise
            # have every part of it refused as "no test checks what it does".
            for c, ok, words in measure_claims(page, claims, pb, workdir, w, h, request, "mutant"):
                if not ok:
                    killer = f"the {c.get('kind')} claim"
                    break
        if killer is None:
            problems.append(f"without {label}, every test still passes — no test checks what it does, or nobody asked "
                            "for it; test it or remove it")
        elif killer != "?":
            notes.append(f"without {label}, {killer} fails — it is needed")
    return notes, problems


def references(spec):
    """Existing ids the tests act on, and ids whose own state they check."""
    acted, asserted = set(), set()
    for sc in spec.get("scenarios") or []:
        for s in sc.get("steps") or []:
            if not isinstance(s, dict) or not isinstance(s.get("el"), str):
                continue
            if s.get("do"):
                acted.add(s["el"])
            elif s.get("expect") in CHANGING:
                asserted.add(s["el"])
    return acted, asserted


# ───────────────────────────── the prompts ─────────────────────────────

SPEC_PROMPT = """You are Aethron's test writer. A person asked for a change to a web page Aethron rebuilt and measured.
Do NOT write code. Decide what "done" means for THIS request and write it as tests a browser runs:
they must FAIL on the page as it is now, and PASS once the change is made the way the person meant.

You are given the person's words and the page's elements (id, tag, measured box x/y/w/h in pixels, words,
colours). The element with id "bg" is the page itself; anything outside its box is cut off.

Return ONLY JSON, no markdown fences:
{"understanding": "<one or two sentences: what the person wants>",
 "requirements": [{"says": "<the person's own words this part covers>", "means": "<what must be true>"}],
 "new_ids": ["<data-ae-id of every element the change must create>"],
 "scenarios": [{"name": "<short>", "requirement": 0, "keeps": false, "steps": [<actions and checks, in order>]}],
 "claims": []}

Every part of the request needs a requirement quoting its words; every requirement needs a scenario.
A scenario is what one person does and sees, in order, starting from the page as it loads.

ACTIONS
  {"do": "hover", "el": "<id>"}                 {"do": "leave"}   (pointer to an empty spot)
  {"do": "click", "el": "<id>"}                 {"do": "key", "key": "Escape" | "Enter" | "Tab" | "a"}
  {"do": "type", "el": "<id>", "text": "..."}   {"do": "clear", "el": "<id>"}
  {"do": "focus", "el": "<id>"}                 {"do": "blur", "el": "<id>"}
  {"do": "scroll", "y": 400}                    {"do": "wait", "ms": 1500}   (15000 at most)
  {"do": "drag", "el": "<id>", "dx": 120, "dy": 0}    {"do": "select", "el": "<id>", "value": "..."}
CHECKS
  {"expect": "visible" | "hidden" | "exists" | "missing" | "on_top" | "clickable" | "inside_page" | "focused", "el": "<id>"}
  {"expect": "text", "el": "<id>", "equals" | "contains" | "not_contains" | "matches": "..."}
       (the words a person reads; for a text box, its value or else its placeholder)
  {"expect": "value", "el": "<id>", "equals" | "contains": "..."}
  {"expect": "count", "sel": "<css selector>", "equals" | "min" | "max": 3, "visible": true}
  {"expect": "style", "el": "<id>", "prop": "<any CSS property>", "equals": "#FFFFFF" | "14px" | "none", "min": .., "max": ..}
  {"expect": "attr", "el": "<id>", "name": "aria-expanded", "equals": "true"}
  {"expect": "box", "el": "<id>", "prop": "x" | "y" | "w" | "h" | "right" | "bottom", "equals" | "min" | "max": 120}
  {"expect": "relation", "el": "<id>", "to": "<id>", "is": "below" | "above" | "left_of" | "right_of" | "inside" | "overlaps" | "near", "gap_max": 24}
  {"expect": "readable", "el": "<id>", "min": 4.5}
"el" may instead be {"sel": "<css selector>", "nth": 0} for a part inside an element.

PROVEN CHECKS you may also use, in "claims", each with "requirement": <index>:
  {"kind": "appears_on", "id": "<new id>", "trigger": "<id>", "on": "hover" | "click" | "focus", "items": 3}
       (a pop-up used like a person: hidden first, opens next to its trigger on top and inside the page,
        reachable by the pointer, closes again, trigger keeps its behaviour)
  {"kind": "style_on", "id": "<id>", "on": "hover" | "click" | "focus", "property": "background" | "text" | "border" | "opacity", "equals": "#RRGGBB"}
  {"kind": "size", "id": "<id>", "dimension": "height" | "width", "change": "-10%"}
  {"kind": "changes_over_time", "id": "<id>"}   (TYPED TEXT only: followed letter by letter. A spinner
                                                 has no words — use "moves" for anything that turns)
  {"kind": "moves", "id": "<id>", "trigger": "<id>", "on": "click" | "hover", "for_ms": 2000,
   "property": "rotate" | "fade" | "slide"}   ("property" optional: the KIND of motion asked for)
       (something that MOVES: Aethron acts, then watches that element every 50ms. With "for_ms" it
        must move during that time and be still afterwards — a loader that never stops is refused.
        Leave "for_ms" out for motion that should keep going, and "trigger" out if it starts by itself.)
  {"kind": "colors", "region": "background", "families": ["orange", "black"]}
  {"kind": "removed", "id": "<id>"}          (that element is gone from the page — CLAIM THIS to delete
                                              anything; naming it in "touches" without a claim is refused)
  {"kind": "added", "id": "<new id>"}        (a new element, measured on the page and inside it)
  {"kind": "text", "id": "<id>", "equals": "<the exact new words>"}
  {"kind": "color", "id": "<id>", "property": "background" | "text", "equals": "#RRGGBB"}
  {"kind": "position", "id": "<id>", "axis": "x" | "y", "change": "-24px"}

GOOD TESTS
* Check the state before the action as well as after: hidden, then hover shows it, then leaving hides it.
* Check what a person would notice: where it is relative to what it belongs to, on top, inside the page,
  readable, and that it goes away again when it should.
* Use exact words only when the person gave them; otherwise check what they described.
* Anything that lasts a while ("for about 2 seconds"): check it right after the action, while it lasts, and
  that it is gone after it should end (wait past the time, then expect hidden or missing).
* A test that already passes on the page as it is will be refused.
* "keeps": true marks something that already works and must keep working (it must pass now).
"""

CRITIC_PROMPT = """You review tests for a change to a web page. You see ONLY what the person asked for and the tests someone
wrote from it — not the page's code, and you are not the one who wrote them.

The question: if every one of these tests passes, is it PROVEN that the person got what they asked for, the way
they meant it?

A PROVEN CHECK line is a test too: Aethron measures it off the rendered page. "colors" measures what share of the
region each named colour family covers; "changes_over_time" follows an element's words for 20 seconds; "size" and
"position" compare the measured box before and after; "appears_on" hovers or clicks like a person;
"moves" acts and then WATCHES the element every 50ms — it proves the thing really moves, that it is the
kind of motion asked for when "property" is given, and that it stops when a length was asked for. Without being
asked, Aethron also checks every change: nothing else on the page may change, a recoloured background must keep
its shape, its blending and where its light and dark sit, and a moving background must still move. Do not list
those as gaps.

List only real problems:
- "gaps": something the person clearly asked for that no test checks. Quote the person's own words.
- "wrong": a test that checks something different from what was asked, or the opposite of it.
Do not list style preferences, extras nobody asked for, or wording. Empty lists are a good answer when the tests cover it.

Return ONLY JSON: {"gaps": [{"words": "<the person's words>", "why": "<what no test checks>"}],
                   "wrong": [{"test": "<test name>", "why": "<what is wrong>"}]}
"""

BUILD_PROMPT = """You are writing the code for a change to a web page Aethron rebuilt from a screenshot.
The TESTS below were written first, from the person's request, and have been run: they FAIL on the page as it is.
Make every test pass by editing the page's code, and do nothing the request and the tests do not ask for.
You cannot change the tests.

Return ONLY JSON, no markdown fences:
{"patches": [{"find": "<text copied exactly from the source>", "replace": "<new text>", "count": 1}],
 "css": "<optional CSS to add>", "js": "<optional JavaScript to add>",
 "touches": ["<id of every element whose size, position, words, colours or styles you change, and every new id>"],
 "note": "<one line: what you did>"}

RULES
* DO IT ONE WAY. Every part you send is removed in turn and some test must then fail, or the whole change
  is refused as a part nobody asked for. So never do the same job twice: if a patch already sets the colour
  inline, do NOT also add a CSS rule for it; if a CSS rule does it, do not also patch the element. Send the
  smallest thing that makes the tests pass — one patch is usually the whole change.
* "find" is copied character for character; "count" is how many times it appears; patches apply in order.
* Create every id in new_ids with exactly that data-ae-id. Every new element needs its own data-ae-id.
* Scripts may not use fetch, XMLHttpRequest, WebSocket, import(), eval, new Function, cookies, storage,
  window.open, navigation, iframes, or any network URL. Scripts must not throw.
* Elements carry their colours and sizes in INLINE style attributes; a stylesheet rule that must change one of
  them needs !important.
* Anything that opens is a SIBLING of its trigger, never inside a button; while closed it is display:none or
  visibility:hidden so it never covers anything. The page clips everything outside the bg element's box.
* An element you only attach behaviour to keeps its look and its existing handlers (no stopPropagation or
  preventDefault on its click).

WAYS OF BUILDING THAT PASS A PERSON, not only a test
* A pop-up on HOVER: place it touching its trigger with no gap (its top at the trigger's bottom edge, or
  overlapping it by a pixel), show it on the trigger's :hover AND on its own :hover, and hide it with
  opacity:0;visibility:hidden plus transition:opacity .15s,visibility .15s, so the pointer can cross onto it.
* A pop-up on CLICK: one listener on the trigger toggles it; one listener on document closes it when a click
  lands outside both; a keydown listener closes it on Escape. Never stop the event.
* Something that lasts "about N seconds": show it on the action, and remove or hide it with setTimeout after
  N seconds — then show whatever comes next.
* A recolour of the background: use the COLOUR MAP when one is given; change colour values only.

WHAT AETHRON DOES WITH YOUR CODE
* Runs every test on your page — all must pass.
* Removes each part of your change in turn (the CSS, the script, each patch): some test must fail each time.
  A part no test needs is refused.
* Checks every element not in "touches" measures exactly as before, nothing clickable is covered, the page at
  rest is unchanged outside what you named, and no script errors.
"""


def _listing(pb, html):
    kinds = {e["id"]: e["kind"] for e in AE.manifest(html)["elements"]}
    rows = []
    for i, e in pb["els"].items():
        row = {"id": i, "tag": e.get("tag"), "kind": kinds.get(i, "ground" if i == "bg" else "?"),
               "x": e["x"], "y": e["y"], "w": e["w"], "h": e["h"], "color": e["color"], "background": e["bg"]}
        if i != "bg" and e.get("text"):
            row["text"] = e["text"][:80]
        rows.append(row)
    return "\n".join(json.dumps(r) for r in rows)


def _readable_spec(spec):
    lines = [f"UNDERSTANDING: {spec.get('understanding', '')}"]
    for n, r in enumerate(spec.get("requirements") or []):
        lines.append(f"REQUIREMENT {n}: says {r.get('says')!r} — means {r.get('means', '')!r}")
    for sc in spec.get("scenarios") or []:
        lines.append(f"TEST '{sc.get('name')}' (requirement {sc.get('requirement')}{', keeps' if sc.get('keeps') else ''}):")
        lines += [f"   {k + 1}. {say_step(s)}" for k, s in enumerate(sc.get("steps") or []) if isinstance(s, dict)]
    for c in spec.get("claims") or []:
        lines.append(f"PROVEN CHECK (requirement {c.get('requirement')}): {json.dumps(c)}")
    return "\n".join(lines)


# ───────────────────────────── the loop ─────────────────────────────

def build(html, request, workdir, call=None, budget_usd=0.05, max_calls=16, spec_tries=3, code_tries=4,
          minutes=20):
    ledger = {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "stopped": None}
    if call is None:
        import aethron_build as B

        def call(prompt):
            return B.gemini_text(prompt, ledger, budget_usd, max_tokens=12000)
    base = {"verdict": "REFUSED", "html": html, "spec": None, "plan": None, "proof": [], "problems": [],
            "attempts": [], "ledger": ledger}
    calls = {"n": 0}

    end = time.time() + minutes * 60

    def ask(prompt):
        if calls["n"] >= max_calls:
            raise RuntimeError(f"stopped after {max_calls} model calls")
        # TIME IS A BUDGET TOO. Measured 2026-09-15 on a shaky connection: one request spent
        # 39 minutes inside per-key timeouts and back-off before giving up. Nobody watching a
        # page wants that, and nothing about it was more likely to succeed at minute 38.
        if time.time() > end:
            raise RuntimeError(f"stopped after {minutes} minutes — the provider was too slow "
                               f"or unreachable; nothing was changed")
        calls["n"] += 1
        return call(prompt)
    canvas = AE.manifest(html)["canvas"]
    w, h = canvas.get("w"), canvas.get("h")
    if not w:
        return {**base, "problems": ["the page's canvas could not be read"]}
    pb = C.probe(html, workdir, w, h, 1200)
    if not pb or not pb.get("els"):
        return {**base, "verdict": "NOT MEASURABLE", "problems": ["Aethron could not measure this page"]}
    elements = _listing(pb, html)

    # 1-4: tests first, held to the words, failing today, reviewed blind
    spec_first = SPEC_PROMPT + "\n\nTHE PERSON ASKED FOR:\n" + request + "\n\nTHE ELEMENTS (measured):\n" + elements
    prompt, spec, red_notes, review, already = spec_first, None, [], "not reviewed", set()
    for attempt in range(spec_tries):
        try:
            reply = ask(prompt)
        except Exception as why:
            # A PROVIDER THAT DID NOT ANSWER IS NOT A REQUEST THAT WAS REFUSED. Measured
            # 2026-09-15: Gemini answered 503 to both free keys mid-run and the report came
            # back REFUSED, which reads as "Aethron judged your request and said no". The
            # page is untouched either way; the verdict has to say which of the two happened.
            return {**base, "verdict": "NOT ASKED",
                    "problems": (base["problems"] or []) + [f"could not ask the model: {why}"]}
        spec = C._parse_plan(reply)
        problems = validate_spec(spec, request, pb)
        if not problems:
            red_notes, problems, already = red(html, spec, workdir, w, h, request, pb)
        if not problems:
            try:
                verdict = critic_problems(ask(CRITIC_PROMPT + "\n\nTHE PERSON ASKED FOR:\n" + request
                                              + "\n\nTHE TESTS:\n" + _readable_spec(spec)), request, spec)
            except Exception as why:
                verdict = None
                review = f"not reviewed ({why})"
            if verdict is None:
                review = review if review != "not reviewed" else "not reviewed (the reviewer's reply was unreadable)"
            elif verdict:
                problems = verdict
            else:
                review = "an independent reviewer, shown only the request and the tests, found no gap"
        base["attempts"].append({"phase": "tests", "spec": spec, "problems": problems, "red": red_notes})
        if not problems:
            break
        base["problems"] = problems
        prompt = (spec_first + "\n\nYOUR LAST TESTS:\n" + (_readable_spec(spec) if isinstance(spec, dict) else
                                                           str(reply)[:1500])
                  + "\n\nTHEY WERE REFUSED BECAUSE:\n- " + "\n- ".join(problems)
                  + "\n\nWrite the tests again, fixing exactly these problems.\n")
    else:
        return {**base, "spec": spec, "problems": ["the tests could not be written well enough: "] + base["problems"]}
    base["spec"], base["problems"] = spec, []
    claims = [c for c in spec.get("claims") or [] if isinstance(c, dict)]
    new_ids = [i for i in spec.get("new_ids") or [] if isinstance(i, str)]
    acted, asserted = references(spec)
    existing = set(pb["els"])
    acted, asserted = acted & existing, asserted & existing

    # 5-8: code, then every test, every part needed, nothing else broken
    code_first = (BUILD_PROMPT + "\n\nTHE PERSON ASKED FOR:\n" + request + "\n\nTHE TESTS:\n" + _readable_spec(spec)
                  + "\n\nnew_ids: " + json.dumps(new_ids) + "\n\nON THE PAGE AS IT IS:\n- " + "\n- ".join(red_notes)
                  + "\n\nTHE ELEMENTS (measured):\n" + elements + C.colour_guidance(html, request)
                  + "\n\nTHE SOURCE:\n" + html)
    prompt = code_first
    for attempt in range(code_tries):
        try:
            reply = ask(prompt)
        except Exception as why:
            base["problems"] = base["problems"] or [f"could not ask the model: {why}"]
            base["verdict"] = "NOT ASKED"      # the provider, not the request
            break
        plan = C._parse_plan(reply)
        problems, proof, after = [], [], html
        if plan is None:
            problems = ["the reply was not the JSON asked for"]
        else:
            unsafe = C._unsafe(plan)
            if unsafe:
                problems.append("the code uses what pages may not: " + ", ".join(unsafe))
            problems += C.unmeasured_touches(html, plan, claims, extra=set(new_ids) | asserted | acted, pb=pb)
            after, errs = C.apply_plan(html, plan)
            problems += errs
            agree = None if errs else C.copies_agree(html, after)
            if agree:
                problems.append(agree)
        if not problems:
            # PROMISED MEANS ON THE PAGE, ASKED OF THE BROWSER. A search of the source found
            # data-ae-id="c01" inside the script's own querySelector('[data-ae-id="c01"]') and
            # called a counter that was never created "created" — and would have refused a real
            # element built by a script. The rendered page is the only honest answer.
            pa = C.probe(after, workdir, w, h, 1200)
            if not pa or not pa.get("els"):
                problems.append("the changed page could not be measured — it may not load")
            else:
                # SOME THINGS ARE BORN WHEN SOMEONE ACTS. A spinner built by the click that starts
                # it does not exist on the page at rest, and demanding it there refuses a perfectly
                # ordinary design. Ids a claim reaches by ACTING (a pop-up on hover, a loader on
                # click) are proven by that claim's own measurement, which acts first.
                on_action = {c.get("id") for c in claims
                             if c.get("kind") in ("appears_on", "moves") and c.get("trigger")}
                problems += [f"{i} was promised in new_ids and is not on the changed page" for i in new_ids
                             if i not in pa["els"] and i not in on_action]
        if not problems:
            for sc in spec.get("scenarios") or []:
                out = outcome(sc, run_scenario(after, workdir, w, h, sc), already)
                name = sc.get("name")
                if out is None:
                    problems.append(f"'{name}' could not run on the changed page — UNVERIFIED")
                elif out["passed"]:
                    proof.append(f"ok   '{name}' passes" + (" (it kept working)" if sc.get("keeps") else ""))
                else:
                    problems.append(f"'{name}' fails on the changed page: " + "; ".join(out["failures"][:3]))
        if not problems:
            notes, problems = needed(html, plan, spec, workdir, w, h, request, pb, already)
            proof += ["ok   " + n for n in notes]
        if not problems:
            measured, guard = C.verify(html, after, dict(plan, expect=claims), pb, workdir, w, h, request,
                                       new_ids=new_ids, acted_on=acted, asserted=asserted)
            proof += measured
            problems += guard
        if not problems:
            for host in sorted(acted - asserted - set(new_ids) - {c.get("id") for c in claims}):
                for on in ("hover", "click"):
                    spec_i = {"trigger": host, "pop": None, "on": on}
                    kept = C.kept_behaviour(host, on, C.interact(after, workdir, w, h, spec_i),
                                            C.interact(html, workdir, w, h, spec_i))
                    problems += kept
            if not problems and acted - asserted:
                proof.append("ok   what the tests only act on still behaves and looks as it did")
        if not problems:
            # THE AUTOMATIC GENERAL MEASURE: record everything, diff everything, and every difference
            # must be explained by the request — including kinds nobody wrote a check for.
            import aethron_agm as AGM
            seen = AGM.observe(html, after, workdir, w, h)
            if seen is None:
                problems.append("AGM: the pages could not be recorded — UNVERIFIED")
            else:
                related = set(new_ids) | asserted | {c.get("id") for c in claims if isinstance(c.get("id"), str)}
                related |= {"bg" if c.get("region", "background") == "background" else c.get("region")
                            for c in claims if c.get("kind") == "colors"}
                notes, agm_problems = AGM.explain(seen, request, related=related, call=ask, acted=acted, claims=claims)
                proof.append(f"ok   AGM recorded {len(seen)} difference(s) between the page before and after; "
                             f"{len(notes)} explained by the request")
                # Show WHAT was explained and why, not only how many: a count cannot be checked by
                # the person reading it, and a report that hides its own reasons is the old blindness.
                proof += ["ok     " + n for n in notes]
                problems += agm_problems
        base["attempts"].append({"phase": "code", "plan": plan, "problems": problems, "proof": proof})
        if plan is not None and not problems:
            story = [f"understood: {spec.get('understanding', '')}"]
            story += [f"before the change: {n}" for n in red_notes]
            story.append(review)
            return {**base, "verdict": "APPLIED", "html": after, "plan": plan, "proof": story + proof,
                    "problems": []}
        base["problems"] = problems
        prompt = (code_first + "\n\nYOUR LAST CODE WAS UNDONE:\n" + (json.dumps(plan)[:4000] if plan else str(reply)[:1500])
                  + "\n\nAETHRON MEASURED:\n- " + "\n- ".join(problems)
                  + "\n\nWrite new code against THE SOURCE above that makes every test pass and fixes exactly these.\n")
    base["html"] = html
    return base


def report(result):
    lines = [f"VERDICT: {result['verdict']}"]
    spec = result.get("spec") or {}
    if spec.get("understanding"):
        lines.append(f"  understood: {spec['understanding']}")
    for p in result.get("proof", []):
        if not p.startswith("understood"):
            lines.append("  " + p)
    for p in result.get("problems", []):
        lines.append("  NOT DONE: " + p)
    led = result.get("ledger", {})
    lines.append(f"  model calls {led.get('calls', 0)} · attempts {len(result.get('attempts', []))} · "
                 f"${led.get('usd', 0):.4f} charged" + (f" · {led.get('free_calls')} on free keys"
                                                        if led.get("free_calls") else ""))
    return "\n".join(lines)


# ───────────────────────────── selftest ─────────────────────────────

def _selftest():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}   {detail}")

    pb = {"els": {"bg": {}, "t01": {}, "s01": {}}}
    ask = "Show how many characters I have typed under the chat box, like 5 / 200, updating as I type"
    good = {"understanding": "a counter", "new_ids": ["c01"],
            "requirements": [{"says": "show how many characters I have typed under the chat box, like 5 / 200",
                              "means": "a counter below the box"},
                             {"says": "updating as I type", "means": "it changes with each character"}],
            "scenarios": [{"name": "counter sits under the box", "requirement": 0,
                           "steps": [{"expect": "visible", "el": "c01"},
                                     {"expect": "relation", "el": "c01", "to": "t01", "is": "below", "gap_max": 24}]},
                          {"name": "counter follows typing", "requirement": 1,
                           "steps": [{"do": "type", "el": "t01", "text": "Hello"},
                                     {"expect": "text", "el": "c01", "equals": "5 / 200"}]}]}
    print("── tests are held to the request before they are run")
    check("tests that cover every part of the request pass validation", not validate_spec(good, ask, pb),
          str(validate_spec(good, ask, pb)))
    half = dict(good, requirements=good["requirements"][:1], scenarios=good["scenarios"][:1])
    got = validate_spec(half, ask, pb)
    check("leaving 'updating as I type' out is refused, naming the words", any("updat" in g or "type" in g
                                                                              for g in got), str(got))
    stray = json.loads(json.dumps(good))
    stray["scenarios"][0]["steps"][0]["el"] = "zz9"
    check("a test on an element that is neither there nor promised is refused",
          any("zz9" in g for g in validate_spec(stray, ask, pb)))
    invented = json.loads(json.dumps(good))
    invented["requirements"].append({"says": "make the logo spin forever", "means": "x"})
    invented["scenarios"].append({"name": "spin", "requirement": 2, "steps": [{"expect": "exists", "el": "t01"}]})
    check("a requirement the person never said is refused",
          any("not in the request" in g for g in validate_spec(invented, ask, pb)))
    orphan = json.loads(json.dumps(good))
    orphan["scenarios"] = orphan["scenarios"][:1]
    check("a requirement with no test is refused", any("has no test" in g for g in validate_spec(orphan, ask, pb)))
    quoted = 'Put the words "Built with Aethron" under the chat box'
    qspec = {"requirements": [{"says": "put the words under the chat box built with aethron", "means": "x"}],
             "new_ids": ["c01"],
             "scenarios": [{"name": "words", "requirement": 0, "steps": [{"expect": "visible", "el": "c01"}]}]}
    check("the person's exact quoted words must be checked exactly",
          any("Built with Aethron" in g for g in validate_spec(qspec, quoted, pb)))
    blind = json.loads(json.dumps(good))
    blind["scenarios"][1]["steps"] = [{"do": "type", "el": "t01", "text": "Hello"}]
    check("a test that checks nothing is refused", any("checks nothing" in g for g in validate_spec(blind, ask, pb)))

    print("\n── the reviewer is heard only when it quotes the request")
    real = critic_problems(json.dumps({"gaps": [{"words": "updating as I type", "why": "never types"}]}), ask, good)
    check("a gap quoting the request is kept", real and "updating as I type" in real[0], str(real))
    made_up = critic_problems(json.dumps({"gaps": [{"words": "a dark mode toggle", "why": "x"}]}), ask, good)
    check("a gap about something never asked is dropped", made_up == [], str(made_up))
    check("an unreadable review is reported as no review, not as approval", critic_problems("nope", ask, good) is None)

    print("\n── every part of a change can be taken away and tested")
    plan = {"patches": [{"find": "a", "replace": "b"}, {"find": "c", "replace": "d"}], "css": ".x{}", "js": "1"}
    labels = [m[0] for m in mutants(plan)]
    check("the CSS, the script and each patch are each removed in turn",
          labels[:2] == ["the CSS", "the script"] and len(labels) == 4, str(labels))
    acted, asserted = references(good)
    check("the tests' references split into acted-on and checked", acted == {"t01"} and asserted == {"c01"},
          f"{acted} {asserted}")
    print(f"\nspec selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if len(argv) < 2:
        print('usage: aethron_spec.py <page.html> "<what to change>" [--out out.html] '
              '[--budget 0.05] [--minutes 20]')
        return 2
    page, words = Path(argv[0]).resolve(), argv[1]
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else page
    budget = float(argv[argv.index("--budget") + 1]) if "--budget" in argv else 0.05
    minutes = float(argv[argv.index("--minutes") + 1]) if "--minutes" in argv else 20
    work = Path(tempfile.mkdtemp(prefix="ae-spec-"))

    # ANY PAGE, NOT ONLY THE ONES AETHRON BUILT. The whole measure reads [data-ae-id]; a page
    # that carries none is not "unchangeable", it is unread. Stamp it first — and only if the
    # stamping is PROVEN to change no pixel and to land on the elements it was picked for.
    import aethron_adopt as AD
    text = page.read_text()
    if AD.needs_adopting(text):
        print(f"this page carries no measurable elements — adopting it first")
        rep = AD.adopt(page, write=True)
        AD.report(rep)
        if not rep.get("verdict", "").startswith(("ADOPTED", "ALREADY")):
            print("VERDICT: NOT ASKED — the page could not be made measurable, so nothing "
                  "was asked of a model and the page is untouched")
            return 1
        text = page.read_text()

    # Relative assets resolve against the page's real home, and the folder is SERVED so
    # root-absolute paths (/assets/…) mean what they mean to a visitor. Without this a real
    # migration renders unstyled and every measurement is of a page nobody will ever see.
    C.set_asset_base(page.parent)
    C.serve_assets(page.parent)
    result = build(text, words, work, budget_usd=budget, minutes=minutes)
    print(report(result))
    if result["verdict"] == "APPLIED":
        out.write_text(result["html"])
        print(f"  written: {out}")
    out.with_suffix(".spec.json").write_text(json.dumps(
        {k: v for k, v in result.items() if k != "html"}, indent=1, default=str))
    return 0 if result["verdict"] == "APPLIED" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

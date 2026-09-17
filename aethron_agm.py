#!/usr/bin/env python3
"""AGM — the Automatic General Measure. Record everything, diff everything, explain every difference.

The owner, 2026-09-15: Aethron "only measures for something you specifically added to measure".
Every check it had — size, colour share, typing, pop-ups, lightness, gradient structure — existed
because someone thought of it first, and the next blind spot would stay blind until someone did
again. The orange sky passed a colour check while its light collapsed; nothing asked about light.

The AGM does not choose what to look at. Before and after a change it records, from the browser:

  THE DOM      every element's box, visibility, own words, value, attributes, ::before/::after,
               and EVERY computed style property — not a hand-picked few.
  THE PIXELS   the screen in cells: lightness, hue, saturation, detail (edges).
  THE MOTION   every running animation, and how each cell of the screen moves over time.
  BEHAVIOUR    a crawler's sweep: every interactive element hovered, clicked, typed into, and
               what that did to everything else on the page.

Then it diffs the two records into observations, each filed under a general dimension —
existence, visibility, position, size, text, typography, colour, lightness, shape, effects,
detail, motion, behaviour, layout — and every observation must be EXPLAINED:

  * it belongs to what the tests require (a new element, or an element the tests check), in a
    dimension the request's words speak about; or
  * a reviewer, shown only the request and the observation, quotes the words that ask for it.

An observation in a dimension the request never mentions is refused outright: a recolour that
also darkens the sky is a lightness change nobody asked for, whatever else it got right. No
request-specific check is involved, so a request nobody has seen before is measured the same way.
What cannot be seen is what never reaches the browser — a server call, a stored value.
"""
import hashlib
import json
import math
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_change as C     # noqa: E402

SNAPSHOT_TAIL = """<script>(function(){
function key(el){var parts=[],n=el;while(n&&n.nodeType===1&&n!==document.documentElement&&n!==document.body){
if(n.hasAttribute('data-ae-id')){parts.unshift('#'+n.getAttribute('data-ae-id'));return parts.join('>');}
var i=1,s=n;while((s=s.previousElementSibling)){if(s.tagName===n.tagName)i++;}parts.unshift(n.tagName.toLowerCase()+':'+i);n=n.parentElement;}
return parts.join('>');}
function freeze(){try{document.getAnimations().forEach(function(a){try{var t=a.effect&&a.effect.getComputedTiming();
if(t&&!isFinite(t.endTime)){a.pause();a.currentTime=0;}else{a.finish();}}catch(e){}});}catch(e){}}
function record(){var els=[],all=document.body.querySelectorAll('*');
for(var i=0;i<all.length;i++){var el=all[i];if(/^(SCRIPT|STYLE|LINK|META|NOSCRIPT|TEMPLATE)$/.test(el.tagName))continue;
var r=el.getBoundingClientRect(),cs=getComputedStyle(el),st={};
for(var j=0;j<cs.length;j++){var p=cs[j];if(p.charAt(0)==='-')continue;st[p]=cs.getPropertyValue(p);}
var op=1,shown=true;for(var n=el;n&&n.nodeType===1;n=n.parentElement){var c=getComputedStyle(n);if(c.display==='none')shown=false;op*=parseFloat(c.opacity||'1');}
if(cs.visibility!=='visible')shown=false;
var at={};for(var k=0;k<el.attributes.length;k++){var a=el.attributes[k];if(a.name!=='style')at[a.name]=a.value;}
var own='';for(var t=el.firstChild;t;t=t.nextSibling){if(t.nodeType===3)own+=t.nodeValue;}
var ps={};['::before','::after'].forEach(function(q){var pc=getComputedStyle(el,q);if(pc.content&&pc.content!=='none'&&pc.content!=='normal')
ps[q]={content:pc.content,color:pc.color,background:pc.backgroundColor,transform:pc.transform,opacity:pc.opacity,display:pc.display,width:pc.width,height:pc.height};});
els.push({k:key(el),tag:el.tagName.toLowerCase(),ae:el.getAttribute('data-ae-id'),x:Math.round(r.left*10)/10,y:Math.round(r.top*10)/10,
w:Math.round(r.width*10)/10,h:Math.round(r.height*10)/10,shown:shown,op:Math.round(op*1000)/1000,
own:own.replace(/\\s+/g,' ').trim().slice(0,200),value:(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.tagName==='SELECT')?String(el.value).slice(0,200):null,
at:at,st:st,ps:ps});}
return els;}
function run(){var out={els:[],anims:[],errors:window.__aeErr||[]};freeze();var first=record();
try{document.getAnimations().forEach(function(a){var ef=a.effect,tg=ef&&ef.target,props='';
try{props=ef.getKeyframes().map(function(f){return Object.keys(f).filter(function(x){return ['offset','easing','composite','computedOffset'].indexOf(x)<0;}).sort().join(',');}).join('|');}catch(e){}
var tm=ef?ef.getTiming():{};out.anims.push({t:tg?key(tg):null,name:a.animationName||a.transitionProperty||a.id||'',dur:String(tm.duration),it:String(tm.iterations),props:props});});}catch(e){}
// WHAT CHANGES ON ITS OWN IS RECORDED AS SUCH. Measured: the moving sky was read at slightly different
// moments before and after, so an untouched gradient looked changed. Endless animations are held at
// their first frame, and the page is read twice, 1.5s apart: whatever still differs moves by itself
// (a timer, a typing loop) and is compared as motion, never as a change of value.
function diff(a,b,into){var byK={};b.forEach(function(e){byK[e.k]=e;});
a.forEach(function(e){var s=byK[e.k];if(!s){into[e.k+'|exists|']=1;return;}
['x','y','w','h','shown','op','own','value'].forEach(function(f){if(String(e[f])!==String(s[f]))into[e.k+'|'+f+'|']=1;});
Object.keys(e.at).forEach(function(n){if(e.at[n]!==s.at[n])into[e.k+'|at|'+n]=1;});
Object.keys(e.st).forEach(function(p){if(e.st[p]!==s.st[p])into[e.k+'|st|'+p]=1;});});}
// THREE READINGS, NOT TWO, AND EVERY PAIR. Which properties happen to differ across one 1.5s
// gap depends on where in its cycle each reading fell — so on a restless page an element
// drifts on one side by CHANCE and the same marquee is then reported as motion that NEWLY
// appeared, on a change that never touched it. Measured elsewhere in this project: unioning
// the differences over three moments took a page's apparent restlessness from 273 elements
// to 23. The more ways it can be caught moving, the less its stillness is luck.
setTimeout(function(){freeze();var second=record();
setTimeout(function(){freeze();var third=record(),seen={};
diff(first,second,seen);diff(second,third,seen);diff(first,third,seen);
out.els=first;out.drift=Object.keys(seen);
var d=document.createElement('script');d.type='application/json';d.id='__ae_snapshot';d.textContent=JSON.stringify(out);document.body.appendChild(d);},1500);},1500);}
// Fonts first: a label read before its web font arrives is 2-4px narrower, which is a difference in the
// loading, not in the page.
setTimeout(function(){var f=document.fonts&&document.fonts.ready?document.fonts.ready:Promise.resolve();
Promise.race([f,new Promise(function(r){setTimeout(r,3000);})]).then(function(){setTimeout(run,100);});},1200);})();</script>"""

# THE CRAWLER'S SWEEP (after Crawljax: find what can change state, fire it, record the state it
# leads to). Every interactive element is hovered, clicked or typed into, one at a time, and what
# that did to EVERY element on the page is recorded relative to the moment before. Navigation and
# form submission are held back, identically before and after, so the page survives the sweep.
PERSON_JS = C.INTERACT_TAIL[len("<script>(function(){"):C.INTERACT_TAIL.index("async function run(){try{")]
SWEEP_RUN = """
var LIGHT=['color','background-color','background-image','transform','filter','opacity','border-top-color','box-shadow',
'text-decoration-line','outline-style','cursor'];
function keyOf(el){var parts=[],n=el;while(n&&n.nodeType===1&&n!==document.documentElement&&n!==document.body){
if(n.hasAttribute('data-ae-id')){parts.unshift('#'+n.getAttribute('data-ae-id'));return parts.join('>');}
var i=1,s=n;while((s=s.previousElementSibling)){if(s.tagName===n.tagName)i++;}parts.unshift(n.tagName.toLowerCase()+':'+i);n=n.parentElement;}
return parts.join('>');}
function fp(){var o={},all=document.body.querySelectorAll('*');for(var i=0;i<all.length;i++){var el=all[i];
if(/^(SCRIPT|STYLE|LINK|META|NOSCRIPT|TEMPLATE)$/.test(el.tagName))continue;var r=el.getBoundingClientRect(),cs=getComputedStyle(el),
op=1,shown=cs.visibility==='visible';for(var n=el;n&&n.nodeType===1;n=n.parentElement){var c=getComputedStyle(n);if(c.display==='none')shown=false;op*=parseFloat(c.opacity||'1');}
var at=[];for(var k=0;k<el.attributes.length;k++){var a=el.attributes[k];if(a.name==='style')continue;
at.push(a.name+'='+(a.name==='class'?a.value.split(/\\s+/).filter(function(x){return x&&x!==HOVER;}).sort().join(' '):a.value));}
var own='';for(var t=el.firstChild;t;t=t.nextSibling){if(t.nodeType===3)own+=t.nodeValue;}
o[keyOf(el)]={shown:(shown&&op>0.05)?1:0,box:[Math.round(r.left/2)*2,Math.round(r.top/2)*2,Math.round(r.width/2)*2,Math.round(r.height/2)*2].join(','),
text:own.replace(/\\s+/g,' ').trim().slice(0,80),value:('value' in el&&/INPUT|TEXTAREA|SELECT/.test(el.tagName))?String(el.value).slice(0,80):'',
attrs:at.sort().join(' ; ').slice(0,300),style:LIGHT.map(function(p){return p+':'+String(cs.getPropertyValue(p)).slice(0,60);}).join(' ; ')};}
return o;}
function delta(a,b){var out=[],ks={},k;for(k in a)ks[k]=1;for(k in b)ks[k]=1;
for(k in ks){if(!a[k]){out.push([k,'appears','','']);continue;}if(!b[k]){out.push([k,'goes','','']);continue;}
['shown','box','text','value','attrs','style'].forEach(function(f){if(String(a[k][f])!==String(b[k][f]))out.push([k,f,String(a[k][f]).slice(0,120),String(b[k][f]).slice(0,120)]);});}
return out;}
function textField(el){return el.tagName==='TEXTAREA'||(el.tagName==='INPUT'&&!/^(button|submit|checkbox|radio|reset|image)$/i.test(el.type||''));}
async function run(){try{
for(var i=0;i<document.styleSheets.length;i++){try{emulate(document.styleSheets[i].cssRules,document.styleSheets[i]);}catch(x){}}
document.addEventListener('click',function(e){var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;
if(a&&!/^(#|javascript:)/i.test(a.getAttribute('href')||''))e.preventDefault();},true);
document.addEventListener('submit',function(e){e.preventDefault();},true);
point(far([]));await wait(300);settle();
try{await Promise.race([document.fonts.ready,wait(3000)]);}catch(x){}
// WHAT CHANGES WITH NOBODY TOUCHING IT IS NOT BEHAVIOUR. Measured on the owner's page: the moving sky
// changed between every "before" and "after" reading, so every hover and click seemed to repaint it.
// Endless animations are paused before each reading, and anything that still drifts on its own over
// a quiet 1.5 seconds (a typing loop, a timer) is left out of every reaction.
var still=function(){try{document.getAnimations().forEach(function(a){try{var t=a.effect&&a.effect.getComputedTiming();
if(t&&!isFinite(t.endTime))a.pause();}catch(x){}});}catch(x){}};
still();var quiet0=fp();await wait(1500);settle();still();var drift={};delta(quiet0,fp()).forEach(function(c){drift[c[0]+'|'+c[1]]=1;});
out.drift=Object.keys(drift);
var real=function(list){return list.filter(function(c){return !drift[c[0]+'|'+c[1]];});};
var pool=document.querySelectorAll('a,button,input,textarea,select,summary,[role=button],[role=menuitem],[role=tab],[tabindex],[onclick],[data-ae-id]'),cands=[];
for(var j=0;j<pool.length&&cands.length<24;j++){var el=pool[j],v=vis(el);
if(el.getAttribute('data-ae-id')==='bg'||!opn(v)||!v.onTop)continue;
if(/^(A|BUTTON|INPUT|TEXTAREA|SELECT|SUMMARY)$/.test(el.tagName)||el.getAttribute('role')||el.hasAttribute('tabindex')||el.hasAttribute('onclick')||getComputedStyle(el).cursor==='pointer')cands.push(el);}
out.acts={};
for(var c=0;c<cands.length;c++){var el2=cands[c],k=keyOf(el2),rec={},rest;
still();rest=fp();point(el2);await wait(350);settle();still();rec.hover=real(delta(rest,fp())).slice(0,25);
point(far([]));await wait(450);settle();
still();rest=fp();
if(textField(el2)){press(el2);var old=el2.value;el2.value=(old||'')+'Ae';el2.dispatchEvent(new Event('input',{bubbles:true}));
await wait(450);settle();still();rec.type=real(delta(rest,fp())).slice(0,25);el2.value=old;el2.dispatchEvent(new Event('input',{bubbles:true}));}
else{press(el2);await wait(450);settle();still();rec.click=real(delta(rest,fp())).slice(0,25);}
key('Escape');press(far([]));await wait(450);settle();try{window.scrollTo(0,0);}catch(x){}
out.acts[k]=rec;}
}catch(x){out.errors.push(String((x&&x.message)||x));}
finally{var d=document.createElement('script');d.type='application/json';d.id='__ae_sweep';d.textContent=JSON.stringify(out);document.body.appendChild(d);}}
setTimeout(run,500);"""
SWEEP_TAIL = "<script>(function(){" + PERSON_JS.replace("__SPEC__", "{}") + SWEEP_RUN + "})();</script>"

FREEZE = ("<style data-ae-freeze>*,*::before,*::after{animation-play-state:paused!important;"
          "animation-delay:-__T__s!important}</style>")

# The style dimension each computed property belongs to. A property not listed is "other" and is
# never waved through by vocabulary: it needs a reviewer's quote or it is refused.
STYLE_DIMENSIONS = [
    ("colour", r"^(color|background-color|background-image|border(-(top|right|bottom|left|block|inline)(-(start|end))?)?-color|"
               r"outline-color|fill|stroke|caret-color|text-decoration-color|column-rule-color|accent-color|"
               r"text-emphasis-color|flood-color|lighting-color|stop-color|(column|row)-rule-color)$"),
    ("typography", r"^(font|letter-spacing|line-height|text-|word-spacing|white-space|hyphens|tab-size|writing-mode|direction|"
                   r"unicode-bidi|quotes|vertical-align)"),
    ("size", r"^(width|height|min-|max-|inline-size|block-size|padding|border(-(top|right|bottom|left|block|inline)(-(start|end))?)?-width|"
             r"box-sizing|aspect-ratio)"),
    ("position", r"^(left|top|right|bottom|inset|margin|position|float|clear|z-index)"),
    ("shape", r"^(border-.*radius|clip-path|clip$|mask|shape-)"),
    ("effects", r"^(box-shadow|text-shadow|filter|backdrop-filter|mix-blend-mode|isolation|background-blend-mode)$"),
    ("visibility", r"^(opacity|visibility|display|content-visibility)$"),
    ("motion", r"^(transform|translate|rotate|scale|perspective|animation|transition|offset|will-change)"),
    ("behaviour", r"^(cursor|pointer-events|user-select|touch-action|resize|scroll-)"),
    ("layout", r"^(flex|grid|align|justify|place-|gap|row-gap|column-gap|order|overflow|object-|columns|column-(count|width|span|fill)|"
               r"table-layout|border-(collapse|spacing)|list-style|counter)"),
    ("border", r"^(border|outline)"),
    ("background", r"^background"),
]
# What words let a dimension change without a reviewer, when the change sits on something the
# tests are about. General vocabulary — nothing here names a particular request.
DIMENSION_WORDS = {
    "existence": r"\b(add\w*|new|create\w*|insert\w*|put|place|remove\w*|delete\w*|get rid|pop-?\s?ups?|pops?\s+up|drop-?\s?downs?|tooltips?|menus?|messages?|counters?|loaders?|spinners?|badges?|icons?|boxe?s?|panels?|banners?|labels?|buttons?|links?|options?|show\w*)\b",
    "visibility": r"\b(show\w*|hid(e|es|den|ing)|appear\w*|disappear\w*|visible|invisible|reveal\w*|fade\w*|pop\w*|open\w*|clos\w*|remove\w*|toggle\w*)\b",
    "text": r"\b(text|words?|say\w*|label\w*|renam\w*|placeholder|title|heading|headline|copy|message|content|typ\w*|counter|count\w*|number|characters?)\b|\"",
    "typography": r"\b(font|bold|italic|typeface|letter\w*|weight|underline\w*|uppercase|lowercase|capital\w*|text size|larger text|smaller text)\b",
    "colour": r"\b(colou?r\w*|hue|tint|shade|recolou?r\w*|paint\w*|theme)\b|" + C.COLOUR_WORDS,
    "lightness": C.TONE_WORDS,
    "contrast": C.TONE_WORDS[:-3] + r"|contrast|readab\w*|legib\w*|stand\w* out)\b",
    "detail": C.RESHAPE_WORDS,
    "shape": r"\b(round\w*|corners?|radius|circle|circular|square|pill|shape\w*|curved?)\b",
    "size": r"\b(size|height|width|tall\w*|short\w*|wide\w*|narrow\w*|big\w*|small\w*|large\w*|shrink|grow|enlarge|resize|thin\w*|thick\w*)\b",
    "position": r"\b(move\w*|position\w*|align\w*|cent(er|re)\w*|left|right|top|bottom|under|below|above|beside|next to|near|inside|up|down)\b",
    "effects": r"\b(shadow\w*|blur\w*|glow\w*|glass\w*|filter\w*|depth|elevat\w*|frosted)\b",
    "motion": r"\b(anim\w*|mov\w*|motion|spin\w*|rotat\w*|slid\w*|bounc\w*|fad\w*|transition\w*|typ\w*|alive|puls\w*|shak\w*|loader|spinner|marquee|scroll\w*|automatic\w*|loop\w*|cycl\w*|repeat\w*|again)\b",
    "behaviour": r"\b(hover\w*|click\w*|tap\w*|press\w*|focus\w*|typ\w*|drag\w*|toggl\w*|open\w*|clos\w*|when i|pop-?\s?ups?|drop-?\s?downs?|menus?|select\w*|scroll\w*)\b",
    "layout": r"\b(layout|columns?|rows?|grid|stack\w*|wrap\w*|order|gap|spacing|spread)\b",
    "border": r"\b(border\w*|outline\w*|line|stroke)\b",
    "background": r"\b(background|backdrop|sky)\b|" + C.COLOUR_WORDS,
}


def _style_dimension(prop):
    for name, pattern in STYLE_DIMENSIONS:
        if re.match(pattern, prop):
            return name
    return "other"


TEXTUAL_ATTRS = ("placeholder", "value", "title", "alt", "aria-label", "aria-valuenow", "aria-valuetext")
ORIGINS = ("transform-origin", "perspective-origin")
FAR_EDGES = ("right", "bottom", "inset-inline-end", "inset-block-end")
GEOMETRY = ("existence", "visibility", "position", "size")


def _drift_dimension(field, name=""):
    """The dimension of something that changes by itself, from the snapshot's drift record."""
    if field in ("x", "y"):
        return "position"
    if field in ("w", "h"):
        return "size"
    if field in ("shown", "op"):
        return "visibility"
    if field in ("own", "value"):
        return "text"
    if field == "exists":
        return "existence"
    if field == "at":
        if name in TEXTUAL_ATTRS:
            return "text"
        return "behaviour" if name.startswith("aria-") or name in ("role", "tabindex", "href", "type") else "attribute"
    if field == "st":
        return _style_dimension(name)
    return "other"


def _same_numbers(before, after, slack=0.05):
    """Two computed values that differ only in sub-pixel rounding — '668.875px' against
    '668.891px' — are the same value measured twice, not a difference."""
    b, a = str(before or ""), str(after or "")
    nb, na = re.findall(r"-?\d+\.?\d*", b), re.findall(r"-?\d+\.?\d*", a)
    if not nb or len(nb) != len(na):
        return False
    if re.sub(r"-?\d+\.?\d*", "#", b) != re.sub(r"-?\d+\.?\d*", "#", a):
        return False       # the words around the numbers differ: a real change
    return all(abs(float(x) - float(y)) <= slack for x, y in zip(nb, na))


def _default_origin(value, e):
    """Is this transform/perspective origin just the centre of the element's box (the default)?"""
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(value or ""))
    return len(nums) >= 2 and abs(float(nums[0]) - e["w"] / 2) <= 0.6 and abs(float(nums[1]) - e["h"] / 2) <= 0.6


def _reach(e):
    """How far past its box an element's shadow falls, read from its computed box-shadow."""
    text = str((e.get("st") or {}).get("box-shadow") or "none")
    if text == "none":
        return 0.0
    depth, part, parts = 0, "", []
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    parts.append(part)
    best = 0.0
    for p in parts:
        if "inset" in p:
            continue
        nums = [abs(float(n)) for n in re.findall(r"(-?\d+(?:\.\d+)?)px", re.sub(r"rgba?\([^)]*\)", "", p))]
        if nums:
            best = max(best, sum((nums + [0, 0, 0, 0])[:4]))
    return min(best, 160.0)


# ───────────────────────────── recording ─────────────────────────────

_SNAPS = {}


def snapshot(html, workdir, w, h):
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h)
    if key in _SNAPS:
        return json.loads(_SNAPS[key])
    page = html.replace("<head>", "<head>" + C.PROBE_HEAD, 1) if "<head>" in html else C.PROBE_HEAD + html
    page = page.replace("</body>", SNAPSHOT_TAIL + "</body>", 1) if "</body>" in page else page + SNAPSHOT_TAIL
    # ONE BROWSER THAT DID NOT ANSWER IS NOT A PAGE THAT CANNOT BE RECORDED. Measured 2026-09-15: the
    # honest counter came back "AGM: the pages could not be recorded — UNVERIFIED" once, under load,
    # and recorded in 2s on every run alone. probe() already retries once for the same reason.
    got = None
    for _ in range(2):
        got = C._dump(page, workdir, w, h, 11000, "snapshot", "__ae_snapshot")   # three readings 1.5s apart need the clock to outlast them
        if got is not None:
            break
    if got is not None:
        _SNAPS[key] = json.dumps(got)
    return got


_SWEEPS = {}


def sweep(html, workdir, w, h):
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h)
    if key in _SWEEPS:
        return json.loads(_SWEEPS[key])
    page = html.replace("</body>", SWEEP_TAIL + "</body>", 1) if "</body>" in html else html + SWEEP_TAIL
    got = None
    for _ in range(2):
        got = C._dump(page, workdir, w, h, 60000, "sweep", "__ae_sweep")
        if got is not None:
            break
    if got is not None:
        _SWEEPS[key] = json.dumps(got)
    return got


def _say(change):
    k, field, was, now = (list(change) + ["", "", "", ""])[:4]
    if field in ("appears", "goes"):
        return f"{k} {field}"
    if field == "shown":
        return f"{k} {'shows' if now == '1' else 'hides'}"
    return f"{k} {field} {was[:50]!r} -> {now[:50]!r}"


def _props(text):
    """A recorded style string ("color:rgb(0, 0, 0) ; background-color:…") as properties."""
    out = {}
    for part in str(text or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def behaviour_changes(bb, ba):
    """What hovering, clicking or typing into each element DOES, before against after."""
    out = []
    ab, aa = (bb or {}).get("acts") or {}, (ba or {}).get("acts") or {}
    for k in sorted(set(ab) | set(aa)):
        if k not in aa:
            out.append(_obs(_subject(k), "behaviour", f"{k} can no longer be hovered or clicked"))
            continue
        if k not in ab:
            continue          # a new interactive element: the DOM diff already records that it exists
        for act in ("hover", "click", "type"):
            before = {tuple(str(v) for v in x) for x in ab[k].get(act) or []}
            after = {tuple(str(v) for v in x) for x in aa[k].get(act) or []}
            gained, lost = sorted(after - before), sorted(before - after)
            # THE SAME REACTION WITH DIFFERENT VALUES IS NOT A LOST ONE AND A NEW ONE. Measured
            # 2026-09-15 on "make the Generate button green": hovering still brightened the same
            # button, but from a green instead of a black, and that was refused as a behaviour
            # nobody asked for. It is the colour change, seen under the pointer — so it is filed
            # by WHAT differs (colour here), on the element whose value changed, and a claim that
            # explains the colour explains this too.
            g_by = {(x[0], x[1]): x for x in gained}
            l_by = {(x[0], x[1]): x for x in lost}
            for same in sorted(set(g_by) & set(l_by)):
                g, l = g_by[same], l_by[same]
                moved = sorted({p for p in set(_props(g[3])) | set(_props(l[3]))
                                if _props(g[3]).get(p) != _props(l[3]).get(p)}
                               | {p for p in set(_props(g[2])) | set(_props(l[2]))
                                  if _props(g[2]).get(p) != _props(l[2]).get(p)})
                dim = (_style_dimension(moved[0]) if moved and same[1] == "style"
                       else _drift_dimension("at", same[1]) if same[1] == "attrs" else "behaviour")
                o = _obs(_subject(g[0]), dim,
                         f"{act} {k} still changes {same[0]}, with different values"
                         + (f" ({', '.join(moved[:3])})" if moved else "")
                         + f": {str(l[2])[:40]!r} -> {str(l[3])[:40]!r} became "
                           f"{str(g[2])[:40]!r} -> {str(g[3])[:40]!r}")
                o["involves"] = sorted({_subject(g[0]), _subject(k)})
                out.append(o)
                gained.remove(g)
                lost.remove(l)
            involved = {_subject(x[0]) for x in gained + lost}
            if gained:
                o = _obs(_subject(k), "behaviour", f"{act} {k} now also: " + "; ".join(_say(x) for x in gained[:4])
                         + (f" (+{len(gained) - 4} more)" if len(gained) > 4 else ""))
                o["involves"] = sorted(involved)
                out.append(o)
            if lost:
                o = _obs(_subject(k), "behaviour", f"{act} {k} no longer: " + "; ".join(_say(x) for x in lost[:4])
                         + (f" (+{len(lost) - 4} more)" if len(lost) > 4 else ""))
                o["involves"] = sorted(involved)
                o["lost"] = True
                out.append(o)
    return out


def _raw(shot, cx, cy, size):
    return [shot.rgb(x, y) for y in range(cy, min(cy + size, shot.h), 2) for x in range(cx, min(cx + size, shot.w), 2)]


def _stats(labs):
    n = len(labs)
    mL = sum(v[0] for v in labs) / n
    return mL, sum(v[1] for v in labs) / n, sum(v[2] for v in labs) / n, (sum((v[0] - mL) ** 2 for v in labs) / n) ** 0.5


def _corr(p, q):
    n = len(p)
    mp, mq = sum(x[0] for x in p) / n, sum(y[0] for y in q) / n
    sp = (sum((x[0] - mp) ** 2 for x in p) / n) ** 0.5
    sq = (sum((y[0] - mq) ** 2 for y in q) / n) ** 0.5
    if sp < 1e-6 or sq < 1e-6:
        return 1.0
    return sum((x[0] - mp) * (y[0] - mq) for x, y in zip(p, q)) / n / (sp * sq)


def lightness_grid(shot, w, h, size=16):
    """Mean lightness per cell — enough to see where the screen moves between two moments."""
    out = {}
    for cy in range(0, min(h, shot.h), size):
        for cx in range(0, min(w, shot.w), size):
            px = _raw(shot, cx, cy, size)
            if px:
                out[(cx, cy)] = sum(C.rgb_to_lab(p)[0] for p in px) / len(px)
    return out


def _frames(html, workdir, w, h, t, name):
    import aethron_figma_grade as GR
    import aethron_vision as V
    page = html.replace("</head>", FREEZE.replace("__T__", str(t)) + "</head>", 1)
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    f, png = work / f"_agm_{name}.html", work / f"_agm_{name}.png"
    f.write_text(C._with_base(page))
    if png.exists():
        png.unlink()
    GR.shoot(f, w, h, png)
    return V.load(png) if png.exists() else None


# ───────────────────────────── diffing ─────────────────────────────

def _subject(k):
    m = re.match(r"#([^>]+)", k or "")
    return m.group(1) if m else (k or "?")


def _obs(subject, dimension, detail, where=None, magnitude=None):
    return {"subject": subject, "dimension": dimension, "detail": detail, "where": where, "magnitude": magnitude}


def dom_changes(sb, sa):
    before = {e["k"]: e for e in sb.get("els") or []}
    after = {e["k"]: e for e in sa.get("els") or []}
    out = []
    drift_b, drift_a = set(sb.get("drift") or []), set(sa.get("drift") or [])
    drift = drift_b | drift_a

    def moves(k, field, name=""):
        return f"{k}|{field}|{name}" in drift
    # Only an element that changes by itself on ONE side is a difference. Which of its properties
    # happened to differ across 1.5s depends on where in its cycle each reading fell, so comparing
    # those lists would report the same moving sky as "changed" on every run.
    # It is filed under WHAT changes over time — a placeholder that now types itself is text changing,
    # a colour that now flickers is colour changing — so "motion" can never hide a colour.
    for k in sorted({d.split("|")[0] for d in drift_a} ^ {d.split("|")[0] for d in drift_b}):
        now = sorted(d for d in drift_a if d.startswith(k + "|"))
        was = sorted(d for d in drift_b if d.startswith(k + "|"))
        how = "now changes on its own over time" if now else "no longer changes on its own"
        byd = {}
        for d in (now or was):
            _, field, name = (d.split("|", 2) + ["", ""])[:3]
            byd.setdefault(_drift_dimension(field, name), []).append(name or field)
        for dim, names in sorted(byd.items()):
            o = _obs(_subject(k), dim, f"{k} {how}: {', '.join(names[:3])}")
            o["over_time"] = "gained" if now else "lost"
            out.append(o)
    for k in after.keys() - before.keys():
        e = after[k]
        out.append(_obs(_subject(k), "existence", f"{k} ({e['tag']}) is new" + (f", reading {e['own']!r}" if e["own"] else ""),
                        where=[e["x"], e["y"], e["w"], e["h"]]))
    for k in before.keys() - after.keys():
        out.append(_obs(_subject(k), "existence", f"{k} ({before[k]['tag']}) is gone"))
    for k in before.keys() & after.keys():
        b, a = before[k], after[k]
        box = [a["x"], a["y"], a["w"], a["h"]]
        if not (moves(k, "shown") or moves(k, "op")) and (
                (b["shown"] and b["op"] > 0.05) != (a["shown"] and a["op"] > 0.05) or abs(b["op"] - a["op"]) > 0.05):
            out.append(_obs(_subject(k), "visibility", f"{k} visibility {b['shown']}/{b['op']} -> {a['shown']}/{a['op']}", box))
        if not (moves(k, "x") or moves(k, "y")) and (abs(a["x"] - b["x"]) > 1.5 or abs(a["y"] - b["y"]) > 1.5):
            out.append(_obs(_subject(k), "position", f"{k} moved {b['x']},{b['y']} -> {a['x']},{a['y']}", box))
        if not (moves(k, "w") or moves(k, "h")) and (abs(a["w"] - b["w"]) > 1.5 or abs(a["h"] - b["h"]) > 1.5):
            out.append(_obs(_subject(k), "size", f"{k} resized {b['w']}x{b['h']} -> {a['w']}x{a['h']}", box))
        if not (moves(k, "own") or moves(k, "value")) and (a["own"] != b["own"] or a.get("value") != b.get("value")):
            out.append(_obs(_subject(k), "text", f"{k} words {b['own'] or b.get('value')!r} -> {a['own'] or a.get('value')!r}", box))
        for name in sorted(set(a["at"]) | set(b["at"])):
            if moves(k, "at", name):
                continue
            if a["at"].get(name) != b["at"].get(name):
                dim = "behaviour" if name.startswith("aria-") or name in ("role", "tabindex", "href", "type") else "attribute"
                out.append(_obs(_subject(k), dim, f"{k} attribute {name} {b['at'].get(name)!r} -> {a['at'].get(name)!r}", box))
        # WHAT FOLLOWS FROM A NEW SIZE IS THE SIZE CHANGE. A card made shorter reports a new `bottom`
        # (its far edge moved, the card did not) and a new transform-origin (still its centre, of a
        # smaller box). Filed as position and motion they were refused as changes nobody asked for.
        moved_box = abs(a["x"] - b["x"]) > 1.5 or abs(a["y"] - b["y"]) > 1.5
        resized = abs(a["w"] - b["w"]) > 1.5 or abs(a["h"] - b["h"]) > 1.5
        byd = {}
        for prop in sorted(set(a["st"]) | set(b["st"])):
            if moves(k, "st", prop) or a["st"].get(prop) == b["st"].get(prop):
                continue
            # A SIXTEEN-THOUSANDTH OF A PIXEL IS NOT A CHANGE. Measured 2026-09-15: new headline
            # words made width read 668.875px against 668.891px, and that was refused as "a change
            # of size nobody asked for". Numbers that agree to within a twentieth of a pixel are
            # the same number rendered twice.
            if _same_numbers(b["st"].get(prop), a["st"].get(prop)):
                continue
            if resized and prop in ORIGINS and _default_origin(b["st"].get(prop), b) \
                    and _default_origin(a["st"].get(prop), a):
                continue
            dim = _style_dimension(prop)
            if dim == "position" and prop in FAR_EDGES and not moved_box:
                if resized:
                    dim = "size"          # this element's own far edge followed its new size
                else:
                    # ITS BOX IS EXACTLY WHERE IT WAS. A label inside a button that grew reports a
                    # different `right`, because that number is measured against the button — the
                    # label did not move an inch, and refusing it asks a container never to resize.
                    continue
            byd.setdefault(dim, []).append(prop)
        for dim, props in byd.items():
            sample = "; ".join(f"{p} {str(b['st'].get(p))[:40]!r} -> {str(a['st'].get(p))[:40]!r}" for p in props[:3])
            out.append(_obs(_subject(k), dim, f"{k} {dim}: {sample}" + (f" (+{len(props) - 3} more)" if len(props) > 3 else ""),
                            box))
        if a.get("ps") != b.get("ps"):
            out.append(_obs(_subject(k), "effects", f"{k} ::before/::after changed", box))
    return out


def anim_changes(sb, sa):
    def sig(snap):
        out = {}
        for x in snap.get("anims") or []:
            out.setdefault((x.get("t"), x.get("props")), []).append((x.get("dur"), x.get("it")))
        return out
    b, a = sig(sb), sig(sa)
    out = []
    for key in sorted(set(a) | set(b), key=str):
        if a.get(key) != b.get(key):
            out.append(_obs(_subject(key[0]), "motion", f"animation of {key[0]} on {key[1] or '?'}: "
                                                          f"{len(b.get(key, []))} running {b.get(key)} -> "
                                                          f"{len(a.get(key, []))} running {a.get(key)}"))
    return out


def _owner(cx, cy, size, snap, w, h, changed=None, extra=(), overlap=False):
    """The smallest visible element with data-ae-id covering the cell's centre — among the elements
    that CHANGED, when `changed` is given; the page otherwise. Measured on the owner's orange sky:
    a patch of screen was blamed on the headline, a 1px rule and the glass card, none of which
    changed — the sky behind them did."""
    px, py, best = cx + size / 2, cy + size / 2, None

    def inside(x, y, bw, bh):
        # `overlap`: the cell touches the box at all, not only at its centre. Measured on the typing page:
        # letters that type themselves reached one cell past the text box's edge, and that cell was blamed
        # on the card behind it.
        if overlap:
            return x < cx + size and cx < x + bw and y < cy + size and cy < y + bh
        return x <= px < x + bw and y <= py < y + bh
    for e in snap.get("els") or []:
        if not e.get("ae") or e["ae"] == "bg" or not e["shown"] or e["op"] <= 0.05 or e["w"] * e["h"] >= 0.6 * w * h:
            continue
        if changed is not None and e["ae"] not in changed:
            continue
        if inside(e["x"], e["y"], e["w"], e["h"]):
            if best is None or e["w"] * e["h"] < best["w"] * best["h"]:
                best = e
    # Where an element WAS, and where its shadow falls: a cell there belongs to that element's move,
    # not to the page behind it. `extra` holds (id, x, y, w, h, the element's own area).
    for ae, x, y, bw, bh, area in extra:
        if changed is not None and ae not in changed:
            continue
        if inside(x, y, bw, bh) and (best is None or area < best["w"] * best["h"]):
            best = {"ae": ae, "w": area, "h": 1}
    return best["ae"] if best else "bg"


def _moved_boxes(snap_before, snap_after, w, h):
    """Where each element that moved, resized, appeared or vanished WAS and IS, with its shadow's
    reach: [(id, x, y, w, h, the element's own area)] — the screen its change is allowed to touch."""
    out = []
    now = {e.get("ae"): e for e in (snap_after or {}).get("els") or [] if e.get("ae")}
    for e in (snap_before or {}).get("els") or []:
        ae, a = e.get("ae"), now.get(e.get("ae"))
        if not ae or ae == "bg" or e["w"] * e["h"] >= 0.6 * w * h or not (e["shown"] or (a and a["shown"])):
            continue
        if a and max(abs(a[q] - e[q]) for q in ("x", "y", "w", "h")) <= 1.5 and a["shown"] == e["shown"]:
            continue
        for box in (e, a) if a else (e,):
            r = _reach(box)
            out.append((ae, box["x"] - r, box["y"] - r, box["w"] + 2 * r, box["h"] + 2 * r, e["w"] * e["h"]))
    return out


def pixel_changes(shot_b, shot_a, snap_after, w, h, changed=(), size=16, snap_before=None):
    """Cells that changed, per owner and dimension:
      lightness  the mean light moved more than 10
      colour     the hue turned more than 15 degrees, or the saturation moved more than 12
      detail     the PATTERN of light and dark changed (correlation under 0.8, or texture came or went)
      contrast   the same pattern, much stronger or weaker (how clearly text stands out)
    Structure and contrast are kept apart the way SSIM keeps them apart: text over a recoloured sky
    keeps its pattern even when its contrast moves."""
    groups = {}
    total = 0
    changed = set(changed)
    extra = _moved_boxes(snap_before, snap_after, w, h) if snap_before else []
    for cy in range(0, min(h, shot_b.h, shot_a.h), size):
        for cx in range(0, min(w, shot_b.w, shot_a.w), size):
            total += 1
            rb, ra = _raw(shot_b, cx, cy, size), _raw(shot_a, cx, cy, size)
            if not rb or rb == ra:
                continue
            pb_, pa_ = [C.rgb_to_lab(p) for p in rb], [C.rgb_to_lab(p) for p in ra]
            l0, a0, b0, s0 = _stats(pb_)
            l1, a1, b1, s1 = _stats(pa_)
            c0, c1 = math.hypot(a0, b0), math.hypot(a1, b1)
            hue_step = abs(((math.degrees(math.atan2(b1, a1)) - math.degrees(math.atan2(b0, a0))) + 180) % 360 - 180)
            corr = _corr(pb_, pa_)
            found = []
            if abs(l1 - l0) > 10:
                found.append(("lightness", l1 - l0))
            if (c0 > 10 and c1 > 10 and hue_step > 15) or abs(c1 - c0) > 12:
                found.append(("colour", hue_step if c0 > 10 and c1 > 10 else c1 - c0))
            if (s0 > 3 and s1 > 3 and corr < 0.8) or (max(s0, s1) > 6 and min(s0, s1) < 1.5):
                found.append(("detail", corr))
            elif s0 > 4 and s1 > 1.5 and abs(s1 / s0 - 1) > 0.35:
                found.append(("contrast", s1 / s0))
            if not found:
                continue
            for dim, delta in found:
                # A changed element the cell touches owns it; contrast that no changed element explains
                # belongs to whatever sits on top — the text that became harder or easier to read.
                owner = _owner(cx, cy, size, snap_after, w, h, changed, extra, overlap=True)
                if owner == "bg" and dim == "contrast":
                    owner = _owner(cx, cy, size, snap_after, w, h, extra=extra)
                l0_, l1_ = l0, l1
                cell = (cx, cy)
                g = groups.setdefault((owner, dim), {"n": 0, "sum": 0.0, "l0": 0.0, "l1": 0.0, "box": [1e9, 1e9, -1, -1]})
                g["n"] += 1
                g["sum"] += delta
                g["l0"] += l0_
                g["l1"] += l1_
                g["box"] = [min(g["box"][0], cell[0]), min(g["box"][1], cell[1]),
                            max(g["box"][2], cell[0] + size), max(g["box"][3], cell[1] + size)]
    out = []
    for (owner, dim), g in groups.items():
        share = g["n"] / total * 100
        box = [g["box"][0], g["box"][1], g["box"][2] - g["box"][0], g["box"][3] - g["box"][1]]
        if dim == "lightness":
            text = f"lightness moved {g['sum'] / g['n']:+.0f} on average ({g['l0'] / g['n']:.0f} -> {g['l1'] / g['n']:.0f})"
        elif dim == "colour":
            text = f"colour changed (hue or saturation) in {g['n']} cells"
        elif dim == "contrast":
            text = f"contrast changed x{g['sum'] / g['n']:.2f} on average in {g['n']} cells (how clearly it stands out)"
        else:
            text = f"the pattern of light and dark changed in {g['n']} cells (similarity {g['sum'] / g['n']:.2f})"
        o = _obs(owner, dim, f"{owner}: {text}, over {share:.1f}% of the page", box, round(share, 2))
        o["source"] = "pixels"
        out.append(o)
    return out


def fresh_errors(before_reading, after_reading, cap=3):
    """Only the faults that were NOT already there. A page's own broken scripts are not a
    difference between before and after — they are the same on both sides."""
    was = set((before_reading or {}).get("errors") or [])
    return [e for e in ((after_reading or {}).get("errors") or []) if e not in was][:cap]


def observe(before, after, workdir, w, h):
    """Everything that differs between two pages, in words, filed by dimension."""
    sb, sa = snapshot(before, workdir, w, h), snapshot(after, workdir, w, h)
    if sb is None or sa is None:
        return None
    obs = dom_changes(sb, sa) + anim_changes(sb, sa)
    wb, wa = sweep(before, workdir, w, h), sweep(after, workdir, w, h)
    if wb is None or wa is None:
        obs.append(_obs("page", "errors", "the behaviour sweep could not run — behaviour is UNVERIFIED"))
    else:
        obs += behaviour_changes(wb, wa)
        # A FAULT THE PAGE ALREADY HAD IS NOT A DIFFERENCE. The before reading is right here
        # and was never consulted: a real template throws before anything is touched (test-2
        # ships `WebFont is not defined`, `gsap is not defined`, `Lenis is not defined`), and
        # a correct minimal change was refused for them on every attempt — while the only
        # escape the model could find, an extra rule to quieten it, was then correctly
        # refused by mutation for being a part no test needs. A loop between a false refusal
        # and a true one, with no right answer in it.
        for e in fresh_errors(wb, wa, 2):
            obs.append(_obs("page", "errors", f"the behaviour sweep failed: {e}"))
    for e in fresh_errors(sb, sa, 3):
        obs.append(_obs("page", "errors", f"the page threw: {e}"))
    changed = {o["subject"] for o in obs}
    shot_b, shot_a = C._shot(before, workdir, w, h, "before"), C._shot(after, workdir, w, h, "after")
    if shot_b is not None and shot_a is not None:
        obs += pixel_changes(shot_b, shot_a, sa, w, h, changed, snap_before=sb)
    else:
        obs.append(_obs("page", "errors", "the screen could not be rendered — pixels are UNVERIFIED"))
    if sb.get("anims") or sa.get("anims"):
        # MOTION: WHERE the screen moves between two moments, before and after, in light only. Measured
        # on the owner's sky: counting colour as movement made an orange glow moving exactly like the
        # blue one look like different motion. Motion that appears, vanishes, or changes strength
        # more than 2.5x is an observation; the animations themselves are compared in the DOM.
        fb = [_frames(before, workdir, w, h, t, f"b{t}") for t in (2, 5)]
        fa = [_frames(after, workdir, w, h, t, f"a{t}") for t in (2, 5)]
        if all(fb) and all(fa):
            mb = _motion(lightness_grid(fb[0], w, h), lightness_grid(fb[1], w, h))
            ma = _motion(lightness_grid(fa[0], w, h), lightness_grid(fa[1], w, h))
            # Owned like the pixels are: a shorter glass card uncovers the moving sky below its old edge, and
            # that newly visible movement is the card's resize — measured on the battery's fixture, where it
            # was blamed on the sky and refused as motion nobody asked for.
            extra = _moved_boxes(sb, sa, w, h)
            moved = {}
            for cell in mb.keys() & ma.keys():
                b_, a_ = mb[cell], ma[cell]
                if (a_ > 4 and b_ < 1.5) or (b_ > 4 and a_ < 1.5) or (min(a_, b_) > 3 and max(a_, b_) / min(a_, b_) > 2.5):
                    moved.setdefault(_owner(cell[0], cell[1], 16, sa, w, h, changed, extra, overlap=True),
                                     []).append(a_ - b_)
            for owner, deltas in moved.items():
                o = _obs(owner, "motion", f"{owner}: how it moves changed in {len(deltas)} cells "
                                          f"(movement {sum(deltas) / len(deltas):+.1f} on average)")
                o["source"] = "frames"
                obs.append(o)
    return obs


def _motion(g1, g2):
    return {k: abs(g1[k] - g2[k]) for k in g1.keys() & g2.keys()}


# ───────────────────────────── explaining ─────────────────────────────

AGM_PROMPT = """You judge measured differences on a web page against what a person asked for.
You see the person's words and a numbered list of differences Aethron MEASURED between the page before and
after a change. You do not see the code.

For each difference answer:
- "asked": the person asked for this.
- "needed": it is a necessary part of what they asked for (for example, a new pop-up's own colours).
- "not asked": nothing the person said asks for it or requires it.
For "asked" or "needed", quote the person's own words that ask for it or require it. Be strict: a change to
how light or dark something is, its shape, its motion or its behaviour is only asked for when the words say so.

Return ONLY JSON: {"verdicts": [{"n": 1, "verdict": "asked" | "needed" | "not asked", "words": "<the person's words>"}]}
"""


# What a MEASURED claim is evidence of. verify measured the claim true and held it to the request's
# words (reduce must measure smaller; a button may ride with the card it sits on), so when the AGM
# records the same fact on the same element, the claim explains it — named in the request or not.
CLAIM_DIMENSIONS = {"size": ("size",), "position": ("position",), "color": ("colour",), "text": ("text",),
                    "removed": ("existence", "visibility"), "added": ("existence",),
                    "appears_on": ("existence", "visibility")}


def explain(observations, request, related=(), call=None, acted=(), claims=()):
    """-> (accepted notes, problems). `related`: ids the tests are about — new elements, elements the
    tests check, claim subjects. `acted`: ids the tests act on. `claims`: the measured claims. A change
    on something related, in a dimension the request's words speak about, is explained; so is the
    exact fact a measured claim measured; so is a new reaction of an element the tests act on, when
    everything it changes is related. The screen where an element moved, resized or changes by itself
    is decided by that element's own change. Everything else needs a reviewer's quote, or is refused.
    A LOST reaction is never explained by vocabulary alone."""
    words = C._unquoted(request).lower() + " " + (request or "").lower()
    related, acted = set(related), set(acted)
    claimed, timed = set(), set()
    for c in claims or ():
        if not isinstance(c, dict):
            continue
        eid = c.get("id") if isinstance(c.get("id"), str) else None
        if c.get("kind") == "colors":
            region = c.get("region") or "background"
            claimed.add(("bg" if region == "background" else region, "colour"))
        elif c.get("kind") == "changes_over_time" and eid:
            timed.add(eid)
        elif eid:
            claimed |= {(eid, d) for d in CLAIM_DIMENSIONS.get(c.get("kind"), ())}
    notes, problems, ask, follows, good = [], [], [], [], set()
    # A NEW ELEMENT'S OWN PIXELS, STYLES AND WORDS ARE PART OF IT. When its existence is explained,
    # the light, detail and contrast it adds where it sits are that same change, not new ones.
    born = {o["subject"] for o in observations if o["dimension"] == "existence" and " is new" in o["detail"]
            and o["subject"] in related and re.search(DIMENSION_WORDS["existence"], words)}
    moving = bool(re.search(DIMENSION_WORDS["motion"], words))
    # WHAT AN ELEMENT'S OWN CHANGE DOES TO THE SCREEN IS THAT CHANGE. A button riding up with a shorter
    # card changes the pixels where it was and where it is; a placeholder that types itself changes the
    # pixels of its words. Measured 2026-09-15: without this, an honest height reduction had 36 of its
    # 38 differences refused. Anything else behind those pixels — a colour, a filter, an overlay — is
    # its own DOM difference and is judged on its own.
    shaped = {o["subject"] for o in observations if o.get("source") not in ("pixels", "frames")
              and (o["dimension"] in GEOMETRY or o.get("over_time"))}
    # NEW WORDS TAKE A DIFFERENT AMOUNT OF ROOM. A headline given shorter text is narrower, and its
    # far edge moves with it; refusing that asks for words that change without occupying space.
    reworded = {o["subject"] for o in observations
                if o["dimension"] == "text" and o.get("source") not in ("pixels", "frames")
                and re.search(DIMENSION_WORDS["text"], words)
                and (o["subject"] in related or (o["subject"], "text") in claimed)}
    # A BLACK BUTTON MADE GREEN IS LIGHTER, AND THAT IS THE SAME FACT. Measured 2026-09-15: the
    # pixels of a correctly recoloured button were refused for lightness, detail and contrast.
    # NOT the page's background: a recoloured sky must keep its light where it was — the owner's
    # own complaint — and that is enforced separately, band by band.
    recoloured = {o["subject"] for o in observations
                  if o["dimension"] == "colour" and o.get("source") not in ("pixels", "frames")
                  and o["subject"] != "bg"
                  and ((o["subject"], "colour") in claimed
                       or (o["subject"] in related and re.search(DIMENSION_WORDS["colour"], words)))}
    for o in observations:
        vocab = DIMENSION_WORDS.get(o["dimension"])
        spoken = bool(vocab and re.search(vocab, words))
        if o["subject"] in born and o["dimension"] != "errors" and not o.get("lost"):
            notes.append(f"AGM explained (part of the new {o['subject']} the tests require): {o['detail']}")
            good.add(o["subject"])
            continue
        if o.get("source") in ("pixels", "frames") and (o["subject"] in shaped
                                                        or o["subject"] in recoloured):
            follows.append(o)
            continue
        reaction = (o["dimension"] == "behaviour" and not o.get("lost") and o["subject"] in acted
                    and set(o.get("involves") or []) <= related | {o["subject"]})
        if o["dimension"] == "errors":
            problems.append(f"AGM: {o['detail']}")
        elif o.get("over_time"):
            # Changing BY ITSELF needs words for it ("automatically", "types", "again") as well as for
            # what changes: a timed claim explains words that type themselves, never a colour that flickers.
            if (o["subject"] in timed and o["dimension"] == "text") or (moving and spoken and o["subject"] in related):
                notes.append(f"AGM explained (changes by itself, as the request and the tests require): {o['detail']}")
                good.add(o["subject"])
            elif moving and spoken:
                ask.append(o)
            else:
                problems.append(f"AGM: {o['detail']} — nothing in the request asks for {o['dimension']} that changes by itself")
        elif reaction and spoken:
            notes.append(f"AGM explained (a reaction the tests act on, changing only what they are about): {o['detail']}")
        elif o.get("lost") and spoken:
            ask.append(o)
        elif (o["subject"], o["dimension"]) in claimed:
            notes.append(f"AGM explained ({o['dimension']} of {o['subject']}, measured true by its claim): {o['detail']}")
            good.add(o["subject"])
        elif o["subject"] in related and spoken:
            notes.append(f"AGM explained ({o['dimension']}, {o['subject']} is what the tests are about): {o['detail']}")
            good.add(o["subject"])
        elif o["dimension"] in ("size", "position") and o["subject"] in reworded:
            notes.append(f"AGM explained (the room {o['subject']}'s new words take): {o['detail']}")
            good.add(o["subject"])
        elif not spoken and o["dimension"] != "other":
            problems.append(f"AGM: {o['detail']} — nothing in the request asks for a change of {o['dimension']}")
        else:
            ask.append(o)
    if ask:
        verdicts = None
        if call is not None:
            listing = "\n".join(f"{n + 1}. [{o['dimension']}] {o['detail']}" for n, o in enumerate(ask))
            try:
                reply = call(AGM_PROMPT + "\n\nTHE PERSON ASKED FOR:\n" + request + "\n\nTHE MEASURED DIFFERENCES:\n" + listing)
                verdicts = C._parse_plan(reply) if isinstance(reply, str) else reply
            except Exception:
                verdicts = None
        by_n = {}
        for v in (verdicts or {}).get("verdicts") or []:
            if isinstance(v, dict) and isinstance(v.get("n"), int):
                by_n[v["n"]] = v
        stems = {w[:5] for w in re.findall(r"[a-z0-9]+", (request or "").lower()) if len(w) > 2}
        for n, o in enumerate(ask):
            v = by_n.get(n + 1) or {}
            quote = str(v.get("words") or "")
            qs = {w[:5] for w in re.findall(r"[a-z0-9]+", quote.lower()) if len(w) > 2}
            quoted = bool(qs) and len(qs & stems) >= 0.6 * len(qs)
            if v.get("verdict") in ("asked", "needed") and quoted:
                notes.append(f"AGM explained by the reviewer (\"{quote[:80]}\"): {o['detail']}")
                good.add(o["subject"])
            elif verdicts is None:
                problems.append(f"AGM: {o['detail']} — could not be checked against the request (no reviewer) — UNVERIFIED")
            else:
                problems.append(f"AGM: {o['detail']} — the reviewer found nothing in the request that asks for it")
    for o in follows:
        if o["subject"] in good:
            notes.append(f"AGM explained (the screen where {o['subject']}'s explained change happened): {o['detail']}")
        else:
            problems.append(f"AGM: {o['detail']} — the screen where {o['subject']} changed, and that change was not explained")
    return notes, problems


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

    base = {"k": "#s04", "tag": "button", "ae": "s04", "x": 10, "y": 10, "w": 100, "h": 40, "shown": True, "op": 1,
            "own": "", "value": None, "at": {"aria-pressed": "false"}, "st": {"color": "rgb(0, 0, 0)", "cursor": "pointer",
                                                                           "text-transform": "none"}, "ps": {}}
    print("── every difference is found, in any property, and filed by dimension")
    after = dict(base, st=dict(base["st"], **{"text-transform": "uppercase", "cursor": "wait"}))
    got = dom_changes({"els": [base]}, {"els": [after]})
    dims = sorted(o["dimension"] for o in got)
    check("a property no check was ever written for is still found", dims == ["behaviour", "typography"], str(got))
    pop = {"k": "#p04", "tag": "div", "ae": "p04", "x": 10, "y": 50, "w": 120, "h": 80, "shown": False, "op": 0,
           "own": "", "value": None, "at": {}, "st": {}, "ps": {}}
    got = dom_changes({"els": [base]}, {"els": [base, pop]})
    check("a new element is found", len(got) == 1 and got[0]["dimension"] == "existence" and got[0]["subject"] == "p04", str(got))

    drifting_b = {"els": [base], "drift": ["#s04|st|color"]}
    drifting_a = {"els": [dict(base, st=dict(base["st"], color="rgb(9, 9, 9)"))], "drift": ["#s04|st|color"]}
    check("a property that changes on its own in both pages is not a difference", not dom_changes(drifting_b, drifting_a))
    got = dom_changes({"els": [base], "drift": []}, {"els": [base], "drift": ["#s04|at|placeholder"]})
    check("something that now changes on its own (a typing loop) is filed as its text changing over time",
          len(got) == 1 and got[0]["dimension"] == "text" and got[0].get("over_time") == "gained", str(got))

    print("\n── what each element DOES is recorded, and a lost reaction is found")
    toggles = {"acts": {"#s03": {"hover": [["#s03", "style", "filter:none", "filter:brightness(1.12)"]],
                                 "click": [["#s03", "attrs", "aria-pressed=false", "aria-pressed=true"]]}}}
    with_pop = {"acts": {"#s03": {"hover": [["#s03", "style", "filter:none", "filter:brightness(1.12)"],
                                            ["#p03", "shown", "0", "1"]],
                                  "click": [["#s03", "attrs", "aria-pressed=false", "aria-pressed=true"],
                                            ["#p03", "shown", "0", "1"]]}}}
    got = behaviour_changes(toggles, with_pop)
    check("hovering and clicking now also show the pop-up: two new reactions",
          len(got) == 2 and all("now also" in o["detail"] and o["involves"] == ["p03"] for o in got), str(got))
    broke = {"acts": {"#s03": {"hover": toggles["acts"]["#s03"]["hover"], "click": []}}}
    got = behaviour_changes(toggles, broke)
    check("a click that no longer toggles is a lost reaction", len(got) == 1 and got[0].get("lost"), str(got))
    notes, problems = explain(behaviour_changes(toggles, with_pop), "Give the Mac OS button a pop-up on hover or click",
                              related={"p03"}, acted={"s03"})
    check("new reactions of a tested trigger that only show the new pop-up are explained", notes and not problems,
          str(problems))
    notes, problems = explain(got, "Give the Mac OS button a pop-up on hover or click", related={"p03"}, acted={"s03"})
    check("a lost reaction is never waved through by vocabulary — with no reviewer it is UNVERIFIED",
          problems and "UNVERIFIED" in problems[0], str(problems))

    print("\n── the light of the screen is measured, not only its colours")

    class Paint:
        def __init__(self, fn, w=64, h=64):
            self.w, self.h, self.fn = w, h, fn

        def rgb(self, x, y):
            return self.fn(x, y)
    blue = Paint(lambda x, y: (250, 250, 252) if y < 32 else (47, 70, 236))
    kept = Paint(lambda x, y: (250, 250, 252) if y < 32 else C.from_lch(C.lch((47, 70, 236))[0], C.lch((47, 70, 236))[1],
                                                                          C.lch(C.TARGET_REF["orange"])[2]))
    flat = Paint(lambda x, y: (255, 120, 20) if y < 32 else (170, 60, 8))
    snap = {"els": []}
    a = pixel_changes(blue, kept, snap, 64, 64)
    check("recoloured at the same light: a colour change only", sorted({o["dimension"] for o in a}) == ["colour"], str(a))
    b = pixel_changes(blue, flat, snap, 64, 64)
    check("recoloured and darkened: the light change is found as well", "lightness" in {o["dimension"] for o in b}, str(b))
    text = Paint(lambda x, y: (255, 255, 255) if (x // 4 + y // 4) % 2 else (20, 30, 90))
    text_on_orange = Paint(lambda x, y: (255, 255, 255) if (x // 4 + y // 4) % 2 else (90, 40, 10))
    c = pixel_changes(text, text_on_orange, snap, 64, 64)
    check("text over a recoloured ground keeps its pattern: no detail change is reported",
          "detail" not in {o["dimension"] for o in c}, str(c))
    stripes = Paint(lambda x, y: (250, 250, 250) if y % 8 < 4 else (10, 10, 10))
    solid = Paint(lambda x, y: (130, 130, 130))
    d = pixel_changes(stripes, solid, snap, 64, 64)
    check("a pattern that vanishes is a detail change", "detail" in {o["dimension"] for o in d}, str(d))
    tagged = {"els": [{"ae": "t05", "shown": True, "op": 1, "x": 0, "y": 0, "w": 64, "h": 32}]}
    e = pixel_changes(blue, flat, tagged, 64, 64, changed={"bg"})
    check("pixels under an element that did not change belong to what changed behind it",
          {o["subject"] for o in e if o["dimension"] != "contrast"} == {"bg"}, str(e))

    print("\n── every difference must be explained by the request")
    ask = "Change the background animation to orange blending with black"
    notes, problems = explain(a, ask, related={"bg"})
    check("a colour change on the background, asked in colour words, is explained", not problems and notes, str(problems))
    notes, problems = explain(b, ask, related={"bg"})
    check("the same request cannot explain a lightness change", any("lightness" in p for p in problems), str(problems))
    unasked = [_obs("s04", "behaviour", "#s04 behaviour: cursor 'pointer' -> 'wait'")]
    notes, problems = explain(unasked, "Give the Mac OS button a pop-up when I hover it", related={"p04"},
                              call=lambda prompt: json.dumps({"verdicts": [{"n": 1, "verdict": "not asked", "words": ""}]}))
    check("a behaviour change the reviewer finds unasked is refused", problems and "nothing in the request" in problems[0],
          str(problems))
    notes, problems = explain(unasked, "Give the Mac OS button a pop-up when I hover it", related={"p04"},
                              call=lambda prompt: json.dumps({"verdicts": [{"n": 1, "verdict": "asked",
                                                                           "words": "make everything spin"}]}))
    check("a reviewer's approval quoting words the person never said is refused", problems, str(problems))
    notes, problems = explain(unasked, "Give the Mac OS button a pop-up when I hover it", related={"p04"})
    check("with no reviewer, an unexplained change is UNVERIFIED, never accepted", problems and "UNVERIFIED" in problems[0],
          str(problems))
    was = {"acts": {"#s01": {"hover": [["#s01", "style", "background-color:rgb(16, 16, 20)",
                                        "background-color:rgb(20, 20, 24)"]]}}}
    now = {"acts": {"#s01": {"hover": [["#s01", "style", "background-color:rgb(34, 197, 94)",
                                        "background-color:rgb(41, 220, 110)"]]}}}
    got = behaviour_changes(was, now)
    check("the same hover in a new colour is a colour difference, not a reaction lost and another gained",
          len(got) == 1 and got[0]["dimension"] == "colour" and not got[0].get("lost"), str(got))
    notes, problems = explain(got, "Make the Generate button green", related={"s01"},
                              claims=[{"kind": "color", "id": "s01", "property": "background",
                                       "equals": "#22c55e"}])
    check("  ...and the claim that explains the button's colour explains it under the pointer too",
          notes and not problems, str(problems))

    head_b = dict(base, k="#t05", ae="t05", tag="h1", w=668.9, own="Build Apps People Love",
                  st={"width": "668.875px", "right": "372.125px", "color": "rgb(0, 0, 0)"})
    head_a = dict(head_b, w=621.0, own="Ship apps people love",
                  st={"width": "621.016px", "right": "419.984px", "color": "rgb(0, 0, 0)"})
    got = dom_changes({"els": [head_b]}, {"els": [head_a]})
    notes, problems = explain(got, "Change the headline to say 'Ship apps people love'", related={"t05"},
                              claims=[{"kind": "text", "id": "t05", "equals": "Ship apps people love"}])
    check("new words are allowed the room they take — the width and far edge that follow them",
          notes and not problems, str(problems))
    same = dict(head_b, w=668.891, st={"width": "668.891px", "right": "372.109px", "color": "rgb(0, 0, 0)"})
    check("a sixteen-thousandth of a pixel is not a difference at all",
          not dom_changes({"els": [head_b]}, {"els": [same]}), str(dom_changes({"els": [head_b]}, {"els": [same]})))

    print("\n── a change and what it must cause are one change")
    card_b = dict(base, k="#s00", ae="s00", tag="div", x=100, y=100, w=300, h=200,
                  st={"height": "200px", "bottom": "260px", "transform-origin": "150px 100px", "color": "rgb(0, 0, 0)"})
    card_a = dict(card_b, h=180, st={"height": "180px", "bottom": "280px", "transform-origin": "150px 90px",
                                     "color": "rgb(0, 0, 0)"})
    got = dom_changes({"els": [card_b]}, {"els": [card_a]})
    check("a shorter card: its far edge and its centre follow the new size, so every difference is a size change",
          got and {o["dimension"] for o in got} == {"size"}, str(got))
    chip_b = dict(base, k="#s01", ae="s01", y=260)
    chip_a = dict(chip_b, y=240, st=dict(base["st"], top="240px"))
    obs = dom_changes({"els": [card_b, chip_b]}, {"els": [card_a, chip_a]})
    ask_h = "Can you reduce this chat box height a little bit?"
    claims_h = [{"kind": "size", "id": "s00", "dimension": "height", "change": "-10%"},
                {"kind": "position", "id": "s01", "axis": "y", "change": "-20px"}]
    notes, problems = explain(obs, ask_h, related={"s00", "s01"}, claims=claims_h)
    check("a button riding up with the shorter card is explained by its measured position claim", notes and not problems,
          str(problems))
    notes, problems = explain(obs, ask_h, related={"s00", "s01"}, claims=claims_h[:1])
    check("the same move with no claim that measured it is refused", any("s01" in p for p in problems), str(problems))
    moved = [_obs("s01", "position", "#s01 moved 10,260 -> 10,240"),
             dict(_obs("s01", "lightness", "s01: lightness moved -20 on average", None, 0.4), source="pixels")]
    notes, problems = explain(moved, ask_h, related={"s00", "s01"}, claims=claims_h)
    check("the screen where an explained move happened is part of that move", len(notes) == 2 and not problems,
          str(problems))
    notes, problems = explain(moved, "Make the headline bold", related={"t00"})
    check("the screen where an unexplained move happened is refused along with the move", len(problems) == 2,
          str(problems))
    dark_top = Paint(lambda x, y: (20, 20, 20) if y < 32 else (240, 240, 240))
    dark_low = Paint(lambda x, y: (20, 20, 20) if 16 <= y < 48 else (240, 240, 240))
    was = {"els": [{"ae": "s01", "shown": True, "op": 1, "x": 0, "y": 0, "w": 64, "h": 32, "st": {}}]}
    now = {"els": [{"ae": "s01", "shown": True, "op": 1, "x": 0, "y": 16, "w": 64, "h": 32, "st": {}}]}
    f = pixel_changes(dark_top, dark_low, now, 64, 64, changed={"s01"}, snap_before=was)
    check("the screen an element left behind belongs to that element, not to the page behind it",
          f and {o["subject"] for o in f} == {"s01"}, str(f))
    blank = Paint(lambda x, y: (240, 240, 240), 128, 64)
    typed = Paint(lambda x, y: (20, 20, 20) if 12 <= x < 30 and 20 <= y < 28 else (240, 240, 240), 128, 64)
    field = {"els": [{"ae": "t08", "shown": True, "op": 1, "x": 0, "y": 0, "w": 20, "h": 64, "st": {}},
                     {"ae": "s00", "shown": True, "op": 1, "x": 0, "y": 0, "w": 60, "h": 64, "st": {}}]}
    g = pixel_changes(blank, typed, field, 128, 64, changed={"t08"})
    check("letters that reach a cell past their own box still belong to the element that changed, not the card behind",
          g and {o["subject"] for o in g} == {"t08"}, str(g))
    check("a shadow's reach is read from the computed box-shadow",
          _reach({"st": {"box-shadow": "rgba(82, 105, 159, 0.787) 0px 38px 84px 0px"}}) == 122, "")
    flicker = dom_changes({"els": [base], "drift": []}, {"els": [base], "drift": ["#s04|at|placeholder", "#s04|st|color"]})
    notes, problems = explain(flicker, "Make the chat box type different texts automatically", related={"s04"},
                              claims=[{"kind": "changes_over_time", "id": "s04"}])
    check("a placeholder that now types itself is explained; a colour that now changes by itself is not",
          any("placeholder" in n for n in notes) and any("color" in p for p in problems), str(notes) + str(problems))
    green = [_obs("s01", "colour", "#s01 colour: background-color 'rgb(16,16,20)' -> 'rgb(0,128,0)'"),
             dict(_obs("s01", "lightness", "s01: lightness moved +55 on average (28 -> 83), over 0.6%",
                       None, 0.6), source="pixels"),
             dict(_obs("s01", "contrast", "s01: contrast changed x0.06 in 6 cells", None, 0.1), source="pixels")]
    notes, problems = explain(green, "Make the Generate button green", related={"s01"},
                              claims=[{"kind": "color", "id": "s01", "property": "background",
                                       "equals": "#008000"}])
    check("a button made green is allowed to be lighter where it sits", len(notes) == 3 and not problems,
          str(problems))
    orange = [_obs("bg", "colour", "#bg colour: background-image changed"),
              dict(_obs("bg", "lightness", "bg: lightness moved -39 on average", None, 41.7), source="pixels")]
    notes, problems = explain(orange, ask, related={"bg"}, claims=[{"kind": "colors", "region": "background",
                                                                   "families": ["orange", "black"]}])
    check("a measured colour claim still cannot explain a darker sky", any("lightness" in p for p in problems),
          str(problems))

    print("\n── a fault the page already had is not a difference")
    gsap = "Uncaught ReferenceError: gsap is not defined"
    webfont = "Uncaught ReferenceError: WebFont is not defined"
    check("a template's own broken scripts are not blamed on the change",
          fresh_errors({"errors": [gsap, webfont]}, {"errors": [gsap, webfont]}) == [])
    check("  ...but one the change introduced is reported",
          fresh_errors({"errors": [gsap]}, {"errors": [gsap, "TypeError: x is not a function"]})
          == ["TypeError: x is not a function"])
    check("  ...and a page that threw nothing before is held to that",
          fresh_errors({}, {"errors": [gsap]}) == [gsap])
    check("  ...an error that DISAPPEARED is not reported as new",
          fresh_errors({"errors": [gsap]}, {"errors": []}) == [])

    print(f"\nagm selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if len(argv) < 2:
        print("usage: aethron_agm.py <before.html> <after.html> [\"request\"]")
        return 2
    before, after = Path(argv[0]).read_text(), Path(argv[1]).read_text()
    import aethron_edit as AE
    canvas = AE.manifest(before)["canvas"]
    work = Path(tempfile.mkdtemp(prefix="ae-agm-"))
    obs = observe(before, after, work, canvas["w"], canvas["h"])
    if obs is None:
        print("VERDICT: SKIPPED — the pages could not be recorded")
        return 0
    for o in obs:
        print(f"  [{o['dimension']:10}] {o['detail']}")
    if len(argv) > 2:
        notes, problems = explain(obs, argv[2], related=set())
        print("\n".join(["", *notes, *problems]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

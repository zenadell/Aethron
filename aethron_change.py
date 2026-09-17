#!/usr/bin/env python3
"""Change anything on a page Aethron built — and PROVE the change is the one asked for.

The owner, after three reasonable requests came back "not supported": a coding agent that
cannot change its own work at the user's command, to any shape or structure, is rubbish,
however accurate the first version was. And in the same breath: whatever it changes must
be MEASURED, so that it never hallucinates a change and never does something else.

Both, then. The model is free to write code — patches to the page's own source, CSS, a
script — the way any coding agent is. It is NOT trusted about what that code did:

  1. Before anything, Aethron reads every element's real box, words, colours and styles
     from the browser. A page it cannot measure is a page it will not change.
  2. The model must say what will be TRUE afterwards, in claims Aethron can measure:
     "s00 height -10%", "the background is mostly orange and black", "t08's words change
     as time passes". The claims must match the REQUEST, not just the code: a request to
     reduce needs a claim that reduces, "a little" is bounded, a colour the user named
     must be a colour that is measured.
  3. The change is applied to a copy and measured: every claim, to the pixel; the direction
     and size of the change against the words used; every element the model did not name
     keeps its box, words, colours and styles; things riding on a changed element may move
     with it and nothing else; nothing leaves the page or scrolls it sideways; no script
     errors; pixels outside what was named are unchanged; a moving background still moves
     and still starts on its design.
  4. Anything that fails goes back to the model WITH THE MEASURED NUMBERS. If it still
     fails, nothing is changed and the reasons are reported.

Anything that reacts — a pop-up, a dropdown, a colour on hover — is USED the way a person
uses it: hovered, the pointer carried onto what opened, moved away, clicked, Escape pressed,
a click outside. What a person would see after each step is measured, and so is what the
trigger did before the change: a pop-up that opens but breaks its button, hides behind the
card, can never be closed, or sits invisibly over the next button is not a pop-up that works.
"""
from collections import Counter
import colorsys
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_edit as AE      # noqa: E402

PROBE_HEAD = ("<script>window.__aeErr=[];addEventListener('error',function(e){"
              "__aeErr.push(String((e&&e.message)||'script error'))});</script>")
PROBE_TAIL = """<script>(function(){
// HOLD THE PAGE STILL BEFORE READING IT. A real template is alive — a ticker slides, a
// slider advances, a headline types itself — and two readings taken at different moments
// differ with NOTHING changed. Measured on a real Webflow migration: 28 elements reported as
// moved, the same elements moving the OTHER way on the next run, because the page does not
// render identically twice. Filtering that noise afterwards cannot keep up with it (the set
// of things that drifted is different every render); the AGM's snapshot has always frozen
// first, and the probe everything else rests on never did.
function freeze(){try{document.getAnimations().forEach(function(a){try{
var t=a.effect&&a.effect.getComputedTiming();
if(t&&!isFinite(t.endTime)){a.pause();a.currentTime=0;}else{a.finish();}}catch(e){}});}catch(e){}}
function ownText(el){if(el.tagName==='TEXTAREA'||el.tagName==='INPUT')return el.value||el.getAttribute('placeholder')||'';
var s='',tw=document.createTreeWalker(el,NodeFilter.SHOW_TEXT),n;while((n=tw.nextNode())){var p=n.parentElement;
if(p&&p.closest('[data-ae-id]')===el)s+=n.nodeValue+' ';}return s.replace(/\\s+/g,' ').trim().slice(0,300);}
function hitOf(el,r){if(el.getAttribute('data-ae-id')==='bg'||r.width<1||r.height<1)return null;
var x=r.left+r.width/2,y=r.top+r.height/2;if(x<0||y<0||x>=innerWidth||y>=innerHeight)return null;
var t=document.elementFromPoint(x,y);return !!(t&&(t===el||el.contains(t)));}
function run(){freeze();var o={els:{},errors:window.__aeErr||[],
sw:document.documentElement.scrollWidth,iw:innerWidth,fonts:document.fonts?document.fonts.status:''};
document.querySelectorAll('[data-ae-id]').forEach(function(el){var r=el.getBoundingClientRect(),
cs=getComputedStyle(el),lab=el.tagName==='BUTTON'?el.querySelector('span'):null;
o.els[el.getAttribute('data-ae-id')]={tag:el.tagName.toLowerCase(),x:Math.round(r.left*10)/10,y:Math.round(r.top*10)/10,
w:Math.round(r.width*10)/10,h:Math.round(r.height*10)/10,
text:(el.tagName==='TEXTAREA'?(el.value||el.getAttribute('placeholder')||''):(el.textContent||''))
.replace(/\\s+/g,' ').trim().slice(0,300),color:cs.color,bg:cs.backgroundColor,
bgi:(cs.backgroundImage||'').slice(0,400),vis:cs.visibility,disp:cs.display,op:cs.opacity,
lcolor:lab?getComputedStyle(lab).color:null,own:ownText(el),hit:hitOf(el,r),
up:(function(){var p=el.parentElement&&el.parentElement.closest('[data-ae-id]');
return p?p.getAttribute('data-ae-id'):null;})(),
st:[cs.fontSize,cs.fontWeight,cs.fontFamily,cs.letterSpacing,cs.borderRadius,cs.boxShadow,
cs.backdropFilter||cs.webkitBackdropFilter||'',cs.filter,
cs.borderTopWidth+' '+cs.borderTopStyle+' '+cs.borderTopColor].join('|')};});
o.struct=Array.prototype.filter.call(document.body.querySelectorAll('*'),function(e){
return !e.hasAttribute('data-ae-id')&&!(e.parentElement&&e.parentElement.closest('[data-ae-id]:not([data-ae-id="bg"])'))
&&!/^(SCRIPT|STYLE|LINK|META|NOSCRIPT|TEMPLATE)$/.test(e.tagName);}).map(function(e){
return e.tagName.toLowerCase()+(typeof e.className==='string'&&e.className.trim()?'.'+e.className.trim().split(/\\s+/).join('.'):'');}).join(' ');
var d=document.createElement('script');d.type='application/json';d.id='__ae_probe';
d.textContent=JSON.stringify(o);document.body.appendChild(d);}
// READ IT WHEN IT HAS STOPPED ARRIVING, NOT AT A FIXED MOMENT. A real page settles at its
// own pace — fonts resolve, lazy images decode, late script writes DOM — and a reading taken
// on a timer catches a different stage of that every load. Measured on a real Webflow
// migration: 262 elements differed between two renders of the UNCHANGED page. That was
// called a limit of what can be proven; it is not, it is a reading taken too early. So:
// wait for the fonts to be done, every image to be complete, and the DOM to go quiet for a
// stretch — then freeze and record. The cap means a page that never settles is still read
// rather than hanging, and what it costs is virtual time, which is not wall time.
function settled(cb){var last=performance.now(),obs=null;
try{obs=new MutationObserver(function(){last=performance.now();});
obs.observe(document.documentElement,{subtree:true,childList:true,attributes:true,characterData:true});}catch(e){}
function imgs(){var l=document.images;for(var i=0;i<l.length;i++){if(!l[i].complete)return false;}return true;}
function tick(){var now=performance.now();
var f=(!document.fonts)||document.fonts.status==='loaded';
if((f&&imgs()&&(now-last)>__QUIET__)||now>__CAP__){try{obs&&obs.disconnect();}catch(e){}cb();return;}
setTimeout(tick,50);}
setTimeout(tick,120);}
// SETTLING AND SAMPLING ARE DIFFERENT JOBS. The wait for the page to stop arriving runs to an
// ABSOLUTE deadline, identical for every reading — when it was measured from each sampling
// moment instead, the early reading gave up unsettled while a later one settled, and the two
// disagreed about 273 elements on a page nobody had touched. Only AFTER the page is settled
// does the moment matter, and then it means what it should: how far into the page's own
// motion we are looking.
settled(function(){setTimeout(run,__AT__);});})();</script>"""

TIMELINE_TAIL = """<script>(function(){var ids=__IDS__,seq={},pre={},kept={},USER='My own idea, typed by a person';
ids.forEach(function(i){seq[i]=[];});
function node(i){return document.querySelector('[data-ae-id="'+i+'"]');}
function field(el){return el&&(el.tagName==='TEXTAREA'||el.tagName==='INPUT');}
function read(el){return (field(el)?(el.value||el.getAttribute('placeholder')||''):(el.textContent||''))
.replace(/\\s+/g,' ').trim();}
function tick(){ids.forEach(function(i){var el=node(i),s=el?read(el):null,a=seq[i];
if(a.length<6000&&(!a.length||a[a.length-1]!==s))a.push(s);});}
tick();var iv=setInterval(tick,__EVERY__);
setTimeout(function(){ids.forEach(function(i){var el=node(i);pre[i]=seq[i].slice();
if(field(el)){el.focus();el.value=USER;el.dispatchEvent(new Event('input',{bubbles:true}));}});},__USER_AT__);
setTimeout(function(){clearInterval(iv);ids.forEach(function(i){var el=node(i);if(field(el))kept[i]=el.value===USER;});
var d=document.createElement('script');d.type='application/json';d.id='__ae_timeline';
d.textContent=JSON.stringify({seq:pre,kept:kept});document.body.appendChild(d);},__UNTIL__);})();</script>"""

# A PERSON, SCRIPTED. CSS :hover cannot be switched on by a synthetic event, so every :hover rule
# is copied onto a class the simulated pointer carries up the tree it rests on; the events a
# real pointer sends are dispatched too, for pages that listen for them. After each step,
# finite animations and transitions are finished, so what is read is where the page SETTLES.
INTERACT_TAIL = """<script>(function(){
var SPEC=__SPEC__,HOVER='__ae_hover',hovered=[],out={errors:[]};
var SKIP=/^(class|style|aria-expanded|aria-haspopup|aria-controls|aria-describedby|aria-owns|data-state|data-open)$/;
var ISEL='a,button,[role=menuitem],[role=option],[role=menuitemradio],[role=menuitemcheckbox],li,label,input,select';
var INERT='a,button,input,textarea,select,label,summary,[onclick],[role=button],[role=menuitem],[tabindex]';
function q(i){return i?document.querySelector('[data-ae-id="'+i+'"]'):null;}
function wait(ms){return new Promise(function(r){setTimeout(r,ms);});}
function r1(v){return Math.round(v*10)/10;}
function settle(){void document.body.offsetWidth;try{document.getAnimations().forEach(function(a){try{
var t=a.effect&&a.effect.getComputedTiming();if(t&&isFinite(t.endTime))a.finish();}catch(e){}});}catch(e){}}
function emulate(list,owner){for(var i=list.length-1;i>=0;i--){var r=list[i];
if(r.selectorText===undefined){if(r.cssRules){try{emulate(r.cssRules,r);}catch(e){}}continue;}
if(r.selectorText.indexOf(':hover')>-1){try{owner.insertRule(r.selectorText.replace(/:hover/g,'.'+HOVER)
+'{'+r.style.cssText+'}',i+1);}catch(e){}}}}
function fire(n,type,bub){if(!n)return;var b=n.getBoundingClientRect(),ev,o={bubbles:bub,cancelable:true,composed:true,
view:window,clientX:b.left+b.width/2,clientY:b.top+b.height/2};
try{ev=(type.indexOf('pointer')===0&&window.PointerEvent)?new PointerEvent(type,Object.assign({pointerType:'mouse',
isPrimary:true,pointerId:1},o)):new MouseEvent(type,o);}catch(e){ev=new MouseEvent(type,o);}n.dispatchEvent(ev);}
function chain(el){var c=[];for(var n=el;n&&n.nodeType===1;n=n.parentElement)c.push(n);return c;}
function point(el){var next=chain(el),prev=hovered;
prev.forEach(function(n){if(next.indexOf(n)<0)n.classList.remove(HOVER);});
if(prev[0]&&prev[0]!==el){fire(prev[0],'pointerout',true);fire(prev[0],'mouseout',true);}
prev.forEach(function(n){if(next.indexOf(n)<0){fire(n,'pointerleave',false);fire(n,'mouseleave',false);}});
next.forEach(function(n){n.classList.add(HOVER);});
if(el&&prev[0]!==el){fire(el,'pointerover',true);fire(el,'mouseover',true);}
next.slice().reverse().forEach(function(n){if(prev.indexOf(n)<0){fire(n,'pointerenter',false);fire(n,'mouseenter',false);}});
if(el){fire(el,'pointermove',true);fire(el,'mousemove',true);}hovered=next;}
function press(el){if(!el)return;point(el);fire(el,'pointerdown',true);fire(el,'mousedown',true);
try{el.focus({preventScroll:true});}catch(e){}fire(el,'pointerup',true);fire(el,'mouseup',true);
if(typeof el.click==='function')el.click();else fire(el,'click',true);}
function key(k){var t=document.activeElement||document.body;['keydown','keyup'].forEach(function(ty){
t.dispatchEvent(new KeyboardEvent(ty,{key:k,code:k,bubbles:true,cancelable:true}));});}
function far(avoid){var pts=[[4,innerHeight-4],[innerWidth-4,innerHeight-4],[4,4],[innerWidth-4,4],[innerWidth/2,innerHeight-4]];
for(var i=0;i<pts.length;i++){var e=document.elementFromPoint(pts[i][0],pts[i][1]);
if(e&&!e.closest(INERT)&&!avoid.some(function(a){return a&&(a===e||a.contains(e));}))return e;}return document.body;}
function attrs(el){var o={};if(!el)return o;Array.prototype.forEach.call(el.attributes,function(a){
if(!SKIP.test(a.name))o[a.name]=a.value;});(el.getAttribute('class')||'').split(/\\s+/).forEach(function(c){
if(c&&c!==HOVER)o['class.'+c]='on';});return o;}
function style(el){if(!el)return null;var s=getComputedStyle(el),lab=el.querySelector?el.querySelector('span'):null;
return {bg:s.backgroundColor,bgi:(s.backgroundImage||'').slice(0,120),color:s.color,label:lab?getComputedStyle(lab).color:s.color,
filter:s.filter,transform:s.transform,shadow:s.boxShadow,opacity:s.opacity,border:s.borderTopColor};}
function items(el){var c=Array.prototype.filter.call(el.querySelectorAll(ISEL),function(n){return ((n.textContent||'')+(n.value||'')).trim();});
c=c.filter(function(n){return !c.some(function(m){return m!==n&&n.contains(m);});});
if(!c.length)c=Array.prototype.filter.call(el.querySelectorAll('*'),function(n){return !n.children.length&&(n.textContent||'').trim();});
if(!c.length&&(el.textContent||'').trim())c=[el];return c;}
function lum(c){var m=(c||'').match(/rgba?\\(([^)]+)\\)/);if(!m)return null;var p=m[1].split(',').map(parseFloat);
return {v:[p[0],p[1],p[2]].map(function(x){x/=255;return x<=0.03928?x/12.92:Math.pow((x+0.055)/1.055,2.4);}),a:p.length>3?p[3]:1};}
function L(x){return 0.2126*x.v[0]+0.7152*x.v[1]+0.0722*x.v[2];}
function contrast(item,box){var fg=lum(getComputedStyle(item).color);if(!fg)return null;
for(var n=item;n&&n.nodeType===1;n=n.parentElement){var s=getComputedStyle(n),bg=lum(s.backgroundColor);
if(s.backgroundImage&&s.backgroundImage!=='none')return null;
if(bg&&bg.a>=0.85){var a=L(fg),b=L(bg);return r1((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05));}
if(bg&&bg.a>0.05)return null;if(n===box)break;}return null;}
function vis(el){if(!el)return {exists:false};var b=el.getBoundingClientRect(),op=1,shown=true,n,s;
for(n=el;n&&n.nodeType===1;n=n.parentElement){s=getComputedStyle(n);if(s.display==='none')shown=false;op*=parseFloat(s.opacity||'1');}
s=getComputedStyle(el);if(s.visibility!=='visible')shown=false;
var l=b.left,t=b.top,rr=b.right,bo=b.bottom,clip=null;
for(n=el.parentElement;n&&n!==document.documentElement;n=n.parentElement){var c=getComputedStyle(n);
if(c.overflowX!=='visible'||c.overflowY!=='visible'){var p=n.getBoundingClientRect();
if(!clip&&(b.left<p.left-0.5||b.top<p.top-0.5||b.right>p.right+0.5||b.bottom>p.bottom+0.5))clip=n.getAttribute('data-ae-id')||n.tagName.toLowerCase();
l=Math.max(l,p.left);t=Math.max(t,p.top);rr=Math.min(rr,p.right);bo=Math.min(bo,p.bottom);}}
if(!clip&&(b.left<-0.5||b.top<-0.5||b.right>innerWidth+0.5||b.bottom>innerHeight+0.5))clip='the window';
l=Math.max(l,0);t=Math.max(t,0);rr=Math.min(rr,innerWidth);bo=Math.min(bo,innerHeight);
var area=b.width*b.height,va=Math.max(0,rr-l)*Math.max(0,bo-t),top=va>0?document.elementFromPoint((l+rr)/2,(t+bo)/2):null,
mine=!!(top&&(top===el||el.contains(top))),own=top&&top.closest?top.closest('[data-ae-id]'):null,it=shown?items(el):[];
return {exists:true,shown:shown,op:Math.round(op*100)/100,x:r1(b.left),y:r1(b.top),w:r1(b.width),h:r1(b.height),
frac:area?Math.round(va/area*1000)/1000:0,clip:clip,onTop:mine,cover:(!mine&&own)?own.getAttribute('data-ae-id'):null,
pe:s.pointerEvents,bg:s.backgroundColor,items:it.map(function(x){return ((x.textContent||'')+(x.value||'')).replace(/\\s+/g,' ').trim().slice(0,40);}).slice(0,12),
contrast:it.length?contrast(it[0],el):null};}
function hid(v){return !v||!v.exists||!v.shown||v.op<=0.05||v.w*v.h===0||v.frac<0.01;}
function opn(v){return !!v&&v.exists&&v.shown&&v.op>=0.9&&v.w*v.h>0&&v.frac>0.01;}
async function run(){try{
for(var i=0;i<document.styleSheets.length;i++){try{emulate(document.styleSheets[i].cssRules,document.styleSheets[i]);}catch(e){}}
var t=q(SPEC.trigger),p=q(SPEC.pop),on=SPEC.on;out.trigger=!!t;if(!t)return;
point(far([t,p]));await wait(250);settle();out.rest=vis(p);out.restStyle=style(t);var a0=attrs(t);
if(on==='hover'){point(t);await wait(700);settle();out.actStyle=style(t);out.open=vis(p);
 if(p&&opn(out.open)){var tb=t.getBoundingClientRect(),pb=p.getBoundingClientRect(),
  gx=Math.max(pb.left-tb.right,tb.left-pb.right,0),gy=Math.max(pb.top-tb.bottom,tb.top-pb.bottom,0);out.gap=r1(Math.max(gx,gy));
  if(gx>1||gy>1){var mx=gx>1?(pb.left>=tb.right?(tb.right+pb.left)/2:(pb.right+tb.left)/2):(Math.max(tb.left,pb.left)+Math.min(tb.right,pb.right))/2,
   my=gy>1?(pb.top>=tb.bottom?(tb.bottom+pb.top)/2:(pb.bottom+tb.top)/2):(Math.max(tb.top,pb.top)+Math.min(tb.bottom,pb.bottom))/2;
   point(document.elementFromPoint(mx,my)||document.body);await wait(60);}
  var c=p.getBoundingClientRect(),h=document.elementFromPoint(c.left+c.width/2,c.top+c.height/2);
  out.reachable=!!(h&&(h===p||p.contains(h)));if(out.reachable){point(h);await wait(700);settle();out.stay=vis(p);}}
 point(far([t,p]));await wait(900);settle();out.closed=vis(p);
 press(t);await wait(400);settle();out.clickAttrs=[a0,attrs(t)];point(far([t,p]));
}else if(on==='click'){press(t);await wait(450);settle();out.actStyle=style(t);out.open=vis(p);out.clickAttrs=[a0,attrs(t)];
 if(p){key('Escape');await wait(450);settle();out.esc=vis(p);
  if(hid(out.esc)){press(t);await wait(450);settle();}
  var f=far([t,p]);out.beforeOutside=vis(p);press(f);await wait(450);settle();out.outside=vis(p);
  if(hid(out.outside)){press(t);await wait(450);settle();}
  out.beforeToggle=vis(p);press(t);await wait(450);settle();out.toggle=vis(p);}
}else if(on==='focus'){try{t.focus({preventScroll:true});}catch(e){}await wait(450);settle();out.actStyle=style(t);out.open=vis(p);
 if(p){try{t.blur();}catch(e){}await wait(450);settle();out.closed=vis(p);}}
}catch(e){out.errors.push(String((e&&e.message)||e));}
finally{var d=document.createElement('script');d.type='application/json';d.id='__ae_interact';
d.textContent=JSON.stringify(out);document.body.appendChild(d);}}
setTimeout(run,400);})();</script>"""

FAMILIES = ("black", "white", "grey", "red", "orange", "yellow", "green", "teal", "blue",
            "purple", "pink")
ALIASES = {"gray": "grey", "cyan": "teal", "violet": "purple", "magenta": "pink", "brown": "orange"}
COLOUR_WORDS = r"\b(orange|black|blue|red|green|purple|pink|yellow|white|grey|gray|teal|cyan|violet|magenta|brown)\b"

UNSAFE = [
    (r"\bfetch\s*\(", "fetch"), (r"XMLHttpRequest", "XMLHttpRequest"), (r"WebSocket", "WebSocket"),
    (r"EventSource", "EventSource"), (r"sendBeacon", "sendBeacon"), (r"\bimport\s*\(", "import()"),
    (r"\beval\s*\(", "eval"), (r"new\s+Function", "new Function"), (r"document\.cookie", "document.cookie"),
    (r"localStorage|sessionStorage|indexedDB", "browser storage"), (r"window\.open", "window.open"),
    (r"\blocation\s*(\.href|\.replace|\.assign|=[^=])", "navigation"), (r"document\.write", "document.write"),
    (r"<iframe", "an iframe"), (r"<script[^>]*\bsrc\s*=", "an external script"), (r"javascript:", "a javascript: URL"),
    (r"https?://", "a network URL"), (r"(src|href|action)\s*=\s*['\"]?//", "a network URL"),
    (r"url\(\s*['\"]?//", "a network URL"), (r"@import", "@import"),
]

# WHAT A REQUEST IS ABOUT decides what the claims must measure. Deliberately narrow: a rule
# that demands a claim the request never implied sends a correct change back forever.
TYPING = (r"\b(typing|typewriter|auto-?typ\w*)\b|\bautomatic\w*\s+typ\w*|"
          r"\btyp(e|es)\b[^.]{0,40}\bautomatic\w*")
INTENT = [
    ("a size claim", ("size",),
     r"\b(height|tall|taller|short|shorter|width|wide|wider|narrow|narrower|bigger|smaller|larger|"
     r"shrink|grow|enlarge|resize)\b", None),
    ("a position claim", ("position",), r"\b(move|shift|nudge|drag)\b",
     r"\b(left|right|up|upwards|down|downwards|higher|lower|px|pixels?|cent(er|re)|top|bottom)\b"),
    ("a removed claim", ("removed",),
     r"\b(remove|delete|get rid of|take out|hide)\b(?!\s+(the\s+)?(\w+\s+)?(animation|motion|movement|blur|"
     r"shadow|glow|gradient|border|outline|underline|effect)s?\b)", None),
    ("a colour claim", ("color", "colors", "style_on", "appears_on"), r"\bcolou?rs?\b|" + COLOUR_WORDS, None),
    ("a changes_over_time claim", ("changes_over_time",), TYPING, None),
    # Something that TURNS is not something that types. A spinner has no words, so the typing
    # claim can only ever report "nothing typed" about it — which is true, and about the wrong
    # thing. Motion words ask for a moves claim, which watches the element instead of reading it.
    ("a moves claim", ("moves",),
     r"\b(spin\w*|spinner|loader|loading|rotat\w*|whirl\w*|pulse|pulsing|blink\w*|bounc\w*|"
     r"wobbl\w*|orbit\w*|swirl\w*)\b", None),
    ("an added claim", ("added",),
     r"\badd\s+(a|an|another|one more|some)?\s*(new\s+)?(\w+\s+)?(button|link|card|section|chip|pill|"
     r"heading|paragraph|image|logo|input|field|label|badge|item)s?\b", None),
    ("a text claim", ("text",),
     r"\b(rename|reword|wording)\b|\bto (say|read)\b|\bchange the (name|text|title|headline|heading|label|"
     r"words|copy|placeholder)\b(?!\s+(colou?r|size|font|weight|style))", None),
]
SHRINK = r"\b(reduce|decrease|smaller|shorter|narrower|shrink|thinner|less (tall|wide))\b"
GROW = r"\b(increase|bigger|taller|wider|larger|grow|enlarge|expand)\b"
HEIGHT_W = r"\b(height|tall|taller|short|shorter)\b"
WIDTH_W = r"\b(width|wide|wider|narrow|narrower)\b"
LITTLE = r"\b(a little|a bit|slightly|a touch|a tad)\b"
STOP_MOTION = (r"\b(stop|freeze|static|motionless|no (more )?(animation|motion|movement)|"
               r"(keep|make) (it|the background) still|remove the (\w+ )?(animation|motion|movement)|"
               r"don'?t (animate|move))\b")
INTERACT_KINDS = ("appears_on", "style_on")
POPUP = (r"\b(pop-?\s?ups?|pops?\s+up|popovers?|drop-?\s?downs?|tooltips?|flyouts?|"
         r"(a|small|little|context)\s+menu)\b")
HOVER_W = r"\b(hover\w*|mouse\s*over\w*|mouseover)\b"
CLICK_W = r"\b(click\w*|tap\w*|press\w*)\b"
FOCUS_W = r"\b(focus\w*|keyboard)\b"
OPTIONS_W = r"\b(options|choices|items|links|menu|list)\b"
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10}
# "the Mac OS button or the iOS button" leaves the choice to the model; "hover or click" does not.
CHOICE = r"\beither\b|\b(button|chip|pill|link|tab)s?\s*,?\s+or\b"
VERB = {"hover": "hovering", "click": "clicking", "focus": "focusing"}

CODE_PROMPT = """You are changing a web page that Aethron rebuilt from a screenshot and measured.
Make exactly the change the user asked for by editing its code, and nothing else.

You are given the user's request, the page's elements with their ids and MEASURED boxes
(x, y, w, h in pixels, read from a browser) and what each one holds, and the full source.

Return ONLY JSON, no markdown fences:
{"patches": [{"find": "<text copied exactly from the source>", "replace": "<new text>", "count": 1}],
 "css": "<optional CSS to add>", "js": "<optional JavaScript to add>",
 "touches": ["<id of every element whose size, position, words, colours or styles you change>"],
 "expect": [<claims>],
 "note": "<one line: what you did>"}

RULES
* "find" is copied character for character. "count" is how many times it appears in the
  source; the patch lands only if that number is exact, and then replaces every one.
  Patches apply in order, each to the result of the one before.
* Keep every data-ae-id. A new element needs a new, unique data-ae-id.
* Scripts may not use fetch, XMLHttpRequest, WebSocket, import(), eval, new Function,
  cookies, storage, window.open, navigation, iframes, or any network URL.
* Anything you do not list in "touches" must measure exactly as before. Elements sitting ON
  an element you change may move with it (listed under "holds"), but must stay on it.
* Change only what was asked. If only the height was asked for, the width stays.
  "A little" means roughly 8-12%.
* If the background moves, its gradient is written TWICE — in the .page rule and in the
  <style data-ae-alive> block. A colour change must be made in both, identically, and the
  background must keep moving unless the user asked it to stop.
* Positions are absolute pixels on the canvas. If a box gets shorter, move whatever sits
  near its bottom edge up so it stays inside.
* Anything that OPENS (pop-up, dropdown, tooltip, menu) is a NEW element with its own
  data-ae-id, placed as a SIBLING of its trigger — never a button or link inside a button.
  While closed it must be display:none or visibility:hidden, so it never covers, or swallows
  clicks meant for, what is under it. The page clips everything outside the bg element's box.
* Elements carry their colours and sizes in INLINE style attributes, and an inline declaration
  outranks any stylesheet rule: a :hover or state rule that changes one of them needs !important.
* An element you only attach behaviour to (a trigger) must look and behave exactly as before
  at rest, on hover and on click: keep its existing handlers working (no stopPropagation or
  preventDefault on its click) and do not restyle it unless a style_on claim says so.

CLAIMS — each one is MEASURED in a browser; a claim that does not hold undoes your change
  {"kind": "size", "id": "s00", "dimension": "height" | "width", "change": "-10%" | "-24px"}
  {"kind": "position", "id": "s01", "axis": "x" | "y", "change": "-40px"}
  {"kind": "text", "id": "t05", "equals": "the exact new words"}
  {"kind": "color", "id": "s01", "property": "background" | "text", "equals": "#RRGGBB"}
  {"kind": "colors", "region": "background" | "<id>", "families": ["orange", "black"],
   "min_share": 0.6}   families: black white grey red orange yellow green teal blue purple pink
                       (each family must cover at least 8% of the region; together at least 60%;
                        white, grey or black tones a recolour keeps may be added to the families)
  {"kind": "removed", "id": "s04"}
  {"kind": "added", "id": "<the new data-ae-id>"}
  {"kind": "changes_over_time", "id": "t08"}   (its words change as time passes)
  {"kind": "appears_on", "id": "<new data-ae-id>", "trigger": "s04", "on": "hover" | "click" | "focus",
   "items": 3, "background": "#FFFFFF"}   ("background" is optional)
     Aethron USES the page like a person and measures: hidden before anyone acts; opens on that
     action fully inside the page, on top of everything, at most 24px from its trigger, holding
     exactly "items" readable options (contrast 4.5:1); on hover it stays open while the pointer
     moves onto it and closes when the pointer leaves; on click, Escape, a click outside or a
     second click closes it. One claim per trigger per action: hover AND click on four buttons
     is eight claims.
  {"kind": "style_on", "id": "s01", "on": "hover" | "click" | "focus",
   "property": "background" | "text" | "border" | "opacity", "equals": "#RRGGBB" | 0.8}
Give a claim for every part of the request, and make the code do EXACTLY what you claim:
if a 244px box should lose 10%, claim -10% and set it to 219.6px.
"""


# ───────────────────────────── measuring ─────────────────────────────

_PROBES = {}
VIEWPORT_SLACK = 200

# A PAGE THAT LIVES BESIDE ITS ASSETS. Every render here writes a copy of the page into a
# working directory and points Chrome at it — which silently breaks `./css/site.css` and
# every relative image on a REAL page, so the whole measurement would be taken of an
# unstyled document without a word of warning. Set this to the page's own directory and a
# single <base> tag makes every relative reference resolve the way it does for a visitor.
# Nothing is written into the user's project to achieve it.
ASSET_BASE = None


# AND A <base> CANNOT SAVE A ROOT-ABSOLUTE PATH. Measured on a real Webflow migration: its
# stylesheets are `/assets/r/0586d92038e6.css`, and under file:// a leading slash means the
# FILESYSTEM ROOT, so every render was of an unstyled document — the nav stacked full-width
# in default link blue — and the whole measurement was taken of a page no visitor will ever
# see. The only honest way to render such a page is to SERVE it, which is what a visitor's
# browser does. Nothing is left behind in the user's project: the temp file is deleted.
ASSET_ORIGIN = None      # "http://127.0.0.1:PORT" when the page's folder is being served
_SERVED = {"srv": None, "dir": None}


def set_asset_base(page_dir):
    """Point relative assets at the directory the page really lives in. Returns the previous
    value so a caller can put it back."""
    global ASSET_BASE
    was = ASSET_BASE
    ASSET_BASE = (Path(page_dir).resolve().as_uri().rstrip("/") + "/") if page_dir else None
    return was


def serve_assets(page_dir):
    """Serve the page's own folder so that `/assets/...` means what it means to a visitor.
    Idempotent per directory; returns the origin, or None if it could not be served."""
    global ASSET_ORIGIN
    if page_dir is None:
        stop_serving()
        return None
    d = Path(page_dir).resolve()
    if _SERVED["dir"] == d and _SERVED["srv"] is not None:
        return ASSET_ORIGIN
    stop_serving()
    try:
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  functools.partial(Quiet, directory=str(d)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _SERVED["srv"], _SERVED["dir"] = srv, d
        ASSET_ORIGIN = f"http://127.0.0.1:{srv.server_address[1]}"
        return ASSET_ORIGIN
    except Exception:
        ASSET_ORIGIN = None
        return None


def stop_serving():
    global ASSET_ORIGIN
    if _SERVED["srv"] is not None:
        try:
            _SERVED["srv"].shutdown()
            _SERVED["srv"].server_close()
        except Exception:
            pass
    _SERVED["srv"], _SERVED["dir"], ASSET_ORIGIN = None, None, None


def _place(page, name):
    """Where should this rendering live, and what URL reaches it? Inside the served folder
    when one is up (so root-absolute paths resolve), otherwise the working directory."""
    if ASSET_ORIGIN and _SERVED["dir"] is not None:
        f = _SERVED["dir"] / f"__ae_{name}.html"
        f.write_text(page)
        return f, f"{ASSET_ORIGIN}/__ae_{name}.html", True
    return None, None, False


def _with_base(page):
    if not ASSET_BASE or re.search(r"<base\b", page, re.I):
        return page
    tag = f'<base href="{ASSET_BASE}">'
    m = re.search(r"<head\b[^>]*>", page, re.I)
    if m:
        return page[:m.end()] + tag + page[m.end():]
    m = re.search(r"<html\b[^>]*>", page, re.I)
    if m:
        return page[:m.end()] + "<head>" + tag + "</head>" + page[m.end():]
    return tag + page


SETTLE_QUIET = 400      # virtual ms of no DOM change that counts as "it has stopped arriving"
SETTLE_CAP = 8000       # and a page that never settles is read anyway, rather than hanging


def _probe_once(html, workdir, w, h, at_ms):
    page = html.replace("<head>", "<head>" + PROBE_HEAD, 1) if "<head>" in html else PROBE_HEAD + html
    tail = (PROBE_TAIL.replace("__AT__", str(int(at_ms)))
            .replace("__QUIET__", str(SETTLE_QUIET))
            .replace("__CAP__", str(SETTLE_CAP)))
    page = page.replace("</body>", tail + "</body>", 1) if "</body>" in page else page + tail
    # The virtual clock has to outlast the settle window, or the page is cut off mid-wait and
    # the reading never happens at all.
    return _dump(page, workdir, w, h, int(at_ms) + SETTLE_CAP + 2000,
                 f"probe_{int(at_ms)}", "__ae_probe")


def timeline(html, workdir, w, h, ids, user_at_ms=20000, until_ms=26000, every_ms=25):
    """The words each element shows, followed in ONE render: a sample every `every_ms` for
    `user_at_ms` of page time. Then a PERSON types into any text field among them, and at
    `until_ms` Aethron reads whether their words are still there."""
    tail = (TIMELINE_TAIL.replace("__IDS__", json.dumps(list(ids))).replace("__EVERY__", str(int(every_ms)))
            .replace("__USER_AT__", str(int(user_at_ms))).replace("__UNTIL__", str(int(until_ms))))
    page = html.replace("</body>", tail + "</body>", 1) if "</body>" in html else html + tail
    return _dump(page, workdir, w, h, int(until_ms) + 1500, "timeline", "__ae_timeline")


# SOMETHING THAT MOVES IS WATCHED, NOT ASKED WHETHER IT MOVED. Measured 2026-09-15: asked for a
# spinner inside Generate "for about 2 seconds", the model kept claiming changes_over_time — the
# TYPING claim — and was refused with "0 phrases typed letter by letter", which is true and about
# the wrong thing. A spinner has no words. So: act, then sample what the element LOOKS like every
# 50ms and judge from the samples whether it moved, and whether it stopped when it was told to.
MOVES_TAIL = """<script>(function(){var S=__SPEC__,out={samples:[],acted:false,error:null};
function node(i){return document.querySelector('[data-ae-id="'+i+'"]');}
function fp(el){if(!el)return "gone";var c=getComputedStyle(el),r=el.getBoundingClientRect();
var an=(el.getAnimations?el.getAnimations():[]).map(function(a){
var t=(a.effect&&a.effect.getComputedTiming)?a.effect.getComputedTiming():{};
var props='';try{props=(a.effect.getKeyframes()||[]).map(function(f){return Object.keys(f).filter(function(x){
return ['offset','easing','composite','computedOffset'].indexOf(x)<0;}).join('+');}).join('/');}catch(e){}
return (a.animationName||a.transitionProperty||'anim')+':'+a.playState+':'+Math.round(t.duration||0)+':'+props;}).sort().join(',');
return [c.transform,c.opacity,c.visibility,c.display,Math.round(r.left*10)/10,Math.round(r.top*10)/10,
Math.round(r.width*10)/10,Math.round(r.height*10)/10,c.backgroundColor,c.borderTopColor,c.backgroundImage.slice(0,40),
(el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,40),an].join('|');}
function finish(){var d=document.createElement('script');d.type='application/json';d.id='__ae_moves';
d.textContent=JSON.stringify(out);document.body.appendChild(d);}
setTimeout(function(){
  try{var t=S.trigger?node(S.trigger):null;
    if(t){var r=t.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,
      ev=function(k){t.dispatchEvent(new MouseEvent(k,{bubbles:true,clientX:x,clientY:y}));};
      ['pointerover','pointerenter','mouseover','mouseenter','pointermove','mousemove'].forEach(ev);
      if(S.on!=='hover'){['pointerdown','mousedown','pointerup','mouseup','click'].forEach(ev);}
      out.acted=true;}
  }catch(e){out.error=String(e);}
  var start=performance.now();
  var iv=setInterval(function(){var el=node(S.id);
    out.samples.push([Math.round(performance.now()-start),fp(el)]);
    if(performance.now()-start>=S.watch_ms){clearInterval(iv);finish();}},50);
},500);})();</script>"""

_MOVES = {}


def moves(html, workdir, w, h, spec):
    """Act, then watch one element for `watch_ms` of page time: what it looked like, every 50ms."""
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h, json.dumps(spec, sort_keys=True))
    if key in _MOVES:
        return json.loads(_MOVES[key])
    tail = MOVES_TAIL.replace("__SPEC__", json.dumps(spec))
    page = html.replace("</body>", tail + "</body>", 1) if "</body>" in html else html + tail
    got = _dump(page, workdir, w, h, int(spec.get("watch_ms", 5000)) + 2500, "moves", "__ae_moves")
    if got is not None:
        _MOVES[key] = json.dumps(got)
    return got


def judge_moves(c, res):
    """Did it move — and, if the words gave it a length, did it stop? Returns (ok, the measurement)."""
    eid = c.get("id")
    if not res:
        return False, f"moves claim on {eid}: the page could not be watched"
    if res.get("error"):
        return False, f"moves claim on {eid}: the page threw while acting: {res['error']}"
    s = [(int(t), f) for t, f in (res.get("samples") or [])]
    if len(s) < 5:
        return False, f"moves claim on {eid}: it could not be watched ({len(s)} readings)"
    if all(f == "gone" for _, f in s):
        return False, f"{eid} never appeared, so nothing of it could move"
    # WHY it did not move matters more than THAT it did not: an element held at display:none for
    # the whole watch was never on screen, and the fix for that is not "animate harder".
    def hidden(f):
        p = f.split("|")
        return len(p) > 7 and (p[3] == "none" or p[2] == "hidden" or p[1] == "0"
                               or (float(p[6] or 0) == 0 and float(p[7] or 0) == 0))
    if all(hidden(f) for _, f in s if f != "gone"):
        return False, (f"{eid} was never visible during the {s[-1][0]}ms it was watched — it stayed "
                       f"hidden (display, visibility or zero size), so nobody saw it move"
                       + ("" if res.get("acted") else "; nothing was clicked"))
    # A CLOCK THAT DOES NOT TICK IS NOT A PAGE THAT DOES NOT MOVE. Under the virtual clock these
    # renders use, a CSS animation's transform never advances — every sample of a spinning ring
    # reads matrix(1,0,0,1,0,0). So the browser is ASKED what is animating on the element, the
    # same lesson the entrance recorder learned: read the animations, do not watch the pixels.
    # Motion driven by timers instead (setInterval moving a style) still shows up as samples that
    # differ, because virtual time advances timers.
    def spinning(f):
        return ":running:" in f          # name:playState:duration, recorded per sample
    turning = [t for t, f in s if spinning(f)]
    moved = sorted({t for (t, f), (_, prev) in zip(s[1:], s) if f != prev} | set(turning))
    end = s[-1][0]
    want = c.get("for_ms")
    if not moved:
        return False, (f"{eid} did not move at all: {len(s)} readings over {end}ms, every one identical"
                       + (" (nothing was clicked)" if not res.get("acted") else ""))
    wrong_kind = _moves_kind(c, s)      # before the length: fading for 2s is not spinning for 2s
    if wrong_kind:
        return False, wrong_kind
    if want:
        want = int(want)
        during = [t for t in moved if t <= want * 1.25 + 250]
        after = [t for t in moved if t > want + 800]
        if len(during) < 3:
            return False, (f"{eid} barely moved while it should have: {len(during)} change(s) in the "
                           f"first {want}ms (it changed {len(moved)} times in {end}ms overall)")
        if after:
            return False, (f"{eid} was asked to move for about {want}ms and was still moving at "
                           f"{after[-1]}ms — it never stopped ({len(after)} changes after "
                           f"{want + 800}ms)")
        return True, (f"{eid} moved {len(during)} times within {want}ms and was still by "
                      f"{want + 800}ms — watched to {end}ms")
    late = [t for t in moved if t > end * 0.66]
    if not late:
        return False, (f"{eid} moved {len(moved)} times but stopped after {moved[-1]}ms of {end}ms — "
                       f"the request asks for motion that keeps going")
    return True, f"{eid} kept moving: {len(moved)} changes across {end}ms, {len(late)} of them in the last third"


def _moves_kind(c, s):
    """Is it the KIND of motion the words asked for? Checked before the length, because a loader
    that fades when a spin was asked for is wrong however long it does it."""
    want_prop = str(c.get("property") or "").lower()
    if want_prop:
        # "spin slowly" is not just motion: it is motion of a particular kind. The keyframes say
        # which properties actually animate, so a claim may name one and be held to it.
        alias = {"rotate": ("rotate", "transform"), "spin": ("rotate", "transform"),
                 "fade": ("opacity",), "slide": ("translate", "transform", "left", "top")}.get(
                     want_prop, (want_prop,))
        by_anim = any(any(p in f.lower() for p in alias) for _, f in s if ":running:" in f)
        by_style = len({f.split("|")[0] for _, f in s if f != "gone"}) > 1 and "transform" in alias
        if not (by_anim or by_style):
            return (f"{c.get('id')} changes, but nothing in it animates {want_prop} — the request "
                    f"asks for that kind of motion, and the keyframes do not carry it")
    return None


_INTERACTS = {}


def interact(html, workdir, w, h, spec, budget_ms=9000):
    """Use the page like a person — hover, carry the pointer onto what opened, leave, click,
    Escape, click outside — and read what that person would see after each step. One render
    per spec, so nothing left open by one test leaks into the next."""
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h, json.dumps(spec, sort_keys=True))
    if key in _INTERACTS:
        return json.loads(_INTERACTS[key])
    tail = INTERACT_TAIL.replace("__SPEC__", json.dumps(spec))
    page = html.replace("</body>", tail + "</body>", 1) if "</body>" in html else html + tail
    got = _dump(page, workdir, w, h, budget_ms, "interact", "__ae_interact")
    if got is not None:
        _INTERACTS[key] = json.dumps(got)
    return got


def _dump(page, workdir, w, h, budget_ms, name, marker):
    import aethron_figma_grade as GR
    b = GR.find_browser()
    if not b:
        return None
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    served, url, is_served = _place(page, f"dump_{name}")
    if is_served:
        f = served
    else:
        f = work / f"_{name}.html"
        f.write_text(_with_base(page))
        url = f.resolve().as_uri()
    prof = tempfile.mkdtemp(prefix="ae-probe-")
    # MEASURED 2026-09-15: headless --dump-dom gives a viewport 87px SHORTER than the window it
    # was asked for (900x560 -> 473 tall, 1414x858 -> 771). Everything in the bottom strip of
    # a page was outside the viewport — unhittable, and "cut off by the window". The window is
    # opened taller than the canvas so the whole page is really in view.
    proc = subprocess.Popen(
        [b, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--user-data-dir={prof}", f"--window-size={w},{h + VIEWPORT_SLACK}",
         f"--virtual-time-budget={int(budget_ms)}", "--dump-dom", url],
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
    done.wait(max(90, budget_ms / 1000 * 4))
    try:
        import os
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    shutil.rmtree(prof, ignore_errors=True)
    try:
        f.unlink()
    except OSError:
        pass
    m = re.search(r'<script type="application/json" id="' + marker + r'">(.*?)</script>', "".join(dom), re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def probe(html, workdir, w, h, at_ms=1200):
    """Every element's REAL box, words, colours and styles, read from the browser at `at_ms`
    of page time (virtual time, so scripts and timers are deterministic)."""
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h, int(at_ms), ASSET_BASE, ASSET_ORIGIN, str(workdir))
    if key in _PROBES:
        return json.loads(_PROBES[key])
    got = None
    for _ in range(2):
        got = _probe_once(html, workdir, w, h, at_ms)
        if got is None or got.get("fonts") in ("loaded", ""):
            break
    if got and got.get("fonts") in ("loaded", ""):
        _PROBES[key] = json.dumps(got)
    return got


def family(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    h *= 360
    if v < 0.16:
        return "black"
    if s < 0.14:
        return "white" if v > 0.86 else "grey"
    if h < 10 or h >= 345:
        return "red"
    if h < 48:
        return "orange"
    if h < 70:
        return "yellow"
    if h < 170:
        return "green"
    if h < 200:
        return "teal"
    if h < 255:
        return "blue"
    if h < 300:
        return "purple"
    return "pink"


def _box_colour(png, el, step=2):
    """The colour an element ACTUALLY SHOWS: the median of its interior. The median ignores its
    own label's glyphs and the antialiased rim, and a gradient laid over a fill reads as the
    gradient — which is the point."""
    import aethron_vision as V
    try:
        shot = V.load(png)
    except Exception:
        return None
    x0, x1 = int(el["x"] + el["w"] * 0.22), int(el["x"] + el["w"] * 0.78)
    y0, y1 = int(el["y"] + el["h"] * 0.25), int(el["y"] + el["h"] * 0.75)
    px = [shot.rgb(x, y)
          for y in range(max(0, y0), min(shot.h, max(y0 + 1, y1)), step)
          for x in range(max(0, x0), min(shot.w, max(x0 + 1, x1)), step)]
    if not px:
        return None
    return tuple(sorted(p[i] for p in px)[len(px) // 2] for i in range(3))


def shares(png, box=None, exclude=(), step=3):
    import aethron_vision as V
    shot = V.load(png)
    x0, y0, x1, y1 = (0, 0, shot.w, shot.h) if box is None else (
        max(0, int(box[0])), max(0, int(box[1])), min(shot.w, int(box[0] + box[2])), min(shot.h, int(box[1] + box[3])))
    counts, n = {}, 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            if any(ex <= x < ex + ew and ey <= y < ey + eh for ex, ey, ew, eh in exclude):
                continue
            fam = family(*shot.rgb(x, y))
            counts[fam] = counts.get(fam, 0) + 1
            n += 1
    return {k: v / n for k, v in counts.items()} if n else {}


def _rgb(value):
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value or "")
    if m:
        return tuple(int(g) for g in m.groups())
    return AE._rgb_of(value)


def _num(spec):
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(%|px)?\s*$", str(spec))
    return float(m.group(1)) if m else None


def _unquoted(text):
    return re.sub(r'"[^"]*"|“[^”]*”', " ", text or "").lower()


def describe(probed, html, eid):
    e = probed["els"].get(eid)
    if eid == "bg":
        return "the page background"
    if not e:
        return eid
    if e["text"]:
        return f"{eid} ('{e['text'][:40]}')"
    held = [probed["els"][r]["text"][:24] for r in AE.riders(html, eid)
            if r in probed["els"] and probed["els"][r]["text"]]
    return f"{eid} (holding {', '.join(repr(t) for t in held[:3])})" if held else eid


# ───────────────────────────── the plan ─────────────────────────────

def _parse_plan(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        plan = json.loads(t[a:b + 1])
    except ValueError:
        return None
    return plan if isinstance(plan, dict) else None


def _unsafe(plan):
    patches = [p for p in plan.get("patches") or [] if isinstance(p, dict)]
    finds = " ".join(str(p.get("find", "")) for p in patches)
    new = " ".join(str(p.get("replace", "")) for p in patches)
    new += " " + str(plan.get("css") or "") + " " + str(plan.get("js") or "")
    found = []
    for pat, name in UNSAFE:
        if len(re.findall(pat, new, re.I)) > len(re.findall(pat, finds, re.I)) and name not in found:
            found.append(name)
    return found


def apply_plan(html, plan):
    errors = []
    patches = plan.get("patches") or []
    if not isinstance(patches, list):
        return html, ["patches must be a list of find/replace pairs"]
    if not patches and not plan.get("css") and not plan.get("js"):
        return html, ["no change was proposed"]
    for i, p in enumerate(patches):
        if not isinstance(p, dict) or not isinstance(p.get("find"), str) or not isinstance(p.get("replace"), str):
            errors.append(f"patch {i + 1} is not a find/replace pair")
            continue
        want = p.get("count", 1)
        if isinstance(want, bool) or not isinstance(want, int) or want < 1:
            errors.append(f"patch {i + 1}: count must be a whole number of at least 1")
            continue
        n = html.count(p["find"]) if p["find"] else 0
        if n != want:
            errors.append(f"patch {i + 1}: its find text appears {n} time(s) in the source, "
                          f"but the patch says {want}")
            continue
        html = html.replace(p["find"], p["replace"])
    if plan.get("css"):
        html = html.replace("</head>", f"<style data-ae-code>{plan['css']}</style></head>", 1)
    if plan.get("js"):
        html = html.replace("</body>", f"<script data-ae-code>{plan['js']}</script></body>", 1)
    return html, errors


def intent_gaps(request, claims, pb=None):
    """Do the claims say what the REQUEST said? Checked before anything is rendered."""
    words = _unquoted(request)
    kinds = {c.get("kind") for c in claims if isinstance(c, dict)}
    typing = re.search(TYPING, words)
    popup = re.search(POPUP, words)
    gaps = []
    for name, need, pat, also in INTENT:
        if need == ("text",) and typing:
            continue
        # "a smaller box that hides when I move away" describes the pop-up, not a resize,
        # a removal or a move of anything already on the page.
        if popup and (need in (("size",), ("removed",)) or (need == ("position",) and re.search(
                r"\b(mouse|pointer|cursor|away)\b", words))):
            continue
        if re.search(pat, words) and (also is None or re.search(also, words)) and not kinds & set(need):
            gaps.append(f"the request asks for something your claims do not measure — add {name}")
    sizes = [c for c in claims if isinstance(c, dict) and c.get("kind") == "size"]
    if sizes:
        dims = {c.get("dimension") for c in sizes}
        if re.search(HEIGHT_W, words) and not re.search(WIDTH_W, words) and "height" not in dims:
            gaps.append("the request is about HEIGHT — the size claim must be on dimension height")
        if re.search(WIDTH_W, words) and not re.search(HEIGHT_W, words) and "width" not in dims:
            gaps.append("the request is about WIDTH — the size claim must be on dimension width")
        shrink, grow = bool(re.search(SHRINK, words)), bool(re.search(GROW, words))
        if shrink != grow:
            for c in sizes:
                v = _num(c.get("change"))
                if v is not None and (v < 0) != shrink:
                    gaps.append(f"the request asks to {'reduce' if shrink else 'increase'}, but the claim on "
                                f"{c.get('id')} says {c.get('change')}")
    for c in (c for c in claims if isinstance(c, dict) and c.get("kind") == "position"):
        v, axis = _num(c.get("change")), c.get("axis")
        wants = ((axis == "x" and re.search(r"\bleft\b", words) and not re.search(r"\bright\b", words), -1),
                 (axis == "x" and re.search(r"\bright\b", words) and not re.search(r"\bleft\b", words), 1),
                 (axis == "y" and re.search(r"\b(up|upwards|higher)\b", words), -1),
                 (axis == "y" and re.search(r"\b(down|downwards|lower)\b", words), 1))
        for hit, sign in wants:
            if hit and v is not None and v * sign <= 0:
                gaps.append(f"the request moves it the other way to the claim on {c.get('id')} ({c.get('change')})")
    named = {ALIASES.get(f, f) for f in re.findall(COLOUR_WORDS, words)}
    if named:
        claimed = set()
        for c in claims:
            if not isinstance(c, dict):
                continue
            if c.get("kind") == "colors":
                claimed |= {ALIASES.get(str(f), str(f)) for f in c.get("families") or []}
            value = (c.get("equals") if c.get("kind") == "color" or (
                c.get("kind") == "style_on" and c.get("property") in ("background", "text", "border"))
                else c.get("background") if c.get("kind") == "appears_on" else None)
            rgb = AE._rgb_of(str(value)) if value else None
            if rgb:
                claimed.add(family(*rgb))
        missing = sorted(named - claimed)
        if missing:
            gaps.append(f"the request names {', '.join(missing)}, which no colour claim measures")
    inter = [c for c in claims if isinstance(c, dict) and c.get("kind") in INTERACT_KINDS]
    pops = [c for c in inter if c.get("kind") == "appears_on"]
    if popup and not pops:
        gaps.append("the request asks for something that opens — add an appears_on claim for each trigger and "
                    "each action")
    for on, pat in (("hover", HOVER_W), ("click", CLICK_W), ("focus", FOCUS_W)):
        if not re.search(pat, words):
            continue
        pool = pops if popup else inter
        if (popup or on == "hover") and not any(c.get("on") == on for c in pool):
            gaps.append(f"the request says {on}, but no {'appears_on' if popup else 'style_on or appears_on'} "
                        f"claim acts on {on}")
    triggers = {c.get("trigger") if c.get("kind") == "appears_on" else c.get("id") for c in inter}
    if inter and not re.search(CHOICE, words):
        m = re.search(r"\b(?:all|each|every|both)\s+(?:of\s+)?(?:the\s+)?(two|three|four|five|six|seven|eight|nine|"
                      r"ten|\d+)\b", words)
        n = (NUMBER_WORDS.get(m.group(1)) or int(m.group(1))) if m else (2 if re.search(r"\bboth\b", words) else 0)
        if n and len(triggers) < n:
            gaps.append(f"the request covers {n} elements, but the claims act on {len(triggers)}: "
                        f"{', '.join(sorted(t for t in triggers if t)) or 'none'}")
        for i, e in ((pb or {}).get("els") or {}).items():
            toks = re.findall(r"[a-z0-9]+", (e.get("text") or "").lower())
            if e.get("tag") not in ("button", "a") or not toks or len(toks) > 4 or i in triggers:
                continue
            if re.search(r"\b" + r"\s*".join(map(re.escape, toks)) + r"\s+(button|chip|pill|link|tab)s?\b", words):
                gaps.append(f"the request names the '{e['text']}' {e['tag']}, but no claim acts on {i}")
    return gaps


CURSOR = " |_▌▍█▋▎▏"
DIFFERENT = r"\b(different|another|other|various|multiple|several|rotat\w*|cycl\w*)\b"
AGAIN = r"\b(again|loop\w*|repeat\w*|keeps?|continu\w*|forever|cycl\w*|one after another)\b"
CLEARS = r"\b(disappear\w*|delet\w*|eras\w*|clear\w*|backspac\w*|vanish\w*)\b"


def typing_story(seq, original=""):
    """What a typing animation DID, from the words it showed over time. A phrase is a text
    that grew letter by letter and then stopped growing; the page's own starting words are
    not a phrase."""
    steps = []
    for s in seq:
        s = (s or "").rstrip(CURSOR)
        if not steps or steps[-1] != s:
            steps.append(s)
    phrases, cleared = [], False
    for i, s in enumerate(steps):
        prev = steps[i - 1] if i else ""
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        # Cleared means the typed words are gone: an empty field, or one showing its own
        # placeholder again (an emptied textarea reads as its placeholder).
        if (not s.strip() or s == original) and phrases:
            cleared = True
        if s.strip() and s.startswith(prev) and (nxt is None or not nxt.startswith(s)) and s != original:
            j = i
            while j > 0 and s.startswith(steps[j - 1]) and len(steps[j]) - len(steps[j - 1]) <= 3:
                j -= 1
            phrases.append({"text": s, "typed": len(s) - len(steps[j]) >= min(4, len(s)) or not steps[j],
                            "cleared_before": cleared})
            cleared = False
    return steps, phrases


def _distinct(texts):
    out = []
    for t in texts:
        if not any(t.startswith(o) or o.startswith(t) for o in out):
            out.append(t)
    return out


def typing_verdict(eid, seq, original, request, seconds=20, kept=None, is_input=False):
    """Hold a typing animation to the WORDS of the request: typed letter by letter; different
    texts if different texts were asked; again if again; cleared between phrases if the text
    was meant to disappear."""
    steps, phrases = typing_story(seq, original)
    words = _unquoted(request)
    typed = [p for p in phrases if p["typed"]]
    kinds = _distinct([p["text"] for p in typed])
    story = " → ".join(("(cleared) " if p["cleared_before"] else "") + repr(p["text"][:36]) for p in typed[:5])
    msg = (f"{eid} followed for {seconds}s: {len(typed)} phrase(s) typed letter by letter, "
           f"{len(kinds)} different — {story or 'nothing typed'}")
    need = []
    if len(steps) < 2:
        need.append("its words never changed")
    if re.search(TYPING, words) and not typed:
        need.append("nothing was typed letter by letter")
    if re.search(DIFFERENT, words) and len(kinds) < 2:
        need.append("the request asks for different texts, but fewer than two different ones were typed")
    if re.search(AGAIN, words) and len(typed) < 2:
        need.append("the request asks for it to type again, but it typed only once")
    if re.search(CLEARS, words) and not any(p["cleared_before"] for p in typed[1:]):
        need.append("the request asks for the text to disappear before the next one, but it was never cleared "
                    "between phrases")
    if is_input and kept is False:
        need.append("a person typing into it loses their words — the animation overwrote them; animate the "
                    "placeholder, or stop while the field holds the person's text")
    elif is_input and kept is None:
        need.append("whether a person's own typing survives could not be measured — UNVERIFIED")
    elif is_input:
        msg += "; a person's own typing is kept"
    return not need, msg + ("" if not need else " — " + "; ".join(need))


def _shown(v):
    return bool(v) and bool(v.get("exists")) and bool(v.get("shown")) and float(v.get("op") or 0) >= 0.9 \
        and float(v.get("w") or 0) * float(v.get("h") or 0) > 0 and float(v.get("frac") or 0) > 0.01


def _closed(v):
    """Measured, and not showing. An unmeasured step is never counted as closed."""
    return bool(v) and bool(v.get("exists")) and (
        not v.get("shown") or float(v.get("op") or 0) <= 0.05
        or float(v.get("w") or 0) * float(v.get("h") or 0) == 0 or float(v.get("frac") or 0) < 0.01)


def kept_behaviour(trig, on, res, base, restyled=()):
    """The trigger must still do what it did: every attribute its click changed before, it
    changes the same way now; and while acted on it looks as it did (except what is claimed)."""
    if not base or not base.get("trigger"):
        return [f"what acting on {trig} did before the change could not be measured — UNVERIFIED"]
    need = []
    b0, b1 = (base.get("clickAttrs") or [{}, {}])[:2]
    a0, a1 = (res.get("clickAttrs") or [{}, {}])[:2]
    for k in sorted(set(b0) | set(b1)):
        if b0.get(k) != b1.get(k) and (a0.get(k) != b0.get(k) or a1.get(k) != b1.get(k)):
            need.append(f"clicking {trig} no longer does what it did: {k} went {b0.get(k)!r} -> {b1.get(k)!r} "
                        f"before, {a0.get(k)!r} -> {a1.get(k)!r} now")
    bs, as_ = base.get("actStyle") or {}, res.get("actStyle") or {}
    diff = [k for k in bs if k not in restyled and bs.get(k) != as_.get(k)]
    if diff:
        need.append(f"{VERB.get(on, on)} {trig} no longer looks as it did: "
                    + ", ".join(f"{k} {bs[k]} -> {as_.get(k)}" for k in diff[:3]))
    return need


def judge_popup(c, res, base, request, trigger_box=None):
    """Hold something that opens to what a person needs from it, measured step by step."""
    pop, trig, on = c.get("id"), c.get("trigger"), c.get("on")
    label = f"{pop} on {on} of {trig}"
    if on not in VERB:
        return False, f"{label}: 'on' must be hover, click or focus"
    if not res:
        return False, f"{label}: the page could not be used in a browser — UNVERIFIED"
    if not res.get("trigger"):
        return False, f"{label}: {trig} is not on the page"
    rest, opened = res.get("rest") or {}, res.get("open") or {}
    if not rest.get("exists"):
        return False, f"{label}: {pop} is not on the page"
    words = _unquoted(request)
    verb = VERB[on]
    need, seen = [], []
    if res.get("errors"):
        need.append("the page failed while being used: " + "; ".join(res["errors"][:2]))
    if not _closed(rest):
        need.append(f"{pop} already shows before anyone acts on {trig}")
    if not _shown(opened):
        need.append(f"{verb} {trig} does not open {pop}"
                    + (f" (it only reaches opacity {opened.get('op')})" if opened.get("shown") else ""))
    else:
        seen.append(f"{verb} {trig} opens {pop} at {opened['x']:.0f},{opened['y']:.0f}, "
                    f"{opened['w']:.0f}x{opened['h']:.0f}")
        if float(opened.get("frac") or 0) < 0.95:
            need.append(f"{pop} is cut off: {float(opened['frac']) * 100:.0f}% of it is visible"
                        + (f", clipped by {opened['clip']}" if opened.get("clip") else ""))
        if not opened.get("onTop"):
            cov = opened.get("cover")
            need.append(f"{pop} is covered by {cov} where a person would click it" if cov and cov != "bg"
                        else f"clicks go straight through {pop} (pointer-events: {opened.get('pe')})")
        if trigger_box:
            tx, ty, tw, th = trigger_box
            gap = max(opened["x"] - (tx + tw), tx - (opened["x"] + opened["w"]),
                      opened["y"] - (ty + th), ty - (opened["y"] + opened["h"]), 0)
            if gap > 24:
                need.append(f"{pop} opens {gap:.0f}px away from {trig} — it should sit next to it (24px at most)")
        items = opened.get("items") or []
        want = c.get("items")
        if not items:
            need.append(f"{pop} holds no options")
        elif isinstance(want, int) and not isinstance(want, bool) and len(items) != want:
            need.append(f"{pop} was claimed to hold {want} option(s) but holds {len(items)}: "
                        + ", ".join(repr(i) for i in items[:5]))
        elif re.search(OPTIONS_W, words) and len(items) < 2:
            need.append(f"options were asked for, but {pop} holds only one")
        if items:
            seen.append(f"{len(items)} option(s): " + ", ".join(repr(i) for i in items[:4]))
        con = opened.get("contrast")
        if isinstance(con, (int, float)) and con < 4.5:
            need.append(f"the options in {pop} are hard to read: contrast {con}:1, needs 4.5:1")
        if c.get("background"):
            want_bg, got_bg = AE._rgb_of(str(c["background"])), _rgb(opened.get("bg"))
            if not want_bg or not got_bg or sum(abs(a - b) for a, b in zip(want_bg, got_bg)) > 12:
                need.append(f"{pop} background measured {opened.get('bg')}, claimed {c['background']}")
        if on == "hover":
            if res.get("reachable") is False:
                need.append(f"the pointer cannot reach {pop}: it closes while crossing the {res.get('gap')}px gap "
                            f"from {trig}")
            elif not _shown(res.get("stay")):
                need.append(f"{pop} closes when the pointer moves onto it, so its options cannot be clicked")
            else:
                seen.append("stays open with the pointer on it")
        if on in ("hover", "focus"):
            if not _closed(res.get("closed")):
                need.append(f"{pop} stays open after the pointer leaves {trig}" if on == "hover"
                            else f"{pop} stays open after {trig} loses focus")
            else:
                seen.append("closes when the pointer leaves" if on == "hover" else "closes when focus leaves")
        if on == "click":
            closers = []
            if _closed(res.get("esc")):
                closers.append("Escape")
            if _shown(res.get("beforeOutside")) and _closed(res.get("outside")):
                closers.append("a click outside")
            if _shown(res.get("beforeToggle")) and _closed(res.get("toggle")):
                closers.append(f"clicking {trig} again")
            if not closers:
                need.append(f"once open, {pop} cannot be closed — not by Escape, a click outside, or clicking "
                            f"{trig} again")
            else:
                seen.append("closes with " + ", ".join(closers))
            if re.search(r"\boutside\b", words) and "a click outside" not in closers:
                need.append(f"a click outside was asked to close {pop}, and it does not")
            if re.search(r"\b(escape|esc)\b", words) and "Escape" not in closers:
                need.append(f"Escape was asked to close {pop}, and it does not")
    need += kept_behaviour(trig, on, res, base)
    msg = f"{label}: " + "; ".join(seen or ["nothing opened"])
    return not need, msg + ("" if not need else " — " + "; ".join(need))


def judge_style(c, res, base):
    """A look that changes while a person acts on an element, measured while they do."""
    eid, on, prop = c.get("id"), c.get("on"), c.get("property")
    label = f"{eid} while {VERB.get(on, on)}"
    key = {"background": "bg", "text": "label", "border": "border", "opacity": "opacity"}.get(prop)
    if on not in VERB or not key:
        return False, f"{label}: 'on' must be hover, click or focus and 'property' background, text, border or opacity"
    if not res or not res.get("trigger"):
        return False, f"{label}: the element could not be used in a browser — UNVERIFIED"
    act, rest = res.get("actStyle") or {}, res.get("restStyle") or {}
    if key == "opacity":
        try:
            want, got = float(c.get("equals")), float(act.get("opacity"))
        except (TypeError, ValueError):
            return False, f"{label}: opacity must be a number"
        ok = abs(want - got) <= 0.03
        msg = f"{label}: opacity measured {rest.get('opacity')} at rest -> {got}, claimed {want}"
    else:
        want, got, was = AE._rgb_of(str(c.get("equals", ""))), _rgb(act.get(key)), _rgb(rest.get(key))
        if not want or not got:
            return False, f"{label}: could not read {prop} as a colour"
        ok = sum(abs(a - b) for a, b in zip(want, got)) <= 12
        msg = f"{label}: {prop} measured rgb{was} at rest -> rgb{got}, claimed rgb{want}"
    if ok and rest.get(key) == act.get(key):
        ok, msg = False, msg + " — nothing changes when it is acted on"
    need = kept_behaviour(eid, on, res, base, restyled={key, "bgi"} if key == "bg" else {key})
    return ok and not need, msg + ("" if not need else " — " + "; ".join(need))


def unmeasured_touches(html, plan, claims, extra=(), pb=None):
    """Naming an element as changed is a licence to change it, so every one must be
    MEASURED: the subject of a claim, or something sitting on a claimed element. Measured
    live: a model shrinking the chat box named the background and every decorative rule as
    'touched' with no claim on any of them — which licensed changing them unmeasured, and
    naming the background switched off the on-screen check altogether."""
    touched = {t for t in plan.get("touches") or [] if isinstance(t, str)}
    subjects = {c.get("id") for c in claims if isinstance(c.get("id"), str)}
    subjects |= {c.get("trigger") for c in claims
                 if c.get("kind") in ("appears_on", "moves") and isinstance(c.get("trigger"), str)}
    subjects |= {i for i in extra if isinstance(i, str)}
    for c in claims:
        if c.get("kind") == "colors":
            region = c.get("region", "background")
            subjects.add("bg" if region == "background" else region)
    allowed = set(subjects)
    for s in subjects:
        if s != "bg":
            try:
                allowed |= set(AE.riders(html, s))
            except Exception:
                pass
    # Naming something that merely INHERITS a claimed change is honest, not a licence: the
    # claim on its ancestor is what measures it. Without this the model is in a closed trap —
    # not naming the child is refused, naming it is refused, and forcing it back is refused
    # by mutation for being a part no test needs.
    if pb and pb.get("els"):
        for t in list(touched - allowed):
            if inherits_from(pb, t, subjects):
                allowed.add(t)
    extra = sorted(touched - allowed)
    if not extra:
        return []
    one = len(extra) == 1
    return [f"{', '.join(extra)} {'was' if one else 'were'} named as changed, but no claim measures "
            f"{'it' if one else 'them'} and {'it sits' if one else 'they sit'} on nothing that is claimed — "
            "leave " + ("it" if one else "them") + " exactly as before, or claim what changes"
            + (" (the background may only change when a colors claim on the background measures it)"
               if "bg" in extra else "")]


def check_claim(c, pb, pa, pl, png, w, h, request=""):
    """Measure one claim. Returns (ok, the measurement in words)."""
    kind, eid = c.get("kind"), c.get("id")
    eb, ea = pb["els"].get(eid), pa["els"].get(eid)
    if kind in ("size", "position"):
        key = {"height": "h", "width": "w"}.get(c.get("dimension")) if kind == "size" else \
            {"x": "x", "y": "y"}.get(c.get("axis"))
        if not key or not eb or not ea:
            return False, f"{kind} claim on {eid}: that element or dimension could not be measured"
        m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(%|px)?\s*$", str(c.get("change")))
        if not m:
            return False, f"{kind} claim on {eid}: change must be like -10% or -24px"
        v = float(m.group(1))
        if abs(v) < 1:
            return False, f"{kind} claim on {eid}: a change of {c.get('change')} is no change"
        if m.group(2) == "%":
            if kind == "position":
                return False, f"position claim on {eid}: give the change in px"
            target = eb[key] * (1 + v / 100)
        else:
            target = eb[key] + v
        tol = max(2.0, 0.03 * max(1.0, eb[key] if kind == "size" else abs(target - eb[key])))
        ok = abs(ea[key] - target) <= tol
        return ok, (f"{eid} {c.get('dimension') or c.get('axis')}: measured {eb[key]:.1f} -> {ea[key]:.1f}px, "
                    f"claimed {c.get('change')} (= {target:.1f}px, tolerance {tol:.1f})")
    if kind == "text":
        if not ea:
            return False, f"text claim on {eid}: element not found after the change"
        want = re.sub(r"\s+", " ", str(c.get("equals", ""))).strip()
        return ea["text"] == want, f"{eid} text: measured '{ea['text'][:60]}', claimed '{want[:60]}'"
    if kind == "color":
        if not ea:
            return False, f"color claim on {eid}: element not found after the change"
        want = AE._rgb_of(str(c.get("equals", "")))
        prop = c.get("property")
        got = _rgb(ea["bg"]) if prop == "background" else _rgb(ea.get("lcolor") or ea["color"])
        if not want or not got:
            return False, f"color claim on {eid}: could not read {prop} as a colour"
        d = sum(abs(a - b) for a, b in zip(want, got))
        if d > 12:
            return False, f"{eid} {prop}: measured rgb{got}, claimed rgb{want} (difference {d})"
        # AND THE SCREEN, NOT ONLY THE PROPERTY. Measured 2026-09-15 on "make the Generate button
        # green": the reply set background-color green and LEFT THE GRADIENT on top of it, so the
        # computed property was green, this claim passed, and the button looked exactly as before.
        # A colour nobody can see is not a colour that was changed.
        if prop == "background" and png is not None and ea and ea["w"] > 6 and ea["h"] > 6:
            seen = _box_colour(png, ea)
            if seen:
                d2 = sum(abs(a - b) for a, b in zip(want, seen))
                if d2 > 90:
                    return False, (f"{eid} background: the style says rgb{got}, but on screen the "
                                   f"element is rgb{seen} — something is painted over it (a gradient "
                                   f"or an image), so nobody sees the colour that was claimed")
                return True, (f"{eid} {prop}: measured rgb{got} and rgb{seen} on screen, claimed "
                              f"rgb{want}")
        return True, f"{eid} {prop}: measured rgb{got}, claimed rgb{want} (difference {d})"
    if kind == "colors":
        fams = [ALIASES.get(str(f), str(f)) for f in c.get("families") or []]
        if not fams or any(f not in FAMILIES for f in fams) or not png:
            return False, f"colors claim: families must be from {list(FAMILIES)}"
        try:
            need = max(0.6, float(c.get("min_share", 0.6) or 0.6))
        except (TypeError, ValueError):
            need = 0.6
        region = c.get("region", "background")
        if region == "background":
            boxes = [(e["x"], e["y"], e["w"], e["h"]) for i, e in pa["els"].items()
                     if i != "bg" and e["w"] * e["h"] < 0.6 * w * h]
            got = shares(png, None, boxes)
        else:
            e = pa["els"].get(region)
            if not e:
                return False, f"colors claim: {region} could not be measured"
            got = shares(png, (e["x"], e["y"], e["w"], e["h"]))
        # A recolour keeps its white and black tones, so a claim may count them — but never a hue
        # the request did not name, which is how a sky left mostly blue could pass as orange.
        named = {ALIASES.get(x, x) for x in re.findall(COLOUR_WORDS, _unquoted(request))}
        extra = [f for f in fams if named and f not in named and f not in ("black", "white", "grey")]
        if extra:
            return False, f"colors claim counts {', '.join(extra)}, which the request did not ask for"
        share = sum(got.get(f, 0) for f in fams)
        thin = [f for f in fams if (not named or f in named) and got.get(f, 0) < 0.08]
        top = ", ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(got.items(), key=lambda t: -t[1])[:5])
        return share >= need and not thin, (
            f"{region} colours measured: {top} — {' + '.join(fams)} = {share * 100:.0f}% (needs {need * 100:.0f}%)"
            + (f"; {', '.join(thin)} under 8%" if thin else ""))
    if kind == "removed":
        gone = ea is None or ea["disp"] == "none" or ea["vis"] == "hidden" or ea["w"] * ea["h"] == 0
        return gone, f"{eid}: " + ("no longer on the page" if gone else "still on the page")
    if kind == "added":
        ok = bool(ea) and not eb and ea["w"] > 0 and ea["h"] > 0 and ea["x"] >= -1 and ea["y"] >= -1 \
            and ea["x"] + ea["w"] <= w + 1 and ea["y"] + ea["h"] <= h + 1
        return ok, f"{eid}: " + (f"new, measured at {ea['x']:.0f},{ea['y']:.0f} {ea['w']:.0f}x{ea['h']:.0f}" if ok
                                 else "not found as a new element inside the page")
    if kind == "changes_over_time":
        tl = pl or {}
        seq = (tl.get("seq") or {}).get(eid)
        if not ea or not seq:
            return False, f"changes_over_time on {eid}: its words could not be followed over time"
        return typing_verdict(eid, seq, eb["text"] if eb else "", request, 20, (tl.get("kept") or {}).get(eid),
                              ea.get("tag") in ("textarea", "input"))
    return False, f"unknown claim kind {kind!r}"


def measured_intent(request, claims, pb, pa):
    """The measured change must go the way the words went, and only as far."""
    words = _unquoted(request)
    shrink, grow = bool(re.search(SHRINK, words)), bool(re.search(GROW, words))
    little = re.search(LITTLE, words)
    sizes = [c for c in claims if c.get("kind") == "size"]
    covered = {(c.get("id"), c.get("dimension")) for c in sizes}
    problems = []
    for c in sizes:
        key = {"height": "h", "width": "w"}.get(c.get("dimension"))
        eid = c.get("id")
        eb, ea = pb["els"].get(eid), pa["els"].get(eid)
        if not key or not eb or not ea or not eb[key]:
            continue
        pct = (ea[key] - eb[key]) / eb[key] * 100
        other = "width" if c.get("dimension") == "height" else "height"
        ok_ = "w" if other == "width" else "h"
        if (eid, other) not in covered and abs(ea[ok_] - eb[ok_]) > 1.5:
            problems.append(f"only the {c.get('dimension')} of {eid} was asked for, but its {other} also "
                            f"changed {eb[ok_]} -> {ea[ok_]}px")
        if abs(pct) < 0.5:
            continue          # nothing changed: the claim itself already failed, and says so
        if shrink != grow and (pct < 0) != shrink:
            problems.append(f"{eid} {c.get('dimension')} measured {pct:+.1f}%, but the request asks to "
                            f"{'reduce' if shrink else 'increase'} it")
        if little and not 3 <= abs(pct) <= 25:
            problems.append(f"'a little' was asked for; {eid} {c.get('dimension')} measured {pct:+.1f}% "
                            f"— keep it between 3% and 25%")
    return problems


# Properties a child takes from its parent unless it sets its own. Setting a colour on a
# link IS setting it on the text inside the link — one change, seen twice.
INHERITED = {"color", "st"}


def inherits_from(pb, eid, claimed):
    """Is this element inside something whose own change it would inherit? Walks the probe's
    parent chain. Measured live on a real template: a model asked to recolour a nav link set
    the colour on the <a> and was refused because the <div> inside it also changed; it named
    the <div> and was refused for naming something no claim measures; it forced the <div> to
    `inherit` and was refused because no test needed that CSS. Three correct answers, three
    refusals, no fourth option — a closed trap, and the same consequence rule this project
    already applies to position, size and reactions, missing one last place."""
    seen = set()
    cur = (pb["els"].get(eid) or {}).get("up")
    while cur and cur not in seen:
        if cur in claimed:
            return cur
        seen.add(cur)
        cur = (pb["els"].get(cur) or {}).get("up")
    return None


DRIFT_KEYS = ("x", "y", "w", "h", "text", "own", "color", "bg", "bgi", "vis", "disp",
              "op", "lcolor", "st")


def self_drift(before, workdir, w, h, moments=(1200, 2600, 4200)):
    """What does this page change about ITSELF, with nothing touched?

    A real template is alive — a ticker slides, a heading types itself, content loads in.
    `identity()` compares one reading of the before page with one reading of the after page,
    so on a live page it reports the page's own motion as changes nobody named: measured on a
    real Webflow migration, 40+ elements were refused for moving, and every one of them was a
    marquee or a typing headline doing what it always does. This is the rule the AGM already
    lives by — hold the page still, measure the drift, exclude it — which the change path's
    own identity check never had. Two readings of the UNCHANGED page, at two moments; whatever
    differs between them moves by itself and cannot be evidence about a change.
    """
    reads = [probe(before, workdir, w, h, t) for t in moments]
    reads = [r for r in reads if r and r.get("els")]
    if len(reads) < 2:
        return None
    # THREE MOMENTS, NOT TWO. A page that settles at random is not characterised by one pair:
    # measured on a real template, an element read the same at two moments and differently at
    # a third, and the same element moved one way on one run and the other way on the next.
    # Every pair is compared and the differences UNIONED, so the more ways it can be caught
    # moving, the more of its restlessness is known before anything is judged.
    moves = set()
    for a in reads:
        for b in reads:
            if a is b:
                continue
            for i, e in a["els"].items():
                f = b["els"].get(i)
                if f is None:
                    moves.add((i, "*"))
                    continue
                for k in DRIFT_KEYS:
                    x, y = e.get(k), f.get(k)
                    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                        if abs(x - y) > 1.5:
                            moves.add((i, k))
                    elif x != y:
                        moves.add((i, k))
    return moves


def identity(pb, pa, allowed, w, h, movable=(), hosts=(), inheriting=(), drifting=()):
    """Everything not named must measure exactly as before; things riding on a named
    element may change position and nothing else. A HOST — an element that only has
    behaviour attached — must look exactly as before at rest, down to its own words, even
    though it is named. And nothing a person could click before may be covered now."""
    bad = []
    for i, e in pb["els"].items():
        if i == "bg" or (i in allowed and i not in hosts):
            continue
        a = pa["els"].get(i)
        if a is None:
            bad.append(f"{i} disappeared, but it was not named")
            continue
        if (i, "*") in drifting:
            continue                    # this element is not the same thing twice running
        for k in (("w", "h") if i in movable else ("x", "y", "w", "h")):
            if abs(a[k] - e[k]) > 1.5 and (i, k) not in drifting:
                bad.append(f"{i} {k} changed {e[k]} -> {a[k]}, but " + (
                    "it only gets new behaviour" if i in hosts else "it was not named"))
                break
        for k in ("own" if i in hosts else "text", "color", "bg", "bgi", "vis", "disp", "op", "lcolor", "st"):
            if a.get(k) != e.get(k):
                if (i, k) in drifting:
                    continue          # it reads differently at two moments on its own
                if k in INHERITED and i not in hosts:
                    host = inherits_from(pb, i, inheriting)
                    if host:
                        continue      # this IS the claimed change, seen on what inherits it
                bad.append(f"{i} {'style' if k == 'st' else 'words' if k == 'own' else k} changed, but "
                           + ("it only gets new behaviour" if i in hosts else "it was not named"))
                break
        if e.get("hit") is True and a.get("hit") is False and e.get("tag") in ("button", "a", "input", "textarea",
                                                                               "select", "label"):
            bad.append(f"{i} can no longer be clicked: something now covers its centre")
    # "NOW sits outside the page" has to mean it was INSIDE before. Measured on a real
    # template: a marquee's items legitimately sit past the right edge (that is what a ticker
    # is), and everything below a capped canvas height is outside it by construction — 33
    # elements refused for a position they already had. Only a move from inside to outside is
    # the change's doing.
    def outside(e):
        return (e["x"] < -1 or e["y"] < -1
                or e["x"] + e["w"] > w + 1 or e["y"] + e["h"] > h + 1)

    for i, a in pa["els"].items():
        if i == "bg" or not (a["w"] > 0 and a["h"] > 0) or a["disp"] == "none":
            continue
        e = pb["els"].get(i)
        if outside(a) and (e is None or not outside(e)):
            bad.append(f"{i} now sits outside the page")
    if pa.get("sw", 0) > pa.get("iw", 0) + 1:
        bad.append("the page now scrolls sideways")
    # WHAT THE PAGE ALREADY THREW IS NOT THE CHANGE'S FAULT. A real template ships broken
    # scripts — test-2 throws `gsap is not defined`, `WebFont is not defined` and
    # `Lenis is not defined` before anything is touched — and blaming a change for a fault
    # that predates it refuses correct work. Anything NEW still fails, which is the point.
    fresh = [e for e in (pa.get("errors") or []) if e not in set(pb.get("errors") or [])]
    if fresh:
        bad.append("script errors: " + "; ".join(fresh[:3]))
    return bad


def new_overlaps(pb, pa, involved, kinds, skip=()):
    """Two things that did not overlap before and partly overlap now. A box inside another
    is not an overlap — that is a button on its card. Decorative rules and strokes lie
    beneath content and are not counted. Pairs in `skip` are already reported more
    precisely (a rider hanging off its host)."""
    def box(e):
        return e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]

    def inter(a, b):
        return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))

    def holds(a, b):
        return a[0] - 1.5 <= b[0] and a[1] - 1.5 <= b[1] and b[2] <= a[2] + 1.5 and b[3] <= a[3] + 1.5

    def partial(a, b):
        return inter(a, b) > 4 and not holds(a, b) and not holds(b, a)

    def solid(i, snap):
        e = snap["els"].get(i)
        return bool(e) and i != "bg" and kinds.get(i) not in ("ground", "rule", "stroke") \
            and e["disp"] != "none" and e["vis"] != "hidden" and e["w"] * e["h"] > 0 \
            and float(e.get("op") or 1) > 0.05
    out, seen = [], set()
    for a in sorted(i for i in involved if solid(i, pa)):
        for b in pa["els"]:
            if b == a or not solid(b, pa) or frozenset((a, b)) in seen or frozenset((a, b)) in skip:
                continue
            A, B = box(pa["els"][a]), box(pa["els"][b])
            if not partial(A, B):
                continue
            if solid(a, pb) and solid(b, pb) and partial(box(pb["els"][a]), box(pb["els"][b])):
                continue
            seen.add(frozenset((a, b)))
            out.append(f"{a} now overlaps {b}: {a} spans x {A[0]:.0f}-{A[2]:.0f}, y {A[1]:.1f}-{A[3]:.1f}; "
                       f"{b} spans x {B[0]:.0f}-{B[2]:.0f}, y {B[1]:.1f}-{B[3]:.1f} — they did not overlap before")
    return out


def _reach(st):
    """How far an element paints beyond its box: its shadow, or a few pixels of glyph."""
    parts = (st or "").split("|")
    shadow = parts[5] if len(parts) > 5 else ""
    reach = 0.0
    nums = [abs(float(n)) for n in re.findall(r"(-?\d+(?:\.\d+)?)px", shadow)]
    for i in range(0, len(nums), 4):
        g = nums[i:i + 4] + [0.0] * (4 - len(nums[i:i + 4]))
        reach = max(reach, max(g[0], g[1]) + g[2] + g[3])
    return reach + 4


_SHOTS = {}


def _shot(html, workdir, w, h, name):
    import aethron_figma_grade as GR
    import aethron_vision as V
    # WHERE IT RENDERS IS PART OF WHAT IT RENDERS. The same html gives a different picture
    # depending on where its relative assets resolve from — the working directory, or the
    # base when one is set. A cache keyed on the html alone hands back the wrong picture the
    # moment two pages, or two locations, are measured in one run. Found by the adopt battery:
    # the same page rendered at home and away scored identical because the second read never
    # happened at all.
    key = (hashlib.sha1(html.encode()).hexdigest(), w, h, ASSET_BASE, ASSET_ORIGIN, str(workdir))
    if key not in _SHOTS:
        work = Path(workdir)
        work.mkdir(parents=True, exist_ok=True)
        served, url, is_served = _place(html, f"px_{name}")
        png = (served.with_suffix(".png") if is_served else work / f"_px_{name}.png")
        f = served if is_served else work / f"_px_{name}.html"
        if not is_served:
            f.write_text(_with_base(html))
        try:
            if png.exists():
                png.unlink()
            GR.shoot(f, w, h, png, url=url if is_served else None)
            if not png.exists():
                return None
            _SHOTS[key] = V.load(png)
        finally:
            if is_served:
                for q in (f, png):
                    try:
                        q.unlink()
                    except OSError:
                        pass
    return _SHOTS[key]


def pixels_outside(before, after, boxes, workdir, w, h, tol=24, step=2):
    """Render both pages once and list the sampled pixels that changed OUTSIDE the given
    boxes. None when it cannot render."""
    a, b = _shot(before, workdir, w, h, "before"), _shot(after, workdir, w, h, "after")
    if a is None or b is None:
        return None
    allow = bytearray(w * h)
    for x, y, bw, bh in boxes:
        x0, x1 = max(0, int(x)), min(w, int(x + bw) + 1)
        for row in range(max(0, int(y)), min(h, int(y + bh) + 1)):
            if x1 > x0:
                allow[row * w + x0:row * w + x1] = b"\x01" * (x1 - x0)
    bad = []
    for y in range(0, min(h, a.h, b.h), step):
        for x in range(0, min(w, a.w, b.w), step):
            if allow[y * w + x]:
                continue
            p, q = a.rgb(x, y), b.rgb(x, y)
            if abs(p[0] - q[0]) > tol or abs(p[1] - q[1]) > tol or abs(p[2] - q[2]) > tol:
                bad.append((x, y))
    return bad


def _unstable_screen(before, boxes, workdir, w, h, found):
    """Do two renders of the SAME page agree? Asked only when a difference was found, so an
    honest change pays nothing for it. `found` is what the real comparison saw: drift of a fifth
    of that is enough to make the reading worthless."""
    twin = pixels_outside(before, before + "<!-- a second look -->", boxes, workdir, w, h)
    return twin is None or len(twin) > max(6, found // 5)


# ───────────────────────────── the loop ─────────────────────────────

def _alive_block(html):
    m = AE.ALIVE_RE.search(html)
    return m.group(0) if m else None


def _page_rule(html):
    m = re.search(r"\.page\{[^}]*\}", html)
    return m.group(0) if m else None


def _colours(block):
    return {tuple(int(v) for v in m) for m in re.findall(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", block or "")}


# ───────────────────── a recolour changes the hue, not the light ─────────────────────
#
# THE OWNER'S EYE, 2026-09-15: the orange sky "is not the same shape as the blue one". Measured, it
# was two faults a colour-share check could never see. The model REBUILT the gradient — 8 glow
# layers with ~10 stops and solid plateaus became fewer layers with a 2-stop fade — and it turned
# the white core orange: the brightest quarter of the sky fell from L 95 to L 52, the light tones
# from 78 to 37. The blue sky is a ramp of light (white, light blue, blue, deep blue, black); a
# recolour must keep that ramp and every shape in it, and change only the hue.

TARGET_REF = {"red": (220, 40, 40), "orange": (255, 140, 30), "yellow": (235, 195, 20), "green": (40, 170, 70),
              "teal": (20, 165, 165), "blue": (40, 90, 230), "purple": (140, 60, 210), "pink": (230, 80, 160)}
TONE_WORDS = (r"\b(dark(er|en|ness)?|light(er|en|ness)?|bright(er|en|ness)?|dim(mer)?|pale(r)?|deep(er)?|vivid|"
              r"muted|contrast|invert(ed)?|neon|pastel|soft(er)?|harsh(er)?|glow(ier)?)\b")
RESHAPE_WORDS = (r"\b(shape|size|bigger|smaller|larger|position|move|spread|wider|narrower|layers?|blobs?|add|"
                 r"remove|more|fewer|less|simpl\w*|flat(ter)?|pattern)\b")


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c):
    c = max(0.0, c)
    return 255.0 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)


def rgb_to_lab(rgb):
    r, g, b = (_lin(float(v)) for v in rgb[:3])
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def lab_to_rgb(L, a, b):
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def inv(t):
        return t ** 3 if t ** 3 > 0.008856 else (t - 16 / 116) / 7.787
    x, y, z = inv(fx) * 0.95047, inv(fy), inv(fz) * 1.08883
    r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    g = -0.9689 * x + 1.8758 * y + 0.0415 * z
    bl = 0.0557 * x - 0.2040 * y + 1.0570 * z
    return (_gam(r), _gam(g), _gam(bl)), all(-0.002 <= v <= 1.002 for v in (r, g, bl))


def lch(rgb):
    import math
    L, a, b = rgb_to_lab(rgb)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def from_lch(L, chroma, hue):
    """The colour with this lightness and hue, its chroma reduced only as far as sRGB requires."""
    import math
    ca, sa = math.cos(math.radians(hue)), math.sin(math.radians(hue))
    rgb, fits = lab_to_rgb(L, chroma * ca, chroma * sa)
    if not fits:
        lo, hi = 0.0, chroma
        for _ in range(30):
            mid = (lo + hi) / 2
            if lab_to_rgb(L, mid * ca, mid * sa)[1]:
                lo = mid
            else:
                hi = mid
        rgb = lab_to_rgb(L, lo * ca, lo * sa)[0]
    return tuple(int(round(max(0.0, min(255.0, v)))) for v in rgb)


def tone_map(block, family_name):
    """Every colour in `block`, recoloured to `family_name` at EXACTLY its own lightness and chroma.
    Neutrals — white, grey, black — keep their light and stay neutral."""
    hue = lch(TARGET_REF[family_name])[2]
    out = {}
    for t in _colours(block):
        L, chroma, _ = lch(t)
        out[t] = t if chroma < 8 else from_lch(L, chroma, hue)
    return out


def colour_guidance(html, request):
    """For a recolour of the gradient background: the exact new colours, computed by Aethron, so a
    model never guesses a colour's lightness."""
    words = _unquoted(request)
    fams = [ALIASES.get(f, f) for f in re.findall(COLOUR_WORDS, words)]
    hues = [f for f in fams if f in TARGET_REF]
    rule = _page_rule(html)
    if not hues or not rule or "gradient" not in rule or re.search(TONE_WORDS, words):
        return ""
    lines = []
    for old, new in sorted(tone_map(rule + (_alive_block(html) or ""), hues[0]).items(), key=lambda kv: -lch(kv[0])[0]):
        L = lch(old)[0]
        lines.append(f"  rgb{old}  lightness {L:.0f}  " + ("keep — white, grey and black keep their light" if old == new
                                                           else f"-> rgb{new}  (lightness {lch(new)[0]:.0f})"))
    return ("\n\nCOLOUR MAP FOR THE BACKGROUND — computed by Aethron: the same lightness, the new hue. A recolour changes "
            "HUE, not LIGHT: the brightest glow stays the brightest, the darkest stays the darkest. Replace each colour "
            "with a counted patch of its 'rgba(R,G,B,' text, in BOTH the .page rule and the moving copy, and change "
            "NOTHING else in the gradients — not the layers, their sizes or positions, the stop positions or the "
            "alphas:\n" + "\n".join(lines))


def _split_top(text, sep=","):
    out, depth, cur = [], 0, []
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _alpha(col):
    m = re.match(r"rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)", col.strip())
    return float(m.group(1)) if m else (0.0 if col.strip() == "transparent" else 1.0)


def gradient_shape(block):
    """The background with every colour taken out: layers, their geometry, and each stop's alpha
    and position. Two gradients with the same shape differ only in colour."""
    m = re.search(r"background(?:-image)?\s*:\s*(.*?)(?:;|\})", block or "", re.S)
    if not m:
        return None
    shape = []
    for item in _split_top(m.group(1)):
        g = re.match(r"((?:repeating-)?(?:radial|linear|conic))-gradient\((.*)\)\s*$", item, re.S)
        if not g:
            shape.append(("fill", round(_alpha(item), 3)))
            continue
        args = _split_top(g.group(2))
        geo = ""
        if args and not re.match(r"(rgba?|hsla?)\(|#|transparent\b", args[0]):
            geo, args = re.sub(r"\s+", " ", args[0]), args[1:]
        stops = []
        for a in args:
            cm = re.match(r"(rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8}|transparent|[a-z]+)\s*(.*)$", a, re.S)
            stops.append((round(_alpha(cm.group(1)), 3), cm.group(2).strip()) if cm else (1.0, a))
        shape.append((g.group(1), geo, tuple(stops)))
    return shape


def shape_kept(before, after):
    """None when every gradient kept its layers, geometry, stops and alphas; else what was rebuilt."""
    for label, pick in (("the still background (.page)", _page_rule), ("its moving copy", _alive_block)):
        sb, sa = gradient_shape(pick(before)), gradient_shape(pick(after))
        if sb is None or sa is None or sb == sa:
            continue
        gb, ga = [x for x in sb if x[0] != "fill"], [x for x in sa if x[0] != "fill"]
        if len(gb) != len(ga):
            return (f"the recolour rebuilt {label}: its {len(gb)} glow layers became {len(ga)} — a colour change keeps "
                    "every layer and changes only the colour values")
        for i, (x, y) in enumerate(zip(gb, ga)):
            if x[1] != y[1]:
                return f"the recolour moved or resized glow layer {i + 1} of {label}: '{x[1]}' became '{y[1]}'"
            if x[2] != y[2]:
                return (f"the recolour reshaped glow layer {i + 1} of {label}: its {len(x[2])} stops (positions and "
                        f"transparency) became {len(y[2])} — keep every stop and change only its colour")
        return f"the recolour changed the structure of {label} (its base fill)"
    return None


TONE_BANDS = ((0, 10, "the black"), (10, 30, "the darkest tones"), (30, 50, "the deep tones"),
              (50, 70, "the mid tones"), (70, 85, "the light tones"), (85, 101, "the brightest glow"))


def tone_kept(a, b, boxes, w, h, step=4):
    """Compare the LIGHT of two renders, band by band from black to the brightest glow, outside the
    element boxes. -> (ok, words). A band holding 3% of the sky may move by 10 lightness at most."""
    acc = {band: [0, 0.0, 0.0] for band in TONE_BANDS}
    n = 0
    diff = 0.0
    for y in range(0, min(h, a.h, b.h), step):
        for x in range(0, min(w, a.w, b.w), step):
            if any(bx <= x < bx + bw and by <= y < by + bh for bx, by, bw, bh in boxes):
                continue
            l0, l1 = rgb_to_lab(a.rgb(x, y))[0], rgb_to_lab(b.rgb(x, y))[0]
            for band in TONE_BANDS:
                if band[0] <= l0 < band[1]:
                    acc[band][0] += 1
                    acc[band][1] += l0
                    acc[band][2] += l1
            diff += abs(l1 - l0)
            n += 1
    if not n:
        return False, "no background was left to measure — UNVERIFIED"
    rows, bad = [], []
    for band, (count, s0, s1) in acc.items():
        if not count:
            continue
        share, m0, m1 = count / n, s0 / count, s1 / count
        rows.append(f"{band[2]} {m0:.0f}->{m1:.0f}")
        if share >= 0.03 and abs(m1 - m0) > 10:
            bad.append(f"{band[2]} ({share * 100:.0f}% of the sky) went from lightness {m0:.0f} to {m1:.0f}")
    mean = diff / n
    if mean > 8 and not bad:
        bad.append(f"the sky's lightness moved {mean:.1f} on average")
    if bad:
        return False, ("the recolour changed the LIGHT, not just the colour: " + "; ".join(bad)
                       + " — keep each colour's lightness and change only its hue")
    return True, f"the light of the sky is kept, band by band (lightness {', '.join(rows)})"


def copies_agree(before, after):
    """A moving background is written twice. If both copies carried the same colours before
    the change, they must after — otherwise the page at rest and the page in motion are two
    different designs. Checked from the source, before anything is rendered."""
    rb, ab, ra, aa = _page_rule(before), _alive_block(before), _page_rule(after), _alive_block(after)
    if not (rb and ab and ra and aa) or _colours(rb) != _colours(ab):
        return None
    still, moving = _colours(ra) - _colours(aa), _colours(aa) - _colours(ra)
    if not still and not moving:
        return None

    def fmt(s):
        return ", ".join(f"rgb{t}" for t in sorted(s)[:4]) + (" …" if len(s) > 4 else "")
    return ("the still gradient (.page) and its moving copy (<style data-ae-alive>) no longer carry the same "
            "colours" + (f"; only in .page: {fmt(still)}" if still else "")
            + (f"; only in the moving copy: {fmt(moving)}" if moving else ""))


def verify(before, after, plan, pb, workdir, w, h, request="", new_ids=(), acted_on=(), asserted=()):
    import aethron_figma_grade as GR
    measured, problems = [], []
    claims = [c for c in plan.get("expect") or [] if isinstance(c, dict)]
    pa = probe(after, workdir, w, h, 1200)
    if pa and pa.get("fonts") != pb.get("fonts"):
        pa = probe(after, workdir, w, h, 1200)
    if not pa or not pa.get("els"):
        return measured, ["the changed page could not be measured — it may not load"]
    if pa.get("fonts") != pb.get("fonts"):
        return measured, ["the page's fonts loaded differently in the two measurements, so they cannot be "
                          "compared — UNVERIFIED"]
    touched = {t for t in plan.get("touches") or [] if isinstance(t, str)}
    grown = {c.get("id") for c in claims if c.get("kind") in ("added", "removed", "appears_on")} | set(new_ids)
    acted = ({c.get("trigger") for c in claims if c.get("kind") == "appears_on"}
             | {c.get("id") for c in claims if c.get("kind") == "style_on"} | set(acted_on))
    others = {c.get("id") for c in claims if c.get("kind") not in INTERACT_KINDS} | set(asserted)
    hosts = {i for i in acted if isinstance(i, str)} - others - grown
    named = touched | grown
    carried = set()
    for t in touched - hosts:
        carried |= set(AE.riders(before, t))
    carried -= named
    kinds = {e["id"]: e["kind"] for e in AE.manifest(before)["elements"] + AE.manifest(after)["elements"]}
    # A colour or type claim is inherited by everything inside its subject. That is one
    # change seen twice, not two changes.
    inheriting = {c.get("id") for c in claims
                  if c.get("kind") in ("color", "colors", "style_on")
                  and isinstance(c.get("id"), str)} | (touched & others)
    # What this page does on its own, measured once and used by BOTH checks below. An element
    # that will not hold still is not evidence in the DOM and it is not evidence on the SCREEN
    # either — measured live, a headline that types itself put 11,360 changed pixels around
    # itself and refused a one-line recolour of a nav link three times running, while naming
    # it instead was refused for naming what no claim measures. No legal move existed.
    _drift = {"got": None, "done": False}

    def restless():
        if not _drift["done"]:
            _drift["done"] = True
            d = self_drift(before, workdir, w, h)
            _drift["got"] = {i for i, _ in d} if d else set()
        return _drift["got"]

    same = identity(pb, pa, named, w, h, movable=carried, hosts=hosts, inheriting=inheriting)
    if same:
        # ONLY WHEN SOMETHING WAS FOUND, as with the screen self-check: an honest change on a
        # still page pays nothing for this. A live page's own motion is not evidence.
        drift = restless()
        if drift:
            # AT ELEMENT GRANULARITY, not field. The set of fields that drift is DIFFERENT on
            # every render — measured on a real template, the same elements moved one way on
            # one run and the other way on the next — so a field-level filter is always one
            # render behind. An element that will not hold still cannot be asserted unchanged
            # at all; the honest report is that it could not be compared, which is what this
            # says, rather than a refusal it did not earn or a pass it did not prove.
            unstable = set(drift)
            same = identity(pb, pa, named, w, h, movable=carried, hosts=hosts,
                            inheriting=inheriting,
                            drifting={(i, "*") for i in unstable})
            note = (f"{len(unstable)} element(s) on this page will not hold still — two "
                    f"readings of it UNCHANGED already differ — so they could not be "
                    f"compared and are reported, not refused")
            measured.append(note)
            if len(unstable) > max(8, len(pb["els"]) // 3):
                problems.append(f"{note}; too much of this page moves by itself for the rest "
                                f"to be proven unchanged — UNVERIFIED")
    problems += same
    if pa.get("struct") != pb.get("struct"):
        tb, ta = Counter((pb.get("struct") or "").split()), Counter((pa.get("struct") or "").split())
        new_t, gone_t = list((ta - tb).elements()), list((tb - ta).elements())
        problems.append("elements without a data-ae-id changed — "
                        + "; ".join(p for p in (f"new: {', '.join(new_t[:4])}" if new_t else "",
                                                f"gone: {', '.join(gone_t[:4])}" if gone_t else "") if p)
                        + " — give every new element its own data-ae-id and a claim")
    hanging = set()
    for t in touched:
        host = pa["els"].get(t)
        for r in AE.riders(before, t):
            ra = pa["els"].get(r)
            if host and ra and ra["w"] * ra["h"] > 0 and ra["disp"] != "none" and not (
                    ra["x"] >= host["x"] - 1.5 and ra["y"] >= host["y"] - 1.5
                    and ra["x"] + ra["w"] <= host["x"] + host["w"] + 1.5
                    and ra["y"] + ra["h"] <= host["y"] + host["h"] + 1.5):
                hanging.add(frozenset((t, r)))
                problems.append(f"{r} sits on {t} but now hangs outside it "
                                f"({ra['y'] + ra['h']:.1f} against its bottom edge at {host['y'] + host['h']:.1f})")
    problems += new_overlaps(pb, pa, named | carried, kinds, skip=hanging)
    watch = sorted({c["id"] for c in claims if c.get("kind") == "changes_over_time" and isinstance(c.get("id"), str)})
    pl = timeline(after, workdir, w, h, watch) if watch else None
    png = None
    if any(c.get("kind") == "colors" or (c.get("kind") == "color" and c.get("property") == "background")
           for c in claims):
        f, out = Path(workdir) / "_change_after.html", Path(workdir) / "_change_after.png"
        f.write_text(after)
        if out.exists():
            out.unlink()
        GR.shoot(f, w, h, out)
        png = out if out.exists() else None
    for c in claims:
        if c.get("kind") in INTERACT_KINDS:
            pop = c.get("id") if c.get("kind") == "appears_on" else None
            trig = c.get("trigger") if pop else c.get("id")
            spec = {"trigger": trig, "pop": pop, "on": c.get("on")}
            res = interact(after, workdir, w, h, spec)
            base = interact(before, workdir, w, h, dict(spec, pop=None))
            if pop:
                tb = pb["els"].get(trig)
                ok, msg = judge_popup(c, res, base, request, (tb["x"], tb["y"], tb["w"], tb["h"]) if tb else None)
            else:
                ok, msg = judge_style(c, res, base)
        elif c.get("kind") == "moves":
            want = c.get("for_ms")
            spec = {"id": c.get("id"), "trigger": c.get("trigger"), "on": c.get("on") or "click",
                    "watch_ms": int(want) + 2500 if want else 5000}
            ok, msg = judge_moves(c, moves(after, workdir, w, h, spec))
        else:
            ok, msg = check_claim(c, pb, pa, pl, png, w, h, request)
        measured.append(("ok   " if ok else "FAIL ") + msg)
        if not ok:
            problems.append(msg)
    problems += measured_intent(request, claims, pb, pa)
    words = _unquoted(request)
    if (_page_rule(before) != _page_rule(after) or _alive_block(before) != _alive_block(after)) \
            and re.search(COLOUR_WORDS, words):
        # A RECOLOUR KEEPS THE SKY'S SHAPE AND ITS LIGHT, unless the words ask otherwise.
        if not re.search(RESHAPE_WORDS, words):
            rebuilt = shape_kept(before, after)
            if rebuilt:
                problems.append(rebuilt)
            else:
                measured.append("ok   every glow layer kept its size, position, stops and transparency — only the "
                                "colours changed")
        if not problems and not re.search(TONE_WORDS, words):
            sa, sb = _shot(before, workdir, w, h, "before"), _shot(after, workdir, w, h, "after")
            if sa is None or sb is None:
                problems.append("the light of the recolour could not be measured — UNVERIFIED")
            else:
                boxes = [(e["x"], e["y"], e["w"], e["h"]) for i, e in pa["els"].items()
                         if i != "bg" and e["w"] * e["h"] < 0.6 * w * h]
                kept, words_ = tone_kept(sa, sb, boxes, w, h)
                measured.append(("ok   " if kept else "FAIL ") + words_)
                if not kept:
                    problems.append(words_)
    if not problems and not (new_ids or acted_on or asserted) and not any(
            pa["els"].get(i) != pb["els"].get(i) for i in named) and not any(
            c.get("kind") in ("changes_over_time",) + INTERACT_KINDS for c in claims):
        problems.append("nothing measurable changed on the elements you named")
    if not problems and "bg" not in named:
        # ON SCREEN, EVERYTHING ELSE IS UNCHANGED: two renders, and every pixel outside the
        # named elements (old and new places, with their shadows) must match. A host only
        # gets behaviour, so its own pixels at rest are held too.
        boxes = []
        # The named elements and their reach — and ALSO everything this page animates on its
        # own. A typing headline redraws itself whatever anyone changes; its pixels answer a
        # question nobody asked. Measured before it is trusted, never assumed.
        for i in ((named | carried) - hosts) | restless():
            for snap in (pb, pa):
                e = snap["els"].get(i)
                if e:
                    r = _reach(e.get("st"))
                    boxes.append((e["x"] - r, e["y"] - r, e["w"] + 2 * r, e["h"] + 2 * r))
        bad = pixels_outside(before, after, boxes, workdir, w, h)
        noise_known = False
        if bad is not None and len(bad) > 6:
            # THE NOISE FLOOR IS MEASURED, NOT ASSUMED. A real template does not render twice
            # alike — measured on a Webflow migration, two renders of the UNCHANGED page differ
            # at every canvas size — so "these pixels changed" mixes the change with the
            # renderer's mood. The same page against itself says WHERE it is unreliable, and
            # only pixels that changed for real AND held still on their own are evidence.
            # Same rule as element drift, applied to the screen.
            noise = pixels_outside(before, before + "<!-- a second look -->", boxes,
                                   workdir, w, h)
            noise_known = noise is not None
            if noise:
                near = set()
                for x, y in noise:                      # a noisy pixel taints its neighbourhood
                    for dx in (-2, 0, 2):
                        for dy in (-2, 0, 2):
                            near.add((x + dx, y + dy))
                real = [p for p in bad if p not in near]
                if len(real) < len(bad):
                    measured.append(f"{len(bad) - len(real)} of {len(bad)} changed pixel(s) sit "
                                    f"where this page does not draw the same way twice, and are "
                                    f"not counted as evidence")
                bad = real
        if bad is None:
            problems.append("the on-screen check could not render — UNVERIFIED")
        elif len(bad) > 6 and not noise_known and _unstable_screen(
                before, boxes, workdir, w, h, len(bad)):
            # THE INSTRUMENT IS CHECKED BEFORE THE CHANGE IS BLAMED. Measured 2026-09-15: a live
            # run reported 97,300 pixels changed outside a 20px-wider button, and replaying that
            # exact patch afterwards changed 0. A second render of the SAME page says whether
            # these two renders were comparable at all — if they were not, the count proves
            # nothing, and refusing on it would blame a change for the renderer's mood.
            problems.append("the screen could not be compared — two renders of the SAME page differ, "
                            "so what changed cannot be told from what merely drew differently "
                            "— UNVERIFIED")
        elif len(bad) > 6:
            xs, ys = [p[0] for p in bad], [p[1] for p in bad]
            hit = {}
            for x, y in bad:
                for i, e in pa["els"].items():
                    if i != "bg" and kinds.get(i) not in ("ground", "stroke") and e["w"] * e["h"] < 0.6 * w * h \
                            and e["x"] <= x < e["x"] + e["w"] and e["y"] <= y < e["y"] + e["h"]:
                        hit[i] = hit.get(i, 0) + 1
            where = ", ".join(describe(pb, before, i) for i, _ in sorted(hit.items(), key=lambda t: -t[1])[:3])
            problems.append(f"about {len(bad) * 4} pixels changed outside what was named, around x "
                            f"{min(xs)}-{max(xs)}, y {min(ys)}-{max(ys)} — on {where or 'the background'}")
        else:
            measured.append("ok   on screen, nothing outside the named elements changed")
    ab, aa = _alive_block(before), _alive_block(after)
    if not problems and ab and not aa and not re.search(STOP_MOTION, _unquoted(request)):
        problems.append("the background was moving and no longer is — the request did not ask to stop it")
    elif not problems and aa and (aa != ab or _page_rule(before) != _page_rule(after)):
        pr = AE.prove_alive(after, workdir, w, h)
        measured.append(f"moving background: {pr['verdict']}, frame 0 matches the still page on "
                        f"{pr.get('frame0_matches_still')}%, half-cycle change {pr.get('half_cycle_mean_change')}")
        if pr["verdict"] != "PASS":
            problems.append("moving background: " + (pr.get("why") or "could not be measured — UNVERIFIED")
                            + " (the .page rule and the data-ae-alive block must carry the same gradient)")
    return measured, problems


def change(html, request, workdir, budget_usd=0.15, call=None, retries=2):
    ledger = {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "stopped": None}
    if call is None:
        import aethron_build as B

        def call(prompt):
            return B.gemini_text(prompt, ledger, budget_usd, max_tokens=12000)
    base = {"verdict": "REFUSED", "html": html, "measured": [], "problems": [], "attempts": [],
            "note": "", "ledger": ledger, "targets": []}
    canvas = AE.manifest(html)["canvas"]
    w, h = canvas.get("w"), canvas.get("h")
    if not w:
        return {**base, "problems": ["the page's canvas could not be read"]}
    pb = probe(html, workdir, w, h, 1200)
    if not pb or not pb.get("els"):
        return {**base, "verdict": "NOT MEASURABLE",
                "problems": ["Aethron could not measure this page, so it will not change it"]}
    kinds = {e["id"]: e["kind"] for e in AE.manifest(html)["elements"]}
    listing = []
    for i, e in pb["els"].items():
        row = {"id": i, "tag": e.get("tag"), "kind": kinds.get(i, "ground" if i == "bg" else "?"),
               "x": e["x"], "y": e["y"], "w": e["w"], "h": e["h"], "color": e["color"], "background": e["bg"]}
        if i != "bg" and e["text"]:
            row["text"] = e["text"][:80]
        holds = AE.riders(html, i) if i != "bg" else []
        if holds:
            row["holds"] = holds
        listing.append(row)
    first = (CODE_PROMPT + "\n\nTHE USER ASKED FOR:\n" + request + "\n\nTHE ELEMENTS (measured):\n"
             + "\n".join(json.dumps(r) for r in listing) + colour_guidance(html, request)
             + "\n\nTHE SOURCE:\n" + html)
    prompt = first
    for attempt in range(retries + 1):
        try:
            reply = call(prompt)
        except Exception as why:
            base["problems"] = [f"could not ask the model: {why}"]
            # NOT ASKED on ANY attempt: a provider that stopped answering halfway is still
            # a request nobody judged, and REFUSED would blame the person who asked.
            base["verdict"] = "NOT ASKED"
            break
        plan = _parse_plan(reply)
        problems, measured, after = [], [], html
        if plan is None:
            problems = ["the reply was not the JSON asked for"]
        else:
            claims = [c for c in plan.get("expect") or [] if isinstance(c, dict)]
            unsafe = _unsafe(plan)
            if unsafe:
                problems.append("the code uses what pages may not: " + ", ".join(unsafe))
            if not claims:
                problems.append("no claims were given, so nothing could be measured")
            problems += intent_gaps(request, claims, pb)
            problems += unmeasured_touches(html, plan, claims, pb=pb)
            after, patch_errors = apply_plan(html, plan)
            problems += patch_errors
            agree = None if patch_errors else copies_agree(html, after)
            if agree:
                problems.append(agree)
            if not problems:
                measured, problems = verify(html, after, plan, pb, workdir, w, h, request)
        base["attempts"].append({"note": (plan or {}).get("note", ""), "plan": plan, "measured": measured,
                                 "problems": problems})
        if plan is not None and not problems:
            targets = sorted({t for t in plan.get("touches") or [] if isinstance(t, str)})
            return {**base, "verdict": "APPLIED", "html": after, "measured": measured, "problems": [],
                    "note": plan.get("note", ""), "plan": plan,
                    "targets": [describe(pb, html, t) for t in targets]}
        base["measured"], base["problems"] = measured, problems
        prompt = (first + "\n\nYOUR LAST PLAN WAS UNDONE:\n" + (json.dumps(plan)[:4000] if plan else (reply or "")[:2000])
                  + "\n\nAethron measured:\n- " + "\n- ".join(measured or ["(nothing — it failed before rendering)"])
                  + "\n\nIT FAILED BECAUSE:\n- " + "\n- ".join(problems)
                  + "\n\nWrite a new plan against THE SOURCE above that fixes exactly these problems.\n")
    base["html"] = html
    return base


def report(result):
    lines = [f"VERDICT: {result['verdict']}"]
    if result.get("note"):
        lines.append(f"  model: {result['note']}")
    if result.get("targets"):
        lines.append("  changed: " + "; ".join(result["targets"]))
    for m in result.get("measured", []):
        lines.append("  " + m)
    for p in result.get("problems", []):
        lines.append("  NOT DONE: " + p)
    led = result.get("ledger", {})
    lines.append(f"  attempts {len(result.get('attempts', []))} · calls {led.get('calls', 0)} · "
                 f"${led.get('usd', 0):.4f}")
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

    page = '<html><head></head><body><main data-ae-id="bg"><div data-ae-id="s00" style="height:245px"></div></main></body></html>'
    print("── patches land exactly as counted, or not at all")
    out, errs = apply_plan(page, {"patches": [{"find": "height:245px", "replace": "height:220.5px"}]})
    check("an exact patch lands", "height:220.5px" in out and not errs)
    _, errs = apply_plan(page, {"patches": [{"find": "height: 245px", "replace": "x"}]})
    check("a find that is not in the source is refused", errs and "0 time" in errs[0], str(errs))
    _, errs = apply_plan(page + page, {"patches": [{"find": "height:245px", "replace": "x"}]})
    check("a find that appears twice is refused when one was declared", errs and "2 time" in errs[0], str(errs))
    out, errs = apply_plan(page + page, {"patches": [{"find": "height:245px", "replace": "height:9px", "count": 2}]})
    check("  ...and lands everywhere when two were declared", out.count("height:9px") == 2 and not errs, str(errs))
    _, errs = apply_plan(page, {})
    check("a plan that proposes nothing is refused", errs == ["no change was proposed"])

    print("\n── code that pages may not run is refused")
    check("fetch is refused", _unsafe({"js": "fetch('/x')"}))
    check("an external script is refused", _unsafe({"patches": [{"find": "</body>", "replace": '<script src="https://x.io/a.js"></script></body>'}]}))
    check("navigation is refused", _unsafe({"js": "location.href='/x'"}))
    check("a protocol-relative image is refused", _unsafe({"css": ".a{background:url(//x.io/p.png)}"}))
    check("a typing loop is allowed", not _unsafe({"js": "setInterval(function(){t.placeholder='hi'},900)"}))
    check("a Google Fonts link already in the source may be kept",
          not _unsafe({"patches": [{"find": '<link href="https://fonts.googleapis.com/x">', "replace": '<link href="https://fonts.googleapis.com/x"><b>'}]}))

    print("\n── the owner's three requests demand the right claims")
    height = "Can you reduce this chat box height a little bit?"
    check("'reduce this chat box height' needs a size claim", intent_gaps(height, [{"kind": "color"}]))
    check("  ...a -10% height claim satisfies it",
          not intent_gaps(height, [{"kind": "size", "id": "s00", "dimension": "height", "change": "-10%"}]))
    check("  ...a +10% claim does not — the request said reduce",
          intent_gaps(height, [{"kind": "size", "id": "s00", "dimension": "height", "change": "+10%"}]))
    check("  ...a width claim does not — the request said height",
          intent_gaps(height, [{"kind": "size", "id": "s00", "dimension": "width", "change": "-10%"}]))
    typing = "Make the chat box automatically type different texts, one after another"
    check("'type different texts' needs changes_over_time, not a fixed text",
          intent_gaps(typing, [{"kind": "text"}]) and not intent_gaps(typing, [{"kind": "changes_over_time"}]))
    orange = "Change the background animation to orange blending with black"
    check("'orange blending with black' needs a colour claim", intent_gaps(orange, [{"kind": "size"}]))
    check("  ...naming both colours",
          intent_gaps(orange, [{"kind": "colors", "families": ["orange"]}])
          and not intent_gaps(orange, [{"kind": "colors", "families": ["orange", "black"]}]))
    check("'remove the background animation' does not demand a removed element",
          all("removed" not in g for g in intent_gaps("remove the background animation", [])))
    check("quoted words are content, not instructions",
          not intent_gaps('change the placeholder to "Type in orange"', [{"kind": "text"}]))
    check("'make the background move like it is alive' is not a position request",
          not any("position" in g for g in intent_gaps("make the background move like it is alive", [])))

    print("\n── colours are named by measurement")
    check("orange is orange", family(255, 120, 0) == "orange")
    check("a dark orange in a blend is still orange", family(90, 40, 8) == "orange")
    check("near-black is black", family(10, 8, 6) == "black")
    check("the site's blue is blue", family(78, 153, 238) == "blue")

    print("\n── claims are measured, and a lie is caught")
    el = {"x": 342, "y": 395, "w": 729, "h": 244, "text": "", "color": "rgb(0, 0, 0)", "bg": "x", "bgi": "none",
          "vis": "visible", "disp": "block", "op": "1", "lcolor": None, "st": "a"}
    pb = {"els": {"s00": dict(el)}}
    pa = {"els": {"s00": dict(el, h=219.6)}}
    claim = {"kind": "size", "id": "s00", "dimension": "height", "change": "-10%"}
    good = check_claim(claim, pb, pa, None, None, 1414, 858)
    check("a 10% shorter box measures as -10%", good[0], good[1])
    lie = check_claim(claim, pb, pb, None, None, 1414, 858)
    check("a box claimed 10% shorter that did not change is caught", not lie[0] and "244.0 -> 244.0" in lie[1], lie[1])
    zero = check_claim(dict(claim, change="0%"), pb, pa, None, None, 1414, 858)
    check("a claim of no change cannot satisfy a request for change", not zero[0])
    big = {"els": {"s00": dict(el, h=146.4)}}
    check("'a little' that is really -40% is caught",
          any("a little" in p for p in measured_intent(height, [dict(claim, change="-40%")], pb, big)))
    wide = {"els": {"s00": dict(el, h=219.6, w=650)}}
    check("only the height was asked for; a width that moved too is caught",
          any("width also changed" in p for p in measured_intent(height, [claim], pb, wide)))
    moved = {"els": {"s00": dict(el, x=300)}}
    check("an element that moved without being named is caught", identity(pb, moved, set(), 1414, 858))
    check("  ...and allowed when it was named", not identity(pb, moved, {"s00"}, 1414, 858))
    check("  ...and allowed to move when it rides on a named element", not identity(pb, moved, set(), 1414, 858, {"s00"}))
    restyled = {"els": {"s00": dict(el, x=300, st="b")}}
    check("  ...but a rider that is also restyled is caught", identity(pb, restyled, set(), 1414, 858, {"s00"}))

    print("\n── a change is inherited by what is inside it: one change, seen twice")
    # A nav link (a002) with the words in a div inside it (a003). Recolouring the link
    # recolours the div, because that is what inheritance is.
    lb = {"els": {"a002": dict(el, color="rgb(207, 227, 255)"),
                  "a003": dict(el, color="rgb(207, 227, 255)", up="a002"),
                  "a009": dict(el, color="rgb(207, 227, 255)", up=None)}}
    la = {"els": {"a002": dict(el, color="rgb(0, 128, 0)"),
                  "a003": dict(el, color="rgb(0, 128, 0)", up="a002"),
                  "a009": dict(el, color="rgb(207, 227, 255)", up=None)}}
    check("the words inside a recoloured link are not a second, unnamed change",
          not identity(lb, la, {"a002"}, 1414, 858, inheriting={"a002"}))
    check("  ...and with nothing claimed it is still caught",
          identity(lb, la, {"a002"}, 1414, 858))
    elsewhere = {"els": dict(la["els"], a009=dict(el, color="rgb(255, 0, 0)", up=None))}
    check("  ...an element OUTSIDE the claim is still caught",
          any("a009" in p for p in identity(lb, elsewhere, {"a002"}, 1414, 858,
                                            inheriting={"a002"})))
    own_bg = {"els": dict(la["els"], a003=dict(el, color="rgb(0, 128, 0)", bg="red", up="a002"))}
    check("  ...and a child that changed something NOT inherited is still caught",
          any("a003" in p for p in identity(lb, own_bg, {"a002"}, 1414, 858,
                                            inheriting={"a002"})))
    deep = {"els": {"a002": dict(el), "a003": dict(el, up="a002"), "a004": dict(el, up="a003")}}
    check("inheritance is followed all the way up, not one step",
          inherits_from(deep, "a004", {"a002"}) == "a002")
    check("  ...and a loop in the chain cannot hang it",
          inherits_from({"els": {"x": dict(el, up="y"), "y": dict(el, up="x")}}, "x", {"z"}) is None)
    colour_claim = [{"kind": "color", "id": "a002", "property": "text", "to": "#008000"}]
    nav = ('<main data-ae-id="bg"><a data-ae-id="a002" style="left:0px;top:0px;width:80px;height:20px">'
           '<div data-ae-id="a003" style="left:0px;top:0px;width:80px;height:20px">About</div></a>'
           '<a data-ae-id="a009" style="left:200px;top:0px;width:80px;height:20px">Careers</a></main>')
    check("naming the words inside a recoloured link is honest, not a licence",
          not unmeasured_touches(nav, {"touches": ["a002", "a003"]}, colour_claim, pb=lb))
    check("  ...but naming something it does not contain is still refused",
          unmeasured_touches(nav, {"touches": ["a002", "a009"]}, colour_claim, pb=lb))

    print("\n── a page that moves by itself is not evidence about a change")
    tick = {"els": {"s00": dict(el), "t01": dict(el, x=100)}}
    moved_on = {"els": {"s00": dict(el), "t01": dict(el, x=140)}}
    check("a ticker that slides on its own is refused when nobody knows it drifts",
          any("t01" in p for p in identity(tick, moved_on, set(), 1414, 858)))
    check("  ...and excluded once the page's own drift has been measured",
          not identity(tick, moved_on, set(), 1414, 858, drifting={("t01", "x")}))
    check("  ...but a DIFFERENT element moving is still caught",
          any("s00" in p for p in identity(tick, {"els": {"s00": dict(el, y=90),
                                                          "t01": dict(el, x=140)}},
                                           set(), 1414, 858, drifting={("t01", "x")})))
    check("  ...and drift on one property does not excuse another",
          any("t01" in p for p in identity(tick, {"els": {"s00": dict(el),
                                                          "t01": dict(el, x=140, bg="red")}},
                                           set(), 1414, 858, drifting={("t01", "x")})))

    print("\n── a place the element already occupied is not somewhere it was pushed")
    tick_b = {"els": {"s00": dict(el), "m01": dict(el, x=1500, w=200)}}     # already past the edge
    tick_a = {"els": {"s00": dict(el), "m01": dict(el, x=1560, w=200)}}
    check("a marquee that always overflowed is not 'pushed off the page'",
          not any("outside" in p for p in identity(tick_b, tick_a, set(), 1414, 858,
                                                   drifting={("m01", "x")})))
    push_b = {"els": {"s00": dict(el, x=10, w=100)}}
    push_a = {"els": {"s00": dict(el, x=1400, w=100)}}
    check("  ...but something moved from inside to outside still is",
          any("outside" in p for p in identity(push_b, push_a, {"s00"}, 1414, 858)))

    print("\n── a fault the page already had is not the change's fault")
    broke = {"els": {"s00": dict(el)}, "errors": ["Uncaught ReferenceError: gsap is not defined"]}
    still = {"els": {"s00": dict(el)}, "errors": ["Uncaught ReferenceError: gsap is not defined"]}
    check("a template's own broken script does not refuse a correct change",
          not identity(broke, still, set(), 1414, 858))
    worse = {"els": {"s00": dict(el)},
             "errors": ["Uncaught ReferenceError: gsap is not defined", "TypeError: x is not a function"]}
    check("  ...but an error the change introduced still does",
          any("TypeError" in p for p in identity(broke, worse, set(), 1414, 858)))

    print("\n── overlaps and reach are measured, not guessed")
    kinds = {"t08": "input", "s02": "button", "r07": "rule"}
    box_b = {"els": {"t08": dict(el, x=376, y=426, w=661, h=135), "s02": dict(el, x=691, y=578, w=112, h=41),
                     "r07": dict(el, x=648, y=560, w=384, h=1)}}
    box_a = {"els": {"t08": dict(el, x=376, y=426, w=661, h=135), "s02": dict(el, x=691, y=553.6, w=112, h=41),
                     "r07": dict(el, x=648, y=560, w=384, h=1)}}
    got = new_overlaps(box_b, box_a, {"s02"}, kinds)
    check("a button moved up into the text box is named with both edges",
          len(got) == 1 and "s02 now overlaps t08" in got[0] and "553.6" in got[0], str(got))
    inside = {"els": dict(box_a["els"], s02=dict(el, x=691, y=450, w=112, h=41))}
    check("a button that sits wholly inside the box is not an overlap", not new_overlaps(box_b, inside, {"s02"}, kinds))
    check("a shadow's reach is its offset plus its blur",
          _reach("a|b|c|d|e|rgba(82, 105, 159, 0.787) 0px 38px 84px 0px|f") == 126)
    check("no shadow still allows a few pixels of glyph", _reach("a|b|c|d|e|none|f") == 4)

    print("\n── a typing animation is followed letter by letter, and held to the words")

    def typed(s):
        return [s[:i] for i in range(1, len(s) + 1)]
    ask = "type a text, then it disappears and types again, different texts, like a typing animation"
    loop = (["Type something"] + typed("Build an app") + ["Build an", "Build", ""] + typed("Design a site")
            + ["Design", ""] + typed("Build an app"))
    good_t, msg = typing_verdict("t08", loop, "Type something", ask)
    check("typed, cleared, a different text typed, and again: accepted", good_t, msg)
    once_t, msg = typing_verdict("t08", ["Type something"] + typed("Build an app"), "Type something", ask)
    check("one text typed once is refused — different texts, again, were asked",
          not once_t and "fewer than two" in msg and "only once" in msg, msg)
    swap_t, msg = typing_verdict("t08", ["Type something", "Build an app", "Design a site", "Build an app"],
                                 "Type something", "Make the chat box automatically type different texts")
    check("texts swapped whole, never typed, are refused as typing", not swap_t and "nothing was typed" in msg, msg)
    run_on, msg = typing_verdict("t08", ["Type something"] + typed("Build an app") + typed("Design a site"),
                                 "Type something", ask)
    check("a next text that begins without the last one disappearing is refused", not run_on and "never cleared" in msg,
          msg)
    shows_placeholder = (["Type something"] + typed("Build an app") + ["Build", "Type something"]
                         + typed("Design a site") + ["Design", "Type something"] + typed("Build an app"))
    emptied, msg = typing_verdict("t08", shows_placeholder, "Type something", ask)
    check("a field emptied back to its placeholder between phrases counts as cleared", emptied, msg)
    lost, msg = typing_verdict("t08", loop, "Type something", ask, kept=False, is_input=True)
    check("a typing animation that overwrites what a person types into the field is refused",
          not lost and "loses their words" in msg, msg)
    kept_t, msg = typing_verdict("t08", loop, "Type something", ask, kept=True, is_input=True)
    check("  ...and one that leaves their words alone is accepted, and says so", kept_t and "typing is kept" in msg, msg)

    print("\n── naming something as changed is only allowed when it is measured")
    one = '<main class="page" data-ae-id="bg"><div class="sf" data-ae-id="s00" style="left:0px;top:0px;width:9px;height:9px"></div></main>'
    size = [{"kind": "size", "id": "s00", "dimension": "height", "change": "-10%"}]
    dodge = unmeasured_touches(one, {"touches": ["s00", "bg", "r01"]}, size)
    check("naming the background and a rule, with only a size claim, is refused naming both",
          dodge and "bg, r01 were named" in dodge[0], str(dodge))
    check("  ...the claimed element alone is fine", not unmeasured_touches(one, {"touches": ["s00"]}, size))
    sky_claim = [{"kind": "colors", "region": "background", "families": ["orange", "black"]}]
    check("the background may be named when a colors claim measures it",
          not unmeasured_touches(one, {"touches": ["bg"]}, sky_claim))

    print("\n── a moving background's two copies stay one design")
    sky = ('<style>.page{position:relative;width:9px;height:9px;background:rgba(47,70,236,1) }</style>'
           '<style data-ae-alive="x">.page{background:rgba(47,70,236,1);animation:a 9s}</style>')
    half = sky.replace("rgba(47,70,236,1) }", "rgba(255,122,24,1) }")
    both = sky.replace("rgba(47,70,236,", "rgba(255,122,24,")
    gap = copies_agree(sky, half)
    check("recolouring only the still copy is named, with the colours on each side",
          gap and "only in .page: rgb(255, 122, 24)" in gap and "only in the moving copy: rgb(47, 70, 236)" in gap,
          str(gap))
    check("  ...recolouring both copies passes", copies_agree(sky, both) is None)

    print("\n── a recolour changes the hue, not the light, and keeps every shape")
    tm = tone_map("rgba(251,249,252,1) rgba(103,181,230,1) rgba(47,70,236,1) rgb(6,5,12)", "orange")
    check("white and near-black keep their light and stay neutral",
          tm[(251, 249, 252)] == (251, 249, 252) and tm[(6, 5, 12)] == (6, 5, 12), str(tm))
    light, deep = tm[(103, 181, 230)], tm[(47, 70, 236)]
    check("light blue becomes an orange of the same lightness",
          family(*light) == "orange" and abs(lch(light)[0] - lch((103, 181, 230))[0]) < 1.5,
          f"{light} lightness {lch(light)[0]:.1f}")
    check("deep blue becomes a deep orange of the same lightness",
          family(*deep) == "orange" and abs(lch(deep)[0] - lch((47, 70, 236))[0]) < 1.5,
          f"{deep} lightness {lch(deep)[0]:.1f}")
    blue_sky = ('.page{background:radial-gradient(ellipse 80% 34% at 48% 109%, rgba(251,249,252,1.000) 0.00%, '
                'rgba(251,249,252,1.000) 43.63%, rgba(251,249,252,0.000) 100.00%),radial-gradient(ellipse 71% 52% at '
                '39% 108%, rgba(103,181,230,1.000) 0.00%, rgba(103,181,230,0.000) 100.00%), rgb(6,5,12)}')
    check("swapping colour values only keeps the shape",
          shape_kept(blue_sky, blue_sky.replace("rgba(103,181,230,", "rgba(230,155,100,")) is None)
    flattened = blue_sky.replace("rgba(251,249,252,1.000) 0.00%, rgba(251,249,252,1.000) 43.63%, "
                                 "rgba(251,249,252,0.000) 100.00%", "rgba(255,102,0,0.8) 0.00%, rgba(255,102,0,0) 100.00%")
    got = shape_kept(blue_sky, flattened) or ""
    check("a glow whose plateau was replaced by a two-stop fade is refused", "3 stops" in got and "became 2" in got, got)
    dropped = blue_sky.replace("radial-gradient(ellipse 71% 52% at 39% 108%, rgba(103,181,230,1.000) 0.00%, "
                               "rgba(103,181,230,0.000) 100.00%), ", "")
    got = shape_kept(blue_sky, dropped) or ""
    check("a glow layer dropped from the sky is refused", "2 glow layers became 1" in got, got)

    class Flat:
        def __init__(self, paint):
            self.w, self.h, self.paint = 40, 40, paint

        def rgb(self, x, y):
            return self.paint(y / 39)

    def ramp(t):
        a, b, c = (250, 250, 252), (47, 70, 236), (1, 1, 1)
        s, (p, q) = (t / 0.5, (a, b)) if t < 0.5 else ((t - 0.5) / 0.5, (b, c))
        return tuple(round(p[i] + (q[i] - p[i]) * s) for i in range(3))
    hue = lch(TARGET_REF["orange"])[2]

    def same_light(t):
        L, cc, _ = lch(ramp(t))
        return ramp(t) if cc < 8 else from_lch(L, cc, hue)

    def all_orange(t):
        return tuple(round(v * (1 - t)) for v in (255, 120, 20))
    kept_t, msg = tone_kept(Flat(ramp), Flat(same_light), [], 40, 40, step=1)
    check("the same light in a new hue is kept", kept_t, msg)
    lost_t, msg = tone_kept(Flat(ramp), Flat(all_orange), [], 40, 40, step=1)
    check("a white core turned full orange is refused, naming the brightest glow",
          not lost_t and "brightest glow" in msg, msg)

    print("\n── a pop-up request demands the right claims")
    owner_words = ("can you make the macOS button or the iOS button or all four buttons have a pop-up, like if I "
                   "hover on it or if I click on it, it will pop up a smaller box with an option")
    hov = {"kind": "appears_on", "id": "p03", "trigger": "s03", "on": "hover", "items": 3}
    clk = dict(hov, on="click")
    check("a pop-up request with no appears_on claim is refused",
          any("appears_on" in g for g in intent_gaps(owner_words, [{"kind": "added", "id": "p03"}])))
    check("  ...'hover or click' needs both actions, not just hover",
          any("says click" in g for g in intent_gaps(owner_words, [hov])))
    check("  ...both actions are enough, and 'a smaller box' is not a resize request",
          not intent_gaps(owner_words, [hov, clk]), str(intent_gaps(owner_words, [hov, clk])))
    four = "Give all four buttons a pop-up with options when I hover them"
    check("'all four buttons' with one trigger is refused", any("covers 4" in g for g in intent_gaps(four, [hov])))
    probe_like = {"els": {"s03": {"tag": "button", "text": "Mac OS"}, "s04": {"tag": "button", "text": "IOS"}}}
    check("naming 'the IOS button' while the claims open from Mac OS is refused",
          any("'IOS'" in g for g in intent_gaps("Show a pop-up with options when I hover the IOS button", [hov],
                                                probe_like)))
    check("  ...but 'the macOS button or the iOS button' leaves the choice open",
          not any("names the" in g for g in intent_gaps(owner_words, [hov, clk], probe_like)))
    check("a hover colour change needs a hover claim",
          any("says hover" in g for g in intent_gaps("make the Generate button white when I hover it",
                                                     [{"kind": "color", "id": "s01", "equals": "#FFFFFF"}])))

    print("\n── a pop-up is judged the way a person would use it")
    shut_v = {"exists": True, "shown": False, "op": 0, "x": 290, "y": 415, "w": 170, "h": 100, "frac": 1, "items": []}

    def open_v(**k):
        v = {"exists": True, "shown": True, "op": 1, "x": 290, "y": 415, "w": 170, "h": 100, "frac": 1, "clip": None,
             "onTop": True, "cover": None, "pe": "auto", "bg": "rgb(255, 255, 255)", "contrast": 17.9,
             "items": ["Download for Mac", "Apple silicon", "Intel"]}
        v.update(k)
        return v
    toggles = [{"aria-pressed": "false"}, {"aria-pressed": "true"}]
    base_r = {"trigger": True, "clickAttrs": toggles, "actStyle": {"filter": "brightness(1.12)"}}
    good_h = {"trigger": True, "rest": shut_v, "open": open_v(), "gap": 4, "reachable": True, "stay": open_v(),
              "closed": shut_v, "clickAttrs": toggles, "actStyle": {"filter": "brightness(1.12)"}, "errors": []}
    tbox = (290, 373, 96, 38)
    ask_h = "Show a pop-up with options when I hover the Mac OS button"

    def judged(res, claim=hov, words=ask_h):
        return judge_popup(claim, res, base_r, words, tbox)
    fine, msg = judged(good_h)
    check("hidden, opens next to it, reachable, stays, closes, toggle kept: accepted", fine, msg)
    check("  ...and the report says what a person saw", "stays open with the pointer on it" in msg
          and "3 option(s)" in msg, msg)
    check("already showing before anyone hovers is refused", "already shows" in judged(dict(good_h, rest=open_v()))[1])
    check("a pop-up that closes while the pointer crosses the gap is refused",
          "cannot reach" in judged(dict(good_h, reachable=False, gap=12))[1])
    check("a pop-up that closes once the pointer is on it is refused",
          "closes when the pointer moves onto it" in judged(dict(good_h, stay=shut_v))[1])
    check("a pop-up cut off by the page is refused, naming what clips it",
          "cut off: 40%" in judged(dict(good_h, open=open_v(frac=0.4, clip="bg")))[1])
    check("a pop-up behind the card is refused, naming the card",
          "covered by s00" in judged(dict(good_h, open=open_v(onTop=False, cover="s00")))[1])
    check("a pop-up 200px from its button is refused", "away from s03" in judged(dict(good_h, open=open_v(y=620)))[1])
    check("three options claimed but two there is refused",
          "holds 2" in judged(dict(good_h, open=open_v(items=["a", "b"])))[1])
    check("unreadable options are refused", "hard to read" in judged(dict(good_h, open=open_v(contrast=1.9)))[1])
    check("a pop-up that stays open after the pointer leaves is refused",
          "stays open after" in judged(dict(good_h, closed=open_v()))[1])
    broken = dict(good_h, clickAttrs=[{"aria-pressed": "false"}, {"aria-pressed": "false"}])
    check("a pop-up that stops its button's own toggle is refused",
          "no longer does what it did: aria-pressed" in judged(broken)[1])
    check("a hover look changed on the trigger is refused",
          "no longer looks as it did" in judged(dict(good_h, actStyle={"filter": "none"}))[1])
    good_c = {"trigger": True, "rest": shut_v, "open": open_v(), "clickAttrs": toggles, "esc": open_v(),
              "beforeOutside": open_v(), "outside": shut_v, "beforeToggle": open_v(), "toggle": open_v(),
              "actStyle": {"filter": "brightness(1.12)"}, "errors": []}
    fine, msg = judged(good_c, clk, "Show a pop-up with options when I click the Mac OS button")
    check("a click pop-up that closes with a click outside is accepted, and says how", fine and "a click outside" in msg,
          msg)
    stuck = dict(good_c, outside=open_v(), toggle=open_v(), esc=open_v())
    check("a click pop-up that can never be closed is refused",
          "cannot be closed" in judged(stuck, clk, "Show a pop-up when I click the Mac OS button")[1])
    check("an unmeasured step never counts as closed", not _closed(None) and not _closed({}))

    print("\n── an element that only gets behaviour must not change, and nothing may be covered")
    host_b = {"els": {"s03": dict(el, tag="button", own="Mac OS", text="Mac OS", hit=True),
                      "s04": dict(el, tag="button", own="IOS", text="IOS", hit=True)}}
    host_a = {"els": {"s03": dict(el, tag="button", own="Mac OS ▾", text="Mac OS ▾", hit=True),
                      "s04": dict(el, tag="button", own="IOS", text="IOS", hit=False)}}
    bad = identity(host_b, host_a, {"s03"}, 1414, 858, hosts={"s03"})
    check("a trigger whose own words changed is refused even though it was named",
          any(p.startswith("s03 words changed") for p in bad), str(bad))
    check("a button an invisible layer now covers is refused", any("s04 can no longer be clicked" in p for p in bad),
          str(bad))
    check("a trigger may be named as touched when a claim acts on it",
          not unmeasured_touches(one, {"touches": ["s00", "p00"]},
                                 [{"kind": "appears_on", "id": "p00", "trigger": "s00", "on": "hover"}]))
    print(f"\nchange selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if len(argv) < 2:
        print('usage: aethron_change.py <page.html> "<what to change>" [--out out.html] [--budget 0.15]')
        return 2
    page, words = Path(argv[0]), argv[1]
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else page
    budget = float(argv[argv.index("--budget") + 1]) if "--budget" in argv else 0.15
    work = Path(tempfile.mkdtemp(prefix="ae-change-"))
    result = change(page.read_text(), words, work, budget_usd=budget)
    print(report(result))
    if result["verdict"] == "APPLIED":
        out.write_text(result["html"])
        print(f"  written: {out}")
    out.with_suffix(".change.json").write_text(json.dumps(
        {k: v for k, v in result.items() if k != "html"}, indent=2, default=str))
    shutil.rmtree(work, ignore_errors=True)
    return 0 if result["verdict"] == "APPLIED" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

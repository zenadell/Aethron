(function(){
/* Phones have no hover. A project card's deck spreads on hover on a desktop;
   on a touch screen it spreads by itself while the card sits in the middle of
   the screen, and folds back once it scrolls away. Framer's motion code reacts
   to pointer events whatever their source, so the same animation plays. */
var touch=matchMedia('(hover: none)').matches;
var seen=new WeakSet(),io=null;
function fire(el,names){names.forEach(function(n){var E=n.indexOf('pointer')===0?PointerEvent:MouseEvent;
  el.dispatchEvent(new E(n,{bubbles:n.slice(-4)==='over'||n.slice(-3)==='out',pointerType:'mouse',isPrimary:true,pointerId:1}))})}
if(touch&&'IntersectionObserver' in window){
  io=new IntersectionObserver(function(es){es.forEach(function(e){
    var a=e.target;
    if(e.isIntersecting&&!a.__aeOn){a.__aeOn=1;fire(a,['pointerover','pointerenter','mouseover','mouseenter'])}
    else if(!e.isIntersecting&&a.__aeOn){a.__aeOn=0;fire(a,['pointerout','pointerleave','mouseout','mouseleave'])}
  })},{rootMargin:'-30% 0px -30% 0px'});
}
/* The album switcher moves to the next album every 7 seconds while it is on
   screen. A tap on a tab restarts the wait; a mouse resting on it pauses. */
var WAIT=7000,groups=[];
function visible(e){return e.getClientRects().length>0}
function tap(e){var r=e.getBoundingClientRect(),o={bubbles:true,cancelable:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2,
  pointerId:1,isPrimary:true,pointerType:'mouse',button:0};
  e.dispatchEvent(new PointerEvent('pointerdown',o));e.dispatchEvent(new PointerEvent('pointerup',o));e.dispatchEvent(new MouseEvent('click',o))}
function bindGroup(g){
  var st={g:g,t:Date.now(),hold:false,inview:false};groups.push(st);
  g.addEventListener('pointerdown',function(e){if(e.isTrusted)st.t=Date.now()},true);
  // the switcher and the album it shows: the section counts as on screen
  // when either part of it is (on a phone the card alone fills the screen).
  var box=g;while(box.parentElement&&!box.querySelector('[data-framer-name="Album Card"]'))box=box.parentElement;
  var card=box.querySelector('[data-framer-name="Album Card"]');
  [g,card].forEach(function(el){if(!el)return;
    el.addEventListener('pointerenter',function(e){if(e.isTrusted&&e.pointerType==='mouse')st.hold=true});
    el.addEventListener('pointerleave',function(e){if(e.isTrusted&&e.pointerType==='mouse'){st.hold=false;st.t=Date.now()}})});
  if('IntersectionObserver' in window)new IntersectionObserver(function(es){es.forEach(function(e){
    if(e.isIntersecting&&!st.inview)st.t=Date.now();st.inview=e.isIntersecting})},{threshold:0,rootMargin:'-15% 0px -15% 0px'}).observe(box);
  else st.inview=true;
}
function scan(){
  if(io)[].forEach.call(document.querySelectorAll('[data-framer-name="Project Card"] > a'),function(a){
    if(!seen.has(a)){seen.add(a);io.observe(a)}});
  [].forEach.call(document.querySelectorAll('[data-framer-name="Album Tabs"]'),function(g){
    if(!seen.has(g)){seen.add(g);bindGroup(g)}});
  groups=groups.filter(function(s){return s.g.isConnected});
}
setInterval(function(){
  scan();
  groups.forEach(function(s){
    if(!s.inview||s.hold||document.hidden||!visible(s.g)||Date.now()-s.t<WAIT)return;
    var tabs=[].filter.call(s.g.querySelectorAll('[data-framer-name="Album Tab"]'),visible);
    if(tabs.length<2)return;
    var i=tabs.findIndex(function(t){var f=t.firstElementChild;return f&&f.getAttribute('data-framer-name')==='Active'});
    var next=tabs[(i+1)%tabs.length].firstElementChild;
    if(next){tap(next);s.t=Date.now()}
  });
},500);
scan();
})();

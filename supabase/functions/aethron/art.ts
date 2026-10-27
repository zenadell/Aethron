// Illustrations for announcements: small, animated, self-contained SVGs in
// Aethron's own style (the same language as the app's What's New cards).
//
// TRUST: an illustration is drawn by a model, so it is treated as untrusted
// text until it passes `checkSvg` — an allow-list of elements, no scripts,
// no event handlers, no outside addresses. It is also only ever shown as an
// <img> (which never runs scripts), so a mistake here is a broken picture,
// never code running in someone's app.

import { HttpError } from "./util.ts";

export const ART_W = 400, ART_H = 206;
const MAX_BYTES = 24_000;

const ALLOWED = new Set([
  "svg", "g", "defs", "style", "title", "desc", "rect", "circle", "ellipse", "line", "path", "polygon", "polyline",
  "lineargradient", "radialgradient", "stop", "clippath", "mask", "pattern", "symbol", "use", "text", "tspan",
  "filter", "fegaussianblur", "feoffset", "feblend", "fecolormatrix", "femerge", "femergenode", "fecomposite",
  "feflood", "fedropshadow", "animate", "animatetransform", "animatemotion", "mpath", "set",
]);

/** Accept an SVG only if it is plainly a picture. Returns it cleaned up, or
 *  throws with every reason it was refused (so a model can fix its drawing). */
export function checkSvg(raw: string): string {
  let s = String(raw || "").trim();
  // a model may wrap it in a code fence or add words around it
  const m = s.match(/<svg[\s\S]*<\/svg>/i);
  if (!m) throw new HttpError(400, "no <svg>…</svg> in the drawing");
  s = m[0];
  const why: string[] = [];
  if (new TextEncoder().encode(s).length > MAX_BYTES) why.push(`it is over ${MAX_BYTES / 1000} KB`);
  if (/<!(doctype|entity)/i.test(s)) why.push("it declares a DOCTYPE or ENTITY");
  if (/<\?/.test(s)) why.push("it has processing instructions");
  if (/\son[a-z]+\s*=/i.test(s)) why.push("it has event handlers (on…=)");
  if (/javascript:|data:text|vbscript:/i.test(s)) why.push("it has a script address");
  if (/@import|expression\s*\(/i.test(s)) why.push("its style imports or runs code");
  // every address must stay inside the drawing (#id); nothing is fetched
  for (const h of s.matchAll(/(?:xlink:)?href\s*=\s*["']([^"']*)["']/gi)) {
    if (!h[1].startsWith("#")) why.push(`it links outside itself (${h[1].slice(0, 40)})`);
  }
  for (const u of s.matchAll(/url\(\s*['"]?([^'")]*)/gi)) {
    if (!u[1].trim().startsWith("#")) why.push(`its style loads an outside address (${u[1].slice(0, 40)})`);
  }
  for (const t of s.matchAll(/<\s*([a-zA-Z][\w:.-]*)/g)) {
    const tag = t[1].toLowerCase();
    if (!ALLOWED.has(tag)) why.push(`<${t[1]}> is not allowed`);
  }
  // with motion turned off a drawing must still be complete: nothing may be
  // hidden at rest and only shown by an animation
  const still = s.replace(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "");
  if (/(?<![-\w])opacity\s*:\s*0(?![.\d])/i.test(still) || /(?<![-\w])opacity\s*=\s*["']0(?:\.0*)?["']/i.test(still)) {
    why.push("something is hidden (opacity 0) until an animation shows it, so with motion off it never appears — " +
      "leave it visible and animate from the hidden state with animation-fill-mode: both");
  }
  if (!/viewBox\s*=\s*["']\s*0\s+0\s+400\s+206\s*["']/i.test(s)) why.push('the root must be viewBox="0 0 400 206"');
  if (why.length) throw new HttpError(422, "the drawing was refused: " + [...new Set(why)].slice(0, 8).join("; "));
  if (!/xmlns=/.test(s.slice(0, 200))) s = s.replace(/<svg/i, '<svg xmlns="http://www.w3.org/2000/svg"');
  return s;
}

// ── the house style, as finished drawings ───────────────────────────
// They are the fallback when no admin AI is set (or it draws something we
// refuse), AND the examples the admin AI is shown, so a model learns the
// style from real work rather than from adjectives.

const BASE = `
.bg{fill:#0f0e10}.fr{fill:#151417;stroke:#2c2a30;stroke-width:1}.tb{fill:#1c1b1f}
.dt{fill:#3a383f}.ln{fill:#2c2a31}.ln2{fill:#3a3740}.acc{fill:#d97757}.acc2{fill:#f0a07f}
.glow{fill:url(#ae-glow)}.ink{fill:#eceaf0}.mute{fill:#8d8995}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}`;

const FRAME = (inner: string, css: string) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 206">
<defs><radialGradient id="ae-glow" cx="50%" cy="0%" r="75%"><stop offset="0" stop-color="#d97757" stop-opacity=".22"/>
<stop offset="1" stop-color="#d97757" stop-opacity="0"/></radialGradient></defs>
<style>${BASE}${css}</style>
<rect class="bg" width="400" height="206"/><rect class="glow" width="400" height="206"/>
<rect class="fr" x="20.5" y="14.5" width="359" height="179" rx="12"/>
<path class="tb" d="M32 15h336a11 11 0 0 1 11 11v10H21V26a11 11 0 0 1 11-11z"/>
<circle class="dt" cx="34" cy="25.5" r="3.2"/><circle class="dt" cx="45" cy="25.5" r="3.2"/><circle class="dt" cx="56" cy="25.5" r="3.2"/>
${inner}</svg>`;

export const TEMPLATES: Record<string, string> = {
  // the team paused Aethron / maintenance
  maintenance: FRAME(`
<rect class="ln" x="36" y="52" width="70" height="6" rx="3"/><rect class="ln" x="36" y="66" width="52" height="6" rx="3"/>
<rect class="ln" x="36" y="80" width="62" height="6" rx="3"/>
<g class="ring"><circle cx="232" cy="108" r="46" fill="none" stroke="#d97757" stroke-opacity=".35" stroke-width="1.5"/></g>
<g class="ring r2"><circle cx="232" cy="108" r="46" fill="none" stroke="#d97757" stroke-opacity=".35" stroke-width="1.5"/></g>
<circle cx="232" cy="108" r="34" fill="#1f1d22" stroke="#3a3740"/>
<g class="bars"><rect class="acc" x="220" y="94" width="8" height="28" rx="3"/><rect class="acc" x="236" y="94" width="8" height="28" rx="3"/></g>
<g class="gear" transform="translate(318 74)"><circle r="11" fill="none" stroke="#8d8995" stroke-width="5" stroke-dasharray="4 3.2"/>
<circle r="4.5" fill="#151417" stroke="#8d8995" stroke-width="2"/></g>
<rect class="ln" x="150" y="168" width="164" height="6" rx="3"/><rect class="acc sweep" x="150" y="168" width="44" height="6" rx="3"/>`,
    `.ring{transform-origin:232px 108px;animation:pulse 2.8s ease-out infinite}.r2{animation-delay:1.4s}
@keyframes pulse{0%{transform:scale(.75);opacity:.9}100%{transform:scale(1.45);opacity:0}}
.bars{transform-origin:232px 108px;animation:breathe 2.8s ease-in-out infinite}
@keyframes breathe{50%{transform:scale(.92)}}
.gear{animation:spin 6s linear infinite}@keyframes spin{to{transform:translate(318px,74px) rotate(360deg)}}
.sweep{animation:sweep 2.4s cubic-bezier(.6,0,.3,1) infinite}@keyframes sweep{0%{transform:translateX(0)}50%{transform:translateX(120px)}100%{transform:translateX(0)}}`),

  // something new to try
  feature: FRAME(`
<path d="M21 36h64v157H32a11 11 0 0 1-11-11z" fill="#131215"/>
<rect class="ln" x="32" y="50" width="40" height="5" rx="2.5"/><rect class="ln" x="32" y="64" width="30" height="5" rx="2.5"/>
<rect class="acc" x="32" y="78" width="36" height="5" rx="2.5" opacity=".9"/>
<g class="card c1"><rect x="104" y="50" width="120" height="66" rx="9" fill="#1f1d22" stroke="#34313a"/>
<rect class="ln2" x="116" y="62" width="64" height="6" rx="3"/><rect class="ln" x="116" y="74" width="88" height="5" rx="2.5"/>
<rect class="ln" x="116" y="85" width="70" height="5" rx="2.5"/><rect class="acc" x="116" y="98" width="34" height="9" rx="4.5"/></g>
<g class="card c2"><rect x="236" y="50" width="124" height="128" rx="9" fill="#1f1d22" stroke="#d97757" stroke-opacity=".55"/>
<rect x="246" y="60" width="104" height="58" rx="6" fill="#2a1d18"/><circle class="acc2" cx="298" cy="89" r="14" opacity=".9"/>
<rect class="ln2" x="246" y="128" width="72" height="6" rx="3"/><rect class="ln" x="246" y="140" width="96" height="5" rx="2.5"/>
<rect class="acc" x="246" y="156" width="48" height="12" rx="6"/></g>
<g class="card c3"><rect x="104" y="126" width="120" height="52" rx="9" fill="#1f1d22" stroke="#34313a"/>
<rect class="ln2" x="116" y="138" width="54" height="6" rx="3"/><rect class="ln" x="116" y="150" width="84" height="5" rx="2.5"/></g>
<g class="spark"><path class="acc2" d="M352 40l3 9 9 3-9 3-3 9-3-9-9-3 9-3z"/></g>`,
    `.card{animation:rise .7s cubic-bezier(.2,.8,.2,1) both}
.c1{animation-delay:.1s}.c2{animation-delay:.35s}.c3{animation-delay:.6s}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.spark{transform-origin:352px 52px;animation:tw 2.2s ease-in-out infinite}
@keyframes tw{0%,100%{transform:scale(.6) rotate(0);opacity:.5}50%{transform:scale(1.1) rotate(45deg);opacity:1}}`),

  // a warning or something people should know now
  alert: FRAME(`
<g class="wave"><circle cx="200" cy="106" r="40" fill="none" stroke="#e8b04b" stroke-opacity=".4" stroke-width="1.5"/></g>
<g class="wave w2"><circle cx="200" cy="106" r="40" fill="none" stroke="#e8b04b" stroke-opacity=".4" stroke-width="1.5"/></g>
<g class="bell"><path d="M200 74c-16 0-26 12-26 28v14l-8 10h68l-8-10v-14c0-16-10-28-26-28z" fill="#2a2418" stroke="#e8b04b" stroke-width="2"/>
<circle cx="200" cy="133" r="6" fill="#e8b04b"/></g>
<rect class="ln" x="40" y="160" width="86" height="6" rx="3"/><rect class="ln" x="40" y="172" width="60" height="6" rx="3"/>
<rect class="ln" x="274" y="160" width="86" height="6" rx="3"/><rect class="ln" x="300" y="172" width="60" height="6" rx="3"/>`,
    `.wave{transform-origin:200px 106px;animation:wv 2.4s ease-out infinite}.w2{animation-delay:1.2s}
@keyframes wv{0%{transform:scale(.8);opacity:1}100%{transform:scale(1.7);opacity:0}}
.bell{transform-origin:200px 76px;animation:ring 2.4s ease-in-out infinite}
@keyframes ring{0%,60%,100%{transform:rotate(0)}66%{transform:rotate(9deg)}72%{transform:rotate(-8deg)}78%{transform:rotate(5deg)}84%{transform:rotate(-3deg)}}`),

  // a new version / an update to install
  update: FRAME(`
<rect x="150" y="62" width="100" height="92" rx="14" fill="#1f1d22" stroke="#34313a"/>
<g class="arrow"><path d="M200 128V84m0 0l-15 15m15-15l15 15" fill="none" stroke="#f0a07f" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></g>
<rect class="ln" x="120" y="170" width="160" height="6" rx="3"/><rect class="acc fill" x="120" y="170" width="160" height="6" rx="3"/>
<rect class="ln" x="40" y="54" width="70" height="6" rx="3"/><rect class="ln" x="40" y="68" width="50" height="6" rx="3"/>
<rect class="ln" x="290" y="54" width="70" height="6" rx="3"/><rect class="ln" x="310" y="68" width="50" height="6" rx="3"/>`,
    `.arrow{animation:up 1.8s cubic-bezier(.5,0,.3,1) infinite}@keyframes up{0%{transform:translateY(8px);opacity:0}30%{opacity:1}70%{opacity:1}100%{transform:translateY(-8px);opacity:0}}
.fill{transform-origin:120px 173px;animation:fill 3.6s ease-in-out infinite}@keyframes fill{0%{transform:scaleX(0)}70%,100%{transform:scaleX(1)}}`),
};

/** Pick the fallback drawing that fits the words. */
export function templateFor(title: string, body: string, level: string): string {
  const t = (title + " " + body).toLowerCase();
  if (/paus|maint|down ?time|fixing|outage|back soon|upgrad(e|ing) (our|the) server/.test(t)) return "maintenance";
  if (/updat|version|release|install|download/.test(t)) return "update";
  if (level === "critical" || level === "warn" || /warn|important|heads.?up|security|deadline|ending|price|billing/.test(t)) return "alert";
  return "feature";
}

export const ILLUSTRATOR_PROMPT = `You are the illustrator for Aethron, a premium desktop app (dark, warm, Apple-quality). You draw ONE small animated illustration for an announcement shown to every user, as a single SVG.

HARD RULES (a drawing that breaks any of them is refused):
- Root: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 206">. Nothing outside it.
- Only these elements: svg g defs style title rect circle ellipse line path polygon polyline linearGradient radialGradient stop clipPath mask pattern symbol use text tspan filter feGaussianBlur feOffset feBlend feColorMatrix feMerge feMergeNode feComposite feFlood feDropShadow animate animateTransform.
- No scripts, no event handlers, no <image>, no <foreignObject>, no external URLs or fonts. href and url() may only point to #ids inside the drawing.
- The drawing must look complete with animations OFF (people who turn motion off see the still frame): never set opacity:0 on an element at rest; animate FROM hidden with animation-fill-mode: both.
- Under 12 KB. Animations are CSS @keyframes inside one <style> (or SMIL), looping, calm (1.6s–6s), ease curves; include @media (prefers-reduced-motion:reduce){*{animation:none!important}}.

STYLE (match the examples exactly in spirit):
- Background #0f0e10 with a soft warm radial glow from the top in #d97757 at ~20% opacity.
- Most pictures sit inside an app-window frame: rounded rect (rx 12) #151417 with a 1px #2c2a30 stroke, a #1c1b1f title bar with three small dots.
- Shapes are calm rounded bars and cards (#1f1d22, #2c2a31, #3a3740), ONE accent family: #d97757 / #f0a07f (warnings may use #e8b04b). Ink #eceaf0 used sparingly.
- One clear focal element in the middle that tells the story (a pause mark, a sparkle on a new card, a bell, an arrow into a box, a rocket, a calendar, a shield…), with ONE or TWO tasteful motions (pulse ring, gentle rise, sweep, slow spin). Never busy, never cartoonish, no emoji, no gradients that are not the house palette.
- Avoid text inside the picture (the card shows the words). If you must, at most one tiny label in system-ui.
- Use unique ids prefixed "ae-".

Answer with the SVG only.`;

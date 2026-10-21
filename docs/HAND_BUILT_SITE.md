# How the hand-built Aethron page was made

*Written 2026-10-21 for teaching Aethron's coding agent.*

temple asked for this record. In October 2026 a Claude session was asked
to put Aethron's site on a Framer template that temple's friend designed
(https://kaviyan.framer.website, used with the designer's permission). It
first REBUILT the template by hand as one static page (`website/` on
branch `claude/project-thread-ws10fv`, PR #31). temple rated the rebuild
about 90% of the original and chose to MIGRATE the real template instead,
since the Framer original has richer detail and smoother motion. The
rebuild was not shipped. temple wants the method kept, because it is a
good worked example of how a coding agent should go from a reference site
to clean, fast code.

Everything below is what was actually done, in order, with the reason for
each choice. The source is `website/index.html`: one file of about 46KB,
with inline CSS and JS, and about 550KB of images.

---

## 0. The lesson from temple's verdict

Rebuilding a reference site by hand reaches about 90%. The last 10% is the
designer's own detail: easing curves, hover states, micro-animations, and
spacing tuned frame by frame. A rebuild can only reach it by guessing.
**When the user owns or is allowed to use the original, migrate it (carry
it) instead of rebuilding.** That is AETHRON.md's rule "CARRY, DON'T
RECREATE". The hand-build is the right tool when there is nothing to carry:
a screenshot, a mock-up, or a brief.

Ask first: "Do I have the original, and may I use it?" If yes, migrate. If
no, rebuild with the method below.

---

## 1. Study the reference before writing anything

1. **Screenshot it at the size people see it** (1440x900), once at rest
   and once as a full page. Headless Chromium through Playwright, using
   `executable_path='/opt/pw-browsers/chromium'` in the cloud container.
   Scroll the page in steps before the full-page shot, or lazy images and
   scroll animations stay empty.
2. **Screenshot the scroll story frame by frame.** Take one viewport shot
   every 500px of scroll and lay them out as a contact sheet (a PIL grid).
   This showed the template's signature effect, which no single screenshot
   reveals: five plush characters start as a row under the headline,
   shrink into inline icons inside a sentence, then fly into a dark "dock"
   panel.
3. **Ask the page what it is made of. Do not guess.**
   - `document.images` with rendered size and `alt`. This found the five
     agents as 1254px PNGs, each with an alt text naming it ("Cut, the
     editing agent"…), and every other picture with its role.
   - `grep 'font-family:"…"'` across the HTML found the typeface
     (Switzer, plus Geist Mono for numbers).
   - `grep` for `.mp4 .webm .glb .splinecode` found none. That proved the
     "3D agents" are pre-rendered PNGs moved by transforms, not live 3D.
     That single fact decided that the rebuild could be light.
   - `data-framer-name` counts gave the section vocabulary.
4. **List the sections and decide what to keep.** The template has about
   20 sections. Kept: nav, hero, scroll story, logo strip, numbers, how it
   works (4 cards), three ways in, an app screenshot, the agents grid,
   pricing, FAQ, the final CTA and the footer. Dropped: team, testimonials,
   blog, gallery and services. They would have needed invented people,
   quotes and projects, and a product site must not fabricate those.

## 2. Find what already exists

- **The current site:** `projects/aethron-site`, a forge migration of a
  Webflow template, deployed by Netlify from `netlify.toml` (base
  `projects/aethron-site`, command `forge build`). Netlify site
  `incandescent-longma-2957f0`, domain `aethron.jomiez.com`. Netlify is
  linked to the GitHub repo, so every PR gets a deploy preview.
- **The newest release:** the app's own updater reads
  `<supabase>/storage/v1/object/public/releases/mac.json`
  (`{"version","url","notes"}`), and the bucket is public. The download
  button should read the same file, so the site and the app can never
  disagree about the latest version.
- **Brand assets:** `brand/mark-white.png`, turned into a dark mark for a
  light page by keeping the alpha and filling with ink (PIL). Also
  `brand/icon-1024.png` for the favicon.
- **A real product screenshot:** the screenshots in `docs/screenshots`
  showed July's UI, so the current studio was started in a throwaway
  `AETHRON_HOME` and photographed. Set `localStorage.forge_tour='done'`
  first, or the walkthrough covers the screen.
- **Facts the copy must not get wrong:** the build runner is
  `macos-latest`, which is arm64, so the app is **Apple silicon only**. The
  beta is unsigned, so the first launch needs right-click → Open
  (docs/DESKTOP.md). Billing is not enforced
  (`release_config.json: enforce_billing:false`), so pricing says "Free
  during the beta".

## 3. Assets: small, local, the right format

| Asset | Source | Treatment |
|---|---|---|
| 5 agents | template PNGs, `?scale-down-to=512` | WebP q82, about 35–58KB each (was about 110KB as PNG) |
| 4 step images | template JPGs | ≤900px, WebP q78, 6–25KB |
| App screenshot | own capture at 2x | 1600px WebP q80, 36KB |
| Font | Switzer 400/500/600 from Fontshare | self-hosted woff2, about 17–20KB each, `font-display:swap`, 500 preloaded |
| OG image | own render of the hero at 1200x630 | JPEG q85 |

Rules that came from this:
- **Self-host fonts.** One less third-party connection, and it works where
  a CSP blocks font CDNs. Preload only the weight that sets the headline.
- **Preload the first agent image.** It is in the first frame.
- **`loading="lazy"` on everything below the fold, with width and
  height set** so lazy images do not shift the layout.
- Delete assets nothing references (two were downloaded and never used).

## 4. Structure: one file, tokens first

- One `index.html` with inline `<style>` and `<script>`. That is one
  request for everything above the fold, with no build step and no
  framework. The old site's lag came from shipping a whole platform
  runtime.
- The style starts with a token block in `:root`: background `#fafafa`,
  ink, muted, line, card, well, night (the dark dock), `--go` (the
  template's green), font stacks, radius and wrap width. Every colour
  below comes from a token. The page commits to one light theme because
  the template is light, so it sets `color-scheme:light` and an explicit
  body background.
- The layout is a centred 1200px column. Sibling groups use grid or flex
  with `gap`, never per-element margins. There is a side gutter of
  `max(16px, env(safe-area-inset-*))`.
- The template's card language is reproduced as one pattern. A white
  "shell" (radius 28, 8px padding, 1px ring shadow) holds inner cards
  (radius 20, ring shadow). Section heads are an eyebrow chip (a small
  black rounded icon tile plus an uppercase label), an h2, and an optional
  right-hand paragraph that wraps under it on narrow screens.
- Semantics: `<header>`, `<nav aria-label>`, a single `<main>`, a single
  `<h1>`, a section per block with `aria-labelledby`, real `<a>`/`<button>`,
  a skip link, `aria-expanded` on the menu button, and `<details>/<summary>`
  for the FAQ (no JS needed). Decorative agent copies are `aria-hidden`.

## 5. The signature: agents that travel as you scroll

This is the part worth copying.

### DOM
```
.story            tall wrapper (gives the scroll distance)
  .stage          sticky, 100svh, overflow hidden
    .scene.hero       h1 + text + .row of 5 .slot (each holds an <img>)
    .scene.sentence   a <p> with 5 inline .slot spans between words
    .scene.dock-scene the dark panel with 5 .slot cells (+ hover tips)
    .fly              5 absolutely positioned <img>, the travellers
```

### It works without JavaScript
Every `.slot` contains its own real `<img>`. With no JS, or with
`prefers-reduced-motion`, the three scenes are ordinary stacked sections,
each showing its own agents. **The page is complete at rest.** JS adds
`.live` to the story, and only then:
- `.story.live` gets height `360vh` and `.stage` becomes sticky.
- The scenes become absolutely stacked layers.
- The slot images become `visibility:hidden`. They keep their size, so
  they still mark where an agent should sit.
- The `.fly` layer appears.

### Measure slots once, without transforms
```js
function pos(el){ let x=0,y=0,n=el;
  while(n && n!==stage){ x+=n.offsetLeft; y+=n.offsetTop; n=n.offsetParent }
  return {x,y,w:el.offsetWidth} }
```
`offsetLeft/offsetTop` ignore CSS transforms, so the scenes can be faded
and slid while their slot positions stay stable. Measure on load, on
`document.fonts.ready` (text reflows when the real font arrives) and on
resize. Never measure on every scroll frame.

### One progress number drives everything
```js
p = clamp(-story.getBoundingClientRect().top / (story.offsetHeight - innerHeight))
a = ease(seg(p,.08,.38))   // hero → sentence
b = ease(seg(p,.56,.86))   // sentence → dock
```
`seg(p,start,end)` maps a window of the scroll to 0..1. The timeline is
hero (0–0.08), travel to the sentence (0.08–0.38), hold while the words
light up one by one (to 0.56), travel to the dock (0.56–0.86), then hold.
The scenes cross-fade with `opacity` and a small `translateY` on the same
progress number.

Each traveller is interpolated between its slot in the scene it leaves
and its slot in the scene it is heading to, with an S-curve
(`easeInOutCubic`). A rotation that peaks mid-flight
(`sin(a·π)·±14°`, alternating per agent) makes the flight feel thrown
rather than slid.

### Transform only, centred
```js
img.style.transform =
  `translate(${x+w/2-100}px,${y+w/2-100}px) rotate(${rot}deg) scale(${w/200})`
```
Each flyer is a fixed 200x200 box with `transform-origin:50% 50%` and
`will-change:transform`. The page only ever writes `transform`, so no
frame triggers layout and the compositor does all the work. (The first
version used `transform-origin:0 0`, which made the rotation swing around
the corner.)

### Idle life without waste
In the hero and the dock, each agent bobs (`sin(t·1.6+i·1.3)·6px`) and
tilts slightly. The rAF loop runs **only** while the story is on screen
(IntersectionObserver) **and** the agents are resting (p<0.12 or p>0.84).
In between, frames are driven by `scroll` events alone (passive listener,
with one rAF per burst). Scroll away and the loop costs nothing.

### Hover in the dock
The flyers have `pointer-events:none`. The slots underneath sit in exactly
the same place and take the hover, so a CSS-only tooltip (`.slot:hover
.tip`) names each agent with no JS hit-testing. Slots have `tabindex=0`,
so keyboard users get the same tips through `:focus-visible`.

### Phone
The same code runs. Slot sizes come from CSS (`clamp(64px,19vw,270px)`),
so the measurements adapt. One change: on phones the hero row sits in
normal flow under the buttons instead of pinned to the bottom of the
screen, which had left a big empty gap.

## 6. The rest of the motion: CSS only, and little of it
- Buttons: a springy `cubic-bezier(.3,1.6,.5,1)` on transform, and
  `scale(.97)` when pressed.
- The menu: opacity, translate and scale with visibility, so the closed
  menu cannot be clicked. It closes on outside click, Escape and link
  click.
- The menu pill shows how far down the page you are (`0%…100%`), updated
  in the same rAF as the story. A detail copied from the template.
- The final CTA's agents bob with a CSS keyframe, staggered by
  `animation-delay`.
- Agent cards lift 3px on hover.
- `prefers-reduced-motion`: no smooth scroll, no bobbing, transitions
  almost instant, and the story is static.

## 7. Download: always the newest release
```js
fetch(FEED,{cache:'no-store'}).then(r=>r.ok?r.json():null).then(j=>{
  if(!j||!/^https:\/\//.test(j.url)) return;
  document.querySelectorAll('a.dl').forEach(a=>a.href=j.url);
  ver.textContent='Version '+j.version+' · Apple silicon · free during the beta';
})
```
- Every download link carries class `dl` and a **hard-coded fallback
  href** (the version current when it was built). If the feed fails, or
  JS is off, Download still works.
- The URL is checked to be `https://` before it is used, because the feed
  is data, not code.
- It reads the same file as the app's updater, so a release published by
  CI shows up on the site within seconds, with no site deploy needed.
- Proven on the Netlify deploy preview: `a.dl.href` came back as
  `…/Aethron-1.1.39-mac.zip`, and the version line updated. Inside a
  claude.ai Artifact preview the fetch is blocked by the CSP, so the
  fallback href shows. That is expected.

## 8. Copy rules that were applied
- Write from the visitor's side, in short active sentences. No em-dash
  asides.
- Use only true numbers: 6 framework targets, 45 test suites per release,
  100% of projects stay on the Mac. No invented user counts.
- Every claim must match the product today: Apple silicon only, the
  unsigned first-launch step, free during beta, bring your own key from
  the listed providers.
- Agent names are short nouns that say the job: Forge (migrate), Lens
  (screenshot to code), Probe (check pages), Mend (fix), Port (framework
  export). A sixth card, "Any model", covers bring-your-own-key.

## 9. SEO and sharing
`<title>`, a meta description, Open Graph title, description, image (an
absolute URL, because crawlers do not resolve relative ones) and type, a
Twitter large-image card, `theme-color`, a favicon and `lang="en"`.

## 10. How it was checked (the loop)
A Playwright script served `website/` with `python3 -m http.server` and:
1. Loaded at **1440x900 and 390x844** (`is_mobile`, `has_touch` for the
   phone).
2. Collected `pageerror` and console errors. Zero.
3. Scrolled the story to p = 0, .15, .3, .45, .62, .75 and .92, took a
   shot at each, and made a contact sheet. **Pitfall:**
   `html{scroll-behavior:smooth}` makes `scrollTo` animate, so a shot taken
   400ms later catches agents mid-flight and looks like a bug. Use
   `scrollTo({top, behavior:'instant'})` in tests.
4. Scrolled the whole page in 500px steps so every lazy image loads, then
   asserted `[...document.images].filter(i=>i.complete&&i.naturalWidth==0)`
   is empty. Zero broken.
5. Checked `document.documentElement.scrollWidth === innerWidth`. No
   sideways scroll.
6. Read the contact sheets by eye. That pass found the footer logo stacked
   (a `display:block` rule beating `.logo{display:flex}`), uneven step
   cards (a flexible figure height), and the empty gap on phones. All were
   fixed in one pass.
7. Published once as an Artifact for a quick look, and once through the
   Netlify deploy preview to prove the download feed and the font on a
   real host.

## 11. Deployment shape
- `netlify.toml`: `base = "website"`, `publish = "."`, `command = "true"`
  (no build), and long cache headers on `/assets/*`.
- The commit carried `[skip ci]` so a website change does not cut a Mac
  release (every push to main runs the release workflow).
- The PR stays a draft until the owner approves going live, because
  replacing a production site faces the public.

## 12. What an agent should take from this
1. Read the reference with instruments (images, fonts, media types,
   contact sheets of scroll states) before writing a line.
2. Decide whether to carry or rebuild. If the original is usable, carry
   it.
3. When rebuilding: tokens first, one file, real semantics, and a page
   complete without JS.
4. Do scroll stories with sticky stages, slots measured with
   `offsetLeft`, one progress number, transform-only movers, and a rAF
   loop that sleeps when nothing is visible.
5. Wire live facts (download URL, version) to the same source the product
   uses, with a hard-coded fallback.
6. Check at two widths, with zero console errors, zero broken images and
   no sideways scroll. Read the screenshots. Test with instant scrolling.
7. Never invent people, quotes or numbers on a product site.

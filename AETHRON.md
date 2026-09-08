# Template Forge — Project Context (auto-loaded every session)

(This folder is named `template-editor` — the owner's chosen home for
the project. "Template Forge" is the tool's working title; same thing.)

## What this is
A standalone CLI (`forge.py`, single file, zero dependencies) that
migrates Framer/Webflow template exports into fully-owned sites —
rebranded text/images/links, no platform badge or telemetry — while
keeping the design and animations pixel-identical. Designed so ANY
model (DeepSeek, Gemini, Claude, small local models) can drive a
migration: the model only fills `copy_map.json`; the tool does every
dangerous mechanical step with hard guardrails.

This is the owner's flagship 2026 project. They design and buy many
Framer templates and this tool is how they make them theirs.

## Read before changing anything
1. `PLAYBOOK.md` — the method: why exports fight back (React hydration,
   3-layer text, CMS byte budgets, range protocol, MIME), the workflow,
   the model prompt, the failure table. The rules in it are law; each
   exists because the naive approach failed during the reference build.
2. `README.md` — usage.

## Status (2026-07-11): v1 complete and proven
- Proven end-to-end against the HandGrid Framer export: fetch pulled
  59 chunks + 4 CMS binaries + 279 icons automatically; inventory found
  156 strings (75 CMS-byte-budgeted), 36 images, 52 links; build+verify
  caught missed brand mentions; browser run: zero failed requests,
  animations intact.
- Reference implementation this generalizes: `~/Desktop/servly-v3`
  (HandGrid → Servly, done manually first; its README documents the
  original battle). Useful as a second test corpus — its
  `template/index.html` is a pristine Framer export.

## Known v1 gaps / roadmap (owner-approved directions)
- Webflow path HARDENED 2026-07-11: init now keeps css/js/images/fonts
  from multi-file exports (was: HTML only — a real Webflow zip would
  have silently lost its assets); build passes them through with copy
  pairs applied to text assets; local `<img src>` files are inventoried.
  Proven against a synthetic 2-page Webflow zip (see test battery
  below), still awaiting a REAL Webflow export for final confirmation.
- ~~Logo/wordmark generator~~ DONE 2026-07-11: `forge.py logo "Brand"`
  discovers the template's @font-face fonts (unicode-range aware),
  downloads one to `pristine/fonts/`, renders the wordmark via
  fontTools SVGPathPen into `assets/`; build now mirrors `assets/`
  into `site/assets/`, and verify checks local image refs exist.
  Only command with a dependency (fonttools + brotli). Tested against
  the servly-v3 reference export: Outfit 700 wordmark rendered
  correctly; subset-glyph and missing-file guards verified.
- Multi-page: synthetic 2-page export proven end-to-end; a real
  multi-page FRAMER export remains untested.
- ~~Web UI~~ DONE 2026-07-11 as `studio.py` — see below.

## Test battery (2026-07-11): 42/42 scenarios green
`scratchpad battery.py` (rewrite if needed — it drives the live studio
API): three-layer hydration equality byte-for-byte; images swapped via
local upload / external URL / query-string variants; multi-byte budget
fills; emoji; tel/mailto/http links; brand tokens + forbidden-word
verify on real HandGrid export; ?range= slice math; .js@ MIME;
rebuild idempotency; site.zip; path traversal + filename traversal +
duplicate name + malformed AI paste + over-budget + backtick guards;
full synthetic Webflow 2-page migration. Browser check: migrated site
renders in studio preview with ZERO console errors / failed requests.
Bugs found & fixed by the battery: (1) brand token derived from
pages[0] which is alphabetically sorted — about.html would yield
"About" as the brand; now prefers index.html; (2) failed init left a
half-created project dir; (3) async tab-render race in studio UI;
(4) viewport/robots meta content offered as fillable strings.

## Studio (2026-07-11): the AI IDE layer — `studio.py`
`python3 studio.py` → http://127.0.0.1:8899. Single stdlib-only file,
same zero-dep ethos. It wraps forge.py as subprocesses (forge stays the
sole authority for every mechanical step — all invariants still hold):
- upload .html/.zip → init; one-click fetch/inventory/build/verify with
  live job logs; projects live in `projects/` (gitignore-worthy).
- Plan tab: owner writes `project_plan.md`; "Fill with AI" calls ANY
  model (Anthropic wire format or OpenAI-compatible: DeepSeek, Gemini,
  OpenAI, Ollama), batched 40 entries/request so small models never
  truncate; server-side guardrails reject over-budget/backtick fills
  with reasons. Manual mode: copy prompt+JSON → paste answer → merge
  (same guardrails) — works with zero API keys.
- Strings tab: live UTF-8 byte-budget meters; Images tab: upload files
  into assets/ + wordmark generator UI (wraps forge logo); Links tab.
- Preview tab: spawns a real `forge.py serve` per project (both Framer
  protocols exact); Download: zip of site/.
- Proven through the API against the HandGrid export: identical numbers
  to the CLI run (59 chunks/4 CMS/279 icons/155 strings), verify CLEAN,
  guardrail rejections confirmed (over-budget + backtick), preview live.
- Bug found & fixed during studio testing: inventory was extracting
  viewport/robots meta `content` as fillable strings (a model filling
  `width=device-width` would break every page) — TextExtract now only
  harvests copy-bearing metas (title/description/og:/twitter:).

## First real-world migration (2026-07-11): NexMind → Jomiez
Owner's own export (`~/Downloads/nexmind_framer_website.html`, 2.6MB,
63 chunks, 6 CMS binaries, 559 copy-map entries). DeepSeek v4-pro
(model id `deepseek-v4-pro`) filled the map live through the studio:
29 fields, 0 guardrail rejections, every Nexmind mention caught incl.
CMS-budgeted logo text (the logo was a TEXT element, no wordmark image
needed). Verify CLEAN; browser: zero console errors/failed requests.
Owner's earlier failed attempt (projects/test-1) diagnosed: their plan
said only "remove buy template badge" — no brand info, so the model
correctly changed nothing, and they never opened Preview/site output.
Hardening that came out of this template:
- telemetry/editorbar strip regexes now tolerate entity-encoded quotes
  (&#34;) and `</script>` on its own line (saved-page-source format);
  verify caught the leftovers first — the loop works.
- HIDE_CSS now hides marketplace checkout links (buy.polar.sh,
  lemonsqueezy, gumroad, framer.com/marketplace) — the "Buy Template"
  pill. Hidden by HREF, so retargeting the link in copy_map makes the
  button legitimately reappear.
- brand tokens strip ®/™/© ("Nexmind®" title must mop up "Nexmind").
- studio DeepSeek preset now defaults to `deepseek-v4-pro`
  (key offers deepseek-v4-flash / deepseek-v4-pro).

## Autonomy test (2026-07-11 evening): Lesmana → Jomiez
Second real export (portfolio template, 53 chunks, 8 CMS, 436 entries),
run hands-off per owner's instruction. Outcome: CLEAN site, zero
console errors/failed requests, logo + signature swapped. What the
test surfaced and the fixes now in the tool:
1. REPLACEMENT ENGINE REWRITE (the big one): sequential str.replace let
   short pairs corrupt longer fills — DeepSeek set 'Design'->'AI' and
   built HTML contained 'AI Workflow AI' and 'Your for'. _apply and the
   CMS byte pass are now a SINGLE-PASS longest-first alternation regex
   with \b guards on single-word pairs (ASCII \w check keeps text and
   bytes layers identical). The old O(n²) collision WARNING is gone —
   the hazard it warned about no longer exists. Battery re-passed 42/42.
2. AI fill resilience: one transient DeepSeek failure used to abort the
   whole job (leaving the lowercase brand token unfilled — verify
   caught the leftover). Now: 3 retries/batch with backoff, failed
   batches skip + report, fills are resumable (only unfilled entries
   are re-sent), job ok=False if any batch failed.
3. Brand marks are IMAGES (wordmark PNG + signature PNG here): no text
   scan can see them — grep proved 'Lesmana' existed in ZERO served
   files while the logo still displayed it. verify now prints a NOTE
   when no image entries were replaced. forge.py logo rendered both
   replacements (Geist 700 wordmark + gold-dot accent; system script
   font for the signature) — owner-quality last mile in ~1 min.
4. _discover_fonts now html.unescape()s @font-face blocks (saved page
   sources entity-encode quotes; labels showed '&#39' as the family).
5. inventory auto-sets forbidden_words from the brand token; studio
   Plan tab now edits forbidden_words + hide_selectors (/api/config).
DeepSeek v4-pro guardrail note: it tried to rewrite a raw CMS rich-text
node 8 bytes over budget — rejected cleanly; that one heading keeps
original text unless the owner shortens it in the Strings tab.

## Visual editor (2026-07-11 night): click-to-edit admin in studio
Owner asked to "point at a thing and change it". Studio now has EDIT
MODE (Preview tab → ✏️): `/edit/<project>/` serves the built site from
the studio origin with a picker overlay injected — hover highlights,
click any text or image → floating panel resolves it to its copy_map
entry (POST /api/entry/resolve; creates an entry with a computed CMS
byte budget if inventory missed it), edits with live budget meter,
Save & rebuild (POST /api/entry/set → build job → iframe reload,
~3s round trip). All changes still flow through copy_map + build.
Wrinkles handled: /edit mount rewrites absolute {pub}/ asset refs in
HTML/CSS AND chunk JS strings (hydration re-renders img srcs from
chunk data; every string-prefix form covered) plus the
${location.origin} CMS/icon bases; .framercms ?range= replicated.
KNOWN QUIRKS: (1) Framer splits sentences across spans — the picker
edits the exact fragment clicked (panel shows the original fragment);
(2) chrome reports naturalWidth 0 for some SVGs that render fine;
(3) one CDN image variant 404s on framerusercontent for the Lesmana
template — pre-existing export artifact, verify against original.
Badge insurance added same session: HIDE_CSS also hides .__framer-badge
class + a[href*="framer.com/r/badge"] (some runtimes create their own
container). Owner's "badge still visible" report was their stale
test-1 build from before the day's fixes — rebuilt, resolved.

## Edit-mode hardening (owner bug report: "old keeps showing / not
## all images editable") — 2026-07-11 late night
Three root causes, all fixed and retested:
1. NO CACHE HEADERS on either server — Chrome heuristically cached
   chunks/html, so rebuilds didn't show without a hard reload. Both
   forge serve and studio /edit now send Cache-Control: no-store.
2. Editor-picked text carries DOM-normalized whitespace; the pristine
   source may be hard-wrapped/&nbsp;'d → exact replace no-op'd
   silently. Picked entries are now created with "flex": true; the
   engine matches them with a whitespace/entity-tolerant pattern
   ((?:\s|&nbsp;|\xa0)+ between words) in text layers, and in CMS
   binaries with PER-OCCURRENCE padding so the size lock still holds.
   (flex is per-entry and editor-created only; inventory entries keep
   exact one-pass semantics.)
3. Picker only saw <img> under the cursor. Now walks the full
   document.elementsFromPoint stack: text/imgs behind transparent
   overlay divs are reachable, and CSS background-image elements are
   picked (second pass, so text in front still wins). Verified live:
   bg-pattern div → "Replace image" panel with upload.
Also: Strings/Images/Links tabs now have "Save & rebuild" (the old
Save-only button was a trap — owners saved and saw nothing change).
Battery re-run after engine change: 42/42.

## Edit-mode round 2 (owner: "images not editable / other pages don't
## change / changed image refuses to show") — 2026-07-12
All three were real; fixes verified live + battery 42/42:
1. URL PADDING BUG (critical, affects any CMS-resident image/link):
   space-padding landed MID-URL because CMS stores url+query
   contiguously → /assets/x.jpg%20%20…?width → 404 (this also corrupted
   the Works page, whose cards read that binary). CMS padding for
   URL-ish values (starts with / http ./) is now "#000…" — fragments
   are never sent to the server, byte lock intact, same padded string
   in all three layers. Junk copy_map entries created by picking the
   broken image were cleaned.
2. IMAGES UNPICKABLE: Framer sets pointer-events:none on image layers —
   they are invisible to hit-testing. Edit-mode overlay now injects
   *{pointer-events:auto !important} (safe: overlay intercepts all
   clicks). Plus stacked-image chooser: click reports ALL images under
   the cursor (imgs + css background-image, topmost first); >1 opens a
   "which one?" panel with thumbnails. Verified on the rotating hero.
3. OTHER PAGES: single-page exports render about/works/etc. as CLIENT-
   SIDE routes — content lives only in chunks; deep links 404'd. Both
   servers now SPA-fallback extension-less paths to index.html; flex
   matching also covers \\n/\\t ESCAPED whitespace inside minified
   chunk strings; resolve returns found:{html,chunks,cms} and the panel
   warns "couldn't locate — click a shorter fragment" instead of
   silently no-op'ing.
REMINDER for the owner-facing docs: picking only works in ✏ edit mode,
not the plain preview.

## Plan polisher (owner request: "too lazy to write the plan format")
Plan tab → "✨ Polish rough plan with AI": POST /api/ai/plan takes the
raw textarea ("jomiez, ai agency, chill tone, insta @jomiez"), rewrites
it into the canonical plan via the configured model (same AI settings
as fill), saves to project_plan.md, returns it for review — placeholder
inference (domain/email/socials from brand), <FILL: brand name> marker
if the brand itself is missing, owner details never dropped. Tested
live with deepseek-v4-pro; prompt tuned once (model echoed a <Brand>
placeholder verbatim — prompt now demands the actual name).

## First REAL Webflow migration (test-2, kitpro "Cyntra") — 2026-07-12
The last untested platform path, owner-driven through the studio with
me watching. Webflow-specific lessons now baked into the tool:
- modern CDN host cdn.prod.website-files.com added to image inventory
  (old regex only knew assets.*) — all 152 images caught.
- srcset variants are SEPARATE size-suffixed files: build strips
  srcset/sizes from any <img> whose (new) src stem no longer appears
  in its srcset, else browsers keep showing stale variants.
- kitpro templates ship a validator/review tracker in TWO parts:
  external script tag + inline __WF_REVIEW_BRIDGE bootstrap — both
  stripped at build.
- THE BIG ONE — CDN SHIELD: Webflow asset filenames embed the brand
  (kitpro-cyntra…min.css). DeepSeek hallucinated renamed CDN urls
  (pointing at files that don't exist → unstyled site), and the brand
  tokens would corrupt them regardless. _apply now vaults every
  website-files/framerusercontent url before replacement and restores
  after — EXCEPT urls that are themselves pair olds (deliberate
  retargets). Backtick excluded from the url charclass (template-
  literal delimiter in chunks). verify's forbidden-word scan masks
  shielded urls: brand-inside-CDN-filenames is a NOTE not a FAIL
  (never rendered). Asset-extension hrefs (css/ico/png/…) are no
  longer inventoried as retargetable links.
- Roadmap idea from this: `forge.py fetch` for Webflow could localize
  CDN assets (css/js/images) with brand-free names — full ownership,
  removes even filename mentions.

## Scattered multi-page ingestion (owner request) — 2026-07-12
Webflow has no cross-page linking like Framer: owners save each page
separately with arbitrary filenames. Now handled end-to-end:
- studio upload accepts MULTIPLE loose files (input `multiple`) or a
  zip; api_create writes them all into one temp dir for init.
- init normalizes: reads each page's canonical/og:url to derive its
  ROUTE, detects the home page ("" route, fallback shortest name),
  renames every page to its route ("pricing page final FINAL2.html" →
  pricing.html, home → index.html), records cfg.routes (path→file) and
  cfg.own_hosts (the template's live domain(s)).
- build localizes inter-page links: absolute (https://site.webflow.io/
  about-us) AND root-relative (/pricing#plans) hrefs become
  ./about-us.html / ./pricing.html#plans, anchors/queries preserved.
Proven with a 3-page synthetic ("acme corp — home (SAVED).html" etc.):
clean filenames out, links wired, verify CLEAN, all pages served.
Battery 42/42 after. Real scattered Cyntra pages = next natural test.

## Self-hosting builds — 2026-07-12
Owner downloaded site.zip, handed it to another AI (Antigravity/
Gemini), which served it with a plain `python -m http.server` → BLANK
PAGE (the documented range-protocol failure, live in the wild). Fix:
every build writes `site/serve.py` (standalone stdlib runner: ?range=
slices, .js@ MIME, SPA fallback) + platform-aware `site/README.txt`.
The zip is now self-explanatory to humans and AIs alike. Verified: the
shipped runner passes the exact range-slice test; battery 42/42.

## Click-to-REMOVE (owner request: stubborn credits/badges) — 2026-07-12
Edit-mode panel now has 🗑 Remove. Every pick message carries elInfo
(tag/id/classes minus runtime ones/data-framer-name/index-among-
matches). POST /api/entry/remove routes by platform:
- WEBFLOW/STATIC → copy_map["remove"] entry; build PHYSICALLY deletes
  the nth matching element via balanced-tag scan (_remove_nth_element;
  refuses to delete when it can't locate — never guess-deletes).
  Verified: "Made by SomeStudio" credit gone from code, a sibling
  sharing one class survives.
- FRAMER → appends a selector (#id / [data-framer-name=…] / .classes)
  to hide_selectors — React re-creates deleted DOM, so removal is a
  permanent baked-in CSS rule (invariant preserved). Used it for real
  on jomiez-lesmana's "MADE BY VELOX THEMES" footer credit.
Owner's product vision recorded: click-anything editing (colors,
animation behavior next) where every change is written into the
shipped code, not overlaid. Battery 42/42.

## Click-to-restyle + motion control ("proceed with all") — 2026-07-12
The pick panel now has 🎨 Style & motion: text/background color, font
size/weight, custom CSS lines, and a "freeze all motion" preset
(animation/transition/transform:none + opacity:1 — !important CSS
beats even the runtime's inline entrance transforms, so it genuinely
kills Framer appear/hover motion per element). Scope choice: "this
element only" (unique cssPath: #id / nth-child chain, stable across
rebuilds since builds are deterministic) or "all matching"
([data-framer-name]/class selector). Changes preview LIVE in the edit
iframe (inline !important), then Apply persists to copy_map["styles"]
{selector, css{}, label} and build bakes a <style data-forge-styles>
block into every page's head — sanitized (props ^[a-zA-Z-]+$, values
reject {}<;\\/expression; selectors reject {}< but ALLOW '>' — child
combinator; that bug cost one round). Plan tab gained "reduce ALL
motion site-wide" (cfg.reduce_motion → global .01s duration rule).
Verified end-to-end: live preview 55px → saved selector → baked rule
in shipped head; injection attempts filtered; battery 42/42.

## Container picking + ⬆ parent breadcrumbs — 2026-07-12
Owner: "style only works on text; buttons/backgrounds unreachable."
Root cause: picker only grabbed text leaves and images — clicking a
button styled its inner SPAN. Now: every pick carries elInfoFull (self
+ up to 6 ancestor elInfos); the panel renders breadcrumb chips
(span → div "Content" → a "Button 02" → …) — click to climb to the
element that owns the background; container panel = style+remove only,
style section auto-open. Clicking "empty" areas picks the topmost
sized container directly. Verified live: text→button climb, live
background paint on [data-framer-name="Button 02"], saved nth-child
path rule baked in shipped head. Battery 42/42.

## Target visibility (owner styled the wrong container) — 2026-07-12
Owner styled "Button Container" (button+avatars+rating row went cyan)
because chips gave no visual feedback about which element they name.
Fixes: (1) full-viewport decorative image layers no longer steal picks
from text/buttons (overlayHuge check — the pointer-events:auto change
had let a hero background svg hijack every header click); (2) the
CURRENT style target stays outlined green (.__forge-target) while the
panel is open, and HOVERING an ancestor chip moves the outline to that
element (scrollIntoView'd) — see before you commit. Verified: click
"GET IN TOUCH" text → P outlined; hover ⬆a chip → outline jumps to the
button anchor.

## UNDO + production-hardening pass — 2026-07-12
Owner painted the whole page cyan by accident → asked for undo + an
overall bug-hunt. Shipped:
- UNDO: snapshot() of copy_map/forge.json/project_plan.md into
  <project>/.history/ (capped 30) before EVERY mutating endpoint
  (copymap/plan/config POST, entry set/remove, style, ai fill/merge/
  plan). POST /api/undo restores + pops; header shows "↩ Undo (n)",
  click = restore + rebuild + reload. In the battery (44/44).
  BUG CAUGHT BY BATTERY: my regex auto-wiring put snapshot() into the
  GET handlers (reads snapshotted current state → undo "restored" the
  mutated state; manual test passed only because no GET intervened).
  Lesson: hand-place mutations, let the battery judge.
- Live-preview leak: 🎨 preview styles lingered after Cancel (the cyan
  page). Touched nodes are tracked and reverted on panel close.
- Container safety: page-spanning wrappers are picked only as last
  resort and the panel warns "spans (almost) the whole page".
- Escape closes the panel.

## Backend scaffold + dev handoff (owner: "platform for developers")
`forge.py backend` generates backend/app.py + AGENT_GUIDE.md:
- app.py (stdlib): serves site/ with all three protocols AND a content
  API — copy_map.json IS the database. POST /api/content validates
  (byte budgets, forbidden chars) then auto-rebuilds; POST /api/media
  uploads to assets/. "EXTEND HERE" section for the receiving dev/AI.
- AGENT_GUIDE.md: platform-specific rules (framer 3-layer/hydration vs
  webflow), the golden rule (never hand-edit site//pristine/), the
  serving protocols, where custom code goes. Written FOR AI IDEs.
- Studio "⬇ dev handoff" button: <name>-project.zip = forge.py + full
  project (pristine, copy_map, assets, site, backend; .history
  excluded) — fully self-contained and rebuildable anywhere.
Battle-tested: backend served the Lesmana site, rejected an over-
budget write, applied a legit write with auto-rebuild (change live in
all layers), media upload served post-rebuild. Battery 44/44.

## MCP SERVER (the platform move) — 2026-07-12
`forge_mcp.py` (zero-dep stdio JSON-RPC; registered in .mcp.json —
restart the agent session to load it): 16 tools exposing the whole
pipeline to ANY MCP agent: list/create/delete project, fetch,
inventory, get/set plan, get_content (paged, only_unfilled, filter),
set_content + set_content_bulk (guarded: byte budgets + forbidden
chars, snapshot before write, auto-build), add_style, build, verify,
generate_backend, serve_preview, undo. THE AGENT IS THE COPY MODEL —
no API keys: it reads entries, writes fills, guardrails enforce the
physics. Snapshots share the studio's .history format (undo interops).
Verified over raw JSON-RPC: init/list, agent-native read→write→build→
change-in-shipped-code→undo→reverted, backtick + budget guards firing
with exact messages. Battery 44/44.

## FOREIGN-AGENT TEST: Antigravity/Gemini drove a full migration — 2026-07-12
Owner registered forge_mcp in Antigravity (16/16 tools loaded) and had
GEMINI migrate a fresh export (Flowtive → Gravitest) using ONLY our MCP
tools: create/fetch/inventory/set_plan/fill(5 entries incl brand tokens
+ the '©Reddevs' designer credit + retargeted framer.website link)/
build/verify/serve_preview. Result: verify CLEAN, zero text leftovers,
site rendering with animations. THE THESIS HOLDS: the agent's own model
does the thinking; our guardrails make it unbreakable.
The one gap Gemini couldn't close: the LOGO IMAGE (known class —
verify's NOTE said so but no tool existed). Added MCP tool
generate_logo (wraps forge logo; call-twice UX: first call lists the
template's fonts, second renders; smoke-tested — Flowtive ships
Inter Tight/Instrument Serif). Clients must refresh MCP to see tool 17.

## URL SCRAPING (owner: "paste the template URL, skip view-page-source")
No external repo needed — live Framer/Webflow sites serve fully-SSR'd
HTML per route, so scraping = fetch home + discover same-host routes
from nav hrefs + fetch each (cap: home+14). `forge.py init <url>`,
studio URL field, MCP create_project url param — all three faces.
Framer uses ./about-style RELATIVE hrefs (learned live: first scrape
of lesmana.framer.website found 1 page; fix found 9 incl 4 CMS work-
detail routes, dead /404 skipped). Build's link localizer now also
rewrites ./route hrefs to local files (only when the page exists
locally — single-page SPAs untouched).
MULTI-PAGE FRAMER: PROVEN via this path — 9-page live scrape, 53
chunks + 8 CMS shared, 412 strings inventoried across all pages,
build + link rewiring + verify all correct. Last roadmap unknown
closed. Battery 44/44.

## ROGUE-AGENT INCIDENT + pristine sealing — 2026-07-12
Antigravity/Gemini, asked to swap the Flowtive dummy-logo ticker,
correctly diagnosed a REAL limitation (one dummy URL fills 6 slots →
set_content would make all 6 identical), then went AROUND the MCP:
wrote replace_logos.py and hand-edited pristine/index.html. Result:
invisible no-op (the ticker renders from CHUNKS — hydration reverted
the HTML edit) + corrupted pristine. Cleaned up: pristine restored
from the live URL, rogue script deleted, rebuilt CLEAN. Its
generate_logo outputs (5 wordmark SVGs) were fine and kept.
Hardening shipped:
- PRISTINE SEALING: sha256 manifest (pages/chunks/cms) written at
  init + re-sealed after fetch (pristine/.forge-manifest.json);
  verify FAILS on any modification ("restore + route through
  copy_map"), build prints a loud warning. Tamper-evident, not
  tamper-proof (agents have their own file tools) — but nothing slips
  by silently now. All existing projects sealed; live-tested.
- In-band teaching: get_content tool description + AGENT_GUIDE now
  state the never-hand-edit rule and the repeated-asset limitation
  (one URL in many slots changes together; per-slot split impossible
  by value replacement).
~~KNOWN LIMITATION~~ SOLVED same day (owner: "shouldn't our system
handle it?"): PER-SLOT IMAGE OVERRIDES via the style layer — MCP tool
#18 `replace_image_slots` {old_url, new_urls[]} distributes different
images round-robin across every slot a shared asset fills, as
per-element `content:url()` !important CSS (hydration-proof).
THE HARD PART — selector stability: nth-child paths computed from SSR
HTML BREAK on Framer because the runtime REMOVES pruned breakpoint-
variant siblings after hydration (live section:nth-child(7) vs SSR
(9) — debugged against the live DOM). forge._img_slot_selectors now
builds a real parse tree and anchors each selector on the nearest
ancestor whose CLASS SET is unique among siblings (framer-* classes
survive hydration; variant twins share the anchor and intentionally
get the same per-slot override), with nth-child only below the anchor
where no pruning happens. Also: breakpoint variants use DIFFERENT
dummy asset files — cover each url (get srcs from the live page or
inventory). Proven on gravitest's collaborator grid: 5 real wordmarks
across 6 IPSUM slots, 31 rules, screenshot-verified. Studio hint added
(image panel → 🎨 content:url for "only this spot"). Battery 44/44.

## Research notes (2026-07-12): positioning + next moves
Competitors (PullPage, FramerExporter, Exflow, github zaynors/
framer-export) EXPORT framer sites + strip the badge — none do the
rebranding migration (3-layer byte-locked copy fill by any model),
visual hard-coded editing, backend generation, or Webflow. Our moat is
the guarded content pipeline, not export.
Validated roadmap:
1. MCP SERVER (top priority for "platform" goal): expose
   create/fetch/inventory/fill/build/verify/content-edit as MCP tools
   (stdio JSON-RPC, stdlib-able; or FastMCP if deps acceptable) so
   Claude Code/Antigravity/any agent drives migrations natively.
   AGENT_GUIDE + REST API already cover the interim.
2. Asset localizer for BOTH platforms (framer-export repo validates
   feasibility): download webflow CDN css/js/images (renamed brand-
   free), rewrite refs incl srcset — removes last brand mentions + CDN
   dependency.
3. Multi-page FRAMER export test still outstanding.

## Per-character split-text editing (owner: "handle span stuff") — 2026-07-12
Framer splits headings into per-CHARACTER spans (`<span>N</span>` …).
Two consequences fixed:
- inventory MISSES them (TextExtract drops <2-char data) — the full
  string lives only in the CHUNKS. Not fixed at inventory (noisy);
  handled at pick time instead.
- clicking a fragment now: picker sends leaf + ancestor texts
  (fragment-safe, accepts 1-char leaves); resolve tries them
  smallest-first against copy_map, else creates an entry from the
  smallest MEANINGFUL candidate that exists in source. Key subtlety:
  DOM concatenates adjacent text blocks with NO space
  ("automatically.It gives…") so no node matches a chunk string —
  resolve now also splits candidates at glue points
  (?<=[.!?])(?=[A-Z]) so each real paragraph is matchable. Proven on
  test-3 (Nova template): click the "N" of a per-char heading →
  "Edit text" panel with the full sentence → edit → build → new text
  in chunks, old gone. Battery 44/44.
Also this session: one-click "⚡ Prepare project" (fetch→inventory→
build with a live progress bar; verify deliberately excluded — it
flags the unfilled brand); post-prep primary button is "🔨 Build" not
the fill-wiping "Re-run"; AI fill now auto-builds; inventory MERGE-
preserves existing fills/styles/removals on re-run (tested: "preserved
67 existing fill(s)"); URL scrape organizes into a real folder tree,
zip only on download (site.zip or dev-handoff).

## Split-text edits went INVISIBLE (owner hit it live) — 2026-07-12
Editing a per-character split heading made the whole section's text
vanish. Reproduced + root-caused: the edit lands in the CHUNK string,
SSR HTML still spells the OLD chars → hydration mismatch → React
re-creates the char spans AFTER Framer's appear engine ran → spans
stuck at their entrance pose (opacity:.001, translateY(12px)) forever.
Fix: the panel save flow now detects split-text entries (found:
chunks-only) and auto-bakes a visibility guard style
(`ANCHOR, ANCHOR *{opacity:1;transform:none}` — anchor = nearest
h1-h6/p ancestor by class) so the new text simply appears instead of
animating. Panel shows a "✂ split-text detected" note; re-edits of
picked entries recompute found so the guard re-fires. Verified by
computed styles before/after (0.001/12px → 1/none).
NOTE: the Browser-pane screenshot tool went stale this session
(injected a red max-z probe — capture still showed blank gray). Trust
DOM/computed-style checks over pane screenshots when they disagree.
Battery 44/44.

## No-tradeoff split text + rotators + edit-mode nav — 2026-07-12
Owner rejected the visibility-guard tradeoff ("i don't want any
trade-off") — and was right. Shipped instead:
1. SPLIT-TEXT REGENERATOR (forge build): _regen_split_runs finds
   per-char span runs (word-wrappers of single-char spans, markup
   verified uniform), strips tags to get the run's text, and REBUILDS
   the run spelling the pair's NEW text with cloned markup. SSR now
   matches what the runtime renders from the chunk → hydration equal →
   entrance animation fully preserved. The studio visibility guard was
   REMOVED (panel note now says "handled automatically"). Verified:
   built HTML spells the new text char-by-char; browser-level animation
   check impossible this session (pane's animation engine frozen — CSS
   transition probe didn't tick; same root cause as dead screenshots) —
   owner confirms visually.
2. ROTATING/TYPEWRITER text: phrases live as text:`…` items in chunk
   arrays. Owner's phrase edit HAD worked (found text:`jomiezflo` in
   built chunk) — they just never saw it cycle by. resolve now detects
   the contiguous text:-literal group around the picked phrase and
   returns rotator:[{old,new}…]; the panel renders ALL phrases as
   inputs (entries auto-created), save loops entry/set + one rebuild.
   Tested on jomiez: 5 phrases surfaced with existing fill prefilled.
3. EDIT-MODE NAVIGATION: ⌥/⌘-click follows links (overlay passthrough,
   works for SPA routes via serve_edit fallback); multi-page projects
   get a page dropdown in the edit toolbar (project_info now exposes
   pages). Battery 44/44.

## All tiers shipped (owner: "proceed with all") — 2026-07-12
1. EDIT-MODE NAV v2 (the "bummer" killed): 🧭 Browse/✏️ Edit toggle
   (postMessage 'mode' → overlay PICKING flag; browse = normal clicks/
   navigation), ← back button, and a page dropdown that harvests SPA
   routes from the live iframe DOM on every load (single-page Framer
   projects' client-side routes now navigable — cfg.pages alone missed
   them).
2. ASSET LOCALIZER (roadmap #2, DONE): `forge.py localize` downloads
   every remote CDN asset (website-files/framerusercontent/gstatic)
   to pristine/remote-assets/ as sha1-hashed brand-free names, 2-round
   (css files are scanned for absolute AND relative url() refs);
   cfg.localized maps url→file. Build rewrites refs (pages AFTER
   _apply so owner swaps win; chunks; passthrough text assets; css
   internals incl relative) and serves from /assets/r/. Gotcha found
   live: Webflow filenames contain parens/spaces ("fav-icon (1).png") —
   REMOTE_ASSET_RE must not stop at parens; trailing ,);. stripped.
   Proven on test-2: 155 assets local, ZERO website-files refs left,
   no brand in any filename, css serves locally with 0 remote refs.
   Studio: "🏠 Localize assets" chip in steps; MCP tool #19
   localize_assets.
3. PACKAGING (local only — owner undecided on open-source): git repo
   initialized (9 files, projects/ gitignored), product-grade README,
   "License: TBD". Publishing is the owner's call; nothing pushed.
Battery 44/44.

## Slot-override over-match (owner: "logos where backgrounds were")
Gravitest regression, 2 root causes, both fixed (see git log):
(1) sibling-unique classes repeat across COUSIN cards → slot selectors
collapsed & painted logos onto unrelated imgs incl. section textures.
Anchors now require document-wide near-uniqueness (count<=3 = variant
twins, intentionally shared). (2) one "dummy" url was actually a
section background texture — _img_slot_selectors now carries a width
hint (?width= / srcset) and replace_image_slots REFUSES slots >700px
as backgrounds. Verified live: 10 logo overrides, 0 background
hijacks, textures clean. NOTE: git log is now also a session log.

## Sticky hover v2: overlay DRIVES the flip (owner: "how it doesn't
## switch to the hover side to edit that too") — 2026-07-14
Sticky mode passively letting real enter events through did NOT flip
hover-variant cards in the edit mount (the raw event starvation was
never isolated — the Browser pane's synthetic hover went unreliable
mid-diagnosis: desynced coords/dropped events, so event-level
bisection was impossible). What WAS proven: Framer keeps BOTH variant
layers in the DOM ("Initial content" visible, "Hover content" at
opacity:0 translated below the card's overflow:hidden root), the flip
is JS-event-driven (freeze blocking works), and framer-motion/React
accept synthetic events — they never check isTrusted. So sticky now
dispatches the hover itself: on every trusted mousemove, stickyEnter()
fires pointerenter/mouseenter up the ancestor chain (once per element,
`entered` list pruned on disconnect) + bubbling over/move on the deep
target, and never sends leaves; real out/leave stay blocked at capture.
Cards flip as you sweep and STAY flipped → hover side clickable.
isTrusted guards added: blocker listeners ignore synthetic events (our
own must pass), mousemove ignores synthetics (no re-pick recursion).
`window.__forgeHover(x,y)` exposed for tests/programmatic flips.
Verified live on sadewa team cards: toggle → flip (Hover content
opacity 0→1, slid into frame) → pinned → bio click resolves to full
"Edit text" panel with Hover-content breadcrumbs. Battery 44/44.
Pane lesson: computer-tool hover coords can silently desync from
screenshots — calibrate with event listeners before trusting
hover-based tests; prefer __forgeHover for flip testing.

## DESIGN LIBRARY (owner's Option 2, "proceed") — 2026-07-14
The endorsed product move: saved migrations become a searchable design
library any plan can be matched against. Shipped end-to-end:
- forge.py `card`: distills a project into design_card.json — palette
  (freq-ranked hex from pages+css, rgb() normalized), fonts
  (@font-face families incl. multi-word, google fonts links, Webflow
  WebFont.load families w/ entity-encoded quotes), authored section
  names (framer data-framer-name; webflow class-based with the
  generic wrapper class dropped by frequency), motion features
  (hover_variants / split_text_runs / rotators from chunk text:`
  groups / marquee / appear / cms count), counts from copy_map, title/
  description/og:image FROM INDEX.HTML FIRST (the brand-token lesson
  again — pages[0] is alphabetical), cfg.source_url (init now records
  scrape URLs). Proven on sadewa/test-2/gravitest/jomiez-lesmana.
- LICENSE-CLEAN BY CONSTRUCTION: cards hold fingerprints, never
  template files. "Start from this" re-imports from the card's source
  URL or copies pages from the owner's own local project; a card alone
  cannot rebuild a template, so a shared library is safe.
- studio: library/ dir (gitignored), GET /api/library, POST
  /api/library/{save,delete,start}, POST /api/ai/match (any model,
  MATCH_PROMPT returns top-3 [{id,score,reason}], ids validated);
  sidebar 📚 Design library view (S.view branch) with palette
  swatches/font/feature-chip cards, per-project "📚 save design"
  header button, plan textarea + "✦ Match my plan".
- MCP tools #20-22: list_library / save_to_library /
  create_from_library — THE AGENT IS THE MATCHER (reads cards, ranks
  itself, no API key), consistent with the fill thesis.
- Live-tested: DeepSeek v4-pro match on "AI automation agency, lime
  accent, animated team cards" → sadewa 95% (called out the exact lime
  accent + hover/appear team cards + blog/careers from sections) vs
  test-2 25% — the TEXT fingerprint carries the design signal; vision
  upgrade optional later (cards already store preview_image og:url).
  Note: match calls can exceed 30s on reasoning models.
- Battery 44→51 (7 library scenarios: save/list/start-spawn/guards/
  delete). All 51 green.

## DEPLOY ANYWHERE: static-host hardening (owner: "proceed") — 2026-07-14
The last-mile gap closed: site.zip used to NEED serve.py's protocols
(the wild blank-page incident). Now every build is fully static:
1. RANGE GUARD PATCH (the blank-page killer): the CMS loader in the
   chunks does `if(l.length!==i)throw` after `?range=` fetches — a
   static host ignores the query, returns the whole file, boom. Build
   now rewrites that guard (RANGE_GUARD_RE + _static_slice, minify-
   name-agnostic): over-long response == full file → slice client-side
   from the merged {from,to} list, byte-identical to a protocol
   server's reply. Protocol servers still hit the untouched fast path
   (patch is dormant on exact responses; equal-length full-file case
   is provably identical bytes).
2. ICON MIME: name.js@1.2.3 → name.1.2.3.js at copy + `.js@(ver)` →
   `.(ver).js` literal rewrite in chunks (import() hard-rejects wrong
   MIME on static hosts; icons are self-contained, grep-verified).
   pristine keeps original names; serve.py keeps the .js@ protocol for
   OLD builds.
3. Build ships _redirects (`/* /index.html 200`, Netlify+CF Pages),
   404.html (= index copy, GitHub Pages), vercel.json (extension-less
   rewrite), DEPLOY.md (per-host one-steps + the GH-Pages-subpath
   caveat: asset paths are root-absolute — user site or custom domain
   only). serve.py/README.txt reworded (no more BLANK PAGE warning);
   AGENT_GUIDE Serving section rewritten.
PROVEN on the exact wild failure: acme-demo on plain `python3 -m
http.server` → 4.6k chars rendered, 57 images, hydrated, ZERO console
errors; network shows ?range= requests answered with full files and
sliced in-page; renamed icons all 200. Battery 51→56 (icons .js-named/
no .js@ anywhere, chunk guard patched, deploy artifacts, slice math ==
protocol slices). NOTE for future debugging: Tier-2 pre-slicing to
static files was rejected — range query strings are RUNTIME-BATCHED
(searchParams.set from a merged list), not literals.

## Whitespace-landmine strings (owner hit it live: marquee edit
## "refused" — angry, rightly) — 2026-07-14
test-1 marquee edit showed 9 report hits yet the page kept the old
text. Root cause chain: TextExtract NORMALIZES whitespace runs while
harvesting, so inventory minted an entry ("…CREATIVITY ✱ DESIGN…")
that can never exact-match the source ("…CREATIVITY ✱␣␣DESIGN…");
build's ws-tolerant fallback fixed the PAGES only, chunks kept the
old spelling → hydration reverted the edit while the no-op detector
counted the page hits as success. THREE fixes, all shipped:
1. inventory: any harvested multi-space string that no longer appears
   byte-for-byte in ANY layer is auto-marked "flex": true (+ CMS
   budget computed via flex match when the wrapped form lives in CMS).
2. studio resolve: reusing a non-flex entry whose old isn't exact in
   pages/chunks upgrades it to flex (owner's existing projects heal
   on next pick).
3. build report: new __at_risk__ list — filled multi-word pairs that
   hit pages but STILL appear (flex match) in built chunks = hydration
   will revert; studio counts these as dead edits (chip + panel
   refuses to close on them). Honesty net for any future matching gap.
Also cleaned test-1's two stale junk deads ('nexmind®' lowercase
token that exists nowhere; framer.website link entry made obsolete by
the inter-page link localizer). Verified live: marquee renders
"JOMIEZ CREATIVITY ✱ … THE CHAKA PRECISION", dead chip gone.
Battery 56→60 (landmine harvested / flex-marked / landed / reported).

## SELF-HEALING (owner: "AI self-heal that fixes any failure") — 2026-07-14
Owner asked for an AI that monitors failures and rewrites platform
code to fix them. Talked them out of the dangerous half (rogue-agent
lesson: models editing code = corruption) by pointing out that a log
detector that knows 100% what's wrong needs no model — the fix can be
deterministic. Shipped the safe version end-to-end:
- forge.py `heal`: reads site/.forge-report.json, walks a ladder per
  broken fill — (1) flex upgrade when the source merely wraps/spaces
  the text differently, (2) source-casing adoption, (3) nearest-
  source-string adoption via difflib >=0.85 (CMS byte budgets
  enforced — refuses with exact overshoot), (4) honest STUCK line
  with the 3 closest candidates. Strings only; never touches
  platform code; corpus = TextExtract + chunk text:`/children:`
  literals + CMS string dump from PRISTINE.
- report semantics tightened: `__moot__` = zero-hit fills whose old
  existed in pristine (or is an auto-minted brand token) and is GONE
  from the built output — longer fills already consumed every
  mention; success, not dead. Dead chip / heal / MCP warnings all
  exclude moot. (Battery caught both: brand tokens 'OldBrand' and
  case-variant 'Oldbrand' were being flagged/stuck.)
- studio: /api/heal (snapshotted; returns healed/stuck counts +
  new_old since adoption can change the entry key); panel save flow
  now runs heal->rebuild->re-verify automatically when an edit
  doesn't take (bounded, one attempt); dead-edits chip offers
  heal-all + rebuild.
- MCP tool #23 `heal`; set_content_bulk warnings now say "run heal";
  AGENT_GUIDE gained "WHEN AN EDIT FAILS — the runbook" (the owner's
  requested "training data", placed where driving agents read it —
  report -> heal -> rebuild -> fix the ENTRY, never the pipeline).
Battery 60→69: casing typo + word typo healed and landed, nonsense
honestly STUCK with candidates, moot tokens exempt, single-entry
heal path. All 69 green.

## Image-edit self-heal (owner hit it live: test-2 logo/portrait
## swap "did nothing", whole set failed) — 2026-07-14
Owner tried replacing a Webflow CDN image; the edit reported dead and
self-heal (text-only) couldn't help. Root cause was a THREE-layer bug,
all fixed + battery-proven (74->78):
1. PICKER MANGLING: /api/entry/resolve did urllib.unquote THEN
   urlbase's .replace(" ","") on the picked src, but compared against
   e["old"] WITHOUT unquote -> a Webflow filename with %20 never
   matched its inventory entry, minting a junk duplicate (spaces
   stripped: "logo%20cyntra%201"->"logocyntra1") that matches nothing.
   (Left resolve as-is: the junk pick is zero-effect -> auto-heal now
   fixes it fully. Fixing resolve alone would turn it into a silent
   PARTIAL swap that heal can't see, so heal is the right layer.)
2. SRCSET VARIANTS: one asset ships as 7+ size/format files
   (…-p-500 …-p-2000, .avif/.png); swapping one leaves the rest.
3. forge.py heal IMAGE PASS (the fix): for each broken image entry,
   _asset_stem() takes the longest alnum run in the FILENAME (not the
   whole URL — the site-id path segment is shared by EVERY asset and
   matched 107 urls in the first cut), then _image_variant_urls()
   finds every real source URL sharing it and points them all at the
   new image; junk picks (old not in source) are dropped.
4. CDN SHIELD parens bug: CDN_URL_RE excluded ( ) so a "Portrait
   (6).avif" url was shielded as a truncated fragment and its pair-old
   exception missed -> never swapped. Char class now allows parens
   (matches the localizer's REMOTE_ASSET_RE); pair-old match is by
   prefix so a trailing url(...) paren is harmless.
Browser-verified on real test-2: logo + portrait swapped, 0 broken
images, old asset ids gone. Battery +4 image-heal scenarios (mangled
pick zero-effect -> heal swaps all srcset variants, parens/encoding
safe, junk dropped). All 78 green. assertTookEffect already routes
image saves through the heal loop, so edit-mode image failures now
auto-heal end to end.

## THE OWNER'S STRESS TEST: "Variant 1" destructive hide — 2026-07-14
Owner challenged the self-heal claim with a real breakage: removing
the Framer badge on jomiez-lesmana wrote hide_selectors incl.
[data-framer-name="Variant 1"] — a FRAMER DEFAULT NAME. SSR showed 3
matches (both nav arrows + the Case Studies menu item) and hydration
mints more on other routes; their swapped image vanished, site
"broken". Honest test run first, no help: build baked it silently,
verify said CLEAN, heal said nothing (its ladder was fills-only) —
though heal DID catch+fix an unrelated dead bio fill (96% adoption).
The gap, closed in all three places:
- PREVENTION (studio remove): blast-radius guard — generic-name attr
  selectors are scoped via the element's own framer-* class set
  (count==1) or a unique ancestor anchor, else REFUSED with "click ⬆
  to a specific parent". hide_selector_audit + GENERIC_FRAMER_NAME_RE
  live in forge.py (studio imports them).
- DETECTION (verify): FAILs on any hide selector with a generic
  Framer default name or >3 SSR matches, with the exact fix path.
- CURE (heal): new first rung drops destructive hide rules from
  cfg (writes forge.json), loud HEALED line. jomiez-lesmana healed
  by the tool itself: arrows + Case Studies restored, Velox credit
  and badge STILL hidden — browser-verified.
Battery 69→74 (refusal message, verify FAIL, heal drop, config clean,
verify CLEAN after). Shell lesson recorded: cwd persists across Bash
calls — rm -rf with relative paths from a stale cwd silently no-ops
(or worse); always absolute-path destructive commands.

## 2026-grade UI v2: Claude-warm redesign + walkthrough — 2026-07-14
Owner: "looks AI designed… more like Claude/Framer/Figma, stop emoji
icons, add a walkthrough." Shipped: warm near-black tokens with ONE
solid terracotta accent (#d97757; primary buttons use warm-white text,
no gradients), 24-icon inline Lucide-style SVG set + I(name,size)
helper (data-ic spans for static HTML, ${I()} in templates; NEVER
assign icon strings via textContent — that bug printed raw SVG once),
buttons inline-flex, 6-step spotlight walkthrough (auto-runs once via
localStorage.forge_tour, replay from header help button / empty-state
"Show me around"). Battery 74/74. node --check on the extracted
<script> is the fast studio-JS syntax gate.

## PRODUCT DIRECTION: desktop app + thin cloud (owner decided) — 2026-07-14
Owner chose the DESKTOP-APP model over hosted SaaS (asked, presented
tradeoffs): users download a packaged app (code stays private-ish via
PyInstaller bytecode; not a vault but not git-clone either), template
data stays 100% LOCAL per user. A thin cloud backend adds only two
things: account login + privacy-respecting telemetry (track users/
progress/failures to improve over time). Plus a marketing website
(register + download); owner will supply a template and I migrate it
with the Aethron MCP (dogfood test). Repo is now on GitHub as
zenadell/Aethron (empty repo push pending owner). Rename Template
Forge->Aethron done (command name forge.py + MCP key "template-forge"
kept for compat). MIT LICENSE added. Render FREE tier rejected for
hosting (ephemeral disk loses projects, sleep, single-port kills the
per-project preview model, 512MB) — noted for any future hosted path.
Supabase chosen (auth + Postgres + storage in one) — Cloudinary NOT
needed. Because data stays local, Supabase FREE tier fits.

## Cloud layer (aethron_cloud.py) — dormant by default — 2026-07-14
- Supabase GoTrue REST auth (signup/login) + fire-and-forget telemetry.
  track() has a STRICT allow-list _sanitize(): events carry only event
  name/platform/counts/durations/error_category — NEVER user copy,
  images, links, brand. Enforced in code, not policy.
- Modes: REAL (both AETHRON_SUPABASE_URL + _ANON_KEY set = gate +
  network), DRY (AETHRON_CLOUD_DEBUG=path only = gate on, auth accepts
  any creds, telemetry to JSONL, zero infra — how to test), default
  (nothing set = fully dormant, no gate, no network; local + battery
  untouched).
- studio.py: _gate() + /api/auth/{login,signup,logout,me}, server-side
  SESSIONS (Supabase token never reaches browser), warm LOGIN_HTML,
  telemetry hooks at project_created/run_step/heal. supabase_schema.sql
  (events + RLS own-rows + profiles/plan for later monetization).
  docs/CLOUD_SETUP.md. Verified dry-run (gate+privacy-safe events) and
  dormant default; battery 78/78.
NEXT: owner brings marketing-site template -> migrate via MCP; owner
creates Supabase project; owner decides code-signing (Apple $99/yr +
Windows cert). Desktop packaging (PyInstaller launcher) still to build.

## Billing enforcement + first marketing-site migration — 2026-07-15
BILLING LOCKOUT (owner: "when we enforce payment, auto-lock every free
user out"): aethron_cloud.ENFORCE_BILLING (env AETHRON_ENFORCE_BILLING)
+ entitled(plan) + plan_of(token,uid) reading profiles.plan. Studio
login flows (email + google + dry) and _gate all check entitled():
beta (unset) = everyone in; flip on + restart = only pro/studio, every
free account blocked at next login (402 "free beta access has ended")
AND active sessions re-checked in _gate. Fails safe to 'free'. Tested 3
states in dry mode (off/free->200, on/free->402, on/pro->200); telemetry
logs login_blocked. AETHRON_CLOUD_DEBUG_PLAN sets the dry plan. Battery
78/78 (cloud dormant by default). docs/CLOUD_SETUP.md updated.

FIRST MARKETING-SITE MIGRATION via MCP (dogfood, owner watched):
developflow-template.webflow.io -> aethron-site, driven entirely
through mcp__template-forge__* tools. Proved multi-page Webflow scrape
(1 URL -> 15 pages incl nested /integration/*). 427 strings/184 imgs/12
links inventoried. Rebranded: brand token, all page titles, homepage
hero + 3 pillars + feature block + 3-step, pricing (Free $0/Pro $19/
Studio $49 monthly, $0/$15/$39 yearly), about (founder=Chaka, real
Aethron story), footer. Swapped hero mockup + 3 feature imgs with real
Studio screenshots (docs/screenshots -> project assets), generated
Aethron wordmark (Inter Tight 700 via forge logo --font fontsource
woff2) and swapped the logo image. verify CLEAN, 0 broken images, 0
DevelopFlow/Craftflow leftovers. LEFT FOR OWNER (structural, needs
editor): remove extra team cards (keep only Chaka), fake stat counters
on about, "Powered By Webflow" footer credit element, the "Upload your
image" decorative hero card. Project lives in projects/ (gitignored) —
not committed.

## MCP REMOVE PARITY + Webflow 404 fix (owner drove both) — 2026-07-15
Owner migrated their marketing site (developflow webflow -> aethron-site)
via MCP and hit real gaps:
1. 404 BUG (big): after clicking around a Webflow migration, links
   "claim not existing" / silently show home. Root cause: SPA fall-back-
   to-index is right for FRAMER (client routes) but WRONG for WEBFLOW
   (multi-page — unmatched path is a genuine 404). Fix: cmd_serve +
   shipped serve.py + _write_deploy are PLATFORM-AWARE now. Framer keeps
   extensionless->index.html; webflow/static serves styled 404.html w/
   404 status (never home). Build injects PLATFORM into serve.py; webflow
   _redirects = "/* /404.html 404", keeps the scraped 404.html.
2. MCP COULDN'T REMOVE (owner: "very big flaw"). Added forge._locate_
   element(html, contains, ancestor, occurrence) — text-based element
   finder (the agent-facing analogue of the editor's click+breadcrumb):
   finds tightest element with the text, climbs `ancestor` to a card/
   section, `occurrence` disambiguates nav-vs-footer repeats, auto-climbs
   off un-targetable bare tags. MCP tools #24 remove_element (webflow
   physical delete page-scoped OR all_pages for shared chrome; framer
   blast-radius-guarded hide; preview_only; snapshotted) and #25
   remove_page. build: removals page-scoped via optional "page"; webflow
   passthrough no longer re-copies a removed .html as an asset. Proven:
   removed 2 fake team cards (ancestor=3), careers page, Careers nav+
   footer links (occurrence+all_pages).
Also this migration: localize killed the developflow CDN css/favicon
leak (186 assets local, 0 brand refs); logo wordmark via forge logo
--font fontsource Inter-Tight woff2 (webflow WebFont.load fonts aren't
auto-discovered — known gap); founder=Tim (Nweke Ezinna Emmanuel), CEO
of Jomiez (Chaka is his AI product, NOT his name); footer "© 2026
Aethron — Powered By Jomiez" (em-dash not pipe: "| Powered By" is a
blanked token and the engine re-scans replacements, so new-contains-old
duplicates — invariant). Pricing Free $0/Pro $19/Studio $49. Project
gitignored (not committed). HONEST MCP VERDICT told to owner: mechanical
pipeline is idiot-proof (0 rejections all session); logo/image-fit/copy/
structural-removal need a capable operator or (now) the new tools; a
dumb AI gets ~80%.

## DESKTOP APP PACKAGING (the last product piece) — 2026-07-15
`bash build_desktop.sh` -> dist/Aethron.app (PyInstaller onefile +
BUNDLE; Windows: same script on Windows -> Aethron.exe). The two
tricks, both battle-relevant:
1. ONE BINARY, TWO ROLES: studio subprocesses forge via sys.executable
   — frozen apps have no python, so `Aethron --forge <cmd>` dispatches
   into forge.COMMANDS in-process (desktop.py), and studio.forge_argv()
   picks dev vs frozen argv. ALL 9 subprocess sites now use forge_argv
   (a regex refactor initially made the helper recurse into itself —
   caught by py_compile + battery).
2. AETHRON_HOME: bundles are read-only; projects/ + library/ live in
   ~/Library/Application Support/Aethron (mac) / %APPDATA%/Aethron
   (win); defaults to repo dir in dev. desktop.py sets it pre-import.
PROVEN frozen: --forge dispatch lists commands, studio boots+serves
200, and a full init->inventory->build ran INSIDE the .app via self-
subprocess (zero python on host). Battery 85/85 in dev after refactor.
aethron.spec hiddenimports carry fontTools/brotli (lazy imports in
cmd_logo that PyInstaller can't see). dist/+build/ gitignored.
docs/DESKTOP.md covers build/signing (unsigned = right-click Open,
fine for beta). NOTE: console=False buffers stdout — logs via BROWSER
env trick during tests. Owner still owed: Supabase keys + Google OAuth
client + repo push + DeepSeek key rotation + marketing-site deploy.

## PRE-PUSH AUDIT (owner: "find all bugs, especially image heal") — 2026-07-15
Deep trace of every recovery path found 3 real bugs, all fixed +
battery-proven (85 -> 92):
1. CMS CORRUPTION/CRASH HAZARD in build-time image expansion: expansion
   pairs bypassed _pairs_from_map's budget logic (in_cms hardcoded
   False). Variant in a CMS blob + longer new -> negative padding ->
   size-drift assert CRASH; shorter new -> SPACE padding mid-URL (the
   documented 404 bug). Fixed: expansion computes in_cms per variant,
   fragment-pads (#000), skips gracefully when it can't fit. Proven on
   acme (CMS blobs hold 13-18 image urls EACH — hazard was real).
2. die("CMS budget exceeded") fired AFTER rmtree(site) -> failed build
   left an EMPTY site. Fixed twice over: (a) pairs/validation now run
   BEFORE the wipe (failed build preserves the previous site); (b) for
   URL news the die is gone entirely — local assets ship as a SHORT
   DETERMINISTIC ALIAS (/assets/i<sha1-8>.<ext>, physical copy) that
   fits the slot; external urls demote to text-layers-only with NOTE.
   A 128-char filename picked onto a 67-byte CMS slot now just works.
3. Webflow edit-mode page dropdown listed harvested hrefs that aren't
   real pages (-> styled 404 on select). Now filters to cfg pages.
Also: frozen-app audit — HTTPS/scrape works frozen (certs fine);
onefile had ~20s cold start (temp re-extraction) -> switched spec to
ONEDIR inside the .app = ~2s; browser now opens AFTER the server binds
(no more connection-refused tab on slow starts). Battery 92/92.
Debug lesson: pgrep parent vs PyInstaller child — the LISTENER belongs
to the child pid; sample(1) showed dlopen/__import__ = slow, not hung.

## RUNTIME PROBE — L1's missing half (`forge.py probe`) — 2026-08-10
verify reads FILES; a browser runs CODE, and between them sits every
failure a file scan cannot see. Now closed, zero new dependencies:
serve site/ with the SAME handler `serve` uses (extracted as
`_site_handler`, so the probe measures the real protocols), point a
headless Chromium at each page, read three signals —
1. post-JS DOM (`--dump-dom`) -> rendered text length + image count;
2. console (`--enable-logging=stderr` -> `:CONSOLE` lines, message +
   source url);
3. OUR OWN request log (`on_request` hook on send_response) = every URL
   the browser actually asked for and the status we gave it. This is
   the signal no screenshot provides and no CDP client is needed for —
   and it catches assets requested by JAVASCRIPT, which no markup scan
   can ever see.
FAIL only on observable damage: page renders <120 chars, rendered text
<40% of SSR text (hydration wiping the page), any 4xx/5xx runtime
request, or console errors matching CONSOLE_FATAL (load/parse/module
failures). Everything else — React #418/#422, Framer "nextVariant
should be defined", deprecations — is a NOTE with "compare against the
untouched original" (the invariant). First cut FAILed a page that
rendered 5,530 chars and 59 images; a probe that cries wolf teaches the
agent to ignore it.
HONESTY RULE (the whole point): no browser -> `VERDICT: SKIPPED …
UNVERIFIED (not proven good)`, never PASS. `AETHRON_BROWSER=none`
forces it (that is how the battery tests it).
PROVEN, both directions: waller (the Next.js vacuous-pass case) —
71 of 73 runtime requests 404 (fonts + css the CHUNKS request, with
`?dpl=` query strings), while it renders text so no blank-page check
alone would have caught it. acme-demo — CLEAN; then a corrupted chunk
(file present, throws on parse) made verify still say CLEAN while probe
reported "content DISAPPEARS after JS: 13392 -> 129 chars" +
SyntaxError. Chrome quirk handled: it does NOT exit after --dump-dom —
read stdout until `</html>`, then kill (waiting costs the full timeout
on every page).
Wired everywhere: agent tool #8 + `_final_check()` (verify CLEAN alone
no longer ends the loop — files clean AND pages run, skipped runtime
returns an explicit warning), MCP tool #26, studio step "5 Runtime
check", verify's own verdict now says "CLEAN on disk — now run probe",
AGENT_GUIDE gained "WHEN THE SITE LOOKS BROKEN BUT THE CHECKS PASS".
Report -> site/.forge-probe.json (per page: rendered/ssr text, requests
+ requested list, failed_requests, console). Probe battery (scratchpad
probe_battery.py) 19/19 green; the old 92-scenario battery.py was lost
with its scratchpad — rewrite when next needed.

## CODING LAYER — Aethron becomes a dev platform — 2026-08-20
Owner's directive (non-negotiable, restated after I'd argued against
vendoring FCC): integrate the free-Claude-Code capability INTO Aethron,
IDE included; Aethron is to be in Claude Code's class, not a template
tool. Shipped `aethron_code.py` + a real IDE in the studio.
THE SEAM THAT MAKES IT LEGITIMATE: Claude Code's provider is an ENV
VAR. `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` point the CLI at any
Anthropic-compatible endpoint — that is literally all `fcc-claude`
does. So FCC is a PRESET (`provider: fcc` -> 127.0.0.1:8082 / token
`freecc`), nothing vendored, patched or redistributed. Presets:
anthropic (CLI's own login) | fcc | custom (gateway/self-host/ours).
Architecture: CodeSession drives `claude -p --input-format stream-json
--output-format stream-json --verbose --session-id … --permission-mode
…`, normalizes the wire into ONE event contract (ready/text/thinking/
tool/tool_result/done/log/exit) so the IDE is written against Aethron,
not against any CLI. InternalRuntime (aethron_agent) stays the no-CLI
path. `--mcp-config` + `--strict-mcp-config` inject OUR forge MCP so
the coding agent drives the guarded pipeline and the user's personal
MCP servers stay out; `--append-system-prompt PROJECT_RULES` fires
automatically when the workspace has a forge.json (never hand-edit
site//pristine/, use mcp__aethron__*, build->verify->probe->heal).
Studio: sidebar "Code" -> workspace picker (any project, or fresh ones
under HOME/workspaces), provider/base-url/token/model row, file tree +
editor + agent chat, event polling, live tree refresh after tool calls.
File API is workspace-scoped (traversal rejected, ../ and unknown
workspace both 4xx) and REFUSES writes to site//pristine/ with the
reason — the invariant enforced at the IDE layer, not just in docs.
PROOF WITHOUT A KEY (`aethron_code.py --selftest`, 9/9): an in-process
MOCK Anthropic endpoint + the real CLI proves spawn, env routing (no
Anthropic login — the FCC path), streamed text, tool calls, a file
actually changed in the workspace, our MCP tools injected AND isolated
(mcp == ['aethron']), agent calling the guarded pipeline with real
data. Mock lesson: pick the scripted turn from REQUEST CONTENT, never a
counter — the CLI makes auxiliary calls (titles) that desync a counter
and silently "pass" the wrong turn. Also: Write refuses a file it has
not Read, so the script must Read first (real tool contract, not a
convenient one).
Studio API proven end-to-end on a live server: create workspace ->
write/read file -> traversal refused -> site//pristine/ write refused
-> start session -> send -> events streamed -> agent wrote index.html
-> tree updated. IDE layout debugged numerically (pane screenshots are
scaled/unreliable): grid children default to min-height:auto and grew
past the container, putting the chat input 33px below an unscrollable
fold — fixed with .ide>*{min-height:0} + fitIde() measuring the real
top, and the <1100px path stacks and scrolls instead.
NEXT (owner's vision, in order): (1) framework CHOICE for output —
the honest architecture is agent-driven translation with probe as the
acceptance test (probe --compare: same rendered text/DOM as the
original), not a hand-written HTML->JSX transpiler; (2) Figma as a new
L0 source adapter (REST -> frames/tokens), currently unsupported;
(3) hosted gateway = our own Anthropic-compatible endpoint so users
need no keys at all.

## ONE KEY FOR EVERYTHING + the wire translator — 2026-08-20
Owner: whatever API key is set in settings must power BOTH sides —
copy fill/plan/match/heal AND the coding agent. DeepSeek, Gemini,
Anthropic, whatever. Shipped as two new files.
`aethron_brain.py` = the single source of truth: provider registry
(deepseek/anthropic/claude-cli/gemini/openai/openrouter/groq/ollama/
fcc/custom, each with wire+base+default model+where-to-get-a-key),
settings in aethron_config.json "ai" (env overrides), resolve() with an
HONEST `ready`/`why` (local providers need no key; claude-cli needs
none), text_call() for the template side, and anthropic_endpoint()
which returns mode direct | cli-login | bridge.
`aethron_bridge.py` = THE PIECE THAT MAKES IT TRUE. The claude CLI
speaks ONLY the Anthropic Messages API; DeepSeek/Gemini/OpenAI/Groq/
Ollama speak OpenAI's. So we translate, in ~500 stdlib lines we own (no
FCC install, no uv, no Python 3.14): system/messages/tool defs/
tool_use/tool_result both ways, streaming SSE (message_start ->
content_block_start/delta/stop per block, input_json_delta for tool
args -> message_delta/stop) and non-streaming, plus /v1/messages/
count_tokens and readable upstream errors.
TWO BUGS THAT COST TIME, BOTH INSTRUCTIVE:
1. The CLI POSTs `/v1/messages?beta=true`. Matching on the RAW path
   404s it — and the CLI reports that as "There's an issue with the
   selected model … it may not exist", which sends you hunting the
   model instead of the route. Always match urlparse(path).path.
2. In bridge mode NEVER pass --model: the CLI validates the name and
   refuses foreign ids ("deepseek-v4-pro may not exist"). The bridge
   rewrites the model upstream on every request, so the CLI's own
   default name is harmless. The `ready` event then reports the REAL
   model ("deepseek-v4-pro (via Aethron bridge)"), not the CLI's.
Wiring: aethron_code provider "auto" (now the default) asks the brain
for an Anthropic-speaking endpoint; aethron_agent.Model.from_settings()
(base gets /v1 appended when the settings hold the human URL);
studio call_model routes through brain.text_call with per-call
overrides; new GET/POST /api/ai/settings; ai_settings() helper replaces
four copies of "if not st['model']: fail" and returns the brain's own
reason (400 not 500); the browser's localStorage forge_ai is migrated
once then dropped — settings are server-side so CLI/studio/app cannot
drift. Studio UI: ONE aiSettingsHtml() block rendered in the Plan tab
AND the Code view (provider list from the server; model auto-fills;
blank key keeps the stored one); the Code view's own provider/base/
token row is gone.
PROOF (all offline, no key): brain --selftest; bridge --selftest 12/12
(translation both ways + unreachable-upstream error); code --selftest
12/12 — scenario 3 drives the REAL claude CLI through OUR bridge into a
mock OpenAI upstream and the agent writes a file, i.e. a DeepSeek-shaped
key runs the coding agent with no Anthropic account anywhere. Live
studio: save one key -> /api/code/status flips to "DeepSeek via
Aethron's translator", start session with NO per-session config ->
agent creates landing.html in the workspace.

## FRAMEWORK-PORT REFEREE + tests live in the repo now — 2026-08-20
Toward "the user chooses the output framework": the deterministic half
first, because an unverifiable port is worthless. `forge.py probe
--baseline` records a READER-level fingerprint per page (rendered text,
h1-h3 outline, image count) and `--against=<dir|url>` renders the other
build and compares (word-level difflib ratio; FAIL under 90% or any
missing heading). Framework-agnostic by construction: it judges what
the browser renders, never the markup. Proven: site vs itself = 100%,
a hand-written "naive port" = 2% identical, 1/12 headings, 1/59 images,
missing headings named, exit 1. NOTE while testing: degrading the SSR
HTML of a Framer build changes NOTHING at runtime (hydration re-renders
from chunks) — that is the invariant working, and it is why the referee
must measure the rendered DOM.
TESTS NOW LIVE IN `tests/` (twice now a scratchpad battery was lost —
stop writing them there): tests/probe_battery.py (27 checks incl. the
referee) and tests/run_all.py = syntax + brain + bridge + code layer +
probe battery, ~4 min, no key/login/network. Currently ALL GREEN.
NEXT for the framework work: the port itself is an AGENT task (read
site/, write the Next/Astro/Vue app) whose acceptance test is exactly
`probe --against`; then a `forge.py export --framework` wrapper that
scaffolds, runs the agent and refuses to hand over a port the referee
rejects.

## AGENTIC SELF-HEAL — the ladder, then a mind — 2026-08-20
Owner: "we can't just rely on that script… it is only a premade fix".
Correct. `aethron_healer.py` keeps the deterministic ladder as step one
(free, certain, no tokens) and escalates ONLY what it cannot express:
heal -> build -> verify -> probe -> collect EVIDENCE (verify FAILs,
probe runtime failures, report dead entries + __at_risk__, the ladder's
own STUCK lines) -> agent round(s) with the guarded MCP tools ->
build/verify/probe AGAIN -> honest STUCK with everything tried.
The agent never decides success; the checks do (the rule that has held
since the rogue-agent incident).
NEW ENFORCEMENT (this is the part that makes agentic healing safe):
`--settings {"permissions":{"deny":["Write(./site/**)","Edit(./site/**)",
"Write(./pristine/**)",…]}}` is passed to every session in a template
project. PROVEN: the CLI refuses with "Permission to edit … has been
denied" and the file is untouched. Until now PROJECT_RULES only ASKED
the model not to hand-edit generated output; now it cannot. Auto-applied
whenever the workspace has a forge.json (override via cfg["settings"]).
Studio: dead-edits chip runs deterministic heal, and offers the AI
healer only when the ladder is stuck; runs as a job streaming into Logs
(start_fn_job = the job contract for in-process work). Undo covers it —
a snapshot is taken before the agent starts.
tests/healer_battery.py breaks a real project (brand token unfilled ->
verify FAILs), proves the ladder cannot fix it and says so, then lets a
MOCK model drive the REAL CLI + REAL tools: its hand edit of site/ is
denied, its set_content calls land in copy_map, and verify+probe decide.
TWO REAL BUGS THE BATTERY CAUGHT (both would have hit the shipped IDE,
not just the healer):
1. INJECTING our MCP server is not GRANTING it — every mcp__aethron__*
   call came back "Claude requested permissions … but you haven't
   granted it yet", so the agent silently accomplished nothing. Fixed:
   `--allowedTools mcp__aethron` whenever aethron_tools is on (our own
   tools are guarded by construction, so they are pre-approved).
2. forge_mcp.py ignored AETHRON_HOME — it always used the REPO's
   projects/. Inside the desktop app (data in ~/Library/Application
   Support/Aethron) an agent would have seen an empty project list while
   the studio showed a dozen. Now HOME/PROJECTS/LIBRARY follow the env
   like studio.py, and the dirs are created with parents=True.
Test lesson: the first version of the deny check passed VACUOUSLY when
the agent never ran (unconfigured) — it now asserts the forbidden write
was actually ATTEMPTED before asserting it was blocked. Also fixed:
healer resolved the provider from saved settings only, ignoring a
per-call cfg (that was the "unconfigured" bug); and aethron_code's mock
provider now returns (srv, URL) like the bridge's, after a (srv, port)
mix-up put an int in ANTHROPIC_BASE_URL.

## LIVE TEST on the owner's templates + REASONING-MODEL BUG — 2026-08-20
Owner supplied a DeepSeek key and three Framer templates. Migrations
(agero, intelli) were clean first try: scrape -> fetch -> inventory ->
build -> probe CLEAN (agero 65 chunks/370 strings/152 imgs rendered;
intelli 4930 chars/49 imgs). One scrape crash fixed: Framer's CDN ends
big SSR responses early, and a single http.client.IncompleteRead killed
the whole migration — get() now retries and accepts a partial body only
when it contains </html>.
THE BIG FIND — `aethron_bridge` broke on REASONING MODELS. DeepSeek
v4-pro returns `reasoning_content` and then REQUIRES it echoed back:
"The reasoning_content in the thinking mode must be passed back to the
API" (HTTP 400). The Anthropic wire has no such field, so the CLI can
never echo it — every agent conversation died at the SECOND tool call
(~15s, $0.01/round, agent "did nothing"). Diagnosis only became
possible by printing tool_results and the done-error verbatim; the
symptom looked like a lazy model. Fix: the bridge REMEMBERS reasoning
per assistant turn, keyed by the tool-call id that turn produced (ids
survive the round trip through the CLI untouched), and re-attaches it
in to_openai. After the fix the same prompt ran for 10+ minutes of real
work instead of aborting.
Also fixed in the exporter: npm() already prefixes "npm" so
meta["build"] must be ["run","build"] (it ran `npm npm run build` and
fed the agent a meaningless error for 3 rounds); and export sessions now
DENY Bash — Aethron owns the toolchain, the agent only reads and writes
files (it was burning turns shelling out and stopping).
`forge probe --against` earned its keep immediately: both first ports
were graded "0% identical, 0/26 headings, 0/152 images" and REFUSED.
That is the design working — a port that is not the same site is not
handed over.

## PIXEL-PERFECT FRAMEWORK PORT — `aethron_convert.py` — 2026-08-23
Owner's non-negotiable: the framework port must be pixel-perfect like
editor mode, with NO dependency on the source platform — not even a
locally-hosted copy of its runtime ("the user won't be getting what he
paid for"). Correct instinct, and the AI rewrite proved it: 75% text,
$1.83, a design that merely resembles the original.
THE PRINCIPLE, same as editor mode: CARRY, DON'T RECREATE. Measured on
agero before writing any code — strip EVERY script from the rendered
DOM and the page still gives 6,296 chars, 152 images, 26/26 headings,
99% identical. The design is entirely in the CSS Framer emits; the
runtime contributes NOTHING to layout/type/colour/spacing. It has one
visible job: parking elements at low opacity until they animate in.
So: read the post-hydration DOM, strip all scripts, recover the parked
entrance states onto `data-ae`, emit astro/next/vite, ship a ~20-line
IntersectionObserver of OUR OWN that replays them. Result on agero:
text 6296=6296, images 152=152, 99% referee, 12 elements parked below
the fold vs the original's 13 (motion genuinely replays), 1 script tag
(ours), 0 platform links, 0 .mjs, 0 .framercms.
FOUR BUGS FOUND BY MEASURING, EACH INVISIBLE TO A CONTENT SCORE:
1. `data-ae` was appended AFTER the closing `>` — attribute became page
   text. Insert inside the tag.
2. Any translate/scale counted as an entrance -> 197 "entrances" on a
   page with ~49 hidden elements. `translate(-50%,-50%)` is CENTRING;
   stripping it moves the layout, and the text score still says 99%.
   Rule: an entrance requires the element to be HIDDEN.
3. "Hidden" by regex matched 0.18 as well as 0.001. Framer parks at
   BOTH 0 and 0.001, and uses 0.06/0.18 as real design values — parse
   the number, threshold at 0.05. (Also: my "49 hidden" was wrong; the
   true count is 170, because I grepped opacity:0 and missed 0.001,
   which our own notes had already documented.)
4. The motion runtime was written to public/ but never REFERENCED —
   181 entrances sat inert in the build. Ship != wire.
THREE LEAKS THE PORT SHIPPED UNTIL CAUGHT: a live href to
framer.com/@nframe/?tab=marketplace (editor mode only CSS-hides those),
30 modulepreload tags making the browser fetch 5.5MB of chunks nothing
runs, and assets/chunks + assets/cms copied wholesale. All stripped;
the content those chunks rendered is already baked into the carried DOM.
Referee gained: animated counters (numeric headings) are not required
verbatim — they are one frame of a count-up; and a route fallback
(/about vs /about.html) because grading a 404 reads as "0% identical".
PROVEN ON: agero (Framer, 11 pages, 223 assets). STILL TO PROVE: a
Webflow project (test-2), intelli/spartanai, and the next/vite emitters
(only astro is measured end-to-end).

## OWNERSHIP IS NOW GRADED, NOT ASSUMED — 2026-08-23
Webflow (test-2) converted and scored "100% identical, 116/116 images"
while shipping 112 refs to cdn.prod.website-files.com. It rendered
perfectly ONLY because the machine had internet: a user self-hosting
that port owns nothing. The content referee was blind to it.
`_platform_refs()` + PLATFORM_HOSTS now FAIL any port still pointing at
framerusercontent/framer.com/website-files/webflow/cloudfront, naming
the hosts and the fix (localize, rebuild, convert).
Cause chain, all fixed: (1) the converter copied only site/assets — a
Webflow build keeps localized files in site/remote-assets/, so 5 of 160
files came across and the rest silently fell back to the CDN; it now
copies every asset dir. (2) test-2's own build had drifted un-localized.
(3) THE BADGE: `<a class="w-webflow-badge">` with 2 images off Webflow's
CDN. Editor mode can only CSS-hide it (the runtime re-creates it) — a
converted port has NO runtime, so it is physically deleted, along with
framer badge containers. Port images 114 vs the original's 116 = exactly
those two. (4) preconnect hints were stripped by a pattern expecting
rel= before href=; real tags ship href first, so it matched nothing.
Order-independent now.
TWO REPORTING BUGS THAT HID REAL VERDICTS: the converter's referee
filter only forwarded lines starting PASS/FAIL/missing, so "NOT OWNED"
never reached the log — the failure looked inexplicable. And the port
was judged on the probe's EXIT CODE, which folds in the ORIGINAL
template's runtime health: test-2 ships 2 console errors, which
condemned a perfect port for a defect it faithfully inherited. The port
is now judged on the comparison line alone.
STATE: agero (Framer, 11 pages) 100% identical, 26/26 headings,
152/152 images. test-2 (Webflow) 100% identical, 114/116 (badge gone).
Both PIXEL-PERFECT PORT READY, astro target, $0, deterministic.
OPEN AND HONEST: next/vite emitters are written but UNMEASURED and
predate the section-split refactor. And Webflow motion beyond parked
entrance states is NOT reproduced — test-2 drops 22 scripts including
GSAP + webflow.js IX2. Framer works because its parked states sit in
the DOM; GSAP animates imperatively with nothing to recover. Webflow
needs its own policy (localize and keep those libraries, as Webflow's
own export does).

## THE SOURCE MAPS — inference retired — 2026-08-25
Owner asked (via two Grok memos) whether a tool exists that maps every
animation and its CODE, organizes it, and transforms it per framework.
Answer: not for Framer/Webflow — grok-1 is entirely Figma (Motion
Export plugin, Figma Motion Dev Mode); grok-2's Framer advice is the
runtime-preserving exporters (PullPage/ExFlow), i.e. the black-box
tradeoff we already rejected, and it concedes no tool does the
inventory→conversion job. But grok-2 named one lever worth taking:
`document.getAnimations()`. Ran it on live agero: 5 animations, ALL
marquees, exact (59280ms, translateX(0)→(-2964px), linear = 50px/s;
another at 100px/s — matching the `speed:` configs found in chunks, so
two independent readings agree). Its silence is data too: scroll-linked
scale and counters are rAF+inline-style, never WAAPI.
THE REAL FIND, from reading a chunk's last line: every Framer chunk
ends `//# sourceMappingURL=<name>.mjs.map`, and the CDN SERVES those
maps with `sourcesContent` — the AUTHORED SOURCE of the template's own
components, unminified, real names:
    import{motion,useScroll,useTransform}from"framer-motion";
    useScroll({target:ref,offset:["start 0.75","start 0.15"]})
agero: 62/65 maps served, 120 authored modules. The owner's four
standing complaints are literally named files — SlideShow.js (33KB),
Ticker.js, AnimatedNumberCounter_Prod.js ({type:"spring",duration:1,
bounce:0}), Reveal_Text.js. Reveal_Text is the "Effortless design…"
text: a scroll-driven PER-CHARACTER COLOUR ramp (#5c5c5c→#000000) —
which is exactly why every timeline capture missed it, since it moves
neither transform nor opacity. Inference could never have found that;
reading found it in one grep. Confirmed general, not an agero fluke:
intelli/sadewa/gravitest/jomiez-lesmana all serve maps. Only Framer's
vendored bundles (react/framer/rolldown) 403 — no loss, the engine is
framer-motion from npm.
SHIPPED `aethron_source.py`: recover() fetches every map and expands
sourcesContent into pristine/sources/ (authored vs vendored split);
build_map() writes animations.json — per component the behaviours,
the literal config at each animation site with line+col+excerpt
(Framer emits one long line, so the COLUMN is what locates code), the
elements it renders (matched by framer-* class through the import
graph, since code components declare no class of their own), and the
`framer` package surface to shim (50 exports on agero). Webflow
reports UNAVAILABLE, never an empty pass. convert() now calls it and
ships ANIMATIONS/ + README into every port — authored modules ONLY,
never Framer's vendored runtime (that is the vendor's code and the
port does not depend on it).
TWO BUGS WORTH REMEMBERING: (1) `className="framer-X"` literal
matching found 16 of 120 modules and MISSED every animating one —
generated components build classes with cx(scopingClassNames,…) and
template literals, so collect the framer-* TOKENS instead. (2) The
token regex must be whole-token guarded both ends: without the
lookahead it truncated `framer-link-hover-text-color` to `framer-link`
and credited one component with 8,957 elements; without the lookbehind
it matched inside `--framer-*` CSS custom properties. Sane numbers
after the fix (SlideShow 59 elements, Ticker 179).
STILL OPEN: this is stage one — the map. Stage two is the port that
CARRIES these components (they are plain React + framer-motion) behind
a shim for the proprietary `framer` package. Not attempted yet.

## THE ENTRANCE RECORDER — the owner's hero, fixed — 2026-08-25
The "Effortless design…" heading the owner named twice was NOT
Reveal_Text.js (that component is recovered but the template never
mounts it). It is Framer's APPEAR animation on split text: each
character parked in the SSR at opacity:0.001, filter:blur(10px),
translateY(10px). The port shipped them at opacity:1/blur(0px) with no
data-ae — because recover_entrances reads the POST-JS DOM, and by dump
time the entrance has finished and the parked pose is gone. Nothing to
detect, so nothing was recovered. That is the whole bug.
THE FIX — read the animations, do not watch the pixels. Entrances are
Web Animations, so the browser will hand over the exact keyframes and
timing — but only if you look EARLY and ACCUMULATE: agero showed 1
animation at t=0, 19 at t=60ms, 7 by t=140ms (they finish and are
collected; that is why a single late getAnimations() call sees only the
5 marquees). `aethron_motion.ENTRANCE_JS` polls from the first frame,
dedupes by target+props+duration+delay, and stamps data-ae-id.
Measured on agero: 130 animations, 103 of them OUTSIDE the appear
engine's reach — exactly the gap. The heading came back as 20
animations, two per character (opacity + filter), delays 200/250/300/
350/400/450/500/550/600/650 — the 50ms stagger, exact. The easing is a
`linear(0 0%, 0.024 2.56…)` FUNCTION: framer-motion had already
flattened its spring, so replaying it needs no spring integrator at
all. MOTION_JS gained playRecorded(): el.animate(frames, timing) hands
the original's own animation objects straight back to the browser.
Marquees start at load; everything else parks and plays.
THREE MEASUREMENT TRAPS, each of which faked a result:
1. Under --virtual-time-budget the ANIMATION timeline barely advances —
   document.timeline.currentTime was 289ms after 1800ms of setTimeout,
   so every animation sat at progress:0 and the page looked dead while
   being perfectly correct. Do not judge motion by sampling opacity in
   headless; SEEK the animations (a.currentTime = T) and read what a
   viewer would see at T. That reproduced the travelling wave.
2. My own print formatting lied: f"{0.999:.2f}" is "1.00", and slicing
   the leading char gave ".00" — two cells read as broken in an
   otherwise perfect profile. Check the formatter before the code.
3. Three identical runs gave the full entrance twice and NOTHING the
   third time. Cause: the runtime's whole wiring block sat behind a
   double requestAnimationFrame, which under starvation never fired.
   Now `setTimeout(wire, 150)` backs it and in-view elements play
   immediately instead of waiting for an IntersectionObserver (an
   observer that also failed to fire for a plainly visible element).
   This is NOT a test crutch: a throttled rAF is ordinary in a
   background tab, and without it the content guarantee was the only
   thing between a user and a blank hero.
tests/motion_battery.py (15 checks) now measures this directly —
recorder finds staggered delays, port animates every character, nothing
visible at start, all arrived at end, opacity never travels backwards,
mid-flight the characters are staggered rather than in lockstep, and
the port's marquees match the original's durations. Wired into
run_all.py, which also gained a 900s per-suite timeout: the healer
battery had been HANGING inside heal() behind a 30-minute inner
timeout, so `tail -25` printed nothing and exit 0 read as success — a
hang is the worst vacuous pass. That hang is real, pre-existing and
still unfixed (it blocks after "with the agent"; the mock provider
means it costs nothing, so it is a blocked subprocess, not an API
call).
NOTE: agero grades 134/152 images, not 152/152 as recorded above. The
18 are inside a marketplace promo card (framer.com/@nframe/?tab=
marketplace) that strip_platform correctly deletes — same category as
the Webflow badge, not a regression. Bisected to be sure: capture is
152 with and without the recorder.
COVERED by this mechanism: entrances (incl. per-character stagger) and
marquees. NOT covered, because they are rAF + inline style rather than
WAAPI: the scroll-linked SlideShow scale and the counters. Those need
the source-map components (stage two).

## THE HEALER "HANG" WAS THREE AGENT-RUNTIME BUGS — 2026-08-25
tests/healer_battery.py had been hanging for ~30 minutes and exiting
with nothing, which read as success. Chased it properly with
faulthandler.dump_traceback_later (the right tool: no py-spy needed,
and it names the exact blocking line in every thread).
THE SURFACE CAUSE was the test's own mock model looping forever —
text -> mcp__aethron__set_content_bulk -> tool_result -> repeat, every
1.3s. It picked its script position by counting tool_result blocks in
the request, but THE CLI ALSO MAKES AUXILIARY CALLS (conversation
titles, summaries) carrying a fresh, tiny message list with no history:
measured, requests 2 and 4 of a four-request turn were auxiliary and
showed ZERO tool_results, so the count never passed 1. The mock's own
docstring warned that a TURN counter drifts; a content counter drifts
too, just differently. Fixed structurally — read the tool_use blocks
the ASSISTANT actually emitted. Substring markers do not work here at
all: HEAL_PROMPT names mcp__aethron__set_content_bulk and its evidence
contains both "applied" and "rejected", so the first rewrite made the
mock skip its opening move and the deny check went VACUOUS — caught
only because that check asserts the forbidden write was ATTEMPTED
before asserting it was blocked. That guard earned its keep.
THREE REAL PRODUCT BUGS underneath, all in aethron_code and all
inherited by heal() in the studio and MCP tool #23:
1. THE DEADLINE WAS NEVER ENFORCED. `for ev in s.events(timeout=max(1,
   deadline - time.time()))` — events() is a GENERATOR, so the argument
   is evaluated once and becomes a 30-MINUTE ALLOWANCE PER EVENT, and
   the `if time.time() > deadline` guard only ran AFTER an event
   arrived. events() now polls at 2s granularity; timeout is a real
   total budget (returns at 6.6s on a 6s budget).
2. NO IDLE LIMIT: a wedged child and a slow one looked identical. Added
   idle=300 (fires at 6.2s on a 600s budget) and the error says WHICH
   limit tripped — "went silent" and "did not finish" are different
   faults with different fixes.
3. NOTHING BOUNDED A LOOP. This CLI has NO --max-turns (checked), and
   idle cannot help because a loop is never silent, it is busy. Added
   repeat_limit=6 on identical consecutive tool calls: stops the real
   loop at 1.7s instead of spending the whole 600s budget. With a live
   provider that is the difference between cents and a whole budget on
   one wrong idea.
4. MCP CHILDREN LEAKED: the MCP servers are children of the CLI, so
   terminating the CLI orphans them (one forge_mcp.py was found still
   running from an earlier session). Matters more now that sessions are
   stopped deliberately. Popen gets start_new_session=True and close()
   signals the process GROUP. Measured: 1 spawned, 0 alive after close.
Battery 11/13 -> 13/13, code selftest still 12/12.
SHELL LESSON, twice in one session: `run_all.py | tail -20` reports
TAIL's exit code, so a failing suite looked like a pass — the exact
vacuous-pass trap that run_all's own new 900s timeout was added to
prevent. Never judge a suite through a pipe; redirect to a file and
read $?.

## STAGE TWO: rAF MOTION, AND FOUR BROKEN INSTRUMENTS — 2026-08-25
Owner confirmed entrances + marquees are 100% identical; asked for the
rest. Built tests/motion_gap.py FIRST (scroll both builds, diff what
changed) instead of assuming — and three of the things I set out to fix
turned out not to exist:
- COUNTERS ARE NOT ANIMATED. In a real browser 1+ -> 26+ takes 8ms
  (t=5ms to t=13ms, sampled every frame for 3s). A one-shot text swap,
  not a count-up. The port already carries the final values.
- SlideShow's call-site props read effectsScale:1, effectsRotate:0 —
  the scroll effects are OFF on agero. I would have built for an
  animation this template does not have.
- 4 of 5 "scroll-linked" elements are static; the gap tool had counted
  entrance-parked poses as scroll states.
THE ONE REAL CATEGORY: continuous rAF motion — a badge rotating by
inline style at 71.86 deg/s (5s turn) plus three 6s float loops. No
animation object, no entrance, no scroll key: invisible to everything
built so far, so the port shipped them frozen.
THE BLOCKER AND THE FIX: virtual-time headless CANNOT measure rAF —
the same badge reads 0.60 deg/s under it, 120x slow and not off by any
constant, because the rAF loop is starved relative to the clock. So
`capture_realtime()` runs a SECOND load with NO --virtual-time-budget
and the page POSTs its findings back over HTTP (--dump-dom is what
needed the virtual clock in the first place). It measures 71.85 deg/s,
agreeing with a real browser to 3 s.f.
`analyse_continuous()` fits pure rotations exactly from their rate,
finds loop periods by autocorrelation, and REPORTS what it cannot
establish rather than guessing — an auto-advancing carousel (75px every
~3s, accumulating) is named with its reason because its cycle exceeds
the capture window. Selectors are framer-* classes, not stamped ids:
the real-time pass is a different page load from the one the port's DOM
comes from.
FOUR MEASUREMENT BUGS IN MY OWN TOOLING, each giving a confident wrong
answer. Worth remembering as a class:
1. A SYNCHRONOUS scroll loop never lets the browser render, so rAF
   never fires and nothing updates — it "proved" five elements static.
   Yield between stops (await sleep + rAF) or measure nothing.
2. Period-by-autocorrelation is dominated by FLAT stretches: a carousel
   resting between bursts fitted at 240ms instead of ~3s. A candidate
   period must be required to CONTAIN the motion, checked over any
   window, not just the first.
3. The capture window must be >= 2x the longest period worth finding
   (only lags up to half the window are testable). A 9s window reported
   "no loops" for three 6s loops — a silent-looking failure.
4. THE WORST ONE: under a virtual clock the ORIGINAL's appear engine
   never fires for below-the-fold elements — a header and a notch sat
   at opacity:0.001 for a whole 12-stop scan. The gap tool was
   comparing a correct port against a BROKEN RENDERING OF THE ORIGINAL
   and reporting the port as faulty. It now runs on capture_realtime
   too. Switching it moved the original's own count 63 -> 111.
GAP TOOL LIMITATION, now in its docstring: it can only judge PERSISTENT
motion. One-shot entrances are separate page loads with their own
timing, so the same element is routinely mid-flight in one build and
finished in the other; checked directly, every element on one such
"missing" list carried a working entrance in the port. Use
motion_battery for entrances (it SEEKS animations instead of hoping to
sample the right instant).
Battery 19->20: the marquee check is now DIRECTIONAL (every original
marquee must be in the port) because the port legitimately runs MORE —
rAF motion re-expressed as real animations is the point, and asserting
equality failed the port for doing its job.
STATE on agero: 141 entrance anims + 1 rotation + 3 loops reproduced;
1 carousel reported, not guessed. Referee 99% identical, 26/26
headings.

## SELF-UPDATE THAT ACTUALLY WORKS — two bugs, one fatal — 2026-09-05
The owner's standing demand ("auto update itself just like Claude does")
had a mechanism that passed 18/18 offline and would have BRICKED every
user's app on the first real update. Both faults only appear when you
publish for real and then LAUNCH the result.
1. A PRIVATE REPO CANNOT SERVE ITS OWN UPDATES. zenadell/Aethron is
   private; an installed app checks anonymously and api.github.com
   answers 404, so a carefully published release is invisible and the
   owner is back to dragging zips into /Applications. `feed_url()` now
   prefers a plain JSON file the app can read with no key —
   AETHRON_UPDATE_FEED, then cfg["update_feed"], then
   <supabase>/storage/v1/object/public/releases/<mac|win|linux>.json,
   GitHub last. check() accepts BOTH shapes ({version,url,notes} and a
   GitHub release), so the feed can move later without a new build.
   `publish_release.py` uploads zip + feed to a public Supabase bucket
   (service key from the env, never the repo, never the bundle — the
   app only ever READS, and reads need no key), writes the feed LAST so
   users are never pointed at an asset that failed to upload, and reads
   both back ANONYMOUSLY to prove it the way an installed app sees it.
   release.sh calls it; the gh release is now optional decoration.
2. THE FATAL ONE — zipfile.extractall DESTROYS SYMLINKS. The build
   ships 81 of them (Python.framework/Versions/Current and friends);
   Python's extractor writes each one out as a REGULAR FILE. The result
   looks perfect — right size, right layout, valid-looking bundle — and
   macOS refuses it with "Launchd job spawn failed" (POSIX 111) because
   the framework layout is gone and the signature no longer matches.
   MEASURED: build 81 symlinks / installed copy 0, `codesign --verify`
   "code object is not signed at all". `_extract()` now uses ditto on
   macOS (the same tool build_desktop.sh uses to CREATE the archive —
   use the matching tool to open it), keeping the zip-slip guard on the
   listing so it protects whichever extractor runs. `_runnable()` runs
   codesign BEFORE the swap (ad-hoc re-signs once if unsigned, refuses
   otherwise), so a damaged download never replaces a working app.
PROVEN END TO END, not asserted: installed 1.0.1 -> served a real 1.0.2
zip from a local feed -> app updated ITSELF -> bricked, launchd refused.
Fixed, rebuilt, repeated: 1.0.2 -> 1.0.3, same path, ONE copy, 81
symlinks intact, signature valid, WINDOW CAME BACK, and the session
survived (still logged in — no re-login through an update).
Selftest 18 -> 27, incl. a bundle carrying a real symlink through the
real download path — the check that would have caught the fatal bug.
Also: aethron.spec now reads CFBundleShortVersionString from
aethron_update.VERSION. They had drifted (plist 1.0.0, updater 1.0.1),
which is how you end up certain you are testing a build you never
installed. And studio's launch check uses timeout=8, not 30.
MEASUREMENT LESSON, the same one as the pkill incident: my port scan
found 8899 answering and I nearly reported on it — it was a stale
`python studio.py` from earlier in the session, PID 2118, while the app
I had just launched sat on 8900. Resolve the port FROM THE PID
(lsof -a -p <pid>), never by scanning for whoever answers first.

## THE INSTRUMENT'S READING WAS DELETED BEFORE ANYONE READ IT
## — 2026-09-07
Chasing the keep_runtime entrance regression found a bug affecting
BOTH conversion modes, and it is the purest example of the class this
week has been about.

`strip_instrumentation()` removes every `<script id="__ae_*">` from the
capture, with the correct reasoning in its own docstring: "the
instrument is not the result". But the RECORDER writes what it measured
into exactly such a node, and `motion.entrance_spec(got["dom"])` reads
that node FIFTY LINES LATER — after the strip. So entrance_spec has
been returning `{"anims": []}` on every conversion, silently, and no
port has shipped a measured entrance recording since.

MEASURED, before and after: the phrase "N animation(s) measured from
the live runtime" appeared ZERO times across an 11-page conversion;
after reading the node before stripping it, the same conversion reports
178 / 330 / 163 per page. The fix is one line moved.

The instrument is not the result — but its READING is, and it has to be
taken before the instrument is thrown away.

Also fixed alongside it: `anim_tag()` did `return ""` under
keep_runtime ("the original's code is already in the body"), which
DISCARDED the engine's own data tags it had just accumulated and
returned before MOTION_TAG was appended, so the port shipped no engine
data and no runtime of ours; and the keep_runtime capture branch set
`n_ae = 0` instead of calling recover_entrances. Measured on agero:
data-ae attributes 0 -> 496, `__ae_entrance` absent -> present, content
unchanged at 100% identical / 26-26 headings / 152-152 images.

## IDENTITY FIXED, ENTRANCE STILL WRONG — measured 2026-09-08
The named blocker below is CLOSED. The runtime now reports on itself
(<html data-ae-stats>), which turned a ten-minute regenerate-and-guess
cycle into one number:

    {"anims":141,"byId":0,"byPath":127,"lost":14,"noframes":0,
     "held":27,"parked":49,"played":49,"engineFired":0}

  byId 0      every stamped id still fails — the diagnosis was right
  byPath 127  the structural-path fallback RESOLVES them
  played 49   49 elements parked and animated
  h1          all 10 characters carry data-ae-done: our runtime ran

WHAT WAS ADDED: the recorder emits `path` per animation — nearest
ancestor with framer-* classes (verified document-unique) plus an
nth-child chain, since per-character spans carry no classes of their
own and the class-only selectorOf used by the continuous pass cannot
reach them. compress_entrance carries it as "p"; the runtime tries the
id first and falls back to the path, accepting it only when it matches
exactly one element.

STILL RED, and honestly so. The animation RUNS but from a pose that is
already visible. Dumped after load, a character reads

    transform: translateY(10px); filter: blur(0px); opacity: 1;

translateY(10px) is the PARKED transform while opacity is already 1 and
blur already 0 — a mixed state, so a viewer sees no entrance. The
battery's "nothing is visible at the start" is reporting exactly that
and it is correct to fail.

NEXT, and it is a narrow question now: parkRecorded saves `was` = the
element's CURRENT inline style and then writes frame[0] over it;
playRecorded restores `was` and animates. On a carried-runtime port the
SSR html ALREADY carries Framer's parked pose inline, so `was` is
itself a parked pose and the restore puts back a half-parked state
while the engine has meanwhile set opacity to 1. Suspect the interaction
between the carried inline parked styles and our park/restore pair.
Read the actual values at each step before changing anything — three
regenerate cycles were spent this session on hypotheses that measuring
would have settled in one.

DO NOT relax the battery to reach green. Green today would mean the
suite stopped noticing an entrance a user does not see.

## (closed) THE REAL BLOCKER: element identity does not survive keep_runtime
## — measured 2026-09-08, supersedes the "gates" diagnosis below
The four motion checks are still red and the cause is now MEASURED,
not inferred, and it is not the gates.

    __ae_entrance in the shipped port : 141 animations, 27 appear-owned
    h1 spans in the shipped port      : 11
    h1 spans carrying data-ae-id      : 0

The runtime finds an element by
`document.querySelector('[data-ae-id="' + a.id + '"]')`. The RECORDER
stamps data-ae-id into the CAPTURE dom; keep_runtime ships the SSR html
instead. So the ids exist only in a document that is discarded, every
lookup returns null, and `if (!el) return` fires BEFORE any gate is
reached. That is why fixing the gates changed nothing — they were a
real flaw, and they were downstream of this one.

THE FIX, and the precedent is already in this codebase: identify
elements by a STABLE SELECTOR, not a stamped id. `analyse_continuous`
solved exactly this and its note says why — "Selectors are framer-*
classes, not stamped ids: the real-time pass is a different page load
from the one the port's DOM comes from." The entrance recorder needs
the same treatment: emit a framer-* class path (plus an index among
matches) alongside the id, and have the runtime fall back to it when
the id is absent. Then the recording survives ANY document, which is
what keep_runtime needs and what the non-carry mode gets for free
because it ships the capture itself.

Scope: change ENTRANCE_JS to compute the selector per element, widen
the recording's per-animation keys (currently a/d/i/s), and give the
runtime an id-then-selector lookup. Then regenerate (~10 min) and run
motion_battery. Do not relax the battery.

## KEPT FROM THAT WORK (sound, and needed regardless): arbitration
ENGINE was `typeof animator !== 'undefined'` — it tested whether
Framer's appear engine OBJECT EXISTS, never whether it animates. In a
carried-runtime port it exists and does nothing. Three gates deferred
to it on that basis, two of them written as
    if (ENGINE && ...) return;
    if (!ENGINE && ...) return;
which is an unconditional skip in the costume of a decision.

Now: a watcher polls from the earliest moment for any appear-element
actually animating (early and accumulating, the lesson the recorder
already taught), recordings the gates hand over are HELD rather than
dropped, and if the engine never fired they are played after 1300ms.
Two systems cannot fight if the second acts only where the first did
nothing. This is correct and stays in; it simply cannot help until the
elements can be found at all.

## THE MEMORY FILE WAS DESTROYED AND RECOVERED — 2026-09-08
The owner asked (twice) why the project's memory was named for the
model rather than the product. Renaming it, `git mv` failed — the file
is GITIGNORED and was never tracked — and the very next command,
`cat > CLAUDE.md`, overwrote 105KB of accumulated lessons with a
one-line pointer. No git copy, no backup: a failed command followed by
an unchecked overwrite.

Recovered in full from the session transcripts under
~/.claude/projects/<project>/*.jsonl, which record the file verbatim
each time it is auto-loaded. Longest copy wins — a shorter one is a
truncated snippet and would have silently lost sections.

THREE RULES OUT OF IT:
1. NEVER `cat >` a file whose preceding command failed. The failure is
   information about the target's state.
2. This file is the project's institutional memory and it was ONE
   COMMAND from being gone forever. It is now tracked in git (removed
   from .gitignore) so history is the backup.
3. It lives at AETHRON.md. CLAUDE.md remains a one-line `@AETHRON.md`
   import ONLY because the harness auto-loads that exact filename;
   deleting it would silently stop the context loading at all. The
   product's memory carries the product's name.

## STILL OPEN: the port defers to an engine that does not fire
The four motion checks remain red, and the cause is now NAMED rather
than suspected. THREE gates in MOTION_JS hand appear-elements to the
carried engine:

    662:  if (ENGINE && a.appear) return;              recorded animations
    689:  (ENGINE ? [] : byId).forEach(...)            exact-spec elements
    702:  if (ENGINE && el.hasAttribute('data-framer-appear-id')) return;
    703:  if (!ENGINE && el.hasAttribute('data-framer-appear-id')) return;

Lines 702-703 are worth reading twice: written as if ENGINE mattered,
both branches return — an unconditional skip in the costume of a
considered decision.

The principle behind the gates is right ("two systems animating one
element fight over the same style"). The flaw is that ENGINE tests
whether the engine is PRESENT, never whether it WORKS. Under
keep_runtime it is present, does not animate, and all three of our
mechanisms switch themselves off in deference to it.

THE FIX IS ARBITRATION, NOT ANOTHER GATE: let the runtime check, once,
whether anything is actually animating an appear-element shortly after
wiring, and only defer to the engine if it is. Two systems cannot fight
if the second acts only where the first did nothing. Needs its own
measurement cycle (regenerate the port ~10 min, then motion_battery) —
do NOT change it without one, and do not relax the battery.

## (superseded) OPEN REGRESSION: keep_runtime ports lost per-character
## entrances — found by the full suite, 2026-09-07
`convert(keep_runtime=True)` (the default) carries the original's own
animation engine instead of recovering parked entrance states onto
data-ae. Content is perfect — agero regrades 100% identical, 26/26
headings, 152/152 images — and MARQUEES animate from the carried
engine. But the per-character APPEAR entrances do not play:
tests/motion_battery measures withAnims=[0,0,0] over n=[10,10,10]
characters on a FRESHLY regenerated port, while "the recorder found
animations" and "the port has a per-character heading" both pass. So
the recorder works and the characters exist; the carried engine simply
never fires their entrance.

This is the capability the owner confirmed as "100% identical" under
the PREVIOUS mechanism (data-ae + our own IntersectionObserver
replaying el.animate). keep_runtime traded it away silently — nothing
failed, because convert reports success on content alone.

NOT YET FIXED. The suite is correct to be red here; do not "fix" it by
relaxing the battery. Two honest options: make the carried engine's
appear trigger fire in the port, or run BOTH mechanisms (carry the
engine AND recover entrances) and let the battery decide.

Verified pre-existing: the same 4 checks fail identically against the
pre-2026-09-06 forge.py, so it is not from that day's work.

## ROADMAP (owner, 2026-09-08): GROW the template, do not just fill it
NOT NOW — the owner was explicit: finish adaptation and the open
regressions first. Recorded so it is not lost.

THE ASK: a user wants something the template does not contain. The
template ships four team slots and they need ten. Or they want a
section that was never designed. Today Aethron can only CHANGE what
exists — every mechanism (copy_map, byte-locked CMS, per-slot image
overrides, remove) edits or hides slots the designer already drew.
Adding a fifth card is outside all of it.

WHAT THE OWNER ACTUALLY SPECIFIED, in their words: the new thing must
use "the design logic, the style, the animation" OF THAT SPECIFIC AREA
— not a generic component, and not a model's idea of what matches. A
fifth card must be the fourth card's twin: same classes, same nesting,
same entrance animation with the stagger continued, same hover variant.

WHY THIS FITS THE EXISTING GRAIN (and is not a new product):
  * the template already contains the pattern, N times over. Card 4 IS
    the specification for card 5. This is CARRY, DON'T RECREATE applied
    to structure instead of to a whole page — the same principle that
    made the framework port work, and the reason it should be
    deterministic rather than generated.
  * `_locate_element` + the editor's breadcrumbs already find "the card
    that contains this text" and climb to it. Cloning the located
    subtree is the inverse of `_remove_nth_element`, which already does
    balanced-tag surgery safely and REFUSES when it cannot locate.
  * the entrance recorder reads per-element delays exactly (the 50ms
    stagger on agero). A cloned card can be given the NEXT delay in the
    series rather than a guessed one.

THE HARD PARTS, named now so they are not discovered late:
  1. FRAMER HYDRATION. A card added to the SSR HTML is erased the
     moment React re-renders from the chunks — the rogue-agent incident
     in miniature. Cards on Framer come from CMS collections or from
     chunk data; growing a list may mean growing the CMS binary, which
     is BYTE-LOCKED. Either a new size-lock strategy, or the addition
     lives in a layer hydration cannot revert. Unsolved, and it is the
     crux.
  2. Webflow is far easier (static markup, no hydration) and is where
     this should be proven first.
  3. The framework PORT is easiest of all — it has no runtime to fight,
     which may mean "add new content" is a port-only capability at
     first. Worth saying out loud rather than promising it everywhere.
  4. ACCEPTANCE: what proves a grown card is right? Probably the pixel
     referee against a HAND-PLACED expectation, plus "the clone's
     computed style matches its sibling's" — a card that renders
     differently from its twin is a failure however good it looks.

DO NOT start this until the motion regression is closed and the
self-heal work the owner asked for is finished.

## I ATTACKED MY OWN AUDITOR AND IT LOST, 11 TO 1 — 2026-09-08
The owner asked for the new work to be retested unprompted and harder.
aethron_audit's selftest was 18/18 — which proves only that each rule
fires on the example ITS AUTHOR IMAGINED. So tests/adapt_battery.py
asks the opposite question: what does a lying instrument have to look
like to get PAST it?

FIRST RUN: 12 hostile verdicts, 1 caught, ELEVEN through.

    work={"files": "many"}            a word is not a count
    work={"files": 0, "status": "ok"} a string key hid an all-zero result
    work={"files": -5}                impossible, and not zero
    work={"seconds_elapsed": 12}      the clock is not the work
    status "pass" (lowercase)         audited by NOTHING
    status "MOSTLY_OK"                a tool defining its way out
    template_remaining vs templateRemaining   never compared
    measures {"identical": "99%"} vs "12%"    strings skipped
    NaN                               fails every comparison silently
    no artifact named                 can never be stale
    bounds declared, value a string   CRASHED the rule

The last one was caught only because the auditor is held to its own
standard and reported its own crash. That guard earned its keep.

ALL ELEVEN CLOSED: work must be a positive, finite, non-time COUNT;
an unrecognised status is its own finding (UNKNOWN); measure names are
normalised before comparison; numeric strings and percentages parse;
NaN and infinities are IMPOSSIBLE; bounds with a non-numeric value are
IMPOSSIBLE instead of a crash; a PASS naming no artifact is an
UNCHECKABLE note.

TWO FALSE GREENS IN MY OWN TESTS, both instructive:
1. After the fixes the battery said 12/12. It was lying: the
   trivial-criterion attack was "caught" by UNCHECKABLE — a rule with
   nothing to do with it — because that verdict happened to name no
   artifact. ONE RULE MASKING ANOTHER IS A FALSE GREEN. Every attack
   now names a fresh artifact so only the rule under test can fire.
   True score: 11 caught, 1 hole.
2. Adding UNCHECKABLE broke three selftest checks, because
   `trustworthy` was `not findings` and UNCHECKABLE fires on almost
   every honest verdict. A rule that flags everything is a rule people
   learn to ignore — the probe taught this once already. Notes are now
   separated from disqualifying findings.

THE ONE HOLE LEFT, open on purpose:
    Verdict(PASS, work={"files": 9}, evidence={"criteria_checked": 1})
UNFALSIFIABLE only fires at zero criteria, and whether a criterion is
MEANINGFUL is not decidable from outside — nine files with one genuine
criterion is a legitimate check. A ratio heuristic would cry wolf on
honest runs. It stays named and printed rather than papered over.

LESSON, general: a suite that only asserts things work will one day
report a broken product as healthy. Write the adversarial half.

## Invariants (do not break)\n\n## Invariants (do not break)

## Invariants (do not break)
- `pristine/` is never modified; `site/` is never hand-edited; every
  change flows through `copy_map.json` + `build`.
- CMS replacements must keep exact byte length (space-padded) and the
  SAME padded text must go into HTML and chunks (hydration equality).
- New strings must never contain backticks or `${`.
- Badges/promos are hidden with CSS, never removed from the DOM.
- Builds are STATIC-HOST SAFE (2026-07-14): the chunk range guard is
  patched to slice full-file responses client-side and icons ship as
  real .js — but serve.py/studio/backend must KEEP the exact protocols
  (framercms `?range=` slices, `.js@*` → text/javascript) for previews
  and for pre-hardening builds.
- React #418/#422 console warnings are pre-existing export artifacts —
  verify against the untouched original before "fixing" anything.
- A CHECK THAT CANNOT RUN REPORTS SKIPPED, NEVER PASS (the vacuous-pass
  lesson: Framer checks "passing" on a Next.js site shipped a blank
  page as healthy). Applies to verify, probe, and anything added later.
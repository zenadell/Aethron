# The Template Migration Playbook

**Audience: any AI model (DeepSeek, Gemini, Anthropic, GPT, a 7B local
model — anything that can edit a JSON file), or a careful human.**

This playbook + `forge.py` migrate a Framer or Webflow template export
into something you fully own — your brand, your text, your images, no
platform badge, no telemetry — while keeping the design and animations
100% pixel-identical. The method was proven start-to-finish on a real
product (HandGrid template → Servly marketplace) and then re-proven by
this tool automatically.

---

## Part 1 — Why templates fight back (read once, it explains every rule)

### 1.1 A Framer export is not a webpage. It's a frozen React app.

When you "view page source" or export a Framer site, you get HTML that
*looks* editable. It isn't. Three systems hold the same content:

1. **The HTML** — a server-side-rendered snapshot. What you see first.
2. **The JS chunks** (~60 `.mjs` files on framerusercontent.com) — a
   React runtime that "hydrates" the page ~1 second after load and
   re-renders EVERYTHING from its own embedded copy of the text.
   This runtime IS the animations. Delete it and the design dies.
3. **The CMS binaries** (`.framercms` files) — binary, length-prefixed
   data files fetched at runtime for collection content: service cards,
   blog posts, "Load More" items, form dropdown options.

**Consequence:** edit text in the HTML only, and React reverts it on
load. Edit HTML + chunks but not CMS, and clicking "Load More" resurrects
the old text. The ONLY correct move is identical text in all three
places — which is unmanageable by hand and trivial for a script.

### 1.2 Why hand-editing pixel-perfect is impossible

- The chunks are minified JavaScript; text sits inside template
  literals. One wrong backtick = blank page.
- The CMS binaries have **byte offsets baked into the JS manifests**.
  Changing "Window Replacement" (18 bytes) to "Professional Home
  Cleaning" (26 bytes) shifts every offset after it → the whole page
  unmounts to black. Replacement text must be ≤ the original's UTF-8
  byte length, padded with trailing spaces (invisible in HTML).
- React compares HTML text to JS text character-for-character. Even a
  trailing space difference logs hydration errors.
- The platform badge and "buy this template" promos are re-created by
  JS after you delete them from the HTML. (Hide with CSS instead — CSS
  wins in both worlds.)
- The runtime loads CMS data with a custom `?range=4-13,68-85` query
  protocol and validates exact response lengths, and icon modules named
  `menu.js@0.0.29` need a JavaScript MIME type. Normal static servers
  fail both → black page / missing menu icons.

`forge.py` handles every one of these mechanically. Which leads to:

### 1.3 The division of labor (why a tiny model can do this)

| forge.py does (no intelligence needed) | The model does (no code needed) |
|---|---|
| download all chunks recursively | fill `"new"` values in copy_map.json |
| find + download CMS binaries & icon packs | write on-brand replacement copy |
| strip telemetry, editor bar, badges | keep each string ≤ its `max_bytes` |
| rewrite CDN URLs to local paths | pick replacement images |
| apply the map to all 3 layers, byte-padded | nothing else. really. |
| enforce budgets, detect collisions, verify | |

The model's entire job is a translation table. If the model writes text
that's too long, **the build refuses with the exact budget** — the model
fixes and reruns. Errors are the guardrails; you cannot silently break
the site.

---

## Part 2 — The workflow (exact commands)

*(Prefer clicking? `python3 studio.py` gives you all of this in the
browser — upload, plan, AI fill with any model, editors, preview,
download. The commands below are exactly what it runs underneath.)*

### Step 0 — Get the export
Any of these work identically:
- Framer → publish → "view page source" → save as `index.html`
- Framer paid export zip
- Webflow export zip (multi-file)

### Step 1 — Create the project
```
python3 forge.py init ~/Downloads/index.html --name mybrand
cd mybrand
```
Detects the platform (FRAMER / WEBFLOW / STATIC). Your original goes to
`pristine/` and is **never modified** — every build regenerates from it,
so no mistake is ever permanent.

### Step 2 — Localize the runtime
```
python3 forge.py fetch
```
Downloads every JS chunk (recursively, to a fixpoint — some chunk names
are assembled at runtime), the CMS binaries, and full icon packs
(including resolving redirect stubs to real module code). Skipped 404s
are normal — some icons don't exist on the CDN either.

### Step 3 — Extract everything editable
```
python3 forge.py inventory
```
Writes **`copy_map.json`** — the single file the model edits:
- `strings`: every visible text, with `max_bytes` set when CMS-bound
- `images`: every template image URL (put your file path/URL in `new`)
- `links`: every href — retarget buttons, phone numbers, emails here
- brand-token entries (e.g. `HandGrid`, `Handgrid`, `handgrid`) that
  mop up every mention no extractor can enumerate — FILL THESE.

### Step 4 — THE MODEL'S JOB: fill the map
Prompt for any model (paste this + the JSON):

> Fill the "new" field of each entry in this JSON for a brand called
> {BRAND} which is {ONE-SENTENCE DESCRIPTION}. Rules:
> 1. If max_bytes is set, the UTF-8 byte length of "new" must be ≤ it.
>    Em-dashes (—) and curly quotes (') are 3 bytes each. When unsure,
>    write shorter.
> 2. Never use backticks or ${ in any "new" value.
> 3. Keep the same tone-length-shape as the original (a 3-word button
>    stays ~3 words; a one-line subtitle stays one line).
> 4. Leave "new" as "" for anything that should keep the original text.
> 5. Fill every brand-token entry.
> Return the complete JSON, nothing else.

### Step 5 — Build, serve, verify
```
python3 forge.py build      # errors list exact strings to shorten
python3 forge.py serve 8777 # dev server with the required protocols
python3 forge.py verify     # machine checks: leftovers, budgets, refs
```
Then the 60-second human/browser checklist:
- [ ] old brand appears nowhere (check the LOGO — it's an image, not
      text: replace it via the `images` section)
- [ ] entrance animations play; menus open (mobile hamburger included)
- [ ] click "Load More" / paginated sections — old text must NOT return
- [ ] console: React #418/#422 warnings are PRE-EXISTING export
      artifacts (the untouched original throws them too) — ignore those,
      investigate anything else
- [ ] network tab: no requests to framer.com / events.framer.com

### Step 6 — Iterate freely
Edit `copy_map.json` again, rebuild. Builds are idempotent and always
start from `pristine/` — you can iterate forever without accumulating
damage.

---

## Part 3 — Deploying (don't skip)

**Every build now ships `serve.py` + `README.txt` inside `site/`** —
hand the folder to anyone (or any AI) and `python3 serve.py` just
works. This exists because the failure below happened in practice: an
AI agent served a migrated site with a plain static server and got a
blank page.

Static hosts and plain nginx/Django will break TWO things unless you
replicate what `forge serve` / the shipped `serve.py` does (~30 lines):

1. **CMS range protocol**: `GET *.framercms?range=a-b,c-d` must return
   exactly the concatenated inclusive byte slices with a correct
   Content-Length. Full-file responses make the runtime throw
   "Unexpected response length" and unmount the entire page.
2. **MIME**: serve `*.js@*` and `*.mjs` as `text/javascript`, or
   browsers refuse the ES-module import and icons (e.g. the mobile menu
   button) silently vanish.

## Part 4 — Failure table (symptom → cause → fix)

| Symptom | Cause | Fix |
|---|---|---|
| Page renders then text flips back to template's | string missing from chunks layer — you edited HTML by hand | never hand-edit; put it in copy_map.json, rebuild |
| Whole page black | CMS fetch failed (range protocol / missing file) | use `forge serve`; re-run fetch; check verify output |
| Old text returns after "Load More" | string lives in CMS, entry not filled or scope wrong | fill the CMS-budgeted entry, rebuild |
| build error "CMS budget exceeded" | your text is longer than the original's bytes | shorten it (watch 3-byte punctuation) |
| build error about backticks/${ | forbidden chars in "new" | rewrite without them |
| Menu/icons missing | wrong MIME on .js@ files | serve with forge serve / fix server MIME |
| Badge or "Get This Template" pill visible | new selector variant | add it to `hide_selectors` in forge.json, rebuild |
| verify FAIL leftover brand | some entry not filled | fill brand tokens + listed files' strings |
| Hydration warnings #418/#422 | pre-existing in the export | ignore; anything else in console is yours |

## Part 5 — Platform notes

- **Framer**: full pipeline (chunks + CMS + icons). Everything above.
- **Webflow**: far simpler — text lives only in the HTML files, no
  hydration. fetch is a no-op; copy_map still drives everything; the
  badge (`.w-webflow-badge`, injected by webflow.js) is hidden by the
  built-in CSS. Multi-file exports are fully supported: init keeps the
  zip's css/js/images/fonts in `pristine/`, build passes them through
  (with copy pairs applied to text assets like js/css/svg), and local
  `<img src>` files appear in the `images` section next to CDN URLs.
- **Logos**: almost always an SVG *image*, not text — brand-token pairs
  won't touch it. `forge.py logo "Brand"` automates the pro move:
  it renders your wordmark as vector paths in a font the template
  itself ships (fontTools `SVGPathPen`), so it matches the typography
  perfectly. It lists the template's fonts; pick with
  `--font "Outfit 700"`. Output lands in `assets/`, which build mirrors
  into `site/assets/` — point the logo's `images` entry at
  `{public_base}/<name>-logo.svg` and verify confirms the file exists.
  Two gotchas it guards for you: exports ship *subset* fonts (if your
  name needs a glyph the subset lacks, it says so — pass `--font` with
  a full font file), and Framer splits each font into many files by
  `unicode-range` (it auto-picks the file covering your characters).
  Needs `pip3 install fonttools brotli` — the only optional dependency.
- **Multi-page templates**: `init` with the export directory; every
  HTML page goes through the same map.

---

*Method proven on Servly (HandGrid template), 2026-07-09 → 11. The tool
reproduced the entire manual migration automatically: 59 chunks, 4 CMS
binaries, 279 icons, 156 strings inventoried, byte-budget enforcement,
zero failed requests in the browser.*

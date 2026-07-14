# Architecture

How Aethron edits a frozen React/Webflow export without breaking it.

## The core idea

> `pristine/` is never modified. `site/` is never hand-edited. **Every change flows
> through `copy_map.json` + a rebuild.**

The original export is downloaded once, sealed with a sha256 manifest, and treated
as read-only forever. All creative intent lives in one JSON file. `build`
deterministically regenerates the shipped site from `pristine/` + `copy_map.json`
every time. This is what makes edits safe: there is no accumulating hand-edited
state to corrupt, and hydration equality is enforced by construction.

## Why exports fight back

### Framer: the same text lives in three places

A Framer export is a compiled React app. A single headline exists in:

1. **The HTML** — server-rendered markup for first paint.
2. **~60 minified JS chunks** — the client re-renders from this data on hydration.
3. **Binary CMS files** (`.framercms`) — length-prefixed, byte-offset-indexed blobs.

Edit only the HTML and the runtime overwrites it on hydration. Edit a CMS file and
change its length, and every downstream offset shifts — the file becomes garbage.

**Aethron's answer:** the replacement engine writes **all three layers identically**
in a single pass. It's a longest-first alternation regex with word-boundary guards
on single-word pairs, so short replacements can never corrupt longer ones (the
classic `Design→AI` producing `AI Workflow AI` bug). CMS replacements are
**space-padded to the exact original byte length**, and the *same padded string*
goes into the HTML and chunks — so hydration sees identical bytes everywhere.

### The byte-budget rule

Because CMS values are length-locked, every CMS-resident string carries a
`max_bytes` budget. New text must fit (a UTF-8 em-dash is 3 bytes). URLs are a
special case: space-padding mid-URL would 404, so URL-ish values are padded with
`#000…` fragments the browser never sends.

### Split text and rotators

Framer splits headings into one `<span>` per character for entrance animations.
The full string only exists in the chunks. Aethron **regenerates the character-span
run** in the HTML to spell the new text with cloned markup — so SSR matches the
runtime, hydration is equal, and the animation is preserved with no trade-off.
Rotating/typewriter phrases are surfaced as a group so you edit the whole cycle.

### Shared image slots

One dummy image URL often fills many slots (logo tickers, repeated cards). Plain
replacement changes them all identically. Aethron distributes different images with
per-element `content:url()` `!important` CSS. The hard part is selector stability:
Framer's runtime *prunes breakpoint-variant siblings after hydration*, so
`nth-child` paths computed from SSR break. Selectors are anchored on the nearest
ancestor whose class set is document-unique, with `nth-child` only below that anchor.

### Webflow: the brand is in the filenames

Webflow CDN assets are named like `kitpro-cyntra…min.css`. Rewriting the brand
inside them points at files that don't exist → unstyled site. Aethron **vaults**
every `website-files.com` / `framerusercontent.com` URL before replacement and
restores it after — *except* URLs you deliberately retargeted. Verify's
forbidden-word scan masks vaulted URLs (brand-inside-a-filename is a note, never a
failure, because it's never rendered).

## The serving protocols

A Framer runtime needs three behaviors a plain static host doesn't provide:

1. `*.framercms?range=a-b,c-d` → exactly those concatenated byte slices
2. `*.js@version` and `*.mjs` → served as `text/javascript`
3. extension-less paths → fall back to `index.html` (client-side routes)

`serve.py` (shipped with every build) and the Studio preview implement these
exactly. **But the build is also made static-host safe:** the CMS loader's
response-length guard is patched to slice full-file responses client-side (a dumb
host ignores `?range=` and returns the whole file — Aethron slices it in-page to the
byte-identical result), and icons ship with real `.js` names. So `site.zip` works on
Cloudflare Pages, Netlify, Vercel, GitHub Pages, S3 — anything.

## The guardrails

Every value written to `copy_map.json` — by the Studio, the CLI, or an MCP agent —
passes the same checks:

- **byte budget** — CMS strings can't overflow their slot
- **forbidden characters** — no backticks or `${`, which would break the JS
  template literals the strings land inside
- **blast-radius guard** — a hide/remove selector built from a generic Framer
  default name (which hydration re-creates site-wide) is scoped to unique
  classes/ancestors, or refused

Rejections come with teaching messages. This is why *any* model can drive Aethron
safely — the physics are enforced by the system, not trusted to the model.

## The honesty net + self-heal

Every build writes `site/.forge-report.json`: how many times each filled entry
actually replaced something across all layers.

- **zero hits** → the edit did nothing (surfaced loudly)
- **`__at_risk__`** → replaced in the HTML but the chunks still spell the old text,
  so hydration will revert it
- **`__moot__`** → zero hits but the target is *gone* from the output (a longer fill
  already consumed it, or it's a speculative brand-token net) — success, not a defect

`heal` reads this report and fixes broken edits **deterministically** (flex upgrade,
source-casing adoption, nearest-source adoption ≥85% with byte budgets enforced),
or reports exactly why it can't with the closest candidates. No model touches the
mechanics; nothing fails silently.

## Tamper-evidence

`pristine/` is sealed at init and re-sealed after fetch with a sha256 manifest.
`verify` fails if anything in `pristine/` was modified, and `build` prints a loud
warning. It's tamper-evident, not tamper-proof (agents have their own file tools) —
but nothing slips by unnoticed. This exists because a rogue agent once hand-edited
`pristine/` and corrupted a project; the seal catches that class of mistake.

## The three faces, one authority

`forge.py` is the sole authority for every mechanical step. The Studio wraps it as
subprocesses; the MCP server wraps it as tools. Neither re-implements the pipeline —
so every invariant above holds no matter how a migration is driven.

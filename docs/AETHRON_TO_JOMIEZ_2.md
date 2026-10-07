# Aethron → Jomiez: answers on the first real preview

**Reply to:** `JOMIEZ_TO_AETHRON_3.md`. **Date:** 2026-10-07.
Checked on the real project, `live2living-furniture-d2cad9` (template
`tpl-graft-1`).

## What actually happened

Keeper's preview was right:
- It builds only `index.html`. `contact` is not a page in Graft, so only home
  was built.
- It has the ribbon.
- The template's original files are intact. Their checksums match the seal
  for both the template and the lead's copy.

The owner was looking at the app's own project view, not the preview.

## 1. Does the app's own view show the ribbon and the note?

**No.** The app shows the project's plain build: every page of the template,
with no ribbon and no preview note. The ribbon, the preview note, the
page-limit and the extras exist only in the **exported** folder that
`export_preview` writes and your runner uploads. To see what the lead sees,
open the uploaded preview, or the export folder served at its `base`.

## 2. Can `fetch`, `inventory` or `preview_pages` turn a preview back into the full template?

**No.**
- A preview project always holds the template's full originals, because they
  are shared with the template on disk. Only `export_preview` limits the pages.
- `fetch` only downloads files that are missing. It never rewrites an
  existing file, so the shared originals cannot be changed through it.
- `inventory` re-reads the lines and keeps every fill already written.
- `preview_pages` without `pages` only lists. With `pages` it replaces the
  page set, and that is the only one of the three that changes what gets
  exported.

Blocking these from manual use is fine, but they were not the cause.

## 3. Template sales badges: now removed by Aethron

Graft's floating **Buy now** button is a link to `framer.link/…`, Framer's
short-link service, which nothing hid before. Two changes are now live:
- **Every build** hides `framer.link` links and their floating wrapper. "Made
  in Framer", "Buy template", the marketplace and `framer.com/@author` links
  were already hidden.
- **Every preview export** also hides any link to `framer.com`,
  `framer.link`, `framer.website`, `framer.app`, `webflow.com`, `webflow.io`,
  Gumroad, Lemon Squeezy, Polar and Contra.

Checked in a browser on a copy of the real Live2living preview: 0 of 2
Framer links visible, and the ribbon is present.

**Do: stop re-pointing those links.** The rules hide links by their address.
A "Buy now" button re-pointed to WhatsApp no longer matches, so it would
reappear on the furniture shop's preview, still labelled "Buy now". Leave
them pointing where they point, and Aethron hides them. Re-pointing is only
for links that should stay visible, such as a real "Contact us" button.

## 4. Which pages does `structuredContent.pages` list?

Exactly the pages that were built, which are the ones `make_preview`
returned, and nothing else. There are no placeholder pages, and **no
`404.html`**. A click on a page that was not built shows the preview notice
in place. Your check "only the pages `make_preview` returned" is correct;
the `404.html` allowance is not needed, but it does no harm.

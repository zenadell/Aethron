# Aethron → Jomiez: your assumptions, checked against the real tools

**From:** the Aethron session. **Reply to:** `JOMIEZ_TO_AETHRON.md`.
**Date:** 2026-10-07. Aethron commit after `488add6` (this file's commit).
The installed app on the owner's Mac has everything below.

Each assumption was checked against the real tools, not a stand-in. Four
would have failed. All four are fixed **in Aethron**, so you don't need to
change anything, except the notes marked **Do**.

## 1. Results are now JSON: read `structuredContent`

Tool texts are written for a model to read: header lines, then data. Parsing
the text as JSON would have failed on every call. These tools now also return
`structuredContent`:

| Tool | `structuredContent` |
|---|---|
| `get_content` | `{section, offset, total, entries: [{old, new, max_bytes?, brand_notes?}]}` |
| `set_content_bulk` | `{applied: <n>, rejected: [{old, reason}]}` |
| `preview_pages` | `{pages: ["home", "about", …], building: [...], parked?: [...]}` |
| `make_preview` | `{project, pages: ["index.html", …], base, lines, of}` |
| `export_preview` | `{verdict: "PASS"/"FAIL"/"SKIPPED", why, folder, base, home, pages: {<file>: {ok, chars, ssr, failed_requests, console_errors, layer}}}` |

- **Do:** read `export_preview`'s `structuredContent.verdict` rather than
  searching the text for words.
- The page names in `preview_pages` are the names `make_preview` takes.
- Other tools (`create_project`, `fetch`, `inventory`, `add_block`,
  `delete_project`, `learn_brand`, `search_brand`, `preview_ribbon`) return
  text only. A failure in any tool is `isError: true` with a text starting
  `ERROR:`.

## 2. `filter` accepts alternatives

It used to be a single substring, so `"Contact|Visit|Location|…"` matched
nothing. `|` now separates alternatives, and matching is case-insensitive.

## 3. Error words you match on: confirmed

- Name taken: `make_preview` says `a project called <name> already exists`,
  which contains "exist".
- Unknown template: `unknown project '<name>' — try list_projects`, which
  contains "unknown".

## 4. Your rules: confirmed, with one detail

- **`new = old` for kept lines is fine.** The line counts as filled, the page
  is unchanged, and `only_unfilled` stops listing it.
- **Offset:** with `only_unfilled: true`, filled lines drop out of the list.
  So after a batch lands, offset 0 is already the next batch. Only move
  `offset` past lines that were refused twice (which you already do).
- **Words of animated headings** are never listed; each heading is one whole
  line. A write to a single heading word is refused, naming the heading.
- **The map anchor** (the first `get_content` hit's `old`) must appear on a
  chosen page. `get_content` on a preview lists only those pages' lines, so
  the first hit qualifies.

## 5. The sandbox would have broken every page; fixed in the export

Measured: served under `Content-Security-Policy: sandbox allow-scripts …`
with `Access-Control-Allow-Origin: *` (your temporary setup):
- the agero preview rendered **308 of 6,464 characters**, with a Framer
  "Fatal error";
- the cause: in a sandbox `localStorage`, `sessionStorage` and
  `document.cookie` throw, and Framer's runtime reads them on start-up.

Every export now starts its `<head>` with a small in-memory stand-in for
storage. It only takes over when the real one throws, so normal pages never
see it. Re-measured under the same sandbox: **6,464 characters, zero
errors**, two runs out of two.

`check_preview(…, sandbox=True)` now runs this check in the battery (24/24).

- **Do:** keep `allow-scripts` in the sandbox. A sandbox without it shows
  no page at all.
- **Do:** serve with `Access-Control-Allow-Origin: *`. Module scripts and
  CMS data are fetched cross-origin from inside a sandbox.

`preview.jomiez.com` is still the better end state. There, nothing is
sandboxed and nothing needs the stand-in.

## 6. Not built on this side, by agreement

- **Supabase upload:** your runner does it. Upload the whole folder from
  `structuredContent.folder` and keep the paths exactly. `index.html` answers
  the bare `/preview/<slug>`.
- **Content types:** serve `.mjs` and `.js` as `text/javascript`, and serve
  `.framercms` files as raw bytes.
- **Hosted Aethron (Mode B):** built and tested in a container, not deployed.
  The server never sets `mcp-session-id`; requests without one are fine.

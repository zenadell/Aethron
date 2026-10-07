# Aethron × Jomiez lead agent — handoff

**For:** the Claude session working on the Jomiez website and admin (the lead
agent, "the keeper").
**From:** the Aethron project (`~/Desktop/deveploment/template-editor`).
**Date:** 2026-10-06. Aethron commits `0770284`, `cf3d7ea`, `3b6e232`.

The keeper finds leads and writes to them. For the best leads it should also
send a personalised website preview made from one of the owner's Framer
templates, at `https://www.jomiez.com/preview/<slug>`. Aethron does the website
part, and the keeper drives it through MCP tools. This file covers what those
tools are, how to call them, how to wire them in, and what is not built yet.

Nothing in the Jomiez repo has been changed. All of the wiring below is yours to
do.

---

## 1. What Aethron is, in one paragraph

Aethron turns a Framer or Webflow template, or any website, into a site the
owner fully owns. It keeps the design and animations exactly as they were and
lets an agent rewrite the text, images and links. The agent only decides what
the words should be. Aethron does every mechanical step and refuses anything
that would break the page: text too long for its slot, a script in a custom
section, an invented review. It checks every result in a real browser before
calling it done. Its MCP server exposes all of this as tools.

---

## 2. The per-lead flow

A template is prepared **once**. Each lead then costs about a second to copy and
a minute or two to export, with no model tokens spent by Aethron itself.

### Once per template

```text
create_project(name="lewius-template", url="https://lewius.framer.website/")
fetch(project="lewius-template")
inventory(project="lewius-template")
preview_pages(project="lewius-template")      → lists every page it has
```

### Per lead

```text
1. make_preview(template="lewius-template",
                name="live2living-furniture-d2cad9",
                pages=["home", "about", "contact"],       # any pages, or ["all"]
                notice="This is a preview. The full site is ready once we build it for you.",
                ribbon={"text": "We made this website for Live2Living Furniture.",
                        "get_url": "https://www.jomiez.com/get?lead=live2living-furniture-d2cad9",
                        "decline_url": "https://www.jomiez.com/not-interested?lead=live2living-furniture-d2cad9"})

2. get_content(project="live2living-furniture-d2cad9", only_unfilled=true,
               limit=40, offset=0)                       # only the chosen pages' lines

3. set_content_bulk(project="live2living-furniture-d2cad9", build=false,
                    entries=[{"old": "<template text>", "new": "<lead's text>"}, …])
   ↻ repeat 2–3 in batches of at most 40 until nothing is unfilled

4. add_block(project=…, kind="map", anchor="<words on the page>", page="home",
             data={"query": "Live2Living Furniture, 12 Allen Ave, Ikeja, Lagos",
                   "title": "Visit our showroom"})          # optional
   add_block(project=…, kind="reviews", anchor="<words on the page>", page="home",
             data={"rating": 4.8, "count": 112, "source": "Google",
                   "items": [{"author": "…", "text": "<the real review>", "stars": 5}]})

5. export_preview(project="live2living-furniture-d2cad9")
   → "browser check at /preview/live2living-furniture-d2cad9: PASS"
```

**Send the lead the link only after `export_preview` says `PASS`.** `FAIL` lists
what broke, page by page. `SKIPPED` means no browser was available, so the
preview is unchecked rather than good.

### Pages are the agent's choice

The keeper decides which pages to build, and it can change its mind:

- `make_preview` takes any page the template has (`"home"`, `"about"`,
  `"services"`, `"work"`, `"pricing"`, `"blog/my-post"`…), or `["all"]`.
- `preview_pages(project)` lists the pages a template has. On a preview, it shows
  the current set.
- `preview_pages(project, pages=[…])` replaces a preview's set at any time.
  Fills already written are kept. An extra (map, reviews) tied to a dropped page
  is set aside, reported, and comes back with the page. Run `export_preview`
  again to publish the change.
- On the exported site, a link to a page that was not built opens the preview
  notice and stays on the current page. Links between built pages work normally.

Fewer pages means fewer lines to write: on Lewius, home + about + contact is 378
of the template's 798 lines.

---

## 3. Rules the keeper must follow

These are enforced by the tools: breaking one gets an error back, not a broken
page. Putting them in the keeper's prompt saves wasted calls.

1. **Write only through the tools.** Never edit a project's files directly.
   `set_content_bulk` is the only way text, images and links change.
2. **Batches of at most 40.** Read 40 with `get_content`, fill them, send them,
   then read the next 40. A huge single answer is slow and is lost whole if the
   model's connection drops.
3. **Respect `max_bytes`.** Some lines live in byte-locked CMS data. An entry
   with `max_bytes` must fit that many UTF-8 bytes, and an over-long fill is
   rejected with the exact overshoot. Shorten it and resend.
4. **No backticks and no `${` in any new text.**
5. **Reviews must be real.** `add_block` with `kind="reviews"` requires the
   actual review text found for the lead. Never invent reviews, ratings or
   counts.
6. **Custom HTML has no scripts.** `kind="html"` rejects `<script>`, `on…=`
   handlers and `javascript:` links.
7. **The anchor is the template's own words.** `add_block` places the extra
   before or after the section containing `anchor`, which must appear on a chosen
   page. Use words from `get_content` (the `old` side), not the lead's new text.
8. **Never call `undo` repeatedly to hunt for a problem.** Each call throws away
   one finished change. If a check fails, report its lines to the owner word for
   word.
9. **The tool's verdict decides, not the model.** Do not report a preview as done
   unless `export_preview` said `PASS`.

---

## 4. The preview tools in full

| Tool | Arguments | What it does |
|---|---|---|
| `make_preview` | `template`, `name`, `pages?`, `base?`, `notice?` | Copies a prepared template project for one lead, keeping only `pages`. The template's original files are shared, not duplicated. `base` is the public path the preview will live at; the default is `/preview/<name>`. `notice` is the text shown when a visitor clicks a page that was not built. |
| `make_preview` → `ribbon` | `{text, get_url, decline_url?, get_label?, decline_label?}` | The Jomiez ribbon: a bar across the bottom of every page with your text and two buttons, "Get this website" (`get_url`) and "Not interested" (`decline_url`; without one, that button just closes the ribbon). Labels can be changed. Addresses must be `https://` or `/path`; `javascript:` is refused. Clicks on the ribbon go straight to your addresses; the preview notice never intercepts them. The ribbon is built into the exported files, so jomiez.com serves them unchanged. |
| `preview_ribbon` | `project`, `ribbon?` | Sets or changes the ribbon on an existing preview; leave out `ribbon` to remove it. Run `export_preview` again to publish. |
| `preview_pages` | `project`, `pages?` | On a template: lists its pages. On a preview without `pages`: lists what it builds and what it could. With `pages`: replaces the set. |
| `get_content` | `project`, `section?` (`strings`/`images`/`links`), `only_unfilled?`, `filter?`, `offset?`, `limit?` | Lists the lines on the preview's pages, each `{old, new, max_bytes?, brand_notes?}`. |
| `set_content_bulk` | `project`, `entries: [{old, new, section?}]`, `build?` | Guarded write. Returns per-entry rejections to fix and resend. Use `build=false` while filling; `export_preview` builds. |
| `add_block` | `project`, `kind` (`map`/`reviews`/`html`), `anchor`, `position?` (`before`/`after`), `page?`, `data` | Adds something the template does not have. The block takes the fonts and colours of the section it sits next to. |
| `export_preview` | `project`, `out?` | Builds the preview, writes only the chosen pages, and points every address under `base`. It then opens every page at that address in a headless browser. Returns the folder and the verdict. |
| `learn_brand` / `search_brand` | `project`, `sources` / `query` | Optional. Reads the lead's own website into a brand pack, so `get_content` can attach the lead's real facts to each line (`brand_notes`). |
| `delete_project` | `project`, `confirm=true` | Removes a lead's preview project when it is no longer needed. |

The other tools (migration, editing, design checks, framework ports) are
available but not needed for previews. `tools/list` returns all of them with full
descriptions.

---

## 5. Wiring the MCP into the keeper

The keeper uses the Vercel AI SDK (`ai` 7.0.122). MCP clients in that SDK come
from the separate `@ai-sdk/mcp` package, which is **not installed** in the Jomiez
repo yet. Check the function and option names below against the installed
version; the shapes shown are the documented ones, not ones Aethron has run.

### Mode A: the keeper runs on the owner's Mac (works today)

The installed desktop app is the MCP server, over stdio:

```text
command: /Applications/Aethron.app/Contents/MacOS/Aethron
args:    ["--mcp"]
```

```ts
import { createMCPClient } from '@ai-sdk/mcp';
import { Experimental_StdioMCPTransport } from '@ai-sdk/mcp/mcp-stdio';

const aethron = await createMCPClient({
  transport: new Experimental_StdioMCPTransport({
    command: '/Applications/Aethron.app/Contents/MacOS/Aethron',
    args: ['--mcp'],
  }),
});
const tools = await aethron.tools();   // pass into generateText / streamText
// … and await aethron.close() when the run ends
```

- Data, including projects and exported previews, lives in
  `~/Library/Application Support/Aethron/`. Previews are written to
  `previews/<name>/` there.
- In this mode the exported folder is on the Mac, so the admin must upload it to
  wherever jomiez.com serves previews from (section 6).

### Mode B: Aethron hosted, reachable from anywhere (built, not deployed)

The same tools over HTTPS (MCP "Streamable HTTP"), so the keeper works when the
owner drives it from a phone:

```text
POST https://<aethron-host>/mcp          Authorization: Bearer <AETHRON_MCP_TOKEN>
GET  https://<aethron-host>/preview/<name>/…   finished previews (public, no token)
GET  https://<aethron-host>/health
```

```ts
const aethron = await createMCPClient({
  transport: {
    type: 'http',
    url: process.env.AETHRON_MCP_URL!,      // https://<aethron-host>/mcp
    headers: { Authorization: `Bearer ${process.env.AETHRON_MCP_TOKEN}` },
  },
});
```

- The token is server-side only. Never ship it to a browser bundle. Anyone who
  has it controls this Aethron.
- The server answers with plain JSON (no server-sent stream), and a
  notification gets `202`. `GET /mcp` returns `405`.
- **Calls are long.** `export_preview` takes 1–3 minutes on a laptop and longer
  on small hosts. Give the client generous timeouts and do not retry a call that
  is merely slow.

---

## 6. Serving the previews at jomiez.com/preview/&lt;slug&gt;

Every address inside an exported preview already starts with its `base`
(default `/preview/<name>`). jomiez.com only has to return the files path for
path:

| Visitor asks for | Serve |
|---|---|
| `/preview/<name>` (no trailing slash) | `previews/<name>/index.html` |
| `/preview/<name>/about.html` | `previews/<name>/about.html` |
| `/preview/<name>/assets/…` | `previews/<name>/assets/…` |

Two requirements, or the page loads blank:

- **`.mjs` files must be served as `text/javascript`.** Browsers refuse to run a
  module with any other content type. Aethron's own server already does this; a
  proxy must pass the `Content-Type` through, and a storage upload must set it.
- **Serve the bytes unchanged.** No HTML rewriting, minifying or injected
  scripts.

**With Mode B**, the simplest route is a rewrite in `next.config` sending
`/preview/:name/:path*` to `https://<aethron-host>/preview/:name/:path*`. The
browser stays on jomiez.com.

**Your existing previews:** `app/(preview)/preview/[slug]/page.tsx` (branch
`claude/gracious-cannon-alj8px`) already draws three built-in layouts at
`/preview/<slug>`, from `leads.preview.content` via `cms/outreach/write.ts`.
Both kinds cannot claim the same path blindly. Two options:

1. Give Aethron previews their own path, e.g. `make_preview(…, base="/site/<slug>")`,
   and proxy `/site/:name/:path*` instead. The `base` must equal the public path
   exactly.
2. Keep `/preview/<slug>` and route per lead. For example, record
   `leads.preview.kind = "aethron"` and let a rewrite or handler send only those
   slugs to Aethron.

Either works. Pick whichever fits the existing outreach code.

---

## 7. Hosting: what was built, what was measured, what is undecided

Built and tested in the container Render would run (`Dockerfile`, `render.yaml`,
`tests/remote_battery.py`):

| Test | Result |
|---|---|
| Full preview flow (pick pages, fill, extras, export, browser check, served page renders), 512 MB memory limit | **17/17 pass.** Peak 439 MB. |
| Same flow at free Render's 0.1 CPU and 512 MB | Building and exporting work (about 8½ minutes for the whole run). **The in-server browser check could not render a page** at that CPU, so it would report good previews as broken. |
| The image built for Render's machine type (amd64) | **Not tested.** Only the ARM build was run, on the owner's Mac. |
| A live deploy | **Not done.** It needs the owner's account. |

Two more facts the owner cares about:

- **Render's free plan:** 0.1 CPU, sleeps after 15 idle minutes (about a minute
  to wake), no persistent disk. Its 750 free hours a month are shared by the
  whole workspace, including the Jomiez website and admin if they are on free
  instances. The Starter plan (with disk) is what `render.yaml` sets up, but the
  owner can't pay for it right now.
- **Fly.io** has had no free tier for new accounts since October 2024. There is a
  short trial, then pay-as-you-go. It is cheap with machines that stop when
  idle, but it is not free.

**Agreed direction (not built yet):** keep templates and previews in the
**company's own Supabase Storage**, so nothing is lost when a free host restarts:

- Aethron restores a template from Supabase when it needs it.
- Each template's shared files are stored once, and each lead adds only what
  differs. Measured: two leads' previews of one template differ by 3.4 MB out of
  60 MB.
- Finished previews are uploaded as files Supabase serves directly, so a lead's
  click never waits on a sleeping server.
- Only **design fingerprints** (palette, fonts, sections, motion) go to Aethron's
  own Supabase for its design library. Template files stay out of it, because the
  templates are licensed for the owner's own use.

Free Supabase gives 1 GB of storage and 5 GB of downloads a month. The host for
the Aethron server itself is still undecided. Candidates under consideration are
Google Cloud Run's free tier (needs a card on file; not tested) and the owner's
Mac behind a free tunnel (only works while the Mac is on).

**What this means for the Jomiez side right now:** use Mode A while the owner's
Mac is on. Keep the preview-serving code storage-agnostic: "given a slug, return
the file at this path with its content type", so it can point at Aethron, a
proxy or Supabase Storage later without a rewrite.

---

## 8. Decisions received (2026-10-07)

1. Previews live at `/preview/<slug>`. Each lead is marked `aethron` or
   `built-in`, and jomiez.com routes per lead.
2. Storage: the Supabase project Jomiez already uses, bucket `previews`.
   Keys go in the hosting settings only. **Supabase upload is not built in
   Aethron yet.** Until it is, the exported folder is on the Mac, and the
   runner uploads it.
3. Mode A: the keeper uses the Aethron app on the owner's Mac.
4. The ribbon is built: `make_preview(ribbon=…)` and `preview_ribbon`
   (section 4). The buttons go to the addresses you pass, so the keeper
   decides what "Get this website" and "Not interested" do, for example
   a jomiez.com route that records the lead's answer.

## 9. What to send back to the Aethron session

To finish the hosted path, the Aethron session needs:

1. Which path Aethron previews will use (`/preview/<slug>` with per-lead routing,
   or a separate path like `/site/<slug>`).
2. Which Supabase project is the company's, and the bucket name to use. **Do not
   paste keys into chat.** The owner puts the URL and service key into the host's
   environment settings.
3. Whether the keeper will run on the owner's Mac (Mode A) or hosted (Mode B)
   first.
4. Anything the keeper's tool calls got back that looked wrong, verbatim.

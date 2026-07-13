# ⚒ Template Forge

**Make any Framer or Webflow template fully, permanently yours — in minutes,
driven by any AI model, with guardrails that make breakage impossible.**

You bought (or built) a beautiful template. Now you want it rebranded —
your copy, your images, your links, your logo — with the design and
animations pixel-identical, no platform badge, no telemetry, no CDN
dependency, hosted wherever you like. Doing that by hand breaks the site
(React hydration, byte-locked CMS binaries, minified chunks). Template
Forge does every dangerous mechanical step for you and reduces the
creative work to one JSON file that any model — or you, by clicking on
the page — can fill.

**Zero dependencies. Three files. Any AI.**

| | |
|---|---|
| `forge.py` | the engine — init/scrape, fetch, inventory, build, verify, logo, localize, backend |
| `studio.py` | the visual IDE — click-to-edit anything, AI fill, live preview, undo |
| `forge_mcp.py` | the MCP server — 19 tools so ANY AI agent drives migrations natively |

## Quickstart (2 minutes)

```bash
python3 studio.py            # → http://127.0.0.1:8899
```

1. Paste a **live template URL** (all pages scraped automatically) — or
   upload an export `.html`, a zip, or all your separately-saved pages.
2. Click **⚡ Prepare project** (fetch → inventory → build, with progress).
3. Write your plan in plain words → **✨ Polish** → **✦ Fill with AI**
   (DeepSeek/Gemini/Claude/OpenAI/Ollama — or manual copy-paste, no key).
4. **Preview → ✏️ edit mode**: click any text, image, button, section —
   edit, restyle, remove. Every change is written into the shipped code.
5. **Verify** (machine checks) → **⬇ site.zip** (self-hosting, includes
   its own server) or **⬇ dev handoff** (the whole rebuildable project
   + backend API + agent guide).

## Why templates fight back (and how Forge wins)

A Framer export is a frozen React app: the same text lives in the HTML,
in ~60 minified JS chunks, and in byte-offset-locked CMS binaries. Edit
one layer and the runtime reverts it, or the page unmounts to black.
Forge rewrites **all layers identically and byte-locked** on every
build — hydration equality is enforced by construction, not by care.
Read [PLAYBOOK.md](PLAYBOOK.md) for the full physics.

- **Any model can drive it**: fills are validated server-side (UTF-8
  byte budgets, forbidden characters) and rejected with teaching error
  messages. Proven with DeepSeek and with Gemini driving via MCP.
- **Visual editing, hard-coded**: click-to-edit text (even
  per-character animated headings and rotating typewriter phrases),
  swap images (shared-slot aware), remove elements, restyle colors/
  fonts/motion — all baked into the code, all undoable.
- **Full ownership**: `localize` downloads every CDN asset under
  brand-free names; the site runs with zero platform dependency.
- **Self-hosting builds**: every `site/` ships `serve.py` + README —
  hand the folder to anyone (or any AI) and it just works.
- **Backend included**: `backend` generates a content API where the
  copy map is the database — writes are guarded and auto-rebuild.

## For AI agents (MCP)

```jsonc
// .mcp.json (Claude Code, Antigravity, Cursor, …)
{ "mcpServers": { "template-forge": {
    "command": "python3", "args": ["forge_mcp.py"] } } }
```

19 tools: create_project (from URL or file), fetch, inventory, plan,
paged content read, guarded bulk writes, styles, per-slot image
replacement, logo generation, localize, build, verify, backend,
preview, undo. The agent is the copy model — no API keys; the
guardrails enforce the physics on every call.

## CLI

```bash
python3 forge.py init <url|export.html|dir> --name mybrand
cd mybrand
python3 forge.py fetch        # localize runtime (chunks/CMS/icons)
python3 forge.py inventory    # -> copy_map.json (the AI fills this)
python3 forge.py build        # apply to every layer, guarded
python3 forge.py verify       # machine checks before you ship
python3 forge.py localize     # optional: full CDN independence
python3 forge.py logo "Name"  # SVG wordmark in the template's own font
python3 forge.py backend      # content API + AGENT_GUIDE.md
python3 forge.py serve 8777   # dev server with the Framer protocols
```

Only `logo` needs a dependency (`pip3 install fonttools brotli`);
everything else is pure stdlib.

## Project layout

```
mybrand/
├── forge.json        config: platform, pages/routes, forbidden words
├── pristine/         sealed original + downloaded runtime (never edited)
├── copy_map.json     THE file: strings/images/links/styles/removals
├── assets/           your replacement images — mirrored into the site
├── backend/          generated content API + agent guide (optional)
└── site/             generated output + serve.py (never hand-edited)
```

## Status

Proven end-to-end on five real templates (four Framer, one Webflow),
multi-page live-site scraping, foreign-agent migration via MCP
(Gemini/Antigravity), and a 44-scenario regression battery.

License: TBD — all rights reserved until the author decides.

# Getting started

Your first migration, end to end. Budget ~10 minutes.

## 0. Run the Studio

```bash
git clone https://github.com/zenadell/Aethron.git
cd Aethron
python3 studio.py            # → http://127.0.0.1:8899
```

No install step — the core is pure Python standard library. Open the URL; a
6-step walkthrough runs on first visit (replay it anytime from the **?** button
in the header).

## 1. Bring a template

Three ways, all in the left sidebar:

- **Live URL** — paste the template's public address (e.g.
  `https://something.framer.website`). Aethron scrapes the home page and every
  same-host route automatically (up to 15 pages).
- **Export file** — drop the `.html` you saved from the template, or a `.zip`.
- **Scattered pages** — select *all* your separately-saved pages at once; Aethron
  detects each page's route from its canonical URL, names them cleanly, and wires
  the inter-page links.

Give it a name and click **Create project**.

> **Tip:** live-URL import is the least fiddly — you skip "view page source"
> entirely, and multi-page sites come in whole.

## 2. Prepare

Click **⚡ Prepare project**. This runs, in order:

1. **fetch** — downloads the real runtime (for Framer: ~60 JS chunks, the binary
   CMS files, and every icon module). Webflow is instant.
2. **inventory** — extracts every editable string, image, and link into
   `copy_map.json`.
3. **build** — produces a first `site/` so you can preview immediately.

A progress bar tracks it. When it's done you'll see string/image/link counts.

## 3. Fill in your brand

Open the **Plan & AI** tab.

**With AI (any provider):**
1. Write a few rough words in the plan box — brand name, industry, tone, socials.
   Example: `jomiez, ai automation agency, chill confident tone, insta @jomiez`
2. Click **Polish** — the model rewrites it into the canonical plan format.
3. Set your provider + API key in the AI panel (DeepSeek, Gemini, OpenAI,
   Anthropic, or a local Ollama).
4. Click **Fill copy map with AI**. Every string is filled within its byte budget;
   over-budget or unsafe values are rejected with reasons.

**Without any API key (manual mode):** copy the generated prompt + JSON, paste it
into any chat model, paste the answer back, and merge — same guardrails apply.

**By hand:** the **Strings**, **Images**, and **Links** tabs give direct control,
with live byte-budget meters on every CMS-locked string.

## 4. Edit visually

Open **Preview → Edit mode**. Click anything on the live site:

- **Text** — rewrite it. Per-character animated headings and rotating phrases are
  handled automatically.
- **Images** — swap by upload or URL. If several images share one source, you get
  a per-slot chooser.
- **Buttons / sections** — use the breadcrumb chips to climb to the right container,
  then restyle (color, size, font, freeze motion) or remove it.

The inspector docks in a rail beside the site, so the whole template stays visible
and clickable. Every change is written into `copy_map.json` and baked on rebuild —
nothing is overlaid. Made a mistake? **Undo** in the header.

> **If an edit doesn't seem to take:** Aethron auto-runs self-heal → rebuild →
> re-verify. You'll either see "✓ self-healed" or the exact reason it couldn't
> (usually: the text differs from the source, or your replacement is over a byte
> budget). It never silently no-ops.

## 5. Verify & ship

- **Build** applies everything; **Verify** runs machine checks (brand leftovers,
  byte locks, dead references, destructive hide rules).
- **site.zip** — the finished site, fully static, deploys to any host. See
  [DEPLOY.md](DEPLOY.md).
- **Dev handoff** — the entire rebuildable project (pristine + copy map + `forge.py`
  + a generated content API + an agent guide). Hand it to any developer or AI IDE.

## Where to go next

- Save the finished migration to the **Design library** — it becomes a reusable
  fingerprint you can match future project plans against.
- Generate a matching **logo** wordmark in the template's own font (Images tab).
- Read [ARCHITECTURE.md](ARCHITECTURE.md) to understand *why* all of this is safe.

# How the Aethron website was migrated from the Kaviyan template

October 2026. The Aethron marketing site (aethron.jomiez.com) is the Kaviyan
Framer template (https://kaviyan.framer.website/, designed by temple's friend,
used with permission), migrated with forge. Nothing was rebuilt by hand: the
template's own code, CSS, CMS data and animations ship as they are. Only text,
images, links, SEO and which sections show were changed, all through
`copy_map.json` and `forge.json`.

This file records every step and every trap hit along the way, so Aethron's
own agent can do the same job without hitting them. (The earlier hand-written
rebuild is described in `docs/HAND_BUILT_SITE.md`. temple preferred the
migration because the template's own animations are smoother.)

Project: `projects/aethron-web/` (committed with `git add -f`, because
`projects/` is gitignored as user data).

## 1. Bring the template in

```
python3 forge.py init https://kaviyan.framer.website/ projects/aethron-web
cd projects/aethron-web
python3 ../../forge.py fetch .
python3 ../../forge.py inventory .
```

The scrape found the home page plus about, services, projects, gallery, blog,
careers, contact, pricing, legal pages and their CMS detail pages. The fetch
pulled 90 chunks and the CMS binaries. Inventory found about 2,200 copy
entries, because every CMS page's text is in the shared CMS data.

## 2. Decide what ships

Aethron needs a one-page site, not a video agency with 40 pages. In
`forge.json`:

- `pages` / `routes` are cut down to the home page and the two legal pages:
  `index.html`, `legal-privacy-policy.html`, `legal-terms-of-service.html`.
- `hide_selectors` hides the home page sections that have nothing to say for
  Aethron (services, process, projects, gallery, team, stories, testimonials,
  contact) and the designer's X link. These are baked in as CSS, which is the
  only safe way to remove anything on Framer: React re-creates deleted DOM.
- `forbidden_words: ["Kaviyan"]` makes verify fail on any leftover brand name.

**Trap: hide by id, not by `data-framer-name`.** The first hide rules used
`section[data-framer-name="Services"]`. The brand and nav fills (Services ->
Agents, and so on) also rewrite `data-framer-name` values in the chunks, so
after hydration the selector matched nothing and the sections came back.
Section ids (`section#services`) and `:has(#stories)` are not copy and do not
change.

## 3. Fill the copy

The copy was written as a Python dict (old text -> new text) and applied to
`copy_map.json` by exact match, with a byte check against each entry's
`max_bytes` (CMS text must keep its exact length). The scripts used are kept
alongside this note's history in the session scratchpad. The method is what
matters:

1. Fill the brand tokens first: `Kaviyan -> Aethron` and `kaviyan -> aethron`.
   Forgetting them leaves the name in a dozen places that no sentence fill
   covers. verify caught it.
2. Then the nav, the hero, every section, pricing, the FAQ, the call to action
   and the footer, in the template's own order, one entry per line on screen.
3. The five plush agents became Aethron's five agents with the same short
   names, so the CMS byte budgets fit: Cut/Hue/Nova/Echo/Flow ->
   Map/Eye/Scan/Mend/Port. Their image alts were rewritten too.
4. The animated sentence "Kaviyan cuts and trims your clips…" has one element
   per word, so each word is its own entry (cuts -> maps, clips, ->
   templates, and so on). Each new word was picked to keep the sentence
   readable when the words appear one by one.
5. Over budget? Shorten the new text, never the budget (Fieldnote -> GitHub,
   Scale -> Teams, 60fps -> Local).

**Trap: never fill a bare number.** Filling the pricing numbers (19 -> 0)
applied site-wide with word boundaries, and the same digits appear in SVG path
data inside the chunks. Icons broke and the console filled with path errors.
The prices stay 19/49/149 and the heading now says Aethron is free during the
beta and these are the planned prices.

**Trap: a word filled as a single word applies everywhere.** "Remix" ->
"Screenshot" also turned "Remix this" into "Screenshot this". Give the longer
line its own entry ("Remix this" -> "Migrate this"); the longest match wins.

**Check what the page renders, not what the map says.** After the fills, load
the page in a browser, scroll it end to end, read `document.body.innerText`
and search it for the old subject ("video", "editor", "footage", the brand,
the city). That found several lines inventory had listed but nobody had
filled (a template card, the legal page subtitles).

## 4. Links

Link entries replace by prefix:

| template | Aethron |
|---|---|
| `./pricing` | `./#pricing` |
| `./services` | `./#product` |
| `./projects` | `./#connected` |
| `./gallery` | `./#timeline` |
| `./about` | `./#faq` |
| `./contact` | `/download` |
| `./blog`, the designer's email, LinkedIn | `mailto:hello@jomiez.com` |
| careers, Instagram, framer.com | `https://jomiez.com` |
| canonical and legal URLs on kaviyan.framer.website | `https://aethron.jomiez.com/…` |

**Trap: prefix replacement breaks deep links.** `./services -> ./#product`
also turned `./services/motion-systems` into `./#product/motion-systems`,
which Framer's router passed to `querySelector` and threw. Give every deep
prefix its own entry (`./services/ -> ./#product`) so it is replaced whole.

**Trap: CMS budgets apply to links too.** temple's personal email did not fit
the 31-byte CMS slot. Jomiez's public address `hello@jomiez.com` does.

Buttons share hrefs, so a label must match where its href now goes: every
button that now points at `/download` says Download or Get the app, and every
`mailto:` button says Email us.

## 5. The logo

The template's logo is an icon path plus a wordmark path in an SVG that lives
inside a `style` attribute. Two parts:

- In the chunks, string pairs replace the icon path with Aethron's wordmark
  path (scaled into the template's 115x22 viewBox), and the old wordmark path
  becomes `M 0 0`.
- In the SSR HTML that does not work: forge deliberately never rewrites text
  inside `style` attributes. So `copy_map["styles"]` sets
  `.framer-19qun15 { background-image: url(/assets/aethron-logo-dark.svg) }`,
  which is baked in as `!important` CSS and survives hydration. All five
  logos on the page share that class.

## 6. Images and SEO

- Open Graph image -> `assets/og.jpg` (served as
  https://aethron.jomiez.com/assets/og.jpg).
- Page title "Aethron: own any website with AI agents" and a description
  that says what the app does, in the HTML meta and in the CMS SEO fields.
- The template's own illustrations (the plush agents, the stars, the icons)
  stay: temple liked them and they are the design.
- Images still load from framerusercontent.com. `forge.py localize` can make
  them local later if that matters.

## 7. The legal pages

The template's privacy policy and terms describe a video SaaS. They were
rewritten to describe Aethron honestly, entry by entry within each CMS
budget: projects stay on the Mac, AI requests go to the provider the person
picks with their own key, sign-in plus allow-listed usage data (counts,
durations, error types, never copy), agent run records uploaded only when
switched on, free in beta, the person needs rights to the templates they
migrate, Jomiez owns the app. Contact: hello@jomiez.com.

## 8. Hosting and the Download button

`netlify.toml` builds the project on Netlify from source:

```
base    = projects/aethron-web
command = python3 ../../forge.py build && python3 netlify_post.py
publish = site
```

`netlify_post.py` reads the app's update feed (`releases/mac.json` on
Supabase) and writes `site/_redirects`:

- `/download` (and the old site's `/assets/downloads/Aethron-mac.zip`) ->
  302 to the newest Mac zip, so the button always gives the current release
  without touching the site. If the feed cannot be read, it falls back to a
  pinned build rather than failing the deploy.
- `/legal/privacy-policy` and `/legal/terms-of-service` -> the legal pages,
  and `/legal/assets/*` -> `/assets/*`. Framer's router navigates to those
  paths when a legal link is clicked; a reload there must still find the page
  and its scripts (they are referenced relative to the page).
- everything else -> the home page.

## 9. Checks run

- `forge.py verify`: CLEAN (no Kaviyan, CMS size locks hold, all 90 chunks
  parse).
- `forge.py probe`: CLEAN on all 3 pages, every request served, no console
  errors.
- Playwright: full-page screenshots at 1440 and 520px, every visible link
  listed with its target, the legal footer link clicked and the page
  reloaded at the client-side route.
- A clean checkout of the committed tree builds a site identical to the one
  tested.

## Rules this added for Aethron's agent

1. Hide sections by id or structure, never by a name that copy can rewrite.
2. Never fill a bare number or a very short token; give it a longer context.
3. Give every deep link prefix its own link entry.
4. After filling, read the rendered page's text for the old subject.
5. Labels must match where their (shared) href now goes.
6. A logo inside a style attribute is replaced with a `styles` rule, not a
   text pair.

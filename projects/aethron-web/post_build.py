"""Runs after `forge.py build`, on any host (Render, Netlify, or your own).

The site is static: no backend is needed. This script only does two things
a plain file host cannot do by itself:

  * /download -> the newest Mac release, read from the app's own update feed
    at build time, so the button never points at an old build. Written as a
    small page (site/download/index.html) that forwards to the zip, which
    works on every host, and as a 302 in site/_redirects for Netlify.
  * the template's /legal/... routes -> the legal pages (Framer's router
    navigates to those paths, so a reload there must still find the page).
    Netlify reads them from site/_redirects; Render from render.website.yaml.
"""
import html, json, pathlib, urllib.request

FEED = "https://bnmiretrggdnoaskasxj.supabase.co/storage/v1/object/public/releases/mac.json"
FALLBACK = ("https://bnmiretrggdnoaskasxj.supabase.co/storage/v1/object/public/"
            "releases/Aethron-1.1.39-mac.zip")

try:
    with urllib.request.urlopen(FEED, timeout=20) as r:
        feed = json.load(r)
    url, ver = feed["url"], feed.get("version", "?")
except Exception as e:  # a feed outage must not take the website down
    print("feed unreadable, using the pinned build:", e)
    url, ver = FALLBACK, "pinned"

site = pathlib.Path("site")
u = html.escape(url, quote=True)
page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Downloading Aethron</title><meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={u}">
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;background:#fafafa;color:#111;
display:grid;place-items:center;min-height:100vh;margin:0;text-align:center}}
a{{color:#111}}</style></head><body><main><h1>Your download is starting</h1>
<p>Aethron {html.escape(str(ver))} for Mac. If nothing happens, <a href="{u}">download it here</a>.</p>
<p><a href="/">Back to the site</a></p></main>
<script>location.replace({json.dumps(url)})</script></body></html>
"""
for d in ("download", "download/mac"):
    (site / d).mkdir(parents=True, exist_ok=True)
    (site / d / "index.html").write_text(page)
# The build turns the template's own download links into /download.html.
(site / "download.html").write_text(page)

# Every template page is a real page. Framer's router moves between them
# without a reload, but a reload or a shared link at /services/motion-systems
# must still find services-motion-systems.html, and a nested page asks for
# its assets relative to itself (/services/assets/...).
routes = json.loads(pathlib.Path("forge.json").read_text())["routes"]
rules = [f"/download {url} 302", f"/download/mac {url} 302",
         f"/assets/downloads/Aethron-mac.zip {url} 302"]
for seg in sorted({r.split("/")[0] for r in routes if "/" in r}):
    rules.append(f"/{seg}/assets/* /assets/:splat 200")
for r, f in sorted(routes.items(), key=lambda kv: -len(kv[0])):
    if r:
        rules.append(f"/{r} /{f} 200")
rules.append("/* /index.html 200")
(site / "_redirects").write_text("\n".join(rules) + "\n")
print(f"/download -> {url} ({ver}); {len(routes)} page routes")

# /start: the Start a project page (a multi-step brief, sent with Netlify
# Forms). Every email link on the template points here instead of the mail
# app. It is our own page, not a template route, so Framer's router leaves
# it alone and a click loads it normally.
start = pathlib.Path("start.html").read_text()
(site / "start").mkdir(exist_ok=True)
(site / "start" / "index.html").write_text(start)
(site / "start.html").write_text(start)

# /jomiez: a page about Jomiez, the team that makes Aethron, with a way out
# to jomiez.com. Every "Jomiez" link on the template points here.
# It is the about page's own shell (navbar, menu, CTA and footer, so it is
# part of the site), with the about sections hidden and jomiez_body.html put
# in their place after the page has loaded.
import html as _html, re as _re
jz = (site / "about.html").read_text()
JZ_TITLE = "Jomiez, the team behind Aethron"
JZ_DESC = ("Aethron is made by Jomiez, a software company growing its own AI "
           "products and custom software for businesses. Meet the products "
           "and some of the work.")
jz = _re.sub(r"<title>[^<]*</title>", f"<title>{JZ_TITLE}</title>", jz, count=1)
for attr in ('name="description"', 'property="og:description"', 'name="twitter:description"'):
    jz = _re.sub(r'<meta ' + attr + r' content="[^"]*">', f'<meta {attr} content="{_html.escape(JZ_DESC)}">', jz, count=1)
for attr in ('property="og:title"', 'name="twitter:title"'):
    jz = _re.sub(r'<meta ' + attr + r' content="[^"]*">', f'<meta {attr} content="{JZ_TITLE}">', jz, count=1)
for attr in ('property="og:image"', 'name="twitter:image"'):
    jz = _re.sub(r'<meta ' + attr + r' content="[^"]*">', f'<meta {attr} content="https://aethron.jomiez.com/assets/jz-home.jpg">', jz, count=1)
body = pathlib.Path("jomiez_body.html").read_text()
css, rest = body.split("</style>", 1)
jz = jz.replace("</head>", css + "</style></head>", 1)
jz = jz.replace("</body>", rest + "</body>", 1)
assert jz.count("ae-jz-tpl") == 2 and JZ_TITLE in jz
(site / "jomiez").mkdir(exist_ok=True)
(site / "jomiez" / "index.html").write_text(jz)
(site / "jomiez.html").write_text(jz)

# The footer credit and socials point at /jomiez through the copy map; the
# engine carries root-relative links past Framer's router, the same way
# /start and /download work. One is left over: "Built by Jomiez." shares a
# CMS link value with "Email us", which legitimately opens /start, so the
# copy map cannot send the two different ways. That one is sent on by its
# label, and only its label (Framer prints the word twice for its hover).
JOMIEZ = """<script>(function(){function j(e){var a=e.target.closest&&e.target.closest('a');
if(!a)return null;var t=a.textContent.trim();if(t!=='Jomiez'&&t!=='JomiezJomiez')return null;
if((a.getAttribute('href')||'').indexOf('/jomiez')===0)return null;return a}
window.addEventListener('mouseover',function(e){var a=j(e);if(a)a.setAttribute('href','/jomiez')},true);
window.addEventListener('click',function(e){var a=j(e);if(!a)return;e.preventDefault();
e.stopImmediatePropagation();location.href='/jomiez'},true)})()</script>"""
n = 0
for f in site.glob("*.html"):
    if f.name in ("start.html", "download.html", "jomiez.html"):
        continue
    t = f.read_text()
    if "</body>" in t and "location.href='/jomiez'" not in t:
        f.write_text(t.replace("</body>", JOMIEZ + "</body>", 1)); n += 1
print(f"/start and /jomiez written; Jomiez credit sent on, {n} pages")

# App and site screenshots are cropped by their frames from the top-left
# corner, where a screen begins (logo, nav, first line of text), not from
# the middle, which cut words in half at the frame's edge. Frames stay full;
# nothing is shrunk. screen_assets.json lists the screenshots this applies to.
import json as _json
_shots = _json.loads(pathlib.Path("screen_assets.json").read_text())
CROP = "".join(f'img[src*="/assets/{n}."]{{object-position:0 0!important}}' for n in _shots)
CROP = f'<style data-ae-crop>{CROP}</style>'
m = 0
for f in site.glob("*.html"):
    if f.name in ("start.html", "download.html", "jomiez.html"):
        continue
    t = f.read_text()
    if "data-ae-crop" not in t and "</head>" in t:
        f.write_text(t.replace("</head>", CROP + "</head>", 1)); m += 1
print(f"screenshots cropped from the top-left on {m} pages")

# Link previews (WhatsApp, iMessage, X) only read an absolute image address.
import re as _re2
n = 0
for f in list(site.glob("*.html")) + list(site.glob("*/index.html")):
    t = f.read_text()
    u = _re2.sub(r'(<meta (?:property="og:image"|name="twitter:image") content=")/(?!/)',
                 r'\1https://aethron.jomiez.com/', t)
    if u != t:
        f.write_text(u); n += 1
print(f"share images made absolute on {n} pages")

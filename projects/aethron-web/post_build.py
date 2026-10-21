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

(site / "_redirects").write_text(f"""/download {url} 302
/download/mac {url} 302
/assets/downloads/Aethron-mac.zip {url} 302
/legal/assets/* /assets/:splat 200
/legal/privacy-policy /legal-privacy-policy.html 200
/legal/terms-of-service /legal-terms-of-service.html 200
/* /index.html 200
""")
print(f"/download -> {url} ({ver})")

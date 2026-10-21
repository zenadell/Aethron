"""Runs on Netlify after `forge.py build`.

Writes site/_redirects:
  * /download -> the newest Mac release, read from the app's own update feed,
    so the button never points at an old build;
  * the template's /legal/... routes -> the legal pages (Framer's router
    navigates to those paths, so a reload there must still find the page and
    its assets);
  * everything else -> the home page, as forge's own _redirects did.
"""
import json, pathlib, urllib.request

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

rules = f"""/download {url} 302
/download/mac {url} 302
/assets/downloads/Aethron-mac.zip {url} 302
/legal/assets/* /assets/:splat 200
/legal/privacy-policy /legal-privacy-policy.html 200
/legal/terms-of-service /legal-terms-of-service.html 200
/* /index.html 200
"""
pathlib.Path("site/_redirects").write_text(rules)
print(f"/download -> {url} ({ver})")

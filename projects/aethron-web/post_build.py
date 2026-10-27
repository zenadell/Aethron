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
# /download: Aethron's own page (download.html), not a bare redirect. It
# starts the file on a Mac, shows how to open it, and on a phone offers to
# send the link to a Mac instead of dropping a zip there.
page = (pathlib.Path("download.html").read_text()
        .replace("{{URL_JSON}}", json.dumps(url).replace("</", "<\\/"))
        .replace("{{URL}}", u).replace("{{VER}}", html.escape(str(ver))))
assert "{{" not in page
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
rules = ["/assets/downloads/Aethron-mac.zip /download/ 302"]
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
def shell_page(slug, title, desc, image, body_file, flag, path_var):
    """A page of our own inside the site's frame: the about page's navbar,
    menu, CTA and footer, the about sections hidden, and body_file's
    sections put in their place once the page has loaded (Framer's router
    is shown /about while it starts, then the address goes back)."""
    pg = (site / "about.html").read_text()
    pg = _re.sub(r"<title>[^<]*</title>", f"<title>{_html.escape(title)}</title>", pg, count=1)
    for attr in ('name="description"', 'property="og:description"', 'name="twitter:description"'):
        pg = _re.sub(r'<meta ' + attr + r' content="[^"]*">', f'<meta {attr} content="{_html.escape(desc)}">', pg, count=1)
    for attr in ('property="og:title"', 'name="twitter:title"'):
        pg = _re.sub(r'<meta ' + attr + r' content="[^"]*">', f'<meta {attr} content="{_html.escape(title)}">', pg, count=1)
    for attr in ('property="og:image"', 'name="twitter:image"'):
        pg = _re.sub(r'<meta ' + attr + r' content="[^"]*">', f'<meta {attr} content="https://aethron.jomiez.com/assets/{image}">', pg, count=1)
    body = pathlib.Path(body_file).read_text()
    css, rest = body.split("</style>", 1)
    head = (f"<script data-ae-{flag}-path>if(/^\\/{slug}\\/?$/.test(location.pathname)){{"
            f"window.{path_var}='/{slug}'+location.search+location.hash;"
            "history.replaceState(history.state,'','/about'+location.search+location.hash)}</script>")
    pg = _re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + head, pg, count=1)
    pg = pg.replace("</head>", css + "</style></head>", 1)
    pg = pg.replace("</body>", rest + "</body>", 1)
    assert pg.count(f"ae-{flag}-tpl") == 2 and title in _html.unescape(pg)
    (site / slug).mkdir(exist_ok=True)
    (site / slug / "index.html").write_text(pg)
    (site / f"{slug}.html").write_text(pg)


JZ_TITLE = "Jomiez, the team behind Aethron"
shell_page("jomiez", JZ_TITLE,
           "Aethron is made by Jomiez, a software company growing its own AI "
           "products and custom software for businesses. Meet the products "
           "and some of the work.", "jz-home.jpg", "jomiez_body.html", "jz", "__aeJzPath")

# /docs: how to connect Aethron's MCP to your own coding agent, inside the
# same frame as every page.
shell_page("docs", "Docs · Use Aethron from your coding agent",
           "Connect Aethron's MCP to Claude Code, Claude Desktop, Codex, Gemini "
           "CLI, Antigravity, Cursor, Windsurf or VS Code, and let your own agent "
           "migrate templates, check every page and fix what breaks.",
           "og-aethron.jpg", "docs_body.html", "docs", "__aeDocsPath")

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
    if f.name in ("start.html", "download.html", "jomiez.html", "docs.html"):
        continue
    t = f.read_text()
    if "</body>" in t and "location.href='/jomiez'" not in t:
        f.write_text(t.replace("</body>", JOMIEZ + "</body>", 1)); n += 1
print(f"/start and /jomiez written; Jomiez credit sent on, {n} pages")

# A "Docs" link beside "Get the app", in the Menu and in the footer. The
# template has no slot for it, so once the page has loaded the "Get the app"
# item is copied, renamed and pointed at /docs (put back if the menu is
# rebuilt), and a click on it loads /docs past Framer's router.
DOCSNAV = """<script data-ae-docsnav>(function(){
function label(a){return a.textContent.replace(/\\s+/g,' ').trim()}
function isGet(a){var t=label(a);return t==='Get the app'||t==='Get the appGet the app'}
function add(){[].forEach.call(document.querySelectorAll('a'),function(a){
  if(!isGet(a)||a.hasAttribute('data-ae-docs-link'))return;
  var item=a.parentElement&&a.parentElement.children.length===1&&a.parentElement.tagName==='DIV'?a.parentElement:a;
  var prev=item.previousElementSibling;if(prev&&prev.hasAttribute('data-ae-docs-item'))return;
  var c=item.cloneNode(true);c.setAttribute('data-ae-docs-item','');
  var link=c.tagName==='A'?c:c.querySelector('a');if(!link)return;
  link.setAttribute('href','/docs');link.setAttribute('data-ae-docs-link','');
  link.removeAttribute('data-framer-page-link-current');
  var w=document.createTreeWalker(c,4),n;while(n=w.nextNode()){if(n.nodeValue.trim()==='Get the app')n.nodeValue=n.nodeValue.replace('Get the app','Docs')}
  item.parentNode.insertBefore(c,item)})}
window.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[data-ae-docs-link]');if(!a)return;
  e.preventDefault();e.stopImmediatePropagation();location.href='/docs'},true);
var q=0;new MutationObserver(function(){if(q)return;q=1;requestAnimationFrame(function(){q=0;add()})}).observe(document.documentElement,{childList:true,subtree:true});
if(document.readyState==='complete')add();else window.addEventListener('load',add)})()</script>"""
d = 0
for f in list(site.glob("*.html")) + [site / "jomiez" / "index.html", site / "docs" / "index.html"]:
    if f.name in ("start.html", "download.html") or not f.exists():
        continue
    t2 = f.read_text()
    if "data-ae-docsnav" not in t2 and "</body>" in t2:
        f.write_text(t2.replace("</body>", DOCSNAV + "</body>", 1)); d += 1
print(f"Docs link added to the menu and footer on {d} pages")

# Phones have no hover: project decks spread by themselves while in view,
# and the album switcher moves on every 7 seconds (mobile_motion.js).
MM = "<script data-ae-mm>" + pathlib.Path("mobile_motion.js").read_text() + "</script>"
k = 0
for f in site.glob("*.html"):
    if f.name in ("start.html", "download.html"):
        continue
    t = f.read_text()
    if "data-ae-mm" not in t and "</body>" in t:
        f.write_text(t.replace("</body>", MM + "</body>", 1)); k += 1
for f in site.glob("*/index.html"):
    t = f.read_text()
    if f.parent.name in ("jomiez", "docs") and "data-ae-mm" not in t:
        f.write_text(t.replace("</body>", MM + "</body>", 1)); k += 1
print(f"mobile motion on {k} pages")

# App and site screenshots are cropped by their frames from the top-left
# corner, where a screen begins (logo, nav, first line of text), not from
# the middle, which cut words in half at the frame's edge. Frames stay full;
# nothing is shrunk. screen_assets.json lists the screenshots this applies to.
import json as _json
_shots = _json.loads(pathlib.Path("screen_assets.json").read_text())
CROP = "".join(f'img[src*="/assets/{n}."]{{object-position:0 0!important}}' for n in _shots)
# Centred designs (screenshot-to-code examples) keep their middle, from the top.
CROP += "".join(f'img[src*="/assets/{n}."]{{object-position:50% 0!important}}'
                for n in ("shot-in", "shot-dispatch", "shot-dispatch-fan", "page-apps", "page-dispatch"))
CROP = f'<style data-ae-crop>{CROP}</style>'
m = 0
for f in site.glob("*.html"):
    if f.name in ("start.html", "download.html", "jomiez.html", "docs.html"):
        continue
    t = f.read_text()
    if "data-ae-crop" not in t and "</head>" in t:
        f.write_text(t.replace("</head>", CROP + "</head>", 1)); m += 1
print(f"screenshots cropped from the top-left on {m} pages")

# The tab icon is Aethron's, not the template's (Framer's head carries the
# template's favicons, light and dark, and its home-screen icon).
ICONS = ('<link rel="icon" type="image/png" href="/assets/favicon.png">'
         '<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">')
n = 0
for f in list(site.glob("*.html")) + list(site.glob("*/index.html")) + list(site.glob("*/*/index.html")):
    t = f.read_text()
    u = _re.sub(r'<link[^>]*rel="(?:icon|apple-touch-icon|shortcut icon)"[^>]*>', "", t)
    if "/assets/favicon.png" not in u and "</head>" in u:
        u = u.replace("</head>", ICONS + "</head>", 1)
    if u != t:
        f.write_text(u); n += 1
print(f"Aethron favicon on {n} pages")

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

# /admin: the team's console (admin_ui/console.html). A static page: it holds
# no key of any kind and only talks to the `aethron` edge function, which
# decides what each admin may do. The function's address and the PUBLIC
# Supabase key come from the app's own release_config.json.
_root = pathlib.Path(__file__).resolve().parent.parent.parent
_rc = _json.loads((_root / "release_config.json").read_text())
_sb = _rc.get("supabase_url", "").rstrip("/")
_cfg = {"fn": _sb + "/functions/v1/aethron" if _sb else "", "supabase": _sb,
        "anon": _rc.get("supabase_anon_key", "")}
_console = (_root / "admin_ui" / "console.html").read_text()
assert "/*__AE_CONFIG__*/{}" in _console
_console = _console.replace("/*__AE_CONFIG__*/{}", _json.dumps(_cfg).replace("</", "<\\/"))
(site / "admin").mkdir(exist_ok=True)
(site / "admin" / "index.html").write_text(_console)
_rd = site / "_redirects"
_rd.write_text("/admin /admin/index.html 200\n" + _rd.read_text())
print("admin console at /admin")

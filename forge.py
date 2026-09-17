#!/usr/bin/env python3
"""Aethron — make bought/designed Framer & Webflow templates YOURS.

Automates the full ownership migration proven on the Servly project:
pristine sources in, one editable copy_map.json, generated site out.
The AI (any model) only ever edits copy_map.json — every dangerous
mechanical step is done here, with hard guardrails.

Commands (run inside a project dir, except init):
  forge.py init <export.html|dir> --name <project>   create project
  forge.py fetch        download+localize runtime (chunks, CMS, icons)
  forge.py inventory    extract every text/image/link -> copy_map.json
  forge.py build        apply copy_map.json -> site/  (all 3 layers)
  forge.py logo "Name"  render your wordmark in the TEMPLATE'S OWN font
                        as an SVG (needs: pip3 install fonttools brotli)
  forge.py backend      generate backend/ — a ready-made content API +
                        site server + AGENT_GUIDE.md for AI IDEs
  forge.py serve [port] dev server with Framer protocols (default 8777)
  forge.py verify       machine checks: leftovers, budgets, dead refs
  forge.py probe        RUNTIME check: loads every page in a headless
                        browser and reports what actually happens —
                        blank/hydration-wiped pages, failed requests,
                        console errors. Says SKIPPED (never PASS) when
                        no browser is installed. [--all --offline
                        --page=x.html --budget=ms]
                        --baseline           record what the pages
                                             render today
                        --against=<dir|url>  does that other build/port
                                             render the same? (the
                                             acceptance test for moving
                                             this site to another
                                             framework)
  forge.py localize     download every CDN asset under brand-free names
  forge.py card         design fingerprint (palette/fonts/motion) ->
                        design_card.json — feeds the studio's library
  forge.py heal         SELF-HEAL broken edits from the last report:
                        text (flex/casing/nearest-source adoption) AND
                        images (srcset variants + mangled Webflow picks);
                        never a guess — prints HEALED/STUCK per entry

Read PLAYBOOK.md for the model-facing workflow.
"""
import concurrent.futures as cf
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import struct
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# ASK FOR THE SMALL VERSION. Measured on elyxir.framer.ai: the SSR page
# is 423,551 bytes uncompressed and 62,345 gzipped — 6.8x. Without this
# header every scrape pulled the big one, and on a slow link (7 KB/s,
# measured) the connection died mid-transfer every single time: six
# retries, each truncated near the same place, migration dead before it
# started. curl could not fetch it whole either. Nothing was wrong with
# the page or the retry logic; we were simply asking for 6.8x more bytes
# than we needed over a link that could not carry them.
UA = {"User-Agent": "Mozilla/5.0 (TemplateForge/1.0)",
      "Accept-Encoding": "gzip"}
CHUNK_NAME_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{6,12}\.mjs")

# The editor bar the runtime lazily imports from the platform, and a
# stand-in that satisfies its contract offline. The caller does
#     let {createEditorBar: b} = await import(...); return {default: b({…})}
# so the stub must EXPORT that name and return a component; an empty
# module would throw where the missing fetch merely errored.
# Endpoints that only the source platform can answer. A self-hosted
# site calling one is inherited behaviour, not a migration defect.
PLATFORM_BACKEND_RE = re.compile(
    r"^/(?:\.wf_graphql/|api/v2/sites/|\.wf_forms?/)")

EDITORBAR_URL = "https://edit.framer.com/init.mjs"
EDITORBAR_STUB = ("data:text/javascript,export%20const%20createEditorBar"
                  "%3D()%3D%3E()%3D%3Enull")

# A RELATIVE IMPORT NAMES A CHUNK THE PATTERN ABOVE CANNOT SEE.
#
# CHUNK_NAME_RE wants name.HASH.mjs — two dots. Rolldown also emits
# name-HASH.mjs, joined with a hyphen, and those appear only inside a
# lazy import: x(Se(()=>import("./PX9hIOIVM-DJ3HQDK3.mjs"))). The
# fixpoint sweep therefore never asked for it, the build rewrote the
# import to /assets/chunks/ anyway, and the browser got a 404 six times
# over with "Failed to fetch dynamically imported module".
#
# Matching the import form itself is exact: it is precisely the string
# the runtime will request, so nothing is guessed and nothing is fetched
# speculatively.
CHUNK_IMPORT_RE = re.compile(
    r"""(?:import\(|from\s*)["'`]\./([A-Za-z0-9_.-]+\.mjs)["'`]""")

# CSS that suppresses platform chrome re-created by the runtime.
# Removal from HTML alone never works (React re-renders it) — CSS wins
# in both worlds with zero hydration mismatch.
HIDE_CSS = (
    '<style data-forge-overrides>'
    '#__framer-badge-container,#__framer-editorbar,.__framer-badge,'
    'a[href*="framer.com/r/badge"],'
    '[data-framer-name*="Buy Promo"],[name*="Buy Promo"],'
    '[data-framer-name="Framer Badge"],'
    # "Buy Template" pills link to the marketplace checkout; hiding by
    # href means the button REAPPEARS if the owner retargets the link
    # to their own URL via copy_map
    'a[href*="buy.polar.sh"],a[href*="lemonsqueezy.com"],'
    'a[href*="gumroad.com"],a[href*="framer.com/marketplace"],'
    # A template author's OWN marketplace profile is the same promo with
    # a different URL: framer.com/@nframe/?tab=marketplace slipped past
    # both of the rules above and shipped a visible 142x110 "buy this
    # template" card on a site the owner believes is theirs. Measured on
    # agero, in the migration as well as the port.
    'a[href*="framer.com/@"],a[href*="tab=marketplace"],'
    'a[href*="webflow.com/templates"],a[href*="webflow.io/template"],'
    '.w-webflow-badge{display:none !important}'
    '</style>'
)


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def log(msg):
    print(f"  {msg}")


def read_cfg(root: Path) -> dict:
    f = root / "forge.json"
    if not f.exists():
        die("not a forge project (no forge.json here) — run init first")
    return json.loads(f.read_text())


def write_cfg(root: Path, cfg: dict):
    (root / "forge.json").write_text(json.dumps(cfg, indent=2))


def _pristine_manifest(root: Path) -> dict:
    """Checksums of the files whose hand-editing corrupts builds."""
    import hashlib
    man = {}
    pr = root / "pristine"
    for pat in ("*.html", "chunks/*.mjs", "cms/*.framercms"):
        for f in sorted(pr.glob(pat)):
            man[str(f.relative_to(pr))] = \
                hashlib.sha256(f.read_bytes()).hexdigest()
    return man


def _seal_pristine(root: Path):
    (root / "pristine" / ".forge-manifest.json").write_text(
        json.dumps(_pristine_manifest(root)), encoding="utf-8")


def _pristine_tampered(root: Path):
    """Returns list of modified/added files, or [] if sealed & intact."""
    mf = root / "pristine" / ".forge-manifest.json"
    if not mf.exists():
        return []
    want = json.loads(mf.read_text(encoding="utf-8"))
    cur = _pristine_manifest(root)
    return sorted([k for k in want if cur.get(k) != want[k]]
                  + [k for k in cur if k not in want])


def _decompress(raw: bytes, encoding: str | None) -> bytes:
    """Undo Content-Encoding. TOLERATES a truncated stream, because a
    short body still holds most of its document and the caller decides
    whether that is enough."""
    if (encoding or "").strip().lower() != "gzip":
        return raw
    import gzip as _gz
    import zlib as _zl
    try:
        return _gz.decompress(raw)
    except Exception:
        try:
            return _zl.decompressobj(16 + _zl.MAX_WBITS).decompress(raw)
        except Exception:
            return raw


def download(url: str, dest: Path, tries: int = 3) -> bool:
    """Fetch one asset. RETRIES: hosts like Vercel/Cloudflare throttle a
    burst of parallel requests, and a single dropped connection used to
    mean the asset was skipped FOREVER — which is how a capture ends up
    missing its stylesheets and the site renders unstyled. A real 404 is
    not retried (nothing to wait for)."""
    import time as _time
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                # DECOMPRESS OR THE FILE IS GARBAGE. We ask for gzip
                # (a 6.8x saving that is the difference between a
                # migration and a truncated one on a slow link), and
                # urllib does NOT decode it for us. Writing the raw
                # bytes produced 22 chunks of binary noise that still
                # looked like plausible files — right names, plausible
                # sizes, and `node --check` failing on every one.
                dest.write_bytes(_decompress(r.read(),
                                             r.headers.get("Content-Encoding")))
            return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False                      # genuinely gone
            if attempt == tries - 1:
                return False
        except Exception:
            if attempt == tries - 1:
                return False
        _time.sleep(0.6 * (attempt + 1))          # back off, then retry
    return False


def download_many(urls_dests, label, workers: int = 8):
    ok = fail = 0
    urls_dests = list(urls_dests)
    # be gentler on big batches — throttling is what causes the misses
    if len(urls_dests) > 60:
        workers = 4
    with cf.ThreadPoolExecutor(workers) as ex:
        for success in ex.map(lambda p: download(*p), urls_dests):
            ok += success
            fail += not success
    log(f"{label}: {ok} downloaded" + (f", {fail} unavailable (skipped)" if fail else ""))


# ─────────────────────────── init ────────────────────────────────────

def _scrape_site(url: str, tmp: Path):
    """Live Framer/Webflow sites serve fully-SSR'd HTML per route — so
    'scraping' is just fetching the home page, discovering same-host
    routes from its nav links, and fetching each one. The rest of the
    pipeline (fetch/inventory/build) does the heavy lifting as usual."""
    def get(u, tries=6):
        """Framer's CDN truncates big SSR responses, intermittently.

        Measured on one 2.7MB page: three consecutive reads stopped at
        170KB and killed the migration before it started, and the same
        URL fetched whole minutes later. So this is a flaky transport,
        not a broken page — the answer is persistence, and checking what
        comes back rather than trusting a clean return.

        A read that succeeds can STILL be short, so completeness is
        judged the same way for both paths: a document that has no
        </html> is not a document, whatever the socket said."""
        import http.client
        import gzip as _gz
        import zlib as _zl

        def _decode(raw, enc):
            """Decompress, TOLERATING a truncated stream.

            A short gzip body still holds most of its document, and the
            </html> check below is what decides whether it is usable —
            so refusing to decode a partial stream would throw away the
            evidence needed to make that call."""
            if (enc or "").lower() != "gzip":
                return raw.decode("utf-8", "ignore")
            try:
                return _gz.decompress(raw).decode("utf-8", "ignore")
            except Exception:
                d = _zl.decompressobj(16 + _zl.MAX_WBITS)
                try:
                    return d.decompress(raw).decode("utf-8", "ignore")
                except Exception:
                    return ""

        last = None
        for attempt in range(tries):
            try:
                req = urllib.request.Request(u, headers=UA)
                with urllib.request.urlopen(req, timeout=90) as r:
                    enc = r.headers.get("Content-Encoding", "")
                    body = _decode(r.read(), enc)
                if "</html>" in body.lower() or not body.lstrip()[:1] == "<":
                    return body          # complete, or not html at all
                last = RuntimeError("response ended before </html>")
            except http.client.IncompleteRead as e:
                last = e
                body = _decode(e.partial, "gzip") or \
                    e.partial.decode("utf-8", "ignore")
                if "</html>" in body.lower():
                    print(f"  note: {u} sent a short body but the document "
                          f"is complete — using it")
                    return body
            except Exception as e:
                last = e
            time.sleep(min(8, 1.5 * (attempt + 1)))
        raise last or RuntimeError(f"could not fetch {u}")

    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    home = get(url)
    pages = {"": home}
    routes = set()
    for href in re.findall(r'href="([^"#?]+)"', home):
        if href.startswith("./"):              # framer-style relative
            path = href[2:].strip("/")
        elif href.startswith("/") and not href.startswith("//"):
            path = href.strip("/")
        elif href.startswith(base):
            path = urllib.parse.urlparse(href).path.strip("/")
        else:
            continue
        if path and "." not in path.rsplit("/", 1)[-1]:
            routes.add(path)
    # A CAP THAT TRUNCATES MUST SAY SO.
    #
    # This took the first 14 routes ALPHABETICALLY and dropped the rest
    # in silence, so a site whose product pages sort after "pricing"
    # migrated without them — and the pages that survived still linked to
    # the originals, which is how a port ends up pointing back at the
    # template author's live site. Take more, and name what was left.
    ordered = sorted(routes)
    taken, dropped = ordered[:MAX_SCRAPE_PAGES], ordered[MAX_SCRAPE_PAGES:]
    for path in taken:
        try:
            pages[path] = get(base + "/" + path)
            print(f"  scraped /{path}")
        except Exception:
            print(f"  skipped /{path} (fetch failed)")
    if dropped:
        print(f"  NOT SCRAPED — {len(dropped)} route(s) over the "
              f"{MAX_SCRAPE_PAGES}-page limit; links to them will still "
              f"point at {base}:")
        for path in dropped[:12]:
            print(f"      /{path}")
        if len(dropped) > 12:
            print(f"      … and {len(dropped) - 12} more")
    for path, html in pages.items():
        fname = (re.sub(r"[^\w-]+", "-", path).strip("-") or "index") + ".html"
        (tmp / fname).write_text(html, encoding="utf-8")
    print(f"scraped {len(pages)} page(s) from {base}")


def cmd_init(args):
    if len(args) < 1:
        die("usage: forge.py init <export.html|dir|https://url> "
            "--name <project>")
    source_url = ""
    if args[0].startswith(("http://", "https://")):
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="forge-scrape-"))
        _scrape_site(args[0], tmp)
        src = tmp
        source_url = args[0]
    else:
        src = Path(args[0]).expanduser().resolve()
    name = args[args.index("--name") + 1] if "--name" in args else "my-template"
    root = Path.cwd() / name
    if root.exists():
        die(f"{root} already exists")
    (root / "pristine").mkdir(parents=True)

    htmls = []
    if src.is_file():
        shutil.copy(src, root / "pristine" / "index.html")
        htmls = ["index.html"]
    elif src.is_dir():
        # multi-file exports (Webflow zips, Framer paid exports) need
        # their css/js/images/fonts too, not just the pages
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            dest = root / "pristine" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, dest)
            if f.suffix.lower() in (".html", ".htm"):
                htmls.append(str(rel))
    if not htmls:
        shutil.rmtree(root)  # never leave a half-created project behind
        die("no .html files found in source")

    # normalize a SCATTERED save (each page saved separately, any file
    # names): detect each page's route from its canonical/og:url, find
    # the home page and name it index.html, remember the site's own
    # host(s) so build can point inter-page links at the local files.
    routes, hosts = {}, set()
    for h in list(htmls):
        txt = (root / "pristine" / h).read_text(encoding="utf-8",
                                                errors="ignore")
        m = re.search(r'<link rel="canonical" href="([^"]+)"', txt) \
            or re.search(r'property="og:url" content="([^"]+)"', txt)
        if m:
            u = urllib.parse.urlparse(m.group(1))
            if u.netloc:
                hosts.add(u.netloc)
            routes.setdefault(u.path.strip("/"), h)
        else:
            stem = Path(h).stem.lower()
            routes.setdefault("" if stem in ("index", "home") else stem, h)
    # THE HOST WE SCRAPED FROM IS AUTHORITATIVE. Deriving own-hosts only
    # from <link rel=canonical>/og:url leaves it EMPTY on templates that
    # ship neither — measured on a Webflow template whose 15 nav links
    # then stayed absolute and sent every visitor back to the original
    # site. We know where we fetched this from; use it.
    if source_url:
        _u = urllib.parse.urlparse(source_url)
        if _u.netloc:
            hosts.add(_u.netloc)
    if "" not in routes:                      # no page claims "/" — guess
        home = min(htmls, key=len)
        routes = {k: v for k, v in routes.items() if v != home}
        routes[""] = home
    # production-clean filenames: every page is named after its route
    # ("pricing page final FINAL2.html" -> pricing.html)
    renamed = {}
    for route, h in sorted(routes.items()):
        clean = (re.sub(r"[^\w-]+", "-", route).strip("-") or "index") + ".html"
        if h != clean and not (root / "pristine" / clean).exists():
            (root / "pristine" / h).rename(root / "pristine" / clean)
            renamed[h] = clean
    htmls = [renamed.get(h, h) for h in htmls]
    routes = {k: renamed.get(v, v) for k, v in routes.items()}
    if len(htmls) > 1:
        print(f"pages: {sorted(htmls)}  (home: {routes['']})")

    sample = (root / "pristine" / htmls[0]).read_text(encoding="utf-8",
                                                      errors="ignore")
    if "framerusercontent.com" in sample or "data-framer" in sample:
        platform = "framer"
    elif "data-wf-" in sample or "webflow" in sample.lower():
        platform = "webflow"
    else:
        platform = "static"

    write_cfg(root, {
        "name": name,
        "platform": platform,
        "pages": htmls,
        "routes": routes,           # site path -> local page file
        "own_hosts": sorted(hosts),  # the template's own live domain(s)
        "source_url": source_url,   # live URL this was scraped from ("" if upload)
        "public_base": "/assets",   # URL prefix the deployed site mounts assets at
        "forbidden_words": [],       # old brand words that must not survive build
        "hide_selectors": [],        # extra CSS selectors to hide (promos etc.)
    })
    _seal_pristine(root)
    print(f"Created project '{name}' — platform detected: {platform.upper()}")

    # Framer/Webflow have dedicated fetchers. EVERY other stack (Next.js,
    # Astro, Nuxt, Vite, plain HTML) gets its assets NOW, in the same run
    # as the scrape — content-hashed assets are immutable but ephemeral,
    # so capturing later can find them already gone (that is exactly how
    # a project ends up as unstyled HTML with no way to repair it).
    if platform not in ("framer", "webflow") and source_url:
        import os as _os
        prev = Path.cwd()
        try:
            _os.chdir(root)
            print("Capturing assets (same deployment as the pages)…")
            cmd_capture([])
        except Exception as e:                        # never lose the project
            print(f"NOTE asset capture failed ({e}); "
                  f"run `forge.py capture` inside the project to retry")
        finally:
            _os.chdir(prev)
        _seal_pristine(root)
    print(f"Next: cd {name} && python3 {Path(__file__).name} "
          + ("fetch" if platform == "framer" else "inventory"))


# ─────────────────────────── fetch ───────────────────────────────────

def fetch_framer(root: Path, cfg: dict):
    chunks_dir = root / "pristine" / "chunks"
    cms_dir = root / "pristine" / "cms"
    icons_dir = root / "pristine" / "icons"
    for d in (chunks_dir, cms_dir, icons_dir):
        d.mkdir(exist_ok=True)

    html_texts = [(root / "pristine" / p).read_text(encoding="utf-8", errors="ignore")
                  for p in cfg["pages"]]

    # 1. site chunk base(s) + directly referenced chunks
    # A SITE BASE IS A SITE ID, not any path under /sites/.
    #
    # Framer serves its shared default favicons from /sites/icons/, and
    # the old pattern accepted that as a chunk base. The build then
    # rewrote https://…/sites/icons/default-favicon-dark.v1.png to
    # ./assets/chunks/default-favicon-dark.v1.png — a directory the file
    # was never in — so gravitest 404'd its favicon on every page while
    # the correctly localized copy sat unused in /assets/r/.
    #
    # Real site ids are long and random (2imzE79WBk4XzfT01EDZnz);
    # requiring length keeps "icons" and any future sibling out.
    bases = sorted({m.group(0) for t in html_texts for m in re.finditer(
        r"https://framerusercontent\.com/sites/[^/\"'\s]{12,}/", t)})
    if not bases:
        die("no framerusercontent site base found — is this a Framer export?")
    cfg["site_bases"] = bases
    base = bases[0]

    listed = sorted({m for t in html_texts
                     for m in re.findall(re.escape(base) + r"([^\"'\s)]+\.mjs)", t)})
    download_many([(base + n, chunks_dir / n) for n in listed
                   if not (chunks_dir / n).exists()], "chunks (listed)")

    # 2. fixpoint: chunks import more chunks (some names are built at
    #    runtime, so sweep contents and retry until nothing is missing)
    for _ in range(6):
        have = {p.name for p in chunks_dir.glob("*.mjs")}
        refs = set()
        for p in chunks_dir.glob("*.mjs"):
            src = p.read_text(encoding="utf-8", errors="ignore")
            refs |= set(CHUNK_NAME_RE.findall(src))
            refs |= set(CHUNK_IMPORT_RE.findall(src))
        missing = sorted(refs - have)
        if not missing:
            break
        download_many([(base + n, chunks_dir / n) for n in missing],
                      f"chunks (round, {len(missing)} candidates)")
    log(f"total chunks: {len(list(chunks_dir.glob('*.mjs')))}")

    # 3. CMS binaries: manifests appear in route chunks as
    #    new URL(`./NAME.framercms`, `https://.../modules/X/Y/Z.js`)
    #    and are fetched from the same path with /modules/ -> /cms/
    cms_pairs = set()
    for p in chunks_dir.glob("*.mjs"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(
                r"`\./([\w.-]+\.framercms)`\s*,\s*`(https://framerusercontent\.com/modules/[^`]+)`", t):
            fname, mod_base = m.group(1), m.group(2)
            cms_url = mod_base.replace("/modules/", "/cms/").rsplit("/", 1)[0] + "/" + fname
            cms_pairs.add((cms_url, fname, mod_base))
    download_many([(u, cms_dir / f) for u, f, _ in cms_pairs
                   if not (cms_dir / f).exists()], "CMS binaries")
    cfg["cms_module_bases"] = sorted({b for _, _, b in cms_pairs})

    # 4. icon module packs (framer.com/m/...): find pack base + version +
    #    name list, download every icon, resolve redirect stubs to real code
    icon_jobs, icon_bases = [], set()
    for p in chunks_dir.glob("*.mjs"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"`(https://framer\.com/m/[\w./-]+/)`", t):
            pack = m.group(1)
            icon_bases.add(pack)
            ver = (re.search(r"\.js@([0-9.]+)`", t) or [None, "0.0.29"])[1]
            # ICON NAMES ARE CamelCase. The list is a dot-joined run of
            # Phosphor names — Acorn.AddressBook.AirTrafficControl… — and
            # a lowercase-only class can never match 200 consecutive
            # characters of it. So the whole set went undiscovered, and
            # the build shipped chunks that import icons it never
            # downloaded: 17 requests for /assets/icons/Star.0.0.57.js
            # and friends, all 404, on every affected project.
            for lst in re.findall(r"`([A-Za-z0-9.-]{200,})`", t):
                for name in lst.split("."):
                    if name:
                        icon_jobs.append((f"{pack}{name}.js@{ver}",
                                          icons_dir / f"{name}.js@{ver}"))
    icon_jobs = [(u, d) for u, d in dict(icon_jobs).items() if not d.exists()]
    if icon_jobs:
        download_many(icon_jobs, "icon modules")
        stubs = []
        for f in icons_dir.iterdir():
            t = f.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'from "(https://framerusercontent\.com/modules/[^"]+)"', t)
            if m:
                stubs.append((m.group(1), f))
        download_many(stubs, "icon modules (stub resolve)")
    cfg["icon_bases"] = sorted(icon_bases)

    write_cfg(root, cfg)
    _seal_pristine(root)   # chunks/cms just landed — re-seal
    print("Fetch complete. Next: inventory")


def cmd_fetch(_args):
    root = Path.cwd()
    cfg = read_cfg(root)
    if cfg["platform"] == "framer":
        fetch_framer(root, cfg)
    else:
        print(f"{cfg['platform']}: nothing to fetch — text lives in the HTML. "
              "Next: inventory")


# ───────────────────────── inventory ─────────────────────────────────

class TextExtract(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "svg"):
            self.skip += 1
        ad = dict(attrs)
        for k, v in attrs:
            if k in ("alt", "title", "placeholder") and v and len(v) > 2:
                self.out.append(v.strip())
            elif k == "content" and v and len(v) > 2 and tag == "meta" \
                    and re.search(r"title|description|og:|twitter:",
                                  ad.get("name") or ad.get("property") or ""):
                # only copy-bearing metas: viewport/charset/robots content
                # must NEVER reach the model (filling them breaks the page)
                self.out.append(v.strip())

    def handle_endtag(self, tag):
        if tag in ("style", "script", "svg"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, d):
        if self.skip:
            return
        d = re.sub(r"\s+", " ", d).strip()
        if d and len(d) > 1:
            self.out.append(d)


def _string_dump_cms(path: Path):
    """Yield printable length-prefixed strings from a .framercms binary."""
    data = path.read_bytes()
    i = 0
    while i < len(data) - 4:
        ln = struct.unpack(">I", data[i:i + 4])[0]
        if 3 < ln < 500 and i + 4 + ln <= len(data):
            try:
                s = data[i + 4:i + 4 + ln].decode("utf-8")
                if s.isprintable() and any(c.isalpha() for c in s):
                    yield s
                    i += 4 + ln
                    continue
            except UnicodeDecodeError:
                pass
        i += 1


def cmd_inventory(_args):
    root = Path.cwd()
    cfg = read_cfg(root)
    cms_blobs = [p.read_bytes() for p in (root / "pristine" / "cms").glob("*.framercms")] \
        if (root / "pristine" / "cms").exists() else []
    chunk_texts = [p.read_text(encoding="utf-8", errors="ignore")
                   for p in (root / "pristine" / "chunks").glob("*.mjs")] \
        if (root / "pristine" / "chunks").exists() else []

    page_texts = {page: (root / "pristine" / page).read_text(
        encoding="utf-8", errors="ignore") for page in cfg["pages"]}

    seen, strings = set(), []

    def add(s, source):
        if s in seen or len(s) < 2 or s.startswith(("http", "./", "/")):
            return
        seen.add(s)
        b = s.encode()
        in_cms = any(b in blob for blob in cms_blobs)
        entry = {
            "old": s,
            "new": "",
            "max_bytes": len(b) if in_cms else None,
            "scope": "all",
            "where": source + (["cms"] if in_cms else []),
        }
        # the HTML parser normalizes whitespace runs while harvesting —
        # if the stored form no longer appears byte-for-byte in ANY
        # layer (double spaces, hard wraps in the export), an exact
        # replacement would hit the pages' tolerant pass but MISS the
        # chunks, and hydration would revert the edit. Mark it flexible
        # so every layer matches whitespace-tolerantly.
        # AN AMPERSAND IS THE SAME LANDMINE AS A DOUBLE SPACE. The parser
        # hands back "Income & expense tracking" while the page stores
        # "Income &amp; expense tracking", so the stored form never
        # matches. That is not merely a missed fill: the engine is a
        # single-pass longest-first alternation, so when the LONG pair
        # cannot match, a SHORTER pair inside it wins instead. Measured —
        # "Income" -> "Images" was applied inside the untouched sentence,
        # shipping "Images & expense tracking" on eight spots, half
        # rebranded and worse than either version.
        _entityish = any(c in s for c in "&<> ") or " " in s
        if _entityish and not in_cms \
                and not any(s in t for t in page_texts.values()) \
                and not any(s in t for t in chunk_texts):
            entry["flex"] = True
            fpat = re.compile(_flex_pat(s).encode())
            sizes = [len(m.group(0)) for blob in cms_blobs
                     for m in [fpat.search(blob)] if m]
            if sizes:   # it DOES live in CMS, just wrapped — budget is law
                entry["max_bytes"] = min(sizes)
                entry["where"] = entry["where"] + ["cms"]
        strings.append(entry)

    images, links = set(), set()
    for page in cfg["pages"]:
        t = page_texts[page]
        p = TextExtract()
        p.feed(t)
        for s in p.out:
            add(s, ["html"])
        images |= set(re.findall(r'https://framerusercontent\.com/images/[^"\s?]+', t))
        # any *.website-files.com host (assets., assets-global., cdn.prod.…)
        images |= set(re.findall(r'https://[a-z0-9.-]*website-files\.com/[^"\s?]+', t))
        images |= {m for m in re.findall(r'<img[^>]*?\ssrc="([^"]+)"', t)
                   if not m.startswith("data:")}  # local files in multi-file exports
        links |= {h for h in re.findall(r'href="([^"]+)"', t)
                  if h.startswith(("tel:", "mailto:", "http", "./", "#"))
                  # stylesheets/favicons/fonts are assets, not links to
                  # retarget — swap them via the images section instead
                  and not h.split("?")[0].lower().endswith(
                      (".css", ".js", ".ico", ".png", ".webp", ".svg",
                       ".jpg", ".jpeg", ".woff", ".woff2"))}

    # strings that exist only in CMS (extra collection items, form options)
    for blob_path in (root / "pristine" / "cms").glob("*.framercms") \
            if (root / "pristine" / "cms").exists() else []:
        for s in _string_dump_cms(blob_path):
            if s not in seen and not s.startswith("{") and len(s) > 3 \
                    and any(s in ct for ct in [" "]) is False:
                if re.fullmatch(r"[\w-]{9}|nextItemId|previousItemId", s):
                    continue  # internal ids
                add(s, [])

    # Brand-token catch-alls: the template's brand name appears in ways
    # no text extractor can enumerate (attributes, JS props, URLs), so
    # offer every case variant as a final mop-up pair. These are applied
    # LAST automatically (shortest strings sort last in the build).
    # derive the brand from the HOME page — pages[] is sorted, so [0] may
    # be about.html and would yield "About" as the brand token
    home = next((p for p in cfg["pages"]
                 if Path(p).name in ("index.html", "index.htm")),
                cfg["pages"][0])
    title_m = re.search(r"<title>([^<]+)</title>",
                        (root / "pristine" / home).read_text(
                            encoding="utf-8", errors="ignore"))
    if title_m:
        brand = re.split(r"[\s—|:-]+", title_m.group(1).strip())[0]
        brand = re.sub(r"[®™©]", "", brand)  # "Nexmind®" must mop up "Nexmind"
        # the old brand must not survive the build — police it by default
        if brand and not cfg.get("forbidden_words"):
            cfg["forbidden_words"] = [brand]
            write_cfg(root, cfg)
            print(f"forbidden_words auto-set to [{brand!r}] (verify enforces)")
        for variant in {brand, brand.lower(), brand.capitalize()}:
            if variant and variant not in seen:
                seen.add(variant)
                strings.append({
                    "old": variant, "new": "",
                    "max_bytes": len(variant.encode()) if any(
                        variant.encode() in b for b in cms_blobs) else None,
                    "scope": "all",
                    "where": ["brand-token: fill this to catch EVERY "
                              "remaining mention (attributes, JS, URLs)"],
                })

    copy_map = {
        "_instructions": "Fill 'new' for every string you want changed. "
                         "Leave 'new' empty to keep the original. RULES: "
                         "(1) if max_bytes is set, len(new.encode('utf-8')) must be <= max_bytes "
                         "(em-dash/curly quotes = 3 bytes); "
                         "(2) never use backticks ` or ${ in 'new'; "
                         "(3) if a 'new' value contains another entry's 'old' text, set that "
                         "other entry's scope to 'cms' or rewrite one of them.",
        "strings": strings,
        "images": [{"old": u, "new": ""} for u in sorted(images)],
        "links": [{"old": u, "new": ""} for u in sorted(links)],
    }

    # PRESERVE existing work: re-running inventory must never wipe fills,
    # styles, or removals the owner/AI already made. Merge 'new' values
    # (and styles/remove sections) from any current copy_map by 'old' key.
    existing = root / "copy_map.json"
    if existing.exists():
        try:
            prev = json.loads(existing.read_text(encoding="utf-8"))
            kept = 0
            for sec in ("strings", "images", "links"):
                pnew = {e["old"]: e["new"] for e in prev.get(sec, [])
                        if e.get("new")}
                # A REFUSAL IS AS WORTH KEEPING AS A FILL. The rebrand
                # loop drops an entry after two declines, so 'Home' stops
                # costing a request every round. Re-running inventory used
                # to reset that ledger while preserving the fills, and the
                # next run spent six batches of 25 re-asking about strings
                # a model had already refused twice.
                ptry = {e["old"]: e["_tries"] for e in prev.get(sec, [])
                        if e.get("_tries")}
                for e in copy_map[sec]:
                    if e["old"] in pnew:
                        e["new"] = pnew[e["old"]]
                        kept += 1
                    if e["old"] in ptry:
                        e["_tries"] = ptry[e["old"]]
            for sec in ("styles", "remove"):
                if prev.get(sec):
                    copy_map[sec] = prev[sec]
            if kept or prev.get("styles") or prev.get("remove"):
                print(f"preserved {kept} existing fill(s)"
                      + (" + styles/removals" if prev.get("styles")
                         or prev.get("remove") else ""))
        except Exception:
            pass

    # PROSE THAT ONLY EXISTS IN THE CHUNKS.
    #
    # Framer splits a sentence across per-word spans, so harvesting the
    # page's text nodes yields fragments while the WHOLE sentence lives in
    # a chunk as one template literal. Measured on a real rebrand: 99% of
    # HTML text nodes were covered and the homepage still said "Makro is a
    # financial clarity platform ... understand cash", because that string
    # was never an entry. Nothing could fill it, and short pairs rewrote
    # half of it into something nobody wrote.
    #
    # So harvest the literals too. Prose only — anything with code in it,
    # a url, or no sentence shape stays out, because a bad entry here gets
    # replaced across every layer.
    _have = {e["old"] for e in strings}
    # RUNTIME STRINGS ARE NOT SITE COPY. The first filter kept anything
    # sentence-shaped, which swept in font stacks, glyph coverage sets,
    # React's own error messages and Framer's editor hints — 48 entries
    # the model correctly refused to rebrand, because "React.Children.only
    # expected to receive a single React element child" is not marketing.
    # It looked like the model failing; it was the harvest asking a silly
    # question.
    _CODEY = ("=>", "function", "return ", "${", "://", "var(", "px",
              "null", "undefined", "className", "sans-serif", "serif",
              "Placeholder", "React", "props", "component", "must be",
              "expected to", "npm", "webpack", "Fragment", "useState",
              "Youtube video", "thumbnail improves", "maxBatchSize")
    _added = 0
    for _src in chunk_texts:
        for _lit in re.findall(r"`([^`]{25,400})`", _src):
            s = _lit.strip()
            if s in _have or len(s.split()) < 5:
                continue
            if any(c in s for c in _CODEY) or s[0] in "<{[/.#":
                continue
            if not re.search(r"[a-z]", s) or not re.search(r"[.!?,:]", s):
                continue
            if sum(ch.isalpha() or ch.isspace() for ch in s) < len(s) * 0.85:
                continue
            # a glyph-coverage string is single characters separated by
            # spaces; real prose is words
            _w = s.split()
            if sum(1 for x in _w if len(x) == 1) > len(_w) * 0.25:
                continue
            if sum(1 for x in _w if len(x) >= 3) < 5:
                continue
            _in_cms = any(s.encode() in b for b in cms_blobs)
            ent = {"old": s, "new": "", "scope": "all",
                   "where": ["chunks"] + (["cms"] if _in_cms else []),
                   # the key must exist even when there is no lock: the
                   # summary and every consumer read it unconditionally
                   "max_bytes": len(s.encode()) if _in_cms else None}
            strings.append(ent)
            _have.add(s)
            _added += 1

    # DISPLAY STRINGS TOO SHORT TO LOOK LIKE PROSE.
    #
    # The pass above asks "does this read like a sentence?", so it needs
    # five words and punctuation — and a stat label like `Balance
    # Increase` has neither. That label is rendered per-character, so the
    # HTML holds twenty single-character spans and the whole string exists
    # only here. It survived a rebrand that scored 0 brand mentions and
    # 80% rewritten, sitting on the homepage in the owner's own words'
    # place.
    #
    # POSITION IS THE EVIDENCE, NOT SHAPE. A literal in `children:` or
    # `text:` position is what React renders — it is copy by construction,
    # so no sentence test is needed or wanted. Framer's own marketplace
    # promo card ("Proceed to checkout", "$995 total value") is harvested
    # too; that card is CSS-hidden, so filling it is harmless noise rather
    # than a wrong edit. Runtime state words are excluded because they are
    # swapped by code, not read by a visitor.
    _STATE = {"content", "success", "error", "loading", "idle", "default",
              "true", "false", "none", "auto"}
    for _src in chunk_texts:
        for _m in re.finditer(r"(?:children|text):`([^`]{2,200})`", _src):
            s = _m.group(1).strip()
            if s in _have or s.lower() in _STATE:
                continue
            if any(c in s for c in _CODEY) or s[0] in "<{[/.#":
                continue
            if not re.search(r"[A-Za-z]{2}", s):
                continue
            if " " not in s and len(s) < 4:
                continue
            _in_cms = any(s.encode() in b for b in cms_blobs)
            strings.append({"old": s, "new": "", "scope": "all",
                            "where": ["chunks"] + (["cms"] if _in_cms else []),
                            "max_bytes": len(s.encode()) if _in_cms else None})
            _have.add(s)
            _added += 1
    if _added:
        print(f"  chunk prose: {_added} sentence(s) that exist only in the "
              f"runtime data (split text) — now fillable")

    existing.write_text(
        json.dumps(copy_map, indent=1, ensure_ascii=False), encoding="utf-8")
    _seal_pristine(root)
    print(f"copy_map.json written: {len(strings)} strings "
          f"({sum(1 for s in strings if s['max_bytes'])} CMS-budgeted), "
          f"{len(images)} images, {len(links)} links.")
    print("Fill in the 'new' fields (this is the AI's ONLY job), then: build")


# ─────────────────────────── build ───────────────────────────────────

VOID_TAGS = {"img", "br", "hr", "input", "meta", "link", "source",
             "area", "base", "col", "embed", "track", "wbr"}

# Per-character split text (entrance animations): Framer renders
# headings as word-wrappers of single-char spans. Replacing the string
# in the chunks alone breaks hydration (SSR spells the OLD chars) and
# the appear engine leaves the text invisible. So build REBUILDS each
# matching run to spell the NEW text with identical span markup — the
# runtime sees exactly what it would render itself: animation intact.
_CHAR = r"(?:&[a-zA-Z]+;|&#\d+;|[^<])"
SPLIT_RUN_RE = re.compile(
    rf"(?:<span\b[^>]*>(?:<span\b[^>]*>{_CHAR}</span>)+</span>\s?)+")
CHAR_SPAN_RE = re.compile(rf"(<span\b[^>]*>)({_CHAR})</span>")


def _regen_split_runs(t, pairs, stats=None):
    want = {}
    for p in pairs:
        # single WORDS split per-char are common (giant marquee text) —
        # match case-insensitively and transfer the run's casing
        if p["scope"] != "cms" and p["new"].strip() and len(p["old"]) >= 3:
            want[re.sub(r"\s+", " ", p["old"]).strip().lower()] = \
                (p["old"], p["new"].strip())
    if not want:
        return t, 0
    out, pos, count = [], 0, 0
    for m in SPLIT_RUN_RE.finditer(t):
        seg = m.group(0)
        key = re.sub(r"\s+", " ",
                     html_mod.unescape(re.sub(r"<[^>]+>", "", seg))).strip()
        hit = want.get(key.lower())
        if hit is None:
            continue
        orig_old, new = hit
        if key.isupper() and not new.isupper():
            new = new.upper()
        elif key[:1].isupper() and orig_old[:1].islower():
            new = new[:1].upper() + new[1:]
        wrap_open = re.match(r"<span\b[^>]*>", seg).group(0)
        chars = CHAR_SPAN_RE.findall(seg)
        if len({c[0] for c in chars}) != 1:
            continue            # irregular markup — refuse, stay safe
        char_open = chars[0][0]
        rebuilt = " ".join(
            wrap_open + "".join(char_open + html_mod.escape(ch) + "</span>"
                                for ch in w) + "</span>"
            for w in new.split(" ") if w)
        if seg.endswith(" "):
            rebuilt += " "
        out.append(t[pos:m.start()])
        out.append(rebuilt)
        pos = m.end()
        count += 1
        if stats is not None:
            stats[orig_old] = stats.get(orig_old, 0) + 1
    if not count:
        return t, 0
    out.append(t[pos:])
    return "".join(out), count


def _remove_nth_element(html, rm):
    """Physically delete the nth element matching {tag,id,classes} —
    balanced-tag scan, no parser needed. Returns html unchanged if the
    element can't be located (never guess-deletes)."""
    tag = rm.get("tag", "div")

    def pred(open_tag):
        if rm.get("id"):
            return re.search(rf'\bid="{re.escape(rm["id"])}"', open_tag)
        # href match — lets us delete class-less links (Webflow's footer
        # "Powered By" credit is a bare <a href="webflow.com"> with no
        # class, un-targetable by tag+class alone).
        if rm.get("href"):
            return re.search(rf'href="[^"]*{re.escape(rm["href"])}',
                             open_tag)
        want = rm.get("classes") or []
        if not want:
            return False
        m = re.search(r'class="([^"]*)"', open_tag)
        return m and set(want) <= set(m.group(1).split())

    idx = -1
    for m in re.finditer(rf"<{tag}\b[^>]*>", html, re.I):
        if not pred(m.group(0)):
            continue
        idx += 1
        if idx != rm.get("index", 0):
            continue
        start = m.start()
        if tag.lower() in VOID_TAGS or m.group(0).endswith("/>"):
            return html[:start] + html[m.end():]
        depth, pos = 1, m.end()
        pat = re.compile(rf"<{tag}\b[^>]*>|</{tag}\s*>", re.I)
        while depth:
            m2 = pat.search(html, pos)
            if not m2:
                return html          # malformed — refuse to delete
            depth += -1 if m2.group(0).startswith("</") else 1
            pos = m2.end()
        return html[:start] + html[pos:]
    return html

def _locate_element(html, contains, ancestor=0, occurrence=0):
    """Find the element whose visible text contains `contains`, climb
    `ancestor` levels, and return a {tag,id,classes,index,label}
    descriptor that _remove_nth_element can delete — plus a text preview
    so the caller can confirm before deleting. None if not found.

    This is the agent-facing analogue of the visual editor's click +
    breadcrumb: the editor picks the element from a DOM click; here we
    pick it from the text an agent already knows (via get_content)."""
    from html.parser import HTMLParser
    want = re.sub(r"\s+", " ", contains or "").strip().lower()
    if not want:
        return None
    ok_cls = re.compile(r"^[A-Za-z0-9_-]+$")

    class Node:
        __slots__ = ("tag", "id", "classes", "parent", "children", "own",
                     "href")

        def __init__(self, tag, eid, classes, parent, href=None):
            self.tag, self.id, self.classes, self.parent = tag, eid, classes, parent
            self.children, self.own, self.href = [], "", href

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = Node("root", None, [], None)
            self.cur = self.root
            self.order = []          # every element in document order
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            ad = dict(attrs)
            classes = [c for c in (ad.get("class") or "").split()
                       if ok_cls.match(c)]
            n = Node(tag, ad.get("id"), classes, self.cur,
                     ad.get("href"))
            self.cur.children.append(n)
            self.order.append(n)
            # never harvest text from <head> (title/meta) or non-visual
            # nodes — otherwise a word in the page <title> becomes a
            # "hit" and the target climbs to <html>.
            if tag in ("script", "style", "svg", "head"):
                self.skip += 1
            if tag not in VOID_TAGS:
                self.cur = n

        def handle_startendtag(self, tag, attrs):
            ad = dict(attrs)
            classes = [c for c in (ad.get("class") or "").split()
                       if ok_cls.match(c)]
            n = Node(tag, ad.get("id"), classes, self.cur,
                     ad.get("href"))
            self.cur.children.append(n)
            self.order.append(n)

        def handle_endtag(self, tag):
            if tag in ("script", "style", "svg", "head") and self.skip:
                self.skip -= 1
            c = self.cur
            while c.parent is not None:
                if c.tag == tag:
                    self.cur = c.parent
                    return
                c = c.parent

        def handle_data(self, d):
            if self.skip:
                return
            t = d.strip()
            if t:
                self.cur.own = (self.cur.own + " " + t).strip()

    p = P()
    try:
        p.feed(html)
    except Exception:
        return None

    fulltext = {}

    def full(n):
        if id(n) in fulltext:
            return fulltext[id(n)]
        s = n.own
        for c in n.children:
            s = (s + " " + full(c)).strip()
        s = re.sub(r"\s+", " ", s).strip()
        fulltext[id(n)] = s
        return s

    # tightest elements whose text contains the query — i.e. those with
    # no CHILD that also contains it (so the nav "Careers" link and the
    # footer "Careers" link are two distinct hits, not one collapsed).
    # `occurrence` picks among them in document order.
    tight = [n for n in p.order if want in full(n).lower()
             and not any(want in full(c).lower() for c in n.children)]
    if not tight:
        return None
    if occurrence >= len(tight):
        return None
    target = tight[occurrence]
    for _ in range(max(0, ancestor)):
        if target.parent is None or target.parent is p.root:
            break
        target = target.parent
    # _remove_nth_element targets by tag+id/classes, so a bare element
    # with neither can't be addressed — climb to the nearest ancestor
    # that CAN be (keeps the agent from getting an un-deletable target).
    while not target.id and not target.classes \
            and not (target.href and not target.href.startswith("#")) \
            and target.parent is not None and target.parent is not p.root:
        target = target.parent
    # never hand back a structural wrapper (deleting <html>/<body> nukes
    # the page) or an un-addressable element — refuse instead. A
    # class-less link is still addressable by its href.
    if target.tag in ("html", "head", "body", "root"):
        return None
    href_key = None
    if not target.id and not target.classes:
        if target.href and not target.href.startswith("#"):
            href_key = target.href.split("?")[0]
        else:
            return None

    # index among document-order elements matching this remove predicate
    def matches(n):
        if n.tag != target.tag:
            return False
        if target.id:
            return n.id == target.id
        if href_key:
            return n.href and n.href.split("?")[0] == href_key
        return target.classes and set(target.classes) <= set(n.classes)

    idx = -1
    for n in p.order:
        if matches(n):
            idx += 1
            if n is target:
                break
    out = {"tag": target.tag, "id": target.id,
           "classes": target.classes, "index": idx,
           "label": full(target)[:80]}
    if href_key:
        out["href"] = href_key
    return out


# An ENCODED OPERATOR, not encoded data. `=&gt;` is an arrow function and
# `&amp;&amp;` is a logical and: both are syntax, and neither survives in
# working JavaScript. A string literal that merely contains "&gt;" has no
# encoded operator anywhere, which is what keeps it out of this.
ENCODED_JS = re.compile(
    r"=&gt;|&amp;&amp;|&lt;=|&gt;="
    # An encoded quote where a STRING BEGINS — after [ ( , : ; { = or
    # whitespace. Webflow's WebFont bootstrap has no encoded operator at
    # all, only encoded quotes: `families: [&#34;Open Sans…` , which JS
    # reads as a bitwise AND and rejects with "Unexpected token '&'".
    # Position is what makes this safe: in a legitimate literal such as
    # var s = "&#34;" the entity follows a real quote, never a delimiter.
    r"|[\s\[\(,:;{=]&(?:#34|#39|quot|apos);")
SCRIPT_BLOCK = re.compile(r"(<script[^>]*>)(.*?)(</script>)", re.S | re.I)
# ANY script with a non-JavaScript type carries DATA, not code. Listing
# the types by name missed framer/appear — the entrance-animation
# payload — so those animations were shipping unparseable on every
# saved-file capture while the named handover block was being repaired
# right next to them. The repair verifies itself, so breadth is safe.
DATA_SCRIPT = re.compile(
    r'\btype="(?!text/javascript|application/javascript|module|'
    r'text/babel)[^"]+"', re.I)


def _decode_encoded_scripts(text: str) -> str:
    """Undo HTML-entity encoding inside inline <script> bodies.

    A <script> is a raw-text element: entities inside it are literal
    characters, so a correct serializer never writes them. Some "save
    page source" paths do it anyway, and the result is JavaScript that
    cannot parse — `(()=&gt;{` instead of `(()=>{`.

    The browser said so plainly and for a long time: "Uncaught
    SyntaxError: Unexpected token ')'" and "Unexpected token '&'" on
    every page of the affected builds. It was assumed to be a
    pre-existing export artifact of the kind the invariant says to leave
    alone, and it was not — it is a broken capture we can repair.

    Affects only projects supplied as SAVED FILES (jomiez, jomiez-lesmana,
    test-1, test-2); nothing scraped from a live URL is encoded this way.

    Decoding is gated on an encoded OPERATOR being present, so a script
    whose string literals happen to contain "&gt;" is never touched.
    """
    def fix(m):
        open_tag, body, close = m.groups()
        if DATA_SCRIPT.search(open_tag):
            # A DATA BLOCK CAN BE CHECKED RATHER THAN GUESSED AT.
            #
            # Framer's handover block is JSON the runtime reads with
            # JSON.parse(el.textContent). textContent of a raw-text
            # element is literal, so an encoded quote is never decoded
            # for it and the parse dies at character eight — which is
            # exactly what the browser reported.
            #
            # Here the repair is verifiable: decode only when the block
            # does NOT parse as-is and DOES parse decoded. A blob whose
            # string values merely contain entities still parses as-is
            # and is left alone.
            s = body.strip()
            if not s:
                return m.group(0)
            try:
                json.loads(s)
                return m.group(0)
            except ValueError:
                pass
            try:
                fixed = html_mod.unescape(s)
                json.loads(fixed)
            except ValueError:
                return m.group(0)
            return open_tag + fixed + close
        if not ENCODED_JS.search(body):
            return m.group(0)
        return open_tag + html_mod.unescape(body) + close
    return SCRIPT_BLOCK.sub(fix, text)


def _cms_localize_pairs(cfg, cms_blobs, existing):
    """Localized urls that live inside byte-locked CMS data, as pairs.

    _localize_refs is a plain text substitution, which is right for pages
    and chunks and impossible for a .framercms blob: those are length-
    locked, and a shorter replacement shifts every offset after it.

    So they go through the pair machinery instead, which already knows
    how to hold the lock — fragment padding (#000…), never spaces, since
    the CMS stores a url and its ?query contiguously and a space lands
    mid-url. The padded form then goes into pages, chunks AND the blob
    identically, which is what hydration equality requires.

    The trailing query survives byte-for-byte after the padding; it ends
    up inside the fragment, which is never sent to a server, and a local
    file has no use for scale-down-to= anyway.
    """
    lmap = cfg.get("localized", {}) or {}
    if not lmap or not cms_blobs:
        return []
    have = {p["old"] for p in existing}
    out = []
    for url, fn in lmap.items():
        if url in have or not url.startswith("http"):
            continue
        old_b = url.encode()
        if not any(old_b in blob for blob in cms_blobs):
            continue
        new = "/assets/r/" + fn
        pad = len(old_b) - len(new.encode())
        if pad < 0:
            # cannot hold the lock; pages and chunks still get it via
            # _localize_refs, and the check will still report the blob.
            continue
        out.append({"old": url,
                    "new": new + ("#" + "0" * (pad - 1) if pad else ""),
                    "scope": "all", "in_cms": True, "flex": False})
    if out:
        log(f"CMS: localizing {len(out)} platform url(s) inside byte-locked "
            f"CMS data")
    return out


def _pairs_from_map(root: Path, cms_blobs):
    cm = json.loads((root / "copy_map.json").read_text(encoding="utf-8"))
    pairs = []
    for entry in cm["strings"] + cm["images"] + cm["links"]:
        old, new = entry["old"], entry.get("new", "")
        if not new or new == old:
            continue
        if "`" in new or "${" in new:
            die(f"'new' for {old[:40]!r} contains ` or ${{ — forbidden "
                "(these strings land inside JS template literals)")
        old_b, new_b = old.encode(), new.encode()
        in_cms = any(old_b in blob for blob in cms_blobs)
        # THE CMS STORES RICH TEXT AS JSON; THE HTML DOES NOT.
        #
        # A paragraph harvested from a page carries a plain quote, and the
        # same paragraph inside a .framercms blob carries \" — so the
        # exact-byte test says "not in CMS", the long pair never applies
        # there, and only the SHORT pairs land. Measured: a changelog
        # paragraph became "…model your tactile future. Set building lets
        # you create multiple \"what if\" forecasts… See how it affects
        # runway." — half ceramics, half finance, on a page that scored
        # zero brand mentions. Exactly the half-rewrite the audit exists
        # to prevent, arriving by a route it could not see.
        #
        # So try the JSON-escaped spelling too, and carry BOTH forms: the
        # text layers keep the plain one, the CMS pass gets the escaped
        # one, and the byte lock is measured on what the blob holds.
        cms_old, cms_new = old, new
        if not in_cms and ('"' in old or "\\" in old):
            esc_o, esc_n = json.dumps(old)[1:-1], json.dumps(new)[1:-1]
            if any(esc_o.encode() in blob for blob in cms_blobs):
                in_cms = True
                cms_old, cms_new = esc_o, esc_n
                old_b, new_b = esc_o.encode(), esc_n.encode()
        cms_over = False
        if in_cms:
            if len(new_b) > len(old_b):
                if new.startswith(("/", "http", "./")):
                    # a URL that doesn't fit its byte-locked slot is
                    # RECOVERABLE — cmd_build ships a short alias copy
                    # (local assets) or falls back to text-layers-only
                    # (external). Never a dead build.
                    cms_over, in_cms = True, False
                else:
                    # TEXT THAT WILL NOT FIT IS NOT A DEAD BUILD EITHER.
                    #
                    # A URL over its slot degrades to text-layers-only
                    # (above); text killed the whole build instead. That
                    # asymmetry bites the commonest rebrand there is: a
                    # brand whose name is longer than the one it replaces.
                    # Measured on this repo — "Makro"(5) -> "Jomiez"(6) is
                    # one byte over a locked slot, and one byte refused a
                    # 644-string migration outright.
                    #
                    # So it degrades the same way: HTML and chunks get the
                    # new text (they stay identical to each other, which
                    # is what hydration requires), the CMS blob keeps the
                    # old bytes, and the mismatch is REPORTED rather than
                    # hidden. verify's forbidden-word scan then names the
                    # leftover, so the owner learns it from a check
                    # instead of from a screenshot.
                    cms_over, in_cms = True, False
                    OVER_SLOT.add(old.lower())
                    log(f"NOTE {old[:32]!r} is {len(new_b) - len(old_b)} "
                        f"byte(s) over its CMS slot ({len(old_b)}): text "
                        f"layers updated, CMS keeps the original. Shorten "
                        f"the replacement to change it everywhere.")
            else:
                # identical padded text EVERYWHERE or hydration mismatches
                pad = len(old_b) - len(new_b)
                if pad and new.startswith(("/", "http", "./")):
                    # URLs: spaces inside a URL 404 (CMS often stores the
                    # url and its ?query contiguously — padding lands MID-
                    # url). A #fragment is never sent to the server, so it
                    # pads to exact byte length without breaking the fetch.
                    new = new + "#" + "0" * (pad - 1)
                else:
                    new = new + " " * pad
        pairs.append({"old": old, "new": new,
                      "scope": entry.get("scope", "all"), "in_cms": in_cms,
                      "cms_over": cms_over,
                      "flex": bool(entry.get("flex"))})
        if in_cms and cms_old != old:
            # The blob spells it escaped, so it needs its own pair.
            # `new` is already padded by the ESCAPED delta above, so the
            # escaped replacement lands at exactly the escaped old's byte
            # length, and both layers decode to the same characters —
            # which is what hydration compares. scope="cms" keeps _rx
            # (pages and chunks) from ever seeing this spelling.
            pairs.append({"old": cms_old, "new": json.dumps(new)[1:-1],
                          "scope": "cms", "in_cms": True,
                          "cms_over": False, "flex": False})
    # longest-first ordering; replacement itself is single-pass (see _rx)
    # so pair collisions can no longer clobber each other's new text
    pairs.sort(key=lambda p: -len(p["old"]))
    return pairs


def _rx(pairs, escaped):
    """One compiled alternation (longest-first) + replacement lookup.
    Single-pass: text a pair just inserted is never re-scanned, so
    'Design'->'AI' can't turn 'AI Workflow Design' into 'AI Workflow AI'.
    Single-word pairs get \\b guards so they can't hit 'Designer'.
    (ASCII \\w check keeps text and CMS-bytes semantics identical.)"""
    parts, lookup = [], {}
    for p in pairs:
        if p["scope"] == "cms":
            continue
        # THE SAME SENTENCE IS SPELLED DIFFERENTLY AT EACH NESTING DEPTH.
        #
        # A page carries the paragraph plainly inside <p>, and carries it
        # AGAIN inside an embedded CMS payload — a JSON document inside a
        # JS string — where its quotes are escaped once, then twice. Only
        # the plain copy matched, so the visible text came out half
        # rewritten: "…model your tactile future. Set building lets you
        # create multiple \"what if\" forecasts… See how it affects
        # runway." The short pairs reached the nested copy; the long one
        # that would have replaced the whole sentence never could.
        #
        # Registering every escape level costs nothing when the string
        # holds no quote (the common case skips this entirely) and makes
        # the replacement whole when it does.
        forms = [(p["old"], p["new"])]
        if '"' in p["old"] or "\\" in p["old"]:
            o1, n1 = json.dumps(p["old"])[1:-1], json.dumps(p["new"])[1:-1]
            o2, n2 = json.dumps(o1)[1:-1], json.dumps(n1)[1:-1]
            forms += [(o1, n1), (o2, n2)]
        for old, new in forms:
            _register(parts, lookup, old, new, p, escaped)
    return (re.compile("|".join(parts)) if parts else None), lookup


def _register(parts, lookup, old, new, p, escaped):
    """Add one spelling of a pair to the alternation."""
    if escaped:
        old = html_mod.escape(old, quote=False)
        new = html_mod.escape(new, quote=False)
    if old in lookup:
        return
    pat = re.escape(old)
    if re.fullmatch(r"\w+", old, re.A):
        pat = rf"\b{pat}\b"
    parts.append(pat)
    lookup[old] = (new, p["old"])           # (replacement, original old)


def _flex_pat(old):
    """Whitespace/entity-tolerant pattern: matches the text however the
    export wrapped it (newlines, indent, &nbsp;, \\xa0) — including the
    ESCAPED forms (\\n, \\t) that appear inside minified chunk strings,
    which is where client-side route pages keep their copy."""
    ws = r"(?:\s|&nbsp;|\xa0|\\n|\\t)+"

    # AND THE CHARACTERS THAT ARE THEMSELVES ENTITY-ENCODED.
    #
    # The docstring already promised entity tolerance and delivered it
    # only for SPACES. A word containing & < > or a quote is stored as
    # &amp; &lt; &gt; &#39; in the page, so the harvested form never
    # matched — and because replacement is one pass, longest-first, a
    # failed long pair lets a SHORTER pair match inside it. Measured:
    # "Income" -> "Images" fired inside "Income &amp; expense tracking",
    # shipping a half-rebranded line on eight spots.
    ENT = {"&": r"(?:&|&amp;)", "<": r"(?:<|&lt;)", ">": r"(?:>|&gt;)",
           "'": r"(?:'|&#39;|&apos;|’)", '"': r'(?:"|&#34;|&quot;)'}

    def word(w):
        return "".join(ENT.get(c, re.escape(c)) for c in w)

    return ws.join(word(w) for w in old.split())


# Webflow asset filenames EMBED the brand (kitpro-cyntra…min.css) —
# rewriting them points at CDN files that don't exist and kills the
# stylesheet. Shield template-CDN urls from all replacement unless the
# owner deliberately retargeted that exact asset (it's a pair old).
CDN_URL_RE = re.compile(
    r"https://[a-z0-9.-]*(?:website-files\.com|framerusercontent\.com)"
    r"[^\"'\s<>`]+")  # backtick ends template-literal urls in chunks;
# parens ARE allowed — Webflow filenames embed them ("Portrait (6).avif"),
# and the shield's pair-old exception matches by prefix so a trailing
# url(...) paren is harmless (vault+restore is byte-exact either way)


def _js_literal_mask(s):
    """-> (mask, trustworthy). mask[i] is 1 where s[i] is inside a JS string.

    IN A CHUNK, COPY LIVES IN LITERALS AND NOTHING ELSE DOES. Framer names
    a variant "Annual" and then emits it BOTH as display text inside a
    template literal AND as a bare object key in code:

        Xf={"Annual Mobile":`PnMUPIAUp`, Annual:`yEfDEXydQ`, Montly:`aRW8N7ZRz`}

    Rewriting the copy to "Complete Sets" also rewrote the key, producing
    `Complete Sets:` — two identifiers where JavaScript expects one. The
    chunk stopped parsing, hydration died, and the page rendered 126
    characters instead of 8,149. verify saw nothing wrong: every file was
    present and the brand was gone. Only probe caught it.

    The scanner does not understand regex literals or comments, so an
    apostrophe inside `/['"]/` can desync it. Measured across 431 real
    chunks in 7 projects, 430 end with every literal closed; the one that
    does not is a vendored lottie player. An unbalanced end is the tell,
    and the caller degrades instead of trusting a bad mask.
    """
    mask = bytearray(len(s))
    delim = None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if delim:
            mask[i] = 1
            if c == "\\":
                if i + 1 < n:
                    mask[i + 1] = 1
                i += 2
                continue
            if c == delim:
                delim = None
        elif c in "`\"'":
            delim = c
            mask[i] = 1
        i += 1
    return mask, delim is None


def _code_position(s, a, b):
    """Conservative fallback: does s[a:b] sit where JS expects a NAME?

    Used only when the literal mask cannot be trusted. It catches the
    shape that actually broke a build — a bare object key or a property
    access — and leaves everything else replaceable, so a desynced mask
    costs correctness in one narrow direction rather than silently
    dropping every fill in the file.
    """
    before = s[:a].rstrip()[-1:]
    after = s[b:b + 1]
    if before == ".":
        return True
    return after == ":" and before in ",{;"


def _apply(text, pairs, escaped=False, stats=None, code=False):
    pair_urls = tuple(p["old"] for p in pairs if p["old"].startswith("http"))
    vault = []

    def _shield(m):
        u = m.group(0)
        if pair_urls and u.startswith(pair_urls):
            return u
        vault.append(u)
        return f"\x00CDN{len(vault) - 1}\x00"

    def bump(orig, n=1):
        if stats is not None and n:
            stats[orig] = stats.get(orig, 0) + n

    text = CDN_URL_RE.sub(_shield, text)
    mask = trusted = None
    if code:
        mask, trusted = _js_literal_mask(text)

    def _replaceable(m):
        if not code:
            return True
        if trusted:
            return bool(mask[m.start()])
        return not _code_position(text, m.start(), m.end())

    rx, lookup = _rx(pairs, escaped=False)
    if rx:
        def repl(m):
            if not _replaceable(m):
                return m.group(0)        # this is code, not copy
            new, orig = lookup[m.group(0)]
            bump(orig)
            return new
        text = rx.sub(repl, text)
    # editor-picked entries carry the DOM-normalized text, which may
    # differ from the source's whitespace — match those flexibly
    for p in pairs:
        if p.get("flex") and p["scope"] != "cms" and " " in p["old"]:
            rep = p["new"].replace("\\", "\\\\")
            if code:
                m2, t2 = _js_literal_mask(text)

                def _flex_repl(m, _r=rep, _m=m2, _t=t2):
                    ok = bool(_m[m.start()]) if _t else \
                        not _code_position(text, m.start(), m.end())
                    return m.expand(_r) if ok else m.group(0)
                text, n = re.subn(_flex_pat(p["old"]), _flex_repl, text)
            else:
                text, n = re.subn(_flex_pat(p["old"]), rep, text)
            bump(p["old"], n)
    if escaped:
        rx2, lk2 = _rx(pairs, escaped=True)
        if rx2:
            def repl2(m):
                new, orig = lk2[m.group(0)]
                bump(orig)
                return new
            text = rx2.sub(repl2, text)
        for p in pairs:  # exports hard-wrap long text nodes
            if p["scope"] == "cms" or " " not in p["old"]:
                continue
            o = html_mod.escape(p["old"], quote=False)
            n = html_mod.escape(p["new"], quote=False)
            pat = r"\s+".join(re.escape(w) for w in o.split())
            text, k = re.subn(pat, n.replace("\\", "\\\\"), text)
            bump(p["old"], k)
    for i, u in enumerate(vault):
        text = text.replace(f"\x00CDN{i}\x00", u)
    return text


# The Framer CMS loader's response guard, as shipped (minified names
# vary per template, structure doesn't):
#   let c=await s.arrayBuffer(),l=new Uint8Array(c);
#   if(l.length!==i)throw Error(`…Unexpected response length`);
#   let u=new We,d=0;for(let e of n){…}
# where n = the merged {from,to} range list the request was built from.
RANGE_GUARD_RE = re.compile(
    r"let ([\w$]+)=await ([\w$]+)\.arrayBuffer\(\),"
    r"([\w$]+)=new Uint8Array\(\1\);"
    r"if\(\3\.length!==([\w$]+)\)throw Error\(`[^`]*`\);"
    r"(let [\w$]+=new [\w$]+,[\w$]+=0;for\(let [\w$]+ of ([\w$]+)\))")



# Localising an asset turns an ABSOLUTE CDN url into a root-relative
# path, and Framer's own code calls `new URL(src)` on image sources with
# no base. That is fine for "https://framerusercontent.com/images/x.png"
# and throws for "/assets/r/x.png" — measured on one template: 36 throws,
# a code component crashed, and 18 of 19 images vanished from the page
# while every file check still passed.
#
# Supplying a base fixes it without changing any absolute url, because
# `new URL(absolute, base)` ignores the base. Only the single-argument
# form is rewritten; calls that already pass a base are left alone.
# ONLY an image source. A blanket patch is wrong: Framer also uses
# `try{ new URL(x) }` as an "is this absolute?" TEST, where the
# throw is the feature — giving those a base made every string
# parse as absolute, the routing logic broke, and the page stopped
# rendering entirely. `.src` is only ever an asset url we localized.
BARE_URL_RE = re.compile(
    r"new URL\(\s*([\w$]+(?:\??\.[\w$]+)*\??\.src)\s*\)")


def _url_base(text: str) -> tuple:
    """-> (patched, count). Give bare `new URL(x)` a base to resolve
    against, so localized relative paths parse."""
    return BARE_URL_RE.subn(r"new URL(\1,location.origin)", text)

def _static_slice(m):
    c, s, l, i, rest, n = m.groups()
    return (
        f"let {c}=await {s}.arrayBuffer(),{l}=new Uint8Array({c});"
        # full file came back (static host ignored ?range=): slice it
        # ourselves into exactly what a protocol server would have sent
        f"if({l}.length>{i}){{let __fw=new Uint8Array({i}),__fo=0;"
        f"for(let __fr of {n}){{__fw.set({l}.subarray(__fr.from,__fr.to),"
        f"__fo),__fo+=__fr.to-__fr.from}}{l}=__fw}}"
        f"if({l}.length!=={i})"
        f"throw Error(`Request failed: Unexpected response length`);"
        + rest)


# Framer's default/auto component names. Hiding by one of these hits
# unrelated components site-wide — and hydration mints MORE instances
# than the SSR HTML shows (the "Variant 1" incident: SSR counted 3,
# the live rule also swallowed nav arrows and a menu item).
GENERIC_FRAMER_NAME_RE = re.compile(
    r"^(?:Variant \d+|Desktop|Phone|Mobile|Tablet|Frame(?: \d+)?|"
    r"Container|Content|Wrapper|Stack|Group|Text|Image|Icon|Arrow|"
    r"Nav|Top|Menu|Item|Card|Button|Link|\d+)$")


def hide_selector_audit(sel, page_texts):
    """Blast-radius audit for OUR hide-selector grammar.
    Returns {count, generic, risky, why} — risky selectors hide
    unrelated parts of the site and must be scoped or dropped."""
    sel = sel.strip()
    m = re.fullmatch(r'\[data-framer-name="([^"]+)"\]', sel)
    if m:
        name = m.group(1)
        n = sum(t.count(f'data-framer-name="{name}"') for t in page_texts)
        generic = bool(GENERIC_FRAMER_NAME_RE.fullmatch(name))
        why = (f"'{name}' is a Framer default name — hydration re-creates "
               "it on unrelated components site-wide" if generic else
               f"matches {n} elements across the pages")
        return {"count": n, "generic": generic,
                "risky": generic or n > 3, "why": why}
    cls = re.findall(r"\.([\w-]+)", sel)
    if cls and " " not in sel and ">" not in sel:
        n = sum(1 for t in page_texts
                for cm_ in re.finditer(r'class="([^"]*)"', t)
                if all(c in cm_.group(1).split() for c in cls))
        return {"count": n, "generic": False, "risky": n > 3,
                "why": f"matches {n} elements across the pages"}
    return {"count": None, "generic": False, "risky": False, "why": ""}


# ─────────────────────────── heal ────────────────────────────────────
# SELF-HEALING for failed edits. The report tells us with certainty
# which fills replaced nothing (zeros) or would be reverted by
# hydration (__at_risk__). Every cause we know has a DETERMINISTIC
# fix, so no model touches the mechanics — the ladder:
#   1. flex upgrade        old matches source, just wrapped/spaced
#   2. casing adoption     old matches a real source string except case
#   3. fuzzy adoption      old is a near-miss of ONE clear source
#                          string (>=0.85 similarity) — adopt its exact
#                          spelling, keep the owner's new text
#   4. honest diagnosis    say exactly why + the closest candidates,
#                          never guess
# Byte budgets stay law: a healed old that lives in CMS refuses the
# fix (with the exact overshoot) rather than let build die later.

def _heal_corpus(root: Path, cfg: dict):
    """Every string a user could legitimately be editing, from all
    three layers — the candidate universe for adoption."""
    texts = [(root / "pristine" / p).read_text(encoding="utf-8",
                                               errors="ignore")
             for p in cfg["pages"]]
    chunk_ts = [p.read_text(encoding="utf-8", errors="ignore")
                for p in (root / "pristine" / "chunks").glob("*.mjs")]
    blobs = [p.read_bytes()
             for p in (root / "pristine" / "cms").glob("*.framercms")]
    cands = set()
    for t in texts:
        te = TextExtract()
        te.feed(t)
        cands |= {s for s in te.out if len(s) >= 3}
    for t in chunk_ts:
        cands |= {m.group(1) for m in
                  re.finditer(r"(?:text|children):\s*`([^`]{3,400})`", t)}
    for p in (root / "pristine" / "cms").glob("*.framercms"):
        cands |= {s for s in _string_dump_cms(p) if len(s) >= 3}
    return texts + chunk_ts, blobs, sorted(cands)


def _asset_stem(url: str):
    """The stable identifier a CDN asset keeps across every size/format
    variant: the longest alphanumeric run in its FILENAME (Webflow
    content hash / asset id, Framer image id). Filename only — the
    site-id path segment is shared by EVERY asset and would match the
    whole site. 16+ chars = unique enough to never collide."""
    tail = url.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    runs = re.findall(r"[A-Za-z0-9]{16,}", tail)
    return max(runs, key=len) if runs else None


def _image_variant_urls(stem: str, texts):
    """Every real source URL sharing this asset id — all srcset size
    variants and every filename encoding, exactly as the source spells
    them (so replacement lands byte-for-byte). The char class allows
    ( ) because Webflow filenames embed them ("Portrait (6).avif");
    trailing punctuation (from url(…) / srcset / prose) is stripped,
    same as the asset localizer."""
    pat = re.compile(r"https?://[^\"'\s<>`\\]*" + re.escape(stem)
                     + r"[^\"'\s<>`\\]*")
    urls = set()
    for t in texts:
        for m in pat.finditer(t):
            urls.add(m.group(0).rstrip(",);."))
    return urls


def cmd_heal(args):
    import difflib
    root = Path.cwd()
    cfg = read_cfg(root)
    cm = json.loads((root / "copy_map.json").read_text(encoding="utf-8"))
    report = {}
    rp = root / "site" / ".forge-report.json"
    if rp.exists():
        report = json.loads(rp.read_text(encoding="utf-8"))
    at_risk = set(report.get("__at_risk__", []))
    only = args[args.index("--entry") + 1] if "--entry" in args else None

    texts, blobs, cands = _heal_corpus(root, cfg)

    # destructive hide rules first (the "Variant 1" incident): a hide
    # selector with a generic Framer default name — or one matching
    # many elements — swallows unrelated parts of the site. Drop it;
    # re-removing in edit mode now produces a safely scoped selector.
    page_texts_only = [(root / "pristine" / p).read_text(
        encoding="utf-8", errors="ignore") for p in cfg["pages"]]
    kept, dropped = [], []
    for sel in cfg.get("hide_selectors", []):
        a = hide_selector_audit(sel, page_texts_only)
        (dropped if a["risky"] else kept).append((sel, a))
    if dropped:
        cfg["hide_selectors"] = [s for s, _ in kept]
        write_cfg(root, cfg)
        for sel, a in dropped:
            print(f"HEALED: destructive hide rule DROPPED: {sel} — "
                  f"{a['why']}. It was hiding unrelated elements; "
                  "re-remove the element in edit mode (selectors are "
                  "now scoped safely).")

    def exact_any(s):
        sb = s.encode()
        return any(s in t for t in texts) or any(sb in b for b in blobs)

    def flex_any(s):
        pat = re.compile(_flex_pat(s))
        return any(pat.search(t) for t in texts) or \
            any(re.search(pat.pattern.encode(), b) for b in blobs)

    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    def cms_budget(s):
        sb = s.encode()
        if any(sb in b for b in blobs):
            return len(sb)
        m = [len(x.group(0)) for b in blobs
             for x in [re.search(re.compile(_flex_pat(s).encode()), b)] if x]
        return min(m) if m else None

    healed, stuck, changed = [], [], False
    for e in cm.get("strings", []):
        old, new = e["old"], (e.get("new") or "")
        if not new or new == old:
            continue
        if only is not None:
            if old != only:
                continue
        elif not (report.get(old) == 0 or old in at_risk):
            continue                       # only touch what is broken
        if old in set(report.get("__moot__", [])):
            print(f"OK:     {old[:60]!r} -> nothing left to replace — "
                  "longer fills already cover every mention")
            continue

        def adopt(src):
            nonlocal changed
            e["old"] = src
            if " " in src and not exact_any(src):
                e["flex"] = True
            budget = cms_budget(src)
            if budget is not None:
                e["max_bytes"] = budget
            changed = True

        budget_new = len(new.encode())
        if " " in old and not e.get("flex") and flex_any(old):
            e["flex"] = True
            changed = True
            healed.append((old, "flex — source wraps/spaces it differently"))
            continue
        # casing: a real source string that differs only by case/space
        ci = [c for c in cands if norm(c).lower() == norm(old).lower()]
        if ci:
            b = cms_budget(ci[0])
            if b is not None and budget_new > b:
                stuck.append((old, f"found the real string but your text "
                              f"is {budget_new - b} byte(s) over its CMS "
                              "budget — shorten it"))
                continue
            adopt(ci[0])
            healed.append((old, f"adopted source casing: {ci[0][:60]!r}"))
            continue
        # fuzzy: ONE clear near-miss in the source
        scored = sorted(((difflib.SequenceMatcher(None, norm(old).lower(),
                                                  norm(c).lower()).ratio(), c)
                         for c in cands if abs(len(c) - len(old)) <
                         max(20, len(old))), reverse=True)
        if scored and scored[0][0] >= 0.85:
            best = scored[0][1]
            b = cms_budget(best)
            if b is not None and budget_new > b:
                stuck.append((old, f"nearest source string found but your "
                              f"text is {budget_new - b} byte(s) over its "
                              "CMS budget — shorten it"))
                continue
            adopt(best)
            healed.append((old, f"adopted nearest source string "
                           f"({scored[0][0]:.0%}): {best[:60]!r}"))
            continue
        near = [c[:60] for _, c in scored[:3]]
        stuck.append((old, "no source string is close enough — re-pick "
                      f"the element in edit mode. Closest: {near}"))

    # ── IMAGE heal ── an image fill can miss for reasons no string edit
    # has: (1) the picked URL was mangled (edit-mode decoded %20 spaces
    # out of a Webflow filename → matches nothing); (2) the asset ships
    # as SRCSET VARIANTS (…-p-500 …-p-2000 …) and only one was swapped,
    # so the browser still shows the old one at other sizes. Both heal
    # the same way: find every REAL source URL that shares this asset's
    # id, point them all at the new image, and drop the junk pick.
    img_src_texts = list(texts)   # pages + chunks
    for cf in (root / "pristine").rglob("*.css"):
        img_src_texts.append(cf.read_text(encoding="utf-8", errors="ignore"))
    img_healed, img_stuck = [], []
    moot_set = set(report.get("__moot__", []))
    existing = {e["old"] for e in cm.get("images", [])}
    for e in list(cm.get("images", [])):
        old, new = e["old"], (e.get("new") or "")
        if not new or new == old:
            continue
        if only is not None:
            if old != only:
                continue
        elif not (report.get(old) == 0 or old in at_risk) or old in moot_set:
            continue
        stem = _asset_stem(old)
        if not stem:
            img_stuck.append((old, "couldn't derive an asset id from the "
                              "URL — re-pick the image in edit mode"))
            continue
        variants = _image_variant_urls(stem, img_src_texts)
        if not variants:
            img_stuck.append((old, "this image isn't in the source — it may "
                              "already be swapped, or the URL is stale"))
            continue
        old_is_real = old in variants
        n_filled = 0
        for v in sorted(variants):
            ex = next((x for x in cm["images"] if x["old"] == v), None)
            if ex is None:
                cm["images"].append({"old": v, "new": new,
                                     "scope": e.get("scope", "all"),
                                     "where": ["heal-image-variant"]})
                existing.add(v)
                n_filled += 1
            elif not ex.get("new"):
                ex["new"] = new
                n_filled += 1
        if not old_is_real:   # the mangled/junk pick: retire it
            cm["images"] = [x for x in cm["images"] if x is not e]
        changed = True
        img_healed.append((old, f"swapped {n_filled} real source variant(s) "
                           "of this image (all sizes/formats)"))

    if changed:
        (root / "copy_map.json").write_text(
            json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")
    for o, why in healed + img_healed:
        print(f"HEALED: {o[:60]!r} -> {why}")
    for o, why in stuck + img_stuck:
        print(f"STUCK:  {o[:60]!r} -> {why}")
    if not (healed or stuck or dropped or img_healed or img_stuck):
        print("nothing to heal — no broken fills in the last report")
    print(f"heal: {len(healed) + len(dropped) + len(img_healed)} fixed, "
          f"{len(stuck) + len(img_stuck)} need the owner"
          + (" — rebuild to apply" if changed or dropped else ""))


def _write_deploy(site: Path, cfg: dict):
    """Every build ships deploy-ready: the output is fully static
    (client-side CMS range slicing, real .js icon names), so the only
    host-side need left is the SPA fallback for deep routes — covered
    per host family below."""
    framer = cfg.get("platform") == "framer"
    if framer:
        # single-page app: unmatched deep links are client routes ->
        # index.html (real files still win).
        (site / "_redirects").write_text("/* /index.html 200\n",
                                         encoding="utf-8")
        if (site / "index.html").exists():
            shutil.copy(site / "index.html", site / "404.html")
        (site / "vercel.json").write_text(json.dumps({"rewrites": [
            {"source": "/((?!.*\\.).*)", "destination": "/index.html"}]},
            indent=1), encoding="utf-8")
    else:
        # multi-page (Webflow/static): an unmatched path is a genuine
        # 404 -> the styled 404.html with a 404 status, NEVER the home
        # page. (The scraped 404.html is the template's own not-found
        # page; only synthesize one if it's missing.)
        (site / "_redirects").write_text("/* /404.html 404\n",
                                         encoding="utf-8")
        if not (site / "404.html").exists() and (site / "index.html").exists():
            shutil.copy(site / "index.html", site / "404.html")
        # Vercel serves 404.html automatically on not-found — no rewrite.
        (site / "vercel.json").write_text(json.dumps({}, indent=1),
                                          encoding="utf-8")
    (site / "DEPLOY.md").write_text(f"""# Deploy this site

This folder is **fully static** — no server logic needed. The build
already handles the Framer runtime's quirks (CMS byte ranges are
sliced client-side; icon modules ship with real `.js` names). The
files `_redirects`, `404.html` and `vercel.json` cover deep-link
routing per host; hosts ignore the ones they don't use.

## Cloudflare Pages (recommended — free, custom domains)
Dashboard -> Workers & Pages -> Create -> Pages -> Upload assets ->
drop THIS folder. Or: `npx wrangler pages deploy .`

## Netlify
Drag THIS folder onto https://app.netlify.com/drop — done.

## Vercel
`npx vercel .` in this folder (or import in the dashboard).

## GitHub Pages
Push this folder's CONTENTS to your `username.github.io` repo (or a
custom-domain site). NOTE: project pages served under `/repo-name/`
won't work — asset paths are root-absolute; use a user site or a
custom domain.

## Any other static host / CDN / S3 / nginx
Upload as-is. Optional: route unknown extension-less paths to
/index.html so deep links work (real pages and assets must win).

## Local
`python3 serve.py` -> http://127.0.0.1:8000/
(platform: {cfg['platform']}; migrated with Aethron)
""", encoding="utf-8")


def cmd_build(_args):
    root = Path.cwd()
    cfg = read_cfg(root)
    tampered = _pristine_tampered(root)
    if tampered:
        print(f"WARNING: pristine/ was MODIFIED ({len(tampered)} file(s): "
              f"{tampered[:3]}). pristine is the sealed source of truth — "
              "hand-edits get reverted by hydration and corrupt builds. "
              "Restore it (re-init / re-scrape); make changes via "
              "copy_map.json instead.")
    site = root / "site"

    # VALIDATE BEFORE WIPING: pairs (and their budget checks) are built
    # first, so a fatal copy-map problem aborts while the previous
    # site/ is still intact — a failed build must never leave the user
    # with a wiped or half-written site.
    cms_src = root / "pristine" / "cms"
    cms_blobs = [p.read_bytes() for p in cms_src.glob("*.framercms")] \
        if cms_src.exists() else []
    pairs = _pairs_from_map(root, cms_blobs)
    pairs += _cms_localize_pairs(cfg, cms_blobs, pairs)

    # CAN'T-FAIL local images in CMS slots: a local asset whose path is
    # longer than a byte-locked slot can't fit — so build ships a SHORT
    # deterministic ALIAS copy (/assets/i<sha1-8>.<ext>) and swaps that
    # in instead. (External urls can't be aliased — those occurrences
    # stay text-layer-only, loudly noted.)
    alias_files = []       # (alias_rel, source_rel) written once site exists
    for p in pairs:
        if not p.pop("cms_over", False):
            continue
        newp = p["new"]
        if newp.startswith("/") and not newp.startswith("//"):
            ext = ("." + newp.rsplit(".", 1)[-1][:5]) if "." in \
                newp.rsplit("/", 1)[-1] else ""
            alias = "/assets/i" + hashlib.sha1(
                newp.encode()).hexdigest()[:8] + ext
            pad = len(p["old"].encode()) - len(alias.encode())
            if pad >= 0:
                alias_files.append((alias, newp))
                p["new"] = alias + ("#" + "0" * (pad - 1) if pad else "")
                p["in_cms"] = True
                log(f"NOTE: {newp} is longer than its CMS slot — shipped "
                    f"as short alias {alias}")
                continue
        p["in_cms"] = False    # honest fallback: text layers only
        log(f"NOTE: {p['new'][:50]} can't fit its CMS slot even aliased — "
            "CMS occurrences keep the original (text layers swapped)")

    if site.exists():
        shutil.rmtree(site)
    (site / "assets").mkdir(parents=True)

    # ── IMAGE SWAP = ASSET IDENTITY (can't-fail image editing) ──
    # An image entry's `old` is whatever the picker captured — one CDN
    # url, one srcset variant, a %20-mangled name, or (after localize) a
    # hashed local path. Matching that exact string is fragile. Instead,
    # for every filled image pair we find EVERY real source url that
    # shares the asset's id/stem — all srcset sizes, all formats, all
    # encodings — and point them ALL at the new image, PROACTIVELY (heal
    # was doing this only after a failure). Localized picks are reverse-
    # mapped to their original CDN url first, so the stem is recoverable.
    _rev_localized = {v: k for k, v in cfg.get("localized", {}).items()}
    _img_src = [(root / "pristine" / pg).read_text(encoding="utf-8",
                errors="ignore") for pg in cfg["pages"]]
    for _p in (root / "pristine").glob("chunks/*.mjs"):
        _img_src.append(_p.read_text(encoding="utf-8", errors="ignore"))
    for _p in (root / "pristine").rglob("*.css"):
        _img_src.append(_p.read_text(encoding="utf-8", errors="ignore"))
    _img_exts = (".avif", ".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg")
    _existing = {p["old"] for p in pairs}
    _img_extra = []
    _expanded_from = set()   # originals whose swap is done by their variants
    for p in list(pairs):
        old, new = p["old"], p["new"]
        base_old = old.split("?")[0].split("#")[0]
        if p.get("in_cms") or not new.startswith(("/", "http", "./")):
            continue
        if not base_old.lower().endswith(_img_exts) \
                and "/images/" not in base_old \
                and "website-files" not in base_old \
                and "framerusercontent" not in base_old:
            continue
        base = base_old
        if base.startswith("/assets/r/"):        # localized -> real url
            base = _rev_localized.get(base.rsplit("/", 1)[-1], base)
        stem = _asset_stem(base)
        if not stem:
            continue
        for v in sorted(_image_variant_urls(stem, _img_src)):   # deterministic
            if v != old:
                _expanded_from.add(old)
            if v in _existing:
                continue
            # SAME budget rules as _pairs_from_map: a variant living in
            # a CMS blob is byte-locked. Fragment-pad URLs (#000… — never
            # sent to the server); if the new path simply doesn't fit,
            # SKIP that variant gracefully (auto-generated pairs must
            # never crash a build) — the report still shows the swap.
            v_new, v_b = new, v.encode()
            v_cms = any(v_b in blob for blob in cms_blobs)
            if v_cms:
                pad = len(v_b) - len(v_new.encode())
                if pad < 0:
                    log(f"NOTE: variant …{v[-40:]} is CMS-locked and "
                        f"shorter than the new path — skipped (rename "
                        "the new image shorter to cover it)")
                    continue
                if pad:
                    v_new = v_new + "#" + "0" * (pad - 1)
            _existing.add(v)
            _img_extra.append({"old": v, "new": v_new, "scope": "all",
                               "in_cms": v_cms, "flex": False})
    if _img_extra:
        pairs += _img_extra
        # longest-first, then by url — fully deterministic (idempotent builds)
        pairs.sort(key=lambda p: (-len(p["old"]), p["old"]))
        log(f"image variants: +{len(_img_extra)} source url(s) covered "
            "(all sizes/formats/encodings)")

    stats = {p["old"]: 0 for p in pairs}   # per-entry replacement counts
    cm_all = json.loads((root / "copy_map.json").read_text(
        encoding="utf-8")) if (root / "copy_map.json").exists() else {}
    removals = cm_all.get("remove", [])
    if removals:
        log(f"removals: {len(removals)} element(s)")

    # owner style overrides (visual editor 🎨) — baked into every page
    # as !important CSS: beats inline styles, so it even overrides the
    # runtime's JS-driven entrance transforms. Values are sanitized so
    # nothing can escape the declaration block.
    style_css = ""
    for s in cm_all.get("styles", []):
        sel = (s.get("selector") or "").strip()
        if not sel or re.search(r"[{}<]", sel):  # '>' = child combinator
            continue
        decls = [f"{p}:{v} !important"
                 for p, v in (s.get("css") or {}).items()
                 if re.fullmatch(r"[a-zA-Z-]+", p) and isinstance(v, str)
                 and v.strip() and not re.search(r"[{}<>;\\]|expression",
                                                 v, re.I)]
        if decls:
            style_css += sel + "{" + ";".join(decls) + "}"
    if cfg.get("reduce_motion"):
        style_css += ("*{animation-duration:.01s !important;"
                      "transition-duration:.01s !important;"
                      "scroll-behavior:auto !important}")
    if style_css:
        log(f"style overrides: {len(cm_all.get('styles', []))} rule(s)"
            + (" + reduced motion" if cfg.get("reduce_motion") else ""))
    hide_css = HIDE_CSS
    for sel in cfg.get("hide_selectors", []):
        hide_css = hide_css.replace(
            "</style>", f"{sel}{{display:none !important}}</style>")
    if style_css:
        hide_css += f"<style data-forge-styles>{style_css}</style>"

    pub = cfg.get("public_base", "/assets").rstrip("/")

    # HTML pages
    routes = cfg.get("routes", {})
    for page in cfg["pages"]:
        t = (root / "pristine" / page).read_text(encoding="utf-8", errors="ignore")
        for b in cfg.get("site_bases", []):
            t = t.replace(b, "./assets/chunks/")

        # PER-ELEMENT link retargets (copy_map["retargets"]) — rewrite
        # the href of anchors matching class/id AND (optionally) their
        # current href, PHYSICALLY in the code. This is how CTA buttons
        # get pointed somewhere new without touching same-URL nav links
        # (a.primary-button href=/contact -> download, while the nav
        # Contact link keeps /contact). Runs BEFORE the link localizer,
        # so when_href matches the pristine form (/contact). Webflow/
        # static only — Framer hydration re-renders hrefs from chunks.
        if cfg["platform"] != "framer":
            for rt in cm_all.get("retargets", []):
                sel = str(rt.get("selector", "")).strip()
                new_href = str(rt.get("href", "")).strip()
                when = str(rt.get("when_href", "")).strip()
                if not sel or not new_href \
                        or re.search(r"[\"'<>`\s]", new_href) \
                        or not re.fullmatch(r"[A-Za-z0-9 .#_-]+", sel):
                    continue
                m_id = re.match(r"#([\w-]+)$", sel)
                tag = (re.match(r"([a-zA-Z]+)", sel) or [None, "a"])[1]
                want_cls = re.findall(r"\.([\w-]+)", sel)

                def _retarget(m):
                    open_tag = m.group(0)
                    if m_id:
                        if not re.search(
                                rf'\bid="{re.escape(m_id.group(1))}"',
                                open_tag):
                            return open_tag
                    elif want_cls:
                        cm_ = re.search(r'class="([^"]*)"', open_tag)
                        if not cm_ or not set(want_cls) <= \
                                set(cm_.group(1).split()):
                            return open_tag
                    href = re.search(r'href="([^"]*)"', open_tag)
                    if not href:
                        return open_tag
                    if when and href.group(1).split("#")[0].split("?")[0] \
                            .rstrip("/") != when.rstrip("/"):
                        return open_tag
                    return open_tag.replace(
                        f'href="{href.group(1)}"', f'href="{new_href}"')
                t = re.sub(rf"<{tag}\b[^>]*>", _retarget, t)

        # scattered saves link pages absolutely to the LIVE site
        # (https://foo.webflow.io/about) or root-relative (/about) —
        # point them at the local page files instead
        def _localize(m):
            path, tail = m.group("p").strip("/"), m.group("t")
            tgt = routes.get(path)
            if not tgt and path:
                # A NESTED ROUTE AND ITS FILE ARE SPELLED DIFFERENTLY.
                # init names files by flattening the route
                # ("category/webflow" -> category-webflow.html), but when
                # a template ships no <link rel=canonical> the routes
                # table gets keyed by that flattened stem while the page's
                # own links still say "/category/webflow" — so the lookup
                # missed and 15 nav links kept pointing at the original
                # live site. Flatten the same way before giving up.
                slug = re.sub(r"[^\w-]+", "-", path).strip("-")
                tgt = routes.get(slug)
                if not tgt and slug + ".html" in (cfg.get("pages") or []):
                    tgt = slug + ".html"
            return f'href="./{tgt}{tail}"' if tgt else m.group(0)
        for host in cfg.get("own_hosts", []):
            t = re.sub(rf'href="https?://{re.escape(host)}'
                       r'(?P<p>/[^"#?]*|)(?P<t>[^"]*)"', _localize, t)
        if len(routes) > 1:
            t = re.sub(r'href="(?P<p>/[^"#?]*)(?P<t>[^"]*)"', _localize, t)
            # framer-style relative routes (./about) — only rewritten
            # when the page actually exists locally, so single-page SPA
            # navigation is never touched
            t = re.sub(r'href="\./(?P<p>[^"#?]*)(?P<t>[^"]*)"', _localize, t)

        # owner-requested element removals (visual editor's 🗑 or the
        # MCP remove_element tool) — physically deleted from the code.
        # Framer projects route removals to hide_selectors instead
        # (React re-creates DOM). A removal may be scoped to one page
        # via "page"; absent = applies to every page (shared chrome).
        # apply highest index first: deleting a low-index element shifts
        # every higher index of the SAME selector down by one, so removing
        # e.g. footer-link #8 then #10 would hit the wrong element. Sorting
        # descending keeps every entry's index valid at delete time.
        for rm in sorted(removals, key=lambda r: -(r.get("index") or 0)):
            if rm.get("page") and rm["page"] != page:
                continue
            t = _remove_nth_element(t, rm)
        # strip platform identity/telemetry (design untouched by all of these)
        # tolerant forms: saved page sources entity-encode quotes (&#34;)
        # and may break the closing </script> onto its own line
        t = re.sub(r'<script[^>]*src="https://events\.framer\.com[^"]*"'
                   r'[^>]*>\s*</script>\n?', "", t)
        t = re.sub(r'<script>\s*try\s*\{\s*if\s*\(localStorage\.getItem\('
                   r'(?:"|&#34;)__framer_force[^<]*</script>\n?', "", t, flags=re.S)
        t = re.sub(r'<meta name="framer-search-index"[^>]*>\s*', "", t)
        t = re.sub(r'<meta name="generator"[^>]*>\s*', "", t)
        t = re.sub(r'<link rel="canonical"[^>]*>\s*', "", t)
        t = re.sub(r'<meta property="og:url"[^>]*>\s*', "", t)
        t = re.sub(r"<!-- (Made in Framer|Published) [^>]*-->\n?", "", t)
        # template-store validator/review snippets don't belong on a
        # shipped site (seen on kitpro Webflow templates): both the
        # external tag AND the inline __WF_REVIEW_BRIDGE bootstrap
        t = re.sub(r'<script[^>]*src="https://validation-worker\.[^"]*"'
                   r'[^>]*>\s*</script>\n?', "", t)
        t = re.sub(r"<script>[^<]*(?:validation-worker|__WF_REVIEW_BRIDGE)"
                   r"[^<]*</script>\n?", "", t)
        t = t.replace("</head>", hide_css + "\n</head>")
        t = _apply(t, pairs, escaped=True, stats=stats)
        t = _localize_refs(t, cfg.get("localized", {}))
        t, n_regen = _regen_split_runs(t, pairs, stats=stats)
        if n_regen:
            log(f"{page}: {n_regen} split-text run(s) regenerated "
                "(animation preserved)")

        # swapped images: Webflow srcset lists size-suffixed VARIANT
        # files of the original — after a swap the browser would still
        # pick a stale variant. If the (new) src's filename stem no
        # longer appears in the srcset, the srcset is stale: drop it.
        def _strip_stale_srcset(m):
            tag = m.group(0)
            src = re.search(r'\ssrc="([^"]+)"', tag)
            ss = re.search(r'\ssrcset="[^"]*"', tag)
            if not src or not ss:
                return tag
            stem = src.group(1).split("?")[0].split("#")[0] \
                .rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem and stem in ss.group(0):
                return tag
            return re.sub(r'\s(?:srcset|sizes)="[^"]*"', "", tag)
        t = re.sub(r"<img[^>]*>", _strip_stale_srcset, t)

        # Subresource Integrity on LOCAL assets: build rewrites CSS/JS
        # content (brand replacement, localized url() refs), so a
        # <link>/<script integrity="sha…"> hash from the original CDN no
        # longer matches — the browser then SILENTLY refuses the file
        # and the page renders unstyled. Once we own the asset, SRI is
        # invalid; strip integrity/crossorigin from any LOCAL subresource
        # (external CDN refs we didn't touch keep theirs).
        def _drop_local_sri(m):
            tag = m.group(0)
            ref = re.search(r'(?:href|src)="([^"]*)"', tag)
            if ref and not ref.group(1).startswith(("http://", "https://",
                                                    "//")):
                tag = re.sub(r'\s+(?:integrity|crossorigin)="[^"]*"', "", tag)
            return tag
        t = re.sub(r"<(?:link|script)\b[^>]*>", _drop_local_sri, t)

        t = _decode_encoded_scripts(t)

        dest = site / page
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(t, encoding="utf-8")

    # non-page files from multi-file exports (Webflow css/js/images…):
    # pass through with copy pairs applied to text assets. Framer runtime
    # dirs (chunks/cms/icons) and downloaded fonts are handled separately.
    # remote-assets/ is SHIPPED TWICE, and only one copy is real.
    #
    # The localized store has its own loop above, which copies it to
    # /assets/r/ and rewrites the references inside every text asset —
    # that is the copy the build points at. This passthrough then copied
    # the same directory again, verbatim, to /remote-assets/: nothing
    # references it (measured: 0 referring files in all 17 projects,
    # 106-327 duplicate files each), so it doubled the asset payload of
    # every migration and carried the un-rewritten originals with it.
    #
    # That is how ovo regressed. Its served /assets/r/ was clean — 0
    # platform urls — while the shadow copy held 375, and the ownership
    # check, rightly, reads what is shipped rather than what is used.
    reserved = {"chunks", "cms", "icons", "fonts", "remote-assets"}
    page_set = set(cfg["pages"])
    text_ext = (".css", ".js", ".txt", ".xml", ".json", ".svg", ".webmanifest")
    for f in (root / "pristine").rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root / "pristine")
        # .html pages are handled by the page loop above; a pristine page
        # NOT in page_set was intentionally removed (remove_page) — never
        # pass it through as an "asset", or removed pages would reappear.
        if rel.parts[0] in reserved or str(rel) in page_set \
                or f.suffix.lower() in (".html", ".htm"):
            continue
        dest = site / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix.lower() in text_ext:
            dest.write_text(
                _localize_refs(
                    _apply(f.read_text(encoding="utf-8", errors="ignore"),
                           pairs), cfg.get("localized", {})),
                encoding="utf-8")
        else:
            shutil.copy(f, dest)

    # JS chunks: localize CMS + icon bases, kill editor bar, apply copy
    chunks_src = root / "pristine" / "chunks"
    if chunks_src.exists():
        cdir = site / "assets" / "chunks"
        cdir.mkdir()
        n_patched = n_static = n_urlfix = 0
        for p in chunks_src.glob("*.mjs"):
            t = p.read_text(encoding="utf-8", errors="ignore")
            orig = t
            for mb in cfg.get("cms_module_bases", []):
                fake_local = "${location.origin}" + pub + "/cms/" + mb.rsplit("/", 1)[1]
                t = t.replace(mb, fake_local)
            for ib in cfg.get("icon_bases", []):
                t = t.replace(ib, "${location.origin}" + pub + "/icons/")
            t = re.sub(r"EditorBar:([A-Za-z$_][\w$]*)===void 0\?void 0:",
                       "EditorBar:!0?void 0:", t)
            # NEUTRALISE THE EDITOR BAR AT ITS URL, not at its ternary.
            #
            # The patch above matches one shape of the guard; ovo ships
            # another, so the guard stayed live and the page did
            #     await import("https://edit.framer.com/init.mjs")
            # at runtime — a self-hosted site reaching for Framer's editor,
            # which fails with "Failed to fetch dynamically imported
            # module" and is a live call home besides.
            #
            # The import destructures createEditorBar and CALLS it, so an
            # empty module would throw. This stub satisfies the contract
            # and renders nothing. Rewriting the url works whatever the
            # surrounding guard looks like.
            t = t.replace(EDITORBAR_URL, EDITORBAR_STUB)
            # STATIC-HOST HARDENING (the blank-page killer): the CMS
            # loader requests ?range=a-b,c-d and THROWS if the response
            # length differs — a dumb static host ignores the query and
            # returns the whole file. Patch the guard: an over-long
            # response IS the full file, so slice it client-side from
            # the merged range list (identical bytes to a protocol
            # server's reply). Protocol servers still return exact
            # slices and take the untouched fast path.
            t, k = RANGE_GUARD_RE.subn(_static_slice, t)
            n_static += k
            # a localized path is relative; `new URL(x)` needs a base
            t, n_url = _url_base(t)
            n_urlfix += n_url
            # icon modules are import()ed — name.js@1.2.3 gets a wrong
            # MIME on static hosts, which module scripts hard-reject.
            # Ship them as name.1.2.3.js instead (copy is renamed below)
            t = re.sub(r"\.js@([0-9.]+)", r".\1.js", t)
            t = _apply(t, pairs, stats=stats, code=True)
            t = _localize_refs(t, cfg.get("localized", {}))
            (cdir / p.name).write_text(t, encoding="utf-8")
            n_patched += t != orig
        log(f"chunks: {n_patched} patched "
            f"({n_static} static-host range guard(s), "
            f"{n_urlfix} url base(s)), "
            f"{len(list(chunks_src.glob('*.mjs')))} total")

    # CMS binaries: exact-byte-length padded replacement (offsets are law)
    if cms_src.exists() and list(cms_src.glob("*.framercms")):
        mdir = site / "assets" / "cms"
        mdir.mkdir()
        # one-pass, longest-first, \b-guarded exactly like _apply —
        # hydration equality demands identical semantics in every layer
        bparts, blookup = [], {}
        for pr in pairs:
            # ONLY PAIRS THAT WERE FITTED TO A SLOT MAY TOUCH A BLOB.
            #
            # This loop pads with b" " * (len(old) - len(new)), which is
            # empty when the replacement is LONGER — so the blob grows and
            # the size assert below fires. Pairs marked cms_over are
            # text-layers-only by construction (the CMS keeps its bytes and
            # build NOTEs it), and applying them here both corrupts the
            # offsets and contradicts what the NOTE just promised.
            #
            # Latent until the fill layer stopped DISCARDING over-budget
            # answers: no such pair could exist before, so nothing ever
            # reached this line unfitted. The first template with tight
            # slots produced thirteen of them and a dead build.
            if not pr.get("in_cms"):
                continue
            old_b = pr["old"].encode()
            if old_b in blookup:
                continue
            pat = re.escape(old_b)
            if re.fullmatch(r"\w+", pr["old"], re.A):
                pat = rb"\b" + pat + rb"\b"
            bparts.append(pat)
            blookup[old_b] = (pr["new"].encode()
                              + b" " * (len(old_b) - len(pr["new"].encode())),
                              pr["old"])
        brx = re.compile(b"|".join(bparts)) if bparts else None
        flex_subs = []
        for pr in pairs:
            if pr.get("flex") and " " in pr["old"]:
                nb = pr["new"].encode()
                flex_subs.append((
                    re.compile(_flex_pat(pr["old"]).encode()),
                    lambda m, nb=nb: nb + b" " * (len(m.group(0)) - len(nb))
                    if len(nb) <= len(m.group(0)) else m.group(0),
                    pr["old"]))

        def brepl(m):
            new_b, orig = blookup[m.group(0)]
            stats[orig] = stats.get(orig, 0) + 1
            return new_b
        for p in cms_src.glob("*.framercms"):
            data = p.read_bytes()
            if brx:
                data = brx.sub(brepl, data)
            for frx, frepl, forig in flex_subs:  # per-occurrence padding
                data, k = frx.subn(frepl, data)
                stats[forig] = stats.get(forig, 0) + k
            assert len(data) == p.stat().st_size, f"size drift in {p.name}"
            (mdir / p.name).write_bytes(data)
        log(f"CMS: {len(list(cms_src.glob('*.framercms')))} binaries patched")

    icons_src = root / "pristine" / "icons"
    if icons_src.exists() and any(icons_src.iterdir()):
        # ship name.js@1.2.3 as name.1.2.3.js — real extension, real
        # MIME on any static host (chunk import()s were rewritten to
        # match). pristine keeps the original names.
        idir = site / "assets" / "icons"
        idir.mkdir(parents=True)
        for f in icons_src.iterdir():
            if not f.is_file():
                continue
            m = re.fullmatch(r"(.+)\.js@([0-9.]+)", f.name)
            shutil.copy(f, idir / (f"{m.group(1)}.{m.group(2)}.js"
                                   if m else f.name))

    # localized remote assets -> site/assets/r/ (css gets its internal
    # refs rewritten: absolute CDN urls AND relative url(...) paths)
    lmap = cfg.get("localized", {})
    if lmap:
        rdir = root / "pristine" / "remote-assets"
        rev = {fn: u for u, fn in lmap.items()}
        adir = site / "assets" / "r"
        adir.mkdir(parents=True, exist_ok=True)
        n_loc = 0
        for u, fn in lmap.items():
            src = rdir / fn
            if not src.exists():
                continue
            if fn.endswith(".css"):
                css = _localize_refs(src.read_text(encoding="utf-8",
                                     errors="ignore"), lmap)
                base_dir = rev[fn].rsplit("/", 1)[0]
                for rel in set(re.findall(
                        r"url\(\s*['\"]?(?!https?:|data:|//|#|/assets/)"
                        r"([^'\")\s]+)", css)):
                    absu = urllib.parse.urljoin(base_dir + "/", rel) \
                        .split("?")[0].split("#")[0]
                    if absu in lmap:
                        css = css.replace(rel, "/assets/r/" + lmap[absu])
                (adir / fn).write_text(css, encoding="utf-8")
            elif fn.lower().endswith((".js", ".mjs", ".json")):
                # A LOCALIZED MODULE STILL IMPORTS ITS OWN IMPORTS.
                #
                # localize follows the module graph and downloads every
                # layer, so the files were all here — but only .css was
                # rewritten on the way into the build. A downloaded .js
                # was copied byte-for-byte, absolute platform URLs and
                # all, so the deepest layer of the graph still pointed at
                # the CDN after a "successful" localize.
                #
                # The lazy form is why this survived so long:
                #     C=[()=>import("https://framerusercontent.com/…js")]
                # It is fetched only when that component mounts, so the
                # page renders, the markup scans are clean, and the
                # runtime probe records zero platform requests. Measured
                # on mondragon: 2 modules, 58KB, already downloaded and
                # mapped, still served from Framer.
                (adir / fn).write_text(
                    _localize_refs(src.read_text(encoding="utf-8",
                                                 errors="ignore"), lmap),
                    encoding="utf-8")
            else:
                shutil.copy(src, adir / fn)
            n_loc += 1
        log(f"localized assets: {n_loc} file(s) served from /assets/r/ "
            "(no CDN dependency)")

    # user replacement assets (logo.svg, photos …): <project>/assets/ is
    # mirrored into the served tree, so image entries in copy_map.json can
    # point at "{public_base}/<file>" and survive every rebuild.
    # (chunks/, cms/, icons/ are reserved names inside it.)
    user_assets = root / "assets"
    if user_assets.exists():
        n = 0
        for f in user_assets.rglob("*"):
            if f.is_file():
                dest = site / "assets" / f.relative_to(user_assets)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(f, dest)
                n += 1
        log(f"user assets: {n} file(s) from assets/")

    # short alias copies for CMS-slot-constrained local images (see the
    # cms_over pass above) — physical files, so every layer's alias url
    # resolves. Source is the user's own asset under /assets/…
    for alias, src_url in alias_files:
        rel = src_url.split("?")[0].split("#")[0]
        rel = rel[len(pub):].lstrip("/") if rel.startswith(pub) \
            else rel.lstrip("/")
        srcf = root / "assets" / rel
        if rel.startswith("r/"):          # localized asset store
            srcf = root / "pristine" / "remote-assets" / rel[2:]
        dest = site / alias.lstrip("/")
        if srcf.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(srcf, dest)
        else:
            log(f"WARNING: alias source missing for {src_url} — the "
                "aliased slot will 404 (upload the file to assets/)")

    # effectiveness report: how many times each filled entry actually
    # replaced something, across ALL layers. Zero = the edit is a
    # silent no-op — surfaced loudly by the studio and MCP.
    # hydration-revert guard: WORSE than zero is a multi-word fill that
    # landed in the pages while the built chunks still spell the OLD
    # text — React re-renders from the chunks, so the browser quietly
    # shows the old text over a "successful" build. Flag those too.
    at_risk = []
    bdir = site / "assets" / "chunks"
    if cfg["platform"] == "framer" and bdir.exists():
        bc = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                       for p in bdir.glob("*.mjs"))
        for p in pairs:
            if " " not in p["old"] or not p["new"].strip():
                continue
            if stats.get(p["old"], 0) and re.search(_flex_pat(p["old"]), bc):
                at_risk.append(p["old"])
        if at_risk:
            log(f"WARNING: {len(at_risk)} edit(s) landed in the pages but "
                f"the chunks still spell the OLD text — hydration will "
                f"revert them (first: {at_risk[0][:50]!r})")
    # moot fills: zero hits BUT the old text existed in pristine and is
    # GONE from the built output — longer fills already consumed every
    # mention (classic brand mop-up tokens). That's success, not a dead
    # edit; only zero-hit entries whose target never existed (typos) or
    # still survives are genuinely broken.
    tsuf = (".html", ".htm", ".mjs", ".js", ".css", ".svg")
    pristine_txt = "\n".join(
        f.read_text(encoding="utf-8", errors="ignore")
        for f in (root / "pristine").rglob("*")
        if f.is_file() and f.suffix in tsuf)
    built_txt = "\n".join(
        f.read_text(encoding="utf-8", errors="ignore")
        for f in site.rglob("*") if f.is_file() and f.suffix in tsuf)
    pristine_cms = [p.read_bytes()
                    for p in (root / "pristine" / "cms").glob("*.framercms")]
    built_cms = [p.read_bytes()
                 for p in (site / "assets" / "cms").glob("*.framercms")] \
        if (site / "assets" / "cms").exists() else []
    # brand tokens are SPECULATIVE mop-up nets (inventory mints every
    # case variant); one that never occurred is expected, not broken
    cm_all = json.loads((root / "copy_map.json").read_text(encoding="utf-8"))
    token_olds = {e["old"] for sec in ("strings", "images", "links")
                  for e in cm_all.get(sec, [])
                  if "brand-token" in str(e.get("where", ""))}
    moot = []
    for p in pairs:
        if stats.get(p["old"], 0):
            continue
        po, pb = p["old"], p["old"].encode()
        # an image entry whose swap was carried out by its expanded
        # variants (localized/mangled/srcset picks) — the original url
        # matched nothing itself, but the new image IS in the output.
        # That's success, not a dead edit.
        if po in _expanded_from and p.get("new", "") in built_txt:
            moot.append(po)
            continue
        in_pristine = po in pristine_txt or any(pb in b for b in pristine_cms)
        in_built = po in built_txt or any(pb in b for b in built_cms)
        if (in_pristine or po in token_olds) and not in_built:
            moot.append(po)
    report = dict(stats)
    if at_risk:
        report["__at_risk__"] = at_risk
    if moot:
        report["__moot__"] = sorted(set(moot))
        # what could not fit a byte-locked CMS slot, so verify
        # can report it as a limit rather than a mistake
        report["__cms_over__"] = sorted(OVER_SLOT)
    (site / ".forge-report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    zeros = [o for o, n in stats.items() if n == 0 and o not in set(moot)]
    if zeros:
        log(f"WARNING: {len(zeros)} filled entr(ies) replaced NOTHING "
            f"(first: {zeros[0][:50]!r}) — see site/.forge-report.json")

    # ship the runner + a README so the folder is self-explanatory to
    # any human or AI that receives it
    (site / "serve.py").write_text(
        SERVE_PY.replace("__AETHRON_PLATFORM__", cfg.get("platform", "static")),
        encoding="utf-8")
    readme = ("This site was migrated with Aethron.\n\n"
              "RUN IT LOCALLY:  python3 serve.py  ->  "
              "http://127.0.0.1:8000/\n\n"
              "DEPLOY IT: this folder is fully static — see DEPLOY.md "
              "for one-step\ninstructions (Cloudflare Pages, Netlify, "
              "Vercel, GitHub Pages, any\nstatic host or CDN).\n")
    (site / "README.txt").write_text(readme, encoding="utf-8")
    _write_deploy(site, cfg)

    print("Build complete -> site/   (self-hosting: python3 serve.py)"
          "   Next: serve, then verify")


# ─────────────────────────── logo ────────────────────────────────────
# Logos are SVG *images*, not text — brand-token pairs never touch them.
# This renders the new brand name as vector paths in the template's own
# font (the recipe proven manually on Servly), so the wordmark matches
# the typography exactly. Only command with a dependency: fontTools
# (+ brotli for .woff2). Everything else in forge stays zero-dependency.

FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}")


def _parse_unicode_ranges(block):
    """None = covers everything; else [(lo, hi), ...] codepoint ranges."""
    m = re.search(r"unicode-range:\s*([^;}]+)", block)
    if not m:
        return None
    ranges = []
    for part in m.group(1).split(","):
        r = re.fullmatch(r"\s*[Uu]\+([0-9A-Fa-f?]+)(?:-([0-9A-Fa-f]+))?\s*", part)
        if not r:
            continue
        a, b = r.group(1), r.group(2)
        lo = int(a.replace("?", "0"), 16)
        hi = int(b, 16) if b else int(a.replace("?", "F"), 16)
        ranges.append((lo, hi))
    return ranges


def _discover_fonts(root: Path, cfg: dict):
    """@font-face blocks from pages, css and chunks -> [{url,label,ranges}].
    Framer splits each family/weight into several files by unicode-range;
    the caller filters to the file that covers the wordmark's characters."""
    texts = [(root / "pristine" / p).read_text(encoding="utf-8", errors="ignore")
             for p in cfg["pages"]]
    texts += [f.read_text(encoding="utf-8", errors="ignore")
              for pat in ("*.css", "chunks/*.mjs")
              for f in (root / "pristine").rglob(pat)]
    faces, seen = [], set()
    for t in texts:
        for block in FONT_FACE_RE.findall(t):
            block = html_mod.unescape(block)  # saved sources encode quotes
            u = re.search(r'url\(["\']?(https?://[^"\')\s]+)', block)
            if not u or u.group(1) in seen:
                continue
            seen.add(u.group(1))
            parts = [re.search(r'font-family:\s*["\']?([^;"\'}]+)', block),
                     re.search(r"font-weight:\s*([^;}\s]+)", block),
                     re.search(r"font-style:\s*([^;}\s]+)", block)]
            faces.append({
                "url": u.group(1),
                "label": " ".join(p.group(1).strip() for p in parts if p),
                "ranges": _parse_unicode_ranges(block),
            })
    return faces


def cmd_logo(args):
    usage = ('usage: forge.py logo "Brand Name" [--font <substring|path|url>] '
             '[--out file.svg] [--color #111111] [--tracking 0.02]')
    if not args or args[0].startswith("--"):
        die(usage)
    text = args[0]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default

    color = opt("--color", "#111111")
    tracking = float(opt("--tracking", "0"))
    font_sel = opt("--font")

    try:
        from fontTools.ttLib import TTFont
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen
    except ImportError:
        die("the logo command needs fontTools: pip3 install fonttools brotli")

    root = Path.cwd()
    in_project = (root / "forge.json").exists()

    # resolve the font: explicit path > explicit URL > discovered in template
    if font_sel and Path(font_sel).expanduser().is_file():
        font_file = Path(font_sel).expanduser()
    else:
        if font_sel and font_sel.startswith("http"):
            url, label = font_sel, "(--font url)"
        else:
            if not in_project:
                die("not inside a forge project — pass --font <path-or-url>\n" + usage)
            faces = _discover_fonts(root, read_cfg(root))
            needed = {ord(c) for c in text if not c.isspace()}
            faces = [f for f in faces if f["ranges"] is None or all(
                any(lo <= cp <= hi for lo, hi in f["ranges"]) for cp in needed)]
            if font_sel:
                faces = [f for f in faces if font_sel.lower() in f["label"].lower()
                         or font_sel in f["url"]]
            picks = {}  # one file per family/weight/style
            for f in faces:
                picks.setdefault(f["label"], f)
            if not picks:
                die("no template font matches — pass --font <path-or-url> "
                    "(or a different substring; try without --font to list all)")
            if len(picks) > 1:
                print("Template fonts covering this text — rerun with "
                      "--font <distinguishing substring>:")
                for lbl in sorted(picks):
                    print(f"  {lbl}")
                sys.exit(1)
            (face,) = picks.values()
            url, label = face["url"], face["label"]
        fdir = (root / "pristine" / "fonts") if in_project else Path.cwd()
        fdir.mkdir(parents=True, exist_ok=True)
        font_file = fdir / url.rsplit("/", 1)[1]
        if not font_file.exists() and not download(url, font_file):
            die(f"could not download {url}")
        log(f"font: {label} — {font_file.name}")

    try:
        font = TTFont(str(font_file), fontNumber=0)
        glyph_set = font.getGlyphSet()
    except Exception as e:
        die(f"could not open {font_file.name}: {e} "
            "(.woff2 needs: pip3 install brotli)")
    upm = font["head"].unitsPerEm
    cmap = font.getBestCmap()

    missing = sorted({c for c in text if ord(c) not in cmap and c != " "})
    if missing:
        die(f"font has no glyphs for {missing} — exports ship SUBSET fonts; "
            "pass --font with a complete font file for this family "
            "(foundry / Google Fonts download)")

    kern = {}
    if "kern" in font:  # GPOS-only fonts simply get no kerning — acceptable
        for sub in getattr(font["kern"], "kernTables", []):
            kern.update(getattr(sub, "kernTable", None) or {})

    x, prev, d_parts, track = 0.0, None, [], tracking * upm
    for ch in text:
        gname = cmap.get(ord(ch))
        if gname is None:          # space missing from subset: synthesize
            x, prev = x + upm / 3, None
            continue
        if prev is not None:
            x += kern.get((prev, gname), 0)
        pen = SVGPathPen(glyph_set)
        glyph_set[gname].draw(TransformPen(pen, (1, 0, 0, 1, x, 0)))
        if pen.getCommands():
            d_parts.append(pen.getCommands())
        x += glyph_set[gname].width + track
        prev = gname
    total = max(x - track, 1)

    # font coords are y-up; flip into SVG space. viewBox spans the font's
    # full ascent..descent so different wordmarks in one family align.
    asc, desc = font["hhea"].ascent, font["hhea"].descent  # desc <= 0
    height = asc - desc
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'viewBox="0 {-asc} {total:.0f} {height}" '
           f'width="{32 * total / height:.0f}" height="32" '
           f'role="img" aria-label="{html_mod.escape(text)}">'
           f'<g transform="scale(1,-1)"><path fill="{color}" '
           f'd="{" ".join(d_parts)}"/></g></svg>')

    default_dir = (root / "assets") if in_project else root
    out = Path(opt("--out") or default_dir /
               (re.sub(r"[^\w]+", "-", text.lower()).strip("-") + "-logo.svg"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(f"Wrote {out} ({len(svg)} bytes, {len(d_parts)} glyphs)")
    if in_project:
        pub = read_cfg(root).get("public_base", "/assets").rstrip("/")
        print(f'Point the logo entry in copy_map.json "images" at: '
              f'"{pub}/{out.name}" — build copies assets/ into the site.')


# Every built site ships with its own runner. Builds are static-host
# safe (client-side CMS range slicing, real .js icon names) — this
# runner adds the exact protocols + SPA fallback for local dev, and
# keeps OLDER builds (pre static-hardening) working too.
SERVE_PY = '''#!/usr/bin/env python3
"""Run this site:  python3 serve.py [port]   (default 8000)

The build is fully static — see DEPLOY.md for one-step hosting
(Cloudflare Pages, Netlify, Vercel, GitHub Pages, any CDN). This
runner is the nicest local dev server: it also implements the exact
Framer protocols (CMS ?range= byte slices, .js MIME, SPA route
fallback), which older Aethron builds require.
"""
import re, sys, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLATFORM = "__AETHRON_PLATFORM__"  # set by build (framer|webflow|static)


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def guess_type(self, path):
        if ".js@" in path or path.endswith((".js", ".mjs")):
            return "text/javascript"
        return super().guess_type(path)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith(".framercms") and "range" in q:
            f = Path(self.translate_path(u.path))
            if not f.is_file():
                return self.send_error(404)
            data = f.read_bytes()
            pieces = []
            for part in q["range"][0].split(","):
                m = re.fullmatch(r"(\\d+)-(\\d+)?", part.strip())
                if not m:
                    return self.send_error(400)
                s = int(m.group(1))
                e = int(m.group(2)) + 1 if m.group(2) else len(data)
                pieces.append(data[s:e])
            body = b"".join(pieces)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # platform-aware not-found: Framer = SPA (client routes ->
        # index.html); Webflow/static = multi-page (unmatched path is a
        # real 404 -> styled 404.html, never the wrong home page).
        f = Path(self.translate_path(u.path))
        if not f.exists():
            extensionless = "." not in Path(u.path).name
            if PLATFORM == "framer" and extensionless \\
                    and (ROOT / "index.html").exists():
                self.path = "/index.html"
            else:
                fb = ROOT / "404.html"
                if fb.exists():
                    body = fb.read_bytes()
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
        return super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"serving at http://127.0.0.1:{port}/  (Ctrl-C stops)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


# ─────────────────────────── localize ────────────────────────────────
# Full ownership: download every remote CDN asset (Webflow css/js/
# images, Framer images, Google fonts) into the project under a
# brand-free hashed filename, and let build serve them locally. After
# this, the shipped site has ZERO dependency on the template platform's
# CDN — and no brand traces in filenames either.

# parens/percent allowed: Webflow filenames like "fav-icon (1).png";
# trailing punctuation is stripped after matching
# The platform CDNs, AND the third-party CDNs templates load their motion
# from. Restricting this to website-files/framerusercontent left a Webflow
# port fetching GSAP, Lenis, jQuery and a CMS filter from unpkg, jsdelivr,
# ajax.googleapis and cloudfront at runtime: the site was "owned" right up
# until the visitor was offline, and then it did not move. Framer never
# exposed this because Framer bundles its own runtime.
# How many sub-pages a URL scrape will fetch. Raisable by env for large
# sites; the crawl is breadth-first over the home page's own links, so
# this bounds a runaway, it is not a quality judgement.
MAX_SCRAPE_PAGES = int(os.environ.get("AETHRON_MAX_PAGES", "40"))

# Olds whose replacement could not fit a byte-locked CMS slot.
# Written into the build report so verify can tell 'cannot' from
# 'forgot' — the difference between a NOTE and a FAIL.
OVER_SLOT = set()

REMOTE_ASSET_RE = re.compile(
    r"https://(?:[a-z0-9.-]*website-files\.com|framerusercontent\.com"
    r"|fonts\.gstatic\.com|fonts\.googleapis\.com"
    r"|unpkg\.com|cdn\.jsdelivr\.net|ajax\.googleapis\.com"
    r"|cdnjs\.cloudflare\.com|[a-z0-9]+\.cloudfront\.net)"
    r"/[^\"'\s<>`\\]+")


# ── universal capture / reference audit (any platform) ───────────────
# Framer and Webflow have bespoke fetchers. Everything else (Next.js,
# Astro, Nuxt, Vite, plain HTML) used to get only its HTML saved, which
# renders as an unstyled skeleton. These helpers download whatever the
# markup actually asks for, and prove afterwards that nothing is missing.

ASSET_EXTS = {
    "css", "js", "mjs", "cjs", "json", "map",
    "png", "jpg", "jpeg", "webp", "avif", "gif", "svg", "ico", "bmp",
    "woff", "woff2", "ttf", "otf", "eot",
    "mp4", "webm", "mov", "ogg", "mp3", "wav", "pdf", "txt", "xml",
}
# runtime dirs that mean "a framework lives here" — refs under these are
# assets even when the path carries no file extension (/_next/image?…)
FRAMEWORK_DIRS = ("/_next/", "/_nuxt/", "/_astro/", "/_app/", "/assets/",
                  "/static/", "/build/", "/dist/")
# never localise these — they are tracking/analytics, stripped anyway
TRACKER_HOSTS = ("googletagmanager.com", "google-analytics.com",
                 "doubleclick.net", "facebook.net", "hotjar.com",
                 "segment.io", "sentry.io", "intercom.io", "clarity.ms")


def _markup_refs(text: str) -> set:
    """Every asset-ish URL a page/stylesheet references."""
    out = set()
    for m in re.finditer(r'(?:src|href|poster|data-src)\s*=\s*"([^"]*)"', text):
        out.add(m.group(1))
    for m in re.finditer(r'srcset\s*=\s*"([^"]*)"', text):
        for part in m.group(1).split(","):
            u = part.strip().split(" ")[0]
            if u:
                out.add(u)
    for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)", text):
        out.add(m.group(1))
    return {r.strip() for r in out if r.strip()}


def _asset_like(ref: str) -> bool:
    """True for things that must resolve to a FILE. Extension-less refs
    are page routes (SPA links) unless they sit under a framework dir —
    otherwise every nav link would look like a missing asset."""
    path = ref.split("#")[0].split("?")[0]
    tail = path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    return ext in ASSET_EXTS or any(k in ref for k in FRAMEWORK_DIRS)


def _ref_audit(site: Path) -> dict:
    """{missing ref -> set(files that reference it)} across the built
    site. Local refs only; remote URLs are the caller's choice to keep."""
    missing = {}
    for f in site.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in (".html", ".css"):
            continue
        text = html_mod.unescape(
            f.read_text(encoding="utf-8", errors="ignore"))
        for ref in _markup_refs(text):
            if ref.startswith(("http://", "https://", "//", "data:",
                               "mailto:", "tel:", "javascript:", "#")):
                continue
            if not _asset_like(ref):
                continue
            path = ref.split("#")[0].split("?")[0]
            if not path:
                continue
            target = (site / path.lstrip("/")) if path.startswith("/") \
                else (f.parent / path)
            try:
                ok = target.exists()
            except OSError:
                ok = False
            if not ok:
                missing.setdefault(ref, set()).add(str(f.relative_to(site)))
    return missing


def _local_name(url: str) -> str:
    import hashlib
    base = url.split("?")[0].split("#")[0]
    ext = ""
    tail = base.rsplit("/", 1)[-1]
    if "." in tail:
        ext = "." + tail.rsplit(".", 1)[-1][:8]
    return hashlib.sha1(base.encode()).hexdigest()[:12] + ext


def cmd_capture(_args):
    """Download EVERY asset the pages reference — any platform.

    Framer/Webflow have dedicated fetchers; this is the universal one.
    It walks the markup (src/href/srcset/url()), resolves each ref
    against the original site, follows stylesheets one level deeper,
    and records the mapping in cfg['localized'] so build rewrites the
    refs to local copies — the same proven path `localize` uses.

    Framework-aware: Next.js serves images through /_next/image?url=…
    (an API, not a file), so the real source is decoded from the query
    and downloaded, while the ORIGINAL ref stays the rewrite key."""
    root = Path.cwd()
    cfg = read_cfg(root)
    src = cfg.get("source_url") or ""
    origin = ""
    if src:
        p = urllib.parse.urlparse(src)
        origin = f"{p.scheme}://{p.netloc}"
    if not origin:
        die("no source_url in forge.json — capture needs the original "
            "site to download from (re-run init with the live URL)")
    rdir = root / "pristine" / "remote-assets"
    rdir.mkdir(parents=True, exist_ok=True)
    lmap = cfg.get("localized", {})

    def resolve(ref):
        """(rewrite_key, download_url) or None."""
        key = html_mod.unescape(ref).strip()
        if not key or key.startswith(("data:", "mailto:", "tel:",
                                      "javascript:", "#")):
            return None
        if key.startswith("//"):
            dl = "https:" + key
        elif key.startswith(("http://", "https://")):
            dl = key
        else:
            dl = urllib.parse.urljoin(origin + "/", key.lstrip("/")
                                      if key.startswith("/") else key)
        if any(h in dl for h in TRACKER_HOSTS):
            return None
        pr = urllib.parse.urlparse(dl)
        if pr.path.rstrip("/").endswith("/_next/image"):
            q = urllib.parse.parse_qs(pr.query).get("url", [""])[0]
            if q:
                dl = q if q.startswith("http") else urllib.parse.urljoin(
                    origin + "/", q.lstrip("/"))
        return key, dl

    def harvest(text):
        found = {}
        for ref in _markup_refs(text):
            if not _asset_like(ref):
                continue
            r = resolve(ref)
            if r:
                found[r[0]] = r[1]
        return found

    wanted = {}
    for pg in cfg.get("pages", []):
        f = root / "pristine" / pg
        if f.exists():
            wanted.update(harvest(f.read_text(encoding="utf-8",
                                              errors="ignore")))
    todo = {k: v for k, v in wanted.items() if k not in lmap}
    jobs = [(v, rdir / _local_name(v)) for v in dict.fromkeys(todo.values())
            if not (rdir / _local_name(v)).exists()]
    if jobs:
        download_many(jobs, f"assets (round 1, {len(jobs)} urls)")
    for k, v in todo.items():
        if (rdir / _local_name(v)).exists():
            lmap[k] = _local_name(v)

    # round 2 — stylesheets pull in fonts and background images
    extra = {}
    for k, fn in list(lmap.items()):
        if not fn.endswith(".css"):
            continue
        css = (rdir / fn).read_text(encoding="utf-8", errors="ignore")
        base = wanted.get(k, k).rsplit("/", 1)[0] + "/"
        for ref in _markup_refs(css):
            if ref.startswith(("data:", "#")):
                continue
            dl = ref if ref.startswith("http") else urllib.parse.urljoin(
                base, ref)
            if any(h in dl for h in TRACKER_HOSTS):
                continue
            if ref not in lmap:
                extra[ref] = dl.split("#")[0]
    jobs = [(v, rdir / _local_name(v)) for v in dict.fromkeys(extra.values())
            if not (rdir / _local_name(v)).exists()]
    if jobs:
        download_many(jobs, f"assets (round 2 via css, {len(jobs)} urls)")
    for k, v in extra.items():
        if (rdir / _local_name(v)).exists():
            lmap[k] = _local_name(v)

    cfg["localized"] = lmap
    write_cfg(root, cfg)
    got = len(lmap)
    misses = [k for k in wanted if k not in lmap]
    print(f"captured {got} asset(s) -> pristine/remote-assets/")

    # STALE-SCRAPE DETECTION. Modern hosts (Next.js/Vercel, Astro, Vite)
    # serve content-hashed, immutable assets: after a redeploy the old
    # hashes 404 forever. So an HTML copy captured days later can be
    # unfixable in place — and silently shipping it is how a user gets a
    # blank page. Say so plainly instead.
    critical = [m for m in misses
                if m.split("?")[0].endswith((".css", ".js", ".mjs"))]
    if misses and len(misses) >= max(3, len(wanted) // 10) and critical:
        print(f"\nSTALE SOURCE: {len(misses)} ref(s) are gone from the "
              f"origin, including {len(critical)} stylesheet/script(s).")
        print("  The saved pages were scraped from an older deployment; "
              "that build's hashed assets no longer exist anywhere.")
        print("  FIX: re-scrape the site now so pages and assets come "
              "from the SAME deployment:")
        print(f"       forge.py init {src} --name <name>   (then inventory"
              f" — existing fills are preserved)")
    elif misses:
        print(f"NOTE {len(misses)} ref(s) unavailable (404/blocked): "
              f"{', '.join(m[:60] for m in misses[:3])}")
    print("Run build — refs are rewritten to local copies, then verify.")


def cmd_localize(_args):
    root = Path.cwd()
    cfg = read_cfg(root)
    rdir = root / "pristine" / "remote-assets"
    rdir.mkdir(parents=True, exist_ok=True)
    lmap = cfg.get("localized", {})

    def harvest(text):
        out = set()
        for u in REMOTE_ASSET_RE.findall(html_mod.unescape(text)):
            base = u.split("?")[0].split("#")[0].rstrip(",);.")
            # chunks/CMS/icons are already localized by fetch
            if base.endswith((".mjs", ".framercms")):
                continue
            # …but /sites/ is not only runtime files. Framer serves the
            # default favicons from /sites/icons/, and skipping the whole
            # prefix meant nobody fetched them: localize passed them over
            # as "fetch's job" and fetch only knows chunks, CMS and icon
            # modules. gravitest shipped a 404 for
            # /sites/icons/default-favicon-dark.v1.png on every page.
            # A plain image or font under /sites/ is an asset like any
            # other, so let those through and keep skipping the code.
            if "/sites/" in base and not base.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif",
                     ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf")):
                continue
            out.add(base)
        return out

    urls = set()
    for pg in cfg["pages"]:
        urls |= harvest((root / "pristine" / pg).read_text(
            encoding="utf-8", errors="ignore"))
    chunks = root / "pristine" / "chunks"
    if chunks.exists():
        for c in chunks.glob("*.mjs"):
            urls |= harvest(c.read_text(encoding="utf-8", errors="ignore"))

    # THE CMS IS WHERE THE IMAGES ACTUALLY LIVE.
    #
    # Every CMS-driven image — blog covers, project cards, team photos —
    # has its url inside a .framercms binary, and the runtime fetches
    # straight from that data. Nothing harvested them, so a migration
    # could pass every ownership check while its entire blog streamed
    # from the platform CDN: mondragon 526 urls, sadewa 426,
    # createstudio 406, on builds reported CLEAN.
    cms = root / "pristine" / "cms"
    if cms.exists():
        for c in cms.glob("*.framercms"):
            urls |= harvest(c.read_text(encoding="utf-8", errors="ignore"))

    # THE OWNER'S OWN CHOICES ARE ASSETS TOO.
    #
    # A fill in copy_map can point anywhere, and an image picked in the
    # editor from the template's own CDN lands here as an absolute
    # platform URL. Nothing harvested those, so the one asset the owner
    # actually chose was the one asset that stayed rented: the build
    # shipped it, the ownership check failed, and the only fix available
    # was to DELETE the choice — which is what an agent did, passing the
    # check by throwing the owner's image away.
    #
    # Harvesting fills makes the constructive fix exist: the picked image
    # is downloaded like any other asset, build rewrites the reference,
    # and the owner keeps what they chose AND owns it.
    cmap = root / "copy_map.json"
    if cmap.is_file():
        try:
            _cm = json.loads(cmap.read_text(encoding="utf-8"))
        except Exception:
            _cm = {}
        for _sec, _entries in (_cm.items() if isinstance(_cm, dict) else []):
            if not isinstance(_entries, list):
                continue
            for _e in _entries:
                if isinstance(_e, dict) and _e.get("new"):
                    urls |= harvest(str(_e["new"]))

    new_urls = [u for u in sorted(urls) if u not in lmap]
    jobs = [(u, rdir / _local_name(u)) for u in new_urls
            if not (rdir / _local_name(u)).exists()]
    if jobs:
        download_many(jobs, f"assets (round 1, {len(jobs)} urls)")
    for u in new_urls:
        if (rdir / _local_name(u)).exists():
            lmap[u] = _local_name(u)

    # round 2: css files reference more assets (fonts, images) —
    # absolute CDN urls AND urls relative to the css file's location
    extra = set()
    for u, fn in list(lmap.items()):
        if not fn.endswith(".css"):
            continue
        css = (rdir / fn).read_text(encoding="utf-8", errors="ignore")
        extra |= harvest(css)
        # ABSOLUTE url() NEEDS ITS OWN READING INSIDE CSS.
        #
        # REMOTE_ASSET_RE deliberately allows parentheses, because Webflow
        # ships filenames like "fav-icon (1).png" and stopping at "(" cut
        # those in half. In MINIFIED css that generosity backfires: url()
        # has no other delimiter, so the match runs past the closing paren
        # into the next declaration and harvests
        #     .../background-image.svg);backgr
        # which 404s. rstrip(",);.") cannot repair it — the junk is in the
        # middle, not the tail — so localize honestly reported the asset
        # "unavailable" and the platform URL stayed in the shipped css.
        # Measured on test-2: 8 of 8 round-2 downloads failed this way,
        # every one of them reachable when asked for properly.
        #
        # Inside url(...) the paren IS the delimiter, so read it as such
        # here and leave the permissive pattern for markup attributes.
        for absu in re.findall(r"url\(\s*['\"]?(https?://[^'\")\s]+)", css):
            extra.add(absu.split("#")[0])
        base_dir = u.rsplit("/", 1)[0]
        for rel in re.findall(r"url\(\s*['\"]?(?!https?:|data:|//|#)"
                              r"([^'\")\s]+)", css):
            extra.add(urllib.parse.urljoin(base_dir + "/", rel)
                      .split("?")[0].split("#")[0])
    extra = {u for u in extra if u.startswith("http") and u not in lmap}
    jobs = [(u, rdir / _local_name(u)) for u in sorted(extra)
            if not (rdir / _local_name(u)).exists()]
    if jobs:
        download_many(jobs, f"assets (round 2 via css, {len(jobs)} urls)")
    for u in extra:
        if (rdir / _local_name(u)).exists():
            lmap[u] = _local_name(u)

    # round 3+: FOLLOW THE MODULE GRAPH.
    #
    # A downloaded JS module imports more modules, which import more
    # again. Rounds 1 and 2 scanned pages and CSS, so the first layer of
    # /modules/ came down and everything it referenced stayed on the
    # platform CDN: 217 absolute URLs survived on createstudio, in files
    # the entry-page check never opened. Keep harvesting what was just
    # downloaded until a pass finds nothing new.
    for depth in range(8):
        more = set()
        for u, fn in list(lmap.items()):
            if not fn.lower().endswith((".js", ".mjs")):
                continue
            f = rdir / fn
            if not f.is_file():
                continue
            more |= harvest(f.read_text(encoding="utf-8", errors="ignore"))
        more = {u for u in more if u.startswith("http") and u not in lmap}
        if not more:
            break
        jobs = [(u, rdir / _local_name(u)) for u in sorted(more)
                if not (rdir / _local_name(u)).exists()]
        if jobs:
            download_many(jobs, f"assets (round {depth + 3} via js, "
                                f"{len(jobs)} urls)")
        added = 0
        for u in more:
            if (rdir / _local_name(u)).exists():
                lmap[u] = _local_name(u)
                added += 1
        if not added:
            break

    cfg["localized"] = lmap
    write_cfg(root, cfg)
    print(f"localized map: {len(lmap)} asset(s) -> pristine/remote-assets/")
    print("Run build — refs are rewritten and the site stops depending "
          "on the template platform's CDN entirely.")


def _localize_refs(text: str, lmap: dict) -> str:
    """Rewrite remote asset urls (plain or entity-encoded) to the local
    /assets/r/ copies. Longest urls first so query-variants don't clash."""
    if not lmap:
        return text
    for u in sorted(lmap, key=len, reverse=True):
        local = "/assets/r/" + lmap[u]
        text = text.replace(u, local)
        eu = u.replace("&", "&amp;")
        if eu != u:
            text = text.replace(eu, local)
    return text


# ──────────────────── per-slot image overrides ───────────────────────
# When ONE asset url fills MANY slots (dummy logo tickers), value
# replacement changes them all together. The style layer can split
# them: compute each slot's nth-child CSS path from the SSR HTML
# (which matches the hydrated DOM) and override per-element with
# content:url(...) — !important CSS beats whatever React re-renders.

def _img_slot_selectors(html: str, url: str):
    """[(css_selector, kind)] for every <img> (kind='img') or inline
    background-image element (kind='bg') referencing url.

    Selectors must survive hydration: Framer's runtime REMOVES pruned
    breakpoint-variant siblings, shifting nth-child positions computed
    from the SSR HTML. So we anchor on the nearest ancestor whose class
    set is unique among its siblings (framer-* classes are stable across
    hydration) and use nth-child only BELOW the anchor, where no pruning
    happens. Variant twins share the anchor class — they intentionally
    match together, giving the same slot the same override per variant."""
    from html.parser import HTMLParser
    base = url.split("?")[0].split("#")[0]
    ok_cls = re.compile(r"^[A-Za-z0-9_-]+$")

    class Node:
        __slots__ = ("tag", "id", "classes", "nth", "parent", "children")

        def __init__(self, tag, eid, classes, nth, parent):
            self.tag, self.id, self.classes = tag, eid, classes
            self.nth, self.parent, self.children = nth, parent, []

    doc_counts = {}

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.root = Node("root", None, [], 1, None)
            self.cur = self.root
            self.hits = []

        def _open(self, tag, attrs):
            ad = dict(attrs)
            classes = [c for c in (ad.get("class") or "").split()
                       if ok_cls.match(c)]
            if classes:
                k = (tag, tuple(classes))
                doc_counts[k] = doc_counts.get(k, 0) + 1
            n = Node(tag, ad.get("id"), classes,
                     len(self.cur.children) + 1, self.cur)
            self.cur.children.append(n)
            hay = (ad.get("src") or "") + " " + (ad.get("srcset") or "")
            style = ad.get("style") or ""
            if tag == "img" and base in hay:
                m = re.search(r"[?&]width=(\d+)", hay) or \
                    re.search(r"(\d+)w\b", ad.get("srcset") or "")
                w = int(m.group(1)) if m else 0
                self.hits.append((n, "img", w))
            elif base in style and "background" in style:
                self.hits.append((n, "bg", 9999))
            return n

        def handle_starttag(self, tag, attrs):
            n = self._open(tag, attrs)
            if tag not in VOID_TAGS:
                self.cur = n

        def handle_startendtag(self, tag, attrs):
            self._open(tag, attrs)

        def handle_endtag(self, tag):
            c = self.cur
            while c.parent is not None:
                if c.tag == tag:
                    self.cur = c.parent
                    return
                c = c.parent

    def sel_for(node):
        """Chain upward. A classed hop needs sibling-uniqueness (else
        nth-child); the chain only STOPS at an ancestor whose tag+class
        set is (near-)unique DOCUMENT-WIDE — sibling-unique classes
        repeat across cousin cards and would over-match (learned the
        hard way: logos painted onto section backgrounds). Count <= 3
        allows breakpoint-variant twins, which SHOULD share the rule."""
        segs = []
        cur = node
        while cur.parent is not None:
            if cur.id:
                segs.append("#" + cur.id)
                break
            if cur.tag == "body":
                segs.append("body")
                break
            if cur.classes:
                same = [s for s in cur.parent.children
                        if s.tag == cur.tag and s.classes == cur.classes]
                if len(same) == 1:
                    segs.append(cur.tag + "".join("." + c
                                                  for c in cur.classes[:3]))
                    if doc_counts.get((cur.tag, tuple(cur.classes)), 99) <= 3:
                        break           # document-unique-ish — anchor here
                    cur = cur.parent
                    continue
            segs.append(f"{cur.tag}:nth-child({cur.nth})")
            cur = cur.parent
        return " > ".join(reversed(segs))

    p = P()
    p.feed(html)
    return [(sel_for(n), kind, w) for n, kind, w in p.hits]


def image_slot_styles(root: Path, old_url: str, new_urls: list):
    """Round-robin new_urls over every slot old_url fills; returns the
    style entries to append to copy_map['styles']."""
    cfg = read_cfg(root)
    entries, n = [], 0
    for page in cfg["pages"]:
        src = root / "site" / page
        if not src.exists():
            src = root / "pristine" / page
        slots = _img_slot_selectors(
            src.read_text(encoding="utf-8", errors="ignore"), old_url)
        for sel, kind, w in slots:
            if w > 700:      # background-sized usage of the same asset:
                continue     # replacing it paints a logo over a section
            new = new_urls[n % len(new_urls)]
            css = ({"content": f'url("{new}")', "object-fit": "contain"}
                   if kind == "img" else
                   {"background-image": f'url("{new}")'})
            entries.append({"selector": sel, "css": css,
                            "label": f"slot {n + 1}: {new.rsplit('/', 1)[-1]}"})
            n += 1
    return entries


# ─────────────────────────── backend ─────────────────────────────────
# "Hand it to any AI IDE without it breaking anything": a pre-built,
# zero-dependency backend where copy_map.json IS the content database.
# Writes flow through the SAME guardrails as the studio, then a rebuild
# regenerates site/ — dynamic content that cannot break hydration.

BACKEND_PY = r'''#!/usr/bin/env python3
"""Forge backend — content API + site server for this migrated project.

Run:  python3 backend/app.py [port]        (default 8090)

Safe by construction: content writes go through the same guarded
pipeline as the visual editor (CMS byte budgets, forbidden characters),
then `forge.py build` regenerates site/ — so dynamic content can never
break the template runtime. READ AGENT_GUIDE.md BEFORE EXTENDING.

API:
  GET  /api/content            the full copy map (strings/images/links)
  POST /api/content            {"section","old","new"} -> guarded write
                               + automatic rebuild
  POST /api/media              {"filename","data_b64"} -> saved to
                               assets/, served at /assets/<name>
  GET  /api/status             rebuild state
  GET  /<anything else>        the built site (with the Framer
                               protocols: ?range=, .js@ MIME, SPA)
"""
import base64, json, mimetypes, re, subprocess, sys, threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # the project folder
SITE = ROOT / "site"
FORGE = next((p for p in (ROOT / "forge.py",
                          ROOT.parent.parent / "forge.py")
              if p.exists()), None)
LOCK = threading.Lock()
STATE = {"running": False, "ok": None, "log": ""}


def rebuild():
    def run():
        with LOCK:
            STATE.update(running=True)
            r = subprocess.run([sys.executable, str(FORGE), "build"],
                               cwd=ROOT, capture_output=True, text=True)
            STATE.update(running=False, ok=r.returncode == 0,
                         log=(r.stdout + r.stderr)[-2000:])
    threading.Thread(target=run, daemon=True).start()


class App(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def j(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/content":
            return self.j(json.loads(
                (ROOT / "copy_map.json").read_text(encoding="utf-8")))
        if u.path == "/api/status":
            return self.j(STATE)
        f = (SITE / u.path.lstrip("/")).resolve()
        if f.is_dir():
            f = f / "index.html"
        if not f.is_relative_to(SITE):
            return self.send_error(404)
        if not f.is_file():
            if "." not in Path(u.path).name:
                f = SITE / "index.html"          # SPA client-side route
            else:
                return self.send_error(404)
        data = f.read_bytes()
        if f.name.endswith(".framercms") and "range" in q:
            pieces = []
            for part in q["range"][0].split(","):
                m = re.fullmatch(r"(\d+)-(\d+)?", part.strip())
                if not m:
                    return self.send_error(400)
                s = int(m.group(1))
                e = int(m.group(2)) + 1 if m.group(2) else len(data)
                pieces.append(data[s:e])
            data, ctype = b"".join(pieces), "application/octet-stream"
        elif ".js@" in f.name or f.name.endswith((".js", ".mjs")):
            ctype = "text/javascript"
        else:
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.j({"error": "bad json"}, 400)
        if u.path == "/api/content":
            cm_f = ROOT / "copy_map.json"
            cm = json.loads(cm_f.read_text(encoding="utf-8"))
            new = body.get("new", "")
            if "`" in new or "${" in new:
                return self.j({"error": "backtick/${ forbidden "
                               "(strings land in JS template literals)"}, 400)
            for e in cm.get(body.get("section", ""), []):
                if e["old"] == body.get("old"):
                    mb = e.get("max_bytes")
                    if mb and len(new.encode()) > mb:
                        return self.j({"error": "over CMS byte budget "
                                       f"({len(new.encode())}>{mb}) — "
                                       "shorten the text"}, 400)
                    e["new"] = new
                    cm_f.write_text(json.dumps(cm, indent=1,
                                    ensure_ascii=False), encoding="utf-8")
                    rebuild()
                    return self.j({"ok": True, "rebuilding": True})
            return self.j({"error": "entry not found"}, 404)
        if u.path == "/api/media":
            name = re.sub(r"[^\w.-]+", "-",
                          Path(body.get("filename", "")).name)
            if not name.strip("-."):
                return self.j({"error": "no filename"}, 400)
            (ROOT / "assets").mkdir(exist_ok=True)
            (ROOT / "assets" / name).write_bytes(
                base64.b64decode(body.get("data_b64", "")))
            rebuild()
            return self.j({"ok": True, "url": "/assets/" + name,
                           "rebuilding": True})
        # ------------------- EXTEND HERE -------------------
        # Add your own endpoints above this line (forms, auth,
        # dashboards, anything). Keep the rules in AGENT_GUIDE.md.
        return self.j({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    if FORGE is None:
        print("WARNING: forge.py not found — content writes won't rebuild")
    print(f"forge backend at http://127.0.0.1:{port}/  (site + content API)")
    ThreadingHTTPServer(("127.0.0.1", port), App).serve_forever()
'''

AGENT_GUIDE = """# AGENT GUIDE — read this before touching anything

This folder is a migrated {PLATFORM} template project produced by
Aethron. It is fully self-contained: you can rebuild, serve,
and extend it. There is exactly ONE rule that keeps everything safe:

    NEVER hand-edit site/ or pristine/.
    site/     is REGENERATED by every build (your edits get wiped).
    pristine/ is the sealed original (the build's source of truth).

## How content works (this is the part other AIs get wrong)
{PLATFORM_NOTES}

pristine/ is CHECKSUM-SEALED: verify fails loudly if you modify it,
and hydration silently reverts your edits anyway (this has happened —
an agent hand-injected image URLs into pristine HTML; the runtime
re-rendered the originals and the "change" was invisible). Also note:
if one image URL fills several slots (repeated dummy logos), replacing
it changes ALL those slots — the design reuses a single asset.

All template content lives in copy_map.json — strings, images, links,
styles, removals. Change an entry's "new" value, run
`python3 forge.py build`, and site/ is regenerated with the change
applied to every layer correctly. The backend already wires this:

    POST /api/content {{"section":"strings","old":"<exact old text>",
                        "new":"<replacement>"}}

writes are validated (byte budgets, forbidden characters) and trigger
the rebuild automatically. That IS the safe "database-driven content"
path — treat copy_map.json as the database.

## Serving
site/ is STATIC-HOST SAFE as built: the CMS ?range= responses are
sliced client-side when a dumb server returns the full file, and icon
modules ship with real .js names — see site/DEPLOY.md for one-step
hosting (Cloudflare Pages/Netlify/Vercel/GitHub Pages/any CDN).
backend/app.py and site/serve.py additionally implement the exact
protocols (range slices, .js MIME, SPA fallback) — use either for
local dev; the only host-side nicety left is routing unknown
extension-less paths to /index.html for deep links (the shipped
_redirects / 404.html / vercel.json cover the big hosts).

## WHEN AN EDIT FAILS — the runbook (follow it, in order)
A fill that "didn't work" is NEVER fixed by editing site/, pristine/
or this platform's code. The system tells you exactly what happened:
1. Read site/.forge-report.json after build. Your entry's count is
   the number of real replacements. 0 = it matched nothing.
   "__at_risk__" = it changed the pages but the chunks still spell
   the old text — the browser will revert it on hydration.
2. Run `python3 forge.py heal` (MCP: the heal tool). Deterministic,
   never a guess. Text: whitespace-flexible matching, source-casing
   adoption, nearest-source adoption. Images: finds every real source
   URL sharing the asset id (all srcset size/format variants) and
   points them at your new image, fixing mangled picks and partial
   swaps. Prints HEALED/STUCK per entry.
3. Rebuild. Re-check the report.
4. Still STUCK? The reason line says why (usually: the text you
   targeted doesn't exist in the source, or your replacement is over
   a CMS byte budget). Fix the ENTRY (copy_map.json via the content
   API / set_content) — adjust "old" to the printed closest candidate
   or shorten "new" — and rebuild. Never work around the pipeline.

## WHEN THE SITE "LOOKS BROKEN" BUT THE CHECKS PASS
`verify` reads files. It cannot see a page that ships every byte and
still renders blank, or an asset the JAVASCRIPT asks for that was never
downloaded (the markup never mentions it, so no file scan looks for it).
Run `python3 forge.py probe` (MCP: the probe tool): it loads each built
page in a headless browser and reports the post-JS text, every request
that failed with its status, and console errors. Failed requests ->
`forge.py capture` then rebuild. If it says SKIPPED, no browser was
found — that means UNVERIFIED, not fine; say so rather than declaring
the site healthy.

## Where YOUR code goes
- backend/app.py -> "EXTEND HERE" section: add endpoints freely
  (forms, auth, dashboards, webhooks). The static/API serving above it
  is load-bearing — extend, don't rewrite.
- New pages/features: build them as separate files/apps served
  alongside; link them from the template via copy_map link entries.
- New images: POST /api/media (or drop files in assets/) -> they are
  served at /assets/<name> and survive every rebuild.

## Commands
    python3 backend/app.py [port]   run site + content API (dev)
    python3 forge.py build          regenerate site/ from copy_map
    python3 forge.py verify         machine checks before shipping
    python3 forge.py probe          RUNTIME check — loads the built
                                    pages in a headless browser and
                                    reports blank/hydration-wiped pages,
                                    requests that failed, console errors.
                                    verify reads files; this runs them.
                                    Says SKIPPED (never PASS) with no
                                    browser installed.
    python3 site/serve.py           serve the static site only

Production note: app.py binds 127.0.0.1 and has NO auth — put it
behind your reverse proxy / auth layer before exposing it.
"""

FRAMER_NOTES = """This is a FRAMER export: a frozen React app. The same
text exists in THREE places (HTML, JS chunks, CMS binaries) and React
compares them character-for-character on load. Editing text in site/
HTML directly = the page flips back or logs hydration errors. CMS-bound
strings additionally have byte-length budgets (offsets are baked into
manifests). This is why ALL content changes must flow through
copy_map.json + build — the build keeps the three layers identical and
byte-locked. Do not remove badge/promo DOM nodes; hide via CSS
(hide_selectors in forge.json) — React re-creates removed DOM."""

WEBFLOW_NOTES = """This is a WEBFLOW/static export: content lives in the
HTML files only — no hydration, no byte budgets. Server-side templating
of the built HTML is safe here IF you must, but the copy_map + build
path still gives you validation, styles, removals and undo for free.
Do not rename/rehost the template's CDN asset urls (filenames embed the
original brand; renamed files don't exist on the CDN)."""


def cmd_backend(_args):
    root = Path.cwd()
    cfg = read_cfg(root)
    bdir = root / "backend"
    bdir.mkdir(exist_ok=True)
    (bdir / "app.py").write_text(BACKEND_PY, encoding="utf-8")
    notes = FRAMER_NOTES if cfg["platform"] == "framer" else WEBFLOW_NOTES
    (bdir / "AGENT_GUIDE.md").write_text(
        AGENT_GUIDE.format(PLATFORM=cfg["platform"].upper(),
                           PLATFORM_NOTES=notes), encoding="utf-8")
    print("backend/ generated: app.py (content API + site server) "
          "+ AGENT_GUIDE.md")
    print("Run: python3 backend/app.py 8090")


# ─────────────────────────── serve ───────────────────────────────────

def _site_handler(site: Path, platform: str, on_request=None, quiet=False):
    """The request handler behind BOTH `serve` and `probe` — one
    implementation of the serving protocols, so the probe measures the
    same server the owner previews with.

    on_request(path, status) turns the server into a network recorder:
    every asset the browser actually asks for, with the status it got.
    That is ground truth about runtime behaviour — no CDP, no deps."""
    from http.server import SimpleHTTPRequestHandler
    import urllib.parse

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(site), **kw)

        def log_message(self, fmt, *a):
            if not quiet:
                super().log_message(fmt, *a)

        def send_response(self, code, message=None):
            if on_request is not None:
                on_request(self.path, int(code))
            super().send_response(code, message)

        def end_headers(self):
            # dev server: never let the browser cache a stale build
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def guess_type(self, path):
            if ".js@" in path or path.endswith((".js", ".mjs")):
                return "text/javascript"
            return super().guess_type(path)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path.endswith(".framercms") and "range" in q:
                f = Path(self.translate_path(u.path))
                if not f.is_file():
                    return self.send_error(404)
                data = f.read_bytes()
                pieces = []
                for part in q["range"][0].split(","):
                    m = re.fullmatch(r"(\d+)-(\d+)?", part.strip())
                    if not m:
                        return self.send_error(400)
                    s = int(m.group(1))
                    e = int(m.group(2)) + 1 if m.group(2) else len(data)
                    pieces.append(data[s:e])
                body = b"".join(pieces)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Not-found routing is PLATFORM-AWARE:
            #  • Framer is a single-page app — extension-less deep links
            #    (about, works, …) are CLIENT ROUTES rendered from
            #    index.html, so fall back there.
            #  • Webflow/static is multi-page — an unmatched path is a
            #    genuine 404 (a dead CMS/collection link), so serve the
            #    styled 404.html with a real 404 status instead of
            #    wrongly showing the home page.
            f = Path(self.translate_path(u.path))
            if not f.exists():
                extensionless = "." not in Path(u.path).name
                if platform == "framer" and extensionless \
                        and (site / "index.html").exists():
                    self.path = "/index.html"
                else:
                    fb = site / "404.html"
                    if fb.exists():
                        body = fb.read_bytes()
                        self.send_response(404)
                        self.send_header("Content-Type",
                                         "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        return self.wfile.write(body)
            return super().do_GET()

    return H


def cmd_serve(args):
    from http.server import ThreadingHTTPServer
    root = Path.cwd()
    platform = read_cfg(root).get("platform", "static")
    port = int(args[0]) if args else 8777
    site = root / "site"
    if not site.exists():
        die("no site/ — run build first")
    H = _site_handler(site, platform)
    print(f"Serving site/ at http://localhost:{port}/  (Ctrl-C to stop)")
    ThreadingHTTPServer(("", port), H).serve_forever()


# ─────────────────────────── probe ───────────────────────────────────
# L1 perception, runtime half. `verify` reads FILES; a browser runs
# CODE — and between the two sits every failure a file scan cannot see:
# a page that ships every asset and still renders blank, a chunk that
# throws on load, a hydration pass that wipes the body, an image whose
# src is built by JS and never appears in the markup.
#
# NO DEPENDENCIES, THREE SIGNALS: we serve site/ ourselves (the very
# handler `serve` uses), point a headless Chromium at each page, and
# read (1) the post-JS DOM via --dump-dom, (2) the console via
# --enable-logging=stderr, (3) OUR OWN request log — every URL the
# browser asked for and the status we answered with. Signal 3 is the
# one no screenshot gives you and no CDP client is needed for.
#
# THE HONESTY RULE: with no browser installed this reports SKIPPED,
# never PASS. A check that could not run must never look like a check
# that passed — that is precisely how a blank Next.js site once shipped
# as "healthy".

BROWSER_ENV = "AETHRON_BROWSER"
BROWSER_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
BROWSER_ON_PATH = ("google-chrome", "google-chrome-stable", "chromium",
                   "chromium-browser", "chrome", "msedge", "brave-browser")

# WHAT COUNTS AS A FAILURE. Only errors that mean something did not
# LOAD or could not EXECUTE — those break the site no matter which
# template you started from. Everything else (React #418/#422 hydration
# recovery, Framer variant assertions, deprecation chatter) is reported
# as a NOTE: the invariant says those are pre-existing export artifacts,
# and a probe that fails a page which visibly renders 5,000 characters
# teaches the agent to distrust it.
CONSOLE_FATAL = (
    "chunkloaderror", "failed to load resource", "syntaxerror",
    "unexpected token", "unexpected end of input", "is not defined",
    "is not a function", "cannot read properties", "net::err_",
    "refused to execute", "refused to apply", "mime type",
    "content security policy", "failed to fetch",
    "error loading dynamically imported module",
    "importing a module script failed", "failed to resolve module",
)
# Pure chatter — not even worth a note.
CONSOLE_IGNORE = (
    "download the react devtools", "[fast refresh]",
    "was preloaded using link preload but not used",
    "autofocus processing was blocked", "third-party cookie",
    "favicon.ico",
)


def _find_browser() -> str:
    """A Chromium-family binary, or "" — never a guess."""
    import os
    env = os.environ.get(BROWSER_ENV, "").strip()
    if env.lower() in ("none", "off", "0"):
        return ""            # explicit opt-out (and how tests force SKIPPED)
    if env:
        if Path(env).exists():
            return env
        print(f"NOTE {BROWSER_ENV}={env} does not exist — looking for a "
              f"browser in the usual places")
    for p in BROWSER_CANDIDATES:
        if Path(p).exists():
            return p
    for name in BROWSER_ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return ""


# Block-level tags create a visual break; inline tags do NOT. Framer
# splits headings into one <span> PER CHARACTER, so replacing every tag
# with a space turns "Effortless" into "E f f o r t l e s s" — which
# makes the original unmatchable by any faithful port. Separator choice
# is therefore part of the measurement, not a detail.
BLOCK_TAGS = ("html|head|body|div|p|section|article|header|footer|main|nav|"
              "aside|ul|ol|li|dl|dt|dd|table|thead|tbody|tr|td|th|form|"
              "fieldset|figure|figcaption|blockquote|pre|hr|br|h[1-6]|"
              "video|iframe|canvas|address|details|summary|option")


def _visible_text(html: str) -> str:
    """What a reader would actually see — no script/style/svg payloads,
    and inline markup joined the way a browser joins it."""
    t = re.sub(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>",
               " ", html)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(rf"(?is)</?(?:{BLOCK_TAGS})\b[^>]*>", " ", t)   # break
    t = re.sub(r"(?s)<[^>]+>", "", t)                          # inline: join
    return re.sub(r"\s+", " ", html_mod.unescape(t)).strip()


def _visible_reading(dom: str):
    """The page's own report of what it painted, or None if absent.

    None is not zero and must never be read as one: a missing reading
    means the measurement did not run (no injection, a page that threw
    before load, a handler that fell back), and a check that cannot run
    reports SKIPPED, never PASS — and never FAIL either.
    """
    m = re.search(r'<script[^>]+id="__ae_visible"[^>]*>(.*?)</script>',
                  dom or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _console_messages(log: str) -> list:
    """[(message, source_url)] from Chrome's stderr, de-duplicated."""
    out, seen = [], set()
    for ln in log.splitlines():
        if ":CONSOLE" not in ln:
            continue
        m = re.search(r"CONSOLE[^\]]*\]\s*(.*)", ln)
        body = (m.group(1) if m else ln).strip()
        src = ""
        sm = re.search(r",\s*source:\s*(\S+)", body)
        if sm:
            src = sm.group(1)
            body = body[:sm.start()]
        body = body.strip().strip('"').strip()
        if body and body not in seen:
            seen.add(body)
            out.append((body, src))
    return out


def _reference_render(root: Path, page: str, browser: str, budget: int):
    """Visible-text length of the UNTOUCHED export for this page, or None.

    pristine/ is the original as captured, so serving it and rendering it
    answers "does the original do this too?" without a live URL and
    without a person. Returns None when it cannot be rendered — a
    comparison that could not run must never be reported as agreement.
    """
    src = root / "pristine" / page
    if not src.is_file() or not browser:
        return None
    try:
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        d = str((root / "pristine").resolve())
        h = functools.partial(SimpleHTTPRequestHandler, directory=d)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
        srv.RequestHandlerClass.log_message = lambda *a, **k: None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            got = _render_page(
                browser, f"http://127.0.0.1:{srv.server_address[1]}/{page}",
                budget_ms=budget)
        finally:
            srv.shutdown()
        return len(_visible_text(got["dom"])) if got.get("dom") else None
    except Exception:
        return None


def _render_page(browser: str, url: str, budget_ms=8000, timeout=60,
                 offline=False) -> dict:
    """Load one page in headless Chromium. -> {dom, console, error}.

    Chromium does not always exit after --dump-dom, so we read stdout
    until the document is complete and then kill it — waiting for the
    process would cost the full timeout on every page."""
    import os as _os
    import subprocess
    import tempfile
    import threading
    tmp = tempfile.mkdtemp(prefix="forge-probe-")
    errlog = Path(tmp) / "chrome.log"
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions",
           "--disable-background-networking", "--mute-audio",
           "--window-size=1440,2400", "--hide-scrollbars",
           f"--user-data-dir={tmp}/profile",
           "--enable-logging=stderr", "--log-level=0"]
    if budget_ms:
        cmd.append(f"--virtual-time-budget={budget_ms}")
    if offline:
        cmd.append("--host-resolver-rules=MAP * ~NOTFOUND, "
                   "EXCLUDE 127.0.0.1")
    cmd += ["--dump-dom", url]
    chunks, done = [], threading.Event()
    try:
        with open(errlog, "wb") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef)

            def pump():
                try:
                    while True:
                        b = _os.read(proc.stdout.fileno(), 65536)
                        if not b:
                            return
                        chunks.append(b)
                        if b"</html>" in b:
                            return
                finally:
                    done.set()

            threading.Thread(target=pump, daemon=True).start()
            finished = done.wait(timeout)
            proc.kill()
            proc.wait(timeout=10)
        dom = b"".join(chunks).decode("utf-8", "ignore")
        log = errlog.read_text(encoding="utf-8", errors="ignore")
        if not dom and budget_ms:
            # THE VIRTUAL CLOCK CAN WEDGE. On fiber's desktop variant
            # (>=1280px wide) every budget tried — 3s through 90s —
            # produced not one byte in 45s, while the same page with no
            # budget at the same window size rendered in 3s: 253KB, 19
            # images. The budget exists only to tell --dump-dom when the
            # page has settled, so when it never expires, drop it and
            # dump at load instead. Reported as mode="realtime" because a
            # less-settled DOM is a weaker measurement, not a free win.
            again = _render_page(browser, url, budget_ms=0,
                                 timeout=min(timeout, 45), offline=offline)
            if again.get("dom"):
                again["mode"] = "realtime"
                return again
        return {"dom": dom, "console": _console_messages(log),
                "mode": "virtual" if budget_ms else "realtime",
                "error": "" if finished or dom else
                         f"browser did not render within {timeout}s"}
    except Exception as e:                                # pragma: no cover
        return {"dom": "", "console": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fingerprint(page_result: dict, dom: str = "") -> dict:
    """What a page IS, to a reader — not how it was built. Text, the
    heading outline and how many images rendered. This is what a port to
    another framework has to preserve; markup and class names are not."""
    return {"text": page_result.get("_text", "")[:20000],
            "words": len((page_result.get("_text", "")).split()),
            "headings": page_result.get("_headings", []),
            "images": page_result.get("images", 0)}


def _headings(dom: str) -> list:
    """h1-h3 text, normalised the same way (split-character headings
    must read as words, or no port could ever match them)."""
    out = []
    for m in re.finditer(r"(?is)<h([1-3])\b[^>]*>(.*?)</h\1\s*>", dom):
        t = _visible_text(m.group(2))
        if t:
            out.append(t[:120])
    return out[:40]


# A live clock renders a different value every second, so comparing two
# renders of the SAME page can never reach 100%. Normalise anything that
# is time-of-day before diffing — otherwise a perfect port is reported
# as 99% forever, and a real 1% loss hides in the same noise.
# "1 M", "18 %", "1 +", "24" — a stat counter caught mid count-up. No
# port can reproduce a specific frame of an animation, so these are not
# required verbatim.
COUNTER_HEADING = re.compile(r"(?i)\s*[\d.,]+\s*(?:[%+]|[mkb]|bn|m\+|k\+)?\s*")

LIVE_VALUE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\b")


# Text that belongs to the platform's own badge. A port DELETES that
# badge — that is the product's whole promise — so counting its absence
# as missing content penalises the port for succeeding.
PLATFORM_PROMO = re.compile(
    r"(?i)(?:create a free website with framer[^.]*\.?|"
    r"made in webflow|powered by webflow|"
    r"the website builder loved by startups[^.]*\.?)")

# My own extractor inserts a space between block elements, and Framer
# renders a counter's number and its unit as separate blocks ("1" "M").
# A port that writes them inline shows the SAME thing to a reader.
COUNTER_UNIT = re.compile(r"(?i)(\d)\s+([%+]|[mkb]\b)")


def _normalise_live(text: str) -> str:
    text = LIVE_VALUE.sub("<time>", text)
    text = PLATFORM_PROMO.sub("", text)
    return COUNTER_UNIT.sub(r"\1\2", text)


def _similarity(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, _normalise_live(a).split(),
                                   _normalise_live(b).split()).ratio()


# Hosts that mean "this page still belongs to the platform it came
# from". A port can render perfectly and be 100% identical while every
# image streams from the vendor's CDN — it looks owned and is not. That
# is invisible to a content score, so it is checked separately.
PLATFORM_HOSTS = re.compile(
    r"(?i)https?://[^\"']*(?:framerusercontent\.com|framer\.com|"
    r"framer\.website|website-files\.com|webflow\.com|webflow\.io|"
    r"d3e54v103j8qbb\.cloudfront\.net)[^\"']*")


# Hosts that SERVE THE SITE'S BYTES. A reference to one of these is a
# dependency: pull the plug and the page loses content. Hosts that merely
# host the vendor's own website are a different thing — a link.
ASSET_HOSTS = ("framerusercontent.com", "website-files.com",
               "d3e54v103j8qbb.cloudfront.net", "cloudfront.net")


def _platform_refs(dom: str) -> dict:
    """{host: count} of everything the page still FETCHES from the platform.

    A LINK IS NOT A DEPENDENCY. The invariant is that badges and promos
    are CSS-hidden rather than deleted, because the runtime re-creates any
    node removed from the DOM — so a carried runtime legitimately still
    holds an <a href> to framer.com/@author?tab=marketplace, hidden and
    never fetched.

    Counting that as NOT OWNED failed a port whose every one of 83 runtime
    requests was served locally, under the message "renders only because
    that CDN is reachable", which was simply untrue of it.

    Asset hosts stay strict: those are the bytes the page cannot do
    without, and today they hid 445 CMS urls behind a check that was not
    reading the right files. Vendor-site links are reported separately by
    the caller as a note.
    """
    out = {}
    for u in PLATFORM_HOSTS.findall(dom):
        try:
            host = u.split("/")[2]
        except IndexError:
            continue
        if not any(h in host for h in ASSET_HOSTS):
            continue
        out[host] = out.get(host, 0) + 1
    return out


def _platform_links(dom: str) -> dict:
    """{host: count} of vendor-site links — hidden promos, not fetches."""
    out = {}
    for u in PLATFORM_HOSTS.findall(dom):
        try:
            host = u.split("/")[2]
        except IndexError:
            continue
        if any(h in host for h in ASSET_HOSTS):
            continue
        out[host] = out.get(host, 0) + 1
    return out


def _compare_against(root: Path, target: str, results: list, budget: int):
    """Compare what the browser renders here with what it renders at
    `target` (a URL, or a directory served the same way). Returns the
    number of pages that differ enough to matter.

    The acceptance test for `port this site to Next/Astro/anything`:
    the port passes when the reader cannot tell."""
    from http.server import ThreadingHTTPServer
    import threading
    browser = _find_browser()
    base = {p["page"]: p for p in results if p.get("runtime") == "ok"}
    if not base:
        print("\nnothing to compare (no page rendered here)")
        return 1
    srv = None
    if re.match(r"https?://", target):
        origin = target.rstrip("/")
    else:
        d = Path(target).expanduser().resolve()
        if not d.is_dir():
            die(f"--against: no such directory or URL: {target}")
        H = _site_handler(d, "static", quiet=True)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"\n── comparing against {origin}")
    bad = 0
    try:
        for page, mine in base.items():
            # a port usually serves clean routes: index.html -> /
            # A port may serve clean routes (/about) or files
            # (/about.html) depending on the framework's output mode.
            # Asking for the wrong one grades a 404 as "0% identical",
            # which looks exactly like a broken port.
            route = "" if page == "index.html" else page[:-5] \
                if page.endswith(".html") else page
            got = _render_page(browser, f"{origin}/{route}",
                               budget_ms=budget)
            if len(_visible_text(got.get("dom") or "")) < 200 and route:
                alt = _render_page(browser, f"{origin}/{page}",
                                   budget_ms=budget)
                if len(_visible_text(alt.get("dom") or "")) > \
                        len(_visible_text(got.get("dom") or "")):
                    got, route = alt, page
            if not got["dom"]:
                print(f"FAIL {page}: nothing rendered at {origin}/{route}")
                bad += 1
                continue
            theirs = _visible_text(got["dom"])
            sim = _similarity(mine.get("_text", ""), theirs)
            h_mine = mine.get("_headings", [])
            h_theirs = _headings(got["dom"])
            # A heading counts as PRESENT if its words are there — as a
            # heading of its own, or inside a larger one. Framer splits a
            # hero line into one element per word ("Effortless",
            # "Design", "for"), so demanding an exact element-for-element
            # match would fail every port that writes it as one sane
            # heading, i.e. punish the better structure.
            joined = " ".join(h_theirs).lower()
            body = theirs.lower()
            # Headings with no letters ("1 +", "18 %") are Framer's stat
            # counters: they animate up from zero, so the baseline caught
            # one FRAME of an animation. Requiring it verbatim asks a port
            # to reproduce a moment in time — no faithful port can.
            checkable = [h for h in h_mine if not COUNTER_HEADING.fullmatch(h)]
            animated = len(h_mine) - len(checkable)
            joined_n, body_n = _normalise_live(joined), _normalise_live(body)
            missing = [h for h in checkable
                       if _normalise_live(h.lower()) not in joined_n
                       and _normalise_live(h.lower()) not in body_n]
            imgs = len(re.findall(r"<img\b", got["dom"]))
            leaks = _platform_refs(got["dom"])
            # IMAGES WERE COUNTED AND NEVER JUDGED. The Next emitter
            # rendered 65 of the original's 153 and the tool announced
            # "PIXEL-PERFECT PORT READY", because the verdict was text,
            # headings and ownership only. More than half the pictures
            # missing is not a rounding error and no reader would call
            # that page the same page.
            #
            # The bar is 90% because it has to tolerate a genuinely
            # faithful port, and the measured one does: astro renders
            # 152 of 153 on this same template. 65 is damage.
            n_mine = int(mine.get("images", 0) or 0)
            lost_imgs = n_mine >= 10 and imgs < 0.9 * n_mine
            ok = sim >= 0.90 and not missing and not leaks and not lost_imgs
            print(("PASS " if ok else "FAIL ")
                  + f"{page or '/'}: text {int(sim * 100)}% identical, "
                    f"headings {len(h_theirs)}/{len(h_mine)}, "
                    f"images {imgs}/{mine.get('images', 0)}"
                  + (f" ({animated} animated counter(s) not required)"
                     if animated else ""))
            if missing:
                print(f"       missing heading(s): {missing[:3]}")
            if lost_imgs:
                print(f"       {n_mine - imgs} of {n_mine} image(s) are "
                      f"NOT RENDERED by this build — the text can match "
                      f"while the page looks nothing like the original.")
            if sim < 0.90:
                # SHOW WHERE THEY DIVERGE. The referee computes the score
                # from both texts and then prints only the number, so
                # "73% identical" says a quarter of the page is wrong and
                # not one word about which quarter. A person re-renders
                # both builds by hand to find out; an agent handed this
                # spent $3.10 and 27 tool calls deriving what the referee
                # already knew.
                #
                # The first divergence is almost always the defect itself:
                # a body sliced from the wrong offset starts with the tail
                # of a comment, and one line of context says so outright.
                _a, _b = mine.get("_text", "").split(), theirs.split()
                _i = 0
                while _i < min(len(_a), len(_b)) and _a[_i] == _b[_i]:
                    _i += 1
                print(f"       first divergence at word {_i} of "
                      f"{len(_a)} (original) / {len(_b)} (this build):")
                print(f"         original: …{' '.join(_a[max(0, _i - 6):_i + 10])[:150]}")
                print(f"         this one: …{' '.join(_b[max(0, _i - 6):_i + 10])[:150]}")
            vendor = _platform_links(got["dom"])
            if vendor and not leaks:
                # Reported, never failed: the invariant is that promos are
                # CSS-hidden rather than deleted, because the runtime
                # re-creates a removed node. Nothing is fetched.
                print("       NOTE hidden vendor link(s), nothing fetched: "
                      + ", ".join(f"{n}x {h}" for h, n in
                                  sorted(vendor.items(), key=lambda x: -x[1])[:2]))
            if leaks:
                total = sum(leaks.values())
                print(f"       NOT OWNED: {total} reference(s) still point "
                      f"at the source platform —")
                for host, n in sorted(leaks.items(), key=lambda x: -x[1])[:3]:
                    print(f"         {n:>4}x {host}")
                print(f"       the port renders only because that CDN is "
                      f"reachable.")
                # Whose fault it is changes what the owner should do. A
                # port that leaks what the ORIGINAL also leaks is a
                # faithful copy of an un-localized project — the fix is
                # upstream, in the project. A port that leaks what the
                # original does not is the converter losing ownership,
                # and that is the bug the Webflow build shipped.
                own = mine.get("_leaks") or {}
                if own:
                    print(f"       the ORIGINAL leaks {sum(own.values())} "
                          f"too — localize the PROJECT first: "
                          f"`forge.py localize`, `build`, then convert.")
                else:
                    print(f"       the original does NOT — the port lost "
                          f"ownership the source had.")
            if not ok:
                bad += 1
    finally:
        if srv:
            srv.shutdown()
    print(f"     {len(base) - bad}/{len(base)} page(s) render the same")
    return bad


def cmd_probe(args):
    from http.server import ThreadingHTTPServer
    import threading
    root = Path.cwd()
    cfg = read_cfg(root)
    site = root / "site"
    if not site.exists():
        die("no site/ — run build first")
    flags = [a for a in args if a.startswith("--")]
    every = "--all" in flags
    offline = "--offline" in flags
    budget = next((int(a.split("=", 1)[1]) for a in flags
                   if a.startswith("--budget=")), 8000)
    want = next((a.split("=", 1)[1] for a in flags
                 if a.startswith("--page=")), "")

    pages = sorted(cfg.get("pages", []), key=lambda p: p != "index.html")
    pages = [p for p in pages if (site / p).exists()]
    if want:
        pages = [p for p in pages if p == want or p == want + ".html"]
        if not pages:
            die(f"no such page in site/: {want}")
    skipped = 0
    if not every and len(pages) > 6:
        skipped = len(pages) - 6
        pages = pages[:6]
    if not pages:
        die("no built pages to probe")

    requests, lock = [], threading.Lock()

    def record(path, status):
        with lock:
            requests.append((path, status))

    # Serve the site with a reader's-eye measurement riding along. It
    # answers the question a character count cannot: is any of this text
    # actually PAINTED? Eight deliberately broken sites — transparent,
    # display:none, shoved off screen, images all dead — passed as
    # CLEAN before this, because the old measurement read the HTML
    # string and the HTML was perfect in every one of them.
    try:
        import aethron_motion as _motion
        H = _motion._injecting_handler(site, cfg.get("platform", "static"),
                                       _motion.VISIBLE_JS, on_request=record)
    except Exception:
        # The probe still works without it; it just sees less. Say so
        # rather than pretend, when the reading turns out to be missing.
        H = _site_handler(site, cfg.get("platform", "static"),
                          on_request=record, quiet=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    browser = _find_browser()
    print(f"probing {len(pages)} page(s) on http://127.0.0.1:{port}"
          + (f"  (+{skipped} more — use --all)" if skipped else ""))
    if browser:
        print(f"browser: {browser}")
    else:
        print(f"browser: NONE FOUND — set {BROWSER_ENV}=/path/to/chrome")

    results, fails, notes = [], 0, 0
    try:
        for page in pages:
            with lock:
                requests.clear()
            ssr = _visible_text((site / page).read_text(encoding="utf-8",
                                                        errors="ignore"))
            r = {"page": page, "ssr_text": len(ssr)}
            print(f"\n── {page}")
            if not browser:
                r["runtime"] = "skipped"
                results.append(r)
                continue
            got = _render_page(browser, f"http://127.0.0.1:{port}/{page}",
                               budget_ms=budget, offline=offline)
            with lock:
                reqs = list(requests)
            dom_text = _visible_text(got["dom"])
            vis = _visible_reading(got["dom"])
            # A favicon the SITE asks for is the site's problem; one only
            # the browser asks for is not. The question is whether the
            # page references THAT PATH — not whether it links some icon.
            # qourvac2 links six localized icons on every page and Chrome
            # still probed /favicon.ico on two of them, so "an icon is
            # linked" wrongly made the browser's own fallback a defect.
            page_links_favicon = "/favicon.ico" in (got["dom"] or "")
            bad, platform_calls = {}, {}
            for path, status in reqs:
                if status < 400:
                    continue
                clean = path.split("#")[0].split("?")[0]
                # THE BROWSER ASKS FOR THIS ONE BY ITSELF. Chrome requests
                # /favicon.ico on every navigation when no icon is linked,
                # so a site that never mentions a favicon is charged with a
                # 404 it did not cause — measured on qourvac2 and
                # webflow-demo, neither of which references one on any of
                # their 36 and 3 pages. The console filter already ignored
                # it; the request filter did not, so the same non-event was
                # a NOTE in one place and a FAIL in the other.
                if clean == "/favicon.ico" and not page_links_favicon:
                    continue
                # A PLATFORM BACKEND CANNOT BE SELF-HOSTED. Webflow's
                # runtime opens an Apollo client against /.wf_graphql/
                # (and /api/v2/sites/ in the designer) to fetch a CSRF
                # token. Those endpoints exist only on Webflow's own
                # infrastructure, so the call fails on every other host —
                # a plain static host included — and the runtime carries
                # on: the probe's own "nothing failed to load or execute"
                # holds on all six pages. Owning the site is precisely
                # what makes this unanswerable, so it is reported rather
                # than counted as breakage.
                if PLATFORM_BACKEND_RE.match(clean):
                    platform_calls.setdefault(clean, status)
                    continue
                bad.setdefault(path.split("#")[0], status)
            hard, soft = [], []
            for msg, src in got["console"]:
                low = msg.lower()
                remote = bool(src) and "127.0.0.1" not in src
                if any(k in low for k in CONSOLE_IGNORE):
                    continue
                if not any(k in low for k in CONSOLE_FATAL):
                    soft.append(msg)
                elif remote:
                    # a third-party script we do not ship failing is the
                    # owner's call, not a migration defect
                    soft.append(f"{msg} [remote: {src[:60]}]")
                else:
                    hard.append(msg)
            asked = list(dict.fromkeys(p for p, _ in reqs))
            # kept out of the report (underscore keys are stripped), but
            # this is what a baseline/comparison is made of
            r["_text"] = dom_text
            r["_headings"] = _headings(got["dom"])
            # The original's own platform dependency, so a comparison can
            # tell "the port lost ownership" from "the project never had
            # it" — two failures with different fixes.
            r["_leaks"] = _platform_refs(got["dom"])
            r.update(rendered_text=len(dom_text), requests=len(reqs),
                     failed_requests=bad, requested=asked[:200],
                     images=len(re.findall(r"<img\b", got["dom"])),
                     console_errors=hard[:20], console_notes=soft[:10],
                     runtime="ok" if got["dom"] else "failed",
                     error=got["error"])
            # Carried into the report, not just printed: the healer and
            # the studio read this file, and "the text is present but
            # nothing is painted" is exactly the kind of evidence an
            # agent needs and can never recover from a PASS/FAIL line.
            # None means the measurement did not run — kept distinct
            # from zero, which would read as "nothing was visible".
            r["visible"] = vis

            if got["error"] or not got["dom"]:
                print(f"FAIL could not render: {got['error'] or 'empty DOM'}")
                fails += 1
                results.append(r)
                continue

            # 1. does it render at all?
            # SMALL IS NOT BROKEN. The blank-page rule exists to catch a
            # page whose content DISAPPEARS — the wild failure where a
            # site ships 13k of HTML and renders 129 chars. A page that
            # renders everything it shipped has lost nothing, however
            # little that is: webflow-demo's about.html is 116 chars in
            # the source and 116 on screen, and was failed for "the page
            # ships but shows nothing" while showing all of it.
            # ── IS ANY OF IT PAINTED? ─────────────────────────────────
            # Runs before the character checks because a page that is in
            # the DOM and not on the screen would otherwise be praised
            # for the size of its text.
            if vis is not None:
                seen_t = int(vis.get("text_seen") or 0)
                paint_t = int(vis.get("text_painted") or 0)
                park_t = int(vis.get("text_parked") or 0)
                n_img = int(vis.get("images") or 0)
                n_ok = int(vis.get("images_loaded") or 0)
                if seen_t >= 200 and paint_t < 120:
                    if park_t >= 0.5 * seen_t or vis.get("appear"):
                        # Framer parks entrances at opacity 0.001 and the
                        # headless virtual clock may never let the appear
                        # engine run, so the page reads as invisible while
                        # being perfectly healthy — measured on agero's
                        # blog.html, 164 nodes parked at 0.001.
                        #
                        # UNPROVEN IS NOT BROKEN. The rule this project
                        # already lives by — a check that cannot run
                        # reports SKIPPED, never PASS — cuts both ways:
                        # it must not report FAIL either.
                        print(f"NOTE visibility UNPROVEN: {paint_t} of "
                              f"{seen_t} chars painted, but this page "
                              f"animates content in and the entrance had "
                              f"not played when measured. Not evidence of "
                              f"damage; open the page to be sure.")
                        notes += 1
                    else:
                        print(f"FAIL the text is in the DOM but NOT ON "
                              f"SCREEN: {seen_t} chars present, {paint_t} "
                              f"painted — a reader sees a blank page "
                              f"(transparent, display:none, or positioned "
                              f"out of the document)")
                        fails += 1
                if n_img and n_ok == 0:
                    print(f"FAIL every image is broken: {n_img} declared, "
                          f"0 decoded"
                          + (f" — e.g. {vis['images_broken'][0]}"
                             if vis.get("images_broken") else ""))
                    fails += 1
                elif n_img and n_ok < n_img:
                    miss = n_img - n_ok
                    print(f"{'FAIL' if miss > 0.25 * n_img else 'NOTE'} "
                          f"{miss} of {n_img} image(s) did not load"
                          + (f" — e.g. {vis['images_broken'][0]}"
                             if vis.get("images_broken") else "")
                          + " (a third-party host the request log never "
                            "sees)")
                    if miss > 0.25 * n_img:
                        fails += 1
                    else:
                        notes += 1

            faithful = ssr and len(dom_text) >= 0.9 * len(ssr)
            if len(dom_text) < 120 and not faithful:
                print(f"FAIL renders BLANK ({len(dom_text)} chars of text "
                      f"after JS) — the page ships but shows nothing"
                      + (f", from {len(ssr)} chars of HTML" if ssr else ""))
                fails += 1
            elif ssr and len(ssr) >= 400 and len(dom_text) < 0.4 * len(ssr):
                # ASK THE ORIGINAL BEFORE BLAMING THE BUILD.
                #
                # The ratio is a heuristic and it cannot tell "hydration
                # wiped the page" from "this template's SSR carries more
                # text than it ever displays". Both look like a big number
                # shrinking. The invariant already says what to do —
                # compare against the untouched original — and pristine/
                # IS that original, so the probe can do it rather than
                # leave it to a person.
                #
                # Measured on test-3: 10,128 chars of HTML rendering 3,737
                # looked like a wipe; the pristine export renders 3,875
                # from the same page, so the build loses one character to
                # the original, not two thirds of the page.
                ref = _reference_render(root, page, browser, budget)
                if ref is not None and len(dom_text) >= 0.9 * ref:
                    print(f"NOTE the ORIGINAL renders the same: {len(ssr)} "
                          f"chars of HTML -> {ref} in the untouched export "
                          f"vs {len(dom_text)} here. Not a wipe; this "
                          f"template's SSR text is not all displayed.")
                    print(f"PASS renders {len(dom_text)} chars of text, "
                          f"{r['images']} image(s)")
                else:
                    print(f"FAIL content DISAPPEARS after JS: {len(ssr)} "
                          f"chars in the HTML -> {len(dom_text)} rendered "
                          f"(hydration is wiping the page)"
                          + (f"; the untouched export renders {ref}"
                             if ref is not None else
                             "; could not render the original to compare"))
                    fails += 1
            else:
                print(f"PASS renders {len(dom_text)} chars of text, "
                      f"{r['images']} image(s)")

            # 2. what did the browser actually fail to fetch?
            if bad:
                print(f"FAIL {len(bad)} request(s) failed at runtime "
                      f"(of {len(reqs)}):")
                for path, status in list(bad.items())[:8]:
                    print(f"       {status}  {path[:96]}")
                print("     -> `forge.py capture` downloads everything the "
                      "pages reference, then rebuild")
                fails += 1
            else:
                print(f"PASS all {len(reqs)} runtime request(s) served")
            if platform_calls:
                print("     NOTE the platform runtime calls its own backend, "
                      "which no self-hosted copy can answer: "
                      + ", ".join(f"{p} ({s})"
                                  for p, s in list(platform_calls.items())[:3]))

            # 3. did the code throw?
            if hard:
                print(f"FAIL {len(hard)} console error(s) — code or assets "
                      f"failed to load:")
                for m in hard[:5]:
                    print(f"       {m[:110]}")
                fails += 1
            else:
                print("PASS nothing failed to load or execute")
            if soft:
                print(f"NOTE {len(soft)} other console message(s) — usually "
                      f"pre-existing export artifacts; compare with the "
                      f"untouched original before chasing them:")
                for m in soft[:3]:
                    print(f"       {m[:100]}")
                notes += 1
            results.append(r)
    finally:
        srv.shutdown()

    report = {"pages": [{k: v for k, v in p.items()
                         if not k.startswith("_")} for p in results],
              "browser": browser or None,
              "probed": len(pages), "not_probed": skipped,
              "fails": fails, "offline": offline}
    (site / ".forge-probe.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")

    # ── baseline / comparison ────────────────────────────────────────
    # THE REFEREE for any port of this site to another framework: what
    # the browser renders must still be the same. Fingerprints are taken
    # from the RENDERED page, so they are framework-agnostic by
    # construction — a Next.js or Astro rebuild is judged on output,
    # never on how it got there.
    if browser and "--baseline" in flags:
        base = {p["page"]: _fingerprint(p) for p in results
                if p.get("runtime") == "ok"}
        (root / ".forge-baseline.json").write_text(
            json.dumps({"pages": base}, indent=1), encoding="utf-8")
        print(f"\nbaseline saved for {len(base)} page(s) -> "
              f".forge-baseline.json")
    against = next((a.split("=", 1)[1] for a in flags
                    if a.startswith("--against=")), "")
    if against:
        if not browser:
            print("\nVERDICT: SKIPPED — comparison needs a browser")
            sys.exit(0)
        # --against asks ONE question: is the other build the same site?
        # The original's own runtime health is a different question, and
        # folding it in here condemns a faithful port for a defect it
        # correctly inherited — a template that ships two console errors
        # would fail every port of itself, including a byte-identical
        # copy. Report the original's problems as context; judge on the
        # comparison alone.
        drift = _compare_against(root, against, results, budget)
        if fails:
            print(f"\nnote: the ORIGINAL has {fails} runtime problem(s) of "
                  "its own (listed above). A port inherits those; they do "
                  "not count against the comparison.")
        print("\nVERDICT:", "the port does NOT match the original"
              if drift else "the port matches the original")
        sys.exit(1 if drift else 0)

    if not browser:
        print("\nVERDICT: SKIPPED — no Chromium-family browser found, so "
              "the site is UNVERIFIED at runtime (not proven good).\n"
              f"  Install Chrome, or set {BROWSER_ENV} to a Chromium "
              "binary, and run `forge.py probe` again.")
        sys.exit(0)
    # SAY WHAT WAS NOT LOOKED AT. The probe renders a capped number of
    # pages, and on a 22-page site that meant 6 checked and 16 never
    # opened — reported as "CLEAN at runtime" with no qualification.
    # That is the vacuous pass the invariant forbids: a check that cannot
    # run reports SKIPPED, never PASS, and one that ran on a quarter of
    # the site must say which quarter.
    scope = (f" ({len(pages)} of {len(pages) + skipped} pages checked; "
             f"{skipped} not probed — `forge.py probe --all` covers "
             f"every page)" if skipped else f" (all {len(pages)} pages)")
    print("\nVERDICT:", f"{fails} runtime problem(s) — the built site does "
          f"not behave correctly{scope}" if fails else
          "CLEAN at runtime — pages render, every request served, no "
          f"console errors{scope}")
    sys.exit(1 if fails else 0)


# ─────────────────────────── verify ──────────────────────────────────

# How much of the template's own wording a finished REBRAND may still
# show. Nav labels, UI verbs, prices and legal boilerplate legitimately
# survive, so the floor is well above zero.
#
# CALIBRATED ON THE STATIC INSTRUMENT, WHICH IS NOT THE RENDERED TRUTH.
# _rebrand_depth reads built SSR HTML, and SSR carries pruned breakpoint
# variants that the browser never shows, so it reads HIGHER than what a
# visitor sees. Measured on the same project, twice:
#     before the fill gate was fixed:  static 0.47   rendered 0.46  (bad)
#     after:                           static 0.32   rendered 0.20  (good)
# So the two instruments agree on a failure and diverge by 12 points on a
# success. A threshold of 0.30 read against static condemns the good run;
# 0.40 separates both measured pairs on the instrument this check
# actually uses.
#
# TWO POINTS FROM ONE MIGRATION IS NOT A CORPUS. Widen it only with
# evidence from several finished migrations — never to quiet one run.
REBRAND_SAME_MAX = 0.40


def _rebrand_depth(root: Path, site: Path):
    """-> (fraction of visible copy identical to the template, pages) | None.

    Compares what the BUILT pages show against what the PRISTINE pages
    show, word for word. Text only: markup, classes and asset paths are
    irrelevant to whether the writing is yours.

    It reads the source HTML rather than rendering, so it is a file check
    like the rest of verify — cheap, no browser. That undercounts on
    templates whose copy lives mostly in chunks, which is the safe
    direction: it will under-claim a rebrand, never over-claim one.
    """
    try:
        import difflib
        pages = [p for p in (root / "pristine").glob("*.html")][:8]
        if not pages:
            return None
        ratios = []
        for p in pages:
            built = site / p.name
            if not built.is_file():
                continue
            a = _visible_text(p.read_text(encoding="utf-8", errors="ignore"))
            b = _visible_text(built.read_text(encoding="utf-8",
                                              errors="ignore"))
            if len(a.split()) < 30:
                continue
            ratios.append(difflib.SequenceMatcher(
                None, a.split(), b.split()).ratio())
        if not ratios:
            return None
        return sum(ratios) / len(ratios), len(ratios)
    except Exception:
        return None


def cmd_verify(_args):
    root = Path.cwd()
    cfg = read_cfg(root)
    site = root / "site"
    if not site.exists():
        die("no site/ — run build first")
    fails = 0

    # 1. forbidden words gone from every generated file. Webflow embeds
    # the brand in CDN asset FILENAMES (kitpro-cyntra…css) which the
    # build deliberately shields (rewriting them 404s the stylesheet) —
    # those are never rendered, so they're a NOTE, not a FAIL.
    cdn_b = re.compile(CDN_URL_RE.pattern.encode())
    words = cfg.get("forbidden_words", [])
    # The build records what could not fit a locked slot; without it
    # verify cannot tell "impossible" from "forgotten".
    _rep = site / ".forge-report.json"
    over_slot = set()
    if _rep.is_file():
        try:
            over_slot = {s.lower() for s in
                         json.loads(_rep.read_text()).get("__cms_over__", [])}
        except Exception:
            over_slot = set()

    for w in words:
        hits, url_only = [], []
        for f in site.rglob("*"):
            # `sources/` is the AUTHORED SOURCE recovered from the
            # platform's own source maps — reference material for whoever
            # inherits the project, loaded by nothing and referenced by no
            # page. Of course it still says the template's original name:
            # those are its variable names and comments. The doctor learned
            # to skip it after it reported 217 platform urls on a build
            # that fetched none; the brand scan never did, so a rebrand
            # that had replaced every rendered mention still reported FAIL
            # and pointed at five files the browser never opens.
            _rel = str(f.relative_to(site)).replace("\\", "/")
            if _rel.startswith("sources/") or "/sources/" in _rel:
                continue
            if f.is_file() and f.suffix in (".html", ".mjs", ".framercms", ".js"):
                blob = f.read_bytes()
                present = w.encode() in blob or w.lower().encode() in blob
                if not present:
                    continue
                masked = cdn_b.sub(b"", blob)
                if w.encode() in masked or w.lower().encode() in masked:
                    hits.append(str(f.relative_to(site)))
                else:
                    url_only.append(str(f.relative_to(site)))
        # A LEFTOVER THAT PHYSICALLY CANNOT BE REPLACED IS NOT A DEFECT.
        #
        # A CMS slot is byte-locked, so a replacement longer than the
        # original cannot be written into one — and the commonest rebrand
        # of all hits this: a brand whose name is longer than the one it
        # replaces. Build already degrades to text-layers-only and says
        # so. Reporting the survivor as a plain FAIL on every subsequent
        # verify teaches the owner that verify cries wolf, which is how a
        # real leftover gets ignored later.
        #
        # It is still reported — loudly, with the reason and the remedy —
        # just not counted as a failure the owner cannot act on.
        cms_only = hits and all(h.endswith(".framercms") for h in hits)
        if hits and cms_only and w.lower() in over_slot:
            print(f"NOTE '{w}' remains in {len(hits)} CMS binary(ies): the "
                  f"replacement is longer than its byte-locked slot, so the "
                  f"text layers were updated and the CMS kept the original. "
                  f"Shorten the replacement to change it everywhere.")
        elif hits:
            print(f"FAIL leftover '{w}' in: {', '.join(hits[:5])}")
            fails += 1
        else:
            print(f"PASS no leftover '{w}'" + (
                f" (remains only inside CDN asset filenames in "
                f"{len(url_only)} file(s) — never rendered)" if url_only else ""))

    # 1.5 pristine integrity: hand-edits to the sealed original corrupt
    # every build (and hydration reverts them anyway — the classic trap)
    tampered = _pristine_tampered(root)
    if tampered:
        print(f"FAIL pristine/ was MODIFIED ({len(tampered)} file(s): "
              f"{', '.join(tampered[:3])}) — restore it (re-init/"
              "re-scrape) and route ALL changes through copy_map.json")
        fails += 1
    elif (root / "pristine" / ".forge-manifest.json").exists():
        print("PASS pristine integrity (sealed original untouched)")

    # 2. platform-appropriate integrity checks.
    # HISTORY: these Framer checks used to run on EVERY platform, so a
    # Next.js capture with ZERO downloaded assets printed "PASS: all
    # referenced chunks present" (vacuously true — it has no chunks) and
    # shipped a blank page as healthy. Checks now only run where they
    # mean something; the reference audit below covers every platform.
    if cfg.get("platform") == "framer":
        for page in cfg["pages"]:
            t = (site / page).read_text(encoding="utf-8", errors="ignore")
            missing = [n for n in re.findall(r"\./assets/chunks/([^\"'\s)]+\.mjs)", t)
                       if not (site / "assets" / "chunks" / n).exists()]
            if missing:
                print(f"FAIL {page}: missing chunks {missing[:5]}")
                fails += 1
            else:
                print(f"PASS {page}: all referenced chunks present")
            remote = re.findall(
                r"https://(?:events\.framer\.com|framer\.com/edit)[^\"']*", t)
            print(("FAIL" if remote else "PASS")
                  + f" {page}: framer telemetry refs: {len(remote)}")
            fails += bool(remote)

    # 2b. REFERENCE AUDIT (every platform): does each asset the built
    # pages ask for actually exist on disk? This is the check that would
    # have caught the blank-page disaster on the very first build.
    missing_refs = _ref_audit(site)
    if missing_refs:
        total = sum(len(v) for v in missing_refs.values())
        print(f"FAIL {len(missing_refs)} referenced asset(s) missing from "
              f"site/ ({total} reference(s)) — the page cannot render "
              f"correctly:")
        for ref in list(missing_refs)[:8]:
            print(f"       {ref[:88]}")
        fw = sorted({k for k in FRAMEWORK_DIRS
                     for r in missing_refs if k in r})
        if fw:
            print(f"     these are {', '.join(fw)} runtime assets — run "
                  f"`forge.py capture` to download everything the pages "
                  f"reference, then rebuild")
        else:
            print("     run `forge.py capture` (or `localize`) then rebuild")
        fails += 1
    else:
        print("PASS every referenced asset exists in site/")

    # 3. local image replacements (logo etc.) actually in the built site
    cm_file = root / "copy_map.json"
    if cm_file.exists():
        pub = cfg.get("public_base", "/assets").rstrip("/")
        for e in json.loads(cm_file.read_text(encoding="utf-8")).get("images", []):
            new = e.get("new", "")
            for prefix in (pub + "/", "./assets/", "/assets/"):
                if new.startswith(prefix):
                    ok = (site / "assets" / new[len(prefix):]).is_file()
                    print(("PASS" if ok else "FAIL") + f" local image present: {new}")
                    fails += not ok
                    break

    # 3b. asset health — the "unstyled page" catcher. A local CSS/JS
    # that (a) is referenced but missing, or (b) carries a stale SRI
    # integrity hash that no longer matches its (rewritten) content, is
    # SILENTLY dropped by the browser -> the page renders unstyled.
    import hashlib as _hl
    import base64 as _b64
    for pg in cfg["pages"]:
        fp = site / pg
        if not fp.exists():
            continue
        htxt = fp.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"<(?:link|script)\b[^>]*>", htxt):
            tag = m.group(0)
            ref = re.search(r'(?:href|src)="([^"]+\.(?:css|js|mjs))"', tag)
            if not ref:
                continue
            url = ref.group(1)
            if url.startswith(("http://", "https://", "//")):
                continue
            local = site / url.lstrip("/").lstrip("./")
            if not local.exists():
                local = site / url.split("/", 1)[-1] if "/" in url else local
            if not local.exists():
                print(f"FAIL missing asset {url} (referenced by {pg}) — "
                      "the page will render unstyled/broken")
                fails += 1
                continue
            ig = re.search(r'integrity="sha(\d+)-([^"]+)"', tag)
            if ig:
                algo = {"256": _hl.sha256, "384": _hl.sha384,
                        "512": _hl.sha512}.get(ig.group(1))
                if algo:
                    got = _b64.b64encode(algo(local.read_bytes()).digest()).decode()
                    if got != ig.group(2):
                        print(f"FAIL stale SRI on {url} in {pg} — the browser "
                              "will REJECT this file (unstyled page). Rebuild: "
                              "build now strips SRI from local assets.")
                        fails += 1

    # 4. CMS sizes unchanged vs pristine
    for p in (root / "pristine" / "cms").glob("*.framercms") \
            if (root / "pristine" / "cms").exists() else []:
        out = site / "assets" / "cms" / p.name
        okay = out.exists() and out.stat().st_size == p.stat().st_size
        print(("PASS" if okay else "FAIL") + f" CMS size lock: {p.name}")
        fails += not okay

    cm_f = root / "copy_map.json"
    if cm_f.exists():
        cm = json.loads(cm_f.read_text(encoding="utf-8"))
        if not any(e.get("new") for e in cm.get("images", [])):
            print("NOTE: no images were replaced. Brand marks (logo, "
                  "signature) are usually IMAGES — words drawn inside "
                  "them are invisible to these checks. Eyeball the "
                  "header/footer; `forge.py logo` renders replacements.")

    # destructive hide rules: a selector built from a generic Framer
    # default name (or matching many elements) hides unrelated parts
    # of the site — hydration mints more instances than SSR shows
    page_texts = [(root / "pristine" / p).read_text(encoding="utf-8",
                                                    errors="ignore")
                  for p in cfg["pages"]]
    for sel in cfg.get("hide_selectors", []):
        a = hide_selector_audit(sel, page_texts)
        if a["risky"]:
            print(f"FAIL destructive hide rule: {sel} — {a['why']} "
                  "(fix: python3 forge.py heal drops it; then re-remove "
                  "the element in edit mode)")
            fails += 1

    # 6. HOW MUCH OF THIS SITE IS STILL THE TEMPLATE'S?
    #
    # Every other check here asks whether the OLD BRAND survived. None
    # asked whether the COPY did — so a migration could replace the brand
    # token everywhere, report "no leftover 'Makro'", and still be a
    # finance product with a new logo on it. That happened: 0 brand
    # mentions, 72% of strings untouched, and the owner saw it on the
    # page in seconds while the tool said clean.
    #
    # The port referee compares rendered text and wants it IDENTICAL. This
    # is the same measurement wanting the opposite, and it is the only
    # honest way to tell a rebrand from a relabel.
    # A CHUNK THAT DOES NOT PARSE IS A BLANK PAGE, and every file-level
    # check above passes on one: the bytes are there, the refs resolve,
    # the brand is gone. Only probe caught the `Complete Sets:` syntax
    # error that killed hydration — and probe needs a browser. This needs
    # only a parser, so it runs in far more places.
    cdir = site / (cfg.get("public_base", "/assets").strip("/")) / "chunks"
    mjs = sorted(cdir.glob("*.mjs")) if cdir.exists() else []
    if mjs:
        node = shutil.which("node")
        if not node:
            print(f"SKIPPED chunk syntax ({len(mjs)} file(s)) — no node on "
                  f"this machine; UNVERIFIED, not proven good. "
                  f"`forge.py probe` catches this at runtime.")
        else:
            import subprocess
            broken = []
            for f in mjs:
                r = subprocess.run([node, "--check", str(f)],
                                   capture_output=True, text=True)
                if r.returncode:
                    first = next((l for l in r.stderr.splitlines()
                                  if "Error" in l), "parse error")
                    broken.append(f"{f.name}: {first.strip()[:90]}")
            if broken:
                for b in broken:
                    print(f"FAIL chunk does not parse — {b}")
                print("     a replacement landed in CODE, not copy. The page "
                      "will render blank. Shorten or remove that fill.")
                fails += len(broken)
            else:
                print(f"PASS all {len(mjs)} chunk(s) parse as JavaScript")

    _depth = _rebrand_depth(root, site)
    if _depth is not None:
        same, pages = _depth
        pct = int(same * 100)
        # MEASURING A FAILURE AND PRINTING "PASS" IS THE VACUOUS PASS AGAIN.
        # This branch used to say `PASS copy is 53% rewritten` for a site
        # that was 47% word-for-word the template's — the owner's exact
        # complaint ("ninety percent of the data is still about the
        # original template"), printed as a success by the check built to
        # catch it. A number this check computed correctly must not be
        # narrated as good news.
        #
        # It only becomes a FAIL when a full rebrand was actually asked
        # for — brand_brief.json is the record of that. Swapping only the
        # brand token is a legitimate thing to want, and failing those
        # runs would teach owners to ignore this line.
        wanted_rebrand = (root / "brand_brief.json").is_file()
        print(f"MEASURED {pct}% of the copy across {pages} page(s) is still "
              f"word-for-word the template's (static HTML; a browser "
              f"typically renders ~10 points lower)")
        if same >= 0.85:
            print(f"{'FAIL' if wanted_rebrand else 'NOTE'} the brand is "
                  f"replaced; the content is not. Most of copy_map is still "
                  f"unfilled.")
            fails += bool(wanted_rebrand)
        elif wanted_rebrand and same >= REBRAND_SAME_MAX:
            print(f"FAIL a rebrand was requested but this is still largely "
                  f"the template's copy (threshold {int(REBRAND_SAME_MAX*100)}"
                  f"%). Run `forge.py rebrand` again — it fills only what is "
                  f"still the template's.")
            fails += 1

    # These are FILE checks. They cannot see a page that ships every
    # byte and still renders blank — `probe` runs the code.
    print("\nVERDICT:", "CLEAN on disk — now run `forge.py probe` to see "
          "what the browser actually does with it"
          if not fails else f"{fails} problem(s) — fix and rebuild")
    sys.exit(1 if fails else 0)


# ─────────────────────────── main ────────────────────────────────────

# ─────────────────────────── card ────────────────────────────────────
# The design library is built from CARDS: a distilled fingerprint of a
# template's look and motion — palette, fonts, sections, animation
# features, scale — NEVER the template files themselves. A shared
# library therefore stays license-clean: each user re-imports their own
# purchase (or the card's source URL) to actually build from it.

def cmd_card(_args):
    from collections import Counter
    root = Path.cwd()
    cfg = read_cfg(root)
    # index.html first: title/description/preview must come from the
    # HOME page, not the alphabetically first one (the brand-token
    # lesson, again)
    all_html = "\n".join(
        (root / "pristine" / p).read_text(encoding="utf-8", errors="ignore")
        for p in sorted(cfg["pages"], key=lambda p: p != "index.html"))
    style_soup = all_html + "\n" + "\n".join(
        f.read_text(encoding="utf-8", errors="ignore")
        for f in (root / "pristine").rglob("*.css"))

    # palette: every color literal in pages+css, frequency-ranked
    cnt = Counter()
    for m in re.finditer(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", style_soup):
        h = m.group(1).lower()
        cnt["#" + ("".join(c * 2 for c in h) if len(h) == 3 else h)] += 1
    for m in re.finditer(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
                         style_soup):
        r_, g_, b_ = (min(255, int(x)) for x in m.groups())
        cnt[f"#{r_:02x}{g_:02x}{b_:02x}"] += 1
    palette = [c for c, n in cnt.most_common(60)
               if c not in ("#000000", "#ffffff")][:8]

    # fonts: template @font-face families + google fonts <link>s
    fams = []
    for f in _discover_fonts(root, cfg):
        toks = (f["label"] or "").split()
        while toks and (toks[-1].isdigit() or toks[-1].lower() in
                        ("normal", "italic", "oblique")):
            toks.pop()
        fam = " ".join(toks)
        if fam and fam not in fams:
            fams.append(fam)
    for m in re.finditer(r"fonts\.googleapis\.com/css2?\?family=([^\"'&]+)",
                         all_html):
        for fam in urllib.parse.unquote(m.group(1)).replace("+", " ") \
                                                   .split("|"):
            fam = fam.split(":")[0].strip()
            if fam and fam not in fams:
                fams.append(fam)
    # Webflow loads fonts via WebFont.load({google:{families:[...]}})
    for m in re.finditer(r"families:\s*\[([^\]]*)\]",
                         html_mod.unescape(all_html)):
        for frag in m.group(1).split(","):
            frag = frag.strip()
            if not frag.startswith('"'):
                continue          # a weights fragment, not a family
            fam = frag.lstrip('"').split(":")[0].strip().rstrip('"')
            if fam and fam != "Material Icons" and fam not in fams:
                fams.append(fam)

    def meta(attr, val):
        m = (re.search(rf'{attr}="{val}"[^>]*content="([^"]*)"', all_html)
             or re.search(rf'content="([^"]*)"[^>]*{attr}="{val}"', all_html))
        return html_mod.unescape(m.group(1)).strip() if m else ""

    tm = re.search(r"<title[^>]*>([^<]*)</title>", all_html)
    title = html_mod.unescape(tm.group(1)).strip() if tm else ""

    # authored section names (framer) / section ids (webflow, static)
    sections = []
    for m in re.finditer(
            r'<(?:section|header|footer|nav)[^>]+data-framer-name="([^"]+)"',
            all_html):
        s = html_mod.unescape(m.group(1))
        if s not in sections:
            sections.append(s)
    if not sections:
        for m in re.finditer(r'<(?:section|header|footer)[^>]+id="([^"]+)"',
                             all_html):
            if m.group(1) not in sections:
                sections.append(m.group(1))
    if not sections:
        # webflow: sections are named by class; the class shared by
        # (almost) every section is the generic wrapper — drop it
        lists = [m.group(1).split() for m in re.finditer(
            r'<section[^>]+class="([^"]+)"', all_html)]
        freq = Counter(c for cl in lists for c in set(cl))
        for cl in lists:
            for c in cl:
                if freq[c] <= max(3, len(lists) // 3) and c not in sections:
                    sections.append(c)
                    break

    # motion / component features that make a template feel alive
    chunks = list((root / "pristine" / "chunks").glob("*.mjs")) \
        if (root / "pristine" / "chunks").is_dir() else []
    rotators = 0
    for p in chunks:
        rotators += len(re.findall(
            r"text:`[^`]{2,80}`(?:\s*\}\s*,\s*\{\s*text:`[^`]{2,80}`){2,}",
            p.read_text(encoding="utf-8", errors="ignore")))
    feats = {
        "hover_variants": len(re.findall(
            r'data-framer-name="[^"]*[Hh]over', all_html)),
        "split_text_runs": sum(1 for _ in SPLIT_RUN_RE.finditer(all_html)),
        "rotators": rotators,
        "marquee": bool(re.search(
            r'data-framer-name="[^"]*(?:[Mm]arquee|[Tt]icker)', all_html)),
        "appear_animations": bool(re.search(
            r"data-framer-appear-id|__framer-appear", all_html)),
        "chunks": len(chunks),
        "cms_collections": len(list((root / "pristine" / "cms")
                                    .glob("*.framercms")))
        if (root / "pristine" / "cms").is_dir() else 0,
    }

    cm = {}
    if (root / "copy_map.json").exists():
        cm = json.loads((root / "copy_map.json")
                        .read_text(encoding="utf-8"))
    counts = {"pages": len(cfg["pages"]),
              "strings": len(cm.get("strings", [])),
              "images": len(cm.get("images", [])),
              "links": len(cm.get("links", []))}

    card = {
        "name": cfg["name"], "platform": cfg["platform"],
        "title": title, "description": meta("name", "description"),
        "source_url": cfg.get("source_url", ""),
        "preview_image": meta("property", "og:image"),
        "palette": palette, "fonts": fams, "sections": sections[:16],
        "features": feats, "counts": counts,
    }
    (root / "design_card.json").write_text(json.dumps(card, indent=1),
                                           encoding="utf-8")
    print(f"design card: {len(palette)} colors, {len(fams)} fonts, "
          f"{len(sections)} sections, {counts['pages']} page(s), "
          f"features: " + ", ".join(k for k, v in feats.items() if v))


def _cmd_rebrand(args):
    """Deep rebrand: a few lines about the owner -> the whole site is theirs.

    Lives in aethron_rebrand.py because it is the only command that needs a
    model; forge stays usable with no key at all.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import aethron_rebrand
    return aethron_rebrand.cmd_rebrand(args)


def cmd_audit(argv):
    """Audit the CHECKS, not the site.

    Every hard bug in this project's history was a tool that ran,
    returned cleanly, and was wrong. This reads the verdicts the other
    commands produced and refuses the ones that cannot carry their own
    weight: a PASS with no work, an exit code with no verdict, two
    instruments contradicting each other, a measurement older than what
    it measures, a comparison against a broken reference.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import aethron_audit as A
    except Exception as e:
        die(f"the auditor is unavailable in this build ({e})")
    root = Path(argv[0]).expanduser() if argv and not argv[0].startswith("-") \
        else Path.cwd()
    if not (root / "forge.json").is_file():
        die(f"no forge.json in {root} — run this inside a project")

    vs = []
    site = root / "site"

    probe = site / ".forge-probe.json"
    if probe.is_file():
        vs.append(A.from_probe(probe, artifact=site / "index.html"))
    else:
        vs.append(A.Verdict("probe", A.SKIPPED,
                            instrument={"why": "never run"}))

    rep = site / ".forge-report.json"
    if rep.is_file():
        try:
            d = json.loads(rep.read_text())
            dead = [k for k in (d.get("__dead__") or [])]
            risk = [k for k in (d.get("__at_risk__") or [])]
            vs.append(A.Verdict(
                "build.report", A.FAIL if (dead or risk) else A.PASS,
                work={"pairs": len(d.get("pairs") or d) if isinstance(d, dict)
                      else 0},
                # criteria_checked is what it EXAMINED, never what it
                # found. Counting findings made a clean build look
                # unfalsifiable, which would have cried wolf on every
                # good run — and a check that cries wolf gets ignored,
                # which is how the probe nearly failed to be useful.
                evidence={"problems": dead + risk,
                          "criteria_checked": len(
                              d.get("pairs") or d.get("fills") or [])
                          or sum(1 for _ in (d if isinstance(d, dict)
                                             else []))},
                artifact=site / "index.html", at=rep.stat().st_mtime))
        except Exception:
            pass

    cmf = root / "copy_map.json"
    if cmf.is_file():
        cm = json.loads(cmf.read_text(encoding="utf-8"))
        st = cm.get("strings") or []
        if st:
            unfilled = sum(1 for e in st if not e.get("new"))
            vs.append(A.Verdict(
                "rebrand.entry_scan", A.PASS,
                work={"entries": len(st)},
                measures={"template_remaining":
                          round(unfilled / len(st), 4)},
                evidence={"criteria_checked": len(st)},
                artifact=cmf, at=cmf.stat().st_mtime))

    # the word-overlap measure verify uses for the same property
    try:
        import difflib as _dl
        import html as _h
        tot = same = 0
        for f in sorted(site.glob("*.html")):
            pr = root / "pristine" / f.name
            if not pr.exists():
                continue

            def words(q):
                t = q.read_text(encoding="utf-8", errors="ignore")
                t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
                t = re.sub(r"(?is)url\([^)]*\)", " ", t)
                return _h.unescape(re.sub(r"<[^>]+>", " ", t)).split()
            a, b = words(pr), words(f)
            sm = _dl.SequenceMatcher(None, a, b)
            same += sum(bl.size for bl in sm.get_matching_blocks())
            tot += len(a)
        if tot:
            vs.append(A.Verdict(
                "verify.word_overlap", A.PASS,
                work={"pages": len(list(site.glob("*.html"))), "words": tot},
                measures={"template_remaining": round(same / tot, 4)},
                artifact=site / "index.html"))
    except Exception:
        pass

    r = A.trust(vs)
    print(f"── {len(vs)} verdict(s) audited")
    for v in vs:
        print(f"   {v.status:<8} {v.check:<24} work={v.work or '{}'}")
    print()
    if r["trustworthy"]:
        print("VERDICT: the checks can be trusted — every PASS was earned")
        return
    for f in r["findings"]:
        print(f.line())
        print()
    if r["downgraded"]:
        print("DOWNGRADED (their PASS is not accepted): "
              + ", ".join(r["downgraded"]))
    print("VERDICT: some checks did not earn their result — see above")
    sys.exit(2)


def cmd_adopt(argv):
    """Make a page Aethron did not build measurable, so it can be changed.

    Every measured path in this project reads `[data-ae-id]`, and Aethron stamped those
    only on pages it generated itself. Measured 2026-09-16 on this repo's own migrations:
    agero 3,655 renderable elements and ZERO ids, sadewa 4,605 and zero. So a user's own
    site was not "hard to change", it was unreadable — probe() returned no elements at all
    and every request stopped at "the page's canvas could not be read".

    This stamps stable ids into the SOURCE (so they survive every later render and rebuild)
    and refuses unless two things are PROVEN: the picture did not change, every pixel
    compared; and every id landed on the element it was picked for. The second check is not
    a formality — shuffling every id onto the wrong element changes zero pixels, so the
    pixel proof alone cannot see it.
    """
    import argparse
    import aethron_adopt as AD
    ap = argparse.ArgumentParser(prog="forge adopt")
    ap.add_argument("page", help="the .html file to make measurable")
    ap.add_argument("--width", type=int, default=1414)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--cap", type=int, default=AD.MAX_ELEMENTS,
                    help="most elements to name (the measure records every style of each)")
    ap.add_argument("--dry-run", action="store_true", help="measure and report, write nothing")
    a = ap.parse_args(argv)
    rep = AD.adopt(a.page, a.width, a.height, a.cap, write=not a.dry_run)
    AD.report(rep)
    return 0 if rep.get("verdict", "").startswith(("ADOPTED", "ALREADY")) else 1


def cmd_vision(argv):
    """Measure a screenshot before any model is allowed to guess at it.

    The point is the division of labour this project already runs on:
    the tool establishes physics, the model decides meaning. Asked to
    read a font size that breaks the surrounding pattern, multimodal
    models score 7.89%; a measurement of the ink's height either reads
    it or fails loudly. So every number a generator would otherwise
    hallucinate — colours, bounds, radii, spacing, type size — comes
    from here first.
    """
    if not argv or {"-h", "--help"} & set(argv):
        print("usage: forge vision <screenshot.png|jpg> [--json]")
        print("       Measures colours, bands, columns, solid regions,")
        print("       corner radii and text size. No model involved.")
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import aethron_vision
    except Exception as e:                       # pragma: no cover
        die(f"the measurement pass is unavailable in this build ({e})")
    sys.exit(aethron_vision.main(argv))


def cmd_screenshot(argv):
    """A screenshot becomes a page, with no model in the loop at all.

    THE DIVISION OF LABOUR, SETTLED BY MEASUREMENT. Handed one
    screenshot and the same measurements, a cheap model rebuilt 6 of the
    page's 21 lines correctly and put the testimonial where the button
    belongs; this path rebuilt all of them. Not because the model is bad
    at its job, but because it was being given the wrong job — reading a
    pixel — while the thing it is genuinely good at, deciding what the
    words should say, was never asked for.

    So nothing here guesses. The ground and its gradients are carried
    out of the screenshot, the rules are painted from their own pixels,
    the filled elements and their corners are measured, the words come
    from the OCR that ships with the machine, and anything that cannot
    be set as type is carried as a crop rather than approximated.

    `forge edit` is where a model comes in, afterwards, to change what
    the page SAYS and how it LOOKS — never where anything sits.
    """
    if not argv or {"-h", "--help"} & set(argv):
        print("usage: forge screenshot <image> <outdir> [--no-fit]")
        print("                        [--responsive]")
        print("                        [--framework html|astro|next|"
              "react|vue|svelte]")
        print("       Rebuilds the screenshot as one HTML file and")
        print("       checks every line of it. No model, no API key.")
        print("       With --responsive, also converts the measured")
        print("       layout into a flowing, full-screen site and")
        print("       proves it BOTH ways: the lines still land at the")
        print("       design width, and nothing spills at phone width.")
        print("       With --framework, also emits a real project in")
        print("       that framework and pixel-grades it against the")
        print("       page it came from.")
        return
    if len(argv) < 2:
        die("need a screenshot and an output directory")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import aethron_vision
    except Exception as e:                       # pragma: no cover
        die(f"the measurement pass is unavailable in this build ({e})")
    v = aethron_vision.rebuild(argv[0], argv[1],
                               fit="--no-fit" not in argv)
    if v["verdict"] == "SKIPPED":
        print("VERDICT: SKIPPED — " + v["why"])
        return
    print(f"\n{v['page']}")
    print("VERDICT: " + v["verdict"])
    # THE SECOND VERDICT, AND IT ASKS A DIFFERENT QUESTION. The first
    # says the pixels match. This one says whether what matched them is
    # a website — clickable, selectable, structured — or a picture of
    # one. A page can score 50 of 50 on content and still be inert.
    try:
        import aethron_web
        wr = aethron_web.audit_page(Path(v["page"]).read_text(), argv[0])
        print()
        aethron_web.report(wr)
    except Exception as e:                       # pragma: no cover
        print(f"(the website audit could not run: {e})")
    if "--responsive" in argv:
        # THE PAGE SO FAR IS A POSTER: every element absolutely placed on
        # a canvas of a fixed size, which is faithful and is not a
        # website. This converts the measured layout into real flow and
        # then has to earn it twice — the same line checker at the
        # design width, and no horizontal spill at phone width. A pass
        # on one alone is worthless: reflow with the layout thrown away
        # is easy, and so is fidelity that cannot move.
        import aethron_flow
        page = Path(v["page"])
        rf = aethron_flow.flow(page.read_text(), page.parent.name,
                               src=page)
        dest = Path(argv[1]) / "responsive"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text(rf["html"])
        if rf["assets"]:
            (dest / "assets").mkdir(exist_ok=True)
            for a in rf["assets"]:
                (dest / "assets" / a["file"]).write_bytes(a["bytes"])
        ver = aethron_flow.prove(dest / "index.html", argv[0],
                                 rf["canvas"])
        print(f"\n{dest / 'index.html'}")
        print("VERDICT: " + ver["verdict"] + " — " + ver["why"])

    if "--framework" in argv:
        import aethron_screen
        fw = argv[argv.index("--framework") + 1]
        if fw not in aethron_screen.EMITTERS:
            die(f"--framework must be one of "
                f"{sorted(aethron_screen.EMITTERS)}")
        out = Path(argv[1])
        page = Path(v["page"])
        ir = aethron_screen.page_ir(page.read_text(), out.name)
        dest = out / fw
        aethron_screen.EMITTERS[fw](ir, dest)
        print(f"\n{fw}: {len(ir['elements'])} element(s) in "
              f"{len(ir['sections'])} section(s), "
              f"{len(ir['assets'])} asset(s) -> {dest}")
        b = aethron_screen.build(dest, fw)
        if b["ok"] is None:
            print("VERDICT: SKIPPED — " + b["log"])
            return
        if not b["ok"]:
            print(f"the {fw} project failed at {b['stage']}:")
            print("\n".join(b["log"].splitlines()[-25:]))
            sys.exit(1)
        g = aethron_screen.grade(dest, fw, page.with_suffix(".png"),
                                 ir["canvas"])
        if g["ok"] is None:
            print("VERDICT: SKIPPED — " + g["why"])
            return
        print(("PASS " if g["ok"] else "FAIL ") + g["why"])
        sys.exit(0 if g["ok"] else 1)
    sys.exit(0 if v["verdict"] == "PASS" else 1)


def _carry_page_assets(page, dest, html):
    """A PAGE WRITTEN ELSEWHERE TAKES ITS PICTURES WITH IT. Relative images stayed behind,
    and the copied page showed a broken logo where the original showed the logo."""
    if dest.parent.resolve() == page.parent.resolve():
        return
    import shutil as _sh
    for src in set(re.findall(r'src="(?!https?:|data:|/)([^"]+)"', html)):
        a, b = page.parent / src, dest.parent / src
        if a.is_file() and not b.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy(a, b)


def cmd_edit(argv):
    """Change what a rebuilt page SAYS and how it LOOKS — never where.

    The guarded seam between a model's judgement and a measured page.
    An edit names an element by id and sets a property from a short
    allow-list; position, size and transform are refused outright,
    because those were read off the original's own pixels and a model
    reading sizes off an image is right about 8% of the time.

        forge edit page.html --list
        forge edit page.html --set t04 text "Start free"
        forge edit page.html --set f00 background "#B9FF66"
        forge edit page.html --apply edits.json --out new.html
        forge edit page.html --animate drift+breathe 10 visible --prove
        forge edit page.html --animate off
        forge edit page.html --ask "reduce the chat box height a little"

    --ask takes ANY change in plain words. The model writes the code and says
    what will be true afterwards; Aethron renders the result and measures every
    claim, holds the claims to the words (reduce = a measured decrease, "a
    little" is bounded, a named colour is measured), requires everything not
    named to be unchanged on screen, and leaves the page untouched if it fails.
    Pop-ups, dropdowns and hover or click effects are USED like a person uses
    them, and must not break the button they hang from or cover the one beside it.

    --animate STYLE PERIOD STRENGTH makes the page's fitted background move
    (drift, breathe or drift+breathe; seconds per cycle 4-120; strength
    0.05-1, or subtle / visible / strong, which Aethron reaches by
    rendering and measuring how much the sky actually changes). --prove renders it and measures that frame 0 is still the
    design and that it really moves.
    """
    if not argv or {"-h", "--help"} & set(argv):
        print(cmd_edit.__doc__)
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import aethron_edit as AE
    page = Path(argv[0])
    if not page.is_file():
        die(f"no such page: {page}")
    html = page.read_text()
    if "--list" in argv or len(argv) == 1:
        man = AE.manifest(html)
        print(f"{len(man['elements'])} element(s) on a "
              f"{man['canvas']['w']}x{man['canvas']['h']} page\n")
        for e in man["elements"]:
            bits = [f"{e['id']:>8}  {e['kind']:<8}"]
            if e.get("text"):
                bits.append(repr(e["text"][:44]))
            if e.get("background"):
                bits.append(e["background"][:24])
            if e.get("color"):
                bits.append(e["color"])
            print("  " + "  ".join(bits))
        return
    if "--ask" in argv:
        # ANY CHANGE, MEASURED. The model writes code; Aethron measures every claim it
        # makes, holds the claims to the words used, and undoes anything that fails.
        # TESTS FIRST, for requests nobody built a check for: the model writes tests that must
        # fail today, a reviewer checks them against the words, then code every test must pass.
        import aethron_spec as SP
        import tempfile as _tf
        words = argv[argv.index("--ask") + 1]
        budget = float(argv[argv.index("--budget") + 1]) if "--budget" in argv else 0.05
        dest = Path(argv[argv.index("--out") + 1]) if "--out" in argv else page
        work = Path(_tf.mkdtemp(prefix="ae-change-"))
        r = SP.build(html, words, work, budget_usd=budget)
        shutil.rmtree(work, ignore_errors=True)
        print(SP.report(r))
        led = r["ledger"]
        print(f"  spend: {led.get('calls', 0)} call(s), {led.get('free_calls', 0)} on free keys, "
              f"${led.get('usd', 0):.4f} charged of ${budget:.2f}")
        # THE RECORD OF WHAT WAS ASKED AND WHAT WAS DONE travels with the page, so anything
        # that reports on it reads the run's own output instead of a person retyping it.
        rep = {k: v for k, v in r.items() if k != "html"}
        rep.update({"request": words, "budget_usd": budget, "page": str(page)})
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.with_name(dest.stem + ".ask.json").write_text(json.dumps(rep, indent=1, default=str))
        if r["verdict"] == "APPLIED":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(r["html"])
            _carry_page_assets(page, dest, r["html"])
            print(f"  -> {dest}")
        sys.exit(0 if r["verdict"] == "APPLIED" else 1)
    edits = []
    if "--apply" in argv:
        edits = json.loads(Path(argv[argv.index("--apply") + 1]).read_text())
        edits = edits.get("edits", edits) if isinstance(edits, dict) else edits
    while "--animate" in argv:
        i = argv.index("--animate")
        ground = next((e["id"] for e in AE.manifest(html)["elements"]
                       if e["kind"] == "ground"), None)
        style = argv[i + 1]
        if style == "off":
            spec, n = {"style": "off"}, 2
        else:
            raw = argv[i + 3]
            try:
                strength = float(raw)
            except ValueError:
                strength = raw
            if isinstance(strength, str) and strength in AE.ALIVE_TARGETS:
                canvas = AE.manifest(html)["canvas"]
                strength, tried = AE.tune_alive(html, style, float(argv[i + 2]), raw,
                                                page.parent, canvas["w"], canvas["h"])
                print(f"motion tuned to '{raw}' by measuring: " + ", ".join(
                    f"strength {a} -> {b} levels" if b is not None else str(a) for a, b in tried)
                    + f"  => strength {strength}")
            spec, n = {"style": style, "period": float(argv[i + 2]),
                       "strength": strength}, 4
        edits.append({"id": ground, "animate": spec})
        del argv[i:i + n]
    while "--set" in argv:
        i = argv.index("--set")
        eid, prop, val = argv[i + 1], argv[i + 2], argv[i + 3]
        try:
            val = json.loads(val)
        except Exception:
            pass
        edits.append({"id": eid, "set": {prop: val}})
        del argv[i:i + 4]
    out, applied, refused = AE.apply(html, edits)
    for why in refused:
        print(f"  REFUSED  {why}")
    dest = Path(argv[argv.index("--out") + 1]) if "--out" in argv else page
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out)
    _carry_page_assets(page, dest, out)
    print(f"{len(applied)} applied, {len(refused)} refused -> {dest}")
    if "--prove" in argv and any("animate" in e for e in applied):
        canvas = AE.manifest(out)["canvas"]
        proof = AE.prove_alive(out, dest.parent, canvas["w"], canvas["h"])
        print(f"motion {proof['verdict']}: " + ", ".join(
            f"{k} {v}" for k, v in proof.items() if k != "verdict" and v != ""))
        if proof["verdict"] == "FAIL":
            sys.exit(1)
    sys.exit(1 if refused else 0)


def cmd_figma(argv):
    """Import a Figma design as a page — a new L0 SOURCE.

    Everything downstream is unchanged: this writes pages and assets,
    then inventory / rebrand / build / verify / probe take over exactly
    as they do for a scraped Framer or Webflow site. A source adapter
    cannot regress the pipeline because it does not touch it.
    """
    if not argv or {"-h", "--help"} & set(argv):
        print("usage: forge figma <figma-url> [--node 123:456] "
              "[--out DIR]\n"
              "       forge figma <url> --grade   (compare against "
              "Figma's own render)")
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import aethron_figma
    except Exception as e:
        die(f"the Figma importer is unavailable in this build ({e})")
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv \
        else Path.cwd() / "figma-page"
    node = argv[argv.index("--node") + 1] if "--node" in argv else None
    r = aethron_figma.convert(argv[0], out, node)
    print(json.dumps(r, indent=1))

    if "--grade" in argv:
        try:
            import aethron_figma_grade as grader
        except Exception as e:
            print(f"NOTE: grader unavailable ({e}) — the page is "
                  f"UNVERIFIED, not proven good")
            return
        truth = out / "figma_truth.png"
        if not truth.is_file():
            print("NOTE: no reference render on disk — "
                  "UNVERIFIED, not proven good")
            return
        g = grader.grade(out, truth)
        if g.get("ok") is False:
            sys.exit(2)


def cmd_convert(argv):
    """Port a built migration to a framework project.

    Lives in aethron_convert.py, but is dispatched from HERE so it
    reaches the user the same way every other step does: the Studio runs
    steps as `forge <cmd>` subprocesses, and inside the frozen app that
    becomes `Aethron --forge <cmd>` in-process. A capability the app
    cannot dispatch is a capability the owner does not have — the port
    was CLI-only and not even shipped in the bundle until this.

    Honest about its two external needs, because a check that cannot run
    reports SKIPPED, never PASS:
      * a headless browser, to read the original as it truly renders
      * node + npm, to install and build the emitted project
    """
    # Check for help ANYWHERE, not just first: `convert <project> --help`
    # is what a person types, and treating it as a project path starts a
    # real multi-minute port instead of printing one line. (Found by
    # doing exactly that.)
    if not argv or {"-h", "--help"} & set(argv):
        print("usage: forge convert <project> "
              "[--framework react|astro|next|vite]"
              "\n                            [--pages a.html,b.html] "
              "[--no-build]")
        return
    import shutil as _sh
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import aethron_convert
    except Exception as e:                       # pragma: no cover
        die(f"the framework porter is unavailable in this build ({e})")

    fw = "astro"
    if "--framework" in argv:
        fw = argv[argv.index("--framework") + 1]
    # "react" is the word a person types; Next.js is the React emitter.
    # Refusing the obvious name and printing a list is a small cruelty.
    fw = {"react": "next", "nextjs": "next", "next.js": "next"}.get(
        fw.strip().lower(), fw.strip().lower())
    if fw not in aethron_convert.FRAMEWORKS:
        die(f"--framework must be one of {aethron_convert.FRAMEWORKS}")
    pages = None
    if "--pages" in argv:
        pages = argv[argv.index("--pages") + 1].split(",")

    build = "--no-build" not in argv
    if build and not _sh.which("npm"):
        # SKIPPED, not a silent half-success: without npm we can emit the
        # project but never grade it, and an ungraded port is exactly the
        # thing the referee exists to refuse.
        print("NOTE: node/npm not found — emitting the project WITHOUT "
              "building or grading it.")
        print("      Install Node (nodejs.org), then re-run to get a "
              "verdict.")
        build = False

    res = aethron_convert.convert(Path(argv[0]).expanduser(), fw, pages,
                                  build=build)
    gaps = res.get("motion_gaps") or []
    print()
    if not res.get("ok"):
        print("NOT ACCEPTED: " + str(res.get("dir") or res.get("out") or ""))
        # SAY WHY. convert() already collected the toolchain's own words
        # into res["log"] and nothing printed them, so a failed install
        # or a build that would not compile reached the owner as a single
        # line naming a directory — ten minutes of work reported as a
        # shrug. The Next emitter shipped a guaranteed type error for
        # every Framer template and this is why nobody could see it.
        stage = res.get("stage")
        if stage:
            print(f"       it failed at: {stage}")
        log = (res.get("log") or "").strip()
        if log:
            print("       what the toolchain said:")
            for ln in log.splitlines()[-25:]:
                print("       | " + ln)
    elif gaps:
        print("CONTENT IDENTICAL, MOTION INCOMPLETE: "
              + str(res.get("out") or ""))
        for g in gaps[:12]:
            print("   " + g)
    else:
        print("PIXEL-PERFECT PORT READY: " + str(res.get("out") or ""))
    if res.get("verdict"):
        print(res["verdict"])
    if not res.get("ok"):
        sys.exit(2)


COMMANDS = {"init": cmd_init, "fetch": cmd_fetch, "inventory": cmd_inventory,
            "convert": cmd_convert, "figma": cmd_figma,
            "vision": cmd_vision, "screenshot": cmd_screenshot,
            "adopt": cmd_adopt,
            "edit": cmd_edit,
            "audit": cmd_audit,
            "build": cmd_build, "logo": cmd_logo, "backend": cmd_backend,
            "localize": cmd_localize, "capture": cmd_capture,
            "serve": cmd_serve, "probe": cmd_probe,
            "verify": cmd_verify, "card": cmd_card, "heal": cmd_heal,
            "rebrand": _cmd_rebrand}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])

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
  forge.py localize     download every CDN asset under brand-free names
  forge.py card         design fingerprint (palette/fonts/motion) ->
                        design_card.json — feeds the studio's library
  forge.py heal         SELF-HEAL broken fills from the last report:
                        flex/casing/nearest-source adoption — never a
                        guess; prints HEALED/STUCK per entry

Read PLAYBOOK.md for the model-facing workflow.
"""
import concurrent.futures as cf
import html as html_mod
import json
import re
import shutil
import struct
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (TemplateForge/1.0)"}
CHUNK_NAME_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{6,12}\.mjs")

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


def download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            dest.write_bytes(r.read())
        return True
    except Exception:
        return False


def download_many(urls_dests, label):
    ok = fail = 0
    with cf.ThreadPoolExecutor(8) as ex:
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
    def get(u):
        req = urllib.request.Request(u, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "ignore")

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
    for path in sorted(routes)[:14]:          # cap: home + 14 subpages
        try:
            pages[path] = get(base + "/" + path)
            print(f"  scraped /{path}")
        except Exception:
            print(f"  skipped /{path} (fetch failed)")
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
    print(f"Next: cd {name} && python3 {Path(__file__).name} fetch")


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
    bases = sorted({m.group(0) for t in html_texts for m in re.finditer(
        r"https://framerusercontent\.com/sites/[^/\"'\s]+/", t)})
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
            refs |= set(CHUNK_NAME_RE.findall(p.read_text(encoding="utf-8",
                                                          errors="ignore")))
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
            for lst in re.findall(r"`([a-z0-9.-]{200,})`", t):
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
        if " " in s and not in_cms \
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
                for e in copy_map[sec]:
                    if e["old"] in pnew:
                        e["new"] = pnew[e["old"]]
                        kept += 1
            for sec in ("styles", "remove"):
                if prev.get(sec):
                    copy_map[sec] = prev[sec]
            if kept or prev.get("styles") or prev.get("remove"):
                print(f"preserved {kept} existing fill(s)"
                      + (" + styles/removals" if prev.get("styles")
                         or prev.get("remove") else ""))
        except Exception:
            pass

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
        if in_cms:
            if len(new_b) > len(old_b):
                die(f"CMS budget exceeded for {old[:40]!r}: "
                    f"{len(new_b)} > {len(old_b)} bytes. Shorten the text.")
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
                      "flex": bool(entry.get("flex"))})
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
        old, new = p["old"], p["new"]
        if escaped:
            old = html_mod.escape(old, quote=False)
            new = html_mod.escape(new, quote=False)
        if old in lookup:
            continue
        pat = re.escape(old)
        if re.fullmatch(r"\w+", old, re.A):
            pat = rf"\b{pat}\b"
        parts.append(pat)
        lookup[old] = (new, p["old"])       # (replacement, original old)
    if not parts:
        return None, None
    return re.compile("|".join(parts)), lookup


def _flex_pat(old):
    """Whitespace/entity-tolerant pattern: matches the text however the
    export wrapped it (newlines, indent, &nbsp;, \\xa0) — including the
    ESCAPED forms (\\n, \\t) that appear inside minified chunk strings,
    which is where client-side route pages keep their copy."""
    ws = r"(?:\s|&nbsp;|\xa0|\\n|\\t)+"
    return ws.join(re.escape(w) for w in old.split())


# Webflow asset filenames EMBED the brand (kitpro-cyntra…min.css) —
# rewriting them points at CDN files that don't exist and kills the
# stylesheet. Shield template-CDN urls from all replacement unless the
# owner deliberately retargeted that exact asset (it's a pair old).
CDN_URL_RE = re.compile(
    r"https://[a-z0-9.-]*(?:website-files\.com|framerusercontent\.com)"
    r"[^\"'\s()<>`]+")  # backtick ends template-literal urls in chunks


def _apply(text, pairs, escaped=False, stats=None):
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
    rx, lookup = _rx(pairs, escaped=False)
    if rx:
        def repl(m):
            new, orig = lookup[m.group(0)]
            bump(orig)
            return new
        text = rx.sub(repl, text)
    # editor-picked entries carry the DOM-normalized text, which may
    # differ from the source's whitespace — match those flexibly
    for p in pairs:
        if p.get("flex") and p["scope"] != "cms" and " " in p["old"]:
            text, n = re.subn(_flex_pat(p["old"]),
                              p["new"].replace("\\", "\\\\"), text)
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

    if changed:
        (root / "copy_map.json").write_text(
            json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")
    for o, why in healed:
        print(f"HEALED: {o[:60]!r} -> {why}")
    for o, why in stuck:
        print(f"STUCK:  {o[:60]!r} -> {why}")
    if not healed and not stuck and not dropped:
        print("nothing to heal — no broken fills in the last report")
    print(f"heal: {len(healed) + len(dropped)} fixed, "
          f"{len(stuck)} need the owner"
          + (" — rebuild to apply" if changed or dropped else ""))


def _write_deploy(site: Path, cfg: dict):
    """Every build ships deploy-ready: the output is fully static
    (client-side CMS range slicing, real .js icon names), so the only
    host-side need left is the SPA fallback for deep routes — covered
    per host family below."""
    # Netlify + Cloudflare Pages: shadow fallback (real files win)
    (site / "_redirects").write_text("/* /index.html 200\n",
                                     encoding="utf-8")
    # GitHub Pages & friends: 404 page = the app shell (client routes)
    if (site / "index.html").exists():
        shutil.copy(site / "index.html", site / "404.html")
    # Vercel: filesystem wins, extension-less routes fall back
    (site / "vercel.json").write_text(json.dumps({"rewrites": [
        {"source": "/((?!.*\\.).*)", "destination": "/index.html"}]},
        indent=1), encoding="utf-8")
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
    if site.exists():
        shutil.rmtree(site)
    (site / "assets").mkdir(parents=True)

    cms_src = root / "pristine" / "cms"
    cms_blobs = [p.read_bytes() for p in cms_src.glob("*.framercms")] \
        if cms_src.exists() else []
    pairs = _pairs_from_map(root, cms_blobs)
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

        # scattered saves link pages absolutely to the LIVE site
        # (https://foo.webflow.io/about) or root-relative (/about) —
        # point them at the local page files instead
        def _localize(m):
            path, tail = m.group("p").strip("/"), m.group("t")
            tgt = routes.get(path)
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

        # owner-requested element removals (visual editor's 🗑) —
        # physically deleted from the code. Framer projects route
        # removals to hide_selectors instead (React re-creates DOM).
        for rm in removals:
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
        dest = site / page
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(t, encoding="utf-8")

    # non-page files from multi-file exports (Webflow css/js/images…):
    # pass through with copy pairs applied to text assets. Framer runtime
    # dirs (chunks/cms/icons) and downloaded fonts are handled separately.
    reserved = {"chunks", "cms", "icons", "fonts"}
    page_set = set(cfg["pages"])
    text_ext = (".css", ".js", ".txt", ".xml", ".json", ".svg", ".webmanifest")
    for f in (root / "pristine").rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root / "pristine")
        if rel.parts[0] in reserved or str(rel) in page_set:
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
        n_patched = n_static = 0
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
            # icon modules are import()ed — name.js@1.2.3 gets a wrong
            # MIME on static hosts, which module scripts hard-reject.
            # Ship them as name.1.2.3.js instead (copy is renamed below)
            t = re.sub(r"\.js@([0-9.]+)", r".\1.js", t)
            t = _apply(t, pairs, stats=stats)
            t = _localize_refs(t, cfg.get("localized", {}))
            (cdir / p.name).write_text(t, encoding="utf-8")
            n_patched += t != orig
        log(f"chunks: {n_patched} patched "
            f"({n_static} static-host range guard(s)), "
            f"{len(list(chunks_src.glob('*.mjs')))} total")

    # CMS binaries: exact-byte-length padded replacement (offsets are law)
    if cms_src.exists() and list(cms_src.glob("*.framercms")):
        mdir = site / "assets" / "cms"
        mdir.mkdir()
        # one-pass, longest-first, \b-guarded exactly like _apply —
        # hydration equality demands identical semantics in every layer
        bparts, blookup = [], {}
        for pr in pairs:
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
        in_pristine = po in pristine_txt or any(pb in b for b in pristine_cms)
        in_built = po in built_txt or any(pb in b for b in built_cms)
        if (in_pristine or po in token_olds) and not in_built:
            moot.append(po)
    report = dict(stats)
    if at_risk:
        report["__at_risk__"] = at_risk
    if moot:
        report["__moot__"] = moot
    (site / ".forge-report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    zeros = [o for o, n in stats.items() if n == 0 and o not in set(moot)]
    if zeros:
        log(f"WARNING: {len(zeros)} filled entr(ies) replaced NOTHING "
            f"(first: {zeros[0][:50]!r}) — see site/.forge-report.json")

    # ship the runner + a README so the folder is self-explanatory to
    # any human or AI that receives it
    (site / "serve.py").write_text(SERVE_PY, encoding="utf-8")
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
        f = Path(self.translate_path(u.path))
        if not f.exists() and "." not in Path(u.path).name:
            self.path = "/index.html"
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
REMOTE_ASSET_RE = re.compile(
    r"https://(?:[a-z0-9.-]*website-files\.com|framerusercontent\.com"
    r"|fonts\.gstatic\.com)/[^\"'\s<>`\\]+")


def _local_name(url: str) -> str:
    import hashlib
    base = url.split("?")[0].split("#")[0]
    ext = ""
    tail = base.rsplit("/", 1)[-1]
    if "." in tail:
        ext = "." + tail.rsplit(".", 1)[-1][:8]
    return hashlib.sha1(base.encode()).hexdigest()[:12] + ext


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
            if "/sites/" in base or base.endswith((".mjs", ".framercms")):
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
2. Run `python3 forge.py heal` (MCP: the heal tool). Deterministic
   ladder: whitespace-flexible matching, source-casing adoption,
   nearest-source-string adoption. It prints HEALED/STUCK per entry.
3. Rebuild. Re-check the report.
4. Still STUCK? The reason line says why (usually: the text you
   targeted doesn't exist in the source, or your replacement is over
   a CMS byte budget). Fix the ENTRY (copy_map.json via the content
   API / set_content) — adjust "old" to the printed closest candidate
   or shorten "new" — and rebuild. Never work around the pipeline.

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

def cmd_serve(args):
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse
    root = Path.cwd()
    read_cfg(root)
    port = int(args[0]) if args else 8777
    site = root / "site"
    if not site.exists():
        die("no site/ — run build first")

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(site), **kw)

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
            # SPA fallback: extension-less paths are client-side routes
            # (about, works, …) — the runtime router renders them from
            # index.html. Replicate on production servers too.
            f = Path(self.translate_path(u.path))
            if not f.exists() and "." not in Path(u.path).name:
                self.path = "/index.html"
            return super().do_GET()

    print(f"Serving site/ at http://localhost:{port}/  (Ctrl-C to stop)")
    ThreadingHTTPServer(("", port), H).serve_forever()


# ─────────────────────────── verify ──────────────────────────────────

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
    for w in words:
        hits, url_only = [], []
        for f in site.rglob("*"):
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
        if hits:
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

    # 2. every locally-referenced chunk exists
    for page in cfg["pages"]:
        t = (site / page).read_text(encoding="utf-8", errors="ignore")
        missing = [n for n in re.findall(r"\./assets/chunks/([^\"'\s)]+\.mjs)", t)
                   if not (site / "assets" / "chunks" / n).exists()]
        if missing:
            print(f"FAIL {page}: missing chunks {missing[:5]}")
            fails += 1
        else:
            print(f"PASS {page}: all referenced chunks present")
        remote = re.findall(r"https://(?:events\.framer\.com|framer\.com/edit)[^\"']*", t)
        print(("FAIL" if remote else "PASS") + f" {page}: framer telemetry refs: {len(remote)}")
        fails += bool(remote)

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

    print("\nVERDICT:", "CLEAN — open it in a browser and run the human checklist"
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


COMMANDS = {"init": cmd_init, "fetch": cmd_fetch, "inventory": cmd_inventory,
            "build": cmd_build, "logo": cmd_logo, "backend": cmd_backend,
            "localize": cmd_localize, "serve": cmd_serve,
            "verify": cmd_verify, "card": cmd_card, "heal": cmd_heal}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])

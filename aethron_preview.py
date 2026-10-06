#!/usr/bin/env python3
"""Personalised previews from one migrated template — cheap per lead.

    make_preview(template, name, pages, base)   copy a prepared template
    page_lines(project)                         the lines on the chosen pages
    add_block(project, kind, anchor, ...)       a map / reviews / your own
                                                section placed next to text
    export_preview(project, out)                the chosen pages, built to
                                                live at `base` (e.g.
                                                /preview/<slug>), with the
                                                preview layer

WHY IT IS SHAPED THIS WAY. A lead agent pays for TOKENS, and tokens go on
reading and writing copy — fetching and building cost nothing. So the
template is migrated ONCE (create_project, fetch, inventory) and each lead
gets a copy in seconds, with only the chosen pages' lines handed to the
agent: a few dozen, not the template's hundreds.

THE PREVIEW LAYER sits OUTSIDE the template's own app, so the page's code
cannot wipe it. Framer switches pages in the browser (a click on "About"
never loads about.html), so a page that was not built would still appear,
full of template text: the layer catches every link click before Framer's
router does — a built page loads properly, any other shows the notice.

EXTRAS (map, reviews, a custom section) are placed next to an element
found by its words, take their type and colours from that element, and are
put back if the page's code removes them. They are new content, not part
of the template's design, and their data comes from the caller: nothing
here invents a review.
"""
import html as html_mod
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULT_NOTICE = ("This is a preview. The full site — this page included — "
                  "is ready once we build it for you.")


def _cfg(d: Path) -> dict:
    return json.loads((d / "forge.json").read_text(encoding="utf-8"))


def _save_cfg(d: Path, cfg: dict):
    (d / "forge.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _route(page: str) -> str:
    """about.html -> about; index.html -> '' (the home page)."""
    r = page[:-5] if page.endswith(".html") else page
    return "" if r == "index" else r


def resolve_pages(cfg: dict, wanted) -> list:
    """Page names a person or agent would use -> the project's page files.
    'home'/'index'/'/' mean the home page; 'about', '/about', 'about.html'
    all mean about.html. Unknown names are an error that lists what exists,
    never a silent skip."""
    have = ["index.html"] + [p for p in cfg.get("pages", [])
                             if p != "index.html"]
    routes = cfg.get("routes") or []
    out, missing = [], []
    for w in wanted or ["home"]:
        k = str(w).strip().lower().strip("/")
        k = k[:-5] if k.endswith(".html") else k
        if k in ("", "home", "index"):
            hit = "index.html"
        else:
            hit = next((p for p in have if _route(p) == k), None)
            if not hit and k in routes:     # a nested route: blog/post
                hit = re.sub(r"[^\w-]+", "-", k).strip("-") + ".html"
                hit = hit if hit in have else None
        (out.append(hit) if hit else missing.append(str(w)))
    if missing:
        raise ValueError(f"no page called {', '.join(missing)} — this "
                         f"template has: home, " + ", ".join(
                             _route(p) for p in have if p != "index.html"))
    return list(dict.fromkeys(out))


def make_preview(template_dir: Path, dest: Path, pages, base: str,
                 notice: str = "") -> dict:
    """Copy a PREPARED template project (fetched + inventoried) for one
    lead. The copy keeps the original and its downloads; what was built or
    undone in the template stays behind."""
    template_dir, dest = Path(template_dir), Path(dest)
    if not (template_dir / "copy_map.json").exists():
        raise ValueError(f"{template_dir.name} is not ready to copy: run "
                         "fetch and inventory on it first (once)")
    if dest.exists():
        raise ValueError(f"a project called {dest.name} already exists")
    tcfg = _cfg(template_dir)
    chosen = resolve_pages(tcfg, pages)
    base = "/" + base.strip("/")
    # pristine/ is SEALED and never modified, so every lead's copy can
    # share its bytes (a hard link) instead of duplicating a template's
    # tens of megabytes per lead; anything else is copied
    def _share(src, dst):
        # …except the seal manifest, which `fetch` rewrites in place: a
        # shared one would re-seal the TEMPLATE from a lead's copy
        if "pristine" in Path(src).relative_to(template_dir).parts \
                and not Path(src).name.startswith(".forge-"):
            try:
                return os.link(src, dst)
            except OSError:
                pass
        return shutil.copy2(src, dst)
    # ONLY what a build reads. A template project also holds its framework
    # ports (convert-*/export-*, with node_modules): measured on agero,
    # 30,000 files and 17 of the 18 seconds a preview took to make.
    KEEP = {"pristine", "assets", "copy_map.json", "forge.json",
            "project_plan.md", "animations.json"}

    def _only_build_inputs(where, names):
        return [] if Path(where) != template_dir else \
            [n for n in names if n not in KEEP]
    shutil.copytree(template_dir, dest, copy_function=_share,
                    ignore=_only_build_inputs)
    cfg = _cfg(dest)
    cfg["name"] = dest.name
    # the build points the runtime's own asset fetches here (CMS, icons)
    cfg["public_base"] = base + "/assets"
    cfg["preview"] = {"template": template_dir.name, "pages": chosen,
                      "base": base, "notice": notice or DEFAULT_NOTICE,
                      "blocks": []}
    _save_cfg(dest, cfg)
    lines = page_lines(dest)
    return {"project": dest.name, "pages": chosen, "base": base,
            "lines": len(lines), "of": _count_all(dest)}


def _count_all(d: Path) -> int:
    cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
    return sum(len(cm.get(s, [])) for s in ("strings", "images", "links"))


def page_lines(d: Path, pages=None) -> set:
    """The `old` of every copy line that appears on the chosen pages.

    A line belongs to a page when its words (or its address, for images
    and links) are in that page's original HTML. Shared chrome — nav,
    footer — is on every page and so always included. A word of an
    animated heading is never a line (see forge.word_fragments)."""
    import forge
    d = Path(d)
    cfg = _cfg(d)
    pages = pages or (cfg.get("preview") or {}).get("pages")
    if not pages:
        return set()
    cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
    frag = forge.word_fragments(d, cm)
    raw = "\n".join((d / "pristine" / p).read_text(encoding="utf-8",
                                                    errors="ignore")
                    for p in pages if (d / "pristine" / p).exists())
    text = html_mod.unescape(re.sub(r"\s+", " ", raw))
    out = set()
    for sec in ("strings", "images", "links"):
        for e in cm.get(sec, []):
            o = e["old"]
            if sec == "strings" and o in frag:
                continue
            if o in raw or re.sub(r"\s+", " ", o) in text:
                out.add(o)
    return out


# ───────────────────────────── extras ──────────────────────────────────

KINDS = ("map", "reviews", "html")


def add_block(d: Path, kind: str, anchor: str, position: str = "after",
              data=None, page: str = "") -> dict:
    """Record an extra section; export_preview renders it.

    kind     map (data: {"query": "address or place name", "title"?}),
             reviews (data: {"rating"?: 4.8, "count"?: 120, "source"?:
             "Google", "items": [{"author", "text", "stars"?}]}),
             html (data: {"html": "<section>…</section>"}).
    anchor   words on the page; the block goes before/after the SECTION
             that holds them.
    page     which chosen page it goes on (default: every chosen page the
             anchor words appear on).
    """
    d = Path(d)
    cfg = _cfg(d)
    if "preview" not in cfg:
        raise ValueError("extras are for previews — make one with "
                         "make_preview first")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if position not in ("before", "after"):
        raise ValueError("position must be before or after")
    data = dict(data or {})
    if kind == "map" and not str(data.get("query") or "").strip():
        raise ValueError("a map needs data.query — an address or place name")
    if kind == "reviews":
        items = [i for i in data.get("items") or []
                 if str((i or {}).get("text") or "").strip()]
        if not items:
            raise ValueError("reviews need data.items with the real review "
                             "text — nothing is invented here")
        data["items"] = items[:12]
    if kind == "html":
        h = str(data.get("html") or "")
        if not h.strip():
            raise ValueError("an html block needs data.html")
        if re.search(r"<\s*script|\son\w+\s*=|javascript:", h, re.I):
            raise ValueError("an html block cannot carry scripts or event "
                             "handlers")
    pages = cfg["preview"]["pages"]
    if page:
        pages = resolve_pages(cfg, [page])
        if pages[0] not in cfg["preview"]["pages"]:
            raise ValueError(f"{page!r} is not one of this preview's pages")
    found = [p for p in pages if anchor and anchor.lower() in html_mod.unescape(
        (d / "pristine" / p).read_text(encoding="utf-8", errors="ignore")).lower()]
    if not found:
        raise ValueError(f"the words {anchor!r} are not on the chosen pages "
                         "— use words that appear in the section to sit "
                         "next to (the template's original words)")
    block = {"id": f"b{len(cfg['preview']['blocks']) + 1}", "kind": kind,
             "anchor": anchor, "position": position, "data": data,
             "pages": found}
    cfg["preview"]["blocks"].append(block)
    _save_cfg(d, cfg)
    return block


# ─────────────────────────── the layer ─────────────────────────────────

LAYER_CSS = """
#ae-pv-note{position:fixed;inset:0;z-index:2147483646;display:none;
 align-items:center;justify-content:center;background:rgba(10,10,12,.46);
 -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
#ae-pv-note.on{display:flex}
#ae-pv-note .c{max-width:380px;margin:20px;padding:24px 24px 20px;
 border-radius:16px;background:#fff;color:#16161a;
 box-shadow:0 20px 60px rgba(0,0,0,.28)}
#ae-pv-note b{display:block;font-size:17px;margin:0 0 6px}
#ae-pv-note p{margin:0 0 16px;color:#4a4a52}
#ae-pv-note button{border:0;border-radius:10px;padding:10px 16px;
 background:#16161a;color:#fff;font:600 14px/1 inherit;cursor:pointer}
#ae-pv-tag{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);
 z-index:2147483645;padding:7px 13px;border-radius:99px;
 background:rgba(22,22,26,.86);color:#fff;
 font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
 letter-spacing:.02em;pointer-events:none}
.ae-pv-block{box-sizing:border-box;width:100%;padding:56px 20px}
.ae-pv-block .in{max-width:1080px;margin:0 auto}
.ae-pv-block h2{margin:0 0 20px;font-size:clamp(24px,3vw,36px);line-height:1.15}
.ae-pv-map iframe{width:100%;height:380px;border:0;border-radius:16px}
.ae-pv-rev .sum{margin:0 0 20px;opacity:.75}
.ae-pv-rev .grid{display:grid;gap:14px;
 grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.ae-pv-rev .r{padding:18px;border-radius:14px;border:1px solid rgba(127,127,127,.25)}
.ae-pv-rev .st{letter-spacing:2px;margin:0 0 8px}
.ae-pv-rev .who{margin:10px 0 0;font-weight:600;opacity:.8}
"""

LAYER_JS = r"""
(function(){
 var C=window.__AE_PREVIEW__; if(!C) return;
 var base=C.base.replace(/\/$/,''), here=location.origin;
 function routeOf(u){ // a URL on this site -> the page route it names
   var p=u.pathname; if(p.indexOf(base)===0) p=p.slice(base.length);
   p=p.replace(/^\/+|\/+$/g,'').replace(/\.html$/,'');
   return p==='index'?'':p; }
 var note=document.createElement('div'); note.id='ae-pv-note';
 note.innerHTML='<div class="c" role="dialog" aria-modal="true"><b></b><p></p><button type="button">OK</button></div>';
 note.querySelector('b').textContent=C.title||'Preview mode';
 note.querySelector('p').textContent=C.notice;
 note.addEventListener('click',function(e){ if(e.target===note||e.target.tagName==='BUTTON') note.className=''; });
 document.addEventListener('keydown',function(e){ if(e.key==='Escape') note.className=''; });
 var tag=document.createElement('div'); tag.id='ae-pv-tag'; tag.textContent=C.tag||'Preview';
 function mount(){ if(!note.isConnected) document.body.appendChild(note);
                   if(C.tag!==false && !tag.isConnected) document.body.appendChild(tag); }
 // CAPTURE ON WINDOW: runs before the template's own router sees the click
 window.addEventListener('click',function(e){
   if(e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey) return;
   var a=e.target&&e.target.closest&&e.target.closest('a[href]'); if(!a) return;
   var u; try{ u=new URL(a.getAttribute('href'),location.href); }catch(_){ return; }
   if(u.origin!==here) return;                       // other sites: leave
   if(!/^https?:$/.test(u.protocol)) return;          // mailto:, tel:
   var r=routeOf(u);
   if(r===routeOf(location) && u.hash) return;        // #section on this page
   e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
   if(C.pages.hasOwnProperty(r)){ location.assign(base+'/'+C.pages[r]+u.hash); return; }
   mount(); note.className='on';
 },true);
 // EXTRAS, placed next to the section holding their anchor words, and put
 // back if the page's own code removes them (Framer re-renders its tree)
 function findAnchor(words){
   words=words.toLowerCase();
   var w=document.createTreeWalker(document.body,NodeFilter.SHOW_ELEMENT), n, best=null;
   while((n=w.nextNode())){ if(n.closest('.ae-pv-block,#ae-pv-note')) continue;
     var t=(n.innerText||'').toLowerCase();
     if(t.indexOf(words)>=0) best=n; }
   if(!best) return null;
   var s=best; // climb to the section that holds the words
   while(s.parentElement && s.parentElement!==document.body){
     var p=s.parentElement, r=p.getBoundingClientRect();
     if(r.width>=document.documentElement.clientWidth*0.9 && p.children.length>1 && p.querySelectorAll('h1,h2,h3').length>1) break;
     s=p; }
   return s; }
 function styleFrom(el){
   var h=el.querySelector('h1,h2,h3')||el, b=el.querySelector('p')||el;
   var hs=getComputedStyle(h), bs=getComputedStyle(b);
   return {hf:hs.fontFamily, hc:hs.color, hw:hs.fontWeight, bf:bs.fontFamily, bc:bs.color}; }
 function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
 function render(b,st){
   var el=document.createElement('section'); el.className='ae-pv-block ae-pv-'+(b.kind==='reviews'?'rev':b.kind);
   el.setAttribute('data-ae-block',b.id); var d=b.data||{}, h='';
   if(b.kind==='map'){
     h='<div class="in">'+(d.title?'<h2>'+esc(d.title)+'</h2>':'')+
       '<iframe loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="Map" src="https://www.google.com/maps?q='+encodeURIComponent(d.query)+'&output=embed"></iframe></div>';
   } else if(b.kind==='reviews'){
     var stars=function(n){ n=Math.round(+n||0); return n?'★★★★★'.slice(0,n)+'☆☆☆☆☆'.slice(0,5-n):''; };
     h='<div class="in"><h2>'+esc(d.title||'What customers say')+'</h2>'+
       ((d.rating||d.count)?'<p class="sum">'+(d.rating?esc(d.rating)+' ★ ':'')+(d.count?'from '+esc(d.count)+' reviews':'')+(d.source?' on '+esc(d.source):'')+'</p>':'')+
       '<div class="grid">'+d.items.map(function(i){ return '<div class="r">'+(i.stars?'<p class="st">'+stars(i.stars)+'</p>':'')+'<p>'+esc(i.text)+'</p>'+(i.author?'<p class="who">'+esc(i.author)+'</p>':'')+'</div>'; }).join('')+'</div></div>';
   } else { h=d.html; }
   el.innerHTML=h;
   el.querySelectorAll('h1,h2,h3').forEach(function(x){ x.style.fontFamily=st.hf; x.style.color=st.hc; x.style.fontWeight=st.hw; });
   el.style.fontFamily=st.bf; el.style.color=st.bc;
   return el; }
 function place(){
   var mine=C.pages[routeOf(location)]||'index.html';
   (C.blocks||[]).forEach(function(b){
     if(b.pages && b.pages.indexOf(mine)<0) return;   // another page's extra
     if(document.querySelector('[data-ae-block="'+b.id+'"]')) return;
     var s=findAnchor(b.anchor); if(!s) return;
     var el=render(b,styleFrom(s));
     s.parentNode.insertBefore(el, b.position==='before'?s:s.nextSibling); }); }
 function go(){ mount(); place();
   new MutationObserver(function(){ clearTimeout(go.t); go.t=setTimeout(place,120); })
     .observe(document.body,{childList:true,subtree:true}); }
 if(document.readyState==='complete') setTimeout(go,300);
 else window.addEventListener('load',function(){ setTimeout(go,300); });
})();
"""


def _page_keys(cfg: dict, page: str) -> list:
    """Every route a link to this page can show up as: its file stem
    (about, blog-why-x — what the build's links use) and, for a nested
    page, its real address (blog/why-x — what the template's router uses)."""
    keys = [_route(page)]
    for r in cfg.get("routes") or []:
        if re.sub(r"[^\w-]+", "-", r).strip("-") + ".html" == page:
            keys.append(r.strip("/"))
    return list(dict.fromkeys(keys))


def _layer(cfg: dict) -> str:
    pv = cfg["preview"]
    conf = {"base": pv["base"], "notice": pv.get("notice") or DEFAULT_NOTICE,
            "title": pv.get("title") or "Preview mode",
            "tag": pv.get("tag", "Preview"),
            "pages": {k: p for p in pv["pages"] for k in _page_keys(cfg, p)},
            "blocks": pv.get("blocks", [])}
    # JSON inside <script>: "</" must not close the tag early
    blob = json.dumps(conf).replace("</", "<\\/")
    return (f"<style data-ae-preview>{LAYER_CSS}</style>"
            f"<script data-ae-preview>window.__AE_PREVIEW__={blob};"
            f"{LAYER_JS}</script>")


def export_preview(d: Path, out: Path) -> dict:
    """Write the chosen pages, built, to `out` so that `out` is served at
    the preview's base address. Builds first (the build already points the
    runtime's own fetches at base/assets). Pages refer to their files as
    ./assets/…, which only resolves when the address ends in '/'; a
    preview address usually does not, so those become absolute here."""
    import subprocess
    d, out = Path(d), Path(out)
    cfg = _cfg(d)
    if "preview" not in cfg:
        raise ValueError("not a preview project — make one with make_preview")
    pv = cfg["preview"]
    base = pv["base"]
    cmd = [sys.executable, str(ROOT / "forge.py"), "build"]
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--forge", "build"]
    r = subprocess.run(cmd, cwd=d, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("build failed:\n" + (r.stdout + r.stderr)[-1500:])
    site = d / "site"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    pages = {p: _route(p) for p in pv["pages"]}
    for name in site.iterdir():
        if name.suffix == ".html" or name.name.startswith("."):
            continue                       # pages: only the chosen ones
        if name.name in ("serve.py", "README.txt", "DEPLOY.md", "_redirects",
                         "vercel.json", "sources"):
            continue                       # sources/: the template's authored
        dst = out / name.name              # code, recovered for reading only
        (shutil.copytree if name.is_dir() else shutil.copy2)(name, dst)
    stuck = _rebase_assets(out, base)
    layer = _layer(cfg)
    for page in pages:
        t = (site / page).read_text(encoding="utf-8")
        for q in ('"', "'", "(", " ", ","):
            # relative refs, and any absolute /assets/ ref the build left —
            # neither resolves under …/preview/<slug> with no trailing '/'
            t = t.replace(q + "./assets/", q + base + "/assets/")
            t = t.replace(q + "/assets/", q + base + "/assets/")
        # links between the chosen pages, and the home link, by address
        for p in pages:
            for k in _page_keys(cfg, p):
                for href in (f'"./{p}"', f'"./{k}"' if k else '"./"'):
                    t = t.replace("href=" + href, f'href="{base}/{p}"')
        t = t.replace("</body>", layer + "</body>", 1)
        (out / page).write_text(t, encoding="utf-8")
    # the address itself (…/preview/<slug>) serves the home page
    home = "index.html" if "index.html" in pages else next(iter(pages))
    return {"out": str(out), "base": base, "pages": list(pages),
            "home": home, "blocks": len(pv.get("blocks", [])),
            "unmoved": stuck}


# A LOCALIZED FILE IS ADDRESSED FROM THE SITE'S ROOT. The build writes the
# template's downloaded images as "/assets/r/<file>" into pages, code and
# CMS data alike; at /preview/<slug> that root is the jomiez site's, not the
# preview's, so every one of them 404s (measured on agero: 16 images over
# two pages). Text files take the base in front. CMS data is BYTE-LOCKED,
# and the build padded each local address with "#000…" to the length of
# the remote one it replaced — so the base is paid for out of that padding,
# and the blob keeps its exact length.
_TEXT_ASSET = (".mjs", ".js", ".css", ".json", ".svg", ".txt")
_LEAD = ("\"", "'", "`", "(", " ", ",", "=")


def _rebase_assets(out: Path, base: str) -> int:
    stuck = 0
    for f in (out / "assets").rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() in _TEXT_ASSET:
            t = f.read_text(encoding="utf-8", errors="surrogateescape")
            n = t
            for q in _LEAD:
                n = n.replace(q + "/assets/", q + base + "/assets/")
            if n != t:
                f.write_text(n, encoding="utf-8", errors="surrogateescape")
        elif f.suffix == ".framercms":
            b = f.read_bytes()
            add = len(base.encode())

            def fit(m):
                nonlocal stuck
                pad = len(m.group(2))
                if pad < add:
                    stuck += 1         # no room: left as it was, and the
                    return m.group(0)  # browser check will name it
                left = pad - add
                return (base.encode() + m.group(1)
                        + (b"#" + b"0" * (left - 1) if left else b""))
            nb = re.sub(rb"(/assets/r/[\w.@-]+(?:\?[\w=&.%-]*)?)((?:#0*)?)",
                        fit, b)
            if nb != b:
                assert len(nb) == len(b), "CMS length changed"
                f.write_bytes(nb)
    return stuck


# ───────────────────────────── the check ───────────────────────────────

def check_preview(out: Path, base: str) -> dict:
    """Open every exported page AT ITS PREVIEW ADDRESS in a headless
    browser and report what a visitor would get. The agent does not decide
    that a preview works; this does. No browser -> SKIPPED, never PASS."""
    import http.server
    import mimetypes
    import threading
    import forge
    out, base = Path(out), "/" + base.strip("/")
    browser = forge._find_browser()
    pages = sorted(p.name for p in out.glob("*.html"))
    if not browser:
        return {"verdict": "SKIPPED", "why": "no Chromium-family browser — "
                "the preview is UNVERIFIED, not proven good", "pages": {}}
    failed = []
    mimetypes.add_type("text/javascript", ".mjs")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(s, *a, **k):
            super().__init__(*a, directory=str(out), **k)

        def translate_path(s, path):
            p = path.split("?")[0].split("#")[0]
            if p.rstrip("/") == base:
                return str(out / "index.html")
            if p.startswith(base + "/"):
                return str(out / p[len(base) + 1:])
            return str(out / "__outside_the_preview__")

        def send_response(s, code, msg=None):
            if code >= 400:
                failed.append(f"{code} {s.path}")
            super().send_response(code, msg)

        def log_message(s, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    report, ok = {}, True
    try:
        for page in pages:
            url = f"http://127.0.0.1:{srv.server_address[1]}{base}" + \
                  ("" if page == "index.html" else "/" + page)
            # a page that renders a fraction of its own text is broken,
            # however many characters that fraction is (measured: a Lewius
            # home at 252 of ~5,400 passed a flat 200-character floor)
            ssr = len(forge._visible_text(
                (out / page).read_text(encoding="utf-8", errors="ignore")))
            floor = max(200, int(0.4 * ssr))
            for attempt in (1, 2):
                before = len(failed)
                got = forge._render_page(browser, url, budget_ms=8000)
                dom = got.get("dom") or ""
                text = forge._visible_text(dom)
                errs = [c for c in (got.get("console") or [])
                        if re.search(r"error|failed|uncaught", str(c), re.I)]
                bad = failed[before:]
                page_ok = len(text) >= floor and not bad \
                    and not got.get("error")
                # headless Chrome is killed once the page is dumped and a
                # module still loading then reports itself failed; a real
                # fault (a 404, a wiped page) happens on the second render
                # too, so only a failure seen twice is one
                if page_ok:
                    break
            ok &= page_ok
            report[page] = {"ok": page_ok, "chars": len(text), "ssr": ssr,
                            "renders": attempt,
                            "failed_requests": bad[:8],
                            "console_errors": [str(c)[:160] for c in errs[:5]],
                            "layer": "__AE_PREVIEW__" in dom}
    finally:
        srv.shutdown()
    return {"verdict": "PASS" if ok else "FAIL", "pages": report}

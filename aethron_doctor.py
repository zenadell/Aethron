#!/usr/bin/env python3
"""Every diagnostic that has ever caught a real defect, kept.

WHY THIS EXISTS

Each time a migration went wrong, an instrument got written to find out
why — and then thrown away. The instrument that proved we were shipping
our own capture recorder to users. The one that counted hidden footer
elements at the viewport the owner actually browses at. The one that
compared which motion libraries initialise in each build. Every one was
deleted after a single use, so the next failure needed a person to write
it again.

That is backwards. A tool that found a defect once will find it again,
on a template nobody has seen yet, at three in the morning, without
anyone watching. So they live here now.

Each check reports a NAMED defect with the evidence that proves it, or
says it could not run — never a bare pass. Run it against any build:

    python3 aethron_doctor.py <project>
    python3 aethron_doctor.py <project> --build=port --page=index.html
    python3 aethron_doctor.py <project> --quick        (static checks only)

Exit code is 0 only when every check that ran, passed.
"""
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import forge                        # noqa: E402
import aethron_motion as motion     # noqa: E402

BUILDS = {
    "original": ("pristine", None),
    "migration": ("site", None),
    "port": ("convert-astro/dist", "static"),
    "port-next": ("convert-next/out", "static"),
    "port-vite": ("convert-vite/dist", "static"),
}

# Hosts that mean the "owned" copy is still renting something. Social and
# attribution links are content and belong to the page; these are not.
DEPENDENCY_HOSTS = re.compile(
    r"(website-files\.com|framerusercontent\.com|framer\.com"
    r"|webflow\.com|unpkg\.com|cdn\.jsdelivr\.net|ajax\.googleapis\.com"
    r"|cdnjs\.cloudflare\.com|[a-z0-9]+\.cloudfront\.net"
    r"|fonts\.googleapis\.com|fonts\.gstatic\.com)", re.I)

CONTENT_HOSTS = re.compile(
    r"(x\.com|twitter\.com|facebook\.com|linkedin\.com|instagram\.com"
    r"|dribbble\.com|behance\.net|youtube\.com|github\.com|savee\.com)", re.I)


class Result:
    def __init__(self):
        self.checks = []

    def add(self, name, status, detail="", items=None):
        self.checks.append({"name": name, "status": status,
                            "detail": detail, "items": items or []})

    def failed(self):
        return [c for c in self.checks if c["status"] == "FAIL"]

    def skipped(self):
        return [c for c in self.checks if c["status"] == "SKIPPED"]


# ───────────────────────── static checks ────────────────────────────────

def check_instrumentation(html: str, r: Result):
    """Our measuring apparatus must never reach a user.

    It shipped once: the recorder scrolls the page in steps to take
    readings, so the user's page scrolled itself to the bottom on every
    load, and its replay fought the platform's runtime for control of the
    same elements."""
    hits = []
    if re.search(r'data-aethron-probe', html):
        hits.append("injected capture probe")
    for m in re.findall(r'id="(__ae_[a-z]+)"', html):
        hits.append(f"data node {m}")
    if re.search(r'https?://127\.0\.0\.1:\d+', html):
        hits.append("capture server origin (127.0.0.1:PORT) baked into links")
    if hits:
        r.add("instrumentation leak", "FAIL",
              "Aethron's own tooling is present in the shipped page", hits)
    else:
        r.add("instrumentation leak", "PASS", "no Aethron tooling in output")


def check_shipped_assets(root: Path, r: Result):
    """Scan EVERY shipped file, not the entry page.

    `platform assets` read index.html, found nothing, and passed — while
    447 absolute framerusercontent.com URLs sat inside the chunk files,
    which is where the runtime builds its requests from. The migration
    reported clean and would have failed the moment a user went offline.
    A check that inspects one file cannot speak for a build."""
    import re as _re
    pat = _re.compile(r'https://(?:[a-z0-9.-]*website-files\.com'
                      r'|framerusercontent\.com|d3e54v103j8qbb\.cloudfront'
                      r'\.net)/[^"\'\s<>`\\)]+')
    hits, files = 0, []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (
                ".html", ".js", ".mjs", ".css", ".json"):
            continue
        # `sources/` is the authored source recovered from the platform's
        # own source maps — reference material for whoever inherits the
        # project, referenced by no page and loaded by nothing. Counting
        # it reported 217 platform urls on a build that fetches none.
        if "sources/" in str(f.relative_to(root)).replace("\\", "/"):
            continue
        try:
            n = len(pat.findall(f.read_text(errors="ignore")))
        except Exception:
            continue
        if n:
            hits += n
            files.append(f"{n}x {f.relative_to(root)}")
    if hits:
        r.add("platform urls in shipped files", "FAIL",
              f"{hits} absolute platform URL(s) across {len(files)} file(s) "
              f"— these are fetched at runtime, so the build is not "
              f"self-contained", files[:8])
    else:
        r.add("platform urls in shipped files", "PASS",
              "no absolute platform URLs anywhere in the build")


def check_dependencies(html: str, r: Result):
    """A copy that FETCHES its own code from someone else is rented.

    Only things the browser actually requests count: src, srcset, and
    <link href>. An <a href> to the platform is a badge or attribution —
    content, hidden by CSS, and judged by check_visible_promos on whether
    a viewer can see it. Counting anchors here failed nearly every
    project in the corpus for carrying a hidden link that costs nothing
    and fetches nothing, which is exactly the kind of noise that teaches
    you to stop reading the report.
    """
    # Resource HINTS are not dependencies either: <link rel="preconnect"
    # href="https://fonts.googleapis.com"> has no path and fetches
    # nothing.
    hint_urls = set()
    for tag in re.findall(r'(?is)<link\b[^>]*>', html):
        if re.search(r'rel="[^"]*(preconnect|dns-prefetch)', tag, re.I):
            m = re.search(r'href="(https?://[^"]+)"', tag)
            if m:
                hint_urls.add(m.group(1))

    fetched = re.findall(r'(?:src|srcset)="(https?://[^"]+)"', html)
    fetched += [u for u in re.findall(r'(?is)<link\b[^>]*href="(https?://[^"]+)"',
                                      html) if u not in hint_urls]
    anchors = re.findall(r'(?is)<a\b[^>]*href="(https?://[^"]+)"', html)
    remote_scripts = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)

    def host_of(u):
        return re.sub(r"^https?://([^/]+).*", r"\1", u)

    dep_scripts = [u for u in remote_scripts if DEPENDENCY_HOSTS.search(u)]
    if dep_scripts:
        r.add("rented code", "FAIL",
              f"{len(dep_scripts)} script(s) load from someone else's CDN — "
              f"the site stops working offline",
              sorted({u[:90] for u in dep_scripts}))
    else:
        r.add("rented code", "PASS", "no scripts loaded from external CDNs")

    deps = [host_of(u) for u in fetched if DEPENDENCY_HOSTS.search(host_of(u))]
    if deps:
        from collections import Counter
        c = Counter(deps)
        r.add("platform assets", "FAIL",
              f"{len(deps)} asset(s) are still fetched from a platform CDN",
              [f"{n}x {h}" for h, n in c.most_common(8)])
    else:
        r.add("platform assets", "PASS", "no assets fetched from a platform")

    plat_anchors = [host_of(u) for u in anchors
                    if DEPENDENCY_HOSTS.search(host_of(u))]
    if plat_anchors:
        from collections import Counter
        c = Counter(plat_anchors)
        r.add("platform links", "NOTE",
              f"{len(plat_anchors)} attribution/badge link(s) present — "
              f"allowed if hidden; see visible platform promos",
              [f"{n}x {h}" for h, n in c.most_common(4)])


def check_own_host_links(html: str, cfg: dict, r: Result):
    """Links that send visitors back to the template author's live site."""
    hosts = cfg.get("own_hosts") or []
    src = cfg.get("source_url") or ""
    if src:
        m = re.match(r"https?://([^/]+)", src)
        if m and m.group(1) not in hosts:
            hosts = hosts + [m.group(1)]
    if not hosts:
        r.add("self-referencing links", "SKIPPED",
              "no own_hosts recorded — cannot tell the origin site apart")
        return
    found = []
    for h in hosts:
        found += re.findall(r'(?:src|href)="(https?://%s[^"]*)"'
                            % re.escape(h), html)
    if found:
        r.add("self-referencing links", "FAIL",
              f"{len(found)} link(s) point back at the original site",
              sorted({u[:80] for u in found})[:8])
    else:
        r.add("self-referencing links", "PASS",
              "no links back to the source site")


def check_baked_state(html: str, r: Result, reference: str = None):
    """Capture-time state frozen into the markup.

    Reading the post-JS DOM bakes whatever pose each element held when
    the camera fired. Elements that had not revealed yet keep opacity:0
    forever — five footer elements stayed invisible that way, and a card
    stack shipped starting from the wrong card."""
    n = len(re.findall(r'style="[^"]*opacity:\s*0[^.\d]', html))
    if reference is None:
        r.add("baked animation state", "PASS" if n == 0 else "NOTE",
              f"{n} element(s) carry an inline opacity:0")
        return
    ref_n = len(re.findall(r'style="[^"]*opacity:\s*0[^.\d]', reference))
    if n > ref_n:
        r.add("baked animation state", "FAIL",
              f"{n - ref_n} more element(s) frozen hidden than the source "
              f"({n} vs {ref_n}) — capture-time state was shipped")
    else:
        r.add("baked animation state", "PASS",
              f"{n} inline opacity:0, matching the source ({ref_n})")


def check_script_parity(html: str, reference: str, r: Result):
    """Scripts the source shipped that the copy lost.

    This matters because a runtime that CONSUMES its inputs leaves holes:
    Webflow's commerce code reads its list templates and deletes them, so
    a captured page has no trace of them and the copy rebuilds nothing.

    Matching is by CONTENT FINGERPRINT, not text. The build minifies
    inline scripts — `window.Webflow = window.Webflow` becomes
    `window.Webflow=window.Webflow`, 3801 bytes become 1781 — and a
    textual compare called that a missing script. A check that cries wolf
    is worse than no check, because it teaches you to ignore the one that
    is real.
    """
    KEYWORDS = {"function", "return", "const", "this", "true", "false",
                "null", "undefined", "window", "document", "length",
                "value", "push", "call", "apply", "typeof", "else",
                "prototype", "var", "let"}

    def fingerprint(b):
        toks = set(re.findall(r"[A-Za-z_$][\w$]{3,}", b)) - KEYWORDS
        # string literals are the most stable thing in a minified script:
        # a mangler renames variables but never rewrites text
        toks |= set(re.findall(r'["\']([^"\'\n]{6,60})["\']', b))
        return toks

    def scripts(h):
        return [b for b in
                re.findall(r'(?is)<script\b(?![^>]*\bsrc=)[^>]*>(.*?)'
                           r'</script\s*>', h) if b.strip()]

    ref, got = scripts(reference), scripts(html)
    got_fps = [fingerprint(b) for b in got]
    lost = []
    for b in ref:
        fp = fingerprint(b)
        if len(fp) < 4:
            continue                     # too small to identify reliably
        best = 0.0
        for g in got_fps:
            if not g:
                continue
            overlap = len(fp & g) / len(fp)
            best = max(best, overlap)
        if best < 0.5:
            lost.append((b, best))
    if lost:
        r.add("script parity", "FAIL",
              f"{len(lost)} inline script(s) present in the source are "
              f"missing from this build",
              [f"(best match {int(s * 100)}%) "
               + re.sub(r"\s+", " ", b)[:64] for b, s in lost[:6]])
    else:
        r.add("script parity", "PASS",
              f"all {len(ref)} inline script(s) accounted for "
              f"({len(got)} in the build)")


# ───────────────────────── live checks ──────────────────────────────────

RUNTIME_PROBE = r"""
(function () {
  setTimeout(function () {
    var out = { err: [], libs: {}, hidden: {}, counts: {} };
    try {
      var names = ["gsap", "ScrollTrigger", "Lenis", "jQuery", "Webflow",
                   "Swiper", "barba", "SplitText"];
      for (var i = 0; i < names.length; i++) {
        out.libs[names[i]] = typeof window[names[i]];
      }
      if (window.ScrollTrigger && window.ScrollTrigger.getAll) {
        out.counts.scrolltriggers = window.ScrollTrigger.getAll().length;
      }
      if (window.gsap && window.gsap.globalTimeline) {
        out.counts.tweens = window.gsap.globalTimeline.getChildren().length;
      }
      out.counts.scripts = document.querySelectorAll("script").length;
      out.counts.images = document.querySelectorAll("img").length;
      var broken = 0, im = document.querySelectorAll("img");
      for (var j = 0; j < im.length; j++) {
        if (im[j].complete && im[j].naturalWidth === 0) { broken++; }
      }
      out.counts.brokenImages = broken;
      out.counts.docH = document.documentElement.scrollHeight;
      out.counts.text = (document.body.innerText || "").trim().length;
    } catch (e) { out.err.push(String(e && e.message).slice(0, 90)); }
    /* how much stays invisible after a full scroll — the check that
       turned "the footer is gone" into "5 elements keep opacity 0" */
    /* A platform promo that is PRESENT is fine — badges are hidden, not
       deleted, because the runtime re-creates them. One that is VISIBLE
       is a "buy this template" card on a site the owner believes is
       theirs. Measured on agero: 142x110, display:flex, in the migration
       as well as the port, because its url was framer.com/@author/
       ?tab=marketplace and the hide rules only knew /r/badge. */
    try {
      out.promos = [];
      var pl = document.querySelectorAll(
        'a[href*="framer.com"],a[href*="webflow.com"],a[href*="gumroad"],'
        + 'a[href*="lemonsqueezy"],a[href*="buy.polar.sh"]');
      for (var q = 0; q < pl.length; q++) {
        var ps = getComputedStyle(pl[q]), pr = pl[q].getBoundingClientRect();
        if (ps.display !== "none" && ps.visibility !== "hidden" &&
            +ps.opacity > 0.05 && pr.width > 1 && pr.height > 1) {
          out.promos.push((pl[q].getAttribute("href") || "").slice(0, 60)
                          + "  " + Math.round(pr.width) + "x"
                          + Math.round(pr.height));
        }
      }
    } catch (e) { out.err.push("promo: " + String(e && e.message).slice(0, 50)); }
    var H = document.documentElement.scrollHeight - innerHeight;
    window.scrollTo(0, H);
    setTimeout(function () {
      try {
        var all = document.querySelectorAll("body *"), hid = 0, tot = 0;
        for (var k = 0; k < all.length; k++) {
          var s = getComputedStyle(all[k]);
          if (s.display === "none") { continue; }
          tot++;
          if (+s.opacity < 0.05 || s.visibility === "hidden") { hid++; }
        }
        out.hidden = { hidden: hid, of: tot };
      } catch (e) { out.err.push("vis: " + String(e && e.message).slice(0, 60)); }
      try { fetch("/__ae_capture", { method: "POST",
                                     body: JSON.stringify(out) }); } catch (e) { }
    }, 1500);
  }, %(settle)d);
})();
"""


def live_probe(root: Path, platform: str, page: str, win="1440,900",
               wait_s=70) -> dict:
    return motion.capture_realtime(root, platform, page,
                                   RUNTIME_PROBE % {"settle": 8000},
                                   wait_s=wait_s, win=win)


def check_offline(root: Path, platform: str, page: str, r: Result):
    """Does it still work with the internet unreachable?

    This is the only honest test of "you own it"."""
    browser = forge._find_browser()
    if not browser:
        r.add("works offline", "SKIPPED", "no browser — UNVERIFIED")
        return
    from http.server import ThreadingHTTPServer
    got, ready = {}, threading.Event()
    base = motion._injecting_handler(root, platform,
                                     RUNTIME_PROBE % {"settle": 7000})

    class H(base):
        def do_POST(self):
            if self.path == "/__ae_capture":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    got.update(json.loads(self.rfile.read(n) or b"{}"))
                except Exception:
                    pass
                self.send_response(204)
                self.end_headers()
                ready.set()
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="doctor-offline-")
    proc = None
    try:
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--window-size=1440,900", "--hide-scrollbars",
             f"--user-data-dir={tmp}/p",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
             f"http://127.0.0.1:{srv.server_address[1]}/{page}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready.wait(70)
    finally:
        if proc:
            proc.terminate()
        srv.shutdown()
    if not got:
        r.add("works offline", "FAIL",
              "nothing rendered with the network blocked")
        return
    c = got.get("counts") or {}
    if (c.get("text") or 0) < 200:
        r.add("works offline", "FAIL",
              f"only {c.get('text')} chars rendered offline")
    elif c.get("brokenImages"):
        r.add("works offline", "FAIL",
              f"{c['brokenImages']} image(s) broken with no network")
    else:
        r.add("works offline", "PASS",
              f"{c.get('text')} chars, {c.get('images')} images, "
              f"0 broken, {c.get('scripts')} scripts — no internet needed")


def check_runtime(live: dict, r: Result, reference: dict = None):
    if not live or not live.get("available"):
        r.add("runtime libraries", "SKIPPED",
              f"page did not report ({(live or {}).get('reason')}) — "
              f"UNVERIFIED")
        return
    libs = live.get("libs") or {}
    active = [k for k, v in libs.items() if v not in ("undefined", None)]
    counts = live.get("counts") or {}
    detail = (f"active: {', '.join(active) or 'none'}; "
              f"scrolltriggers={counts.get('scrolltriggers')}, "
              f"tweens={counts.get('tweens')}")
    if reference and reference.get("available"):
        ref_libs = reference.get("libs") or {}
        lost = [k for k in ref_libs
                if ref_libs[k] != "undefined" and libs.get(k) == "undefined"]
        rc = reference.get("counts") or {}
        if lost:
            r.add("runtime libraries", "FAIL",
                  f"libraries present in the source do not initialise here",
                  lost)
            return
        for key in ("scrolltriggers", "tweens"):
            a, b = rc.get(key), counts.get(key)
            if a and b is not None and b < a * 0.9:
                r.add("runtime libraries", "FAIL",
                      f"{key}: {b} here vs {a} in the source — motion is "
                      f"not fully wired")
                return
    r.add("runtime libraries", "PASS", detail)
    if counts.get("brokenImages"):
        r.add("broken images", "FAIL",
              f"{counts['brokenImages']} image(s) fail to load")
    else:
        r.add("broken images", "PASS", f"{counts.get('images')} images, none broken")


def check_visible_promos(live: dict, r: Result):
    """Platform promos the viewer can actually see.

    Presence is allowed — Framer re-creates its badge, so the rule is to
    hide it, not delete it. Visibility is not: a template author's
    marketplace card rendering at 142x110 on the owner's own site is the
    exact thing this product exists to remove."""
    if not live or not live.get("available"):
        r.add("visible platform promos", "SKIPPED",
              "no live reading — UNVERIFIED")
        return
    promos = live.get("promos") or []
    if promos:
        r.add("visible platform promos", "FAIL",
              f"{len(promos)} platform promo(s) are VISIBLE to the viewer",
              promos[:6])
    else:
        r.add("visible platform promos", "PASS",
              "no platform promos visible")


def check_visibility(live: dict, r: Result, reference: dict = None):
    """Elements that never become visible, counted after a full scroll."""
    if not live or not live.get("available"):
        r.add("hidden elements", "SKIPPED", "no live reading — UNVERIFIED")
        return
    h = live.get("hidden") or {}
    if reference and reference.get("available"):
        rh = reference.get("hidden") or {}
        if rh.get("hidden") is not None and h.get("hidden") is not None:
            if h["hidden"] > rh["hidden"]:
                r.add("hidden elements", "FAIL",
                      f"{h['hidden'] - rh['hidden']} more element(s) never "
                      f"become visible than in the source "
                      f"({h['hidden']} vs {rh['hidden']})")
                return
    r.add("hidden elements", "PASS",
          f"{h.get('hidden')} of {h.get('of')} invisible after a full scroll")


# ──────────────────────────── driver ────────────────────────────────────

def resolve(proj: Path, name: str):
    if name not in BUILDS:
        return None, None
    sub, plat = BUILDS[name]
    root = proj / sub
    if not root.is_dir():
        return None, None
    if plat is None:
        cfg = json.loads((proj / "forge.json").read_text(encoding="utf-8"))
        plat = cfg.get("platform", "static")
    return root, plat


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 2
    proj = Path(args[0]).resolve()
    cfg_path = proj / "forge.json"
    if not cfg_path.is_file():
        print(f"not an Aethron project: {proj}")
        return 2
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    page = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--page=")), "index.html")
    quick = "--quick" in argv
    want = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--build=")), None)
    builds = [want] if want else [n for n in ("migration", "port")
                                  if resolve(proj, n)[0]]

    ref_root, ref_plat = resolve(proj, "migration")
    ref_html = ""
    if ref_root and (ref_root / page).is_file():
        ref_html = (ref_root / page).read_text(encoding="utf-8", errors="ignore")
    ref_live = None

    worst = 0
    for name in builds:
        root, plat = resolve(proj, name)
        if not root:
            print(f"\n### {name}: no such build — SKIPPED")
            continue
        if not (root / page).is_file():
            print(f"\n### {name}: no {page} — SKIPPED")
            continue
        html = (root / page).read_text(encoding="utf-8", errors="ignore")
        r = Result()
        print(f"\n{'=' * 66}\nDOCTOR — {name}  ({root.name}/{page})\n{'=' * 66}")

        check_instrumentation(html, r)
        check_dependencies(html, r)
        check_shipped_assets(root, r)
        check_own_host_links(html, cfg, r)
        is_copy = name != "migration" and ref_html
        check_baked_state(html, r, ref_html if is_copy else None)
        if is_copy:
            check_script_parity(html, ref_html, r)

        if not quick:
            live = live_probe(root, plat, page)
            if is_copy and ref_live is None and ref_root:
                ref_live = live_probe(ref_root, ref_plat, page)
            check_runtime(live, r, ref_live if is_copy else None)
            check_visibility(live, r, ref_live if is_copy else None)
            check_visible_promos(live, r)
            check_offline(root, plat, page, r)
            if name == "migration":
                ref_live = live

        for c in r.checks:
            mark = {"PASS": "  PASS", "FAIL": "  FAIL", "NOTE": "  note",
                    "SKIPPED": "  SKIP"}[c["status"]]
            print(f"{mark}  {c['name']}: {c['detail']}")
            for it in c["items"][:8]:
                print(f"          {it}")
        bad, skip = r.failed(), r.skipped()
        print(f"\n  {len(bad)} failure(s), {len(skip)} unverified, "
              f"{len(r.checks) - len(bad) - len(skip)} passed")
        worst = max(worst, 1 if bad else 0)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

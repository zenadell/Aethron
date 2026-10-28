#!/usr/bin/env python3
"""Find a FREE Framer or Webflow template that fits a person's site.

Someone brings their own site and asks Aethron to make it better. The
better site is a well-made template carrying THEIR words, so first there
has to be a template. This finds candidates; the agent picks one (the
agent is the judge, as with fills), checks it in its own browser and
migrates into it.

Where the facts come from — read, never guessed:
  * Framer: a marketplace listing page (all, or one category) carries
    every card's data in the page itself: title, slug, author, price
    (null = free) and the live preview address.
  * Webflow: the free-templates page lists its templates (structured
    data), and each template's own page carries its preview address
    (<name>.webflow.io) and its price ("0" = free).

Free only, always: a template with a price is never offered. A page
that answers with a rate limit or a bot check is reported as such and
not retried — this reads the marketplaces as a person's browser would,
one page at a time, and never works around them.

    python3 aethron_templates.py "dental clinic, calm, booking"   # live
    python3 aethron_templates.py --selftest                      # fixtures
"""
import html as _html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AETHRON_HOME") or ROOT)
CACHE = HOME / "template_cache.json"
CACHE_S = 6 * 3600

FRAMER = "https://www.framer.com/marketplace/templates/"
FRAMER_CAT = FRAMER + "categories/{}/"
WEBFLOW_FREE = "https://webflow.com/templates/free-website-templates"
WEBFLOW_PAGE = "https://webflow.com/templates/html/{}"

# words people use -> Framer's own category slugs (read off the listing)
SYNONYMS = {
    "agency": ["agency", "studio", "creative", "branding"],
    "ai": ["ai", "artificial", "llm", "chatbot", "machine"],
    "saas": ["saas", "startup", "product", "platform", "dashboard", "b2b"],
    "software": ["software", "app", "tech", "developer", "devtools"],
    "apps": ["app", "mobile", "ios", "android"],
    "portfolio": ["portfolio", "freelancer", "personal", "designer", "showcase"],
    "photography": ["photo", "photography", "photographer"],
    "restaurants": ["restaurant", "cafe", "bar", "food", "menu", "bakery", "kitchen"],
    "medical": ["medical", "clinic", "dental", "dentist", "doctor", "hospital"],
    "health": ["health", "care", "therapy", "wellness"],
    "fitness": ["fitness", "gym", "trainer", "yoga", "sport"],
    "real-estate": ["real estate", "property", "realtor", "homes", "rental"],
    "ecommerce": ["shop", "store", "ecommerce", "products", "retail"],
    "consulting": ["consulting", "consultant", "advisory", "coach"],
    "legal": ["law", "legal", "lawyer", "attorney"],
    "education": ["school", "course", "education", "academy", "learning"],
    "events": ["event", "conference", "meetup"],
    "weddings": ["wedding", "bride"],
    "travel": ["travel", "tour", "trip", "hotel"],
    "construction": ["construction", "builder", "contractor", "renovation"],
    "fintech": ["fintech", "finance", "bank", "payments", "crypto"],
    "marketing": ["marketing", "seo", "growth", "ads"],
    "blogs": ["blog", "writer", "newsletter"],
    "music": ["music", "band", "artist", "dj"],
    "architecture": ["architect", "architecture", "interior"],
    "beauty": ["beauty", "salon", "spa", "hair"],
    "fashion": ["fashion", "clothing", "apparel"],
}


def _words(t):
    return set(re.findall(r"[a-z0-9]+", str(t or "").lower()))


def categories_for(about, known=None):
    """Framer categories the person's words point at, best first."""
    w, low = _words(about), str(about or "").lower()
    out = []
    for cat, syn in SYNONYMS.items():
        hit = sum(1 for s in syn if (s in low if " " in s else s in w))
        if cat.replace("-", " ") in low:
            hit += 1
        if hit and (not known or cat in known):
            out.append((hit, cat))
    for c in known or []:
        if c in w and c not in [x[1] for x in out]:
            out.append((1, c))
    return [c for _, c in sorted(out, key=lambda x: -x[0])]


# ── reading pages ─────────────────────────────────────────────────────
def _blocked(dom):
    t = (dom or "")[:6000].lower()
    for k in ("too many requests", "rate limit", "verify you are human",
              "just a moment", "attention required", "access denied"):
        if k in t:
            return k
    return ""


def render(url, budget_ms=12000):
    """A real browser load (listings draw their cards with JavaScript).
    -> (dom, why_not)."""
    import forge
    b = forge._find_browser()
    if not b:
        return "", "no browser to read the marketplace with"
    r = forge._render_page(b, url, budget_ms=budget_ms, timeout=60)
    dom = r.get("dom") or ""
    if not dom:
        return "", r.get("error") or "the page did not load"
    why = _blocked(dom)
    if why:
        return "", f"the marketplace answered with a {why} page (not retried)"
    return dom, ""


def fetch(url, timeout=20):
    """A plain read, for pages that are complete without JavaScript."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Aethron (+https://aethron.jomiez.com)",
        "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore"), ""
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}" + (" (rate limited, not retried)"
                                       if e.code == 429 else "")
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


# ── Framer ───────────────────────────────────────────────────────────
_FR_ITEM = re.compile(
    r'"title":"((?:[^"\\]|\\.)*)","slug":"([a-z0-9-]+)"'
    r'(?:,"author":\{"name":"((?:[^"\\]|\\.)*)"\})?'
    r'.{0,2500}?"type":"template","attributes":\{"price":(null|"[^"]*")'
    r'[^{}]*?"previewUrl":"([^"]+)"', re.S)


def parse_framer(dom, category=""):
    """Every card on a Framer listing -> [{...}] (free AND paid; the
    caller filters). The data sits in the page's own script payload with
    its quotes escaped."""
    u = dom.replace('\\"', '"').replace("\\u0026", "&")
    out, seen = [], set()
    for title, slug, author, price, prev in _FR_ITEM.findall(u):
        if slug in seen:
            continue
        seen.add(slug)
        p = price.strip('"')
        out.append({"platform": "framer", "name": _html.unescape(title),
                    "slug": slug, "author": _html.unescape(author or ""),
                    "price": None if price == "null" else p,
                    "free": price == "null" or p in ("0", "0.00", ""),
                    "preview": prev.rstrip("\\"),
                    "page": FRAMER + slug + "/", "category": category})
    return out


def framer_categories(dom):
    return sorted(set(re.findall(
        r"/marketplace/templates/categories/([a-z0-9-]+)/", dom or "")))


# ── Webflow ──────────────────────────────────────────────────────────
def parse_webflow_list(dom):
    """Template slugs on the free page, in the page's own order."""
    out = []
    for s in re.findall(r"/templates/html/([a-z0-9-]+)", dom or ""):
        if s not in out:
            out.append(s)
    return out


def parse_webflow_page(page, slug=""):
    """One Webflow template page -> {...} or None."""
    if not page:
        return None
    p = page.replace("&quot;", '"').replace("&amp;", "&")
    prev = (re.search(r'"previewIframeUrl":\{"href":"(https://[a-z0-9-]+\.webflow\.io/?)"', p)
            or re.search(r'(https://[a-z0-9-]+\.webflow\.io)/?', p))
    if not prev:
        return None
    title = re.search(r"<title>([^<]*)", page)
    name = _html.unescape(title.group(1)) if title else slug
    name = re.sub(r"\s+(Website\s+)?(Page\s+)?Template\s+for\s+Webflow.*$", "", name,
                  flags=re.I).strip() or slug
    desc = (re.search(r'<meta content="([^"]*)" name="description"', page)
            or re.search(r'<meta name="description" content="([^"]*)"', page))
    # THIS template's price only: the page also lists related templates,
    # each with its own price. Its own card names its slug; failing
    # that, its own structured data (an Offer) carries it.
    own = re.findall(r'"template_slug":"%s"[^{}]*?"price":"([^"]*)"' % re.escape(slug), p) if slug else []
    if not own:
        for ld in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
            if '"Offer"' in ld or '"offers"' in ld:
                own += re.findall(r'"price":\s*"([^"]*)"', ld)
    prices = [x.strip() for x in own if x.strip()]
    free = bool(prices) and all(x.lower() in ("0", "0.0", "0.00", "free") for x in prices)
    return {"platform": "webflow", "name": name, "slug": slug,
            "author": "", "price": None if free else (prices[0] if prices else "?"),
            "free": free, "preview": prev.group(1).rstrip("/") + "/",
            "page": WEBFLOW_PAGE.format(slug),
            "about": _html.unescape(desc.group(1)).strip() if desc else ""}


# ── scoring and the search ───────────────────────────────────────────
def score(t, about):
    """How well a template's own words meet the person's. A tiebreak
    for the agent, never the decision."""
    want = _words(about) - {"a", "the", "and", "for", "my", "our", "site",
                            "website", "with", "to", "of", "in", "make", "better"}
    have = _words(" ".join([t.get("name", ""), t.get("slug", ""),
                            t.get("category", ""), t.get("about", "")]))
    s = len(want & have) * 2
    if t.get("category") and t["category"] in categories_for(about):
        s += 3
    return s


def _cache():
    try:
        c = json.loads(CACHE.read_text())
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _remember(key, val):
    c = _cache()
    c[key] = {"t": time.time(), "v": val}
    for k in sorted(c, key=lambda k: c[k].get("t", 0))[:-60]:
        c.pop(k, None)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(c))
    except Exception:
        pass


def _cached(key):
    e = _cache().get(key)
    return e["v"] if e and time.time() - e.get("t", 0) < CACHE_S else None


def framer_listing(category=""):
    url = FRAMER_CAT.format(category) if category else FRAMER
    got = _cached(url)
    if got is not None:
        return got, ""
    dom, why = render(url)
    if not dom:
        return [], why
    items = parse_framer(dom, category)
    if not items:
        return [], "the listing loaded but no templates could be read from it"
    _remember(url, items)
    _remember("framer:categories", framer_categories(dom))
    return items, ""


def webflow_free(limit=12):
    got = _cached(WEBFLOW_FREE)
    if got is not None:
        return got, ""
    dom, why = render(WEBFLOW_FREE)
    if not dom:
        return [], why
    out, notes = [], []
    for slug in parse_webflow_list(dom)[:max(limit, 1)]:
        page, why = fetch(WEBFLOW_PAGE.format(slug))
        if not page:
            notes.append(f"{slug}: {why}")
            if "429" in why:
                break
            continue
        t = parse_webflow_page(page, slug)
        if t:
            out.append(t)
    if out:
        _remember(WEBFLOW_FREE, out)
    return out, "; ".join(notes[:3])


def find(about, platform="any", limit=10):
    """Free templates that fit `about`, best first.
    -> {"templates": [...], "notes": [...], "searched": [...]}"""
    platform = (platform or "any").lower()
    found, notes, searched = [], [], []
    if platform in ("any", "framer"):
        known = (_cached("framer:categories") or list(SYNONYMS))
        cats = categories_for(about, known)[:2]
        for cat in cats + [""]:
            items, why = framer_listing(cat)
            searched.append("Framer " + (cat or "all templates"))
            if why:
                notes.append(f"Framer {cat or 'listing'}: {why}")
            found += items
            if len([t for t in found if t["free"]]) >= limit * 2:
                break
    if platform in ("any", "webflow"):
        items, why = webflow_free(limit=max(limit, 12))
        searched.append("Webflow free templates")
        if why:
            notes.append(f"Webflow: {why}")
        found += items
    seen, free = set(), []
    for t in found:
        k = (t["platform"], t["slug"])
        if k in seen or not t["free"]:
            continue
        seen.add(k)
        t = dict(t, score=score(t, about))
        free.append(t)
    free.sort(key=lambda t: -t["score"])
    return {"templates": free[:max(1, int(limit))], "notes": notes,
            "searched": searched,
            "paid_skipped": len({(t["platform"], t["slug"]) for t in found
                                 if not t["free"]})}


# ── selftest (fixtures saved from the real pages; no network) ─────────
def _selftest():
    D = ROOT / "tests" / "pages" / "templates"
    ok = fail = 0

    def check(name, cond, info=""):
        nonlocal ok, fail
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  — {info}"))
        ok, fail = ok + bool(cond), fail + (not cond)

    fr = parse_framer((D / "framer_listing.html").read_text(), "agency")
    check("Framer cards are read from the listing", len(fr) >= 5, len(fr))
    check("each carries its live preview", all(t["preview"].startswith("https://") for t in fr))
    free = [t for t in fr if t["free"]]
    paid = [t for t in fr if not t["free"]]
    check("free and paid are told apart (price null = free)", free and paid,
          [(t["slug"], t["price"]) for t in fr])
    check("a paid one keeps its price", all(t["price"] for t in paid))
    check("Framer categories are read", "portfolio" in framer_categories(
        (D / "framer_listing.html").read_text()))
    wl = parse_webflow_list((D / "webflow_listing.html").read_text())
    check("Webflow's free list is read", len(wl) >= 10 and "filoo-website-template" in wl, wl[:3])
    wp = parse_webflow_page((D / "webflow_template.html").read_text(), "filoo-website-template")
    check("a Webflow page gives its preview", wp and wp["preview"] == "https://fiilo.webflow.io/", wp)
    check("and says it is free", wp and wp["free"], wp)
    check("its name is clean", wp and wp["name"] == "Fiilo", wp and wp["name"])
    check("a page with a price is not free",
          not parse_webflow_page('<title>X</title><script type="application/ld+json">{"offers":'
                                 '{"@type":"Offer","price": "49"}}</script> https://x.webflow.io/', "x")["free"])
    rel = ('"template_slug":"x","price":"Free" "template_slug":"y","price":"$59 USD" '
           'https://x.webflow.io/')
    check("a related template's price does not count against this one",
          parse_webflow_page("<title>X</title>" + rel, "x")["free"])
    check("a page with no preview is not a candidate",
          parse_webflow_page("<title>X</title>", "x") is None)
    check("words map to categories",
          categories_for("a calm dental clinic with booking")[:1] == ["medical"]
          and "restaurants" in categories_for("our bakery and cafe"))
    check("a rate-limit page is named, not parsed",
          _blocked("<html><body>429 Too Many Requests</body></html>") != "")
    s1 = score({"name": "Dentro", "slug": "dentro", "category": "medical", "about": "dental clinic"},
               "dental clinic")
    s2 = score({"name": "Rushes", "slug": "rushes", "category": "", "about": ""}, "dental clinic")
    check("scoring prefers the template whose words meet the person's", s1 > s2, (s1, s2))
    print(f"\n{ok}/{ok + fail} checks passed")
    return 1 if fail else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(_selftest())
    r = find(" ".join(sys.argv[1:]) or "agency")
    for t in r["templates"]:
        print(f"{t['score']:>2}  {t['platform']:7} {t['name'][:30]:30} {t['preview']}")
    for n in r["notes"]:
        print("note:", n)

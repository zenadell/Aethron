#!/usr/bin/env python3
"""Aethron's brand memory — retrieval that grounds the copy in what is true.

    python3 aethron_rag.py learn  <project_dir> <url-or-file> [...]
    python3 aethron_rag.py search <project_dir> "query" [k]
    python3 aethron_rag.py --selftest

THE PROBLEM IT SOLVES, MEASURED. jomiez had 387 lines still to write,
1,219 words, 23 of them whole paragraphs — case studies, a privacy policy,
blog intros. The agent had a four-line brand plan and whatever single page
it happened to fetch, so it either invented facts about the client or
reworded the template's; the truth check read 56% of the copy as still the
template's. The client's own site held the answers the whole time.

WHAT IT DOES. `learn` reads the brand's material (its live site, or text/
markdown/html files) into a BRAND PACK inside the project: passages, each
stamped with its page and heading, each embedded. `search` finds the
passages that answer a question. `notes_for` is what the copy tools use:
for every line to be written, the template's own words describe what the
slot is ABOUT, so they are the query — the "case study" paragraph brings
back the brand's real case study.

THE CHOICES, AND WHERE THEY COME FROM
  * HYBRID. Meaning-based search misses exact names and numbers; keyword
    search misses paraphrase. Anthropic's published contextual-retrieval
    study measured failed retrievals falling ~49% when both are combined
    (and ~35% for contextualised chunks alone). Fused by reciprocal rank,
    which needs no tuning between two scores on different scales.
  * CONTEXT IN EVERY PASSAGE, FOR FREE. That study writes context per chunk
    with a model; on a free tier that is a request per passage. The page
    title and heading path say most of the same thing and cost nothing, so
    every passage is indexed as "title: <page> › <heading> | text: …" —
    which is also the document format Google documents for its model.
  * GEMINI, NOT THIS MACHINE. gemini-embedding-2 on the keys already in
    settings, free keys first, benched PER MODEL (its allowance is separate
    from the chat model's — measured: a key spent on gemini-3.8-flash
    embedded fine). No local model, nothing heavy on the owner's Mac.
  * HONEST FALLBACKS. No Gemini key or no allowance left: keyword search
    only, and it SAYS so. A question nothing in the material answers gets
    NOTHING back — an unrelated passage handed to a copywriter becomes an
    invented fact with a citation, which is worse than no note at all.

Stdlib only, like the rest of the pipeline.
"""
import array
import base64
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

EMBED_MODEL = "gemini-embedding-2"
DIMS = 768
BASE = os.environ.get("AETHRON_EMBED_BASE",
                      "https://generativelanguage.googleapis.com/v1beta")
BATCH = 90                 # the endpoint takes 100 per request; stay under
PACK = "brand/pack.json"
MAX_PAGES = 25
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Aethron/brand-reader"}
# A match at or above STRONG_COS answered its question in every measured
# case; between MIN_COS and it a passage is ON THE TOPIC but not proof of a
# fact (measured: "what awards has the studio won?" — no awards anywhere —
# still scored 0.645 against a case study). The label travels with it.
STRONG_COS = float(os.environ.get("AETHRON_RAG_STRONG_COS", "0.70"))
# Below this a match is not an answer. Calibrated on the owner's own site
# (see the selftest notes and AETHRON.md): related passages sat at 0.66+,
# unrelated ones under 0.58 on gemini-embedding-2 at 768 dimensions.
MIN_COS = float(os.environ.get("AETHRON_RAG_MIN_COS", "0.60"))
RRF_K = 60
WORDS_PER_CHUNK = 170


class Unavailable(Exception):
    """Meaning-based search cannot run; keyword search still can."""


# ───────────────────────── reading material ──────────────────────────

_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}
_BLOCK = {"p", "div", "section", "article", "li", "ul", "ol", "br", "tr",
          "td", "th", "table", "header", "footer", "main", "aside", "nav",
          "blockquote", "figcaption", "dd", "dt", "form", "label", "button",
          "h1", "h2", "h3", "h4", "h5", "h6", "a", "span_block"}
_HEAD = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}


class _Reader(HTMLParser):
    """Visible text, in order, with the heading it sits under."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.paras, self.links = "", [], []
        self._skip, self._buf, self._in_title = 0, [], False
        self._head_level, self._heads = 0, {}

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if not t:
            return
        if self._head_level:
            self._heads[self._head_level] = t
            for lv in [x for x in self._heads if x > self._head_level]:
                del self._heads[lv]
            self.paras.append(("H", t, self._path()))
        else:
            self.paras.append(("P", t, self._path()))

    def _path(self):
        return " › ".join(self._heads[k] for k in sorted(self._heads))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href:
                self.links.append(href)
            # a site's email and phone usually live ONLY in a link — the
            # text says "Contact us" — and they are exactly what a footer
            # or contact section needs
            low = href.lower()
            if low.startswith("mailto:") and "@" in low:
                self.paras.append(("P", "Contact email: "
                                   + urllib.parse.unquote(href[7:].split("?")[0]),
                                   self._path()))
            elif low.startswith("tel:") and re.search(r"\d{5}", low):
                self.paras.append(("P", "Contact phone: " + href[4:],
                                   self._path()))
        if tag in _BLOCK:
            self._flush()
        if tag in _HEAD:
            self._head_level = _HEAD[tag]

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK or tag in _HEAD:
            self._flush()
        if tag in _HEAD:
            self._head_level = 0

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self._buf.append(data)


def read_html(text: str):
    r = _Reader()
    try:
        r.feed(text)
        r.close()
    except Exception:
        pass
    r._flush()
    return html.unescape(r.title).strip(), r.paras, r.links


def read_markdown(text: str):
    paras, heads, title = [], {}, ""
    for block in re.split(r"\n\s*\n", text):
        b = block.strip()
        if not b:
            continue
        m = re.match(r"^(#{1,4})\s+(.+)$", b.splitlines()[0])
        if m:
            lv = len(m.group(1))
            heads[lv] = m.group(2).strip()
            for x in [k for k in heads if k > lv]:
                del heads[x]
            title = title or heads[lv]
            rest = "\n".join(b.splitlines()[1:]).strip()
            if not rest:
                continue
            b = rest
        path = " › ".join(heads[k] for k in sorted(heads))
        paras.append(("P", re.sub(r"\s+", " ", b), path))
    return title, paras


def _fetch(url: str, tries=4) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                if "html" not in (r.headers.get("Content-Type") or "html"):
                    return ""
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return ""
            last = e
        except Exception as e:
            last = e
        time.sleep(1.5 * (i + 1))
    raise last or RuntimeError(f"could not read {url}")


def crawl(url: str, max_pages=MAX_PAGES, log=print):
    """-> [(url, title, paras)] for the site's own pages, home first."""
    start = url if re.match(r"https?://", url) else "https://" + url
    host = urllib.parse.urlparse(start).netloc
    seen, queue, out = set(), [start], []
    skip_ext = re.compile(r"\.(png|jpe?g|gif|svg|webp|avif|ico|css|js|mjs|"
                          r"json|xml|pdf|zip|mp4|webm|woff2?|ttf)(\?|$)", re.I)
    while queue and len(out) < max_pages:
        u = queue.pop(0)
        key = urllib.parse.urlparse(u)._replace(fragment="", query="").geturl() \
            .rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        try:
            body = _fetch(u)
        except Exception as e:
            log(f"  could not read {u}: {e}")
            continue
        if not body:
            continue
        title, paras, links = read_html(body)
        out.append((u, title, paras))
        for h in links:
            if h.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            nxt = urllib.parse.urljoin(u, h)
            p = urllib.parse.urlparse(nxt)
            if p.netloc == host and not skip_ext.search(p.path):
                queue.append(p._replace(fragment="").geturl())
    return out


def chunk(src: str, title: str, paras, seen_text: set) -> list:
    """Passages of ~WORDS_PER_CHUNK words that never cross a heading.

    Text seen on an earlier page is dropped: a site repeats its navigation
    and footer on every page, and twenty copies of the footer would crowd
    every real answer out of the results."""
    out, cur, cur_head, n, keys = [], [], None, 0, []

    def emit():
        nonlocal cur, n, keys
        if cur:
            out.append({"src": src, "title": title, "heading": cur_head or "",
                        "text": " ".join(cur), "keys": keys,
                        "_longest": max(len(x.split()) for x in cur)})
        cur, n, keys = [], 0, []

    for kind, text, head in paras:
        if kind == "H" or _BOILER.search(text):
            continue
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        pk = _pkey(norm)
        if len(norm) < 3 or pk in seen_text:
            continue
        seen_text.add(pk)
        if head != cur_head:
            emit()
            cur_head = head
        words = text.split()
        if len(words) > WORDS_PER_CHUNK * 1.6:
            emit()
            for s in re.split(r"(?<=[.!?])\s+", text):
                sw = len(s.split())
                if n + sw > WORDS_PER_CHUNK and cur:
                    emit()
                cur.append(s)
                keys.append(pk)
                n += sw
            emit()
            continue
        if n + len(words) > WORDS_PER_CHUNK and cur:
            emit()
        cur.append(text)
        keys.append(pk)
        n += len(words)
    emit()
    # A lone short fragment is not a passage, and neither is a MENU: a run
    # of two-word labels ("Home About Services Work Contact Us") matched
    # "how to bake bread" better than anything real on the owner's site.
    # Short lines that carry a fact — an email, a phone number, an address
    # with digits — are kept; they are exactly what a footer needs.
    def menu(c):
        return c.get("_longest", 99) < 5 and not re.search(r"@|\d{3}", c["text"])
    return [c for c in out if (len(c["text"].split()) >= 4
                               or re.search(r"@|\d{5}", c["text"]))
            and not menu(c)]


# Words a site builder puts on every form, never words the brand wrote.
# Measured on the owner's site: "Thank you for your submission! Oops!
# Something went wrong." was the TOP answer to "how do I contact them".
_BOILER = re.compile(r"(?i)thank you!?\s*(for\s+)?your submission|"
                     r"submission has been received|oops!?\s*something went "
                     r"wrong|^\s*(loading|submit|send)\W*$")


def _pkey(norm: str) -> str:
    """A paragraph's fingerprint: what "seen before" is remembered by, so a
    re-read page is deduplicated against paragraphs, not whole passages."""
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def _doc_text(c: dict) -> str:
    where = " › ".join(x for x in (c.get("title"), c.get("heading")) if x)
    return f"title: {where or 'none'} | text: {c['text']}"


# ───────────────────────── embedding ─────────────────────────────────

def _keys() -> list:
    """Gemini keys, free first, minus any spent today ON THIS MODEL."""
    try:
        import aethron_brain as brain
    except Exception:
        return []
    if os.environ.get("AETHRON_EMBED_KEYS"):          # tests and power users
        return [k for k in os.environ["AETHRON_EMBED_KEYS"].split(",") if k]
    # ANY SAVED GEMINI KEY, whichever provider is active for writing. Only
    # Gemini serves these embeddings, and tying them to the active
    # provider meant switching to DeepSeek quietly dropped brand memory to
    # keyword search.
    spent = brain.spent_for(EMBED_MODEL)
    return [k for k in brain.profile_keys("gemini")
            if brain._fp(k) not in spent]


def _bench(key: str):
    try:
        import aethron_brain as brain
        brain.mark_exhausted(key, EMBED_MODEL)
    except Exception:
        pass


def embed(texts: list, deadline_s=150) -> list:
    """-> one unit vector per text. Raises Unavailable with the reason."""
    keys = _keys()
    if not keys:
        raise Unavailable("no Gemini key with embedding allowance today "
                          "(meaning-based search needs one)")
    import aethron_bridge as br
    out, deadline = [], time.time() + deadline_s
    for i in range(0, len(texts), BATCH):
        part = texts[i:i + BATCH]
        body = json.dumps({"requests": [
            {"model": f"models/{EMBED_MODEL}",
             "content": {"parts": [{"text": t[:30000]}]},
             "output_dimensionality": DIMS} for t in part]}).encode()
        got, why = None, ""
        while got is None:
            if not keys:
                raise Unavailable(why or "every key has used today's "
                                  "embedding allowance")
            if time.time() > deadline:
                raise Unavailable(why or "the embedding service did not "
                                  "answer in time")
            k = keys[0]
            req = urllib.request.Request(
                f"{BASE}/models/{EMBED_MODEL}:batchEmbedContents", body,
                {"x-goog-api-key": k, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    got = [e["values"] for e in
                           json.loads(r.read())["embeddings"]]
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    kind, secs = br._quota_kind(e)
                    if kind == "spent":
                        _bench(k)
                        keys.pop(0)
                        why = "every key has used today's embedding allowance"
                        continue
                    keys.append(keys.pop(0))
                    why = "the embedding service is rate-limiting"
                    time.sleep(min(secs, 20) if len(keys) == 1 else 0.6)
                    continue
                if e.code >= 500:
                    keys.append(keys.pop(0))
                    why = f"the embedding service is busy ({e.code})"
                    time.sleep(2)
                    continue
                raise Unavailable(br._upstream_error(e))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                keys.append(keys.pop(0))
                why = f"could not reach the embedding service ({e})"
                time.sleep(2)
        out.extend(_unit(v) for v in got)
    return out


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pack_vec(v) -> str:
    return base64.b64encode(array.array("f", v).tobytes()).decode()


def _unpack_vec(s: str):
    a = array.array("f")
    a.frombytes(base64.b64decode(s))
    return a


# ───────────────────────── the pack ──────────────────────────────────

def pack_path(project: Path) -> Path:
    return Path(project) / PACK


def load_pack(project: Path) -> dict:
    try:
        return json.loads(pack_path(project).read_text(encoding="utf-8"))
    except Exception:
        return {}


def learn(project, sources, max_pages=MAX_PAGES, log=print) -> dict:
    """Read each source into the project's brand pack. Re-learning a source
    replaces it; passages whose text did not change keep their vectors."""
    project = Path(project)
    old = load_pack(project)
    cache = {hashlib.sha1(_doc_text(c).encode()).hexdigest(): c.get("v", "")
             for c in old.get("chunks", []) if c.get("v")}
    keep = [c for c in old.get("chunks", [])]
    docs, read = [], []
    for s in sources:
        s = str(s).strip()
        if not s:
            continue
        if re.match(r"(https?://)?[\w-]+(\.[\w-]+)+(/|$)", s) and \
                not Path(s).expanduser().exists():
            pages = crawl(s, max_pages=max_pages, log=log)
            read.append({"src": s, "pages": len(pages)})
            docs += pages
        else:
            f = Path(s).expanduser()
            if not f.is_file():
                log(f"  skipped {s}: not a web address or a file")
                continue
            txt = f.read_text(encoding="utf-8", errors="replace")
            if f.suffix.lower() in (".html", ".htm"):
                title, paras, _ = read_html(txt)
            elif f.suffix.lower() in (".md", ".markdown", ".txt"):
                title, paras = read_markdown(txt)
            else:
                log(f"  skipped {f.name}: only web pages, .html, .md and "
                    f".txt are read (PDF is not yet)")
                continue
            read.append({"src": str(f), "pages": 1})
            docs.append((str(f), title or f.stem, paras))
    fresh_srcs = {d[0] for d in docs} | {r["src"] for r in read}
    keep = [c for c in keep if c["src"] not in fresh_srcs
            and not any(c["src"].startswith(r["src"].rstrip("/"))
                        for r in read if r["src"].startswith("http"))]
    seen = {k for c in keep for k in (c.get("keys") or [])}
    new = []
    for src, title, paras in docs:
        new += chunk(src, title, paras, seen)
    for c in new:
        c.pop("_longest", None)
    chunks = keep + new
    todo = [c for c in chunks
            if hashlib.sha1(_doc_text(c).encode()).hexdigest() not in cache]
    for c in chunks:
        c["v"] = cache.get(hashlib.sha1(_doc_text(c).encode()).hexdigest(), "")
    dense, why = True, ""
    if todo:
        try:
            vecs = embed([_doc_text(c) for c in todo])
            for c, v in zip(todo, vecs):
                c["v"] = _pack_vec(v)
        except Unavailable as e:
            dense, why = False, str(e)
    embedded = sum(1 for c in chunks if c.get("v"))
    pack = {"version": 1, "model": EMBED_MODEL if embedded else "",
            "dims": DIMS, "built": int(time.time()),
            "sources": sorted({c["src"] for c in chunks}),
            "chunks": chunks}
    p = pack_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(pack), encoding="utf-8")
    tmp.replace(p)
    return {"read": read, "passages": len(chunks), "new": len(new),
            "embedded": embedded, "reused": len(chunks) - len(todo),
            "meaning_search": dense and embedded == len(chunks),
            "why": why}


# ───────────────────────── search ────────────────────────────────────

_TOK = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = set("""a an and are as at be by for from has have in is it its of on
or that the this to was were will with you your we our i my me us they their
not but can if so do does""".split())


def _toks(s: str) -> list:
    return [t for t in _TOK.findall(s.lower()) if t not in _STOP]


def _bm25(chunks, query, k1=1.2, b=0.75):
    docs = [_toks(_doc_text(c)) for c in chunks]
    N = len(docs) or 1
    avg = sum(len(d) for d in docs) / N or 1
    df = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    q = set(_toks(query))
    scores = []
    for i, d in enumerate(docs):
        tf, s = {}, 0.0
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        for t in q:
            if t in tf:
                idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        scores.append(s)
    return scores


def search(project, queries, k=3, pack=None) -> dict:
    """-> {"mode": "hybrid"|"keyword", "why": str,
           "results": [[{text, src, title, heading, cos, score}], …]}"""
    pack = pack if pack is not None else load_pack(project)
    chunks = pack.get("chunks") or []
    if isinstance(queries, str):
        queries = [queries]
    if not chunks:
        return {"mode": "none", "why": "this project has no brand pack yet "
                "(learn_brand reads the brand's site or documents into one)",
                "results": [[] for _ in queries]}
    mode, why, qv = "keyword", "", None
    vecs = [(_unpack_vec(c["v"]) if c.get("v") else None) for c in chunks]
    if pack.get("model") and any(v is not None for v in vecs):
        try:
            qv = embed([f"task: search result | query: {q}" for q in queries])
            mode = "hybrid"
        except Unavailable as e:
            why = str(e)
    else:
        why = "the pack was built without meaning-based vectors"
    results = []
    for qi, q in enumerate(queries):
        lex = _bm25(chunks, q)
        lex_rank = sorted((i for i in range(len(chunks)) if lex[i] > 0),
                          key=lambda i: -lex[i])
        cos = [0.0] * len(chunks)
        dense_rank = []
        if qv is not None:
            for i, v in enumerate(vecs):
                if v is not None:
                    cos[i] = sum(a * b for a, b in zip(qv[qi], v))
            dense_rank = sorted((i for i in range(len(chunks))
                                 if vecs[i] is not None and cos[i] >= MIN_COS),
                                key=lambda i: -cos[i])
        fused = {}
        for rank_list in (dense_rank, lex_rank):
            for r, i in enumerate(rank_list):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
        # A KEYWORD HIT IS NOT AN ANSWER BY ITSELF when meaning says the
        # passage is unrelated: "policy" matches a pricing policy and a
        # privacy policy alike. With vectors present, a passage must also
        # clear the meaning bar, or be the rare exact-name match that
        # carries most of the query's words.
        if qv is not None:
            qset = set(_toks(q))
            for i in list(fused):
                if cos[i] < MIN_COS:
                    share = len(qset & set(_toks(_doc_text(chunks[i])))) / \
                        (len(qset) or 1)
                    if share < 0.6:
                        fused.pop(i)
        top = sorted(fused, key=lambda i: -fused[i])[:k]
        results.append([{"text": chunks[i]["text"], "src": chunks[i]["src"],
                         "title": chunks[i].get("title", ""),
                         "heading": chunks[i].get("heading", ""),
                         "cos": round(cos[i], 3), "score": round(fused[i], 4)}
                        for i in top])
    return {"mode": mode, "why": why, "results": results}


def _strength(hit: dict, mode: str) -> str:
    """strong = it answers; loose = it is about the topic, not proof."""
    if mode != "hybrid":
        return "keyword"
    return "strong" if hit.get("cos", 0) >= STRONG_COS else "loose"


def notes_for(project, olds: list, per=2, max_chars=320) -> dict:
    """{old: [note, …]} for the lines worth grounding. The template's own
    words say what a slot is about; a label of three words does not, and
    grounding "Step 02" would only hand the writer noise."""
    pack = load_pack(project)
    if not pack.get("chunks"):
        return {"notes": {}, "mode": "none", "why": ""}
    want = [o for o in olds if len(_toks(o)) >= 3]
    if not want:
        return {"notes": {}, "mode": "", "why": ""}
    r = search(project, want, k=per, pack=pack)
    notes = {}
    for o, hits in zip(want, r["results"]):
        if hits:
            notes[o] = [{"fact": h["text"][:max_chars]
                         + ("…" if len(h["text"]) > max_chars else ""),
                         "from": (h["title"] or h["src"])
                         + (f" › {h['heading']}" if h["heading"] else ""),
                         "match": _strength(h, r["mode"])}
                        for h in hits]
    return {"notes": notes, "mode": r["mode"], "why": r["why"]}


# ───────────────────────── selftest ──────────────────────────────────

def selftest() -> int:
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, bool(ok)))
        print(("  ok   " if ok else "  FAIL ") + name
              + ("" if ok else f"   {detail}"))

    # A FAKE GOOGLE whose "meaning" is a bag of word stems — deterministic,
    # so a ranking failure is the code's fault and not the model's mood.
    # Words are folded to a 4-letter stem so "builds"/"build" meet.
    calls = []

    def fake_vec(text):
        v = [0.0] * 64
        t = text.split("|", 1)[-1]
        for w in _toks(t):
            h = int(hashlib.md5(w[:4].encode()).hexdigest(), 16)
            v[h % 64] += 1.0
        return v

    class G(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            key = self.headers.get("x-goog-api-key", "")
            calls.append((key, len(body["requests"])))
            if key == "spent-key":
                msg = json.dumps([{"error": {"code": 429, "message":
                    "You exceeded your current quota, please check your plan "
                    "and billing details.", "details": [{"violations": [{
                        "quotaId": "EmbedContentRequestsPerDayPerProject"
                                   "PerModel-FreeTier"}]}]}}]).encode()
                self.send_response(429)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            out = json.dumps({"embeddings": [
                {"values": fake_vec(r["content"]["parts"][0]["text"])}
                for r in body["requests"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), G)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    global BASE, MIN_COS
    saved = (BASE, MIN_COS, os.environ.get("AETHRON_EMBED_KEYS"))
    BASE = f"http://127.0.0.1:{srv.server_address[1]}/v1beta"
    MIN_COS = 0.30           # bag-of-words cosines run lower than the model's
    os.environ["AETHRON_EMBED_KEYS"] = "good-key"

    page = lambda title, body: (f"<html><head><title>{title}</title>"
                                "<script>var secret='x'</script></head><body>"
                                "<nav><a href='/'>Home</a> <a href='/work'>Work"
                                "</a></nav>" + body + "<footer><p>Jomiez Studio,"
                                " Lagos. hello@jomiez.com</p></footer></body>"
                                "</html>")
    home = page("Jomiez — Software Studio",
                "<h1>We build software</h1><p>Jomiez designs, builds and "
                "deploys custom web applications, mobile platforms and "
                "production AI systems for growing companies.</p>"
                "<h2>Services</h2><p>Product design, full-stack engineering, "
                "machine learning integration and ongoing support.</p>"
                "<h2>Contact</h2><a href='mailto:studio@jomiez.com'>Email us"
                "</a><div>Thank you! Your submission has been received!</div>"
                "<div>Oops! Something went wrong while submitting the form."
                "</div>")
    work = page("Work — Jomiez", "<h1>Case studies</h1><h2>Kora Health</h2>"
                "<p>Kora Health needed a patient booking platform. Jomiez "
                "shipped it in nine weeks; bookings rose 40 percent.</p>"
                "<h2>Privacy</h2><p>We only keep the email address you give us "
                "when you contact the studio, and never sell it.</p>")
    t, paras, links = read_html(home)
    ck("the page reader keeps the title and drops scripts",
       t.startswith("Jomiez") and not any("secret" in p[1] for p in paras))
    ck("each passage knows the heading it sits under",
       any(p[2] == "We build software › Services" for p in paras), str(paras))
    seen = set()
    c1 = chunk("home", t, paras, seen)
    t2, p2, _ = read_html(work)
    c2 = chunk("work", t2, p2, seen)
    ck("the footer repeated on every page is kept once, not twice",
       sum("hello@jomiez.com" in c["text"] for c in c1 + c2) == 1)
    ck("every passage is indexed with its page and heading",
       _doc_text(c2[0]).startswith("title: Work — Jomiez › Case studies"),
       _doc_text(c2[0]))

    proj = Path(tempfile.mkdtemp(prefix="aethron-rag-"))
    for name, text in (("home.html", home), ("work.html", work)):
        (proj / name).write_text(text)
    res = learn(proj, [proj / "home.html", proj / "work.html"], log=lambda *_: 0)
    texts = " ".join(c["text"] for c in load_pack(proj)["chunks"])
    ck("an email that lives only in a link becomes a fact",
       "Contact email: studio@jomiez.com" in texts, texts[:300])
    ck("a form's canned messages are not the brand's words",
       "submission" not in texts and "Oops" not in texts)
    ck("the brand pack is built and every passage embedded",
       res["passages"] >= 4 and res["embedded"] == res["passages"]
       and res["meaning_search"], str(res))
    r = search(proj, "a case study about a healthcare booking app", k=1)
    ck("a question in other words finds the case study (hybrid)",
       r["mode"] == "hybrid" and r["results"][0]
       and "Kora" in r["results"][0][0]["text"], str(r["results"]))
    r = search(proj, "Kora", k=1)
    ck("an exact name is found by its keyword",
       r["results"][0] and "Kora" in r["results"][0][0]["text"])
    r = search(proj, "quarterly tax filing deadlines in Germany", k=3)
    ck("a question the material does not answer gets NOTHING back",
       r["results"] == [[]], str(r["results"]))
    n = notes_for(proj, ["Montra is a productivity platform built for teams "
                         "and the case study of how we shipped it",
                         "Step 02"])
    ck("copy notes come for a paragraph slot, not for a three-word label",
       any("Kora" in x["fact"] for x in n["notes"].get(
           "Montra is a productivity platform built for teams and the case "
           "study of how we shipped it", [])) and "Step 02" not in n["notes"],
       str(n))
    before = len(calls)
    res2 = learn(proj, [proj / "work.html"], log=lambda *_: 0)
    ck("re-reading unchanged material embeds nothing again",
       len(calls) == before and res2["reused"] == res2["passages"], str(res2))

    import aethron_brain as brain
    saved_state = brain.KEYSTATE.read_text() if brain.KEYSTATE.exists() else None
    try:
        os.environ["AETHRON_EMBED_KEYS"] = "spent-key,good-key"
        calls.clear()
        v = embed(["title: x | text: hello there"])
        ck("a key spent for the day hands over to the next at once",
           len(v) == 1 and [c[0] for c in calls] == ["spent-key", "good-key"],
           str(calls))
        ck("  ...and is benched for the EMBEDDING model only",
           brain._fp("spent-key") in brain.spent_for(EMBED_MODEL)
           and brain._fp("spent-key") not in brain.spent_for("gemini-3.8-flash"))
    finally:
        if saved_state is None:
            brain.KEYSTATE.unlink(missing_ok=True)
        else:
            brain.KEYSTATE.write_text(saved_state)
    os.environ["AETHRON_EMBED_KEYS"] = ""
    os.environ.pop("AETHRON_EMBED_KEYS")
    import aethron_brain as _b
    real_load = _b.load
    _b.load = lambda: {**real_load(), "provider": "deepseek"}
    try:
        r = search(proj, "Kora", k=1)
        ck("with no Gemini key it falls back to keywords and SAYS so",
           r["mode"] == "keyword" and "Gemini" in r["why"]
           and r["results"][0] and "Kora" in r["results"][0][0]["text"],
           str(r)[:200])
    finally:
        _b.load = real_load
    BASE, MIN_COS = saved[0], saved[1]
    if saved[2] is not None:
        os.environ["AETHRON_EMBED_KEYS"] = saved[2]
    srv.shutdown()
    import shutil
    shutil.rmtree(proj, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[1] == "--selftest":
        return selftest()
    if argv[1] == "learn" and len(argv) >= 4:
        r = learn(Path(argv[2]), argv[3:])
        print(json.dumps(r, indent=1))
        return 0
    if argv[1] == "search" and len(argv) >= 4:
        k = int(argv[4]) if len(argv) > 4 else 3
        print(json.dumps(search(Path(argv[2]), argv[3], k=k), indent=1,
                         ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

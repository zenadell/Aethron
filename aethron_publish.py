"""Publish a finished site to the person's own Netlify account.

Aethron has no hosting of its own yet, so a site goes where the person
already can host for free: their Netlify account. One click gives them
`<name>.netlify.app`; republishing replaces the whole site at once,
every version is kept for rollback, and their own domain gets HTTPS from
Netlify. Nothing is hosted, stored or paid for by Aethron.

  connect     Netlify's own sign-in in the browser (the "ticket" flow the
              Netlify CLI uses: no secret, no redirect page), or a personal
              access token pasted by hand. The token lives in
              <AETHRON_HOME>/publish.json (0600) and never reaches a page.
  publish     the folder's files are hashed; Netlify answers which ones it
              does not already have, only those are uploaded, and the
              version goes live when Netlify says it is ready. A deploy
              that is not ready is never reported as published.
  versions    list / restore (rollback) / unpublish (delete the site).
  domain      attach a custom domain, say exactly which DNS record to add,
              and report whether the record is there and HTTPS issued.

Every request goes to AETHRON_NETLIFY_API (default the real API), which
is how the selftest drives all of it against a fake Netlify.
"""
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = os.environ.get("AETHRON_NETLIFY_API", "https://api.netlify.com/api/v1").rstrip("/")
AUTHORIZE = os.environ.get("AETHRON_NETLIFY_AUTHORIZE",
                           "https://app.netlify.com/authorize")
TOKENS_URL = "https://app.netlify.com/user/applications#personal-access-tokens"
APEX_IP = "75.2.60.5"          # Netlify's load balancer, for a bare domain

# never shipped: our own runners and reports, and anything hidden
SKIP_NAMES = {"serve.py", "README.txt", "DEPLOY.md", "vercel.json",
              "__pycache__", "Thumbs.db"}
MAX_FILE = 100 * 1024 * 1024   # Netlify refuses larger single files
LOCK = threading.Lock()


class PublishError(Exception):
    """A failure with a sentence a person can act on."""


def home() -> Path:
    return Path(os.environ.get("AETHRON_HOME") or Path(__file__).resolve().parent)


def _read(name, default):
    try:
        return json.loads((home() / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(name, data):
    f = home() / name
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(f)


# ── the account ───────────────────────────────────────────────────────
def client_id() -> str:
    v = os.environ.get("AETHRON_NETLIFY_CLIENT_ID")
    if v:
        return v
    for base in (home(), Path(__file__).resolve().parent):
        try:
            v = json.loads((base / "aethron_config.json").read_text()).get(
                "netlify_client_id")
            if v:
                return v
        except Exception:
            pass
    return ""


def _token():
    return (_read("publish.json", {}).get("netlify") or {}).get("token") or ""


def account():
    """What the screen may know: who is connected. Never the token."""
    n = _read("publish.json", {}).get("netlify") or {}
    if not n.get("token"):
        return {"connected": False, "browser_signin": bool(client_id())}
    return {"connected": True, "email": n.get("email", ""),
            "name": n.get("name", ""), "browser_signin": bool(client_id())}


def _req(method, path, body=None, token=None, raw=None, ctype=None,
         timeout=60, tries=4):
    """One API call. Retries what is worth retrying (rate limits, 5xx,
    dropped connections); turns everything else into a PublishError."""
    url = path if path.startswith("http") else API + path
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    tok = token if token is not None else _token()
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        req.add_header("User-Agent", "Aethron")
        if data is not None:
            req.add_header("Content-Type", ctype or (
                "application/octet-stream" if raw is not None else "application/json"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:400]
            if e.code == 429 or e.code >= 500:
                wait = e.headers.get("Retry-After") if e.headers else None
                last = f"Netlify answered {e.code}"
                time.sleep(min(float(wait or 0) or 1.5 * (attempt + 1), 20))
                continue
            try:
                msg = json.loads(msg).get("message") or msg
            except Exception:
                pass
            if e.code == 401:
                raise PublishError("Netlify no longer accepts Aethron's sign-in. "
                                   "Connect your Netlify account again.") from None
            err = PublishError(f"Netlify refused this ({e.code}): {msg}".strip())
            err.code = e.code
            raise err from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            last = f"could not reach Netlify ({getattr(e, 'reason', e)})"
            time.sleep(1.5 * (attempt + 1))
    raise PublishError(last or "could not reach Netlify")


def connect_token(token: str):
    """A personal access token pasted by the person: checked, then kept."""
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]{20,200}", token):
        raise PublishError("That doesn't look like a Netlify access token.")
    user = _req("GET", "/user", token=token)
    return _save_account(token, user)


def _save_account(token, user):
    with LOCK:
        d = _read("publish.json", {})
        d["netlify"] = {"token": token, "email": user.get("email", ""),
                        "name": user.get("full_name") or user.get("slug") or "",
                        "at": time.time()}
        _write("publish.json", d)
    return account()


def disconnect():
    with LOCK:
        d = _read("publish.json", {})
        d.pop("netlify", None)
        _write("publish.json", d)
    return account()


def signin_start():
    """Netlify's browser sign-in, as the Netlify CLI does it: a ticket is
    created, the person approves it on netlify.com, and Aethron exchanges
    it for a token. Returns (ticket_id, url to open)."""
    cid = client_id()
    if not cid:
        raise PublishError("Browser sign-in isn't set up in this copy of Aethron. "
                           "Use an access token instead.")
    t = _req("POST", "/oauth/tickets?client_id=" + urllib.parse.quote(cid),
             body={}, token="")
    tid = t.get("id")
    if not tid:
        raise PublishError("Netlify did not start a sign-in.")
    url = AUTHORIZE + "?" + urllib.parse.urlencode(
        {"response_type": "ticket", "ticket": tid})
    return tid, url


def signin_poll(ticket_id: str):
    """'pending' until the person approves; then the account is saved."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,80}", ticket_id or ""):
        raise PublishError("unknown sign-in")
    t = _req("GET", f"/oauth/tickets/{ticket_id}", token="")
    if not t.get("authorized"):
        return {"status": "pending"}
    ex = _req("POST", f"/oauth/tickets/{ticket_id}/exchange", body={}, token="")
    tok = ex.get("access_token")
    if not tok:
        raise PublishError("Netlify approved the sign-in but sent no token.")
    user = _req("GET", "/user", token=tok)
    out = _save_account(tok, user)
    out["status"] = "ok"
    return out


# ── what gets uploaded ────────────────────────────────────────────────
def folder_files(folder: Path):
    """{"/path": Path} for every file of a built site worth shipping."""
    folder = Path(folder).resolve()
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder)
        if any(part.startswith(".") or part in SKIP_NAMES for part in rel.parts):
            continue
        if p.is_file() and not p.is_symlink():
            out["/" + rel.as_posix()] = p
    if "/index.html" not in out:
        raise PublishError("There's no index.html to publish. Build the site first.")
    return out


REF_RE = re.compile(r"""(?:src|href|poster)\s*=\s*["']([^"'#?]+)|url\(\s*["']?([^"')#?]+)""",
                    re.I)


def page_files(page: Path):
    """A rebuilt page ships as index.html plus only the files it refers to
    (never the screenshot it was made from, or its history)."""
    page = Path(page).resolve()
    base = page.parent
    out = {"/index.html": page}
    html = page.read_text(encoding="utf-8", errors="replace")
    for m in REF_RE.finditer(html):
        ref = (m.group(1) or m.group(2) or "").strip()
        if not ref or re.match(r"^(?:[a-z]+:|//|data:)", ref, re.I):
            continue
        p = (base / urllib.parse.unquote(ref.lstrip("/"))).resolve()
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        if p.is_file() and not any(x.startswith(".") for x in rel.parts):
            out["/" + rel.as_posix()] = p
    return out


def _sha1(p: Path):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ── a site, published ─────────────────────────────────────────────────
def records():
    return _read("published.json", {})


def record(key):
    return records().get(key)


def _save_record(key, rec):
    with LOCK:
        d = _read("published.json", {})
        if rec is None:
            d.pop(key, None)
        else:
            d[key] = rec
        _write("published.json", d)


def slug(text):
    s = re.sub(r"[^a-z0-9-]+", "-", str(text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:58].strip("-")
    return s or "site"


def _create_site(name, log):
    base = slug(name)
    tries = [base] + [f"{base}-{n}" for n in range(2, 6)] + [
        f"{base}-{os.urandom(2).hex()}" for _ in range(3)]
    for cand in tries:
        try:
            site = _req("POST", "/sites", body={"name": cand})
            log(f"created {site.get('name')}.netlify.app on your Netlify account")
            return site
        except PublishError as e:
            if getattr(e, "code", 0) == 422:
                continue                 # the name is taken: try the next
            raise
    raise PublishError(f"Every address like {base}.netlify.app is taken. "
                       "Choose another name.")


def _site_or_none(site_id):
    try:
        return _req("GET", f"/sites/{site_id}")
    except PublishError as e:
        if getattr(e, "code", 0) == 404:
            return None
        raise


def publish(key, files, name="", log=print, title=""):
    """Upload `files` ({"/path": Path}) as a new version of the site kept
    under `key`, creating the site the first time. Returns the record."""
    if not _token():
        raise PublishError("Connect your Netlify account first.")
    if not files or "/index.html" not in files:
        raise PublishError("There's no index.html to publish.")
    big = [k for k, p in files.items() if p.stat().st_size > MAX_FILE]
    if big:
        raise PublishError(f"{big[0]} is over 100 MB, which Netlify doesn't accept.")

    rec = record(key) or {}
    site = _site_or_none(rec["site_id"]) if rec.get("site_id") else None
    if rec.get("site_id") and not site:
        log("the site was deleted on Netlify; creating it again")
    if not site:
        site = _create_site(name or rec.get("name") or key.split(":")[-1], log)

    log(f"checking {len(files)} files …")
    digests = {k: _sha1(p) for k, p in files.items()}
    by_sha = {}
    for k, s in digests.items():
        by_sha.setdefault(s, k)
    dep = _req("POST", f"/sites/{site['id']}/deploys",
               body={"files": digests, "async": True})
    t0 = time.time()
    while dep.get("state") in ("preparing", "new"):
        if time.time() - t0 > 300:
            raise PublishError("Netlify took too long to prepare the upload.")
        time.sleep(1)
        dep = _req("GET", f"/deploys/{dep['id']}")
    if dep.get("state") == "error":
        raise PublishError("Netlify could not prepare the upload: "
                           + (dep.get("error_message") or "no reason given"))
    need = [by_sha[s] for s in (dep.get("required") or []) if s in by_sha]
    log(f"uploading {len(need)} new or changed file(s)"
        + (f", {len(files) - len(need)} already on Netlify" if len(files) > len(need) else ""))

    done = [0]

    def put(path):
        _req("PUT", f"/deploys/{dep['id']}/files/"
             + urllib.parse.quote(path.lstrip("/"), safe="/"),
             raw=files[path].read_bytes(), timeout=300)
        with LOCK:
            done[0] += 1
            n = done[0]
        if n == len(need) or n % max(1, len(need) // 10) == 0:
            log(f"uploaded {n}/{len(need)} ({round(100 * n / len(need))}%)")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(put, need))

    log("going live …")
    t0 = time.time()
    while True:
        dep = _req("GET", f"/deploys/{dep['id']}")
        state = dep.get("state")
        if state == "ready":
            break
        if state == "error":
            raise PublishError("Netlify could not publish this version: "
                               + (dep.get("error_message") or "no reason given"))
        if time.time() - t0 > 300:
            raise PublishError("Netlify didn't finish publishing within 5 minutes. "
                               "It may still appear; check again shortly.")
        time.sleep(1.2)

    site = _req("GET", f"/sites/{site['id']}")
    url = site.get("ssl_url") or site.get("url") or f"https://{site['name']}.netlify.app"
    rec = {"site_id": site["id"], "name": site.get("name"),
           "url": url, "default_url": f"https://{site.get('name')}.netlify.app",
           "admin_url": site.get("admin_url", ""),
           "custom_domain": site.get("custom_domain") or "",
           "deploy_id": dep["id"], "published_at": time.time(),
           "files": len(files), "title": title or rec.get("title", "")}
    _save_record(key, rec)
    log(f"published: {url}")
    return rec


def versions(key, limit=12):
    rec = record(key)
    if not rec:
        return []
    site = _site_or_none(rec["site_id"])
    if not site:
        return []
    live = site.get("published_deploy", {}).get("id") or rec.get("deploy_id")
    out = []
    for d in _req("GET", f"/sites/{rec['site_id']}/deploys?per_page={limit}") or []:
        if d.get("state") != "ready":
            continue
        out.append({"id": d["id"], "at": d.get("published_at") or d.get("created_at"),
                    "live": d["id"] == live})
    return out


def rollback(key, deploy_id):
    rec = record(key)
    if not rec:
        raise PublishError("This site isn't published.")
    if not re.fullmatch(r"[a-f0-9]{8,40}", deploy_id or ""):
        raise PublishError("unknown version")
    _req("POST", f"/sites/{rec['site_id']}/deploys/{deploy_id}/restore", body={})
    rec["deploy_id"] = deploy_id
    _save_record(key, rec)
    return rec


def rename(key, name):
    rec = record(key)
    if not rec:
        raise PublishError("This site isn't published.")
    new = slug(name)
    if new == rec.get("name"):
        return rec
    try:
        site = _req("PATCH", f"/sites/{rec['site_id']}", body={"name": new})
    except PublishError as e:
        if getattr(e, "code", 0) == 422:
            raise PublishError(f"{new}.netlify.app is taken. Try another name.") from None
        raise
    rec.update(name=site.get("name", new),
               default_url=f"https://{site.get('name', new)}.netlify.app")
    if not rec.get("custom_domain"):
        rec["url"] = rec["default_url"]
    _save_record(key, rec)
    return rec


def unpublish(key):
    rec = record(key)
    if not rec:
        return None
    try:
        _req("DELETE", f"/sites/{rec['site_id']}")
    except PublishError as e:
        if getattr(e, "code", 0) != 404:
            raise
    _save_record(key, None)
    return None


# ── their own domain ──────────────────────────────────────────────────
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def clean_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0].rstrip(".")
    if not DOMAIN_RE.match(d) or d.endswith(".netlify.app"):
        raise PublishError("Type a domain like yourbrand.com or www.yourbrand.com.")
    return d


def _is_apex(d):
    parts = d.split(".")
    # two-part public suffixes (co.uk, com.ng…): example.co.uk is an apex
    if len(parts) == 3 and parts[-2] in ("co", "com", "org", "net", "gov", "edu", "ac"):
        return True
    return len(parts) == 2


def dns_records(domain, site_name):
    if _is_apex(domain):
        return [{"type": "A", "host": "@", "value": APEX_IP},
                {"type": "CNAME", "host": "www", "value": f"{site_name}.netlify.app"}]
    host = domain.split(".")[0]
    return [{"type": "CNAME", "host": host, "value": f"{site_name}.netlify.app"}]


def set_domain(key, domain):
    rec = record(key)
    if not rec:
        raise PublishError("Publish the site first, then add your domain.")
    d = clean_domain(domain)
    body = {"custom_domain": d}
    if _is_apex(d):
        body["domain_aliases"] = ["www." + d]
    try:
        _req("PATCH", f"/sites/{rec['site_id']}", body=body)
    except PublishError as e:
        if getattr(e, "code", 0) == 422:
            raise PublishError(f"{d} is already attached to another Netlify site. "
                               "Remove it there first.") from None
        raise
    rec["custom_domain"] = d
    _save_record(key, rec)
    return domain_status(key)


def remove_domain(key):
    rec = record(key)
    if not rec:
        raise PublishError("This site isn't published.")
    _req("PATCH", f"/sites/{rec['site_id']}", body={"custom_domain": None,
                                                     "domain_aliases": []})
    rec["custom_domain"] = ""
    rec["url"] = rec.get("default_url") or rec["url"]
    _save_record(key, rec)
    return rec


def _resolve(host):
    try:
        return {a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return set()


def domain_status(key):
    """Is the DNS record there, and has Netlify issued HTTPS?"""
    rec = record(key)
    if not rec or not rec.get("custom_domain"):
        return {"domain": "", "records": []}
    d, name = rec["custom_domain"], rec["name"]
    want = _resolve(f"{name}.netlify.app") | {APEX_IP}
    got = _resolve(d)
    dns_ok = bool(got & want)
    https = "waiting"
    if dns_ok:
        try:
            ssl = _req("GET", f"/sites/{rec['site_id']}/ssl", tries=2)
            https = "ready" if ssl.get("state") in ("issued", "custom") else "pending"
            if https != "ready":
                try:
                    _req("POST", f"/sites/{rec['site_id']}/ssl", body={}, tries=1)
                except PublishError:
                    pass
        except PublishError:
            https = "pending"
    if dns_ok and https == "ready":
        rec["url"] = f"https://{d}"
        _save_record(key, rec)
    return {"domain": d, "records": dns_records(d, name), "dns": dns_ok,
            "https": https, "url": rec["url"]}


# ── selftest: everything above, against a fake Netlify ────────────────
def fake_netlify():
    """An in-process Netlify that keeps the rules this module relies on:
    unique names, upload-only-what-is-required, sha1 checked on upload,
    a deploy is 'ready' only when every required file arrived."""
    import http.server
    st = {"sites": {}, "deploys": {}, "blobs": {}, "tickets": {},
          "token": "tok_" + "a" * 24, "puts": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _auth(self):
            ok = self.headers.get("Authorization") == "Bearer " + st["token"]
            if not ok:
                self._j({"message": "Access Denied"}, 401)
            return ok

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            p = urllib.parse.urlparse(self.path).path.replace("/api/v1", "")
            if p.startswith("/oauth/tickets/"):
                t = st["tickets"].get(p.split("/")[3])
                return self._j(t) if t else self._j({}, 404)
            if not self._auth():
                return
            if p == "/user":
                return self._j({"email": "you@example.com", "full_name": "You"})
            m = re.fullmatch(r"/sites/([^/]+)(/deploys|/ssl)?", p)
            if m:
                s = st["sites"].get(m.group(1))
                if not s:
                    return self._j({}, 404)
                if m.group(2) == "/deploys":
                    ds = [d for d in st["deploys"].values() if d["site_id"] == s["id"]]
                    return self._j(sorted(ds, key=lambda d: -d["n"]))
                if m.group(2) == "/ssl":
                    return self._j({"state": "issued"})
                live = st["deploys"].get(s.get("live") or "", {})
                return self._j(dict(s, published_deploy={"id": live.get("id")}))
            m = re.fullmatch(r"/deploys/([^/]+)", p)
            if m:
                d = st["deploys"].get(m.group(1))
                if not d:
                    return self._j({}, 404)
                if d["state"] == "uploading" and all(
                        x in st["blobs"] for x in d["required"]):
                    d["state"] = "ready"
                    st["sites"][d["site_id"]]["live"] = d["id"]
                return self._j(d)
            self._j({}, 404)

        def do_POST(self):
            p = urllib.parse.urlparse(self.path).path.replace("/api/v1", "")
            body = json.loads(self._body() or b"{}")
            if p == "/oauth/tickets":
                tid = "tk" + os.urandom(4).hex()
                st["tickets"][tid] = {"id": tid, "authorized": False}
                return self._j(st["tickets"][tid], 201)
            m = re.fullmatch(r"/oauth/tickets/([^/]+)/exchange", p)
            if m:
                t = st["tickets"].get(m.group(1))
                if not t or not t["authorized"]:
                    return self._j({}, 401)
                return self._j({"access_token": st["token"]})
            if not self._auth():
                return
            if p == "/sites":
                name = body.get("name") or "random"
                if any(s["name"] == name for s in st["sites"].values()):
                    return self._j({"message": "subdomain must be unique"}, 422)
                sid = "s" + os.urandom(6).hex()
                st["sites"][sid] = {"id": sid, "name": name, "custom_domain": None,
                                    "url": f"http://{name}.netlify.app",
                                    "ssl_url": f"https://{name}.netlify.app",
                                    "admin_url": f"https://app.netlify.com/sites/{name}"}
                return self._j(st["sites"][sid], 201)
            m = re.fullmatch(r"/sites/([^/]+)/deploys", p)
            if m and m.group(1) in st["sites"]:
                did = os.urandom(12).hex()
                files = body.get("files") or {}
                req = sorted({s for s in files.values() if s not in st["blobs"]})
                st["deploys"][did] = {"id": did, "site_id": m.group(1),
                                      "files": files, "required": req,
                                      "state": "uploading", "n": len(st["deploys"]),
                                      "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
                return self._j(st["deploys"][did])
            m = re.fullmatch(r"/sites/([^/]+)/deploys/([^/]+)/restore", p)
            if m and m.group(2) in st["deploys"]:
                st["sites"][m.group(1)]["live"] = m.group(2)
                return self._j(st["deploys"][m.group(2)])
            if re.fullmatch(r"/sites/([^/]+)/ssl", p):
                return self._j({"state": "issued"})
            self._j({}, 404)

        def do_PUT(self):
            p = urllib.parse.urlparse(self.path).path.replace("/api/v1", "")
            if not self._auth():
                return
            m = re.fullmatch(r"/deploys/([^/]+)/files/(.+)", p)
            d = st["deploys"].get(m.group(1)) if m else None
            if not d:
                return self._j({}, 404)
            path = "/" + urllib.parse.unquote(m.group(2))
            data = self._body()
            if hashlib.sha1(data).hexdigest() != d["files"].get(path):
                return self._j({"message": "sha mismatch"}, 422)
            st["blobs"][d["files"][path]] = data
            st["puts"] += 1
            return self._j({"path": path})

        def do_PATCH(self):
            p = urllib.parse.urlparse(self.path).path.replace("/api/v1", "")
            if not self._auth():
                return
            body = json.loads(self._body() or b"{}")
            m = re.fullmatch(r"/sites/([^/]+)", p)
            s = st["sites"].get(m.group(1)) if m else None
            if not s:
                return self._j({}, 404)
            if "name" in body:
                if any(x["name"] == body["name"] and x is not s for x in st["sites"].values()):
                    return self._j({"message": "taken"}, 422)
                s["name"] = body["name"]
            if "custom_domain" in body:
                s["custom_domain"] = body["custom_domain"]
            return self._j(s)

        def do_DELETE(self):
            p = urllib.parse.urlparse(self.path).path.replace("/api/v1", "")
            if not self._auth():
                return
            m = re.fullmatch(r"/sites/([^/]+)", p)
            if m and st["sites"].pop(m.group(1), None):
                self.send_response(204)
                self.end_headers()
                return
            self._j({}, 404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st, f"http://127.0.0.1:{srv.server_address[1]}/api/v1"


def selftest():
    import tempfile
    global API
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print(("PASS " if cond else "FAIL ") + name)

    srv, st, url = fake_netlify()
    saved = (API, os.environ.get("AETHRON_HOME"), os.environ.get("AETHRON_NETLIFY_CLIENT_ID"))
    API = url
    with tempfile.TemporaryDirectory() as d:
        os.environ["AETHRON_HOME"] = d
        os.environ.pop("AETHRON_NETLIFY_CLIENT_ID", None)
        try:
            site = Path(d) / "site"
            (site / "assets").mkdir(parents=True)
            (site / "index.html").write_text("<h1>hi</h1>")
            (site / "about.html").write_text("<h1>about</h1>")
            (site / "assets" / "a b.css").write_text("body{}")
            (site / "serve.py").write_text("x")
            (site / ".forge-report.json").write_text("{}")

            check("not connected at first", not account()["connected"])
            try:
                connect_token("short")
                check("a malformed token is refused", False)
            except PublishError:
                check("a malformed token is refused", True)
            try:
                connect_token("tok_" + "b" * 24)
                check("a token Netlify rejects is refused", False)
            except PublishError as e:
                check("a token Netlify rejects is refused", "Connect" in str(e))
            a = connect_token(st["token"])
            check("a real token connects and names the account",
                  a["connected"] and a["email"] == "you@example.com")
            check("the token never appears in what the screen gets",
                  st["token"] not in json.dumps(account()))
            check("the token file is private",
                  (os.stat(Path(d) / "publish.json").st_mode & 0o077) == 0)

            files = folder_files(site)
            check("our own runner and reports are not shipped",
                  set(files) == {"/index.html", "/about.html", "/assets/a b.css"})

            st["sites"]["x"] = {"id": "x", "name": "my-site"}   # name taken
            rec = publish("project:my-site", files, "my-site", log=lambda m: None)
            check("a taken name gets the next free one", rec["name"] == "my-site-2")
            check("published URL is https on netlify.app",
                  rec["url"] == "https://my-site-2.netlify.app")
            check("every file was uploaded once", st["puts"] == 3)
            dep = st["deploys"][rec["deploy_id"]]
            check("the version is live only after every file arrived",
                  dep["state"] == "ready")
            check("a file with a space in its name arrived intact",
                  st["blobs"][dep["files"]["/assets/a b.css"]] == b"body{}")

            first = rec["deploy_id"]
            (site / "about.html").write_text("<h1>about us</h1>")
            puts = st["puts"]
            rec2 = publish("project:my-site", folder_files(site), log=lambda m: None)
            check("republish keeps the same site", rec2["site_id"] == rec["site_id"])
            check("republish uploads only what changed", st["puts"] - puts == 1)

            vs = versions("project:my-site")
            check("both versions are listed, the new one live",
                  len(vs) == 2 and vs[0]["live"] and not vs[1]["live"])
            rollback("project:my-site", first)
            check("rollback makes the old version live",
                  st["sites"][rec["site_id"]]["live"] == first)

            r = rename("project:my-site", "Bakery Shop")
            check("rename moves the address",
                  r["url"] == "https://bakery-shop.netlify.app")

            try:
                set_domain("project:my-site", "not a domain")
                check("a nonsense domain is refused", False)
            except PublishError:
                check("a nonsense domain is refused", True)
            s = set_domain("project:my-site", "https://Bakery.Example.COM/")
            check("a subdomain asks for one CNAME to the site",
                  s["domain"] == "bakery.example.com"
                  and s["records"] == [{"type": "CNAME", "host": "bakery",
                                        "value": "bakery-shop.netlify.app"}])
            check("an apex domain asks for the A record and www",
                  [x["type"] for x in dns_records("example.com", "n")] == ["A", "CNAME"]
                  and dns_records("example.co.uk", "n")[0]["value"] == APEX_IP)
            check("HTTPS is not claimed while DNS points elsewhere",
                  s["dns"] is False and s["https"] == "waiting")
            remove_domain("project:my-site")
            check("removing the domain returns to netlify.app",
                  record("project:my-site")["url"] == "https://bakery-shop.netlify.app")

            page = Path(d) / "page"
            page.mkdir()
            (page / "shot.png").write_bytes(b"x")
            (page / "logo.svg").write_text("<svg/>")
            (page / "site.html").write_text('<img src="logo.svg"><img src="https://x/y.png">')
            pf = page_files(page / "site.html")
            check("a page ships as index.html plus what it uses, not its screenshot",
                  set(pf) == {"/index.html", "/logo.svg"})

            # the site was deleted on Netlify behind Aethron's back
            st["sites"].pop(record("project:my-site")["site_id"])
            r3 = publish("project:my-site", folder_files(site), log=lambda m: None)
            check("a site deleted on Netlify is created again",
                  r3["site_id"] in st["sites"])

            unpublish("project:my-site")
            check("unpublish deletes the site and forgets it",
                  record("project:my-site") is None and r3["site_id"] not in st["sites"])

            disconnect()
            try:
                publish("project:x", folder_files(site), log=lambda m: None)
                check("publishing needs an account", False)
            except PublishError as e:
                check("publishing needs an account", "Connect" in str(e))

            os.environ["AETHRON_NETLIFY_CLIENT_ID"] = "cid123"
            tid, u = signin_start()
            check("browser sign-in opens Netlify's own page",
                  u.startswith(AUTHORIZE) and "ticket=" + tid in u)
            check("…and waits until it is approved", signin_poll(tid)["status"] == "pending")
            st["tickets"][tid]["authorized"] = True
            check("…then the account is connected",
                  signin_poll(tid)["status"] == "ok" and account()["connected"])
        finally:
            API = saved[0]
            for k, v in (("AETHRON_HOME", saved[1]), ("AETHRON_NETLIFY_CLIENT_ID", saved[2])):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            srv.shutdown()
    print(f"\n{ok} passed, {fail} failed")
    return fail == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(json.dumps(account(), indent=1))

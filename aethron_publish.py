"""Publish a finished site: to Aethron's own hosting (<name>.aethron.site,
on Cloudflare's edge, see cloud/hosting/worker.js) or to the person's own
Netlify account.

Netlify is where the person already can host for free: their account. One click gives them
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


# ── "Built with Aethron": the badge on a published site ───────────────
SITE_URL = "https://aethron.jomiez.com/?ref=badge"
BADGE_MARK = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAYAAACM/rhtAAAFaElEQVR42t2ZSYhcVRSG///crrSdoaveq0qcGkWNqChOGDDOiqAuEhFbCI7RxJV7F7oSXbh0oSKi4oCguHTGcaHgyjHOEdRoxFD9qlKlAVN17+8i98mj7XTdqsSNDy41UPed884537nnnCImuwjAjbnHAxD+bxfH/L0BCPV6/QQzu1cS4z1C5TeqWCoAMJIG4L5Op7OrvEeqwKlJFDSzbWa2TUrzGEl4738CcP+4Co5rQa5fv37FwsLCVwCOi5YadY8AwCR93+12zwIwHCcWbQzlHAC12+2rSJ4Y99aiF5ZbKwCYmZ2W5/nlUTn3Xyio6K47q58TrwAAIYTt/xUkBiCsW7fuxMFg8CWA6TH3CwAl/WFmpxZF8WtqLNo4lh4OhzeSPCLmNI5pCG9mqyVtGUd2qoIewIoQws2RXDuElHZrjEF/uBR0AJRl2ZVmdkpJ5QQKmqQA4MxGo3FRKizJgiTdUQ34Ca9AEiS3HS5IDEDI83wuhPANyVWJuW9ZWADsrdVqp+zZs+f3UbBYioUlbTGzVRPAsSQsJOv79++fT9HBEuCYknTbhCfPcl67I8r3kyroAKher19qZmfEALfDoKBJCiTPybLs/FGwjBRIcvthgGMpWCjp9kkhMQCh1WodPRwOvyW55hDhOBgshff+5F6vV8TPSrWgxbNz3szWJMDhSQLAoySfiO99Aiy5c+76Skglu9gDcJXcxxRPkHzEzB4bByhJ20uFUxV0ANRoNC4EcHYCHIGkSfq0KIqv2+325yGEbxKKASdJJDc0m83zDgaLLQPH7dFVISGeQPKZ+H5A8pnEvZ4kQwi3pVJMAH716tUtAJtjYeBGKOdCCD0ze7Hy/fOS/iy9McKKAHBDvV5vLBXrtoR7UavV5knmqXCQfLndbv8W97tOp7NL0ms8YMYUWNaRvHYpWGwJOKwCR1KxEUJ48t8RwqfGKUgOlm9tMRx5nm8geZ4O2N6NgiOEsGPv3r0fxP6kbDlds9l8T9L3qbAA2NhoNM5aXM796+lCCFsTXPPPk5J8DsAgrhDXcOfOnX+RfH4MWByArYv1YuVVs7OzuXPuWwCtESeHYg7bF3uM3xuNxt0AriE5lPRSt9t9ZHZ29iTn3I6EHqZsTXc7505dWFjolzpZNTCnpqauI9lKhIMAXiuK4pcsy15wzj1gZheSvNQ593CWZQ/3er0fJL2bYEWLPcsxIYRNVZ2qFkSj0fjAzC6Q5FPibzAYbDSzv5xzH0saREGl5R3JOQCnA3gzIeF7kk7SO51O58p/phiVsupckuenwBHdsbPf739kZhsqFnflOOVAsaIzi6J4S9LPY8BySZ7np5VyrLSemW2NQ56RcJCEpKcBwHv/YyUhh3LMFgdLu+L3z40BS03SLaXrDcCw2WyukTSf0FIqPumfJJ8FwF6v914I4Q0zq5E0ks7MpiQ93ul0dsR++llJ+xO6OIs63Dg3NzcDwJeJdpOZHVUm6lEnh6TXK6O0wfT09HwI4UFJn0n6xHt/T7fbvat0e7/f/64Cix8FC8nj+/3+1VU43jazK2IgL/eUA5K1EMKmbrf7SmIDPgVgmOf5PICXIky1EUYwSa90Op3NlmXZ6SQ3RnIVBS61hjE+vmi1Wm8tquEWj4RdJUN4AFy5cuWrkr4jWYsjuIPJkSQv6bK1a9eudzMzMw+Z2bnlJHS5JelTADft3r37t0pKWVzGLzX5sl6vN5ienv6Q5MUkjxwly8ymvfermGXZFgCzJAeSXJki4knhAIikB7CrKIr345HGCQbiBKC5ubmZffv2Xey9P9Y5V/bdJBmq94yy/5iobTyUlnPSvxNS+ocyzx3qXwmMiibJ/Bs0j5h/mPcEDgAAAABJRU5ErkJggg=="
# A closed shadow root, so the site's own CSS can neither restyle it nor
# be restyled by it. Bottom-right, like Framer's; when the host draws its
# own badge there (Netlify's free plan does, in an iframe titled "Powered
# by Netlify") it moves to the bottom-left so neither covers the other.
BADGE_TAG = """<script data-aethron-badge>(function(){
if(window.__aeBadge||!document.body||!document.body.attachShadow)return;window.__aeBadge=1;
var host=document.createElement('div');host.setAttribute('data-aethron-badge','');
host.style.cssText='position:fixed;z-index:2147483000;bottom:16px;right:16px;left:auto';
var r=host.attachShadow({mode:'closed'});
r.innerHTML='<style>'+
':host{all:initial}'+
'a{display:flex;align-items:center;gap:8px;height:32px;padding:0 12px 0 9px;border-radius:10px;'+
'background:#fff;color:#111;text-decoration:none;font:500 12px/1 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif;'+
'letter-spacing:-.01em;box-shadow:0 0 0 1px rgba(0,0,0,.07),0 2px 4px rgba(0,0,0,.06),0 8px 22px -6px rgba(0,0,0,.18);'+
'opacity:0;transform:translateY(6px);transition:opacity .4s ease,transform .4s cubic-bezier(.23,1,.32,1),box-shadow .2s}'+
'a.on{opacity:1;transform:none}'+
'a:hover{box-shadow:0 0 0 1px rgba(0,0,0,.1),0 2px 4px rgba(0,0,0,.08),0 12px 28px -6px rgba(0,0,0,.24)}'+
'a:focus-visible{outline:2px solid #d97757;outline-offset:2px}'+
'img{width:16px;height:16px;display:block}'+
'.t{color:#555}.t b{color:#111;font-weight:600}'+
'.s{width:1px;height:14px;background:rgba(0,0,0,.12)}'+
'.c{color:#c4593a;font-weight:600;display:flex;align-items:center;gap:4px}'+
'.c i{font-style:normal;transition:transform .2s}a:hover .c i{transform:translateX(2px)}'+
'@media (max-width:480px){a{height:28px;font-size:11px;padding:0 10px 0 8px;gap:6px}img{width:14px;height:14px}}'+
'@media (prefers-reduced-motion:reduce){a{transition:none;transform:none}}'+
'@media print{a{display:none}}'+
'</style><a href="__URL__" target="_blank" rel="noopener" aria-label="Built with Aethron. Try Aethron">'+
'<img src="__IMG__" alt=""><span class="t">Built with <b>Aethron</b></span><span class="s"></span>'+
'<span class="c">Try Aethron<i>&rarr;</i></span></a>';
document.body.appendChild(host);
var a=r.querySelector('a');setTimeout(function(){a.classList.add('on')},900);
function other(){var f=document.querySelector('#nl-badge-frame,#nl-hud-frame,iframe[title="Powered by Netlify"],iframe[title="Netlify"]');
if(!f)return null;var b=f.getBoundingClientRect();return b.width>0&&b.right>innerWidth/2?b:null}
function place(){var b=other(),me=a.getBoundingClientRect().width||240,s=host.style;
s.left='auto';s.right='16px';s.bottom='16px';if(!b)return;
if(innerWidth-b.width-32>=me){s.left='16px';s.right='auto'}
else s.bottom=Math.round(innerHeight-b.top+8)+'px'}
place();var n=0,t=setInterval(function(){place();if(++n>20)clearInterval(t)},500);
addEventListener('resize',place);
})();</script>""".replace("__URL__", SITE_URL).replace("__IMG__", BADGE_MARK)


def badge_choice():
    """(can_hide, why). The badge is shown on every published site; it
    can be switched off on Pro and Studio. During the beta, when billing
    is not enforced, everyone may switch it off."""
    try:
        import aethron_access as A
        if not A.enforced():
            return True, ""
        plan = A._recorded()[0] or "free"
        if plan in ("pro", "studio"):
            return True, ""
        return False, "Removing the badge is part of Pro and Studio."
    except Exception:
        return True, ""


def with_badge(html: bytes) -> bytes:
    """The page with the badge before its closing body tag (once)."""
    if b"data-aethron-badge" in html:
        return html
    tag = BADGE_TAG.encode()
    i = html.lower().rfind(b"</body>")
    return html[:i] + tag + html[i:] if i >= 0 else html + tag


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


# ── Aethron's own hosting: <name>.aethron.site ───────────────────────
# A Cloudflare Worker (cloud/hosting/worker.js) serves every site from
# Cloudflare's edge. This copy proves who it is with its own device token
# — the same one the rest of Aethron uses — and holds no Cloudflare key.
HOSTING_API = os.environ.get("AETHRON_HOSTING_API", "https://aethron.site/__api").rstrip("/")
HOSTING_ROOT = os.environ.get("AETHRON_HOSTING_ROOT", "aethron.site")
_HOSTING_UP = {"at": 0.0, "ok": False}


def _device_token():
    try:
        import aethron_control
        return aethron_control.device_token()
    except Exception:
        return ""


def hosting():
    """Is Aethron hosting usable from this copy? {ready, root, why}."""
    if os.environ.get("AETHRON_HOSTING", "on") == "off":
        return {"ready": False, "root": HOSTING_ROOT, "why": "switched off"}
    if not _device_token():
        return {"ready": False, "root": HOSTING_ROOT, "why": "sign in to Aethron to publish here"}
    if time.time() - _HOSTING_UP["at"] > (600 if _HOSTING_UP["ok"] else 60):
        try:
            with urllib.request.urlopen(HOSTING_API + "/v1/health", timeout=6) as r:
                _HOSTING_UP["ok"] = json.loads(r.read() or b"{}").get("ok") is True
        except Exception:
            _HOSTING_UP["ok"] = False
        _HOSTING_UP["at"] = time.time()
    if not _HOSTING_UP["ok"]:
        return {"ready": False, "root": HOSTING_ROOT, "why": "Aethron hosting could not be reached"}
    return {"ready": True, "root": HOSTING_ROOT, "why": ""}


def _hreq(method, path, body=None, raw=None, timeout=60):
    tok = _device_token()
    if not tok:
        raise PublishError("Sign in to Aethron to publish.")
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(HOSTING_API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or ""
        except Exception:
            msg = ""
        err = PublishError(msg or f"Aethron hosting answered {e.code}")
        err.code = e.code
        raise err from None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise PublishError(f"Aethron hosting could not be reached ({e}).") from None


def host_of(key):
    rec = record(key)
    return (rec.get("host") or "netlify") if rec else None


def _hslug(text):
    s = slug(text)[:42].strip("-")
    return s or "site"


def _publish_aethron(key, files, name, log, title, badge):
    rec = record(key) or {}
    if rec.get("host") != "aethron":
        rec = {}
    site_name = rec.get("name")
    if not site_name:
        base = _hslug(name or key.split(":")[-1])
        tries = [base] + [f"{base[:39]}-{n}" for n in range(2, 6)] + [
            f"{base[:37]}-{os.urandom(2).hex()}" for _ in range(3)]
        for cand in tries:
            try:
                _hreq("POST", "/v1/sites", body={"name": cand})
                site_name = cand
                log(f"claimed {cand}.{HOSTING_ROOT}")
                break
            except PublishError as e:
                if getattr(e, "code", 0) == 409:
                    continue
                raise
        if not site_name:
            raise PublishError(f"Every address like {base}.{HOSTING_ROOT} is taken. Choose another name.")
    blobs = {k: with_badge(p.read_bytes()) for k, p in files.items()
             if badge and k.lower().endswith((".html", ".htm"))}
    log(f"checking {len(files)} files …")
    digests = {k: (hashlib.sha1(blobs[k]).hexdigest() if k in blobs else _sha1(p))
               for k, p in files.items()}
    by_sha = {}
    for k, s in digests.items():
        by_sha.setdefault(s, k)
    try:
        dep = _hreq("POST", f"/v1/sites/{site_name}/deploys", body={"files": digests})
    except PublishError as e:
        if getattr(e, "code", 0) == 404 and rec:      # removed on the server: start again
            _save_record(key, None)
            return _publish_aethron(key, files, name or site_name, log, title, badge)
        raise
    need = [by_sha[s] for s in dep.get("required") or [] if s in by_sha]
    log(f"uploading {len(need)} new or changed file(s)"
        + (f", {len(by_sha) - len(need)} already there" if len(by_sha) > len(need) else ""))
    done = [0]

    def put(path):
        data = blobs[path] if path in blobs else files[path].read_bytes()
        _hreq("PUT", f"/v1/blobs/{digests[path]}", raw=data, timeout=300)
        with LOCK:
            done[0] += 1
            n = done[0]
        if n == len(need) or n % max(1, len(need) // 10) == 0:
            log(f"uploaded {n}/{len(need)} ({round(100 * n / len(need))}%)")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(put, need))
    log("going live …")
    fin = _hreq("POST", f"/v1/sites/{site_name}/deploys/{dep['id']}/finish", body={})
    url = fin.get("url") or f"https://{site_name}.{HOSTING_ROOT}"
    rec = {"host": "aethron", "name": site_name, "url": url, "default_url": url,
           "custom_domain": "", "deploy_id": dep["id"], "published_at": time.time(),
           "files": len(files), "title": title or rec.get("title", ""), "badge": bool(badge)}
    _save_record(key, rec)
    log(f"published: {url}")
    return rec


def publish(key, files, name="", log=print, title="", badge=True, host=None):
    """Upload `files` ({"/path": Path}) as a new version of the site kept
    under `key`, creating the site the first time. Returns the record.

    host: "aethron" (<name>.aethron.site) or "netlify" (the person's own
    account). Unset, a site stays where it is, and a new one goes to
    Aethron hosting when it is reachable, else Netlify."""
    if not files or "/index.html" not in files:
        raise PublishError("There's no index.html to publish.")
    host = host or host_of(key) or ("aethron" if hosting()["ready"] else "netlify")
    if host == "aethron":
        big = [k for k, p in files.items() if p.stat().st_size > 25 * 1024 * 1024]
        if big:
            raise PublishError(f"{big[0]} is over 25 MB, which Aethron hosting doesn't accept.")
        if not badge and not badge_choice()[0]:
            log(badge_choice()[1] + " The badge stays on.")
            badge = True
        return _publish_aethron(key, files, name, log, title, badge)
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

    if not badge and not badge_choice()[0]:
        log(badge_choice()[1] + " The badge stays on.")
        badge = True
    # pages carry the badge; their bytes are what is hashed and uploaded
    blobs = {k: with_badge(p.read_bytes()) for k, p in files.items()
             if badge and k.lower().endswith((".html", ".htm"))}
    log(f"checking {len(files)} files …")
    digests = {k: (hashlib.sha1(blobs[k]).hexdigest() if k in blobs else _sha1(p))
               for k, p in files.items()}
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
             raw=blobs[path] if path in blobs else files[path].read_bytes(),
             timeout=300)
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
           "files": len(files), "title": title or rec.get("title", ""),
           "badge": bool(badge)}
    _save_record(key, rec)
    log(f"published: {url}")
    return rec


def versions(key, limit=12):
    rec = record(key)
    if not rec:
        return []
    if rec.get("host") == "aethron":
        try:
            ds = _hreq("GET", f"/v1/sites/{rec['name']}/deploys").get("deploys") or []
        except PublishError as e:
            if getattr(e, "code", 0) == 404:
                return []
            raise
        return [{"id": d["id"], "at": d.get("created"), "live": bool(d.get("live"))} for d in ds[:limit]]
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
    if rec.get("host") == "aethron":
        if not re.fullmatch(r"[a-z0-9]{6,30}", deploy_id or ""):
            raise PublishError("unknown version")
        _hreq("POST", f"/v1/sites/{rec['name']}/rollback", body={"id": deploy_id})
        rec["deploy_id"] = deploy_id
        _save_record(key, rec)
        return rec
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
    if rec.get("host") == "aethron":
        new = _hslug(name)
        if new == rec.get("name"):
            return rec
        try:
            r = _hreq("POST", f"/v1/sites/{rec['name']}/rename", body={"name": new})
        except PublishError as e:
            if getattr(e, "code", 0) == 409:
                raise PublishError(f"{new}.{HOSTING_ROOT} is taken. Try another name.") from None
            raise
        rec.update(name=r["name"], url=r["url"], default_url=r["url"])
        _save_record(key, rec)
        return rec
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
    if rec.get("host") == "aethron":
        try:
            _hreq("DELETE", f"/v1/sites/{rec['name']}")
        except PublishError as e:
            if getattr(e, "code", 0) != 404:
                raise
        _save_record(key, None)
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
    if rec.get("host") == "aethron":
        raise PublishError("Your own domain on Aethron hosting is coming soon. "
                           f"Your site is live at {rec['url']} meanwhile.")
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
            idx = st["blobs"][dep["files"]["/index.html"]]
            check("every page carries the Built with Aethron badge, once",
                  idx.count(b"<script data-aethron-badge>") == 1)
            check("the badge links to Aethron's site",
                  SITE_URL.encode() in idx)
            check("the badge goes into the page, never into the source file",
                  b"aethron-badge" not in (site / "index.html").read_bytes())

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

            nob = publish("project:my-site", folder_files(site), badge=False,
                          log=lambda m: None)
            check("during the beta the badge can be switched off",
                  b"aethron-badge" not in st["blobs"][st["deploys"][nob["deploy_id"]]["files"]["/index.html"]]
                  and nob["badge"] is False)
            import aethron_cloud as _C, aethron_access as _A
            _was = _C.ENFORCE_BILLING
            try:
                _C.ENFORCE_BILLING = True
                _A.record_plan("free", "u")
                forced = publish("project:my-site", folder_files(site), badge=False,
                                 log=lambda m: None)
                check("on the free plan, once billing is on, the badge stays",
                      forced["badge"] is True and b"aethron-badge" in
                      st["blobs"][st["deploys"][forced["deploy_id"]]["files"]["/index.html"]])
                _A.record_plan("pro", "u")
                check("on Pro it can be switched off", badge_choice()[0])
            finally:
                _C.ENFORCE_BILLING = _was
            rollback("project:my-site", first)
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

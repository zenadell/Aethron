#!/usr/bin/env python3
"""Aethron's own browser — one real Chrome the agent drives step by step.

Every other browser use in Aethron is an instrument: load a page, take a
reading, close it. That cannot answer the question a person actually asks
("does the Contact button go to my contact page?"), because answering it
means opening the cart, pressing the button and looking where it landed —
a VISIT, not a reading. Aethron told its owner a button was fixed that it
had never pressed; it still went to a Stripe checkout.

So this is a browser that stays open for the whole conversation and is
driven like a person drives one: open, click (by the words on the thing),
type, scroll, go back, read what is on screen and where every link leads,
take a picture. Clicks are REAL mouse events at the element's centre, so
whatever the page does on a real click happens here too, and a click that
lands on something covering the element says so instead of pretending.

Stdlib only: Chrome is driven over the DevTools protocol with a small
WebSocket client written here. Chrome comes from forge._find_browser, so
AETHRON_BROWSER means the same thing as everywhere else. The profile lives
in AETHRON_HOME/browser so cookies and logins survive between visits —
it is the agent's own browser, not a fresh stranger every time. Headless
runs report a normal Chrome user agent; nothing is done to defeat bot
checks, and a page that blocks the visit is reported as blocked.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AETHRON_HOME") or ROOT)


class BrowserError(Exception):
    pass


# ── a minimal WebSocket client (RFC 6455, client side) ─────────────────────
class _WS:
    def __init__(self, url: str, timeout=15):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80),
                                             timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.sock.recv(4096)
            if not c:
                raise BrowserError("browser closed the connection")
            head += c
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise BrowserError("browser refused the connection")
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self.sock.settimeout(None)
        self.lock = threading.Lock()

    def send(self, text: str):
        data = text.encode()
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) \
            if n < 4096 else _xor(data, mask)
        with self.lock:
            self.sock.sendall(bytes(hdr) + body)

    def _read(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(1 << 16)
            if not c:
                raise BrowserError("browser connection closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str | None:
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b1 & 0x80:
                mask = self._read(4)
                data = _xor(self._read(n), mask)
            else:
                data = self._read(n)
            if op == 0x8:
                return None
            if op == 0x9:                       # ping -> pong
                with self.lock:
                    self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if op in (0x1, 0x2, 0x0):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _xor(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big") if n else 0
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big") if n else b""


# ── what the page is asked, as JavaScript ──────────────────────────────────
_LIB = r"""
(function(){
if (window.__ae) return;
function vis(e){ if(!e||!e.getClientRects||!e.getClientRects().length) return false;
  var s=getComputedStyle(e); if(s.visibility==='hidden'||s.display==='none') return false;
  var r=e.getBoundingClientRect(); if(r.width<2||r.height<2) return false;
  var o=1,x=e; while(x&&x.nodeType===1){o*=+getComputedStyle(x).opacity;x=x.parentElement}
  return o>0.05 }
function txt(e){ return ((e.innerText||e.value||e.getAttribute('aria-label')||e.getAttribute('title')||e.alt||'')+'').replace(/\s+/g,' ').trim() }
function norm(s){ return (s||'').replace(/\s+/g,' ').trim().toLowerCase() }
var CLICK='a[href],button,[role=button],[role=link],[role=menuitem],[role=tab],input[type=submit],input[type=button],summary,label,[onclick],[tabindex]';
function clickable(e){ while(e&&e.nodeType===1){ if(e.matches(CLICK)) return e; e=e.parentElement } return null }
function sel(e){ if(!e||e.nodeType!==1) return '';
  if(e.id&&document.querySelectorAll('#'+CSS.escape(e.id)).length===1) return '#'+CSS.escape(e.id);
  var n=e.getAttribute('data-framer-name'); if(n){ var q='[data-framer-name="'+n.replace(/"/g,'\\"')+'"]'; if(document.querySelectorAll(q).length===1) return q }
  var path=[],x=e; while(x&&x.nodeType===1&&x!==document.body){ var p=x.parentElement; if(!p) break;
    var i=Array.prototype.indexOf.call(p.children,x)+1; path.unshift(x.tagName.toLowerCase()+':nth-child('+i+')');
    if(x.id&&document.querySelectorAll('#'+CSS.escape(x.id)).length===1){ path[0]='#'+CSS.escape(x.id); break } x=p }
  return (x===document.body?'body > ':'')+path.join(' > ') }
function find(q, selector, nth, any){
  if(selector){ var a=[].slice.call(document.querySelectorAll(selector)).filter(vis); return a[nth||0]||null }
  var want=norm(q); if(!want) return null;
  var all=[].slice.call(document.querySelectorAll('body *')).filter(function(e){
    var t=norm(txt(e)); return t&&t.indexOf(want)>=0 && vis(e) });
  // the smallest element carrying the words, then what you would press
  all=all.filter(function(e){ return ![].some.call(e.children,function(c){ return all.indexOf(c)>=0 }) });
  all.sort(function(a,b){ var ea=norm(txt(a))===want?0:1, eb=norm(txt(b))===want?0:1; return ea-eb });
  var hits=[]; all.forEach(function(e){ var c=any?e:(clickable(e)||e); if(hits.indexOf(c)<0) hits.push(c) });
  return hits[nth||0]||null }
function desc(e){ if(!e) return null; var r=e.getBoundingClientRect(), a=e.closest&&e.closest('a[href]');
  return {tag:e.tagName.toLowerCase(), text:txt(e).slice(0,120), selector:sel(e),
    href:a?a.href:null, target:a?(a.getAttribute('target')||''):'',
    box:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
    framer_name:(e.closest&&e.closest('[data-framer-name]')||{getAttribute:function(){return null}}).getAttribute('data-framer-name')} }
function state(limit){ limit=limit||4000;
  var links=[],seen={}; [].forEach.call(document.querySelectorAll('a[href]'),function(a){
    if(!vis(a)) return; var k=txt(a)+'|'+a.href; if(seen[k]) return; seen[k]=1;
    links.push({text:txt(a).slice(0,80), href:a.href, target:a.getAttribute('target')||''}) });
  var buttons=[]; [].forEach.call(document.querySelectorAll('button,[role=button],input[type=submit],input[type=button]'),function(b){
    if(vis(b)){ var t=txt(b); if(t&&buttons.indexOf(t)<0) buttons.push(t.slice(0,80)) } });
  var inputs=[]; [].forEach.call(document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,select'),function(i){
    if(!vis(i)) return; var l=i.labels&&i.labels[0]?txt(i.labels[0]):'';
    inputs.push({name:i.name||'', label:l||i.placeholder||i.getAttribute('aria-label')||'', type:i.type||i.tagName.toLowerCase(), value:(i.value||'').slice(0,80)}) });
  return {url:location.href, title:document.title,
    text:(document.body?document.body.innerText:'').replace(/\n{3,}/g,'\n\n').slice(0,limit),
    links:links.slice(0,120), buttons:buttons.slice(0,60), inputs:inputs.slice(0,40),
    scroll:[Math.round(scrollY), Math.round(document.documentElement.scrollHeight), innerHeight]} }
window.__ae={vis:vis,find:find,desc:desc,state:state,sel:sel,txt:txt};
})();
"""


def _js(s) -> str:
    return json.dumps(s)


class Browser:
    """One Chrome, one current tab, kept open until close()."""

    def __init__(self, headful: bool | None = None, width=1366, height=900):
        self.headful = (os.environ.get("AETHRON_BROWSER_HEADFUL") == "1"
                        if headful is None else headful)
        self.size = (width, height)
        self.proc = None
        self.ws = None
        self.tmp = None
        self.mid = 0
        self.waiting: dict[int, queue.Queue] = {}
        self.events: queue.Queue = queue.Queue()
        self.session = None
        self.target = None
        self.tabs: dict[str, dict] = {}     # targetId -> info
        self.dialogs: list[str] = []
        self.failed: list[str] = []
        self.console: list[str] = []

    # ── life ───────────────────────────────────────────────────────────
    def start(self):
        if self.ws:
            return self
        sys.path.insert(0, str(ROOT))
        import forge
        exe = forge._find_browser()
        if not exe:
            raise BrowserError("no Chrome/Chromium found (set AETHRON_BROWSER)")
        prof = HOME / "browser" / "profile"
        try:
            prof.mkdir(parents=True, exist_ok=True)
            if _profile_busy(prof):
                raise OSError("profile in use")
        except OSError:
            self.tmp = tempfile.mkdtemp(prefix="ae-browser-")
            prof = Path(self.tmp)
        port_file = prof / "DevToolsActivePort"
        try:
            port_file.unlink()
        except OSError:
            pass
        args = [exe, f"--user-data-dir={prof}", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter",
                f"--window-size={self.size[0]},{self.size[1]}",
                "about:blank"]
        if not self.headful:
            args.insert(1, "--headless=new")
            args.insert(2, "--hide-scrollbars")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < 30:
            if port_file.exists():
                lines = port_file.read_text().split("\n")
                if len(lines) >= 2 and lines[0].strip():
                    break
            if self.proc.poll() is not None:
                raise BrowserError("Chrome exited at start")
            time.sleep(0.1)
        else:
            self.close()
            raise BrowserError("Chrome did not start within 30s")
        port = int(lines[0].strip())
        self.ws = _WS(f"ws://127.0.0.1:{port}{lines[1].strip()}")
        threading.Thread(target=self._pump, daemon=True).start()
        self.send("Target.setDiscoverTargets", {"discover": True})
        ua = self.send("Browser.getVersion").get("userAgent", "")
        self.ua = ua.replace("HeadlessChrome", "Chrome")
        pages = [t for t in self.send("Target.getTargets")["targetInfos"]
                 if t["type"] == "page"]
        tid = pages[0]["targetId"] if pages else \
            self.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        self._attach(tid)
        return self

    def close(self):
        try:
            if self.ws:
                try:
                    self.send("Browser.close", timeout=3)
                except Exception:
                    pass
                self.ws.close()
        finally:
            self.ws = None
            if self.proc and self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, 15)
                except OSError:
                    self.proc.terminate()
                try:
                    self.proc.wait(5)
                except Exception:
                    pass
            self.proc = None
            if self.tmp:
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = None

    def alive(self) -> bool:
        return bool(self.ws and not getattr(self, "dead", False)
                    and self.proc and self.proc.poll() is None)

    # ── protocol ───────────────────────────────────────────────────────
    def _pump(self):
        try:
            while True:
                raw = self.ws.recv() if self.ws else None
                if raw is None:
                    break
                m = json.loads(raw)
                if "id" in m and m["id"] in self.waiting:
                    self.waiting[m["id"]].put(m)
                else:
                    self._event(m)
        except Exception:
            pass
        # the connection is gone for good: say so, so the next tool call
        # starts a new browser instead of writing into a dead socket
        # (every browser_open answered "Broken pipe" until the app quit)
        self.dead = True
        for q in list(self.waiting.values()):
            q.put({"error": {"message": "browser connection closed"}})

    def _event(self, m):
        meth, p = m.get("method", ""), m.get("params", {})
        if meth in ("Target.targetCreated", "Target.targetInfoChanged"):
            ti = p["targetInfo"]
            if ti.get("type") == "page":
                self.tabs[ti["targetId"]] = ti
        elif meth == "Target.targetDestroyed":
            self.tabs.pop(p.get("targetId"), None)
        elif meth == "Page.javascriptDialogOpening":
            self.dialogs.append(p.get("message", ""))
            try:
                self.send("Page.handleJavaScriptDialog", {"accept": True},
                          wait=False)
            except Exception:
                pass
        elif meth == "Network.loadingFailed" and not p.get("canceled"):
            self.failed.append(p.get("errorText", ""))
        elif meth == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.console.append((d.get("exception") or {}).get(
                "description", d.get("text", ""))[:200])
        self.events.put(m)

    def send(self, method, params=None, timeout=30, wait=True,
             session: bool = False):
        if not self.ws or getattr(self, "dead", False):
            raise BrowserError("browser is not running")
        self.mid += 1
        mid = self.mid
        msg = {"id": mid, "method": method, "params": params or {}}
        if session and self.session:
            msg["sessionId"] = self.session
        q = queue.Queue()
        if wait:
            self.waiting[mid] = q
        try:
            self.ws.send(json.dumps(msg))
        except OSError:                   # Chrome went away under us
            self.dead = True
            self.waiting.pop(mid, None)
            raise BrowserError("browser connection closed")
        if not wait:
            return {}
        try:
            r = q.get(timeout=timeout)
        except queue.Empty:
            raise BrowserError(f"browser did not answer {method} in {timeout}s")
        finally:
            self.waiting.pop(mid, None)
        if "error" in r:
            raise BrowserError(f"{method}: {r['error'].get('message')}")
        return r.get("result", {})

    def page(self, method, params=None, timeout=30):
        return self.send(method, params, timeout=timeout, session=True)

    def _attach(self, tid):
        s = self.send("Target.attachToTarget",
                      {"targetId": tid, "flatten": True})["sessionId"]
        self.session, self.target = s, tid
        for meth in ("Page.enable", "Runtime.enable", "Network.enable"):
            self.page(meth)
        self.page("Network.setUserAgentOverride", {"userAgent": self.ua})
        self.page("Emulation.setDeviceMetricsOverride",
                  {"width": self.size[0], "height": self.size[1],
                   "deviceScaleFactor": 1, "mobile": False})
        self.page("Page.addScriptToEvaluateOnNewDocument", {"source": _LIB})
        self.send("Target.activateTarget", {"targetId": tid})

    def eval(self, expr, timeout=20):
        r = self.page("Runtime.evaluate",
                      {"expression": _LIB + ";\n" + expr,
                       "returnByValue": True, "awaitPromise": True},
                      timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise BrowserError("page script failed: " + str(
                (d.get("exception") or {}).get("description") or d.get("text")))
        return r.get("result", {}).get("value")

    def url(self) -> str:
        try:
            return self.eval("location.href", timeout=5) or ""
        except BrowserError:
            return (self.tabs.get(self.target) or {}).get("url", "")

    def _settle(self, quiet=0.6, limit=8.0):
        """Wait for the page to stop arriving: load done, then quiet."""
        t0 = time.time()
        last = None
        while time.time() - t0 < limit:
            try:
                st = self.eval("[document.readyState, document.body ? "
                               "document.body.innerHTML.length : 0, location.href]",
                               timeout=5)
            except BrowserError:
                time.sleep(0.2)
                continue
            if st and st[0] == "complete" and st == last:
                time.sleep(quiet)
                try:
                    if self.eval("[document.readyState, document.body ? "
                                 "document.body.innerHTML.length : 0, "
                                 "location.href]", timeout=5) == st:
                        return
                except BrowserError:
                    pass
            last = st
            time.sleep(0.25)

    # ── what the agent does ────────────────────────────────────────────
    def open(self, url: str, settle=True) -> dict:
        self.start()
        if not re.match(r"^(https?|file|about|data):", url):
            url = "https://" + url
        self.failed.clear()
        self.console.clear()
        r = self.page("Page.navigate", {"url": url}, timeout=45)
        if r.get("errorText"):
            raise BrowserError(f"could not open {url}: {r['errorText']}")
        if settle:
            self._settle()
        return self.read()

    def read(self, limit=4000) -> dict:
        st = self.eval(f"__ae.state({int(limit)})") or {}
        st["blocked"] = _blocked(st)
        return st

    def find(self, text=None, selector=None, nth=0, any=True) -> dict | None:
        return self.eval(f"__ae.desc(__ae.find({_js(text)},{_js(selector)},"
                         f"{int(nth)},{'true' if any else 'false'}))")

    def click(self, text=None, selector=None, nth=0, wait=4.0) -> dict:
        """Press the thing that carries these words, like a person would."""
        self.start()
        js = (f"(function(){{var e=__ae.find({_js(text)},{_js(selector)},"
              f"{int(nth)},false); if(!e) return null;"
              "e.scrollIntoView({block:'center',inline:'center'});"
              "var r=e.getBoundingClientRect(),x=r.left+r.width/2,"
              "y=r.top+r.height/2,top=document.elementFromPoint(x,y);"
              "var d=__ae.desc(e); d.x=x; d.y=y;"
              "d.covered=(top&&top!==e&&!e.contains(top)&&!top.contains(e))?"
              "__ae.desc(top):null; return d})()")
        el = self.eval(js)
        if not el:
            what = f"selector {selector!r}" if selector else repr(text)
            raise BrowserError(f"nothing visible matches {what} on "
                               f"{self.url()}")
        before_url = self.url()
        before_tabs = set(self.tabs)
        self.page("Input.dispatchMouseEvent", {"type": "mouseMoved",
                  "x": el["x"], "y": el["y"]})
        for kind in ("mousePressed", "mouseReleased"):
            self.page("Input.dispatchMouseEvent",
                      {"type": kind, "x": el["x"], "y": el["y"],
                       "button": "left", "clickCount": 1})
        new_tab, t0 = None, time.time()
        while time.time() - t0 < wait:
            time.sleep(0.25)
            fresh = [t for t in self.tabs if t not in before_tabs]
            if fresh:
                new_tab = fresh[0]
                time.sleep(0.8)          # let the new tab learn its address
                break
            if self.url() != before_url:
                break
        out = {"clicked": {k: el.get(k) for k in
                           ("tag", "text", "selector", "href", "framer_name")},
               "covered_by": el.get("covered"), "from": before_url}
        if new_tab:
            info = self.tabs.get(new_tab) or {}
            u = info.get("url", "")
            t1 = time.time()
            while (not u or u == "about:blank") and time.time() - t1 < 5:
                time.sleep(0.3)
                u = (self.tabs.get(new_tab) or {}).get("url", "")
            out["new_tab"] = u
            out["landed"] = u
            self._attach(new_tab)
            self._settle(limit=6)
        else:
            self._settle(limit=6)
            out["landed"] = self.url()
        out["navigated"] = out["landed"] != before_url
        if self.dialogs:
            out["dialogs"] = self.dialogs[-3:]
            self.dialogs.clear()
        return out

    def type(self, text: str, into=None, selector=None, submit=False) -> dict:
        self.start()
        js = (f"(function(){{var e=null,s={_js(selector)},q={_js(into)};"
              "if(s) e=document.querySelector(s);"
              "if(!e&&q){var w=q.toLowerCase();"
              "e=[].slice.call(document.querySelectorAll('input,textarea,"
              "[contenteditable=true]')).filter(__ae.vis).find(function(i){"
              "var l=(i.labels&&i.labels[0]?__ae.txt(i.labels[0]):'')+' '+"
              "(i.placeholder||'')+' '+(i.name||'')+' '+"
              "(i.getAttribute('aria-label')||'');"
              "return l.toLowerCase().indexOf(w)>=0})}"
              "if(!e&&!s&&!q) e=document.activeElement;"
              "if(!e) return null; e.scrollIntoView({block:'center'});"
              "e.focus(); if('value' in e){e.select&&e.select()} "
              "return __ae.desc(e)})()")
        el = self.eval(js)
        if not el:
            raise BrowserError(f"no visible field matches {into or selector!r}")
        self.page("Input.insertText", {"text": text})
        if submit:
            for kind in ("keyDown", "keyUp"):
                self.page("Input.dispatchKeyEvent",
                          {"type": kind, "key": "Enter", "code": "Enter",
                           "windowsVirtualKeyCode": 13, "text": "\r"})
            self._settle(limit=6)
        return {"field": el, "typed": text, "url": self.url()}

    def scroll(self, by: int | None = None, to: str | None = None) -> dict:
        self.start()
        if to:
            if to in ("top", "bottom"):
                self.eval("scrollTo(0," + ("0" if to == "top" else
                          "document.documentElement.scrollHeight") + ")")
            else:
                d = self.find(to)
                if not d:
                    raise BrowserError(f"nothing visible says {to!r}")
                self.eval(f"__ae.find({_js(to)},null,0,true)"
                          ".scrollIntoView({block:'center'})")
        else:
            self.page("Input.dispatchMouseEvent",
                      {"type": "mouseWheel", "x": self.size[0] / 2,
                       "y": self.size[1] / 2, "deltaX": 0,
                       "deltaY": by if by is not None else self.size[1] * 0.8})
        time.sleep(0.6)
        return {"scroll": self.eval("[Math.round(scrollY),"
                                    "document.documentElement.scrollHeight,"
                                    "innerHeight]")}

    def back(self) -> dict:
        self.start()
        h = self.page("Page.getNavigationHistory")
        i = h.get("currentIndex", 0)
        if i <= 0:
            raise BrowserError("there is no page to go back to in this tab")
        self.page("Page.navigateToHistoryEntry",
                  {"entryId": h["entries"][i - 1]["id"]})
        self._settle(limit=6)
        return {"url": self.url()}

    def screenshot(self, full=False, fmt="png", quality=70) -> bytes:
        self.start()
        p = {"format": fmt}
        if fmt == "jpeg":
            p["quality"] = int(quality)
        if full:
            p["captureBeyondViewport"] = True
            h = self.eval("document.documentElement.scrollHeight") or 900
            p["clip"] = {"x": 0, "y": 0, "width": self.size[0],
                         "height": min(int(h), 12000), "scale": 1}
        return base64.b64decode(self.page("Page.captureScreenshot", p,
                                          timeout=45)["data"])


def _profile_busy(prof: Path) -> bool:
    """Another Chrome is using this profile right now. A lock left by one
    that died (its pid gone) is not "in use": Chrome clears it itself."""
    lock = prof / "SingletonLock"
    if not lock.is_symlink() and not lock.exists():
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def _blocked(st: dict) -> str:
    """A page that is a bot check, not the site — said out loud."""
    t = ((st.get("title") or "") + " " + (st.get("text") or "")[:600]).lower()
    for sign in ("verify you are human", "checking your browser",
                 "just a moment", "attention required", "captcha",
                 "access denied", "are you a robot", "unusual traffic"):
        if sign in t:
            return sign
    return ""


def same_place(a: str, b: str) -> bool:
    """Does landing on `a` satisfy an expected address `b`?"""
    def k(u):
        p = urllib.parse.urlparse((u or "").strip())
        host = (p.netloc or "").lower().removeprefix("www.")
        return p.scheme.lower(), host, (p.path or "/").rstrip("/") or "/"
    if b.startswith(("mailto:", "tel:")):
        return (a or "").lower().startswith(b.lower())
    ka, kb = k(a), k(b)
    if not kb[1]:                        # "/contact" — path only
        return ka[2] == kb[2]
    return ka[1:] == kb[1:]


# ── a site served from a project folder, for opening previews ─────────────
_SERVED: dict[str, tuple] = {}


def serve_site(site: Path, platform: str) -> str:
    """Serve a built site/ the way `forge serve` does; reuse per folder."""
    key = str(Path(site).resolve())
    if key in _SERVED:
        return _SERVED[key][1]
    sys.path.insert(0, str(ROOT))
    import forge
    from http.server import ThreadingHTTPServer
    h = forge._site_handler(Path(site), platform, quiet=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    _SERVED[key] = (srv, base)
    return base


# ── self-test ──────────────────────────────────────────────────────────────
def _selftest() -> int:
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import functools
    ok = fail = 0

    def check(name, cond, info=""):
        nonlocal ok, fail
        print(("PASS " if cond else "FAIL ") + name +
              ("" if cond else f"  — {info}"))
        ok, fail = ok + bool(cond), fail + (not cond)

    d = Path(tempfile.mkdtemp(prefix="ae-bt-"))
    (d / "index.html").write_text(
        "<title>Shop</title><button id=bag>Open bag</button>"
        "<div id=drawer style='display:none'><a href='#' id=c>Contact us</a>"
        "<span id=tag>FRAMESHIP</span></div>"
        "<p style='margin-top:1500px'>Far below</p>"
        "<form action='/next.html'><input placeholder='Your email' name=e>"
        "</form><a href='/next.html'>Next page</a>"
        "<script>bag.onclick=function(){drawer.style.display='block'};"
        "c.onclick=function(e){e.preventDefault();"
        "window.open('/stripe.html','_blank')}</script>")
    (d / "next.html").write_text("<title>Next</title><h1>Second page</h1>")
    (d / "stripe.html").write_text("<title>Pay</title>Checkout")
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    b = Browser()
    try:
        try:
            st = b.open(base)
        except BrowserError as e:
            print(f"VERDICT: SKIPPED — {e}")
            return 0
        check("opens a page and reads it", st["title"] == "Shop"
              and "Open bag" in st["buttons"], st)
        check("a hidden link is not listed as on screen",
              not any(l["text"] == "Contact us" for l in st["links"]))
        try:
            b.click("Contact us")
            check("refuses to click what is not on screen", False)
        except BrowserError:
            check("refuses to click what is not on screen", True)
        r = b.click("Open bag")
        check("a click runs the page's own code",
              b.find("Contact us") is not None, r)
        r = b.click("Contact us")
        check("follows a link that opens a new tab", r.get("new_tab", "")
              .endswith("/stripe.html"), r)
        check("and says where it landed",
              same_place(r["landed"], base + "stripe.html"), r)
        check("reads the new tab", b.read()["title"] == "Pay")
        b.open(base)
        r = b.scroll(to="Far below")
        check("scrolls to words", r["scroll"][0] > 500, r)
        r = b.type("me@x.com", into="email", submit=True)
        check("types into a field by its label and submits",
              "next.html" in r["url"], r)
        r = b.back()
        check("goes back", r["url"].rstrip("/") == base.rstrip("/"), r)
        r = b.click("Next page")
        check("an ordinary link navigates", r["navigated"] and
              r["landed"].endswith("next.html"), r)
        png = b.screenshot()
        check("takes a picture", png[:8] == b"\x89PNG\r\n\x1a\n")
        check("same_place matches by address", same_place(
            "https://www.jomiez.com/contact/", "https://jomiez.com/contact")
            and not same_place("https://buy.stripe.com/x",
                               "https://jomiez.com/contact"))
        check("a bot check is named", _blocked(
            {"title": "Just a moment...", "text": ""}) != "")
    finally:
        b.close()
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{ok}/{ok + fail} checks passed")
    return 1 if fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)

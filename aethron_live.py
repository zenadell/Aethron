#!/usr/bin/env python3
"""Talk with Ember in real time: one open line to Gemini Live.

Before this, a spoken turn was: listen until 1.8 s of silence, send the
words as text, wait for the WHOLE spoken answer, then play it. Seconds of
dead air every time, and no way to talk over Ember.

Now a conversation is one Gemini Live session:

  microphone ── 16 kHz PCM ──> EchoGate ──> Gemini Live
                                              │
  window  <── events (audio as it is made, what you said, what Ember says,
              what Ember's body does, "you interrupted") ──┘

* The audio of Ember's answer is played the moment the first piece of it
  arrives, and stops the moment you talk over it (the server notices you
  started speaking and says `interrupted`).
* The body is driven by the same `ember_behave` tool the spoken requests
  already use, so what Ember does is validated by `aethron_ember.act_parse`
  exactly as before: an answer with a plan, a button to press, or a clear
  request typed into the chat.
* The key never reaches the window: this process holds the connection.
* ECHO: Ember's voice comes out of the speakers and back into the
  microphone, and Gemini would hear itself and stop talking. While Ember
  speaks, the gate forwards the microphone only when it is clearly louder
  than the echo it expects from what is playing (learned from the room),
  with the 300 ms before it so the first word is not lost. With headphones
  there is no echo and nothing is held back.
* A conversation ends when you say so, press Esc, double-tap Ember, after
  IDLE_S of nobody saying anything, or after MAX_S. Every turn's tokens
  are recorded where all requests are (aethron_usage), priced at the
  Live rates, and paid ones count toward the session limit.

    python3 aethron_live.py --selftest     (a fake Live server, no key)
"""
from __future__ import annotations

import base64
import collections
import json
import math
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
from array import array

HOST = "generativelanguage.googleapis.com"
PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MODELS = ("gemini-3.8-live", "gemini-3.8-live-extended-thinking")
IN_RATE, OUT_RATE = 16000, 24000
IDLE_S = 45.0          # nobody said anything for this long: the conversation is over
MAX_S = 20 * 60.0      # one conversation is never longer than this
KEEP_EVENTS = 3000
# $ per million tokens: audio in, audio out, text in, text out (Google's Live
# rates for gemini-3.8-live as published, November 2026)
PRICE = {"gemini-3.8-live": (3.0, 12.0, 0.75, 4.5),
         "gemini-3.8-live-extended-thinking": (3.0, 12.0, 0.75, 4.5)}

LIVE_RULES = (
    "\n\nTHIS IS A LIVE SPOKEN CONVERSATION. Every time the person finishes speaking: "
    "first call the ember_behave tool exactly once with the complete JSON object the "
    "rules above describe for what they just said (send, action or answer), then say "
    "your own short line OUT LOUD, in character, under 12 words. For send and action, "
    "say what you are doing ('typing it in', 'opening it'). If they say goodbye or "
    "that's all, give a short goodbye as an answer. Never say you did something "
    "without calling the tool. If they talk over you, stop and listen.")

BEHAVE = {
    "name": "ember_behave",
    "description": ("Make Ember actually do what the person asked: press one of Aethron's own "
                    "controls, type a clear request into the chat for Aethron's agent, or answer "
                    "with Ember's body. ALWAYS call this exactly once per turn with your whole "
                    "answer as the JSON object the rules describe. The app validates it and does "
                    "only what is really possible."),
    "parameters": {"type": "OBJECT", "properties": {
        "reply_json": {"type": "STRING", "description": "The complete JSON object answer, exactly as the rules describe."}},
        "required": ["reply_json"]},
}


# ───────────────────────────────────────────────────────── audio helpers

def rms16(pcm: bytes) -> float:
    """Loudness of 16-bit little-endian PCM, 0..1."""
    if len(pcm) < 2:
        return 0.0
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    s = 0
    for v in a:
        s += v * v
    return math.sqrt(s / len(a)) / 32768.0


def to16k(samples, rate: float) -> bytes:
    """Float samples (-1..1) at `rate` -> 16 kHz 16-bit little-endian PCM.
    Averages over each output sample's span when coming down from a higher
    rate (so it does not alias), linear when going up."""
    n = len(samples)
    if n == 0 or rate <= 0:
        return b""
    step = rate / IN_RATE
    out = array("h")
    pos = 0.0
    while True:
        if step >= 1:
            a, b = int(pos), int(pos + step)
            if b > n:
                break
            seg = samples[a:b] if b > a else samples[a:a + 1]
            v = sum(seg) / len(seg)
        else:
            i = int(pos)
            if i + 1 >= n:
                break
            f = pos - i
            v = samples[i] * (1 - f) + samples[i + 1] * f
        out.append(max(-32768, min(32767, int(round(v * 32767)))))
        pos += step
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


class EchoGate:
    """Holds the microphone back while Ember is speaking, unless the person
    is clearly talking over it.

    `played(pcm)` is told every piece of Ember's voice as it is sent to the
    window (played back in real time from the moment it arrives).
    `admit(pcm)` decides for each piece of microphone audio. The room's echo
    is learned as mic loudness / voice loudness while the person is quiet;
    talking over Ember needs to be BARGE times louder than that, or louder
    than the quiet room by FLOOR_X, for at least two pieces in a row."""

    BARGE = 2.6
    FLOOR_X = 5.0
    MIN_LEVEL = 0.02
    TAIL = 0.35          # the room still rings this long after the voice stops

    def __init__(self):
        self.until = 0.0                    # when the voice queued so far finishes
        self.timeline = collections.deque()  # (t0, t1, rms) of Ember's voice
        self.ratio = 0.5                    # echo coupling, learned
        self.floor = 0.004                  # the quiet room, learned
        self.loud = 0
        self.open = False
        self.held = collections.deque(maxlen=3)
        self.lock = threading.Lock()

    def played(self, pcm: bytes, now: float = None):
        now = time.time() if now is None else now
        dur = len(pcm) / 2 / OUT_RATE
        with self.lock:
            t0 = max(now, self.until)
            # 20 ms frames, so the gate knows when Ember is loud and when it breathes
            fr = int(OUT_RATE * 0.02) * 2
            for i in range(0, len(pcm), fr):
                a = t0 + i / 2 / OUT_RATE
                self.timeline.append((a, a + fr / 2 / OUT_RATE, rms16(pcm[i:i + fr])))
            self.until = t0 + dur
            while self.timeline and self.timeline[0][1] < now - 2:
                self.timeline.popleft()

    def interrupted(self, now: float = None):
        now = time.time() if now is None else now
        with self.lock:
            self.until = now
            while self.timeline and self.timeline[-1][0] > now:
                self.timeline.pop()

    def speaking(self, now: float = None) -> bool:
        now = time.time() if now is None else now
        return now < self.until + self.TAIL

    def _voice(self, now):
        # what is coming out of the speakers now (and just before: the room is slow)
        v = 0.0
        for a, b, r in self.timeline:
            if b >= now - 0.15 and a <= now:
                v = max(v, r)
        return v

    def admit(self, pcm: bytes, now: float = None) -> list:
        """-> the pieces to send now (several when the person just started
        talking over Ember: what was held back comes first)."""
        now = time.time() if now is None else now
        mic = rms16(pcm)
        with self.lock:
            if not self.speaking(now):
                if not self.open and mic < self.floor * 3:
                    self.floor = self.floor * 0.95 + mic * 0.05
                self.loud, self.open = 0, False
                self.held.clear()
                return [pcm]
            if self.open:                       # they are talking over Ember: keep listening
                if mic < max(self.floor * 2, self.MIN_LEVEL / 2):
                    self.loud = max(0, self.loud - 1)
                    if self.loud == 0:
                        self.open = False
                return [pcm]
            voice = self._voice(now)
            echo = self.ratio * voice
            need = max(echo * self.BARGE, self.floor * self.FLOOR_X, self.MIN_LEVEL)
            if mic > need:
                self.loud += 1
                if self.loud >= 2:
                    self.open = True
                    out = list(self.held) + [pcm]
                    self.held.clear()
                    return out
            else:
                self.loud = 0
                if voice > 0.01 and mic > 0:
                    # the room's echo, learned slowly and kept sane
                    r = min(2.0, max(0.02, mic / voice))
                    self.ratio = self.ratio * 0.9 + r * 0.1
            self.held.append(pcm)
            return []


# ───────────────────────────────────────────────────────── a WebSocket

class WS:
    """A small WebSocket client: ws:// or wss://, masked frames out,
    fragmented frames in, ping/pong, and a recv that can time out without
    losing half a frame."""

    def __init__(self, url: str, timeout: float = 10.0):
        u = urllib.parse.urlsplit(url)
        tls = u.scheme == "wss"
        port = u.port or (443 if tls else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if tls else raw
        self.s.settimeout(timeout)
        self.wlock = threading.Lock()
        self.buf = b""
        self.part = b""
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\nUser-Agent: Aethron\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("the voice service closed the connection")
            head += c
        head, self.buf = head.split(b"\r\n\r\n", 1)
        first = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in first + " ":
            raise ConnectionError("the voice service refused: " + first)

    def send(self, obj, op: int = 1):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        n = len(data)
        hdr = bytes([0x80 | op])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        if n:
            m = (mask * (n // 4 + 1))[:n]
            body = (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        else:
            body = b""
        with self.wlock:
            self.s.sendall(hdr + mask + body)

    def _frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        n, i = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        mask = None
        if b[1] & 0x80:
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        data, self.buf = b[i:i + n], b[i + n:]
        if mask and n:
            m = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        return b[0], data

    def recv(self, timeout: float = 1.0):
        """One whole message (parsed JSON), or None when nothing came in time."""
        end = time.time() + timeout
        while True:
            f = self._frame()
            if f is None:
                left = end - time.time()
                if left <= 0:
                    return None
                self.s.settimeout(left)
                try:
                    c = self.s.recv(65536)
                except (socket.timeout, TimeoutError):
                    return None
                if not c:
                    raise EOFError("the voice service closed the connection")
                self.buf += c
                continue
            b1, data = f
            op = b1 & 0x0F
            if op == 8:
                why = data[2:].decode(errors="replace") if len(data) > 2 else ""
                raise EOFError("closed" + (": " + why if why else ""))
            if op == 9:
                self.send(data, op=10)
                continue
            if op == 10:
                continue
            self.part += data
            if b1 & 0x80:
                msg, self.part = self.part, b""
                try:
                    return json.loads(msg.decode() or "{}")
                except ValueError:
                    return {}

    def close(self):
        try:
            self.send(b"", op=8)
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


# ───────────────────────────────────────────────────────── the session

def _url(key: str) -> str:
    base = os.environ.get("AETHRON_LIVE_URL") or f"wss://{HOST}{PATH}"
    return base + ("&" if "?" in base else "?") + "key=" + urllib.parse.quote(key)


def keys() -> list:
    """(key, free) for every Gemini key this machine has, free ones first."""
    out, free = [], set()
    try:
        import aethron_brain as B
        for p in (B._stored().get("profiles") or {}).values():
            if p.get("provider") == "gemini":
                free.update(str(k).strip() for k in (p.get("api_keys") or []) if str(k).strip())
    except Exception:
        pass
    try:
        import aethron_ember
        ks = aethron_ember.gemini_keys()
    except Exception:
        ks = [k.strip() for k in (os.environ.get("GEMINI_API_KEY") or "").split(",") if k.strip()]
    for k in ks:
        out.append((k, k in free))
    out.sort(key=lambda kv: not kv[1])
    return out


def available() -> dict:
    ks = keys()
    if not ks:
        return {"ok": False, "why": "talking live needs a Gemini key: Settings, Model, add Gemini"}
    return {"ok": True, "keys": len(ks), "free": sum(1 for _, f in ks if f)}


def _cost(model, usage) -> tuple:
    """(in, out, usd) for one turn's usageMetadata."""
    pa, po, ta, to = PRICE.get(model, PRICE["gemini-3.8-live"])
    def split(details):
        audio = text = 0
        for d in details or []:
            n = int(d.get("tokenCount") or 0)
            if str(d.get("modality") or "").upper() == "AUDIO":
                audio += n
            else:
                text += n
        return audio, text
    ia, it = split(usage.get("promptTokensDetails"))
    oa, ot = split(usage.get("responseTokensDetails"))
    thoughts = int(usage.get("thoughtsTokenCount") or 0)
    inp = int(usage.get("promptTokenCount") or 0)
    out = int(usage.get("responseTokenCount") or 0) + thoughts
    if not (ia or it):
        ia = inp                     # no breakdown: priced as audio, the dearer rate
    if not (oa or ot):
        oa = out - thoughts          # no breakdown: what Ember says is audio
    ot += thoughts                   # thinking is billed as text output
    usd = (ia * pa + it * ta + oa * po + ot * to) / 1e6
    return inp, out, usd


class Session:
    """One conversation. Thread-safe: audio arrives from the microphone's
    thread, events are read by the window's requests, one reader thread
    talks to the service."""

    def __init__(self, system: str, model: str = "gemini-3.8-live", voice: str = "Aoede",
                 projects=None, on_end=None, key_list=None):
        self.system = system
        self.model = model if model in MODELS else "gemini-3.8-live"
        self.voice = voice or "Aoede"
        self.projects = list(projects or [])
        self.key_list = list(key_list if key_list is not None else keys())
        self.gate = EchoGate()
        self.cond = threading.Condition()
        self.events = collections.deque(maxlen=KEEP_EVENTS)
        self.seq = 0
        self.ws = None
        self.key = ""
        self.free = False
        self.handle = ""
        self.closed = False
        self.why = ""
        self.started = time.time()
        self.id = base64.b32encode(os.urandom(5)).decode().lower()   # which conversation an answer belongs to
        self.last = time.time()          # someone said something
        self.heard = ""                  # what the person said this turn
        self.said = ""                   # what Ember said this turn
        self.usage = None                # this turn's usage, the last one seen
        self.pending_state = ""
        self.turns = 0
        self.spent = 0.0
        self.sent_audio = 0
        self.on_end = on_end            # called once when the conversation ends

    # ---------------------------------------------------- events
    def _emit(self, kind, **data):
        with self.cond:
            self.seq += 1
            ev = dict(data, t=kind, n=self.seq)
            self.events.append(ev)
            self.cond.notify_all()
        return ev

    def read(self, since: int = 0, wait: float = 20.0) -> dict:
        end = time.time() + wait
        with self.cond:
            while True:
                got = [e for e in self.events if e["n"] > since]
                if got or self.closed or time.time() >= end:
                    return {"events": got, "seq": self.seq, "closed": self.closed, "why": self.why,
                            "speaking": self.gate.speaking(), "id": self.id}
                self.cond.wait(max(0.05, end - time.time()))

    # ---------------------------------------------------- connecting
    def _setup(self):
        s = {"model": "models/" + self.model,
             "generationConfig": {"responseModalities": ["AUDIO"],
                                  "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
             "systemInstruction": {"parts": [{"text": self.system + LIVE_RULES}]},
             "tools": [{"functionDeclarations": [BEHAVE]}],
             "inputAudioTranscription": {}, "outputAudioTranscription": {},
             "realtimeInputConfig": {"automaticActivityDetection": {"prefixPaddingMs": 120, "silenceDurationMs": 650},
                                     "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"},
             "contextWindowCompression": {"slidingWindow": {}},
             "sessionResumption": {"handle": self.handle} if self.handle else {}}
        if self.model.endswith("extended-thinking"):
            s["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "LOW"}
        return {"setup": s}

    def _connect(self, key):
        ws = WS(_url(key), timeout=10)
        ws.send(self._setup())
        end = time.time() + 12
        while time.time() < end:
            m = ws.recv(max(0.2, end - time.time()))
            if m is None:
                continue
            if "setupComplete" in m:
                return ws
            err = m.get("error")
            if err:
                ws.close()
                raise ConnectionError(str(err.get("message") if isinstance(err, dict) else err)[:200])
        ws.close()
        raise TimeoutError("the voice service did not set up in time")

    def start(self) -> dict:
        if not self.key_list:
            return {"ok": False, "why": "talking live needs a Gemini key: Settings, Model, add Gemini"}
        try:
            import aethron_brain as B
            limit = B.session_check()
        except Exception:
            limit = ""
        last = None
        for key, free in self.key_list[:4]:
            if limit and not free:
                last = PermissionError(limit)
                continue
            try:
                self.ws, self.key, self.free = self._connect(key), key, free
                break
            except Exception as e:
                last = e
        if not self.ws:
            return {"ok": False, "why": "the voice service did not answer: " + str(last or "no key worked")[:200]}
        threading.Thread(target=self._reader, daemon=True).start()
        self._emit("ready", model=self.model, voice=self.voice)
        return {"ok": True, "model": self.model, "id": self.id}

    # ---------------------------------------------------- what comes in
    def _reader(self):
        while not self.closed:
            if time.time() - self.last > IDLE_S and not self.gate.speaking():
                return self.stop("quiet")
            if time.time() - self.started > MAX_S:
                return self.stop("long")
            try:
                m = self.ws.recv(0.5)
            except Exception as e:
                if self.closed:
                    return
                if self.handle and self._resume():
                    continue
                return self.stop("lost", str(e)[:200])
            if m is None:
                continue
            try:
                self._handle(m)
            except Exception as e:      # one odd message never ends a conversation
                self._emit("log", text=f"could not read a message ({type(e).__name__})")

    def _resume(self) -> bool:
        old = self.ws
        try:
            self.ws = self._connect(self.key)
            self._emit("log", text="reconnected, the conversation goes on")
            return True
        except Exception:
            return False
        finally:
            try:
                old.close()
            except Exception:
                pass

    def _handle(self, m):
        if "sessionResumptionUpdate" in m:
            u = m["sessionResumptionUpdate"] or {}
            if u.get("resumable") and u.get("newHandle"):
                self.handle = u["newHandle"]
        if "goAway" in m:
            if self.handle:
                self._resume()
            return
        if "usageMetadata" in m:
            self.usage = m["usageMetadata"]
        tc = m.get("toolCall")
        if tc:
            self._tools(tc.get("functionCalls") or [])
        if m.get("toolCallCancellation"):
            self._emit("cancel", ids=(m["toolCallCancellation"] or {}).get("ids") or [])
        sc = m.get("serverContent")
        if not sc:
            return
        tr = (sc.get("inputTranscription") or {}).get("text")
        if tr:
            self.heard += tr
            self.last = time.time()
            self._emit("heard", text=self.heard.strip())
        for p in (sc.get("modelTurn") or {}).get("parts") or []:
            d = p.get("inlineData") or {}
            if d.get("data"):
                try:
                    pcm = base64.b64decode(d["data"])
                except Exception:
                    continue
                rate = OUT_RATE
                mt = str(d.get("mimeType") or "")
                mr = re.search(r"rate=(\d+)", mt)
                if mr:
                    rate = int(mr.group(1))
                self.gate.played(pcm if rate == OUT_RATE else pcm[: int(len(pcm) * OUT_RATE / rate) // 2 * 2])
                self.last = time.time()
                self._emit("audio", pcm=d["data"], rate=rate)
        ot = (sc.get("outputTranscription") or {}).get("text")
        if ot:
            self.said += ot
            self.last = time.time()
            self._emit("said", text=self.said.strip())
        if sc.get("interrupted"):
            self.gate.interrupted()
            self._emit("interrupted")
        if sc.get("turnComplete"):
            self._turn_done()

    def _tools(self, calls):
        out = []
        for fc in calls:
            name, args = fc.get("name"), fc.get("args") or {}
            result = "ok"
            if name == "ember_behave":
                raw = args.get("reply_json") if isinstance(args.get("reply_json"), str) else json.dumps(args)
                try:
                    import aethron_ember as AE
                    r = AE.act_parse(raw, self.heard.strip() or "(spoken)", self.projects)
                    if r.get("new"):
                        try:
                            AE.learn(r["new"])
                        except Exception:
                            pass
                    r["by"] = "live"
                    self._emit("act", r=r, id=fc.get("id"))
                    result = ("ok: Ember is doing it on screen now. Say your short line."
                              if r.get("kind") in ("send", "action") else "ok: Ember is doing it. Say your line.")
                except Exception as e:
                    result = f"not done: {str(e)[:120]}. Answer in words."
            else:
                result = "unknown tool"
            out.append({"id": fc.get("id"), "name": name, "response": {"result": result}})
        if out:
            self._send({"toolResponse": {"functionResponses": out}})

    def _turn_done(self):
        heard, said = self.heard.strip(), self.said.strip()
        self.heard = self.said = ""
        self.turns += 1
        if self.usage:
            inp, out, usd = _cost(self.model, self.usage)
            self.usage = None
            try:
                import aethron_usage
                aethron_usage.record(f"https://{HOST}{PATH}", self.model, "ember-voice", inp, out,
                                     usd=usd, free=self.free)
            except Exception:
                pass
            if not self.free and usd:
                self.spent += usd
                try:
                    import aethron_brain as B
                    B.session_add(usd=usd, requests=1)
                except Exception:
                    pass
        self._emit("turn", heard=heard, said=said)
        self._turn_flush()

    # ---------------------------------------------------- what goes out
    def _send(self, obj):
        if self.closed or not self.ws:
            return False
        try:
            self.ws.send(obj)
            return True
        except Exception:
            return False

    def audio(self, pcm: bytes) -> int:
        """Microphone audio, 16 kHz 16-bit PCM. -> pieces actually sent."""
        if self.closed or not pcm:
            return 0
        n = 0
        for piece in self.gate.admit(pcm):
            if self._send({"realtimeInput": {"audio": {"data": base64.b64encode(piece).decode(),
                                                       "mimeType": f"audio/pcm;rate={IN_RATE}"}}}):
                n += 1
                self.sent_audio += 1
        return n

    def text(self, t: str):
        t = str(t or "").strip()[:2000]
        if t:
            self.heard = t
            self.last = time.time()
            self._send({"realtimeInput": {"text": t}})

    def state(self, st: str):
        """What Aethron shows changed: told to the model when it is not mid-answer."""
        self.pending_state = str(st or "")[:1500]
        if not self.gate.speaking() and not self.heard:
            self._turn_flush()

    def _turn_flush(self):
        if self.pending_state:
            st, self.pending_state = self.pending_state, ""
            self._send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": "[What Aethron shows now, for context only, do not answer this: " + st + "]"}]}],
                                          "turnComplete": False}})

    def stop(self, why: str = "done", detail: str = ""):
        if self.closed:
            return
        self.closed = True
        self.why = why
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self._emit("end", why=why, detail=detail, turns=self.turns, usd=round(self.spent, 6))
        with self.cond:
            self.cond.notify_all()
        cb, self.on_end = self.on_end, None
        if callable(cb):
            try:
                cb(self)
            except Exception:
                pass

    def status(self) -> dict:
        return {"id": self.id, "live": not self.closed, "model": self.model, "turns": self.turns, "why": self.why,
                "speaking": self.gate.speaking(), "free": self.free, "seconds": round(time.time() - self.started, 1),
                "sent": self.sent_audio}


# ───────────────────────────────────────────────────────── one at a time

_CUR = {"s": None, "mic": None}
_LOCK = threading.Lock()


def current():
    s = _CUR["s"]
    return s if s and not s.closed else None


def find(sid: str = ""):
    """The conversation with this id (finished or not), or the current one
    when no id is given. A poll for an older conversation must never be
    answered for a newer one: that is how a stale "closed" ended a new talk."""
    s = _CUR["s"]
    if not sid:
        return s
    return s if s and s.id == sid else None


def begin(system: str, model: str = "gemini-3.8-live", voice: str = "Aoede", projects=None,
          native_mic: bool = False) -> dict:
    """Start a conversation (ending any other). With native_mic, this
    computer's own microphone feeds it directly (the Mac); otherwise the
    window sends its microphone with `audio()`."""
    with _LOCK:
        old = _CUR["s"]
        if old and not old.closed:
            old.stop("replaced")
        _stop_mic()
        s = Session(system, model=model, voice=voice, projects=projects, on_end=lambda _s: _stop_mic())
        got = s.start()
        if not got.get("ok"):
            return got
        _CUR["s"] = s
        mic = "page"
        if native_mic:
            try:
                import aethron_voice
                st = aethron_voice.Streamer(s.audio)
                why = st.start()
                if why:
                    s._emit("log", text=why)
                    if "switched off" in why:
                        s.stop("mic", why)
                        return {"ok": False, "why": why}
                else:
                    _CUR["mic"], mic = st, "native"
            except Exception as e:
                s._emit("log", text=f"the Mac's microphone did not start ({type(e).__name__})")
        return dict(got, mic=mic)


def _stop_mic():
    m, _CUR["mic"] = _CUR["mic"], None
    if m:
        try:
            m.stop()
        except Exception:
            pass


def end(why: str = "done"):
    s = _CUR["s"]
    if s:
        s.stop(why)
    _stop_mic()


# ───────────────────────────────────────────────────────── selftest

class FakeLive:
    """A pretend Gemini Live on 127.0.0.1: answers setup, records what it is
    sent, and plays a script when told. For the selftest and the browser
    battery (AETHRON_LIVE_URL points at it)."""

    def __init__(self):
        import hashlib
        self._h = hashlib
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}{PATH}"
        self.setups, self.got, self.conns = [], [], []
        self.refuse = 0
        self.on_message = None           # fn(fake, conn, msg) to answer like the service would
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        head = b""
        while b"\r\n\r\n" not in head:
            d = c.recv(4096)
            if not d:
                return
            head += d
        req, rest = head.split(b"\r\n\r\n", 1)
        if self.refuse:
            self.refuse -= 1
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            c.close()
            return
        key = re.search(rb"Sec-WebSocket-Key: *(\S+)", req, re.I).group(1)
        acc = base64.b64encode(self._h.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
        c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        conn = {"c": c, "path": req.split(b" ")[1].decode(), "lock": threading.Lock()}
        self.conns.append(conn)
        buf = rest
        while True:
            try:
                while True:
                    f = self._frame(buf)
                    if f:
                        break
                    d = c.recv(65536)
                    if not d:
                        return
                    buf += d
            except OSError:
                return
            op, data, buf = f
            if op == 8:
                try:
                    c.close()
                except OSError:
                    pass
                return
            try:
                m = json.loads(data.decode() or "{}")
            except ValueError:
                continue
            if "setup" in m:
                self.setups.append(m["setup"])
                self.send(conn, {"setupComplete": {}})
                continue
            self.got.append(m)
            if self.on_message:
                try:
                    self.on_message(self, conn, m)
                except Exception as e:  # pragma: no cover
                    print("fake live:", e)

    @staticmethod
    def _frame(b):
        if len(b) < 2:
            return None
        n, i = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        mask = None
        if b[1] & 0x80:
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        data = b[i:i + n]
        if mask and n:
            m = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        return b[0] & 0x0F, data, b[i + n:]

    def send(self, conn, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = bytes([0x81]) + (bytes([n]) if n < 126 else (bytes([126]) + struct.pack(">H", n) if n < 65536
                                                          else bytes([127]) + struct.pack(">Q", n)))
        with conn["lock"]:
            conn["c"].sendall(hdr + data)

    def last(self):
        return self.conns[-1] if self.conns else None

    @staticmethod
    def tone(seconds=0.3, hz=220, level=0.4, rate=OUT_RATE) -> str:
        n = int(seconds * rate)
        a = array("h", (int(level * 32767 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))
        if sys.byteorder == "big":
            a.byteswap()
        return base64.b64encode(a.tobytes()).decode()

    def close(self):
        try:
            self.srv.close()
        except OSError:
            pass
        for c in self.conns:
            try:
                c["c"].close()
            except OSError:
                pass


def _selftest() -> int:
    ok = bad = 0

    def check(name, cond, extra=""):
        nonlocal ok, bad
        ok += bool(cond)
        bad += not cond
        print(("  ok   " if cond else "  FAIL ") + name + ("" if cond or not extra else f"   {extra}"))

    os.environ["AETHRON_USAGE"] = "off"
    # the resampler and loudness
    sig = [math.sin(2 * math.pi * 440 * i / 48000) * 0.5 for i in range(4800)]
    pcm = to16k(sig, 48000)
    check("48 kHz comes down to 16 kHz (100 ms = 1600 samples)", abs(len(pcm) // 2 - 1600) <= 1, str(len(pcm) // 2))
    check("and keeps its loudness", abs(rms16(pcm) - 0.5 / math.sqrt(2)) < 0.03, f"{rms16(pcm):.3f}")
    check("44.1 kHz too", abs(len(to16k(sig[:4410], 44100)) // 2 - 1600) <= 2)
    check("silence is quiet", rms16(b"\x00\x00" * 800) == 0.0)

    # the echo gate
    def tone16(level, n=1600):
        a = array("h", (int(level * 32767 * math.sin(2 * math.pi * 300 * i / IN_RATE)) for i in range(n)))
        return a.tobytes()
    g = EchoGate()
    t = 1000.0
    check("with Ember quiet, everything is sent", g.admit(tone16(0.01), t) and g.admit(tone16(0.3), t + .1))
    voice = base64.b64decode(FakeLive.tone(3.0, level=0.5))
    g.played(voice, t + 1)
    sent = sum(len(g.admit(tone16(0.12), t + 1.2 + i * .1)) for i in range(15))
    check("Ember's own voice coming back through the microphone is held back", sent == 0, f"{sent} sent, ratio {g.ratio:.2f}")
    got = []
    for i in range(4):
        got += g.admit(tone16(0.9), t + 2.8 + i * .1)
    check("talking over Ember gets through, with what came just before it", len(got) >= 3, f"{len(got)} pieces")
    g.interrupted(t + 3.3)
    check("after an interruption the gate stops expecting Ember's voice", not g.speaking(t + 3.3 + EchoGate.TAIL + .01))

    # a whole conversation against a pretend service
    fake = FakeLive()
    os.environ["AETHRON_LIVE_URL"] = fake.url

    def answer(fk, conn, m):
        ri = m.get("realtimeInput") or {}
        if ri.get("audio") or ri.get("text"):
            if getattr(answer, "done", 0):
                return
            answer.done = 1
            fk.send(conn, {"serverContent": {"inputTranscription": {"text": "hey ember do a little "}}})
            fk.send(conn, {"serverContent": {"inputTranscription": {"text": "dance"}}})
            fk.send(conn, {"toolCall": {"functionCalls": [{"id": "c1", "name": "ember_behave", "args": {
                "reply_json": json.dumps({"kind": "answer", "say": "watch this", "plan": [{"do": "celebrate"}]})}}]}})
            for _ in range(3):
                fk.send(conn, {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": FakeLive.tone(.2)}}]}}})
            fk.send(conn, {"serverContent": {"outputTranscription": {"text": "watch this"}}})
            fk.send(conn, {"usageMetadata": {"promptTokenCount": 100, "responseTokenCount": 50,
                                             "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 80}, {"modality": "TEXT", "tokenCount": 20}],
                                             "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 50}]}})
            fk.send(conn, {"sessionResumptionUpdate": {"newHandle": "h-1", "resumable": True}})
            fk.send(conn, {"serverContent": {"turnComplete": True}})
    fake.on_message = answer
    s = Session("You are Ember.", key_list=[("k-free", True)], projects=["demo"])
    r = s.start()
    check("it connects and sets up", r.get("ok") and fake.setups, str(r))
    st = fake.setups[0] if fake.setups else {}
    check("asking for audio in Ember's voice", st.get("generationConfig", {}).get("responseModalities") == ["AUDIO"]
          and st["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Aoede", str(st)[:200])
    check("with both transcriptions, the body tool and barge-in on",
          "inputAudioTranscription" in st and "outputAudioTranscription" in st
          and st["tools"][0]["functionDeclarations"][0]["name"] == "ember_behave"
          and st["realtimeInputConfig"]["activityHandling"] == "START_OF_ACTIVITY_INTERRUPTS")
    check("the key goes in the address, not the setup", "key=k-free" in fake.last()["path"] and "k-free" not in json.dumps(st))
    n = s.audio(tone16(0.3))
    time.sleep(.6)
    a = [m for m in fake.got if (m.get("realtimeInput") or {}).get("audio")]
    check("microphone audio goes out as 16 kHz PCM", n == 1 and a and a[0]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000", str(a[:1])[:160])
    ev = s.read(0, 3)["events"]
    kinds = [e["t"] for e in ev]
    check("what they said arrives as they say it", any(e["t"] == "heard" and e["text"] == "hey ember do a little dance" for e in ev), str(kinds))
    acts = [e for e in ev if e["t"] == "act"]
    check("the body tool comes through validated", acts and acts[0]["r"].get("kind") == "answer" and acts[0]["r"].get("plan"), str(acts)[:200])
    tr = [m for m in fake.got if "toolResponse" in m]
    check("and is answered so Ember goes on to speak", tr and tr[0]["toolResponse"]["functionResponses"][0]["id"] == "c1")
    check("the voice arrives in pieces, as it is made", sum(1 for e in ev if e["t"] == "audio") == 3)
    for _ in range(20):
        if any(e["t"] == "turn" for e in s.read(0, .2)["events"]):
            break
    ev = s.read(0, .2)["events"]
    turn = [e for e in ev if e["t"] == "turn"]
    check("a finished turn says what was said on both sides", turn and turn[0]["heard"] == "hey ember do a little dance" and turn[0]["said"] == "watch this", str(turn))
    check("and the turn's tokens are priced at the Live rates", abs(_cost("gemini-3.8-live", {"promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 1_000_000}],
                                                                       "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 1_000_000}]})[2] - 15.0) < 1e-6)
    check("a free key costs nothing against the session limit", s.spent == 0)
    # talking over Ember
    fake.send(fake.last(), {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": FakeLive.tone(2.0), "mimeType": "audio/pcm;rate=24000"}}]}}})
    time.sleep(.3)
    check("while Ember talks, the gate knows", s.gate.speaking())
    fake.send(fake.last(), {"serverContent": {"interrupted": True}})
    time.sleep(.3)
    ev2 = s.read(0, .2)["events"]
    check("an interruption reaches the window", any(e["t"] == "interrupted" for e in ev2))
    check("and Ember stops expecting its own voice", not s.gate.speaking(time.time() + EchoGate.TAIL + .01))
    # the service asks to reconnect
    before = len(fake.conns)
    fake.send(fake.last(), {"goAway": {"timeLeft": "5s"}})
    for _ in range(30):
        if len(fake.conns) > before:
            break
        time.sleep(.1)
    check("when the service asks to move, it reconnects and carries on", len(fake.conns) > before and fake.setups[-1].get("sessionResumption", {}).get("handle") == "h-1",
          str(fake.setups[-1].get("sessionResumption")))
    s.state("Open project: demo")
    time.sleep(.3)
    cc = [m for m in fake.got if "clientContent" in m]
    check("what Aethron shows is told without asking for an answer", cc and cc[-1]["clientContent"]["turnComplete"] is False)
    s.stop("done")
    check("stopping ends it and says so", s.read(0, .2)["events"][-1]["t"] == "end" and s.closed)
    check("a closed conversation sends nothing", s.audio(tone16(.3)) == 0)

    # refusals
    fake.refuse = 4
    s2 = Session("x", key_list=[("bad1", True), ("bad2", True)])
    r2 = s2.start()
    check("a service that refuses every key says so, it does not hang", not r2["ok"] and "did not answer" in r2["why"], str(r2))
    s3 = Session("x", key_list=[])
    check("no Gemini key: told where to add one", not s3.start()["ok"])
    # quiet ends it
    global IDLE_S
    keep, IDLE_S = IDLE_S, 0.6
    fake.on_message = None
    fake.refuse = 0
    s4 = Session("x", key_list=[("k", True)])
    r4 = s4.start()
    for _ in range(40):
        if s4.closed:
            break
        time.sleep(.1)
    IDLE_S = keep
    check("when nobody says anything for a while, it ends by itself", r4.get("ok") and s4.closed and s4.why == "quiet", f"{r4} {s4.why}")
    fake.close()
    os.environ.pop("AETHRON_LIVE_URL", None)
    print(f"\n{ok}/{ok + bad} live checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(available()))

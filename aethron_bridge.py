#!/usr/bin/env python3
"""Aethron Bridge — one API key drives everything.

THE PROBLEM IT SOLVES: Aethron's template side already speaks to any
model (DeepSeek, Gemini, OpenAI, Groq, OpenRouter, Ollama, Anthropic).
The coding side drives the Claude Code CLI, and that CLI speaks exactly
one dialect: the Anthropic Messages API. So without a translator the
owner would need TWO configurations — a DeepSeek key for copy and an
Anthropic account for code. That is not the product.

This is the translator, in one stdlib file:

    claude CLI ──Anthropic /v1/messages──> BRIDGE ──OpenAI /chat/completions──> DeepSeek
                                             │                                  Gemini
                <────── Anthropic SSE ───────┘                                  OpenAI
                                                                                Groq …

Set ONE key in Aethron's settings and it powers copy fill, plan
polish, design matching, self-heal AND the coding agent. Nothing is
vendored and no external proxy is required — this is ~500 lines we own,
zero dependencies, and it is what makes "any API key" true rather than
aspirational.

Run it standalone:
    python3 aethron_bridge.py --port 8790          (reads Aethron settings)
    python3 aethron_bridge.py --selftest           (no key needed)
"""
import json
import sys
import urllib.parse
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MAX_TOKENS = 8192


# ─────────────────── Anthropic  ->  OpenAI  (request) ────────────────

def _text_of(content) -> str:
    """Anthropic content is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def to_openai(body: dict, model: str = "") -> dict:
    """Anthropic Messages request -> OpenAI chat/completions request.

    Handles the parts that actually matter for an agent: system prompt,
    multi-block messages, tool definitions, tool calls and tool results.
    Unknown fields are dropped rather than forwarded — a provider that
    rejects an unknown key would break the whole session."""
    msgs = []
    system = body.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system)})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        text_parts, tool_calls, tool_results = [], [], []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text", ""))
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id", ""), "type": "function",
                    "function": {"name": b.get("name", ""),
                                 "arguments": json.dumps(b.get("input", {}))}})
            elif t == "tool_result":
                tool_results.append({
                    "role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                    "content": _tool_result_text(b.get("content"))})
            elif t == "image":
                # keep the turn coherent even where vision is unsupported
                text_parts.append("[image omitted by the Aethron bridge]")
        # tool results are their own OpenAI messages and must come first
        msgs.extend(tool_results)
        if role == "assistant" and tool_calls:
            msgs.append({"role": "assistant",
                         "content": "\n".join(text_parts) or None,
                         "tool_calls": tool_calls})
        elif text_parts or not tool_results:
            msgs.append({"role": role, "content": "\n".join(text_parts)})

    out = {"model": model or body.get("model", ""), "messages": msgs,
           "max_tokens": body.get("max_tokens") or DEFAULT_MAX_TOKENS,
           "stream": bool(body.get("stream"))}
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    tools = []
    for t in body.get("tools") or []:
        if not t.get("name"):
            continue          # server-side tool defs (web_search etc.)
        tools.append({"type": "function", "function": {
            "name": t["name"], "description": t.get("description", "")[:1024],
            "parameters": t.get("input_schema") or {"type": "object",
                                                    "properties": {}}}})
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc["name"]}}
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    return out


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict))
    return json.dumps(content) if content is not None else ""


STOP_MAP = {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use", "function_call": "tool_use",
            "content_filter": "end_turn"}


# ─────────────────── OpenAI  ->  Anthropic  (response) ───────────────

class AnthropicStream:
    """Builds the Anthropic SSE event sequence from OpenAI stream deltas.

    Claude Code is strict about this shape: message_start, then one
    content_block_start/delta*/stop per block, then message_delta with
    the stop reason, then message_stop. Tool calls arrive as
    input_json_delta chunks — assembling them wrongly is the difference
    between a working agent and a silent one."""

    def __init__(self, model: str, emit):
        self.model = model
        self.emit = emit
        self.started = False
        self.index = -1
        self.open_kind = None      # 'text' | 'tool'
        self.tool_slots = {}       # openai index -> our block index
        self.stop = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _ev(self, name, data):
        self.emit(name, data)

    def start(self):
        if self.started:
            return
        self.started = True
        self._ev("message_start", {"type": "message_start", "message": {
            "id": "msg_bridge", "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": self.usage}})

    def _close_block(self):
        if self.open_kind is not None:
            self._ev("content_block_stop", {"type": "content_block_stop",
                                            "index": self.index})
            self.open_kind = None

    def text(self, chunk: str):
        if not chunk:
            return
        self.start()
        if self.open_kind != "text":
            self._close_block()
            self.index += 1
            self.open_kind = "text"
            self._ev("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "text", "text": ""}})
        self._ev("content_block_delta", {
            "type": "content_block_delta", "index": self.index,
            "delta": {"type": "text_delta", "text": chunk}})

    def tool(self, slot: int, call_id: str, name: str, args_chunk: str):
        self.start()
        if slot not in self.tool_slots:
            self._close_block()
            self.index += 1
            self.tool_slots[slot] = self.index
            self.open_kind = "tool"
            self._ev("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "tool_use",
                                  "id": call_id or f"toolu_{self.index}",
                                  "name": name or "tool", "input": {}}})
        if args_chunk:
            self._ev("content_block_delta", {
                "type": "content_block_delta",
                "index": self.tool_slots[slot],
                "delta": {"type": "input_json_delta",
                          "partial_json": args_chunk}})

    def finish(self, stop_reason=None, usage=None):
        self.start()
        self._close_block()
        if usage:
            self.usage.update(usage)
        self._ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason or self.stop,
                      "stop_sequence": None},
            "usage": {"output_tokens": self.usage.get("output_tokens", 0)}})
        self._ev("message_stop", {"type": "message_stop"})


def from_openai_message(data: dict, model: str) -> dict:
    """Non-streaming OpenAI response -> Anthropic message object."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({"type": "tool_use", "id": tc.get("id", ""),
                        "name": fn.get("name", ""), "input": args})
    u = data.get("usage") or {}
    return {"id": data.get("id", "msg_bridge"), "type": "message",
            "role": "assistant", "model": model or data.get("model", ""),
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": STOP_MAP.get(choice.get("finish_reason"),
                                        "end_turn"),
            "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0),
                      "output_tokens": u.get("completion_tokens", 0)}}


# ─────────────────────────── the server ──────────────────────────────

class BridgeConfig:
    """Where upstream lives. One place, so the studio, the CLI and the
    desktop app cannot drift apart."""

    def __init__(self, base_url, api_key, model="", token="aethron"):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.token = token          # what the CLI must present to US

    @property
    def endpoint(self):
        base = self.base_url
        if not base.endswith("/v1") and "/v1" not in base:
            base += "/v1"
        return base + "/chat/completions"


def _handler(cfg: BridgeConfig, log=None):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _say(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, msg):
            # Anthropic error shape: the CLI renders `message` verbatim,
            # so upstream failures reach the user instead of "unknown".
            self._say(code, {"type": "error",
                             "error": {"type": "api_error", "message": msg}})

        @property
        def route(self):
            # the CLI calls /v1/messages?beta=true — matching on the raw
            # path 404s it, and the CLI reports that as "the model may
            # not exist", which sends you hunting in entirely the wrong
            # place. Always compare the path WITHOUT the query.
            return urllib.parse.urlparse(self.path).path.rstrip("/")

        def do_GET(self):
            if self.route.startswith("/health"):
                return self._say(200, {"ok": True, "upstream": cfg.base_url,
                                       "model": cfg.model})
            self._error(404, "not found")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if self.route.endswith("count_tokens"):
                # rough but honest: 4 chars ≈ 1 token. Only used for UI
                # meters; never for correctness.
                return self._say(200, {"input_tokens": max(1, len(raw) // 4)})
            if not self.route.endswith("/v1/messages"):
                return self._error(404, f"unsupported path {self.route}")
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._error(400, "bad JSON")
            req = to_openai(body, cfg.model)
            if log:
                log({"event": "request", "model": req["model"],
                     "messages": len(req["messages"]),
                     "tools": len(req.get("tools") or []),
                     "stream": req["stream"]})
            if req["stream"]:
                return self._stream(req, body)
            return self._once(req)

        # -- upstream ------------------------------------------------
        def _open(self, req):
            headers = {"Content-Type": "application/json"}
            if cfg.api_key:
                headers["Authorization"] = "Bearer " + cfg.api_key
            r = urllib.request.Request(cfg.endpoint,
                                       json.dumps(req).encode(), headers)
            return urllib.request.urlopen(r, timeout=600)

        def _once(self, req):
            try:
                with self._open(req) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                return self._error(e.code, _upstream_error(e))
            except Exception as e:
                return self._error(502, f"bridge could not reach "
                                        f"{cfg.base_url}: {e}")
            self._say(200, from_openai_message(data, cfg.model))

        def _stream(self, req, original):
            try:
                up = self._open(req)
            except urllib.error.HTTPError as e:
                return self._error(e.code, _upstream_error(e))
            except Exception as e:
                return self._error(502, f"bridge could not reach "
                                        f"{cfg.base_url}: {e}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(name, data):
                self.wfile.write(f"event: {name}\ndata: "
                                 f"{json.dumps(data)}\n\n".encode())
                self.wfile.flush()

            out = AnthropicStream(cfg.model or req.get("model", ""), emit)
            stop, usage = None, {}
            names = {}
            try:
                for line in up:
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)
                    except ValueError:
                        continue
                    if d.get("usage"):
                        u = d["usage"]
                        usage = {"input_tokens": u.get("prompt_tokens", 0),
                                 "output_tokens": u.get("completion_tokens", 0)}
                    for ch in d.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            out.text(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = tc.get("index", 0)
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                names[slot] = fn["name"]
                            out.tool(slot, tc.get("id", ""),
                                     names.get(slot, ""),
                                     fn.get("arguments") or "")
                        if ch.get("finish_reason"):
                            stop = STOP_MAP.get(ch["finish_reason"],
                                                "end_turn")
            except Exception as e:                       # upstream died
                out.text(f"\n[bridge: upstream stream failed: {e}]")
            finally:
                try:
                    out.finish(stop, usage)
                except Exception:
                    pass
                up.close()

    return H


def _upstream_error(e) -> str:
    try:
        detail = json.loads(e.read())
        msg = (detail.get("error") or {}).get("message") or json.dumps(detail)
    except Exception:
        msg = e.reason if hasattr(e, "reason") else str(e)
    return f"upstream {e.code}: {str(msg)[:400]}"


def start(cfg: BridgeConfig, port=0, log=None):
    """-> (server, url). Serve it in a thread; stop with server.shutdown()."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler(cfg, log))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ─────────────────────────── selftest ────────────────────────────────

def _mock_openai(script):
    """A minimal OpenAI-compatible upstream: [{'text':…}|{'tool':name,
    'input':{…}}] per turn, chosen by whether tool results came back."""
    turns = list(script)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            seen = sum(1 for m in body.get("messages", [])
                       if m.get("role") == "tool")
            blocks = turns[min(seen, len(turns) - 1)]
            if not body.get("tools"):
                blocks = [{"text": "ok"}]
            chunks = []
            for i, b in enumerate(blocks):
                if "tool" in b:
                    chunks.append({"choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": i, "id": f"call_{i}",
                                        "type": "function",
                                        "function": {"name": b["tool"],
                                                     "arguments": ""}}]}}]})
                    chunks.append({"choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": i, "function": {
                            "arguments": json.dumps(b.get("input", {}))}}]}}]})
                else:
                    for piece in _split(b.get("text", "")):
                        chunks.append({"choices": [{"index": 0,
                                                    "delta": {"content": piece}}]})
            fin = "tool_calls" if any("tool" in b for b in blocks) else "stop"
            if not body.get("stream"):
                msg = {"role": "assistant",
                       "content": "".join(b.get("text", "") for b in blocks),
                       "tool_calls": [
                           {"id": f"call_{i}", "type": "function",
                            "function": {"name": b["tool"],
                                         "arguments": json.dumps(
                                             b.get("input", {}))}}
                           for i, b in enumerate(blocks) if "tool" in b]}
                out = json.dumps({"id": "cmpl", "choices": [
                    {"index": 0, "message": msg, "finish_reason": fin}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            chunks.append({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": fin}],
                           "usage": {"prompt_tokens": 10,
                                     "completion_tokens": 5}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _split(text, n=12):
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def selftest() -> int:
    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, ok))
        print(("  ok   " if ok else "  FAIL ") + name
              + (f"   {detail}" if not ok and detail else ""))

    # 1. request translation (the part that silently breaks agents)
    req = to_openai({
        "system": [{"type": "text", "text": "be brief"}],
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "a.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "contents"}]}]}],
        "tools": [{"name": "Read", "description": "read a file",
                   "input_schema": {"type": "object",
                                    "properties": {"file_path":
                                                   {"type": "string"}}}}],
        "max_tokens": 100, "stream": True}, model="deepseek-chat")
    ck("system prompt becomes an OpenAI system message",
       req["messages"][0] == {"role": "system", "content": "be brief"})
    asst = next(m for m in req["messages"] if m["role"] == "assistant")
    ck("tool_use becomes an OpenAI tool_call",
       asst["tool_calls"][0]["function"]["name"] == "Read"
       and json.loads(asst["tool_calls"][0]["function"]["arguments"])
       == {"file_path": "a.txt"})
    tool_msg = next(m for m in req["messages"] if m["role"] == "tool")
    ck("tool_result becomes an OpenAI tool message",
       tool_msg["tool_call_id"] == "t1" and tool_msg["content"] == "contents")
    ck("tools convert to function schemas",
       req["tools"][0]["function"]["name"] == "Read")
    ck("model override wins", req["model"] == "deepseek-chat")

    # 2. response translation, end to end over HTTP
    up, up_url = _mock_openai([[{"text": "hello there"},
                                {"tool": "Write",
                                 "input": {"file_path": "x", "content": "y"}}]])
    srv, url = start(BridgeConfig(up_url, "k", "deepseek-chat"))
    body = json.dumps({"model": "claude-x", "max_tokens": 50, "stream": True,
                       "messages": [{"role": "user", "content": "go"}],
                       "tools": [{"name": "Write", "input_schema":
                                  {"type": "object"}}]}).encode()
    r = urllib.request.urlopen(urllib.request.Request(
        url + "/v1/messages", body, {"Content-Type": "application/json"}),
        timeout=30)
    events = []
    for line in r:
        s = line.decode().strip()
        if s.startswith("event:"):
            events.append(s.split(":", 1)[1].strip())
        elif s.startswith("data:") and "input_json_delta" in s:
            events.append("TOOLJSON:" + json.loads(s[5:])["delta"]
                          ["partial_json"])
    ck("emits message_start first", events and events[0] == "message_start")
    ck("streams a text block", "content_block_start" in events
       and "content_block_delta" in events)
    ck("streams tool input as input_json_delta",
       any(e.startswith("TOOLJSON:") for e in events))
    ck("tool arguments survive the round trip",
       any(json.loads(e[9:]) == {"file_path": "x", "content": "y"}
           for e in events if e.startswith("TOOLJSON:")))
    ck("ends with message_stop", events[-1] == "message_stop")

    # 3. non-streaming path
    body = json.dumps({"model": "claude-x", "max_tokens": 50,
                       "messages": [{"role": "user", "content": "go"}]}).encode()
    r = urllib.request.urlopen(urllib.request.Request(
        url + "/v1/messages", body, {"Content-Type": "application/json"}),
        timeout=30)
    msg = json.loads(r.read())
    ck("non-streaming returns an Anthropic message",
       msg["type"] == "message" and msg["role"] == "assistant")

    # 4. upstream failures reach the caller as readable errors
    bad_srv, bad_url = start(BridgeConfig("http://127.0.0.1:9", "k", "m"))
    try:
        urllib.request.urlopen(urllib.request.Request(
            bad_url + "/v1/messages", body,
            {"Content-Type": "application/json"}), timeout=30)
        ck("unreachable upstream reports an error", False)
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read())
        ck("unreachable upstream reports an error",
           e.code == 502 and "could not reach" in
           detail["error"]["message"])
    bad_srv.shutdown()
    srv.shutdown()
    up.shutdown()

    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    try:
        import aethron_brain as brain
        cfg = brain.bridge_config()
    except Exception as e:
        print(f"no AI settings ({e}) — set them in the studio or "
              f"aethron_config.json")
        return 1
    port = 0
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    srv, url = start(cfg, port)
    print(f"Aethron bridge: {url}/v1/messages -> {cfg.base_url} "
          f"({cfg.model or 'provider default'})")
    print("point Claude Code at it:")
    print(f"  ANTHROPIC_BASE_URL={url} ANTHROPIC_AUTH_TOKEN={cfg.token} claude")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

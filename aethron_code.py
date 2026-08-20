#!/usr/bin/env python3
"""Aethron Code — the coding layer. Aethron stops being a template tool
here and becomes a development platform.

THE IDEA, in one paragraph: a coding agent is a process that reads and
writes files in a workspace and streams what it is doing. Claude Code is
the best one that exists, and it already speaks a machine protocol
(`--input-format stream-json --output-format stream-json`). So Aethron
does not reimplement it and does not wrap a fork of it: it OWNS the
workspace, the tools and the UI, and drives the agent process behind a
stable interface. Swap the process, keep the platform.

WHY THIS ANSWERS "MAKE IT FREE" WITHOUT A FORK: Claude Code's provider
is an environment variable. `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`
point it at ANY Anthropic-compatible endpoint — the user's own Claude
subscription, a local Free-Claude-Code proxy (localhost:8082, 48+
providers), an OpenRouter/DeepSeek gateway, or Aethron's own hosted
gateway later. That is precisely what `fcc-claude` does, so integrating
"free Claude Code" is a PRESET here, not a dependency: nothing is
vendored, nothing is patched, no third-party CLI is redistributed.

    provider preset ─┐
                     ├─> ClaudeCodeRuntime (the `claude` CLI, stream-json)
    workspace ───────┤
                     └─> InternalRuntime  (our own loop — no CLI needed)
                                │
                     normalized events -> Aethron IDE / studio / API

THE SAFETY RULE IS UNCHANGED: the agent proposes, deterministic checks
dispose. The workspace is the sandbox (cwd, no --add-dir), Aethron's own
MCP tools are injected so migrations still go through the guarded
pipeline, and permission mode is ours to set — not the model's.

Run it:
    python3 aethron_code.py <workspace> "your prompt"      one-shot
    python3 aethron_code.py --status                       what's available
    python3 aethron_code.py --selftest                     proves the whole
                                                           pipe with a mock
                                                           provider (no key,
                                                           no login, no cost)
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE_MCP = ROOT / "forge_mcp.py"

# ─────────────────────── provider presets ────────────────────────────
# Everything Claude Code needs to talk to something other than
# Anthropic. `token` is a DEFAULT, always overridable by config/env —
# never a secret we ship.
PROVIDERS = {
    # DEFAULT: whatever single key the owner set in Aethron's settings —
    # DeepSeek, Gemini, OpenAI, Groq, Ollama, Anthropic. Non-Anthropic
    # wires are translated by aethron_bridge on the way through, so one
    # key really does power both the design side and the code side.
    "auto": {"label": "Aethron AI settings (one key for everything)",
             "base_url": "", "token": "", "needs_login": False},
    # the user's own Claude subscription or API key (claude /login)
    "anthropic": {"label": "Anthropic (your Claude account)",
                  "base_url": "", "token": "", "needs_login": True},
    # Free Claude Code: local proxy, 48+ providers behind one endpoint
    "fcc": {"label": "Free Claude Code proxy (local)",
            "base_url": "http://127.0.0.1:8082", "token": "freecc",
            "needs_login": False,
            "hint": "start it with `fcc-server` (see the FCC README)"},
    # any Anthropic-compatible gateway, incl. Aethron's own later
    "custom": {"label": "Custom Anthropic-compatible endpoint",
               "base_url": "", "token": "", "needs_login": False},
}

DEFAULT_CFG = {
    "runtime": "claude-code",      # or "internal"
    "provider": "auto",            # follow Aethron's one AI setting
    "base_url": "",                # overrides the preset
    "token": "",
    "model": "",                   # "" = the runtime's default
    "permission_mode": "acceptEdits",
    "allowed_tools": [],           # [] = the runtime's default set
    "disallowed_tools": [],
    "isolate": True,               # ignore the user's own MCP servers
    "aethron_tools": True,         # inject Aethron's MCP (guarded pipeline)
    "max_budget_usd": 0,           # 0 = no cap
}


# What a general-purpose coding agent must know the moment its
# workspace is an Aethron template project. Without this it will
# "helpfully" hand-edit site/ or pristine/ — the exact failure that
# corrupted a project once (a foreign agent rewrote pristine/index.html
# and hydration silently reverted it).
PROJECT_RULES = """
This workspace is an Aethron template project. Two rules override any
instinct you have about editing files here:

1. NEVER edit `site/` or `pristine/`. `site/` is generated output and
   `pristine/` is the sealed original (sha256-manifested — edits are
   detected and fail verification). The same text lives in up to three
   layers (HTML, JS chunks, CMS binaries) and the browser compares them
   on load, so a hand edit either reverts on hydration or breaks the
   page. ALL content changes go through copy_map.json + a build.
2. Use the `mcp__aethron__*` tools for content, styles, removals,
   builds and checks. They enforce the physics (CMS byte budgets,
   forbidden characters, snapshots you can undo).

After changing anything: `build`, then `verify` (files), then `probe`
(what the browser actually does). If an edit lands nowhere, run `heal`
before trying anything clever. Everything else in the workspace —
backend/, your own code, new apps — is normal code you own and may
edit freely.
"""


def load_config(home: Path = None) -> dict:
    """aethron_config.json -> {"code": {...}}, env wins over the file."""
    cfg = dict(DEFAULT_CFG)
    for f in (ROOT / "aethron_config.json",
              (home or ROOT) / "aethron_config.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cfg.update(data.get("code") or {})
        except Exception:
            pass
    env = os.environ
    if env.get("AETHRON_CODE_PROVIDER"):
        cfg["provider"] = env["AETHRON_CODE_PROVIDER"]
    if env.get("AETHRON_CODE_BASE_URL"):
        cfg["base_url"] = env["AETHRON_CODE_BASE_URL"]
    if env.get("AETHRON_CODE_TOKEN"):
        cfg["token"] = env["AETHRON_CODE_TOKEN"]
    if env.get("AETHRON_CODE_MODEL"):
        cfg["model"] = env["AETHRON_CODE_MODEL"]
    if env.get("AETHRON_CODE_RUNTIME"):
        cfg["runtime"] = env["AETHRON_CODE_RUNTIME"]
    return cfg


def resolve_provider(cfg: dict) -> dict:
    """-> {base_url, token, label, ready, why}"""
    if cfg.get("provider", "auto") == "auto":
        # ONE KEY FOR EVERYTHING: ask the brain for an Anthropic-speaking
        # endpoint. It hands back the provider directly when it already
        # speaks that wire, and starts the translator when it doesn't.
        try:
            import aethron_brain as brain
            e = brain.anthropic_endpoint()
            if e["mode"] == "unconfigured":
                return {"base_url": "", "token": "", "ready": False,
                        "label": "Aethron AI settings", "hint": "",
                        "why": e["why"] + " in the AI settings"}
            return {"base_url": e["base_url"], "token": e["token"],
                    "label": "Aethron AI settings", "ready": True,
                    "why": e["why"], "hint": "",
                    "mode": e["mode"], "model": e["model"]}
        except Exception as ex:                        # pragma: no cover
            return {"base_url": "", "token": "", "ready": False,
                    "label": "Aethron AI settings", "hint": "",
                    "why": f"AI settings unavailable: {ex}"}
    preset = PROVIDERS.get(cfg.get("provider", "anthropic"),
                           PROVIDERS["anthropic"])
    base = cfg.get("base_url") or preset["base_url"]
    token = cfg.get("token") or preset["token"]
    ready, why = True, ""
    if preset.get("needs_login") and not base:
        # the CLI carries its own OAuth/keychain login; we cannot see it,
        # so we do not claim it works — the first run says so honestly
        why = "uses the `claude` CLI's own login (run `claude` once)"
    elif base:
        why = f"routed to {base}"
    return {"base_url": base, "token": token, "label": preset["label"],
            "ready": ready, "why": why,
            # an explicit endpoint may itself be a translator (someone
            # else's, or ours started by hand) — say so and the model
            # name stays the endpoint's business
            "mode": cfg.get("mode", "direct"),
            "hint": preset.get("hint", "")}


def find_claude() -> str:
    for name in ("claude",):
        p = shutil.which(name)
        if p:
            return p
    for p in (Path.home() / ".local/bin/claude",
              Path("/usr/local/bin/claude"),
              Path.home() / ".claude/local/claude"):
        if p.exists():
            return str(p)
    return ""


_VERSION_CACHE = {}


def claude_version(binary: str = "") -> str:
    """Memoised: the IDE asks on every status poll, and spawning a
    process per poll made the whole view feel slow."""
    b = binary or find_claude()
    if not b:
        return ""
    if b in _VERSION_CACHE:
        return _VERSION_CACHE[b]
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True,
                           timeout=20)
        v = r.stdout.strip().splitlines()[0] if r.returncode == 0 else ""
    except Exception:
        v = ""
    _VERSION_CACHE[b] = v
    return v


def status(home: Path = None) -> dict:
    """What can this machine actually run? Never a guess — the IDE shows
    this verbatim, and an unavailable runtime says why."""
    cfg = load_config(home)
    binary = find_claude()
    prov = resolve_provider(cfg)
    fcc_up = _port_open(prov["base_url"]) if prov["base_url"] else None
    return {
        "config": cfg,
        "provider": prov,
        "providers": {k: v["label"] for k, v in PROVIDERS.items()},
        "runtimes": {
            "claude-code": {"available": bool(binary), "binary": binary,
                            "version": claude_version(binary),
                            "why": "" if binary else
                            "the `claude` CLI is not installed "
                            "(npm i -g @anthropic-ai/claude-code)"},
            "internal": {"available": True, "binary": "",
                         "version": "aethron-agent",
                         "why": "always available; any provider via API key"},
        },
        "endpoint_reachable": fcc_up,
        "aethron_tools": (ROOT / "forge_mcp.py").exists(),
    }


def _port_open(base_url: str) -> bool:
    import socket
    import urllib.parse
    try:
        u = urllib.parse.urlparse(base_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except Exception:
        return False


# ─────────────────────── the session ─────────────────────────────────

class CodeSession:
    """One coding conversation over one workspace.

    Backend-agnostic on purpose: `events()` yields the same normalized
    dicts whether the work is done by the Claude Code CLI, by our own
    loop, or by whatever comes next. The IDE is written against these
    events, so the platform outlives any single agent."""

    def __init__(self, workspace, cfg=None, home=None, on_event=None):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"no such workspace: {self.workspace}")
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(load_config(home))
        self.cfg.update(cfg or {})
        self.home = Path(home) if home else None
        self.on_event = on_event or (lambda e: None)
        self.id = str(uuid.uuid4())
        self.session_id = ""          # the CLI's own id, for --resume
        self.proc = None
        self.q = queue.Queue()
        self._events = []
        self._tmp = None
        self.busy = False
        self.cost_usd = 0.0
        self.error = ""

    # ---- lifecycle ---------------------------------------------------
    def start(self):
        if self.cfg["runtime"] != "claude-code":
            raise ValueError("only the claude-code runtime is a live "
                             "process; use aethron_agent for 'internal'")
        binary = find_claude()
        if not binary:
            raise RuntimeError("the `claude` CLI is not installed — "
                               "install it, or switch the runtime to "
                               "'internal' (any provider, API key)")
        argv = [binary, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--session-id", self.id,
                "--permission-mode", self.cfg["permission_mode"]]
        prov = resolve_provider(self.cfg)
        # MODEL OWNERSHIP: when the bridge is in the path it rewrites the
        # model on every request, so the CLI must NOT be given a foreign
        # model id — it validates the name and refuses to start
        # ("deepseek-v4-pro … may not exist"). Anywhere else the name
        # belongs to the endpoint we are talking to.
        if prov.get("mode") != "bridge":
            model = self.cfg.get("model") or prov.get("model") or ""
            if model:
                argv += ["--model", model]
        if self.cfg.get("allowed_tools"):
            argv += ["--allowedTools", *self.cfg["allowed_tools"]]
        if self.cfg.get("disallowed_tools"):
            argv += ["--disallowedTools", *self.cfg["disallowed_tools"]]
        if self.cfg.get("max_budget_usd"):
            argv += ["--max-budget-usd", str(self.cfg["max_budget_usd"])]
        extra = self.cfg.get("append_system", "")
        if extra is None:
            extra = ""
        elif not extra and (self.workspace / "forge.json").exists():
            extra = PROJECT_RULES          # a template project: teach it
        if extra:
            argv += ["--append-system-prompt", extra]
        mcp = self._mcp_config()
        if mcp:
            argv += ["--mcp-config", mcp]
            if self.cfg.get("isolate"):
                # the user's personal MCP servers are not part of the
                # product's contract — keep the session reproducible
                argv.append("--strict-mcp-config")
        env = self._env()
        self.proc = subprocess.Popen(
            argv, cwd=str(self.workspace), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        return self

    def _env(self) -> dict:
        env = dict(os.environ)
        prov = resolve_provider(self.cfg)
        if prov["base_url"]:
            # THE INTEGRATION POINT: any Anthropic-compatible endpoint —
            # a local Free-Claude-Code proxy, a gateway, our own later.
            env["ANTHROPIC_BASE_URL"] = prov["base_url"]
            if prov["token"]:
                env["ANTHROPIC_AUTH_TOKEN"] = prov["token"]
        env["CLAUDE_CODE_NONINTERACTIVE"] = "1"
        env.setdefault("AETHRON_HOME", str(self.home or ROOT))
        return env

    def _mcp_config(self) -> str:
        """Hand the coding agent Aethron's OWN tools. This is what keeps
        the physics: even a general coding agent edits template content
        through the guarded pipeline (byte budgets, snapshots, verify)
        instead of hand-editing generated files."""
        if not self.cfg.get("aethron_tools") or not FORGE_MCP.exists():
            return ""
        self._tmp = tempfile.mkdtemp(prefix="aethron-code-")
        f = Path(self._tmp) / "mcp.json"
        f.write_text(json.dumps({"mcpServers": {"aethron": {
            "command": sys.executable, "args": [str(FORGE_MCP)],
            "env": {"AETHRON_HOME": str(self.home or ROOT)}}}}),
            encoding="utf-8")
        return str(f)

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def interrupt(self):
        """Stop the current turn. The session dies with it — restart is
        cheap and honest, and beats pretending we cancelled cleanly."""
        self.close()
        self._emit({"type": "status", "text": "interrupted"})

    @property
    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    # ---- talking -----------------------------------------------------
    def send(self, text: str):
        if not self.alive:
            raise RuntimeError("session is not running")
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.busy = True
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def events(self, timeout=None):
        """Blocking iterator of normalized events (for CLI use)."""
        while True:
            try:
                ev = self.q.get(timeout=timeout)
            except queue.Empty:
                return
            yield ev
            if ev["type"] in ("done", "exit", "error"):
                if ev["type"] != "done":
                    return
                if not self.alive:
                    return

    def drain(self, since=0):
        """Everything since index `since` — how the web UI polls."""
        return self._events[since:], len(self._events)

    # ---- the pipe ----------------------------------------------------
    def _emit(self, ev):
        ev.setdefault("t", time.time())
        self._events.append(ev)
        self.q.put(ev)
        try:
            self.on_event(ev)
        except Exception:
            pass

    def _pump_stderr(self):
        for line in self.proc.stderr:
            line = line.strip()
            if line:
                self._emit({"type": "log", "text": line[:500]})

    def _pump_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                self._emit({"type": "log", "text": line[:500]})
                continue
            for ev in self._normalize(d):
                self._emit(ev)
        code = self.proc.wait()
        self._emit({"type": "exit", "code": code})

    def _normalize(self, d) -> list:
        """CLI wire format -> Aethron events. One place to absorb any
        future protocol change; the UI never sees the difference."""
        t = d.get("type")
        out = []
        if t == "system" and d.get("subtype") == "init":
            self.session_id = d.get("session_id", "")
            model = d.get("model", "")
            prov = resolve_provider(self.cfg)
            if prov.get("mode") == "bridge":
                # the CLI reports its own default name; the request is
                # actually answered by the configured provider, and the
                # UI must say what is really running
                model = f"{prov.get('model') or 'configured model'} " \
                        f"(via Aethron bridge)"
            out.append({"type": "ready", "session_id": self.session_id,
                        "model": model,
                        "tools": d.get("tools", []),
                        "mcp": [m.get("name") for m in
                                d.get("mcp_servers", [])],
                        "cwd": d.get("cwd", "")})
        elif t == "assistant":
            for c in d.get("message", {}).get("content", []) or []:
                k = c.get("type")
                if k == "text" and c.get("text"):
                    out.append({"type": "text", "text": c["text"]})
                elif k == "thinking" and c.get("thinking"):
                    out.append({"type": "thinking", "text": c["thinking"]})
                elif k == "tool_use":
                    out.append({"type": "tool", "id": c.get("id", ""),
                                "name": c.get("name", ""),
                                "input": c.get("input", {})})
        elif t == "user":
            for c in d.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    if isinstance(body, list):
                        body = " ".join(x.get("text", "") for x in body
                                        if isinstance(x, dict))
                    out.append({"type": "tool_result",
                                "id": c.get("tool_use_id", ""),
                                "ok": not c.get("is_error"),
                                "text": str(body or "")[:4000]})
        elif t == "result":
            self.busy = False
            self.cost_usd = d.get("total_cost_usd") or self.cost_usd
            err = bool(d.get("is_error"))
            text = d.get("result") or ""
            if err and not text:
                text = d.get("subtype", "error")
            self.error = text if err else ""
            out.append({"type": "done", "error": err, "text": text,
                        "cost_usd": d.get("total_cost_usd", 0),
                        "turns": d.get("num_turns", 0),
                        "session_id": d.get("session_id", "")})
        elif t == "stream_event":
            pass          # partial deltas: only with --include-partial-messages
        return out


# ─────────────────────── one-shot helper ─────────────────────────────

def run_once(workspace, prompt, cfg=None, home=None, timeout=900,
             on_event=None) -> dict:
    """Start a session, ask one thing, wait for the turn to finish."""
    s = CodeSession(workspace, cfg, home, on_event)
    s.start()
    s.send(prompt)
    deadline = time.time() + timeout
    text, tools, err = [], [], ""
    for ev in s.events(timeout=max(1, deadline - time.time())):
        if ev["type"] == "text":
            text.append(ev["text"])
        elif ev["type"] == "tool":
            tools.append(ev["name"])
        elif ev["type"] == "done":
            err = ev["text"] if ev["error"] else ""
            break
        elif ev["type"] == "exit":
            err = err or f"agent exited ({ev['code']}) without answering"
            break
        if time.time() > deadline:
            err = "timed out"
            break
    events = list(s._events)
    s.close()
    return {"ok": not err, "error": err, "text": "\n".join(text),
            "tools": tools, "cost_usd": s.cost_usd, "events": events,
            "session_id": s.session_id}


# ─────────────────────── mock provider (tests) ───────────────────────
# Proves the ENTIRE pipe — spawn, env routing, streaming, tool calls,
# file writes — with no API key, no login and no cost. It is also the
# reference for what "Anthropic-compatible" means for a gateway: if a
# provider speaks this, Aethron can drive Claude Code against it.

def mock_provider(port=0, script=None):
    """A minimal Anthropic-compatible /v1/messages endpoint. Returns
    (server, port); serve in a thread. `script` is a list of turns, each
    a list of content blocks: {"text": ...} or
    {"tool": name, "input": {...}}."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    turns = list(script or [[{"text": "ok"}]])
    state = {"n": 0}

    def pick(body: dict):
        """Which scripted turn answers THIS request?

        Content-driven, not a counter: the CLI also makes auxiliary
        calls (conversation titles, summaries) that carry no tools, and
        a counter silently desynchronises the script against them —
        which is exactly how the first run of this test 'passed' the
        wrong turn."""
        if not body.get("tools"):
            return [{"text": "ok"}]          # auxiliary call, not the loop
        seen = 0
        for m in body.get("messages", []):
            content = m.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        seen += 1
        return turns[min(seen, len(turns) - 1)]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _sse(self, chunks):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for ev, data in chunks:
                self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n"
                                 .encode())
                self.wfile.flush()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            state["n"] += 1
            blocks = pick(body)
            stop = "tool_use" if any("tool" in b for b in blocks) else "end_turn"
            chunks = [("message_start", {
                "type": "message_start",
                "message": {"id": "msg_mock", "type": "message",
                            "role": "assistant", "model": "mock-1",
                            "content": [], "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 1,
                                      "output_tokens": 1}}})]
            for idx, b in enumerate(blocks):
                if "tool" in b:
                    chunks += [
                        ("content_block_start", {
                            "type": "content_block_start", "index": idx,
                            "content_block": {"type": "tool_use",
                                              "id": f"toolu_{idx}",
                                              "name": b["tool"],
                                              "input": {}}}),
                        ("content_block_delta", {
                            "type": "content_block_delta", "index": idx,
                            "delta": {"type": "input_json_delta",
                                      "partial_json": json.dumps(
                                          b.get("input", {}))}}),
                        ("content_block_stop", {
                            "type": "content_block_stop", "index": idx})]
                else:
                    chunks += [
                        ("content_block_start", {
                            "type": "content_block_start", "index": idx,
                            "content_block": {"type": "text", "text": ""}}),
                        ("content_block_delta", {
                            "type": "content_block_delta", "index": idx,
                            "delta": {"type": "text_delta",
                                      "text": b.get("text", "")}}),
                        ("content_block_stop", {
                            "type": "content_block_stop", "index": idx})]
            chunks += [
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": stop,
                                             "stop_sequence": None},
                                   "usage": {"output_tokens": 1}}),
                ("message_stop", {"type": "message_stop"})]
            self._sse(chunks)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv, srv.server_address[1]


def selftest() -> int:
    """End-to-end proof: Aethron starts Claude Code, routes it to an
    endpoint of OUR choosing (the Free-Claude-Code pattern), and the
    agent edits a file inside the workspace — no Anthropic login."""
    import threading as th
    if not find_claude():
        print("SKIPPED — the `claude` CLI is not installed (this test "
              "proves the CLI integration, so it cannot run without it)")
        return 0
    ws = Path(tempfile.mkdtemp(prefix="aethron-code-selftest-"))
    (ws / "hello.txt").write_text("before\n", encoding="utf-8")
    srv, port = mock_provider(script=[
        # Claude Code refuses to overwrite a file it has not read — the
        # script follows the real tool contract, not a convenient one.
        [{"text": "Reading the file first."},
         {"tool": "Read", "input": {"file_path": str(ws / "hello.txt")}}],
        [{"text": "Writing the file."},
         {"tool": "Write", "input": {"file_path": str(ws / "hello.txt"),
                                     "content": "after\n"}}],
        [{"text": "Done — hello.txt now says after."}],
    ])
    th.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"mock provider on http://127.0.0.1:{port}  workspace {ws}")
    cfg = {"provider": "custom", "base_url": f"http://127.0.0.1:{port}",
           "token": "mock", "permission_mode": "bypassPermissions",
           "aethron_tools": False, "isolate": True, "model": "mock-1"}
    res = run_once(ws, "change hello.txt to say after", cfg=cfg, timeout=120)
    srv.shutdown()
    got = (ws / "hello.txt").read_text(encoding="utf-8").strip()
    checks = [
        ("session started", any(e["type"] == "ready" for e in res["events"])),
        ("routed to our endpoint (no Anthropic login)", res["ok"] or
         "Not logged in" not in (res["error"] or "")),
        ("model text streamed", "Writing the file." in res["text"]
         or bool(res["text"])),
        ("tool call surfaced", "Write" in res["tools"]),
        ("file actually changed in the workspace", got == "after"),
    ]
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
    if not all(ok for _, ok in checks):
        print("\nerror:", res["error"])
        print("events:", json.dumps(res["events"], indent=1)[:2500])
    shutil.rmtree(ws, ignore_errors=True)

    # SCENARIO 2 — the fusion: the coding agent drives Aethron's OWN
    # guarded pipeline through our MCP server, and sees nothing else.
    ws2 = Path(tempfile.mkdtemp(prefix="aethron-code-mcp-"))
    srv2, port2 = mock_provider(script=[
        [{"text": "Listing Aethron projects."},
         {"tool": "mcp__aethron__list_projects", "input": {}}],
        [{"text": "Listed them."}],
    ])
    th.Thread(target=srv2.serve_forever, daemon=True).start()
    res2 = run_once(ws2, "list the aethron projects", cfg={
        "provider": "custom", "base_url": f"http://127.0.0.1:{port2}",
        "token": "mock", "permission_mode": "bypassPermissions",
        "aethron_tools": True, "isolate": True, "model": "mock-1"},
        home=ROOT, timeout=240)
    srv2.shutdown()
    ready = next((e for e in res2["events"] if e["type"] == "ready"), {})
    tool_ok = next((e for e in res2["events"]
                    if e["type"] == "tool_result"), {})
    checks += [
        ("aethron MCP tools injected",
         "aethron" in (ready.get("mcp") or [])),
        ("only our tools (user's own MCP servers isolated)",
         (ready.get("mcp") or []) == ["aethron"]),
        ("agent called the guarded pipeline",
         "mcp__aethron__list_projects" in res2["tools"]),
        ("pipeline answered with real data", bool(tool_ok.get("ok"))),
    ]
    for name, ok in checks[5:]:
        print(("  ok   " if ok else "  FAIL ") + name)
    shutil.rmtree(ws2, ignore_errors=True)

    # SCENARIO 3 — ONE KEY FOR EVERYTHING: an OpenAI-wire provider
    # (DeepSeek/Gemini/OpenAI/Ollama shaped) drives the CODING agent,
    # translated by Aethron's own bridge. No Anthropic account anywhere
    # in this path.
    import aethron_bridge
    ws3 = Path(tempfile.mkdtemp(prefix="aethron-code-bridge-"))
    up, up_url = aethron_bridge._mock_openai([
        [{"text": "Creating the file."},
         {"tool": "Write", "input": {"file_path": str(ws3 / "made.txt"),
                                     "content": "by a non-anthropic model\n"}}],
        [{"text": "Done."}]])
    bsrv, burl = aethron_bridge.start(
        aethron_bridge.BridgeConfig(up_url, "sk-fake-deepseek-key",
                                    "deepseek-v4-pro"))
    res3 = run_once(ws3, "create made.txt", cfg={
        "provider": "custom", "base_url": burl, "token": "aethron",
        "mode": "bridge",   # the bridge owns the model name upstream
        "permission_mode": "bypassPermissions",
        "aethron_tools": False, "isolate": True}, timeout=180)
    bsrv.shutdown()
    up.shutdown()
    made = (ws3 / "made.txt")
    checks += [
        ("OpenAI-wire provider drives the coding agent (via the bridge)",
         "Write" in res3["tools"]),
        ("its tool call really wrote the file",
         made.exists() and "non-anthropic" in made.read_text()),
        ("no Anthropic account involved anywhere in that path",
         "Not logged in" not in (res3["error"] or "")),
    ]
    for name, ok in checks[9:]:
        print(("  ok   " if ok else "  FAIL ") + name)
    if not all(ok for _, ok in checks[9:]):
        print("  error:", res3["error"])
        print("  events:", json.dumps(res3["events"], indent=1)[:1500])
    shutil.rmtree(ws3, ignore_errors=True)

    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


# ─────────────────────── cli ─────────────────────────────────────────

def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--status":
        print(json.dumps(status(), indent=1))
        return 0
    if args[0] == "--selftest":
        return selftest()
    ws = Path(args[0]).expanduser()
    prompt = " ".join(args[1:])
    if not prompt:
        print("usage: aethron_code.py <workspace> \"prompt\"")
        return 1

    def show(ev):
        k = ev["type"]
        if k == "ready":
            print(f"[session {ev['session_id'][:8]}  model {ev['model']}  "
                  f"tools {len(ev['tools'])}  mcp {ev['mcp']}]")
        elif k == "text":
            print(ev["text"])
        elif k == "tool":
            print(f"  → {ev['name']}({json.dumps(ev['input'])[:100]})")
        elif k == "tool_result":
            print(f"    {'ok' if ev['ok'] else 'ERROR'}: "
                  f"{ev['text'][:150].splitlines()[0] if ev['text'] else ''}")
        elif k == "done" and ev["error"]:
            print("ERROR:", ev["text"])

    res = run_once(ws, prompt, on_event=show)
    if res["cost_usd"]:
        print(f"\n(${res['cost_usd']:.4f})")
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

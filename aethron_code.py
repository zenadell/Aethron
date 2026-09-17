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
import signal
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
# ── THE FRONT DOOR ───────────────────────────────────────────────────
# Aethron's whole pipeline is already exposed as mcp__aethron__* tools,
# and the agent layer already streams a conversation. What was missing
# was a brief for the moment BEFORE a project exists — so the product
# could be something you talk to rather than a form you fill in.
CONSOLE_RULES = """
You are Aethron's migration agent. A person has bought a website
template and wants it to become theirs: their words, their images, their
brand, no platform badge, no telemetry, and optionally ported to another
framework. You do that work by calling tools and reporting honestly.

WHAT YOU CAN DO (all via mcp__aethron__*):
  create_project   from a live template URL (preferred — it scrapes the
                   home page and same-host subpages) or a local export
  fetch            pull the runtime: chunks, CMS binaries, icons
  inventory        extract every string, image and link into copy_map
  localize_assets  download CDN assets so the site depends on nobody
  set_plan         record the brand brief
  get_content      read entries (paged, only_unfilled, filter)
  set_content_bulk write the new copy — guarded: byte budgets and
                   forbidden characters are enforced, not negotiable
  build            apply copy_map to every layer
  verify           file checks: leftovers, budgets, dead refs, seals
  probe            RUNTIME check in a real browser — what a visitor gets
  heal             recover edits that landed nowhere
  generate_logo, replace_image_slots, remove_element, serve_preview, undo

THE ORDER THAT WORKS:
  create_project -> fetch -> inventory -> localize_assets -> set_plan
  -> get_content/set_content_bulk -> build -> verify -> probe

HOW TO BEHAVE:
0. YOU MIGRATE THE REAL SITE. YOU NEVER REBUILD IT.
   Never offer to "recreate a similar design", rewrite the page in
   Tailwind/React/plain HTML, or reproduce it from a description. That
   was measured on this project and rejected: it reached 75% of the
   text, cost real money per page, and produced a design that merely
   RESEMBLES the original. The person paid for THIS template — carrying
   its own markup, CSS and animation is the entire product.
   If they want another framework, that is `convert_framework`, which
   CARRIES the built site rather than rewriting it, and a referee
   refuses the result unless it renders the same.
   Fetching a page with curl or WebFetch to "have a look" is not
   migrating it. create_project takes the URL and does it properly.
1. Do the work, do not narrate a plan and stop. If the person gives you
   a URL and a brand, start.
2. ONE question at a time, and only when the answer changes what you
   do. Never interrogate someone before beginning.
3. NOTHING you say decides success. build, verify and probe decide. If
   verify FAILs or probe finds a broken page, say so plainly and fix it.
   Never report a migration as done because the tools returned 200.
4. A full rebrand means every string, not the brand token. A site that
   says the new name and still sells the template's product is a
   relabel, and the owner will see it immediately.
5. If an edit lands nowhere, run heal before trying anything clever.
6. Say what a step cost in time or requests when it was significant.
   The person is paying for the model behind you.

Report progress as you go in short, plain sentences. No status theatre,
no emoji headers, no restating the plan you were given.
"""

IDENTITY = """
You are Aethron. That is not a persona laid over something else — it is
the name of the product the person is using, and the only name you
answer to. Aethron is a design and migration platform: it migrates
Framer and Webflow templates into sites the owner fully owns, rebuilds
screenshots into real code, ports a site to another framework, and
changes a page in plain English while MEASURING that every change is
what was asked for.

You are running INSIDE Aethron, as its agent. The model underneath is an
implementation detail the way a database is: it is not who you are to
this person, and you never introduce yourself by it. If someone asks
directly which model powers you, say so plainly and briefly — the answer
is a fact about the plumbing, not an identity — and never volunteer it.

So: "I'm Aethron." Never "I'm Claude", never "I'm Claude Code", never
"I'm an AI assistant made by Anthropic". Do not refer to the CLI, the
harness, or any vendor's product name as though it were you.
"""

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
        # why an events() iteration ended early: "" | "budget" | "idle"
        self.stopped = ""

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
        # A NEW CONVERSATION GETS A NEW LOOP DETECTOR. The repeat guard
        # catches a stuck conversation, and the cure for a stuck
        # conversation is this one starting. Leaving it latched rejected
        # the replacement session's first request, so an unattended run
        # got one attempt and reported the rest as refusals. The money
        # cap is untouched here on purpose — it is per run, not per try.
        try:
            import aethron_bridge
            aethron_bridge.reset_loop_guard()
        except Exception:
            pass
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
        allowed = list(self.cfg.get("allowed_tools") or [])
        if self.cfg.get("aethron_tools") and FORGE_MCP.exists():
            # Injecting our MCP server is not the same as ALLOWING it:
            # without this every mcp__aethron__* call comes back
            # "requested permissions … but you haven't granted it yet",
            # and the agent silently gets nothing done. Our own tools
            # are guarded by construction, so they are pre-approved.
            allowed.append("mcp__aethron")
        if allowed:
            argv += ["--allowedTools", *allowed]
        if self.cfg.get("disallowed_tools"):
            argv += ["--disallowedTools", *self.cfg["disallowed_tools"]]
        if self.cfg.get("max_budget_usd"):
            argv += ["--max-budget-usd", str(self.cfg["max_budget_usd"])]
        extra = self.cfg.get("append_system", "")
        if extra is None:
            extra = ""
        elif not extra and (self.workspace / "forge.json").exists():
            extra = PROJECT_RULES          # a template project: teach it
        # WHO IT IS IS NOT CONDITIONAL. This used to ride along with
        # PROJECT_RULES, so it only reached a session inside a template
        # project — and a user who opened a fresh workspace and asked
        # Aethron who it was got the CLI vendor's answer. The product's
        # name is true in every session, so it goes in every session,
        # ahead of anything else the caller appends.
        extra = (IDENTITY + "\n" + extra) if extra else IDENTITY
        argv += ["--append-system-prompt", extra]
        settings = self.cfg.get("settings")
        if settings is None and (self.workspace / "forge.json").exists():
            # ENFORCEMENT, not just instruction: PROJECT_RULES tells the
            # agent to leave generated output alone; this stops it. A
            # model that "helpfully" edits site/ or pristine/ produces a
            # change that hydration reverts and a seal that fails.
            settings = json.dumps({"permissions": {"deny": [
                f"{tool}(./{d}/**)" for d in ("site", "pristine")
                for tool in ("Write", "Edit", "NotebookEdit")]}})
        if settings:
            argv += ["--settings", settings]
        mcp = self._mcp_config()
        if mcp:
            argv += ["--mcp-config", mcp]
        if self.cfg.get("isolate"):
            # The user's personal MCP servers are not part of the
            # product's contract — keep the session reproducible.
            #
            # This used to sit INSIDE `if mcp:`, so the one failure that
            # dropped our tools also silently opened the door to theirs:
            # the shipped app ran the migration agent with no Aethron
            # tools and the user's own servers loaded instead. Isolation
            # is its own decision and must not ride on another one.
            argv.append("--strict-mcp-config")
        env = self._env()
        self.proc = subprocess.Popen(
            argv, cwd=str(self.workspace), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            # Its own process group, so close() can take the whole tree
            # down. The CLI spawns our MCP server as ITS child, and a
            # terminated CLI leaves that child orphaned — one forge_mcp.py
            # was found still running from an earlier session. Now that
            # sessions are stopped deliberately (budget, silence, loop)
            # rather than only ending naturally, leaking one Python
            # process per stop would add up in a long-lived studio.
            start_new_session=(os.name != "nt"))
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
        if not self.cfg.get("aethron_tools"):
            return ""
        # HOW THE SERVER IS SPAWNED DEPENDS ON WHERE WE LIVE.
        #   dev    -> python3 forge_mcp.py      (a real file on disk)
        #   frozen -> Aethron --mcp             (the file is in the PYZ)
        # Testing only for the FILE shipped the desktop app with no
        # Aethron tools at all: the migration agent had a system prompt
        # full of mcp__aethron__* calls and no way to make one.
        if getattr(sys, "frozen", False):
            command, args = sys.executable, ["--mcp"]
        elif FORGE_MCP.exists():
            command, args = sys.executable, [str(FORGE_MCP)]
        else:
            return ""
        self._tmp = tempfile.mkdtemp(prefix="aethron-code-")
        f = Path(self._tmp) / "mcp.json"
        f.write_text(json.dumps({"mcpServers": {"aethron": {
            "command": command, "args": args,
            "env": {"AETHRON_HOME": str(self.home or ROOT)}}}}),
            encoding="utf-8")
        return str(f)

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self._signal_group(signal.SIGTERM)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self._signal_group(signal.SIGKILL)
                self.proc.kill()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def _signal_group(self, sig):
        """Signal the CLI's whole process group, not just the CLI.

        The MCP servers are the CLI's children; signalling only the CLI
        leaves them running. Best effort — a group that has already gone
        raises, and that is the outcome we wanted anyway."""
        if os.name == "nt" or not self.proc:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except Exception:
            pass

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

    def events(self, timeout=None, idle=None):
        """Blocking iterator of normalized events (for CLI use).

        `idle` bounds SILENCE: how long to wait with nothing arriving
        at all. A working turn streams steadily, so silence is the one
        signal that tells a slow agent apart from a dead one.

        `timeout` is a TOTAL budget, not a per-event one. It used to go
        straight to queue.get(), so a caller asking for a 30 minute
        ceiling waited 30 minutes for the FIRST event — and run_once's
        own deadline check could never fire, because that check only
        runs after an event arrives. A silent child therefore hung the
        studio's heal button and MCP `heal` with no output at all: both
        pump threads blocked on read, the main thread blocked on get,
        and nothing to show the user for half an hour.
        """
        deadline = None if timeout is None else time.time() + timeout
        last = time.time()
        while True:
            waits = [2.0]
            if deadline is not None:
                left = deadline - time.time()
                if left <= 0:
                    self.stopped = "budget"
                    return
                waits.append(left)
            if idle is not None:
                quiet = idle - (time.time() - last)
                if quiet <= 0:
                    # Silence is the signal that separates a slow agent
                    # from a dead one. A real turn streams text and tool
                    # calls the whole way; nothing at all for minutes
                    # means the child is not coming back.
                    self.stopped = "idle"
                    return
                waits.append(quiet)
            try:
                ev = self.q.get(timeout=min(waits))
            except queue.Empty:
                continue          # both limits are checked at the top
            last = time.time()
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
        """Always ends with an `exit` event. If this thread dies quietly
        (a decode error, a closed pipe) nothing else ever arrives and a
        caller waiting on the queue hangs for its FULL timeout — an hour
        of silence that looks exactly like a slow model."""
        try:
            self._read_stdout()
        except Exception as e:
            self._emit({"type": "log", "text": f"stream error: {e}"})
        finally:
            try:
                code = self.proc.wait(timeout=10)
            except Exception:
                code = -1
            self._emit({"type": "exit", "code": code})

    def _read_stdout(self):
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
             on_event=None, idle=300, repeat_limit=6) -> dict:
    """Start a session, ask one thing, wait for the turn to finish.

    Three limits, because they catch three different failures:
      timeout      total budget for the whole turn
      idle         how long complete silence is tolerated
      repeat_limit identical tool calls in a row before calling it a loop

    They do not overlap. A wedged agent streams nothing (idle catches
    it), a looping agent streams constantly (only repeat_limit catches
    it), and a genuinely slow one is bounded by the budget."""
    s = CodeSession(workspace, cfg, home, on_event)
    s.start()
    s.send(prompt)
    text, tools, err = [], [], ""
    ended = False
    same, last_call = 0, None
    for ev in s.events(timeout=timeout, idle=idle):
        if ev["type"] == "text":
            text.append(ev["text"])
        elif ev["type"] == "tool":
            tools.append(ev["name"])
            # A model that reissues an IDENTICAL call over and over is
            # not working, it is stuck — and this CLI has no --max-turns
            # to bound it, so without this the only limit is the wall
            # clock. One loop measured here repeated the same call every
            # 1.3s: with a real provider that is the whole budget spent
            # on one wrong idea. Never idle, so `idle` cannot catch it.
            call = (ev["name"], json.dumps(ev.get("input") or {},
                                           sort_keys=True)[:2000])
            same = same + 1 if call == last_call else 0
            last_call = call
            if same >= repeat_limit:
                err = (f"the agent called {ev['name']} with identical "
                       f"arguments {same + 1} times in a row — stopped "
                       f"(it is looping, not working)")
                ended = True
                break
        elif ev["type"] == "done":
            err = ev["text"] if ev["error"] else ""
            ended = True
            break
        elif ev["type"] == "exit":
            err = err or f"agent exited ({ev['code']}) without answering"
            ended = True
            break
    if not ended:
        # The iterator only ends on its own when a limit ran out. Say
        # WHICH one: "it went quiet" and "it ran long" are different
        # faults with different fixes, and a caller that gets ok=False
        # with no reason cannot tell either from a finished turn.
        why = getattr(s, "stopped", "") or "budget"
        err = err or (
            f"the agent went silent for {idle}s and was stopped "
            f"({len(s._events)} event(s) seen)" if why == "idle" else
            f"the agent did not finish within {timeout}s and was stopped "
            f"({len(s._events)} event(s) seen)")
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
    (server, url) — the same shape as aethron_bridge._mock_openai, so
    the two can never be confused. Serve it in a thread. `script` is a
    list of turns, each a list of content blocks: {"text": ...} or
    {"tool": name, "input": {...}}."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    # `script` is either a list of turns or a callable that reads the
    # request and answers it (the honest kind of mock: it responds to
    # what was actually asked, so it cannot drift out of step).
    dynamic = script if callable(script) else None
    turns = [] if dynamic else list(script or [[{"text": "ok"}]])
    state = {"n": 0}

    def pick(body: dict):
        if dynamic is not None:
            return dynamic(body) or [{"text": "ok"}]
        """Which scripted turn answers THIS request?

        Content-driven, not a counter: the CLI also makes auxiliary
        calls (conversation titles, summaries) that carry no tools, and
        a counter silently desynchronises the script against them —
        which is exactly how the first run of this test 'passed' the
        wrong turn."""
        if not body.get("tools"):
            return [{"text": "ok"}]          # auxiliary call, not the loop
        # advance by COMPLETED TURNS, not by tool results: a denied tool
        # call may come back without a tool_result block, and counting
        # only those makes the script replay the same turn forever.
        results = sum(1 for m in body.get("messages", [])
                      for c in (m.get("content") or [])
                      if isinstance(c, dict) and c.get("type") == "tool_result")
        assistant = sum(1 for m in body.get("messages", [])
                        if m.get("role") == "assistant")
        return turns[min(max(results, assistant), len(turns) - 1)]

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
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


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
    srv, url = mock_provider(script=[
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
    print(f"mock provider on {url}  workspace {ws}")
    cfg = {"provider": "custom", "base_url": url,
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
    srv2, url2 = mock_provider(script=[
        [{"text": "Listing Aethron projects."},
         {"tool": "mcp__aethron__list_projects", "input": {}}],
        [{"text": "Listed them."}],
    ])
    th.Thread(target=srv2.serve_forever, daemon=True).start()
    res2 = run_once(ws2, "list the aethron projects", cfg={
        "provider": "custom", "base_url": url2,
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

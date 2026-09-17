#!/usr/bin/env python3
"""Aethron Agent — the agentic brain, provider-agnostic and hosted.

This is the layer that turns Aethron from a template editor into a
platform: one loop that can migrate, complete, repair and (later)
design, driven by ANY model, exposed to users through Aethron's own UI
so they never install a CLI, a proxy, or see a line of code.

WHY NOT WRAP A THIRD-PARTY CODING CLI: the capability we need is an
agent loop over OUR tools. Routing someone else's CLI to cheap models
is a billing workaround, not capability — and it is the one component
that carries licensing risk in a product we charge for. The loop below
is ours; the model behind it is a config value (Claude API, DeepSeek
V4, or a local model), and an embeddable MCP-native runtime can be
dropped in later behind the same Agent interface.

THE SAFETY RULE (earned the hard way — a foreign agent once hand-edited
pristine/ and silently corrupted a project):

    THE AGENT PROPOSES.  DETERMINISTIC CHECKS DISPOSE.

* the agent gets NO filesystem access — only registered tools
* every tool validates its own input (byte budgets, forbidden chars)
* every mutation is snapshotted and undoable
* the agent MAY NOT declare success; `verify` decides, and the loop
  keeps working until verify is clean or it honestly reports STUCK
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_ROUNDS = 12          # bounded: an agent that cannot finish must say so
MAX_TOOL_OUT = 6000      # keep tool results inside the context budget


# ───────────────────────── model router ──────────────────────────────
# One uniform call shape over every provider. Adding a provider is a
# dict entry, not a refactor — that is FCC's whole value, in ~60 lines.

PROVIDERS = {
    # name:        (base_url, wire format, default model)
    "deepseek":    ("https://api.deepseek.com/v1", "openai", "deepseek-v4-pro"),
    "deepseek-fast": ("https://api.deepseek.com/v1", "openai", "deepseek-v4-flash"),
    "anthropic":   ("https://api.anthropic.com/v1", "anthropic",
                    "claude-sonnet-5"),
    "openai":      ("https://api.openai.com/v1", "openai", "gpt-5.6"),
    "openrouter":  ("https://openrouter.ai/api/v1", "openai", ""),
    "groq":        ("https://api.groq.com/openai/v1", "openai", ""),
    "ollama":      ("http://127.0.0.1:11434/v1", "openai", "qwen3-coder"),
}


class Model:
    """A chat endpoint that can call tools, whoever provides it."""

    @classmethod
    def from_settings(cls):
        """ONE KEY FOR EVERYTHING: build from Aethron's saved AI settings
        (aethron_brain) so the migration agent, the copy fill and the
        coding agent all run on the same provider."""
        import aethron_brain as brain
        r = brain.resolve()
        if not r["ready"]:
            raise ValueError(r["why"])
        m = cls.__new__(cls)
        # both wires here take a versioned base (…/v1/messages,
        # …/v1/chat/completions); the settings store the human URL
        base = r["base"].rstrip("/")
        if "/v1" not in base and not base.endswith("/openai"):
            base += "/v1"
        m.base = base
        m.wire = "anthropic" if r["wire"] == "anthropic" else "openai"
        m.model = r["model"]
        m.key = r["key"]
        m.provider = r["provider"]
        return m

    def __init__(self, provider="deepseek", model="", api_key="", base=""):
        if provider not in PROVIDERS and not base:
            raise ValueError(f"unknown provider {provider!r}; "
                             f"known: {sorted(PROVIDERS)}")
        b, wire, default = PROVIDERS.get(provider, (base, "openai", model))
        self.base = (base or b).rstrip("/")
        self.wire = wire
        self.model = model or default
        self.key = api_key or os.environ.get(
            f"AETHRON_{provider.upper().replace('-', '_')}_KEY", "") \
            or os.environ.get("AETHRON_MODEL_KEY", "")
        self.provider = provider

    def chat(self, messages, tools, temperature=0.2, timeout=180):
        """-> {'text': str, 'calls': [{'id','name','args'}]}"""
        if self.wire == "anthropic":
            return self._anthropic(messages, tools, temperature, timeout)
        return self._openai(messages, tools, temperature, timeout)

    # -- OpenAI-compatible (DeepSeek, OpenAI, OpenRouter, Groq, Ollama) --
    def _openai(self, messages, tools, temperature, timeout):
        body = {"model": self.model, "messages": messages,
                "temperature": temperature}
        if tools:
            body["tools"] = [{"type": "function", "function": t.schema()}
                             for t in tools]
            body["tool_choice"] = "auto"
        data = self._post("/chat/completions", body, timeout)
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"id": c.get("id", ""), "name": fn.get("name", ""),
                          "args": args})
        return {"text": msg.get("content") or "", "calls": calls,
                "raw": msg}

    # -- Anthropic wire ------------------------------------------------
    def _anthropic(self, messages, tools, temperature, timeout):
        sys_txt = "".join(m["content"] for m in messages
                          if m.get("role") == "system")
        conv = [m for m in messages if m.get("role") != "system"]
        body = {"model": self.model, "max_tokens": 4096,
                "temperature": temperature, "messages": conv}
        if sys_txt:
            body["system"] = sys_txt
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description,
                              "input_schema": t.params} for t in tools]
        data = self._post("/messages", body, timeout)
        text, calls = "", []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append({"id": block.get("id", ""),
                              "name": block.get("name", ""),
                              "args": block.get("input") or {}})
        return {"text": text, "calls": calls, "raw": data}

    def _post(self, path, body, timeout):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        if self.wire == "anthropic":
            req.add_header("x-api-key", self.key)
            req.add_header("anthropic-version", "2023-06-01")
        else:
            req.add_header("Authorization", "Bearer " + self.key)
        last = None
        for attempt in range(3):            # transient failures are normal
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:300]
                if e.code in (400, 401, 403, 404):
                    raise RuntimeError(f"model error {e.code}: {detail}")
                last = RuntimeError(f"model error {e.code}: {detail}")
            except Exception as e:
                last = e
            time.sleep(1.5 * (attempt + 1))
        raise last or RuntimeError("model unreachable")


# ─────────────────────────── tools ───────────────────────────────────
# A tool is the ONLY way the agent can touch anything. Each one owns its
# own validation, so a hallucinated argument is rejected with a teaching
# error instead of corrupting a project.

class Tool:
    def __init__(self, name, description, params, fn):
        self.name = name
        self.description = description
        self.params = params            # JSON schema
        self.fn = fn

    def schema(self):
        return {"name": self.name, "description": self.description,
                "parameters": self.params}

    def run(self, project: Path, args: dict) -> str:
        try:
            out = self.fn(project, args)
        except Exception as e:
            return f"ERROR {type(e).__name__}: {e}"
        out = out if isinstance(out, str) else json.dumps(out, default=str)
        return out[:MAX_TOOL_OUT]


def _forge(project: Path, *argv, timeout=900) -> str:
    """Run a forge command inside the project. forge is the sole authority
    for every mechanical step — the agent never bypasses it."""
    exe = [sys.executable, str(ROOT / "forge.py"), *map(str, argv)]
    p = subprocess.run(exe, cwd=str(project), capture_output=True,
                       text=True, timeout=timeout)
    return (p.stdout + p.stderr).strip()


def _copy_map(project: Path) -> dict:
    return json.loads((project / "copy_map.json").read_text(encoding="utf-8"))


def _snapshot(project: Path, label="agent"):
    """Same .history format the studio/MCP undo uses — agent edits are
    undoable by the owner exactly like their own."""
    hist = project / ".history"
    hist.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = hist / f"{stamp}-{label}"
    d.mkdir(exist_ok=True)
    for f in ("copy_map.json", "forge.json", "project_plan.md"):
        src = project / f
        if src.exists():
            (d / f).write_bytes(src.read_bytes())
    keep = sorted(hist.iterdir())
    for old in keep[:-30]:
        for x in old.iterdir():
            x.unlink()
        old.rmdir()


# -- tool implementations ---------------------------------------------

def _t_status(project, args):
    cfg = json.loads((project / "forge.json").read_text())
    cm = _copy_map(project)
    def counts(k):
        items = cm.get(k, [])
        return f"{sum(1 for e in items if e.get('new'))}/{len(items)} filled"
    return json.dumps({
        "platform": cfg.get("platform"), "source": cfg.get("source_url"),
        "pages": len(cfg.get("pages", [])),
        "strings": counts("strings"), "images": counts("images"),
        "links": counts("links"),
        "forbidden_words": cfg.get("forbidden_words", []),
        "plan": (project / "project_plan.md").read_text(encoding="utf-8")[:1200]
        if (project / "project_plan.md").exists() else "(no plan written)",
    }, indent=1)


def _t_read_content(project, args):
    kind = args.get("kind", "strings")
    if kind not in ("strings", "images", "links"):
        return "ERROR kind must be strings|images|links"
    only_unfilled = bool(args.get("only_unfilled", True))
    start = int(args.get("offset", 0))
    limit = min(int(args.get("limit", 40)), 80)
    items = _copy_map(project).get(kind, [])
    rows = [(i, e) for i, e in enumerate(items)
            if not (only_unfilled and e.get("new"))]
    out = [{"index": i, "old": e.get("old", "")[:300],
            "max_bytes": e.get("max_bytes"), "new": e.get("new", "")}
           for i, e in rows[start:start + limit]]
    return json.dumps({"total_matching": len(rows), "returned": len(out),
                       "next_offset": start + len(out), "items": out},
                      ensure_ascii=False)


def _t_write_content(project, args):
    """Guarded bulk write. Rejects anything that would break the physics:
    over-budget CMS text, backticks/${ (they live inside JS template
    literals), or a new value that contains another entry's old text."""
    kind = args.get("kind", "strings")
    edits = args.get("edits") or []
    if kind not in ("strings", "images", "links"):
        return "ERROR kind must be strings|images|links"
    if not isinstance(edits, list) or not edits:
        return "ERROR edits must be a non-empty list of {index,new}"
    cm = _copy_map(project)
    items = cm.get(kind, [])
    applied, rejected = 0, []
    for ed in edits:
        try:
            i = int(ed.get("index"))
            new = str(ed.get("new", ""))
        except (TypeError, ValueError):
            rejected.append(f"{ed!r}: index must be an int, new a string")
            continue
        if not (0 <= i < len(items)):
            rejected.append(f"index {i}: out of range (0..{len(items)-1})")
            continue
        if "`" in new or "${" in new:
            rejected.append(f"index {i}: may not contain ` or ${{ "
                            f"(breaks JS template literals)")
            continue
        mb = items[i].get("max_bytes")
        if mb and len(new.encode("utf-8")) > mb:
            rejected.append(f"index {i}: {len(new.encode())}b exceeds the "
                            f"{mb}b CMS budget — shorten it")
            continue
        items[i]["new"] = new
        applied += 1
    if applied:
        _snapshot(project, "agent-write")
        (project / "copy_map.json").write_text(
            json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")
    return json.dumps({"applied": applied, "rejected": rejected[:12],
                       "note": "run build then verify to make it real"})


def _t_build(project, args):
    return _forge(project, "build")[-MAX_TOOL_OUT:]


def _t_verify(project, args):
    out = _forge(project, "verify")
    keep = [ln for ln in out.splitlines()
            if ln.startswith(("FAIL", "NOTE", "VERDICT", "STUCK"))
            or "missing" in ln.lower()]
    return "\n".join(keep or out.splitlines()[-25:])


def _t_heal(project, args):
    return _forge(project, "heal")[-MAX_TOOL_OUT:]


def _t_capture(project, args):
    return _forge(project, "capture")[-MAX_TOOL_OUT:]


def _t_probe(project, args):
    """Runtime truth. `verify` reads files; this loads the built pages in
    a real headless browser and reports what happens."""
    argv = ["probe"]
    if args.get("page"):
        argv.append("--page=" + str(args["page"]))
    if args.get("all"):
        argv.append("--all")
    return _forge(project, *argv)[-MAX_TOOL_OUT:]


TOOLS = [
    Tool("status", "Overview of the project: platform, source, page count, "
         "how much of the copy map is filled, forbidden words, and the "
         "owner's plan. Call this FIRST.",
         {"type": "object", "properties": {}}, _t_status),
    Tool("read_content", "Page through copy-map entries to see what still "
         "needs filling. Returns index, original text, and any CMS byte "
         "budget.",
         {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["strings", "images", "links"]},
             "only_unfilled": {"type": "boolean"},
             "offset": {"type": "integer"}, "limit": {"type": "integer"}},
          "required": ["kind"]}, _t_read_content),
    Tool("write_content", "Set new values by index. Validated: CMS byte "
         "budgets, no backticks or ${. Rejections come back with reasons — "
         "fix and retry rather than repeating the same value.",
         {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["strings", "images", "links"]},
             "edits": {"type": "array", "items": {"type": "object",
                       "properties": {"index": {"type": "integer"},
                                      "new": {"type": "string"}},
                       "required": ["index", "new"]}}},
          "required": ["kind", "edits"]}, _t_write_content),
    Tool("capture", "Download every asset the pages reference (any "
         "platform). Use when verify reports missing assets.",
         {"type": "object", "properties": {}}, _t_capture),
    Tool("build", "Apply the copy map to every layer and regenerate site/.",
         {"type": "object", "properties": {}}, _t_build),
    Tool("verify", "Machine checks on the built site. THIS decides whether "
         "the work is done — not your own judgement.",
         {"type": "object", "properties": {}}, _t_verify),
    Tool("probe", "Load the built pages in a real headless browser and "
         "report what actually happens: blank or hydration-wiped pages, "
         "every request the browser made that failed, console errors. "
         "verify reads files — this runs the code. Use it after verify "
         "is clean, and whenever the owner says the site 'looks broken' "
         "but the files check out.",
         {"type": "object", "properties": {
             "page": {"type": "string",
                      "description": "one page, e.g. index.html"},
             "all": {"type": "boolean",
                     "description": "probe every page (default: 6)"}}},
         _t_probe),
    Tool("heal", "Deterministically repair edits that landed nowhere "
         "(whitespace/casing/typo/image-variant mismatches).",
         {"type": "object", "properties": {}}, _t_heal),
]
BY_NAME = {t.name: t for t in TOOLS}


SYSTEM = """You are Aethron's migration agent. You turn a scraped
template into a site the owner fully owns, and you repair whatever is
broken along the way.

You are Aethron. If you are asked who or what you are, that is the
answer — never a vendor's product name. The model underneath is
plumbing: state it plainly if asked outright, and never volunteer it.

HARD RULES
- You have no filesystem and no shell. The tools are your only actions.
- You may never claim the job is done. `verify` and `probe` decide.
  Keep working until both are clean, or until you can explain precisely
  what blocks you. If `probe` reports SKIPPED, say so — an unrun check
  is not a passed check.
- Never invent facts about the owner's brand. If the plan does not say
  something, keep the original text rather than making it up.
- New text must never contain a backtick or ${ , and must respect
  max_bytes when present (that is a hard byte budget in a binary file).
- Work in batches: read a page of entries, write them, build, verify.

METHOD
1. status  2. fix breakage first (missing assets -> capture -> build)
3. fill copy honestly from the owner's plan  4. build  5. verify
6. if verify still fails, heal, then address what remains.
7. probe — the browser is the last word. Failed runtime requests mean
   assets the CODE asks for (not the markup) are missing: capture,
   rebuild, probe again.
Prefer few, well-chosen edits over many speculative ones."""


def _final_check(project: Path) -> dict:
    """The verdict is never the agent's. Files must check out AND the
    pages must actually run — with an unrunnable runtime check reported
    as unproven, never as success."""
    v = BY_NAME["verify"].run(project, {})
    p = BY_NAME["probe"].run(project, {})
    static_ok = "CLEAN" in v
    runtime_skipped = "SKIPPED" in p
    runtime_ok = "CLEAN at runtime" in p
    return {"ok": static_ok and (runtime_ok or runtime_skipped),
            "verify": v, "probe": p,
            "runtime_verified": runtime_ok,
            "warning": ("runtime UNVERIFIED — no browser available to the "
                        "agent; the site may render blank")
                       if static_ok and runtime_skipped else ""}


class Agent:
    """Bounded observe → act → verify loop. Recovers on its own; when it
    cannot, it reports honestly instead of pretending."""

    def __init__(self, model: Model, tools=None, max_rounds=MAX_ROUNDS,
                 on_event=None):
        self.model = model
        self.tools = tools or TOOLS
        self.max_rounds = max_rounds
        self.on_event = on_event or (lambda kind, data: None)

    def run(self, project: Path, goal: str) -> dict:
        project = Path(project)
        if not (project / "forge.json").exists():
            return {"ok": False, "error": f"not an Aethron project: {project}"}
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": goal}]
        transcript = []
        for rnd in range(1, self.max_rounds + 1):
            self.on_event("round", {"n": rnd})
            try:
                reply = self.model.chat(msgs, self.tools)
            except Exception as e:
                return {"ok": False, "rounds": rnd, "error": str(e),
                        "transcript": transcript}
            if reply["text"]:
                self.on_event("thought", {"text": reply["text"][:500]})
                transcript.append({"round": rnd, "say": reply["text"][:800]})
            if not reply["calls"]:
                # no action proposed — settle it with the checks
                res = _final_check(project)
                res.update(rounds=rnd, transcript=transcript,
                           note="agent stopped acting; verdict from the "
                                "deterministic checks")
                return res
            msgs.append(reply.get("raw") or
                        {"role": "assistant", "content": reply["text"]})
            for call in reply["calls"]:
                tool = BY_NAME.get(call["name"])
                out = (tool.run(project, call["args"]) if tool
                       else f"ERROR no such tool {call['name']!r}")
                self.on_event("tool", {"name": call["name"],
                                       "args": call["args"],
                                       "out": out[:300]})
                transcript.append({"round": rnd, "tool": call["name"],
                                   "out": out[:400]})
                msgs.append({"role": "tool", "tool_call_id": call["id"],
                             "name": call["name"], "content": out}
                            if self.model.wire == "openai" else
                            {"role": "user", "content": [
                                {"type": "tool_result",
                                 "tool_use_id": call["id"], "content": out}]})
                # Files being clean is NOT the finish line — the browser
                # is. Only stop early when both checks agree.
                if call["name"] in ("verify", "probe") and "CLEAN" in out:
                    res = _final_check(project)
                    if res["ok"]:
                        res.update(rounds=rnd, transcript=transcript)
                        return res
        res = _final_check(project)
        res.update(rounds=self.max_rounds, transcript=transcript,
                   note="round limit reached — remaining problems above")
        return res


def main(argv):
    if len(argv) < 2:
        print("usage: aethron_agent.py <project-dir> [goal]\n"
              "  env: AETHRON_MODEL_KEY, AETHRON_PROVIDER (default deepseek), "
              "AETHRON_MODEL")
        return 1
    project = Path(argv[1]).expanduser()
    goal = " ".join(argv[2:]) or ("Complete this migration: fix anything "
                                  "broken, fill the copy from the owner's "
                                  "plan, and get verify CLEAN.")
    try:
        model = Model.from_settings()      # the one configured key
    except Exception:
        model = Model(os.environ.get("AETHRON_PROVIDER", "deepseek"),
                      os.environ.get("AETHRON_MODEL", ""))
    if not model.key:
        print("no model configured — set one in the studio's AI settings "
              "(or AETHRON_AI_KEY / AETHRON_MODEL_KEY)")
        return 1

    def ev(kind, data):
        if kind == "round":
            print(f"\n── round {data['n']} ──")
        elif kind == "thought":
            print(f"  {data['text'][:200]}")
        elif kind == "tool":
            print(f"  → {data['name']}({json.dumps(data['args'])[:80]})")
            print(f"    {data['out'][:200].splitlines()[0] if data['out'] else ''}")

    res = Agent(model, on_event=ev).run(project, goal)
    print("\n" + ("DONE — files clean and the pages run" if res.get("ok")
                  else "NOT CLEAN — honest report:"))
    print(res.get("verify") or res.get("error", ""))
    if res.get("probe"):
        print(res["probe"])
    if res.get("warning"):
        print("WARNING:", res["warning"])
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# Aethron Code — the coding layer

Aethron is not only a template migrator. It is a workspace where an
agent writes code, you edit it, and deterministic checks decide whether
it worked. This document is the contract.

## The shape

```
  provider preset ─┐
                   ├─> ClaudeCodeRuntime   the `claude` CLI, stream-json
  workspace ───────┤
                   └─> InternalRuntime     our own loop (aethron_agent)
                              │
                     normalized events
                              │
                   studio IDE · API · desktop app
```

`aethron_code.py` owns this. Nothing about the UI, the API or the
guardrails knows which runtime is behind the events.

## Why we drive the CLI instead of forking it

A coding agent is a process that reads and writes files in a workspace
and streams what it does. Claude Code already speaks a machine protocol
(`--input-format stream-json --output-format stream-json`), so Aethron
drives it as a subprocess and keeps what actually matters: the
workspace, the tools, the guardrails and the UI. Nothing is vendored,
patched or redistributed — swap the process, keep the platform.

## One key for everything

The rule: **whatever API key you set in Aethron's AI settings powers
everything** — copy fill, plan polish, design matching, self-heal, the
migration agent and the coding agent. Not one key for the design side
and another for code.

`aethron_brain.py` holds that single setting (`aethron_config.json` →
`"ai"`) and the provider registry: DeepSeek, Anthropic, Gemini, OpenAI,
OpenRouter, Groq, Ollama (local, free), the FCC proxy, the Claude Code
login, or a custom endpoint.

The catch, and how it is solved: the Claude Code CLI speaks **only** the
Anthropic Messages API, while most providers speak OpenAI's. So Aethron
ships its own translator, `aethron_bridge.py` — ~500 stdlib lines,
no external proxy:

```
claude CLI ──Anthropic /v1/messages──> BRIDGE ──OpenAI /chat/completions──> DeepSeek
            <────── Anthropic SSE ───────┘                                  Gemini · OpenAI · Groq · Ollama
```

It translates system prompts, multi-block messages, tool definitions,
tool calls and tool results in both directions, streaming (SSE) and
not. `python3 aethron_bridge.py --selftest` proves the translation and
the failure paths without a key.

Which route a session takes is automatic and visible in the UI:

| your provider | route |
|---------------|-------|
| Anthropic / FCC / custom Anthropic endpoint | `direct` |
| Claude Code login | `cli-login` |
| DeepSeek, Gemini, OpenAI, Groq, Ollama, OpenRouter | `bridge` (translated) |

One detail worth knowing: in `bridge` mode Aethron does **not** pass
`--model` to the CLI — the bridge rewrites the model on every request.
Handing the CLI a foreign model id makes it refuse to start with "the
model may not exist", which sends you debugging in the wrong place.

## Provider presets for the coding runtime

Claude Code's provider is an environment variable:

| preset      | what it does                                              |
|-------------|-----------------------------------------------------------|
| `anthropic` | the CLI's own login (your Claude subscription or API key) |
| `fcc`       | `ANTHROPIC_BASE_URL=http://127.0.0.1:8082`, token `freecc` — a local [Free Claude Code](https://github.com/Alishahryar1/free-claude-code) proxy fronting 48+ providers |
| `custom`    | any Anthropic-compatible endpoint (a gateway, a self-host, Aethron's own later) |

That is exactly what `fcc-claude` does. So "free Claude Code inside
Aethron" is a preset, not a dependency: start `fcc-server`, pick the
preset, and the same IDE runs on whatever the proxy is fronting.

Set it in the studio's Code view, or in `aethron_config.json`:

```json
{ "code": { "provider": "fcc", "model": "", "permission_mode": "acceptEdits" } }
```

Environment overrides (`AETHRON_CODE_PROVIDER`, `_BASE_URL`, `_TOKEN`,
`_MODEL`, `_RUNTIME`) win over the file.

## What keeps the physics

A general coding agent's instinct is to edit the file it can see. In a
template project that is the one thing that must never happen — `site/`
is generated and `pristine/` is sealed, and a hand edit either reverts
on hydration or fails the seal. So:

1. **The IDE refuses the write.** `site/` and `pristine/` are read-only
   in the file API and marked with a lock in the tree.
2. **The agent is told, in its system prompt** (`PROJECT_RULES`), the
   moment its workspace contains a `forge.json`.
3. **Aethron's own MCP tools are injected** (`--mcp-config` +
   `--strict-mcp-config`), so content changes go through the guarded
   pipeline — byte budgets, forbidden characters, snapshots, undo — and
   the user's personal MCP servers stay out of the product's contract.

The rule is unchanged from every other layer: **the agent proposes,
deterministic checks dispose.** `verify` reads the files, `probe` runs
the pages, and neither is the agent's to declare.

## Using it

```bash
python3 aethron_code.py --status                  # what this machine can run
python3 aethron_code.py ~/path/to/workspace "add a contact form"
python3 aethron_code.py --selftest                # proves the pipe, no key
```

In the studio: sidebar → **Code**. Pick a workspace (any Aethron
project, or a fresh one under `workspaces/`), choose a provider, start
a session. File tree on the left, editor in the middle, agent on the
right. Every file the agent touches shows up in the tree.

## The selftest (why it can be trusted without an API key)

`--selftest` starts a **mock Anthropic-compatible endpoint** in-process
and points the CLI at it. It proves, offline and for free:

- Aethron spawns the CLI and routes it to an endpoint of our choosing
  (no Anthropic login involved — the FCC path, exactly)
- text and tool calls stream back as normalized events
- a tool call really changes a file in the workspace
- Aethron's MCP tools are injected and callable, and only ours are

It doubles as the reference for what "Anthropic-compatible" has to
mean: if a gateway speaks this, Aethron can drive Claude Code against
it.

## Self-healing that thinks (`aethron_healer.py`)

`forge.py heal` is a ladder of KNOWN fixes — whitespace flexibility,
source casing, nearest-source adoption, image srcset variants,
destructive hide rules. It is free, certain and never guesses, but it
can only repair failures somebody anticipated. When it runs out, it
says STUCK.

The healer takes it from there:

```
1. deterministic heal          cheap, certain, no tokens   ← always first
2. build → verify → probe      what is ACTUALLY still broken
3. agent round(s)              reads the machine evidence, uses the
                               guarded tools, tries what the ladder
                               cannot express
4. build → verify → probe      ← the agent does not get to say it worked
5. honest STUCK report         with everything that was tried
```

The evidence handed to the model is not a vibe: `verify` FAIL lines,
`probe` runtime failures, the build report's dead entries and
`__at_risk__` list (edits the browser will revert), plus what the
deterministic pass already tried.

What the agent cannot do, **enforced rather than requested**:

- writes to `site/` and `pristine/` are DENIED by the runtime (a
  `permissions.deny` rule on the session, verified in the battery — the
  agent tries the hand edit, the file is untouched)
- content changes go through the `mcp__aethron__*` tools, so byte
  budgets and forbidden characters still hold
- a snapshot is taken before the agent starts: one Undo reverts
  everything it did
- success is decided by verify + probe, never by the model

```bash
python3 aethron_healer.py <project> [--rounds 2] [--no-agent]
```

In the studio the dead-edits chip offers the deterministic pass first
and only escalates when it is stuck; the run streams into the Logs tab.

## Event contract

| event         | fields                                   |
|---------------|------------------------------------------|
| `ready`       | `session_id`, `model`, `tools`, `mcp`, `cwd` |
| `text`        | `text`                                   |
| `thinking`    | `text`                                   |
| `tool`        | `id`, `name`, `input`                    |
| `tool_result` | `id`, `ok`, `text`                       |
| `done`        | `error`, `text`, `cost_usd`, `turns`     |
| `log`, `exit` | `text` / `code`                          |

Anything added later — a different CLI, an embedded runtime, a hosted
agent — normalizes into these, and the IDE does not change.

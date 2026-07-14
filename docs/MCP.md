# Driving Aethron from an AI agent (MCP)

Aethron ships an MCP server (`forge_mcp.py`) so any MCP-speaking agent — Claude Code,
Antigravity, Cursor, or your own — can run migrations natively. **The agent is the
copy model:** it reads the entries and writes the fills itself, no API keys, and the
guardrails enforce the physics on every call.

Proven in the wild: Gemini (via Antigravity) drove a complete Framer → rebrand
migration through these tools with zero human intervention, and verify came back
clean.

## Register it

```jsonc
// .mcp.json
{ "mcpServers": { "template-forge": {
    "command": "python3",
    "args": ["forge_mcp.py"]          // use an absolute path for other clients
} } }
```

Or with the Claude Code CLI:
```bash
claude mcp add aethron -- python3 /absolute/path/to/forge_mcp.py
```

Zero dependencies — it's stdio JSON-RPC 2.0, newline-delimited.

## The typical migration

```
create_project(url or source_path)
  → fetch                       # framer downloads the runtime; webflow is instant
  → inventory                   # builds copy_map.json
  → set_plan(plan)
  → get_content(only_unfilled)  # read what needs filling (paged)
  → set_content_bulk(entries)   # write your fills — guarded, snapshotted, auto-builds
  → build
  → verify                      # machine checks
```

If verify or the build report flags problems:

```
  → heal                        # deterministic fixes; then build again
```

## The tools (23)

**Project lifecycle:** `list_projects`, `create_project` (from URL or file),
`delete_project`, `fetch`, `inventory`.

**Plan & content:** `get_plan`, `set_plan`, `get_content` (paged, `only_unfilled`,
`filter`), `set_content`, `set_content_bulk`.

**Visual/structural:** `add_style`, `replace_image_slots` (different images across a
shared slot), `generate_logo` (wordmark in the template's own font).

**Build & ship:** `build`, `verify`, `heal`, `localize_assets`, `generate_backend`,
`serve_preview`.

**Library & safety:** `list_library`, `save_to_library`, `create_from_library`,
`undo`.

## The rules an agent must follow

These are enforced, but knowing them makes you fast:

1. **Never hand-edit `pristine/`, `site/`, or the pipeline code.** Every change goes
   through `copy_map.json` via `set_content*`. The seal will catch tampering.
2. **Respect byte budgets.** `get_content` shows `max_bytes` where it matters; a
   write over budget is rejected with the exact overshoot.
3. **No backticks or `${`** in any value — they break the JS literals strings land in.
4. **When an edit fails, follow the runbook** (this is in `AGENT_GUIDE.md`, generated
   into every project):
   - Read `site/.forge-report.json`. A count of `0` = replaced nothing;
     `__at_risk__` = hydration will revert it.
   - Run `heal`, then `build`, then re-check the report.
   - Still stuck? The reason line tells you why (target not in source, or over
     budget). Fix the **entry** — never work around the pipeline.
5. **Shared assets:** if one image URL fills many slots and you need them different,
   use `replace_image_slots`, not `set_content`.
6. **Brand logos are usually images.** Text replacement can't touch them —
   `generate_logo` renders a wordmark in the template's own font. (`verify` prints a
   note when no images were replaced, as a reminder.)

See [ARCHITECTURE.md](ARCHITECTURE.md) for *why* each rule exists.

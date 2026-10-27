# Aethron Admin

The team's control room for Aethron: see everyone and everything, and switch any part of
Aethron on or off, for one person or for all of them.

- **Console:** `https://aethron.jomiez.com/admin` (built into the website from
  `admin_ui/console.html`)
- **Server:** one Supabase edge function, `aethron` (`supabase/functions/aethron`)
- **Database:** `supabase_schema.sql` + `supabase/admin_schema.sql`
- **In the app:** `aethron_control.py`, which every process of the app obeys (the studio,
  the agent's bridge, and the MCP server outside agents use)

## What you can do

### See
| | |
|---|---|
| **Overview** | People (total, new today, this week), who is online now, active in 24h, countries, versions in use, AI spend (today, 7 and 30 days), errors in 24h with the top ones, agent runs, the state of every switch. |
| **Live now** | Every open copy of Aethron (checked in within 2.5 minutes): who, country and region, version, system, and what it is doing right now — the job and project, whether the agent is working and in which project, how many projects it holds. |
| **Users** | Every account: email, country, plan, version, joined, last seen, online, 30-day AI spend, holds. Search by email, country or plan. |
| **One person** | Account (joined, last sign-in, sign-in method), plan, every copy they use (country, region, version, what it's doing, when last seen), their AI spend and every request (model, purpose, tokens, cost), their activity, the errors their copies reported, their agent runs, your private note. |
| **Usage & spend** | Managed-AI spend and requests by person, model, day and purpose, for any window. |
| **Errors** | Every failure any copy met, grouped by fingerprint (one fault = one line however many people hit it): how often, how many copies, which versions, which template. Open one to see the failing lines, the log tail and the trace. |
| **Activity** | The event stream (sign-ins, projects, builds, migrations, agent turns), filterable by person and event. |
| **Agent runs** | What agents driving Aethron over MCP did, call by call. |
| **Audit log** | Every admin action: who did it (a person, an API key or the admin AI), what, to whom, when. |

Location is the **country and region only**. A copy's IP address is hashed with a secret and
thrown away; it is never stored.

### Control
| | |
|---|---|
| **Kill switch** | Pause everything: no messages, no agent turns, no AI answers, no MCP tools, for every copy. A running agent stops at its next model call. People see your message. |
| **Banner** | An announcement at the top of every copy (info, warning or critical). |
| **Parts on/off** | The coding agent, MCP for outside agents (Claude Code, Cursor…), migrating/building, screenshot to code, publishing, new sign-ups. |
| **Minimum version** | Copies older than this must update before they work. |
| **Who powers the AI** | *People's own keys* (today), **Aethron's AI** (every copy runs on your key and model; people's keys are not used), or *off*. Switch the provider and model at any time. |
| **Provider keys** | Store Aethron's own keys (Gemini, OpenAI, DeepSeek, Anthropic, OpenRouter, Groq, xAI, Mistral, or any OpenAI-compatible address). Several per provider; a busy one gives way to the next. Shown only as fingerprints, never again. |
| **Spending limits** | A daily AI allowance per person, a daily limit for everyone together, and per-person overrides. Refusals say why and when it resets. |
| **People** | Put someone on hold (with a reason they see), lift it, sign them out of every copy, cut off one copy, set their allowance, set who powers their AI, change their plan, send them a message in their app, keep a private note, delete the account (you type their email to confirm). |
| **Admins** | Add people to the admin as *owner*, *admin* (read and change) or *viewer* (read only). |

### The admin AI
A separate AI with its own provider, model and key, set in **AI & models** (it is not the AI
that powers users). It reads and acts through exactly the same tools you click, with **your**
permissions, and everything it changes is in the audit log as "via ai". It is told never to
pause Aethron, delete anyone or change keys unless you asked in so many words.

Ask it things like *"what broke today and for whom?"*, *"who used the most AI this week?"*, or
*"switch everyone to managed gemini-3.6-flash with a $0.30 daily allowance"*.

### Exports
Everything, errors (with logs), usage, users, activity, agent runs or the audit log, as
Markdown (ready to hand to a person or a coding agent), JSON or CSV.

### API keys and MCP, for you and your coding agents
Create a key in **API keys & MCP** with *read*, *read + write* or *owner* permissions. It is
shown once. With it, any agent can:

- connect to the **admin MCP server** — `https://<project>.supabase.co/functions/v1/aethron/mcp`
  with the header `Authorization: Bearer ak_…`. It gets every admin tool its key allows, plus
  `ask_admin_ai`.
  Claude Code: `claude mcp add --transport http aethron-admin <url> --header "Authorization: Bearer ak_…"`
- call the REST API — `POST …/aethron/admin/call` with `{"tool": "...", "args": {...}}`, or
  `GET …/aethron/admin/export?kind=errors&format=md&days=7` to download a report.

## How it works, and the honest limits

- Every copy checks in about once a minute (`POST /ping`) and keeps the answer in
  `control.json` in its data folder. All of the app's processes read that one answer, so the
  studio, a running agent and the MCP server stop together.
- With **Aethron's AI**, the refusal also happens **on the server**: the gateway
  (`/v1/chat/completions`) checks the kill switch, holds, sign-outs, allowances and the global
  limit before your key is used, so nobody can get round it from their own machine.
- With **people's own keys**, stopping a copy is that copy obeying. Someone who cuts their copy
  off from the network keeps the last answer they saw. A copy that has never checked in has no
  answer and is not stopped.
- A sign-out revokes the copy's device token and refuses every login token issued before it,
  so the person must sign in again.
- The admin's provider keys never leave the function. A copy authenticates to the gateway with
  its own device token, which the admin can revoke.

## Set it up (once)

1. **Supabase access token.** At supabase.com → Account → Access tokens, create one. In GitHub →
   Settings → Secrets and variables → Actions, add:
   - `SUPABASE_ACCESS_TOKEN` — that token
   - `AETHRON_ADMIN_EMAILS` — your sign-in email(s), comma-separated (these are owners)
   - `AETHRON_ADMIN_MASTER_KEY` *(optional)* — a long random string (24+ characters) that signs
     in as owner from anywhere
2. **Deploy.** Merging to main deploys it (`.github/workflows/deploy-cloud.yml`: tests, tables,
   function, then a check that it answers). You can also run that workflow by hand from the
   Actions tab.
3. **Let the console sign you in.** In Supabase → Authentication → URL configuration, add
   `https://aethron.jomiez.com/admin` to the redirect URLs (needed for "Email me a link" and
   Google). Password sign-in and the master key work without this.
4. **Open** `https://aethron.jomiez.com/admin`, sign in, then in **AI & models** add a provider
   key and set up the admin AI.

Turning on Aethron's AI for everyone: **AI & models** → *Aethron's AI* → choose the provider and
model → **Apply**. Set the daily allowance first.

## Testing

- `bun test tests/cloud` — the function, end to end, against a stand-in Supabase (29 checks)
- `python3 tests/control_battery.py` — the app obeying every switch, against the real function (35)
- `python3 tests/admin_console_battery.py` — the console in a real browser (22)
- `bun tests/cloud/serve_fake.ts 8787` — try the console by hand at `http://127.0.0.1:8787/`
  with seeded people (master key `master-key-0123456789abcdefghij`)

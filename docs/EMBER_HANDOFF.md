# Ember — handoff (2026-11-05)

For the agent that continues this work. Read AETHRON.md first: the
"Invariants" list is law, and the Ember entries near the end tell the
story. This file is the short version, plus what is still wrong.

## What Ember is
The companion character in the Aethron app. One file holds it:
`aethron_ember.py`.
- `EMBER_JS` is a raw string served at `/ember.js`. It is the whole
  character: drawing, needs, mood, errands, voice, mind client, menu.
- The Python part has `reply()`, `act()` / `act_local()`, `mind()` and
  the behaviour clamps. Run `python3 aethron_ember.py --selftest`.
- studio.py routes:
  - `/api/ember/say` — chat reply, with a 20s deadline;
  - `/api/ember/act` — spoken or typed request turned into an action;
  - `/api/ember/mind` — calls `_ember_mind_call`, which calls the cloud;
  - `/api/ember/signal`, `/api/ember/inbox` — used when Ember is on the
    desktop.
- Cloud mind: `supabase/functions/aethron/ember.ts`, route POST
  `/ember/mind`.
  - It uses the team's own provider key (`key_groq`). The default
    model is in `ember_model`, and it is auto-replaced when the
    provider retires it.
  - It is metered in `usage_ledger` as purpose "ember", and the last
    failure is kept in secret `ember_last_error`.
  - Admin tools: `set_ember_mind`, `ember_mind`.
- Desktop window: `aethron_desktop.py`. Mac voice: `aethron_voice.py`.

## Tests
- `python3 aethron_ember.py --selftest` (31)
- `python3 tests/ember_battery.py` — Playwright, Chromium at
  /opt/pw-browsers/chromium. 81 checks, green twice in a row.
- `bun test tests/cloud` (40)
- JS syntax gate: extract EMBER_JS and run `node --check` on it saved
  as `.cjs`, never `.js`.

## Done this week (all merged, latest PR #118)
- Faces, needs, errands (dust, read, polish, doodle, patrol, juggle,
  stars, workout), a welcome tour, outfits and glasses.
- Real dust: specks are wiped only during the sweep, and the dust
  survives a list redraw.
- Seat and fall: when the thing Ember sits on vanishes or moves, it
  falls, bounces, lies dizzy and recovers.
- Voice:
  - Double-tap starts a hands-free conversation: listen, act or
    answer, speak aloud, listen again.
  - Stop words or Esc end it.
  - Requests are rewritten and then typed into the chat where you can
    watch, and Send is pressed.
  - Spoken app actions (publish, open a project, settings…) press the
    real buttons.
- The right-click menu works now: Ember had been eating the clicks.
- Mind:
  - About every 70–120s, and after real events, the window sends what
    Ember perceives.
  - The model returns a plan, plus up to 2 NEW behaviours as clamped
    keyframe data, never code. They are kept in
    HOME/ember_behaviors.json and localStorage.
- Mind fixes:
  - a key alone turns it on;
  - a named User-Agent is sent (Groq's Cloudflare returned 1010);
  - a retired model (404 model_not_found) is auto-replaced from the
    provider's /models;
  - failures are recorded.
- Boop and throw lines vary by attitude; recently used acts are
  weighted down.

## THE PROBLEM THE OWNER SEES (unsolved)
"It still only cleans and patrols. I can't tell the AI is driving it.
When I tell it to take its glasses off or change something, it doesn't."

These causes are verified in the code. The live behaviour has not been
checked since #118 shipped.

1. Telling Ember something cannot change its behaviour. Typed chat goes
   to `reply()`, which returns only say, face, gesture, mood, name and
   remember. There is no outfit, no glasses, no "stop cleaning", no
   plan. Speech goes to `act()`, which knows app actions only. Neither
   path reaches the mind's plan format.
   **Fix:** let `reply()` (and act's "answer" kind) return the same
   plan fields the mind returns (outfit, glasses, do, go, wait, new
   behaviours, a "stop errands for N min"), and have the window apply
   them with the mind's own plan runner. Then "take your glasses off"
   is obeyed at once, and "stop cleaning" suppresses that errand.
2. The built-in errand picker still runs the show. The mind is asked
   only every 70–120s. Between plans, `choose()` keeps picking errands,
   and dust and patrol have the highest weights. A plan's actions last
   a few seconds, then the errands take over again.
   **Fix:** while the mind is live, make it the planner. Ask it
   whenever Ember goes idle, cheaply and limited by `per_hour`, and
   fall back to the built-in `choose()` only when the mind is off or
   over its cap. Pass the recent errands so it varies them.
3. It is not proven that thoughts succeed in production since #118. To
   check, run the "read-logs" workflow: push to a `claude/**` branch
   that touches `tools/cloud_logs.py`, then read the job log. Check:
   - the "EMBER'S MIND" section shows ember ledger rows today;
   - `ember_model` is set;
   - `ember_last_error` is empty;
   - the Groq models list includes the model in use.
   If the rows are still errors, fix that before anything else.
4. Nothing on screen shows that the mind is thinking. Add a small
   honest cue when a plan arrives, for example a thought bubble "…" for
   a second or a tiny sparkle, and a line in Settings → Companion:
   "Mind: on · last thought 2 min ago · N today". Then the owner can
   see it, rather than take it on trust.

## Next tasks, in order
1. Read the logs (point 3) and confirm thoughts succeed.
2. Chat and voice instructions change behaviour (point 1). Add battery
   checks:
   - "take your glasses off" turns shades off;
   - "wear a beanie" sets the outfit;
   - "stop cleaning" means no dust errand for the stated time.
3. The mind is the default planner when live (point 2). Add a check
   that, with a stub mind, consecutive idle actions come from mind
   plans and not `choose()`.
4. Visible "mind" cue and the status line in Settings (point 4).
5. Prove the microphone on a real Mac: permission prompts, then
   recognition. This was never proven; it was tested only with a
   scripted recogniser.

## Rules that bit us (keep them)
- Ember takes presses in the capture phase. Anything Ember shows (menu,
  reply box) must be excluded via `mine(e)`, or its buttons die.
- Its CSS classes must not collide with studio's: `.empty` once made the
  reply box 540px tall. Prefix names with `ae-` / `ember-`.
- It is off when `navigator.webdriver` is true, unless the page is
  opened with `?ember=1`. Tests use `?ember=1`.
- Never let a model's output become code. New behaviours are clamped
  keyframe data only.
- Merging: squash-merge to main cuts a Mac + Windows release. Never put
  the CI-skip tag text in any commit or merge message (GitHub matches it
  anywhere). Force-push is blocked: merge the branch history instead.
- The owner is on usage limits. No helper agents, run batteries once,
  and keep replies short.

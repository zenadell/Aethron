# Next session: see what every agent does with Aethron

## Status 2026-10-18 (cloud session)
Built and tested — see AETHRON.md "RUN RECORDS + THE PROGREX HERO":
items 1, 2, 4 done; item 3 done for pictures that FAIL TO LOAD (the
reproduced hero loss). Both owner questions answered: the keeper reaches
Aethron on the owner's Mac (terminal-started server), and records upload to
Supabase (docs/CLOUD_SETUP.md §5). To read them here, the cloud
environment needs AETHRON_SUPABASE_URL + SUPABASE_SERVICE_KEY secrets.
Not done: pictures REMOVED without error (count vs template).

Written 2026-10-18 for the cloud session. Read AETHRON.md first.

## The problem, in the owner's words
Other agents now drive Aethron (the Jomiez admin agent, "the keeper",
over MCP; later other people's agents). When something goes wrong,
nobody can see what happened: which agent called which tool, with what,
what Aethron answered, how long it took, and what broke or went missing.

The real case that prompted this: the keeper made a lead preview from the
Progrex template (https://progrex.framer.website, local projects
`tpl-progrex-2` -> preview `crux-climbing-center-central-24318d`) and the
HERO IMAGE was lost. Rebuilt from a fresh URL: same result. The owner
could not tell whether Aethron or the admin agent caused it.

## What to build
1. **A run record for every caller.** Every MCP call (stdio and
   `--http`) is written to a log with: caller (MCP `clientInfo` name from
   `initialize`, plus a label per HTTP token), session id, project,
   tool, arguments, result summary (`structuredContent` when there is
   one), errors, duration, time. One block per caller and session, so
   runs never mix. It must never record a key or token, and it must be
   cheap.
2. **A way to read it.** In the studio: a view listing callers and their
   runs, each run as a timeline with failures marked. For agents: an
   MCP tool or file the next session can read.
3. **Catch what goes MISSING, not only errors.** The hero image raised
   no error. After `export_preview` / `build`, compare against the
   template: images the template's pages render that the built preview
   does not (probe already counts images per page; `probe --against`
   compares two builds). A dropped image must show up in the run
   record as a finding.
4. **A way to test it without the owner.** A scenario runner that plays
   an agent: drives the real MCP tools through a keeper-like flow
   (create from URL, make_preview, fill, add_block, export_preview) on
   public templates, and reports what the run record caught. Start with
   Progrex, and reproduce the hero-image loss before fixing it.

## Things the next session must settle with the owner
- **Where the keeper's Aethron runs.** On the owner's Mac (the app), or
  the hosted `forge_mcp.py --http` (built and container-tested, never
  deployed live). That decides where run records are written.
- **How the cloud session sees records from the owner's Mac.** The cloud
  cannot read the Mac. The likely answer is an opt-in upload of run
  records to the existing Supabase project (a private table or bucket),
  read with a key kept in the cloud environment's secrets. Never in the
  repo. Ask before building it.

## Rules that apply
- Every push to main ships to the owner's Mac. Push only checked work.
  Use `[skip ci]` for notes-only commits.
- The owner wants fewer approval prompts and shorter sessions: work in
  steps, test with the existing batteries (tests/preview_battery.py,
  tests/remote_battery.py), and report results plainly.

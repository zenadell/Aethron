"""Migration jobs: one request in, a finished project out — in the background.

WHY THIS EXISTS. A migration used to be one long agent conversation: the
agent read 40 lines, wrote 40 lines, and repeated that twenty times, every
request carrying the whole run in its memory. A template took one to three
hours, cost dollars, and when the connection dropped or the window closed
half-way the run simply stopped. None of that work needs a conversation.

So the work is split the way a router and its workers are split:
  * the CHAT agent talks to the person and starts a job (start_migration);
  * the JOB is a plain background process that runs the pipeline step by
    step — create, fetch, inventory, brand, plan, fill, build, check — and
    writes every step's result to <home>/jobs/<project>/state.json;
  * the FILL asks the model for 40 lines at a time, several batches at
    once, with a fresh, small prompt each time: minutes, not hours;
  * a WATCHDOG (supervise) restarts a job whose process died, and the
    studio runs it on launch, so a crash, a quit or a restart resumes the
    job from the step it was on. Nothing already done is done twice.

The job never decides success either: the check step is the healer, which
builds, verifies and runs the page in a browser, and the result reported is
measured from disk.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

STEPS = ["source", "fetch", "inventory", "brand", "plan", "fill", "build",
         "share", "check"]
LABEL = {"source": "Reading the site", "fetch": "Downloading its files",
         "inventory": "Finding every line, image and link",
         "brand": "Reading the brand's material", "plan": "Writing the brief",
         "fill": "Writing the new copy", "build": "Building the site",
         "share": "Making the link preview",
         "check": "Checking it in a browser"}
BATCH = max(5, int(os.environ.get("AETHRON_JOB_BATCH", "40")))
PARALLEL = max(1, min(8, int(os.environ.get("AETHRON_JOB_PARALLEL", "6"))))
TRIES = 3                  # per step, inside one runner
RELAUNCHES = 6             # runner processes the watchdog may start
STALE = 120                # seconds without a heartbeat = the runner is gone
HUNG = 1800                # alive, but silent this long = stuck; replace it
CHECK_SECONDS = 900        # the whole check, agent round included, ends here
# A JOB ALWAYS FINISHES. These steps help the site but are not the site:
# when they cannot be done, the job says so and carries on.
OPTIONAL = {"brand", "share", "check"}
TRIES_FOR = {"check": 1}   # a check is long; a second identical one is not
                           # more likely to pass
_JOB_CMD = ([sys.executable, "--job"] if getattr(sys, "frozen", False)
            else [sys.executable, str(ROOT / "aethron_jobs.py"), "run"])


def _home() -> Path:
    return Path(os.environ.get("AETHRON_HOME", ROOT)).resolve()


def projects_dir() -> Path:
    return _home() / "projects"


def _pdir(project: str) -> Path:
    d = (projects_dir() / str(project)).resolve()
    if not d.is_relative_to(projects_dir().resolve()):
        raise ValueError(f"bad project name {project!r}")
    return d


def _jdir(project: str) -> Path:
    """A job's notes live beside the projects, never inside one: a new
    project is created from its address, and the engine refuses to create
    into a folder that already exists."""
    _pdir(project)                       # same name rules as a project
    return _home() / "jobs" / slug(project)


def slug(name: str) -> str:
    import re
    return re.sub(r"[^\w-]+", "-", str(name).strip().lower()).strip("-")


# ─────────────────────────── state on disk ───────────────────────────

_LOCK = threading.Lock()


def read(project: str) -> dict:
    f = _jdir(project) / "state.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(project: str, st: dict):
    j = _jdir(project)
    j.mkdir(parents=True, exist_ok=True)
    tmp = j / f"state.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, j / "state.json")


def update(project: str, **kw) -> dict:
    """Read-modify-write, so the heartbeat and the steps never overwrite
    each other's fields."""
    with _LOCK:
        st = read(project)
        for k, v in kw.items():
            if k == "steps":
                st.setdefault("steps", {})
                for s, val in v.items():
                    st["steps"].setdefault(s, {}).update(val)
            else:
                st[k] = v
        _write(project, st)
        return st


def log(project: str, text: str):
    j = _jdir(project)
    j.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H:%M:%S")
    with open(j / "log.txt", "a", encoding="utf-8") as f:
        for line in str(text).rstrip().splitlines() or [""]:
            f.write(f"{stamp} {line}\n")


def _last_activity(project: str) -> float:
    try:
        return (_jdir(project) / "log.txt").stat().st_mtime
    except OSError:
        return time.time()


def log_tail(project: str, n=40) -> list:
    try:
        lines = (_jdir(project) / "log.txt").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-n:]


def alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:                        # a zombie (finished, not reaped) is not alive
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
    except Exception:
        return True


def running(st: dict) -> bool:
    return st.get("status") in ("queued", "running") and alive(st.get("pid")) \
        and time.time() - float(st.get("heartbeat") or 0) < STALE


# ───────────────────────────── control ───────────────────────────────

def start(project: str, request: str = "", url: str = "", brand=None,
          restart: bool = False) -> dict:
    """Start (or continue) the migration of one project. A job already
    running is left alone and returned; a finished or failed one starts
    again from the first step not done — unless restart, which redoes the
    checks and the copy writing that is still empty."""
    project = slug(project)
    if not project:
        raise ValueError("give the project a name")
    d = _pdir(project)
    if not (d / "forge.json").exists() and not url:
        raise ValueError(f"no project {project!r} yet — give the address "
                         "of the site or template to start from")
    st = read(project)
    if st and running(st):
        if request and request not in st.get("requests", []):
            update(project, requests=st.get("requests", []) + [request])
        return st
    steps = st.get("steps", {}) if st and not restart else {}
    # what a new request changes has to be done again: the brief, the copy
    # still empty, the build and the checks
    if request:
        for s in ("plan", "fill", "build", "check"):
            steps.pop(s, None)
    else:
        # "finish up" on a finished job still means: write what is still
        # empty, rebuild and check again — things may have changed since
        for s in ("fill", "build", "check"):
            steps.pop(s, None)
    for s in list(steps):
        if steps[s].get("status") != "done":
            steps.pop(s)
    reqs = list(st.get("requests", [])) if st else []
    if request and request not in reqs:
        reqs.append(request)
    new = {"id": f"{time.time():.3f}", "project": project,
           "url": url or st.get("url", ""), "requests": reqs,
           "brand": list(brand or st.get("brand") or []),
           "status": "queued", "step": "", "steps": steps,
           "created": time.time(), "heartbeat": time.time(),
           "relaunches": 0, "question": "", "error": "", "result": {},
           "progress": {}}
    with _LOCK:
        _write(project, new)
    log(project, f"── job started ({'restart' if restart else 'continue'})"
        + (f": {request}" if request else ""))
    spawn(project)
    return read(project)


def spawn(project: str):
    env = dict(os.environ)
    env["AETHRON_HOME"] = str(_home())
    j = _jdir(project)
    j.mkdir(parents=True, exist_ok=True)
    out = open(j / "runner.out", "ab")
    p = subprocess.Popen([*_JOB_CMD, project], cwd=str(ROOT), env=env,
                         stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True)
    update(project, pid=p.pid, heartbeat=time.time())
    return p.pid


def stop(project: str) -> dict:
    project = slug(project)
    st = read(project)
    if not st:
        return {}
    pid = st.get("pid")
    if alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    log(project, "── stopped by the person")
    return update(project, status="stopped", pid=0)


def supervise() -> list:
    """THE WATCHDOG. Called every half minute by the studio (and on its
    launch): a job whose runner died — a crash, the app quit, the Mac
    restarted — is started again from the step it was on. A runner that
    is alive but silent for half an hour is replaced. A job that keeps
    dying is marked failed with what it last said, never retried forever.
    """
    acted = []
    root = _home() / "jobs"
    if not root.is_dir():
        return acted
    for f in root.glob("*/state.json"):
        project = f.parent.name
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("status") not in ("queued", "running"):
            continue
        age = time.time() - float(st.get("heartbeat") or 0)
        pid = st.get("pid")
        if alive(pid):
            # THE HEARTBEAT IS A THREAD, SO IT BEATS WHILE THE WORK IS
            # WEDGED. Stuck is judged by the log: nothing written for
            # HUNG seconds while the heart still beats (a sleeping Mac
            # stops both, and is not stuck).
            quiet = time.time() - _last_activity(project)
            if quiet < HUNG or age > STALE:
                continue
            age = quiet
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except OSError:
                pass
            log(project, f"── the runner went silent for {int(age // 60)} "
                f"min — replacing it")
        elif age < 10:
            continue                 # just spawned, not yet beating
        n = int(st.get("relaunches") or 0) + 1
        if n > RELAUNCHES:
            update(project, status="failed", pid=0,
                   error=st.get("error") or "the job kept stopping; the log "
                   "shows the last thing it did")
            log(project, "── gave up: the job stopped too many times")
            acted.append((project, "failed"))
            continue
        update(project, relaunches=n)
        log(project, f"── resuming at '{st.get('step') or 'start'}' "
            f"(restart {n} of {RELAUNCHES})")
        spawn(project)
        acted.append((project, "resumed"))
    return acted


def summary(project: str) -> dict:
    """What a screen or an agent needs: the steps in order with their
    state, progress of the copy writing, the question or the result."""
    st = read(slug(project))
    if not st:
        return {}
    if st.get("status") in ("queued", "running") and not alive(st.get("pid")) \
            and time.time() - float(st.get("heartbeat") or 0) > STALE:
        st["status"] = "resuming"
    steps = []
    for s in STEPS:
        info = st.get("steps", {}).get(s, {})
        status = info.get("status") or ("live" if st.get("step") == s
                                        and st.get("status") == "running"
                                        else "todo")
        steps.append({"id": s, "label": LABEL[s], "status": status,
                      "note": info.get("note", ""),
                      "secs": round(info["t1"] - info["t0"])
                      if info.get("t1") and info.get("t0") else None})
    return {"project": st.get("project"), "status": st.get("status"),
            "step": st.get("step"), "steps": steps,
            "progress": st.get("progress", {}), "question": st.get("question"),
            "error": st.get("error"), "result": st.get("result", {}),
            "started": st.get("created"), "heartbeat": st.get("heartbeat"),
            "finished": st.get("finished"), "requests": st.get("requests", []),
            "log": log_tail(st.get("project") or project, 12)}


def describe(project: str) -> str:
    """The same, in words — for an agent that started the job."""
    s = summary(project)
    if not s:
        return f"no migration job for {project!r}"
    mins = ((s.get("finished") or time.time()) - (s.get("started") or 0)) / 60
    lines = [f"migration job for {s['project']}: {s['status'].upper()} "
             f"({mins:.0f} min)"]
    for st in s["steps"]:
        mark = {"done": "✓", "live": "…", "failed": "✗", "skipped": "–",
                "todo": " "}.get(st["status"], "?")
        lines.append(f"  {mark} {st['label']}"
                     + (f" — {st['note']}" if st["note"] else ""))
    p = s.get("progress") or {}
    if p.get("total"):
        lines.append(f"copy: {p.get('done', 0)} of {p['total']} lines "
                     f"written, {p.get('rejected', 0)} refused")
    if s.get("question"):
        lines.append("NEEDS THE PERSON: " + s["question"])
    if s.get("error"):
        lines.append("ERROR: " + s["error"])
    r = s.get("result") or {}
    if r:
        lines.append("RESULT: " + r.get("verdict", ""))
        for x in r.get("problems", [])[:8]:
            lines.append("  - " + x)
    return "\n".join(lines)


# ───────────────────────────── the runner ────────────────────────────

class Wait(Exception):
    """The job needs the person: one concrete question."""


class Stopped(Exception):
    pass


class Leftover(RuntimeError):
    """Some lines got no answer. After the last try they stay as the
    template wrote them and the job goes on to build."""


_PRE = {}        # step -> thread started early (brand runs beside fetch)


def _forge(project: str, *args, timeout=1800):
    import forge_mcp
    return forge_mcp.run_forge(project, *args, timeout=timeout)


def _stopping(project):
    if read(project).get("status") == "stopped":
        raise Stopped()
    try:                     # the Aethron team's switches reach jobs too
        import aethron_control
        why = aethron_control.why("agent")
    except Exception:
        why = ""
    if why:
        raise Wait(why)


def step_source(project, st):
    d = _pdir(project)
    if (d / "forge.json").exists():
        return "already here"
    import forge_mcp
    src = st["url"]
    arg = {"url": src} if src.startswith(("http://", "https://")) \
        else {"source_path": src}
    out = forge_mcp.t_create_project({"name": project, **arg})
    log(project, out)
    return f"read {st['url']}"


def step_fetch(project, st):
    d = _pdir(project)
    if (d / "copy_map.json").exists():
        return "already downloaded"
    ok, out = _forge(project, "fetch")
    log(project, out[-3000:])
    if not ok:
        raise RuntimeError("fetch failed: " + out.strip().splitlines()[-1]
                           if out.strip() else "fetch failed")
    return "done"


def step_inventory(project, st):
    d = _pdir(project)
    if (d / "copy_map.json").exists():
        cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
        return f"{len(cm.get('strings', []))} lines"
    ok, out = _forge(project, "inventory")
    log(project, out[-2000:])
    if not ok:
        raise RuntimeError("inventory failed")
    cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
    return (f"{len(cm.get('strings', []))} lines, "
            f"{len(cm.get('images', []))} images, "
            f"{len(cm.get('links', []))} links")


def step_brand(project, st):
    pre = _PRE.pop("brand", None)
    if pre is not None:        # started while the site was downloading
        pre["t"].join(timeout=900)
        if "out" in pre:
            return pre["out"]
    return _brand_work(project, st)


def _brand_work(project, st):
    srcs = st.get("brand") or []
    d = _pdir(project)
    if not srcs:
        return "skipped" if not (d / "brand" / "pack.json").exists() \
            else "already read"
    if (d / "brand" / "pack.json").exists() and not st.get("relearn"):
        return "already read"
    try:
        import aethron_rag
        r = aethron_rag.learn(d, srcs, max_pages=25)
        return f"{r.get('chunks', r.get('passages', '?'))} passages"
    except Exception as e:           # grounding is a help, never a blocker
        log(project, f"brand material could not be read: {e}")
        return f"could not read it ({type(e).__name__}) — writing without it"


def step_plan(project, st):
    import aethron_brain as brain
    import aethron_fill as fill
    d = _pdir(project)
    plan_f = d / "project_plan.md"
    plan = plan_f.read_text(encoding="utf-8").strip() if plan_f.exists() \
        else ""
    reqs = [r for r in st.get("requests", []) if r.strip()]
    new_reqs = [r for r in reqs if r.strip() not in plan]
    if plan and not new_reqs:
        return "kept the brief"
    if not plan and not reqs:
        raise Wait("Who is the site for? Tell me the brand's name, what it "
                   "does, and anything it should say (a website of theirs "
                   "helps too).")
    if plan:     # keep everything the owner wrote; add the new request
        out = plan + "\n\nNew instructions:\n" + "\n".join(new_reqs)
    else:
        out = brain.text_call(fill.PLAN_POLISH_PROMPT + "\n".join(reqs),
                              max_tokens=4000).strip()
        if "<FILL: brand name>" in out:
            raise Wait("What is the brand's name?")
    plan_f.write_text(out + "\n", encoding="utf-8")
    return "written from your request"


def _entries(d: Path) -> tuple:
    from forge import word_fragments
    cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
    frag = word_fragments(d, cm)
    keep = None
    cfg = json.loads((d / "forge.json").read_text(encoding="utf-8"))
    if cfg.get("preview"):
        import aethron_preview as _P
        keep = _P.page_lines(d)
    out = []
    for sec in ("strings", "images", "links"):
        for e in cm.get(sec, []):
            if sec == "strings" and e["old"] in frag:
                continue
            if keep is not None and e["old"] not in keep:
                continue
            out.append((sec, e))
    return out, cm


def step_fill(project, st):
    """Write the copy in parallel batches. Each batch is a small, fresh
    prompt; a batch that lands is saved at once; a failed one is split and
    asked again; a refused line goes back once with the reason. Resumable:
    only lines not yet asked are sent."""
    import aethron_brain as brain
    import aethron_fill as fill
    import forge_mcp
    d = _pdir(project)
    plan = (d / "project_plan.md").read_text(encoding="utf-8")
    allx, _ = _entries(d)
    total = len(allx)
    asked_f = _jdir(project) / "asked.json"
    try:
        asked = set(json.loads(asked_f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        asked = set()
    todo = [(s, e) for s, e in allx if not e.get("new")
            and f"{s}\x00{e['old']}" not in asked]
    state = {"done": sum(1 for _, e in allx if e.get("new")),
             "rejected": 0, "failed": 0}
    update(project, progress={"total": total, **state})
    if not todo:
        return f"{state['done']} of {total} written"
    log(project, f"writing {len(todo)} line(s) in batches of {BATCH}, "
        f"{PARALLEL} at a time")
    write_lock = threading.Lock()
    notes_ok = (d / "brand" / "pack.json").exists()

    def notes(batch):
        if not notes_ok:
            return
        try:
            import aethron_rag
            r = aethron_rag.notes_for(d, [e["old"] for s, e in batch
                                          if s == "strings"])
            for s, e in batch:
                if e["old"] in r["notes"]:
                    e["brand_notes"] = r["notes"][e["old"]]
        except Exception:
            pass

    def ask(batch, depth=0, retry_note=""):
        _stopping(project)
        why = brain.session_check() if hasattr(brain, "session_check") else ""
        if why:
            raise Wait(why)
        payload = {}
        for s, e in batch:
            slim = {"old": e["old"], "new": ""}
            for k in ("max_bytes", "brand_notes"):
                if e.get(k):
                    slim[k] = e[k]
            payload.setdefault(s, []).append(slim)
        prompt = fill.build_prompt(plan, payload)
        if retry_note:
            prompt += "\n\n" + retry_note
        try:
            out = brain.text_call(prompt)
            got = fill.extract_json(out)
        except ValueError as ex:
            msg = str(ex)
            if any(w in msg.lower() for w in ("credit", "quota", "balance",
                                               "limit", "api key", "unauth",
                                               "permission")) \
                    and "json" not in msg.lower():
                raise Wait(f"The AI provider refused: {msg[:300]}")
            got = None
        except Exception as ex:
            log(project, f"  a batch failed: {type(ex).__name__}: "
                f"{str(ex)[:160]}")
            got = None
        if got is None:
            if len(batch) > 5 and depth < 3:     # smaller is more reliable
                h = len(batch) // 2
                ask(batch[:h], depth + 1)
                ask(batch[h:], depth + 1)
                return
            with write_lock:
                state["failed"] += len(batch)
                update(project, progress={"total": total, **state})
            return
        entries = []
        for s, items in got.items():
            if s not in ("strings", "images", "links") or not isinstance(
                    items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("new"):
                    entries.append({"section": s, "old": it.get("old", ""),
                                    "new": it["new"]})
        with write_lock:
            r = forge_mcp.t_set_content_bulk(
                {"project": project, "entries": entries, "build": False}) \
                if entries else None
            data = getattr(r, "data", None) or {}
            applied = int(data.get("applied", 0))
            refused = data.get("rejected", []) or []
            for s, e in batch:
                asked.add(f"{s}\x00{e['old']}")
            asked_f.write_text(json.dumps(sorted(asked)), encoding="utf-8")
            state["done"] += applied
            update(project, progress={"total": total, **state})
        # A REFUSED LINE GOES BACK ONCE, WITH THE REASON. Over a byte
        # budget is the common one; told the limit, a model fits it.
        if refused and depth < 1:
            by_old = {e["old"]: (s, e) for s, e in batch}
            back, reasons = [], []
            for x in refused:
                o = x.get("old") if isinstance(x, dict) else None
                if o in by_old:
                    back.append(by_old[o])
                    reasons.append(f"- {o[:60]!r}: {x.get('reason', '')}")
            if back:
                ask(back, depth + 1, "These were REFUSED last time; fix "
                    "exactly this:\n" + "\n".join(reasons))
                return
        with write_lock:
            state["rejected"] += len(refused)
            update(project, progress={"total": total, **state})

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    def one(b):          # brand notes are looked up inside the pool, so
        notes(b)         # they no longer hold every batch up in a queue
        ask(b)
    with ThreadPoolExecutor(PARALLEL) as ex:
        futs = [ex.submit(one, b) for b in batches]
        err = None
        for f in futs:
            try:
                f.result()
            except (Wait, Stopped) as e:
                err = err or e
            except Exception as e:
                err = err or e
        if err:
            raise err
    if state["failed"]:
        # they were never marked asked, so the next attempt sends only these
        raise Leftover(f"{state['failed']} line(s) got no answer from "
                       "the AI provider")
    allx, _ = _entries(d)
    done = sum(1 for _, e in allx if e.get("new"))
    return f"{done} of {total} written"


def step_build(project, st):
    ok, out = _forge(project, "build")
    log(project, out[-2500:])
    if not ok:
        raise RuntimeError("build failed: " + (out.strip().splitlines()
                                               or ["?"])[-1])
    return "built"


def step_share(project, st):
    """The link preview shows the migrated site, not the template's own
    marketplace picture or logo (aethron_share)."""
    if os.environ.get("AETHRON_BROWSER", "").lower() in ("none", "off", "0"):
        return "skipped (no browser)"
    import aethron_share
    d = _pdir(project)

    def rebuild():
        ok, out = _forge(project, "build")
        if not ok:
            raise RuntimeError("rebuild after the link preview failed")
    return aethron_share.ensure(d, log=lambda t: log(project, "  " + t),
                                rebuild=rebuild)


def _filled(d) -> int:
    allx, _ = _entries(d)
    return sum(1 for _, x in allx if x.get("new"))


def step_check(project, st):
    """The healer: deterministic repair first, then — only for what it
    cannot express — ONE agent round with a fresh, small context and a
    hard time limit. The verdict is measured from disk and from a real
    browser. Copy the job wrote is never lost to a repair round."""
    import aethron_healer
    d = _pdir(project)
    cm_f = d / "copy_map.json"
    before = cm_f.read_text(encoding="utf-8") if cm_f.exists() else ""
    n_before = _filled(d)

    def ev(kind, data):
        t = (data or {}).get("text") if isinstance(data, dict) else data
        if t:
            log(project, f"  {t}")
            if kind in ("stage", "heal", "retry", "error", "stuck"):
                update(project, steps={"check": {"note": str(t)[:120]}})
    agent = os.environ.get("AETHRON_JOB_AGENT", "1") != "0"
    limit = int(os.environ.get("AETHRON_JOB_AGENT_SECONDS", "600"))
    budget = int(os.environ.get("AETHRON_JOB_CHECK_SECONDS",
                                str(CHECK_SECONDS)))
    r = aethron_healer.heal(d, rounds=1, use_agent=agent, on_event=ev,
                            live=True, agent_timeout=limit,
                            deadline=time.time() + budget)
    # A REPAIR ROUND MUST NOT UNDO THE JOB. An agent that reaches for
    # undo walks back through the fill's own snapshots: the lines vanish
    # while the card still says they were written.
    lost = n_before - _filled(d)
    if before and lost > 0:
        cm_f.write_text(before, encoding="utf-8")
        log(project, f"  the repair round lost {lost} written line(s) — "
            "put them back and rebuilt")
        _forge(project, "build")
        r = dict(r, ok=False)
    e = r.get("evidence") or {}
    problems = []
    for src in ("verify", "probe"):
        for line in str(e.get(src, "")).splitlines():
            if line.strip().startswith(("FAIL", "✗", "ERROR")):
                problems.append(line.strip()[:200])
    done = _filled(d)
    allx, _ = _entries(d)
    ok = bool(r.get("ok"))
    verdict = ("finished — verify and the browser check pass" if ok else
               "finished with problems the checks name" if problems else
               "finished, but it could not be checked in a browser"
               if r.get("stage") in ("unproven",) else
               "finished — the check ran out of time on what is left"
               if r.get("stage") == "out-of-time" else
               "finished with problems")
    update(project, result={"ok": ok, "verdict": verdict,
                            "problems": problems[:20], "filled": done,
                            "total": len(allx), "stage": r.get("stage")})
    return "pass" if ok else f"{len(problems) or 'some'} problem(s)"


def _start_brand_early(project):
    """Reading the brand's site does not need the template's files, so it
    runs while they download instead of after."""
    st = read(project)
    if not st.get("brand") or \
            st.get("steps", {}).get("brand", {}).get("status") == "done":
        return
    box = {}

    def go():
        try:
            box["out"] = _brand_work(project, st)
        except Exception as e:
            box["err"] = e      # the step itself runs again and reports
    box["t"] = threading.Thread(target=go, daemon=True)
    _PRE["brand"] = box
    log(project, "  reading the brand material alongside")
    box["t"].start()


RUNNERS = {"source": step_source, "fetch": step_fetch,
           "inventory": step_inventory, "brand": step_brand,
           "plan": step_plan, "fill": step_fill, "build": step_build,
           "share": step_share,
           "check": step_check}


def run(project: str):
    project = slug(project)
    st = read(project)
    if not st:
        print(f"no job for {project}")
        return 1
    me = os.getpid()
    other = st.get("pid")
    if other and other != me and alive(other) and \
            time.time() - float(st.get("heartbeat") or 0) < STALE:
        print(f"another runner ({other}) has this job")
        return 0
    update(project, pid=me, status="running", heartbeat=time.time())
    os.environ["AETHRON_JOB_SELF"] = project   # its own tools are let through
    beat = threading.Event()

    def heart():
        while not beat.wait(5):
            try:
                if read(project).get("pid") == me:
                    update(project, heartbeat=time.time())
            except Exception:
                pass
    threading.Thread(target=heart, daemon=True).start()
    try:
        for s in STEPS:
            st = read(project)
            if st.get("status") == "stopped":
                return 0
            if st.get("steps", {}).get(s, {}).get("status") == "done":
                continue
            _stopping(project)
            update(project, step=s, steps={s: {"status": "live",
                                               "t0": time.time()}})
            log(project, f"▶ {LABEL[s]}")
            note = None
            tries = TRIES_FOR.get(s, TRIES)
            for attempt in range(1, tries + 1):
                try:
                    note = RUNNERS[s](project, read(project))
                    break
                except (Wait, Stopped):
                    raise
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    log(project, f"  attempt {attempt}/{tries} failed — "
                        f"{msg[:400]}")
                    if attempt == tries:
                        if s in OPTIONAL or isinstance(e, Leftover):
                            # not the site itself: say so, carry on
                            note = (f"{e} — left as the template wrote them"
                                    if isinstance(e, Leftover) else
                                    f"could not finish ({msg[:140]}) — "
                                    "carried on without it")
                            if s == "check":
                                update(project, result={
                                    "ok": False, "filled": _filled(
                                        _pdir(project)),
                                    "verdict": "finished — the check "
                                    "could not run to the end",
                                    "problems": [msg[:200]]})
                            break
                        update(project, steps={s: {"status": "failed",
                                                   "t1": time.time(),
                                                   "note": msg[:200]}})
                        raise
                    update(project, steps={s: {"note": f"retrying "
                                               f"({attempt}/{tries})"}})
                    time.sleep(min(60, 10 * attempt * attempt))
            update(project, steps={s: {"status": "done", "t1": time.time(),
                                       "note": note or ""}})
            log(project, f"  ✓ {note or 'done'}")
            if s == "source":
                _start_brand_early(project)
        update(project, status="done", step="", finished=time.time(), pid=0,
               question="", error="")
        log(project, "── job finished")
        return 0
    except Wait as w:
        update(project, status="waiting", question=str(w), pid=0,
               steps={read(project).get("step", ""): {"status": "todo",
                                                     "note": "waiting for "
                                                     "you"}})
        log(project, f"── waiting for the person: {w}")
        return 0
    except Stopped:
        update(project, status="stopped", pid=0)
        return 0
    except Exception as e:
        update(project, status="failed", pid=0, finished=time.time(),
               error=f"{type(e).__name__}: {str(e)[:300]}")
        log(project, f"── job failed: {e}")
        try:
            import aethron_reports
            aethron_reports.report("job", "migration-job", _pdir(project),
                                   log="\n".join(log_tail(project, 80)),
                                   error=str(e))
        except Exception:
            pass
        return 1
    finally:
        beat.set()


def main(argv):
    if len(argv) >= 2 and argv[0] == "run":
        return run(argv[1])
    if len(argv) >= 1 and argv[0] == "status":
        print(describe(argv[1]) if len(argv) > 1 else "give a project")
        return 0
    if len(argv) >= 1 and argv[0] == "supervise":
        print(supervise())
        return 0
    print("usage: aethron_jobs.py run|status|supervise <project>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

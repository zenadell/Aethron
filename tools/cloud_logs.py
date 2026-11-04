#!/usr/bin/env python3
"""Print what the app uploaded (failure reports, agent runs, events, managed
AI usage, devices), for reading in a GitHub Actions log.

Needs AETHRON_SUPABASE_URL and SUPABASE_SERVICE_KEY. Never prints a key:
records are already scrubbed on the Mac, and long strings are cut here."""
import json, os, sys, urllib.request, urllib.parse
from collections import defaultdict

URL = os.environ["AETHRON_SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def rows(table, query):
    req = urllib.request.Request(f"{URL}/rest/v1/{table}?{query}", headers={
        "apikey": KEY, "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception as e:                          # a missing table says so
        body = getattr(e, "read", lambda: b"")()[:300]
        print(f"!! {table}: {e} {body!r}")
        return []


def cut(v, n=600):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"…(+{len(v)-n})"
    if isinstance(v, dict):
        return {k: cut(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [cut(x, n) for x in v[:40]]
    return v


import datetime as dt
since = (dt.datetime.utcnow() - dt.timedelta(days=DAYS)).isoformat() + "Z"
q = urllib.parse.quote

print(f"===== FAILURE REPORTS (last {DAYS} days) =====")
fr = rows("failure_reports", f"received_at=gte.{q(since)}&order=received_at.desc&limit=2000")
groups = defaultdict(list)
for r in fr:
    groups[r["fingerprint"]].append(r)
print(f"{len(fr)} reports, {len(groups)} distinct problems")
for fp, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    r = g[0]
    print(f"\n--- [{len(g)}x, {len({x['machine'] for x in g})} machine(s)] "
          f"{r['kind']}/{r['step']} :: {r['category']} :: v{r['app_version']} "
          f":: {r['source_url']} :: last {r['received_at']}")
    rec = r["record"]
    for k in ("error", "lines", "fails", "template", "project", "trace", "log"):
        if k in rec:
            print(f"  {k}: {json.dumps(cut(rec[k], 1500))}")
    others = {k: v for k, v in rec.items()
              if k not in ("error", "lines", "fails", "template", "project", "trace", "log")}
    print(f"  other: {json.dumps(cut(others, 300))}")

print(f"\n===== AGENT RUNS (last {DAYS} days) =====")
ar = rows("agent_runs", f"received_at=gte.{q(since)}&order=received_at.asc&limit=5000")
runs = defaultdict(list)
for r in ar:
    runs[(r["caller"], r["run"])].append(r)
print(f"{len(ar)} calls in {len(runs)} runs")
for (caller, run), g in runs.items():
    bad = [x for x in g if x["status"] not in ("ok",)]
    print(f"\n--- run {caller}/{run}: {len(g)} calls, {len(bad)} not ok, "
          f"project(s) {sorted({x['project'] or '' for x in g})}")
    for x in g:
        rec = x["record"]
        line = f"  #{x['n']} {x['tool']} [{x['status']}] {x['ms']}ms"
        if x["status"] != "ok" or rec.get("findings"):
            line += f"\n     args: {json.dumps(cut(rec.get('args'), 200))}"
            line += f"\n     findings: {json.dumps(cut(rec.get('findings'), 400))}"
            line += f"\n     result: {json.dumps(cut(rec.get('result') or rec.get('text'), 900))}"
        print(line)

print(f"\n===== EVENTS (last {DAYS} days) =====")
ev = rows("events", f"created_at=gte.{q(since)}&order=created_at.asc&limit=3000")
cnt = defaultdict(int)
for e in ev:
    cnt[e["event"]] += 1
print(dict(sorted(cnt.items(), key=lambda kv: -kv[1])))
for e in ev:
    p = e.get("props") or {}
    if any(w in e["event"] for w in ("fail", "error", "block", "stuck", "heal")) \
            or p.get("error_category") or p.get("ok") is False:
        print(f"  {e['created_at']} {e['event']} {json.dumps(cut(p, 300))}")

print(f"\n===== MANAGED AI USAGE (last {DAYS} days) =====")
us = rows("usage_ledger", f"at=gte.{q(since)}&order=at.asc&limit=5000")
st = defaultdict(int)
for u in us:
    st[(u["provider"], u["model"], u["status"])] += 1
for k, v in sorted(st.items(), key=lambda kv: -kv[1]):
    print(f"  {v:5d}  {k}")
spend = defaultdict(float)
for u in us:
    if str(u.get("at", ""))[:10] == dt.datetime.utcnow().strftime("%Y-%m-%d"):
        spend[str(u.get("user_id"))[:8]] += float(u.get("cost_usd") or 0)
print("  spent today (UTC) by account:", {k: round(v, 4) for k, v in spend.items()})
ctl = rows("control", "id=eq.1")
if ctl:
    c = ctl[0]
    print("  control:", {k: c.get(k) for k in ("ai_mode", "managed_provider", "managed_model",
                                            "default_daily_usd", "global_daily_usd", "paused")})
for uc in rows("user_controls", "limit=20"):
    print("  user control:", {k: uc.get(k) for k in uc if k not in ("note",)})
ms = sorted(int(u.get("ms") or 0) for u in us)
if ms:
    print(f"  answer time ms: median {ms[len(ms)//2]}, 90% {ms[int(len(ms)*.9)]}, max {ms[-1]}")
    print("  longest:", [(u["at"][11:19], u["purpose"], u["ms"], u["output_tokens"]) for u in sorted(us, key=lambda u: -(u.get("ms") or 0))[:8]])
    print("  last 5:", [(u["at"][11:19], u["purpose"], u["ms"]) for u in us[-5:]])
print("  latest that did not answer:")
for u in [u for u in us if u.get("status") != "ok"][-15:]:
    print(f"    {u['at']} {u['provider']} {u['model']} {u['status']} {u.get('ms')}ms")

print("\n===== DEVICES =====")
for d in rows("devices", "order=last_seen.desc&limit=50"):
    print(f"  {d['machine'][:8]} v{d['version']} {d['os']} {d['country']} "
          f"last {d['last_seen']} activity={json.dumps(cut(d.get('activity'), 200))}")

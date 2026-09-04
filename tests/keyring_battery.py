#!/usr/bin/env python3
"""Does the key ring actually protect the paid key?

THE POINT OF THIS FILE. The ring exists so a run drains free-tier keys
before it spends real credit. That is a claim about money, and a claim
about money that has never been tested is just a hope — so every path is
exercised here against MOCKED failures: no network, no key, no cost.

The distinction that matters most is between the two 429s. A provider
returns the same status for "you have sent too many requests this
minute" and "your allowance for today is gone". Rotating on the first
would burn all four free keys in seconds and land on the paid key while
every free allowance was still intact — the precise failure the ring was
built to prevent.
"""
import io
import json
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aethron_brain as brain                                   # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


def scenario(t):
    print(f"\n── {t}")


def http429_code(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {},
                                  io.BytesIO(body.encode()))


def http429(body):
    return urllib.error.HTTPError(
        "http://x", 429, "Too Many Requests", {},
        io.BytesIO(body.encode()))


QUOTA = json.dumps({"error": {"message":
                    "You exceeded your current quota, please check your "
                    "plan and billing details."}})
RATE = json.dumps({"error": {"message":
                   "Too many requests per minute. Please retry shortly."}})

FREE = ["free-key-1", "free-key-2", "free-key-3", "free-key-4"]
PAID = "paid-key-LAST"
# PRODUCTION SHAPE. The ring lives in the saved settings and callers
# pass no cfg at all, so the battery patches load() rather than handing
# text_call a config-shaped dict — passing one would look like an
# explicit per-call key pin, which is a different (and also supported)
# code path.
SETTINGS = {"provider": "gemini", "model": "m",
            "api_keys": FREE,      # free ring, tried in order
            "api_key": PAID}       # last resort
CFG = None


def run():
    tmp = Path(tempfile.mkdtemp())
    brain.KEYSTATE = tmp / "keys.json"
    real_once, real_sleep = brain._text_once, time.sleep
    real_load = brain.load
    brain.load = lambda: dict(brain.DEFAULT, **SETTINGS)
    time.sleep = lambda *_a, **_k: None          # no real backoff waits

    try:
        scenario("the ring is ordered, and the paid key is last")
        r = brain.ring(CFG)
        check("all five keys are in the ring", len(r) == 5, f"{len(r)}")
        check("the paid key is last", r[-1] == PAID, r[-1])
        check("resolve picks the FIRST free key, not the paid one",
              brain.resolve(CFG)["key"] == FREE[0])

        scenario("a spent free key rotates to the next free key")
        used = []

        def once_quota_on_first(r_, *a):
            used.append(r_["key"])
            if r_["key"] == FREE[0]:
                raise http429(QUOTA)
            return "ok"
        brain._text_once = once_quota_on_first
        out = brain.text_call("hi", cfg=CFG)
        check("the call still succeeded", out == "ok", out)
        check("it moved to the second free key",
              used[-1] == FREE[1], str(used))
        check("the spent key is remembered",
              brain._fp(FREE[0]) in brain._keystate()["exhausted"])
        check("the paid key was never touched", PAID not in used)

        # A PER-MINUTE LIMIT BELONGS TO ONE KEY, NOT TO THE ACCOUNT.
        # The first version of this test asserted the call waits on the
        # same key. That is the wrong goal: the next FREE key has its own
        # per-minute allowance, so moving to it serves the request now,
        # still for nothing. What must hold is that a pace limit never
        # counts as a spent allowance and never reaches the paid key.
        scenario("a RATE limit rotates across free keys, and costs nothing")
        brain.KEYSTATE = tmp / "keys2.json"
        seen = []

        def once_rate_then_ok(r_, *a):
            seen.append(r_["key"])
            if len(seen) < 3:
                raise http429(RATE)
            return "ok"
        brain._text_once = once_rate_then_ok
        out = brain.text_call("hi", cfg=CFG)
        check("the call succeeded", out == "ok", out)
        check("it used other FREE keys, never the paid one",
              PAID not in seen, str(seen))
        check("no key was marked spent by a pace limit",
              not brain._keystate()["exhausted"],
              str(brain._keystate()["exhausted"]))

        scenario("the paid key is reached ONLY when every free key is gone")
        brain.KEYSTATE = tmp / "keys3.json"
        order = []

        def once_all_free_spent(r_, *a):
            order.append(r_["key"])
            if r_["key"] != PAID:
                raise http429(QUOTA)
            return "paid"
        brain._text_once = once_all_free_spent
        out = brain.text_call("hi", cfg=CFG)
        check("it eventually succeeded on the paid key", out == "paid", out)
        check("it tried all four free keys first, in order",
              order[:4] == FREE, str(order[:4]))
        check("the paid key was last, and used once",
              order[-1] == PAID and order.count(PAID) == 1, str(order))

        scenario("exhaustion survives a new process")
        state = json.loads((tmp / "keys3.json").read_text())
        check("the state file lists the four spent free keys",
              len(state["exhausted"]) == 4, str(len(state["exhausted"])))
        check("it stores FINGERPRINTS, never key material",
              not any(k in json.dumps(state) for k in FREE + [PAID]))
        check("a fresh resolve goes straight to the paid key",
              brain.resolve(CFG)["key"] == PAID)

        scenario("free allowances come back tomorrow")
        (tmp / "keys3.json").write_text(json.dumps(
            {"date": "2000-01-01",
             "exhausted": [brain._fp(k) for k in FREE]}))
        check("a stale day resets the ring",
              len(brain.live_keys(CFG)) == 5,
              str(len(brain.live_keys(CFG))))
        check("resolve is back on the first free key",
              brain.resolve(CFG)["key"] == FREE[0])

        scenario("nothing left to try is said plainly, not silently")
        brain.KEYSTATE = tmp / "keys4.json"

        def always_quota(r_, *a):
            raise http429(QUOTA)
        brain._text_once = always_quota
        try:
            brain.text_call("hi", cfg=CFG)
            check("it raises when the whole ring is spent", False, "no raise")
        except Exception as e:
            check("it raises when the whole ring is spent", True)
            check("the error names the real cause",
                  "quota" in str(e).lower() or "429" in str(e), str(e)[:70])


        scenario("a 503 is a blip, not a dead key")
        brain.KEYSTATE = tmp / "keys5.json"
        hits = []

        def busy_then_ok(r_, *a):
            hits.append(r_["key"])
            if len(hits) <= 2:                       # first two keys busy
                raise http429_code(503, "high demand")
            return "ok"
        brain._text_once = busy_then_ok
        out = brain.text_call("hi", cfg=CFG)
        check("a busy key is skipped, not killed", out == "ok", out)
        check("nothing was marked spent by a 503",
              not brain._keystate()["exhausted"])
        check("it stayed on the FREE ring", PAID not in hits, str(hits))

        # A KEY THAT HANGS IS PARKED, NOT RETRIED. Measured on the real
        # account, a dead free key does not fail fast — it accepts the
        # connection and never answers, costing a full timeout every
        # time it is tried. Re-sweeping it on every backoff round turned
        # one rebrand batch into 18 minutes of nothing. So each free key
        # gets ONE attempt, then goes cold for a while; free capacity is
        # retried on a later call, not inside this one.
        scenario("paid credit is reached only after every free key is tried")
        brain.KEYSTATE = tmp / "keys6.json"
        seq = []

        def all_free_busy(r_, *a):
            seq.append(r_["key"])
            if r_["key"] != PAID:
                raise http429_code(503, "high demand")
            return "paid"
        brain._text_once = all_free_busy
        out = brain.text_call("hi", cfg=CFG)
        check("it did fall back to paid in the end", out == "paid", out)
        check("every free key was tried before the paid one",
              set(seq[:-1]) == set(FREE), str(seq))
        check("each hanging key cost exactly one timeout, not five",
              all(seq.count(k) == 1 for k in FREE), str(seq))
        check("no free key was marked spent by a blip",
              not brain._keystate()["exhausted"])
        check("the hanging keys are parked, so the next call skips them",
              len(brain._keystate().get("cold", {})) == len(FREE),
              str(brain._keystate().get("cold")))

        scenario("a single api_key still works (nothing regressed)")
        one = {"provider": "gemini", "model": "m", "api_key": "solo",
               "api_keys": []}
        check("an explicit per-call key still pins",
              brain.resolve({"api_key": "pinned"})["key"] == "pinned")
        brain.KEYSTATE = tmp / "keys7.json"
        check("a lone key is a one-key ring", brain.ring(one) == ["solo"])
        check("resolve returns it", brain.resolve(one)["key"] == "solo")
    finally:
        brain._text_once, time.sleep = real_once, real_sleep
        brain.load = real_load

    print(f"\n{PASS}/{PASS + FAIL} green")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())

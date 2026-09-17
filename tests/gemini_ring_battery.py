#!/usr/bin/env python3
"""Does the change engine's Gemini call walk the key ring — and never mistake a free quota for no credit?

Measured 2026-09-15: gemini_text took the ring's first key and read Google's free-tier message
"You exceeded your current quota, please check your plan and billing details" as "credits
depleted" because it contains the word billing. One spent free key stopped every request while
two free keys that answered in two seconds sat unused. And the ledger priced every free call at
list price, so a report could show money that was never charged.

Every path here runs against FAKED responses: fake keys, a temporary key-state file, no network,
no real key, no cost.
"""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aethron_brain as brain      # noqa: E402
import aethron_build as B          # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


def quota(kind):
    qid = {"day": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
           "minute": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}[kind]
    return json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                  "message": "You exceeded your current quota, please check your plan and billing details.",
                                  "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                               "violations": [{"quotaId": qid}]},
                                              {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                               "retryDelay": "7s"}]}}])


DEPLETED = json.dumps([{"error": {"code": 429, "message": "Your prepayment credits are depleted. Please go to AI "
                                                          "Studio to manage your project and billing."}}])
ANSWER = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 1000, "total_tokens": 1500}}


class Reply:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def world(script):
    """script: key -> list of outcomes, consumed in order: 'ok' | 'day' | 'minute' | 'depleted' | 'hang'."""
    calls, sleeps = [], []
    queues = {k: list(v) for k, v in script.items()}

    def urlopen(req, timeout=None):
        key = (req.get_header("Authorization") or "").split(" ", 1)[-1]
        calls.append(key)
        what = queues[key].pop(0) if queues.get(key) else "ok"
        if what == "ok":
            return Reply(ANSWER)
        if what == "hang":
            raise TimeoutError("timed out")
        body = DEPLETED if what == "depleted" else quota(what)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(body.encode()))
    urllib.request.urlopen = urlopen
    time.sleep = lambda s: sleeps.append(s)
    return calls, sleeps


def fresh(free, paid, wallet=None):
    tmp = Path(tempfile.mkdtemp(prefix="ae-ring-"))
    brain.KEYSTATE = tmp / "keystate.json"
    brain.WALLET = tmp / "spend.json"
    os.environ.pop("AETHRON_WALLET_USD", None)   # the machine's own ceiling must not steer a test
    brain.wallet_set(brain.DEFAULT_WALLET if wallet is None else wallet)
    brain.free_ring = lambda cfg=None: list(free)
    brain.paid_key = lambda cfg=None: paid


def main():
    real_open, real_sleep = urllib.request.urlopen, time.sleep
    try:
        print("── a spent free key is skipped, not mistaken for no credit")
        fresh(["free-A", "free-B"], "paid-P")
        calls, _ = world({"free-A": ["day"], "free-B": ["ok"]})
        led = {}
        got = B.gemini_text("hi", led, budget_usd=0.05)
        check("free-A's daily quota sends the request on to free-B, which answers", got == "OK" and calls == ["free-A", "free-B"],
              str(calls))
        check("  ...free-A is marked spent for today", brain._fp("free-A") in brain._keystate()["exhausted"])
        check("  ...and a free call charges nothing, while its list price is kept apart",
              led["usd"] == 0 and led["free_calls"] == 1 and led["list_usd"] > 0, str(led))
        calls2, _ = world({"free-B": ["ok"]})
        B.gemini_text("again", {}, budget_usd=0.05)
        check("the next call does not try the spent key again today", calls2 == ["free-B"], str(calls2))

        print("\n── the wallet: a ceiling one run cannot see, and twenty runs cannot pass")
        fresh(["free-A"], "paid-P", wallet=0.0005)
        calls, _ = world({"free-A": ["day"], "paid-P": ["ok"]})
        led = {}
        try:
            B.gemini_text("hi", led, budget_usd=5.0)
            check("a paid call is refused when the wallet cannot cover its worst case", False, "it was made")
        except B.BudgetExhausted as e:
            check("a paid call is refused when the wallet cannot cover its worst case",
                  "wallet" in str(e) and "paid-P" not in calls, str(e)[:160])
        fresh(["free-A"], "paid-P", wallet=2.0)
        world({"free-A": ["day"], "paid-P": ["ok"]})
        led = {}
        B.gemini_text("hi", led, budget_usd=5.0)
        w = brain.wallet()
        check("  ...an allowed paid call comes off the wallet, by the real bill",
              led["usd"] > 0 and abs(w["spent_usd"] - led["usd"]) < 1e-6 and w["calls"] == 1, str(w))
        check("  ...and what is left is reported with the run", abs(led["wallet_left"] - (2.0 - led["usd"])) < 1e-6,
              str(led.get("wallet_left")))
        fresh(["free-A"], "paid-P", wallet=2.0)
        world({"free-A": ["ok"]})
        B.gemini_text("hi", {}, budget_usd=5.0)
        check("  ...a free call never touches it", brain.wallet()["spent_usd"] == 0.0, str(brain.wallet()))

        print("\n── a per-minute limit is waited out on the same key")
        fresh(["free-A", "free-B"], "paid-P")
        calls, sleeps = world({"free-A": ["minute", "ok"]})
        B.gemini_text("hi", {}, budget_usd=0.05)
        check("the same key is retried after the delay Google gave", calls == ["free-A", "free-A"] and sleeps
              and sleeps[0] == 8, f"{calls} {sleeps}")
        check("  ...and it is not marked spent", brain._fp("free-A") not in brain._keystate()["exhausted"])

        print("\n── a key that hangs is parked")
        fresh(["free-A", "free-B"], "paid-P")
        calls, _ = world({"free-A": ["hang"], "free-B": ["ok"]})
        B.gemini_text("hi", {}, budget_usd=0.05)
        check("free-A timing out hands the request to free-B", calls == ["free-A", "free-B"], str(calls))
        check("  ...and free-A is parked, not marked spent",
              brain._is_cold("free-A") and brain._fp("free-A") not in brain._keystate()["exhausted"])

        print("\n── the paid key is last, and money is guarded before the call")
        fresh(["free-A"], "paid-P")
        calls, _ = world({"free-A": ["day"], "paid-P": ["ok"]})
        led = {}
        B.gemini_text("hi", led, budget_usd=0.10, max_tokens=4000)
        check("with the free key spent, the paid key answers and is charged", calls == ["free-A", "paid-P"]
              and led["usd"] > 0 and led["free_calls"] == 0, f"{calls} {led}")
        fresh(["free-A"], "paid-P")
        calls, _ = world({"free-A": ["day"], "paid-P": ["ok"]})
        led = {}
        try:
            B.gemini_text("hi", led, budget_usd=0.001, max_tokens=4000)
            refused = False
        except B.BudgetExhausted as e:
            refused = "over the $0.00 budget" in str(e)
        check("a paid call whose worst case crosses the budget is never started", refused and calls == ["free-A"],
              f"{calls} {led.get('stopped')}")
        fresh(["free-A", "free-B"], "paid-P")
        calls, _ = world({"free-A": ["day"], "free-B": ["day"], "paid-P": ["depleted"]})
        led = {}
        try:
            B.gemini_text("hi", led, budget_usd=0.10)
            why = ""
        except B.BudgetExhausted as e:
            why = str(e)
        check("every key spent says so, key by key, and charges nothing",
              "today's free allowance is spent" in why and "prepaid credit is depleted" in why and led["usd"] == 0,
              why)
        check("  ...and the depleted paid key is marked, so tomorrow's free keys go first",
              brain._fp("paid-P") in brain._keystate()["exhausted"])
    finally:
        urllib.request.urlopen, time.sleep = real_open, real_sleep
    print(f"\ngemini ring battery: {PASS} ok, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

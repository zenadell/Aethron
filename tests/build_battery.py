#!/usr/bin/env python3
"""Does the loop refuse what it should, and keep the best when it does?

The contract this suite defends is the one that separates Aethron from
every other tool in its class: THE HAND-OVER IS CONDITIONAL. Cursor and
Claude Code hand over unconditionally because they never render. Lovable
and v0 hand over and ask a person. This refuses.

Two ways that promise can rot, and both are tested here:

  1. THE REFEREE GOES SOFT — a build that is missing what the user
     literally asked for gets waved through, and the refusal becomes
     decoration.
  2. THE LOOP KEEPS THE LAST ATTEMPT INSTEAD OF THE BEST — which hands
     over a regression roughly half the time if the writer is a model,
     while the report happily quotes the best score it ever saw.

The writer here is a MOCK on purpose. The whole point of the
architecture is that the contract is enforced on the OUTPUT, so it must
hold for any writer — including one having a bad day, which is exactly
what the mock simulates.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_build as B            # noqa: E402

OK = FAIL = SKIP = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   ({detail})" if detail else ""))


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}   ({why})")


BRIEF = ('Build a pricing page for Northwind with three tiers, '
         'a FAQ section, and a "Start free trial" button')

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Northwind</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#FFFFFF;color:#16161C;font-family:Helvetica,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:48px 24px}
.brand{font-size:24px;font-weight:700;display:block;margin-bottom:32px}
h1{font-size:40px;margin-bottom:16px}
.lede{font-size:16px;color:COLOUR;margin-bottom:48px}
.tiers{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:64px}
.tier{flex:1 1 280px;border:1px solid #D8D8E0;border-radius:16px;padding:32px}
.tier h2{font-size:24px;margin-bottom:8px}
.tier p{font-size:16px;color:COLOUR;margin-bottom:24px}
.cta{display:inline-block;font-size:16px;background:#1A3FCC;color:#fff;
 text-decoration:none;border-radius:8px;padding:16px 24px}
h3{font-size:24px;margin-bottom:16px}
.faq p{font-size:16px;color:COLOUR;margin-bottom:16px}
</style></head><body><div class="wrap">
<span class="brand">Northwind</span>
<h1>Pricing that scales with you</h1>
<p class="lede">Every plan includes the measurement engine.</p>
<div class="tiers">"""

TAIL = """</div>
<section class="faq"><h3>FAQ</h3>
<p>Can I change plans later? Yes, at any time.</p></section>
</div></body></html>"""


def page(tiers=3, cta="Start free trial", colour="#3A3A46", spill=False):
    """A page with exactly the defects asked for and no others."""
    names = ["Starter", "Studio", "Agency", "Scale"]
    body = ""
    for i in range(tiers):
        body += (f'<div class="tier"><h2>{names[i % 4]}</h2>'
                 f'<p>Everything in the tier below, and more.</p>'
                 f'<a class="cta" href="#">{cta}</a></div>')
    html = HEAD + body + TAIL
    html = html.replace("COLOUR", colour)
    if spill:
        html = html.replace(".tiers{display:flex",
                            ".tiers{min-width:1500px;display:flex")
    return html


def write(project, html):
    Path(project).mkdir(parents=True, exist_ok=True)
    (Path(project) / "index.html").write_text(html)


def cap_checks():
    """The spend cap, proven with the network faked. Costs nothing.

    Real money is the one thing a test must never touch, and the one
    thing this writer exists to protect: the owner ran this with $0.38
    left. So every path that could spend is exercised against a fake
    endpoint, and the assertions are about what the writer REFUSES.
    """
    import io
    import urllib.error
    import urllib.request

    print("\n── the spend cap cannot be crossed (network faked)")
    real = urllib.request.urlopen
    calls = {"n": 0}

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def ok_reply(finish="stop"):
        import json as _j
        return _j.dumps({
            "choices": [{"finish_reason": finish, "message": {
                "content": "<!doctype html><html><body>hi</body></html>"}}],
            # THINKING IS IN total - prompt. completion_tokens here is
            # deliberately SMALLER than what was billed, which is how a
            # thinking model reports it — the ledger must not trust it.
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500,
                      "total_tokens": 5000}}).encode()

    try:
        # 1. refuses before calling when one worst case exceeds the cap
        w = B.gemini_writer(budget_usd=0.01, key="k", verbose=False)
        d = Path(tempfile.mkdtemp())
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("network touched"))
        try:
            w("p", d, 0)
            check("a cap below one call refuses before calling", False,
                  "the call was allowed")
        except B.BudgetExhausted:
            check("a cap below one call refuses before calling",
                  w.ledger["calls"] == 0 and w.ledger["usd"] == 0
                  and not (d / "index.html").exists())

        # 2. spend comes from REPORTED usage, with thinking counted
        def fake(*a, **k):
            calls["n"] += 1
            return Resp(ok_reply())
        urllib.request.urlopen = fake
        w = B.gemini_writer(budget_usd=0.06, key="k", verbose=False)
        d = Path(tempfile.mkdtemp())
        w("p", d, 0)
        want = 1000 * B.GEMINI_IN + 4000 * B.GEMINI_OUT
        check("spend is measured from usage, thinking included",
              abs(w.ledger["usd"] - want) < 1e-9 and w.ledger["out"] == 4000,
              f"${w.ledger['usd']:.5f} vs ${want:.5f}, out={w.ledger['out']}")
        check("and the page was written", (d / "index.html").exists())

        # 3. once real spend makes the NEXT worst case unaffordable, stop
        before = calls["n"]
        try:
            w("p", d, 1)
            check("a second call that could cross the cap is refused",
                  False, f"spent ${w.ledger['usd']:.4f} and called again")
        except B.BudgetExhausted:
            check("a second call that could cross the cap is refused",
                  calls["n"] == before)

        # 4. "credits depleted" stops at once — no back-off retries
        calls["n"] = 0

        def depleted(*a, **k):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "u", 429, "x", {}, io.BytesIO(
                    b'{"error":"Your prepayment credits are depleted."}'))
        urllib.request.urlopen = depleted
        w = B.gemini_writer(budget_usd=1.0, key="k", verbose=False)
        try:
            w("p", Path(tempfile.mkdtemp()), 0)
            check("depleted credits stop immediately", False, "no stop")
        except B.BudgetExhausted:
            check("depleted credits stop immediately, without retrying",
                  calls["n"] == 1, f"{calls['n']} attempt(s)")

        # 5. a page cut off by the token limit is never written
        urllib.request.urlopen = lambda *a, **k: Resp(ok_reply("length"))
        w = B.gemini_writer(budget_usd=1.0, key="k", verbose=False)
        d = Path(tempfile.mkdtemp())
        try:
            w("p", d, 0)
            check("a truncated page is not written", False, "it was written")
        except RuntimeError:
            check("a truncated page is not written",
                  not (d / "index.html").exists())
    finally:
        urllib.request.urlopen = real


def main():
    import aethron_figma_grade as GR

    print("\n── a brief is a requirements list, not a vague wish")
    reqs = B.requirements(BRIEF)
    kinds = {(r["kind"], r["value"].lower()) for r in reqs}
    check('the quoted copy is extracted literally',
          ("text", "start free trial") in kinds, str(sorted(kinds)))
    check('"three tiers" becomes a countable requirement',
          any(r["kind"] == "count" and r["n"] == 3
              and r["value"] == "tiers" for r in reqs))
    check("proper nouns become requirements",
          ("name", "northwind") in kinds and ("name", "faq") in kinds)
    # A REQUIREMENT THAT FIRES ON AN HONEST PAGE IS WORSE THAN ONE THAT
    # MISSES: the loop would spend every round chasing a phantom, and
    # this project has already watched a correction loop get further
    # from the answer the more rounds it ran.
    noise = B.requirements("make me a clean modern responsive page "
                           "that looks really nice")
    check("adjectives and filler produce no requirements",
          not noise, str(noise))

    cap_checks()

    if not GR.find_browser():
        skip("everything that needs a render", "no browser")
        print(f"\nbuild battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    tmp = Path(tempfile.mkdtemp(prefix="ae-build-battery-"))

    print("\n── a build that honours the brief is ACCEPTED")
    good = tmp / "good"
    write(good, page())
    r = B.check(good, BRIEF, verbose=False)
    check("the good page is accepted", r["verdict"] == "ACCEPTED",
          f"{r['verdict']}: {r['why']}")
    check("and nothing is blocking", not r["blocking"],
          str([f["kind"] for f in r["blocking"]]))

    print("\n── and each defect is refused for the RIGHT reason")
    attacks = {
        "two tiers when the brief said three":
            (page(tiers=2), "WRONG COUNT"),
        "the button the user literally named, renamed":
            (page(cta="Get going"), "MISSING FROM BRIEF"),
        "body copy at 2.1:1":
            (page(colour="#C6C6CE"), "LOW CONTRAST"),
        "content that hangs off a phone":
            (page(spill=True), "SPILLS"),
    }
    for name, (html, want) in attacks.items():
        d = tmp / name.replace(" ", "_")[:30]
        write(d, html)
        rr = B.check(d, BRIEF, verbose=False)
        kinds = {f["kind"] for f in rr["blocking"]}
        check(f"refused: {name}",
              rr["verdict"] == "REFUSED" and want in kinds,
              f"{rr['verdict']}, blocking={sorted(kinds)}")

    print("\n── the loop keeps the BEST attempt, not the last")
    # A writer that improves, then WRECKS the page on its final round.
    # A loop that leaves its last attempt on disk hands over the wreck
    # while quoting the best score it ever saw — which is the most
    # dishonest failure available to this design.
    plan = [page(tiers=1, cta="Get going"),   # bad
            page(tiers=3, cta="Get going"),   # better
            page(tiers=3),                    # best
            page(tiers=1, colour="#C6C6CE")]  # wrecked, on purpose

    def writer(prompt, project, rnd):
        write(project, plan[min(rnd, len(plan) - 1)])

    d = tmp / "loop"
    r = B.session(d, writer, BRIEF, rounds=3, verbose=False)
    check("the loop improved across rounds",
          len(r["trail"]) >= 2 and max(r["trail"]) > r["trail"][0],
          str(r["trail"]))
    check("it reports the best score it reached",
          abs(r["score"] - max(r["trail"])) < 1e-6,
          f"{r['score']} vs {max(r['trail'])}")
    final = B.check(d, BRIEF, verbose=False)
    check("and the project ON DISK is that best one",
          abs(final["score"] - r["score"]) < 1e-6,
          f"on disk {final['score']:.3f} vs reported {r['score']:.3f}")
    check("the wrecking round did not survive",
          final["verdict"] == "ACCEPTED", final["verdict"])

    print("\n── a writer that does nothing cannot produce an ACCEPTED")
    # THE VACUOUS PASS, in its purest form for this module: if the
    # writer never writes, there is nothing to accept, and "no findings"
    # must never read as "no problems".
    empty = tmp / "empty"
    empty.mkdir()

    def lazy(prompt, project, rnd):
        pass

    r = B.session(empty, lazy, BRIEF, rounds=2, verbose=False)
    check("an empty project is REFUSED, never accepted",
          r["verdict"] != "ACCEPTED", r["verdict"])

    print("\n── the repair prompt names selectors, not coordinates")
    bad = tmp / "bad"
    write(bad, page(tiers=2, colour="#C6C6CE"))
    rr = B.check(bad, BRIEF, verbose=False)
    prompt = B._repair_prompt(rr, BRIEF)
    check("the repair prompt leads with the blocking faults",
          "WRONG COUNT" in prompt and "LOW CONTRAST" in prompt)
    check("and carries selectors the writer can act on",
          "at: " in prompt, prompt[:120])
    # THIS CHECK WAS A TAUTOLOGY FIRST — it ended in `or True`, which
    # passes whatever the prompt says. A vacuous check inside a battery
    # written about vacuous passes is the worst kind. The real property:
    # every finding that HAS a selector must show it, so the writer can
    # act on all of them rather than on the ones that happened to print.
    listed = [f for f in sorted(rr["findings"],
                                key=lambda f: 0)][:20]
    with_sel = sum(1 for f in listed if f.get("selector"))
    check("every finding that has a selector shows it",
          prompt.count("    at: ") == with_sel,
          f"{prompt.count('    at: ')} shown vs {with_sel} available")

    print(f"\nbuild battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

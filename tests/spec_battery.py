#!/usr/bin/env python3
"""Can Aethron do a change nobody built it to do — and refuse to fake one?

The owner: a user will ask for things no one wired into Aethron, and an agent that only knows
a fixed menu of checks will reject them or check the wrong thing. So aethron_spec writes the
tests first, proves they fail today, has them reviewed blind, then writes code the tests must
pass, removes each part of that code to prove it is needed, and guards everything else.

The request used here — a live character counter under the chat box — has no check of its own
anywhere in Aethron. The model is scripted (tests, review, code), so every scenario is exact and
free: honest work must land, and each way of faking it must be refused for its own reason.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import aethron_figma_grade as GR      # noqa: E402
import aethron_spec as S              # noqa: E402
import interact_battery as IB         # noqa: E402

OK = FAIL = 0
ASK = "Show how many characters I have typed under the chat box, like 5 / 200, updating as I type"
REQS = [{"says": "show how many characters I have typed under the chat box, like 5 / 200",
         "means": "a counter sits just below the text box"},
        {"says": "updating as I type", "means": "the number follows every character typed"}]
SITS = {"name": "counter sits under the chat box", "requirement": 0,
        "steps": [{"expect": "visible", "el": "c01"},
                  {"expect": "relation", "el": "c01", "to": "t01", "is": "below", "gap_max": 24},
                  {"expect": "text", "el": "c01", "equals": "0 / 200"}]}
FOLLOWS = {"name": "counter follows typing", "requirement": 1,
           "steps": [{"do": "type", "el": "t01", "text": "Hello"},
                     {"expect": "text", "el": "c01", "equals": "5 / 200"},
                     {"do": "type", "el": "t01", "text": " you"},
                     {"expect": "text", "el": "c01", "equals": "9 / 200"}]}
GOOD_SPEC = {"understanding": "a live character counter under the chat box", "requirements": REQS,
             "new_ids": ["c01"], "scenarios": [SITS, FOLLOWS], "claims": []}
CLEAN = {"gaps": [], "wrong": []}
BOX_END = 'color:#858795"></textarea>'
COUNTER = ('<p class="t" data-ae-id="c01" style="left:180px;top:336px;font-size:13.00px;color:#3D4250">'
           '0 / 200</p>')
JS = ("(function(){var t=document.querySelector('[data-ae-id=\"t01\"]'),c=document.querySelector('[data-ae-id=\"c01\"]');"
      "if(!t||!c)return;function u(){c.textContent=(t.value||'').length+' / 200';}"
      "t.addEventListener('input',u);u();})();")
GOOD_CODE = {"patches": [{"find": BOX_END, "replace": BOX_END + COUNTER}], "js": JS, "touches": ["c01"],
             "note": "counter under the text box"}


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   ({detail})" if detail else ""), flush=True)


def scripted(specs=(), reviews=(), codes=()):
    """Answer each phase from its own queue, chosen by which prompt arrived — never by a counter."""
    import aethron_agm as AGM
    queues = {"spec": list(specs), "review": list(reviews), "code": list(codes), "agm": []}
    seen = {"spec": [], "review": [], "code": [], "agm": []}

    def call(prompt):
        if prompt.startswith(AGM.AGM_PROMPT[:60]):
            seen["agm"].append(prompt)
            return json.dumps({"verdicts": []})     # the scripted reviewer approves nothing
        phase = ("review" if prompt.startswith(S.CRITIC_PROMPT[:60]) else
                 "code" if prompt.startswith(S.BUILD_PROMPT[:60]) else "spec")
        seen[phase].append(prompt)
        if not queues[phase]:
            raise RuntimeError(f"the script ran out of {phase} replies")
        return json.dumps(queues[phase].pop(0))
    call.seen = seen
    return call


def build(html, work, call, **kw):
    r = S.build(html, ASK, work, call=call, **kw)
    r["seen"] = call.seen
    return r


def said(r, phrase):
    return any(phrase in p for p in r["problems"])


def main():
    if not GR.find_browser():
        print("VERDICT: SKIPPED — no browser to run the tests in; nothing here is proven")
        return 0
    work = Path(tempfile.mkdtemp(prefix="ae-spec-battery-"))
    html = IB.page()
    check("the fixture page loads with its chat box and toggling chips", html is not None and 'data-ae-id="t01"' in html)
    if not html:
        return 1

    print("\n── a change nobody built Aethron for, done honestly, lands")
    r = build(html, work, scripted([GOOD_SPEC], [CLEAN], [GOOD_CODE]))
    check("a live character counter under the chat box is APPLIED", r["verdict"] == "APPLIED", S.report(r))
    check("  ...its tests were first shown to fail on the page as it was",
          sum("fails on the page as it is, as it should" in p for p in r["proof"]) == 2, str(r["proof"]))
    check("  ...an independent reviewer found no gap",
          any("reviewer, shown only the request and the tests, found no gap" in p for p in r["proof"]))
    check("  ...both tests pass on the changed page", sum(p.startswith("ok   '") and "passes" in p
                                                       for p in r["proof"]) == 2, str(r["proof"]))
    check("  ...taking away the script or the new element breaks a test, so both are needed",
          any("without the script" in p and "needed" in p for p in r["proof"])
          and any("without patch 1" in p and "needed" in p for p in r["proof"]), str(r["proof"]))
    check("  ...and nothing outside the counter changed on screen",
          any("nothing outside the named elements changed" in p for p in r["proof"]), str(r["proof"]))

    print("\n── tests that prove nothing are refused before any code is written")
    lazy = dict(GOOD_SPEC, new_ids=[], scenarios=[
        dict(SITS, steps=[{"expect": "visible", "el": "t01"}]), dict(FOLLOWS, steps=[{"expect": "exists", "el": "t01"}])])
    # Three lazy answers for three tries: the model must RUN OUT OF IDEAS, not the script run
    # out of replies — a script that dries up is a provider that stopped answering, which is
    # NOT ASKED, and this scenario is about tests that prove nothing being refused.
    r = build(html, work, scripted([lazy, lazy, lazy], [CLEAN, CLEAN, CLEAN], []))
    check("tests that already pass on the page as it is are refused",
          r["verdict"] == "REFUSED" and said(r, "already passes on the page as it is"), str(r["problems"]))
    check("  ...and no code was asked for", not r["seen"]["code"])
    half = dict(GOOD_SPEC, requirements=REQS[:1], scenarios=[SITS])
    r = build(html, work, scripted([half, half], [], []))
    check("tests that leave 'updating as I type' out are refused, naming the words",
          said(r, "leave parts of the request out") and not r["seen"]["code"], str(r["problems"]))
    stray = json.loads(json.dumps(GOOD_SPEC))
    stray["scenarios"][0]["steps"][1]["to"] = "t99"
    r = build(html, work, scripted([stray], [], []), spec_tries=1)
    check("a test about an element that does not exist is refused", said(r, "t99"), str(r["problems"]))
    broken_keep = dict(GOOD_SPEC, scenarios=[SITS, FOLLOWS, {"name": "chips stay put", "keeps": True,
                                                              "steps": [{"expect": "hidden", "el": "s02"}]}])
    r = build(html, work, scripted([broken_keep], [CLEAN], []), spec_tries=1)
    check("a 'keeps' test that already fails today is refused", said(r, "already fails on the page as it is"),
          str(r["problems"]))

    print("\n── the reviewer and the tests send work back, and the fixed work lands")
    weak = json.loads(json.dumps(GOOD_SPEC))
    weak["scenarios"][1]["steps"] = [{"do": "type", "el": "t01", "text": "Hello"},
                                     {"expect": "text", "el": "c01", "contains": "/ 200"}]
    gap = {"gaps": [{"words": "updating as I type", "why": "it never checks the number changes"}], "wrong": []}
    r = build(html, work, scripted([weak, GOOD_SPEC], [gap, CLEAN], [GOOD_CODE]))
    check("a reviewer's gap sends the tests back, and the stronger tests land",
          r["verdict"] == "APPLIED" and any("reviewer says no test checks 'updating as I type'" in p
                                            for p in r["attempts"][0]["problems"]), S.report(r))
    frozen = dict(GOOD_CODE, js=JS.replace("t.addEventListener('input',u);", ""))
    r = build(html, work, scripted([GOOD_SPEC], [CLEAN], [frozen, GOOD_CODE]))
    check("code whose counter never moves fails its test, and the fixed code lands",
          r["verdict"] == "APPLIED" and len(r["seen"]["code"]) == 2 and "0 / 200" in r["seen"]["code"][1],
          S.report(r))

    print("\n── code that passes the tests but is not the change is refused")
    extra = dict(GOOD_CODE, patches=GOOD_CODE["patches"] + [
        {"find": 'data-ae-id="s00" style="', "replace": 'data-ae-id="s00" data-note="counter" style="'}])
    r = build(html, work, scripted([GOOD_SPEC], [CLEAN], [extra]), code_tries=1)
    check("an extra part no test needs is refused, naming it",
          r["verdict"] == "REFUSED" and said(r, "without patch 2") and said(r, "every test still passes"),
          str(r["problems"]))
    dimmer = dict(GOOD_CODE, js=JS.replace("u();})();",
                                            "u();document.querySelector('[data-ae-id=\"s04\"]').style.opacity='0.3';})();"))
    r = build(html, work, scripted([GOOD_SPEC], [CLEAN], [dimmer]), code_tries=1)
    check("a counter that passes every test but quietly dims the IOS button is refused",
          r["verdict"] == "REFUSED" and said(r, "s04 op changed"), str(r["problems"]))
    ghost = dict(GOOD_CODE, patches=[{"find": BOX_END, "replace": BOX_END}])
    r = build(html, work, scripted([GOOD_SPEC], [CLEAN], [ghost]), code_tries=1)
    check("code that never creates the promised counter is refused", said(r, "c01 was promised"), str(r["problems"]))
    print("\n── a provider that did not answer is not a request that was refused")

    def dead(prompt):
        raise RuntimeError("provider said 503: the model is overloaded")
    r = S.build(html, ASK, work, call=dead)
    check("a model that cannot be reached gives NOT ASKED, never REFUSED",
          r["verdict"] == "NOT ASKED" and any("could not ask the model" in p for p in r["problems"]),
          r["verdict"] + " " + str(r["problems"])[:120])

    def dies_writing_code(prompt):
        import aethron_agm as AGM
        if prompt.startswith(AGM.AGM_PROMPT[:60]):
            return json.dumps({"verdicts": []})
        if prompt.startswith(S.BUILD_PROMPT[:60]):
            raise RuntimeError("provider said 503: the model is overloaded")
        return json.dumps(GOOD_SPEC if not prompt.startswith(S.CRITIC_PROMPT[:60]) else CLEAN)
    r = S.build(html, ASK, work, call=dies_writing_code)
    check("  ...and the same when it dies after the tests were written",
          r["verdict"] == "NOT ASKED", r["verdict"] + " " + str(r["problems"])[:120])

    print(f"\nspec battery: {OK} ok, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

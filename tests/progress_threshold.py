#!/usr/bin/env python3
"""Progress reports on demand (reporting.py), offline.

`due`: an assessment is due when none exists, when the documentation or TauCetiRoadmap's main moved,
when the last plan's "next qualifies at" has passed, or when our open reports want a look; not while a
failed round's backoff runs or while MAX_OPEN reports are still landing, and then it says when to wake.

`shepherd`, against a stubbed GitHub, follows the rules the hand-run lander learned on 2026-09-27: a
report behind main is updated only once main builds (and not when the gate has already landed it); a
green build the gate is quiet about is re-gated at most twice, then handed to a person; a build that
fails on an up-to-date branch while main builds is handed to a person at once; the gate's first-pass
"not completed" and refusals about an earlier head are not verdicts; a report that is no longer open
is reported as landed or closed.

And the round's survey: a fixer or reviewer no longer pays for a progress due-check, and a worker that
only writes on-demand reports reads no review state. Exit 0 = all hold.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import interaction  # noqa: E402
from tauceti_worker import reporting as R  # noqa: E402
from tauceti_worker.survey import Counters  # noqa: E402
import importlib  # noqa: E402

SV = importlib.import_module("tauceti_worker.survey")

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-progress-threshold-"))
STATE = TMP / "state"
STATE.mkdir()
INC = TMP / "incidents"
INC.mkdir()
interaction.incidents_dir = lambda: INC
R.record_incident = interaction.record_incident
counters = Counters(SimpleNamespace(state=STATE))
paths = R.Paths(STATE)
NOW = time.time()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ----------------------------------------------------------------------------- due

probes = {"docs": "d" * 40, "roadmap": "r" * 40}
R.probe_docs = lambda state, argv_fn: probes["docs"]
R.probe_roadmap = lambda state: probes["roadmap"]


def due():
    return R.due(STATE, counters, None)


ok, why, _ = due()
check("with no assessment yet, an assessment is due", ok and "assessed" in why, why)

write(paths.scan, {"at": NOW, "to_sha": "d" * 40, "roadmap_head": "r" * 40, "chosen": None,
                   "next_qualifies_at": R._iso(NOW + 7200), "summary": "no roadmap qualifies; next: A"})
ok, why, wake = due()
check("nothing moved and nothing qualifies: not due, and says so", not ok and why.startswith("no roadmap qualifies"), why)
check("…waking by the next roadmap probe at the latest", wake is not None and wake <= NOW + R.ROADMAP_PROBE_TTL + 5, wake)

probes["docs"] = "e" * 40
ok, why, _ = due()
check("a new documentation build makes it due", ok and "eeeeeee" in why, why)
probes["docs"] = "d" * 40
probes["roadmap"] = "s" * 40
ok, why, _ = due()
check("TauCetiRoadmap main moving makes it due", ok and "moved" in why, why)
probes["roadmap"] = "r" * 40

write(paths.scan, {**json.loads(paths.scan.read_text()), "next_qualifies_at": R._iso(NOW - 60)})
ok, why, _ = due()
check("the table's next-qualifies time passing makes it due", ok and "qualifies by now" in why, why)
write(paths.scan, {**json.loads(paths.scan.read_text()), "next_qualifies_at": None, "chosen": "IntegralLattices"})
ok, why, _ = due()
check("a roadmap the last plan chose (not yet written) keeps it due", ok and "IntegralLattices" in why, why)
write(paths.scan, {**json.loads(paths.scan.read_text()), "chosen": None, "at": NOW - 2 * R.SCAN_MAX_AGE})
ok, why, _ = due()
check("an hour-old assessment is redone even if no probe answers", ok and "hour" in why, why)
write(paths.scan, {**json.loads(paths.scan.read_text()), "at": NOW})

counters.write("progress-err", 2)
counters.write("progress-err-ts", int(NOW))
ok, why, wake = due()
check("after two failures it waits 30 min, not for ever", not ok and wake and abs(wake - (NOW + 1800)) < 5, (why, wake))
counters.write("progress-err-ts", int(NOW - 1801))
ok, why, _ = due()
check("…and is not held back once the wait is over", "failed" not in why, why)
counters.write("progress-err", 0)

write(paths.landing, {"prs": {"600": {"area": "A"}}, "shepherded_at": NOW - R.SHEPHERD_EVERY - 1})
ok, why, _ = due()
check("an open report makes it due every few minutes", ok and "landing" in why, why)
write(paths.landing, {"prs": {"600": {"area": "A"}, "601": {"area": "B"}}, "shepherded_at": NOW})
ok, why, wake = due()
check("MAX_OPEN reports landing: no new report, wake at the next look",
      not ok and "waiting for 2" in why and abs((wake or 0) - (NOW + R.SHEPHERD_EVERY)) < 5, (why, wake))
paths.landing.unlink()

# The survey's due-check leaves the wake-up time for the loop.
R.STRATEGY = "threshold"
nq = NOW + 1234
write(paths.scan, {"at": NOW, "to_sha": "d" * 40, "roadmap_head": "r" * 40, "chosen": None,
                   "next_qualifies_at": R._iso(nq), "summary": "no roadmap qualifies"})
(STATE / SV.NEXT_ELIGIBLE_COUNTER).unlink(missing_ok=True)
got = SV.progress_due(SimpleNamespace(state=STATE), counters)
hint = counters.read(SV.NEXT_ELIGIBLE_COUNTER)
check("progress_due in threshold mode answers from reporting.due", got == (False, "no roadmap qualifies"), got)
check("…and leaves the loop its wake-up time", 0 < hint <= int(NOW + R.ROADMAP_PROBE_TTL) + 5, hint)

# ----------------------------------------------------------------------------- shepherd


class GH:
    """A stub GitHub: `answers` maps an argv prefix string to stdout (JSON after --jq), and every
    call is recorded."""

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.fail = set()

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        joined = " ".join(argv)
        for key, out in self.answers.items():
            if key in joined:
                rc = 1 if key in self.fail else 0
                return subprocess.CompletedProcess(argv, rc, stdout=out if isinstance(out, str) else json.dumps(out), stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="stub: no answer")

    def writes(self):
        return [c for c in self.calls if ("-X" in c and "PUT" in c) or c[1:3] == ["workflow", "run"]]


def stub(prs, *, behind=0, builds=None, main_red=False, landed=()):
    g = GH()
    g.answers = {
        "pr list": prs,
        "compare/main...": {"b": behind},
        "check-runs": builds if builds is not None else [],
        "commits/main": {"sha": "m" * 40},
        "ci.yml/runs": {"c": "failure" if main_red else "success", "s": "m" * 40},
        "commits?sha=main": [f"progress: A report\n\nCloses #{n}." for n in landed],
        "update-branch": "{}",
        "workflow run": "",
    }
    R.gh_run = g
    return g


def pr(n=600, head="h" * 40, comments=()):
    return {"number": n, "headRefName": "progress/aaaaaaa-bbbbbbb/A", "headRefOid": head,
            "url": f"https://github.com/x/pull/{n}", "comments": list(comments)}


def reset():
    paths.landing.unlink(missing_ok=True)
    for f in INC.glob("*.json"):
        f.unlink()


def ago(minutes):
    return R._iso(time.time() - 60 * minutes)


reset()
g = stub([pr()], behind=3)
acted, open_, notes = R.shepherd(STATE)
put = [c for c in g.writes() if "PUT" in c]
check("a report behind a green main is updated onto it", acted and len(put) == 1 and "expected_head_sha=" + "h" * 40 in put[0], notes)
acted, _o, notes = R.shepherd(STATE)
check("…once: the same head and main is not updated twice", not acted and len([c for c in g.writes() if "PUT" in c]) == 1, notes)

reset()
g = stub([pr()], behind=3, main_red=True)
acted, _o, notes = R.shepherd(STATE)
check("a report behind a red main waits for main", not acted and not g.writes() and "does not build" in notes[0], notes)

reset()
g = stub([pr()], behind=1, landed=[600])
acted, _o, notes = R.shepherd(STATE)
check("a report the gate has landed (still closing) is not updated", not g.writes() and "landed" in notes[0], notes)

reset()
g = stub([pr()], builds=[{"status": "in_progress", "conclusion": None, "completed_at": None}])
acted, _o, notes = R.shepherd(STATE)
check("a running build is waited for", not acted and "build running" in notes[0], notes)

reset()
g = stub([pr()], builds=[{"status": "completed", "conclusion": "success", "completed_at": ago(3)}])
acted, _o, notes = R.shepherd(STATE)
check("a fresh green build is left to the gate's own run", not acted and not g.writes() and "shortly" in notes[0], notes)

reset()
quiet = [{"status": "completed", "conclusion": "success", "completed_at": ago(30)}]
g = stub([pr()], builds=quiet)
acted, _o, notes = R.shepherd(STATE)
runs = [c for c in g.writes() if c[1:3] == ["workflow", "run"]]
check("a gate quiet 20 min after a green build is asked again", acted and len(runs) == 1 and "pr=600" in runs[0], notes)
acted, _o, notes = R.shepherd(STATE)
check("…not again straight away", not acted and len([c for c in g.writes() if c[1:3] == ["workflow", "run"]]) == 1, notes)
land = json.loads(paths.landing.read_text())
land["prs"]["600"]["regated_at"] = time.time() - R.REGATE_AFTER - 1
land["prs"]["600"]["regates"] = R.MAX_REGATES
paths.landing.write_text(json.dumps(land))
acted, _o, notes = R.shepherd(STATE)
inc = list(INC.glob("progress-stuck-*.json"))
check("after the re-asks it goes to a person, not the gate", not acted and len(inc) == 1 and not
      [c for c in g.writes() if c[1:3] == ["workflow", "run"]][1:], (notes, inc))
R.shepherd(STATE)
check("…once per head", json.loads(inc[0].read_text()).get("count") == 1, inc[0].read_text())

reset()
g = stub([pr()], builds=[{"status": "completed", "conclusion": "failure", "completed_at": ago(5)}])
acted, _o, notes = R.shepherd(STATE)
check("a failed build on an up-to-date branch while main builds goes to a person",
      not g.writes() and len(list(INC.glob("progress-stuck-*.json"))) == 1, notes)
reset()
g = stub([pr()], builds=[{"status": "completed", "conclusion": "failure", "completed_at": ago(5)}], main_red=True)
acted, _o, notes = R.shepherd(STATE)
check("…but with main red it waits for main instead", not list(INC.glob("*.json")) and "waiting for main" in notes[0], notes)

reset()
first_pass = {"body": "The automated progress gate did not merge this pull request.\n\n```\nbuild is 'queued' on hhhhhhh, not completed\n```", "createdAt": ago(1)}
g = stub([pr(comments=[first_pass])], builds=[{"status": "completed", "conclusion": "success", "completed_at": ago(2)}])
acted, _o, notes = R.shepherd(STATE)
check("the gate's first-pass 'not completed' is not a verdict", not acted and "shortly" in notes[0], notes)
old_head = {"body": "did not merge\n\n```\nhead 1234567 is behind main by 2\n```", "createdAt": ago(1)}
check("a refusal about an earlier head is not about this one",
      R.latest_refusal([old_head], "h" * 40, None) is None and R.latest_refusal([old_head], "1234567" + "0" * 33, None))
real = {"body": "did not merge\n\n```\nPROGRESS.md was rewritten, not appended to\n```", "createdAt": ago(1)}
check("a real refusal after the build is read", R.latest_refusal([first_pass, real], "h" * 40, time.time() - 600)
      == "PROGRESS.md was rewritten, not appended to")
check("…but not one from before the build finished", R.latest_refusal([real], "h" * 40, time.time()) is None)

reset()
write(paths.landing, {"prs": {"600": {"area": "A"}, "599": {"area": "B"}}})
g = stub([], landed=[600])
acted, open_, notes = R.shepherd(STATE)
check("reports no longer open are reported landed or closed, and forgotten",
      open_ == [] and any("#600 (A) landed" in n for n in notes) and any("#599 (B) was closed" in n for n in notes)
      and json.loads(paths.landing.read_text())["prs"] == {}, notes)

# ----------------------------------------------------------------------------- the round's survey

seen = []
saved = (tc.work_units.survey, tc.work_units.dispatch)
tc.work_units.survey = lambda *_a, **kw: seen.append(kw) or tc.Survey(worker_id="t")
tc.work_units.dispatch = lambda *a, **k: 0
try:
    worker = SimpleNamespace(cfg=None, gh=None, rs=None, counters=None)
    for only in (["fix", "fix-ci", "rebase"], ["review"], ["progress"], []):
        try:
            tc.work_units.run_round(worker, SimpleNamespace(only=only, dry_run=True))
        except Exception:  # noqa: BLE001 - the survey stub has nothing to do; only its arguments matter here
            pass
finally:
    tc.work_units.survey, tc.work_units.dispatch = saved
check("a fixer does not run the progress due-check", seen[0] == {"deep": True, "progress_check": False}, seen[0])
check("nor does a reviewer", seen[1] == {"deep": True, "progress_check": False}, seen[1])
check("an on-demand progress worker reads no review state", seen[2] == {"deep": False, "progress_check": True}, seen[2])
check("an unrestricted worker still does everything", seen[3] == {"deep": True, "progress_check": True}, seen[3])

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

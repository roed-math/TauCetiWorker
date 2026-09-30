#!/usr/bin/env python3
"""The on-demand progress round end to end, offline (work_units._do_progress_threshold).

`tauceti-progress` is a fake script, the writing agent a stub that writes the two bodies, and the
shepherd a stub. Holds: nothing qualifying is a quiet no-progress round that records the assessment
(not a failure); a chosen roadmap is written with the worker's additions to the prompt (the source
snapshot and the check script), a failed check gets exactly one repair pass, `apply`'s pull request is
recorded for the shepherd, and the error streak resets; a check that still fails, or a failed `facts`,
is a counted failure; with MAX_OPEN reports still landing, nothing is planned. Exit 0 = all hold.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import reporting as R  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import Die, NoProgress  # noqa: E402
from tauceti_worker.survey import Counters  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-progress-round-"))
STATE, LOGS = TMP / "state", TMP / "logs"
STATE.mkdir()
LOGS.mkdir()
ROADMAP = TMP / "roadmap"
subprocess.run(["git", "init", "-q", "-b", "main", str(ROADMAP)], check=True)
subprocess.run(["git", "-C", str(ROADMAP), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q",
                "--allow-empty", "-m", "init"], check=True)

FAKE = TMP / "fake-progress"
FAKE.write_text(r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
cmd = args[0]
opt = lambda name: args[args.index(name) + 1] if name in args else None
log = os.environ["FAKE_LOG"]
open(log, "a").write(json.dumps(args + ["cache=" + os.environ.get("TAUCETI_DOCS_CACHE", "")]) + "\n")
mode = os.environ.get("FAKE_MODE", "")
if cmd == "plan":
    table = {"to_sha": "d" * 40, "rows": [{"area": "A", "prs": 12, "qualifies": mode != "notdue",
             "qualifies_from": None, "note": "N+T = 15.0 > 10"}], "chosen": None if mode == "notdue" else "A",
             "next_qualifies_at": "2026-10-01T00:00:00+00:00"}
    open(opt("--table"), "w").write(json.dumps(table))
    if mode == "notdue":
        print("not due: no roadmap qualifies yet: 1 have new pull requests", file=sys.stderr)
        sys.exit(75)
    open(opt("--out"), "w").write(json.dumps({"roadmap": "A", "prs": list(range(12)), "from_sha": "a" * 40,
        "to_sha": "d" * 40, "reason": "A has 12 PR(s)", "status_path": "TauCetiRoadmap/A/STATUS.md",
        "progress_path": "TauCetiRoadmap/A/PROGRESS.md"}))
elif cmd == "facts":
    if mode == "factsfail":
        print("facts: the site is redeploying", file=sys.stderr)
        sys.exit(1)
    open(opt("--out"), "w").write(json.dumps({"declarations": []}))
elif cmd == "prompt":
    print("Write __ROADMAP__ into __STATUS_OUT__ and __SECTION_OUT__; facts in __FACTS_FILE__.")
elif cmd == "check":
    body = open(opt("--status-body")).read()
    if "BAD" in body:
        print("FAIL: the status prose is 900 words; the limit is 750\nNOT OK")
        sys.exit(1)
    print("status prose: 100 words (at most 750)\nOK")
elif cmd == "apply":
    print("opened https://github.com/TauCetiProject/TauCetiRoadmap/pull/777")
''')
FAKE.chmod(0o755)
os.environ["FAKE_LOG"] = str(TMP / "fake.log")
W.progress_argv = lambda state, *a: [sys.executable, str(FAKE), *a]
W.prepare_checkout = lambda cfg: True
W._progress_roadmap_clone = lambda w: ROADMAP
R.snapshot_source = lambda checkout, sha, dest: (dest.mkdir(parents=True, exist_ok=True) or dest)
W.gate_mod.admit_or_log = lambda *a, **k: SimpleNamespace(child_env=lambda: {})
W.gate_mod.current = lambda: SimpleNamespace(record=lambda *a, **k: None)
open_now = []
R.shepherd = lambda state: (False, list(open_now), [])

agent_runs = []
drafts = []


def fake_agent(cwd, prompt, profile, logdir):
    agent_runs.append(prompt)
    draft = drafts.pop(0) if drafts else "good"
    (cwd / "status-body.md").write_text("status BAD\n" if draft == "bad" else "status fine\n")
    (cwd / "section-body.md").write_text("section\n")
    return 0


W.run_agent_host = fake_agent
claims = []
w = SimpleNamespace(cfg=SimpleNamespace(state=STATE, checkout=TMP / "checkout", logdir=LOGS),
                    counters=Counters(SimpleNamespace(state=STATE)),
                    claims=SimpleNamespace(begin_global_work=lambda key: claims.append(key) or 0,
                                           release=lambda: claims.append("release")))
opts = SimpleNamespace(work_model="opus", agent_name="claude")
paths = R.Paths(STATE)


def calls():
    p = TMP / "fake.log"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def run():
    try:
        return W._do_progress_threshold(w, opts), None
    except (NoProgress, Die) as exc:
        return None, exc


# 1) Nothing qualifies.
os.environ["FAKE_MODE"] = "notdue"
rc, exc = run()
scan = json.loads(paths.scan.read_text())
check("nothing qualifying is a no-progress round, not a failure",
      isinstance(exc, NoProgress) and w.counters.read("progress-err") == 0, repr(exc))
check("…that records the assessment for the due-check", scan["to_sha"] == "d" * 40 and scan["roadmap_head"]
      and scan["summary"].startswith("no roadmap qualifies") and not agent_runs, scan)
check("…planned with the threshold strategy, a table and a label cache",
      any(c[0] == "plan" and "threshold" in c and "--table" in c and "--label-cache" in c for c in calls()), calls())

cache = str(R.Paths(STATE).docs_cache)
check("plan and facts read this worker's own documentation cache",
      all(c[-1] == "cache=" + cache for c in calls() if c[0] in ("plan", "facts")), calls())

# 2) A roadmap qualifies; the first draft fails its check and the repair pass fixes it.
os.environ["FAKE_MODE"] = ""
w.counters.write("progress-err", 1)
drafts[:] = ["bad", "good"]
rc, exc = run()
check("a qualifying roadmap is written and opened", rc == 0 and exc is None, repr(exc))
check("the writing prompt carries the check script and the source copy",
      "check.sh" in agent_runs[0] and "/work/src" in agent_runs[0] and "Before you stop" in agent_runs[0], agent_runs[0][-600:])
script = (STATE / "progress" / "work" / "check.sh").read_text()
check("…and the check script runs `tauceti-progress check` on this round's files, with its cache",
      " check " in script and "--status-body" in script and "status-body.md" in script
      and "TAUCETI_DOCS_CACHE=" + cache in script, script)
check("…and the check the round runs itself uses the same cache",
      all(c[-1] == "cache=" + cache for c in calls() if c[0] == "check"), [c for c in calls() if c[0] == "check"])
check("a failed check gets exactly one repair pass, told what failed",
      len(agent_runs) == 2 and "900 words" in agent_runs[1], len(agent_runs))
land = json.loads(paths.landing.read_text())
check("the opened pull request is handed to the shepherd", land["prs"]["777"]["area"] == "A", land)
check("…and the error streak is reset", w.counters.read("progress-err") == 0)

# 3) A check that still fails after the repair pass.
agent_runs.clear()
drafts[:] = ["bad", "bad"]
rc, exc = run()
check("a check that still fails is a counted failure, with nothing applied",
      isinstance(exc, Die) and "check" in str(exc) and w.counters.read("progress-err") == 1
      and w.counters.read("progress-err-ts") > 0 and [c[0] for c in calls()][-1] == "check", repr(exc))

# 4) facts refuses (a documentation deploy mid-run).
os.environ["FAKE_MODE"] = "factsfail"
agent_runs.clear()
rc, exc = run()
check("a failed extraction is a counted failure and no prose is written",
      isinstance(exc, Die) and w.counters.read("progress-err") == 2 and not agent_runs, repr(exc))

# 5) MAX_OPEN reports still landing.
os.environ["FAKE_MODE"] = ""
before = len(calls())
open_now[:] = list(range(R.MAX_OPEN))
rc, exc = run()
check("with MAX_OPEN reports landing nothing is planned, and no claim is taken",
      isinstance(exc, NoProgress) and len(calls()) == before and claims[-1] == "release", (repr(exc), claims[-2:]))
check("every plan ran under the progress claim, released after", claims.count("progress") == 4
      and claims.count("release") == 4, claims)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

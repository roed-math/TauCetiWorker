#!/usr/bin/env python3
"""A round that dies before the agent starts must not spend the PR's fix budget, and a spent budget
must reach the attention list.

TauCetiProject/TauCeti#11891 had no `refs/pull/N/head`, so bubble prepared no Lake mirrors and every
fix round died in the pre-agent `lake exe cache get` (the auth proxy refused Lake's own Mathlib fetch).
Six fixers on two fleets spent all their attempts in seven hours without one agent turn, and the PR
then sat for two days with nothing in the fleet's view. Exit 0 = every case agrees; 1 = a mismatch.
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
import importlib

import tauceti_worker as tc

agents = tc.agents
wu = tc.work_units
survey = importlib.import_module("tauceti_worker.survey")  # `tc.survey` is the function
interaction = tc.interaction
SENTINEL = tc.constants.PRE_AGENT_SETUP_FAILED
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


# --- the bootstrap prints the sentinel and stops when the Mathlib cache step fails -----------------
STUBS = "timeout(){ shift; \"$@\"; }; "


def bootstrap(lake_body):
    cmd = agents.bubble_work_cmd("echo AGENT-STARTED")
    return subprocess.run(["bash", "-c", f"lake(){{ {lake_body}; }}; {STUBS}{cmd}"],
                          capture_output=True, text=True)


r = bootstrap("echo lake-failed >&2; return 1")
check("failed setup exits non-zero", r.returncode != 0, True)
check("failed setup prints the sentinel", SENTINEL in r.stderr.splitlines(), True)
check("failed setup never starts the agent", "AGENT-STARTED" in r.stdout, False)
r = bootstrap("return 0")
check("good setup starts the agent", r.stdout.strip().splitlines()[-1:], ["AGENT-STARTED"])
check("good setup prints no sentinel", SENTINEL in r.stderr, False)


# --- classification: only a sentinel with no agent event is a setup failure ------------------------
INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s", "model": "opus"})


def run(lines, rc):
    script = f"import sys; print({chr(10).join(lines)!r}); sys.exit({rc})"
    with tempfile.TemporaryDirectory() as td:
        os.environ[tc.runtime_status.STATUS_ENV] = str(Path(td) / "status.json")
        agents.run_agent_proc([sys.executable, "-c", script], env=dict(os.environ), logdir=Path(td),
                              label="agent-claude", provider="claude")
    return agents.take_last_agent_setup_failure(), agents.take_last_agent_infra_failure()


setup, infra = run(["Creating bubble", "error: mathlib: failed to fetch", SENTINEL, "Session terminated"], 1)
check("sentinel without agent events is a setup failure", bool(setup), True)
check("a setup failure is not a provider outage", infra, None)
setup, _ = run([INIT, f"the agent quoting: {SENTINEL}", SENTINEL], 1)
check("an agent that started is never a setup failure", setup, None)
setup, _ = run(["error: build failed"], 1)
check("no sentinel, no setup failure", setup, None)
setup, infra = run(["API Error: 529 Overloaded"], 1)
check("a provider outage stays a provider outage", (setup, infra), (None, "provider returned 529"))
setup, _ = run([SENTINEL], 0)
check("a zero exit is never classified", setup, None)
os.environ.pop(tc.runtime_status.STATUS_ENV, None)


# --- the shift: refund the stage counters, charge the head's setup budget, do not pause ------------
CAND = SimpleNamespace(pr=11891, head="d3a8b84f04ae" + "0" * 28)
KEYS = ("fix-11891-d3a8b84f04ae",)
SETUP = "setup-11891-d3a8b84f04ae"

with tempfile.TemporaryDirectory() as state:
    w = SimpleNamespace(counters=wu.Counters(SimpleNamespace(state=Path(state))))
    w.counters.write(KEYS[0], 1)
    agents._LAST_AGENT_SETUP_FAILURE = None
    wu._shift_setup_failure(w, CAND, "fix", KEYS)
    check("an agent failure stays charged", (w.counters.read(KEYS[0]), w.counters.read(SETUP)), (1, 0))

    agents._LAST_AGENT_SETUP_FAILURE = "pre-agent setup failed (Mathlib cache)"
    wu._shift_setup_failure(w, CAND, "fix", KEYS)  # must not raise NoProgress
    check("a setup failure moves the charge", (w.counters.read(KEYS[0]), w.counters.read(SETUP)), (0, 1))
    wu._shift_setup_failure(w, CAND, "fix", KEYS)
    check("one failure, one shift", w.counters.read(SETUP), 1)

    # The survey retires the head at the cap, with a reason that names the setup.
    p = SimpleNamespace(number=CAND.pr, head_oid=CAND.head)
    check("under the cap the head stays actionable", survey._setup_spent(w.counters, p), None)
    w.counters.write(SETUP, tc.constants.MAX_SETUP_FAILURES)
    why = survey._setup_spent(w.counters, p) or ""
    check("at the cap the head is retired", "before the agent started" in why, True)
    moved = SimpleNamespace(number=CAND.pr, head_oid="f" * 40)
    check("a new head has a fresh setup budget", survey._setup_spent(w.counters, moved), None)
agents._LAST_AGENT_SETUP_FAILURE = None


# --- budget-spent incidents: written once, acknowledgement respected, cleared when stale -----------
with tempfile.TemporaryDirectory() as root:
    interaction.incidents_dir = lambda: Path(root) / "incidents"
    head = "a" * 40
    interaction.note_budget_spent("fix", 11891, head, "fix attempts are spent (3/3) — needs a human")
    interaction.note_budget_spent("fix", 11891, head, "fix attempts are spent (3/3) — needs a human")
    recs = interaction.list_incidents()
    check("one record per spent head", len(recs), 1)
    check("seen twice, written once", recs[0].get("count"), 1)
    check("the record names the PR", (recs[0].get("pr"), recs[0].get("stage")), (11891, "fix"))

    acked = Path(root) / "incidents" / "acked"
    acked.mkdir()
    live = next((Path(root) / "incidents").glob("budget-spent-*.json"))
    live.rename(acked / live.name)
    interaction.note_budget_spent("fix", 11891, head, "again")
    check("an acknowledged record stays acknowledged", interaction.list_incidents(), [])

    interaction.note_budget_spent("fix-ci", 12000, "b" * 40, "spent")
    interaction.note_budget_spent("rebase", 12001, "c" * 40, "spent")
    interaction.clear_stale_budget_spent({12000: "b" * 40, 12001: "d" * 40})
    left = sorted(r["pr"] for r in interaction.list_incidents())
    check("closed or moved PRs are cleared, current ones kept", left, [12000])

print("\nFAIL" if fails else "\nall setup-failure budget checks passed")
sys.exit(1 if fails else 0)

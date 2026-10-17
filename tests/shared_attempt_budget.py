#!/usr/bin/env python3
"""A fleet spends one attempt budget per PR head, not one per worker.

2026-10-17: with every author and fixer slot fixing (the fleet's `up.flex`), seven workers could each
spend MAX_FIX_ATTEMPTS on a head none of them can change. With TAUCETI_GATE_DIR set, Counters keeps the
branch-writing stages' budgets (fix, fix-ci, rebase and its checkpoints, bump, lint-repair) and their
provider-outage refunds in the gate store; setup failures and everything else stay per worker, and a
worker outside a fleet keeps its own as before. Checked here:
  * two worker ids share those keys and nothing else; without a gate dir each keeps its own;
  * concurrent increments from several processes all count;
  * a round hands back only its own charges (give_back), never a peer's;
  * losing the branch claim to a peer hands the charge back;
  * a second worker's survey suppresses a head the first one spent, reporting N/M attempts, and the
    budget-spent incident is written once.
Exit 0 = all checks hold; 1 = a mismatch.
"""

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import interaction, work_units  # noqa: E402
from tauceti_worker.constants import MAX_CI_ATTEMPTS  # noqa: E402
from tauceti_worker.github import GitHub  # noqa: E402

survey_mod = importlib.import_module("tauceti_worker.survey")
Counters = survey_mod.Counters

TMP = Path(tempfile.mkdtemp(prefix="tauceti-shared-attempts-"))
HEAD = "c" * 40
GATE = TMP / "gate"
os.environ.pop("TAUCETI_GATE_DIR", None)
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


def counters(wid, gate=GATE):
    with patch.dict(os.environ, {"TAUCETI_GATE_DIR": str(gate)} if gate else {}):
        return Counters(SimpleNamespace(state=TMP / "state" / wid))


# --- which keys are shared ------------------------------------------------------------------------
a, b = counters("w1"), counters("w2")
shared = ["fix-7-cccccccccccc", "ci-7-cccccccccccc", "ci-pr-7", "rebase-pr-7", "rebase-pr-7-0123456789abcdef0123",
          "bump-7-cccccccccccc", "bump-pr-7", "lint-repair-7-cccccccccccc", "lint-repair-pr-7", "infra-fix-ci-7"]
own = ["setup-7-cccccccccccc", "review-err-7", "review-contest-7", "progress-err", "infra-review-7"]
for key in shared + own:
    a.incr(key)
check("the branch stages' budgets and refunds are shared", all(b.read(k) == 1 for k in shared),
      str({k: b.read(k) for k in shared}))
check("setup failures and every other counter stay per worker", all(b.read(k) == 0 for k in own),
      str({k: b.read(k) for k in own}))
check("…and live in the gate store's attempts/ directory", (GATE / "attempts" / "fix-7-cccccccccccc").is_file())

alone_a, alone_b = counters("solo1", gate=None), counters("solo2", gate=None)
alone_a.incr("fix-8-cccccccccccc")
check("without a gate dir each worker keeps its own budget",
      alone_a.read("fix-8-cccccccccccc") == 1 and alone_b.read("fix-8-cccccccccccc") == 0)
check("…in its own state directory, as before", (TMP / "state" / "solo1" / "fix-8-cccccccccccc").is_file())

# --- concurrent increments --------------------------------------------------------------------------
script = (
    "import sys; from pathlib import Path; from types import SimpleNamespace;"
    f"sys.path.insert(0, {str(REPO)!r}); from tauceti_worker.survey import Counters;"
    "c = Counters(SimpleNamespace(state=Path(sys.argv[1])));"
    "[c.incr('fix-9-cccccccccccc') for _ in range(25)]"
)
env = {**os.environ, "TAUCETI_GATE_DIR": str(GATE)}
procs = [subprocess.Popen([sys.executable, "-c", script, str(TMP / "state" / f"p{i}")], env=env) for i in range(4)]
check("concurrent increments from four workers all count",
      all(p.wait() == 0 for p in procs) and a.read("fix-9-cccccccccccc") == 100, str(a.read("fix-9-cccccccccccc")))

# --- a round hands back only its own charges -------------------------------------------------------
key = "ci-pr-10"
mark = a.mark([key])
a.incr(key)  # this round's charge
b.incr(key)  # a peer's, meanwhile
a.give_back(mark)
check("give_back returns this round's charge and keeps the peer's", a.read(key) == 1, str(a.read(key)))
a.give_back(mark)
check("…and gives nothing back twice", a.read(key) == 1, str(a.read(key)))
check("a refund never goes below zero", b.decr("ci-pr-11") == 0 and b.read("ci-pr-11") == 0)

# --- the rebase checkpoint is shared, so both workers derive the same budget key -------------------
info = SimpleNamespace(number=12, head_oid=HEAD, is_draft=False, mergeable="MERGEABLE", build_success=True,
                       labels=(), build_status_at=1_700_000_000)
a.observe_rebase_progress(info, handoff=False)
check("a rebase checkpoint one worker records gives both the same budget key",
      a.rebase_budget_key(12) == b.rebase_budget_key(12) != "rebase-pr-12", b.rebase_budget_key(12))

# --- losing the branch claim hands the charge back -------------------------------------------------
pr13 = survey_mod.PRInfo(number=13, head_oid=HEAD, head_ref="feature", head_owner="me", head_repo="TauCeti",
                         is_draft=False, mergeable="MERGEABLE", author="me", build_success=False, build_failed=True)
b.incr(f"ci-13-{HEAD[:12]}")  # the peer that holds the claim charged its attempt
lost = SimpleNamespace(counters=a, claims=SimpleNamespace(begin_branch_work=lambda *args: False))
rc = work_units.do_fix_ci(lost, SimpleNamespace(open_prs=[pr13]), survey_mod.Candidate(13, HEAD), None, False)
check("a worker that loses the branch claim declines the candidate", rc is None, str(rc))
check("…and hands its charge back, leaving the holder's", (a.read(f"ci-13-{HEAD[:12]}"), a.read("ci-pr-13")) == (1, 0),
      str((a.read(f"ci-13-{HEAD[:12]}"), a.read("ci-pr-13"))))

# --- a peer's survey sees the spent budget ---------------------------------------------------------
incidents = TMP / "incidents"
(incidents / "acked").mkdir(parents=True)
interaction.incidents_dir = lambda: incidents
for _ in range(MAX_CI_ATTEMPTS):
    a.incr(f"ci-14-{HEAD[:12]}")
red = {"number": 14, "headRefOid": HEAD, "headRefName": "feature", "headRepositoryOwner": {"login": "me"},
       "headRepository": {"name": "TauCeti"}, "author": {"login": "me"}, "mergeable": "MERGEABLE", "labels": [],
       "statusCheckRollup": [{"context": "build", "state": "FAILURE"}]}
gh = GitHub()
with (
    patch.object(survey_mod, "me", return_value="me"),
    patch.object(survey_mod, "can_push", side_effect=AssertionError("no canonical bot PRs")),
    patch.object(gh, "open_prs", return_value=[red]),
    patch.object(gh, "_gh", return_value=SimpleNamespace(returncode=0, stdout="")),
):
    sv = survey_mod.survey(SimpleNamespace(wid="w2"), gh, None, b, deep=False)
    survey_mod.survey(SimpleNamespace(wid="w3"), gh, None, counters("w3"), deep=False)
spent = [c for c in sv.red_ci.suppressed if c.pr == 14]
check("another worker's survey suppresses the head the first one spent", [c.pr for c in sv.red_ci.actionable] == []
      and len(spent) == 1, str((sv.red_ci.actionable, sv.red_ci.suppressed)))
check("…with the fleet's count of attempts", spent and (spent[0].attempts, spent[0].budget) == (MAX_CI_ATTEMPTS, MAX_CI_ATTEMPTS))
reason = work_units.pr_focus_reason(sv, SimpleNamespace(only=["fix-ci"]), 14)
check("…which the --pr explanation reports", f"({MAX_CI_ATTEMPTS}/{MAX_CI_ATTEMPTS} attempts spent)" in reason, reason)
check("the budget-spent incident is written once for the fleet",
      len(list(incidents.glob("budget-spent-*.json"))) == 1, str(list(incidents.glob("*"))))

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""A PR a round already declined at its current head is not picked up again until the head moves.

2026-09-21: fix3 declined to rebase #4928 (main subsumed it) and three minutes later fix2 picked the
same PR at the same head and reached the same conclusion. The per-worker attempt counters cannot see
across workers; the fleet-wide `declined` incident can. `attention.declined_at(stage)` lists the
(pr, head) pairs on record, acknowledged or not, and the survey suppresses them for that stage.

Exit 0 = all checks hold; 1 = a mismatch.
"""

import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import attention, interaction  # noqa: E402
from tauceti_worker.github import GitHub  # noqa: E402

survey_mod = importlib.import_module("tauceti_worker.survey")

HEAD, OLD = "a" * 40, "b" * 40
TMP = Path(tempfile.mkdtemp(prefix="tauceti-declined-sup-"))
interaction.incidents_dir = lambda: TMP / "incidents"
(TMP / "incidents" / "acked").mkdir(parents=True)


def incident(folder, stage, pr, head):
    (folder / f"declined-{stage}-{pr}.json").write_text(json.dumps({"kind": "declined", "key": f"{stage}-{pr}", "stage": stage, "pr": pr, "head": head}))


incident(TMP / "incidents", "rebase", 1, HEAD)  # live decline at the current head
incident(TMP / "incidents", "rebase", 2, OLD)  # declined at an OLDER head: the PR moved on
incident(TMP / "incidents" / "acked", "rebase", 3, HEAD)  # acknowledged, same head: still suppressed
incident(TMP / "incidents", "fix", 4, HEAD)  # another stage: does not touch rebase


def pr(number, *, head=HEAD):
    return {
        "number": number, "headRefOid": head, "headRefName": "feature",
        "headRepositoryOwner": {"login": "me"}, "headRepository": {"name": "TauCeti"},
        "author": {"login": "me"}, "statusCheckRollup": [], "mergeable": "CONFLICTING", "labels": [],
    }


fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


check("declined_at lists live and acked rebase declines, not other stages",
      attention.declined_at("rebase") == {(1, HEAD), (2, OLD), (3, HEAD)} and attention.declined_at("fix") == {(4, HEAD)},
      str(attention.declined_at("rebase")))

gh = GitHub()
counters = SimpleNamespace(read=lambda name: 0)
with (
    patch.object(survey_mod, "me", return_value="me"),
    patch.object(survey_mod, "can_push", side_effect=AssertionError("no canonical bot PRs")),
    patch.object(gh, "open_prs", return_value=[pr(1), pr(2), pr(3), pr(4), pr(5)]),
    patch.object(gh, "_gh", return_value=SimpleNamespace(returncode=0, stdout="")),
):
    sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)

check("rebase skips the PRs declined at their current head, acked or not", [c.pr for c in sv.rebaseable.suppressed] == [1, 3], str([c.pr for c in sv.rebaseable.suppressed]))
check("a PR declined at an older head, another stage's decline, and an untouched PR stay actionable", [c.pr for c in sv.rebaseable.actionable] == [2, 4, 5], str([c.pr for c in sv.rebaseable.actionable]))
check("the suppression names its reason", all("declined at this head" in c.reason for c in sv.rebaseable.suppressed))
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

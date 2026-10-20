#!/usr/bin/env python3
"""With TAUCETI_OWN_BRANCHES set, a fleet tends only the PRs on its own branches (a login another
operator's fleet also runs): rebase, fix-ci and the fixer count skip the other branches and bot PRs,
while the open-PR count that the account's cap reads stays account-wide. Unset, nothing changes."""

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
survey_mod = importlib.import_module("tauceti_worker.survey")


def pr(number, author, head, *, bot=False, owner="me", failed=False, conflicting=False, labels=()):
    return {
        "number": number,
        "headRefOid": f"head-{number}",
        "headRefName": head,
        "headRepositoryOwner": {"login": owner},
        "headRepository": {"name": "TauCeti"},
        "statusCheckRollup": ([{"context": "build", "state": "FAILURE"}] if failed else []),
        "author": {"login": author, "is_bot": bot},
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
        "labels": [{"name": n} for n in labels],
    }


RAW = [
    pr(1, "me", "roadmap/a-gqa-c1", failed=True, labels=("ci-failed",)),
    pr(2, "me", "roadmap/b-me-5", failed=True, labels=("ci-failed",)),
    pr(3, "me", "roadmap/c-gqa-fix2", conflicting=True, labels=("awaiting-author",)),
    pr(4, "me", "roadmap/d-me-7", conflicting=True, labels=("awaiting-author",)),
    pr(5, "review-bot", "bump-mathlib/test", bot=True, owner=survey_mod.TAUCETI_OWNER, failed=True, conflicting=True),
]


class Counters:
    def read(self, _name):
        return 0


def classify(pattern):
    saved = survey_mod.me, survey_mod.can_push
    survey_mod.me = lambda: "me"
    survey_mod.can_push = lambda _repo: True
    if pattern is None:
        os.environ.pop("TAUCETI_OWN_BRANCHES", None)
    else:
        os.environ["TAUCETI_OWN_BRANCHES"] = pattern
    try:
        sv = survey_mod.survey(SimpleNamespace(wid="test"), SimpleNamespace(open_prs=lambda: RAW), None, Counters(),
                               deep=False)
        return {
            "rebase": sorted(c.pr for c in sv.rebaseable.actionable),
            "fix-ci": sorted(c.pr for c in sv.red_ci.actionable),
            "needs-fix": sv.mine_needs_fix(),
            "mine-open": len(sv._mine_open_prs),
        }
    finally:
        survey_mod.me, survey_mod.can_push = saved
        os.environ.pop("TAUCETI_OWN_BRANCHES", None)


def main():
    checks = [
        ("unset: every PR of the login, and bot PRs", classify(None),
         {"rebase": [3, 4, 5], "fix-ci": [1, 2], "needs-fix": 4, "mine-open": 4}),
        ("set: only this fleet's branches, no bot PRs", classify("-gqa-"),
         {"rebase": [3], "fix-ci": [1], "needs-fix": 2, "mine-open": 4}),
    ]
    failures = 0
    for name, got, want in checks:
        ok = got == want
        print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
        failures += 0 if ok else 1
    print("ALL OK" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

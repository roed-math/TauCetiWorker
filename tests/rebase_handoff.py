#!/usr/bin/env python3
"""A trusted head-bound request reaches only the tending worker, with bounded retries."""

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.github import GitHub

survey_mod = importlib.import_module("tauceti_worker.survey")
HEAD = "a" * 40


def request(head=HEAD, author="tauceti-review-bot[bot]"):
    return {"author": author, "body": f"Merge-queue recovery for head `{head[:7]}`.\n\n<!--tauceti-rebase:v1 {head}-->"}


def stalled(head=HEAD):
    return {"author": "tauceti-review-bot[bot]",
            "body": f"Merge-queue recovery for head `{head[:7]}`: repeated queue evictions, but this branch already "
                    f"includes current `main`.\n\n<!--tauceti-merge-stalled:v1 {head}-->"}


def test_trusted_paginated_comments():
    gh = GitHub()
    response = SimpleNamespace(returncode=0, stdout="")
    with patch.object(gh, "_gh", return_value=response) as call:
        for rows, expected in [
            ([request(author="peer"), request()], True),
            ([request(author="peer")], False),
            ([request("b" * 40)], False),
            ([{**request(), "body": request()["body"].replace("tauceti-rebase", "tauceti-merge-stalled")}], False),
            ([{**request(), "body": "Quoted text\n" + request()["body"]}], False),
            ([{"author": "tauceti-review-bot[bot]", "body": None}], False),
            ([{"author": "tauceti-review-bot[bot]", "body": []}], False),
            ([None, [], request()], True),
        ]:
            response.stdout = "\n".join(json.dumps(row) for row in rows)
            assert gh.rebase_requested(1, HEAD) is expected, rows
        assert "--paginate" in call.call_args.args[0]
        response.stdout = "not json"
        assert not gh.rebase_requested(1, HEAD)
        response.stdout = json.dumps(request())
        response.returncode = 1
        assert not gh.rebase_requested(1, HEAD)
        call.reset_mock()
        assert not gh.rebase_requested(1, "invalid")
        call.assert_not_called()
        # the sweep's "flagged for a maintainer" verdict counts only when asked for (our own PRs)
        response.returncode = 0
        response.stdout = json.dumps(stalled())
        assert not gh.rebase_requested(1, HEAD)
        assert gh.rebase_requested(1, HEAD, accept_stalled=True)
        assert not gh.rebase_requested(1, "b" * 40, accept_stalled=True)
        response.stdout = json.dumps({**stalled(), "author": "peer"})
        assert not gh.rebase_requested(1, HEAD, accept_stalled=True)


def pr(number, *, author="me", labels=("needs-rebase",), head=HEAD, conflicting=False):
    return {
        "number": number,
        "headRefOid": head,
        "headRefName": "feature",
        "headRepositoryOwner": {"login": author},
        "headRepository": {"name": "TauCeti"},
        "author": {"login": author},
        "statusCheckRollup": [],
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
        "labels": [{"name": label} for label in labels],
    }


def test_survey_ownership_head_pause_and_budget():
    raw = [
        pr(1),
        pr(2, author="peer"),
        pr(3, labels=()),
        pr(4, labels=("needs-rebase", "keep")),
        pr(5, head="b" * 40),
        pr(6),
        pr(7, labels=(), conflicting=True),
        pr(8, labels=("hold",), conflicting=True),
    ]
    gh = GitHub()
    response = SimpleNamespace(returncode=0, stdout=json.dumps(request()))
    counters = SimpleNamespace(read=lambda name: survey_mod.MAX_REBASE_ATTEMPTS if name == "rebase-pr-6" else 0)
    with (
        patch.object(survey_mod, "me", return_value="me"),
        patch.object(survey_mod, "can_push", side_effect=AssertionError("no canonical bot PRs")),
        patch.object(gh, "open_prs", return_value=raw),
        patch.object(gh, "_gh", return_value=response) as calls,
    ):
        sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)
    assert [c.pr for c in sv.rebaseable.actionable] == [1, 7]
    assert [c.pr for c in sv.rebaseable.suppressed] == [6]
    assert all(c.budget == survey_mod.MAX_REBASE_ATTEMPTS for c in sv.rebaseable.actionable)
    assert calls.call_count == 3  # only our labelled, unpaused PRs need comment reads


def test_survey_takes_our_own_stalled_pr():
    """#9857 (2026-09-29): ours, ready to merge, evicted twice, flagged `needs-rebase` with a stalled marker."""
    gh = GitHub()
    response = SimpleNamespace(returncode=0, stdout=json.dumps(stalled()))
    counters = SimpleNamespace(read=lambda name: 0)
    with (
        patch.object(survey_mod, "me", return_value="me"),
        patch.object(survey_mod, "can_push", side_effect=AssertionError("no canonical bot PRs")),
        patch.object(gh, "open_prs", return_value=[pr(1, labels=("ready-to-merge", "needs-rebase"))]),
        patch.object(gh, "_gh", return_value=response),
    ):
        sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)
    assert [c.pr for c in sv.rebaseable.actionable] == [1], sv.rebaseable
    assert "stalled" in sv.rebaseable.actionable[0].reason


if __name__ == "__main__":
    test_trusted_paginated_comments()
    test_survey_ownership_head_pause_and_budget()
    test_survey_takes_our_own_stalled_pr()
    print("PASS: trusted fork handoff, ownership, stale heads, pause labels, retry cap and our own stalled PRs")

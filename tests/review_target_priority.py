#!/usr/bin/env python3
"""Reviewers, when they turn to the account's own PRs, take those serving the target list first
(owner's ruling, 2026-10-02: "if reviewing our own, prioritize target list").

`own_target_reviews_first` reorders only the account's own PRs among the positions they already
held, so how often a reviewer reviews its own account's PRs rather than other people's is unchanged.
Checked directly and through `run_round` with a stubbed survey and dispatcher:

  - own target-list PRs move to the earliest own positions; other people's PRs keep their places;
  - another operator's PR serving the list is not moved;
  - with no own target-list PR, or no list, the order is unchanged;
  - a review round offers an own target-list PR before an own unrelated one that came first.

Exit 0 = all hold; 1 = a mismatch."""

import importlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
for var in ("TAUCETI_GATE_DIR", "TAUCETI_GATE_REQUIRED", "TAUCETI_ROADMAP_TARGETS"):
    os.environ.pop(var, None)
S = importlib.import_module("tauceti_worker.survey")  # the package also exports a function `survey`
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


def pr(n, author="me", ids=()):
    return S.PRInfo(number=n, head_oid="h" * 40, head_ref=f"b{n}", head_owner="o", head_repo="r", is_draft=False,
                    mergeable="MERGEABLE", author=author, build_success=True, build_failed=False, target_ids=tuple(ids))


def cands(*ns):
    return [S.Candidate(n, "h" * 40) for n in ns]


def nums(cs):
    return [c.pr for c in cs]


sv = S.Survey(worker_id="t")
sv._mine_open_prs = [pr(1), pr(2), pr(3)]  # our own; 1 and 3 unrelated, 2 on the list
order = cands(1, 50, 2, 60, 3)  # 50 and 60 are other people's
check("own list PR takes the earliest own position; others keep theirs",
      nums(W.own_target_reviews_first(order, sv, {2, 60})) == [2, 50, 1, 60, 3])
check("another operator's list PR is not moved", nums(W.own_target_reviews_first(cands(50, 60, 1), sv, {60})) == [50, 60, 1])
check("no own list PR: unchanged", nums(W.own_target_reviews_first(order, sv, {60})) == [1, 50, 2, 60, 3])
check("no list: unchanged", nums(W.own_target_reviews_first(order, sv, set())) == [1, 50, 2, 60, 3])
check("several own list PRs keep their relative order",
      nums(W.own_target_reviews_first(cands(1, 3, 50, 2), sv, {3, 2})) == [3, 2, 50, 1])

# Through a review round: the reviewer's order puts own #1 first; own #2 serves the list.
TMP = Path(tempfile.mkdtemp(prefix="tauceti-review-priority-"))
LIST = TMP / "targets.md"
LIST.write_text("# t\n<!-- tauceti-targets:v1 -->\n\n## Area\n- [~] `marked` — L0 (needs: none)\n")
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(LIST)
rv = S.Survey(worker_id="t")
rv.open_prs = [pr(1), pr(2, ids=[("Area", "marked")]), pr(50, author="someone")]
rv._mine_open_prs = [p for p in rv.open_prs if p.author == "me"]
rv.reviewable.actionable = cands(1, 50, 2)
offers = []
saved = (W.survey, W.dispatch, W.prioritize_review_candidates)
W.survey = lambda *_a, **_k: rv
W.dispatch = lambda stage, w, sv_, c, opts: offers.append((stage, c.pr)) or 0
W.prioritize_review_candidates = lambda cs, reviewer, now=None, rng=None: (list(cs), [])
try:
    worker = SimpleNamespace(cfg=SimpleNamespace(state=TMP, logdir=TMP, wid="t"), gh=None, rs=None, counters=None)
    try:
        W.run_round(worker, SimpleNamespace(only=["review"], dry_run=True, prs=()))
    except NoProgress:
        pass
finally:
    W.survey, W.dispatch, W.prioritize_review_candidates = saved
check("a review round offers the own list PR before the own unrelated one", offers == [("review", 2)], offers)

sys.exit(1 if fails else 0)

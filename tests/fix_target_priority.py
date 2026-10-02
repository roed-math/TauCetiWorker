#!/usr/bin/env python3
"""Fixers take PRs that serve the operator's target list first (owner's request, 2026-10-02).

`run_round`'s cascade makes a first pass over the branch-writing stages (rebase, bump, fix-ci, fix)
offering only target-list PRs, in the usual stage order, then the ordinary pass over everything not
yet offered. A PR serves the list when its target marker names an item of the list, or when the list
marks it in flight. Checked with a stubbed survey and dispatcher:

  - a target PR needing a fix wins over an unrelated PR needing a rebase (an earlier stage);
  - a target PR the list marks in flight counts, with no marker;
  - a target PR claimed elsewhere falls through to the ordinary pass, and is not offered twice;
  - with no target-list PR actionable, or no list, the ordinary order is unchanged;
  - a `--pr` round keeps the ordinary order;
  - an unreadable list costs only the preference.

Exit 0 = all hold; 1 = a mismatch."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
for var in ("TAUCETI_GATE_DIR", "TAUCETI_GATE_REQUIRED", "TAUCETI_ROADMAP_TARGETS"):
    os.environ.pop(var, None)
import tauceti_worker as tc  # noqa: E402
import importlib  # noqa: E402

S = importlib.import_module("tauceti_worker.survey")  # the package also exports a function `survey`
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-fix-priority-"))
LIST = TMP / "targets.md"
LIST.write_text("""# targets
<!-- tauceti-targets:v1 -->

## Area
- [~] `marked` — L0 (needs: none)
- [~] `listed` — L0 (needs: none; in flight: #30)
- [ ] `open-one` — L1 (needs: none)
""")


def pr(n, ids=()):
    return S.PRInfo(number=n, head_oid="h" * 40, head_ref=f"b{n}", head_owner="o", head_repo="r", is_draft=False,
                    mergeable="MERGEABLE", author="me", build_success=True, build_failed=False, target_ids=tuple(ids))


def make_survey(rebase=(), fix_ci=(), fix=(), open_prs=()):
    sv = S.Survey(worker_id="t")
    sv.open_prs = list(open_prs)
    sv.rebaseable.actionable = [S.Candidate(n, "h" * 40) for n in rebase]
    sv.red_ci.actionable = [S.Candidate(n, "h" * 40) for n in fix_ci]
    sv.needs_fix.actionable = [S.Candidate(n, "h" * 40) for n in fix]
    return sv


def run(sv, *, claimed=(), prs=(), only=("fix", "fix-ci", "rebase")):
    """Run one round; returns the (stage, pr) dispatch offers in order."""
    offers = []

    def dispatch(stage, w, sv_, c, opts):
        offers.append((stage, c.pr))
        return None if c.pr in claimed else 0

    saved = (W.survey, W.dispatch, W.spread_candidates)
    W.survey = lambda *_a, **_k: sv
    W.dispatch = dispatch
    W.spread_candidates = lambda xs, rng=None: list(xs)  # deterministic order inside a stage
    try:
        worker = SimpleNamespace(cfg=SimpleNamespace(state=TMP, logdir=TMP, wid="t"), gh=None, rs=None, counters=None)
        try:
            W.run_round(worker, SimpleNamespace(only=list(only), dry_run=True, prs=tuple(prs)))
        except NoProgress:
            pass
    finally:
        W.survey, W.dispatch, W.spread_candidates = saved
    return offers


# Unrelated #10 needs a rebase (an earlier stage); #20 serves `marked` and needs a fix.
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(LIST)
opened = [pr(10), pr(20, [("Area", "marked")]), pr(30), pr(40, [("Elsewhere", "marked")])]
offers = run(make_survey(rebase=[10], fix=[20], open_prs=opened))
check("a target PR needing a fix wins over an unrelated rebase", offers == [("fix", 20)], offers)

offers = run(make_survey(rebase=[10], fix_ci=[30], open_prs=opened))
check("a PR the list marks in flight counts without a marker", offers == [("fix-ci", 30)], offers)

offers = run(make_survey(rebase=[10], fix=[40], open_prs=opened))
check("a marker for an item of another area does not count", offers == [("rebase", 10)], offers)

offers = run(make_survey(rebase=[10], fix=[20], open_prs=opened), claimed={20})
check("a claimed target PR falls through, and is offered once", offers == [("fix", 20), ("rebase", 10)], offers)

offers = run(make_survey(rebase=[10, 11], fix=[12], open_prs=opened))
check("without an actionable target PR the order is unchanged", offers == [("rebase", 10)], offers)

offers = run(make_survey(rebase=[10], fix=[20], open_prs=opened), prs=(10, 20))
check("a --pr round keeps the ordinary order", offers == [("rebase", 10)], offers)

os.environ.pop("TAUCETI_ROADMAP_TARGETS")
offers = run(make_survey(rebase=[10], fix=[20], open_prs=opened))
check("without a list the order is unchanged", offers == [("rebase", 10)], offers)

os.environ["TAUCETI_ROADMAP_TARGETS"] = str(TMP / "missing.md")
offers = run(make_survey(rebase=[10], fix=[20], open_prs=opened))
check("an unreadable list costs only the preference", offers == [("rebase", 10)], offers)

check("the module exports the stage set", W.BRANCH_STAGES == {"rebase", "bump", "fix-ci", "fix"} and tc is not None)
sys.exit(1 if fails else 0)

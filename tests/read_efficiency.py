#!/usr/bin/env python3
"""The fleet's read savings of 2026-09-27, offline.
  * One open-PR listing serves every worker for OPEN_PR_SHARED_TTL: a second call inside the window
    reads the shared file, one after it fetches again; outside a fleet nothing is shared.
  * The comment cache is keyed on the comments' own activity: a node's key ignores CI/label/push
    churn (updatedAt) but moves on a new or edited comment, a new review, or a deleted comment.
Exit 0 = all hold; 1 = a mismatch."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github as G  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


# ---- the shared listing -------------------------------------------------------------------------------
T = Path(tempfile.mkdtemp())
calls = []
real_current = G.gate_mod.current
G.gate_mod.current = lambda: SimpleNamespace(enabled=True, dir=T, cache_dir=T / "cache")
gh = G.GitHub("Owner/Repo")
gh._open_prs_fetch = lambda page=100: calls.append(1) or [{"number": len(calls)}]
first = gh.open_prs()
second = G.GitHub("Owner/Repo")
second._open_prs_fetch = gh._open_prs_fetch
again = second.open_prs()
check("a second worker inside the window reads the shared listing", len(calls) == 1 and again == first, str(calls))
f = T / "cache" / "open_prs-Owner__Repo.json"
import json  # noqa: E402

c = json.loads(f.read_text()); c["fetched_at"] -= G.OPEN_PR_SHARED_TTL + 1; f.write_text(json.dumps(c))
second.open_prs()
check("after the window it fetches again", len(calls) == 2, str(calls))
G.gate_mod.current = lambda: SimpleNamespace(enabled=False, dir=None, cache_dir=None)
gh.open_prs(); gh.open_prs()
check("outside a fleet every call fetches", len(calls) == 4, str(calls))
G.gate_mod.current = real_current

# ---- the activity key -----------------------------------------------------------------------------------
def node(ic_n=2, ic_t="2026-09-20T00:00:00Z", rv_n=1, rv_t="2026-09-19T00:00:00Z", updated="2026-09-27T00:00:00Z"):
    return {"number": 1, "updatedAt": updated,
            "comments": {"totalCount": ic_n, "nodes": [{"updatedAt": ic_t}] if ic_n else []},
            "reviews": {"totalCount": rv_n, "nodes": [{"updatedAt": rv_t}] if rv_n else []}}


k = G._activity_key(node())
check("CI, label or push churn (updatedAt) leaves the key alone", G._activity_key(node(updated="2026-09-28T00:00:00Z")) == k)
check("an edited comment moves it", G._activity_key(node(ic_t="2026-09-27T12:00:00Z")) != k)
check("a deleted comment moves it", G._activity_key(node(ic_n=1)) != k)
check("a new review moves it", G._activity_key(node(rv_n=2, rv_t="2026-09-27T12:00:00Z")) != k)
check("no key without the fields (falls back to updatedAt)", G._activity_key({"number": 1}) == "")
check("the listing carries it into the PR record",
      G._pr_json_from_graphql(node())["activityKey"] == k)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

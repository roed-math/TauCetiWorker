#!/usr/bin/env python3
"""A response BODY is not a status line (the 2026-09-24 false account halt).

A gq2 worker's routine read of a PR's comments succeeded, but one comment said its author's retry
request "returned HTTP 403" and a scoreboard said a "blocked" rubric stops the merge. The gate read
the status out of the whole output and the halt words out of the same text ("blocked" contains
"locked"), and halted the fleet as if the account were suspended. It stayed down for a day.

Through the real gh_run against a fake gh:
  * a SUCCESSFUL paginated read whose body quotes "HTTP 403", "blocked", "suspended" and "too many"
    records ok with no status, and the store stays RUNNING;
  * a FAILED call whose error line is a 403 and says "blocked" (a word containing a halt word) is not
    an account halt;
  * the real suspension shape still halts, with reason account-halt.

Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, events, fake_env, finish, gate_env, mktemp, scrub_env  # noqa: E402

BODY = json.dumps([
    {"id": 1, "body": "My direct retry request returned HTTP 403 (repository rights required)."},
    {"id": 2, "body": "The PR merges only once every rubric is green; a blocked or errored rubric stops it. "
                      "An account suspended for too many requests is not what this is."},
])


def run(tmp: Path, gh_rules: list[dict], argv: list[str]):
    from tauceti_worker import gate as G
    from tauceti_worker import github

    gate_env(tmp)
    env = fake_env(tmp, {"gh": gh_rules, "git": [], "bare_repos": {}})
    os.environ.update({k: env[k] for k in ("PATH", "TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG", "TAUCETI_REAL_GH")})
    G._CURRENT = None
    p = github.gh_run(argv)
    st = json.loads((tmp / "gate" / "state.json").read_text()) if (tmp / "gate" / "state.json").exists() else {}
    return p, st


scrub_env()

# ---- 1. a successful read quoting status lines and halt words ---------------------------------------
T1 = mktemp("gate-body-ok-")
p, st = run(T1, [{"match": ["api", "--paginate"], "rc": 0, "stdout": BODY + "\n"}],
            ["gh", "api", "--paginate", "repos/TauCetiProject/TauCeti/issues/8416/comments"])
recs = [e for e in events(T1) if e.get("decision") == "record"]
check("the read itself succeeded", p.returncode == 0)
check("…and was recorded ok with no status", bool(recs) and recs[-1].get("ok") is True and recs[-1].get("status") is None)
check("…with verdict none", bool(recs) and recs[-1].get("verdict") == "none")
check("the store is not halted", st.get("state") in (None, "RUNNING") and not (T1 / "gate" / "halt.json").exists())

# ---- 2. a real 403 whose text has a word CONTAINING a halt word -------------------------------------
T2 = mktemp("gate-body-blocked-")
p, st = run(T2, [{"match": ["pr", "list"], "rc": 1, "stderr": "gh: Resource blocked by an organization policy (HTTP 403)\n"}],
            ["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"])
check("'blocked' in a 403 is not an account halt", st.get("reason") != "account-halt" and not (T2 / "gate" / "halt.json").exists())

# ---- 3. the real suspension shape still halts ----------------------------------------------------------
T3 = mktemp("gate-body-suspended-")
p, st = run(T3, [{"match": ["pr", "list"], "rc": 1, "stderr": "gh: Sorry. Your account was suspended. (HTTP 403)\n"}],
            ["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"])
check("a suspension still halts with reason account-halt", (st.get("state"), st.get("reason")) == ("HALTED_MANUAL", "account-halt"))

finish()

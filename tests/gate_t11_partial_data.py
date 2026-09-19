#!/usr/bin/env python3
"""T11 — GraphQL partial data, a malformed response, incomplete pagination, and repeated 5xx (brief
§5.1, §8.3 T11).

The survey's open-PR query (`gh api graphql`, through the real gh_run) meets, from a fake gh:
  * a truncated JSON body with exit 0        → GitHubError; survey.github_failed; the round yields
  * a GraphQL `errors` body with data null   → GitHubError; never "no work"
  * a page that always claims hasNextPage    → GitHubError after OPEN_PR_MAX_PAGES (bounded); each
                                               page was admitted and recorded, none turned into work
In all three the gate records the outcome and stays RUNNING: not a rate limit, not a permission
verdict, so no cooldown and no quarantine. Then repeated HTTP 502: gh_run retries a read up to
GH_TRANSIENT_TRIES times (sleep stubbed); each attempt is admitted and recorded; on the third
consecutive 5xx the op is quarantined for 30 minutes and the next survey is refused without
spawning gh — bounded, and still never "nothing to do".

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, fake_calls, fake_env, finish, gate_env, mktemp, scrub_env, state  # noqa: E402

scrub_env()
TMP = mktemp("gate-t11-")
gate_env(TMP)
PAGE_MORE = json.dumps(
    {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                    "nodes": [
                        {
                            "number": 1,
                            "title": "t",
                            "body": "",
                            "labels": {"totalCount": 0, "nodes": []},
                            "commits": {"nodes": []},
                        }
                    ],
                }
            }
        }
    }
)
SCENARIO = {
    "gh": [
        {"match": ["api", "graphql"], "rc": 0, "stdout": '{"data":{"repository":{"pullReq', "times": 1},
        {
            "match": ["api", "graphql"],
            "rc": 0,
            "stdout": '{"errors":[{"message":"Something went wrong while executing your query."}],"data":null}\n',
            "times": 1,
        },
        {"match": ["api", "graphql"], "rc": 0, "stdout": PAGE_MORE + "\n", "times": 200},
        {
            "match": ["api", "graphql"],
            "rc": 1,
            "stderr": "gh: HTTP 502: Bad Gateway (https://api.github.com/graphql)\n",
        },
        {"match": ["api", "user"], "rc": 0, "stdout": "alice\n"},
    ],
    "git": [],
    "bare_repos": {},
}
env = fake_env(TMP, SCENARIO)
os.environ.update(
    {k: env[k] for k in ("PATH", "TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG", "TAUCETI_REAL_GH", "TAUCETI_REAL_GIT")}
)
os.environ["TAUCETI_IDENTITY_OK"] = "alice"

import importlib  # noqa: E402

import tauceti_worker as tc  # noqa: E402
from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import github  # noqa: E402
from tauceti_worker.constants import OPEN_PR_MAX_PAGES  # noqa: E402

survey = importlib.import_module("tauceti_worker.survey")  # the package flattens survey() over the module name

gh = github.GitHub()
cfg = SimpleNamespace(wid="w1", state=TMP / "state")
counters = survey.Counters(cfg)


def graphql_calls():
    return [a for a in fake_calls(TMP, "gh") if a[:2] == ["api", "graphql"]]


# ---- 1. truncated JSON, exit 0 --------------------------------------------------------------------------------
sv = survey.survey(cfg, gh, None, counters, deep=False)
check("truncated JSON: the survey reports github_failed (never 'no work')", sv.github_failed and sv.open_prs == [])
check("…with the error kept", any("open PR query" in e for e in sv.errors))
check(
    "…and the store stays RUNNING (no cooldown, no quarantine)",
    state(TMP).get("state") == G.RUNNING and not (TMP / "gate" / "quarantine.json").exists(),
)
n = len(graphql_calls())

# ---- 2. a GraphQL errors body ---------------------------------------------------------------------------------
sv = survey.survey(cfg, gh, None, counters, deep=False)
check("GraphQL errors body: github_failed, not an empty survey", sv.github_failed and sv.open_prs == [])
check("…one request was spent on it", len(graphql_calls()) == n + 1)
check("…store still RUNNING", state(TMP).get("state") == G.RUNNING)
n = len(graphql_calls())

# ---- 3. pagination that never ends --------------------------------------------------------------------------------
sv = survey.survey(cfg, gh, None, counters, deep=False)
check(
    f"an endless hasNextPage stops after OPEN_PR_MAX_PAGES={OPEN_PR_MAX_PAGES} pages and is github_failed",
    sv.github_failed and len(graphql_calls()) == n + OPEN_PR_MAX_PAGES,
)
check("…the partial pages never became open_prs", sv.open_prs == [])
ev = [json.loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
admits = [e for e in ev if e.get("decision") == "admit" and e.get("op") == "graphql"]
records = [e for e in ev if e.get("decision") == "record" and e.get("op") == "graphql"]
check(
    "every page was admitted and recorded (pagination does not escape accounting)",
    len(admits) == len(records) == len(graphql_calls()),
)
check("…store still RUNNING", state(TMP).get("state") == G.RUNNING)
# The consumed-times sidecar: exhaust the paging entry so the 502 entry answers next.
consumed = json.loads((TMP / "scenario.json.consumed").read_text())
consumed["gh:2"] = 200
(TMP / "scenario.json.consumed").write_text(json.dumps(consumed))

# ---- 4. repeated 5xx: bounded retry, then a quarantine of the op ------------------------------------------------------
n = len(graphql_calls())
slept = []
real_sleep = tc.time.sleep
tc.time.sleep = lambda s: slept.append(s)
try:
    t0 = time.time()
    sv = survey.survey(cfg, gh, None, counters, deep=False)
finally:
    tc.time.sleep = real_sleep
check("HTTP 502 ×3: github_failed (not 'no work')", sv.github_failed)
tries = len(graphql_calls()) - n
check(
    f"the read was retried a bounded number of times ({tries} attempts, sleeps {slept})",
    2 <= tries <= 4 and all(s <= 20 for s in slept),
)
q = json.loads((TMP / "gate" / "quarantine.json").read_text())
qe = q.get("graphql:taucetiproject/tauceti")
check("after three consecutive 5xx the op is quarantined", qe is not None and qe.get("reason") == "transient")
check("…for 30 minutes", qe is not None and 1790 <= float(qe["until"]) - t0 <= 1810)
check("…and the store is RUNNING, not cooling down (a 5xx is not a rate limit)", state(TMP).get("state") == G.RUNNING)
n = len(graphql_calls())
sv = survey.survey(cfg, gh, None, counters, deep=False)
check(
    "the next survey is refused by the quarantine without spawning gh", sv.github_failed and len(graphql_calls()) == n
)
check("…and says so", any("quarantined" in e for e in sv.errors))
ev = [json.loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
check(
    "every 5xx attempt was recorded with status 502",
    sum(1 for e in ev if e.get("decision") == "record" and e.get("status") == 502) == tries,
)

finish()

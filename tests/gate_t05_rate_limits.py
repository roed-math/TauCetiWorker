#!/usr/bin/env python3
"""T05 — rate limits: Retry-After, an exhausted primary bucket, and no explicit retry time (brief
§5.2, §8.3 T05).

Through the real gh_run (max_wait=0, so it surfaces rather than sleeps) against a fake gh:
  * HTTP 429 with `Retry-After: 7` → COOLDOWN until now+7 s; no admit before it, including the
    rate_limit probe (github_budget raises GateRefused with that until) and a claim renew.
  * HTTP 403 "API rate limit exceeded" with `x-ratelimit-remaining: 0` and `x-ratelimit-reset` →
    COOLDOWN until the reset.
  * the secondary-limit text with no header → 60 s; on the next consecutive hit 120 s; then 240 s;
    a run of hits is bounded at 15 minutes; a success in between resets the doubling.
  * gh_run's own bounded in-place wait still applies: with max_wait large enough, a refused retry is
    slept out (sleep stubbed and recorded) rather than spun.

Proven by mock (the cooldowns are asserted on `until`, never waited for). Exit 0 = all hold.
"""

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, fake_calls, fake_env, finish, gate_env, mktemp, scrub_env, state  # noqa: E402

scrub_env()
TMP = mktemp("gate-t05-")
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
RESET = int(time.time()) + 300
SECONDARY = "gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)\n"
SCENARIO = {
    "gh": [
        {"match": ["pr", "list"], "rc": 1, "stderr": "gh: HTTP 429: Too Many Requests\nRetry-After: 7\n", "times": 1},
        {
            "match": ["pr", "list"],
            "rc": 1,
            "stderr": f"gh: HTTP 403: API rate limit exceeded for user ID 1\nx-ratelimit-remaining: 0\nx-ratelimit-reset: {RESET}\n",
            "times": 1,
        },
        {"match": ["pr", "list"], "rc": 1, "stderr": SECONDARY, "times": 3},
        {"match": ["pr", "list"], "rc": 0, "stdout": "[]\n", "times": 1},
        {"match": ["pr", "list"], "rc": 1, "stderr": SECONDARY, "times": 2},
        {"match": ["pr", "list"], "rc": 0, "stdout": "[]\n"},
        {"match": ["api", "rate_limit"], "rc": 0, "stdout": "{}\n"},
    ],
    "git": [],
    "bare_repos": {},
}
env = fake_env(TMP, SCENARIO)
os.environ.update(
    {k: env[k] for k in ("PATH", "TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG", "TAUCETI_REAL_GH", "TAUCETI_REAL_GIT")}
)

import tauceti_worker as tc  # noqa: E402
from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import github  # noqa: E402
from tauceti_worker import round as round_mod  # noqa: E402

PR_LIST = ["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"]


def expire(tmp: Path) -> None:
    """Let the cooldown lapse without waiting: move its end into the past."""
    p = tmp / "gate" / "state.json"
    s = json.loads(p.read_text())
    s["until"] = time.time() - 1
    p.write_text(json.dumps(s))


# ---- 1. 429 + Retry-After ---------------------------------------------------------------------------------------
t0 = time.time()
p = github.gh_run(PR_LIST, max_wait=0)
check("the 429 surfaces (max_wait=0)", p.returncode == 1 and "429" in p.stderr)
st = state(TMP)
check("COOLDOWN for Retry-After (7 s)", st.get("state") == G.COOLDOWN and 6 <= float(st["until"]) - t0 <= 8)
until = float(st["until"])
n = len(fake_calls(TMP, "gh"))
p = github.gh_run(PR_LIST, max_wait=0)
check(
    "a read before `until` is refused cooldown",
    p.returncode == 75 and "(cooldown)" in p.stderr and abs(p.gate_refused.until - until) < 0.5,
)
try:
    github.github_budget()
    probed = True
except G.GateRefused as e:
    probed = False
    check(
        "the rate_limit probe is refused with the same until", e.reason == G.R_COOLDOWN and abs(e.until - until) < 0.5
    )
check("…and did not run", not probed)
rc = round_mod.run_claim_sh(["renew", "branch/1"], "alice/tauceti-claims")
check("a claim renew is refused too (rc 2)", rc == 2)
check("nothing reached gh or git during the cooldown", len(fake_calls(TMP, "gh")) == n and fake_calls(TMP, "git") == [])

# ---- 2. exhausted primary bucket: x-ratelimit-reset --------------------------------------------------------------------
expire(TMP)
p = github.gh_run(PR_LIST, max_wait=0)
check("the primary limit surfaces", p.returncode == 1 and "rate limit exceeded" in p.stderr)
st = state(TMP)
check(
    "COOLDOWN until x-ratelimit-reset (+1 s)",
    st.get("state") == G.COOLDOWN
    and abs(float(st["until"]) - (RESET + 1)) <= 1
    and st.get("reason") == "primary-rate-limit",
)

# ---- 3. secondary limit with no header: 60 s, then 120 s, then 240 s; bounded; reset by a success --------------------
expire(TMP)
t0 = time.time()
p = github.gh_run(PR_LIST, max_wait=0)
st = state(TMP)
check(
    "secondary limit, first hit: 60 s",
    st.get("state") == G.COOLDOWN
    and 59 <= float(st["until"]) - t0 <= 61
    and st.get("reason") == "secondary-rate-limit",
)
expire(TMP)
t0 = time.time()
p = github.gh_run(PR_LIST, max_wait=0)
st = state(TMP)
check("second consecutive hit: 120 s", 119 <= float(st["until"]) - t0 <= 121)
expire(TMP)
t0 = time.time()
p = github.gh_run(PR_LIST, max_wait=0)
st = state(TMP)
check("third consecutive hit: 240 s", 239 <= float(st["until"]) - t0 <= 241)
expire(TMP)
p = github.gh_run(PR_LIST, max_wait=0)
check("a success in between (rc 0)", p.returncode == 0)
b = json.loads((TMP / "gate" / "budget.json").read_text())
check("…resets the doubling", "consecutive_secondary" not in b)
b["consecutive_secondary"] = 12
(TMP / "gate" / "budget.json").write_text(json.dumps(b))
t0 = time.time()
p = github.gh_run(PR_LIST, max_wait=0)
st = state(TMP)
check("a long run of hits is bounded to 15 minutes", 899 <= float(st["until"]) - t0 <= 901)

# ---- 4. gh_run's in-place wait honours the gate's until (bounded by max_wait), never spins ------------------------------
expire(TMP)
b = json.loads((TMP / "gate" / "budget.json").read_text())
b.pop("consecutive_secondary", None)
(TMP / "gate" / "budget.json").write_text(json.dumps(b))
slept = []
real_sleep = tc.time.sleep


def fake_sleep(s):
    slept.append(s)
    expire(TMP)  # the world moves on while we "sleep"


tc.time.sleep = fake_sleep
try:
    n = len(fake_calls(TMP, "gh"))
    p = github.gh_run(PR_LIST, max_wait=900)
finally:
    tc.time.sleep = real_sleep
check(
    "with max_wait, the secondary hit is slept out (one recorded sleep) and the retry succeeds",
    p.returncode == 0 and len(slept) == 1 and 55 <= slept[0] <= 62,
)
check("exactly two gh calls: the hit and the one retry", len(fake_calls(TMP, "gh")) == n + 2)
ev = [json.loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
records = [e for e in ev if e.get("decision") == "record"]
check("every dispatch, retries included, was admitted and recorded", len(records) == len(fake_calls(TMP, "gh")))
check(
    "the rate headers were captured on the record",
    any(e.get("rate_reset") == str(RESET) and e.get("rate_remaining") == "0" for e in records),
)

finish()

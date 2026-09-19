#!/usr/bin/env python3
"""T04 — a known permission denial versus an ambiguous 403 (brief §5.1, §8.3 T04).

Through the real gh_run against a fake gh:
  * "HTTP 403: Must have push access" on a reaction POST → that (op, target) is quarantined and
    nothing else is: a `pr list` is admitted, a second reaction attempt is refused `quarantined`
    without spawning gh (no loop), the store stays RUNNING, and `tauceti-gate revalidate` lifts it.
  * a bare "HTTP 403: Forbidden" → global COOLDOWN for 15 minutes, reason unclassified-403; every
    admit (a read, a claim renew, the rate_limit probe) is refused `cooldown` with the same `until`,
    and the fake log grows by exactly the one call that met the 403.
  * a git push rejected for permission ("remote rejected … permission denied") through
    run_claim_sh → quarantine of (acquire, that repo), not a halt, not a cooldown.

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, fake_calls, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env, state  # noqa: E402

scrub_env()
TMP = mktemp("gate-t04-")
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
SCENARIO = {
    "gh": [
        {
            "match": ["api", "-X", "POST", "/repos/TauCetiProject/TauCeti/pulls/comments/1/reactions"],
            "rc": 1,
            "stderr": "gh: Must have push access to view repository collaborators (HTTP 403)\n",
        },
        {"match": ["pr", "list"], "rc": 0, "stdout": "[]\n", "times": 1},
        {
            "match": ["pr", "list"],
            "rc": 1,
            "stderr": "gh: HTTP 403: Forbidden (https://api.github.com/repos/TauCetiProject/TauCeti/pulls)\n",
        },
        {"match": ["api", "rate_limit"], "rc": 0, "stdout": "{}\n"},
    ],
    "git": [
        {
            "match": ["-C"],
            "contains": "push --force-with-lease",
            "rc": 1,
            "stderr": "! [remote rejected] refs/tauceti-claims/x -> refs/tauceti-claims/x (permission denied)\nerror: failed to push some refs\n",
        },
    ],
    "bare_repos": {"alice/tauceti-claims": str(TMP / "claims.git")},
}
env = fake_env(TMP, SCENARIO, CLAIM_GITDIR=str(TMP / "scratch.git"), CLAIM_REPO="alice/tauceti-claims")
os.environ.update(
    {
        k: env[k]
        for k in (
            "PATH",
            "TAUCETI_FAKE_SCENARIO",
            "TAUCETI_FAKE_LOG",
            "TAUCETI_REAL_GH",
            "TAUCETI_REAL_GIT",
            "TAUCETI_PYTHON",
            "TAUCETI_GATE_CLI",
            "CLAIM_GITDIR",
            "CLAIM_REPO",  # the fleet wrapper pins the claim namespace; the push allowlist reads it
        )
    }
)

from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import github  # noqa: E402
from tauceti_worker import round as round_mod  # noqa: E402

gh = github.GitHub()

# ---- 1. a confirmed permission denial quarantines the (op, target) only --------------------------------------
ok = gh.add_reaction(1)
check("add_reaction reports failure", ok is False)
q = json.loads((TMP / "gate" / "quarantine.json").read_text())
check(
    "quarantine.json holds api-post:taucetiproject/tauceti with reason permission-denied",
    q.get("api-post:taucetiproject/tauceti", {}).get("reason") == "permission-denied",
)
check("the store stays RUNNING", state(TMP).get("state") == G.RUNNING)
n = len(fake_calls(TMP, "gh"))
check(
    "another op on the same target is admitted (pr list ran)",
    gh.pr_list(["number"]) == [] and len(fake_calls(TMP, "gh")) == n + 1,
)
n = len(fake_calls(TMP, "gh"))
ok = gh.add_reaction(1)
check(
    "a second reaction attempt is refused without reaching gh (no loop)",
    ok is False and len(fake_calls(TMP, "gh")) == n,
)
ev = [json.loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
check(
    "…as `quarantined`",
    any(e.get("decision") == "refuse" and e.get("reason") == G.R_QUARANTINED and e.get("op") == "api-post" for e in ev),
)
p = gate_cli(["revalidate", "api-post", "TauCetiProject/TauCeti"], env)
check("`tauceti-gate revalidate` lifts the quarantine", p.returncode == 0 and "lifted" in p.stdout)
check("quarantine.json is empty again", json.loads((TMP / "gate" / "quarantine.json").read_text()) == {})

# ---- 2. an unclassified 403 pauses everything for investigation -------------------------------------------------
n = len(fake_calls(TMP, "gh"))
t0 = time.time()
try:
    gh.pr_list(["number"])
    raised = False
except github.GitHubError:
    raised = True
check("the bare 403 surfaces to the caller as the failure it was", raised)
st = state(TMP)
check(
    "state is COOLDOWN, reason unclassified-403",
    (st.get("state"), st.get("reason")) == (G.COOLDOWN, "unclassified-403"),
)
check("…for 15 minutes", 890 <= float(st.get("until")) - t0 <= 905)
check("exactly one gh call met the 403", len(fake_calls(TMP, "gh")) == n + 1)
until = float(st["until"])
p = github.gh_run(["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"], max_wait=0)
check("a read during the pause is refused cooldown", p.returncode == 75 and "(cooldown)" in p.stderr)
check("…carrying the same until", abs(p.gate_refused.until - until) < 1)
rc = round_mod.run_claim_sh(["renew", "branch/7"], "alice/tauceti-claims")
check("a claim renew during the pause is refused (rc 2)", rc == 2)
try:
    github.github_budget()
    probed = True
except G.GateRefused as e:
    probed = False
    check(
        "the rate_limit probe is refused cooldown with the same until",
        e.reason == G.R_COOLDOWN and abs(e.until - until) < 1,
    )
check("the rate_limit probe did not run", not probed)
check(
    "nothing more reached gh or git during the pause",
    len(fake_calls(TMP, "gh")) == n + 1 and fake_calls(TMP, "git") == [],
)
p = gate_cli(["resume"], env)
check(
    "`tauceti-gate resume` clears the cooldown and prints the incident",
    p.returncode == 0 and "unclassified-403" in p.stdout and state(TMP).get("state") == G.RUNNING,
)

# ---- 3. a git push rejected for permission is a quarantine of that (op, repo) ----------------------------------------
rc = round_mod.run_claim_sh(["acquire", "x", "60"], "alice/tauceti-claims")
check("claim.sh reports the rejected push as rc 1 or 2 (not a crash)", rc in (1, 2))
q = json.loads((TMP / "gate" / "quarantine.json").read_text())
check(
    "quarantine.json holds acquire:alice/tauceti-claims (permission-denied)",
    q.get("acquire:alice/tauceti-claims", {}).get("reason") == "permission-denied",
)
check("the store is neither halted nor cooling down", state(TMP).get("state") == G.RUNNING)
n = len(fake_calls(TMP, "git"))
rc = round_mod.run_claim_sh(["acquire", "x", "60"], "alice/tauceti-claims")
check("a second acquire is refused quarantined with no git (no loop)", rc == 2 and len(fake_calls(TMP, "git")) == n)
rc = round_mod.run_claim_sh(["holds", "x"], "alice/tauceti-claims")
check("a read of the same namespace is still admitted (different op)", rc in (0, 1) and len(fake_calls(TMP, "git")) > n)

finish()

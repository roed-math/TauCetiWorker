#!/usr/bin/env python3
"""T02 — one client sees invalid-credential evidence; the whole fleet halts for good (brief §5.2
acceptance, §8.3 T02).

A fake `gh` answers one worker's `gh api repos/…` with HTTP 401 Bad credentials through the real
gh_run. Asserted afterwards, from OTHER clients: the heartbeat's `claim.sh renew` (run_claim_sh, as
cmd_heartbeat calls it) is refused and the fake git is never run; an escalation `gh issue create` is
refused `halted` through gh_run without spawning gh; the CLI in a fresh process refuses `admit` and
reports HALTED_MANUAL; halt.json exists in the gate dir in the identity gate's shape; the identity
gate refuses to start a round; `tauceti-gate resume` does NOT clear it; and after a store re-open
(new process) it is still halted. Then the mirror image: an identity-gate halt (401 on `gh api
user`) halts the fleet store too. Finally the suspension shape (403 + "suspended") halts the same way.

Proven by mock (fake gh/git, real gate store, real wrappers). Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import (  # noqa: E402
    FAKES,
    SCRIPTS,
    check,
    fake_calls,
    fake_env,
    finish,
    gate_cli,
    gate_env,
    mktemp,
    scrub_env,
)

scrub_env()
TMP = mktemp("gate-t02-")
gate_env(TMP)
SCENARIO = {
    "gh": [
        {"match": ["api", "repos/TauCetiProject/TauCeti"], "rc": 1, "stderr": "gh: Bad credentials (HTTP 401)\n"},
        {"match": ["api", "user"], "rc": 1, "stderr": "gh: Bad credentials (HTTP 401)\n"},
    ],
    "git": [],
    "bare_repos": {},
}
env = fake_env(TMP, SCENARIO, CLAIM_REPO="alice/tauceti-claims", CLAIM_GITDIR=str(TMP / "scratch.git"))
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
            "CLAIM_REPO",
        )
    }
)

from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import github, identity  # noqa: E402
from tauceti_worker import round as round_mod  # noqa: E402

# ---- 1. one worker's call meets a 401 --------------------------------------------------------------------
p = github.gh_run(["gh", "api", "repos/TauCetiProject/TauCeti", "--jq", ".permissions.push"])
check("the 401 reaches the caller as the gh failure it was", p.returncode == 1 and "401" in p.stderr)
st = json.loads((TMP / "gate" / "state.json").read_text())
check(
    "state.json is HALTED_MANUAL with reason invalid-credentials",
    (st.get("state"), st.get("reason")) == (G.HALTED_MANUAL, "invalid-credentials"),
)
halt = TMP / "gate" / "halt.json"
check("halt.json exists in the gate dir", halt.exists())
rec = json.loads(halt.read_text())
check(
    "halt.json has the identity gate's shape",
    {"reason", "detail", "at", "login", "login_expected", "source"} <= set(rec),
)
check(
    "halt.json names the reason and the sanitized detail",
    rec["reason"] == "invalid-credentials" and "401" in rec["detail"],
)
n_gh = len(fake_calls(TMP, "gh"))

# ---- 2. every other client's next admit is refused `halted` ------------------------------------------------
rc = round_mod.run_claim_sh(["renew", "branch/7"], "alice/tauceti-claims")
check("a heartbeat renew is refused (rc 2)", rc == 2)
check("…and claim.sh never ran git", fake_calls(TMP, "git") == [])
p = github.gh_run(
    ["gh", "issue", "create", "--repo", "TauCetiProject/TauCeti", "--title", "Review stuck: PR #1", "--body", "x"]
)
check("an escalation issue create is refused halted", p.returncode == 75 and "gate: refused (halted)" in p.stderr)
p = github.gh_run(["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"])
check("a plain read is refused halted too", p.returncode == 75 and "(halted)" in p.stderr)
check("no further gh was spawned by any of them", len(fake_calls(TMP, "gh")) == n_gh)
rc = round_mod.run_claim_sh(["release", "branch/7"], "alice/tauceti-claims")
check(
    "a cleanup-time release is refused as well (the lease is left to expire)", rc == 2 and fake_calls(TMP, "git") == []
)
p = subprocess.run([str(SCRIPTS / "claim.sh"), "renew", "branch/7"], env=env, capture_output=True, text=True)
check(
    "claim.sh invoked directly (the agent's path) is refused before git",
    p.returncode == 2 and fake_calls(TMP, "git") == [],
)
p = subprocess.run(
    [str(SCRIPTS / "shim" / "gh"), "pr", "list", "--repo", "TauCetiProject/TauCeti"],
    env=env,
    capture_output=True,
    text=True,
)
check(
    "the agent's gh shim is refused halted",
    p.returncode == 75 and "(halted)" in p.stderr and len(fake_calls(TMP, "gh")) == n_gh,
)

# ---- 3. a fresh process (restart) sees the halt -------------------------------------------------------------
p = gate_cli(["admit", "api_read", "survey", "TauCetiProject/TauCeti", "--no-wait"], env)
check("a new process: admit refused halted", p.returncode == 75 and "(halted)" in p.stderr)
p = gate_cli(["status", "--json"], env)
check(
    "a new process: status reports HALTED_MANUAL and the halt record",
    p.returncode == 0
    and json.loads(p.stdout).get("state") == G.HALTED_MANUAL
    and json.loads(p.stdout).get("halt", {}).get("reason") == "invalid-credentials",
)
p = gate_cli(["resume"], env)
check(
    "`tauceti-gate resume` prints the incident but does not clear a halt",
    p.returncode == 0 and "still HALTED_MANUAL" in p.stdout,
)
p = gate_cli(["status"], env)
check("still halted after resume", "HALTED_MANUAL" in p.stdout)
try:
    identity.refuse_if_halted(TMP / "state")
    started = True
except identity.Halted as e:
    started = False
    msg = str(e)
check("the identity gate refuses to start a loop/round over the fleet halt", not started and "halted" in msg)
try:
    identity.gate(TMP / "state", "w1", where="round start")
    gated = True
except identity.Halted:
    gated = False
check("identity.gate() halts before spending its own read", not gated and len(fake_calls(TMP, "gh")) == n_gh)

# ---- 4. the mirror image: an identity halt halts the fleet store ------------------------------------------------
TMP2 = mktemp("gate-t02b-")
gate_env(TMP2)
env2 = fake_env(TMP2, SCENARIO)
os.environ.update({k: env2[k] for k in ("TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG")})
G._CURRENT = None
try:
    identity.gate(TMP2 / "state", "w1", where="round start")
    gated = True
except identity.Halted:
    gated = False
check("a 401 on `gh api user` halts the identity gate", not gated)
check("…and writes the per-worker halt.json", (TMP2 / "state" / "halt.json").exists())
st2 = json.loads((TMP2 / "gate" / "state.json").read_text())
check(
    "…and the fleet store is HALTED_MANUAL too",
    st2.get("state") == G.HALTED_MANUAL and (TMP2 / "gate" / "halt.json").exists(),
)
check("the identity read was one gh call", len(fake_calls(TMP2, "gh")) == 1)

# ---- 5. the suspension shape -------------------------------------------------------------------------------
TMP3 = mktemp("gate-t02c-")
gate_env(TMP3)
env3 = fake_env(
    TMP3,
    {
        "gh": [{"match": ["pr", "list"], "rc": 1, "stderr": "gh: Sorry. Your account was suspended. (HTTP 403)\n"}],
        "git": [],
        "bare_repos": {},
    },
)
os.environ.update({k: env3[k] for k in ("TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG")})
G._CURRENT = None
p = github.gh_run(["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"])
st3 = json.loads((TMP3 / "gate" / "state.json").read_text())
check(
    "403 + suspended halts with reason account-halt",
    (st3.get("state"), st3.get("reason")) == (G.HALTED_MANUAL, "account-halt"),
)
check(
    "no gh call ran `gh auth` in any of this",
    not any(a[:1] == ["auth"] for a in fake_calls(TMP, "gh") + fake_calls(TMP2, "gh") + fake_calls(TMP3, "gh")),
)
check("the fakes were the only gh", all(Path(os.environ["TAUCETI_REAL_GH"]) == FAKES / "gh" for _ in [0]))

finish()

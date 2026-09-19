#!/usr/bin/env python3
"""T16 — the controller's storage is unavailable, or its recent budget state cannot be read (brief
§8.3 T16): remote operations fail closed; local work continues; the failure is reported locally.

  * budget.json is corrupt         → admit refused `store-error` (no fresh allowance is invented);
                                     gh_run returns rc 75 without spawning gh; run_claim_sh returns 2
                                     without git; github_budget raises GateRefused(store-error);
                                     `tauceti-gate status` prints the error; the loop's preflight probe
                                     reports it; local claim locks still work; repairing the file
                                     restores admission.
  * the directory cannot be written (chmod 0)
                                   → the same, from the lock itself; `status` still answers.
  * TAUCETI_GATE_REQUIRED=1 with no TAUCETI_GATE_DIR
                                   → Gate.from_env() is a hard error, never a silent bypass; `status`
                                     prints it.

Proven by mock. Exit 0 = all hold; 1 = a mismatch. (Skips the chmod case when running as root.)
"""

import json
import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, fake_calls, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env  # noqa: E402

scrub_env()
TMP = mktemp("gate-t16-")
os.environ["TAUCETI_LOCAL_CLAIMS_DIR"] = str(TMP / "local-locks")
env = gate_env(TMP)
env = fake_env(TMP, {"gh": [{"match": ["pr", "list"], "rc": 0, "stdout": "[]\n"}], "git": [], "bare_repos": {}})
os.environ.update(
    {k: env[k] for k in ("PATH", "TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG", "TAUCETI_REAL_GH", "TAUCETI_REAL_GIT")}
)

from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import github, local_claims  # noqa: E402
from tauceti_worker import round as round_mod  # noqa: E402

g = G.Gate.from_env()
a = g.admit("warm", "TauCetiProject/TauCeti", G.API_READ, wait=False)
g.record(a, G.Outcome(ok=True))
check("a healthy store admits", bool(a.token))

# ---- 1. budget.json corrupt --------------------------------------------------------------------------------------
(TMP / "gate" / "budget.json").write_text('{"windows": {"api_read": [1, 2, 3')
try:
    g.admit("survey", "TauCetiProject/TauCeti", G.API_READ, wait=False)
    reason = None
except G.GateRefused as e:
    reason = e.reason
check("admit with a corrupt budget.json is refused store-error", reason == G.R_STORE)
n = len(fake_calls(TMP, "gh"))
p = github.gh_run(["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"])
check(
    "gh_run returns rc 75 `gate: refused (store-error)` and spawns no gh",
    p.returncode == 75 and "(store-error)" in p.stderr and len(fake_calls(TMP, "gh")) == n,
)
rc = round_mod.run_claim_sh(["renew", "branch/1"], "alice/tauceti-claims")
check("run_claim_sh returns 2 and runs no git", rc == 2 and fake_calls(TMP, "git") == [])
try:
    github.github_budget()
    raised = None
except G.GateRefused as e:
    raised = e.reason
check("github_budget (the loop preflight's probe) raises GateRefused(store-error)", raised == G.R_STORE)
v = G.current().probe()
check(
    "the loop's gate probe reports the store error locally",
    v.get("state") == "STORE-ERROR" and "corrupt" in v.get("error", ""),
)
p = gate_cli(["status"], env)
check(
    "`tauceti-gate status` prints the error and exits 0",
    p.returncode == 0 and "STORE-ERROR" in p.stdout and "corrupt" in p.stdout,
)
p = gate_cli(["status", "--json"], env)
check("…as JSON too", p.returncode == 0 and json.loads(p.stdout).get("state") == "STORE-ERROR")
lease = local_claims.LocalLease.acquire("branch/1", "w16")
check("local work continues: the host-local claim lock is unaffected", lease is not None)
if lease:
    lease.release()
check("nothing reached gh or git", len(fake_calls(TMP, "gh")) == n and fake_calls(TMP, "git") == [])
(TMP / "gate" / "budget.json").write_text(json.dumps({"windows": {}, "inflight": [], "transient": {}}))
a = g.admit("survey", "TauCetiProject/TauCeti", G.API_READ, wait=False)
g.record(a, G.Outcome(ok=True))
check("repairing budget.json restores admission", bool(a.token))

# ---- 2. the directory cannot be written -----------------------------------------------------------------------------
if os.geteuid() == 0:
    check("chmod case skipped as root", True)
else:
    (TMP / "gate").chmod(0)
    try:
        g.admit("survey", "TauCetiProject/TauCeti", G.API_READ, wait=False)
        reason = None
    except G.GateRefused as e:
        reason = e.reason
    check("an unreadable directory: admit refused store-error", reason == G.R_STORE)
    p = github.gh_run(["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"])
    check("gh_run: rc 75, no gh spawned", p.returncode == 75 and len(fake_calls(TMP, "gh")) == n)
    p = gate_cli(["status"], env)
    check("`tauceti-gate status` still answers, naming the error", p.returncode == 0 and "STORE-ERROR" in p.stdout)
    (TMP / "gate").chmod(stat.S_IRWXU)
    a = g.admit("survey", "TauCetiProject/TauCeti", G.API_READ, wait=False)
    g.record(a, G.Outcome(ok=True))
    check("restoring permissions restores admission", bool(a.token))

# ---- 3. required, but no directory ----------------------------------------------------------------------------------
os.environ.pop("TAUCETI_GATE_DIR")
os.environ["TAUCETI_GATE_REQUIRED"] = "1"
try:
    G.Gate.from_env()
    died = False
except Exception as e:  # Die
    died = "TAUCETI_GATE_DIR" in str(e)
check("TAUCETI_GATE_REQUIRED=1 without TAUCETI_GATE_DIR is a hard error, not a bypass", died)
p = gate_cli(["status"], {**env, "TAUCETI_GATE_DIR": "", "TAUCETI_GATE_REQUIRED": "1"})
check("`tauceti-gate status` prints that error", p.returncode == 0 and "TAUCETI_GATE_DIR" in p.stdout)
os.environ.pop("TAUCETI_GATE_REQUIRED")
check("with neither set, the gate is the no-op and nothing is refused", not G.Gate.from_env().enabled)

finish()

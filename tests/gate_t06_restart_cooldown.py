#!/usr/bin/env python3
"""T06 — restart during a cooldown; separately, a second controller (brief §8.3 T06).

A 429 with `Retry-After: 2` puts the store in COOLDOWN. Then, in NEW processes: `tauceti-gate
status` reports the cooldown with the same `until`; `admit` is refused `cooldown`; a second
"controller" (an independent Gate instance in another process pointed at the same directory)
sees exactly the same state and cannot admit either — there is no second allowance to start from.
After the cooldown lapses there is no burst: the first mutation is admitted, the very next one is
refused `spacing` (the 5 s minimum between mutations) even with a wait, and the per-minute window
still counts what was admitted before the cooldown.

Proven by mock (the one real wait is the 2 s cooldown). Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env, state  # noqa: E402

scrub_env()
TMP = mktemp("gate-t06-")
env = gate_env(TMP, TAUCETI_GATE_ADMIT_WAIT="1")
env = fake_env(
    TMP,
    {
        "gh": [{"match": ["pr", "list"], "rc": 1, "stderr": "gh: HTTP 429: Too Many Requests\nRetry-After: 2\n"}],
        "git": [],
        "bare_repos": {},
    },
)
os.environ.update(
    {k: env[k] for k in ("PATH", "TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG", "TAUCETI_REAL_GH", "TAUCETI_REAL_GIT")}
)

from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import github  # noqa: E402

g = G.Gate.from_env()
# Two mutations before the cooldown, so the window has something to remember across the restart.
for i in range(2):
    a = g.admit(f"before-{i}", "TauCetiProject/TauCeti", G.API_MUTATION, wait=False)
    g.record(a, G.Outcome(ok=True, status=201))
    b = json.loads((TMP / "gate" / "budget.json").read_text())
    b["last_mutation_at"] = time.time() - 10  # step past the spacing without waiting
    (TMP / "gate" / "budget.json").write_text(json.dumps(b))
t0 = time.time()
p = github.gh_run(["gh", "pr", "list", "--repo", "TauCetiProject/TauCeti", "--json", "number"], max_wait=0)
st = state(TMP)
check(
    "a 429 with Retry-After: 2 puts the store in COOLDOWN",
    st.get("state") == G.COOLDOWN and 1 <= float(st["until"]) - t0 <= 3,
)
until = float(st["until"])

# ---- 1. restart: fresh processes read the persisted state -------------------------------------------------------------
p = gate_cli(["status", "--json"], env)
v = json.loads(p.stdout)
check(
    "a new process: status is COOLDOWN with the same until",
    v.get("state") == G.COOLDOWN and abs(float(v["until"]) - until) < 0.01,
)
p = gate_cli(["admit", "api_read", "survey", "TauCetiProject/TauCeti", "--no-wait"], env)
check("a new process: admit refused cooldown", p.returncode == 75 and "(cooldown)" in p.stderr)
p = gate_cli(["admit", "api_read", "rate_limit", "-", "--no-wait"], env)
check("a new process: the rate_limit probe is refused too", p.returncode == 75 and "(cooldown)" in p.stderr)

# ---- 2. a second controller: an independent instance, same directory, same verdict ---------------------------------------
controller = TMP / "second_controller.py"
controller.write_text(
    f"import sys, json\nsys.path.insert(0, {str(REPO)!r})\nfrom tauceti_worker import gate as G\n"
    "g = G.Gate.from_env()\nprint(json.dumps(g.probe()))\n"
    "try:\n    g.admit('second-controller', 'TauCetiProject/TauCeti', G.API_MUTATION, wait=False); print('ADMITTED')\n"
    "except G.GateRefused as e:\n    print('REFUSED', e.reason, e.until)\n"
)
p = subprocess.run([sys.executable, str(controller)], env=env, capture_output=True, text=True)
lines = p.stdout.strip().splitlines()
v2 = json.loads(lines[0]) if lines else {}
check(
    "the second controller sees the same persisted state",
    v2.get("state") == G.COOLDOWN and abs(float(v2["until"]) - until) < 0.01,
)
check("…and cannot admit a mutation of its own", len(lines) > 1 and lines[1].startswith("REFUSED cooldown"))

# ---- 3. after the cooldown: no burst -----------------------------------------------------------------------------------------
time.sleep(max(0.0, until - time.time()) + 0.2)
g2 = G.Gate.from_env()  # a "restarted" worker
a = g2.admit("after-0", "TauCetiProject/TauCeti", G.API_MUTATION, wait=False)
check("after the cooldown the first mutation is admitted", bool(a.token))
check("…and the store is RUNNING again with the cooldown noted as expired", state(TMP).get("state") == G.RUNNING)
g2.record(a, G.Outcome(ok=True, status=201))
t1 = time.time()
try:
    g2.admit("after-1", "TauCetiProject/TauCeti", G.API_MUTATION, wait=True)
    burst = True
except G.GateRefused as e:
    burst = False
    check(
        "the very next mutation is refused `spacing` (5 s minimum), even with a wait",
        e.reason == G.R_SPACING and 4 <= e.until - t1 <= 5.5,
    )
check("no burst after the cooldown", not burst)
check("the wait was bounded by TAUCETI_GATE_ADMIT_WAIT (not the full spacing)", time.time() - t1 < 3)
p = gate_cli(["status", "--json"], env)
v = json.loads(p.stdout)
check(
    "the per-minute window still counts the mutations admitted before the cooldown (2 + 1)",
    v["budgets"]["api_mutation"]["minute"] == 3,
)

finish()

#!/usr/bin/env python3
"""T01 — seven workers, a dashboard and interactive automation share ONE allowance with bounded
concurrency, and a restart neither resets nor doubles it (brief §4.3 acceptance, §8.3 T01).

Eight processes (seven "workers" admitting api_mutation, one "dashboard" admitting api_read) hammer
the same TAUCETI_GATE_DIR for two seconds with the per-minute mutation cap set to 6. Each admitted
client reads the in-flight registry while it holds its slot and records the largest it ever saw.
Asserted: no client ever saw more than one in flight on the API lane; the mutation admits across all
seven never exceed the cap; every refusal is `busy` or `budget`; and a NEW process opening the same
store afterwards sees the same counters (the next mutation is refused `budget` with `until` inside
the original minute). Then the interactive automation: an admit from this process, holding the slot,
makes every other client wait or be refused busy — never a second dispatch.

Proven by mock: the gate store and its processes are real, the GitHub side is absent.
Exit 0 = all assertions hold; 1 = a mismatch.
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

from harness import check, finish, gate_env, mktemp, scrub_env  # noqa: E402

CAP_MIN = 6


def client(kind: str, out: Path, seconds: float) -> None:
    """One hammering process: admit as fast as allowed, hold each slot briefly, record."""
    from tauceti_worker import gate as G

    g = G.Gate.from_env()
    admitted = 0
    max_inflight = 0
    reasons: dict[str, int] = {}
    end = time.time() + seconds
    while time.time() < end:
        try:
            a = g.admit("hammer", "TauCetiProject/TauCeti", kind, wait=False)
        except G.GateRefused as e:
            reasons[e.reason] = reasons.get(e.reason, 0) + 1
            time.sleep(0.01)
            continue
        admitted += 1
        try:
            b = json.loads((Path(os.environ["TAUCETI_GATE_DIR"]) / "budget.json").read_text())
            api_lane = [e for e in b.get("inflight", []) if e.get("lane") == "api"]
            max_inflight = max(max_inflight, len(api_lane))
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
        g.record(a, G.Outcome(ok=True, status=200))
    out.write_text(json.dumps({"kind": kind, "admitted": admitted, "max_inflight": max_inflight, "reasons": reasons}))


if len(sys.argv) > 1 and sys.argv[1] == "client":
    client(sys.argv[2], Path(sys.argv[3]), float(sys.argv[4]))
    sys.exit(0)

scrub_env()
TMP = mktemp("gate-t01-")
env = gate_env(
    TMP,
    TAUCETI_GATE_MUTATIONS_PER_MINUTE=str(CAP_MIN),
    TAUCETI_GATE_MUTATIONS_PER_HOUR="1000",
    TAUCETI_GATE_MUTATION_SPACING="0",
)
env["PYTHONPATH"] = str(REPO)

from tauceti_worker import gate as G  # noqa: E402

# ---- 1. eight processes hammer one store -----------------------------------------------------------------
t0 = time.time()
procs = []
outs = []
for i in range(8):
    kind = G.API_READ if i == 7 else G.API_MUTATION
    out = TMP / f"client-{i}.json"
    outs.append(out)
    procs.append(subprocess.Popen([sys.executable, __file__, "client", kind, str(out), "2.0"], env=env))
for p in procs:
    p.wait(30)
results = [json.loads(o.read_text()) for o in outs if o.exists()]
check("all eight clients reported", len(results) == 8)
mutations = sum(r["admitted"] for r in results if r["kind"] == G.API_MUTATION)
reads = sum(r["admitted"] for r in results if r["kind"] == G.API_READ)
check(
    f"mutation admits across seven workers never exceed the per-minute cap ({mutations} <= {CAP_MIN})",
    mutations <= CAP_MIN,
)
check(f"the cap was actually reached ({mutations} == {CAP_MIN})", mutations == CAP_MIN)
check(f"the dashboard's reads were admitted too ({reads} > 0)", reads > 0)
check("no client ever saw more than one in flight on the API lane", all(r["max_inflight"] <= 1 for r in results))
reasons = set()
for r in results:
    reasons |= set(r["reasons"])
check(f"every refusal was busy or budget ({sorted(reasons)})", reasons <= {G.R_BUSY, G.R_BUDGET})
check("refusals happened (the clients did contend)", any(r["reasons"] for r in results))

# ---- 2. restart: a new process opening the store sees the same allowance ----------------------------------
p = subprocess.run(
    [sys.executable, "-m", "tauceti_worker", "gate", "status", "--json"], env=env, capture_output=True, text=True
)
st = json.loads(p.stdout or "{}")
check("a fresh process reads the store", p.returncode == 0 and st.get("state") == G.RUNNING)
check(
    f"its per-minute mutation counter equals what the clients admitted ({st.get('budgets', {}).get('api_mutation', {}).get('minute')} == {mutations})",
    st.get("budgets", {}).get("api_mutation", {}).get("minute") == mutations,
)
check("nothing is left in flight after the clients recorded", st.get("inflight") == [])
p = subprocess.run(
    [
        sys.executable,
        "-m",
        "tauceti_worker",
        "gate",
        "admit",
        "api_mutation",
        "one-more",
        "TauCetiProject/TauCeti",
        "--no-wait",
    ],
    env=env,
    capture_output=True,
    text=True,
)
check(
    "a mutation after the restart is refused `budget` (no fresh allowance)",
    p.returncode == 75 and "gate: refused (budget)" in p.stderr,
)
ev = [json.loads(line) for line in (TMP / "gate" / "events.log").read_text().splitlines()]
last = [e for e in ev if e.get("decision") == "refuse" and e.get("op") == "one-more"]
check(
    "the refusal names when the slot frees, inside the original minute",
    bool(last) and t0 < float(last[-1]["until"]) <= t0 + 61,
)

# ---- 3. interactive automation holds the one API slot: others wait or are refused busy --------------------
g = G.Gate.from_env()
held = g.admit("interactive", "TauCetiProject/TauCeti", G.API_READ)
p = subprocess.run(
    [
        sys.executable,
        "-m",
        "tauceti_worker",
        "gate",
        "admit",
        "api_read",
        "dashboard",
        "TauCetiProject/TauCeti",
        "--no-wait",
    ],
    env=env,
    capture_output=True,
    text=True,
)
check("while one API request is in flight, another client is refused busy", p.returncode == 75 and "(busy)" in p.stderr)
g.record(held, G.Outcome(ok=True, status=200))
p = subprocess.run(
    [
        sys.executable,
        "-m",
        "tauceti_worker",
        "gate",
        "admit",
        "api_read",
        "dashboard",
        "TauCetiProject/TauCeti",
        "--no-wait",
    ],
    env=env,
    capture_output=True,
    text=True,
)
check("once recorded, the slot is free again", p.returncode == 0 and p.stdout.strip())

# ---- 4. a dead owner's in-flight entry is reaped, never a permanent block ---------------------------------
b = json.loads((TMP / "gate" / "budget.json").read_text())
b["inflight"] = [
    {
        "token": "ghost",
        "pid": 999999,
        "op": "x",
        "target": "-",
        "kind": "api_read",
        "lane": "api",
        "started": time.time(),
        "deadline": time.time() + 900,
    }
]
(TMP / "gate" / "budget.json").write_text(json.dumps(b))
try:
    a = g.admit("after-ghost", "TauCetiProject/TauCeti", G.API_READ, wait=False)
    g.record(a, G.Outcome(ok=True))
    ok = True
except G.GateRefused:
    ok = False
check("an in-flight entry whose owner pid is dead is reaped on the next admit", ok)

finish()

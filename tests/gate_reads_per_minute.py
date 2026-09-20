#!/usr/bin/env python3
"""The per-minute read cap: a burst of reads is admitted up to the cap within one minute, the next is
refused `budget` with a wake time inside the window, and after the window slides the reads flow again.
Runs against the real store with a controllable clock (gate.now); no network."""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures = []


def check(name, cond):
    (print("[OK ]", name) if cond else (failures.append(name), print("[XX ]", name)))


tmp = Path(tempfile.mkdtemp())
os.environ.update(
    {
        "TAUCETI_GATE_DIR": str(tmp),
        "TAUCETI_GATE_REQUIRED": "1",
        "TAUCETI_GATE_READS_PER_HOUR": "1000",
        "TAUCETI_GATE_READS_PER_MINUTE": "5",
        "TAUCETI_GATE_READS_RESERVE": "0",
        "TAUCETI_GATE_MAX_INFLIGHT_API": "10",
    }
)
from tauceti_worker import gate as G  # noqa: E402

g = G.Gate.from_env()
clock = [1_000_000.0]
g.now = lambda: clock[0]

admitted = 0
refused = None
for i in range(7):
    try:
        a = g.admit(f"read{i}", "TauCetiProject/TauCeti", G.API_READ)
        g.record(a, G.Outcome(ok=True, status=200))
        admitted += 1
    except G.GateRefused as e:
        refused = e
        break
check("five reads admitted inside one minute", admitted == 5)
check("the sixth is refused with reason budget", refused is not None and refused.reason == G.R_BUDGET)
check(
    "the wake time, when given, is inside the minute window",
    refused is not None and (refused.until is None or 0 < (refused.until - clock[0]) <= 61),
)

clock[0] += 61
try:
    a = g.admit("read-later", "TauCetiProject/TauCeti", G.API_READ)
    g.record(a, G.Outcome(ok=True, status=200))
    again = True
except G.GateRefused:
    again = False
check("after the window slides a read is admitted again", again)

st = g.status()
win = st.get("budgets") or {}
check("status reports the per-minute read window and cap", (win.get("api_read") or {}).get("cap_minute") == 5)

if failures:
    print("gate_reads_per_minute: FAILED", failures)
    sys.exit(1)
print("gate_reads_per_minute: all cases passed")

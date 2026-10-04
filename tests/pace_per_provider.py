#!/usr/bin/env python3
"""Per-provider pacing (2026-10-04): $TAUCETI_PACE_CLAUDE / $TAUCETI_PACE_CODEX override $TAUCETI_PACE
for that provider's windows only. Offline checks: the curve each provider gets, a window classified
under each, the pace-recovery clock following the window's provider, Claude's idle-window policy, and
the CLI rejecting a malformed per-provider curve. Exit 0 = all hold; 1 = a mismatch."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import cli  # noqa: E402
from tauceti_worker import quota as Q  # noqa: E402
from tauceti_worker.config import Die  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


for v in ("TAUCETI_PACE", "TAUCETI_PACE_CLAUDE", "TAUCETI_PACE_CODEX"):
    os.environ.pop(v, None)
os.environ["TAUCETI_PACE"] = "0:50,100:50"
check("without per-provider curves every provider gets TAUCETI_PACE",
      Q.pace_curve("claude") == Q.pace_curve("codex") == Q.pace_curve() == [(0.0, 50.0), (100.0, 50.0)])
os.environ["TAUCETI_PACE_CODEX"] = "0:100,100:100"
check("TAUCETI_PACE_CODEX applies to codex only",
      Q.pace_curve("codex") == [(0.0, 100.0), (100.0, 100.0)] and Q.pace_curve("claude") == [(0.0, 50.0), (100.0, 50.0)])
os.environ["TAUCETI_PACE_CLAUDE"] = "0:10,100:10"
check("TAUCETI_PACE_CLAUDE applies to claude only", Q.pace_curve("claude") == [(0.0, 10.0), (100.0, 10.0)])
check("an unnamed caller still gets TAUCETI_PACE", Q.pace_curve() == [(0.0, 50.0), (100.0, 50.0)])

cl = Q._classify_window("weekly", 30.0, 50.0, time.time() + 3600, False, "claude")
cx = Q._classify_window("weekly", 30.0, 50.0, time.time() + 3600, False, "codex")
check("the same reading is over pace for claude and under pace for codex",
      cl.status == Q.STATUS_OVER_PACE and cx.status == Q.STATUS_UNDER_PACE and cl.provider == "claude", f"{cl} {cx}")
r = Q.Reading("weekly", "active", used=5.0, resets_at=time.time() + 3 * 24 * 3600)
w = Q._window_from_reading(r)
check("a Claude reading is paced on the Claude curve", w.provider == "claude" and w.budget == 10.0, str(w))

os.environ["TAUCETI_PACE_CLAUDE"] = "0:0,60:0,100:100"
now = time.time()
held = Q._classify_window("weekly", 20.0, 50.0, now + 3.5 * 24 * 3600, False, "claude")
free = Q._pace_free_at(held, now)
check("the recovery clock follows the window's own curve", held.status == Q.STATUS_OVER_PACE and free is not None and free > now, str(held))
check("Claude's idle-window policy reads the Claude curve", Q._idle_init_block() is not None)
os.environ["TAUCETI_PACE_CLAUDE"] = "0:20,100:100"
check("…and allows a bootstrap when that curve does", Q._idle_init_block() is None)

os.environ["TAUCETI_PACE_CODEX"] = "50"
try:
    cli.resolve_pace("status", SimpleNamespace(pace=None))
    rejected = False
except Die as e:
    rejected = "TAUCETI_PACE_CODEX" in str(e)
check("the CLI rejects a malformed per-provider curve, naming the variable", rejected)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

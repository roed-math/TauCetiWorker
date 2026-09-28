#!/usr/bin/env python3
"""An idle worker sleeps until something can change, not 60 s (2026-09-27: 13 surveys/hour for 5
reviews). Checks loop.idle_nap and the hint a round leaves for its loop.
Exit 0 = all hold; 1 = a mismatch."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import loop  # noqa: E402
from tauceti_worker.constants import BACKOFF_MAX, IDLE_SURVEY_FLOOR, NEXT_ELIGIBLE_COUNTER  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


check("a short backoff is raised to the floor", loop.idle_nap(60, None, now=0) == IDLE_SURVEY_FLOOR)
check("a hint sooner than the floor still waits the floor", loop.idle_nap(60, 40, now=0) == IDLE_SURVEY_FLOOR)
check("a hint later than the floor is followed", loop.idle_nap(60, IDLE_SURVEY_FLOOR + 100, now=0) == IDLE_SURVEY_FLOOR + 100)
check("…but never past the backoff cap", loop.idle_nap(60, 10 * BACKOFF_MAX, now=0) == BACKOFF_MAX)
check("with no hint a long backoff stands", loop.idle_nap(BACKOFF_MAX, None, now=0) == BACKOFF_MAX)

state = Path(tempfile.mkdtemp())
cfg = SimpleNamespace(state=state)
(state / NEXT_ELIGIBLE_COUNTER).write_text("12345\n")
check("the round's hint is read", loop._take_next_eligible(cfg) == 12345.0)
check("…and consumed, so a later round does not inherit it", loop._take_next_eligible(cfg) is None)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

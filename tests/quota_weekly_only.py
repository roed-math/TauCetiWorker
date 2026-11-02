#!/usr/bin/env python3
"""Subscription accounts with no session window (seen on every fleet account from 2026-10-30).

The usage endpoint reports `five_hour: null` and its `limits` array carries only weekly entries. The
pacer must launch on the weekly alone, and must reuse that payload for CLAUDE_IDLE_CACHE_S instead of
re-reading it every poll. Dependency-free; no network.
"""

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import quota as q

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def iso(delta_s: float) -> str:
    return datetime.fromtimestamp(time.time() + delta_s, UTC).isoformat()


def payload(weekly_pct: float, weekly_reset_s: float = 2 * 86400) -> dict:
    """The shape gqw's claude-1 returned on 2026-11-02, trimmed to the fields the pacer reads."""
    return {
        "five_hour": None,
        "seven_day": {"utilization": weekly_pct, "resets_at": iso(weekly_reset_s)},
        "limits": [
            {"kind": "weekly_all", "group": "weekly", "percent": weekly_pct, "resets_at": iso(weekly_reset_s),
             "scope": None, "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "resets_at": iso(weekly_reset_s),
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": False},
        ],
    }


quota = q.Quota.__new__(q.Quota)
p = quota._claude_provider(q._claude_readings(payload(0)))
check("weekly-only account: windows", [(w.name, w.status) for w in p.windows],
      [("session", tc.STATE_IDLE), ("weekly", "under-pace")])
check("weekly-only account launches", (p.available, p.model), (True, "opus"))
check("...and asks for no bootstrap", (p.bootstrap_eligible, p.pending_bootstrap), (False, []))

p = quota._claude_provider(q._claude_readings(payload(100)))
check("an exhausted weekly still blocks", (p.available, tc._unavail_reason(p)[1]), (False, "weekly exhausted"))

now = time.time()
until = q._claude_valid_until(q._claude_readings(payload(0), now), now)
check("an idle payload is cached for CLAUDE_IDLE_CACHE_S", round(until - now), q.CLAUDE_IDLE_CACHE_S)
until = q._claude_valid_until(q._claude_readings(payload(0, weekly_reset_s=60), now), now)
check("...but never past a weekly reset", round(until - now), 60)
check("without its fetch time it is not cached", q._claude_valid_until(q._claude_readings(payload(0)), None), None)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

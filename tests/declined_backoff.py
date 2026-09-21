#!/usr/bin/env python3
"""A round that declined (verdict on record) pauses briefly; a plain no-progress round backs off.

2026-09-21: a run of subsumed PRs pushed two fixers to the 15-minute backoff cap, one strike per
decline, although each decline was a completed judgement the survey now honours. The round child marks
such a round `declined` in the runtime status (NoProgress(declined=True) → report_failure); the loop
then sleeps INTERROUND and leaves the streak untouched, so backoff neither grows nor resets on it.

Exit 0 = the sleeps come out as expected; 1 = a mismatch.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import runtime_status  # noqa: E402

status = Path(tempfile.mkdtemp(prefix="tauceti-declined-backoff-")) / "status.json"
status.write_text("{}")
os.environ["TAUCETI_RUNTIME_STATUS"] = str(status)
os.environ.pop("TAUCETI_ROUND_DONE_CMD", None)

# The rounds the stub plays back: (exit code, declined flag as the child would report it).
script = [(tc.EX_NOPROGRESS, True), (tc.EX_NOPROGRESS, True), (tc.EX_NOPROGRESS, False), (tc.EX_NOPROGRESS, False), (0, False)]
naps: list[float] = []


def fake_round(tail):
    if not script:
        raise KeyboardInterrupt
    rc, declined = script.pop(0)
    if rc == tc.EX_NOPROGRESS:
        runtime_status.report_failure("rebase #1: the agent finished but nothing landed", code=rc, declined=declined)
    return rc


saved = (tc.loop.choose_model, tc.loop.github_budget, tc.loop.run_round_subprocess, tc.loop.time.sleep)
tc.loop.choose_model = lambda *_a, **_k: ("claude", {})
tc.loop.github_budget = lambda: {}
tc.loop.run_round_subprocess = fake_round
tc.loop.time.sleep = lambda s: naps.append(s)
try:
    args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None, source=None, author_model=None, author_effort=None)
    tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["rebase"], agent="claude")
finally:
    tc.loop.choose_model, tc.loop.github_budget, tc.loop.run_round_subprocess, tc.loop.time.sleep = saved

I, B = tc.loop.INTERROUND, tc.loop.BACKOFF_BASE
want = [I, I, B * 2, B * 4, I]  # two declines: short pauses, streak stays 0; then strikes 1 and 2; then a productive round
ok = naps == want
print(("ok   " if ok else "FAIL ") + f"sleeps {naps} (expected {want})")
final = json.loads(status.read_text())
ok2 = final.get("declined") is False
print(("ok   " if ok2 else "FAIL ") + "the last failure report cleared the declined flag")
print("\nALL OK" if ok and ok2 else "\nFAILED")
sys.exit(0 if ok and ok2 else 1)

#!/usr/bin/env python3
"""$TAUCETI_ROUND_DONE_CMD runs after every round with the round's rc and worker id in its
environment; a failing or slow hook is logged, never fatal. Also: the survey exposes the
awaiting-author count of my open PRs, the figure a fleet reconciler sizes fixers by."""

import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import importlib

from tauceti_worker import loop as L  # noqa: E402

S = importlib.import_module("tauceti_worker.survey")  # the package re-exports survey() the function; we need the module

failures = []


def check(name, cond):
    (print("[OK ]", name) if cond else (failures.append(name), print("[XX ]", name)))


tmp = Path(tempfile.mkdtemp())
marker = tmp / "hook-ran"
hook = tmp / "hook.sh"
hook.write_text('#!/bin/sh\necho "$TAUCETI_ROUND_RC $TAUCETI_ROUND_WID" >> "$MARK"\n')
hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
os.environ["MARK"] = str(marker)

os.environ["TAUCETI_ROUND_DONE_CMD"] = str(hook)
L._round_done_hook(75, "gq2-c1")
L._round_done_hook(0, "gq2-fix1")
lines = marker.read_text().splitlines() if marker.exists() else []
check("hook ran once per round with rc and worker id", lines == ["75 gq2-c1", "0 gq2-fix1"])

os.environ["TAUCETI_ROUND_DONE_CMD"] = str(tmp / "missing-hook")
try:
    L._round_done_hook(0, "gq2-c1")
    check("a missing hook is logged, not raised", True)
except Exception as e:  # noqa: BLE001
    check(f"a missing hook is logged, not raised ({e})", False)

os.environ["TAUCETI_ROUND_DONE_CMD"] = ""
L._round_done_hook(0, "gq2-c1")
check("no hook configured: nothing runs", marker.read_text().splitlines() == lines)

sv = S.Survey(worker_id="t")
sv._mine_open_prs = [
    SimpleNamespace(labels=("awaiting-author",)),
    SimpleNamespace(labels=("awaiting-review",)),
    SimpleNamespace(labels=("awaiting-author", "roadmap/X")),
]
check("survey counts my awaiting-author PRs", sv.mine_awaiting_author() == 2)

if failures:
    print("round_done_hook: FAILED", failures)
    sys.exit(1)
print("round_done_hook: all cases passed")

#!/usr/bin/env python3
"""Attempt budgets shared by several fleets ($TAUCETI_SHARED_ATTEMPTS_DIR).

On 2026-11-04 #22428 had spent three rebase attempts on each of gq2, gqm and gqw: the budgets lived in
each fleet's own gate store. With the variable set, every fleet's workers charge one store, and its
files are group read/write so that the other fleet users (one group, setgid directory) can update them.
Dependency-free; no network. Exit 0 = every case agrees.
"""

import importlib
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
survey = importlib.import_module("tauceti_worker.survey")  # `tc.survey` is the function
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


root = Path(tempfile.mkdtemp(prefix="shared-attempts-"))
shared = root / "fleets" / "attempts"
os.environ["TAUCETI_SHARED_ATTEMPTS_DIR"] = str(shared)


def fleet(name: str):
    os.environ["TAUCETI_GATE_DIR"] = str(root / name / "gate")
    return survey.Counters(SimpleNamespace(state=root / name / "worker"))


gq2, gqm = fleet("gq2"), fleet("gqm")
gq2.incr("rebase-pr-22428")
gqm.incr("rebase-pr-22428")
check("two fleets charge one budget", (gq2.read("rebase-pr-22428"), (shared / "rebase-pr-22428").is_file()), (2, True))
check("...not their own gate stores", (root / "gq2" / "gate" / "attempts").exists(), False)
mode = stat.S_IMODE((shared / "rebase-pr-22428").stat().st_mode)
check("its files are group read/write", mode & 0o060, 0o060)
check("...and so is the lock", stat.S_IMODE((shared / ".lock").stat().st_mode) & 0o060, 0o060)
gq2.incr("setup-22428-abc")
check("a worker's setup failures stay its own", ((root / "gq2" / "worker" / "setup-22428-abc").is_file(),
                                                   (shared / "setup-22428-abc").exists()), (True, False))

del os.environ["TAUCETI_SHARED_ATTEMPTS_DIR"]
alone = fleet("gqa")
alone.incr("fix-1-abc")
check("without the variable a fleet keeps its gate store", (root / "gqa" / "gate" / "attempts" / "fix-1-abc").is_file(), True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

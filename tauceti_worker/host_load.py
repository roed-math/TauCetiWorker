"""Hold build-heavy rounds while the host is overloaded, and start them one at a time.

The loop calls `hold` first with no side effects and again with `take` just before it launches. A round's
first build comes minutes after its start, so a limit alone would let every waiting worker through in the
same minute: the slot spaces starts out until each new round's load is visible. The fleets on one host
share a slot directory. A round that has ended adds no load, so `release` hands its slot back at once.

Everything fails open: a host that cannot report its load, or a slot file nobody can write, must not stop
the fleet.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import log

# Reviews stay at 4-6 min at any load (measured 2026-11-06) and these others do no Lean builds.
LIGHT_STAGES = frozenset({"review", "curate", "decide", "progress", "overlap-scan"})
SLOT_FILE = "slot.json"
_warned: set[str] = set()


@dataclass(frozen=True)
class Hold:
    detail: str
    nap: float


def max_load() -> float:
    """The limit from $TAUCETI_MAX_LOAD; 0 turns the gate off."""
    try:
        return max(0.0, float(os.environ.get("TAUCETI_MAX_LOAD") or 0))
    except ValueError:
        _warn_once("max_load", f"TAUCETI_MAX_LOAD is not a number ({os.environ['TAUCETI_MAX_LOAD']!r})")
        return 0.0


def spacing() -> float:
    try:
        return max(0.0, float(os.environ.get("TAUCETI_LOAD_SPACING") or 180))
    except ValueError:
        return 180.0


def current_load() -> float | None:
    """The higher of the 1- and 5-minute averages: a rise shows at once, and a fall must last to count."""
    try:
        one, five, _ = os.getloadavg()
    except OSError:
        return None
    return max(one, five)


def hold(only: Sequence[str], *, take: str | None = None) -> Hold | None:
    """Why a round of these stages must wait, or None. With `take` (a worker id), getting None also takes
    the start slot for that worker."""
    limit = max_load()
    if not limit or (only and set(only) <= LIGHT_STAGES):
        return None
    load = current_load()
    if load is not None and load >= limit:
        return Hold(f"host load {load:.0f}, limit {limit:g}", random.uniform(45, 90))
    gap = spacing()
    directory = os.environ.get("TAUCETI_LOAD_GATE_DIR")
    if not gap or not directory:
        return None
    try:
        wait = _take_slot(Path(directory), gap, take) if take else _slot_wait(Path(directory), gap)
    except OSError as e:
        _warn_once("slot", f"start slot unavailable, starts are not spaced: {e}")
        return None
    return Hold(f"another round started; next start in {wait:.0f}s", wait + random.uniform(0, 20)) if wait else None


def release(worker: str) -> None:
    """Hand back this worker's slot when its round has ended; another worker's slot stands."""
    directory = os.environ.get("TAUCETI_LOAD_GATE_DIR")
    if not max_load() or not directory:
        return
    with contextlib.suppress(OSError), _locked_slot(Path(directory)) as fd:
        if _read_slot(fd).get("worker") == worker:
            os.ftruncate(fd, 0)


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log(f"host load: {message}")


def _read_slot(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        slot = json.loads(os.read(fd, 4096))
    except ValueError:  # empty once released; garbage must not hold anyone
        return {}
    return slot if isinstance(slot, dict) else {}


def _remaining(slot: dict, gap: float) -> float:
    at = slot.get("at")
    return min(gap, max(0.0, at + gap - time.time())) if isinstance(at, (int, float)) else 0.0


class _locked_slot:
    """The slot file, open and flocked. Group-writable: each fleet user rewrites what another created."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / SLOT_FILE

    def __enter__(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o664)
        with contextlib.suppress(OSError):  # only the file's owner may, and it is already 664 by then
            os.fchmod(self.fd, 0o664)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self.fd

    def __exit__(self, *_exc) -> None:
        os.close(self.fd)  # also drops the flock


def _slot_wait(directory: Path, gap: float) -> float:
    try:
        slot = json.loads((directory / SLOT_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return 0.0
    return _remaining(slot if isinstance(slot, dict) else {}, gap)


def _take_slot(directory: Path, gap: float, worker: str) -> float:
    with _locked_slot(directory) as fd:
        wait = _remaining(_read_slot(fd), gap)
        if wait:
            return wait
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps({"at": time.time(), "worker": worker}).encode())
    return 0.0

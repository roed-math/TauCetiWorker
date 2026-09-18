"""tauceti_worker.local_claims — the host-local half of a claim: a per-key `flock` taken BEFORE any
GitHub lease, so workers sharing a host settle who takes a target or a PR without a network call.

Every GitHub claim operation is a push (see scripts/claim.sh), and a fleet of N workers on one host
used to discover a clash only after each of them had paid an `ls-remote` + lease fetch + push to find
the ref already taken. The lock here is the authority on the SAME host: a sibling that holds it is
known to be working on the key, so the round moves on without touching GitHub. The GitHub lease is
still taken when the local lock is ours, because it is the only thing peers on OTHER hosts can see.

The lock is `fcntl.flock`, not the JSON: the kernel drops it when the holder's fd closes (a crashed
round releases on exit, a killed one too), so there is no TTL to renew and nothing to garbage-collect.
The JSON beside it is a courtesy for the log line ("held by a sibling on this host (worker-2)"), never
consulted to decide anything. Lock files are never unlinked: unlinking would let a third process create
a fresh inode under the same path while a second still holds the old one, and two holders would result.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from .quota import _host_home


def local_claims_dir() -> Path:
    """Where the per-key lock files live. The LOGIN home (not a per-worker $HOME), because every worker
    on the host must agree on the directory for the lock to mean anything; $TAUCETI_LOCAL_CLAIMS_DIR
    overrides it (tests point it at a scratch directory)."""
    override = os.environ.get("TAUCETI_LOCAL_CLAIMS_DIR")
    if override:
        return Path(override)
    return _host_home() / ".cache" / "tauceti-claims" / "local"


def lock_path(key: str) -> Path:
    """One file per key, named by a digest so a key like `author/Area/slug` needs no path escaping."""
    return local_claims_dir() / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.lock"


class LocalLease:
    """A held local lock. Keep the object alive (it owns the fd) for as long as the claim is meant to
    hold — the round registers `release` on its cleanup, alongside the GitHub lease."""

    def __init__(self, key: str, owner: str, path: Path, fd: int):
        self.key = key
        self.owner = owner
        self.path = path
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, key: str, owner: str) -> LocalLease | None:
        """Take the lock for `key`, or return None when another process on this host holds it. Never
        blocks. Two acquires from one process on the same key also conflict (flock is per open file
        description), which is the right answer: a round holds one claim at a time and drops it before
        trying the next candidate."""
        path = lock_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        os.set_inheritable(fd, False)  # a child (the agent, the heartbeat) must not keep it alive
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        # Ours. Record who, for the sibling's log line; a torn or failed write changes nothing above.
        try:
            os.ftruncate(fd, 0)
            os.write(
                fd,
                json.dumps({"owner": owner, "key": key, "acquired_at": int(time.time()), "pid": os.getpid()}).encode()
                + b"\n",
            )
        except OSError:
            pass
        return cls(key, owner, path, fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Drop the lock. Idempotent."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def holder(key: str) -> str:
    """The owner recorded in the lock file for `key`, or "" — informational only (a stale file from a
    dead holder is not a holder; only the flock says who holds it)."""
    try:
        return str(json.loads(lock_path(key).read_text() or "{}").get("owner") or "")
    except (OSError, ValueError):
        return ""

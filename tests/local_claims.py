#!/usr/bin/env python3
"""The host-local claim lock (tauceti_worker.local_claims).

Workers sharing a host settle a key on an `flock` before any GitHub call. This pins the lock's
semantics without a network: a second holder is refused (across processes AND across fds in one
process), release frees the key, a holder that dies without releasing frees it too (the kernel drops
the flock with the fd — no TTL), and the JSON beside the lock names the owner for the log line.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="local-claims-"))
os.environ["TAUCETI_LOCAL_CLAIMS_DIR"] = str(TMP)

from tauceti_worker import local_claims as lc  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


KEY = "author/Alpha/a-one"

# 1) The directory override is honoured and the lock file is named by a digest of the key.
check("lock dir follows TAUCETI_LOCAL_CLAIMS_DIR", lc.lock_path(KEY).parent == TMP)
check("lock file name is a key digest", lc.lock_path(KEY).name.endswith(".lock") and "/" not in lc.lock_path(KEY).name)

# 2) Acquire, record, refuse a second holder, release, re-acquire.
lease = lc.LocalLease.acquire(KEY, "worker-1")
check("first acquire succeeds", lease is not None and lease.held)
rec = json.loads(lc.lock_path(KEY).read_text())
check(
    "owner/key/pid recorded", rec.get("owner") == "worker-1" and rec.get("key") == KEY and rec.get("pid") == os.getpid()
)
check("holder() reads the owner", lc.holder(KEY) == "worker-1")
check("second acquire in the same process is refused", lc.LocalLease.acquire(KEY, "worker-2") is None)
check("a different key is independent", (other := lc.LocalLease.acquire("branch/7", "worker-1")) is not None)
other.release()
lease.release()
check("release drops the lock", not lease.held)
lease.release()  # idempotent
again = lc.LocalLease.acquire(KEY, "worker-2")
check("released key can be taken by another owner", again is not None and lc.holder(KEY) == "worker-2")
again.release()

# 3) Cross-process: a child holds the key; we are refused; the child dies WITHOUT releasing; we succeed.
child_src = f"""
import os, sys
sys.path.insert(0, {str(REPO)!r})
os.environ["TAUCETI_LOCAL_CLAIMS_DIR"] = {str(TMP)!r}
from tauceti_worker import local_claims as lc
lease = lc.LocalLease.acquire({KEY!r}, "sibling")
print("held" if lease else "refused", flush=True)
sys.stdin.readline()  # block until killed; never releases
"""
child = subprocess.Popen(
    [sys.executable, "-c", child_src], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=dict(os.environ)
)
first = child.stdout.readline().strip()
check("child process took the key", first == "held")
check("parent is refused while the child holds it", lc.LocalLease.acquire(KEY, "worker-1") is None)
check("holder() names the sibling", lc.holder(KEY) == "sibling")
child.send_signal(signal.SIGKILL)  # a crash, not a release
child.wait()
mine = lc.LocalLease.acquire(KEY, "worker-1")
check("a crashed holder's lock is gone", mine is not None)
check("the stale JSON was overwritten by the new holder", lc.holder(KEY) == "worker-1")
mine.release()

# 4) The fd is not inherited: a child spawned while we hold the key must not keep it alive after we release.
lease = lc.LocalLease.acquire(KEY, "worker-1")
probe = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"], stdin=subprocess.PIPE)
lease.release()
check("release while a child is alive frees the key (fd not inherited)", lc.LocalLease.acquire(KEY, "x") is not None)
probe.stdin.close()
probe.wait()

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""Claims are taken local-first: the host-local lock decides same-host contention before any GitHub
call, and the agent's own re-claim of a key the round holds is answered by claim.sh without git.

Drives Claims.begin_target_work / begin_branch_work with a stubbed claim.sh (recording every call) and
a real local lock in a scratch directory:
  - a sibling on this host holds the key -> rc 1 / False, and claim.sh is never run
  - the key is locally free -> exactly one `acquire`
  - rc 0 exports TAUCETI_CLAIM_KEY and TAUCETI_CLAIM_HELD; rc 2 keeps the local lock and exports
    TAUCETI_CLAIM_HELD alone; rc 1 drops the local lock and exports nothing
  - release gives the local lock back
Then the shell side: with TAUCETI_CLAIM_HELD=<key>, `claim.sh acquire <key>` exits 0 without running
git at all (a fake `git` first on PATH exits 99 and leaves a marker), while any other key, and `renew`
of the held key, still reach git.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="claim-local-first-"))
os.environ["TAUCETI_LOCAL_CLAIMS_DIR"] = str(TMP / "locks")
for var in ("TAUCETI_CLAIM_KEY", "TAUCETI_CLAIM_REPO", "TAUCETI_CLAIM_HELD", "CLAIM_REPO"):
    os.environ.pop(var, None)

import tauceti_worker as tc  # noqa: E402
from tauceti_worker import local_claims as lc  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


# ---- stubs: claim.sh verdicts are scripted; the namespace and heartbeat are inert -------------------
ST = {"calls": [], "script": [], "log": [], "cleanups": [], "heartbeat": []}


def fake_claim_sh(args, claim_repo):
    ST["calls"].append((tuple(args), claim_repo))
    return ST["script"].pop(0) if ST["script"] else 0


tc.round.run_claim_sh = fake_claim_sh
tc.round.claims_repo = lambda: "alice/tauceti-claims"
tc.round.Claims.start_heartbeat = lambda self, key, repo: ST["heartbeat"].append((key, repo))
tc.round.log = lambda msg: ST["log"].append(msg)
cfg = types.SimpleNamespace(wid="worker-A")
ctx = types.SimpleNamespace(add_cleanup=lambda fn: ST["cleanups"].append(fn))


def reset(script):
    ST["calls"].clear()
    ST["log"].clear()
    ST["heartbeat"].clear()
    ST["script"][:] = script
    return tc.round.Claims(cfg, ctx)


def locally_free(key) -> bool:
    probe = lc.LocalLease.acquire(key, "probe")
    if probe is None:
        return False
    probe.release()
    return True


def release(claims):
    """Claims.release runs claim.sh through subprocess.run (not the run_claim_sh seam); stub that for
    the call so no real claim.sh — and no network — is reached, recording the release it would make."""
    real_run = subprocess.run
    subprocess.run = lambda *a, **k: (
        ST["calls"].append(("release-run", a[0][1:])) or types.SimpleNamespace(returncode=0)
    )
    try:
        claims.release()
    finally:
        subprocess.run = real_run


# ---- 1. a sibling on this host holds the target: no network, rc 1 ---------------------------------
sibling = lc.LocalLease.acquire("author/Alpha/a-one", "worker-B")
claims = reset([])
rc = claims.begin_target_work("Alpha", "a-one")
check("target held by a sibling -> rc 1", rc == 1)
check("target held by a sibling -> claim.sh never run", ST["calls"] == [])
check(
    "target held by a sibling -> logged as such, naming the holder",
    any("held by a sibling on this host (worker-B)" in m for m in ST["log"]),
)
check("target held by a sibling -> nothing exported", "TAUCETI_CLAIM_HELD" not in os.environ)
# the branch flavour behaves the same
sibling_branch = lc.LocalLease.acquire("branch/143", "worker-B")
check("branch held by a sibling -> False", claims.begin_branch_work(143, "abc", "feature", "alice", "TauCeti") is False)
check("branch held by a sibling -> claim.sh never run", ST["calls"] == [])
sibling.release()
sibling_branch.release()

# ---- 2. locally free, GitHub says ours (rc 0) -----------------------------------------------------
claims = reset([0])
rc = claims.begin_target_work("Alpha", "a-one")
check("free + rc 0 -> rc 0", rc == 0)
check(
    "free + rc 0 -> exactly one acquire, against the namespace",
    ST["calls"] == [(("acquire", "author/Alpha/a-one", str(tc.CLAIM_TTL_S)), "alice/tauceti-claims")],
)
check("free + rc 0 -> TAUCETI_CLAIM_KEY exported", os.environ.get("TAUCETI_CLAIM_KEY") == "author/Alpha/a-one")
check("free + rc 0 -> TAUCETI_CLAIM_HELD exported", os.environ.get("TAUCETI_CLAIM_HELD") == "author/Alpha/a-one")
check("free + rc 0 -> heartbeat started", ST["heartbeat"] == [("author/Alpha/a-one", "alice/tauceti-claims")])
check("free + rc 0 -> local lock kept", claims.local is not None and claims.local.held)
check("free + rc 0 -> a sibling is now refused locally", not locally_free("author/Alpha/a-one"))
check("free + rc 0 -> release registered on cleanup", claims.release in ST["cleanups"])
release(claims)
check("release -> GitHub lease released", ("release-run", ["release", "author/Alpha/a-one"]) in ST["calls"])
check("release -> local lock freed", locally_free("author/Alpha/a-one"))
check(
    "release -> env cleared",
    "TAUCETI_CLAIM_KEY" not in os.environ and "TAUCETI_CLAIM_HELD" not in os.environ,
)

# ---- 3. locally free, GitHub cannot register (rc 2): local lock kept, HELD exported, no KEY --------
claims = reset([2])
rc = claims.begin_target_work("Alpha", "a-two")
check("free + rc 2 -> rc 2", rc == 2)
check("free + rc 2 -> one acquire", len(ST["calls"]) == 1)
check("free + rc 2 -> no GitHub lease, no heartbeat", claims.held is None and ST["heartbeat"] == [])
check("free + rc 2 -> local lock kept", claims.local is not None and not locally_free("author/Alpha/a-two"))
check("free + rc 2 -> TAUCETI_CLAIM_HELD exported", os.environ.get("TAUCETI_CLAIM_HELD") == "author/Alpha/a-two")
check("free + rc 2 -> TAUCETI_CLAIM_KEY absent (push arbiter has no lease)", "TAUCETI_CLAIM_KEY" not in os.environ)
check("free + rc 2 -> the CLAIM_REPO hint is logged", any("set CLAIM_REPO=" in m for m in ST["log"]))
release(claims)
check("rc 2 release -> local lock freed", locally_free("author/Alpha/a-two"))
check("rc 2 release -> HELD cleared", "TAUCETI_CLAIM_HELD" not in os.environ)

# ---- 4. locally free, another host holds it (rc 1): local lock dropped, nothing exported ------------
claims = reset([1])
rc = claims.begin_target_work("Alpha", "a-three")
check("free + rc 1 -> rc 1", rc == 1)
check("free + rc 1 -> local lock dropped", claims.local is None and locally_free("author/Alpha/a-three"))
check(
    "free + rc 1 -> nothing exported", "TAUCETI_CLAIM_HELD" not in os.environ and "TAUCETI_CLAIM_KEY" not in os.environ
)
# ...and the next candidate starts clean: one Claims holds one local lock at a time.
ST["script"][:] = [0]
claims.begin_target_work("Alpha", "a-four")
check("next candidate -> its own lock, previous key free", claims.local.key == "author/Alpha/a-four")
release(claims)

# ---- 5. the branch flavour: rc 0 sets the push-arbiter env AND the held key ------------------------
claims = reset([0])
check("branch free + rc 0 -> True", claims.begin_branch_work(7, "abc", "feature", "bob", "TauCeti") is True)
check("branch -> one acquire", ST["calls"] == [(("acquire", "branch/7", str(tc.CLAIM_TTL_S)), "alice/tauceti-claims")])
check("branch -> push arbiter env", os.environ.get("TAUCETI_PUSH_REMOTE") == "https://github.com/bob/TauCeti")
check("branch -> HELD + KEY", os.environ.get("TAUCETI_CLAIM_HELD") == "branch/7" == os.environ.get("TAUCETI_CLAIM_KEY"))
release(claims)
check(
    "branch release -> one GitHub release, local lock freed",
    ST["calls"][-1] == ("release-run", ["release", "branch/7"]) and locally_free("branch/7"),
)

# ---- 6. claim.sh: the held key short-circuits before any git; other keys and renew still reach git --
bindir = TMP / "bin"
bindir.mkdir()
marker = TMP / "git-ran"
fake_git = bindir / "git"
fake_git.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\nexit 99\n')
fake_git.chmod(0o755)
env = {
    **os.environ,
    "PATH": f"{bindir}:{os.environ['PATH']}",
    "CLAIM_GITDIR": str(TMP / "scratch.git"),
    "CLAIM_REPO": "alice/tauceti-claims",
    "TAUCETI_CLAIM_HELD": "author/Alpha/x",
}
claim_sh = REPO / "scripts" / "claim.sh"


def run(*args):
    marker.unlink(missing_ok=True)
    p = subprocess.run([str(claim_sh), *args], env=env, capture_output=True, text=True)
    return p.returncode, marker.exists(), p.stderr


rc, ran_git, err = run("acquire", "author/Alpha/x")
check("claim.sh acquire <held key> -> exit 0", rc == 0)
check("claim.sh acquire <held key> -> git never ran", not ran_git)
check("claim.sh acquire <held key> -> says why", "already held" in err)
rc, ran_git, _ = run("acquire", "author/Alpha/y")
check("claim.sh acquire <other key> -> reaches git", ran_git and rc != 0)
rc, ran_git, _ = run("renew", "author/Alpha/x")
check("claim.sh renew <held key> -> unchanged, reaches git", ran_git and rc != 0)
env.pop("TAUCETI_CLAIM_HELD")
rc, ran_git, _ = run("acquire", "author/Alpha/x")
check("claim.sh acquire without TAUCETI_CLAIM_HELD -> reaches git", ran_git and rc != 0)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

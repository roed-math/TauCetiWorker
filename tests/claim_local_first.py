#!/usr/bin/env python3
"""Claims are taken local-first: the host-local lock decides same-host contention before any GitHub
call, and the agent's own re-claim of a key the round holds is answered by claim.sh without git.

Drives Claims.begin_target_work / begin_branch_work / begin_global_work with a stubbed claim.sh
(recording every call) and a real local lock in a scratch directory:
  - a sibling on this host holds the key -> rc 1 / False, and claim.sh is never run
  - the key is locally free -> exactly one `acquire`
  - rc 0 exports TAUCETI_CLAIM_KEY and TAUCETI_CLAIM_HELD; rc 2 keeps the local lock and exports
    TAUCETI_CLAIM_HELD alone; rc 1 drops the local lock and exports nothing
  - release gives the local lock back
Then the shell side: with TAUCETI_CLAIM_HELD=<key>, `claim.sh acquire <key>` exits 0 without running
git at all (a fake `git` first on PATH exits 99 and leaves a marker), while any other key, and `renew`
of the held key, still reach git. And the namespace gate: with CLAIM_REPO unset, or naming canonical
in any spelling, every network subcommand exits 2 before git runs; a held key still short-circuits to
0 ahead of that gate; any other repository proceeds to git.

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

# ---- 5b. the global flavour (the `progress` key): same layers, no push-arbiter env --------------------
for var in ("TAUCETI_PUSH_REF", "TAUCETI_PUSH_EXPECT", "TAUCETI_PUSH_REMOTE"):
    os.environ.pop(var, None)  # section 5's branch claim set these; release() leaves them (by design)
sibling = lc.LocalLease.acquire("progress", "worker-B")
claims = reset([])
check("global held by a sibling -> rc 1", claims.begin_global_work("progress") == 1)
check("global held by a sibling -> claim.sh never run", ST["calls"] == [])
check("global held by a sibling -> nothing exported", "TAUCETI_CLAIM_HELD" not in os.environ)
sibling.release()
claims = reset([0])
check("global free + rc 0 -> rc 0", claims.begin_global_work("progress") == 0)
check("global -> one acquire", ST["calls"] == [(("acquire", "progress", str(tc.CLAIM_TTL_S)), "alice/tauceti-claims")])
check("global -> heartbeat started", ST["heartbeat"] == [("progress", "alice/tauceti-claims")])
check("global -> HELD + KEY", os.environ.get("TAUCETI_CLAIM_HELD") == "progress" == os.environ.get("TAUCETI_CLAIM_KEY"))
check("global -> no push-arbiter env", "TAUCETI_PUSH_REF" not in os.environ and "TAUCETI_PUSH_REMOTE" not in os.environ)
check("global -> a sibling is now refused locally", not locally_free("progress"))
release(claims)
check(
    "global release -> GitHub lease released, local lock freed",
    ST["calls"][-1] == ("release-run", ["release", "progress"]) and locally_free("progress"),
)
claims = reset([2])
check(
    "global free + rc 2 -> rc 2, local lock kept, HELD exported",
    claims.begin_global_work("progress") == 2
    and not locally_free("progress")
    and os.environ.get("TAUCETI_CLAIM_HELD") == "progress",
)
release(claims)
check(
    "global rc 2 release -> no GitHub release (nothing was registered), lock freed",
    not any(c[0] == "release-run" for c in ST["calls"]) and locally_free("progress"),
)

# ---- 5c. do_progress goes through Claims: a sibling's local lock means no claim.sh, no report ---------
from tauceti_worker import work_units as wu  # noqa: E402

sibling = lc.LocalLease.acquire("progress", "worker-B")
claims = reset([])
ran_inner = []
saved_inner, saved_log = wu._do_progress_inner, wu.log
wu._do_progress_inner = lambda w, opts: ran_inner.append(1) or 0
wu.log = lambda msg: ST["log"].append(msg)
try:
    verdict = wu.do_progress(types.SimpleNamespace(claims=claims), None, None, None, False)
finally:
    wu._do_progress_inner, wu.log = saved_inner, saved_log
check("do_progress with a sibling's lock -> None (skipped)", verdict is None)
check("do_progress with a sibling's lock -> claim.sh never run", ST["calls"] == [])
check("do_progress with a sibling's lock -> the report is not written", ran_inner == [])
check(
    "do_progress with a sibling's lock -> says so",
    any("another worker holds the progress claim" in m for m in ST["log"]),
)
sibling.release()
# ...and when it is ours, the report runs and the claim is given back afterwards.
claims = reset([0])
wu._do_progress_inner = lambda w, opts: ran_inner.append(1) or 0
real_run = subprocess.run
subprocess.run = lambda *a, **k: ST["calls"].append(("release-run", a[0][1:])) or types.SimpleNamespace(returncode=0)
try:
    verdict = wu.do_progress(types.SimpleNamespace(claims=claims), None, None, None, False)
finally:
    wu._do_progress_inner = saved_inner
    subprocess.run = real_run
check("do_progress when free -> the report runs, rc 0", verdict == 0 and ran_inner == [1])
check(
    "do_progress when free -> one acquire then one release",
    [c[0] if c[0] == "release-run" else c[0][0] for c in ST["calls"]] == ["acquire", "release-run"],
)
check("do_progress when free -> local lock freed afterwards", locally_free("progress"))

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

# ---- 7. claim.sh: no default namespace, and canonical is refused before any git runs ----------------
env.pop("CLAIM_REPO")
for sub in ("acquire", "renew", "release", "holds", "read", "list", "gc"):
    rc, ran_git, err = run(sub, "author/Alpha/x")
    check(f"claim.sh {sub} with CLAIM_REPO unset -> exit 2", rc == 2)
    check(f"claim.sh {sub} with CLAIM_REPO unset -> git never ran", not ran_git)
    check(f"claim.sh {sub} with CLAIM_REPO unset -> names the variable", "CLAIM_REPO" in err)
env["CLAIM_REPO"] = ""
rc, ran_git, err = run("acquire", "author/Alpha/x")
check("claim.sh acquire with CLAIM_REPO empty -> exit 2, no git", rc == 2 and not ran_git and "CLAIM_REPO" in err)
for spelling in (
    "TauCetiProject/TauCeti",
    "taucetiproject/tauceti",
    "TAUCETIPROJECT/TAUCETI.git",
    "https://github.com/TauCetiProject/TauCeti",
    "https://github.com/TauCetiProject/TauCeti.git",
    "https://github.com/tauCetiProject/TauCeti/",
    "git@github.com:TauCetiProject/TauCeti.git",
):
    env["CLAIM_REPO"] = spelling
    rc, ran_git, err = run("acquire", "author/Alpha/x")
    check(f"claim.sh acquire with CLAIM_REPO={spelling} -> exit 2", rc == 2)
    check(f"claim.sh acquire with CLAIM_REPO={spelling} -> git never ran", not ran_git)
    check(
        f"claim.sh acquire with CLAIM_REPO={spelling} -> says canonical, names the variable",
        "canonical" in err and "CLAIM_REPO" in err,
    )
env["CLAIM_REPO"] = "TauCetiProject/TauCeti"
rc, ran_git, _ = run("release", "author/Alpha/x")
check("claim.sh release against canonical -> exit 2, no git", rc == 2 and not ran_git)
# The held-key short-circuit stays AHEAD of the gate: the round already holds it, so no verdict on
# the namespace is needed to answer the agent.
env["TAUCETI_CLAIM_HELD"] = "author/Alpha/x"
rc, ran_git, _ = run("acquire", "author/Alpha/x")
check("claim.sh acquire <held key> against canonical -> still exit 0, no git", rc == 0 and not ran_git)
env.pop("TAUCETI_CLAIM_HELD")
# Any other repository — the fork, the shared namespace, a full URL of either — goes on to git. (The
# fake git fails every push, so the verdict is claim.sh's own 2 for an unexpected push error; the
# marker, and the absence of the gate's message, are what show the gate let it through.)
for ok_repo in ("x/y", "TauCetiProject/tauceti-claims", "https://github.com/x/TauCeti.git"):
    env["CLAIM_REPO"] = ok_repo
    rc, ran_git, err = run("acquire", "author/Alpha/x")
    check(
        f"claim.sh acquire with CLAIM_REPO={ok_repo} -> proceeds to git",
        ran_git and rc != 0 and "CLAIM_REPO" not in err,
    )

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""T03 — a claim that targets canonical or an unapproved ref is rejected locally, with zero remote
write attempts (brief §8.3 T03).

Three layers are checked, each with a fake git that records every invocation:
  * claim.sh itself: CLAIM_REPO=TauCetiProject/TauCeti → exit 2 before git; a key that would name a
    ref outside refs/tauceti-claims/ (`../heads/main`, `/x`, `refs/heads/main`) → exit 2 before git.
  * the worker's seam: run_claim_sh(["acquire", …], "TauCetiProject/TauCeti") → 2, git never run.
  * the gate: admit git_push acquire TauCetiProject/TauCeti → refused `target`, in-process and via
    the CLI; the same for a `push` to canonical without the round having been given it; while the
    fork, the claims namespace and the round's TAUCETI_PUSH_REMOTE are admitted.

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import SCRIPTS, check, fake_calls, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env  # noqa: E402

scrub_env()
TMP = mktemp("gate-t03-")
gate_env(TMP)
env = fake_env(TMP, {"gh": [], "git": [], "bare_repos": {}}, CLAIM_GITDIR=str(TMP / "scratch.git"))
os.environ.update(
    {
        k: env[k]
        for k in (
            "PATH",
            "TAUCETI_FAKE_SCENARIO",
            "TAUCETI_FAKE_LOG",
            "TAUCETI_REAL_GIT",
            "TAUCETI_REAL_GH",
            "TAUCETI_PYTHON",
            "TAUCETI_GATE_CLI",
        )
    }
)

from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import round as round_mod  # noqa: E402

claim_sh = str(SCRIPTS / "claim.sh")


def run(*args, **extra):
    return subprocess.run([claim_sh, *args], env={**env, **extra}, capture_output=True, text=True)


# ---- 1. claim.sh: canonical, in every spelling, and a ref outside the namespace --------------------------
for spelling in (
    "TauCetiProject/TauCeti",
    "https://github.com/TauCetiProject/TauCeti.git",
    "git@github.com:taucetiproject/tauceti",
):
    p = run("acquire", "author/Alpha/x", CLAIM_REPO=spelling)
    check(
        f"claim.sh acquire with CLAIM_REPO={spelling} -> exit 2, canonical named",
        p.returncode == 2 and "canonical" in p.stderr,
    )
check("…and the fake git was never invoked", fake_calls(TMP, "git") == [])
for key in ("../heads/main", "/x", "refs/heads/main", "a/../../b", "x/", "bad key"):
    p = run("acquire", key, CLAIM_REPO="alice/tauceti-claims")
    check(f"claim.sh acquire {key!r} -> exit 2 (not a claim key)", p.returncode == 2 and "not a claim key" in p.stderr)
    p = run("release", key, CLAIM_REPO="alice/tauceti-claims")
    check(f"claim.sh release {key!r} -> exit 2", p.returncode == 2)
check("…and the fake git was never invoked for any of them", fake_calls(TMP, "git") == [])
check("no gate event was needed for a local refusal (nothing to account)", not (TMP / "gate" / "events.log").exists())

# ---- 2. the worker's seam ----------------------------------------------------------------------------------
rc = round_mod.run_claim_sh(["acquire", "branch/7", "3600"], "TauCetiProject/TauCeti")
check("run_claim_sh against canonical -> 2", rc == 2)
check("…git never ran", fake_calls(TMP, "git") == [])
ev = [
    e
    for e in (
        []
        if not (TMP / "gate" / "events.log").exists()
        else [__import__("json").loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
    )
]
check(
    "the gate refused it as a bad target before claim.sh could",
    any(e.get("decision") == "refuse" and e.get("reason") == G.R_TARGET for e in ev),
)

# ---- 3. the gate's push allowlist ----------------------------------------------------------------------------
g = G.Gate.from_env()


def refused(op, target, kind=G.GIT_PUSH):
    try:
        a = g.admit(op, target, kind, wait=False)
        g.record(a, G.Outcome(ok=True))
        return None
    except G.GateRefused as e:
        return e.reason


check(
    "admit git_push acquire TauCetiProject/TauCeti -> refused target",
    refused("acquire", "TauCetiProject/TauCeti") == G.R_TARGET,
)
check(
    "admit git_push push TauCetiProject/TauCeti (round given no remote) -> refused target",
    refused("push", "https://github.com/TauCetiProject/TauCeti") == G.R_TARGET,
)
check(
    "admit git_push push to an arbitrary repo -> refused target",
    refused("push", "https://github.com/someone/else") == G.R_TARGET,
)
check(
    "admit git_push acquire TauCetiProject/tauceti-claims -> admitted",
    refused("acquire", "TauCetiProject/tauceti-claims") is None,
)
os.environ["TAUCETI_FORK"] = "alice/TauCeti"
check("admit git_push push to the fork -> admitted", refused("push", "https://github.com/alice/TauCeti") is None)
os.environ["CLAIM_REPO"] = "alice/tauceti-claims"
check("admit git_push renew to $CLAIM_REPO -> admitted", refused("renew", "alice/tauceti-claims") is None)
os.environ["CLAIM_REPO"] = "TauCetiProject/TauCeti"
check("$CLAIM_REPO naming canonical does not allowlist it", refused("acquire", "TauCetiProject/TauCeti") == G.R_TARGET)
os.environ.pop("CLAIM_REPO")
os.environ["TAUCETI_PUSH_REMOTE"] = "https://github.com/bob/TauCeti"
check(
    "admit git_push push to the round's head repo (TAUCETI_PUSH_REMOTE) -> admitted",
    refused("push", "bob/TauCeti") is None,
)
check("…but a claim to that head repo is still refused", refused("acquire", "bob/TauCeti") == G.R_TARGET)
os.environ["TAUCETI_PUSH_REMOTE"] = "https://github.com/TauCetiProject/TauCeti"
check(
    "a round explicitly given canonical as its head repo may push a branch there (a canonical-headed PR)",
    refused("push", "TauCetiProject/TauCeti") is None,
)
check("…but never a claim ref", refused("acquire", "TauCetiProject/TauCeti") == G.R_TARGET)
os.environ.pop("TAUCETI_PUSH_REMOTE")
p = gate_cli(["admit", "git_push", "acquire", "TauCetiProject/TauCeti", "--no-wait"], {**env, **os.environ})
check(
    "CLI: admit git_push acquire TauCetiProject/TauCeti -> 75 (target)", p.returncode == 75 and "(target)" in p.stderr
)
check("still no git anywhere", fake_calls(TMP, "git") == [])

# ---- 4. the git shim refuses a push the round was not given, and a non-https remote ----------------------------
p = subprocess.run(
    [str(SCRIPTS / "shim" / "git"), "push", "origin", "HEAD:refs/tauceti-claims/x"],
    env=env,
    capture_output=True,
    text=True,
)
check("git shim: push with no TAUCETI_PUSH_REF -> 75", p.returncode == 75 and "git-safe-push" in p.stderr)
p = subprocess.run(
    [str(SCRIPTS / "shim" / "git"), "push", "origin", "HEAD:main"],
    env={**env, "TAUCETI_PUSH_REF": "feat/x"},
    capture_output=True,
    text=True,
)
check("git shim: push to a branch other than the round's -> 75", p.returncode == 75 and "feat/x" in p.stderr)
check("…and neither reached git", fake_calls(TMP, "git") == [])

finish()

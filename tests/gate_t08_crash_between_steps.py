#!/usr/bin/env python3
"""T08 — a crash after the push landed, before the PR was created (brief §8.3 T08; design §6).

An author publication is created through the real CLI; the real git-safe-push pushes a real commit
into a local bare repository standing in for the fork (the fake git rewrites the URL), and the
`push` step is `done` with the pushed tip. The process then "dies" (nothing else runs). The next
round (`reconcile --all` from a fresh process, which is what run_round does at its start) finds
nothing uncertain and marks the publication `interrupted`; a resumed gh-safe-pr-create under the
same id creates the PR — and a second git-safe-push under the same id is refused: the fake git log
holds exactly ONE push.

Separately, a push the process died IN (the step left `sent` by a sender that is gone): reconcile
marks it uncertain, reads the branch tip with ONE ls-remote, finds it equal to the recorded local
tip, and marks it done — again without pushing; when the tip is absent it stays uncertain and parked.

Proven by mock (a real git against a local bare repository). Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import (  # noqa: E402
    SCRIPTS,
    bare_repo,
    check,
    fake_calls,
    fake_env,
    finish,
    gate_cli,
    gate_env,
    mktemp,
    scrub_env,
)

scrub_env()
TMP = mktemp("gate-t08-")
os.environ["TAUCETI_WORKER_ID"] = "w8"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
os.environ["TAUCETI_FORK"] = "fake-login/TauCeti"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
FORK = bare_repo(TMP, "fake-login/TauCeti")
TARGET_MARKER = '<!--tauceti-target:v1 {"focus":"Alpha","id":"alpha-y"}-->'
BRANCH = "roadmap/alpha-y-w8"

# A work tree with one commit to push.
WORK = TMP / "work"
subprocess.run(["/usr/bin/git", "init", "-q", str(WORK)], check=True)
GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
(WORK / "f.lean").write_text("theorem t : True := trivial\n")
subprocess.run(["/usr/bin/git", "-C", str(WORK), "add", "."], check=True)
subprocess.run(
    ["/usr/bin/git", "-C", str(WORK), "commit", "-q", "-m", "feat: y"], check=True, env={**os.environ, **GIT}
)
TIP = subprocess.run(
    ["/usr/bin/git", "-C", str(WORK), "rev-parse", "HEAD"], capture_output=True, text=True
).stdout.strip()

SCENARIO = {
    "gh": [{"match": ["pr", "create"], "rc": 0, "stdout": "https://github.com/TauCetiProject/TauCeti/pull/601\n"}],
    "git": [],
    "bare_repos": {"fake-login/TauCeti": str(FORK)},
}
env = fake_env(TMP, SCENARIO)
PUSH_ENV = {
    "TAUCETI_PUSH_REF": BRANCH,
    "TAUCETI_PUSH_REMOTE": "https://github.com/fake-login/TauCeti",
    "TAUCETI_PUSH_EXPECT": "",
}


def publication() -> str:
    p = gate_cli(
        [
            "publication",
            "create",
            "--kind",
            "author",
            "--branch",
            "",
            "--head-sha",
            "",
            "--remote",
            PUSH_ENV["TAUCETI_PUSH_REMOTE"],
        ],
        env,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def checkp(name: str, cond, p) -> None:
    """check(), printing the process's stderr when the condition fails."""
    if not check(name, cond):
        print("      stderr: " + (p.stderr or "").strip().replace("\n", "\n      "))


def record(pub_id: str) -> dict:
    return json.loads((TMP / "gate" / "publications" / f"{pub_id}.json").read_text())


def step(pub_id: str, name: str) -> dict:
    return next(s for s in record(pub_id)["steps"] if s["name"] == name)


def pushes():
    return [a for a in fake_calls(TMP, "git") if a[:1] == ["push"]]


def creates():
    return [a for a in fake_calls(TMP, "gh") if a[:2] == ["pr", "create"]]


def safe_push(pub_id: str):
    return subprocess.run(
        [str(SCRIPTS / "git-safe-push")],
        env={**env, **PUSH_ENV, "TAUCETI_PUBLICATION_ID": pub_id},
        cwd=str(WORK),
        capture_output=True,
        text=True,
    )


def remote_tip() -> str:
    p = subprocess.run(
        ["/usr/bin/git", "-C", str(FORK), "rev-parse", f"refs/heads/{BRANCH}"], capture_output=True, text=True
    )
    return p.stdout.strip() if p.returncode == 0 else ""


# ---- 1. push lands, then the crash -------------------------------------------------------------------------
pub = publication()
p = safe_push(pub)
checkp("git-safe-push (create-only) succeeds against the bare fork", p.returncode == 0, p)
check("…the branch is on the fork at the local tip", remote_tip() == TIP)
check(
    "…the push step is done with that tip",
    step(pub, "push")["state"] == "done" and step(pub, "push")["remote_id"] == TIP,
)
check("…and the publication learned its branch from the push", record(pub)["branch"] == BRANCH)
check("exactly one push so far", len(pushes()) == 1)
# (the crash: nothing else runs under this process)

# ---- 2. the next round reconciles, then resumes at pr_create only ---------------------------------------------
p = gate_cli(["reconcile", "--all"], env)
check(
    "`reconcile --all` finds nothing uncertain and parks the publication `interrupted`",
    record(pub)["parked"] == "interrupted",
)
check("…without pushing again", len(pushes()) == 1)
p = safe_push(pub)
check(
    "a repeated git-safe-push under the same id is refused (75)",
    p.returncode == 75 and "publication: refused" in p.stderr,
)
check("…and did not push", len(pushes()) == 1)
body = TMP / "body.md"
body.write_text(f"This PR adds y.\n\n{TARGET_MARKER}\n")
p = subprocess.run(
    [
        str(SCRIPTS / "gh-safe-pr-create"),
        "--repo",
        "TauCetiProject/TauCeti",
        "--head",
        f"fake-login:{BRANCH}",
        "--title",
        "feat: y",
        "--body-file",
        str(body),
    ],
    env={**env, "TAUCETI_PUBLICATION_ID": pub, "TAUCETI_REQUIRE_TARGET_MARKER": "1"},
    capture_output=True,
    text=True,
)
check("gh-safe-pr-create resumes the publication at pr_create", p.returncode == 0 and len(creates()) == 1)
check(
    "…pr_create and marker_check are done with the PR number",
    step(pub, "pr_create")["remote_id"] == "601" and step(pub, "marker_check")["state"] == "done",
)
check(
    "…the publication is complete and unparked",
    record(pub)["parked"] is None and all(s["state"] == "done" for s in record(pub)["steps"]),
)
check(
    "the fake git log shows exactly one push, the fake gh exactly one create",
    len(pushes()) == 1 and len(creates()) == 1,
)

# ---- 3. the process died IN the push (the step is `sent`, its sender gone) ---------------------------------------
BRANCH2 = "roadmap/alpha-z-w8"
pub2 = gate_cli(
    [
        "publication",
        "create",
        "--kind",
        "author",
        "--branch",
        BRANCH2,
        "--head-sha",
        "",
        "--remote",
        PUSH_ENV["TAUCETI_PUSH_REMOTE"],
    ],
    env,
).stdout.strip()
# `begin` records the CLI's PARENT as the sender; run it under a bash that exits (a compound command, so
# bash forks rather than exec-ing the CLI in place), so the sender is gone by the time reconcile looks.
p = subprocess.run(
    [
        "bash",
        "-c",
        f'"{SCRIPTS / "tauceti-gate"}" publication begin "$1" push --sha "$2"; rc=$?; exit $rc',
        "_",
        pub2,
        TIP,
    ],
    env=env,
    capture_output=True,
    text=True,
)
check("push marked sent by a sender that then died", p.returncode == 0 and step(pub2, "push")["state"] == "sent")
# the push itself did land (plain git straight into the bare repo, standing in for the half-finished push)
subprocess.run(["/usr/bin/git", "-C", str(WORK), "push", "-q", str(FORK), f"HEAD:refs/heads/{BRANCH2}"], check=True)
n_ls = len([a for a in fake_calls(TMP, "git") if a[:1] == ["ls-remote"]])
p = gate_cli(["reconcile", "--all"], env)
check(
    "reconcile: the sent step became uncertain and was resolved by ONE ls-remote",
    len([a for a in fake_calls(TMP, "git") if a[:1] == ["ls-remote"]]) == n_ls + 1,
)
check(
    "…push is done with the tip found on the remote",
    step(pub2, "push")["state"] == "done" and step(pub2, "push")["remote_id"] == TIP,
)
check("…no push was replayed", len(pushes()) == 1)
p = safe_push(pub2)
check("…and git-safe-push under that id is refused", p.returncode == 75 and len(pushes()) == 1)

# ---- 4. the same, but the push never landed: uncertain, parked, not resent ---------------------------------------
BRANCH3 = "roadmap/alpha-w-w8"
pub3 = gate_cli(
    [
        "publication",
        "create",
        "--kind",
        "author",
        "--branch",
        BRANCH3,
        "--head-sha",
        "",
        "--remote",
        PUSH_ENV["TAUCETI_PUSH_REMOTE"],
    ],
    env,
).stdout.strip()
subprocess.run(
    [
        "bash",
        "-c",
        f'"{SCRIPTS / "tauceti-gate"}" publication begin "$1" push --sha "$2"; rc=$?; exit $rc',
        "_",
        pub3,
        TIP,
    ],
    env=env,
    capture_output=True,
    text=True,
)
p = gate_cli(["reconcile", "--all"], env)
check(
    "a sent push whose branch is absent stays uncertain and parks the publication",
    step(pub3, "push")["state"] == "uncertain" and record(pub3)["parked"] == "needs-reconciliation",
)
p = subprocess.run(
    [str(SCRIPTS / "git-safe-push")],
    env={**env, **PUSH_ENV, "TAUCETI_PUSH_REF": BRANCH3, "TAUCETI_PUBLICATION_ID": pub3},
    cwd=str(WORK),
    capture_output=True,
    text=True,
)
check("…and a push under it is refused rather than resent", p.returncode == 75 and len(pushes()) == 1)
p = gate_cli(["status"], env)
check("`status` shows it parked", pub3 in p.stdout)

finish()

#!/usr/bin/env python3
"""T09 — the PR head moves while a fix publication waits (brief §8.3 T09; design §6).

A fix publication is built on head A of PR #42. Before its `push` step, git-safe-push asks the
ledger, which reads the PR's live head with ONE admitted `gh pr view --json headRefOid`:
  * the fake answers B: the step is refused `stale-head`, NOTHING is pushed, no comment is posted
    (the shim's reply is refused the same way), the publication is parked `stale-head`, and the
    refusal is what the round reads as "yield";
  * a publication whose PR is still at its head pushes; after that push the `comment` step's
    revalidation expects the tip OUR push left (not the original head), so the shim's reply goes
    through and is `done` with the comment id the fake returns;
  * a head the fake cannot read (gh fails) refuses too — fail closed, no push.

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
    SHIM,
    bare_repo,
    check,
    events,
    fake_calls,
    fake_env,
    finish,
    gate_cli,
    gate_env,
    mktemp,
    scrub_env,
)

scrub_env()
TMP = mktemp("gate-t09-")
os.environ["TAUCETI_WORKER_ID"] = "w9"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
os.environ["TAUCETI_FORK"] = "fake-login/TauCeti"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
HEADREPO = bare_repo(TMP, "fake-login/TauCeti")
BRANCH = "fix/thing"

WORK = TMP / "work"
subprocess.run(["/usr/bin/git", "init", "-q", str(WORK)], check=True)
GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def commit(msg: str) -> str:
    (WORK / "f.lean").write_text(f"-- {msg}\n")
    subprocess.run(["/usr/bin/git", "-C", str(WORK), "add", "."], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(WORK), "commit", "-q", "-m", msg], check=True, env={**os.environ, **GIT})
    return subprocess.run(
        ["/usr/bin/git", "-C", str(WORK), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


A = commit("feat: first")
subprocess.run(["/usr/bin/git", "-C", str(WORK), "push", "-q", str(HEADREPO), f"HEAD:refs/heads/{BRANCH}"], check=True)
FIX = commit("fix: address review")


def scenario(live_head: str | None, *, view_rc: int = 0):
    return {
        "gh": [
            {
                "match": ["pr", "view", "42"],
                "rc": view_rc,
                "stdout": json.dumps({"headRefOid": live_head}) + "\n" if live_head is not None else "",
                "stderr": "" if view_rc == 0 else "gh: HTTP 502: Bad Gateway\n",
            },
            {"match": ["api", "-X", "POST"], "rc": 0, "stdout": json.dumps({"id": 9001, "body": "reply"}) + "\n"},
        ],
        "git": [],
        "bare_repos": {"fake-login/TauCeti": str(HEADREPO)},
    }


PUSH_ENV = {
    "TAUCETI_PUSH_REF": BRANCH,
    "TAUCETI_PUSH_REMOTE": "https://github.com/fake-login/TauCeti",
    "TAUCETI_PUSH_EXPECT": A,
}


def publication(env, head: str) -> str:
    p = gate_cli(
        [
            "publication",
            "create",
            "--kind",
            "fix",
            "--pr",
            "42",
            "--branch",
            BRANCH,
            "--head-sha",
            head,
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


def posts():
    return [a for a in fake_calls(TMP, "gh") if "-X" in a and "POST" in a]


def views():
    return [a for a in fake_calls(TMP, "gh") if a[:2] == ["pr", "view"]]


def safe_push(env, pub_id: str):
    return subprocess.run(
        [str(SCRIPTS / "git-safe-push")],
        env={**env, **PUSH_ENV, "TAUCETI_PUBLICATION_ID": pub_id},
        cwd=str(WORK),
        capture_output=True,
        text=True,
    )


def reply(env, pub_id: str, body: str = "The finding is wrong: see the synth-check."):
    return subprocess.run(
        [
            str(SHIM / "gh"),
            "api",
            "-X",
            "POST",
            "/repos/TauCetiProject/TauCeti/pulls/42/comments/7/replies",
            "-f",
            f"body={body}",
        ],
        env={**env, **PUSH_ENV, "TAUCETI_PUBLICATION_ID": pub_id},
        cwd=str(WORK),
        capture_output=True,
        text=True,
    )


def remote_tip() -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(HEADREPO), "rev-parse", f"refs/heads/{BRANCH}"], capture_output=True, text=True
    ).stdout.strip()


# ---- 1. the head moved: stale-head, no push, no comment ------------------------------------------------------
env = fake_env(TMP, scenario("b" * 40))
pub = publication(env, A)
p = safe_push(env, pub)
check("git-safe-push is refused (75) when the PR head is no longer the publication's", p.returncode == 75)
check("…with `stale-head` named", "stale-head" in p.stderr)
check("…after ONE pr view read", len(views()) == 1)
check(
    "…nothing was pushed: the fake git saw no push and the branch still holds A", pushes() == [] and remote_tip() == A
)
check(
    "…the publication is parked stale-head, its push still pending",
    record(pub)["parked"] == "stale-head" and step(pub, "push")["state"] == "pending",
)
p = reply(env, pub)
check("the shim's reply under that publication is refused too (75)", p.returncode == 75)
check("…and no POST reached gh", posts() == [])
check(
    "the ledger logged the stale head as an event",
    any(e.get("decision") == "publication" and "stale-head" in str(e.get("op")) for e in events(TMP)),
)

# ---- 2. the head is still ours: push, then the reply is checked against OUR new tip ---------------------------------
env = fake_env(TMP, scenario(A))
pub2 = publication(env, A)
p = safe_push(env, pub2)
checkp("git-safe-push proceeds when the live head equals the publication's", p.returncode == 0, p)
check("…the branch now holds the fix", remote_tip() == FIX and len(pushes()) == 1)
check(
    "…push is done with the new tip", step(pub2, "push")["state"] == "done" and step(pub2, "push")["remote_id"] == FIX
)
env = fake_env(TMP, scenario(FIX))  # GitHub now reports the head our push left
p = reply(env, pub2)
checkp("the reply proceeds: the comment step expects the tip our push left", p.returncode == 0, p)
check(
    "…one POST reached gh, with the publication marker appended to the body",
    len(posts()) == 1 and any("tauceti-publication:v1" in a for a in posts()[0]),
)
check(
    "…comment is done with the comment id",
    step(pub2, "comment")["state"] == "done" and step(pub2, "comment")["remote_id"] == "9001",
)
env = fake_env(TMP, scenario("c" * 40))  # a contributor pushed after us
pub2b = publication(env, FIX)
p = reply(env, pub2b)
check("a reply built on a head that has since moved is refused", p.returncode == 75 and len(posts()) == 1)

# ---- 3. the head cannot be read: fail closed --------------------------------------------------------------------
env = fake_env(TMP, scenario(None, view_rc=1))
pub3 = publication(env, FIX)
p = safe_push(env, pub3)
check(
    "an unreadable head refuses the push (head-unreadable), nothing pushed",
    p.returncode == 75 and "head-unreadable" in p.stderr and len(pushes()) == 1,
)

finish()

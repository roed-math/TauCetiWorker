#!/usr/bin/env python3
"""git-safe-push after an earlier push the pull request never picked up (2026-09-30, #10130).

A fixer's push put commit B on the branch, but GitHub left the PR's head at A, so every later round
leased against A and was refused as "stale info" (or, with HEAD = B, "succeeded" as "Everything
up-to-date" while the PR stayed behind). The wrapper now reads the branch's real tip before pushing:
  * the tip IS HEAD (nothing new to push): an empty re-sync commit goes on top and is pushed, leased on
    B, and the publication's push step (refused once, so pending again) is recorded done;
  * HEAD builds on the tip: HEAD is pushed, leased on B, with no empty commit;
  * the tip is a commit HEAD does not have (a real race): refused as before, nothing overwritten.

Proven by mock: a real git against a local bare repository, the fake gh answering the PR's (stale)
head. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import SCRIPTS, bare_repo, check, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env  # noqa: E402

scrub_env()
TMP = mktemp("safe-push-resync-")
os.environ["TAUCETI_WORKER_ID"] = "w-resync"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
os.environ["TAUCETI_FORK"] = "fake-login/TauCeti"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
HEADREPO = bare_repo(TMP, "fake-login/TauCeti")
BRANCH = "fix/thing"
URL = "https://github.com/fake-login/TauCeti"
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def git(work: Path, *args: str, check_rc: bool = True) -> str:
    p = subprocess.run(["/usr/bin/git", "-C", str(work), *args], capture_output=True, text=True,
                       env={**os.environ, **GIT_ID})
    if check_rc and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()


def clone(name: str) -> Path:
    work = TMP / name
    subprocess.run(["/usr/bin/git", "clone", "-q", str(HEADREPO), str(work)], check=True)
    git(work, "config", "user.name", "t")
    git(work, "config", "user.email", "t@x")
    return work


def commit(work: Path, msg: str) -> str:
    (work / "f.lean").write_text(f"-- {msg}\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", msg)
    return git(work, "rev-parse", "HEAD")


def remote_tip() -> str:
    return git(TMP, "--git-dir", str(HEADREPO), "rev-parse", f"refs/heads/{BRANCH}")


def scenario(pr_head: str) -> dict:
    return {"gh": [{"match": ["pr", "view", "42"], "rc": 0, "stdout": json.dumps({"headRefOid": pr_head}) + "\n"}],
            "git": [], "bare_repos": {"fake-login/TauCeti": str(HEADREPO)}}


def safe_push(work: Path, expect: str, pub_id: str = ""):
    env = fake_env(TMP, scenario(expect))
    env.update({"TAUCETI_PUSH_REF": BRANCH, "TAUCETI_PUSH_REMOTE": URL, "TAUCETI_PUSH_EXPECT": expect})
    if pub_id:
        env["TAUCETI_PUBLICATION_ID"] = pub_id
    return subprocess.run([str(SCRIPTS / "git-safe-push")], env=env, cwd=str(work), capture_output=True, text=True)


def publication(head: str) -> str:
    p = gate_cli(["publication", "create", "--kind", "fix", "--pr", "42", "--branch", BRANCH, "--head-sha", head,
                  "--remote", URL], fake_env(TMP, scenario(head)))
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def push_step(pub_id: str) -> dict:
    rec = json.loads((TMP / "gate" / "publications" / f"{pub_id}.json").read_text())
    return next(s for s in rec["steps"] if s["name"] == "push")


# The PR's history on the branch: A, then an earlier round's fix B that GitHub never attached to the PR.
seed = TMP / "seed"
subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", str(seed)], check=True)
A = commit(seed, "feat: first")
git(seed, "push", "-q", str(HEADREPO), f"HEAD:refs/heads/{BRANCH}")
B = commit(seed, "fix: an earlier round")
git(seed, "push", "-q", str(HEADREPO), f"HEAD:refs/heads/{BRANCH}")

# 1) The retry round checked out the branch (so HEAD = B) and has nothing new: an empty re-sync commit.
w1 = clone("w1")
git(w1, "checkout", "-q", BRANCH)
pub = publication(A)
p = safe_push(w1, A, pub)
tip = remote_tip()
check("a tip HEAD already is gets an empty re-sync commit, pushed", p.returncode == 0 and tip != B
      and git(w1, "rev-parse", "HEAD") == tip and git(w1, "rev-parse", f"{tip}^") == B
      and git(w1, "diff", "--stat", B, tip) == "")
if p.returncode != 0 or tip == B:
    print("      stderr: " + p.stderr.strip().replace("\n", "\n      "))
check("…whose message says why", "re-sync the pull request's head" in git(w1, "log", "-1", "--format=%s"))
check("…and the publication's push step is done at the new tip",
      push_step(pub).get("state") == "done" and push_step(pub).get("remote_id") == tip)

# 2) HEAD builds on the tip: pushed over it with no empty commit.
B2 = remote_tip()
w2 = clone("w2")
git(w2, "checkout", "-q", BRANCH)
C = commit(w2, "fix: the next finding")
p = safe_push(w2, A)
check("HEAD that contains the tip is pushed over it, with no extra commit",
      p.returncode == 0 and remote_tip() == C and git(w2, "rev-parse", "HEAD^") == B2)

# 3) A real race: the branch has a commit this HEAD lacks. Refused, nothing overwritten.
w3 = clone("w3")
git(w3, "checkout", "-q", A)
git(w3, "checkout", "-q", "-b", "mine")
commit(w3, "fix: a competing change")
before = remote_tip()
p = safe_push(w3, A)
check("a tip HEAD does not contain is refused as a race, the branch untouched",
      p.returncode != 0 and remote_tip() == before and "moved since checkout" in p.stderr)

finish()

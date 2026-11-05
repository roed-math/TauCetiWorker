#!/usr/bin/env python3
"""git-safe-push retries a push GitHub answered with a server error, and only that.

On 2026-11-04 a lookahead round's three pushes were answered `remote rejected (Internal Server Error)`
and `(failure)`; the wrapper told the agent to stop and retry next round, and the proof was lost with the
round's sandbox. Now:
  * a server error is tried again, up to three pushes in all, each admitted by the gate, inside one
    publication step; the branch moves once it gets through;
  * three server errors end with a message that says the commits exist only in the sandbox;
  * a lease rejection (`stale info`) is still never retried.

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
TMP = mktemp("push-server-error-")
os.environ["TAUCETI_WORKER_ID"] = "w9"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
os.environ["TAUCETI_FORK"] = "fake-login/TauCeti"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0", TAUCETI_GATE_PUSHES_PER_MINUTE="50", TAUCETI_GATE_PUSHES_PER_HOUR="500")
HEADREPO = bare_repo(TMP, "fake-login/TauCeti")
BRANCH = "fix/thing"
WORK = TMP / "work"
subprocess.run(["/usr/bin/git", "init", "-q", str(WORK)], check=True)
GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def commit(msg: str) -> str:
    (WORK / "f.lean").write_text(f"-- {msg}\n")
    subprocess.run(["/usr/bin/git", "-C", str(WORK), "add", "."], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(WORK), "commit", "-q", "-m", msg], check=True, env={**os.environ, **GIT})
    return subprocess.run(["/usr/bin/git", "-C", str(WORK), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def remote_tip() -> str:
    return subprocess.run(["/usr/bin/git", "-C", str(HEADREPO), "rev-parse", f"refs/heads/{BRANCH}"],
                          capture_output=True, text=True).stdout.strip()


A = commit("feat: first")
subprocess.run(["/usr/bin/git", "-C", str(WORK), "push", "-q", str(HEADREPO), f"HEAD:refs/heads/{BRANCH}"], check=True)
PUSH_ENV = {"TAUCETI_PUSH_REF": BRANCH, "TAUCETI_PUSH_REMOTE": "https://github.com/fake-login/TauCeti",
            "TAUCETI_PUSH_RETRY_SLEEP": "0"}
SERVER = "! [remote rejected] HEAD -> fix/thing (Internal Server Error)\nerror: failed to push some refs\n"
STALE = "! [rejected] HEAD -> fix/thing (stale info)\nerror: failed to push some refs\n"


def push_with(stderr: str, times: int, expect: str):
    """Push the next commit, the first `times` pushes failing with `stderr`. Returns (process, pushes)."""
    before = len([a for a in fake_calls(TMP, "git") if a[:1] == ["push"]])
    env = fake_env(TMP, {"gh": [{"match": ["pr", "view", "42"], "stdout": json.dumps({"headRefOid": expect}) + "\n"}],
                         "git": [{"match": ["push"], "rc": 1, "stderr": stderr, "times": times}],
                         "bare_repos": {"fake-login/TauCeti": str(HEADREPO)}})
    pub = gate_cli(["publication", "create", "--kind", "fix", "--pr", "42", "--branch", BRANCH, "--head-sha", expect,
                    "--remote", PUSH_ENV["TAUCETI_PUSH_REMOTE"]], env).stdout.strip()
    p = subprocess.run([str(SCRIPTS / "git-safe-push")], cwd=str(WORK), capture_output=True, text=True,
                       env={**env, **PUSH_ENV, "TAUCETI_PUSH_EXPECT": expect, "TAUCETI_PUBLICATION_ID": pub})
    return p, len([a for a in fake_calls(TMP, "git") if a[:1] == ["push"]]) - before


B = commit("fix: one")
p, n = push_with(SERVER, 2, A)
check("two server errors, then the push goes through", p.returncode == 0 and remote_tip() == B and n == 3)
check("…each retry was admitted by the gate",
      sum(1 for e in events(TMP) if e.get("kind") == "git_push" and e.get("decision") == "admit") >= 3)

C = commit("fix: two")
p, n = push_with(SERVER, 3, B)
check("three server errors stop after three pushes, and nothing moved", p.returncode == 1 and n == 3 and remote_tip() == B)
check("…saying the commits exist only in this sandbox", "only in this sandbox" in p.stderr)

p, n = push_with(STALE, 1, B)
check("a lease rejection is not retried", p.returncode == 1 and n == 1 and "push rejected" in p.stderr)

finish()

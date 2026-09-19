#!/usr/bin/env python3
"""T10 — the claim renewal fails or the lease expires during a stop (brief §8.3 T10).

git-safe-push re-checks the round's lease immediately before the push (`claim.sh holds`, then
`renew` when it is no longer held) and fails CLOSED when both say no: nothing is pushed and the
publication's push step stays `pending` (the refusal comes before the step is even sent). A stand-in
claim.sh answers from a state file so the lease can be lost and re-acquired without a network:
  * lost, not renewable  → exit 1, "lease … lost", no push, no ledger step sent;
  * re-acquired          → the fresh head check runs (one `gh pr view`) and the push proceeds, once;
  * held but expired and renewable → renew succeeds, the push proceeds.
The ledger's own check then still applies: a re-acquired lease on a PR whose head moved is refused
`stale-head`, so ownership alone never authorises a stale publication.

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
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
TMP = mktemp("gate-t10-")
os.environ["TAUCETI_WORKER_ID"] = "w10"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
os.environ["TAUCETI_FORK"] = "fake-login/TauCeti"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
HEADREPO = bare_repo(TMP, "fake-login/TauCeti")
BRANCH = "fix/lease"

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

# The stand-in claim.sh: `holds` and `renew` answer from $LEASE_STATE ("held" / "lost" / "expired-renewable").
LEASE = TMP / "lease.state"
CLAIM_SH = TMP / "claim.sh"
CLAIM_SH.write_text(
    "#!/usr/bin/env bash\n"
    f'state=$(cat "{LEASE}" 2>/dev/null)\n'
    f'echo "$1 $2 state=$state" >> "{TMP}/claim.log"\n'
    'case "$1" in\n'
    '  holds) [[ "$state" == held ]] && exit 0; exit 1;;\n'
    '  renew) [[ "$state" == expired-renewable ]] && { echo held > "' + str(LEASE) + '"; exit 0; }; exit 1;;\n'
    "  *) exit 2;;\n"
    "esac\n"
)
CLAIM_SH.chmod(0o755)


def scenario(live_head: str):
    return {
        "gh": [{"match": ["pr", "view", "42"], "rc": 0, "stdout": json.dumps({"headRefOid": live_head}) + "\n"}],
        "git": [],
        "bare_repos": {"fake-login/TauCeti": str(HEADREPO)},
    }


PUSH_ENV = {
    "TAUCETI_PUSH_REF": BRANCH,
    "TAUCETI_PUSH_REMOTE": "https://github.com/fake-login/TauCeti",
    "TAUCETI_PUSH_EXPECT": A,
    "TAUCETI_CLAIM_KEY": "branch/42",
    "TAUCETI_CLAIM_SH": str(CLAIM_SH),
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


def step(pub_id: str, name: str) -> dict:
    r = json.loads((TMP / "gate" / "publications" / f"{pub_id}.json").read_text())
    return next(s for s in r["steps"] if s["name"] == name)


def pushes():
    return [a for a in fake_calls(TMP, "git") if a[:1] == ["push"]]


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


def remote_tip() -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(HEADREPO), "rev-parse", f"refs/heads/{BRANCH}"], capture_output=True, text=True
    ).stdout.strip()


env = fake_env(TMP, scenario(A))
pub = publication(env, A)

# ---- 1. the lease is lost and cannot be renewed -----------------------------------------------------------------
LEASE.write_text("lost")
p = safe_push(env, pub)
check("lease lost + renew fails: git-safe-push exits 1 naming the lost lease", p.returncode == 1 and "lost" in p.stderr)
check("…nothing was pushed", pushes() == [] and remote_tip() == A)
check("…no head read was spent (the lease check comes first)", views() == [])
check("…the ledger's push step is still pending (never sent)", step(pub, "push")["state"] == "pending")
log = (TMP / "claim.log").read_text()
check("…claim.sh was asked `holds` then `renew`", "holds branch/42" in log and "renew branch/42" in log)

# ---- 2. re-acquired: the fresh head check runs, then the push ---------------------------------------------------
LEASE.write_text("held")
p = safe_push(env, pub)
checkp("with the lease back, the push proceeds", p.returncode == 0, p)
check("…after exactly one fresh head read", len(views()) == 1)
check("…exactly one push, and the branch holds the fix", len(pushes()) == 1 and remote_tip() == FIX)
check("…push is done with the new tip", step(pub, "push")["state"] == "done" and step(pub, "push")["remote_id"] == FIX)

# ---- 3. expired but renewable: renew, then push ----------------------------------------------------------------------
NEXT = commit("fix: round two")
env = fake_env(TMP, scenario(FIX))
pub2 = publication(env, FIX)
LEASE.write_text("expired-renewable")
p = subprocess.run(
    [str(SCRIPTS / "git-safe-push")],
    env={**env, **PUSH_ENV, "TAUCETI_PUSH_EXPECT": FIX, "TAUCETI_PUBLICATION_ID": pub2},
    cwd=str(WORK),
    capture_output=True,
    text=True,
)
checkp("an expired lease that renews lets the push through", p.returncode == 0 and remote_tip() == NEXT, p)
check("…claim.sh renewed it", LEASE.read_text().strip() == "held")

# ---- 4. ownership alone is not enough: a re-acquired lease on a moved head is still refused ----------------------------
env = fake_env(TMP, scenario("d" * 40))
pub3 = publication(env, NEXT)
LEASE.write_text("held")
n = len(pushes())
p = subprocess.run(
    [str(SCRIPTS / "git-safe-push")],
    env={**env, **PUSH_ENV, "TAUCETI_PUSH_EXPECT": NEXT, "TAUCETI_PUBLICATION_ID": pub3},
    cwd=str(WORK),
    capture_output=True,
    text=True,
)
check(
    "held lease + moved head: refused stale-head, nothing pushed",
    p.returncode == 75 and "stale-head" in p.stderr and len(pushes()) == n,
)

finish()

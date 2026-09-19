#!/usr/bin/env python3
"""T07 — a successful PR creation whose response is lost (brief §8.3 T07; design §6).

The real gh-safe-pr-create runs against a fake gh, under a publication created through the real
`tauceti-gate publication create`:
  * the fake `pr create` exits 0 with an EMPTY stdout (the response was lost): the `pr_create` step
    is `uncertain`, the publication is parked `needs-reconciliation`, and the body file now carries
    the publication's hidden marker;
  * running gh-safe-pr-create again is refused (75) without spawning gh — never a second create;
  * `tauceti-gate reconcile <id>` finds the PR by the hidden id with ONE `pr list` and marks
    `pr_create` (and `marker_check`) done with the PR number; a further create is still refused;
  * a second publication whose PR the fake does not know: reconcile leaves it `uncertain` and parked;
    a "restart" (`reconcile --all` in a new process) does not resend; the create is still refused;
  * a create that dies with a connection error (no HTTP status) is `uncertain` too, and reconciles.

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
TMP = mktemp("gate-t07-")
os.environ["TAUCETI_WORKER_ID"] = "w7"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
TARGET_MARKER = '<!--tauceti-target:v1 {"focus":"Alpha","id":"alpha-x"}-->'
BRANCH = "roadmap/alpha-x-w7"


def pub_marker(pub_id: str) -> str:
    return f'<!--tauceti-publication:v1 {{"id":"{pub_id}"}}-->'


def scenario(known: dict[str, int], *, create_rc=0, create_stdout="", create_stderr=""):
    """The fake gh: `pr create` answers as scripted; `pr list --head <branch>` returns the PRs the
    fake "knows", each carrying its publication marker in the body."""
    prs = [
        {"number": n, "headRefName": BRANCH, "body": f"This PR.\n\n{TARGET_MARKER}\n\n{pub_marker(pid)}\n"}
        for pid, n in known.items()
    ]
    return {
        "gh": [
            {"match": ["pr", "create"], "rc": create_rc, "stdout": create_stdout, "stderr": create_stderr},
            {"match": ["pr", "list"], "rc": 0, "stdout": json.dumps(prs) + "\n"},
        ],
        "git": [],
        "bare_repos": {},
    }


def publication(env) -> str:
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "tauceti_worker",
            "gate",
            "publication",
            "create",
            "--kind",
            "author",
            "--branch",
            BRANCH,
            "--head-sha",
            "0" * 40,
            "--remote",
            "https://github.com/fake-login/TauCeti",
        ],
        env={**env, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    pub_id = p.stdout.strip()
    # The branch was pushed before the create (the author sequence): mark the push step done.
    for args in (["begin", pub_id, "push", "--sha", "0" * 40], ["end", pub_id, "push", "ok", "--remote-id", "0" * 40]):
        q = gate_cli(["publication", *args], env)
        assert q.returncode == 0, q.stderr
    return pub_id


def create(env, pub_id: str, body: Path):
    return subprocess.run(
        [
            str(SCRIPTS / "gh-safe-pr-create"),
            "--repo",
            "TauCetiProject/TauCeti",
            "--base",
            "main",
            "--head",
            f"fake-login:{BRANCH}",
            "--title",
            "feat: x",
            "--body-file",
            str(body),
        ],
        env={**env, "TAUCETI_PUBLICATION_ID": pub_id, "TAUCETI_REQUIRE_TARGET_MARKER": "1"},
        capture_output=True,
        text=True,
    )


def record(pub_id: str) -> dict:
    return json.loads((TMP / "gate" / "publications" / f"{pub_id}.json").read_text())


def step(pub_id: str, name: str) -> dict:
    return next(s for s in record(pub_id)["steps"] if s["name"] == name)


def creates():
    return [a for a in fake_calls(TMP, "gh") if a[:2] == ["pr", "create"]]


def lists():
    return [a for a in fake_calls(TMP, "gh") if a[:2] == ["pr", "list"]]


# ---- 1. the response is lost ------------------------------------------------------------------------------
env = fake_env(TMP, scenario({}))
pub1 = publication(env)
env = fake_env(TMP, scenario({pub1: 501}))  # the PR exists on the "remote" — the create did happen
body = TMP / "body1.md"
body.write_text(f"This PR adds x.\n\n{TARGET_MARKER}\n")
p = create(env, pub1, body)
check("gh-safe-pr-create with a lost response exits 0 (gh did)", p.returncode == 0)
check("…the body file carries the publication marker", pub_marker(pub1) in body.read_text())
check("…the fake gh saw one pr create", len(creates()) == 1)
check(
    "…pr_create is UNCERTAIN (no PR number parsed) and the publication is parked needs-reconciliation",
    step(pub1, "pr_create")["state"] == "uncertain" and record(pub1)["parked"] == "needs-reconciliation",
)
p = create(env, pub1, body)
check("a second gh-safe-pr-create is refused (75) — never a second create", p.returncode == 75)
check("…with the reason on stderr", "publication: refused" in p.stderr)
check("…and gh was not spawned again", len(creates()) == 1)

# ---- 2. reconcile finds the PR by its hidden id -------------------------------------------------------------
n_lists = len(lists())
p = gate_cli(["reconcile", pub1], env)
check(
    "tauceti-gate reconcile <id> exits 0",
    p.returncode == 0,
)
check("…with exactly ONE pr list read", len(lists()) == n_lists + 1)
check(
    "…pr_create is done with the PR number",
    step(pub1, "pr_create")["state"] == "done" and step(pub1, "pr_create")["remote_id"] == "501",
)
check("…marker_check is done too (the body carried the target marker)", step(pub1, "marker_check")["state"] == "done")
check(
    "…the publication records its PR and is no longer parked",
    record(pub1)["pr"] == 501 and record(pub1)["parked"] is None,
)
p = create(env, pub1, body)
check("after reconciliation a create is still refused (the step is done)", p.returncode == 75 and len(creates()) == 1)
check(
    "the read went through the gate as a reserved `reconcile` api_read",
    any(
        e.get("decision") == "admit" and e.get("op") == "reconcile" and e.get("kind") == "api_read" for e in events(TMP)
    ),
)

# ---- 3. no PR to find: stays uncertain, parked, never resent ---------------------------------------------------------
env = fake_env(TMP, scenario({pub1: 501}))
pub2 = publication(env)
body2 = TMP / "body2.md"
body2.write_text(f"This PR adds y.\n\n{TARGET_MARKER}\n")
p = create(env, pub2, body2)
check("second publication: lost response again", p.returncode == 0 and step(pub2, "pr_create")["state"] == "uncertain")
p = gate_cli(["reconcile", pub2], env)
check("reconcile with no such PR on the remote exits 1", p.returncode == 1)
check(
    "…the step stays uncertain and the publication stays parked",
    step(pub2, "pr_create")["state"] == "uncertain" and record(pub2)["parked"] == "needs-reconciliation",
)
# a restart: a new process reconciles everything this worker left behind — nothing is resent
p = gate_cli(["reconcile", "--all"], env)
check("`reconcile --all` from a fresh process resends nothing", len(creates()) == 2)
p = create(env, pub2, body2)
check("…and the create stays refused", p.returncode == 75 and len(creates()) == 2)
p = gate_cli(["status"], env)
check("`tauceti-gate status` names the parked publication", pub2 in p.stdout and "parked" in p.stdout)
check("…and made no gh call", len(fake_calls(TMP, "gh")) == len(creates()) + len(lists()))

# ---- 4. a connection error after the create (no HTTP status) ------------------------------------------------------------
env = fake_env(TMP, scenario({pub1: 501}, create_rc=1, create_stderr="error connecting to api.github.com: timeout\n"))
pub3 = publication(env)
env = fake_env(
    TMP, scenario({pub1: 501, pub3: 503}, create_rc=1, create_stderr="error connecting to api.github.com: timeout\n")
)
body3 = TMP / "body3.md"
body3.write_text(f"This PR adds z.\n\n{TARGET_MARKER}\n")
p = create(env, pub3, body3)
check("a create that dies with a connection error exits non-zero", p.returncode != 0)
check("…and is UNCERTAIN, not pending: it may have landed", step(pub3, "pr_create")["state"] == "uncertain")
p = create(env, pub3, body3)
check("…so a retry is refused", p.returncode == 75)
p = gate_cli(["reconcile", pub3], env)
check("…and reconcile finds it", p.returncode == 0 and step(pub3, "pr_create")["remote_id"] == "503")

# ---- 5. a definite failure is pending again (a retry is allowed, nothing to reconcile) ------------------------------------
env = fake_env(
    TMP, scenario({}, create_rc=1, create_stderr="pull request create failed: GraphQL: Validation Failed (HTTP 422)\n")
)
pub4 = publication(env)
body4 = TMP / "body4.md"
body4.write_text(f"This PR adds w.\n\n{TARGET_MARKER}\n")
p = create(env, pub4, body4)
check(
    "a 422 leaves pr_create pending (proven not created)",
    p.returncode != 0 and step(pub4, "pr_create")["state"] == "pending",
)
check("…and the publication is not parked", record(pub4)["parked"] is None)

finish()

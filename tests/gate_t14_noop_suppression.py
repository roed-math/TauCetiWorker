#!/usr/bin/env python3
"""T14 — repeated fix/review/reaction processing of unchanged state (brief §8.3 T14, §8.1).

Four no-op suppressions, each against the fakes:
  * a fix reply (the shim's one admitted `gh api POST …/replies`) whose sanitised digest equals one
    already posted on the same PR head is SKIPPED: exit 0, "not posting it again", no POST; a reply
    with different content posts once; the digests live in the publication ledger;
  * with `TAUCETI_REACTIONS=0`, add/remove/age of the contest 👀 make ZERO gh calls and the contest
    path's own bookkeeping (a fresh claim is seen, a released one is not) still works off a marker
    file under the fleet store;
  * a contest re-review on a head that already had `TAUCETI_CONTEST_MAX_EXCHANGES` (2) exchanges
    posts NOTHING — no claim, no engine — and records a local "needs a human" incident; below the
    cap the survey's counter is what do_review consults;
  * with `TAUCETI_STUCK_ISSUES=0`, ensure_stuck_issue writes an incident file and never calls gh;
    with it on, an existing issue whose body is unchanged is not edited (one `issue list`, no
    `issue edit`/`create`).

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import SHIM, check, fake_calls, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env  # noqa: E402

scrub_env()
TMP = mktemp("gate-t14-")
os.environ["TAUCETI_WORKER_ID"] = "w14"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
HEAD = "a" * 40
PR = 42


def scenario(*, issue_body: str = "", issue_number: int = 900):
    issues = [{"number": issue_number, "title": f"Review stuck: PR #{PR}", "body": issue_body}] if issue_body else []
    return {
        "gh": [
            {"match": ["pr", "view", str(PR)], "rc": 0, "stdout": json.dumps({"headRefOid": HEAD}) + "\n"},
            {"match": ["api", "-X", "POST"], "rc": 0, "stdout": json.dumps({"id": 31}) + "\n", "times": 1},
            {"match": ["api", "-X", "POST"], "rc": 0, "stdout": json.dumps({"id": 32}) + "\n"},
            {"match": ["issue", "list"], "rc": 0, "stdout": json.dumps(issues) + "\n"},
            {"match": ["issue", "edit"], "rc": 0, "stdout": ""},
            {"match": ["issue", "create"], "rc": 0, "stdout": "https://github.com/TauCetiProject/TauCeti/issues/901\n"},
        ],
        "git": [],
        "bare_repos": {},
    }


env = fake_env(TMP, scenario())
os.environ.update(
    {k: env[k] for k in ("PATH", "TAUCETI_FAKE_SCENARIO", "TAUCETI_FAKE_LOG", "TAUCETI_REAL_GH", "TAUCETI_REAL_GIT")}
)

from tauceti_worker import github, interaction  # noqa: E402
from tauceti_worker import work_units as wu  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402
from tauceti_worker.survey import Candidate, Counters  # noqa: E402


def publication() -> str:
    p = gate_cli(
        [
            "publication",
            "create",
            "--kind",
            "fix",
            "--pr",
            str(PR),
            "--branch",
            "fix/x",
            "--head-sha",
            HEAD,
            "--remote",
            "https://github.com/fake-login/TauCeti",
        ],
        env,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def reply(pub_id: str, body: str):
    return subprocess.run(
        [
            str(SHIM / "gh"),
            "api",
            "-X",
            "POST",
            f"/repos/TauCetiProject/TauCeti/pulls/{PR}/comments/7/replies",
            "-f",
            f"body={body}",
        ],
        env={**env, "TAUCETI_PUBLICATION_ID": pub_id},
        capture_output=True,
        text=True,
    )


def posts():
    return [a for a in fake_calls(TMP, "gh") if "-X" in a and "POST" in a]


def step(pub_id: str, name: str) -> dict:
    r = json.loads((TMP / "gate" / "publications" / f"{pub_id}.json").read_text())
    return next(s for s in r["steps"] if s["name"] == name)


# ---- 1. the same fix reply twice on one head ------------------------------------------------------------------
BODY = "The finding is wrong: `exact?` closes it; see the synth-check output."
pub1 = publication()
p = reply(pub1, BODY)
check("the first reply posts", p.returncode == 0 and len(posts()) == 1)
check(
    "…and its digest is in the ledger", step(pub1, "comment")["state"] == "done" and step(pub1, "comment").get("digest")
)
pub2 = publication()  # a later round on the SAME head, saying the same thing again
p = reply(pub2, "  " + BODY.replace("  ", " ") + "\n")  # whitespace differs; the sanitised digest does not
check(
    "the same reply again is skipped: exit 0 and 'not posting it again'",
    p.returncode == 0 and "not posting it again" in p.stderr,
)
check("…with no second POST", len(posts()) == 1)
check(
    "…the second publication's comment step is still pending (nothing was sent)",
    step(pub2, "comment")["state"] == "pending",
)
p = reply(pub2, "A different objection: the lemma is already in Mathlib as Foo.bar.")
check("a reply with different content posts", p.returncode == 0 and len(posts()) == 2)

# ---- 2. reactions off: zero gh calls, local bookkeeping intact -------------------------------------------------------
os.environ["TAUCETI_REACTIONS"] = "0"
gh = github.GitHub()
n = len(fake_calls(TMP))
check("add_reaction with reactions off succeeds locally", gh.add_reaction(5) is True)
age = gh.fresh_claim_age(5)
check("…and the claim is seen as fresh by the same bookkeeping the survey uses", age is not None and age < 5)
check("…remove_reaction releases it", gh.remove_reaction(5) is True and gh.fresh_claim_age(5) is None)
check("…all with ZERO gh calls", len(fake_calls(TMP)) == n)
check("…the marker lived under the fleet store", not list((TMP / "gate" / "cache" / "reactions").glob("*.json")))
os.environ.pop("TAUCETI_REACTIONS")

# ---- 3. the contest exchange cap -----------------------------------------------------------------------------------
cfg = SimpleNamespace(wid="w14", state=TMP / "state", logdir=TMP / "logs", store_dir=TMP / "store")
counters = Counters(cfg)
w = SimpleNamespace(cfg=cfg, gh=gh, rs=None, counters=counters, claims=None)
opts = SimpleNamespace(work_model="claude")
c = Candidate(PR, HEAD, "author contest on scope", contest="scope", contest_reply_id=7)
counters.write(f"review-contest-{PR}-head-{HEAD[:12]}", interaction.contest_max_exchanges())
n = len(fake_calls(TMP))
raised = None
try:
    wu.do_review(w, None, c, opts, bubble=False)
except NoProgress as e:
    raised = e
check(
    "at the cap do_review yields (NoProgress) before claiming or launching anything",
    raised is not None and "needs a human" in str(raised),
)
check("…with ZERO gh calls (no 👀, no engine)", len(fake_calls(TMP)) == n)
inc = TMP / "gate" / "incidents" / f"contest-cap-{PR}-{HEAD[:12]}.json"
check(
    "…and a local incident says a human is needed",
    inc.exists() and "needs a human" in json.loads(inc.read_text())["message"],
)
try:
    wu.do_review(w, None, c, opts, bubble=False)
except NoProgress:
    pass
check(
    "…a repeat refreshes the ONE incident file rather than adding another",
    json.loads(inc.read_text())["count"] == 2 and len(list(inc.parent.glob("contest-cap-*"))) == 1,
)
check("the default cap is 2 exchanges", interaction.contest_max_exchanges() == 2)
os.environ["TAUCETI_CONTEST_MAX_EXCHANGES"] = "5"
check("…and TAUCETI_CONTEST_MAX_EXCHANGES raises it", interaction.contest_max_exchanges() == 5)
os.environ.pop("TAUCETI_CONTEST_MAX_EXCHANGES")

# ---- 4. stuck-review issues ----------------------------------------------------------------------------------------
os.environ["TAUCETI_STUCK_ISSUES"] = "0"
n = len(fake_calls(TMP))
gh.ensure_stuck_issue(PR, "its review has errored 3 times without posting a verdict", "diag")
inc = TMP / "gate" / "incidents" / f"review-stuck-{PR}.json"
check("stuck issues off: an incident file is written", inc.exists() and json.loads(inc.read_text())["pr"] == PR)
check("…and gh was not called", len(fake_calls(TMP)) == n)
os.environ.pop("TAUCETI_STUCK_ISSUES")
body = github.GitHub._stuck_issue_body(PR, "its review has errored 3 times without posting a verdict", "diag")
env = fake_env(TMP, scenario(issue_body=body))
os.environ["TAUCETI_FAKE_SCENARIO"] = env["TAUCETI_FAKE_SCENARIO"]
n = len(fake_calls(TMP))
gh.ensure_stuck_issue(PR, "its review has errored 3 times without posting a verdict", "diag")
calls = fake_calls(TMP)[n:]
check(
    "stuck issues on, body unchanged: one `issue list`, no edit, no create",
    [a[:2] for a in calls] == [["issue", "list"]],
)
gh.ensure_stuck_issue(PR, "its review has errored 3 times without posting a verdict", "diag")
check("…and again", [a[:2] for a in fake_calls(TMP)[n:]] == [["issue", "list"], ["issue", "list"]])

finish()

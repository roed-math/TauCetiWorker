#!/usr/bin/env python3
"""A sandboxed fix/rebase round's publication is settled by looking at the remote, not parked forever.

Inside a bubble the write wrappers cannot reach the gate store, so a round's steps stay `pending`
however it went; after one night 68 entries sat `parked=interrupted`, 15 of the last 30 for rounds
that had in fact pushed. `Publication.observe_unrecorded`, run by `reconcile_stale` once the round's
process is gone: a branch tip that moved off the recorded head is the push (done, observed); a comment
carrying the publication id is the comment, and none found is `skipped` (fix/rebase rounds need not
comment) — the entry is then complete. An unchanged tip means no remote effect: archived. An unreadable
remote leaves the entry alone for next time. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))
from harness import check as _check  # noqa: E402
from harness import fake_env, finish, gate_cli, gate_env, mktemp, scrub_env


def check(name, cond, detail=""):
    _check(name + (f"  [{detail}]" if detail and not cond else ""), cond)

scrub_env()
TMP = mktemp("gate-observe-")
gate_env(TMP)
os.environ["TAUCETI_WORKER_ID"] = "fixA"
HEAD, MOVED = "a" * 40, "b" * 40
pubs = TMP / "gate" / "publications"


def scenario(tip: str | None, *, ls_rc: int = 0, comments: str = "[]"):
    return {
        "gh": [{"match": ["api"], "rc": 0, "stdout": comments + "\n"}],
        "git": [{"match": ["ls-remote"], "rc": ls_rc, "stdout": (f"{tip}\trefs/heads/roadmap/x\n" if tip else "")}],
        "bare_repos": {},
    }


def create(env, kind="rebase") -> str:
    r = gate_cli(["publication", "create", "--kind", kind, "--branch", "roadmap/x", "--head-sha", HEAD,
                  "--pr", "77", "--repo", "taucetiproject/tauceti", "--remote", "https://x/y.git"], env)
    check(f"a {kind} publication is created", r.returncode == 0 and r.stdout.strip())
    return r.stdout.strip()


def dead(pub_id: str, *, parked: str | None = None) -> None:
    p = pubs / f"{pub_id}.json"
    d = json.loads(p.read_text())
    d["pid"] = 2**22 - 7  # certainly dead
    d["parked"] = parked
    p.write_text(json.dumps(d))


def record(pub_id: str) -> dict | None:
    p = pubs / f"{pub_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


# 1) the round pushed: tip moved, no comment → push done (observed), comment skipped, complete, not parked
env = fake_env(TMP, scenario(MOVED))
env["TAUCETI_WORKER_ID"] = "fixA"
pushed = create(env)
dead(pushed, parked="interrupted")  # exactly how tonight's 68 entries look
r = gate_cli(["reconcile"], env)
check("reconcile runs", r.returncode == 0, r.stderr[-200:])
d = record(pushed)
steps = {s["name"]: s for s in d["steps"]}
check("push observed done at the new tip", steps["push"]["state"] == "done" and steps["push"]["remote_id"] == MOVED and steps["push"].get("observed") is True, str(steps["push"]))
check("comment skipped (none found, none required)", steps["comment"]["state"] == "skipped", str(steps["comment"]))
check("the entry is complete and no longer parked", d["parked"] is None)
st = gate_cli(["status"], env).stdout
check("status no longer lists it as parked", (st[-200:]), "parked:" not in st)

# 2) the round did not push: tip unchanged → archived (no remote effect), never deleted
env = fake_env(TMP, scenario(HEAD))
env["TAUCETI_WORKER_ID"] = "fixA"
idle = create(env)
dead(idle)
gate_cli(["reconcile"], env)
check("an entry with no remote effect is archived", record(idle) is None and (pubs / "archive" / f"{idle}.json").is_file())

# 3) the remote cannot be read → left as it was (parked interrupted), to be observed next time
env = fake_env(TMP, scenario(None, ls_rc=1))
env["TAUCETI_WORKER_ID"] = "fixA"
unknown = create(env)
dead(unknown)
gate_cli(["reconcile"], env)
d = record(unknown)
check("an unreadable remote leaves every step pending and the entry parked", (str(d and d["parked"])), d is not None and all(s["state"] == "pending" for s in d["steps"]) and d["parked"] == "interrupted")

# 4) the comment was posted (marker found) → comment done, not skipped
marker = f"<!--tauceti-publication:{'x'}-->"
env = fake_env(TMP, scenario(MOVED))
env["TAUCETI_WORKER_ID"] = "fixA"
commented = create(env)
from tauceti_worker import publications as pm  # noqa: E402

env = fake_env(TMP, scenario(MOVED, comments=json.dumps([{"id": 4242, "body": "reply\n\n" + pm.marker(commented)}])))
env["TAUCETI_WORKER_ID"] = "fixA"
dead(commented)
gate_cli(["reconcile"], env)
d = record(commented)
steps = {s["name"]: s for s in d["steps"]}
check("a comment carrying the publication id is found and done", steps["comment"]["state"] == "done" and steps["comment"]["remote_id"] == "4242", str(steps["comment"]))
check("that entry is complete too", d["parked"] is None)
finish()

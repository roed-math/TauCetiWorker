#!/usr/bin/env python3
"""`gate publication prune` archives parked-`interrupted` ledger entries that never attempted a send.

A fixer round that opens a publication and dies before its first push (a build that fails, a download
that hits the round cap) leaves an entry with every step `pending`; the next reconcile parks it
`interrupted` and nothing resumes it. Fifty-one of those after one afternoon pushed the fleet's watch
view off the screen. They record no remote state, so `prune` moves them to `publications/archive/`
(never deletes) and logs one event. An entry with any attempted step (done or uncertain) is kept: that
is what the ledger exists for. Exit 0 = all hold; 1 = a mismatch.
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))
from harness import check, events, finish, gate_cli, gate_env, mktemp, scrub_env  # noqa: E402

scrub_env()
TMP = mktemp("gate-prune-")
env = gate_env(TMP)
env["TAUCETI_WORKER_ID"] = "fixA"
os.environ["TAUCETI_WORKER_ID"] = "fixA"
pubs = TMP / "gate" / "publications"


def create() -> str:
    r = gate_cli(["publication", "create", "--kind", "rebase", "--branch", "roadmap/x", "--head-sha", "a" * 40,
                  "--pr", "77", "--repo", "taucetiproject/tauceti", "--remote", "https://x/y.git"], env)
    check("a rebase publication is created", r.returncode == 0 and r.stdout.strip())
    return r.stdout.strip()


def park(pub_id: str, *, attempted: bool) -> None:
    p = pubs / f"{pub_id}.json"
    d = json.loads(p.read_text())
    d["parked"] = "interrupted"
    d["pid"] = 2**22 - 7  # certainly dead
    if attempted:
        d["steps"][0]["state"] = "uncertain"
    p.write_text(json.dumps(d))


dead = create()
park(dead, attempted=False)
tried = create()
park(tried, attempted=True)
live = create()  # open, in progress: not parked at all

before = gate_cli(["status"], env).stdout
check("status lists both parked entries before", before.count("parked:") == 2)

r = gate_cli(["publication", "prune"], env)
check("prune succeeds", r.returncode == 0)
check("prune reports one archived", "archived 1 " in r.stdout)
check("the never-attempted entry is archived, not deleted", (pubs / "archive" / f"{dead}.json").is_file())
check("it is gone from the live ledger", not (pubs / f"{dead}.json").exists())
check("the entry with an uncertain step is kept", (pubs / f"{tried}.json").is_file())
check("the open entry is kept", (pubs / f"{live}.json").is_file())
after = gate_cli(["status"], env).stdout
check("status now lists one parked entry", after.count("parked:") == 1)
ev = [e for e in events(TMP) if e.get("op") == "prune"]
check("one prune event is logged", len(ev) == 1 and "archived 1" in ev[0].get("detail", ""))
r2 = gate_cli(["publication", "prune"], env)
check("a second prune archives nothing", "archived 0 " in r2.stdout)
finish()

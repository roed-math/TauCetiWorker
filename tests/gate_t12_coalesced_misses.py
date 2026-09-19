#!/usr/bin/env python3
"""T12 — simultaneous cache misses, and an open fleet view (brief §8.3 T12; design §5).

Two worker processes share the gate store, observe the same PR at the same `updatedAt`, and both
miss its comments at the same moment. The first claims the in-flight marker under the gate lock and
fetches (the fake gh takes 1.5 s to answer); the second finds the live marker, waits, and re-reads
the sidecar the first one wrote — so the fake gh log holds exactly ONE comments fetch, and both
processes hold the same scoreboard (one `fresh`, one `assumed`). The sidecar lives under the fleet
store's `cache/review_state/`, not a per-worker directory. Then `tauceti-gate status` and `report`
run with the fakes on PATH and make zero gh/git calls: the fleet view renders local state only.

A marker whose owner died is not waited on (a third reader takes the miss over), and a fetch that
fails leaves the waiter to fetch for itself (bounded), so coalescing never turns one failure into
two silences.

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import check, fake_calls, fake_env, finish, gate_cli, gate_env, mktemp, scrub_env  # noqa: E402

scrub_env()
TMP = mktemp("gate-t12-")
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
gate_env(TMP)
PR = 77
UPDATED = "2026-09-19T00:00:00Z"
COMMENTS = [
    {
        "id": 1,
        "updated_at": "2026-09-19T00:00:00Z",
        "body": '<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 {"head_sha":"abc","round":1,"states":{"scope":"green"}}-->',
    }
]
SCENARIO = {
    "gh": [
        {
            "match": ["api", "--paginate", f"/repos/TauCetiProject/TauCeti/issues/{PR}/comments?per_page=100"],
            "rc": 0,
            "stdout": json.dumps(COMMENTS) + "\n",
            "sleep": 1.5,
        }
    ],
    "git": [],
    "bare_repos": {},
}
env = fake_env(TMP, SCENARIO)

READER = TMP / "reader.py"
READER.write_text(
    f"""
import json, os, sys, time
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from tauceti_worker import github, review_state
cfg = SimpleNamespace(sbcache=Path({str(TMP)!r}) / "per-worker" / os.environ.get("TAUCETI_WORKER_ID", "x"))
rs = review_state.ReviewState(cfg, github.GitHub())
rs.observe([SimpleNamespace(number={PR}, updated_at={UPDATED!r})])
t0 = time.time()
m = rs.gh_meta({PR})
print(json.dumps({{"provenance": m.provenance, "meta": m.data, "dir": str(rs.sbcache), "took": round(time.time() - t0, 2)}}))
"""
)


def reader(wid: str):
    return subprocess.Popen(
        [sys.executable, str(READER)],
        env={**env, "TAUCETI_WORKER_ID": wid, "PYTHONPATH": str(REPO)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def comment_fetches():
    return [a for a in fake_calls(TMP, "gh") if a[:2] == ["api", "--paginate"] and "comments" in a[2]]


# ---- 1. two readers miss at once ---------------------------------------------------------------------------
a = reader("w1")
time.sleep(0.3)  # long enough for w1 to have claimed the marker, far shorter than its 1.5 s fetch
b = reader("w2")
out_a, err_a = a.communicate(timeout=60)
out_b, err_b = b.communicate(timeout=60)
ra = json.loads(out_a.strip().splitlines()[-1]) if out_a.strip() else {}
rb = json.loads(out_b.strip().splitlines()[-1]) if out_b.strip() else {}
check("both readers finished cleanly", a.returncode == 0 and b.returncode == 0)
if a.returncode or b.returncode:
    print(err_a, err_b)
check("exactly ONE comments fetch reached the fake gh", len(comment_fetches()) == 1)
check("the first reader's answer is fresh", ra.get("provenance") == "fresh")
check(
    "the second reader's answer is the sidecar the first wrote (assumed), not a second fetch",
    rb.get("provenance") == "assumed",
)
check(
    "…and both hold the same scoreboard",
    ra.get("meta") == rb.get("meta") == {"head_sha": "abc", "round": 1, "states": {"scope": "green"}},
)
check(
    "the sidecars live under the fleet store, shared by both workers",
    ra.get("dir") == rb.get("dir") == str(TMP / "gate" / "cache" / "review_state"),
)
check("…and the shared key sidecar exists", (TMP / "gate" / "cache" / "review_state" / f"{PR}.key.json").exists())
check(
    "the in-flight marker was released after the fetch",
    not (TMP / "gate" / "cache" / "inflight" / f"{PR}.comments.json").exists(),
)

# ---- 2. the fleet view renders local state only -----------------------------------------------------------------
n = len(fake_calls(TMP))
p = gate_cli(["status"], env)
check("`tauceti-gate status` runs", p.returncode == 0 and "gate:" in p.stdout)
p = gate_cli(["status", "--json"], env)
check(
    "…and reports the publication queue (depth 0 here)",
    json.loads(p.stdout).get("publications", {}).get("queue_depth") == 0,
)
p = gate_cli(["report", "--since", "1h"], env)
check(
    "`tauceti-gate report` runs and prints the publication queue",
    p.returncode == 0 and "publications: queue depth" in p.stdout,
)
check("the fleet view made ZERO gh/git calls", len(fake_calls(TMP)) == n)

# ---- 3. a dead owner's marker is not waited on; a failed fetch does not silence the waiter ------------------------
inflight = TMP / "gate" / "cache" / "inflight"
inflight.mkdir(parents=True, exist_ok=True)
(inflight / f"{PR}.comments.json").write_text(json.dumps({"pid": 2**22 + 7, "at": time.time()}))  # no such process
for f in (TMP / "gate" / "cache" / "review_state").glob("*"):
    f.unlink()
t0 = time.time()
c = reader("w3")
out_c, err_c = c.communicate(timeout=60)
rc_ = json.loads(out_c.strip().splitlines()[-1]) if out_c.strip() else {}
check("a marker left by a dead process is taken over: the reader fetched (fresh)", rc_.get("provenance") == "fresh")
check("…without waiting the 20 s bound", rc_.get("took", 99) < 10)
check("…one more fetch", len(comment_fetches()) == 2)
# a peer whose fetch fails (a 404: gh_run does not retry it, unlike a 5xx): the waiter finds no
# entitled sidecar and fetches itself
env = fake_env(
    TMP,
    {
        "gh": [
            {"match": ["api", "--paginate"], "rc": 1, "stderr": "gh: HTTP 404: Not Found\n", "sleep": 1.0, "times": 1},
            {"match": ["api", "--paginate"], "rc": 0, "stdout": json.dumps(COMMENTS) + "\n"},
        ],
        "git": [],
        "bare_repos": {},
    },
)
for f in (TMP / "gate" / "cache" / "review_state").glob("*"):
    f.unlink()
d = reader("w4")
time.sleep(0.3)
e = reader("w5")
out_d, _ = d.communicate(timeout=60)
out_e, _ = e.communicate(timeout=60)
rd = json.loads(out_d.strip().splitlines()[-1]) if out_d.strip() else {}
re_ = json.loads(out_e.strip().splitlines()[-1]) if out_e.strip() else {}
check("the first reader's fetch failed (fetch_failed)", rd.get("provenance") == "fetch_failed")
check(
    "the waiter, finding nothing entitled, fetched for itself and got the board",
    re_.get("provenance") == "fresh" and re_.get("meta", {}).get("head_sha") == "abc",
)

finish()

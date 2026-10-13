#!/usr/bin/env python3
"""Fleets that take turns on one account share their scoreboard sidecars through
$TAUCETI_SHARED_READ_CACHE (review_state.review_state_dir).

Two fleets with separate gate stores read the same PR at the same `updatedAt`. With the shared
directory, the second fleet's read is the first fleet's sidecar (assumed) and the fake gh sees ONE
comments fetch; without it, each store is cold and the second fleet fetches again. A bust from one
fleet (it just posted) removes the shared sidecar, so the other fleet's next read is live.

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

from harness import check, fake_calls, fake_env, finish, mktemp, scrub_env  # noqa: E402

scrub_env()
os.environ.pop("TAUCETI_SHARED_READ_CACHE", None)
TMP = mktemp("shared-read-cache-")
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
PR = 91
UPDATED = "2026-10-12T23:00:00Z"
COMMENTS = [
    {
        "id": 1,
        "updated_at": UPDATED,
        "body": '<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 {"head_sha":"def","round":2,"states":{"scope":"green"}}-->',
    }
]
SCENARIO = {
    "gh": [
        {
            "match": ["api", "--paginate", f"/repos/TauCetiProject/TauCeti/issues/{PR}/comments?per_page=100"],
            "rc": 0,
            "stdout": json.dumps(COMMENTS) + "\n",
        }
    ],
    "git": [],
    "bare_repos": {},
}
env = fake_env(
    TMP, SCENARIO, TAUCETI_GATE_REQUIRED="1", TAUCETI_GATE_MUTATIONS_PER_HOUR="40", TAUCETI_GATE_READS_PER_HOUR="600"
)
SHARED = TMP / "fleet-shared" / "cache"

READER = TMP / "reader.py"
READER.write_text(
    f"""
import json, os, sys
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from tauceti_worker import github, review_state
cfg = SimpleNamespace(sbcache=Path({str(TMP)!r}) / "per-worker")
rs = review_state.ReviewState(cfg, github.GitHub())
rs.observe([SimpleNamespace(number={PR}, updated_at={UPDATED!r})])
if sys.argv[1:] == ["bust"]:
    rs.bust({PR})
    print("{{}}")
    sys.exit(0)
m = rs.gh_meta({PR})
print(json.dumps({{"provenance": m.provenance, "meta": m.data, "dir": str(rs.sbcache)}}))
"""
)


def read(fleet: str, shared: bool, *args: str) -> dict:
    e = {**env, "TAUCETI_GATE_DIR": str(TMP / fleet / "gate"), "TAUCETI_WORKER_ID": f"{fleet}-c1"}
    if shared:
        e["TAUCETI_SHARED_READ_CACHE"] = str(SHARED)
    p = subprocess.run([sys.executable, str(READER), *args], env=e, capture_output=True, text=True, timeout=60)
    if p.returncode:
        print(p.stderr)
    return json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}


def fetches() -> int:
    return len([a for a in fake_calls(TMP, "gh") if a[:2] == ["api", "--paginate"] and "comments" in a[2]])


# ---- 1. shared: the second fleet starts on the first fleet's read ----------------------------------------------
a = read("gq2", shared=True)
check("the first fleet fetched (fresh)", a.get("provenance") == "fresh" and fetches() == 1)
check("…into the shared directory, not its own store", a.get("dir") == str(SHARED / "review_state"))
b = read("gqm", shared=True)
check("the second fleet's read is the first fleet's sidecar (assumed)", b.get("provenance") == "assumed")
check("…with no second fetch", fetches() == 1)
check("…and the same scoreboard", a.get("meta") == b.get("meta") and b.get("meta", {}).get("head_sha") == "def")

# ---- 2. a bust in one fleet reaches the other -------------------------------------------------------------------
read("gqm", True, "bust")
check("a bust (the fleet just posted) removes the shared sidecars", not list((SHARED / "review_state").glob(f"{PR}.*")))
d = read("gq2", shared=True)
check("…so the other fleet's next read is live", d.get("provenance") == "fresh" and fetches() == 2)

# ---- 3. unset: each fleet keeps its own store, as before ---------------------------------------------------------
e1 = read("gqw", shared=False)
check(
    "without the variable the sidecars stay under the fleet's own store",
    e1.get("dir") == str(TMP / "gqw" / "gate" / "cache" / "review_state"),
)
check(
    "…and that cold store fetches even though the shared one is warm",
    e1.get("provenance") == "fresh" and fetches() == 3,
)

finish()

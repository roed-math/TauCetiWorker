#!/usr/bin/env python3
"""The curate stage end to end, offline: a target list with a merged PR, a closed-and-subsumed PR, a
closed PR without a verdict, and an eligible open item whose identifiers a fake `main` declares.
GitHub is a stub (PR states), the clone of main is a local git repository, the model is a stub that
writes `verdicts.json` (one strict yes, one no). Checks: tier A rewrites the finished PRs, tier B puts
only the fully-evidenced items to the model and marks the confirmed one `landed elsewhere`, the
file is written, the `targets-updated` incident lists the changes, and a second run reports the list
as current. Exit 0 = all hold; 1 = a mismatch."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import interaction  # noqa: E402
from tauceti_worker import work_units as W
from tauceti_worker.config import NoProgress  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-curate-"))
interaction.incidents_dir = lambda: TMP / "incidents"
targets = TMP / "targets.md"
targets.write_text("""# targets
<!-- tauceti-targets:v1 -->

## Area
- [~] `merged-one` — L0, "Prove `IsFoo K`." (serves: B1; needs: none; in flight: #101)
- [~] `subsumed-one` — L0, "Define `unitFiltration K i`." (serves: B1; needs: none; in flight: #102)
- [~] `closed-quiet` — L0, "Define `Bar.baz`." (serves: B1; needs: none; in flight: #103)
- [ ] `landed-by-others` — L1, "Prove `Teich.omega` is a section of `residueMap`." (serves: B2; needs: `merged-one`)
- [ ] `half-there` — L1, "Prove `Gone.thing` and `alsoGone`." (serves: B2; needs: `merged-one`)
- [ ] `not-landed` — L1, "Prove `frobeniusAlgEquiv`." (serves: B2; needs: `merged-one`)
""")
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(targets)

# a fake `main` with declarations for two of the three open items
clone = TMP / "state" / "curate" / "TauCeti"
(clone / "TauCeti").mkdir(parents=True)
(clone / "TauCeti" / "Teich.lean").write_text("theorem Teich.omega : True := trivial\n\ndef residueMap : Nat := 0\n")
(clone / "TauCeti" / "Frob.lean").write_text("def frobeniusAlgEquiv : Nat := 0\n")
(clone / "TauCeti" / "Half.lean").write_text("def alsoGone : Nat := 0\n")  # `Gone.thing` is missing: not fully evidenced
subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "main"], check=True)
W._curate_main_checkout = lambda w: clone

# the verdict a declining rebase left for #102
(TMP / "incidents" / "acked").mkdir(parents=True)
(TMP / "incidents" / "acked" / "declined-rebase-102.json").write_text(json.dumps(
    {"kind": "declined", "pr": 102, "stage": "rebase", "head": "x", "last_at": "2026-09-21T00:00:00Z",
     "summary": "main subsumes this PR via #900", "subsumed_by": [900], "mentions": []}))

STATES = {101: "MERGED", 102: "CLOSED", 103: "CLOSED"}
asked = {}


def fake_agent(cwd, prompt, profile, logdir):
    cand = json.loads((Path(cwd) / "candidates.json").read_text())
    asked["slugs"] = [c["slug"] for c in cand["candidates"]]
    asked["prompt_names_file"] = "verdicts.json" in prompt
    verdicts = {}
    for c in cand["candidates"]:
        if c["slug"] == "landed-by-others":
            verdicts[c["slug"]] = {"landed": True, "evidence": "TauCeti/Teich.lean:1 `Teich.omega` — states the section property"}
        else:
            verdicts[c["slug"]] = {"landed": False, "evidence": "only a stub"}
    (Path(cwd) / "verdicts.json").write_text(json.dumps(verdicts))
    return 0


W.run_agent_host = fake_agent
W._effective_authoring_profile = lambda opts: "claude"


class Claims:
    def begin_global_work(self, key):
        return 0

    def release(self):
        pass


class Counters:
    def __init__(self):
        self.d = {}

    def write(self, k, v):
        self.d[k] = v

    def read(self, k):
        return self.d.get(k, 0)


class GH:
    calls = []

    def pr_view(self, pr, fields):
        GH.calls.append(pr)
        return {"state": STATES.get(pr, "OPEN")}

    def pr_list(self, fields, state="merged"):
        return []


w = SimpleNamespace(cfg=SimpleNamespace(state=TMP / "state", logdir=TMP / "logs"), gh=GH(), claims=Claims(), counters=Counters())
sv = SimpleNamespace(open_prs=[])
rc = W.do_curate(w, sv, None, SimpleNamespace(), False)
check("the round counts as productive", rc == 0, str(rc))
check("tier A read each in-flight PR once", sorted(GH.calls) == [101, 102, 103], str(GH.calls))
new = targets.read_text()
check("merged and subsumed items are done", "- [x] `merged-one`" in new and "landed: #101" in new and "- [x] `subsumed-one`" in new and "subsumed by: #900; closed: #102" in new)
check("the closed PR without a verdict is left in flight", "- [~] `closed-quiet`" in new)
check("only fully evidenced eligible items went to the model", asked.get("slugs") == ["landed-by-others", "not-landed"], str(asked))
check("the prompt tells the model where to write", asked.get("prompt_names_file") is True)
check("the confirmed item is marked landed elsewhere with its evidence", "- [x] `landed-by-others`" in new and "landed elsewhere: TauCeti/Teich.lean:1" in new)
check("the refused and half-evidenced items stay open", "- [ ] `not-landed`" in new and "- [ ] `half-there`" in new)
inc = list((TMP / "incidents").glob("targets-updated-*.json"))
rec = json.loads(inc[0].read_text()) if inc else {}
check("a targets-updated incident lists the applied changes and the open question", len(inc) == 1 and len(rec.get("changes", [])) == 3 and len(rec.get("undecided", [])) == 1, str(rec)[:200])
check("the attempt timestamp is recorded", w.counters.read("curate-attempt-ts") > 0)

# second run: nothing left to do, and the model is not asked again about `not-landed` (main unchanged)
GH.calls.clear()
asked.clear()
try:
    W.do_curate(w, sv, None, SimpleNamespace(), False)
    check("a second run raises NoProgress", False)
except NoProgress as e:
    check("a second run reports the list as current, with the open question", "current" in str(e) and "1 closed PR" in str(e), str(e))
check("a not-landed verdict is remembered while main is unchanged", asked == {}, str(asked))
# The round above called do_curate directly. The fleet reaches it through the cascade, which walks
# AUTO_STAGES: a stage that is surveyed but not listed there is never dispatched (2026-09-21).
from tauceti_worker.constants import AUTO_STAGES as _AUTO

check("curate is the last stage of the cascade", "curate" in _AUTO and _AUTO[-1] == "curate", str(_AUTO))

# The gate's push allowlist: the curator may push to the operator's target-list repository and
# nowhere else; never canonical; no other op inherits the permission.
from tauceti_worker import gate as _gate

os.environ["TAUCETI_TARGETS_REPO"] = "roed-math/tauceti-fleet"
check("curate may push to the target-list repo", _gate.push_target_allowed("curate", "https://github.com/roed-math/tauceti-fleet.git"))
check("curate may not push elsewhere", not _gate.push_target_allowed("curate", "https://github.com/roed-math/other.git"))
check("the permission does not extend to other ops", not _gate.push_target_allowed("push", "https://github.com/roed-math/tauceti-fleet.git"))
os.environ["TAUCETI_TARGETS_REPO"] = _gate.TAUCETI
check("never canonical", not _gate.push_target_allowed("curate", f"https://github.com/{_gate.TAUCETI}.git"))
del os.environ["TAUCETI_TARGETS_REPO"]

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

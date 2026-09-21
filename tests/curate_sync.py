#!/usr/bin/env python3
"""The curator's pure half: what it does to the operator's target list.

`sync_inflight` rewrites `[~]` items whose PR has finished — merged → `[x]` (landed: #N); closed with
a recorded verdict that main subsumed it → `[x]` naming the upstream PRs; closed without one →
reported for the owner, left alone; open/unknown → untouched. `item_identifiers` extracts the Lean
names a milestone quotes. `mark_landed_elsewhere` marks an open item done with the curator's evidence.

Exit 0 = all hold; 1 = a mismatch.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import targets as T  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TEXT = """# targets
<!-- tauceti-targets:v1 -->

## Area
- [~] `merged-one` — Layer 0, "Prove `IsFoo K`." (serves: B1; needs: none; in flight: #101)
- [~] `subsumed-one` — Layer 0, "Define `unitFiltration K i`." (serves: B1; needs: none; in flight: #102)
- [~] `closed-quiet` — Layer 0, "Define `Bar.baz`." (serves: B1; needs: none; in flight: #103)
- [~] `still-open` — Layer 0, "Prove `Quux`." (serves: B1; needs: none; in flight: #104)
- [ ] `open-item` — Layer 1, "Prove `Teich.omega` is a section of `residueMap`, with `q − 1` torsion and `ℚ_[p]` unchanged." (serves: B2; needs: `merged-one`)
- [ ] `bare-item` — Layer 1, "Prove the thing." (serves: B2; needs: none)
"""
states = {101: "MERGED", 102: "CLOSED", 103: "CLOSED", 104: "OPEN"}
verdicts = {102: {"summary": "main subsumes this PR via #900", "subsumed_by": [900], "mentions": []},
            103: {"summary": "push blocked by infrastructure", "subsumed_by": [], "mentions": []}}
new, changes = T.sync_inflight(TEXT, states, verdicts)
t = T.parse_targets(new)
by = {it.slug: it for items in t.areas.values() for it in items}
check("merged PR → done, landed noted", by["merged-one"].status == "done" and "landed: #101" in by["merged-one"].line)
check("closed + subsumed verdict → done naming the upstream PR", by["subsumed-one"].status == "done" and "subsumed by: #900; closed: #102" in by["subsumed-one"].line)
check("closed without a verdict is left in flight and reported", by["closed-quiet"].status == "inflight" and any("closed-quiet" in c and "owner decides" in c for c in changes))
check("open PR untouched", by["still-open"].status == "inflight" and "in flight: #104" in by["still-open"].line)
check("the parser still reads the rewritten metadata", by["subsumed-one"].needs == [] and by["merged-one"].meta)
check("three change lines", len(changes) == 3, str(changes))
check("open items now eligible behind the merged one", [it.slug for it in T.eligible_items(t, "Area")] == ["open-item", "bare-item"])

idents = T.item_identifiers(by["open-item"])
check("identifiers: CamelCase and dotted names, not math or plain words", idents == ["Teich.omega", "residueMap"], str(idents))
check("no identifiers for a plain-prose item", T.item_identifiers(by["bare-item"]) == [])

marked, ok = T.mark_landed_elsewhere(new, "open-item", "TauCeti/X.lean:12 `Teich.omega`")
t2 = T.parse_targets(marked)
it2 = {it.slug: it for items in t2.areas.values() for it in items}["open-item"]
check("landed elsewhere marks the item done with its evidence in the metadata", ok and it2.status == "done" and "landed elsewhere: TauCeti/X.lean:12" in it2.line)
check("marking an unknown slug changes nothing", T.mark_landed_elsewhere(new, "nope", "x") == (new, False))
check("idempotent sync", T.sync_inflight(new, states, verdicts)[0] == new)
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

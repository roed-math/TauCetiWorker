#!/usr/bin/env python3
"""The decide stage retires the prerequisites it added once the PRs they unblock have moved on, offline.

A decide-added item whose PR merged or closed comes off the list with its note lines; one whose PR is
still open, one another open item needs, one a parked PR waits on, a done one, and an operator's item
with no `added by the decide stage` clause all stay. A failed state read keeps the item. With no decline
and no wait ruling, an added prerequisite still on the list makes the round due. Exit 0 = all hold."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import decide as D  # noqa: E402
from tauceti_worker import interaction  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.survey import decide_due  # noqa: E402
from tauceti_worker.targets import parse_targets, remove_items  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-decide-retire-"))
INC = TMP / "incidents"
(INC / "acked").mkdir(parents=True)
interaction.incidents_dir = lambda: INC
TAIL = "source: L0; added by the decide stage"
LIST = f"""# targets
<!-- tauceti-targets:v1 -->

## Area
- [ ] `merged-one` — L0, "Prove A." (unblocks: #201; needs: none; {TAIL}; partial: #301)
> a note under merged-one
- [ ] `closed-one` — L0, "Prove B." (unblocks: #202; needs: none; {TAIL})
- [~] `inflight-closed` — L0, "Prove C." (unblocks: #203; needs: none; {TAIL}; in flight: #303)
- [ ] `still-open` — L0, "Prove D." (unblocks: #204; needs: none; {TAIL})
- [ ] `needed` — L0, "Prove E." (unblocks: #205; needs: none; {TAIL})
- [ ] `consumer` — L1, "Prove F." (serves: B1; needs: `needed`)
- [ ] `parked-on` — L0, "Prove G." (unblocks: #206; needs: none; {TAIL})
- [x] `done-one` — L0, "Prove H." (unblocks: #207; needs: none; {TAIL}; landed: #307)
- [ ] `operator-item` — L0, "Prove I." (unblocks: #208; needs: none)
- [ ] `unreadable` — L0, "Prove J." (unblocks: #209; needs: none; {TAIL})

## Gaps (no roadmap milestone yet)
- nothing here
"""
tpath = TMP / "targets.md"
tpath.write_text(LIST)
(INC / "acked" / "declined-fix-210.json").write_text(
    json.dumps({"kind": "declined", "pr": 210, "head": "h210", "decision": "wait", "blocked_on_targets": ["parked-on"]})
)

STATES = {201: "MERGED", 202: "CLOSED", 203: "CLOSED", 205: "MERGED", 206: "MERGED", 207: "MERGED", 208: "MERGED"}
reads: list[int] = []


class GH:
    def pr_view(self, n, fields):
        reads.append(n)
        if n == 209:
            return None
        return {"state": STATES.get(n, "OPEN"), "headRefOid": f"h{n}", "labels": []}


commits = []
W._commit_targets = lambda path, applied, prefix="curate": commits.append((prefix, applied))
w = SimpleNamespace(gh=GH())
open_by_no = {204: SimpleNamespace(number=204, head_oid="h204", labels=())}

lines = D._retire_prerequisites(w, open_by_no, tpath, parse_targets(LIST))
text = tpath.read_text()
left = parse_targets(text)
gone = {"merged-one", "closed-one", "inflight-closed"}
check("a prerequisite whose PR merged is retired", left.find("merged-one") is None)
check("its note line goes with it", "a note under merged-one" not in text)
check("one whose PR closed unmerged is retired", left.find("closed-one") is None)
check("one in flight whose PR closed is retired", left.find("inflight-closed") is None)
check("one whose PR is still open stays", left.find("still-open") is not None)
check("the open PR is not read again", 204 not in reads, str(reads))
check("one another open item needs stays", left.find("needed") is not None)
check("one a parked PR waits on stays", left.find("parked-on") is not None)
check("a done one stays as the record", left.find("done-one") is not None)
check("an operator's own item stays", left.find("operator-item") is not None)
check("a failed state read keeps the item", left.find("unreadable") is not None)
check(
    "each retirement is reported with its reason",
    len(lines) == 3 and all(any(f"`{s}`" in ln for ln in lines) for s in gone) and "#202 closed" in " ".join(lines),
    str(lines),
)
check(
    "the change is committed once, as the decide stage's",
    len(commits) == 1 and commits[0][0] == "decide" and len(commits[0][1]) == 3,
    str(commits),
)
check("the Gaps section is untouched", text.endswith("## Gaps (no roadmap milestone yet)\n- nothing here\n"))

commits.clear()
again = D._retire_prerequisites(w, open_by_no, tpath, parse_targets(text))
check("a second pass has nothing to retire", again == [] and commits == [], str(again))

t2, removed = remove_items("- [ ] `a` — x (needs: none)\n> n\n\n- [ ] `b` — y (needs: none)\n", {"a", "zz"})
check(
    "remove_items drops only the named items and reports what it removed",
    removed == ["a"] and t2 == "\n- [ ] `b` — y (needs: none)\n",
    repr((t2, removed)),
)

# with no decline and no wait ruling, an added prerequisite still listed makes the round due
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tpath)
(INC / "acked" / "declined-fix-210.json").unlink()
never_ran = SimpleNamespace(read=lambda k: 0)
check("an added prerequisite still on the list makes the round due", decide_due(never_ran)[0])
tpath.write_text(LIST.split("## Area")[0] + '## Area\n- [ ] `operator-item` — L0, "Prove I." (needs: none)\n')
check("without one, nothing is due", not decide_due(never_ran)[0])

sys.exit(1 if fails else 0)

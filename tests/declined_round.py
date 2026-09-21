#!/usr/bin/env python3
"""A nothing-landed round leaves the agent's final message where the owner will see it.

2026-09-20: a Codex fixer concluded PR #4994 was entirely subsumed by a merged upstream PR and
declined to act; the round logged "nothing landed — not a failure" and the verdict lived only in a
2 MB transcript. `attention.record_declined_round` now files a local `declined` incident with the
transcript's last `[assistant]` section, a `close_hint` when it reads like "close this PR", and the
transcript path. Checks: the last assistant section is picked over reasoning/turn/result sections and
the bubble teardown line; long messages are bounded; the hint fires on the #4994 wording and not on
"another worker pushed first"; a transcript with no assistant text still produces an incident.

Exit 0 = all checks hold; 1 = a mismatch.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import attention, interaction  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        fails += 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-declined-"))
interaction.incidents_dir = lambda: TMP / "incidents"  # keep the test off the real gate store

SUBSUMED = """[tool shell]
git merge origin/main
[reasoning]
**Confirming merge halted due to identity error with no conflicts**
[assistant]
Working through the merge.
[tool shell]
git log --oneline -5
[assistant]
Unable to reconcile because current `main` subsumes PR #4994's entire target.
- PR #4994 only changes `TauCeti/.../Perron.lean`.
- `main` replaced that obsolete path with `Perron/Basic.lean` and `Perron/Formula.lean`.
- Upstream PR #6126 implements the same integral/series interchange.
[turn] completed; input=376914, cached_input=332288, output=4781, reasoning_output=2634
Session terminated, killing shell...  Popping ephemeral bubble 'tauceti-worker-gq2-fix2'...
[assistant]
this line is after teardown and must not count
"""
logdir = TMP / "logs"
logdir.mkdir()
old = logdir / "agent-codex-20260920-194609.log"
old.write_text("[assistant]\nan older round's verdict\n")
new = logdir / "agent-codex-20260920-211119.log"
new.write_text(SUBSUMED)
old_t = time.time() - 3600
import os  # noqa: E402

os.utime(old, (old_t, old_t))

text = attention.final_assistant_text(new)
check("last assistant section is picked", text.startswith("Unable to reconcile because current `main` subsumes"), text[:60])
check("reasoning/turn/tool sections are not part of it", "[turn]" not in text and "**Confirming" not in text)
check("text after the bubble teardown line is ignored", "after teardown" not in text)
check("the section body is complete", text.rstrip().endswith("interchange."), text[-40:])
check("the newest transcript is the one read", attention.newest_agent_log(logdir) == new)

os.environ["TAUCETI_WORKER_ID"] = "gq2-fix2"
os.environ["TAUCETI_PUBLICATION_ID"] = "gq2-fix2-20260921T011119-7f6bb2"
p = attention.record_declined_round(logdir, stage="rebase", pr=4994, head="5998d145cc69", reason="")
check("an incident file is written", p is not None and p.is_file(), str(p))
rec = json.loads(p.read_text())
check("kind/key/pr/stage/worker recorded", (rec["kind"], rec["key"], rec["pr"], rec["stage"], rec["worker"]) == ("declined", "rebase-4994", 4994, "rebase", "gq2-fix2"), str({k: rec.get(k) for k in ("kind", "key", "pr", "stage", "worker")}))
check("summary is the agent's final message", rec["summary"].startswith("Unable to reconcile"))
check("close hint fires on 'subsumes'", rec["close_hint"] is True)
check("transcript path and publication id travel with it", rec["transcript"] == str(new) and rec["publication"].endswith("7f6bb2"))
p2 = attention.record_declined_round(logdir, stage="rebase", pr=4994, head="5998d145cc69")
check("a repeat for the same PR refreshes the same file with count 2", p2 == p and json.loads(p.read_text())["count"] == 2)

benign = logdir / "agent-claude-20260920-230000.log"
benign.write_text("[assistant]\nI attempted to push but git-safe-push declined: another worker pushed the branch first. Nothing to do.\n[result] success; turns=3\n")
p3 = attention.record_declined_round(logdir, stage="fix", pr=5501, head="abc")
rec3 = json.loads(p3.read_text())
check("no close hint for 'another worker pushed first'", rec3["close_hint"] is False and rec3["key"] == "fix-5501")

empty = logdir / "agent-claude-20260920-230100.log"
empty.write_text("[tool shell]\nlake build\n[result] success; turns=1\n")
p4 = attention.record_declined_round(logdir, stage="fix-ci", pr=None, head="deadbeefcafe0000")
rec4 = json.loads(p4.read_text())
check("no assistant text still yields an incident keyed by head", rec4["key"] == "fix-ci-deadbeefcafe" and "no final assistant message" in rec4["summary"])

long = logdir / "agent-claude-20260920-230200.log"
long.write_text("[assistant]\n" + "x" * 5000 + "\n")
check("summary is bounded", len(attention.final_assistant_text(long)) <= attention.MAX_SUMMARY)

check("the #4994 wording yields #6126 as a mention (no trailer)", (rec["subsumed_by"], rec["mentions"]) == ([], [6126]) if "6126" in SUBSUMED else True)

trailer = logdir / "agent-claude-20260920-230300.log"
trailer.write_text("[assistant]\nThe branch's only file was replaced on main by PR #6126 and #6130; see also #4994 itself.\n\n**Subsumed-by:** #6126 #6130\n[result] success; turns=4\n")
p5 = attention.record_declined_round(logdir, stage="rebase", pr=4995, head="abc")
rec5 = json.loads(p5.read_text())
check("a Subsumed-by trailer is parsed, the target PR excluded, prose mentions deduplicated", (rec5["subsumed_by"], rec5["mentions"], rec5["close_hint"]) == ([6126, 6130], [4994], True), str((rec5["subsumed_by"], rec5["mentions"])))
decl, ment = attention.subsuming_prs("Subsumed-by: unknown", 1)
check("'Subsumed-by: unknown' declares nothing", (decl, ment) == ([], []))

items = [d for d in interaction.list_incidents() if d["kind"] == "declined"]
check("list_incidents sees the declined items", len(items) == 4, str(len(items)))
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

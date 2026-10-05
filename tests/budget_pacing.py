#!/usr/bin/env python3
"""The worker's half of budget pacing (tauceti-fleet `authoring.fallback_max_open = "auto"`):
  * fallback_cap: the fleet's cap file when pacing is on and the file is fresh, else the environment's;
  * the outside-list fallback decides by that cap and says it is budget-paced;
  * round_kind: a round's spend is target work, outside work or shared work;
  * the transcript renderer keeps what the agent reported spending (Claude's cost, Codex's tokens),
    and record_round_spend appends one line per round to state/<id>/rounds.jsonl.
Exit 0 = all hold; 1 = a mismatch."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
for var in ("TAUCETI_GATE_DIR", "TAUCETI_FALLBACK_AUTO", "TAUCETI_TARGETS_FALLBACK_MAX_OPEN", "TAUCETI_ROADMAP_TARGETS",
            "TAUCETI_LOOKAHEAD"):
    os.environ.pop(var, None)
from tauceti_worker import agents  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402
from tauceti_worker.survey import PRInfo  # noqa: E402
from tauceti_worker.targets import parse_targets  # noqa: E402
from tauceti_worker.transcript import AgentTranscriptRenderer  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="budget-pacing-"))
GATE = TMP / "gate"
GATE.mkdir()

# ---- the cap -------------------------------------------------------------------------------------------
check("pacing off: the environment's cap", W.fallback_cap() == (W.TARGETS_FALLBACK_MAX_OPEN, False))
os.environ["TAUCETI_GATE_DIR"] = str(GATE)
(GATE / W.FALLBACK_CAP_FILE).write_text(json.dumps({"cap": 3, "at": time.time()}))
check("pacing off, a cap file present: still the environment's", W.fallback_cap()[1] is False)
os.environ["TAUCETI_FALLBACK_AUTO"] = "1"
check("pacing on, a fresh file: the paced cap", W.fallback_cap() == (3, True))
(GATE / W.FALLBACK_CAP_FILE).write_text(json.dumps({"cap": 3, "at": time.time() - W.FALLBACK_CAP_TTL - 5}))
check("pacing on, a stale file: the environment's", W.fallback_cap() == (W.TARGETS_FALLBACK_MAX_OPEN, False))

# ---- the fallback decides by it ----------------------------------------------------------------------------
BLOCKED = parse_targets("""# t
<!-- tauceti-targets:v1 -->

## A
- [~] `in-flight` — L0, "Being done." (serves: B1; needs: none; in flight: #1)
- [ ] `waits` — L1, "Needs the first." (serves: B1; needs: `in-flight`)
""")
W._live_target_view = lambda t, path, sv, gh: (t, 0, 0)
(GATE / W.FALLBACK_CAP_FILE).write_text(json.dumps({"cap": 3, "at": time.time()}))
w = SimpleNamespace(claims=None, gh=None, cfg=SimpleNamespace(wid="tst-c1"))
try:
    W._pick_target(w, SimpleNamespace(_mine_open_prs=[object()] * 4), BLOCKED, Path("t.md"), "auto", [])
    check("4 open PRs over a paced cap of 3: no outside authoring", False)
except NoProgress as e:
    check("4 open PRs over a paced cap of 3: no outside authoring, said as budget-paced", "> 3 (budget-paced)" in str(e), str(e))
check("3 open PRs under it: author outside the list",
      W._pick_target(w, SimpleNamespace(_mine_open_prs=[object()] * 3), BLOCKED, Path("t.md"), "auto", []) is None)

# ---- what a round spent its budget on ------------------------------------------------------------------------
tfile = TMP / "t.md"
tfile.write_text("""# t
<!-- tauceti-targets:v1 -->

## A
- [ ] `item` — L0, "An item." (serves: B1; needs: none)
""")
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)
ours_target = PRInfo.from_json({"number": 11, "author": {"login": "me"},
                                "body": '<!--tauceti-target:v1 {"focus":"A","id":"item"}-->'})
ours_other = PRInfo.from_json({"number": 12, "author": {"login": "me"}, "body": ""})
theirs = PRInfo.from_json({"number": 13, "author": {"login": "you"}, "body": ""})
sv = SimpleNamespace(open_prs=[ours_target, ours_other, theirs], _mine_open_prs=[ours_target, ours_other])
c = lambda pr: SimpleNamespace(pr=pr, head="", reason="")  # noqa: E731
check("an author on a list item: target", W.round_kind(SimpleNamespace(current_target="A/item"), sv, "roadmap", c(0)) == "target")
check("a lookahead session: target", W.round_kind(SimpleNamespace(current_target="", lookahead_session=(1, 2)), sv, "roadmap", c(0)) == "target")
check("an author outside the list: outside", W.round_kind(SimpleNamespace(current_target=""), sv, "roadmap", c(0)) == "outside")
check("a fix of a PR serving the list: target", W.round_kind(None, sv, "fix", c(11)) == "target")
check("a fix of another PR of ours: outside", W.round_kind(None, sv, "fix-ci", c(12)) == "outside")
check("a review of our list PR: target; of our other PR: outside; of theirs: shared",
      (W.round_kind(None, sv, "review", c(11)), W.round_kind(None, sv, "review", c(12)), W.round_kind(None, sv, "review", c(13)))
      == ("target", "outside", "shared"))
check("a bump, a report, the curator: shared",
      {W.round_kind(None, sv, s, c(0)) for s in ("bump", "progress", "curate", "decide")} == {"shared"})

# ---- what the agent reported spending ---------------------------------------------------------------------------
r = AgentTranscriptRenderer("claude")
line = r.render_line(json.dumps({"type": "result", "subtype": "success", "num_turns": 3, "duration_ms": 1000,
                                 "total_cost_usd": 1.25, "usage": {"input_tokens": 10, "output_tokens": 5}}))
check("Claude's result: its cost is kept and shown", r.cost_usd == 1.25 and r.tokens == {"input_tokens": 10, "output_tokens": 5}
      and "cost=$1.25" in line, line)
x = AgentTranscriptRenderer("codex")
for n in (100, 50):
    x.render_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": n, "output_tokens": 1}}))
check("Codex's turns: their tokens are summed", x.tokens == {"input_tokens": 150, "output_tokens": 2} and x.cost_usd is None)

state = TMP / "state"
state.mkdir()
agents.ROUND_SPEND.update(cost_usd=2.5, tokens={"input_tokens": 7}, provider="claude")
W.record_round_spend(SimpleNamespace(cfg=SimpleNamespace(state=state), current_target=""), sv, "roadmap", c(0), time.time() - 60, 0)
rec = json.loads((state / W.ROUNDS_LOG).read_text().splitlines()[-1])
check("a round's spend is one line: its kind, provider and cost", rec["kind"] == "outside" and rec["provider"] == "claude"
      and rec["cost_usd"] == 2.5 and rec["stage"] == "roadmap" and rec["ended_at"] - rec["started_at"] >= 59, str(rec))

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

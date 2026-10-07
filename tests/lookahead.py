#!/usr/bin/env python3
"""Lookahead authoring (tauceti_worker/lookahead.py, handoff 2026-10-04): a blocked target-list item is
proved against stubs of its in-flight supplier on `lookahead/<area>/<slug>` of the fork, and ported
when the supplier lands. Offline checks:
  * the LOOKAHEAD.md header and the port marker parse, and a sloppy split plan reads as one split;
  * candidates: open items whose unmet needs may all be stubbed (in flight or eligible, settled, and
    themselves unblocked: never a stub of a stub), most depended-on first; `lookahead: no` on the item
    or a supplier, a decline, a recent failure or a complete branch excludes; a partial branch resumes;
  * port plans: a split is ready once the splits it needs have merged, and an in-flight item is offered
    for porting only while every open PR carrying its marker is one of its port PRs (owner's ruling:
    no waiting for merges between independent splits);
  * `_pick_target`: lookahead first when nothing on the list can be taken (owner's ruling), no cap on
    live branches, the port plan handed to do_roadmap, and without lookahead the hold and its expiry;
  * a session's outcome is its branch: moved = pushed, unmoved = a `failed` incident and a pause;
  * the port section names the split, the marker and whether the target marker is partial;
  * the curator's sweep deletes the branch of a done item and reports the rest;
  * list_branches / delete_branch against a local bare repository through the gate.
Exit 0 = all hold; 1 = a mismatch."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))
from harness import gate_env, scrub_env  # noqa: E402

scrub_env()
for var in ("TAUCETI_LOOKAHEAD", "TAUCETI_LOOKAHEAD_HOLD_HOURS",
            "TAUCETI_LOOKAHEAD_BRANCH", "TAUCETI_TARGET_ONLY", "TAUCETI_ROADMAP_TARGETS"):
    os.environ.pop(var, None)
TMP = Path(tempfile.mkdtemp(prefix="lookahead-"))
gate_env(TMP)

from tauceti_worker import interaction  # noqa: E402
from tauceti_worker import lookahead as L  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402
from tauceti_worker.survey import PRInfo  # noqa: E402
from tauceti_worker.targets import overlay_live, parse_targets, render_item  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


def header(area, slug, status="complete", splits=None, suppliers=("sup",), main="abc123"):
    d = {"area": area, "slug": slug, "main": main, "status": status, "suppliers": list(suppliers),
         "splits": splits if splits is not None else [{"n": 1, "after": []}]}
    return f"<!--tauceti-lookahead:v1 {json.dumps(d)}-->\n\n# Lookahead\n"


# ---- the header and the marker ------------------------------------------------------------------------
h = L.parse_header(header("A", "x", splits=[{"n": 1, "after": []}, {"n": 2, "after": []}, {"n": 3, "after": [1, 2], "title": "t"}]))
check("a header parses: status, suppliers, splits", h is not None and h.complete and h.suppliers == ("sup",)
      and [s.n for s in h.splits] == [1, 2, 3] and h.splits[2].after == (1, 2) and h.last_split == 3)
bad = L.parse_header(header("A", "x", splits=[{"n": 1, "after": [7]}]))
check("a split needing an unknown split reads as one split", bad is not None and [s.n for s in bad.splits] == [1])
check("a duplicate split number reads as one split",
      [s.n for s in L.parse_header(header("A", "x", splits=[{"n": 1}, {"n": 1}])).splits] == [1])
check("an unknown status reads as partial", not L.parse_header(header("A", "x", status="done?")).complete)
check("no header, no Header", L.parse_header("# just prose") is None and L.parse_header("<!--tauceti-lookahead:v1 {nope}-->") is None)
m = L.port_marker("lookahead/A/x", 2)
check("the port marker round-trips", L.port_markers(f"body\n{m}\n") == (("lookahead/A/x", 2),))
check("parse_branch takes refs and rejects other branches", L.parse_branch("refs/heads/lookahead/A/x") == ("A", "x")
      and L.parse_branch("roadmap/x") is None and L.parse_branch("lookahead/A") is None)
check("camel", L.camel("linear-equiv-prod-free-of-stable") == "LinearEquivProdFreeOfStable")
check("not used reason", L.not_used_reason("…\nLookahead: not used — the supplier changed route") == "the supplier changed route"
      and L.not_used_reason("Lookahead: ported split 1") is None)
pr = PRInfo.from_json({"number": 5, "body": f"x {m}"})
check("the survey reads port markers off open PRs", pr.lookahead_ports == (("lookahead/A/x", 2),))

# ---- candidates --------------------------------------------------------------------------------------
TEXT = """# t
<!-- tauceti-targets:v1 -->

## Sup
- [~] `flying` — L1, "In flight." (serves: B1; needs: none; in flight: #10)
- [ ] `ready` — L1, "Eligible." (serves: B1; needs: none)
- [ ] `blocked-sup` — L2, "Waits." (serves: B1; needs: `flying`)
- [ ] `unsettled-sup` — L1, "Route open." (serves: B1; needs: none; lookahead: no — route unsettled)
- [x] `base` — L0, "Done." (serves: B1; needs: none; landed: #1)

## Item
- [ ] `on-flying` — L3, "Needs the in-flight one." (serves: B1; needs: `flying`, `base`)
- [ ] `on-ready` — L3, "Needs the eligible one." (serves: B1; needs: `ready`)
- [ ] `on-stub` — L4, "Needs a blocked one." (serves: B1; needs: `blocked-sup`)
- [ ] `on-unsettled` — L3, "Needs an unsettled one." (serves: B1; needs: `unsettled-sup`)
- [ ] `itself-unsettled` — L3, "Unsettled." (serves: B1; needs: `flying`; lookahead: no — see #512)
- [ ] `eligible` — L3, "Nothing unmet." (serves: B1; needs: `base`)
- [ ] `top` — L5, "Needs on-ready." (serves: B1; needs: `on-ready`)
- [ ] `top2` — L6, "Needs top." (serves: B1; needs: `top`)
"""
live = parse_targets(TEXT)
slugs = [c.item.slug for c in L.candidates(live)]
check("candidates: only items whose unmet needs may all be stubbed (never a stub of a stub)",
      set(slugs) == {"on-flying", "on-ready", "blocked-sup"}, str(slugs))
check("candidates: the most depended-on first", slugs == ["on-ready", "blocked-sup", "on-flying"], str(slugs))
check("the stubs are the unmet needs only", [s.slug for s in L.candidates(live)[2].stubs] == ["flying"])
shown = render_item(live, live.find("itself-unsettled"))
check("a `lookahead:` clause is not shown to an agent", "lookahead:" not in shown and "#512" not in shown, shown)
check("declined and failed items are skipped",
      [c.item.slug for c in L.candidates(live, declined={"on-ready"}, failed={"on-flying"})] == ["blocked-sup"])
check("--roadmap-only pins the area", [c.item.slug for c in L.candidates(live, only="Sup")] == ["blocked-sup"]
      and len(L.candidates(live, only="Item")) == 2)
check("--roadmap-skip drops the area", [c.item.slug for c in L.candidates(live, skip=["Item"])] == ["blocked-sup"])
ch = L.parse_header(header("Item", "on-ready"))
ph = L.parse_header(header("Item", "on-flying", status="partial"))
got = L.candidates(live, only="Item", branches={("Item", "on-ready"): "c1", ("Item", "on-flying"): "p1"}, headers={"c1": ch, "p1": ph})
check("a complete branch is done; a partial one resumes", [(c.item.slug, c.resume) for c in got] == [("on-flying", "p1")])
check("a branch whose header is unreadable is left alone",
      [c.item.slug for c in L.candidates(live, only="Item", branches={("Item", "on-flying"): "zz"}, headers={})] == ["on-ready"])

# ---- port plans --------------------------------------------------------------------------------------
H3 = L.parse_header(header("Item", "on-flying", splits=[{"n": 1, "after": []}, {"n": 2, "after": []}, {"n": 3, "after": [1, 2]}],
                          suppliers=("flying",)))
B = "lookahead/Item/on-flying"
plan = L.port_plan(B, H3, {}, {})
check("nothing ported: the independent splits are ready, the last waits", [s.n for s in plan.ready] == [1, 2])
plan = L.port_plan(B, H3, {(B, 1): 501}, {})
check("split 1 open: split 2 is ready without waiting for its merge", [s.n for s in plan.ready] == [2] and plan.next.n == 2)
plan = L.port_plan(B, H3, {(B, 2): 502}, {(B, 1): 501})
check("the last split waits for every other merge", plan.ready == [])
plan = L.port_plan(B, H3, {}, {(B, 1): 501, (B, 2): 502, ("lookahead/Other/y", 3): 9})
check("all others merged: the last split is ready; other branches' markers are ignored", [s.n for s in plan.ready] == [3])
check("not spent while a split is unmerged", not L.spent(plan))
check("spent once every split has merged", L.spent(L.port_plan(B, H3, {}, {(B, 1): 501, (B, 2): 502, (B, 3): 503})))
check("a partial branch is never spent", not L.spent(L.port_plan(B, L.parse_header(header("Item", "on-flying", status="partial")),
                                                                 {}, {(B, 1): 501})))

LIVE2 = parse_targets(TEXT.replace("- [~] `flying` — L1, \"In flight.\" (serves: B1; needs: none; in flight: #10)",
                                   "- [x] `flying` — L1, \"Landed.\" (serves: B1; needs: none; landed: #10)"))
LIVE2 = overlay_live(LIVE2, {("Item", "on-flying")}, set())  # an open PR carries its marker
plans = {("Item", "on-flying"): L.port_plan(B, H3, {(B, 1): 501}, {})}
check("an in-flight item whose open PRs are its port PRs is offered for its next split",
      [it.slug for _a, it in L.port_ready(LIVE2, plans, {("Item", "on-flying"): {501}})] == ["on-flying"])
check("not while another PR carries its marker",
      L.port_ready(LIVE2, plans, {("Item", "on-flying"): {501, 777}}) == [])
check("not while its supplier is still in flight",
      L.port_ready(overlay_live(live, {("Item", "on-flying")}, set()), plans, {("Item", "on-flying"): {501}}) == [])
FREE = L.parse_header(header("Item", "on-flying", suppliers=()))
free_plans = {("Item", "on-flying"): L.port_plan(B, FREE, {}, {})}
check("a complete branch that stubs nothing is ported now, its supplier still in flight",
      [it.slug for _a, it in L.port_ready(live, free_plans, {})] == ["on-flying"] and FREE.stub_free)
PART = L.parse_header(header("Item", "on-flying", status="partial", suppliers=()))
check("…but not a partial one", L.port_ready(live, {("Item", "on-flying"): L.port_plan(B, PART, {}, {})}, {}) == [])
text = W._port_section(SimpleNamespace(), live, "Item", live.find("on-flying"), free_plans[("Item", "on-flying")],
                       SimpleNamespace(fork="alice/TauCeti"))
check("its port section says it stubs nothing and asks for no stub swap", "stubs nothing" in text
      and "replace every import" not in text and L.port_marker(B, 1) in text, text)
assigned = W._render_assigned(live, live.find("on-flying"))
check("and the assigned line does not claim the supplier landed", "`flying` — not complete" in assigned
      and "`base` — all landed" in assigned, assigned)

# ---- _pick_target -------------------------------------------------------------------------------------
INCIDENTS = TMP / "incidents"
interaction.incidents_dir = lambda: INCIDENTS
W._live_target_view = lambda t, path, sv, gh: (t, 0, 0)
W._merged_marker_prs.clear()


class Claims:
    def __init__(self, held=()):
        self.held, self.keys = set(held), []

    def begin_target_work(self, area, slug):
        self.keys.append(f"author/{area}/{slug}")
        return 1 if f"author/{area}/{slug}" in self.held else 0

    def begin_global_work(self, key):
        self.keys.append(key)
        return 1 if key in self.held else 0


def view_for(t, branches=None, headers=None, open_ports=None, merged_ports=None, open_item_prs=None):
    branches, headers = branches or {}, headers or {}
    plans = {k: L.port_plan(L.branch_name(*k), headers[sha], open_ports or {}, merged_ports or {})
             for k, sha in branches.items() if sha in headers}
    spent = {k for k, plan in plans.items() if L.spent(plan)}
    plans = {k: plan for k, plan in plans.items() if k not in spent}
    return W._LookaheadView("alice/TauCeti", branches, headers, plans,
                            L.port_ready(t, plans, open_item_prs or {}), open_item_prs or {}, spent)


def worker(claims=None, wid="gqw-c1"):
    return SimpleNamespace(claims=claims or Claims(), gh=object(), cfg=SimpleNamespace(wid=wid),
                           lookahead_port=None, lookahead_session=None)


sv = SimpleNamespace(_mine_open_prs=[object()] * 3, open_prs=[])
BLOCKED = parse_targets(TEXT.replace("- [ ] `ready` —", "- [~] `ready` —").replace("- [ ] `eligible` —", "- [x] `eligible` —")
                        .replace("- [ ] `unsettled-sup` —", "- [~] `unsettled-sup` —"))
os.environ["TAUCETI_LOOKAHEAD"] = "1"
W._lookahead_view = lambda w, sv, live: view_for(live)
w = worker()
got = W._pick_target(w, sv, BLOCKED, Path("t.md"), "auto", [])
check("nothing eligible: a lookahead session before authoring outside the list",
      got is not None and w.lookahead_session is not None and w.lookahead_session[0].item.slug == "on-ready"
      and w.claims.keys[-1] == "lookahead/Item/on-ready", f"{got} {w.claims.keys}")
w = worker(Claims(held={"lookahead/Item/on-ready"}))
W._pick_target(w, sv, BLOCKED, Path("t.md"), "auto", [])
check("a candidate another worker holds is passed over", w.lookahead_session[0].item.slug == "blocked-sup")
W._lookahead_view = lambda w, sv, live: view_for(live, {("Sup", f"x{i}"): f"s{i}" for i in range(6)},
                                                 {f"s{i}": L.parse_header(header("Sup", f"x{i}")) for i in range(6)})
w = worker()
W._pick_target(w, sv, BLOCKED, Path("t.md"), "auto", [])
check("no cap on live branches: six live, a session still starts", w.lookahead_session is not None
      and w.lookahead_session[0].item.slug == "on-ready", str(w.lookahead_session))

# an eligible item with a branch: ported (lookahead on), held (off)
ELIG = parse_targets(TEXT.replace("- [ ] `ready` —", "- [~] `ready` —").replace("- [ ] `unsettled-sup` —", "- [~] `unsettled-sup` —"))
fresh = L.parse_header(header("Item", "eligible"))
fresh.built_at = time.time() - 3600
W._lookahead_view = lambda w, sv, live: view_for(live, {("Item", "eligible"): "e1"}, {"e1": fresh})
w = worker()
got = W._pick_target(w, sv, ELIG, Path("t.md"), "auto", [])
check("an eligible item with a branch is picked with its port plan", got is not None and got[2].slug == "eligible"
      and w.lookahead_port is not None and w.lookahead_port[0].next.n == 1)

# an in-flight item with a ready split is offered before eligible items
plans_view = lambda w, sv, live: view_for(  # noqa: E731
    live, {("Item", "on-flying"): "f3"}, {"f3": H3}, open_ports={(B, 1): 501}, open_item_prs={("Item", "on-flying"): {501}})
FLYING_DONE = overlay_live(parse_targets(TEXT.replace("- [~] `flying`", "- [x] `flying`")), {("Item", "on-flying")}, set())
W._lookahead_view = plans_view
w = worker()
got = W._pick_target(w, sv, FLYING_DONE, Path("t.md"), "auto", [])
check("an in-flight item with a ready split comes first, with its plan",
      got is not None and got[2].slug == "on-flying" and w.lookahead_port[0].next.n == 2, str(got and got[2].slug))

# an operator's one-off round names its item, and gets nothing else
W._lookahead_view = lambda w, sv, live: view_for(live)
os.environ["TAUCETI_TARGET_ONLY"] = "on-flying"
w = worker()
W._pick_target(w, sv, BLOCKED, Path("t.md"), "auto", [])
check("TAUCETI_TARGET_ONLY: a session on exactly that item", w.lookahead_session is not None
      and w.lookahead_session[0].item.slug == "on-flying", str(w.lookahead_session))
os.environ["TAUCETI_TARGET_ONLY"] = "top2"
try:
    W._pick_target(worker(), sv, BLOCKED, Path("t.md"), "auto", [])
    check("TAUCETI_TARGET_ONLY: an item with nothing to do is no progress, not other work", False)
except NoProgress as e:
    check("TAUCETI_TARGET_ONLY: an item with nothing to do is no progress, not other work", "top2" in str(e), str(e))
os.environ["TAUCETI_TARGET_ONLY"] = "eligible"
W._lookahead_view = lambda w, sv, live: view_for(live, {("Item", "eligible"): "e1"}, {"e1": fresh})
w = worker()
got = W._pick_target(w, sv, ELIG, Path("t.md"), "auto", [])
check("TAUCETI_TARGET_ONLY: an eligible item with a branch is ported", got is not None and got[2].slug == "eligible"
      and w.lookahead_port is not None)
os.environ.pop("TAUCETI_TARGET_ONLY")

os.environ["TAUCETI_LOOKAHEAD"] = "0"  # a fleet without lookahead: it holds, it does not port or prove
W._lookahead_view = lambda w, sv, live: view_for(live, {("Item", "eligible"): "e1"}, {"e1": fresh})
w = worker()
got = W._pick_target(w, sv, ELIG, Path("t.md"), "auto", [])
held = json.loads((INCIDENTS / "lookahead-held-Item-eligible.json").read_text())
check("lookahead off: an item whose fresh branch awaits its port is held, and the attention list says so",
      got is None and held["what"] == "held" and w.lookahead_port is None, str(got))
check("lookahead off: no session either", w.lookahead_session is None)
W._lookahead_view = plans_view
w = worker()
got = W._pick_target(w, sv, FLYING_DONE, Path("t.md"), "auto", [])
check("lookahead off: no port offer for an in-flight item", got is not None and got[2].slug != "on-flying"
      and w.lookahead_port is None, str(got and got[2].slug))
os.environ["TAUCETI_LOOKAHEAD_HOLD_HOURS"] = "0.0001"
time.sleep(1)
W._lookahead_view = lambda w, sv, live: view_for(live, {("Item", "eligible"): "e1"}, {"e1": fresh})
w = worker()
got = W._pick_target(w, sv, ELIG, Path("t.md"), "auto", [])
check("after the hold the item is authored fresh, and that is recorded",
      got is not None and got[2].slug == "eligible" and (INCIDENTS / "lookahead-skipped-Item-eligible.json").is_file())
os.environ.pop("TAUCETI_LOOKAHEAD_HOLD_HOURS")
old = L.parse_header(header("Item", "eligible"))
old.built_at = time.time() - 8 * 86400
for p in INCIDENTS.glob("lookahead-*.json"):
    p.unlink()
W._lookahead_view = lambda w, sv, live: view_for(live, {("Item", "eligible"): "e1"}, {"e1": old})
got = W._pick_target(worker(), sv, ELIG, Path("t.md"), "auto", [])
check("a stale branch holds nothing", got is not None and got[2].slug == "eligible"
      and not (INCIDENTS / "lookahead-held-Item-eligible.json").exists())

# ---- a session's outcome ------------------------------------------------------------------------------
LOGS = TMP / "logs"
LOGS.mkdir()
(LOGS / "agent-claude-1.log").write_text("[assistant]\nThe supplier's statement is unclear.\nLookahead: no branch — unclear\n")
cand = L.candidates(live)[0]
w = SimpleNamespace(cfg=SimpleNamespace(logdir=LOGS), lookahead_session=(cand, view_for(live)), lookahead_started=time.time())
L.list_branches = lambda fork: {("Item", "on-ready"): "new1"}
L.read_header = lambda fork, sha, run=None: L.parse_header(header("Item", "on-ready"))
check("a moved branch is a pushed session", W._lookahead_outcome(w, 0) == 0)
hist = [json.loads(x) for x in (TMP / "gate" / "lookahead" / "history.jsonl").read_text().splitlines()]
check("and a history line records it", hist[-1]["event"] == "session" and hist[-1]["outcome"] == "pushed"
      and hist[-1]["status"] == "complete")
L.list_branches = lambda fork: {}
try:
    W._lookahead_outcome(w, 0)
    check("an unmoved branch is no progress", False)
except NoProgress as e:
    rec = json.loads((INCIDENTS / "lookahead-failed-Item-on-ready.json").read_text())
    check("an unmoved branch: a paused no-progress and a `failed` incident with the agent's words",
          e.declined and "unclear" in rec["detail"] and L.failed_recently("Item", "on-ready"))
check("a failed round keeps its own rc", W._lookahead_outcome(w, 3) == 3)

# ---- the port section ---------------------------------------------------------------------------------
W._pr_added_declarations = lambda w, pr, limit=40: ["TauCeti.foo_bar"]
W._merged_marker_prs[:] = [{"number": 900, "body": '<!--tauceti-target:v1 {"focus":"Sup","id":"flying"}-->'}]
plan = L.port_plan(B, H3, {}, {})
text = W._port_section(SimpleNamespace(), FLYING_DONE, "Item", FLYING_DONE.find("on-flying"), plan, view_for(FLYING_DONE))
check("the port section names the branch, split, marker, the partial flag and the landed declarations",
      B in text and "Port split 1" in text and L.port_marker(B, 1) in text and '"partial":true' in text
      and "#900" in text and "`TauCeti.foo_bar`" in text, text)
plan = L.port_plan(B, H3, {}, {(B, 1): 501, (B, 2): 502})
text = W._port_section(SimpleNamespace(), FLYING_DONE, "Item", FLYING_DONE.find("on-flying"), plan, view_for(FLYING_DONE))
check("the last split completes the target: no partial flag", "Port split 3" in text and "last split" in text)
plan = L.port_plan(B, L.parse_header(header("Item", "on-flying")), {}, {(B, 1): 501})
text = W._port_section(SimpleNamespace(), FLYING_DONE, "Item", FLYING_DONE.find("on-flying"), plan, view_for(FLYING_DONE))
check("a fully ported plan says so", "fully ported" in text)
W._merged_marker_prs.clear()

# ---- the curator's sweep -------------------------------------------------------------------------------
deleted = []
L.delete_branch = lambda fork, area, slug, sha: deleted.append((area, slug)) or True
SWEEP = parse_targets(TEXT.replace("- [ ] `on-ready` —", "- [x] `on-ready` —").replace("- [ ] `on-flying` —", "- [x] `on-flying` —"))
tfile = TMP / "sweep.md"
tfile.write_text(TEXT)
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)
W.load_targets = lambda path: SWEEP
stale_h = L.parse_header(header("Item", "on-stub"))
stale_h.built_at = time.time() - 9 * 86400
BO = "lookahead/Item/on-flying"
W._lookahead_view = lambda w, sv, live, fresh=False: view_for(
    live, {("Item", "on-ready"): "r1", ("Item", "on-flying"): "f1", ("Gone", "away"): "g1", ("Item", "on-stub"): "s9"},
    {"r1": L.parse_header(header("Item", "on-ready")), "f1": L.parse_header(header("Item", "on-flying")),
     "g1": L.parse_header(header("Gone", "away")), "s9": stale_h},
    merged_ports={(BO, 1): 777})
for p in INCIDENTS.glob("lookahead-*.json"):
    p.unlink()
W._lookahead_sweep(SimpleNamespace(gh=object()), None)
check("the sweep deletes the branches of done items",
      sorted(deleted) == [("Item", "on-flying"), ("Item", "on-ready")], str(deleted))
check("a done item ported by its PRs is not an incident; one landed without them is",
      not (INCIDENTS / "lookahead-abandoned-Item-on-flying.json").exists()
      and (INCIDENTS / "lookahead-abandoned-Item-on-ready.json").exists())
check("a branch whose item is not on the list is listed for the owner, never deleted",
      ("Gone", "away") not in deleted and (INCIDENTS / "lookahead-orphan-Gone-away.json").exists())
check("a stale branch of an open item is listed, not deleted",
      ("Item", "on-stub") not in deleted and (INCIDENTS / "lookahead-stale-Item-on-stub.json").exists())
deleted.clear()
W._lookahead_view = lambda w, sv, live, fresh=False: view_for(live, {("Item", "on-flying"): "f1"}, {"f1": L.parse_header(header("Item", "on-flying"))},
                                                             open_ports={(BO, 1): 778})
W._lookahead_sweep(SimpleNamespace(gh=object()), None)
check("a branch whose port PR is still open is kept", deleted == [])
W.load_targets = lambda path: parse_targets(TEXT)  # on-stub still open
SB = "lookahead/Item/on-stub"
W._lookahead_view = lambda w, sv, live, fresh=False: view_for(live, {("Item", "on-stub"): "z1"}, {"z1": L.parse_header(header("Item", "on-stub"))},
                                                             merged_ports={(SB, 1): 900})
W._lookahead_sweep(SimpleNamespace(gh=object()), None)
check("a spent branch is deleted though its item is still open", deleted == [("Item", "on-stub")], str(deleted))
check("…and is offered to no port round", "on-stub" not in str(view_for(parse_targets(TEXT), {("Item", "on-stub"): "z1"},
      {"z1": L.parse_header(header("Item", "on-stub"))}, merged_ports={(SB, 1): 900}).plans))

# ---- unset, nothing reaches GitHub (2026-10-04: a test's sweep deleted a real branch) ---------------------
real_view, real_sweep_view = W._lookahead_view, None
importlib_reloaded = __import__("importlib").reload(W)  # the real _lookahead_view, not this file's stand-ins
os.environ.pop("TAUCETI_LOOKAHEAD", None)


def boom(*_a, **_k):
    raise AssertionError("GitHub reached with TAUCETI_LOOKAHEAD unset")


W.ensure_fork = boom
L.list_branches = boom
L.delete_branch = boom
W.interaction = interaction
try:
    view = W._lookahead_view(worker(), sv, live)
    W._lookahead_sweep(SimpleNamespace(gh=object()), None)
    check("TAUCETI_LOOKAHEAD unset: no fork resolution, listing or deletion", view is None)
except AssertionError as e:
    check("TAUCETI_LOOKAHEAD unset: no fork resolution, listing or deletion", False, str(e))

# ---- the listing a fleet without lookahead reuses ------------------------------------------------------
L.write_snapshot("alice/TauCeti", {("Item", "x"): "t1"}, {})
check("a fresh listing is reused", L.recent_snapshot(60) == ("alice/TauCeti", {("Item", "x"): "t1"}))
check("an old one is not", L.recent_snapshot(-1) is None)

# ---- list_branches / delete_branch, for real, against a bare repository -------------------------------
import importlib  # noqa: E402

importlib.reload(L)  # the real list_branches / read_header / delete_branch
bare = TMP / "fork.git"
subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
work = TMP / "work"
subprocess.run(["git", "init", "-q", str(work)], check=True)
ident = ["-c", "user.name=t", "-c", "user.email=t@x"]
subprocess.run(["git", "-C", str(work), *ident, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
for ref in ("lookahead/Item/on-ready", "lookahead/Item/on-flying", "roadmap/other"):
    subprocess.run(["git", "-C", str(work), "push", "-q", str(bare), f"HEAD:refs/heads/{ref}"], check=True)
tip = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{bare.as_uri()}.insteadOf",
                   "GIT_CONFIG_VALUE_0": "https://github.com/alice/TauCeti", "TAUCETI_FORK": "alice/TauCeti"})
got = L.list_branches("alice/TauCeti")
check("list_branches reads the lookahead branches only", got == {("Item", "on-ready"): tip, ("Item", "on-flying"): tip}, str(got))
check("delete_branch refuses a moved branch", not L.delete_branch("alice/TauCeti", "Item", "on-ready", "0" * 40))
check("delete_branch deletes, leased on the tip", L.delete_branch("alice/TauCeti", "Item", "on-ready", tip)
      and L.list_branches("alice/TauCeti") == {("Item", "on-flying"): tip})
events = [json.loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
check("both went through the gate", any(e.get("op") == "lookahead" and e.get("kind") == "git_push" for e in events)
      and any(e.get("op") == "lookahead" and e.get("kind") == "git_read" for e in events))
calls = []


def fake_gh(argv):
    calls.append(argv)
    if "/contents/" in argv[2]:
        import base64
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(header("Item", "on-flying").encode()).decode())
    return SimpleNamespace(returncode=0, stdout="2026-10-01T12:00:00Z\n")


hh = L.read_header("alice/TauCeti", tip, run=fake_gh)
check("read_header decodes LOOKAHEAD.md and the tip's date", hh is not None and hh.slug == "on-flying"
      and hh.built_at == 1790856000.0, str(hh and hh.built_at))
again = L.read_header("alice/TauCeti", tip, run=lambda argv: (_ for _ in ()).throw(AssertionError("no second read")))
check("a header is read once per tip", again is not None and again.built_at == hh.built_at and len(calls) == 2)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

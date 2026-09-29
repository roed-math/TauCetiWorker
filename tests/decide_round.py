#!/usr/bin/env python3
"""The decide stage end to end, offline. GitHub is a stub (PR states and comments), the model is a
stub that writes `decisions.json`, and the incident store is a temp directory.

Tier A: a decline on a merged PR and one on a PR that moved on are stale; an authoring decline with no
target is noted; one with a target is left to the curator; a PR under a hold label is left alone;
`wait` rulings are re-checked (a merged blocker and a prerequisite now in an open PR become `retry`
with a note, a blocker closed unmerged goes back for a new ruling, a blocker still open keeps waiting).
Tier B: each ruling kind is applied, and each invalid one (retry at a head already retried, wait on a
PR that is not open) becomes an escalation. A retry lifts the survey's decline suppression and hands
the fixer its note; a prerequisite lands at the top of its area in the target list. Exit 0 = all hold."""

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
from tauceti_worker.attention import decision_note_for, declined_at  # noqa: E402
from tauceti_worker.survey import decide_due  # noqa: E402
from tauceti_worker.targets import insert_items  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-decide-"))
INC = TMP / "incidents"
(INC / "acked").mkdir(parents=True)
interaction.incidents_dir = lambda: INC
targets = TMP / "targets.md"
targets.write_text("""# targets
<!-- tauceti-targets:v1 -->

## Area
- [x] `done-one` — L0, "Prove it." (serves: B1; needs: none; landed: #1)
- [ ] `pre-thing` — L1, "Prove the prerequisite." (serves: B1; needs: none)

## Gaps (no roadmap milestone yet)
- nothing here
""")
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(targets)
(TMP / "state" / "refs" / "roadmap" / "TauCetiRoadmap" / "Other").mkdir(parents=True)


def live(name, **rec):
    rec.setdefault("kind", "declined")
    rec.setdefault("first_at", "2026-09-29T00:00:00Z")
    rec.setdefault("summary", f"account for {name}")
    (INC / f"declined-{name}.json").write_text(json.dumps(rec))


def acked(name, **rec):
    rec.setdefault("kind", "declined")
    (INC / "acked" / f"declined-{name}.json").write_text(json.dumps(rec))


def read(name, folder=INC):
    p = folder / f"declined-{name}.json"
    return json.loads(p.read_text()) if p.is_file() else None


live("fix-101", stage="fix", pr=101, head="h101")
live("fix-102", stage="fix", pr=102, head="h102")
live(
    "roadmap-unknown", stage="roadmap", pr=0, summary="The Foo roadmap has no milestone left.\nDetails."
)  # as recorded: pr 0
live("roadmap-Area-pre-thing", stage="roadmap", target="Area/pre-thing")
live("fix-103", stage="fix", pr=103, head="h103")
live("fix-104", stage="fix", pr=104, head="h104")
live("fix-105", stage="fix", pr=105, head="h105")
acked("fix-105", stage="fix", pr=105, head="h105", decision="retry", decision_note="try the contest")
live("rebase-106", stage="rebase", pr=106, head="h106")
live("fix-107", stage="fix", pr=107, head="h107")
live("fix-108", stage="fix", pr=108, head="h108")
live("fix-109", stage="fix", pr=109, head="h109")
acked("fix-110", stage="fix", pr=110, head="h110", decision="wait", blocked_on_prs=[120], decision_note="waits for 120")
acked("fix-111", stage="fix", pr=111, head="h111", decision="wait", blocked_on_prs=[121])
acked("fix-112", stage="fix", pr=112, head="h112", decision="wait", blocked_on_targets=["pre-thing"])
acked("fix-113", stage="fix", pr=113, head="h113", decision="wait", blocked_on_prs=[132])


def pr(n, head=None, labels=(), targets_=()):
    return SimpleNamespace(
        number=n, head_oid=head or f"h{n}", labels=tuple(labels), title=f"PR {n}", target_ids=tuple(targets_)
    )


OPEN = [
    pr(102, head="h102-new"),
    pr(103),
    pr(104),
    pr(105),
    pr(106),
    pr(107, labels=("hold",)),
    pr(108),
    pr(109),
    pr(110),
    pr(111),
    pr(112),
    pr(113),
    pr(130),
    pr(131, targets_=(("Area", "pre-thing"),)),
    pr(132),
]
STATES = {101: "MERGED", 120: "MERGED", 121: "CLOSED"}
asked: dict = {}


class GH:
    def pr_view(self, n, fields):
        if "title" in fields:
            return {
                "title": f"PR {n}",
                "body": "This PR ...",
                "labels": [],
                "files": [{"path": "TauCeti/X.lean"}],
                "url": "",
            }
        return {"state": STATES.get(n, "OPEN"), "headRefOid": f"h{n}", "labels": []}

    def issue_comments(self, n):
        return [
            {
                "body": "<!--tauceti-scoreboard-->\n| scope | changes requested |\n<!--tauceti-meta:v1 {}-->",
                "user": {"login": "bot"},
            }
        ]

    def review_comments(self, n):
        return [{"id": 1, "body": "scope: land Layer 2 first", "user": {"login": "reviewer"}}]


def fake_agent(cwd, prompt, profile, logdir):
    cases = json.loads((Path(cwd) / "cases.json").read_text())
    asked["prs"] = sorted(c["pr"] for c in cases["cases"])
    asked["retry_allowed"] = {c["pr"]: c["retry_allowed"] for c in cases["cases"]}
    asked["scoreboard_has_meta"] = any("tauceti-meta" in c["scoreboard"] for c in cases["cases"])
    asked["prompt"] = prompt
    out = {
        "103": {
            "decision": "prerequisite",
            "note": "needs Layer 2 item 3",
            "items": [
                {
                    "area": "Area",
                    "slug": "eisenstein-converse",
                    "text": "Layer 2, prove the converse direction.",
                    "source": "Layer 2",
                    "needs": ["pre-thing"],
                },
                {
                    "area": "Other",
                    "slug": "other-stage-one",
                    "text": "Stage 4, item 1: the first piece.",
                    "source": "Stage 4",
                    "needs": [],
                },
            ],
        },
        "104": {"decision": "retry", "note": "the prerequisite is in open PR #130; contest citing it"},
        "105": {"decision": "retry", "note": "try again"},
        "106": {
            "decision": "roadmap",
            "note": "needs a split",
            "proposal": "Split Layer 3 item 5 into a topological part and a holomorphic part, because " * 2,
        },
        "108": {"decision": "wait", "note": "waits", "blocked_on_prs": [999]},
        "109": {
            "decision": "escalate",
            "note": "main has all of it",
            "recommend": "close: subsumed by #1000",
            "evidence": "TauCeti/A.lean:10",
        },
        "111": {"decision": "wait", "note": "now waits on #130", "blocked_on_prs": [130]},
    }
    (Path(cwd) / "decisions.json").write_text(json.dumps(out))
    return 0


commits = []
D.run_agent_host = fake_agent
D.DECIDE_MAX_CASES = 10  # seven cases below; the cap has its own check
D.fetch_ref = lambda repo, d: True
W._curate_main_checkout = lambda w: None
W._effective_authoring_profile = lambda opts: "claude"
W._commit_targets = lambda path, applied, prefix="curate": commits.append((prefix, applied))


class Counters:
    def __init__(self):
        self.d = {}

    def write(self, k, v):
        self.d[k] = v

    def read(self, k):
        return self.d.get(k, 0)


w = SimpleNamespace(
    cfg=SimpleNamespace(state=TMP / "state", logdir=TMP / "logs"),
    gh=GH(),
    claims=SimpleNamespace(begin_global_work=lambda k: 0, release=lambda: None),
    counters=Counters(),
)
check("the stage is due with declines pending", decide_due(w.counters)[0])
rc = D.do_decide(w, SimpleNamespace(open_prs=OPEN), None, SimpleNamespace(), False)
check("the round is productive", rc == 0, str(rc))
check("the attempt time is recorded", w.counters.read("decide-attempt-ts") > 0)

# tier A
check("a merged PR's decline is stale", (read("fix-101", INC / "acked") or {}).get("decision") == "stale")
check("a PR that moved on is stale", (read("fix-102", INC / "acked") or {}).get("decision") == "stale")
check(
    "an authoring decline without a target is noted",
    (read("roadmap-unknown", INC / "acked") or {}).get("decision") == "noted",
)
check(
    "an authoring decline with a target is left to the curator",
    "decision" not in (read("roadmap-Area-pre-thing") or {"decision": 1}),
)
check("a PR under a hold label is left for the owner", "decision" not in (read("fix-107") or {"decision": 1}))
check(
    "only open, same-head, unheld PRs reach the model",
    asked.get("prs") == [103, 104, 105, 106, 108, 109, 111],
    str(asked.get("prs")),
)
check("the scoreboard reaches the model without its meta block", asked.get("scoreboard_has_meta") is False)
check(
    "a head already retried once is marked not retryable",
    asked.get("retry_allowed", {}).get(105) is False and asked.get("retry_allowed", {}).get(104) is True,
    str(asked.get("retry_allowed")),
)
r110 = read("fix-110", INC / "acked") or {}
check(
    "a wait whose blocker merged becomes a retry with a note",
    r110.get("decision") == "retry" and "#120" in r110.get("decision_note", ""),
    str(r110)[:200],
)
r112 = read("fix-112", INC / "acked") or {}
check(
    "a wait on a target now in an open PR becomes a retry naming it",
    r112.get("decision") == "retry" and "#131" in r112.get("decision_note", ""),
    str(r112)[:200],
)
r111 = read("fix-111", INC / "acked") or {}
check(
    "a wait whose blocker closed unmerged is ruled again",
    r111.get("decision") == "wait"
    and r111.get("blocked_on_prs") == [130]
    and any(h.get("decision") == "wait" for h in r111.get("history", [])),
    str(r111)[:300],
)
check("a wait on a PR still open keeps waiting", (read("fix-113", INC / "acked") or {}).get("decision") == "wait")

# tier B
r103 = read("fix-103", INC / "acked") or {}
check(
    "a prerequisite ruling parks the PR on its items",
    r103.get("decision") == "wait" and r103.get("blocked_on_targets") == ["eisenstein-converse", "other-stage-one"],
    str(r103)[:300],
)
text = targets.read_text()
check(
    "a prerequisite goes to the top of its area",
    text.index("`eisenstein-converse`") < text.index("`done-one`")
    and "unblocks: #103" in text
    and "needs: `pre-thing`" in text,
)
check("a prerequisite in a new area gets a section before the Gaps", text.index("## Other") < text.index("## Gaps"))
check("the list change is committed as the decide stage's", commits and commits[0][0] == "decide", str(commits))
r104 = read("fix-104", INC / "acked") or {}
check("a retry is filed with its note", r104.get("decision") == "retry" and "#130" in r104.get("decision_note", ""))
check(
    "a retry lifts the survey's suppression",
    (104, "h104") not in declined_at("fix") and (109, "h109") in declined_at("fix"),
)
check(
    "the fixer is handed the retry's note",
    "#130" in decision_note_for(104, "h104") and decision_note_for(104, "other") == "",
)
r105 = read("fix-105") or {}
check(
    "a second retry at the same head is escalated, and stays on the owner's list",
    r105.get("decision") == "escalate" and any(h.get("decision") == "retry" for h in r105.get("history", [])),
    str(r105)[:300],
)
r106 = read("rebase-106") or {}
check(
    "a roadmap ruling stays on the owner's list with a drafted proposal",
    r106.get("decision") == "roadmap" and Path(r106.get("proposal", "/nonexistent")).is_file(),
)
r108 = read("fix-108") or {}
check(
    "a wait on a PR that is not open is escalated",
    r108.get("decision") == "escalate" and "no open PR" in r108.get("decision_note", ""),
)
r109 = read("fix-109") or {}
check(
    "a recommendation to close is the owner's, with its evidence",
    r109.get("decision") == "escalate" and r109.get("recommend", "").startswith("close") and r109.get("evidence"),
)
check(
    "the prompt limits closing to subsumed PRs",
    "You may close a PR only when it is subsumed" in asked.get("prompt", ""),
)

# nothing new: the next round has only the unchanged wait to look at, and says so
w.counters.d.clear()
asked.clear()
try:
    D.do_decide(w, SimpleNamespace(open_prs=OPEN), None, SimpleNamespace(), False)
    check("a round with nothing to rule on is no-progress", False)
except W.NoProgress:
    check("a round with nothing to rule on is no-progress", "prs" not in asked)

# the per-round cap: with more declines than it allows, the oldest go first and the rest wait a round
for n in (201, 202, 203):
    live(f"fix-{n}", stage="fix", pr=n, head=f"h{n}", first_at=f"2026-09-2{n - 200}T00:00:00Z")
D.DECIDE_MAX_CASES = 2
w.counters.d.clear()
asked.clear()
try:
    D.do_decide(w, SimpleNamespace(open_prs=OPEN + [pr(201), pr(202), pr(203)]), None, SimpleNamespace(), False)
except W.NoProgress:
    pass
check("the cap takes the oldest declines first", asked.get("prs") == [201, 202], str(asked.get("prs")))

# the list helper on its own
t2, ok = insert_items("# t\n<!-- tauceti-targets:v1 -->\n", "New", ["- [ ] `x-y-z` — text (needs: none)"])
check(
    "an area missing from a list without Gaps is appended",
    ok and t2.endswith("## New\n- [ ] `x-y-z` — text (needs: none)\n"),
    repr(t2),
)

# ---- closing: only the account's own subsumed PRs, on evidence the code checks, within a daily cap
import subprocess as sp  # noqa: E402

from tauceti_worker.constants import ROADMAP as ROADMAP_REPO  # noqa: E402

main = TMP / "main"
(main / "TauCeti").mkdir(parents=True)
(main / "TauCeti" / "Foo.lean").write_text("theorem Foo.bar : True := trivial\n")
for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main"]):
    sp.run(["git", "-C", str(main), *cmd], check=True)
W._curate_main_checkout = lambda w: main
os.environ["TAUCETI_EXPECT_LOGIN"] = "me"
os.environ["TAUCETI_DECIDE_CLOSE"] = "1"
D.DECIDE_MAX_CASES = 10
D.DECIDE_MAX_CLOSES_PER_DAY = 2
STATES.update({1100: "MERGED", 1101: "OPEN"})
AUTHORS = {304: "someone-else"}
writes = []
_view = GH.pr_view


def view(self, n, fields):
    d = _view(self, n, fields)
    if "author" in fields:
        d["author"] = {"login": AUTHORS.get(n, "me")}
    return d


def gh_write(self, args):
    writes.append(args)
    return sp.CompletedProcess(args, 0, stdout="", stderr="")


GH.pr_view, GH._gh = view, gh_write
COMMENT = "Everything this PR adds is on main now, through the merged PR named below; nothing is left to land."
ANSWERS = {
    "301": {"decision": "close", "note": "subsumed", "merged_prs": [1100], "comment": COMMENT},
    "302": {"decision": "close", "note": "subsumed", "on_main": ["Foo.bar"], "comment": COMMENT},
    "303": {"decision": "close", "note": "subsumed", "merged_prs": [1101], "comment": COMMENT},
    "304": {"decision": "close", "note": "subsumed", "merged_prs": [1100], "comment": COMMENT},
    "305": {"decision": "close", "note": "subsumed", "merged_prs": [1100], "comment": COMMENT},
    "306": {"decision": "close", "note": "subsumed", "merged_prs": [1100], "comment": COMMENT},
    "311": {"decision": "escalate", "note": "the roadmap change was declined; the owner decides"},
}


def agent(cwd, prompt, profile, logdir):
    cwd = Path(cwd)
    if (cwd / "request.json").is_file():  # roadmap-change: edit the README, describe the PR
        readme = cwd / "repo" / "TauCetiRoadmap" / "Area" / "README.md"
        readme.write_text(readme.read_text() + "\n5. Split: the topological part first.\n")
        if EXTRA_FILE:
            (cwd / "repo" / "stray.md").write_text("x")
        (cwd / "pr.json").write_text(
            json.dumps({"title": "Area: split item 5", "body": "This PR splits item 5 so that " * 6})
        )
        return 0
    cases = json.loads((cwd / "cases.json").read_text())
    asked["prs"] = sorted(c["pr"] for c in cases["cases"])
    (cwd / "decisions.json").write_text(json.dumps({str(c["pr"]): ANSWERS.get(str(c["pr"])) for c in cases["cases"]}))
    return 0


D.run_agent_host = agent
EXTRA_FILE = False
for i, n in enumerate((301, 302, 303, 304, 305)):
    live(f"fix-{n}", stage="fix", pr=n, head=f"h{n}", first_at=f"2026-09-29T01:0{i}:00Z")
acked("fix-310", stage="fix", pr=310, head="h310", decision="wait", blocked_on_roadmap_prs=[50])
acked("fix-311", stage="fix", pr=311, head="h311", decision="wait", blocked_on_roadmap_prs=[51])


class RoadmapGH:
    def pr_view(self, n, fields):
        return {"state": {50: "MERGED", 51: "CLOSED"}.get(n, "OPEN")}

    def api_jq(self, path, jq):
        return ROADMAP_REPO

    def _gh(self, args):
        writes.append(args)
        out = f"https://github.com/{ROADMAP_REPO}/pull/77\n" if args[:2] == ["pr", "create"] else ""
        return sp.CompletedProcess(args, 0, stdout=out, stderr="")


D.GitHub = lambda repo: RoadmapGH()
OPEN2 = OPEN + [pr(n) for n in (301, 302, 303, 304, 305, 306, 310, 311)]
w.counters.d.clear()
D.do_decide(w, SimpleNamespace(open_prs=OPEN2), None, SimpleNamespace(), False)
closes = [a for a in writes if a[:2] == ["pr", "close"]]
check(
    "a PR subsumed by a merged PR is closed with the evidence in its comment",
    (read("fix-301", INC / "acked") or {}).get("decision") == "closed"
    and any(a[2] == "301" and "#1100 (merged)" in a[-1] and "reopen it" in a[-1] for a in closes),
    str(closes)[:300],
)
check(
    "a PR whose declarations are on main is closed, citing where",
    (read("fix-302", INC / "acked") or {}).get("decision") == "closed"
    and any(a[2] == "302" and "TauCeti/Foo.lean:1" in a[-1] for a in closes),
)
check(
    "a close citing an unmerged PR is escalated", "has not merged" in (read("fix-303") or {}).get("decision_note", "")
)
check("a close of someone else's PR is escalated", "did not open" in (read("fix-304") or {}).get("decision_note", ""))
check(
    "the daily cap stops the third close",
    "cap" in (read("fix-305") or {}).get("decision_note", "") and len(closes) == 2,
    str(len(closes)),
)
check(
    "a closed PR records what subsumed it, for the curator",
    (read("fix-301", INC / "acked") or {}).get("subsumed_by") == [1100],
)
r310 = read("fix-310", INC / "acked") or {}
check(
    "a wait on a merged roadmap change becomes a retry citing it",
    r310.get("decision") == "retry" and f"{ROADMAP_REPO}/pull/50" in r310.get("decision_note", ""),
    str(r310)[:200],
)
r311 = read("fix-311") or {}
check(
    "a wait on a roadmap change closed unmerged is ruled again",
    r311.get("decision") == "escalate" and any(h.get("decision") == "wait" for h in r311.get("history", [])),
)
os.environ.pop("TAUCETI_DECIDE_CLOSE")
live("fix-306", stage="fix", pr=306, head="h306")
w.counters.d.clear()
D.do_decide(w, SimpleNamespace(open_prs=OPEN2), None, SimpleNamespace(), False)
check(
    "with closing off, a close ruling is escalated",
    "closing is off" in (read("fix-306") or {}).get("decision_note", ""),
)

# ---- filing a roadmap proposal: prepare shows the diff, open pushes to the fork and parks the PR
origin, fork = TMP / "rm-origin", TMP / "rm-fork.git"
for area in ("Area", "Other"):
    (origin / "TauCetiRoadmap" / area).mkdir(parents=True)
    (origin / "TauCetiRoadmap" / area / "README.md").write_text(f"# {area}\n\n4. First.\n")
    (origin / "TauCetiRoadmap" / area / "Suggested.lean").write_text("theorem first : True := sorry\n")
for cmd in (
    ["init", "-q", "-b", "main"],
    ["add", "."],
    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "r"],
):
    sp.run(["git", "-C", str(origin), *cmd], check=True)
sp.run(["git", "init", "-q", "--bare", str(fork)], check=True)
git_calls, builds = [], []
BUILD_OK = True
D.build_suggested = lambda repo, area: builds.append(area) or (BUILD_OK, "build output")


def fake_gated_git(argv, *, op, target, kind=None, **kw):
    git_calls.append((op, target, kind))
    argv = [
        a.replace("https://github.com/me/TauCetiRoadmap.git", str(fork)).replace(
            f"https://github.com/{ROADMAP_REPO}", str(origin)
        )
        for a in argv
    ]
    kw.pop("capture_output", None)
    return sp.run(argv, capture_output=True, text=True, **kw)


D.gate_mod.gated_git = fake_gated_git
EDITS = {"README": True, "SUGGESTED": False, "OTHER_AREA": False}


def roadmap_agent(cwd, prompt, profile, logdir):
    repo = Path(cwd) / "repo" / "TauCetiRoadmap"
    if EDITS["README"]:
        (repo / "Area" / "README.md").write_text(
            (repo / "Area" / "README.md").read_text() + "\n5. Split: the topological part first.\n"
        )
    if EDITS["SUGGESTED"]:
        (repo / "Area" / "Suggested.lean").write_text(
            (repo / "Area" / "Suggested.lean").read_text() + "theorem split : True := sorry\n"
        )
    if EDITS["OTHER_AREA"]:
        (repo / "Other" / "README.md").write_text("changed\n")
    if EXTRA_FILE:
        (Path(cwd) / "repo" / "stray.md").write_text("x")
    (Path(cwd) / "pr.json").write_text(
        json.dumps({"title": "Area: split item 5", "body": "This PR splits item 5 so that " * 6})
    )
    return 0


def dispatch(cwd, prompt, profile, logdir):
    return (
        roadmap_agent(cwd, prompt, profile, logdir)
        if (Path(cwd) / "request.json").is_file()
        else agent(cwd, prompt, profile, logdir)
    )


D.run_agent_host = dispatch
prop = TMP / "decisions" / "roadmap-320.md"
prop.parent.mkdir(parents=True, exist_ok=True)
prop.write_text("# Proposed roadmap change\n\nSplit Area item 5.\n")
live("fix-320", stage="fix", pr=320, head="h320", decision="roadmap", proposal=str(prop), decision_note="split it")
D.roadmap_change_prepare(320)
shown = D.roadmap_change_show(320)
check(
    "prepare commits the README edit on a branch and shows it",
    "Title: Area: split item 5" in shown and "+5. Split: the topological part first." in shown and not builds,
    shown[-300:],
)


def refused(name, **edits):
    EDITS.update(edits)
    try:
        D.roadmap_change_prepare(320)
        check(name, False)
    except D.Die:
        check(name, True)
    EDITS.update(README=True, SUGGESTED=False, OTHER_AREA=False)


EXTRA_FILE = True
refused("a change touching a file outside the roadmap is refused")
EXTRA_FILE = False
refused("a change touching two roadmaps is refused", OTHER_AREA=True)
BUILD_OK = False
refused("a Suggested.lean that does not build is refused", SUGGESTED=True)
BUILD_OK = True
EDITS["SUGGESTED"] = True
builds.clear()
D.roadmap_change_prepare(320)
shown = D.roadmap_change_show(320)
check(
    "a Suggested.lean change is built, then shown with the README change",
    builds == ["Area"]
    and "+theorem split" in shown
    and "Suggested.lean" in shown.split("--- diff", 1)[1].splitlines()[0],
)
EDITS["SUGGESTED"] = False
RoadmapGH.api_jq = lambda self, path, jq: None  # the gate refused the read, or it failed
try:
    D.roadmap_change_open(320)
    check("a fork that cannot be read is reported as unreadable, not as no fork", False)
except D.Die as e:
    check("a fork that cannot be read is reported as unreadable, not as no fork", "could not read" in str(e), str(e))
RoadmapGH.api_jq = lambda self, path, jq: ROADMAP_REPO
url = D.roadmap_change_open(320)
r320 = read("fix-320", INC / "acked") or {}
branches = sp.run(["git", "-C", str(fork), "branch", "--list"], capture_output=True, text=True).stdout
check(
    "open pushes the branch to the fork through the gate",
    "decide/tauceti-320" in branches and ("push", "https://github.com/me/TauCetiRoadmap", "git_push") in git_calls,
)
check(
    "open files the PR and labels it",
    any(a[:2] == ["pr", "create"] and "me:decide/tauceti-320" in a for a in writes)
    and any(a[:2] == ["pr", "edit"] and "awaiting-review" in a for a in writes),
)
check(
    "the TauCeti PR then waits on the roadmap PR",
    url.endswith("/pull/77") and r320.get("decision") == "wait" and r320.get("blocked_on_roadmap_prs") == [77],
    str(r320)[:200],
)
check(
    "one clone serves every proposal",
    (TMP / "decisions" / "roadmap-repo" / ".git").is_dir() and sum(1 for c in git_calls if c[0] == "clone") == 1,
)

sys.exit(1 if fails else 0)

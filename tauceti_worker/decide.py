"""Rule on declined rounds, so the fleet's attention list holds only what needs a human.

A fixer that cannot act on a PR ends its round without a change and records a `declined` incident
(attention.py): the only blocking finding is a scope ruling no code change can clear, main already
has the PR's content, a push raced another. Until 2026-09-29 every one of those waited for the owner,
who kept making the same few rulings: the PR has merged or moved on since (stale), try again (the
decline was transient), wait for the prerequisite, have the prerequisite written. Four of the eight
records on the list that day were about PRs that had already merged.

The decide stage makes those rulings. Two tiers, host-side like the curator:

  A. mechanical, no model. The PR merged, closed, or moved to a new head since the decline: the record
     is stale. An authoring decline that names no target is noted (one with a target is the
     curator's). A `wait` ruling whose blockers have all moved on becomes a `retry` with a note for the
     fixer saying what changed, or goes back for a new ruling if a blocker closed unmerged.
  B. model. Each remaining decline is put to the model with the PR, its scoreboard and review
     threads, the fixer's own account, the open PRs, the target list and the roadmap and rubric
     checkouts. It answers in `decisions.json`, one ruling per PR:
       retry         the decline was transient, or there is evidence the fixer did not have; a note
                     tells the next fixer what it is (once per head: a second decline at the same head
                     cannot be retried again)
       wait          blocked on named open PR(s) or listed target(s); looked at again when they move
       prerequisite  blocked on a roadmap item nobody is building: it is added to the target list, at
                     the top of its area, and the PR waits for it
       close         the PR is subsumed: every PR it names as subsuming it has merged, or every
                     declaration it names is on main. The code checks that evidence, that the PR is the
                     account's own and carries no hold label, and a daily cap, and only then closes it
                     with a comment giving the evidence (the owner allowed this on 2026-09-29; off unless
                     TAUCETI_DECIDE_CLOSE is 1). Anything short of that becomes an escalation.
       roadmap       only a roadmap change resolves it: a drafted proposal for the owner. TauCeti's
                     AGENTS.md forbids agents to open TauCetiRoadmap PRs or issues on their own, and
                     TauCetiRoadmap's CONTRIBUTING.md asks that nobody post a roadmap change they have
                     not read, so the owner files it: `roadmap-change prepare` has an agent apply the
                     proposal to the roadmap's README on a branch and shows the diff, and `roadmap-change
                     open`, run on the owner's say-so, opens the PR (see roadmap_change_main). The PR
                     then waits for that roadmap PR.
       escalate      anything else: the owner decides, with the analysis beside the fixer's account

The code checks each ruling against what it can verify (open PRs, listed targets, the roadmap
checkout, main) and turns an invalid one into an escalation. Its writes are the local incident
records, drafted proposals, the operator's target list, and a close of one of the account's own PRs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from . import gate as gate_mod
from . import interaction
from .agents import (
    TAUCETI_CACHE_ARTIFACT_URL,
    TAUCETI_CACHE_REVISION_URL,
    fetch_ref,
    resolve_authoring_profile,
    run_agent_host,
)
from .attention import (
    CLOSED,
    ESCALATE,
    NOTED,
    RETRY,
    ROADMAP,
    STALE,
    WAIT,
    decided_waits,
    record_decision,
    reopen_decision,
    undecided_declines,
)
from .config import Die, NoProgress, log, roadmap_targets
from .constants import DECIDE_MAX_CASES, DECIDE_MAX_CLOSES_PER_DAY, REVIEW, TAUCETI
from .constants import ROADMAP as ROADMAP_REPO
from .github import GitHub, me
from .paths import HERE
from .targets import _AREA_RE, insert_items, parse_targets

KEEP_LABELS = {"keep", "hold", "wip", "human", "do-not-close"}
PREREQUISITE = "prerequisite"
CLOSE = "close"
_SLUG_OK = re.compile(r"[a-z0-9][a-z0-9-]{2,80}")
_SCOREBOARD = "<!--tauceti-scoreboard-->"
_META_RE = re.compile(r"<!--tauceti-meta:v1 .*?-->", re.S)


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split()) if n <= 300 else str(s or "").strip()
    return s if len(s) <= n else s[: n - 2].rstrip() + " …"


def decisions_dir() -> Path:
    return interaction.incidents_dir().parent / "decisions"


def do_decide(w, sv, c, opts, bubble) -> int | None:
    """One decide round (see the module docstring). Global claim, like curate: one ruler at a time."""
    w.counters.write("decide-attempt-ts", int(time.time()))
    if w.claims.begin_global_work("decide") == 1:
        log("decide: another worker holds the decide claim — skipping (COOP dedup)")
        return None
    try:
        return _do_decide_inner(w, sv, opts)
    finally:
        w.claims.release()


def _pr_state(w, pr: int, open_by_no: dict) -> tuple[str, str, set[str]] | None:
    """(state, head, labels) of a PR: from the survey's open listing when it is there (no read), else one
    gated read. None when the read failed."""
    p = open_by_no.get(pr)
    if p is not None:
        return "OPEN", p.head_oid, {lb.lower() for lb in p.labels}
    d = w.gh.pr_view(pr, ["state", "headRefOid", "labels"])
    if not d or not d.get("state"):
        return None
    return (
        str(d["state"]),
        str(d.get("headRefOid") or ""),
        {str(lb.get("name", "")).lower() for lb in d.get("labels") or []},
    )


def _do_decide_inner(w, sv, opts) -> int | None:
    open_by_no = {p.number: p for p in sv.open_prs}
    tpath = roadmap_targets()
    targets = parse_targets(tpath.read_text()) if tpath is not None and tpath.is_file() else None
    ruled: list[str] = []
    # ---- tier A: rulings to wait whose blockers have moved
    for path, rec in decided_waits():
        line = _recheck_wait(w, path, rec, open_by_no, targets)
        if line:
            ruled.append(line)
    # ---- tier A: declines the facts have overtaken
    cases: list[tuple[Path, dict]] = []
    for path, rec in undecided_declines():
        pr = rec.get("pr")
        if not (isinstance(pr, int) and pr > 0):  # an authoring round records pr 0
            gist = _clip((rec.get("summary") or "").split("\n", 1)[0], 200)
            record_decision(path, NOTED, decision_note=f"an authoring round declined without a target: {gist}")
            ruled.append(f"{path.name}: noted (authoring decline without a target)")
            continue
        st = _pr_state(w, pr, open_by_no)
        if st is None:
            log(f"decide: #{pr}: could not read its state — left for the next round")
            continue
        state, head, labels = st
        if state != "OPEN":
            record_decision(path, STALE, decision_note=f"#{pr} is {state.lower()}")
            ruled.append(f"#{pr}: stale ({state.lower()})")
            continue
        if head and head != rec.get("head"):
            record_decision(path, STALE, decision_note=f"#{pr} moved to {head[:12]} since the decline")
            ruled.append(f"#{pr}: stale (new head {head[:12]})")
            continue
        if labels & KEEP_LABELS:
            log(f"decide: #{pr}: carries a hold label ({', '.join(sorted(labels & KEEP_LABELS))}) — the owner's")
            continue
        cases.append((path, rec))
    if len(cases) > DECIDE_MAX_CASES:
        log(f"decide: {len(cases)} declines to rule on; taking the oldest {DECIDE_MAX_CASES} this round")
        cases = cases[:DECIDE_MAX_CASES]
    # ---- tier B: the model
    if cases:
        ruled += _rule_with_model(w, sv, opts, cases, open_by_no, tpath, targets)
    for line in ruled:
        log(f"decide: {line}")
    if not ruled:
        raise NoProgress("decide: no ruling this round (nothing had moved, or every case failed validation)")
    return 0


# ---- tier A: waits ---------------------------------------------------------------------------------------


def _recheck_wait(w, path: Path, rec: dict, open_by_no: dict, targets) -> str:
    """Look again at a `wait` ruling. Returns a log line when it changed, "" when it still waits."""
    pr = rec["pr"]
    st = _pr_state(w, pr, open_by_no)
    if st is None:
        return ""
    state, head, _labels = st
    if state != "OPEN":
        record_decision(path, STALE, decision_note=f"#{pr} is {state.lower()} (it was waiting)")
        return f"#{pr}: stale ({state.lower()} while waiting)"
    if head and head != rec.get("head"):
        record_decision(path, STALE, decision_note=f"#{pr} moved to {head[:12]} while waiting")
        return f"#{pr}: stale (new head {head[:12]} while waiting)"
    moved: list[str] = []
    for n in rec.get("blocked_on_prs") or []:
        if n in open_by_no:
            return ""
        s = _pr_state(w, n, open_by_no)
        if s is None:
            return ""
        if s[0] != "MERGED":
            reopen_decision(path)
            return f"#{pr}: #{n}, which it waited on, closed unmerged — back for a new ruling"
        moved.append(f"#{n}, which this PR waited on, has merged")
    for slug in rec.get("blocked_on_targets") or []:
        carrier = next((p.number for p in open_by_no.values() if any(i == slug for _f, i in p.target_ids)), None)
        item = targets.find(slug) if targets is not None else None
        if carrier is not None:
            moved.append(f"the prerequisite `{slug}` is now in open PR #{carrier}")
        elif item is not None and item.status == "done":
            moved.append(f"the prerequisite `{slug}` has landed")
        else:
            return ""
    roadmap_merged = []
    for n in rec.get("blocked_on_roadmap_prs") or []:
        d = GitHub(ROADMAP_REPO).pr_view(n, ["state"])
        if not d or not d.get("state") or d["state"] == "OPEN":
            return ""
        if d["state"] != "MERGED":
            reopen_decision(path)
            return (
                f"#{pr}: {ROADMAP_REPO}#{n}, the roadmap change it waited on, closed unmerged — back for a new ruling"
            )
        roadmap_merged.append(n)
        moved.append(f"the roadmap change {ROADMAP_REPO}#{n} this PR waited on has merged")
    if not moved:
        return ""
    if roadmap_merged:
        note = (
            "What changed since this PR was parked: " + "; ".join(moved) + ". The roadmap now reads as that "
            "PR proposed. Reply on the blocking scope finding's thread citing "
            + ", ".join(f"https://github.com/{ROADMAP_REPO}/pull/{n}" for n in roadmap_merged)
            + " and quoting the new wording, so the rubric is re-run against the current roadmap; then address "
            "any other findings as usual."
        )
    else:
        note = (
            "What changed since this PR was parked: " + "; ".join(moved) + ". The scope rubric accepts a "
            "prerequisite stage that exists on main or in an open PR (TauCetiReview rubrics/scope.md, "
            "'confirm that stage exists on `main` or in an open PR'). If the blocking finding is about that "
            "prerequisite, reply on its thread with this evidence; otherwise address the findings as usual."
        )
    if rec.get("decision_note"):
        note += f" The earlier ruling said: {_clip(rec['decision_note'], 600)}"
    record_decision(path, RETRY, decision_note=note)
    return f"#{pr}: retry ({'; '.join(moved)})"


# ---- tier B: the model ------------------------------------------------------------------------------------


def _history(path: Path, rec: dict) -> list[dict]:
    """Earlier rulings on this PR: the record's own history, and the ruling filed under acked/ (a PR
    declined again after a ruling has a fresh live record beside its old acked one)."""
    acked = interaction.incidents_dir() / "acked" / path.name
    out = list(rec.get("history") or [])
    try:
        prior = json.loads(acked.read_text()) if acked.is_file() and acked != path else {}
    except (OSError, ValueError):
        prior = {}
    out += list(prior.get("history") or [])
    if prior.get("decision"):
        out.append({k: prior.get(k) for k in ("decision", "head", "decided_at", "decision_note") if prior.get(k)})
    return out


def _evidence(w, pr: int, rec: dict, history: list[dict]) -> dict:
    """What the model is shown about one declined PR. Bounded: a scoreboard, the newest thread replies."""
    d = w.gh.pr_view(pr, ["title", "body", "labels", "headRefOid", "files", "url", "author"]) or {}
    issue = w.gh.issue_comments(pr) or []
    boards = [c for c in issue if _SCOREBOARD in (c.get("body") or "")]
    scoreboard = _META_RE.sub("", boards[-1].get("body") or "") if boards else ""
    others = [c for c in issue if _SCOREBOARD not in (c.get("body") or "")][-8:]
    threads = (w.gh.review_comments(pr) or [])[-40:]
    head = rec.get("head") or ""
    return {
        "pr": pr,
        "url": d.get("url") or f"https://github.com/TauCetiProject/TauCeti/pull/{pr}",
        "title": d.get("title") or "",
        "author": (d.get("author") or {}).get("login") or "",
        "labels": [lb.get("name") for lb in d.get("labels") or []],
        "head": head,
        "body": _clip(d.get("body"), 4000),
        "files": [f.get("path") for f in (d.get("files") or [])][:80],
        "declined_stage": rec.get("stage"),
        "declined_reason": rec.get("reason"),
        "fixer_account": rec.get("summary") or "",
        "prs_the_fixer_named": list(rec.get("subsumed_by") or []) + list(rec.get("mentions") or []),
        "scoreboard": _clip(scoreboard, 8000),
        "issue_comments": [
            {"user": (c.get("user") or {}).get("login"), "at": c.get("created_at"), "body": _clip(c.get("body"), 1500)}
            for c in others
        ],
        "review_threads": [
            {
                "id": c.get("id"),
                "in_reply_to": c.get("in_reply_to_id"),
                "user": (c.get("user") or {}).get("login"),
                "at": c.get("created_at"),
                "path": c.get("path"),
                "body": _clip(c.get("body"), 1500),
            }
            for c in threads
        ],
        "earlier_rulings": history,
        "retry_allowed": not any(h.get("decision") == RETRY and h.get("head") == head for h in history),
    }


def _rule_with_model(w, sv, opts, cases, open_by_no, tpath, targets) -> list[str]:
    from .work_units import _curate_main_checkout, _effective_authoring_profile

    refs = w.cfg.state / "refs"
    roadmap_ok = fetch_ref(ROADMAP_REPO, refs / "roadmap")
    review_ok = fetch_ref(REVIEW, refs / "review")
    clone = _curate_main_checkout(w)
    work = w.cfg.state / "decide" / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    by_pr: dict[int, tuple[Path, dict, dict]] = {}
    for path, rec in cases:
        ev = _evidence(w, rec["pr"], rec, _history(path, rec))
        by_pr[rec["pr"]] = (path, rec, ev)
    (work / "cases.json").write_text(
        json.dumps(
            {
                "cases": [ev for _p, _r, ev in by_pr.values()],
                "roadmap_checkout": str(refs / "roadmap" / "TauCetiRoadmap") if roadmap_ok else "",
                "rubrics_checkout": str(refs / "review" / "rubrics") if review_ok else "",
                "main_checkout": str(clone) if clone else "",
                "target_list": str(tpath) if tpath else "",
                "open_prs": [
                    {
                        "number": p.number,
                        "title": p.title,
                        "labels": list(p.labels),
                        "targets": [f"{f}/{i}" for f, i in p.target_ids],
                    }
                    for p in sv.open_prs
                ],
            },
            indent=1,
        )
    )
    prompt = (HERE / "prompts" / "decide.md").read_text()
    log(f"decide: asking the model about {len(by_pr)} decline(s): " + ", ".join(f"#{n}" for n in by_pr))
    rc = run_agent_host(work, prompt, _effective_authoring_profile(opts), w.cfg.logdir)
    answers: dict = {}
    vf = work / "decisions.json"
    if rc == 0 and vf.is_file():
        try:
            answers = json.loads(vf.read_text())
        except ValueError:
            log("decide: the model's decisions.json is not valid JSON — every case escalates")
    if not isinstance(answers, dict):
        answers = {}
    out: list[str] = []
    text = tpath.read_text() if tpath is not None and tpath.is_file() else ""
    added: list[str] = []
    ctx = SimpleNamespace(
        w=w,
        open_by_no=open_by_no,
        targets=targets,
        refs=refs,
        clone=clone,
        login=os.environ.get("TAUCETI_EXPECT_LOGIN", "").strip() or me(),
    )
    for pr, (path, rec, ev) in by_pr.items():
        v = answers.get(str(pr))
        line, text, new = _apply(path, rec, ev, v if isinstance(v, dict) else None, ctx, text)
        added += new
        out.append(line)
    if added and tpath is not None:
        from .work_units import _commit_targets

        tpath.write_text(text)
        _commit_targets(
            tpath, [f"`{s}`: added as a prerequisite a blocked PR waits on" for s in added], prefix="decide"
        )
    return out


def _escalate(path: Path, why: str, v: dict | None = None) -> str:
    fields = {"decision_note": _clip(why, 1500)}
    if v:
        if v.get("recommend"):
            fields["recommend"] = _clip(v.get("recommend"), 300)
        if v.get("evidence"):
            fields["evidence"] = _clip(v.get("evidence"), 1500)
    record_decision(path, ESCALATE, **fields)
    return f"{path.name}: escalated to the owner — {_clip(why, 160)}"


def _apply(path, rec, ev, v, ctx, text) -> tuple[str, str, list[str]]:
    """Check one ruling and act on it. Returns (log line, the target list text, slugs added to it)."""
    open_by_no, targets, refs = ctx.open_by_no, ctx.targets, ctx.refs
    pr = rec["pr"]
    if v is None:
        return _escalate(path, "the decide stage gave no ruling for this PR"), text, []
    kind = v.get("decision")
    note = _clip(v.get("note"), 1500)
    if kind == RETRY:
        if not ev["retry_allowed"]:
            return _escalate(path, f"asked to retry at a head already retried once: {note}", v), text, []
        if not note:
            return _escalate(path, "a retry ruling must say what the next fixer should do differently", v), text, []
        record_decision(path, RETRY, decision_note=note)
        return f"#{pr}: retry — {_clip(note, 160)}", text, []
    if kind == WAIT:
        prs = [n for n in v.get("blocked_on_prs") or [] if isinstance(n, int) and n in open_by_no and n != pr]
        slugs = [
            s
            for s in v.get("blocked_on_targets") or []
            if isinstance(s, str)
            and targets is not None
            and (it := targets.find(s)) is not None
            and it.status != "done"
        ]
        if not (prs or slugs):
            return _escalate(path, f"a wait ruling named no open PR and no open listed target: {note}", v), text, []
        record_decision(path, WAIT, blocked_on_prs=prs, blocked_on_targets=slugs, decision_note=note)
        return f"#{pr}: wait on {', '.join([f'#{n}' for n in prs] + [f'`{s}`' for s in slugs])}", text, []
    if kind == PREREQUISITE:
        lines, slugs, errors = [], [], []
        for item in v.get("items") or []:
            err, area, line, slug = _check_item(item, pr, targets, refs, slugs)
            if err:
                errors.append(err)
            elif line:
                lines.append((area, line))
                slugs.append(slug)
            else:
                slugs.append(slug)  # already listed: wait on it
        if errors or not slugs:
            return (
                _escalate(path, f"prerequisite ruling rejected ({'; '.join(errors) or 'no items'}): {note}", v),
                text,
                [],
            )
        added = []
        for area, line in lines:
            text, ok = insert_items(text, area, [line])
            if ok:
                added.append(re.search(r"`([^`]+)`", line).group(1))
        record_decision(
            path, WAIT, blocked_on_prs=[], blocked_on_targets=slugs, added_targets=added, decision_note=note
        )
        return (
            f"#{pr}: waits on prerequisite(s) {', '.join(f'`{s}`' for s in slugs)} ({len(added)} added to the list)",
            text,
            added,
        )
    if kind == ROADMAP:
        proposal = str(v.get("proposal") or "").strip()
        if len(proposal) < 80:
            return _escalate(path, f"a roadmap ruling came without a usable proposal: {note}", v), text, []
        d = decisions_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"roadmap-{pr}.md"
        f.write_text(f"# Proposed roadmap change for PR #{pr}\n\n{proposal[:12000]}\n")
        record_decision(path, ROADMAP, decision_note=note, proposal=str(f))
        return f"#{pr}: needs a roadmap change — proposal drafted at {f}", text, []
    if kind == CLOSE:
        return _close(path, rec, ev, v, ctx, note), text, []
    if kind == ESCALATE:
        return _escalate(path, note or "the decide stage could not settle it", v), text, []
    return _escalate(path, f"the decide stage gave an unknown ruling {kind!r}", v), text, []


def _close(path: Path, rec: dict, ev: dict, v: dict, ctx, note: str) -> str:
    """Close a PR the ruling says is subsumed, once the code has checked everything it can. Any check
    that fails turns the ruling into an escalation carrying the same recommendation, so the owner sees
    exactly what the stage wanted to do and why it did not."""
    pr = rec["pr"]
    ask = {**v, "recommend": v.get("recommend") or "close"}
    if os.environ.get("TAUCETI_DECIDE_CLOSE", "").strip() != "1":
        return _escalate(path, f"recommends closing; closing is off for this fleet (decide.close): {note}", ask)
    if not ctx.login or ev.get("author") != ctx.login:
        return _escalate(
            path, f"recommends closing a PR this account did not open ({ev.get('author') or '?'}): {note}", ask
        )
    labels = {str(lb).lower() for lb in ev.get("labels") or []}
    if labels & KEEP_LABELS:
        return _escalate(path, f"recommends closing a PR under a hold label: {note}", ask)
    comment = str(v.get("comment") or "").strip()
    if len(comment) < 40:
        return _escalate(path, f"a close ruling must carry the comment to post: {note}", ask)
    merged = [n for n in v.get("merged_prs") or [] if isinstance(n, int) and n != pr]
    decls = [str(s) for s in v.get("on_main") or [] if isinstance(s, str) and s.strip()]
    if not (merged or decls):
        return _escalate(path, f"a close ruling must name merged PRs or declarations on main: {note}", ask)
    found: list[str] = []
    for n in merged:
        d = ctx.w.gh.pr_view(n, ["state"])
        if not d or d.get("state") != "MERGED":
            return _escalate(path, f"recommends closing as subsumed by #{n}, which has not merged: {note}", ask)
        found.append(f"#{n} (merged)")
    if decls:
        from .work_units import _grep_declarations

        if ctx.clone is None:
            return _escalate(path, f"recommends closing, but main could not be checked out to verify it: {note}", ask)
        for name in decls:
            hits = _grep_declarations(ctx.clone, name)
            if not hits:
                return _escalate(path, f"recommends closing, but `{name}` is not declared on main: {note}", ask)
            found.append(f"`{name}` ({':'.join(hits[0].split(':', 2)[:2])})")  # a hit is `path:line:text`
    day = time.strftime("%Y%m%d", time.gmtime())
    done_today = int(ctx.w.counters.read(f"decide-closes-{day}") or 0)
    if done_today >= DECIDE_MAX_CLOSES_PER_DAY:
        return _escalate(
            path, f"recommends closing; today's cap of {DECIDE_MAX_CLOSES_PER_DAY} closes is spent: {note}", ask
        )
    body = (
        f"{comment[:1500]}\n\nEvidence checked before closing: {', '.join(found)}.\n\n"
        f"Closed by the fleet's decide stage (an AI agent acting for @{ctx.login}'s owner), after a fixer found "
        "nothing left to do on this PR. If this is wrong, reopen it."
    )
    p = ctx.w.gh._gh(["pr", "close", str(pr), "--repo", TAUCETI, "--comment", body])
    if p.returncode != 0:
        why = ((p.stderr or "") + (p.stdout or "")).strip()[-200:]
        return _escalate(path, f"recommends closing, but the close failed ({why}): {note}", ask)
    ctx.w.counters.write(f"decide-closes-{day}", done_today + 1)
    record_decision(path, CLOSED, decision_note=note or comment[:300], subsumed_by=merged, close_evidence=found)
    return f"#{pr}: closed — {', '.join(found)}"


def _check_item(item, pr: int, targets, refs: Path, taken: list[str]) -> tuple[str, str, str, str]:
    """Validate a prerequisite item. Returns (error, area, line to add or "" if already listed, slug)."""
    if not isinstance(item, dict):
        return "an item is not an object", "", "", ""
    area, slug = str(item.get("area") or ""), str(item.get("slug") or "")
    text, source = _clip(item.get("text"), 900), _clip(item.get("source"), 200)
    if not _AREA_RE.fullmatch(area):
        return f"area {area!r} is not a roadmap directory name", "", "", ""
    if not ((targets is not None and area in targets.areas) or (refs / "roadmap" / "TauCetiRoadmap" / area).is_dir()):
        return f"no roadmap `{area}`", "", "", ""
    if not _SLUG_OK.fullmatch(slug) or slug in taken:
        return f"slug {slug!r} is not a fresh kebab-case slug", "", "", ""
    existing = targets.find(slug) if targets is not None else None
    if existing is not None:
        return (
            ("", area, "", slug) if existing.status != "done" else (f"`{slug}` is already done in the list", "", "", "")
        )
    if len(text) < 20 or not source:
        return f"`{slug}` needs a milestone text (a roadmap quotation) and its source", "", "", ""
    needs = [s for s in item.get("needs") or [] if isinstance(s, str) and _SLUG_OK.fullmatch(s)]
    needs_s = ", ".join(f"`{s}`" for s in needs) or "none"
    line = f"- [ ] `{slug}` — {text} (unblocks: #{pr}; needs: {needs_s}; source: {source}; added by the decide stage)"
    return "", area, line, slug


# ---- filing a roadmap proposal, on the owner's say-so ----------------------------------------------------
#
# `tauceti roadmap-change prepare PR` has an agent apply a `roadmap` ruling's proposal to that roadmap's
# README, and to its Suggested.lean when the change adds, splits or restates a milestone the file
# prototypes, on a branch of the fleet's TauCetiRoadmap clone; a changed Suggested.lean is then built
# the way TauCetiRoadmap's CI builds it. The diff is printed. `tauceti roadmap-change open PR` pushes the
# branch to the account's TauCetiRoadmap fork and opens the pull request, then parks the TauCeti PR on
# it (a `wait` whose blocker is the roadmap PR; see _recheck_wait). The two steps are separate so the
# owner reads the change before it is posted, as TauCetiRoadmap's CONTRIBUTING.md asks; the fleet's
# `tauceti-fleet attention --file-roadmap PR` runs both with a confirmation between them.
#
# One clone serves every proposal, each on its own branch, so its `.lake` (Mathlib and the Tau Ceti
# dependency, several GB) is fetched once rather than per proposal. Callers hold the fleet's periodic
# lock, so two preparations never share it at once.

_ROADMAP_FILE_RE = re.compile(r"TauCetiRoadmap/([A-Za-z0-9_-]+)/(README\.md|Suggested\.lean)")


def _roadmap_record(pr: int) -> tuple[Path, dict]:
    """The live `roadmap` ruling on PR `pr`."""
    for path in sorted(interaction.incidents_dir().glob(f"declined-*-{pr}.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("pr") == pr and rec.get("decision") == ROADMAP and rec.get("proposal"):
            return path, rec
    raise Die(f"#{pr}: no roadmap ruling awaits filing (`tauceti-fleet attention` lists them)")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Die(f"git {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def roadmap_repo() -> Path:
    return decisions_dir() / "roadmap-repo"


def _fresh_branch(branch: str) -> Path:
    """The shared TauCetiRoadmap clone, on `branch` reset to upstream main, its tree clean but its
    `.lake` kept. Cloned on first use."""
    repo = roadmap_repo()
    url = f"https://github.com/{ROADMAP_REPO}"
    if not (repo / ".git").is_dir():
        shutil.rmtree(repo, ignore_errors=True)
        p = gate_mod.gated_git(
            ["git", "clone", "-q", "--filter=blob:none", url, str(repo)],
            op="clone",
            target=ROADMAP_REPO,
            capture_output=True,
        )
    else:
        p = gate_mod.gated_git(
            ["git", "-C", str(repo), "fetch", "-q", url, "+refs/heads/main:refs/remotes/origin/main"],
            op="fetch",
            target=ROADMAP_REPO,
            capture_output=True,
        )
    if p.returncode != 0:
        raise Die(f"{ROADMAP_REPO}: {(p.stderr or p.stdout).strip()[-300:]}")
    _git(repo, "checkout", "-q", "-f", "-B", branch, "origin/main")
    _git(repo, "clean", "-fdq", "-e", ".lake")
    return repo


def _build_env(repo: Path) -> dict[str, str]:
    """TauCetiRoadmap CI's cache setup: Tau Ceti's public Lake artifact service, read-only."""
    cfg = repo / ".lake" / "tauceti-lake-cache.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        'cache.defaultService = "tauceti-public"\n[[cache.service]]\nname = "tauceti-public"\nkind = "s3"\n'
        f'artifactEndpoint = "{TAUCETI_CACHE_ARTIFACT_URL}"\nrevisionEndpoint = "{TAUCETI_CACHE_REVISION_URL}"\n'
    )
    return {**os.environ, "LAKE_CONFIG": str(cfg), "LAKE_CACHE_DIR": str(repo / ".lake" / "cache")}


def build_suggested(repo: Path, area: str) -> tuple[bool, str]:
    """Build one roadmap's Suggested.lean as TauCetiRoadmap's CI does: Mathlib's cache, Tau Ceti's cache
    map (artifacts fetched lazily), then `lake build` of that module. (ok, the tail of the output)."""
    env = _build_env(repo)
    steps = [
        ["lake", "exe", "cache", "get"],
        [
            "lake",
            "cache",
            "get",
            "--package=TauCeti",
            "--service=tauceti-public",
            "--repo=TauCetiProject/TauCeti",
            "--mappings-only",
            "--max-revs=20",
        ],
        ["lake", "build", f"TauCetiRoadmap.{area}.Suggested"],
    ]
    out = ""
    for i, argv in enumerate(steps):
        log(f"roadmap-change: {' '.join(argv)}")
        p = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=3 * 3600)
        out = ((p.stdout or "") + (p.stderr or ""))[-4000:]
        if p.returncode != 0 and i != 1:  # a cache-map miss only means building Tau Ceti modules from source
            return False, out
    return True, out


def roadmap_change_prepare(pr: int) -> Path:
    """Apply the proposal on a branch of the shared clone, build a changed Suggested.lean, commit;
    returns the proposal's work directory (request, agent logs, pr.json, state.json)."""
    path, rec = _roadmap_record(pr)
    proposal = Path(rec["proposal"]).read_text()
    work = decisions_dir() / f"roadmap-{pr}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    branch = f"decide/tauceti-{pr}"
    repo = _fresh_branch(branch)
    (work / "repo").symlink_to(repo, target_is_directory=True)
    (work / "request.json").write_text(
        json.dumps(
            {
                "tauceti_pr": pr,
                "tauceti_pr_url": f"https://github.com/{TAUCETI}/pull/{pr}",
                "proposal": proposal,
                "ruling": rec.get("decision_note") or "",
                "fixer_account": rec.get("summary") or "",
                "checkout": "repo",
            },
            indent=1,
        )
    )
    profile = resolve_authoring_profile("claude")
    # The agent builds a Suggested.lean it changes with the same cache setup the check below uses.
    os.environ.update({k: v for k, v in _build_env(repo).items() if k.startswith("LAKE_")})
    log(f"roadmap-change: #{pr}: asking {profile.model or 'the model'} to apply the proposal in {repo}")
    rc = run_agent_host(work, (HERE / "prompts" / "roadmap-change.md").read_text(), profile, work / "logs")
    if rc != 0:
        raise Die(f"roadmap-change: the agent exited {rc}; see {work / 'logs'}")
    # `.lake` holds the build (and our cache config); the upstream repository ignores it anyway.
    changed = [
        ln[3:]
        for ln in _git(repo, "status", "--porcelain", "--untracked-files=all", "--", ".", ":!.lake").splitlines()
        if ln.strip()
    ]
    matched = [_ROADMAP_FILE_RE.fullmatch(c) for c in changed]
    areas = {m.group(1) for m in matched if m}
    if not changed or not all(matched) or len(areas) != 1:
        raise Die(
            f"roadmap-change: the change may edit only one roadmap's README.md and Suggested.lean; it touched {changed}"
        )
    area = areas.pop()
    try:
        meta = json.loads((work / "pr.json").read_text())
        title, body = str(meta["title"]).strip(), str(meta["body"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        raise Die("roadmap-change: the agent wrote no usable pr.json (title and body)") from None
    if not (5 <= len(title) <= 100) or len(body) < 100:
        raise Die("roadmap-change: pr.json's title or body is too short (or the title too long)")
    if any(m.group(2) == "Suggested.lean" for m in matched):
        ok, tail = build_suggested(repo, area)
        (work / "build.log").write_text(tail)
        if not ok:
            raise Die(
                f"roadmap-change: TauCetiRoadmap.{area}.Suggested does not build; see {work / 'build.log'}:\n{tail[-1500:]}"
            )
    name = _git(repo, "config", "user.name", check=False).strip() or os.environ.get("TAUCETI_EXPECT_LOGIN", "") or me()
    email = _git(repo, "config", "user.email", check=False).strip() or f"{name}@users.noreply.github.com"
    msg = f"{title}\n\nFor {TAUCETI}#{pr}.\n\nCo-Authored-By: {profile.model or 'Claude'} <noreply@anthropic.com>\n"
    _git(repo, "add", "-A", "--", f"TauCetiRoadmap/{area}")
    _git(repo, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", msg)
    (work / "state.json").write_text(
        json.dumps(
            {
                "pr": pr,
                "branch": branch,
                "area": area,
                "title": title,
                "body": body,
                "files": sorted(changed),
                "head": _git(repo, "rev-parse", "HEAD").strip(),
                "model": profile.model or "",
                "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=1,
        )
    )
    return work


def _state(pr: int) -> dict:
    try:
        return json.loads((decisions_dir() / f"roadmap-{pr}" / "state.json").read_text())
    except (OSError, ValueError):
        raise Die(f"#{pr}: nothing prepared (run `roadmap-change prepare {pr}` first)") from None


def roadmap_change_show(pr: int) -> str:
    """The prepared change as the owner should read it: title, description, and the diff."""
    st = _state(pr)
    diff = _git(roadmap_repo(), "show", "--format=", st["head"])
    return f"Title: {st['title']}\n\n{st['body']}\n\n--- diff ({', '.join(st['files'])}) ---\n{diff}"


def roadmap_change_open(pr: int) -> str:
    """Push the prepared branch to the account's TauCetiRoadmap fork and open the PR; park the TauCeti
    PR on it. Returns the new PR's URL."""
    path, rec = _roadmap_record(pr)
    st = _state(pr)
    repo = roadmap_repo()
    ref = f"refs/heads/{st['branch']}"
    if _git(repo, "rev-parse", ref, check=False).strip() != st["head"]:
        raise Die(f"#{pr}: the prepared branch changed since it was shown; prepare it again")
    login = os.environ.get("TAUCETI_EXPECT_LOGIN", "").strip() or me()
    fork = os.environ.get("TAUCETI_ROADMAP_FORK", "").strip() or f"{login}/TauCetiRoadmap"
    parent = GitHub(fork).api_jq(f"repos/{fork}", ".parent.full_name")
    if parent is None:
        raise Die(f"could not read {fork} to check it is a fork of {ROADMAP_REPO} (see the gate's refusal above)")
    if parent.strip() != ROADMAP_REPO:
        raise Die(
            f"{fork} is not a fork of {ROADMAP_REPO} (it reports parent {parent.strip() or 'none'}); create one with "
            f"`gh repo fork {ROADMAP_REPO} --clone=false`, or name yours in TAUCETI_ROADMAP_FORK"
        )
    url = f"https://github.com/{fork}"
    os.environ["TAUCETI_PUSH_REMOTE"] = url  # the gate's push allowlist: this fork, op `push`, this process only
    helper = f"!{HERE / 'scripts' / 'tauceti-gate'} credential"
    p = gate_mod.gated_git(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={helper}",
            "push",
            "-q",
            f"{url}.git",
            f"{ref}:{ref}",
        ],
        op="push",
        target=url,
        kind=gate_mod.GIT_PUSH,
        capture_output=True,
    )
    if p.returncode != 0:
        raise Die(f"push to {fork}: {(p.stderr or p.stdout).strip()[-300:]}")
    gh = GitHub(ROADMAP_REPO)
    q = gh._gh(
        [
            "pr",
            "create",
            "--repo",
            ROADMAP_REPO,
            "--base",
            "main",
            "--head",
            f"{login}:{st['branch']}",
            "--title",
            st["title"],
            "--body",
            st["body"],
        ]
    )
    if q.returncode != 0:
        raise Die(f"gh pr create: {(q.stderr or q.stdout).strip()[-300:]}")
    pr_url = (q.stdout or "").strip().splitlines()[-1]
    m = re.search(r"/pull/(\d+)", pr_url)
    if not m:
        raise Die(f"gh pr create said {pr_url!r}; the PR may exist, check {ROADMAP_REPO}")
    number = int(m.group(1))
    lab = gh._gh(["pr", "edit", str(number), "--repo", ROADMAP_REPO, "--add-label", "awaiting-review"])
    if lab.returncode != 0:
        log(f"roadmap-change: could not label {pr_url} awaiting-review; ask on the PR or Zulip for it")
    record_decision(
        path,
        WAIT,
        blocked_on_prs=[],
        blocked_on_targets=[],
        blocked_on_roadmap_prs=[number],
        roadmap_pr=pr_url,
        decision_note=f"the roadmap change is filed as {pr_url}; this PR waits for it",
    )
    return pr_url


def roadmap_change_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="tauceti roadmap-change", description="File a decide-stage roadmap proposal as a TauCetiRoadmap PR."
    )
    ap.add_argument("action", choices=("prepare", "show", "open", "build"))
    ap.add_argument("target", help="the TauCeti PR the proposal unblocks (for `build`: a roadmap area)")
    a = ap.parse_args(argv)
    if a.action in ("prepare", "open"):
        # The identity gate a round runs at its start: confirm the account before any GitHub read or
        # write, and cache it, which is also what lets the gate admit a read of the account's own fork.
        from .identity import gate as identity_gate

        wid = os.environ.get("TAUCETI_WORKER_ID", "").strip() or "default"
        identity_gate(HERE / "state" / wid, wid, where=f"roadmap-change {a.action}")
    if a.action == "build":  # warm or check the shared clone's build of one roadmap, on upstream main
        ok, tail = build_suggested(_fresh_branch("decide/warm"), a.target)
        print(tail)
        return 0 if ok else 1
    pr = int(a.target)
    if a.action == "prepare":
        roadmap_change_prepare(pr)
        print(roadmap_change_show(pr))
    elif a.action == "show":
        print(roadmap_change_show(pr))
    else:
        print(roadmap_change_open(pr))
    return 0

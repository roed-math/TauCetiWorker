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
       roadmap       only a roadmap change resolves it: a drafted proposal for the owner to file, since
                     TauCeti's AGENTS.md forbids agents to open TauCetiRoadmap PRs or issues
       escalate      anything else, including a recommendation to close the PR: the owner decides,
                     with the analysis beside the fixer's account

The code checks each ruling against what it can verify (open PRs, listed targets, the roadmap
checkout) and turns an invalid one into an escalation. Nothing here writes to TauCeti: the stage's
only writes are the local incident records, drafted proposals, and the operator's target list.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import interaction
from .agents import fetch_ref, run_agent_host
from .attention import (
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
from .config import NoProgress, log, roadmap_targets
from .constants import DECIDE_MAX_CASES, REVIEW
from .constants import ROADMAP as ROADMAP_REPO
from .paths import HERE
from .targets import _AREA_RE, insert_items, parse_targets

KEEP_LABELS = {"keep", "hold", "wip", "human", "do-not-close"}
PREREQUISITE = "prerequisite"
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
        if not isinstance(pr, int):
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
    if not moved:
        return ""
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
    d = w.gh.pr_view(pr, ["title", "body", "labels", "headRefOid", "files", "url"]) or {}
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
    for pr, (path, rec, ev) in by_pr.items():
        v = answers.get(str(pr))
        line, text, new = _apply(path, rec, ev, v if isinstance(v, dict) else None, open_by_no, targets, text, refs)
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


def _apply(path, rec, ev, v, open_by_no, targets, text, refs) -> tuple[str, str, list[str]]:
    """Check one ruling and act on it. Returns (log line, the target list text, slugs added to it)."""
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
    if kind == ESCALATE:
        return _escalate(path, note or "the decide stage could not settle it", v), text, []
    return _escalate(path, f"the decide stage gave an unknown ruling {kind!r}", v), text, []


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

"""A round that ends with nothing landed must not be silent.

A maintenance round (fix, fix-ci, rebase) can finish cleanly and leave no mark on GitHub: the agent
found that main already subsumes the PR, that the branch is obsolete, that someone else pushed first,
or it simply declined. The worker treats that as no-progress, which is right for pacing, but the
agent's reasoning then lives only in a multi-megabyte transcript nobody opens. On 2026-09-20 a Codex
fixer concluded that PR #4994 was entirely subsumed by a merged upstream PR and should be closed, and
the only visible trace was a parked ledger line. The owner asked for a way to know.

So every nothing-landed round records a local incident of kind `declined` (under the gate's
`incidents/`, beside `contest-cap`) carrying the agent's final message, verbatim but bounded, a
`close_hint` when that message reads like "this PR should be closed", and the transcript path. The
fleet view lists these until someone rules on them. Nothing here contacts GitHub. The ruling is the
`decide` stage's (decide.py), which settles most declines itself and hands the owner only the ones
the project reserves for a human (a roadmap change) or that it cannot settle; the owner can still
acknowledge any record by hand.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import interaction
from .interaction import record_incident

DECLINED = "declined"
MAX_SUMMARY = 1500

# A transcript section header as `transcript.AgentTranscriptRenderer` writes it: `[assistant]`,
# `[tool shell]`, `[turn] completed; …`, `[result] success; …`. Anything else at column 0 is body.
_SECTION_RE = re.compile(r"^\[[a-z][a-z _-]*\](?: .*)?$")
_END_MARKERS = ("Session terminated, killing shell",)
# The rebase prompt asks a declining agent to end with `Subsumed-by: #NNNN [#MMMM …]` (or `unknown`).
_SUBSUMED_RE = re.compile(r"^\s*\**Subsumed-by:?\**\s*(.+?)\s*$", re.I | re.M)
_PR_REF_RE = re.compile(r"(?<![\w/])#(\d{2,6})\b")
# The agent said the PR itself is the problem, not this round: worth an owner's look at the PR.
_CLOSE_HINT_RE = re.compile(
    r"subsum|supersed|obsolete|already (?:merged|on main|in main|landed)|should be closed"
    r"|clos(?:e|ing) (?:this|the) PR|duplicate of|no longer (?:needed|applies|necessary)"
    r"|cannot reconcile|recommend closing",
    re.I,
)


def newest_agent_log(logdir: Path) -> Path | None:
    """The transcript of the agent that just ran: the newest `agent-*.log` under the worker's logdir."""
    try:
        logs = [p for p in logdir.glob("agent-*.log") if p.is_file()]
    except OSError:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def final_assistant_text(log_path: Path, limit: int = MAX_SUMMARY) -> str:
    """The last `[assistant]` section of a rendered transcript, bounded to `limit` characters (head
    and a marker, since the first lines carry the verdict). Empty when the transcript has none."""
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return ""
    last: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith(_END_MARKERS):
            break
        if _SECTION_RE.match(line):
            if current is not None and any(s.strip() for s in current):
                last = current
            current = [] if line.rstrip() == "[assistant]" else None
            continue
        if current is not None:
            current.append(line)
    if current is not None and any(s.strip() for s in current):
        last = current
    body = "\n".join(last).strip()
    if len(body) > limit:
        body = body[: limit - 2].rstrip() + " …"
    return body


def subsuming_prs(summary: str, target: int | None) -> tuple[list[int], list[int]]:
    """(declared, mentioned): the PRs a `Subsumed-by:` line names, and, failing that, every other PR
    number the message mentions (an agent that was not asked for the trailer still usually names the
    upstream PR in prose, as the #4994 decline did: "Upstream PR #6126 implements the same …")."""
    declared: list[int] = []
    m = _SUBSUMED_RE.search(summary or "")
    if m:
        declared = [int(n) for n in _PR_REF_RE.findall(m.group(1)) if int(n) != target]
    mentioned = [int(n) for n in _PR_REF_RE.findall(summary or "") if int(n) != target]
    return list(dict.fromkeys(declared)), [n for n in dict.fromkeys(mentioned) if n not in declared]


def record_declined_round(logdir: Path, *, stage: str, pr: int | None, head: str = "",
                          reason: str = "", target: str = "") -> Path | None:
    """Record the agent's own account of a nothing-landed round as a `declined` incident. Never raises:
    an incident is a report, and the round's outcome (no-progress) is already decided."""
    try:
        log_path = newest_agent_log(logdir)
        summary = final_assistant_text(log_path) if log_path else ""
        # An authoring round has no PR yet: key it by its target (`Area/slug`), so two declined targets
        # are two incidents and the picker can skip each (2026-09-23: every authoring decline shared
        # the key `roadmap-unknown`, and 215 rounds re-picked three targets main already had).
        key = f"{stage}-{pr}" if pr else (f"{stage}-{target.replace('/', '-')}" if target
                                           else f"{stage}-{(head or 'unknown')[:12]}")
        declared, mentioned = subsuming_prs(summary, pr)
        # A target the curator handed back and an author declined AGAIN is a real disagreement: carry
        # the hand-back count forward so the curator asks the owner instead of handing it back again.
        handed_back = 0
        if target:
            prior = interaction.incidents_dir() / "acked" / f"{DECLINED}-{key}.json"
            try:
                rec = json.loads(prior.read_text())
                if rec.get("curator") == "not-landed":
                    handed_back = int(rec.get("handed_back") or 0) + 1
            except (OSError, ValueError):
                pass
        return record_incident(
            DECLINED,
            key,
            stage=stage,
            pr=pr,
            head=head,
            reason=reason,
            summary=summary or "(the transcript has no final assistant message)",
            close_hint=bool(summary and (_CLOSE_HINT_RE.search(summary) or declared)),
            subsumed_by=declared,  # what the agent DECLARED (the prompt's trailer)
            mentions=mentioned,  # other PRs it named in prose; a lead, not a verdict
            transcript=str(log_path) if log_path else "",
            **({"target": target} if target else {}),
            **({"handed_back": handed_back} if handed_back else {}),
            publication=os.environ.get("TAUCETI_PUBLICATION_ID") or "",
        )
    except Exception:  # noqa: BLE001 - reporting must never turn into a second failure
        return None


def declined_at(stage: str) -> set[tuple[int, str]]:
    """(pr, head) pairs a `stage` round has already declined, acknowledged or not. A decline is a
    fact about that exact head: until the PR moves, every worker that picks it up again spends a
    round and a model turn to reach the same conclusion (2026-09-21: fix3 and then fix2 rebased
    #4928 three minutes apart, both declining). The survey suppresses these; a new head lifts it.
    Acknowledged incidents count too — the owner's ack means "seen", not "try again". A `retry`
    decision (the decide stage's, or the owner's) is the one thing that does mean "try again"."""
    out: set[tuple[int, str]] = set()
    d = interaction.incidents_dir()  # via the module, so a test can point it elsewhere
    for folder in (d, d / "acked"):
        try:
            paths = list(folder.glob(f"{DECLINED}-{stage}-*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            pr, head = rec.get("pr"), rec.get("head")
            if isinstance(pr, int) and isinstance(head, str) and head and rec.get("decision") != RETRY:
                out.add((pr, head))
    return out


# ---- decisions: what happens to a decline once someone (the decide stage, or the owner) has ruled ----

RETRY, WAIT, ROADMAP, ESCALATE, STALE, NOTED = "retry", "wait", "roadmap", "escalate", "stale", "noted"
# The decide stage closed the PR itself (the owner allowed it, 2026-09-29), on evidence its code checked.
CLOSED = "closed"
# Decisions that still need the owner: the record stays in the live folder, so the fleet's attention
# list keeps showing it, now with the decision's analysis or drafted proposal beside the agent's words.
OWNER_DECISIONS = (ROADMAP, ESCALATE)
_PR_STAGES = ("fix", "fix-ci", "rebase")
_HISTORY_KEYS = ("decision", "head", "decided_at", "decision_note")


def _read(path: Path) -> dict | None:
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write(path: Path, rec: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def undecided_declines() -> list[tuple[Path, dict]]:
    """Live declines nobody has ruled on yet: a PR stage's (fix, fix-ci, rebase) with no `decision`,
    and an authoring decline that names no target (one with a target is the curator's). Oldest first."""
    d = interaction.incidents_dir()
    out: list[tuple[Path, dict]] = []
    try:
        paths = sorted(d.glob(f"{DECLINED}-*.json"))
    except OSError:
        return out
    for path in paths:
        rec = _read(path)
        if rec is None or rec.get("decision"):
            continue
        stage = rec.get("stage")
        if (stage in _PR_STAGES and isinstance(rec.get("pr"), int)) or (stage == "roadmap" and not rec.get("target")):
            out.append((path, rec))
    return sorted(out, key=lambda pair: str(pair[1].get("first_at") or ""))


def decided_waits() -> list[tuple[Path, dict]]:
    """Declines decided `wait`: parked until a named PR or target moves, then looked at again."""
    d = interaction.incidents_dir() / "acked"
    out: list[tuple[Path, dict]] = []
    try:
        paths = sorted(d.glob(f"{DECLINED}-*.json"))
    except OSError:
        return out
    for path in paths:
        rec = _read(path)
        if rec is not None and rec.get("decision") == WAIT and isinstance(rec.get("pr"), int):
            out.append((path, rec))
    return out


def owner_rulings() -> list[tuple[Path, dict]]:
    """Declines ruled `roadmap` or `escalate`: still in the live folder, on the owner's list."""
    out: list[tuple[Path, dict]] = []
    try:
        paths = sorted(interaction.incidents_dir().glob(f"{DECLINED}-*.json"))
    except OSError:
        return out
    for path in paths:
        rec = _read(path)
        if rec is not None and rec.get("decision") in OWNER_DECISIONS and isinstance(rec.get("pr"), int):
            out.append((path, rec))
    return out


def record_decision(path: Path, decision: str, **fields) -> Path | None:
    """Rule on a decline: write `decision` and its fields into the record, keep the earlier rulings on
    the same PR in `history`, and file it — under `acked/` unless the decision still needs the owner.
    Returns the record's new path, or None if it could not be written (never raises)."""
    try:
        rec = _read(path) or {}
        acked = interaction.incidents_dir() / "acked"
        # A PR declined again after an earlier ruling has an older record under acked/ with the same
        # name; carry its ruling forward so "retried once already at this head" is not forgotten.
        prior = _read(acked / path.name) if path.parent != acked else None
        history = list(rec.get("history") or (prior or {}).get("history") or [])
        if prior and prior.get("decision"):
            history.append({k: prior[k] for k in _HISTORY_KEYS if prior.get(k)})
        rec.update(fields)
        rec.update(decision=decision, decided_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), history=history[-10:])
        dest = path.parent if decision in OWNER_DECISIONS else acked
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / path.name
        _write(out, rec)
        if out != path:
            path.unlink(missing_ok=True)
        return out
    except OSError:
        return None


def reopen_decision(path: Path) -> None:
    """Put a decided record back in front of the decide stage (its `wait` blocker closed unmerged, say):
    into the live folder with no decision, the ruling kept in `history`. Never raises."""
    try:
        rec = _read(path)
        if rec is None:
            return
        history = list(rec.get("history") or [])
        history.append({k: rec[k] for k in _HISTORY_KEYS if rec.get(k)})
        for k in ("decision", "decided_at", "decision_note", "blocked_on_prs", "blocked_on_targets"):
            rec.pop(k, None)
        rec["history"] = history[-10:]
        out = interaction.incidents_dir() / path.name
        _write(out, rec)
        if out != path:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def decision_note_for(pr: int, head: str) -> str:
    """The note a `retry` decision left for the next fixer of this PR at this head, or "". A retry
    usually carries new evidence (a prerequisite now in an open PR, say) that the round which declined
    did not have, and the fixer needs to be told it rather than rediscover the same dead end."""
    d = interaction.incidents_dir()
    for folder in (d / "acked", d):
        try:
            paths = list(folder.glob(f"{DECLINED}-*-{pr}.json"))
        except OSError:
            continue
        for path in paths:
            rec = _read(path)
            if rec and rec.get("pr") == pr and rec.get("head") == head and rec.get("decision") == RETRY:
                return str(rec.get("decision_note") or "").strip()
    return ""


def declined_targets() -> dict[str, dict]:
    """Target slugs an authoring round declined, with the incident: `{slug: record}`, live or
    acknowledged. The author's picker skips these, since every author would reach the same conclusion
    (usually "main already has this"), and the curator weighs the agent's account against main. A
    record the curator judged `not-landed` no longer counts: the target goes back to the authors."""
    out: dict[str, dict] = {}
    d = interaction.incidents_dir()
    for folder in (d, d / "acked"):
        try:
            paths = sorted(folder.glob(f"{DECLINED}-roadmap-*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            target = rec.get("target")
            if not isinstance(target, str) or "/" not in target or rec.get("curator") == "not-landed":
                continue
            out[target.split("/", 1)[1]] = {**rec, "path": str(path)}
    return out


def handed_back_targets() -> dict[str, dict]:
    """Declined targets the curator handed back (`curator == "not-landed"`), `{slug: record}`, live
    or acknowledged. The next author's prompt says why, so it does not decline for the same reason."""
    out: dict[str, dict] = {}
    d = interaction.incidents_dir()
    for folder in (d / "acked", d):
        try:
            paths = sorted(folder.glob(f"{DECLINED}-roadmap-*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            target = rec.get("target")
            if isinstance(target, str) and "/" in target and rec.get("curator") == "not-landed":
                out[target.split("/", 1)[1]] = rec
    return out


def mark_declined_target(path: str, **fields) -> None:
    """Annotate a declined-target incident with the curator's verdict. Never raises. A decline the
    curator settled (landed, or handed back) or found waiting on an open PR needs nobody, so it moves
    to `acked/` and leaves the fleet's attention list; declined_targets reads both folders."""
    try:
        p = Path(path)
        rec = json.loads(p.read_text())
        rec.update(fields)
        p.write_text(json.dumps(rec, indent=1))
        # Settled either way, or waiting on an open PR: nothing for the owner to do.
        if (fields.get("curator") in ("landed", "not-landed") or fields.get("blocked_on")) and p.parent.name != "acked":
            (p.parent / "acked").mkdir(exist_ok=True)
            p.replace(p.parent / "acked" / p.name)
    except (OSError, ValueError):
        pass


def verdicts_by_pr() -> dict[int, dict]:
    """The recorded `declined` verdicts by PR, wherever the owner has filed them: live, acknowledged,
    or set aside as infrastructure (any `infra-*` folder). The newest record per PR wins."""
    out: dict[int, dict] = {}
    d = interaction.incidents_dir()
    folders = [d, d / "acked", *sorted(d.glob("infra-*"))] if d.is_dir() else []
    for folder in folders:
        try:
            paths = sorted(folder.glob(f"{DECLINED}-*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            pr = rec.get("pr")
            if isinstance(pr, int) and str(rec.get("last_at") or "") >= str(out.get(pr, {}).get("last_at") or ""):
                out[pr] = rec
    return out

"""Progress reports on demand: TauCetiProgress's `threshold` strategy, and seeing each report land.

The default progress stage asks `tauceti-progress due` whether a report is due project-wide (8 hours
since the last one anywhere) and then writes the busiest roadmap's. A worker that runs the
`threshold` strategy (TAUCETI_PROGRESS_STRATEGY=threshold, a TauCetiProgress build that has it)
instead looks for work continuously: a roadmap qualifies on its own numbers (N PRs in its window, T
days since its last report: N > 0, and declared complete or N + T > 10), subject to the merge gate's
six hours per roadmap, and the one with the most PRs is written. This module is the part of that
which is not the planner's:

* **When to plan** (`due`). A plan asks GitHub about every area, so it runs only when something it
  depends on has moved: the published documentation (a new window end), TauCetiRoadmap's `main` (a
  report landed, a README changed), or the time the last plan said a roadmap would next qualify. Both
  probes are cheap and cached, and neither is a GitHub API read.
* **Seeing a report land** (`shepherd`). The merge gate lands a report by compare-and-swap: its head
  must contain current `main` and `build` must have passed on that head. So every landing makes every
  other open report stale, a red `main` fails every report's build, and the gate's automatic run is
  occasionally lost. A local agent landed 29 reports on 2026-09-27 and learned each of these the hard
  way; its lander's rules are these: bring a report up to date only once `main` builds, judge `main`
  by its newest finished CI run, ignore the gate's "build not completed" first pass and any refusal
  about an earlier head, ask the gate again (at most twice) when it has gone quiet, and hand a person
  only what needs one: a build that fails on an up-to-date report while `main` builds, or a refusal
  that survives the re-asks.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import gate as gate_mod
from .config import log
from .constants import ROADMAP
from .github import gh_run
from .interaction import record_incident

STRATEGY = os.environ.get("TAUCETI_PROGRESS_STRATEGY", "").strip()
# How many of our reports may be open at once. Landing is one at a time anyway; a second lets the next
# report be written while the first builds, and more would only queue branch updates behind each other.
MAX_OPEN = max(1, int(os.environ.get("TAUCETI_PROGRESS_MAX_OPEN", "2")))
SHEPHERD_EVERY = 300  # seconds between looks at our open reports
DOCS_PROBE_TTL = 600  # the documentation deploys every few hours
ROADMAP_PROBE_TTL = 300
SCAN_MAX_AGE = 3600  # re-plan at least this often even if neither probe answers
REGATE_AFTER = 20 * 60  # the build finished this long ago, the report is open, and the gate is silent
MAX_REGATES = 2
ERR_BACKOFF_BASE = 15 * 60
ERR_BACKOFF_MAX = 6 * 3600
RED = {"failure", "timed_out", "startup_failure"}
INCIDENT = "progress-stuck"


def threshold_mode() -> bool:
    return STRATEGY == "threshold"


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        log(f"progress: could not write {path}: {exc}")


class Paths:
    def __init__(self, state: Path):
        self.dir = state / "progress"
        self.table = self.dir / "table.json"  # the planner's candidate table (TauCetiProgress writes it)
        self.labels = self.dir / "labels.json"  # the planner's label cache
        self.scan = self.dir / "scan.json"  # what the last plan was made against, and what it found
        self.landing = self.dir / "landing.json"  # our open reports and what has been done for each
        self.probe = state / "cache" / "progress-probe.json"


# ----------------------------------------------------------------------------- when to plan


def err_backoff(counters) -> tuple[bool, str, float | None]:
    """(waiting, reason, until): after N failed rounds, wait 15 min · 2^(N-1), at most 6 h. A failure is
    usually transient (a documentation deploy mid-extraction, GitHub), so it is waited out, never
    latched until someone clears a file."""
    n = counters.read("progress-err")
    if n <= 0:
        return False, "", None
    wait = min(ERR_BACKOFF_BASE * (1 << min(n - 1, 10)), ERR_BACKOFF_MAX)
    until = counters.read("progress-err-ts") + wait
    if _now() < until:
        return True, f"{n} progress round(s) failed in a row; trying again in {int(until - _now()) // 60} min", until
    return False, "", None


def probe_docs(state: Path, argv_fn) -> str | None:
    """The commit the published documentation describes (`tauceti-progress docs-commit`), cached."""
    p = Paths(state).probe
    cached = _read(p)
    if cached.get("docs_sha") and _now() - float(cached.get("docs_at") or 0) < DOCS_PROBE_TTL:
        return cached["docs_sha"]
    try:
        proc = subprocess.run(argv_fn(state, "docs-commit"), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (proc.stdout or "").strip().splitlines()[-1:] if proc.returncode == 0 else []
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha[0]):
        return None
    _write(p, {**_read(p), "docs_sha": sha[0], "docs_at": _now()})
    return sha[0]


def probe_roadmap(state: Path) -> str | None:
    """TauCetiRoadmap's `main` tip: one gated `git ls-remote`, cached."""
    p = Paths(state).probe
    cached = _read(p)
    if cached.get("roadmap_sha") and _now() - float(cached.get("roadmap_at") or 0) < ROADMAP_PROBE_TTL:
        return cached["roadmap_sha"]
    proc = gate_mod.gated_git(
        ["git", "ls-remote", f"https://github.com/{ROADMAP}", "refs/heads/main"], op="ls-remote", target=ROADMAP
    )
    m = re.match(r"([0-9a-f]{40})\s", proc.stdout or "") if proc.returncode == 0 else None
    if not m:
        return None
    _write(p, {**_read(p), "roadmap_sha": m.group(1), "roadmap_at": _now()})
    return m.group(1)


def due(state: Path, counters, argv_fn) -> tuple[bool, str, float | None]:
    """Is a progress round worth running? `(due, reason, wake_at)`. Never raises, never reads the
    GitHub API: a probe that fails reads as "unchanged", and the scan's age is the backstop."""
    try:
        waiting, why, until = err_backoff(counters)
        if waiting:
            return False, why, until
        paths = Paths(state)
        land = _read(paths.landing)
        open_prs = land.get("prs") or {}
        last_look = float(land.get("shepherded_at") or 0)
        if open_prs and _now() - last_look >= SHEPHERD_EVERY:
            return True, f"{len(open_prs)} report(s) to see through to landing", None
        wake = last_look + SHEPHERD_EVERY if open_prs else None
        if len(open_prs) >= MAX_OPEN:
            return False, f"waiting for {len(open_prs)} open report(s) to land before writing another", wake
        scan = _read(paths.scan)
        if not scan:
            return True, "no roadmap has been assessed yet", None
        if _now() - float(scan.get("at") or 0) >= SCAN_MAX_AGE:
            return True, "the last assessment is an hour old", None
        docs = probe_docs(state, argv_fn)
        if docs and docs != scan.get("to_sha"):
            return True, f"the documentation now describes {docs[:7]}", None
        head = probe_roadmap(state)
        if head and head != scan.get("roadmap_head"):
            return True, f"TauCetiRoadmap main moved to {head[:7]}", None
        nq = _ts(scan.get("next_qualifies_at"))
        if nq is not None and nq <= _now():
            return True, "a roadmap qualifies by now", None
        if scan.get("chosen"):
            return True, f"{scan['chosen']} qualifies", None
        wakes = [t for t in (nq, wake, _now() + ROADMAP_PROBE_TTL) if t]
        return False, scan.get("summary") or "no roadmap qualifies", min(wakes)
    except Exception as exc:  # noqa: BLE001 - a due-check that raises aborts the whole round
        return False, f"progress due-check failed: {exc}", None


def record_scan(state: Path, roadmap_dir: Path, *, reason: str) -> dict:
    """After a plan (chosen or not): remember what it was made against and summarise the table."""
    paths = Paths(state)
    table = _read(paths.table)
    try:
        head = subprocess.run(["git", "-C", str(roadmap_dir), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    rows = table.get("rows") or []
    qualifying = [r for r in rows if r.get("qualifies")]
    waiting = [r for r in rows if not r.get("qualifies") and (r.get("prs") or 0) > 0]
    if qualifying:
        summary = f"{len(qualifying)} roadmap(s) qualify; the most PRs: {qualifying[0]['area']} ({qualifying[0]['prs']})"
    else:
        nxt = min((r for r in waiting if r.get("qualifies_from")), key=lambda r: r["qualifies_from"], default=None)
        summary = "no roadmap qualifies" + (
            f"; next: {nxt['area']} at {nxt['qualifies_from'][:16].replace('T', ' ')} UTC" if nxt else ""
        )
    scan = {
        "at": _now(),
        "to_sha": table.get("to_sha") or "",
        "roadmap_head": head,
        "chosen": table.get("chosen"),
        "next_qualifies_at": table.get("next_qualifies_at"),
        "qualifying": len(qualifying),
        "waiting": len(waiting),
        "summary": summary,
        "reason": reason,
    }
    _write(paths.scan, scan)
    return scan


# ----------------------------------------------------------------------------- the writing round


def snapshot_source(checkout: Path, sha: str, dest: Path) -> Path | None:
    """The library's source at the window's end, for the writing model to check a layer against.
    `git archive`, so no working tree moves and no git history is handed over. None on failure (the
    prompt then says nothing about it)."""
    import shutil

    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        arch = subprocess.Popen(["git", "-C", str(checkout), "archive", sha, "TauCeti"], stdout=subprocess.PIPE)
        untar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=arch.stdout, capture_output=True, timeout=600)
        arch.stdout.close()
        if arch.wait(timeout=600) != 0 or untar.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return dest


def write_check_script(work: Path, argv: list[str]) -> Path:
    """A one-line script the writing model runs to check its two files (`tauceti-progress check`)."""
    script = work / "check.sh"
    script.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in argv) + "\n")
    script.chmod(0o755)
    return script


def addendum(check_script: Path, source: Path | None) -> str:
    """What this worker adds to TauCetiProgress's prompt: the lessons of the reports landed by hand."""
    src = ""
    if source is not None:
        src = (
            f"- The library's source at the window's end is in `{source}` (a plain copy, no history). "
            "When the facts and the previous report leave a layer's state unclear, look there before "
            "judging it: find the declarations the README names for that layer. A layer is `done` only "
            "when every milestone the README gives it is proved there, and `untouched` only when "
            "nothing for it is.\n"
        )
    return (
        "\n\n## Before you stop\n\n"
        + src
        + f"- When both files are written, run `sh {check_script}`. It checks the word limits and the "
        "two headings above, that every documentation link was copied from the supplied material and "
        "still resolves, and that no layer the previous report assessed has become `unassessed`. Fix "
        "every line marked FAIL and run it again, until it prints OK. Lines marked WARN are yours to "
        "judge. Running it is the one command you need; it does not touch git.\n"
    )


def fixup_prompt(check_output: str, status_out: Path, section_out: Path, check_script: Path) -> str:
    return (
        "You wrote a Tau Ceti progress report into two files, and its check failed. Fix the files in place "
        f"and nothing else: `{status_out}` (the status snapshot) and `{section_out}` (the progress-log "
        "section). The check's output is below; every FAIL line must go. Keep what is right, change only "
        f"what the failures require, then run `sh {check_script}` until it prints OK.\n\n"
        "```\n" + check_output.strip()[-6000:] + "\n```\n"
    )


def record_opened(state: Path, number: int, plan: dict, url: str) -> None:
    paths = Paths(state)
    land = _read(paths.landing)
    prs = land.setdefault("prs", {})
    prs[str(number)] = {"area": plan.get("roadmap"), "url": url, "to_sha": plan.get("to_sha"), "opened_at": _now()}
    _write(paths.landing, land)


# ----------------------------------------------------------------------------- seeing reports land


def _gh_json(args: list[str]):
    p = gh_run(["gh", *args])
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout or "null")
    except ValueError:
        return None


def _main_state() -> tuple[str, bool, str]:
    """(main sha, main red?, what CI said): red by the newest FINISHED push run of CI on main. A run
    still going says nothing yet (it is usually the build of the report that just landed)."""
    sha = _gh_json(["api", f"repos/{ROADMAP}/commits/main", "--jq", "{sha: .sha}"]) or {}
    runs = _gh_json(["api", f"repos/{ROADMAP}/actions/workflows/ci.yml/runs?branch=main&event=push&status=completed&per_page=1",
                     "--jq", "{c: (.workflow_runs[0].conclusion // \"\"), s: (.workflow_runs[0].head_sha // \"\")}"]) or {}
    concl = str(runs.get("c") or "")
    return str(sha.get("sha") or ""), concl in RED, f"CI {concl or 'unknown'} on {str(runs.get('s') or '')[:7]}"


def _builds(head: str) -> list[dict]:
    runs = _gh_json(["api", f"repos/{ROADMAP}/commits/{head}/check-runs?per_page=100",
                     "--jq", '[.check_runs[] | select(.name == "build") | {status, conclusion, completed_at}]'])
    return runs if isinstance(runs, list) else []


def _behind(head: str) -> int | None:
    d = _gh_json(["api", f"repos/{ROADMAP}/compare/main...{head}", "--jq", "{b: .behind_by}"])
    return int(d["b"]) if isinstance(d, dict) and isinstance(d.get("b"), int) else None


def refusal_reason(body: str) -> str:
    """The gate's own words, from the fenced block of its "did not merge" comment."""
    m = re.search(r"```\s*\n(.*?)\n\s*```", body, re.S)
    return (m.group(1) if m else body).strip()[:500]


def transient(reason: str) -> bool:
    """The gate's first pass on a new head finds `build` queued or running and says so; it runs again
    by itself when CI completes. Any status word: 'queued' was once missed because only
    'in_progress' was known (2026-09-27)."""
    return "not completed" in reason or "has not reported" in reason


def about_another_head(reason: str, head: str) -> bool:
    """A refusal naming a head other than `head` judged the branch before an update."""
    named = re.findall(r"(?:\bhead |\bon )([0-9a-f]{7})\b", reason)
    return bool(named) and head[:7] not in named


def latest_refusal(comments: list[dict], head: str, since: float | None) -> str | None:
    """The gate's newest real refusal of `head`: posted after `since` (the head's build finished), not
    a first-pass "not completed", and not about an earlier head."""
    for c in reversed(comments or []):
        body = c.get("body") or ""
        if "did not merge" not in body:
            continue
        at = _ts(c.get("createdAt"))
        if since is not None and at is not None and at < since:
            return None
        reason = refusal_reason(body)
        if transient(reason) or about_another_head(reason, head):
            continue
        return reason
    return None


def _escalate(n: int, rec: dict, head: str, detail: str) -> None:
    if rec.get("escalated") == head:
        return
    rec["escalated"] = head
    log(f"  progress: #{n} ({rec.get('area')}) needs a person: {detail}")
    record_incident(INCIDENT, f"{rec.get('area') or 'report'}-{n}", pr=n, repo=ROADMAP, head=head,
                    url=rec.get("url") or f"https://github.com/{ROADMAP}/pull/{n}", area=rec.get("area") or "",
                    detail=f"progress report #{n} ({rec.get('area')}): {detail}")


def _landed(n: int) -> bool:
    """Did the gate land #n? Found by the gate's own commit message ("Closes #n.") among main's newest
    commits, never by remembering where main was: that misread one landing on 2026-09-27."""
    rows = _gh_json(["api", f"repos/{ROADMAP}/commits?sha=main&per_page=40", "--jq", "[.[].commit.message]"])
    return isinstance(rows, list) and any(f"Closes #{n}." in (m or "") for m in rows)


def shepherd(state: Path) -> tuple[bool, list[int], list[str]]:
    """One look at every open report of ours: `(acted, still_open, notes)`. Each GitHub call is gated;
    a failed read leaves that report for the next look."""
    paths = Paths(state)
    land = _read(paths.landing)
    known = land.setdefault("prs", {})
    rows = _gh_json(["pr", "list", "--repo", ROADMAP, "--state", "open", "--author", "@me", "--limit", "50",
                     "--json", "number,headRefName,headRefOid,url,comments"])
    if rows is None:
        return False, [int(n) for n in known], ["could not list our open reports; will look again"]
    mine = {r["number"]: r for r in rows if (r.get("headRefName") or "").startswith("progress/")}
    notes: list[str] = []
    acted = False
    for key in [k for k in known if int(k) not in mine]:
        rec = known.pop(key)
        outcome = "landed" if _landed(int(key)) else "was closed without landing"
        notes.append(f"#{key} ({rec.get('area')}) {outcome}")
    main_sha, main_red, main_ci = ("", False, "") if not mine else _main_state()
    for n, row in sorted(mine.items()):
        rec = known.setdefault(str(n), {"area": row["headRefName"].split("/")[-1], "url": row.get("url"),
                                        "opened_at": _now()})
        rec["area"] = rec.get("area") or row["headRefName"].split("/")[-1]
        head = row.get("headRefOid") or ""
        label = f"#{n} ({rec['area']})"
        behind = _behind(head)
        if behind is None:
            notes.append(f"{label}: could not compare with main")
            continue
        if behind > 0:
            if main_red:
                notes.append(f"{label}: behind main by {behind}, and main does not build ({main_ci}); waiting")
                continue
            if rec.get("updated_head") == head and rec.get("updated_main") == main_sha:
                notes.append(f"{label}: branch update requested; waiting for it")
                continue
            # The gate lands first and closes the pull request a moment later, and its landing is what
            # moved main: updating that branch would only give the gate a stale head to refuse.
            if _landed(n):
                notes.append(f"{label}: landed; the gate is closing it")
                continue
            p = gh_run(["gh", "api", "-X", "PUT", f"repos/{ROADMAP}/pulls/{n}/update-branch", "-f",
                        f"expected_head_sha={head}"])
            if p.returncode == 0:
                rec.update(updated_head=head, updated_main=main_sha)
                acted = True
                notes.append(f"{label}: behind main by {behind}; updated the branch onto {main_sha[:7]}")
            else:
                notes.append(f"{label}: updating the branch failed: {(p.stderr or '').strip()[:160]}")
            continue
        builds = _builds(head)
        if not builds or any(b.get("status") != "completed" for b in builds):
            notes.append(f"{label}: build running on {head[:7]}")
            continue
        finished = max((_ts(b.get("completed_at")) or 0 for b in builds), default=0) or None
        if any(b.get("conclusion") != "success" for b in builds):
            if main_red:
                notes.append(f"{label}: build failed while main does not build ({main_ci}); waiting for main")
            else:
                _escalate(n, rec, head, f"build failed on {head[:7]}, which is up to date with main, while main builds")
                notes.append(f"{label}: build failed on an up-to-date branch; handed to a person")
            continue
        refusal = latest_refusal(row.get("comments") or [], head, finished)
        regates = rec.get("regates") if rec.get("regates_head") == head else 0
        quiet = finished is not None and _now() - finished >= REGATE_AFTER
        recent = _now() - float(rec.get("regated_at") or 0) < REGATE_AFTER
        if refusal is None and not quiet:
            notes.append(f"{label}: build passed on {head[:7]}; the gate should land it shortly")
            continue
        if rec.get("escalated") == head:
            notes.append(f"{label}: waiting for a person (see `attention`)")
            continue
        if regates >= MAX_REGATES:
            _escalate(n, rec, head, f"the gate has not landed it after {regates} re-asks"
                      + (f"; its last refusal: {refusal}" if refusal else ""))
            notes.append(f"{label}: the gate still refuses; handed to a person")
            continue
        if recent:
            notes.append(f"{label}: asked the gate again {int(_now() - float(rec['regated_at'])) // 60} min ago")
            continue
        p = gh_run(["gh", "workflow", "run", "progress-merge.yml", "--repo", ROADMAP, "-f", f"pr={n}"])
        if p.returncode == 0:
            rec.update(regates=regates + 1, regates_head=head, regated_at=_now())
            acted = True
            why = f"it refused: {refusal[:120]}" if refusal else "it has been quiet since the build passed"
            notes.append(f"{label}: asked the gate again ({regates + 1}/{MAX_REGATES}); {why}")
        else:
            notes.append(f"{label}: dispatching the gate failed: {(p.stderr or '').strip()[:160]}")
    land["shepherded_at"] = _now()
    _write(paths.landing, land)
    return acted, sorted(mine), notes


def status_line(state: Path) -> str:
    """One line for a status file or a view: open reports, then the last assessment."""
    paths = Paths(state)
    land, scan = _read(paths.landing), _read(paths.scan)
    parts = []
    prs = land.get("prs") or {}
    if prs:
        parts.append("landing " + ", ".join(f"#{n} {r.get('area')}" for n, r in sorted(prs.items())))
    if scan.get("summary"):
        parts.append(scan["summary"])
    return "; ".join(parts)

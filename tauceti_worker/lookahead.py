"""tauceti_worker.lookahead — proving a blocked target ahead of its supplier.

An item of the operator's list that waits on a supplier still in flight can be proved now, against
`sorry`'d stubs of the supplier's pinned statements, on a branch `lookahead/<area>/<slug>` of the
account's TauCeti fork. The branch is never opened as a pull request. When the supplier lands, an
ordinary author round finds the branch and ports it: each stub is replaced by the landed declaration
and the branch's planned splits are opened, one PR per round. Off unless `TAUCETI_LOOKAHEAD=1`.

All state lives on GitHub, so whichever fleet is active can use a branch another fleet built: the
branches themselves (one `ls-remote` lists them), the `LOOKAHEAD.md` header on each branch (status,
the main commit it was built on, the split plan), and the port marker every port PR carries. The
local files under `<gate>/lookahead/` are a snapshot for the fleet view and a header cache, never a
source of truth. Every outcome that is not "worked as planned" leaves a `lookahead` incident.
"""

from __future__ import annotations

import base64
import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gate as gate_mod
from .config import log
from .targets import TargetItem, Targets

ENV = "TAUCETI_LOOKAHEAD"
HOLD_HOURS_ENV = "TAUCETI_LOOKAHEAD_HOLD_HOURS"
# Exported for a lookahead session only: git-safe-push then pushes nothing but this branch, and
# gh-safe-pr-create refuses outright.
BRANCH_ENV = "TAUCETI_LOOKAHEAD_BRANCH"
# An operator's one-off round (`tauceti-fleet lookahead SLUG`): only this list item, as a session
# while it is blocked or as a port once it is eligible, and never anything else instead.
TARGET_ONLY_ENV = "TAUCETI_TARGET_ONLY"
PREFIX = "lookahead/"
STALE_DAYS = 7
DEFAULT_HOLD_HOURS = 6
OFF_LISTING_TTL = 1800  # see recent_snapshot
FAILED_HOLD_DAYS = 3
INCIDENT = "lookahead"
HEADER_RE = re.compile(r"<!--tauceti-lookahead:v1 (\{.*?\})-->")
PORT_RE = re.compile(r"<!--tauceti-lookahead-port:v1 (\{[^}]*\})-->")
# The port prompt asks for one of these as the report's last word on the branch.
NOT_USED_RE = re.compile(r"^\W*Lookahead:\W*not used\W*(.*)$", re.I | re.M)


def enabled() -> bool:
    """Lookahead sessions and ports: `TAUCETI_LOOKAHEAD=1`."""
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def active() -> bool:
    """Any lookahead machinery at all, the fork's branches listed: `TAUCETI_LOOKAHEAD` is set, to 1, or
    to 0 for a fleet that only holds items for their port and sweeps finished branches (the fleet tool
    always sets one of the two). Unset, nothing here touches GitHub: a test, or a hand-run `work`,
    once deleted a real branch through the sweep (2026-10-04)."""
    return bool(os.environ.get(ENV, "").strip())


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        log(f"{name}={raw!r} is not a number; using {default:g}")
        return default


def target_only() -> str:
    return os.environ.get(TARGET_ONLY_ENV, "").strip()


def hold_hours() -> float:
    return _env_number(HOLD_HOURS_ENV, DEFAULT_HOLD_HOURS)


def branch_name(area: str, slug: str) -> str:
    return f"{PREFIX}{area}/{slug}"


def parse_branch(name: str) -> tuple[str, str] | None:
    name = name.removeprefix("refs/heads/")
    if not name.startswith(PREFIX):
        return None
    area, _, slug = name[len(PREFIX):].partition("/")
    return (area, slug) if area and slug and "/" not in slug else None


def camel(slug: str) -> str:
    """`linear-equiv-prod-free-of-stable` → `LinearEquivProdFreeOfStable`, the stub directory's name."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", slug) if part)


def refused(it: TargetItem) -> bool:
    """The item carries a `lookahead: no …` clause: its route is unsettled, so it is neither proved
    ahead nor stubbed for another item (its statement is not pinned)."""
    for clause in it.meta:
        key, _, value = clause.partition(":")
        if key.strip().lower() == "lookahead" and value.strip().lower().startswith("no"):
            return True
    return False


# ---- the branch header and the port marker ------------------------------------------------------------


@dataclass
class Split:
    n: int
    after: tuple[int, ...] = ()
    title: str = ""


@dataclass
class Header:
    area: str
    slug: str
    main: str = ""
    status: str = "partial"  # "complete": the whole target is proved on the branch
    suppliers: tuple[str, ...] = ()
    splits: tuple[Split, ...] = (Split(1),)
    built_at: float | None = None  # the branch tip's commit time, when known
    raw: dict = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def last_split(self) -> int:
        return max(s.n for s in self.splits)

    @property
    def stub_free(self) -> bool:
        """A complete proof that stubs nothing: what the target uses from its listed needs has all
        landed, so it can be ported now, however coarse the list's `needs:` (the pilot's first branch,
        2026-10-04, was one)."""
        return self.complete and not self.suppliers


def parse_header(text: str) -> Header | None:
    """The `<!--tauceti-lookahead:v1 {…}-->` line of a LOOKAHEAD.md, or None. A malformed split list
    reads as one split, so a branch with a sloppy plan is still ported, just serially."""
    m = HEADER_RE.search(text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("area"), str) or not isinstance(d.get("slug"), str):
        return None
    splits: list[Split] = []
    for s in d.get("splits") or []:
        try:
            n = int(s["n"])
            after = tuple(int(a) for a in (s.get("after") or []) if int(a) != n)
        except (KeyError, TypeError, ValueError):
            splits = []
            break
        splits.append(Split(n, after, str(s.get("title") or "")))
    numbers = {s.n for s in splits}
    if not splits or len(numbers) != len(splits) or any(not set(s.after) <= numbers for s in splits):
        splits = [Split(1)]
    suppliers = tuple(str(x) for x in (d.get("suppliers") or []) if isinstance(x, str))
    status = "complete" if d.get("status") == "complete" else "partial"
    return Header(area=d["area"], slug=d["slug"], main=str(d.get("main") or ""), status=status,
                  suppliers=suppliers, splits=tuple(sorted(splits, key=lambda s: s.n)), raw=d)


def port_marker(branch: str, split: int) -> str:
    return f'<!--tauceti-lookahead-port:v1 {{"branch":"{branch}","split":{split}}}-->'


def port_markers(body: str) -> tuple[tuple[str, int], ...]:
    """Every (branch, split) a PR body's port markers name."""
    out: list[tuple[str, int]] = []
    for m in PORT_RE.finditer(body or ""):
        try:
            d = json.loads(m.group(1))
            out.append((str(d["branch"]), int(d["split"])))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


# ---- choosing what to prove ahead ----------------------------------------------------------------------


def _status(live: Targets, slug: str) -> str:
    it = live.find(slug)
    return "done" if it is None else it.status  # an undefined need counts as done, as eligible_items has it


def _supplier_ok(live: Targets, it: TargetItem) -> bool:
    """A need that may be stubbed: in flight or open, settled, and itself unblocked (every need it
    has is done), so a stub never stands in for another stub."""
    if it.status not in ("open", "inflight") or refused(it):
        return False
    return all(_status(live, n) == "done" for n in it.needs)


def dependents(live: Targets) -> dict[str, int]:
    """For every listed slug, how many items not yet done need it, directly or transitively."""
    direct: dict[str, set[str]] = {}
    for items in live.areas.values():
        for it in items:
            if it.status == "done":
                continue
            for n in it.needs:
                direct.setdefault(n, set()).add(it.slug)
    out: dict[str, int] = {}
    for items in live.areas.values():
        for it in items:
            seen: set[str] = set()
            todo = list(direct.get(it.slug, ()))
            while todo:
                s = todo.pop()
                if s not in seen:
                    seen.add(s)
                    todo.extend(direct.get(s, ()))
            out[it.slug] = len(seen)
    return out


@dataclass
class Candidate:
    area: str
    item: TargetItem
    stubs: list[TargetItem]  # the unmet needs the session stubs
    resume: str = ""  # the tip of a partial branch to continue, else "" (a fresh branch)


def candidates(live: Targets, *, only: str = "any", skip: list[str] | tuple[str, ...] = (),
               declined: set[str] | frozenset[str] = frozenset(), branches: dict | None = None,
               headers: dict | None = None, failed: set[str] | frozenset[str] = frozenset()) -> list[Candidate]:
    """The items a lookahead session may take, most depended-on first, then in file order.

    An item qualifies when it is open, settled (no `lookahead: no`), not declined by an author, not
    recently failed by a session, and has unmet needs every one of which may be stubbed
    (`_supplier_ok`). An item whose branch is complete is done here; one whose branch is partial is
    offered again as a resume; one whose branch header cannot be read is left alone."""
    branches = branches or {}
    headers = headers or {}
    weight = dependents(live)
    pinned = only not in ("any", "auto", "")
    out: list[tuple[int, int, Candidate]] = []
    order = 0
    for area, items in live.areas.items():
        if (pinned and area != only) or (not pinned and area in skip):
            order += len(items)
            continue
        for it in items:
            order += 1
            if it.status != "open" or refused(it) or it.slug in declined or it.slug in failed:
                continue
            unmet = [n for n in it.needs if _status(live, n) != "done"]
            if not unmet:
                continue  # eligible: an ordinary author takes it
            stubs = [live.find(n) for n in unmet]
            if not all(s is not None and _supplier_ok(live, s) for s in stubs):
                continue
            resume = ""
            sha = branches.get((area, it.slug))
            if sha:
                h = headers.get(sha)
                if h is None or h.complete:
                    continue
                resume = sha
            out.append((-weight.get(it.slug, 0), order, Candidate(area, it, stubs, resume)))  # type: ignore[arg-type]
    return [c for _w, _o, c in sorted(out, key=lambda t: (t[0], t[1]))]


@dataclass
class PortPlan:
    branch: str
    header: Header
    opened: dict[int, int]  # split → an open PR porting it
    merged: dict[int, int]  # split → the merged PR that ported it
    ready: list[Split]  # unopened splits whose prerequisite splits have all merged

    @property
    def next(self) -> Split | None:
        return self.ready[0] if self.ready else None


def spent(plan: "PortPlan") -> bool:
    """Every split of a complete branch has merged: it has nothing left to give, whatever its item's
    state, and is neither offered for porting nor kept."""
    return plan.header.complete and not plan.opened and all(s.n in plan.merged for s in plan.header.splits)


def port_plan(branch: str, header: Header, open_ports: dict[tuple[str, int], int],
              merged_ports: dict[tuple[str, int], int]) -> PortPlan:
    opened = {n: pr for (b, n), pr in open_ports.items() if b == branch}
    merged = {n: pr for (b, n), pr in merged_ports.items() if b == branch}
    ready = [s for s in header.splits
             if s.n not in opened and s.n not in merged and all(a in merged for a in s.after)]
    return PortPlan(branch, header, opened, merged, ready)


def port_ready(live: Targets, plans: dict[tuple[str, str], PortPlan],
               open_item_prs: dict[tuple[str, str], set[int]]) -> list[tuple[str, TargetItem]]:
    """Items an author may port a split of now, beyond the eligible ones the list offers anyway: an
    in-flight item whose needs have all landed (the owner's ruling, 2026-10-04: no waiting for merges
    between independent splits), and an open or in-flight item whose branch stubs nothing, whatever
    its needs. Either way every open PR carrying its marker is one of this branch's port PRs, and the
    plan has a ready split."""
    out = []
    for area, items in live.areas.items():
        for it in items:
            plan = plans.get((area, it.slug))
            if plan is None or plan.next is None or it.status == "done":
                continue
            if not plan.header.stub_free:
                if it.status != "inflight" or any(_status(live, n) != "done" for n in it.needs):
                    continue
            if not open_item_prs.get((area, it.slug), set()) <= set(plan.opened.values()):
                continue
            out.append((area, it))
    return out


# ---- GitHub: the branches, their headers, deletion -----------------------------------------------------


def state_dir() -> Path | None:
    g = gate_mod.current()
    return g.dir / "lookahead" if g.enabled and g.dir is not None else None


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        log(f"lookahead: could not write {path}: {e}")


def list_branches(fork: str) -> dict[tuple[str, str], str] | None:
    """`{(area, slug): tip}` for every `lookahead/*` branch on the fork, by one gated `ls-remote`;
    None when it cannot be read (callers then change nothing: no hold, no port, no new session)."""
    url = f"https://github.com/{fork}"
    p = gate_mod.gated_git(["git", "ls-remote", url, f"refs/heads/{PREFIX}*"], op="lookahead", target=url,
                           capture_output=True, timeout=120)
    if p.returncode != 0:
        log(f"lookahead: could not list the fork's lookahead branches ({(p.stderr or '').strip()[-160:]})")
        return None
    out: dict[tuple[str, str], str] = {}
    for line in (p.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        key = parse_branch(ref.strip())
        if key and sha.strip():
            out[key] = sha.strip()
    return out


def read_header(fork: str, sha: str, run=None) -> Header | None:
    """The LOOKAHEAD.md header at commit `sha` of the fork, cached by sha (a commit never changes).
    One gated API read on a miss; the tip's commit date rides along so staleness needs no second read."""
    d = state_dir()
    cache = d / "headers" / f"{sha}.json" if d is not None else None
    if cache is not None:
        try:
            c = json.loads(cache.read_text())
            h = parse_header(c.get("text") or "")
            if h is not None:
                h.built_at = c.get("built_at")
            return h  # an unparseable header stays unparseable at this tip: no second read
        except (OSError, ValueError):
            pass
    if run is None:
        from .github import gh_run as run  # noqa: PLC0415 - late: tests pass a fake
    p = run(["gh", "api", f"repos/{fork}/contents/LOOKAHEAD.md?ref={sha}", "--jq", ".content"])
    if p.returncode != 0:
        log(f"lookahead: could not read LOOKAHEAD.md at {sha[:12]} on {fork}")
        return None
    try:
        text = base64.b64decode((p.stdout or "").strip()).decode("utf-8", "replace")
    except ValueError:
        return None
    built_at = None
    q = run(["gh", "api", f"repos/{fork}/commits/{sha}", "--jq", ".commit.committer.date"])
    if q.returncode == 0:
        built_at = _parse_iso(q.stdout.strip())
    h = parse_header(text)
    if h is not None:
        h.built_at = built_at
    if cache is not None:
        _write_json(cache, {"text": text, "built_at": built_at})
    return h


def _parse_iso(s: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def stale(h: Header, now: float | None = None) -> bool:
    return h.built_at is not None and (now or time.time()) - h.built_at > STALE_DAYS * 86400


def delete_branch(fork: str, area: str, slug: str, sha: str) -> bool:
    """Delete `lookahead/<area>/<slug>` from the fork, leased on `sha` so a branch that moved since it
    was listed is kept. One gated push."""
    url = f"https://github.com/{fork}"
    ref = f"refs/heads/{branch_name(area, slug)}"
    p = gate_mod.gated_git(["git", "push", f"--force-with-lease={ref}:{sha}", url, f":{ref}"], op="lookahead",
                           target=url, kind=gate_mod.GIT_PUSH, capture_output=True, timeout=120)
    if p.returncode != 0:
        log(f"lookahead: could not delete {ref} on {fork} ({(p.stderr or '').strip()[-160:]})")
    return p.returncode == 0


def write_snapshot(fork: str, branches: dict[tuple[str, str], str], headers: dict[str, Header]) -> None:
    """What the fleet view's `lookahead` row reads: the branches as of this listing."""
    d = state_dir()
    if d is None:
        return
    rows = []
    for (area, slug), sha in sorted(branches.items()):
        h = headers.get(sha)
        rows.append({"area": area, "slug": slug, "sha": sha,
                     "status": h.status if h else "unknown", "main": h.main if h else "",
                     "built_at": h.built_at if h else None, "splits": len(h.splits) if h else 0})
    _write_json(d / "branches.json", {"at": time.time(), "fork": fork, "branches": rows})


def recent_snapshot(max_age: float) -> tuple[str, dict[tuple[str, str], str]] | None:
    """(fork, branches) from the last listing when it is younger than `max_age` seconds, else None.
    A fleet without lookahead needs the branches only to hold items for their port, and a listing
    half an hour old is good enough for that, so it does not list the fork every author round."""
    d = state_dir()
    if d is None:
        return None
    try:
        snap = json.loads((d / "branches.json").read_text())
        if not (0 <= time.time() - float(snap["at"]) < max_age) or not snap.get("fork"):
            return None
        return str(snap["fork"]), {(r["area"], r["slug"]): r["sha"] for r in snap.get("branches") or []}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def history(event: str, area: str, slug: str, **fields) -> None:
    """One line per session, port and deletion in `<gate>/lookahead/history.jsonl`: the pilot's
    measurements (session time, outcome) come from here and from the port markers on GitHub."""
    d = state_dir()
    if d is None:
        return
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "area": area, "slug": slug,
           "worker": os.environ.get("TAUCETI_WORKER_ID") or "-", **fields}
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "history.jsonl", "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as e:
        log(f"lookahead: could not append to the history: {e}")


# ---- incidents ------------------------------------------------------------------------------------------


def record(what: str, area: str, slug: str, detail: str, **fields) -> Path | None:
    """A `lookahead` incident `<what>-<area>-<slug>` for the fleet's attention list: failed (a session
    pushed nothing), held (a fleet without lookahead left an item for its port), skipped (the hold ran
    out and the item was authored fresh), mismatch (the port could not use the branch), and the
    curator's sweep: stale, abandoned (deleted unused) and orphan (its item is not on the list)."""
    from .interaction import record_incident  # noqa: PLC0415 - interaction imports gate at load

    return record_incident(INCIDENT, f"{what}-{area}-{slug}", stage="lookahead", what=what,
                           target=f"{area}/{slug}", branch=branch_name(area, slug), detail=detail, **fields)


def _incident(what: str, area: str, slug: str) -> dict | None:
    from .interaction import _safe, incidents_dir  # noqa: PLC0415

    p = incidents_dir() / f"{INCIDENT}-{_safe(f'{what}-{area}-{slug}')}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _age(rec: dict | None, key: str = "first_at") -> float | None:
    if not rec:
        return None
    t = _parse_iso(str(rec.get(key) or ""))
    return None if t is None else time.time() - t


def failed_recently(area: str, slug: str) -> bool:
    """A session for this item pushed nothing within FAILED_HOLD_DAYS and nobody acknowledged it:
    another session would most likely reach the same end, so the item is not offered meanwhile."""
    age = _age(_incident("failed", area, slug), "last_at")
    return age is not None and age < FAILED_HOLD_DAYS * 86400


def hold_started(area: str, slug: str) -> float | None:
    """Seconds since this fleet first held the item for its port, or None if it never has."""
    return _age(_incident("held", area, slug))


def not_used_reason(summary: str) -> str | None:
    """The reason a port round gave for authoring fresh (`Lookahead: not used — …`), or None."""
    m = NOT_USED_RE.search(summary or "")
    return (m.group(1).strip(" —-:") or "no reason given") if m else None

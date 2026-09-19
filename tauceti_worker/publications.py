"""tauceti_worker.publications — the publication ledger and its reconciliation (design §6).

A publication is one of the fleet's two write shapes, recorded step by step so a crash, a lost
response or a moved PR head never turns into a second copy of the same write:

    author   push → pr_create → marker_check     (a new branch on the fork, then a PR from it)
    fix      push → comment                      (force-with-lease to an own PR branch, then a
    rebase   push → comment                       review-thread reply the fix prompt asks for)

`$TAUCETI_GATE_DIR/publications/<id>.json` holds `{id, kind, pr, repo, remote, branch, head_sha,
steps: [{name, state, remote_id, at, detail, ...}], worker, pid, created_at, parked}`. The round
creates the record before the agent launches and exports the id as TAUCETI_PUBLICATION_ID; the
wrapper scripts (git-safe-push, gh-safe-pr-create, the gh shim's one admitted reply) are the
publisher's steps and drive it through `tauceti-gate publication begin/end`.

A step is `pending` until the publisher is about to send it, `sent` from then until the response was
parsed and the remote id stored (`done`), and `uncertain` when the response was lost, the process
died with it `sent`, or the failure text does not prove nothing landed. `reconcile` resolves
`uncertain` with ONE admitted read per step — the branch tip, the PR carrying this id in a hidden
HTML comment, the reply carrying it — and marks `done` or leaves it `uncertain` and parks the
publication (`needs-reconciliation`), which the fleet view surfaces. Nothing here ever resends: a
publisher asked to run a step that is `sent`, `uncertain` or `done` is refused.

Before each step the publisher revalidates: the lease (`claim.sh holds`, in the scripts) and, for a
fix/rebase, that the PR head still equals what this publication was built on (or the tip our own
push left there), read live through the gate as a reserved `preflight`; a moved head refuses the
step `stale-head` and parks the publication so the round yields. A fix reply whose sanitised digest
equals one already posted on the same head is skipped (`duplicate`), never re-posted.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from . import gate as gate_mod
from .config import Die, log
from .constants import TAUCETI
from .github import gh_run

ID_ENV = "TAUCETI_PUBLICATION_ID"
PUBLICATIONS = "publications"

KIND_AUTHOR = "author"
KIND_FIX = "fix"
KIND_REBASE = "rebase"
KINDS = (KIND_AUTHOR, KIND_FIX, KIND_REBASE)
STEPS = {
    KIND_AUTHOR: ("push", "pr_create", "marker_check"),
    KIND_FIX: ("push", "comment"),
    KIND_REBASE: ("push", "comment"),
}

PENDING = "pending"
SENT = "sent"
DONE = "done"
UNCERTAIN = "uncertain"

# Parked reasons.
NEEDS_RECONCILIATION = "needs-reconciliation"
STALE_HEAD = "stale-head"
INTERRUPTED = "interrupted"

# Refusal reasons a publisher sees (`publication: refused (<reason>)`).
R_STATE = "step-state"  # the step is sent/uncertain/done: never resent
R_PARKED = "parked"
R_STALE_HEAD = STALE_HEAD
R_HEAD_UNREADABLE = "head-unreadable"
R_DUPLICATE = "duplicate"
R_UNKNOWN_STEP = "unknown-step"

MARKER_RE = re.compile(r'<!--tauceti-publication:v1 \{"id":"([A-Za-z0-9._-]+)"\}-->')
TARGET_MARKER_RE = re.compile(r"<!--tauceti-target:v1 \{[^}]*\}-->")
_PR_URL_RE = re.compile(r"/pull/(\d+)")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# git output that PROVES nothing landed (a rejected CAS, a missing ref); anything else is uncertain.
_PUSH_DEFINITE_RE = re.compile(r"\[rejected\]|stale info|failed to push some refs|Permission to .* denied", re.I)
# gh output that proves the create did not happen; a 4xx other than "already exists".
_CREATE_DEFINITE_RE = re.compile(r"HTTP 4\d\d|Validation Failed|Must have|not accessible|No commits between", re.I)
_CREATE_EXISTS_RE = re.compile(r"already exists", re.I)


def marker(pub_id: str) -> str:
    return f'<!--tauceti-publication:v1 {{"id":"{pub_id}"}}-->'


def marker_id(text: str) -> str | None:
    m = MARKER_RE.search(text or "")
    return m.group(1) if m else None


def sanitized_digest(body: str) -> str:
    """The digest a fix reply is de-duplicated on: our markers stripped, whitespace collapsed."""
    t = MARKER_RE.sub("", body or "")
    t = re.sub(r"<!--tauceti-[a-z-]+:[^>]*-->", "", t)
    t = " ".join(t.split()).strip().lower()
    return hashlib.sha256(t.encode()).hexdigest()[:16]


class StepRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"publication: refused ({reason}){f' — {detail}' if detail else ''}")

    def message(self) -> str:
        return f"publication: refused ({self.reason})"


def publications_dir() -> Path | None:
    g = gate_mod.current()
    if not g.enabled or g.dir is None:
        return None
    return g.dir / PUBLICATIONS


def _new_id() -> str:
    wid = os.environ.get("TAUCETI_WORKER_ID") or "w"
    wid = "".join(c if c.isalnum() or c in "-_" else "_" for c in wid)[:24]
    return f"{wid}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{secrets.token_hex(3)}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclasses.dataclass
class Publication:
    id: str
    kind: str
    branch: str
    head_sha: str
    pr: int | None = None
    repo: str = TAUCETI  # the base repository (PR create, PR view, comments)
    remote: str = ""  # the push URL (the fork, or the PR's head repository)
    worker: str = "-"
    pid: int = 0
    created_at: str = ""
    parked: str | None = None
    steps: list[dict] = dataclasses.field(default_factory=list)

    # ---- files --------------------------------------------------------------------------------

    @property
    def path(self) -> Path:
        d = publications_dir()
        if d is None:
            raise Die("publication ledger: the gate is disabled (TAUCETI_GATE_DIR unset)")
        return d / f"{self.id}.json"

    def save(self) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=1, sort_keys=True) + "\n")
        os.replace(tmp, p)

    @classmethod
    def load(cls, pub_id: str) -> Publication:
        d = publications_dir()
        if d is None:
            raise Die("publication ledger: the gate is disabled (TAUCETI_GATE_DIR unset)")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", pub_id or ""):
            raise Die(f"publication id {pub_id!r} is not a ledger id")
        p = d / f"{pub_id}.json"
        try:
            raw = json.loads(p.read_text())
        except FileNotFoundError:
            raise Die(f"publication {pub_id}: no such record under {d}") from None
        except (OSError, ValueError) as e:
            raise Die(f"publication {pub_id}: unreadable ({e})") from None
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in fields})

    @classmethod
    def create(
        cls,
        kind: str,
        *,
        branch: str,
        head_sha: str,
        pr: int | None = None,
        repo: str = TAUCETI,
        remote: str = "",
    ) -> Publication:
        if kind not in KINDS:
            raise Die(f"publication kind {kind!r}: one of {', '.join(KINDS)}")
        pub = cls(
            id=_new_id(),
            kind=kind,
            branch=branch,
            head_sha=head_sha,
            pr=pr,
            repo=gate_mod.normalize_repo(repo) if repo else TAUCETI,
            remote=remote,
            worker=os.environ.get("TAUCETI_WORKER_ID") or "-",
            pid=os.getpid(),
            created_at=_now_iso(),
            steps=[{"name": s, "state": PENDING, "remote_id": "", "at": "", "detail": ""} for s in STEPS[kind]],
        )
        with gate_mod.current().locked():
            pub.save()
        gate_mod.current()._event(decision="publication", op="create", target=pub.repo, kind="-", op_id=pub.id)
        return pub

    # ---- shape ----------------------------------------------------------------------------------

    def step(self, name: str) -> dict:
        for s in self.steps:
            if s["name"] == name:
                return s
        raise StepRefused(R_UNKNOWN_STEP, f"{name!r} is not a step of a {self.kind} publication")

    @property
    def complete(self) -> bool:
        return all(s["state"] == DONE for s in self.steps)

    @property
    def has_uncertain(self) -> bool:
        return any(s["state"] == UNCERTAIN for s in self.steps)

    @property
    def open(self) -> bool:
        return not self.complete and self.parked is None

    def summary(self) -> str:
        steps = " ".join(f"{s['name']}={s['state']}" for s in self.steps)
        where = f"#{self.pr}" if self.pr else self.branch
        return f"{self.id} {self.kind} {where} [{steps}]" + (f" parked={self.parked}" if self.parked else "")

    def _set(self, name: str, state: str, *, sender_pid: int = 0, **fields) -> None:
        s = self.step(name)
        s.update({"state": state, "at": _now_iso(), **fields})
        if state == SENT:
            # The process that will be alive for as long as the write is in flight: the wrapper script
            # (the CLI's parent), not the CLI, which exits as soon as it has marked the step.
            s["pid"] = sender_pid or os.getpid()

    # ---- the publisher's calls ---------------------------------------------------------------------

    def begin(
        self, name: str, *, sha: str = "", body: str | None = None, sender_pid: int = 0, branch: str = ""
    ) -> None:
        """Mark `name` sent, after refusing what must not run: a step already sent/uncertain/done, a
        publication that needs reconciliation, a moved PR head (fix/rebase, one admitted read), and a
        reply whose digest was already posted on this head. `sha` is the local tip a push will send;
        `body` is the reply text a comment will send (its digest is recorded)."""
        s = self.step(name)
        if s["state"] != PENDING:
            raise StepRefused(R_STATE, f"{name} is {s['state']} — a publication step is never resent")
        if self.has_uncertain or self.parked == NEEDS_RECONCILIATION:
            raise StepRefused(R_PARKED, f"{self.id} needs reconciliation (`tauceti-gate reconcile {self.id}`)")
        if self.parked == STALE_HEAD:
            raise StepRefused(R_STALE_HEAD, f"{self.id} was parked on a moved head")
        fields: dict = {}
        if self.kind in (KIND_FIX, KIND_REBASE) and self.pr:
            self._revalidate_head(name)
        if name == "comment" and body is not None:
            digest = sanitized_digest(body)
            dup = _duplicate_comment(self, digest)
            if dup is not None:
                raise StepRefused(R_DUPLICATE, f"the same reply is already posted on this head (comment {dup})")
            fields["digest"] = digest
        if sha:
            fields["sha"] = sha
        with gate_mod.current().locked():
            fresh = Publication.load(self.id)  # a parallel writer (the round's reconcile) may have moved it
            if fresh.step(name)["state"] != PENDING:
                raise StepRefused(R_STATE, f"{name} is {fresh.step(name)['state']}")
            self.steps = fresh.steps
            self.parked = fresh.parked
            if branch and not self.branch:
                self.branch = branch  # an author publication learns its branch from the push
            if self.parked == INTERRUPTED:
                self.parked = None  # resumed under its id by a publisher that holds the agent's content
            self._set(name, SENT, sender_pid=sender_pid, **fields)
            self.save()
        gate_mod.current()._event(
            decision="publication", op=f"{name}:sent", target=self.repo, kind="-", op_id=self.id, detail=sha
        )

    def end(self, name: str, ok: bool, *, remote_id: str = "", detail: str = "") -> str:
        """Record the outcome of a sent step. `ok` with a `remote_id` is `done`; `ok` without one is
        `uncertain` (a lost response: the write may well have happened); a failure is `pending` again
        only when its text proves nothing landed, else `uncertain`. Returns the state written."""
        s = self.step(name)
        if s["state"] == PENDING and name == "marker_check" and ok and remote_id:
            pass  # verified locally by gh-safe-pr-create before the create; no send of its own
        elif s["state"] != SENT:
            raise StepRefused(R_STATE, f"{name} is {s['state']}, not sent")
        if ok and remote_id:
            state = DONE
        elif ok:
            state = UNCERTAIN
        elif name == "push" and _PUSH_DEFINITE_RE.search(detail or ""):
            state = PENDING
        elif name == "pr_create" and _CREATE_DEFINITE_RE.search(detail or "") and not _CREATE_EXISTS_RE.search(detail):
            state = PENDING
        elif name == "comment" and re.search(r"HTTP 4\d\d", detail or ""):
            state = PENDING
        else:
            state = UNCERTAIN
        from .identity import sanitize

        with gate_mod.current().locked():
            self._set(name, state, remote_id=remote_id if state == DONE else "", detail=sanitize(detail or ""))
            if state == UNCERTAIN:
                self.parked = NEEDS_RECONCILIATION
            self.save()
        gate_mod.current()._event(
            decision="publication",
            op=f"{name}:{state}",
            target=self.repo,
            kind="-",
            op_id=self.id,
            detail=remote_id or detail,
        )
        return state

    def _expected_head(self) -> str:
        push = self.step("push")
        if push["state"] == DONE and push.get("remote_id"):
            return str(push["remote_id"])
        return self.head_sha

    def _revalidate_head(self, name: str) -> None:
        expected = self._expected_head()
        p = gh_run(
            ["gh", "pr", "view", str(self.pr), "--repo", self.repo, "--json", "headRefOid"],
            gate_op="preflight",
            max_wait=0,
        )
        if p.returncode != 0:
            raise StepRefused(R_HEAD_UNREADABLE, f"could not read #{self.pr}'s head before {name}")
        try:
            live = str(json.loads(p.stdout or "{}").get("headRefOid") or "")
        except ValueError:
            raise StepRefused(R_HEAD_UNREADABLE, f"#{self.pr}: malformed head response") from None
        if live != expected:
            with gate_mod.current().locked():
                self.parked = STALE_HEAD
                self.save()
            gate_mod.current()._event(
                decision="publication",
                op=f"{name}:{STALE_HEAD}",
                target=self.repo,
                kind="-",
                op_id=self.id,
                detail=f"expected {expected[:12]} live {live[:12]}",
            )
            raise StepRefused(R_STALE_HEAD, f"#{self.pr} is at {live[:12]}, this publication expects {expected[:12]}")

    # ---- reconciliation --------------------------------------------------------------------------

    def mark_stale_sent(self) -> bool:
        """A `sent` step whose sender is gone is `uncertain` (design §6: on restart, sent without done
        is uncertain). Returns whether anything changed."""
        changed = False
        for s in self.steps:
            pid = s.get("pid") if isinstance(s.get("pid"), int) else self.pid
            if s["state"] == SENT and not (pid and gate_mod._pid_alive(pid)):
                s["state"] = UNCERTAIN
                s["at"] = _now_iso()
                changed = True
        if changed:
            self.parked = NEEDS_RECONCILIATION
        return changed

    def reconcile(self) -> dict:
        """Resolve every uncertain step with one admitted read each. Returns {step: verdict}."""
        out: dict[str, str] = {}
        body_by_pr: dict[int, str] = {}
        for s in self.steps:
            name = s["name"]
            if s["state"] != UNCERTAIN and not (
                # marker_check has no send of its own: it is settled by the PR body the create step's
                # reconciliation just fetched (no extra read), whatever state it was left in.
                name == "marker_check" and s["state"] == PENDING and out.get("pr_create") == DONE
            ):
                continue
            verdict, remote_id, detail = "uncertain", "", ""
            if name == "push":
                verdict, remote_id, detail = self._reconcile_push(s)
            elif name == "pr_create":
                verdict, remote_id, detail, body = self._reconcile_pr_create()
                if verdict == DONE and body is not None:
                    body_by_pr[int(remote_id)] = body
            elif name == "marker_check":
                verdict, remote_id, detail = self._reconcile_marker(body_by_pr)
            elif name == "comment":
                verdict, remote_id, detail = self._reconcile_comment()
            out[name] = verdict
            if verdict == DONE:
                s.update({"state": DONE, "remote_id": remote_id, "at": _now_iso(), "detail": detail})
                if name == "pr_create" and self.pr is None and remote_id.isdigit():
                    self.pr = int(remote_id)
            else:
                s["detail"] = detail
        # A step left uncertain keeps the publication parked; nothing uncertain and nothing pending
        # after a done step means it can go on (resumed by whoever holds the id) — but an author
        # publication whose remaining steps need the agent that is gone is parked `interrupted`.
        if self.has_uncertain:
            self.parked = NEEDS_RECONCILIATION
        elif self.parked == NEEDS_RECONCILIATION:
            self.parked = None
        if self.parked is None and not self.complete and self.pid and not gate_mod._pid_alive(self.pid):
            if self.pid != os.getpid():
                self.parked = INTERRUPTED
        with gate_mod.current().locked():
            self.save()
        gate_mod.current()._event(
            decision="publication",
            op="reconcile",
            target=self.repo,
            kind="-",
            op_id=self.id,
            detail=" ".join(f"{k}={v}" for k, v in out.items()) or "nothing uncertain",
        )
        return out

    def _reconcile_push(self, s: dict) -> tuple[str, str, str]:
        want = str(s.get("sha") or (self.head_sha if self.kind == KIND_AUTHOR else ""))
        if not self.remote or not want:
            return UNCERTAIN, "", "no remote or no local tip recorded"
        p = gate_mod.gated_git(
            ["git", "ls-remote", self.remote, f"refs/heads/{self.branch}"],
            op="reconcile",
            target=self.remote,
            capture_output=True,
        )
        if p.returncode != 0:
            return UNCERTAIN, "", f"ls-remote failed: {(p.stderr or '').strip()[:120]}"
        tip = ""
        for line in (p.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == f"refs/heads/{self.branch}":
                tip = parts[0]
        if tip and tip == want:
            return DONE, tip, "branch tip matches"
        if not tip:
            return UNCERTAIN, "", "branch absent on the remote (the push did not land, or was pruned)"
        return UNCERTAIN, "", f"branch tip {tip[:12]} != pushed {want[:12]}"

    def _reconcile_pr_create(self) -> tuple[str, str, str, str | None]:
        p = gh_run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo,
                "--head",
                self.branch,
                "--state",
                "all",
                "--json",
                "number,body,headRefName",
            ],
            gate_op="reconcile",
            max_wait=0,
        )
        if p.returncode != 0:
            return UNCERTAIN, "", f"pr list failed: {(p.stderr or '').strip()[:120]}", None
        try:
            prs = json.loads(p.stdout or "[]")
        except ValueError:
            return UNCERTAIN, "", "pr list: malformed response", None
        mine = [x for x in prs if isinstance(x, dict) and marker_id(x.get("body") or "") == self.id]
        if len(mine) == 1:
            return DONE, str(mine[0].get("number")), "found by publication marker", mine[0].get("body") or ""
        if len(mine) > 1:
            return UNCERTAIN, "", f"{len(mine)} PRs carry this publication id", None
        if prs:
            return UNCERTAIN, "", f"{len(prs)} PR(s) from {self.branch} carry no publication marker", None
        return UNCERTAIN, "", "no PR from this branch", None

    def _reconcile_marker(self, body_by_pr: dict[int, str]) -> tuple[str, str, str]:
        pr = self.pr or (int(self.step("pr_create")["remote_id"]) if self.step("pr_create")["remote_id"] else None)
        if pr is None:
            return UNCERTAIN, "", "no PR to check"
        body = body_by_pr.get(pr)
        if body is None:
            p = gh_run(
                ["gh", "pr", "view", str(pr), "--repo", self.repo, "--json", "body"], gate_op="reconcile", max_wait=0
            )
            if p.returncode != 0:
                return UNCERTAIN, "", "pr view failed"
            try:
                body = str(json.loads(p.stdout or "{}").get("body") or "")
            except ValueError:
                return UNCERTAIN, "", "pr view: malformed response"
        if TARGET_MARKER_RE.search(body) and marker_id(body) == self.id:
            return DONE, str(pr), "target and publication markers present"
        return UNCERTAIN, "", "PR body lacks the target marker or this publication's marker"

    def _reconcile_comment(self) -> tuple[str, str, str]:
        if not self.pr:
            return UNCERTAIN, "", "no PR"
        p = gh_run(
            ["gh", "api", "--paginate", f"/repos/{self.repo}/pulls/{self.pr}/comments?per_page=100"],
            gate_op="reconcile",
            max_wait=0,
        )
        if p.returncode != 0:
            return UNCERTAIN, "", f"comments fetch failed: {(p.stderr or '').strip()[:120]}"
        try:
            cs = json.loads(p.stdout or "[]")
        except ValueError:
            return UNCERTAIN, "", "comments: malformed response"
        mine = [c for c in cs if isinstance(c, dict) and marker_id(c.get("body") or "") == self.id]
        if len(mine) == 1:
            return DONE, str(mine[0].get("id")), "found by publication marker"
        if mine:
            return UNCERTAIN, "", f"{len(mine)} comments carry this publication id"
        return UNCERTAIN, "", "no comment carries this publication id"


# ---- the ledger as a whole ----------------------------------------------------------------------------


def _duplicate_comment(pub: Publication, digest: str) -> str | None:
    """The remote id of a done comment with this digest on the same PR and head, if any."""
    for other in list_all():
        if other.id == pub.id or other.pr != pub.pr or other.head_sha != pub.head_sha:
            continue
        for s in other.steps:
            if s["name"] == "comment" and s["state"] == DONE and s.get("digest") == digest:
                return str(s.get("remote_id") or "?")
    return None


def list_all() -> list[Publication]:
    d = publications_dir()
    if d is None:
        return []
    out = []
    try:
        paths = sorted(d.glob("*.json"))
    except OSError:
        return []
    for p in paths:
        try:
            out.append(Publication.load(p.stem))
        except Die:
            continue
    return out


def queue_summary() -> dict:
    """What `status`/`report` print: open (in-progress) publications, and the parked ones by reason."""
    pubs = list_all()
    open_ = [p for p in pubs if p.open]
    parked = [p for p in pubs if p.parked]
    return {
        "queue_depth": len(open_),
        "open": [p.summary() for p in open_],
        "parked": [p.summary() for p in parked],
        "uncertain_steps": sum(1 for p in pubs for s in p.steps if s["state"] == UNCERTAIN),
        "complete": sum(1 for p in pubs if p.complete),
    }


def reconcile_stale(worker: str | None = None, *, everything: bool = False) -> list[tuple[str, dict]]:
    """Round start (design §6): every publication of this worker whose sender is gone has its sent steps
    marked uncertain and is reconciled before new work. `everything` (the CLI's --all) also reconciles
    other workers' parked publications."""
    out = []
    for pub in list_all():
        if pub.complete:
            continue
        if not everything and worker and pub.worker != worker:
            continue
        if pub.pid and gate_mod._pid_alive(pub.pid):
            continue  # a live round's own in-progress publication (this one's, or a sibling's)
        with gate_mod.current().locked():
            fresh = Publication.load(pub.id)
            changed = fresh.mark_stale_sent()
            if changed:
                fresh.save()
        if fresh.has_uncertain:
            out.append((fresh.id, fresh.reconcile()))
        elif fresh.parked is None and fresh.pid and not gate_mod._pid_alive(fresh.pid):
            with gate_mod.current().locked():
                fresh.parked = INTERRUPTED
                fresh.save()
            out.append((fresh.id, {"parked": INTERRUPTED}))
    return out


def create_for_round(kind: str, **kw) -> str:
    """Create the round's publication and export its id for the agent's scripts. Returns "" when the
    gate is disabled (the scripts then run as before, unrecorded)."""
    if publications_dir() is None:
        os.environ.pop(ID_ENV, None)
        return ""
    pub = Publication.create(kind, **kw)
    os.environ[ID_ENV] = pub.id
    log(f"  publication {pub.id}: {kind} {kw.get('branch', '')} @ {str(kw.get('head_sha', ''))[:12]}")
    return pub.id


def round_summary(pub_id: str) -> str | None:
    """One line for the round log after the agent ran: what landed, what did not."""
    if not pub_id or publications_dir() is None:
        return None
    try:
        return Publication.load(pub_id).summary()
    except Die:
        return None

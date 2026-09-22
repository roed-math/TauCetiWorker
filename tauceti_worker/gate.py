"""tauceti_worker.gate — the one shared boundary every GitHub operation of the fleet passes through.

There is no daemon. The store is a directory of small JSON files under one flock, so seven workers,
the fleet view, an agent's `gh`/`git` shim and an interactive session share ONE allowance without a
service to keep alive, and a restart neither resets nor doubles anything: the budgets are rolling
windows of timestamps, the cooldown and the halt are files.

    admit(op, target, kind) -> Admission      (or raises GateRefused)
    record(admission, outcome)                (release, classify, transition)

`kind` is one of `api_read`, `api_mutation`, `git_read`, `git_push` (each with its own accounting; a
git push is never a REST call) or `identity` — the one `gh api user` the identity gate spends, exempt
from the budgets because it is what decides whether there is an account to budget for.

Decisions, in order (design §3): halted → cooldown → disabled marker → login pin → quarantine →
target allowlist → concurrency (wait, then `busy`) → budgets (`unconfigured` when a per-hour cap is
not set: the brief forbids silently defaulting to unlimited). A refusal names its reason and, when
there is one, the time a slot frees; the refused operation stays with its caller, which declines
through its existing no-progress path. Nothing here ever raises a cap, switches identity, or queues
a catch-up burst.

`record` classifies the outcome (design §3 table): 401 / bad credentials and a suspension-shaped 403
halt the whole fleet for good (`HALTED_MANUAL`, plus a `halt.json` in the same shape the identity
gate writes); a confirmed permission denial quarantines that one (op, target); an unclassified 403
pauses everything for investigation; a rate limit enters a shared cooldown that honours Retry-After,
then x-ratelimit-reset, then doubles from 60 s to 15 min; three consecutive 5xx on one op quarantine
it for 30 min. A cooldown expires on its own; HALTED_MANUAL clears only through `--clear-halt`.

`Gate.disabled()` is the no-op used when TAUCETI_GATE_DIR is unset and TAUCETI_GATE_REQUIRED is not
`1`: an operator without the gate keeps upstream's behaviour. The fleet wrapper sets both, so a
missing directory there is a hard error rather than a silent bypass.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from .config import Die, log
from .constants import _GH_PRIMARY_RE, _GH_SECONDARY_RE, _GH_TRANSIENT_RE, CLAIMS, TAUCETI
from .identity import _HALT_WORDS_RE, _INVALID_RE, ACCOUNT_HALT, INVALID_CREDENTIALS, IdentityCheck, sanitize

# ---- names --------------------------------------------------------------------------------------------

DIR_ENV = "TAUCETI_GATE_DIR"
REQUIRED_ENV = "TAUCETI_GATE_REQUIRED"
TOKEN_ENV = "TAUCETI_GATE_TOKEN"  # an admission handed down to a child script, so it does not admit again
GIT_OP_ENV = "TAUCETI_GIT_OP"  # the kind a git credential request belongs to (set by the shim/wrappers)

RUNNING = "RUNNING"
COOLDOWN = "COOLDOWN"
HALTED_MANUAL = "HALTED_MANUAL"

API_READ = "api_read"
API_MUTATION = "api_mutation"
GIT_READ = "git_read"
GIT_PUSH = "git_push"
IDENTITY = "identity"
KINDS = (API_READ, API_MUTATION, GIT_READ, GIT_PUSH, IDENTITY)

# Refusal reasons (the CLI prints them, the tests assert on them, docs/gate.md lists them).
R_HALTED = "halted"
R_COOLDOWN = "cooldown"
R_DISABLED = "disabled"
R_LOGIN = "login"
R_QUARANTINED = "quarantined"
R_TARGET = "target"
R_BUSY = "busy"
R_BUDGET = "budget"
R_SPACING = "spacing"
R_UNCONFIGURED = "unconfigured"
R_STORE = "store-error"
R_KIND = "kind"

REFUSED_RC = 75  # EX_NOPROGRESS: a refused call reads, to every existing caller, as "no progress"

LOCK = "lock"
STATE = "state.json"
BUDGET = "budget.json"
QUARANTINE = "quarantine.json"
EVENTS = "events.log"
HALT = "halt.json"
DISABLED = "disabled"
SPAWNS = "spawns.log"  # one line per real `gh` the shim spawned; `report` matches them against events
CACHE = "cache"  # the fleet-shared read cache (design §5); review_state/ sidecars and inflight/ markers
INFLIGHT = "inflight"
READ_INFLIGHT_TTL = 20  # seconds a miss marker is honoured: a reader waits at most this long for a peer's fetch

# Ops that may draw on the reads reserve (publication preflight and reconciliation).
RESERVED_OPS = frozenset({"preflight", "reconcile", "rate_limit"})

_PERMISSION_RE = re.compile(
    r"Resource not accessible|Must have push access|remote rejected.*permission|Permission to .* denied|"
    r"Write access to repository not granted|protected branch",
    re.I,
)
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_RETRY_AFTER_RE = re.compile(r"retry[- ]after:?\s*(\d+)", re.I)
_RATE_REMAINING_RE = re.compile(r"x-ratelimit-remaining:?\s*(\d+)", re.I)
_RATE_RESET_RE = re.compile(r"x-ratelimit-reset:?\s*(\d+)", re.I)
_REQUEST_ID_RE = re.compile(r"x-github-request-id:?\s*([A-Z0-9:]+)", re.I)


def _env_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise Die(f"{name}={raw!r}: not an integer") from None


@dataclasses.dataclass(frozen=True)
class GateConfig:
    """Every knob, from the environment, with the design's pilot defaults. The two per-hour API caps have
    NO default: unset, the gate refuses `api_read`/`api_mutation` with `unconfigured`."""

    mutations_per_hour: int | None
    reads_per_hour: int | None
    mutations_per_minute: int = 20
    reads_per_minute: int = 100  # a cold survey is ~210 per-PR reads; spread it over ~2 min, not 1
    mutation_spacing: int = 5
    reads_reserve: int = 200
    pushes_per_minute: int = 4
    pushes_per_hour: int = 60
    git_reads_per_hour: int = 300
    max_inflight_api: int = 1
    max_inflight_git: int = 1
    admit_wait: int = 30
    inflight_deadline: int = 900
    cooldown_max: int = 900
    cooldown_base: int = 60
    unclassified_403_cooldown: int = 900
    transient_max: int = 3
    transient_quarantine: int = 1800

    @classmethod
    def from_env(cls) -> GateConfig:
        d = cls.__dataclass_fields__
        return cls(
            mutations_per_hour=_env_int("TAUCETI_GATE_MUTATIONS_PER_HOUR", None),
            reads_per_hour=_env_int("TAUCETI_GATE_READS_PER_HOUR", None),
            mutations_per_minute=_env_int("TAUCETI_GATE_MUTATIONS_PER_MINUTE", d["mutations_per_minute"].default),
            reads_per_minute=_env_int("TAUCETI_GATE_READS_PER_MINUTE", d["reads_per_minute"].default),
            mutation_spacing=_env_int("TAUCETI_GATE_MUTATION_SPACING", d["mutation_spacing"].default),
            reads_reserve=_env_int("TAUCETI_GATE_READS_RESERVE", d["reads_reserve"].default),
            pushes_per_minute=_env_int("TAUCETI_GATE_PUSHES_PER_MINUTE", d["pushes_per_minute"].default),
            pushes_per_hour=_env_int("TAUCETI_GATE_PUSHES_PER_HOUR", d["pushes_per_hour"].default),
            git_reads_per_hour=_env_int("TAUCETI_GATE_GIT_READS_PER_HOUR", d["git_reads_per_hour"].default),
            max_inflight_api=_env_int("TAUCETI_GATE_MAX_INFLIGHT_API", d["max_inflight_api"].default),
            max_inflight_git=_env_int("TAUCETI_GATE_MAX_INFLIGHT_GIT", d["max_inflight_git"].default),
            admit_wait=_env_int("TAUCETI_GATE_ADMIT_WAIT", d["admit_wait"].default),
            inflight_deadline=_env_int("TAUCETI_GATE_INFLIGHT_DEADLINE", d["inflight_deadline"].default),
            cooldown_max=_env_int("TAUCETI_GATE_COOLDOWN_MAX", d["cooldown_max"].default),
            cooldown_base=_env_int("TAUCETI_GATE_COOLDOWN_BASE", d["cooldown_base"].default),
            unclassified_403_cooldown=_env_int(
                "TAUCETI_GATE_UNCLASSIFIED_403_COOLDOWN", d["unclassified_403_cooldown"].default
            ),
            transient_max=_env_int("TAUCETI_GATE_TRANSIENT_MAX", d["transient_max"].default),
            transient_quarantine=_env_int("TAUCETI_GATE_TRANSIENT_QUARANTINE", d["transient_quarantine"].default),
        )


class GateRefused(Exception):
    """The gate did not admit the operation. `reason` is one of the R_* constants; `until` (epoch
    seconds) is when a slot may free, when that is knowable (cooldown, budget, spacing, quarantine)."""

    def __init__(self, reason: str, until: float | None = None, detail: str = "", *, logged: bool = False):
        self.reason = reason
        self.until = until
        self.detail = detail
        self.logged = logged  # an event was already written for it (see Gate._refuse)
        super().__init__(f"gate: refused ({reason}){f' — {detail}' if detail else ''}")

    def message(self) -> str:
        """The one line every wrapper prints and every test greps for."""
        return f"gate: refused ({self.reason})"


@dataclasses.dataclass(frozen=True)
class Admission:
    token: str
    op: str
    target: str
    kind: str
    at: float
    weight: int = 1
    disabled: bool = False  # from Gate.disabled(): nothing was registered, record() is a no-op

    def child_env(self) -> dict[str, str]:
        """What a child script that performs this admitted operation must carry so it neither admits
        again nor counts twice: the token, and the git kind for the credential helper."""
        if self.disabled:
            return {}
        env = {TOKEN_ENV: self.token}
        if self.kind in (GIT_READ, GIT_PUSH):
            env[GIT_OP_ENV] = self.kind
        return env


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What came back. `status` is the HTTP status when known; gh does not always echo one, so `text`
    (stderr + stdout) is classified on its own when it is None. `ok` is the caller's verdict when the
    status says nothing (a git push that succeeded, a claim.sh exit 1 that is a lost race, not an
    error)."""

    ok: bool
    status: int | None = None
    text: str = ""
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    duration_ms: int | None = None
    retry_n: int = 0

    @classmethod
    def from_process(cls, p, *, ok: bool | None = None, duration_ms: int | None = None, retry_n: int = 0) -> Outcome:
        text = (getattr(p, "stderr", "") or "") + "\n" + (getattr(p, "stdout", "") or "")
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        rc = getattr(p, "returncode", 1)
        return cls(
            ok=(rc == 0) if ok is None else ok,
            status=parse_status(text),
            text=text,
            duration_ms=duration_ms,
            retry_n=retry_n,
        )


def parse_status(text: str) -> int | None:
    m = _HTTP_STATUS_RE.search(text or "")
    return int(m.group(1)) if m else None


def _header(outcome: Outcome, name: str, regex: re.Pattern) -> str | None:
    for k, v in outcome.headers.items():
        if k.lower() == name:
            return str(v)
    m = regex.search(outcome.text or "")
    return m.group(1) if m else None


def normalize_repo(target: str) -> str:
    """`owner/repo` in lower case from any of the spellings a shell, a config file or a remote URL
    produces. Non-repo targets (`-`, `user`, a bare word) come back lower-cased and otherwise as given."""
    r = (target or "").strip().lower()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/"):
        r = r.removeprefix(prefix)
    r = r.strip("/").removesuffix(".git").strip("/")
    return r


def _is_repo(target: str) -> bool:
    n = normalize_repo(target)
    return n.count("/") == 1 and all(n.split("/"))


def api_target_allowed(target: str) -> bool:
    """The API allowlist (design §3.4): the org, the worker's own repository, the operator's fork, the
    claims namespace; account-scoped endpoints (`user`, `rate_limit`, invitations) are not repositories
    and pass. Reads of the operator's own namespace pass too: fork resolution asks `repos/<me>/<name>`
    before it knows the fork's name (a deviation from the design, confined to the login's own repos)."""
    if not _is_repo(target):
        return True
    n = normalize_repo(target)
    owner = n.split("/", 1)[0]
    if owner == TAUCETI.split("/", 1)[0].lower():
        return True
    if n == "kim-em/taucetiworker":
        return True
    for env in ("TAUCETI_FORK", "CLAIM_REPO", "TAUCETI_CLAIM_REPO"):
        v = os.environ.get(env, "").strip()
        if v and normalize_repo(v) == n:
            return True
    if n == CLAIMS.lower():
        return True
    login = os.environ.get("TAUCETI_IDENTITY_OK", "").strip().lower()
    return bool(login) and owner == login


def api_write_target_allowed(target: str) -> bool:
    """Mutations get the allowlist without the own-namespace read extension."""
    if not _is_repo(target):
        return True
    n = normalize_repo(target)
    owner = n.split("/", 1)[0]
    if owner == TAUCETI.split("/", 1)[0].lower() or n == "kim-em/taucetiworker" or n == CLAIMS.lower():
        return True
    for env in ("TAUCETI_FORK", "CLAIM_REPO", "TAUCETI_CLAIM_REPO"):
        v = os.environ.get(env, "").strip()
        if v and normalize_repo(v) == n:
            return True
    return False


def push_target_allowed(op: str, target: str) -> bool:
    """The push allowlist: the fork, the claims namespace, and the head repository the round was given
    (`TAUCETI_PUSH_REMOTE`, op `push` only). Canonical is never a claim target, whatever the env says."""
    if not _is_repo(target):
        return False
    n = normalize_repo(target)
    canonical = n == TAUCETI.lower()
    if op in ("acquire", "renew", "release", "gc", "claim") and canonical:
        return False
    for env in ("TAUCETI_FORK", "CLAIM_REPO", "TAUCETI_CLAIM_REPO"):
        v = os.environ.get(env, "").strip()
        if v and normalize_repo(v) == n and not (canonical and env != "TAUCETI_FORK"):
            return True
    if n == CLAIMS.lower():
        return True
    if op == "sync" and n == f"{TAUCETI.split('/', 1)[0].lower()}/taucetidata":
        return True  # the review engine's archive push (--sync-only), probed for push permission first
    # The curator's one push: the repository that holds the operator's target list, named by the
    # operator (`TAUCETI_TARGETS_REPO`); never canonical, never any other op.
    if op == "curate":
        v = os.environ.get("TAUCETI_TARGETS_REPO", "").strip()
        return bool(v) and normalize_repo(v) == n and not canonical
    remote = os.environ.get("TAUCETI_PUSH_REMOTE", "").strip()
    return bool(remote) and normalize_repo(remote) == n and op == "push"


def _now_iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


class StoreError(Exception):
    """The store cannot be read or written. Every remote admit fails closed on it (`store-error`)."""


class Gate:
    """One instance per process; every method takes and releases the store lock itself."""

    def __init__(self, dir: Path | None, *, config: GateConfig | None = None, now: Callable[[], float] = time.time):
        self.dir = Path(dir) if dir is not None else None
        self.config = config or GateConfig.from_env()
        self.now = now
        self._lock_fd: int | None = None

    # ---- construction --------------------------------------------------------------------------

    @classmethod
    def from_env(cls, **kw) -> Gate:
        """The gate the environment names. Unset and not required → the no-op; required and unset →
        Die, since the fleet wrapper always sets both and a missing directory there is a bypass."""
        d = os.environ.get(DIR_ENV, "").strip()
        required = os.environ.get(REQUIRED_ENV, "").strip() == "1"
        if not d:
            if required:
                raise Die(f"{REQUIRED_ENV}=1 but {DIR_ENV} is unset — refusing to run without the GitHub gate")
            return cls.disabled()
        return cls(Path(d).expanduser(), **kw)

    @classmethod
    def disabled(cls) -> Gate:
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    # ---- the lock and the files ----------------------------------------------------------------

    def _path(self, name: str) -> Path:
        assert self.dir is not None
        return self.dir / name

    def _acquire(self) -> None:
        assert self.dir is not None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path(LOCK), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            raise StoreError(f"{self.dir}: {e}") from e
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            os.close(fd)
            raise StoreError(f"{self._path(LOCK)}: {e}") from e
        self._lock_fd = fd

    def _release(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _read(self, name: str, default):
        """A missing file is its default; an unreadable or corrupt one is a StoreError — the brief's T16:
        unreadable budget state fails closed rather than starting a fresh allowance."""
        p = self._path(name)
        try:
            raw = p.read_text()
        except FileNotFoundError:
            return default
        except OSError as e:
            raise StoreError(f"{p}: {e}") from e
        try:
            v = json.loads(raw)
        except ValueError as e:
            raise StoreError(f"{p}: corrupt ({e})") from e
        return v if isinstance(v, type(default)) else default

    def _write(self, name: str, value) -> None:
        p = self._path(name)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")
            os.replace(tmp, p)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise StoreError(f"{p}: {e}") from e

    def _event(self, **fields) -> None:
        rec = {"ts": _now_iso(self.now()), "tz": "UTC", "caller": caller_label(), **fields}
        if "detail" in rec and rec["detail"]:
            rec["detail"] = sanitize(str(rec["detail"]))
        try:
            with open(self._path(EVENTS), "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError as e:
            raise StoreError(f"{self._path(EVENTS)}: {e}") from e

    # ---- state helpers (call with the lock held) -------------------------------------------------

    def _state(self) -> dict:
        s = self._read(STATE, {})
        s.setdefault("state", RUNNING)
        return s

    def _set_state(self, s: dict, state: str, *, reason: str = "", detail: str = "", until: float | None = None):
        prev = s.get("state", RUNNING)
        s.update(
            {
                "state": state,
                "since": self.now(),
                "until": until,
                "reason": reason,
                "detail": sanitize(detail),
                "source": caller_label(),
            }
        )
        self._write(STATE, s)
        self._event(
            decision="transition", kind="-", op="-", target="-", reason=reason, detail=detail, from_=prev, to=state
        )

    def _halted(self, s: dict) -> bool:
        return s.get("state") == HALTED_MANUAL or self._path(HALT).exists()

    def _budget(self) -> dict:
        b = self._read(BUDGET, {})
        b.setdefault("windows", {})
        b.setdefault("inflight", [])
        b.setdefault("transient", {})
        return b

    def _prune(self, b: dict) -> None:
        cutoff = self.now() - 3600
        for k, ts in list(b["windows"].items()):
            kept = [t for t in ts if t > cutoff]
            if kept:
                b["windows"][k] = kept
            else:
                del b["windows"][k]
        live = []
        for e in b["inflight"]:
            if e.get("deadline", 0) < self.now():
                self._event(
                    decision="reaped",
                    reason="deadline",
                    **{k: e.get(k) for k in ("op", "target", "kind")},
                    op_id=e.get("token"),
                )
                continue
            pid = e.get("pid")
            if pid and not _pid_alive(int(pid)):
                self._event(
                    decision="reaped",
                    reason="owner-dead",
                    **{k: e.get(k) for k in ("op", "target", "kind")},
                    op_id=e.get("token"),
                )
                continue
            live.append(e)
        b["inflight"] = live

    @staticmethod
    def _count(b: dict, key: str, window: int, now: float) -> int:
        return sum(1 for t in b["windows"].get(key, []) if t > now - window)

    @staticmethod
    def _earliest_free(b: dict, key: str, window: int, cap: int, now: float, weight: int) -> float:
        """When the oldest timestamps in the window age out far enough for `weight` more to fit."""
        ts = sorted(t for t in b["windows"].get(key, []) if t > now - window)
        need = len(ts) + weight - cap
        if need <= 0:
            return now
        return ts[need - 1] + window + 0.01

    # ---- admission ------------------------------------------------------------------------------

    def admit(
        self, op: str, target: str, kind: str, *, wait: bool = True, weight: int = 1, reserved: bool = False
    ) -> Admission:
        """Admit one operation or raise GateRefused. With `wait`, a busy slot, the mutation spacing and
        a per-minute cap are waited out for up to `admit_wait` seconds (polling with the lock released);
        an hourly cap, a cooldown, a halt, a quarantine and a bad target are refused at once."""
        if kind not in KINDS:
            raise GateRefused(R_KIND, detail=f"unknown kind {kind!r}")
        if not self.enabled:
            return Admission("disabled", op, target, kind, self.now(), weight, disabled=True)
        deadline = self.now() + (self.config.admit_wait if wait else 0)
        while True:
            try:
                self._acquire()
                try:
                    return self._admit_locked(op, target, kind, weight, reserved)
                finally:
                    self._release()
            except StoreError as e:
                self._refuse_unlogged(op, target, kind, R_STORE, str(e))
            except GateRefused as e:
                waitable = e.reason in (R_BUSY, R_SPACING, R_BUDGET)
                if (
                    not waitable
                    or (e.until is not None and e.until > deadline)
                    or self.now() >= deadline
                    or (e.reason == R_BUDGET and e.until is None)
                ):
                    # Final. The waitable reasons are raised without an event (a wait polls many
                    # times); write theirs once, here.
                    if not e.logged:
                        self._log_final_refusal(op, target, kind, e)
                    raise
                time.sleep(min(0.25, max(0.02, (e.until or self.now() + 0.25) - self.now())))

    def _log_final_refusal(self, op: str, target: str, kind: str, e: GateRefused) -> None:
        try:
            self._acquire()
            try:
                self._event(
                    decision="refuse", op=op, target=target, kind=kind, reason=e.reason, until=e.until, detail=e.detail
                )
            finally:
                self._release()
        except StoreError:
            pass

    def _refuse_unlogged(self, op, target, kind, reason, detail) -> None:
        # The store is what failed, so the refusal cannot be written there; say it on the log instead.
        log(f"gate: refused ({reason}) {kind} {op} {target}: {detail}")
        raise GateRefused(reason, detail=detail)

    def _refuse(self, op, target, kind, reason, *, until=None, detail="") -> None:
        self._event(decision="refuse", op=op, target=target, kind=kind, reason=reason, until=until, detail=detail)
        raise GateRefused(reason, until, detail, logged=True)

    def _admit_locked(self, op: str, target: str, kind: str, weight: int, reserved: bool) -> Admission:
        now = self.now()
        cfg = self.config
        s = self._state()
        # 1. halted
        if self._halted(s):
            self._refuse(op, target, kind, R_HALTED, detail=s.get("reason") or "halt.json present")
        # 2. cooldown
        if s.get("state") == COOLDOWN:
            until = float(s.get("until") or 0)
            if now < until:
                self._refuse(op, target, kind, R_COOLDOWN, until=until, detail=s.get("reason", ""))
            self._set_state(s, RUNNING, reason="cooldown-expired")
        # disabled marker (offline / operator `tauceti-gate disable`)
        if self._path(DISABLED).exists():
            self._refuse(op, target, kind, R_DISABLED, detail="disabled marker present")
        # login pin
        login = os.environ.get("TAUCETI_IDENTITY_OK", "").strip()
        if login and kind != IDENTITY:
            pinned = s.get("login")
            if pinned and pinned.lower() != login.lower():
                self._refuse(
                    op, target, kind, R_LOGIN, detail=f"store is pinned to {pinned}, this process acts as {login}"
                )
            if not pinned:
                s["login"] = login
                self._write(STATE, s)
        # 3. quarantine
        q = self._read(QUARANTINE, {})
        key = f"{op}:{normalize_repo(target)}"
        entry = q.get(key)
        if entry:
            until = entry.get("until")
            if until is None or now < float(until):
                self._refuse(op, target, kind, R_QUARANTINED, until=until, detail=entry.get("reason", ""))
            del q[key]
            self._write(QUARANTINE, q)
        # 4. targets
        if kind == API_READ and not api_target_allowed(target):
            self._refuse(op, target, kind, R_TARGET, detail="not an allowlisted API target")
        if kind == API_MUTATION and not api_write_target_allowed(target):
            self._refuse(op, target, kind, R_TARGET, detail="not an allowlisted API write target")
        if kind == GIT_PUSH and not push_target_allowed(op, target):
            self._refuse(op, target, kind, R_TARGET, detail="not an allowlisted push target")
        # 5. concurrency
        b = self._budget()
        self._prune(b)
        lane = "git" if kind in (GIT_READ, GIT_PUSH) else "api"
        cap = cfg.max_inflight_git if lane == "git" else cfg.max_inflight_api
        busy = [e for e in b["inflight"] if e.get("lane") == lane]
        if len(busy) >= cap:
            self._write(BUDGET, b)  # persist the reaping
            raise GateRefused(R_BUSY, detail=f"{len(busy)} in flight on the {lane} lane")
        # 6. budgets
        if kind == API_MUTATION:
            if cfg.mutations_per_hour is None:
                self._refuse(op, target, kind, R_UNCONFIGURED, detail="TAUCETI_GATE_MUTATIONS_PER_HOUR is unset")
            last = b.get("last_mutation_at")
            if last is not None and now - float(last) < cfg.mutation_spacing:
                raise GateRefused(R_SPACING, float(last) + cfg.mutation_spacing)
            self._check_window(b, op, target, kind, API_MUTATION, 60, cfg.mutations_per_minute, weight, now)
            self._check_window(b, op, target, kind, API_MUTATION, 3600, cfg.mutations_per_hour, weight, now)
        elif kind == API_READ:
            if cfg.reads_per_hour is None:
                self._refuse(op, target, kind, R_UNCONFIGURED, detail="TAUCETI_GATE_READS_PER_HOUR is unset")
            cap_h = (
                cfg.reads_per_hour
                if (reserved or op in RESERVED_OPS)
                else max(0, cfg.reads_per_hour - cfg.reads_reserve)
            )
            self._check_window(b, op, target, kind, API_READ, 60, cfg.reads_per_minute, weight, now)
            self._check_window(b, op, target, kind, API_READ, 3600, cap_h, weight, now)
        elif kind == GIT_PUSH:
            wkey = f"{GIT_PUSH}:{normalize_repo(target)}"
            self._check_window(b, op, target, kind, wkey, 60, cfg.pushes_per_minute, weight, now)
            self._check_window(b, op, target, kind, wkey, 3600, cfg.pushes_per_hour, weight, now)
        elif kind == GIT_READ:
            self._check_window(b, op, target, kind, GIT_READ, 3600, cfg.git_reads_per_hour, weight, now)
        # 7. register
        wkey = f"{GIT_PUSH}:{normalize_repo(target)}" if kind == GIT_PUSH else kind
        if kind != IDENTITY:
            b["windows"].setdefault(wkey, []).extend([now] * weight)
        if kind == API_MUTATION:
            b["last_mutation_at"] = now
        token = f"{int(now)}-{os.getpid()}-{secrets.token_hex(4)}"
        b["inflight"].append(
            {
                "token": token,
                "pid": os.getpid(),
                "op": op,
                "target": target,
                "kind": kind,
                "lane": lane,
                "started": now,
                "deadline": now + cfg.inflight_deadline,
                "caller": caller_label(),
            }
        )
        self._write(BUDGET, b)
        self._event(decision="admit", op=op, target=target, kind=kind, op_id=token, weight=weight)
        return Admission(token, op, target, kind, now, weight)

    def _check_window(self, b, op, target, kind, wkey, window, cap, weight, now) -> None:
        used = self._count(b, wkey, window, now)
        if used + weight > cap:
            raise GateRefused(
                R_BUDGET,
                self._earliest_free(b, wkey, window, cap, now, weight),
                f"{wkey}: {used}/{cap} in the last {window}s",
            )

    # ---- recording -------------------------------------------------------------------------------

    def record(self, admission: Admission, outcome: Outcome) -> str:
        """Release the in-flight slot, append the outcome, and classify it into a transition (design §3
        table). Returns the transition applied: `none`, `halt`, `quarantine`, `cooldown`, `transient`."""
        if not self.enabled or admission.disabled:
            return "none"
        try:
            self._acquire()
            try:
                return self._record_locked(admission, outcome)
            finally:
                self._release()
        except StoreError as e:
            log(f"gate: could not record {admission.kind} {admission.op} {admission.target}: {e}")
            return "store-error"

    def _record_locked(self, a: Admission, o: Outcome) -> str:
        now = self.now()
        b = self._budget()
        b["inflight"] = [e for e in b["inflight"] if e.get("token") != a.token]
        status = o.status if o.status is not None else parse_status(o.text)
        text = o.text or ""
        rate_remaining = _header(o, "x-ratelimit-remaining", _RATE_REMAINING_RE)
        rate_reset = _header(o, "x-ratelimit-reset", _RATE_RESET_RE)
        retry_after = _header(o, "retry-after", _RETRY_AFTER_RE)
        request_id = _header(o, "x-github-request-id", _REQUEST_ID_RE)
        verdict, reason = self._classify(o, status, text, retry_after)
        if verdict == "none":
            b["transient"].pop(a.op, None)
            b.pop("consecutive_secondary", None)  # a success ends the "consecutive" run of secondary hits
        self._write(BUDGET, b)
        self._event(
            decision="record",
            op=a.op,
            target=a.target,
            kind=a.kind,
            op_id=a.token,
            status=status,
            ok=o.ok,
            duration_ms=o.duration_ms,
            retry_n=o.retry_n,
            request_id=request_id,
            rate_remaining=rate_remaining,
            rate_reset=rate_reset,
            reason=reason,
            verdict=verdict,
            detail=text if not o.ok else "",
        )
        s = self._state()
        if verdict == "halt":
            self._halt_locked(s, b, reason, text, status)
        elif verdict == "quarantine":
            self._quarantine(a.op, a.target, reason, text, until=None)
        elif verdict == "cooldown":
            secs = self._cooldown_seconds(b, reason, retry_after, rate_remaining, rate_reset, now)
            self._write(BUDGET, b)
            self._set_state(s, COOLDOWN, reason=reason, detail=text, until=now + secs)
        elif verdict == "transient":
            n = int(b["transient"].get(a.op, 0)) + 1
            b["transient"][a.op] = n
            self._write(BUDGET, b)
            if n >= self.config.transient_max:
                b["transient"].pop(a.op, None)
                self._write(BUDGET, b)
                self._quarantine(a.op, a.target, "transient", text, until=now + self.config.transient_quarantine)
        return verdict

    def _classify(self, o: Outcome, status: int | None, text: str, retry_after: str | None) -> tuple[str, str]:
        if o.ok and (status is None or status < 400):
            return "none", ""
        # Rate limits first: a secondary limit is a 403 too, and its text can contain "abuse".
        if status == 429 or retry_after or _GH_SECONDARY_RE.search(text):
            return "cooldown", "secondary-rate-limit" if _GH_SECONDARY_RE.search(text) else "rate-limit"
        if _GH_PRIMARY_RE.search(text):
            return "cooldown", "primary-rate-limit"
        if status == 401 or _INVALID_RE.search(text):
            return "halt", INVALID_CREDENTIALS
        if status == 403:
            if _HALT_WORDS_RE.search(text):
                return "halt", ACCOUNT_HALT
            if _PERMISSION_RE.search(text):
                return "quarantine", "permission-denied"
            return "cooldown", "unclassified-403"
        if _PERMISSION_RE.search(text):
            return "quarantine", "permission-denied"
        if (status is not None and status >= 500) or _GH_TRANSIENT_RE.search(text):
            return "transient", "transient"
        return "none", "failed"

    def _cooldown_seconds(self, b, reason, retry_after, rate_remaining, rate_reset, now) -> float:
        """Design §3: Retry-After, else x-ratelimit-reset when the primary bucket is empty, else 60 s
        doubling per consecutive secondary hit (the counter lives in budget.json and a success clears
        it), bounded to cooldown_max; an unclassified 403 is its own fixed pause."""
        cfg = self.config
        if retry_after:
            try:
                return max(1.0, min(float(retry_after), 3600.0))
            except ValueError:
                pass
        if rate_reset and (rate_remaining == "0" or reason == "primary-rate-limit"):
            try:
                return max(1.0, min(float(rate_reset) - now + 1, 3600.0))
            except ValueError:
                pass
        if reason == "unclassified-403":
            return float(cfg.unclassified_403_cooldown)
        n = int(b.get("consecutive_secondary", 0))
        b["consecutive_secondary"] = n + 1
        return float(min(cfg.cooldown_base * (1 << min(n, 20)), cfg.cooldown_max))

    def _quarantine(self, op: str, target: str, reason: str, text: str, *, until: float | None) -> None:
        q = self._read(QUARANTINE, {})
        q[f"{op}:{normalize_repo(target)}"] = {
            "reason": reason,
            "at": self.now(),
            "until": until,
            "detail": sanitize(text),
            "source": caller_label(),
        }
        self._write(QUARANTINE, q)
        self._event(decision="quarantine", op=op, target=target, kind="-", reason=reason, until=until, detail=text)

    def _halt_locked(self, s: dict, b: dict, reason: str, text: str, status: int | None) -> None:
        check = IdentityCheck("", _token_source(), False, reason, sanitize(text))
        from .identity import write_halt

        write_halt(self._path(HALT), check)
        # Any other write in flight at this moment may or may not have landed: say so for reconciliation.
        for e in b["inflight"]:
            if e.get("kind") in (API_MUTATION, GIT_PUSH):
                self._event(
                    decision="uncertain",
                    op=e.get("op"),
                    target=e.get("target"),
                    kind=e.get("kind"),
                    op_id=e.get("token"),
                    reason=reason,
                )
        self._set_state(s, HALTED_MANUAL, reason=reason, detail=text)

    # ---- operator surface --------------------------------------------------------------------------

    def halt(self, reason: str, detail: str = "") -> None:
        """An operator's manual halt (`tauceti-gate halt <reason>`)."""
        if not self.enabled:
            raise Die("gate disabled (TAUCETI_GATE_DIR unset)")
        self._acquire()
        try:
            s = self._state()
            check = IdentityCheck("", _token_source(), False, reason, sanitize(detail) or "manual halt")
            from .identity import write_halt

            write_halt(self._path(HALT), check)
            self._set_state(s, HALTED_MANUAL, reason=reason, detail=detail or "manual halt")
        finally:
            self._release()

    def halt_from_identity(self, check: IdentityCheck) -> None:
        """The identity gate found a credential no retry fixes: halt the fleet store too, so every other
        client (heartbeats, releases, the dashboard) is refused at its next admit."""
        if not self.enabled:
            return
        try:
            self._acquire()
            try:
                from .identity import write_halt

                write_halt(self._path(HALT), check)
                self._set_state(self._state(), HALTED_MANUAL, reason=check.reason or "identity", detail=check.detail)
            finally:
                self._release()
        except StoreError as e:
            log(f"gate: could not record the identity halt in {self.dir}: {e}")

    def resume(self) -> dict:
        """Clear a cooldown (only). Returns the incident it cleared. HALTED_MANUAL stays: that clears
        through `tauceti work --clear-halt`, which prints the record first."""
        if not self.enabled:
            raise Die("gate disabled (TAUCETI_GATE_DIR unset)")
        self._acquire()
        try:
            s = self._state()
            incident = dict(s)
            if s.get("state") == COOLDOWN:
                self._set_state(s, RUNNING, reason="resumed-by-operator")
            return incident
        finally:
            self._release()

    def clear_halt(self) -> dict | None:
        """What `--clear-halt` does to the fleet store: remove halt.json and return to RUNNING."""
        if not self.enabled:
            return None
        self._acquire()
        try:
            from .identity import read_halt

            rec = read_halt(self._path(HALT))
            self._path(HALT).unlink(missing_ok=True)
            s = self._state()
            if s.get("state") == HALTED_MANUAL:
                self._set_state(s, RUNNING, reason="halt-cleared-by-operator")
            return rec
        finally:
            self._release()

    def revalidate(self, op: str, target: str) -> bool:
        """Lift one quarantine after the operator fixed the permission or the configuration."""
        if not self.enabled:
            raise Die("gate disabled (TAUCETI_GATE_DIR unset)")
        self._acquire()
        try:
            q = self._read(QUARANTINE, {})
            key = f"{op}:{normalize_repo(target)}"
            if key not in q:
                return False
            del q[key]
            self._write(QUARANTINE, q)
            self._event(decision="revalidate", op=op, target=target, kind="-", reason="operator")
            return True
        finally:
            self._release()

    def set_disabled(self, on: bool) -> None:
        if not self.enabled:
            raise Die("gate disabled (TAUCETI_GATE_DIR unset)")
        self._acquire()
        try:
            p = self._path(DISABLED)
            if on:
                p.write_text(f"{_now_iso()} {caller_label()}\n")
            else:
                p.unlink(missing_ok=True)
            self._event(decision="transition", kind="-", op="-", target="-", reason="disable" if on else "enable")
        finally:
            self._release()

    # ---- the lock, for the ledgers that share it ------------------------------------------------

    @contextlib.contextmanager
    def locked(self):
        """The store lock, for the publication ledger and the read-cache markers, which live beside the
        store and take the same lock so one writer at a time touches any of them."""
        self._acquire()
        try:
            yield
        finally:
            self._release()

    @property
    def cache_dir(self) -> Path:
        """The fleet-shared read cache (design §5): `review_state/` sidecars and `inflight/` markers."""
        return self._path(CACHE)

    # ---- shared read cache: miss coalescing (design §5) ------------------------------------------

    def claim_read(self, key: str) -> dict | None:
        """Claim the miss for `key` (a PR number): None when this process now owns the fetch, else the
        live marker of the reader that does (pid alive, younger than READ_INFLIGHT_TTL), for the caller
        to wait on. A dead or aged marker is taken over. Never raises: a store failure means no
        coalescing, which is a duplicate read, not a wrong one."""
        try:
            with self.locked():
                p = self.cache_dir / INFLIGHT / f"{key}.json"
                other = self._read_marker(p)
                if other is not None:
                    return other
                p.parent.mkdir(parents=True, exist_ok=True)
                self._write(p.relative_to(self.dir).as_posix(), {"pid": os.getpid(), "at": self.now()})
                return None
        except (StoreError, OSError, ValueError):
            return None

    def read_claim_live(self, key: str) -> bool:
        """Whether someone else's marker for `key` is still live (the waiter's poll)."""
        try:
            with self.locked():
                return self._read_marker(self.cache_dir / INFLIGHT / f"{key}.json") is not None
        except (StoreError, OSError, ValueError):
            return False

    def release_read(self, key: str) -> None:
        try:
            with self.locked():
                p = self.cache_dir / INFLIGHT / f"{key}.json"
                try:
                    m = json.loads(p.read_text())
                except (OSError, ValueError):
                    return
                if m.get("pid") == os.getpid():
                    p.unlink(missing_ok=True)
        except (StoreError, OSError):
            pass

    def _read_marker(self, p: Path) -> dict | None:
        try:
            m = json.loads(p.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        pid = m.get("pid")
        at = m.get("at")
        if not isinstance(pid, int) or not isinstance(at, (int, float)):
            return None
        if pid == os.getpid() or not _pid_alive(pid) or self.now() - at >= READ_INFLIGHT_TTL:
            return None
        return m

    def probe(self) -> dict:
        """The verdict an admit would reach on the global state alone (halt, cooldown, disabled), with no
        registration and no event — for the loop preflight and `status`. Never raises: a store failure is
        reported in the dict."""
        if not self.enabled:
            return {"enabled": False, "state": RUNNING, "reason": "gate disabled", "until": None}
        try:
            self._acquire()
            try:
                s = self._state()
                self._budget()  # unreadable budget state is a store error too (T16): report it here
                out = {
                    "enabled": True,
                    "dir": str(self.dir),
                    "state": s.get("state", RUNNING),
                    "reason": s.get("reason"),
                    "detail": s.get("detail"),
                    "since": s.get("since"),
                    "until": s.get("until"),
                    "login": s.get("login"),
                    "disabled": self._path(DISABLED).exists(),
                }
                if self._halted(s):
                    out["state"] = HALTED_MANUAL
                    from .identity import read_halt

                    out["halt"] = read_halt(self._path(HALT))
                elif out["state"] == COOLDOWN and float(s.get("until") or 0) <= self.now():
                    out["state"] = RUNNING
                    out["reason"] = "cooldown-expired"
                return out
            finally:
                self._release()
        except StoreError as e:
            return {"enabled": True, "dir": str(self.dir), "state": "STORE-ERROR", "error": str(e), "until": None}

    def status(self) -> dict:
        """probe() plus the budgets and quarantines, for `tauceti-gate status`."""
        out = self.probe()
        if not out.get("enabled") or out.get("error"):
            return out
        try:
            self._acquire()
            try:
                b = self._budget()
                self._prune(b)
                now = self.now()
                cfg = self.config
                windows = {
                    "api_mutation": {
                        "minute": self._count(b, API_MUTATION, 60, now),
                        "hour": self._count(b, API_MUTATION, 3600, now),
                        "cap_minute": cfg.mutations_per_minute,
                        "cap_hour": cfg.mutations_per_hour,
                    },
                    "api_read": {
                        "minute": self._count(b, API_READ, 60, now),
                        "cap_minute": cfg.reads_per_minute,
                        "hour": self._count(b, API_READ, 3600, now),
                        "cap_hour": cfg.reads_per_hour,
                        "reserve": cfg.reads_reserve,
                    },
                    "git_read": {"hour": self._count(b, GIT_READ, 3600, now), "cap_hour": cfg.git_reads_per_hour},
                    "git_push": {
                        normalize_repo(k.split(":", 1)[1]): {
                            "minute": self._count(b, k, 60, now),
                            "hour": self._count(b, k, 3600, now),
                            "cap_minute": cfg.pushes_per_minute,
                            "cap_hour": cfg.pushes_per_hour,
                        }
                        for k in b["windows"]
                        if k.startswith(GIT_PUSH + ":")
                    },
                }
                out["budgets"] = windows
                out["inflight"] = b["inflight"]
                out["last_mutation_at"] = b.get("last_mutation_at")
                out["quarantine"] = self._read(QUARANTINE, {})
                out["unconfigured"] = [
                    n
                    for n, v in (
                        ("TAUCETI_GATE_MUTATIONS_PER_HOUR", cfg.mutations_per_hour),
                        ("TAUCETI_GATE_READS_PER_HOUR", cfg.reads_per_hour),
                    )
                    if v is None
                ]
                return out
            finally:
                self._release()
        except StoreError as e:
            out.update({"state": "STORE-ERROR", "error": str(e)})
            return out

    def refuse_if_halted(self) -> None:
        """Raise identity.Halted when the fleet store is halted (the loop start and every round start)."""
        v = self.probe()
        if v.get("state") == HALTED_MANUAL:
            from .identity import Halted

            h = v.get("halt") or {}
            raise Halted(
                f"gate: the fleet store {self.dir} is halted — {v.get('reason')} "
                f"({h.get('detail') or v.get('detail') or 'no detail'}) since {h.get('at') or _now_iso(v.get('since') or 0)}; "
                f"refusing to start: read it, fix the credential by hand, then `tauceti work --clear-halt`"
            )


# ---- process-wide instance --------------------------------------------------------------------------

_CURRENT: tuple[tuple[str, str], Gate] | None = None


def current() -> Gate:
    """The gate this process uses, built from the environment once per (dir, required) value so a test
    that repoints TAUCETI_GATE_DIR gets a fresh one."""
    global _CURRENT
    key = (os.environ.get(DIR_ENV, ""), os.environ.get(REQUIRED_ENV, ""))
    if _CURRENT is None or _CURRENT[0] != key:
        _CURRENT = (key, Gate.from_env())
    return _CURRENT[1]


def caller_label() -> str:
    wid = os.environ.get("TAUCETI_WORKER_ID") or "-"
    stage = os.environ.get("TAUCETI_STAGE") or os.environ.get("TAUCETI_GATE_CALLER") or ""
    return f"{wid}:{stage}" if stage else wid


def _token_source() -> str:
    from .identity import effective_token_source

    return effective_token_source()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def admit_or_log(op: str, target: str, kind: str, *, weight: int = 1) -> Admission | None:
    """The gate's admit for a site whose failure path is its own: the Admission, or None after logging
    the refusal (the caller then takes the path it takes when the operation failed)."""
    try:
        return current().admit(op, target, kind, weight=weight)
    except GateRefused as e:
        log(f"{kind} {op} {target}: {e.message()}")
        return None


def gated_git(argv: list[str], *, op: str, target: str, kind: str = GIT_READ, **kw):
    """One git transport call (clone / fetch / ls-remote) through the gate. stderr is captured for the
    record and echoed afterwards, so what the operator saw before is what they see now; a refusal is
    the usual rc-75 CompletedProcess."""
    import subprocess
    import sys

    gate = current()
    try:
        adm = gate.admit(op, target, kind)
    except GateRefused as e:
        log(f"git {op} {target}: {e.message()}")
        return refused_process(argv, e)
    env = {**(kw.pop("env", None) or os.environ), **adm.child_env()}
    capture = kw.pop("capture_output", False)
    t0 = time.monotonic()
    if capture:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, **kw)
    else:
        p = subprocess.run(argv, env=env, stderr=subprocess.PIPE, text=True, **kw)
        if p.stderr:
            sys.stderr.write(p.stderr)
            sys.stderr.flush()
    gate.record(adm, Outcome.from_process(p, duration_ms=int((time.monotonic() - t0) * 1000)))
    return p


def refused_process(argv: list[str], e: GateRefused):
    """The CompletedProcess a refused `gh`/script call turns into, so every caller's existing failure
    handling applies: rc 75, and `gate: refused (<reason>)` on stderr."""
    import subprocess

    return subprocess.CompletedProcess(argv, REFUSED_RC, stdout="", stderr=e.message() + "\n")


# ---- classifying a gh argv ------------------------------------------------------------------------------

_GH_MUTATING_VERBS = {
    ("pr", "create"),
    ("pr", "edit"),
    ("pr", "comment"),
    ("pr", "review"),
    ("pr", "close"),
    ("pr", "merge"),
    ("pr", "reopen"),
    ("pr", "ready"),
    ("issue", "create"),
    ("issue", "edit"),
    ("issue", "comment"),
    ("issue", "close"),
    ("repo", "fork"),
    ("repo", "create"),
    ("repo", "delete"),
    ("workflow", "run"),
    ("workflow", "enable"),
    ("workflow", "disable"),
    ("run", "rerun"),
    ("run", "cancel"),
    ("label", "create"),
    ("label", "edit"),
    ("label", "delete"),
}


_GH_API_VALUE_OPTS = frozenset(
    {
        "-X",
        "--method",
        "-f",
        "-F",
        "--field",
        "--raw-field",
        "--input",
        "-H",
        "--header",
        "--jq",
        "-q",
        "-t",
        "--template",
        "--cache",
        "--hostname",
        "-p",
        "--preview",
    }
)


def classify_gh(argv: list[str]) -> tuple[str, str, str]:
    """(kind, op, target) for a `gh` argv: `api -X POST|PATCH|PUT|DELETE`, a field argument, a GraphQL
    mutation document and the write verbs are mutations; everything else reads. The target is the
    `--repo` value, the `repos/<owner>/<repo>` in an api path, or the canonical repository; account
    endpoints (`user`, `rate_limit`) are `-`."""
    args = [a for a in argv[1:] if a]
    if not args:
        return API_READ, "gh", "-"
    kind = API_READ
    op = args[0]
    target = TAUCETI
    if args[0] == "api":
        rest = args[1:]
        path = ""
        skip = False
        for a in rest:
            if skip:
                skip = False
            elif a in _GH_API_VALUE_OPTS:
                skip = True
            elif not a.startswith("-"):
                path = a
                break
        if path == "graphql":
            op = "graphql"
            from .constants import _GQL_MUTATION_RE

            docs = [a[len("query=") :] for a in rest[1:] if a.startswith("query=")]
            if not docs or any(_GQL_MUTATION_RE.search(d) for d in docs):
                kind = API_MUTATION
            owner = next((a[len("owner=") :] for a in rest if a.startswith("owner=")), "")
            name = next((a[len("repo=") :] for a in rest if a.startswith("repo=")), "") or next(
                (a[len("name=") :] for a in rest if a.startswith("name=")), ""
            )
            target = f"{owner}/{name}" if owner and name else TAUCETI
        else:
            op = "api"
            method = "GET"
            for i, a in enumerate(rest):
                if a in ("-X", "--method") and i + 1 < len(rest):
                    method = rest[i + 1].upper()
                elif a.startswith("--method="):
                    method = a.split("=", 1)[1].upper()
                elif a in ("-f", "-F", "--field", "--raw-field", "--input"):
                    method = "POST" if method == "GET" else method
            if method != "GET":
                kind = API_MUTATION
                op = f"api-{method.lower()}"
            p = path.lstrip("/")
            m = re.match(r"repos/([^/]+)/([^/?]+)", p)
            if m:
                target = f"{m.group(1)}/{m.group(2)}"
            elif p.startswith(("user", "rate_limit", "notifications")):
                target = "-"
                op = "rate_limit" if p.startswith("rate_limit") else ("user" if p == "user" else op)
                if p == "user" and method == "GET":
                    kind = IDENTITY  # the identity gate's one read: exempt from the budgets, not from a halt
            else:
                target = "-"
    else:
        if len(args) > 1 and (args[0], args[1]) in _GH_MUTATING_VERBS:
            kind = API_MUTATION
        if len(args) > 1 and not args[1].startswith("-"):
            op = f"{args[0]}-{args[1]}"
        for i, a in enumerate(args):
            if a in ("--repo", "-R") and i + 1 < len(args):
                target = args[i + 1]
            elif a.startswith("--repo="):
                target = a.split("=", 1)[1]
        if args[0] == "repo" and len(args) > 2 and args[1] == "fork":
            target = args[2]
        if args[0] == "repo" and len(args) > 1 and args[1] == "list":
            target = "-"
    return kind, op, target


def classify_claim(args: list[str]) -> tuple[str, str]:
    """(kind, op) for a claim.sh subcommand: the lease writers push, the rest only read."""
    sub = args[0] if args else ""
    if sub in ("acquire", "renew", "release", "gc"):
        return GIT_PUSH, sub
    return GIT_READ, sub or "claim"

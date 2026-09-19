"""tauceti_worker.identity — which GitHub account the worker is about to act as, checked ONCE before a
loop starts and once at the start of every round, and the halt file that stops a loop for good when the
answer is one that no retry, back-off, or sign-in attempt of the worker's own should ever follow.

The check is a single `gh api user`, through the same `gh_run` every other call uses, so it waits out
a rate limit like they do and costs one request per round. What it decides:

  * `gh` answers with a login: the worker acts as it. If `TAUCETI_EXPECT_LOGIN` is set and differs,
    that is `identity-mismatch` — the token in front of `gh` is not the one this worker was configured
    for, and every write it would make would be signed by the wrong account.
  * HTTP 401 / "Bad credentials" / no credential at all: `invalid-credentials`.
  * HTTP 403 whose text says the account is suspended, locked, or "too many": `account-halt`.
  * anything else (network, 5xx, a rate limit): `unknown`. Not a credential verdict, so the round
    proceeds and the survey's own failure handling takes it from there.

The first three halt: the reason is written to `<state>/halt.json` and `Halted` is raised, which the
CLI maps to EX_HALTED. A loop whose round child exits EX_HALTED stops at once — no retry, no
escalating back-off — and a loop refuses to start while the file exists, until an operator has read it
and cleared it with `tauceti work --clear-halt`. Nothing here, or anywhere in the worker, runs
`gh auth` to fix things: a credential problem is the operator's to look at, not the worker's to paper
over unattended.

The validated login is cached in `TAUCETI_IDENTITY_OK` for the rest of the process (and the round
children it spawns, each of which re-validates once on entry) so no write path repeats the call.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from pathlib import Path

from .config import log
from .constants import _GH_PRIMARY_RE, _GH_SECONDARY_RE

EXPECT_ENV = "TAUCETI_EXPECT_LOGIN"
OK_ENV = "TAUCETI_IDENTITY_OK"
HALT_FILE = "halt.json"

INVALID_CREDENTIALS = "invalid-credentials"
ACCOUNT_HALT = "account-halt"
IDENTITY_MISMATCH = "identity-mismatch"
UNKNOWN = "unknown"
HALT_REASONS = frozenset({INVALID_CREDENTIALS, ACCOUNT_HALT, IDENTITY_MISMATCH})

_GH_USER_ARGV = ["gh", "api", "user", "--jq", ".login"]

# gh's own wording. "not logged in" / the sign-in hint is what gh prints with NO credential at all
# (exit 4, no HTTP status); the others carry the status.
_INVALID_RE = re.compile(r"HTTP 401|Bad credentials|not logged in|gh auth login", re.I)
_FORBIDDEN_RE = re.compile(r"HTTP 403")
_HALT_WORDS_RE = re.compile(r"suspended|locked|too many", re.I)
_CONTROL_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # whole escape sequences first
# Never let a token-shaped string into a file or a log line, whatever produced the text.
_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_DETAIL_MAX = 300


class Halted(Exception):
    """The worker must stop and must not come back on its own. Raised by `gate` after the halt file is
    written, and by `refuse_if_halted` when one is already there. The CLI maps it to EX_HALTED."""


@dataclasses.dataclass(frozen=True)
class IdentityCheck:
    login: str  # the login gh answered with ("" when it did not)
    source: str  # which credential gh used: GH_TOKEN, GITHUB_TOKEN, or stored (its keyring / hosts.yml)
    ok: bool  # True iff a login came back and matches the expectation, when there is one
    reason: str | None = None  # one of the module constants when not ok
    detail: str = ""  # sanitized first line of what gh said

    @property
    def halts(self) -> bool:
        return self.reason in HALT_REASONS


def expected_login() -> str | None:
    """The login this worker is configured to act as (`TAUCETI_EXPECT_LOGIN`), or None: the expectation
    is optional, and without one the gate logs the effective login and proceeds."""
    return os.environ.get(EXPECT_ENV, "").strip() or None


def effective_token_source() -> str:
    """Which credential `gh` will use for github.com, in gh's own precedence: `GH_TOKEN`, then
    `GITHUB_TOKEN`, then whatever it stored at sign-in (its keyring or hosts.yml). Reported, never read:
    the gate wants to say WHERE a wrong login came from, not what the token is."""
    if os.environ.get("GH_TOKEN"):
        return "GH_TOKEN"
    if os.environ.get("GITHUB_TOKEN"):
        return "GITHUB_TOKEN"
    return "stored"


def cached_login() -> str | None:
    """The login a gate in this process (or the parent that spawned it) already validated, or None."""
    return os.environ.get(OK_ENV, "").strip() or None


def sanitize(text: str) -> str:
    """The first non-empty line of some process output, safe for a file and a terminal: control and
    escape sequences stripped, anything token-shaped redacted, length capped."""
    for raw in (text or "").splitlines():
        line = _TOKEN_RE.sub("<redacted>", _CONTROL_RE.sub("", raw)).strip()
        if line:
            return line if len(line) <= _DETAIL_MAX else line[:_DETAIL_MAX] + " …[truncated]"
    return ""


def classify(returncode: int, text: str) -> str | None:
    """The reason a `gh api user` failed, or None when it did not. A rate limit is `unknown` even
    though GitHub sends it as a 403: it is the one 403 that waiting does fix, and the loop preflight
    already waits for it."""
    if returncode == 0:
        return None
    if _GH_PRIMARY_RE.search(text) or _GH_SECONDARY_RE.search(text):
        return UNKNOWN
    if _INVALID_RE.search(text):
        return INVALID_CREDENTIALS
    if _FORBIDDEN_RE.search(text) and _HALT_WORDS_RE.search(text):
        return ACCOUNT_HALT
    return UNKNOWN


def validate(gh_run) -> IdentityCheck:
    """One `gh api user` through `gh_run`, classified. Never retries and never calls anything else."""
    source = effective_token_source()
    p = gh_run(_GH_USER_ARGV)
    text = (p.stderr or "") + "\n" + (p.stdout or "")
    reason = classify(p.returncode, text)
    if reason is not None:
        return IdentityCheck("", source, False, reason, sanitize(text))
    login = (p.stdout or "").strip()
    if not login:
        return IdentityCheck("", source, False, UNKNOWN, sanitize(text) or "gh api user returned no login")
    want = expected_login()
    if want and want.lower() != login.lower():
        return IdentityCheck(
            login, source, False, IDENTITY_MISMATCH, f"gh is signed in as {login}, this worker expects {want}"
        )
    return IdentityCheck(login, source, True)


def halt_path(state: Path) -> Path:
    return Path(state) / HALT_FILE


def read_halt(path: Path) -> dict | None:
    try:
        v = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def write_halt(path: Path, check: IdentityCheck) -> dict:
    record = {
        "reason": check.reason,
        "detail": check.detail,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "login": check.login or None,
        "login_expected": expected_login(),
        "source": check.source,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def clear_hint(wid: str | None) -> str:
    who = f" --worker-id {wid}" if wid and wid != "default" else ""
    return f"read it, fix the credential by hand, then `tauceti work{who} --clear-halt`"


def gate(state: Path, wid: str | None = None, gh_run=None, *, where: str = "start") -> IdentityCheck:
    """Validate once and either cache the login (`TAUCETI_IDENTITY_OK`) or halt. `where` names the call
    site for the log line. `gh_run` is a seam for tests; the real one is github.gh_run."""
    if gh_run is None:
        from .github import gh_run as real_gh_run

        gh_run = real_gh_run
    check = validate(gh_run)
    want = expected_login()
    if check.halts:
        path = halt_path(state)
        write_halt(path, check)
        os.environ.pop(OK_ENV, None)
        raise Halted(
            f"identity: {check.reason} at {where} ({check.detail or 'no detail'}; credential source "
            f"{check.source}{f', expected {want}' if want else ''}) — halting: wrote {path}; {clear_hint(wid)}"
        )
    if not check.ok:
        # Not a verdict on the credential: say so and let the round find out what is wrong.
        os.environ.pop(OK_ENV, None)
        log(
            f"identity: could not confirm the gh login at {where} ({check.detail}) — proceeding; the round will surface it"
        )
        return check
    os.environ[OK_ENV] = check.login
    log(
        f"identity: acting as {check.login} via {check.source}"
        + (" (as expected)" if want else f" ({EXPECT_ENV} unset — set it to pin this worker to one account)")
    )
    return check


def refuse_if_halted(state: Path, wid: str | None = None) -> None:
    """A loop does not start over a halt file: the last one stopped for a reason an operator has not yet
    looked at. Raises Halted naming the file and how to clear it."""
    path = halt_path(state)
    record = read_halt(path)
    if record is None and not path.exists():
        return
    why = f"{record.get('reason')} ({record.get('detail')}) at {record.get('at')}" if record else "unreadable record"
    raise Halted(f"identity: halt file {path} present — {why}; refusing to start: {clear_hint(wid)}")


def clear_halt(path: Path, out=print) -> bool:
    """Print the halt record and delete the file (what `tauceti work --clear-halt` does). True if there
    was one. The contents are shown first so clearing is a decision, not a reflex."""
    path = Path(path)
    if not path.exists():
        out(f"no halt file at {path}")
        return False
    try:
        out(path.read_text().rstrip("\n"))
    except OSError as e:
        out(f"{path}: unreadable ({e})")
    path.unlink(missing_ok=True)
    out(f"cleared {path}")
    return True

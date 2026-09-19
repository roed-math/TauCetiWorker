#!/usr/bin/env python3
"""The identity gate: one `gh api user` before a loop and before each round, classified into a verdict
the worker either caches or halts on — and the halt is final until an operator clears it.

Every gh call goes through a stub that records argv, so nothing here touches GitHub, and the record is
checked at the end: no argv anywhere in this file's run may begin `gh auth` (the worker never signs in,
refreshes, or re-authenticates unattended).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

for var in ("TAUCETI_EXPECT_LOGIN", "TAUCETI_IDENTITY_OK", "GH_TOKEN", "GITHUB_TOKEN", "TAUCETI_RUNTIME_STATUS"):
    os.environ.pop(var, None)

import tauceti_worker as tc  # noqa: E402
from tauceti_worker import cli, github, identity, loop  # noqa: E402

fails = 0
ARGV: list[list[str]] = []


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def gh_stub(rc=0, out="", err=""):
    """A gh_run that answers every call the same way and records it."""

    def run(argv, **_kw):
        ARGV.append(list(argv))
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    return run


def never(argv, **_kw):
    ARGV.append(list(argv))
    raise AssertionError(f"unexpected gh call: {argv}")


TMP = Path(tempfile.mkdtemp(prefix="identity-gate-"))
state = TMP / "state"
halt = identity.halt_path(state)
LOG = []
identity.log = LOG.append

# ---- 1. classification: one call each, the right reason -----------------------------------------
cases = [
    ("a login", gh_stub(0, "alice\n"), True, None),
    ("HTTP 401", gh_stub(1, "", "gh: Bad credentials (HTTP 401)"), False, "invalid-credentials"),
    ("401 without the phrase", gh_stub(1, "", "HTTP 401: Unauthorized"), False, "invalid-credentials"),
    (
        "no credential at all",
        gh_stub(4, "", "To get started with GitHub CLI, please run:  gh auth login"),
        False,
        "invalid-credentials",
    ),
    ("403 suspended", gh_stub(1, "", "gh: Sorry. Your account was suspended. (HTTP 403)"), False, "account-halt"),
    ("403 locked", gh_stub(1, "", "HTTP 403: This account is locked"), False, "account-halt"),
    ("403 too many", gh_stub(1, "", "HTTP 403: too many failed attempts"), False, "account-halt"),
    ("403 without a halt word", gh_stub(1, "", "HTTP 403: Resource not accessible by integration"), False, "unknown"),
    (
        "403 primary rate limit",
        gh_stub(1, "", "gh: API rate limit exceeded for user ID 1 (HTTP 403)"),
        False,
        "unknown",
    ),
    (
        "403 secondary rate limit",
        gh_stub(1, "", "You have exceeded a secondary rate limit (HTTP 403)"),
        False,
        "unknown",
    ),
    ("HTTP 502", gh_stub(1, "", "gh: HTTP 502 Bad Gateway"), False, "unknown"),
    ("a dropped connection", gh_stub(1, "", "error connecting to api.github.com"), False, "unknown"),
    ("exit 0 with no login", gh_stub(0, "\n"), False, "unknown"),
]
for name, stub, ok, reason in cases:
    before = len(ARGV)
    c = identity.validate(stub)
    check(f"validate {name} -> ok={ok} reason={reason}", (c.ok, c.reason) == (ok, reason))
    check(
        f"validate {name} -> exactly one gh call, and it is `gh api user`",
        ARGV[before:] == [["gh", "api", "user", "--jq", ".login"]],
    )
check("a good answer carries the login", identity.validate(gh_stub(0, "alice\n")).login == "alice")
check(
    "IdentityCheck.halts is true for the three halting reasons only",
    [
        identity.IdentityCheck("", "stored", False, r).halts
        for r in ("invalid-credentials", "account-halt", "identity-mismatch", "unknown", None)
    ]
    == [True, True, True, False, False],
)

# ---- 2. the expectation: mismatch halts; a match, case-insensitively, is ok ----------------------
os.environ["TAUCETI_EXPECT_LOGIN"] = "alice"
c = identity.validate(gh_stub(0, "bob\n"))
check("expected alice, got bob -> identity-mismatch", (c.ok, c.reason, c.login) == (False, "identity-mismatch", "bob"))
check("mismatch detail names both", "bob" in c.detail and "alice" in c.detail)
check("expected alice, got Alice -> ok", identity.validate(gh_stub(0, "Alice\n")).ok)
os.environ.pop("TAUCETI_EXPECT_LOGIN")
check("no expectation -> any login is ok", identity.validate(gh_stub(0, "bob\n")).ok)

# ---- 3. the token source, in gh's precedence -------------------------------------------------------
check("no token env -> stored", identity.effective_token_source() == "stored")
os.environ["GITHUB_TOKEN"] = "x"
check("GITHUB_TOKEN alone -> GITHUB_TOKEN", identity.effective_token_source() == "GITHUB_TOKEN")
os.environ["GH_TOKEN"] = "y"
check("GH_TOKEN beats GITHUB_TOKEN", identity.effective_token_source() == "GH_TOKEN")
check("the source is reported with the verdict", identity.validate(gh_stub(0, "alice\n")).source == "GH_TOKEN")
os.environ.pop("GH_TOKEN")
os.environ.pop("GITHUB_TOKEN")

# ---- 4. sanitizing what gh said ----------------------------------------------------------------
check("sanitize keeps the first non-empty line", identity.sanitize("\n\n  first \nsecond") == "first")
check(
    "sanitize strips control and escape sequences", identity.sanitize("\x1b[31mbad\x1b[0m\x07 creds\r") == "bad creds"
)
check("sanitize redacts a token-shaped string", "ghp_" not in identity.sanitize("token ghp_" + "A" * 36 + " rejected"))
check("sanitize caps the length", len(identity.sanitize("x" * 1000)) < 400)


# ---- 5. the gate: a halting verdict writes the file and raises; nothing is cached ----------------
def gate(stub, where="loop start"):
    try:
        identity.gate(state, "worker7", stub, where=where)
        return None
    except identity.Halted as e:
        return str(e)


msg = gate(gh_stub(1, "", "gh: Bad credentials (HTTP 401)\x1b[0m\nsecond line"))
check("401 at the gate -> Halted", msg is not None)
check(
    "Halted names the reason, the file, and how to clear it",
    msg and "invalid-credentials" in msg and str(halt) in msg and "--worker-id worker7 --clear-halt" in msg,
)
check("halt file written", halt.is_file())
rec = json.loads(halt.read_text())
check("halt file has the record fields", set(rec) >= {"reason", "detail", "at", "login_expected", "source"})
check(
    "halt file: reason / source / expected",
    (rec["reason"], rec["source"], rec["login_expected"]) == ("invalid-credentials", "stored", None),
)
check("halt file: detail is the sanitized first line", rec["detail"] == "gh: Bad credentials (HTTP 401)")
check("halt file: at is an ISO UTC timestamp", rec["at"].endswith("Z") and "T" in rec["at"])
check("nothing cached after a halt", "TAUCETI_IDENTITY_OK" not in os.environ)
halt.unlink()

os.environ["TAUCETI_EXPECT_LOGIN"] = "alice"
msg = gate(gh_stub(0, "bob\n"), where="round start")
rec = json.loads(halt.read_text()) if halt.is_file() else {}
check(
    "mismatch at the gate -> Halted with identity-mismatch, file records both logins",
    msg and "identity-mismatch" in msg and rec.get("login") == "bob" and rec.get("login_expected") == "alice",
)
halt.unlink()
msg = gate(gh_stub(1, "", "Your account was suspended. (HTTP 403)"))
check("suspended at the gate -> account-halt file", msg and json.loads(halt.read_text())["reason"] == "account-halt")
halt.unlink()
os.environ.pop("TAUCETI_EXPECT_LOGIN")

# ---- 6. the gate: unknown proceeds without a file; ok caches the login for me() ------------------
LOG.clear()
c = identity.gate(state, "worker7", gh_stub(1, "", "HTTP 502"), where="round start")
check("unknown at the gate -> returns (no raise), no halt file", c.reason == "unknown" and not halt.exists())
check("unknown at the gate -> says it is proceeding", any("proceeding" in m for m in LOG))
check("unknown at the gate -> nothing cached", "TAUCETI_IDENTITY_OK" not in os.environ)
LOG.clear()
c = identity.gate(state, "worker7", gh_stub(0, "alice\n"), where="loop start")
check("ok at the gate -> login cached in TAUCETI_IDENTITY_OK", os.environ.get("TAUCETI_IDENTITY_OK") == "alice")
check(
    "ok at the gate, no expectation -> logs the login and the source, and suggests the pin",
    any("acting as alice via stored" in m and "TAUCETI_EXPECT_LOGIN" in m for m in LOG),
)
github.me.cache_clear()
saved_gh_run = github.gh_run
github.gh_run = never
try:
    check("me() answers from the cache without a gh call", github.me() == "alice")
finally:
    github.gh_run = saved_gh_run
    github.me.cache_clear()
os.environ.pop("TAUCETI_IDENTITY_OK")

# ---- 7. a loop refuses to start over a halt file; --clear-halt prints and removes it ---------------
check("no halt file -> refuse_if_halted is silent", identity.refuse_if_halted(state, "worker7") is None)
gate(gh_stub(1, "", "HTTP 401"))
try:
    identity.refuse_if_halted(state, "worker7")
    refused = None
except identity.Halted as e:
    refused = str(e)
check(
    "halt file present -> refuse_if_halted raises, naming the file, the reason, and --clear-halt",
    refused and str(halt) in refused and "invalid-credentials" in refused and "--clear-halt" in refused,
)
printed = []
check(
    "clear_halt returns True and removes the file",
    identity.clear_halt(halt, printed.append) is True and not halt.exists(),
)
check("clear_halt printed the record first", any('"reason": "invalid-credentials"' in p for p in printed))
check(
    "clear_halt on nothing -> False, says so",
    identity.clear_halt(halt, printed.append) is False and "no halt file" in printed[-1],
)
check("`tauceti work --clear-halt` parses", cli.build_parser().parse_args(["work", "--clear-halt"]).clear_halt is True)
check(
    "the CLI maps Halted to EX_HALTED, a code of its own",
    tc.constants.EX_HALTED not in (0, 1, tc.EX_NOPROGRESS, 124, 137, 130, 143),
)
saved_main = cli.main
cli.main = lambda argv=None: (_ for _ in ()).throw(identity.Halted("stop"))
try:
    check("cli_main returns EX_HALTED on Halted", cli.cli_main() == tc.constants.EX_HALTED)
finally:
    cli.main = saved_main

# ---- 8. the loop stops on a halted round: no back-off, no retry ------------------------------------
rounds = []
saved = (loop.choose_model, loop.github_budget, loop.run_round_subprocess, loop.time.sleep)
loop.choose_model = lambda *_a, **_k: ("codex", {})
loop.github_budget = lambda: {}
loop.run_round_subprocess = lambda tail: rounds.append(tail) or tc.constants.EX_HALTED
loop.time.sleep = lambda s: (_ for _ in ()).throw(AssertionError(f"the loop must not sleep after a halt (slept {s})"))
try:
    rc = loop.cmd_loop(
        SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None, source=None),
        SimpleNamespace(wid="w"),
        only=["review"],
        agent="codex",
    )
    check("loop: a round exiting EX_HALTED ends the loop with EX_HALTED", rc == tc.constants.EX_HALTED)
    check("loop: exactly one round was run (no retry)", len(rounds) == 1)
except AssertionError as e:
    check(f"loop: {e}", False)
finally:
    loop.choose_model, loop.github_budget, loop.run_round_subprocess, loop.time.sleep = saved

# ---- 9. nothing above ever tried to sign in ----------------------------------------------------------
check("no `gh auth ...` argv anywhere in this run", not any(a[:2] == ["gh", "auth"] for a in ARGV))
check("every gh call was the one read: gh api user", all(a == ["gh", "api", "user", "--jq", ".login"] for a in ARGV))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

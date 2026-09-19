#!/usr/bin/env python3
"""Nothing in the worker signs in to GitHub on its own.

A credential problem is the operator's to look at (see the identity gate): the worker halts on one and
must never run `gh auth login`, `gh auth refresh`, or `gh auth setup-git` unattended — an unattended
login would either hang on a device-code prompt or, worse, succeed and quietly re-point every write.
This test greps `tauceti_worker/` and `scripts/` for the ways such a call could be spelled — an argv
list (`"auth", "login"`), a shell line (`gh auth login`) — and fails on any hit outside the allowlist
below. The allowlist is by file and substring, so a line has to be the exact hint string it names; a
new occurrence anywhere, including a new file, needs a conscious entry here.

Exit 0 = clean; 1 = a call site (or an allowlist entry that no longer matches anything).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("tauceti_worker", "scripts")
SUFFIXES = {".py", ".sh", ""}  # scripts/ has extensionless bash wrappers

# `gh auth <sub>` as text, or as adjacent argv strings `"auth", "<sub>"` / `'auth', '<sub>'`. The
# subcommands that change or create a sign-in; `gh auth status` and `gh auth token` are reads (doctor
# and the bubble egress probe use them) and are not the concern here.
SUBS = r"(?:login|refresh|setup-git|logout)"
PATTERNS = [
    re.compile(rf"\bgh\s+auth\s+{SUBS}\b"),
    re.compile(rf"""["']auth["']\s*,\s*["']{SUBS}["']"""),
]

# (path relative to the repo, substring that must be on the matching line). Every entry is a message
# to a human, not a call: a hint in an error string, a comment about when a human has to have signed in,
# or a regex that recognises gh's own "please run gh auth login" output.
ALLOW = {
    (
        "tauceti_worker/github.py",
        'raise Die("could not determine the authenticated GitHub account (run `gh auth login`)")',
    ),
    ("tauceti_worker/identity.py", "_INVALID_RE = re.compile("),
    (
        "scripts/docker-entrypoint",
        "# setup and CI run before `gh auth login` has populated the persistent credential volume.",
    ),
}


def files():
    for root in ROOTS:
        for p in sorted((REPO / root).rglob("*")):
            if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts:
                yield p


hits = []
used = set()
for path in files():
    rel = path.relative_to(REPO).as_posix()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        continue
    for n, line in enumerate(text.splitlines(), 1):
        if not any(pat.search(line) for pat in PATTERNS):
            continue
        allowed = [(f, sub) for f, sub in ALLOW if f == rel and sub in line]
        if allowed:
            used.update(allowed)
            continue
        hits.append(f"{rel}:{n}: {line.strip()}")

rc = 0
if hits:
    rc = 1
    print("gh auth call sites outside the allowlist (the worker must never sign in unattended):")
    for h in hits:
        print(f"  {h}")
stale = ALLOW - used
if stale:
    rc = 1
    print("allowlist entries that match no line any more (remove them):")
    for f, sub in sorted(stale):
        print(f"  {f}: {sub!r}")
print(
    f"\n{'PASS' if rc == 0 else 'FAIL'}: {len(hits)} call site(s), {len(stale)} stale allowlist entr{'y' if len(stale) == 1 else 'ies'}"
)
sys.exit(rc)

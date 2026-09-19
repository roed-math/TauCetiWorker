"""The offline harness's fake transport (design §8): `gh`, `git`, `curl` and `security` on PATH answer
from a scenario file instead of the network.

    TAUCETI_FAKE_SCENARIO   JSON: {"gh": [entry…], "git": [entry…], "bare_repos": {"owner/repo": "/path.git"}}
                            entry = {"match": [argv prefix…] | "contains": "substr", "rc": 0, "stdout": "",
                                     "stderr": "", "sleep": 0, "times": N (default unlimited)}
                            Entries are tried in order; the first whose `match` is a prefix of argv AND
                            whose `contains` is in the joined argv (each optional) answers, consuming
                            one of its `times`.
    TAUCETI_FAKE_LOG        every invocation is appended as one JSON line {bin, argv, ts, answered}

Unmatched `gh` → rc 1, "fake gh: no scenario entry". Unmatched `git` → the REAL git
(TAUCETI_FAKE_REAL_GIT, default /usr/bin/git) with every https://github.com/<owner>/<repo> URL
rewritten to that repository's local bare path from `bare_repos` (a URL with no bare repo is
rewritten to a path that does not exist, so the transport fails rather than reaching the network).
`curl` and `security` are never answered: they log and fail (7 / 44), which is what T15 asserts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_STATE_SUFFIX = ".consumed"


def _log(binary: str, argv: list[str], answered: str) -> None:
    path = os.environ.get("TAUCETI_FAKE_LOG")
    if not path:
        return
    with open(path, "a") as f:
        f.write(json.dumps({"bin": binary, "argv": argv, "ts": time.time(), "answered": answered}) + "\n")


def _scenario() -> dict:
    path = os.environ.get("TAUCETI_FAKE_SCENARIO")
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        print(f"fake: scenario {path}: {e}", file=sys.stderr)
        return {}


def _consumed_path() -> Path | None:
    path = os.environ.get("TAUCETI_FAKE_SCENARIO")
    return Path(path + _STATE_SUFFIX) if path else None


def _pick(binary: str, argv: list[str]) -> tuple[int, dict | None]:
    """The index and entry that answers argv, honouring `times` across processes via a sidecar file."""
    sc = _scenario()
    entries = sc.get(binary) or []
    cp = _consumed_path()
    consumed: dict[str, int] = {}
    if cp and cp.exists():
        try:
            consumed = json.loads(cp.read_text())
        except ValueError:
            consumed = {}
    joined = " ".join(argv)
    for i, e in enumerate(entries):
        key = f"{binary}:{i}"
        times = e.get("times")
        if times is not None and consumed.get(key, 0) >= times:
            continue
        m = e.get("match")
        c = e.get("contains")
        if m is None and c is None:
            continue
        if (m is None or argv[: len(m)] == list(m)) and (c is None or c in joined):
            if times is not None:  # only a counted entry needs the sidecar
                consumed[key] = consumed.get(key, 0) + 1
                if cp:
                    cp.write_text(json.dumps(consumed))
            return i, e
    return -1, None


def _answer(binary: str, argv: list[str], e: dict) -> int:
    if e.get("sleep"):
        time.sleep(float(e["sleep"]))
    sys.stdout.write(e.get("stdout", ""))
    sys.stderr.write(e.get("stderr", ""))
    sys.stdout.flush()
    sys.stderr.flush()
    return int(e.get("rc", 0))


def fake_gh(argv: list[str]) -> int:
    i, e = _pick("gh", argv)
    if e is None:
        _log("gh", argv, "unmatched")
        print(f"fake gh: no scenario entry for: {' '.join(argv)}", file=sys.stderr)
        return 1
    _log("gh", argv, f"entry {i}")
    return _answer("gh", argv, e)


_URL_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def _rewrite(arg: str, bare: dict) -> str:
    m = _URL_RE.match(arg)
    if not m:
        return arg
    key = f"{m.group(1)}/{m.group(2)}".lower()
    for k, v in bare.items():
        if k.lower() == key:
            return v
    return f"/nonexistent/fake-bare/{m.group(1)}/{m.group(2)}.git"


def fake_git(argv: list[str]) -> int:
    i, e = _pick("git", argv)
    if e is not None:
        _log("git", argv, f"entry {i}")
        return _answer("git", argv, e)
    sc = _scenario()
    bare = sc.get("bare_repos") or {}
    real = os.environ.get("TAUCETI_FAKE_REAL_GIT") or "/usr/bin/git"
    rewritten = [_rewrite(a, bare) for a in argv]
    _log("git", argv, "real-git-local" if rewritten != argv else "real-git")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    # A real git may still be asked for a credential (a rewritten URL is a path, so it will not be);
    # make sure no helper could ever reach a keychain from here.
    env["GIT_CONFIG_GLOBAL"] = env.get("TAUCETI_FAKE_GITCONFIG", "/dev/null")
    try:
        return subprocess.run([real, *rewritten], env=env).returncode
    except OSError as ex:
        print(f"fake git: {ex}", file=sys.stderr)
        return 1


def fake_curl(argv: list[str]) -> int:
    _log("curl", argv, "refused")
    print("fake curl: (7) Failed to connect: the offline harness has no network", file=sys.stderr)
    return 7


def fake_security(argv: list[str]) -> int:
    _log("security", argv, "refused")
    print("fake security: The specified item could not be found in the keychain.", file=sys.stderr)
    return 44


MAIN = {"gh": fake_gh, "git": fake_git, "curl": fake_curl, "security": fake_security}


def main(binary: str) -> int:
    return MAIN[binary](sys.argv[1:])

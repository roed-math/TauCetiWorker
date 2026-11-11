#!/usr/bin/env python3
"""A bubble round on a branch behind main fetches the mapping of main's newest cached commit before the
agent starts, so merging main restores main's outputs instead of compiling what main changed.

The bootstrap runs in a throwaway clone against a fake `lake` and a stand-in main-cache-rev:
  * branch behind origin/main: main-cache-rev is asked, and Lake fetches that commit's mappings only,
    with main's toolchain, after the branch's own fetch and before the preliminary build;
  * main-cache-rev finding nothing, or the fetch failing, warns and still launches the agent;
  * branch already containing origin/main: nothing extra is asked or fetched.
Exit 0 = all hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


tmp = Path(tempfile.mkdtemp(prefix="bubble-main-cache-"))
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


origin = tmp / "origin.git"
git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
seed = tmp / "seed"
git(tmp, "clone", "-q", str(origin), str(seed))
(seed / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc2\n")
git(seed, "add", "lean-toolchain")
git(seed, "commit", "-q", "-m", "base")
git(seed, "push", "-q", "origin", "HEAD:main")

work = tmp / "work"
git(tmp, "clone", "-q", str(origin), str(work))
git(work, "checkout", "-q", "-b", "pr")
(work / "pr.txt").write_text("pr")
git(work, "add", "pr.txt")
git(work, "commit", "-q", "-m", "pr")

shims = tmp / "bin"
shims.mkdir()
(shims / "lake").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAKE_CALLS"\n'
                            'case "$*" in *--rev=*) exit "${MAIN_FETCH_RC:-0}" ;; esac\nexit 0\n')
(shims / "lake").chmod(0o755)
round_dir = tmp / "round"
round_dir.mkdir()
(round_dir / "main-cache-rev").write_text(
    "import os, sys\n"
    "open(os.environ['PROBE_CALLS'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    "rev = os.environ.get('MAIN_REV')\n"
    "if not rev:\n"
    "    sys.exit(print('main-cache-rev: no published outputs', file=sys.stderr) or 1)\n"
    "print(rev)\n"
    "print(f'main-cache-rev: {rev} ... (3 newer ones have none)', file=sys.stderr)\n"
)


def run(main_rev="", main_fetch_rc=0):
    calls, probes, marker = tmp / "lake-calls", tmp / "probe-calls", tmp / "agent-ran"
    for p in (calls, probes, marker):
        p.unlink(missing_ok=True)
    env = {**GIT_ENV, "PATH": f"{shims}:{os.environ['PATH']}", "LAKE_CALLS": str(calls),
           "PROBE_CALLS": str(probes), "MAIN_REV": main_rev, "MAIN_FETCH_RC": str(main_fetch_rc),
           "TAUCETI_ROUND_DIR": str(round_dir), "MARKER": str(marker)}
    cmd = tc.bubble_work_cmd("sh -c 'touch \"$MARKER\"'")
    r = subprocess.run(["bash", "-c", cmd], cwd=work, env=env, capture_output=True, text=True)
    read = lambda p: p.read_text().splitlines() if p.exists() else []
    return r, read(calls), read(probes), marker.exists()


# main moves on after the PR branched.
(seed / "main.txt").write_text("newer main")
git(seed, "add", "main.txt")
git(seed, "commit", "-q", "-m", "main moves on")
git(seed, "push", "-q", "origin", "HEAD:main")
main_sha = git(seed, "rev-parse", "HEAD")

r, calls, probes, ran = run(main_rev=main_sha)
fetch = f"cache get --service {tc.TAUCETI_CACHE_SERVICE} --repo {tc.TAUCETI}"
want = f"{fetch} --rev={main_sha} --toolchain=leanprover/lean4:v4.36.0-rc2 --mappings-only"
check("main-cache-rev is asked about origin/main", probes == [f"--service {tc.TAUCETI_CACHE_SERVICE} --repo {tc.TAUCETI}"],
      str(probes))
check("Lake fetches that commit's mappings with main's toolchain", want in calls, str(calls))
check("after the branch's own fetch and before the build",
      calls.index(fetch) < calls.index(want) < calls.index("build") if want in calls and fetch in calls else False,
      str(calls))
check("the log names the commit", f"main cache: outputs of {main_sha}" in r.stderr and "3 newer ones have none" in r.stderr,
      r.stderr)
check("the agent runs", r.returncode == 0 and ran, f"rc={r.returncode}")

r, calls, probes, ran = run(main_rev="")
check("no cached main commit: warns", "no cached outputs fetched for main" in r.stderr and "no published outputs" in r.stderr,
      r.stderr)
check("...fetches nothing for main", not any("--rev=" in c for c in calls), str(calls))
check("...and still runs the agent", r.returncode == 0 and ran)

r, calls, probes, ran = run(main_rev=main_sha, main_fetch_rc=1)
check("a failed fetch for main warns and still runs the agent",
      "no cached outputs fetched for main" in r.stderr and r.returncode == 0 and ran, r.stderr)

git(work, "merge", "-q", "--no-edit", "origin/main")
r, calls, probes, ran = run(main_rev=main_sha)
check("a branch already containing main asks nothing", probes == [] and not any("--rev=" in c for c in calls),
      f"{probes} {calls}")
check("...and runs the agent", r.returncode == 0 and ran)

print("\nALL OK" if not fails else f"\nFAILED ({fails})")
sys.exit(1 if fails else 0)

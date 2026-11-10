#!/usr/bin/env python3
"""TauCeti's uvx entrypoints run inside a sandboxed round, offline, from the worker's TauCetiTools mirror.

2026-11: `scripts/lint-env.sh` is `exec uvx --from git+https://github.com/TauCetiProject/TauCetiTools@<sha>
tauceti-tools script lint-env.sh`, and the bubble image has no uv, no PyPI and no proxy route to
TauCetiTools, so up to half of a day's rounds reported "uvx: not found" and linted with a historical
copy of the script, or not at all. This builds a stand-in TauCetiTools with two commits, mirrors it with
`tools_mirror`, and runs the staged scripts/uvx against it with no network: the pinned commit's console
script runs with its arguments, exit code and stdin; an older pin still resolves; a commit the mirror
lacks, a non-TauCetiTools spec and a missing mirror each fail with exit 2 and a reason. Also checks the
mirror is mounted for a work round (echo path) and not for a review/probe command.

Exit 0 = all checks hold; 1 = a mismatch.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


tmp = Path(tempfile.mkdtemp(prefix="tauceti-uvx-"))
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
upstream = tmp / "TauCetiTools"
pkg = upstream / "tauceti_tools"
pkg.mkdir(parents=True)
git("init", "-q", "-b", "main", cwd=upstream)
(upstream / "pyproject.toml").write_text(
    '[project]\nname = "tauceti-tools"\n[project.scripts]\ntauceti-tools = "tauceti_tools.cli:main"\n'
)
(pkg / "__init__.py").write_text("")
(pkg / "cli.py").write_text(
    "import os, sys\n"
    "def main():\n"
    "    print('v1', sys.argv[0], *sys.argv[1:], sys.stdin.read().strip(), sep='|')\n"
    "    print('cwd-shadow', 'shadow' in sys.modules)\n"
    "    return int(os.environ.get('UVX_TEST_RC', '0'))\n"
)
git("add", "-A", cwd=upstream)
git("commit", "-q", "-m", "v1", cwd=upstream)
old = git("rev-parse", "HEAD", cwd=upstream)
(pkg / "cli.py").write_text((pkg / "cli.py").read_text().replace("'v1'", "'v2'"))
git("commit", "-q", "-am", "v2", cwd=upstream)
new = git("rev-parse", "HEAD", cwd=upstream)

cfg = SimpleNamespace(state=tmp / "state", home=tmp / "home", wid="uvx-test", logdir=tmp / "logs")
os.environ["TAUCETI_TOOLS_URL"] = str(upstream)
mirror = tc.agents.tools_mirror(cfg)
check("tools_mirror creates a bare mirror", mirror is not None and (mirror / "HEAD").is_file())
check("the mirror holds every commit on main", bool(mirror) and git("--git-dir", str(mirror), "rev-parse", "main") == new)
stamp = (mirror / "tauceti-fetched").stat().st_mtime
(pkg / "cli.py").write_text((pkg / "cli.py").read_text().replace("'v2'", "'v3'"))
git("commit", "-q", "-am", "v3", cwd=upstream)
v3 = git("rev-parse", "HEAD", cwd=upstream)
check("a fresh mirror is not refetched inside the TTL", tc.agents.tools_mirror(cfg) == mirror
      and git("--git-dir", str(mirror), "rev-parse", "main") == new)
os.utime(mirror / "tauceti-fetched", (stamp - 3600, stamp - 3600))
tc.agents.tools_mirror(cfg)
check("a stale mirror is refetched", git("--git-dir", str(mirror), "rev-parse", "main") == v3)
os.environ["TAUCETI_TOOLS_URL"] = str(tmp / "gone")
os.utime(mirror / "tauceti-fetched", (stamp - 3600, stamp - 3600))
check("a failed refetch keeps serving the last good mirror", tc.agents.tools_mirror(cfg) == mirror)

# The shim, staged as run_in_bubble stages it, in a checkout whose cwd holds a module it must not import.
rounddir = tmp / "round"
rounddir.mkdir()
shutil.copy(REPO / "scripts" / "uvx", rounddir / "uvx")
os.chmod(rounddir / "uvx", 0o755)
checkout = tmp / "checkout"
checkout.mkdir()
(checkout / "shadow.py").write_text("raise SystemExit('cwd module imported')\n")
(checkout / "tauceti_tools").mkdir()
lint_env = checkout / "lint-env.sh"
pybin = tmp / "py-bin"  # the image's python3 is 3.12; this host's /usr/bin/python3 may predate tomllib
pybin.mkdir()
(pybin / "python3").symlink_to(Path(sys.executable).resolve())
base_path = f"{pybin}:/usr/bin:/bin"
env = {k: v for k, v in os.environ.items() if not k.startswith(("TAUCETI_", "PYTHON"))}
env.update(PATH=f"{rounddir}:{base_path}", TAUCETI_UVX_MIRROR=str(mirror), XDG_CACHE_HOME=str(tmp / "cache"))


def uvx_line(spec_sha, *, url="https://github.com/TauCetiProject/TauCetiTools"):
    return f'exec uvx --from git+{url}@{spec_sha} tauceti-tools script lint-env.sh "$@"\n'


def run(line, *args, stdin="", **extra):
    lint_env.write_text("#!/usr/bin/env bash\n" + line)
    return subprocess.run(
        ["bash", str(lint_env), *args], cwd=checkout, env={**env, **extra}, input=stdin, capture_output=True, text=True
    )


p = run(uvx_line(new), "--only", "x y", stdin="in")
check("the pinned commit's console script runs with its arguments and stdin",
      p.returncode == 0 and p.stdout.splitlines()[0] == "v2|tauceti-tools|script|lint-env.sh|--only|x y|in",
      p.stdout + p.stderr)
check("the checkout's own modules do not shadow the package", "cwd-shadow False" in p.stdout, p.stdout)
p = run(uvx_line(old))
check("an older pin on a stale branch still resolves", p.returncode == 0 and p.stdout.startswith("v1|"), p.stdout + p.stderr)
p = run(uvx_line(new), UVX_TEST_RC="3")
check("the console script's exit code is the shim's", p.returncode == 3, f"rc={p.returncode}")
p = run(f"exec uvx --from=git+https://github.com/TauCetiProject/TauCetiTools.git@{v3} tauceti-tools\n")
check("--from=SPEC and a .git URL are accepted", p.returncode == 0 and p.stdout.startswith("v3|"), p.stdout + p.stderr)
p = run(f"exec uvx --from git+https://github.com/TauCetiProject/TauCetiTools@{new} python -c "
        "'import sys, tauceti_tools.cli as c; print(c.__file__.split(\"/tauceti-uvx/\")[1], sys.argv[1:])' a\n")
check("python as the command imports the pinned package, as uvx's environment python does",
      p.returncode == 0 and p.stdout.strip() == f"{new}/tauceti_tools/cli.py ['a']", p.stdout + p.stderr)
p = run(uvx_line("0" * 40))
check("a commit the mirror lacks fails with a reason",
      p.returncode == 2 and "not in the mirror" in p.stderr, f"rc={p.returncode} {p.stderr}")
p = run(uvx_line(new, url="https://github.com/TauCetiProject/TauCetiReview"))
check("another package is refused when no real uvx is on PATH",
      p.returncode == 2 and "only 'uvx --from" in p.stderr, f"rc={p.returncode} {p.stderr}")
p = run(uvx_line(new[:12]))
check("an abbreviated pin is refused", p.returncode == 2, f"rc={p.returncode}")
p = run(uvx_line(v3), TAUCETI_UVX_MIRROR=str(tmp / "absent"), XDG_CACHE_HOME=str(tmp / "cache2"))
check("a missing mirror fails with a reason", p.returncode == 2 and "not mounted" in p.stderr, p.stderr)
real = tmp / "real-bin"
real.mkdir()
(real / "uvx").write_text("#!/bin/sh\necho real-uvx \"$@\"\n")
os.chmod(real / "uvx", 0o755)
p = run(uvx_line(new, url="https://github.com/TauCetiProject/TauCetiReview"), PATH=f"{rounddir}:{real}:{base_path}")
check("another package goes to a real uvx further down PATH",
      p.returncode == 0 and p.stdout.startswith("real-uvx --from git+https://github.com/TauCetiProject/TauCetiReview@"),
      p.stdout + p.stderr)

# A work round mounts the mirror read-only; a review/probe command (inner_cmd) does not.
saved = (tc.agents.ensure_bubble_home, tc.agents._bubble_pop)
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
os.environ["TAUCETI_AGENT_ECHO"] = "1"
try:
    argvs = []
    for inner in (None, "true"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tc.run_in_bubble(SimpleNamespace(cfg=cfg), tc.TAUCETI, "PROMPT", SimpleNamespace(work_model="claude"),
                             inner_cmd=inner, cred_model="claude")
        argvs.append(out.getvalue())
finally:
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved
    os.environ.pop("TAUCETI_AGENT_ECHO", None)
spec = f"--mount {mirror.resolve()}:{tc.agents.TOOLS_MIRROR_MOUNT}:ro"
check("a work round mounts the mirror read-only", spec in argvs[0], argvs[0][-400:])
check("a review/probe command does not", tc.agents.TOOLS_MIRROR_MOUNT not in argvs[1])
check("uvx is staged in /opt/round", os.access(cfg.state / "bubble-round" / "uvx", os.X_OK))
shutil.rmtree(tmp, ignore_errors=True)
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

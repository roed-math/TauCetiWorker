#!/usr/bin/env python3
"""The review rounds' git wrapper keeps bubble's routing under TauCetiReview's blanked config.

TauCetiReview #146 runs pr_diff's git with GIT_CONFIG_GLOBAL=/dev/null; in a bubble that dropped the
url.insteadOf + token header that route git through bubble's proxy, and every sandboxed review died
("could not compute the diff", 2026-09-26). Offline, against a stand-in git that echoes its argv:
  * with the global config blanked, the wrapper adds exactly the routing settings from ~/.gitconfig,
    the header value (which contains a space) intact, and nothing else from that file;
  * without it, git runs with the caller's argv unchanged;
  * the review round puts the wrapper first on PATH.
Exit 0 = all hold; 1 = a mismatch."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHIM = REPO / "scripts" / "review-git" / "git"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


real_git = shutil.which("git")
T = Path(tempfile.mkdtemp(prefix="review-git-"))
shimdir, fakedir, home = T / "review-git", T / "fake", T / "home"
for d in (shimdir, fakedir, home):
    d.mkdir()
shutil.copy(SHIM, shimdir / "git")
os.chmod(shimdir / "git", 0o755)
# A stand-in git: `config` goes to the real git (the wrapper reads ~/.gitconfig with it); anything else
# prints its argv as JSON.
(fakedir / "git").write_text(f"""#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:2] == ["config"]:
    os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])
print(json.dumps(sys.argv[1:]))
""")
os.chmod(fakedir / "git", 0o755)
(home / ".gitconfig").write_text("""[url "http://10.128.128.1:7654/git/"]
\tinsteadOf = https://github.com/
[http "http://10.128.128.1:7654/"]
\textraHeader = X-Bubble-Token: abc123
[user]
\tname = Someone
""")


def run(blank: bool):
    env = {"PATH": f"{shimdir}{os.pathsep}{fakedir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home)}
    if blank:
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    p = subprocess.run([str(shimdir / "git"), "fetch", "-q", "origin", "abc"], env=env, capture_output=True, text=True)
    return json.loads(p.stdout or "null"), p


got, p = run(blank=True)
check("with the config blanked, the routing comes back as -c options", got == [
    "-c", "url.http://10.128.128.1:7654/git/.insteadof=https://github.com/",
    "-c", "http.http://10.128.128.1:7654/.extraheader=X-Bubble-Token: abc123",
    "fetch", "-q", "origin", "abc"], f"{got} {p.stderr}")
got, p = run(blank=False)
check("otherwise git gets the caller's argv unchanged", got == ["fetch", "-q", "origin", "abc"], f"{got} {p.stderr}")
src = (REPO / "tauceti_worker" / "agents.py").read_text()
check("the review round puts the wrapper first on PATH", '"env PATH=/opt/round/review-git:/opt/round:$PATH "' in src)
check("…and every bubble round stages it", '"scripts" / "review-git" / "git"' in src)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

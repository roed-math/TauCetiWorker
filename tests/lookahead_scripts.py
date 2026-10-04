#!/usr/bin/env python3
"""The wrappers' side of lookahead authoring (tauceti_worker/lookahead.py): a lookahead branch carries
sorry'd stubs by design, so the scripts, not only the prompt, keep it away from pull requests.
  * git-safe-push in a lookahead round (TAUCETI_LOOKAHEAD_BRANCH set) pushes that branch and refuses
    any other;
  * every other round refuses a lookahead/ branch, and any HEAD carrying TauCeti/Lookahead/ or
    LOOKAHEAD.md, while an ordinary branch still pushes;
  * gh-safe-pr-create refuses outright in a lookahead round, before gh runs;
  * lookahead-check passes a well-formed branch and names each violation of the narrower contract:
    a file outside TauCeti/, sorry outside TauCeti/Lookahead/, a bad header, an axiom beside sorryAx.
Proven by mock: a real git against local bare repositories, the fake gh. Exit 0 = all hold."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import SCRIPTS, bare_repo, fake_calls, fake_env, finish, gate_env, mktemp, scrub_env  # noqa: E402
from harness import check as _check  # noqa: E402


def check(name: str, cond, detail: str = "") -> None:
    if not _check(name, cond) and detail:
        print("      " + detail.strip().replace("\n", "\n      "))

scrub_env()
os.environ.pop("TAUCETI_LOOKAHEAD_BRANCH", None)
TMP = mktemp("lookahead-scripts-")
os.environ["TAUCETI_WORKER_ID"] = "w-lookahead"
os.environ["TAUCETI_IDENTITY_OK"] = "fake-login"
os.environ["TAUCETI_FORK"] = "fake-login/TauCeti"
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
FORK = bare_repo(TMP, "fake-login/TauCeti")
URL = "https://github.com/fake-login/TauCeti"
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
SCENARIO = {"gh": [], "git": [], "bare_repos": {"fake-login/TauCeti": str(FORK)}}


def git(work: Path, *args: str) -> str:
    p = subprocess.run(["/usr/bin/git", "-C", str(work), *args], capture_output=True, text=True,
                       env={**os.environ, **GIT_ID})
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()


def repo(name: str, files: dict[str, str]) -> Path:
    work = TMP / name
    subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", str(work)], check=True)
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "x")
    return work


def branches() -> set[str]:
    out = git(TMP, "--git-dir", str(FORK), "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return set(out.split()) if out else set()


def push(work: Path, ref: str, lookahead: str = ""):
    env = fake_env(TMP, SCENARIO, TAUCETI_PUSH_REF=ref, TAUCETI_PUSH_REMOTE=URL)
    env.pop("TAUCETI_PUSH_EXPECT", None)
    if lookahead:
        env["TAUCETI_LOOKAHEAD_BRANCH"] = lookahead
    return subprocess.run([str(SCRIPTS / "git-safe-push"), ref], env=env, cwd=str(work), capture_output=True, text=True)


LA = "lookahead/LocalGaloisGroups/thing"
stubbed = repo("stubbed", {"TauCeti/Lookahead/Thing/Stubs.lean": "theorem x : True := sorry\n", "LOOKAHEAD.md": "h\n"})
p = push(stubbed, "roadmap/other-w1", lookahead=LA)
check("a lookahead round refuses any branch but its own", p.returncode == 75 and "roadmap/other-w1" not in branches())
p = push(stubbed, LA, lookahead=LA)
check("a lookahead round pushes its branch, stubs and all", p.returncode == 0 and LA in branches(), p.stderr[-300:])
plain = repo("plain", {"TauCeti/Thing.lean": "theorem x : True := trivial\n"})
p = push(plain, "lookahead/LocalGaloisGroups/other")
check("an ordinary round refuses a lookahead/ branch", p.returncode == 75
      and "lookahead/LocalGaloisGroups/other" not in branches())
p = push(stubbed, "roadmap/port-w1")
check("an ordinary round refuses a HEAD carrying the stubs", p.returncode == 75 and "roadmap/port-w1" not in branches()
      and "lookahead stubs" in p.stderr)
p = push(plain, "roadmap/plain-w1")
check("an ordinary branch still pushes", p.returncode == 0 and "roadmap/plain-w1" in branches(), p.stderr[-300:])

env = fake_env(TMP, SCENARIO, TAUCETI_LOOKAHEAD_BRANCH=LA)
(TMP / "fake.log").unlink(missing_ok=True)
p = subprocess.run([str(SCRIPTS / "gh-safe-pr-create"), "--repo", "TauCetiProject/TauCeti", "--base", "main", "--head",
                    f"fake-login:{LA}", "--title", "t", "--body", "b"], env=env, cwd=str(TMP), capture_output=True, text=True)
check("gh-safe-pr-create refuses in a lookahead round, before gh runs", p.returncode == 75 and not fake_calls(TMP, "gh"))

# ---- lookahead-check -----------------------------------------------------------------------------------
HEADER = ('<!--tauceti-lookahead:v1 {"area":"A","slug":"thing","main":"abc","status":"complete",'
          '"suppliers":["s"],"splits":[{"n":1,"after":[]}]}-->\n')
FAKE_AXIOMS = TMP / "axioms"


def axioms(lines: list[str], rc: int) -> None:
    FAKE_AXIOMS.write_text("#!/usr/bin/env bash\n" + "".join(f"echo '{ln}' >&2\n" for ln in lines) + f"exit {rc}\n")
    FAKE_AXIOMS.chmod(0o755)


def checked(files: dict[str, str], *, offenders=("  TauCeti.thing → [sorryAx]",), rc=1):
    origin = repo(f"origin-{len(list(TMP.iterdir()))}", {"TauCeti/Base.lean": "-- base\n", "README.md": "r\n"})
    work = TMP / f"clone-{len(list(TMP.iterdir()))}"
    subprocess.run(["/usr/bin/git", "clone", "-q", str(origin), str(work)], check=True)
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    axioms(list(offenders), rc)
    env = {**os.environ, "LOOKAHEAD_CHECK_AXIOMS": str(FAKE_AXIOMS)}
    return subprocess.run([str(SCRIPTS / "lookahead-check")], env=env, cwd=str(work), capture_output=True, text=True)


GOOD = {"TauCeti/Lookahead/Thing/Stubs.lean": "set_option warningAsError false\ntheorem s : True := sorry\n",
        "TauCeti/Foo/Thing.lean": "theorem thing : True := s\n", "LOOKAHEAD.md": HEADER}
p = checked(GOOD)
check("lookahead-check passes stubs, a proof through them, and a header", p.returncode == 0, p.stderr[-400:])
p = checked({**GOOD, "README.md": "changed\n"})
check("…and names a file changed outside TauCeti/", p.returncode == 1 and "README.md" in p.stderr)
p = checked({**GOOD, "TauCeti/Foo/Thing.lean": "theorem thing : True := sorry\n"})
check("…and a sorry outside TauCeti/Lookahead/", p.returncode == 1 and "TauCeti/Foo/Thing.lean" in p.stderr)
p = checked({**GOOD, "TauCeti/Foo/Thing.lean": "set_option warningAsError false\ntheorem thing : True := s\n"})
check("…and warningAsError outside TauCeti/Lookahead/", p.returncode == 1 and "warningAsError" in p.stderr)
p = checked({**GOOD, "LOOKAHEAD.md": "# no header\n"})
check("…and a missing header", p.returncode == 1 and "header" in p.stderr)
p = checked(GOOD, offenders=("  TauCeti.thing → [sorryAx]", "  TauCeti.other → [Lean.ofReduceBool]"))
check("…and an axiom beside sorryAx", p.returncode == 1 and "Lean.ofReduceBool" in p.stderr)
p = checked(GOOD, offenders=("axioms: audited 3 TauCeti declaration(s); all within the allowlist",), rc=0)
check("a clean audit passes too", p.returncode == 0, p.stderr[-300:])
p = checked(GOOD, offenders=("error: unknown executable axioms",), rc=1)
check("an audit that fails without offenders cannot be checked (2)", p.returncode == 2)

finish()

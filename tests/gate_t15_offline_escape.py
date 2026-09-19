#!/usr/bin/env python3
"""T15 — the offline harness tries direct gh, HTTP, SSH and credential-helper access; nothing reaches
a production credential or a remote, exit handlers included (brief §8.2, §8.3 T15).

Under the offline environment (tests/fakes first on PATH, the shims next, a required scratch gate):
  * `gh api -X POST …` through the shim                → 75, the fake gh never spawned
  * `gh auth token` through the shim                   → 75, never spawned
  * `curl https://api.github.com/user`                 → the fake curl: logged, exit 7, no network
  * `git push` by ABSOLUTE PATH to the fake git        → the fake rewrites github.com to a local
                                                         path that does not exist: fails, logged
  * the credential helper with no admission            → refused for a push target (exit 1)
  * `security find-generic-password`                   → never invoked by anything above, and GH_TOKEN
                                                         is absent from every environment used
  * exit handlers: Claims.release goes through the gate (admitted `release` against a local bare
    claims repository; with the store halted, refused and no git at all)
  * `tauceti work --offline --dry-run` runs a whole survey round against the fakes: refuses to start
    with GH_TOKEN set; otherwise its fake log holds only reads, no `security`, no `curl`, no push

Proven by mock (and, for the last item, in the real worker entry point). Exit 0 = all hold.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import (  # noqa: E402
    FAKES,
    SCRIPTS,
    bare_repo,
    check,
    fake_calls,
    fake_env,
    fake_log,
    finish,
    gate_env,
    mktemp,
    scrub_env,
)

scrub_env()
TMP = mktemp("gate-t15-")
gate_env(TMP, TAUCETI_GATE_MUTATION_SPACING="0")
claims = bare_repo(TMP, "alice/tauceti-claims")
SCENARIO = {"gh": [], "git": [], "bare_repos": {"alice/tauceti-claims": str(claims)}}
env = fake_env(
    TMP, SCENARIO, CLAIM_REPO="alice/tauceti-claims", CLAIM_GITDIR=str(TMP / "scratch.git"), TAUCETI_WORKER_ID="w15"
)
env.pop("GH_TOKEN", None)
os.environ.update(
    {
        k: env[k]
        for k in (
            "PATH",
            "TAUCETI_FAKE_SCENARIO",
            "TAUCETI_FAKE_LOG",
            "TAUCETI_REAL_GH",
            "TAUCETI_REAL_GIT",
            "TAUCETI_PYTHON",
            "TAUCETI_GATE_CLI",
            "CLAIM_REPO",
            "CLAIM_GITDIR",
            "TAUCETI_WORKER_ID",
        )
    }
)

from tauceti_worker import gate as G  # noqa: E402
from tauceti_worker import round as round_mod  # noqa: E402

check("gh on PATH is the fake", Path(shutil.which("gh")).resolve() == (FAKES / "gh").resolve())
check("GH_TOKEN is absent from the harness environment", "GH_TOKEN" not in env and "GH_TOKEN" not in os.environ)
# The agent's PATH under --offline: the shims first (host_agent_argv prepends them), then the fakes.
agent_env = {**env, "PATH": f"{SCRIPTS / 'shim'}:{SCRIPTS}:{env['PATH']}"}
check(
    "gh on the agent's PATH is the shim, whose real gh is the fake",
    Path(shutil.which("gh", path=agent_env["PATH"])).resolve() == (SCRIPTS / "shim" / "gh").resolve()
    and agent_env["TAUCETI_REAL_GH"] == str(FAKES / "gh"),
)

# ---- 1. direct attempts through the shims and the fakes ------------------------------------------------------------
p = subprocess.run(
    ["gh", "api", "-X", "POST", "repos/TauCetiProject/TauCeti/issues", "-f", "title=x"],
    env=agent_env,
    capture_output=True,
    text=True,
)
check("shim: gh api -X POST -> 75", p.returncode == 75 and "refused" in p.stderr)
p = subprocess.run(["gh", "auth", "token"], env=agent_env, capture_output=True, text=True)
check("shim: gh auth token -> 75", p.returncode == 75)
check("…neither spawned the (fake) gh", fake_calls(TMP, "gh") == [])
p = subprocess.run(["curl", "-s", "https://api.github.com/user"], env=env, capture_output=True, text=True)
check("curl: the fake answers with exit 7 (no network)", p.returncode == 7 and "offline" in p.stderr)
check("…and it is logged as refused", [e["answered"] for e in fake_log(TMP) if e["bin"] == "curl"] == ["refused"])
work = TMP / "work"
subprocess.run(["/usr/bin/git", "init", "-q", str(work)], check=True)
subprocess.run(
    [
        "/usr/bin/git",
        "-C",
        str(work),
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    ],
    check=True,
)
p = subprocess.run(
    [str(FAKES / "git"), "-C", str(work), "push", "https://github.com/TauCetiProject/TauCeti", "HEAD:refs/heads/evil"],
    env=env,
    capture_output=True,
    text=True,
)
check(
    "git push by absolute path to the fake: fails against a nonexistent local path, no remote",
    p.returncode != 0 and "nonexistent/fake-bare" in p.stderr,
)
p = subprocess.run(
    [str(SCRIPTS / "tauceti-gate"), "credential", "get"],
    env={**env, "TAUCETI_GIT_OP": "git_push"},
    input="protocol=https\nhost=github.com\npath=TauCetiProject/TauCeti.git\n\n",
    capture_output=True,
    text=True,
)
check(
    "credential helper: a push credential for canonical is refused (nothing printed, exit 1)",
    p.returncode == 1 and p.stdout == "",
)
check("…without asking gh", not any(a[:1] == ["auth"] for a in fake_calls(TMP, "gh")))
check("security was never invoked", fake_calls(TMP, "security") == [])
check(
    "nothing in the fake log is an accepted remote operation",
    all(e["answered"] in ("refused", "real-git-local", "unmatched") or e["bin"] == "git" for e in fake_log(TMP)),
)

# ---- 2. exit handlers go through the gate: Claims.release ------------------------------------------------------------
ctx = SimpleNamespace(add_cleanup=lambda fn: None)
claims_obj = round_mod.Claims(SimpleNamespace(wid="w15"), ctx)
rc = round_mod.run_claim_sh(["acquire", "branch/15", "600"], "alice/tauceti-claims")
check("acquire against the local bare claims repo succeeds (rc 0)", rc == 0)
refs = subprocess.run(
    ["/usr/bin/git", "-C", str(claims), "for-each-ref", "refs/tauceti-claims/"], capture_output=True, text=True
).stdout
check("…the lease ref landed in the LOCAL bare repository", "refs/tauceti-claims/branch/15" in refs)
claims_obj.held = ("branch/15", "alice/tauceti-claims")
claims_obj.release()
refs = subprocess.run(
    ["/usr/bin/git", "-C", str(claims), "for-each-ref", "refs/tauceti-claims/"], capture_output=True, text=True
).stdout
check(
    "Claims.release deleted the lease through the gate",
    "refs/tauceti-claims/branch/15" not in refs and claims_obj.held is None,
)
ev = [json.loads(x) for x in (TMP / "gate" / "events.log").read_text().splitlines()]
check(
    "…admitted as git_push release to alice/tauceti-claims",
    any(e.get("decision") == "admit" and e.get("op") == "release" and e.get("kind") == G.GIT_PUSH for e in ev),
)
G.Gate.from_env().halt("test-halt", "simulated")
n = len(fake_calls(TMP, "git"))
claims_obj.held = ("branch/15", "alice/tauceti-claims")
claims_obj.release()
check(
    "with the store halted, release is refused and runs no git (the lease is left to expire)",
    claims_obj.held is None and len(fake_calls(TMP, "git")) == n,
)
G.Gate.from_env().clear_halt()

# ---- 3. `tauceti work --offline` ------------------------------------------------------------------------------------
wenv = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith(("TAUCETI_GATE", "TAUCETI_FAKE", "TAUCETI_REAL", "TAUCETI_OFFLINE", "CLAIM_"))
}
wenv["PATH"] = os.environ["PATH"].split(str(FAKES) + ":", 1)[-1]  # the worker must put the fakes on PATH itself
wenv["PYTHONPATH"] = str(REPO)
wenv["TAUCETI_RUNTIME_STATUS"] = ""
wid = "gate-t15"
p = subprocess.run(
    [
        sys.executable,
        "-m",
        "tauceti_worker",
        "work",
        "--offline",
        "--dry-run",
        "--worker-id",
        wid,
        "--agent",
        "codex",
        "--ignore-quota",
    ],
    env={**wenv, "GH_TOKEN": "x"},
    capture_output=True,
    text=True,
    cwd=str(REPO),
)
check(
    "`tauceti work --offline` refuses to start with GH_TOKEN set",
    p.returncode == 1 and "GH_TOKEN" in p.stderr + p.stdout,
)
p = subprocess.run(
    [
        sys.executable,
        "-m",
        "tauceti_worker",
        "work",
        "--offline",
        "--dry-run",
        "--worker-id",
        wid,
        "--agent",
        "codex",
        "--ignore-quota",
    ],
    env={**wenv, "TAUCETI_OFFLINE": "1"},
    capture_output=True,
    text=True,
    cwd=str(REPO),
)
check("…and when `gh` on PATH is not the fake", p.returncode == 1 and "not the fake" in p.stderr + p.stdout)
state_dir = REPO / "state" / wid
shutil.rmtree(state_dir, ignore_errors=True)
p = subprocess.run(
    [
        sys.executable,
        "-m",
        "tauceti_worker",
        "work",
        "--offline",
        "--dry-run",
        "--worker-id",
        wid,
        "--agent",
        "codex",
        "--ignore-quota",
    ],
    env=wenv,
    capture_output=True,
    text=True,
    cwd=str(REPO),
    timeout=600,
)
out = p.stdout + p.stderr
check("a dry-run round runs to completion offline", p.returncode == 0 and "[dry-run]" in out)
calls = (
    [json.loads(x) for x in (state_dir / "fake-calls.log").read_text().splitlines()]
    if (state_dir / "fake-calls.log").exists()
    else []
)
check(
    "the round's fake log has gh reads only",
    calls
    and all(
        c["argv"][:1] in (["api"], ["pr"], ["issue"], ["repo"]) and "-X" not in c["argv"]
        for c in calls
        if c["bin"] == "gh"
    ),
)
check("…no security, no curl", not any(c["bin"] in ("security", "curl") for c in calls))
check("…no git push", not any(c["bin"] == "git" and "push" in c["argv"] for c in calls))
check("the gate store recorded them under the worker's state", (state_dir / "gate-offline" / "events.log").exists())
check("the offline round never saw GH_TOKEN", "GH_TOKEN" not in wenv)
shutil.rmtree(state_dir, ignore_errors=True)

finish()

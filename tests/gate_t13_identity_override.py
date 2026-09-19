#!/usr/bin/env python3
"""T13 — an environment token overrides the stored identity; SSH points elsewhere (brief §6.1,
§8.3 T13).

  * GH_TOKEN is set and TAUCETI_EXPECT_LOGIN=alice, while the (fake) gh answers `gh api user` with
    `bob`: the identity gate halts with `identity-mismatch`, names GH_TOKEN as the source, writes the
    per-worker halt.json AND halts the fleet store; nothing then publishes (an admit is refused
    `halted`), and no `gh auth …` was ever run — no login-repair loop.
  * the login pin: a store pinned to alice refuses a process that validated as bob (`login`).
  * the agent boundary: host_agent_argv's env carries no GH_TOKEN/GITHUB_TOKEN/CLAIMS_TOKEN, leads
    with the shims, and points GH_CONFIG_DIR at a read-only copy.
  * the shims: `gh auth login` / `gh auth token` → 75 without spawning gh; `git remote set-url
    origin git@github.com:x/y` → 75 without spawning git (an SSH remote would resolve to whichever
    key ~/.ssh/config names, i.e. a different identity).
  * the credential helper: with the fleet halted, `tauceti-gate credential get` prints nothing and
    exits 1 (git gets no credential); with the store healthy it admits a git_read and delegates.

Proven by mock. Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fakes"))

from harness import (  # noqa: E402
    SCRIPTS,
    check,
    fake_calls,
    fake_env,
    finish,
    gate_cli,
    gate_env,
    mktemp,
    scrub_env,
    state,
)

scrub_env()
TMP = mktemp("gate-t13-")
gate_env(TMP)
SCENARIO = {
    "gh": [
        {"match": ["api", "user", "--jq", ".login"], "rc": 0, "stdout": "bob\n"},
        {
            "match": ["auth", "git-credential", "get"],
            "rc": 0,
            "stdout": "protocol=https\nhost=github.com\nusername=x\npassword=fake-token\n",
        },
    ],
    "git": [],
    "bare_repos": {},
}
env = fake_env(TMP, SCENARIO)
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
        )
    }
)

from tauceti_worker import agents, identity  # noqa: E402
from tauceti_worker import gate as G  # noqa: E402

# ---- 1. GH_TOKEN in front of a pinned expectation -----------------------------------------------------------------
os.environ["GH_TOKEN"] = "ghp_" + "x" * 36  # the value is never read; only its presence is reported
os.environ["TAUCETI_EXPECT_LOGIN"] = "alice"
try:
    identity.gate(TMP / "state", "w1", where="loop start")
    halted = False
    msg = ""
except identity.Halted as e:
    halted = True
    msg = str(e)
check(
    "a GH_TOKEN that belongs to bob, with alice expected, halts with identity-mismatch",
    halted and "identity-mismatch" in msg,
)
check("…naming GH_TOKEN as the credential source", "credential source GH_TOKEN" in msg)
rec = json.loads((TMP / "state" / "halt.json").read_text())
check(
    "the per-worker halt.json records login bob, expected alice, source GH_TOKEN",
    (rec["login"], rec["login_expected"], rec["source"]) == ("bob", "alice", "GH_TOKEN"),
)
check(
    "the fleet store is halted too",
    state(TMP).get("state") == G.HALTED_MANUAL and (TMP / "gate" / "halt.json").exists(),
)
check(
    "the token value never reached the halt record",
    "x" * 36 not in json.dumps(rec) and "<redacted>" not in rec["detail"] or "x" * 36 not in rec["detail"],
)
p = gate_cli(["admit", "api_mutation", "pr_create", "TauCetiProject/TauCeti", "--no-wait"], {**env, **os.environ})
check("no production publication: admit refused halted", p.returncode == 75 and "(halted)" in p.stderr)
check(
    "exactly one gh call was made, and it was not `gh auth`",
    fake_calls(TMP, "gh") == [["api", "user", "--jq", ".login"]],
)
os.environ.pop("GH_TOKEN")
os.environ.pop("TAUCETI_EXPECT_LOGIN")

# ---- 2. the login pin in the store ------------------------------------------------------------------------------------
TMP2 = mktemp("gate-t13b-")
gate_env(TMP2)
G._CURRENT = None
os.environ["TAUCETI_IDENTITY_OK"] = "alice"
g = G.Gate.from_env()
a = g.admit("survey", "TauCetiProject/TauCeti", G.API_READ, wait=False)
g.record(a, G.Outcome(ok=True))
check("the first validated login is pinned in state.json", state(TMP2).get("login") == "alice")
os.environ["TAUCETI_IDENTITY_OK"] = "bob"
try:
    g.admit("survey", "TauCetiProject/TauCeti", G.API_READ, wait=False)
    reason = None
except G.GateRefused as e:
    reason = e.reason
check("a process acting as bob against a store pinned to alice is refused `login`", reason == G.R_LOGIN)
os.environ.pop("TAUCETI_IDENTITY_OK")

# ---- 3. the agent boundary environment -----------------------------------------------------------------------------------
os.environ["GH_TOKEN"] = "x"
os.environ["GITHUB_TOKEN"] = "y"
os.environ["CLAIMS_TOKEN"] = "z"
os.environ["TAUCETI_GATE_TOKEN"] = "leak"
agents._agent_credential_dirs.cache_clear()
argv, aenv = agents.host_agent_argv("prompt", "claude")
check(
    "no token variable reaches the agent",
    all(v not in aenv for v in ("GH_TOKEN", "GITHUB_TOKEN", "CLAIMS_TOKEN", "TAUCETI_GATE_TOKEN")),
)
check("the shims lead the agent's PATH", aenv["PATH"].startswith(str(SCRIPTS / "shim") + ":"))
check("the real gh/git are recorded for the shims", aenv["TAUCETI_REAL_GH"] and aenv["TAUCETI_REAL_GIT"])
gh_cfg = Path(aenv.get("GH_CONFIG_DIR", ""))
if gh_cfg.is_dir():
    files = sorted(p.name for p in gh_cfg.iterdir())
    check(
        "GH_CONFIG_DIR is a per-round copy holding only config.yml/hosts.yml", set(files) <= {"config.yml", "hosts.yml"}
    )
    check(
        "…and it is read-only, so `gh auth` could persist nothing there",
        not os.access(gh_cfg, os.W_OK) and all(not os.access(gh_cfg / f, os.W_OK) for f in files),
    )
    try:
        (gh_cfg / "hosts.yml").write_text("oauth_token: stolen\n")
        wrote = True
    except OSError:
        wrote = False
    check("writing hosts.yml in the copy fails", not wrote)
else:
    check("GH_CONFIG_DIR: no operator gh config on this host, nothing to copy (skipped)", True)
git_cfg = Path(aenv["GIT_CONFIG_GLOBAL"])
body = git_cfg.read_text()
check(
    "GIT_CONFIG_GLOBAL is a per-round copy whose github.com credential helper is the gate's",
    "helper = !tauceti-gate credential" in body and '[credential "https://github.com"]' in body,
)
for v in ("GH_TOKEN", "GITHUB_TOKEN", "CLAIMS_TOKEN", "TAUCETI_GATE_TOKEN"):
    os.environ.pop(v, None)

# ---- 4. the shims refuse sign-in and SSH remotes without spawning anything ------------------------------------------------
n_gh, n_git = len(fake_calls(TMP, "gh")), len(fake_calls(TMP, "git"))
for args in (["auth", "login"], ["auth", "token"], ["auth", "refresh"], ["auth", "setup-git"]):
    p = subprocess.run([str(SCRIPTS / "shim" / "gh"), *args], env=env, capture_output=True, text=True)
    check(f"gh shim: gh {' '.join(args)} -> 75", p.returncode == 75 and "refused" in p.stderr)
p = subprocess.run(
    [str(SCRIPTS / "shim" / "git"), "remote", "set-url", "origin", "git@github.com:x/y"],
    env=env,
    capture_output=True,
    text=True,
)
check("git shim: remote set-url to an SSH URL -> 75", p.returncode == 75 and "ssh" in p.stderr.lower())
p = subprocess.run(
    [str(SCRIPTS / "shim" / "git"), "remote", "add", "evil", "ssh://git@github.com/x/y"],
    env=env,
    capture_output=True,
    text=True,
)
check("git shim: remote add with ssh:// -> 75", p.returncode == 75)
p = subprocess.run(
    [str(SCRIPTS / "shim" / "git"), "remote", "add", "other", "https://gitlab.example/x/y"],
    env=env,
    capture_output=True,
    text=True,
)
check("git shim: remote add to another host -> 75", p.returncode == 75)
check("none of them spawned gh or git", (len(fake_calls(TMP, "gh")), len(fake_calls(TMP, "git"))) == (n_gh, n_git))

# ---- 5. the credential helper obeys the gate ---------------------------------------------------------------------------------
cred_env = {**env, "TAUCETI_GATE_DIR": str(TMP / "gate"), "TAUCETI_GATE_REQUIRED": "1"}  # the halted store
p = subprocess.run(
    [str(SCRIPTS / "tauceti-gate"), "credential", "get"],
    env=cred_env,
    input="protocol=https\nhost=github.com\npath=alice/TauCeti.git\n\n",
    capture_output=True,
    text=True,
)
check("credential get against a halted store: prints nothing, exits 1", p.returncode == 1 and p.stdout == "")
check("…and never asked gh for a token", not any(a[:2] == ["auth", "git-credential"] for a in fake_calls(TMP, "gh")))
cred_env["TAUCETI_GATE_DIR"] = str(TMP2 / "gate")  # a healthy store
cred_env["TAUCETI_FAKE_LOG"] = str(TMP2 / "fake.log")
p = subprocess.run(
    [str(SCRIPTS / "tauceti-gate"), "credential", "get"],
    env=cred_env,
    input="protocol=https\nhost=github.com\npath=alice/TauCeti.git\n\n",
    capture_output=True,
    text=True,
)
check(
    "credential get against a healthy store: admitted as git_read, delegated to gh",
    p.returncode == 0 and "password=fake-token" in p.stdout,
)
ev = [json.loads(x) for x in (TMP2 / "gate" / "events.log").read_text().splitlines()]
check(
    "…and recorded as a git_read of alice/tauceti",
    any(
        e.get("decision") == "admit"
        and e.get("kind") == G.GIT_READ
        and e.get("op") == "credential"
        and e.get("target") == "alice/tauceti"
        for e in ev
    ),
)
p = subprocess.run(
    [str(SCRIPTS / "tauceti-gate"), "credential", "get"],
    env={**cred_env, "TAUCETI_GIT_OP": "git_push"},
    input="protocol=https\nhost=github.com\npath=someone/else.git\n\n",
    capture_output=True,
    text=True,
)
check(
    "a push credential for a repository outside the allowlist is refused (exit 1, nothing printed)",
    p.returncode == 1 and p.stdout == "",
)

finish()

"""Shared setup for the tests/gate_t*.py acceptance tests (brief §8.3): a scratch gate store with the
pilot budgets configured, the fakes on PATH, the worker's own interpreter for the CLI, and the small
assertion/CLI helpers every one of them uses. Imported as `from harness import …` after the test puts
tests/fakes on sys.path; not a test itself."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FAKES = Path(__file__).resolve().parent
REPO = FAKES.parent.parent
SCRIPTS = REPO / "scripts"
SHIM = SCRIPTS / "shim"

fails = 0


def check(name: str, cond) -> bool:
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")
    return bool(cond)


def finish() -> None:
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    sys.exit(1 if fails else 0)


def scrub_env() -> None:
    """No inherited gate, claim, token or identity state may leak into a test."""
    for var in list(os.environ):
        if var.startswith(("TAUCETI_GATE", "TAUCETI_FAKE", "TAUCETI_CLAIM", "TAUCETI_PUSH")) or var in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "CLAIMS_TOKEN",
            "CLAIM_REPO",
            "TAUCETI_IDENTITY_OK",
            "TAUCETI_EXPECT_LOGIN",
            "TAUCETI_FORK",
            "TAUCETI_GIT_OP",
            "TAUCETI_RUNTIME_STATUS",
            "TAUCETI_WORKER_ID",
            "TAUCETI_REAL_GH",
            "TAUCETI_REAL_GIT",
            "TAUCETI_OFFLINE",
        ):
            os.environ.pop(var, None)


PILOT = {
    "TAUCETI_GATE_MUTATIONS_PER_HOUR": "40",
    "TAUCETI_GATE_READS_PER_HOUR": "600",
    "TAUCETI_GATE_ADMIT_WAIT": "2",
}


def gate_env(tmp: Path, **overrides: str) -> dict[str, str]:
    """A fresh required gate store under `tmp` with the pilot budgets, exported into os.environ (the
    in-process Gate reads os.environ) and returned for subprocesses."""
    gate_dir = tmp / "gate"
    env = {
        "TAUCETI_GATE_DIR": str(gate_dir),
        "TAUCETI_GATE_REQUIRED": "1",
        **PILOT,
        **overrides,
    }
    os.environ.update(env)
    return {**os.environ}


def fake_env(tmp: Path, scenario: dict, **extra: str) -> dict[str, str]:
    """The environment of a process that must see only the fakes: PATH leads with tests/fakes, then the
    shims, then scripts; TAUCETI_REAL_GH/GIT point at the fakes; the scenario and log are under `tmp`."""
    sc = tmp / "scenario.json"
    sc.write_text(json.dumps(scenario))
    (tmp / "scenario.json.consumed").unlink(missing_ok=True)
    env = {
        **os.environ,
        "PATH": f"{FAKES}:{SHIM}:{SCRIPTS}:{os.environ.get('PATH', '')}",
        "TAUCETI_FAKE_SCENARIO": str(sc),
        "TAUCETI_FAKE_LOG": str(tmp / "fake.log"),
        "TAUCETI_REAL_GH": str(FAKES / "gh"),
        "TAUCETI_REAL_GIT": str(FAKES / "git"),
        "TAUCETI_PYTHON": sys.executable,
        "TAUCETI_GATE_CLI": str(SCRIPTS / "tauceti-gate"),
        "PYTHONPATH": str(REPO),
        **extra,
    }
    return env


def fake_log(tmp: Path) -> list[dict]:
    p = tmp / "fake.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def fake_calls(tmp: Path, binary: str | None = None) -> list[list[str]]:
    return [e["argv"] for e in fake_log(tmp) if binary is None or e["bin"] == binary]


def sh(argv: list[str], env: dict[str, str], *, cwd: Path | None = None, input_text: str | None = None):
    return subprocess.run(
        argv, env=env, cwd=str(cwd) if cwd else None, capture_output=True, text=True, input=input_text
    )


def gate_cli(args: list[str], env: dict[str, str]):
    return sh([sys.executable, "-m", "tauceti_worker", "gate", *args], {**env, "PYTHONPATH": str(REPO)})


def events(tmp: Path) -> list[dict]:
    p = tmp / "gate" / "events.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def state(tmp: Path) -> dict:
    """state.json, or the RUNNING a store that has never transitioned is in."""
    p = tmp / "gate" / "state.json"
    return json.loads(p.read_text()) if p.exists() else {"state": "RUNNING"}


def bare_repo(tmp: Path, name: str) -> Path:
    """A local bare repository the fake git pushes to in place of https://github.com/<name>."""
    path = tmp / (name.replace("/", "__") + ".git")
    subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(path)], check=True)
    return path


def mktemp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def wait_for(cond, timeout: float = 5.0, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())

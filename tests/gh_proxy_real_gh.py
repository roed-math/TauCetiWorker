#!/usr/bin/env python3
"""The transport's gh proxy finds the real gh without $TAUCETI_REAL_GH.

bubble writes the PATH of the worker that (re)installs its auth proxy into the daemon's systemd unit, so
the daemon's `gh auth token` reaches the proxy with the proxy's directory first on PATH and no
TAUCETI_REAL_GH (2026-10-16: KeyError, the daemon restart-looped and every round failed preflight).
Exit 0 = all hold."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_transport as transport  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("[OK ] " if cond else "[BAD] ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    real_dir = tmp / "real"
    real_dir.mkdir()
    real = real_dir / "gh"
    real.write_text('#!/bin/sh\necho "real gh: $*"\n')
    real.chmod(0o755)
    os.environ["TAUCETI_GITHUB_SHARED_DIR"] = str(tmp / "shared")
    os.environ["TAUCETI_REAL_GH"] = str(real)
    os.environ["PATH"] = f"{real_dir}{os.pathsep}{os.environ['PATH']}"
    transport._install_proxy(bindir=tmp / "bin")
    proxy = tmp / "bin" / "gh"
    check("the proxy leads PATH", os.environ["PATH"].split(os.pathsep)[0] == str(tmp / "bin"))

    env = {k: v for k, v in os.environ.items() if k != "TAUCETI_REAL_GH"}
    p = subprocess.run([str(proxy), "auth", "token"], env=env, capture_output=True, text=True, timeout=60)
    check(
        "`gh auth token` through the proxy reaches the real gh without TAUCETI_REAL_GH",
        p.returncode == 0 and p.stdout.strip() == "real gh: auth token",
        p.stdout + p.stderr,
    )

    os.environ.pop("TAUCETI_REAL_GH")
    check("real_gh skips the proxy on PATH", transport.real_gh() == str(real), str(transport.real_gh()))
    env["PATH"] = str(tmp / "bin")
    p = subprocess.run([str(proxy), "auth", "token"], env=env, capture_output=True, text=True, timeout=60)
    check(
        "with no other gh on PATH it says so instead of calling itself",
        p.returncode != 0 and "no gh on PATH" in p.stderr,
        p.stdout + p.stderr,
    )

print("PASS" if not fails else f"FAIL: {fails} failure(s)")
sys.exit(1 if fails else 0)

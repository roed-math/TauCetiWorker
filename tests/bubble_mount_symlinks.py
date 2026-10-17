#!/usr/bin/env python3
"""Bubble mounts reach Incus with their symlinks resolved.

An incus-user project resolves a disk source inside its allowed path, so the absolute symlink
`state/<id>/refs/roadmap` (into the shared repository store) was refused as "resolves outside of restricted
parent source path", and every review and authoring round failed. Exit 0 = all hold."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.agents import resolved_mount  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("[OK ] " if cond else "[BAD] ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    snapshot = root / "store" / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    link = root / "refs" / "roadmap"
    link.parent.mkdir()
    os.symlink(snapshot, link)
    check(
        "a symlinked source becomes its target",
        resolved_mount(f"{link}:/opt/roadmap:ro") == f"{snapshot}:/opt/roadmap:ro",
        resolved_mount(f"{link}:/opt/roadmap:ro"),
    )
    check("a plain source is unchanged", resolved_mount(f"{snapshot}:/opt/x:rw") == f"{snapshot}:/opt/x:rw")
    check("the mode survives a two-part spec", resolved_mount(f"{link}:/opt/roadmap") == f"{snapshot}:/opt/roadmap")
    check(
        "a spec that is not a host path is left alone", resolved_mount("named-volume:/opt/v") == "named-volume:/opt/v"
    )

print("PASS" if not fails else f"FAIL: {fails} failure(s)")
sys.exit(1 if fails else 0)

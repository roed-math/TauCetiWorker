"""A read-only Mathlib build per Mathlib revision, shared by every sandboxed round on the host.

A bubble round unpacks Mathlib's build (6.8 GB) into its own fresh container, so a busy host wrote it once
per round and held one copy per container in the page cache (mimir, 2026-10-17: ~33 GB written per round,
page-ins 15 times the one-fleet rate). The store holds one unpacked build per revision, `<store>/<rev>/`:
a git worktree of a Mathlib clone at `rev` after `lake exe cache get` and a no-build replay, marked READY.
The replay matters: Lake rewrites a few `.hash` files the first time it replays an unpacked build, which a
read-only mount refuses ("permission denied ... Pi.olean.server.hash"). Rounds mount the store read-only
and scripts/link-mathlib-store points each matching package's `.lake/build` at it."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

READY = ".tauceti-store-ready"


def ready(store: Path, rev: str) -> bool:
    return (store / rev / READY).is_file()


def _writable(path: Path, on: bool) -> None:
    def flip(p: Path) -> None:
        if not p.is_symlink():
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IWUSR if on else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    flip(path)
    for d, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            flip(Path(d) / name)


def build(store: Path, rev: str, clone: Path, cache_dir: Path, log=print) -> bool:
    """Make `<store>/<rev>` ready from `clone` (a Mathlib clone with `rev` fetched) and the `.ltar` cache in
    `cache_dir`. Idempotent; a failed attempt leaves no READY marker and is retried from scratch."""
    if ready(store, rev):
        return True
    target = store / rev
    remove(store, rev, clone)
    store.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(target), rev], check=True)
    env = {**os.environ, "MATHLIB_CACHE_DIR": str(cache_dir)}
    for argv in (["lake", "exe", "cache", "get"], ["lake", "build", "--no-build", "Mathlib"]):
        p = subprocess.run(argv, cwd=target, env=env, capture_output=True, text=True, timeout=3 * 3600)
        if p.returncode != 0:
            log(f"mathlib store {rev[:12]}: `{' '.join(argv)}` failed: {(p.stderr or p.stdout).strip()[-300:]}")
            return False
    for build_dir in [target / ".lake" / "build", *(target / ".lake" / "packages").glob("*/.lake/build")]:
        _writable(build_dir, False)
    (target / READY).write_text(rev + "\n")
    log(f"mathlib store {rev[:12]}: ready")
    return True


def remove(store: Path, rev: str, clone: Path) -> None:
    target = store / rev
    if target.exists():
        _writable(target, True)
        shutil.rmtree(target)
    subprocess.run(["git", "-C", str(clone), "worktree", "prune"], capture_output=True)


def prune(store: Path, keep: set[str], clone: Path, log=print) -> None:
    """Remove every revision but `keep`: the store is ~7 GB a revision."""
    if not store.is_dir():
        return
    for d in sorted(store.iterdir()):
        if d.is_dir() and d.name not in keep:
            remove(store, d.name, clone)
            log(f"mathlib store {d.name[:12]}: removed (no longer pinned)")

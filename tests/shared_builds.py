#!/usr/bin/env python3
"""Work rounds can share a read-only Mathlib build per revision and keep their own TauCeti build.

A bubble round used to write ~33 GB into its fresh container, Mathlib's unpacked build (6.8 GB) and
TauCeti's (8.8 GB) among it, and to hold a private copy of each in the page cache (mimir, 2026-10-17).
Checked here, offline:
  * scripts/link-mathlib-store links a package's build only when the workspace pins the store's revision
    of it and the store is marked ready, and never replaces a build already there;
  * mathlib_store.build makes a revision ready (with a fake lake) and leaves its builds read-only;
    prune removes the revisions no longer pinned;
  * shared_build_mounts is empty unless asked, and run_in_bubble mounts the store read-only and the
    worker's build read-write for a work round only;
  * the bootstrap links them in before `lake exe cache get`.
Exit 0 = all hold."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import mathlib_store  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


def manifest(path: Path, revs: dict) -> None:
    path.write_text(json.dumps({"packages": [{"name": n, "rev": r} for n, r in revs.items()]}))


tmp = Path(tempfile.mkdtemp(prefix="tauceti-shared-builds-")).resolve()
LINK = REPO / "scripts" / "link-mathlib-store"

# ---- the in-container linker
store = tmp / "store"
s = store / "R1"
for d in (
    s / ".lake" / "build",
    s / ".lake" / "packages" / "batteries" / ".lake" / "build",
    s / ".lake" / "packages" / "Cli" / ".lake" / "build",
):
    d.mkdir(parents=True)
manifest(s / "lake-manifest.json", {"batteries": "B1", "Cli": "C-old"})
ws = tmp / "ws"
for name in ("mathlib", "batteries", "Cli", "aesop"):
    (ws / ".lake" / "packages" / name).mkdir(parents=True)
manifest(ws / "lake-manifest.json", {"mathlib": "R1", "batteries": "B1", "Cli": "C-new", "aesop": "A1"})


def link():
    return subprocess.run([sys.executable, str(LINK), str(store)], cwd=ws, capture_output=True, text=True)


p = link()
check(
    "a store that is not ready links nothing",
    "no build for R1" in p.stdout and not (ws / ".lake/packages/mathlib/.lake/build").exists(),
    p.stdout + p.stderr,
)
(s / mathlib_store.READY).write_text("R1\n")
p = link()
mb, bb = ws / ".lake/packages/mathlib/.lake/build", ws / ".lake/packages/batteries/.lake/build"
check(
    "mathlib and a dependency pinned alike are linked",
    mb.is_symlink()
    and mb.resolve() == (s / ".lake/build")
    and bb.is_symlink()
    and bb.resolve() == (s / ".lake/packages/batteries/.lake/build"),
    p.stdout + p.stderr,
)
check("a dependency pinned differently is not", not (ws / ".lake/packages/Cli/.lake/build").exists())
check("nor one the store lacks", not (ws / ".lake/packages/aesop/.lake/build").exists())
own = ws / ".lake/packages/Cli/.lake/build"
own.mkdir(parents=True)
manifest(ws / "lake-manifest.json", {"mathlib": "R1", "batteries": "B1", "Cli": "C-old"})
link()
check("a build already there is never replaced", own.is_dir() and not own.is_symlink())
manifest(ws / "lake-manifest.json", {"mathlib": "R2"})
check("a revision the store does not hold links nothing", "no build for R2" in link().stdout)

# ---- building a revision on the host, with a fake lake
clone = tmp / "mathlib"
subprocess.run(["git", "init", "-q", str(clone)], check=True)
subprocess.run(
    ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
    check=True,
)
rev = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
fake = tmp / "bin"
fake.mkdir()
(fake / "lake").write_text(
    '#!/bin/sh\necho "$@" >> lake.log\nmkdir -p .lake/build .lake/packages/batteries/.lake/build\n'
    "touch .lake/build/M.olean .lake/packages/batteries/.lake/build/B.olean\n"
)
(fake / "lake").chmod(0o755)
os.environ["PATH"] = f"{fake}{os.pathsep}{os.environ['PATH']}"
built = tmp / "built"
notes = []
ok = mathlib_store.build(built, rev, clone, tmp / "ltar", log=notes.append)
target = built / rev
check("a revision is built and marked ready", ok and mathlib_store.ready(built, rev), "; ".join(notes))
check(
    "…by an unpack and then a no-build replay",
    (target / "lake.log").read_text().splitlines() == ["exe cache get", "build --no-build Mathlib"],
    (target / "lake.log").read_text(),
)
check(
    "…with its builds read-only",
    not os.access(target / ".lake/build/M.olean", os.W_OK)
    and not os.access(target / ".lake/packages/batteries/.lake/build", os.W_OK),
)
check(
    "a ready revision is not rebuilt",
    mathlib_store.build(built, rev, clone, tmp / "ltar") and len((target / "lake.log").read_text().splitlines()) == 2,
)
mathlib_store.prune(built, keep=set(), clone=clone, log=notes.append)
check("prune removes a revision no longer pinned", not target.exists(), "; ".join(notes))
(fake / "lake").write_text("#!/bin/sh\nexit 1\n")
check(
    "a failed unpack is not marked ready",
    not mathlib_store.build(built, rev, clone, tmp / "ltar", log=notes.append) and not mathlib_store.ready(built, rev),
)

# ---- the mounts and the bootstrap
for var in ("TAUCETI_MATHLIB_STORE", "TAUCETI_PERSISTENT_BUILD"):
    os.environ.pop(var, None)
cfg = SimpleNamespace(state=tmp / "state", home=tmp / "home", wid="sb-test", logdir=tmp / "logs")
check("no shared builds unless asked", tc.agents.shared_build_mounts(cfg) == [])
os.environ["TAUCETI_MATHLIB_STORE"] = str(store)
os.environ["TAUCETI_PERSISTENT_BUILD"] = "1"
mounts = tc.agents.shared_build_mounts(cfg)
check(
    "the store read-only and the worker's build read-write",
    mounts == [f"{store}:/opt/mathlib-store:ro", f"{cfg.state / 'tauceti-build'}:/opt/tauceti-build:rw"],
    str(mounts),
)

saved = (tc.agents.ensure_bubble_home, tc.agents._bubble_pop)
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
os.environ["TAUCETI_AGENT_ECHO"] = "1"


def echo(**kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tc.run_in_bubble(SimpleNamespace(cfg=cfg), tc.TAUCETI, "PROMPT", SimpleNamespace(work_model="claude"), **kw)
    return out.getvalue()


try:
    work, review = echo(), echo(inner_cmd="true")
finally:
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved
check("a work round mounts both", "/opt/mathlib-store:ro" in work and "/opt/tauceti-build:rw" in work)
check("a review round mounts neither", "/opt/mathlib-store" not in review and "/opt/tauceti-build" not in review)
check("the linker is staged beside the round scripts", (cfg.state / "bubble-round" / "link-mathlib-store").is_file())
boot = tc.agents.bubble_work_cmd("agent")
check(
    "Lake's artifact cache is left unset, in the bootstrap and in the agent's login shells",
    boot.index("unset LAKE_ARTIFACT_CACHE") < boot.index("lake exe cache get")
    and ".profile" in boot
    and ".bashrc" in boot,
)
check(
    "the bootstrap links the builds before it unpacks",
    boot.index("link-mathlib-store") < boot.index("lake exe cache get")
    and boot.index("/opt/tauceti-build") < boot.index("lake exe cache get"),
)
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

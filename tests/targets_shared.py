#!/usr/bin/env python3
"""A target list shared by several fleets, or by several hosts through its repository.

`update_targets` writes a stage's edit under the list's lock, merged three ways with whatever changed
since the stage read the list. Checked here against real git repositories (a bare `origin` and two
clones, offline, the gate disabled):

  - edits to different items merge, and the result is committed;
  - edits to the same item are not written, and the list is left as the other writer left it;
  - an edit the list already has is not written again;
  - a clone behind its upstream is brought up to it first, so another host's curation is kept and
    the merged list is pushed;
  - a push rejected because the upstream moved is retried once after a sync;
  - a local commit that conflicts with the upstream is left in place, the rebase aborted, and a
    `targets-diverged` incident recorded for the owner;
  - a hand edit not yet committed is never rebased over;
  - a writer waits for the lock another holds.

Exit 0 = all hold; 1 = a mismatch."""

import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
for var in ("TAUCETI_GATE_DIR", "TAUCETI_GATE_REQUIRED"):
    os.environ.pop(var, None)
from tauceti_worker import interaction  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-targets-shared-"))
interaction.incidents_dir = lambda: TMP / "incidents"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
os.environ.update(GIT_ENV)

LIST = """# targets
<!-- tauceti-targets:v1 -->

## Area
- [~] `one` — L0 (needs: none; in flight: #1)
- [ ] `two` — L0 (needs: none)
- [ ] `three` — L0 (needs: none)
- [ ] `four` — L0 (needs: none)
- [~] `five` — L0 (needs: none; in flight: #5)
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def edit(text, slug, new_line):
    out = [new_line if f"`{slug}`" in ln else ln for ln in text.splitlines()]
    return "\n".join(out) + "\n"


DONE_ONE = "- [x] `one` — L0 (needs: none; landed: #1)"
DONE_FIVE = "- [x] `five` — L0 (needs: none; landed: #5)"


def fresh(name):
    """A bare origin holding LIST, and two clones of it (two hosts, or the list's shared clone and
    another)."""
    root = TMP / name
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = root / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    (seed / "targets.md").write_text(LIST)
    git(seed, "add", "targets.md")
    git(seed, "commit", "-q", "-m", "list")
    git(seed, "push", "-q", "origin", "HEAD:main")
    a, b = root / "a", root / "b"
    for c in (a, b):
        subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True, capture_output=True)
    return origin, a / "targets.md", b / "targets.md"


# ---- one shared file, no push: the merge itself
os.environ.pop("TAUCETI_TARGETS_PUSH", None)
_origin, path, _ = fresh("merge")
base = path.read_text()
ours = edit(base, "one", DONE_ONE)
path.write_text(edit(base, "five", DONE_FIVE))  # another fleet's curator, while this one worked
git(path.parent, "commit", "-q", "-am", "the other fleet")
check("edits to different items merge", W.update_targets(path, base, ours, ["one done"]) is True)
now = path.read_text()
check("both edits are in the list", DONE_ONE in now and DONE_FIVE in now, now)
# The lock file beside the list is untracked (a list's repository can ignore `.*.lock`).
check("the merge is committed", git(path.parent, "status", "--porcelain", "--untracked-files=no") == ""
      and git(path.parent, "log", "-1", "--format=%s").startswith("curate: one done"))

base = path.read_text()
ours = edit(base, "two", "- [x] `two` — L0 (needs: none; landed: #2)")
theirs = edit(base, "two", "- [~] `two` — L0 (needs: none; in flight: #22)")
path.write_text(theirs)
check("edits to the same item are not written", W.update_targets(path, base, ours, ["two done"]) is False)
check("the other writer's version stands", path.read_text() == theirs)
path.write_text(base)

ours = edit(base, "four", "- [x] `four` — L0 (needs: none; landed: #4)")
path.write_text(ours)
head = git(path.parent, "rev-parse", "HEAD")
check("an edit the list already has is not written again", W.update_targets(path, base, ours, ["four"]) is False
      and git(path.parent, "rev-parse", "HEAD") == head)
path.write_text(base)

# ---- shared through the repository: sync first, then push
os.environ["TAUCETI_TARGETS_PUSH"] = "1"
origin, a, b = fresh("sync")
os.environ["TAUCETI_TARGETS_REPO"] = "example/targets"  # the gate is disabled here; named for completeness
base_a = a.read_text()
b.write_text(edit(b.read_text(), "five", DONE_FIVE))  # the other host curates and pushes first
git(b.parent, "commit", "-q", "-am", "other host")
git(b.parent, "push", "-q", "origin", "HEAD")
ours = edit(base_a, "one", DONE_ONE)
check("a stale clone's edit is written", W.update_targets(a, base_a, ours, ["one done"]) is True)
check("it kept the other host's curation", DONE_FIVE in a.read_text() and DONE_ONE in a.read_text())
check("and pushed the merged list", DONE_ONE in git(origin, "show", "main:targets.md")
      and DONE_FIVE in git(origin, "show", "main:targets.md"))

# A push rejected because the upstream moved after this clone committed: retried after a sync.
origin, a, b = fresh("retry")
a.write_text(edit(a.read_text(), "one", DONE_ONE))
git(a.parent, "commit", "-q", "-am", "curate: one done")
b.write_text(edit(b.read_text(), "five", DONE_FIVE))
git(b.parent, "commit", "-q", "-am", "other host")
git(b.parent, "push", "-q", "origin", "HEAD")
W._push_targets(a)
shown = git(origin, "show", "main:targets.md")
check("a rejected push is retried after a sync", DONE_ONE in shown and DONE_FIVE in shown, shown)

# A local commit that conflicts with the upstream: never resolved by a rule, handed to the owner.
origin, a, b = fresh("diverged")
a.write_text(edit(a.read_text(), "two", "- [x] `two` — L0 (needs: none; landed: #2)"))
git(a.parent, "commit", "-q", "-am", "local")
b.write_text(edit(b.read_text(), "two", "- [~] `two` — L0 (needs: none; in flight: #22)"))
git(b.parent, "commit", "-q", "-am", "other host")
git(b.parent, "push", "-q", "origin", "HEAD")
local = git(a.parent, "rev-parse", "HEAD")
with W._targets_lock(a):
    W._sync_targets(a)
check("a conflicting rebase is aborted, the local commit kept", git(a.parent, "rev-parse", "HEAD") == local
      and not (a.parent / ".git" / "rebase-merge").exists() and "landed: #2" in a.read_text())
incidents = list((TMP / "incidents").glob("targets-diverged-*.json"))
check("and recorded for the owner", len(incidents) == 1, str(incidents))

# A hand edit not yet committed is never rebased over.
origin, a, b = fresh("dirty")
b.write_text(edit(b.read_text(), "five", DONE_FIVE))
git(b.parent, "commit", "-q", "-am", "other host")
git(b.parent, "push", "-q", "origin", "HEAD")
hand = edit(a.read_text(), "three", "- [ ] `three` — L0, reworded by hand (needs: none)")
a.write_text(hand)
with W._targets_lock(a):
    W._sync_targets(a)
check("an uncommitted hand edit is left alone", a.read_text() == hand
      and git(a.parent, "rev-parse", "HEAD") != git(a.parent, "rev-parse", "origin/main"))
check("and it is not mistaken for a divergence", len(list((TMP / "incidents").glob("targets-diverged-*.json"))) == 1)

# ---- the lock: a writer waits for the one holding it
os.environ.pop("TAUCETI_TARGETS_PUSH", None)
_origin, path, _ = fresh("lock")
base = path.read_text()
lock = path.parent / f".{path.name}.lock"
fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o664)
fcntl.flock(fd, fcntl.LOCK_EX)
done = []
t = threading.Thread(target=lambda: done.append(W.update_targets(path, base, edit(base, "one", DONE_ONE), ["one"])))
t.start()
time.sleep(0.5)
check("a writer waits while another holds the lock", not done and path.read_text() == base)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
t.join(10)
check("and writes once it is released", done == [True] and DONE_ONE in path.read_text())

sys.exit(1 if fails else 0)

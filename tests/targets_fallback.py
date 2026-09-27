#!/usr/bin/env python3
"""An author with nothing on its target list works outside it, while there is room (2026-09-27).

The fleet's authors sat idle for hours with every eligible item in flight or blocked. Owner's ruling:
an author with nothing to take from the list authors outside it, as long as the account has at most
six open PRs. Offline checks of `_pick_target`:
  * an eligible, claimable item is still picked from the list;
  * nothing eligible and at most six open PRs of ours: None (author outside the list);
  * nothing eligible and seven: NoProgress, naming both reasons;
  * every eligible item declined or claimed counts as nothing eligible too.
Exit 0 = all hold; 1 = a mismatch."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import interaction  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402
from tauceti_worker.targets import parse_targets  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="targets-fallback-"))
interaction.incidents_dir = lambda: TMP / "incidents"
W._live_target_view = lambda t, path, sv, gh: (t, 0, 0)

ELIGIBLE = parse_targets("""# t
<!-- tauceti-targets:v1 -->

## Area
- [ ] `ready` — L0, "Do it." (serves: B1; needs: none)
""")
BLOCKED = parse_targets("""# t
<!-- tauceti-targets:v1 -->

## Area
- [~] `in-flight` — L0, "Being done." (serves: B1; needs: none; in flight: #1)
- [ ] `waits` — L1, "Needs the first." (serves: B1; needs: `in-flight`)
""")


def sv_with(n):
    return SimpleNamespace(_mine_open_prs=[object()] * n)


w = SimpleNamespace(claims=None, gh=None)
W._claim_target = lambda claims, candidates, path: (candidates[0][0], candidates[0][1], True)

got = W._pick_target(w, sv_with(7), ELIGIBLE, Path("t.md"), "auto", [])
check("an eligible item is still taken from the list", got is not None and got[2].slug == "ready", str(got))

check("nothing eligible, six open PRs of ours: author outside the list",
      W._pick_target(w, sv_with(6), BLOCKED, Path("t.md"), "auto", []) is None)
try:
    W._pick_target(w, sv_with(7), BLOCKED, Path("t.md"), "auto", [])
    check("nothing eligible, seven open PRs: back off", False)
except NoProgress as e:
    check("nothing eligible, seven open PRs: back off, saying why", "not authoring outside the list" in str(e)
          and "7 open PRs" in str(e), str(e))


def all_claimed(claims, candidates, path):
    raise NoProgress("roadmap: every eligible target is claimed by another worker — nothing to author this round")


W._claim_target = all_claimed
check("every eligible item claimed counts as nothing to take",
      W._pick_target(w, sv_with(2), ELIGIBLE, Path("t.md"), "auto", []) is None)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

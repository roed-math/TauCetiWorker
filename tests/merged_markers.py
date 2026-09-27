#!/usr/bin/env python3
"""Merged PRs that carry a target marker, however old the PR (2026-09-25).

`gh pr list --state merged` returns the most recently CREATED merged PRs, so #8199, opened days
earlier and merged at 10:01Z with the `hilbert90` marker, was outside the 200-PR window, and an
author was sent to redo it minutes later. Offline checks:
  * the live view asks for merged PRs with a marker search sorted by update, and a merged marker
    marks its item done in the overlay;
  * the curator writes such merges into the file: `[x]` with `landed: #N`, dropping `in flight:`;
    items already done, and markers for items the list does not have, are left alone.
Exit 0 = all hold; 1 = a mismatch."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.targets import parse_targets  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TEXT = """# t
<!-- tauceti-targets:v1 -->

## ProfiniteCohomology
- [ ] `hilbert90` — L4, "Hilbert 90." (serves: B11a; needs: none)
- [~] `inflation` — L4, "Inflation." (serves: B6; needs: none; in flight: #8300)
- [x] `restriction` — L4, "Restriction." (serves: B6; needs: none; done: #8100)
- [ ] `still-open` — L4, "Something else." (serves: B6; needs: none)
"""


def marker(area, slug):
    return f'<!--tauceti-target:v1 {{"focus":"{area}","id":"{slug}"}}-->'


MERGED = [
    {"number": 8199, "body": "Hilbert 90.\n" + marker("ProfiniteCohomology", "hilbert90")},
    {"number": 8300, "body": marker("ProfiniteCohomology", "inflation")},
    {"number": 8101, "body": marker("ProfiniteCohomology", "restriction")},
    {"number": 8400, "body": marker("SomewhereElse", "not-listed")},
]

# ---- the live view ----------------------------------------------------------------------------------
seen = {}


def pr_list(fields, *, author=None, state="open", search=None):
    seen.update(state=state, search=search)
    return MERGED


live, n_in, n_done = W._live_target_view(parse_targets(TEXT), Path("t.md"), SimpleNamespace(open_prs=[]),
                                         SimpleNamespace(pr_list=pr_list))
check("the live view searches merged PRs by marker, newest update first",
      seen.get("state") == "merged" and "tauceti-target:v1" in (seen.get("search") or "")
      and "sort:updated-desc" in (seen.get("search") or ""), str(seen))
status = {it.slug: it.status for it in live.areas["ProfiniteCohomology"]}
check("a merged marker marks its item done in the overlay", status["hilbert90"] == "done" and status["inflation"] == "done", str(status))
check("an item without a merged marker stays open", status["still-open"] == "open", str(status))

# ---- the curator's file update -------------------------------------------------------------------------
new, changes = W._mark_merged_markers(TEXT, MERGED)
check("an open item merged with its marker becomes [x] with landed: #N",
      "- [x] `hilbert90` — L4, \"Hilbert 90.\" (serves: B11a; needs: none; landed: #8199)" in new, new)
check("an in-flight item is marked done and loses its in-flight clause",
      "- [x] `inflation` — L4, \"Inflation.\" (serves: B6; needs: none; landed: #8300)" in new, new)
check("an item already done is left as written", "- [x] `restriction` — L4, \"Restriction.\" (serves: B6; needs: none; done: #8100)" in new)
check("the untouched item and unlisted markers change nothing else", "- [ ] `still-open`" in new and len(changes) == 2, str(changes))
again, more = W._mark_merged_markers(new, MERGED)
check("a second pass changes nothing", again == new and not more)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

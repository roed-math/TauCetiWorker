#!/usr/bin/env python3
"""A merged PR that carried an item's marker without completing it leaves the item open (2026-10-03).

Authors put the claimed item's marker on every PR, prerequisites included, and each merge marked the
item done: about half of 92 such items were not, and ClassFieldTheory authors were sent to items
whose suppliers (`localArtinEquiv`, `invMap`, …) did not exist. Offline checks:
  * a marker with `"partial": true` is read as such;
  * `mark_partial` / `sync_inflight` record the PR under `partial:` and keep or reopen the item;
  * the curator records a merge as partial when its marker says so, when the item already lists the
    PR, or when an identifier the item names is missing on main, and defers when it cannot tell;
  * the live view does not count partial merges as landings;
  * `_missing_on_main`: a plain name needs a declaration, a capitalised or dotted one an occurrence.
Exit 0 = all hold; 1 = a mismatch."""

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import targets as T  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.survey import partial_marker_ids, target_marker_ids  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


def marker(slug, partial=False):
    extra = ',"partial":true' if partial else ""
    return f'<!--tauceti-target:v1 {{"focus":"CFT","id":"{slug}"{extra}}}-->'


# ---- the marker ------------------------------------------------------------------------------------
body = "x\n" + marker("local-artin-equiv", partial=True) + "\n" + marker("artin-map")
check("a partial marker still identifies its item", ("CFT", "local-artin-equiv") in target_marker_ids(body))
check("only the flagged marker is partial", partial_marker_ids(body) == frozenset({("CFT", "local-artin-equiv")}))

TEXT = """# t
<!-- tauceti-targets:v1 -->

## CFT
- [ ] `local-artin-equiv` — L6, "define `localArtinEquiv` and `normResidue`" (serves: B5; needs: none)
- [~] `inv-map` — L5, "the invariant `invMap`" (serves: B5; needs: none; in flight: #9905)
- [ ] `artin-map` — L7, "the absolute `artinMap`" (serves: B5; needs: none; partial: #10427)
- [ ] `quiet` — L7, "prose only" (serves: B5; needs: none)
"""

# ---- the file edits ----------------------------------------------------------------------------------
new, ok = T.mark_partial(TEXT, "local-artin-equiv", 10405)
check("an open item gets a partial clause and stays open",
      ok and '- [ ] `local-artin-equiv` — L6, "define `localArtinEquiv` and `normResidue`" (serves: B5; needs: none; partial: #10405)' in new, new)
new2, ok2 = T.mark_partial(new, "local-artin-equiv", 10500)
check("a second partial PR joins the clause", ok2 and "(serves: B5; needs: none; partial: #10405, #10500)" in new2, new2)
check("recording a listed PR again changes nothing", T.mark_partial(new2, "local-artin-equiv", 10405) == (new2, False))
reopened, ok = T.mark_partial(TEXT, "inv-map", 9905)
check("the in-flight PR merging partially reopens the item without its in-flight clause",
      ok and '- [ ] `inv-map` — L5, "the invariant `invMap`" (serves: B5; needs: none; partial: #9905)' in reopened, reopened)
by = {it.slug: it for it in T.parse_targets(new2).areas["CFT"]}
check("partial_prs reads the clause", T.partial_prs(by["local-artin-equiv"]) == {10405, 10500} and T.partial_prs(by["quiet"]) == set())
check("the partial clause is metadata, not needs", by["local-artin-equiv"].needs == [])

synced, changes = T.sync_inflight(TEXT, {9905: "MERGED"}, {}, {9905: "main lacks `invMap`"})
check("sync_inflight reopens a partially merged in-flight item",
      "- [ ] `inv-map` — L5, \"the invariant `invMap`\" (serves: B5; needs: none; partial: #9905)" in synced
      and any("reopened" in c and "main lacks `invMap`" in c for c in changes), f"{synced}\n{changes}")
done, _ = T.sync_inflight(TEXT, {9905: "MERGED"}, {})
check("without a reason it is still marked done", "- [x] `inv-map`" in done and "landed: #9905" in done)

# ---- the curator --------------------------------------------------------------------------------------
MERGED = [
    {"number": 10405, "body": marker("local-artin-equiv", partial=True)},
    {"number": 10427, "body": marker("artin-map")},
    {"number": 10459, "body": marker("quiet")},
]
asked = []


def lacks(it):
    asked.append(it.slug)
    return []


out, changes = W._mark_merged_markers(TEXT, MERGED, lacks)
by = {it.slug: it for it in T.parse_targets(out).areas["CFT"]}
check("a flagged marker leaves its item open, recorded as partial",
      by["local-artin-equiv"].status == "open" and T.partial_prs(by["local-artin-equiv"]) == {10405}, out)
check("a PR the item already lists as partial is not its landing", by["artin-map"].status == "open")
check("an unflagged marker whose item main provides is a landing", by["quiet"].status == "done" and "landed: #10459" in by["quiet"].line)
check("main is consulted only for unflagged, unrecorded markers", asked == ["quiet"], str(asked))
check("one change line each for the partial and the landing", len(changes) == 2, str(changes))

out, changes = W._mark_merged_markers(TEXT, [{"number": 10459, "body": marker("quiet")}], lambda it: ["quietThing"])
check("an identifier missing on main makes the merge partial",
      "(serves: B5; needs: none; partial: #10459)" in out and "- [ ] `quiet`" in out and "`quietThing`" in changes[0], f"{out}\n{changes}")
out, changes = W._mark_merged_markers(TEXT, [{"number": 10459, "body": marker("quiet")}], lambda it: None)
check("no answer from main defers the merge", out == TEXT and not changes)
two = [{"number": 10405, "body": marker("local-artin-equiv", partial=True)}, {"number": 10600, "body": marker("local-artin-equiv")}]
out, changes = W._mark_merged_markers(TEXT, two, lambda it: [])
check("a later complete PR still lands the item after a partial one",
      "- [x] `local-artin-equiv`" in out and "partial: #10405; landed: #10600" in out, out)
again, more = W._mark_merged_markers(out, two, lambda it: [])
check("a second pass changes nothing", again == out and not more)

# ---- the live view -----------------------------------------------------------------------------------
live, _n_in, _n_done = W._live_target_view(
    T.parse_targets(TEXT), Path("t.md"), SimpleNamespace(open_prs=[]),
    SimpleNamespace(pr_list=lambda fields, **kw: MERGED))
status = {it.slug: it.status for it in live.areas["CFT"]}
check("the live view counts neither flagged nor listed partial merges",
      status["local-artin-equiv"] == "open" and status["artin-map"] == "open" and status["quiet"] == "done", str(status))

# ---- what main provides ------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    clone = Path(tmp)
    (clone / "TauCeti").mkdir()
    (clone / "TauCeti" / "A.lean").write_text(
        "import Mathlib\n\n/-- `localArtinEquiv` comes later. -/\n"
        "noncomputable def unitsFormation (K : Type) : Additive K := sorry\n"
        "theorem foo_bar : IsUnit (1 : ℤ) := isUnit_one\n")
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    item = T.parse_targets(
        "<!-- tauceti-targets:v1 -->\n## CFT\n- [ ] `x` — L6, \"`unitsFormation`, `localArtinEquiv`, "
        "`Additive`, `IsUnit`, `CFT.cyclotomicCharacter_artinMap`, `foo_bar`\" (needs: none)\n").areas["CFT"][0]
    got = W._missing_on_main(clone, item)
    check("a plain name mentioned only in a docstring, and an absent dotted name, are missing; "
          "declared names and capitalised names in use are not",
          got == ["localArtinEquiv", "CFT.cyclotomicCharacter_artinMap"], str(got))

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

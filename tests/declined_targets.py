#!/usr/bin/env python3
"""Authoring declines are per target, the picker skips them, and the curator settles them.

2026-09-23: two `auto` authors declined 215 rounds in 17 hours, each time because main already had
the target, and each time the same three targets came straight back: every authoring decline was
recorded under one key (`roadmap-unknown`) and nothing fed it back into the pick. Offline checks:
  * a declined authoring round is recorded under its target, one incident per target;
  * the picker drops declined targets, and backs off when nothing else is eligible;
  * the curator puts a declined target to the model with the declarations the agent's account
    names, found on main: "landed" marks the item done, "not landed" hands it back to the authors,
    and an account naming nothing on main is left for the owner.
Exit 0 = all hold; 1 = a mismatch."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import attention, interaction  # noqa: E402
from tauceti_worker import work_units as W  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402
from tauceti_worker.targets import parse_targets  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="tauceti-declined-"))
interaction.incidents_dir = lambda: TMP / "incidents"
ACCOUNTS = {
    "compactness-lemma": "Stopped: already on main as `nonempty_iInter_of_directed_nonempty_isClosed`, from PR #6239.",
    "transgression": "Stopped: main already has `transgressionMap` and its exactness.",
    "pro-p-frattini": "Stopped: I believe this exists already, but I did not find its name.",
    "directed-inter": "Stopped: Mathlib already has `IsCompact.nonempty_iInter_of_directed_isClosed`; a copy would be a duplicate.",
}


def decline(area, slug):
    attention.newest_agent_log = lambda d: TMP / "agent.log"
    attention.final_assistant_text = lambda p: ACCOUNTS[slug]
    return attention.record_declined_round(TMP / "logs", stage="roadmap", pr=0, target=f"{area}/{slug}")


# ---- recording --------------------------------------------------------------------------------------
p1 = decline("ProfiniteProPGroups", "compactness-lemma")
p2 = decline("ProfiniteCohomology", "transgression")
p3 = decline("ProfiniteProPGroups", "pro-p-frattini")
p4 = decline("ProfiniteProPGroups", "directed-inter")
names = sorted(p.name for p in (TMP / "incidents").glob("declined-*.json"))
check("one incident per declined target", names == ["declined-roadmap-ProfiniteCohomology-transgression.json",
                                                    "declined-roadmap-ProfiniteProPGroups-compactness-lemma.json",
                                                    "declined-roadmap-ProfiniteProPGroups-directed-inter.json",
                                                    "declined-roadmap-ProfiniteProPGroups-pro-p-frattini.json"], str(names))
check("declined_targets lists them by slug",
      set(attention.declined_targets()) == {"compactness-lemma", "transgression", "pro-p-frattini", "directed-inter"})

# ---- the picker ---------------------------------------------------------------------------------------
LIST = TMP / "targets.md"
LIST.write_text("""# t
<!-- tauceti-targets:v1 -->

## ProfiniteProPGroups
- [ ] `compactness-lemma` — L0, "Prove the directed intersection lemma." (serves: B1; needs: none)
- [ ] `pro-p-frattini` — L1, "Prove the Frattini quotient statement." (serves: B1; needs: none)
- [ ] `still-open` — L1, "Prove `somethingNew`." (serves: B1; needs: none)
- [ ] `directed-inter` — L1, "A directed family of closed sets has nonempty intersection." (serves: B1; needs: none)

## ProfiniteCohomology
- [ ] `transgression` — L2, "Construct the transgression." (serves: B3; needs: none)
""")
t = parse_targets(LIST.read_text())
cands = [(a, it) for a, items in t.areas.items() for it in items]
kept = W._without_declined(cands)
check("the picker keeps only the undeclined target", [it.slug for _a, it in kept] == ["still-open"], str(kept))
try:
    W._without_declined([c for c in cands if c[1].slug != "still-open"])
    check("nothing but declined targets backs off", False)
except NoProgress as e:
    check("nothing but declined targets backs off", "declined by an author" in str(e), str(e))

# ---- the curator ----------------------------------------------------------------------------------------
os.environ["TAUCETI_ROADMAP_TARGETS"] = str(LIST)
clone = TMP / "state" / "curate" / "TauCeti"
(clone / "TauCeti").mkdir(parents=True)
(clone / "TauCeti" / "Compact.lean").write_text("theorem nonempty_iInter_of_directed_nonempty_isClosed : True := trivial\n")
(clone / "TauCeti" / "Transgression.lean").write_text("def transgressionMap : Nat := 0\n")
# a fake Mathlib at the commit main pins, holding only the lemma the "directed-inter" decline names
ML = TMP / "mathlib-origin"
(ML / "Mathlib" / "Topology").mkdir(parents=True)
(ML / "Mathlib" / "Topology" / "Compact.lean").write_text(
    "theorem IsCompact.nonempty_iInter_of_directed_isClosed : True := trivial\n")
subprocess.run(["git", "-C", str(ML), "init", "-q"], check=True)
subprocess.run(["git", "-C", str(ML), "add", "."], check=True)
subprocess.run(["git", "-C", str(ML), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ml"], check=True)
ml_rev = subprocess.run(["git", "-C", str(ML), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
(clone / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "url": str(ML), "rev": ml_rev}]}))
subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "main"], check=True)
W._curate_main_checkout = lambda w: clone
asked = {}


def fake_agent(cwd, prompt, profile, logdir):
    cand = json.loads((Path(cwd) / "candidates.json").read_text())["candidates"]
    asked.update({c["slug"]: c for c in cand})
    yes = {"compactness-lemma": "TauCeti/Compact.lean:1 `nonempty_iInter_of_directed_nonempty_isClosed` — the lemma",
           "directed-inter": "Mathlib/Topology/Compact.lean:1 `IsCompact.nonempty_iInter_of_directed_isClosed` — in Mathlib"}
    v = {c["slug"]: ({"landed": True, "evidence": yes[c["slug"]]} if c["slug"] in yes
                     else {"landed": False, "evidence": "only the map, no exactness"})
         for c in cand}
    (Path(cwd) / "verdicts.json").write_text(json.dumps(v))
    return 0


W.run_agent_host = fake_agent
W._effective_authoring_profile = lambda opts: "claude"
W._live_target_view = lambda t, path, sv, gh: (t, 0, 0)


class Claims:
    def begin_global_work(self, key):
        return 0

    def release(self):
        pass


class Counters:
    def write(self, k, v):
        pass

    def read(self, k):
        return 0


w = SimpleNamespace(cfg=SimpleNamespace(state=TMP / "state", logdir=TMP / "logs"), gh=SimpleNamespace(pr_view=lambda *a: {}),
                    claims=Claims(), counters=Counters())
rc = W.do_curate(w, SimpleNamespace(open_prs=[]), None, SimpleNamespace(), False)
check("the curator round wrote a change", rc == 0, str(rc))
check("declined targets with declarations on main went to the model",
      {"compactness-lemma", "transgression"} <= set(asked) and "pro-p-frattini" not in asked, str(sorted(asked)))
check("…with the agent's account and the declarations it named",
      "PR #6239" in asked.get("compactness-lemma", {}).get("author_account", "")
      and list(asked.get("compactness-lemma", {}).get("hits", {})) == ["nonempty_iInter_of_directed_nonempty_isClosed"])
new = LIST.read_text()
check("a confirmed decline marks the item done, landed elsewhere",
      "- [x] `compactness-lemma`" in new and "landed elsewhere" in new, new)
check("a decline naming a Mathlib declaration is checked against the pinned Mathlib",
      any(h.startswith("Mathlib:Mathlib/Topology/Compact.lean")
          for hs in asked.get("directed-inter", {}).get("hits", {}).values() for h in hs), str(asked.get("directed-inter")))
check("…and a confirmed one marks the item done", "- [x] `directed-inter`" in new, new)
check("an unconfirmed one stays open", "- [ ] `transgression`" in new and "- [ ] `pro-p-frattini`" in new)
still = set(attention.declined_targets())
check("the refuted decline is handed back to the authors", "transgression" not in still, str(still))
check("the decline that names nothing on main waits for the owner", "pro-p-frattini" in still, str(still))
inc = json.loads(next((TMP / "incidents").glob("targets-updated-*.json")).read_text())
check("the owner is told about it", any("pro-p-frattini" in u and "owner decides" in u for u in inc.get("undecided", [])),
      str(inc.get("undecided")))

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

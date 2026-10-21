#!/usr/bin/env python3
"""A random authoring pick leaves out finished roadmaps, offline (finished_roadmap_areas):
  * an area whose STATUS.md coverage marker says every layer is done, at the README it assessed;
  * still finished after a README edit that heads no new layer (a cross-reference, say);
  * not finished when a layer is open, when the README now heads a layer the report never saw, when
    an edited README's ids cannot be checked ("G1"-style), when a sub-roadmap sits inside, or when
    STATUS.md has no marker.
Run: python3 tests/finished_areas.py. Exit 0 = all hold."""

import hashlib
import importlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
config = importlib.import_module("tauceti_worker.config")
shared = importlib.import_module("tauceti_worker.shared_repositories")

TMP = Path(tempfile.mkdtemp(prefix="finished-areas-"))
ROOT = TMP / "TauCetiRoadmap"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


def area(name, layers, readme, *, assessed=None, sub=False, marker=True):
    d = ROOT / name
    d.mkdir(parents=True)
    (d / "README.md").write_text(readme)
    sha = hashlib.sha256((assessed if assessed is not None else readme).encode()).hexdigest()
    body = {"layers": [{"id": i, "state": s} for i, s in layers], "readme_sha": sha, "roadmap": name}
    (d / "STATUS.md").write_text((f"<!--tauceti-coverage:v1 {json.dumps(body)}-->\n" if marker else "") + "# Status\n")
    if sub:
        (d / "Sub").mkdir()
        (d / "Sub" / "Suggested.lean").write_text("")


TWO = "# R\n## Layer 0: base\n## Layer 1: top\n"
DONE = [("Layer 0", "done"), ("Layer 1", "done")]
area("Done", DONE, TWO)
area("Edited", DONE, TWO + "\nSee also MassFormula.\n", assessed=TWO)
area("Open", [("Layer 0", "done"), ("Layer 1", "partial")], TWO)
area("Grown", DONE, TWO + "## Layer 2: more\n", assessed=TWO)
area("Unspaced", [("G1", "done"), ("G2", "done")], "# R\n## G1\n## G2\nnew text\n", assessed="# R\n## G1\n## G2\n")
area("Parent", DONE, TWO, sub=True)
area("Unassessed", DONE, TWO, marker=False)

shared.snapshot = lambda repo, ttl=0: TMP
got = config.finished_roadmap_areas()
check("every layer done at the assessed README", "Done" in got, str(sorted(got)))
check("a README edit that heads no new layer keeps it finished", "Edited" in got, str(sorted(got)))
check("an open layer is not finished", "Open" not in got)
check("a README heading a new layer is not finished", "Grown" not in got)
check("an edited README with unchecked ids is not finished", "Unspaced" not in got)
check("an area holding a sub-roadmap is not finished", "Parent" not in got)
check("no marker, not finished", "Unassessed" not in got)

shared.snapshot = lambda repo, ttl=0: None
check("no local roadmap repository: nothing is skipped", config.finished_roadmap_areas() == set())

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

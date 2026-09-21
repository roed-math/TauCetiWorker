#!/usr/bin/env python3
"""Everything a sandboxed round's wrappers need is staged beside them under /opt/round.

2026-09-21: the three write wrappers were staged, but not the gate-lib.sh they `source` (nor the
tauceti-gate they exec when the gate is enabled), so inside a bubble every push attempt exited 75 at
the source line and a green, axiom-clean reconciliation of #5508 was thrown away. This test stages
exactly what run_in_bubble stages into a temp dir and (a) checks every `source`/`.` of a sibling in
those scripts names a staged file, and (b) runs git-safe-push and gh-safe-pr-create there with the
gate disabled, which must get past sourcing (never "No such file", never "command not found").

Exit 0 = all checks hold; 1 = a mismatch.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


staged = set(agents.BUBBLE_ROUND_SCRIPTS)
src = REPO / "scripts"
sourced = set()
for name in agents.BUBBLE_ROUND_SCRIPTS:
    text = (src / name).read_text()
    for m in re.finditer(r'^\s*(?:\.|source)\s+"\$\(cd[^)]*\)\s*&&\s*pwd\)/([A-Za-z0-9._-]+)"', text, re.M):
        sourced.add(m.group(1))
check("every sibling the staged scripts source is itself staged", sourced <= staged, f"missing: {sorted(sourced - staged)}")
check("gate-lib.sh and tauceti-gate are staged", {"gate-lib.sh", "tauceti-gate"} <= staged)

tmp = Path(tempfile.mkdtemp(prefix="tauceti-round-"))
for name in agents.BUBBLE_ROUND_SCRIPTS:
    shutil.copy(src / name, tmp / name)
    os.chmod(tmp / name, 0o755)
env = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")}
env["PATH"] = f"{tmp}:{env.get('PATH', '')}"
for script in ("git-safe-push", "gh-safe-pr-create"):
    p = subprocess.run(["bash", str(tmp / script)], cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
    err = p.stderr
    check(f"{script} gets past sourcing with the gate disabled",
          "No such file or directory" not in err and "command not found" not in err and "gate-lib" not in err,
          err.strip().splitlines()[-1][:120] if err.strip() else f"rc={p.returncode}")
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

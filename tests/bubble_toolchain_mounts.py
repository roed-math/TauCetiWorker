#!/usr/bin/env python3
"""A sandboxed round can build after main bumps the Lean toolchain: the host pool's toolchains ride in.

2026-09-21: two reconciliations that merged main (rc1 → rc2) built nothing because the image only had
the PR head's toolchain and the sandbox cannot download one. `pooled_toolchain_mounts()` offers every
complete toolchain under the host's elan pool as a read-only mount at elan's path, and run_in_bubble
passes them as `--mount` flags. Checked through the no-side-effect echo path, as tests/bubble_cache.py
does. Exit 0 = the argv carries exactly the complete toolchains; 1 = a mismatch.
"""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    fails += 0 if cond else 1


tmp = Path(tempfile.mkdtemp(prefix="tauceti-tc-mounts-"))
pool = tmp / "elan"
for name, complete in (("leanprover--lean4---v4.34.0-rc1", True), ("leanprover--lean4---v4.34.0-rc2", True), ("leanprover--lean4---v4.35.0", False)):
    d = pool / "toolchains" / name / "bin"
    d.mkdir(parents=True)
    if complete:
        (d / "lean").write_text("#!/bin/sh\n")
os.environ["ELAN_HOME"] = str(pool)
specs = tc.agents.pooled_toolchain_mounts()
check("complete toolchains are offered, a half-written one is not",
      specs == [f"{pool}/toolchains/leanprover--lean4---v4.34.0-rc1:/home/user/.elan/toolchains/leanprover--lean4---v4.34.0-rc1:ro",
                f"{pool}/toolchains/leanprover--lean4---v4.34.0-rc2:/home/user/.elan/toolchains/leanprover--lean4---v4.34.0-rc2:ro"], str(specs))

cfg = SimpleNamespace(state=tmp / "state", home=tmp / "home", wid="tc-test", logdir=tmp / "logs")
saved = (tc.agents.ensure_bubble_home, tc.agents._bubble_pop)
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
os.environ["TAUCETI_AGENT_ECHO"] = "1"
try:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(SimpleNamespace(cfg=cfg), tc.TAUCETI, "PROMPT", SimpleNamespace(work_model="claude"))
finally:
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved
argv = out.getvalue()
check("echo succeeds", rc == 0)
check("the open command mounts both complete toolchains read-only", all(f"--mount {s}" in argv for s in specs))
check("the incomplete one is not mounted", "v4.35.0" not in argv)
check("gate-lib.sh is staged beside the wrappers", (cfg.state / "bubble-round" / "gate-lib.sh").is_file())
print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""A round whose sandbox drops under the agent hands its attempt back, a few times per head.

On 2026-11-04 one host stall on mimir dropped seven gq2 bubbles in the same minute (`websocket: close
1006`, then `Connection to bubble-… closed by remote host`, ssh exit 255). Such drops had already spent
#22428's and #27295's attempt budgets, so they reached the attention list as "needs a human" with
nothing wrong with either PR. Exit 0 = every case agrees; 1 = a mismatch.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import importlib

import tauceti_worker as tc

agents = tc.agents
wu = tc.work_units
survey = importlib.import_module("tauceti_worker.survey")  # `tc.survey` is the function
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s", "model": "opus"})
DROP = ["Error: Lost connection to the event listener: websocket: close 1006 (abnormal closure): unexpected EOF",
        "Connection to bubble-tauceti-worker-gqw-x1 closed by remote host.",
        "client_loop: send disconnect: Broken pipe"]


def run(lines, rc):
    script = f"import sys; print({chr(10).join(lines)!r}); sys.exit({rc})"
    with tempfile.TemporaryDirectory() as td:
        os.environ[tc.runtime_status.STATUS_ENV] = str(Path(td) / "status.json")
        agents.run_agent_proc([sys.executable, "-c", script], env=dict(os.environ), logdir=Path(td),
                              label="agent-claude", provider="claude")
    return agents.take_last_agent_disconnect(), agents.take_last_agent_infra_failure()


drop, infra = run([INIT, "working…", *DROP], 255)
check("a dropped sandbox after the agent ran is a disconnect", bool(drop), True)
check("...and not a provider outage", infra, None)
check("the agent's own failure is not", run([INIT, "error: build failed"], 1)[0], None)
check("a disconnect line with another exit status is not", run([INIT, *DROP], 1)[0], None)
check("the record is read once", agents.take_last_agent_disconnect(), None)
os.environ.pop(tc.runtime_status.STATUS_ENV, None)

CAND = SimpleNamespace(pr=27295, head="2d47b932faad" + "0" * 28)
KEY = "fix-27295-2d47b932faad"
with tempfile.TemporaryDirectory() as state:
    w = SimpleNamespace(counters=wu.Counters(SimpleNamespace(state=Path(state))))
    w.counters.write(KEY, 1)
    wu._refund_disconnect(w, CAND, "fix", (KEY,))
    check("without a disconnect the attempt stays charged", w.counters.read(KEY), 1)
    for i in range(tc.constants.MAX_DISCONNECT_REFUNDS):
        agents._LAST_AGENT_DISCONNECT = "the sandbox dropped mid-round"
        wu._refund_disconnect(w, CAND, "fix", (KEY,))
        w.counters.incr(KEY)
    check(f"{tc.constants.MAX_DISCONNECT_REFUNDS} disconnects at a head are handed back", w.counters.read(KEY), 1)
    agents._LAST_AGENT_DISCONNECT = "the sandbox dropped mid-round"
    wu._refund_disconnect(w, CAND, "fix", (KEY,))
    check("...the next one is charged", w.counters.read(KEY), 1)
    check("the allowance is fleet-wide", bool(survey.SHARED_COUNTER_RE.fullmatch("disconnect-27295-2d47b932faad")), True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

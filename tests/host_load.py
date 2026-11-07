#!/usr/bin/env python3
"""The host-load gate: when a build-heavy round waits, and how starts are spaced.
  * off without $TAUCETI_MAX_LOAD; light stages pass at any load; either load average at the limit holds;
  * a host that cannot report its load, or a slot nobody can write, fails open;
  * one worker takes the slot, the rest wait for it to expire or for its round to end, and exactly one of
    several racing processes wins it;
  * in the loop, a held worker reads no quota or GitHub state, a worker parked by quota spends no slot, and
    a round hands its slot back when it ends.

Exit 0 = all hold; 1 = a mismatch.
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import host_load  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="tauceti-host-load-"))
status = TMP / "status.json"
status.write_text("{}")
os.environ["TAUCETI_RUNTIME_STATUS"] = str(status)
os.environ.pop("TAUCETI_ROUND_DONE_CMD", None)
os.environ.pop("TAUCETI_LOAD_SPACING", None)
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


def set_load(one, five=None):
    five = one if five is None else five
    host_load.os.getloadavg = lambda: (one, five, 0.0)


def fresh_slot_dir(name):
    d = TMP / name
    os.environ["TAUCETI_LOAD_GATE_DIR"] = str(d)
    return d


BUILD = ["fix", "fix-ci", "rebase", "roadmap"]

# The limit.
os.environ.pop("TAUCETI_MAX_LOAD", None)
fresh_slot_dir("off")
set_load(500)
check("no limit set: no hold", host_load.hold(BUILD) is None)
check("...and no slot file", not (TMP / "off" / "slot.json").exists())
os.environ["TAUCETI_MAX_LOAD"] = "not-a-number"
check("a limit that is no number turns the gate off", host_load.hold(BUILD) is None)
os.environ["TAUCETI_MAX_LOAD"] = "64"
for only in (["review"], ["curate"], ["progress"], ["review", "decide"]):
    check(f"{only} passes at any load", host_load.hold(only) is None)
check("a mix with one build stage is held", host_load.hold(["review", "fix"]) is not None)
check("no --only (every stage) is held", host_load.hold([]) is not None)
held = host_load.hold(BUILD)
check("the hold says what it read", held is not None and "500" in held.detail and "64" in held.detail, str(held))
check("...and rechecks within 90 s", held is not None and 45 <= held.nap <= 90, str(held))
set_load(100, 10)
check("a rising 1-minute average holds", host_load.hold(BUILD) is not None)
set_load(10, 100)
check("a 5-minute average still falling holds", host_load.hold(BUILD) is not None)
set_load(64)
check("load at the limit holds", host_load.hold(BUILD) is not None)


def no_load():
    raise OSError("no load average")


host_load.os.getloadavg = no_load
slot_dir = fresh_slot_dir("unreadable-load")
check("a host that cannot report load holds nobody", host_load.hold(BUILD) is None)

# The slot.
set_load(10)
slot_dir = fresh_slot_dir("slot")
os.umask(0o077)
check("no slot yet: the peek passes without creating one", host_load.hold(BUILD) is None and not slot_dir.exists())
check("the first worker takes it", host_load.hold(BUILD, take="w1") is None)
mode = stat.S_IMODE((slot_dir / "slot.json").stat().st_mode)
check("...group-writable under any umask", mode & 0o660 == 0o660, oct(mode))
second = host_load.hold(BUILD, take="w2")
check("a second worker waits for it", second is not None and 180 <= second.nap <= 200, str(second))
check("the peek waits too", host_load.hold(BUILD) is not None)
check("light stages ignore it", host_load.hold(["review"], take="r1") is None)
check("...and leave it alone", json.loads((slot_dir / "slot.json").read_text())["worker"] == "w1")
host_load.release("w2")
check("another worker's release leaves it", host_load.hold(BUILD) is not None)
host_load.release("w1")
check("its own round's end frees it", host_load.hold(BUILD, take="w2") is None)
(slot_dir / "slot.json").write_text(json.dumps({"at": 0, "worker": "w9"}))
check("an old slot has expired", host_load.hold(BUILD, take="w3") is None)
(slot_dir / "slot.json").write_text(json.dumps({"at": 4e9, "worker": "w9"}))
future = host_load.hold(BUILD)
check("a stamp from the future waits no longer than the spacing", future is not None and future.nap <= 200, str(future))
(slot_dir / "slot.json").write_text("{not json")
check("a garbled slot holds nobody", host_load.hold(BUILD) is None and host_load.hold(BUILD, take="w4") is None)
os.environ["TAUCETI_LOAD_SPACING"] = "0"
check("spacing 0: no slot", host_load.hold(BUILD, take="w5") is None and host_load.hold(BUILD, take="w6") is None)
os.environ.pop("TAUCETI_LOAD_SPACING")

locked = TMP / "locked"
locked.mkdir()
locked.chmod(0o500)
os.environ["TAUCETI_LOAD_GATE_DIR"] = str(locked)
if os.geteuid():
    check("a slot directory nobody can write fails open", host_load.hold(BUILD, take="w7") is None)
os.environ.pop("TAUCETI_LOAD_GATE_DIR")
check("no slot directory: the limit alone", host_load.hold(BUILD, take="w8") is None)
locked.chmod(0o700)

# Several processes race for one slot.
fresh_slot_dir("race")
os.umask(0o022)
children = []
for i in range(8):
    pid = os.fork()
    if pid == 0:
        os._exit(0 if host_load.hold(BUILD, take=f"racer{i}") is None else 1)
    children.append(pid)
wins = sum(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0 for pid in children)
check("exactly one of 8 racing processes wins", wins == 1, str(wins))


# The loop.
def run_loop(only, *, loads, quota=None, rounds=1):
    """Plays one cmd_loop: `loads` per reading (the last repeats); returns what it called, in order."""
    calls: list = []
    loads = list(loads)
    quota = list(quota or [])

    def reading():
        one = loads.pop(0) if len(loads) > 1 else loads[0]
        return (one, one, 0.0)

    def choose(*_a, **_k):
        calls.append("quota")
        return quota.pop(0) if quota else ("claude", {})

    def github():
        calls.append("github")
        return {}

    def slot_holder():
        slot_file = Path(os.environ["TAUCETI_LOAD_GATE_DIR"]) / "slot.json"
        text = slot_file.read_text() if slot_file.exists() else ""
        return json.loads(text).get("worker") if text else None

    def fake_round(_tail):
        nonlocal rounds
        calls.append("round")
        calls.append(("slot in round", slot_holder()))
        rounds -= 1
        if rounds <= 0:
            raise KeyboardInterrupt
        return 0

    def nap(seconds):
        calls.append(("sleep", seconds, json.loads(status.read_text()).get("state"), slot_holder()))

    host_load.os.getloadavg = reading
    saved = (tc.loop.choose_model, tc.loop.github_budget, tc.loop.run_round_subprocess, tc.loop.time.sleep)
    tc.loop.choose_model, tc.loop.github_budget, tc.loop.run_round_subprocess, tc.loop.time.sleep = (
        choose,
        github,
        fake_round,
        nap,
    )
    try:
        args = SimpleNamespace(
            ignore_quota=False, bubble=False, quota_cmd=None, source=None, author_model=None, author_effort=None
        )
        tc.loop.cmd_loop(args, SimpleNamespace(wid="wtest"), only=only, agent="claude")
    finally:
        tc.loop.choose_model, tc.loop.github_budget, tc.loop.run_round_subprocess, tc.loop.time.sleep = saved
    return calls


fresh_slot_dir("loop-held")
calls = run_loop(BUILD, loads=[200, 200, 200, 10])
held_calls = calls[: calls.index("quota")]
check(
    "a held worker sleeps as waiting-load and reads nothing",
    len(held_calls) == 3 and all(c[0] == "sleep" and c[2] == "waiting-load" for c in held_calls),
    str(held_calls),
)
check("once load falls it runs the round", "round" in calls, str(calls))
check("...holding the slot while it runs", ("slot in round", "wtest") in calls, str(calls))

fresh_slot_dir("loop-release")
calls = run_loop(BUILD, loads=[10], rounds=2)
after_round = next(c for c in calls[calls.index("round") :] if c[0] == "sleep")
check("...and hands it back when the round ends", after_round[3] is None, str(calls))

fresh_slot_dir("loop-review")
calls = run_loop(["review"], loads=[900])
check("a reviewer is not held", calls[:3] == ["quota", "github", "round"], str(calls))

fresh_slot_dir("loop-quota")
calls = run_loop(BUILD, loads=[10], quota=[(None, {})])
check("a worker parked by quota takes no slot before it runs", calls[0] == "quota" and calls[1][2] == "waiting-quota", str(calls))
check(
    "...and the slot goes to the round that runs",
    ("slot in round", "wtest") in calls and calls.count("round") == 1,
    str(calls),
)

fresh_slot_dir("loop-race")
real_take = host_load._take_slot
tries = {"n": 0}


def beaten_once(directory, gap, worker):
    tries["n"] += 1
    return 30.0 if tries["n"] == 1 else real_take(directory, gap, worker)


host_load._take_slot = beaten_once
try:
    calls = run_loop(BUILD, loads=[10])
finally:
    host_load._take_slot = real_take
first_sleep = next(c for c in calls if c[0] == "sleep")
check(
    "losing the race for the slot means another turn of the loop",
    first_sleep[2] == "waiting-load" and calls.count("round") == 1,
    str(calls),
)

print("\nALL OK" if not fails else f"\nFAILED ({fails})")
sys.exit(1 if fails else 0)

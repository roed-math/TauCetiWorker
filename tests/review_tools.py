#!/usr/bin/env python3
"""A bubble review stages the engine's TauCetiTools dependency and coordinates from the host, offline:
  * the Tools ref the engine's pyproject pins is fetched and mounted at /opt/tools, on PYTHONPATH;
  * an engine that knows --no-coordinate gets it, and the host holds the review/<repo>/<pr>/<head>
    claim for the whole run, released afterwards even when the run raises;
  * a head another reviewer holds is skipped with NoProgress and nothing is launched; an
    unregistered claim (rc 2) still runs;
  * an engine that pins no Tools and has no --no-coordinate runs as before, with no claim;
  * _fetch_pinned checks out exactly the pinned commit and does not fetch it a second time.
Run: python3 tests/review_tools.py. Exit 0 = all hold."""

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents  # noqa: E402
from tauceti_worker.config import Die, NoProgress  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


TMP = Path(tempfile.mkdtemp(prefix="review-tools-"))
SHA = "b22318446dcf66776248ed8c19370cd8da396748"
HEAD = "62c5031e722a96b9c8c5634dfa8d52902ec5a49a"


def engine(name, *, pin, coordinates):
    d = TMP / name
    (d / "runner").mkdir(parents=True)
    dep = f'"tauceti-tools @ git+https://github.com/TauCetiProject/TauCetiTools@{SHA}"' if pin else ""
    (d / "pyproject.toml").write_text(f'[project]\nname = "tauceti-review"\ndependencies = [{dep}]\n')
    flag = '    ap.add_argument("--no-coordinate", action="store_true")\n' if coordinates else ""
    (d / "runner" / "cli.py").write_text(f"def main():\n{flag}    pass\n")
    return d


fetched = []


def fake_fetch_pinned(url, ref, d):
    fetched.append((url, ref))
    (d / "tauceti_tools").mkdir(parents=True, exist_ok=True)
    return fetch_ok


class FakeClaims:
    def __init__(self, rc):
        self.rc, self.keys, self.held, self.released = rc, [], False, 0

    def begin_global_work(self, key):
        self.keys.append(key)
        self.held = self.rc != 1
        return self.rc

    def stop_heartbeat(self):
        pass

    def release(self):
        self.held = False
        self.released += 1


runs = []


def fake_run_in_bubble(w, target, prompt, opts, mounts=None, inner_cmd=None, cred_model=None):
    runs.append({"mounts": mounts, "inner": inner_cmd, "held": w.claims.held})
    if raise_in_run:
        raise RuntimeError("bubble died")
    return 0


real_fetch_pinned = agents._fetch_pinned
agents.run_in_bubble = fake_run_in_bubble
agents.fetch_ref = lambda repo, d: True
agents._fetch_pinned = fake_fetch_pinned
agents.me = lambda: "tester"
fetch_ok, raise_in_run = True, False
opts = types.SimpleNamespace()


def review(engine_dir, rc=0):
    os.environ["TAUCETI_REVIEW_ENGINE_DIR"] = str(engine_dir)
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=TMP / "state", store_dir=TMP / "store"), claims=FakeClaims(rc)
    )
    return w, agents.review_in_bubble(w, 470, HEAD, "claude", opts)


w, rc = review(engine("new", pin=True, coordinates=True))
run = runs[-1]
check("the pinned Tools ref is fetched", fetched[-1] == ("https://github.com/TauCetiProject/TauCetiTools", SHA),
      str(fetched))
check("Tools is mounted at /opt/tools", any(m.endswith(":/opt/tools/tauceti_tools:ro") for m in run["mounts"]),
      str(run["mounts"]))
check("/opt/tools is on the engine's PYTHONPATH", "PYTHONPATH=/opt/engine:/opt/tools " in run["inner"])
check("the engine is passed --no-coordinate", " --no-coordinate " in run["inner"])
check("the host claims the head", w.claims.keys == [f"review/taucetiproject/tauceti/470/{HEAD}"], str(w.claims.keys))
check("the claim is held during the run and released after", run["held"] and w.claims.released == 1)

n = len(runs)
try:
    review(TMP / "new", rc=1)
    check("a head held by another reviewer is skipped", False)
except NoProgress as e:
    check("a head held by another reviewer is skipped", "claimed by another reviewer" in str(e), str(e))
check("nothing is launched for a held head", len(runs) == n)

w, rc = review(TMP / "new", rc=2)
check("an unregistered claim still runs", len(runs) == n + 1 and rc == 0)

raise_in_run = True
claims = FakeClaims(0)
w = types.SimpleNamespace(cfg=types.SimpleNamespace(state=TMP / "state", store_dir=TMP / "store"), claims=claims)
try:
    agents.review_in_bubble(w, 470, HEAD, "claude", opts)
except RuntimeError:
    pass
check("a run that raises still releases the claim", claims.released == 1 and not claims.held)
raise_in_run = False

n_fetch = len(fetched)
w, rc = review(engine("old", pin=False, coordinates=False))
run = runs[-1]
check("an engine with no Tools pin fetches nothing", len(fetched) == n_fetch)
check("an engine with no Tools pin mounts nothing extra", not any("/opt/tools" in m for m in run["mounts"]))
check("an engine without --no-coordinate is not passed it", "--no-coordinate" not in run["inner"])
check("an engine without --no-coordinate takes no claim", w.claims.keys == [])

fetch_ok = False
try:
    review(TMP / "new")
    check("a failed Tools fetch stops the review", False)
except Die as e:
    check("a failed Tools fetch stops the review", SHA in str(e), str(e))
fetch_ok = True
os.environ.pop("TAUCETI_REVIEW_ENGINE_DIR", None)

# _fetch_pinned against a local repository with two commits: it must land on the first.
src = TMP / "src"
git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(src)]
subprocess.run(["git", "init", "-q", str(src)], check=True)
subprocess.run([*git, "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
for i in (1, 2):
    (src / "f").write_text(str(i))
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-qm", str(i)], check=True)
first = subprocess.run([*git, "rev-parse", "HEAD~1"], capture_output=True, text=True).stdout.strip()
dest = TMP / "pinned"
ok = real_fetch_pinned(f"file://{src}", first, dest)
got = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
check("_fetch_pinned checks out the pinned commit", ok and got == first and (dest / "f").read_text() == "1", got)
agents.gate_mod.gated_git = lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched"))
check("_fetch_pinned does not fetch a commit already checked out",
      real_fetch_pinned(f"file://{src}", first, dest))

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

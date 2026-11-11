#!/usr/bin/env python3
"""scripts/main-cache-rev finds the newest commit on a ref whose Lake outputs a service publishes.

Against a local HTTP server standing in for the revision endpoint and a throwaway git repository:
  * it asks for exactly the URL Lake would (repo scope, `tc/<toolchain dir>`, `<commit>.jsonl`), using the
    endpoint of the named service from the Lake config, and prints the newest cached commit;
  * it follows first parents only, so a cached commit that came in through a merged side branch is skipped;
  * none within --max is exit 1; an endpoint error, an unknown service or a missing config is exit 2.
Exit 0 = all hold; 1 = a mismatch.
"""

import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "main-cache-rev"
TOOLCHAIN = "leanprover/lean4:v4.36.0-rc2"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


tmp = Path(tempfile.mkdtemp(prefix="main-cache-rev-"))
repo = tmp / "repo"
repo.mkdir()


def git(*args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


git("init", "-q", "-b", "main")
(repo / "lean-toolchain").write_text(TOOLCHAIN + "\n")
git("add", "lean-toolchain")
commits = []
for i in range(5):
    (repo / "f").write_text(str(i))
    git("add", "f")
    git("commit", "-q", "-m", f"c{i}")
    commits.append(git("rev-parse", "HEAD"))
# A side branch with a commit newer than everything above, merged into main afterwards.
git("checkout", "-q", "-b", "side", commits[2])
(repo / "g").write_text("side")
git("add", "g")
git("commit", "-q", "-m", "side")
side = git("rev-parse", "HEAD")
git("checkout", "-q", "main")
git("merge", "-q", "--no-ff", "-m", "merge side", "side")
merge = git("rev-parse", "HEAD")
# First-parent order from main: merge, c4, c3, c2, c1, c0.

cached, failing, requests = set(), set(), []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append(self.path)
        rev = self.path.rsplit("/", 1)[-1].removesuffix(".jsonl")
        if rev in failing:
            self.send_response(500)
        elif rev in cached and self.path.startswith("/rev/TauCetiProject/TauCeti/tc/leanprover--lean4---v4.36.0-rc2/"):
            self.send_response(200)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"
config = tmp / "config.toml"
config.write_text(
    'cache.defaultService = "other"\n\n[[cache.service]]\nname = "other"\nkind = "s3"\n'
    f'artifactEndpoint = "{base}/other-art"\nrevisionEndpoint = "{base}/other-rev"\n\n'
    '[[cache.service]]\nname = "tauceti-public"\nkind = "s3"\n'
    f'artifactEndpoint = "{base}/art"\nrevisionEndpoint = "{base}/rev"\n'
)


def run(*extra, cfg=config):
    requests.clear()
    env = {**os.environ, "LAKE_CONFIG": str(cfg)}
    argv = [sys.executable, str(SCRIPT), "--service", "tauceti-public", "--repo", "TauCetiProject/TauCeti",
            "--ref", "main", *extra]
    return subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True)


cached.update({commits[3], commits[1], side})
r = run()
check("the newest cached first-parent commit is printed", r.returncode == 0 and r.stdout.strip() == commits[3],
      f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
check("it asks the named service's endpoint with Lake's path",
      requests and requests[0] == f"/rev/TauCetiProject/TauCeti/tc/leanprover--lean4---v4.36.0-rc2/{merge}.jsonl",
      str(requests[:1]))
check("a cached commit from a merged side branch is skipped", side not in "".join(requests))
check("it stops at the first cached commit", len(requests) == 3, str(len(requests)))
check("it says how many newer commits had none", "2 newer ones have none" in r.stderr, r.stderr)

r = run("--max", "2")
check("none cached within --max is exit 1", r.returncode == 1 and not r.stdout.strip(), f"rc={r.returncode}")

failing.add(merge)
r = run()
check("an endpoint error is exit 2, not a silent miss", r.returncode == 2 and "did not answer" in r.stderr,
      f"rc={r.returncode} {r.stderr!r}")
failing.clear()

r = run("--service", "missing")
check("an unknown service is exit 2", r.returncode == 2 and "no revisionEndpoint" in r.stderr, r.stderr)
r = run(cfg=tmp / "absent.toml")
check("a missing Lake config is exit 2", r.returncode == 2 and "cannot read Lake config" in r.stderr, r.stderr)

server.shutdown()
print("\nALL OK" if not fails else f"\nFAILED ({fails})")
sys.exit(1 if fails else 0)

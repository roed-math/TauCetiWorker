#!/usr/bin/env python3
"""The review rounds' git and gh wrappers give TauCetiReview what bubble's proxy does not.

TauCetiReview #146 runs pr_diff's git with GIT_CONFIG_GLOBAL=/dev/null; in a bubble that dropped the
url.insteadOf + token header that route git through bubble's proxy, and every sandboxed review died
("could not compute the diff", 2026-09-26). #229 (11-11) also allows only https for scoped source git
and reads the publishing login with `gh api user`, which the proxy refuses. Offline:
  * with the global config blanked, the wrapper adds exactly the http(s) routing settings from
    ~/.gitconfig, the header value (which contains a space) intact, plus the proxy scheme's allowance;
  * without it, git runs with the caller's argv unchanged;
  * against a local dumb-HTTP stand-in for the proxy, git under #229's environment fails and the
    wrapper reaches it with the token header;
  * the gh wrapper answers only the login query;
  * the review round stages both and puts them first on PATH with the login.
Exit 0 = all hold; 1 = a mismatch."""

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHIM = REPO / "scripts" / "review-git" / "git"
GH_SHIM = REPO / "scripts" / "review-git" / "gh"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


real_git = shutil.which("git")
T = Path(tempfile.mkdtemp(prefix="review-git-"))
shimdir, fakedir, home = T / "review-git", T / "fake", T / "home"
for d in (shimdir, fakedir, home):
    d.mkdir()
shutil.copy(SHIM, shimdir / "git")
os.chmod(shimdir / "git", 0o755)
# A stand-in git: `config` goes to the real git (the wrapper reads ~/.gitconfig with it); anything else
# prints its argv as JSON.
(fakedir / "git").write_text(f"""#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:2] == ["config"]:
    os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])
print(json.dumps(sys.argv[1:]))
""")
os.chmod(fakedir / "git", 0o755)
(home / ".gitconfig").write_text("""[url "http://10.128.128.1:7654/git/"]
\tinsteadOf = https://github.com/
[http "http://10.128.128.1:7654/"]
\textraHeader = X-Bubble-Token: abc123
[url "file:///elsewhere/"]
\tinsteadOf = https://example.com/
[user]
\tname = Someone
""")


def run(blank: bool):
    env = {"PATH": f"{shimdir}{os.pathsep}{fakedir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home)}
    if blank:
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    p = subprocess.run([str(shimdir / "git"), "fetch", "-q", "origin", "abc"], env=env, capture_output=True, text=True)
    return json.loads(p.stdout or "null"), p


got, p = run(blank=True)
check("with the config blanked, the routing comes back as -c options", got == [
    "-c", "url.http://10.128.128.1:7654/git/.insteadof=https://github.com/",
    "-c", "http.http://10.128.128.1:7654/.extraheader=X-Bubble-Token: abc123",
    "-c", "protocol.http.allow=always",
    "fetch", "-q", "origin", "abc"], f"{got} {p.stderr}")
got, p = run(blank=False)
check("otherwise git gets the caller's argv unchanged", got == ["fetch", "-q", "origin", "abc"], f"{got} {p.stderr}")

# A dumb-HTTP stand-in for the proxy, serving a bare repo where bubble's insteadOf points.
def sh(*args, cwd=None):
    return subprocess.run([real_git, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


served, work = T / "served", T / "work"
sh("init", "-q", "-b", "main", str(work))
(work / "README").write_text("x\n")
sh("add", "README", cwd=work)
sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=work)
head = sh("rev-parse", "HEAD", cwd=work)
bare = served / "git" / "TauCetiProject" / "TauCeti"
bare.parent.mkdir(parents=True)
sh("clone", "-q", "--bare", str(work), str(bare))
sh("update-server-info", cwd=bare)
tokens = []


class Proxy(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        tokens.append(self.headers.get("X-Bubble-Token"))
        super().do_GET()

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Proxy, directory=str(served)))
threading.Thread(target=server.serve_forever, daemon=True).start()
proxy = f"http://127.0.0.1:{server.server_address[1]}"
proxied = T / "proxied"
proxied.mkdir()
(proxied / ".gitconfig").write_text(f"""[url "{proxy}/git/"]
\tinsteadOf = https://github.com/
[http "{proxy}/"]
\textraHeader = X-Bubble-Token: proxy-token
""")
# What TauCetiTools' scoped_git_environment sets for source git since #229.
scoped = {
    "PATH": f"{shimdir}{os.pathsep}{os.environ['PATH']}", "HOME": str(proxied),
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "protocol.allow", "GIT_CONFIG_VALUE_0": "never",
    "GIT_CONFIG_KEY_1": "protocol.https.allow", "GIT_CONFIG_VALUE_1": "always",
}
repo = "https://github.com/TauCetiProject/TauCeti"
plain = subprocess.run([real_git, "-c", f"url.{proxy}/git/.insteadOf=https://github.com/", "ls-remote", repo],
                       env=scoped, capture_output=True, text=True, timeout=60)
check("under #229's environment, routing alone is refused", plain.returncode != 0
      and "transport 'http' not allowed" in plain.stderr, plain.stderr)
p = subprocess.run([str(shimdir / "git"), "ls-remote", repo, "refs/heads/main"], env=scoped,
                   capture_output=True, text=True, timeout=60)
check("…and the wrapper reaches the proxy with its token", p.returncode == 0
      and p.stdout.split() == [head, "refs/heads/main"] and "proxy-token" in tokens, f"{p.stdout} {p.stderr}")
server.shutdown()
server.server_close()

(fakedir / "gh").write_text('#!/bin/sh\necho "real gh $*"\n')
os.chmod(fakedir / "gh", 0o755)
shutil.copy(GH_SHIM, shimdir / "gh")
os.chmod(shimdir / "gh", 0o755)


def gh(*args, login="someone"):
    env = {"PATH": f"{shimdir}{os.pathsep}{fakedir}{os.pathsep}/usr/bin:/bin"}
    if login:
        env["TAUCETI_BUBBLE_LOGIN"] = login
    return subprocess.run([str(shimdir / "gh"), *args], env=env, capture_output=True, text=True).stdout


check("gh answers the login query from TAUCETI_BUBBLE_LOGIN", gh("api", "user", "--jq", ".login") == "someone\n")
check("…and passes every other call through", gh("api", "user") == "real gh api user\n"
      and gh("pr", "view", "1") == "real gh pr view 1\n"
      and gh("api", "user", "--jq", ".login", login=None) == "real gh api user --jq .login\n")

src = (REPO / "tauceti_worker" / "agents.py").read_text()
check("the review round puts the wrappers first on PATH", '"env PATH=/opt/round/review-git:/opt/round:$PATH "' in src)
check("…with the worker's login", 'f"TAUCETI_BUBBLE_LOGIN={shlex.quote(me())} "' in src)
check("…and every bubble round stages both",
      'for tool in ("git", "gh"):' in src and 'HERE / "scripts" / "review-git" / tool' in src)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

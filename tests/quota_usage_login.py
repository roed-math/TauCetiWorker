#!/usr/bin/env python3
"""Agents on a `claude setup-token` token, usage read through a separate login.

The setup-token carries only user:inference and the usage endpoint answers it 403
oauth_scope_insufficient (2026-11-02), so $TAUCETI_CLAUDE_USAGE_CREDENTIALS names a full login the pacer
reads with. Every worker on the account then shares that one token on the endpoint, so they share one
cache beside it, and a rotation by one of them must not cost the others a refresh token.
Dependency-free; no network.
"""

import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import oauth as credential_refresh
from tauceti_worker import quota as q

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


root = Path(tempfile.mkdtemp(prefix="tauceti-usage-login-"))
usage = root / "logins" / "usage" / ".credentials.json"
usage.parent.mkdir(parents=True)


def write_usage(token: str, expires_in_s: float = 6 * 3600) -> None:
    usage.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "rt-" + token,
                                                   "expiresAt": int((time.time() + expires_in_s) * 1000)}}))


payload = {
    "five_hour": None,
    "seven_day": {"utilization": 3, "resets_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()},
}
seen = []


def fake_http(_url, headers, timeout=15):
    tok = headers["Authorization"].removeprefix("Bearer ")
    seen.append(tok if tok in ("tok-1", "stale", "fresh") else "<a credential from outside this test>")
    if tok == "stale":  # another worker rotated the login after this one read it
        write_usage("fresh")
        return 401, {}, None
    return 200, payload, None


def worker(name: str) -> q.Quota:
    home = root / name
    (home / ".claude").mkdir(parents=True)
    return q.Quota(SimpleNamespace(home=home, quota_cache=home / "cache"))


os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ.pop("TAUCETI_AUTO_REFRESH", None)
check("no usage login: Claude readings stay in the worker's cache", worker("solo")._cache("claude"),
      root / "solo" / "cache")

os.environ[q.CLAUDE_USAGE_CREDENTIALS_ENV] = str(usage)
write_usage("tok-1")
q._http_get_json, saved_http = fake_http, q._http_get_json
rotations = []
credential_refresh.refresh_if_due, saved_refresh = (
    lambda prov, *_a, **kw: rotations.append((prov.credentials, kw.get("force", False))) or "current"
), credential_refresh.refresh_if_due
try:
    a, b = worker("a"), worker("b")
    check("both workers share one Claude cache beside the login", (a._cache("claude"), b._cache("claude")),
          (usage.parent / q.CLAUDE_QUOTA_DIRNAME / "usage-cache",) * 2)
    check("...and keep their own Codex cache", a._cache("codex"), root / "a" / "cache")
    pa = a.claude(refresh=True, renew=True)
    pb = b.claude(refresh=True, renew=True)
    check("the pacer reads usage with the login, not the agents' token", seen, ["tok-1"])
    check("one fetch answers a second worker's forced read inside the floor", (pa.available, pb.available),
          (True, True))

    os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    write_usage("tok-1", expires_in_s=60)
    a._refresh_claude_credential()
    check("renewal rotates the usage login", rotations, [(usage, False)])

    rotations.clear()
    seen.clear()
    write_usage("stale")
    c = worker("c")
    pc = c.claude(refresh=True, renew=True)
    check("a 401 after another worker's rotation re-reads instead of rotating",
          (seen, [r for r in rotations if r[1]], pc.available), (["stale", "fresh"], [], True))
finally:
    q._http_get_json = saved_http
    credential_refresh.refresh_if_due = saved_refresh

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

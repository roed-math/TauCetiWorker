#!/usr/bin/env python3
"""Every Claude usage-endpoint request leaves one line in `<cache>/usage-requests.jsonl`: the worker,
the token fingerprint, the status, the Retry-After, whether the read was forced and a valid cache
existed. The endpoint's rate limit is unpublished and answered 429 with hour-long waits on single
tokens (2026-10-06); these lines are what sizes it. A cache hit makes no request and leaves no line.
Dependency-free; no network. Exit 0 = all hold."""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import quota as Q  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


q = tc.Quota.__new__(tc.Quota)
q.cache_dir = Path(tempfile.mkdtemp())
log = q.cache_dir / "usage-requests.jsonl"
os.environ["TAUCETI_WORKER_ID"] = "tst-rev1"


def lines():
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


Q._http_get_json = lambda url, headers, timeout=15: (429, {}, 1319.0)
prov, readings = q._claude_pass("fp1", "tok", refresh=True)
rec = (lines() or [{}])[-1]
check("a 429 is still reported as before", readings is None and "429" in (prov.error or ""), str(prov.error))
check("…and logged with its wait and token", rec.get("status") == 429 and rec.get("retry_after") == 1319.0
      and rec.get("fp") == "fp1" and rec.get("worker") == "tst-rev1" and rec.get("forced") is True, str(rec))


def boom(url, headers, timeout=15):
    raise tc.GitHubError("usage fetch failed: timed out")


Q._http_get_json = boom
(q.cache_dir / "usage-hold.json").unlink(missing_ok=True)  # the 429 above holds the token; this case wants a request
q._claude_pass("fp1", "tok", refresh=True)
rec = (lines() or [{}])[-1]
check("a failed request is logged with no status", len(lines()) == 2 and rec.get("status") is None
      and "timed out" in rec.get("error", ""), str(rec))

# a valid cached reading serves an unforced read: no request, no line
q._cached_claude = lambda fp: ({"x": 1}, 0.0)
q._from_cached_claude = lambda cached: ("cached", [])
q._claude_pass("fp1", "tok", refresh=False)
check("a cache hit makes no request and logs nothing", len(lines()) == 2, str(len(lines())))

# ---- falling back when the endpoint refuses: a reading up to CLAUDE_FALLBACK_MAX_AGE old, never on a 401
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


now = time.time()
payload = {"five_hour": {"utilization": 20.0, "resets_at": iso(now + 3 * 3600)},
           "seven_day": {"utilization": 50.0, "resets_at": iso(now + 3 * 86400)}}
q2 = tc.Quota.__new__(tc.Quota)
q2.cache_dir = Path(tempfile.mkdtemp())
q2._from_cached_claude = lambda cached: (tc.Provider("claude", True, "opus"), [cached[1]])  # pacing is not under test
valid_until = Q._claude_valid_until(Q._claude_readings(payload))
check("the test payload is a fully resolved reading", valid_until is not None)


def cache(age):
    q2._store_raw("claude", payload, "fp2", valid_until, now - age)


cache(2 * 3600)
check("a 2-hour-old reading is past the ordinary staleness bound", q2._cached_claude("fp2") is None)
for code, served in ((429, True), (503, True), (401, False)):
    (q2.cache_dir / "usage-hold.json").unlink(missing_ok=True)
    Q._http_get_json = lambda url, headers, timeout=15, code=code: (code, {}, 600.0 if code == 429 else None)
    _prov, readings = q2._claude_pass("fp2", "tok", refresh=True)
    check(f"…and an HTTP {code} {'falls back on it' if served else 'never falls back'}", (readings is not None) == served)
Q._http_get_json = boom
(q2.cache_dir / "usage-hold.json").unlink(missing_ok=True)
check("…as does a request that fails outright", q2._claude_pass("fp2", "tok", refresh=True)[1] is not None)

# ---- after a 429 the token does not ask again until the wait (plus a margin) has run out
calls = []


def counted(url, headers, timeout=15):
    calls.append(time.time())
    return (429, {}, 600.0)


Q._http_get_json = counted
(q2.cache_dir / "usage-hold.json").unlink(missing_ok=True)
q2._claude_pass("fp2", "tok", refresh=True)
hold = q2._usage_hold("fp2")
check("a 429 holds the token for its wait plus the margin", 600 + Q.USAGE_RETRY_MARGIN - 5 < hold <= 600 + Q.USAGE_RETRY_MARGIN, str(hold))
check("…only that token", q2._usage_hold("other-fp") == 0)
_prov, readings = q2._claude_pass("fp2", "tok", refresh=True)
check("a forced read during the hold asks nothing and serves the fallback", len(calls) == 1 and readings is not None, str(len(calls)))
cache(7 * 3600)
prov, readings = q2._claude_pass("fp2", "tok", refresh=True)
check("…and with no reading to fall back on, waits out the rest of the hold",
      len(calls) == 1 and readings is None and prov.retry_after and abs(prov.retry_after - hold) < 5, str(prov.retry_after))
(q2.cache_dir / "usage-hold.json").write_text(json.dumps({"fp": "fp2", "until": time.time() - 1}))
prov, readings = q2._claude_pass("fp2", "tok", refresh=True)
check("once the hold has run out the token asks again", len(calls) == 2)
check("a reading older than the fallback bound is not served", readings is None)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

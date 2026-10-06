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
q._claude_pass("fp1", "tok", refresh=True)
rec = (lines() or [{}])[-1]
check("a failed request is logged with no status", len(lines()) == 2 and rec.get("status") is None
      and "timed out" in rec.get("error", ""), str(rec))

# a valid cached reading serves an unforced read: no request, no line
q._cached_claude = lambda fp: ({"x": 1}, 0.0)
q._from_cached_claude = lambda cached: ("cached", [])
q._claude_pass("fp1", "tok", refresh=False)
check("a cache hit makes no request and logs nothing", len(lines()) == 2, str(len(lines())))

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

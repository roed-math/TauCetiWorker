#!/usr/bin/env python3
"""With --auto-refresh, the worker rotates the OPERATOR's Codex source file instead of stalling on 401.

Nothing on the host ever rotated ~/.codex/auth.json: the worker mirrors it (refresh token stripped), so
once the operator's ~10-day access token lapsed every codex worker read "codex token expired; refresh
left to the operator" until a human ran `codex`. The refresher already existed in oauth.py for the Docker
deployment; this wires it into the host pacer under the same opt-in and the same rules as Claude's:
source file only (never the mirror), renewing callers only, rate-limited, off by default.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import oauth  # noqa: E402

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


HOUR5, WEEK = 5 * 3600, 7 * 86400
USAGE_OK = {
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {"used_percent": 5, "limit_window_seconds": HOUR5, "reset_after_seconds": HOUR5 // 2},
        "secondary_window": {"used_percent": 5, "limit_window_seconds": WEEK, "reset_after_seconds": WEEK // 2},
    }
}


def auth(access, refresh="operator-refresh"):
    tokens = {"access_token": access, "id_token": "id-old"}
    if refresh is not None:
        tokens["refresh_token"] = refresh
    return json.dumps({"auth_mode": "chatgpt", "tokens": tokens, "last_refresh": "x"})


def setup(tmp, *, expires_in=60, refresh="operator-refresh", opt_in=True):
    """An isolated worker: it reads a MIRROR of the operator's auth.json, with a marker naming the
    original. Returns (quota, source path, mirror path, stale access token)."""
    real, iso = tmp / "real", tmp / "iso"
    src, dst = real / ".codex", iso / ".codex"
    for d in (src, dst):
        d.mkdir(parents=True)
    (dst / ".tauceti-creds-source").write_text(str(src))
    os.environ["CODEX_HOME"] = str(dst)
    os.environ.pop("TAUCETI_AUTO_REFRESH", None)
    if opt_in:
        os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    stale = jwt(time.time() + expires_in)
    (src / "auth.json").write_text(auth(stale, refresh))
    (dst / "auth.json").write_text(auth(stale, tc.CODEX_RT_PLACEHOLDER))  # mirrors carry no real token
    cfg = types.SimpleNamespace(home=iso, quota_cache=tmp / "cache")
    return tc.Quota(cfg), src / "auth.json", dst / "auth.json", stale


def rotates_to(access):
    return patch.object(
        oauth, "_post_json", return_value=(200, {"access_token": access, "refresh_token": "rt2", "id_token": "id-new"})
    )


def usage_seen(answers=None):
    """Record the bearer each usage read was made with; answer 200 (or the scripted sequence)."""
    seen = []
    script = list(answers or [])

    def fake(url, headers, timeout=15):
        seen.append(headers["Authorization"])
        return script.pop(0) if script else (200, USAGE_OK, None)

    return seen, patch.object(tc.quota, "_http_get_json", side_effect=fake)


saved = {k: os.environ.get(k) for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "TAUCETI_AUTO_REFRESH")}
os.environ.pop("CLAUDE_CONFIG_DIR", None)  # mirror_creds on macOS would otherwise consult the Keychain
try:
    # 1) A near-expiry SOURCE is rotated; the mirror is re-mirrored (placeholder kept) and the same call
    #    measures usage with the fresh token.
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh = jwt(time.time() + 10 * 86400)
        quota, src, mirror, stale = setup(tmp)
        seen, usage = usage_seen()
        with rotates_to(fresh) as post, usage:
            prov = quota.codex(renew=True)
        check("a near-expiry credential is rotated", post.call_count, 1)
        check(
            "the exchange spends the SOURCE's refresh token",
            post.call_args.args[1]["refresh_token"],
            "operator-refresh",
        )
        src_tokens = json.loads(src.read_text())["tokens"]
        check("the source file carries the new access token", src_tokens["access_token"], fresh)
        check("the source file carries the rotated refresh token", src_tokens["refresh_token"], "rt2")
        mir = json.loads(mirror.read_text())["tokens"]
        check("the mirror picks up the new access token", mir["access_token"], fresh)
        check("the mirror still carries only the placeholder", mir["refresh_token"], tc.CODEX_RT_PLACEHOLDER)
        check("usage is read with the fresh token", seen, [f"Bearer {fresh}"])
        check("the provider is usable again", prov.available, True)
        check("no rotation marker lands beside the mirror", list(mirror.parent.glob(".auth.json.refresh.*")), [])
        check(
            "the rotation markers sit beside the source",
            (src.parent / ".auth.json.refresh.last-success").exists(),
            True,
        )

        # 2) Cooldown: the success marker holds the next rotation back. The mirror still follows the
        #    SOURCE (a mirror is not a rotation), so the operator's own re-login lands in the same cycle.
        relogin = jwt(time.time() + 60)
        src.write_text(auth(relogin))
        with patch.object(oauth, "_post_json") as post, usage:
            quota.codex(renew=True, refresh=True)
            quota.codex(renew=True, refresh=True)
        check("the cooldown holds a second rotation back", post.call_count, 0)
        check(
            "the mirror still follows the source under cooldown",
            json.loads(mirror.read_text())["tokens"]["access_token"],
            relogin,
        )
        check("...placeholder kept", json.loads(mirror.read_text())["tokens"]["refresh_token"], tc.CODEX_RT_PLACEHOLDER)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 3) Inspecting reads never rotate, however expired the source.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp, expires_in=-60)
        seen, usage = usage_seen()
        with patch.object(oauth, "_post_json") as post, usage:
            quota.codex()
            tc.Quota(quota.cfg).choose(None)
        check("an inspecting read never rotates", post.call_count, 0)
        check("the source is untouched", json.loads(src.read_text())["tokens"]["access_token"], stale)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4) Flag off: nothing rotates, and the stale token is used as-is.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp, opt_in=False)
        seen, usage = usage_seen()
        with patch.object(oauth, "_post_json") as post, usage:
            quota.codex(renew=True)
        check("without --auto-refresh nothing rotates", post.call_count, 0)
        check("the stale token is still used", seen, [f"Bearer {stale}"])
        check("the source is untouched", json.loads(src.read_text())["tokens"]["refresh_token"], "operator-refresh")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5) The Docker shape: the source is itself a stripped mirror kept by the dedicated refresher. There is
    #    nothing here the worker may spend.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp, refresh=tc.CODEX_RT_PLACEHOLDER)
        seen, usage = usage_seen()
        with patch.object(oauth, "_post_json") as post, usage:
            quota.codex(renew=True)
        check("a placeholder-only source is never rotated", post.call_count, 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 6) The stored expiry says the token is live and the endpoint says 401: rotate once, re-read once.
    tmp = Path(tempfile.mkdtemp())
    try:
        rescued = jwt(time.time() + 10 * 86400)
        quota, src, mirror, stale = setup(tmp, expires_in=5 * 86400)
        seen, usage = usage_seen([(401, {}, None)])
        with rotates_to(rescued) as post, usage:
            prov = quota.codex(renew=True)
        check("a 401 forces exactly one rotation", post.call_count, 1)
        check("the retry uses the rotated token", seen, [f"Bearer {stale}", f"Bearer {rescued}"])
        check("the rescued read decides the verdict", prov.available, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 7) A 401 that rotation cannot fix is reported once, with the existing operator-facing error; and
    #    without `renew` a 401 never even tries.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp, expires_in=5 * 86400)
        seen, usage = usage_seen([(401, {}, None), (401, {}, None)])
        with patch.object(oauth, "_post_json", return_value=(400, None)) as post, usage:
            prov = quota.codex(renew=True)
            plain = quota.codex()
        check("an unrecoverable 401 tries once", post.call_count, 1)
        check("...and stays a hard block", prov.available, False)
        check("...with the operator-facing error", prov.error, "codex token expired; refresh left to the operator")
        check("an inspecting 401 does not rotate", plain.error, "codex token expired; refresh left to the operator")
        check("two reads, no more", len(seen), 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
finally:
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

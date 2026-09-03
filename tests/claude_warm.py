#!/usr/bin/env python3
"""macOS: keep the Claude Keychain credential alive without ever forking its refresh chain.

Refresh tokens are strictly single-use and a chain cannot be forked: two Keychain items holding the same
refresh token means the second to refresh fails, and Claude Code then wipes that item (measured). So on
macOS the worker never holds a refresh token at all. Two mechanisms replace the Linux file refresh:

  * the WARM-UP (`TAUCETI_CLAUDE_WARM=1`): one `claude -p` Haiku turn run with $CLAUDE_CONFIG_DIR removed,
    so Claude Code renews the OPERATOR's item — the one chain on this login — under a host-wide lock;
  * the KEYCHAIN MIRROR: the worker's own suffixed item is rewritten with the operator's access token and
    an EMPTY refreshToken whenever the access token changes, and never otherwise.

Both are exercised against a fake `security` / `claude` so no real Keychain is touched. Exit 0 = pass.
"""

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

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def blob(access, expires_in_s, refresh="rt-operator"):
    return {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": int((time.time() + expires_in_s) * 1000),
            "scopes": ["user:inference"],
        }
    }


OPERATOR = "Claude Code-credentials"


class FakeHost:
    """A login Keychain (service -> JSON string) behind `security`, and a `claude` that renews the
    operator's item the way Claude Code does when `renews` is set. Records every command it saw."""

    def __init__(self, items, *, renews=True, claude_rc=0):
        self.items, self.renews, self.claude_rc = dict(items), renews, claude_rc
        self.security_calls, self.claude_runs = [], []

    def __call__(self, cmd, *a, **kw):
        if cmd[0] == "security":
            self.security_calls.append(list(cmd))
            svc = cmd[cmd.index("-s") + 1]
            if cmd[1] == "find-generic-password":
                if svc in self.items:
                    return types.SimpleNamespace(returncode=0, stdout=self.items[svc], stderr="")
                return types.SimpleNamespace(returncode=44, stdout="", stderr="")
            if cmd[1] == "add-generic-password":
                self.items[svc] = cmd[cmd.index("-w") + 1]
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected security command {cmd}")
        self.claude_runs.append({"argv": list(cmd), "env": kw.get("env"), "cwd": kw.get("cwd")})
        if self.renews and self.claude_rc == 0:
            self.items[OPERATOR] = json.dumps(blob("fresh-access", 8 * 3600, "rt-rotated"))
        return types.SimpleNamespace(returncode=self.claude_rc, stdout="OK\n", stderr="")

    def writes(self):
        return [c for c in self.security_calls if c[1] == "add-generic-password"]


tmp = Path(tempfile.mkdtemp(prefix="claude-warm-"))
host_home = tmp / "home"
iso = tmp / "iso" / ".claude"
saved_env = {k: os.environ.get(k) for k in ("HOME", "USER", "CLAUDE_CONFIG_DIR", "TAUCETI_CLAUDE_WARM")}
saved_platform, saved_run, saved_host = sys.platform, tc.quota.subprocess.run, tc.quota._host_home
lockfile = host_home / ".cache" / "tauceti" / "claude-warm.lock"
marker = lockfile.with_name("claude-warm.last-attempt")


def reset_env(*, warm=True, isolated=True):
    os.environ["HOME"] = str(host_home)
    os.environ["USER"] = "alice"
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    if isolated:
        os.environ["CLAUDE_CONFIG_DIR"] = str(iso)
    os.environ.pop("TAUCETI_CLAUDE_WARM", None)
    if warm:
        os.environ["TAUCETI_CLAUDE_WARM"] = "1"
    for p in (lockfile, marker):
        p.unlink(missing_ok=True)


def quota():
    return tc.Quota(types.SimpleNamespace(home=host_home, quota_cache=tmp / "cache", state=tmp / "state"))


try:
    tc.quota.sys.platform = "darwin"
    tc.quota._host_home = lambda: host_home
    suffix = None

    # ---- the warm-up ------------------------------------------------------------------------------
    # 1) Flag off: an expired operator token is left alone, nothing runs, nothing is written.
    reset_env(warm=False)
    suffix = tc._claude_keychain_suffix()
    check("an isolated dir has a suffix", suffix is not None and len(suffix) == 8, True)
    host = FakeHost({OPERATOR: json.dumps(blob("stale-access", -3600))})
    tc.quota.subprocess.run = host
    check("flag off: no renewal", quota()._refresh_claude_credential(), False)
    check("flag off: claude never runs", host.claude_runs, [])
    check("flag off: nothing written", host.writes(), [])
    check("flag off: no lock or marker appears", lockfile.exists() or marker.exists(), False)

    # 2) Flag on, expired, refresh token present: ONE claude run renews the operator's item.
    reset_env()
    host = FakeHost({OPERATOR: json.dumps(blob("stale-access", -3600))})
    tc.quota.subprocess.run = host
    check("expired token is renewed by a warm-up", quota()._refresh_claude_credential(), True)
    check("exactly one claude run", len(host.claude_runs), 1)
    run = host.claude_runs[0]
    check("the warm-up env has NO CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR" in run["env"], False)
    check("the warm-up env keeps HOME", run["env"].get("HOME"), str(host_home))
    check(
        "one Haiku turn, text output",
        run["argv"][1:],
        [
            "-p",
            tc.quota.CLAUDE_WARM_PROMPT,
            "--model",
            "claude-haiku-4-5-20251001",
            "--max-turns",
            "1",
            "--output-format",
            "text",
        ],
    )
    check("the run's cwd is outside the checkout and removed afterwards", Path(run["cwd"]).exists(), False)
    check("the lock is host-wide, not per worker", lockfile.exists(), True)
    check("the attempt marker sits beside the lock", marker.exists(), True)
    check("the warm-up itself writes no Keychain item", host.writes(), [])
    check(
        "the operator's item was renewed by claude, not by us",
        json.loads(host.items[OPERATOR])["claudeAiOauth"]["accessToken"],
        "fresh-access",
    )

    # 3) A run that does not advance the expiry reports False and is rate-limited afterwards.
    reset_env()
    host = FakeHost({OPERATOR: json.dumps(blob("stale-access", -3600))}, renews=False, claude_rc=1)
    tc.quota.subprocess.run = host
    check("a non-advancing expiry reports False", quota()._refresh_claude_credential(), False)
    check("...after one run", len(host.claude_runs), 1)
    check("the next poll is held by the attempt marker", quota()._refresh_claude_credential(), False)
    check("...without another run", len(host.claude_runs), 1)
    check("nor does force lift the marker", quota()._refresh_claude_credential(force=True), False)
    check("...still one run", len(host.claude_runs), 1)
    with patch.object(tc.quota.time, "time", return_value=time.time() + tc.quota.CLAUDE_REFRESH_RETRY_S + 1):
        quota()._refresh_claude_credential()
    check("the interval elapsing allows a retry", len(host.claude_runs), 2)

    # 4) A live token is not warmed (no lock, no run); `force` (a 401) warms it anyway.
    reset_env()
    host = FakeHost({OPERATOR: json.dumps(blob("live-access", 6 * 3600))})
    tc.quota.subprocess.run = host
    check("a live token is left alone", quota()._refresh_claude_credential(), False)
    check("a live token takes no lock", lockfile.exists(), False)
    check("force warms a token the endpoint rejected", quota()._refresh_claude_credential(force=True), True)
    check("...with one run", len(host.claude_runs), 1)

    # 5) An operator item with no refresh token (never logged in, or wiped) cannot be renewed by anyone.
    reset_env()
    host = FakeHost({OPERATOR: json.dumps(blob("stale-access", -3600, refresh=""))})
    tc.quota.subprocess.run = host
    check("no refresh token: no run", quota()._refresh_claude_credential(), False)
    check("...none at all", host.claude_runs, [])

    # 6) The warm-up reads the OPERATOR's item even when the worker's own item exists and is fresher.
    reset_env()
    host = FakeHost(
        {
            OPERATOR: json.dumps(blob("stale-access", -3600)),
            f"{OPERATOR}-{suffix}": json.dumps(blob("worker-mirror", 6 * 3600, "")),
        }
    )
    tc.quota.subprocess.run = host
    check("the operator's expiry decides, not the worker item's", quota()._refresh_claude_credential(), True)

    # ---- the Keychain mirror ---------------------------------------------------------------------
    # 7) Absent worker item: written once, suffixed, with an EMPTY refreshToken.
    reset_env()
    cfg = types.SimpleNamespace(home=host_home)
    host = FakeHost({OPERATOR: json.dumps(blob("access-A", 3600))})
    tc.quota.subprocess.run = host
    tc.mirror_claude_keychain(cfg)
    writes = host.writes()
    check("one write", len(writes), 1)
    check(
        "the write targets the worker's suffixed service",
        writes[0][:7],
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            f"{OPERATOR}-{suffix}",
            "-a",
            "alice",
        ],
    )
    written = json.loads(host.items[f"{OPERATOR}-{suffix}"])["claudeAiOauth"]
    check("the mirror carries the operator's access token", written["accessToken"], "access-A")
    check("the mirror's refreshToken is EMPTY", written["refreshToken"], "")
    check(
        "the operator's item is untouched",
        json.loads(host.items[OPERATOR])["claudeAiOauth"]["refreshToken"],
        "rt-operator",
    )
    check("the operator's service is never written", [w for w in writes if w[4] == OPERATOR], [])

    # 8) Steady state: same access token, nothing written. A changed token: written again.
    tc.mirror_claude_keychain(cfg)
    check("unchanged token: no write", len(host.writes()), 1)
    host.items[OPERATOR] = json.dumps(blob("access-B", 3600))
    tc.mirror_claude_keychain(cfg)
    check("a changed token is re-mirrored", len(host.writes()), 2)
    check(
        "...with the new token",
        json.loads(host.items[f"{OPERATOR}-{suffix}"])["claudeAiOauth"]["accessToken"],
        "access-B",
    )

    # 9) A worker item still holding a REAL refresh token (a pre-existing per-worker login) is overwritten
    #    even though the access token matches: the whole point is that no worker item can refresh.
    host.items[f"{OPERATOR}-{suffix}"] = json.dumps(blob("access-B", 3600, "rt-own-chain"))
    tc.mirror_claude_keychain(cfg)
    check("a worker item with a refresh token is stripped", len(host.writes()), 3)
    check(
        "...to an empty refreshToken",
        json.loads(host.items[f"{OPERATOR}-{suffix}"])["claudeAiOauth"]["refreshToken"],
        "",
    )

    # 10) Nothing to mirror from (locked / absent / wiped operator item): the worker's item is kept.
    host.items.pop(OPERATOR)
    tc.mirror_claude_keychain(cfg)
    check("no operator item: no write", len(host.writes()), 3)
    host.items[OPERATOR] = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}})
    tc.mirror_claude_keychain(cfg)
    check("a wiped operator item is not mirrored", len(host.writes()), 3)

    # 11) Not isolated: no Keychain traffic at all. Off macOS: likewise.
    reset_env(isolated=False)
    host = FakeHost({OPERATOR: json.dumps(blob("access-A", 3600))})
    tc.quota.subprocess.run = host
    tc.mirror_claude_keychain(cfg)
    check("the default config dir is never mirrored", host.security_calls, [])
    reset_env()
    tc.quota.sys.platform = "linux"
    tc.mirror_claude_keychain(cfg)
    check("off macOS the Keychain is not consulted", host.security_calls, [])
    tc.quota.sys.platform = "darwin"

    # 12) End to end through the pacer: a renewing read warms the operator's item, mirrors it into the
    #     worker's, and measures usage with the worker's (now fresh) token — in ONE call.
    reset_env()
    host = FakeHost(
        {
            OPERATOR: json.dumps(blob("stale-access", -3600)),
            f"{OPERATOR}-{suffix}": json.dumps(blob("stale-access", -3600, "")),
        }
    )
    tc.quota.subprocess.run = host
    seen = []

    def usage(url, headers, timeout=15):
        seen.append(headers["Authorization"])
        from datetime import UTC, datetime

        def at(d):
            return datetime.fromtimestamp(time.time() + d, tz=UTC).isoformat().replace("+00:00", "Z")

        return (
            200,
            {
                "five_hour": {"utilization": 5, "resets_at": at(600)},
                "seven_day": {"utilization": 5, "resets_at": at(3600)},
            },
            None,
        )

    with patch.object(tc.quota, "_http_get_json", side_effect=usage):
        prov = quota().claude(renew=True)
    check("renewing read: one warm-up", len(host.claude_runs), 1)
    check(
        "renewing read: worker item mirrored",
        json.loads(host.items[f"{OPERATOR}-{suffix}"])["claudeAiOauth"]["accessToken"],
        "fresh-access",
    )
    check("renewing read: usage measured with the fresh token", seen, ["Bearer fresh-access"])
    check("renewing read: provider usable", prov.available, True)

    # 13) An inspecting read never warms — but does keep the mirror current (a mirror is not a rotation).
    reset_env()
    host = FakeHost({OPERATOR: json.dumps(blob("stale-access", -3600))})
    tc.quota.subprocess.run = host
    with patch.object(tc.quota, "_http_get_json", return_value=(401, {}, None)):
        quota().claude()
    check("inspecting read: no warm-up", host.claude_runs, [])
    check("inspecting read: the mirror is still kept current", len(host.writes()), 1)
finally:
    tc.quota.sys.platform = saved_platform
    tc.quota.subprocess.run = saved_run
    tc.quota._host_home = saved_host
    for k, v in saved_env.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

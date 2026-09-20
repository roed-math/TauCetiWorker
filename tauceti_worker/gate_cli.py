"""tauceti_worker.gate_cli — `tauceti-gate`, the gate for shell callers: the three wrapper scripts, the
agent's gh/git shims, git's credential helper, and the operator.

    tauceti-gate admit <kind> <op> <target> [--weight N]   prints a token; exit 75 with the reason if refused
    tauceti-gate record <token> <status> [--detail TEXT] [--detail-file F] [--header K=V]...
    tauceti-gate status [--json]                            works with no network and with no store yet
    tauceti-gate report [--since 1h|24h|...]                design §7
    tauceti-gate halt <reason> [detail]   /  resume        resume prints the incident, clears a cooldown only
    tauceti-gate enable / disable                          the disabled marker refuses every admit (offline)
    tauceti-gate revalidate <op> <target>                  lift one quarantine after fixing what caused it
    tauceti-gate credential get                            a git credential helper (design §4)
    tauceti-gate publication create --kind K --branch B --head-sha S [--pr N] [--repo R] [--remote URL]
    tauceti-gate publication begin <id> <step> [--sha S] [--body-file F]   exit 75 refused, 3 duplicate (skip)
    tauceti-gate publication end <id> <step> <ok|fail> [--remote-id X] [--detail-file F]
    tauceti-gate publication show <id> / list               the ledger (design §6)
    tauceti-gate reconcile [<id> | --all]                   resolve uncertain steps with one read each

`<status>` for record is an HTTP status code, `ok`, `fail`, or `rc:<n>` (a process exit status, in
which case the detail text is what gets classified). `python -m tauceti_worker gate …` is the same
program; `scripts/tauceti-gate` is the wrapper on the agent PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import gate as gate_mod
from . import publications as pub_mod
from .config import Die
from .gate import Gate, GateRefused, Outcome

_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _since(spec: str) -> float:
    spec = spec.strip().lower()
    if spec[-1:] in _SINCE_UNITS and spec[:-1].replace(".", "", 1).isdigit():
        return float(spec[:-1]) * _SINCE_UNITS[spec[-1]]
    if spec.replace(".", "", 1).isdigit():
        return float(spec)
    raise Die(f"--since {spec!r}: use e.g. 15m, 1h, 24h")


def _gate() -> Gate:
    return Gate.from_env()


def cmd_admit(args) -> int:
    g = _gate()
    if not g.enabled:
        print("disabled")  # the no-op token: `record disabled …` is accepted and ignored
        return 0
    try:
        a = g.admit(args.op, args.target, args.kind, wait=not args.no_wait, weight=args.weight)
    except GateRefused as e:
        print(e.message(), file=sys.stderr)
        if e.until:
            print(
                f"gate: until {time.strftime('%H:%M:%S', time.localtime(e.until))} ({int(e.until - time.time())}s)",
                file=sys.stderr,
            )
        return gate_mod.REFUSED_RC
    print(a.token)
    return 0


def _status_arg(raw: str) -> tuple[int | None, bool | None]:
    raw = raw.strip().lower()
    if raw == "ok":
        return None, True
    if raw == "fail":
        return None, False
    if raw.startswith("rc:"):
        return None, raw[3:] == "0"  # a script that treats another exit status as a verdict passes `ok`
    if raw.isdigit():
        n = int(raw)
        return n, n < 400
    raise Die(f"record: status {raw!r} is not an HTTP status, ok, fail, or rc:<n>")


def cmd_record(args) -> int:
    g = _gate()
    if not g.enabled or args.token == "disabled":
        return 0
    status, ok = _status_arg(args.status)
    text = args.detail or ""
    if args.detail_file:
        try:
            text = (text + "\n" + Path(args.detail_file).read_text(errors="replace")).strip()
        except OSError as e:
            text = f"{text}\n(detail file unreadable: {e})".strip()
    headers = {}
    for h in args.header or []:
        k, _, v = h.partition("=")
        headers[k.strip()] = v.strip()
    parsed = gate_mod.parse_status(text)
    if status is None and parsed is not None:
        status, ok = parsed, parsed < 400 and ok is not False
    # Find the admission by token in the in-flight registry (the CLI has no Admission object).
    a = _admission_for(g, args.token, args.kind, args.op, args.target)
    verdict = g.record(a, Outcome(ok=bool(ok), status=status, text=text, headers=headers, duration_ms=args.duration_ms))
    if verdict not in ("none", "store-error"):
        print(f"gate: {verdict}", file=sys.stderr)
    return 0


def _admission_for(g: Gate, token: str, kind: str | None, op: str | None, target: str | None):
    entry = None
    try:
        g._acquire()
        try:
            for e in g._budget()["inflight"]:
                if e.get("token") == token:
                    entry = e
                    break
        finally:
            g._release()
    except gate_mod.StoreError:
        entry = None
    if entry is None and not (kind and op and target):
        raise Die(f"record: token {token!r} is not in flight (pass --kind/--op/--target to record it anyway)")
    return gate_mod.Admission(
        token,
        op or (entry or {}).get("op") or "-",
        target or (entry or {}).get("target") or "-",
        kind or (entry or {}).get("kind") or gate_mod.API_READ,
        float((entry or {}).get("started") or time.time()),
    )


def _summary(v: dict) -> str:
    if v.get("error") and not v.get("dir"):
        return f"gate: {v['error']}"
    if not v.get("enabled"):
        return "gate: disabled (TAUCETI_GATE_DIR unset, TAUCETI_GATE_REQUIRED not 1) — every operation is admitted unaccounted"
    if v.get("error"):
        return f"gate: STORE-ERROR at {v.get('dir')}: {v['error']} — every remote operation is refused (store-error); local work continues"
    state = v.get("state")
    bits = [f"gate: {state}"]
    if state == gate_mod.COOLDOWN and v.get("until"):
        bits.append(
            f"until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(v['until'])))} ({v.get('reason')})"
        )
    elif state == gate_mod.HALTED_MANUAL:
        h = v.get("halt") or {}
        bits.append(
            f"{v.get('reason') or h.get('reason')}: {h.get('detail') or v.get('detail') or 'no detail'} — clear with `tauceti work --clear-halt`"
        )
    if v.get("disabled"):
        bits.append("[disabled marker: every admit refused]")
    if v.get("unconfigured"):
        bits.append("[unconfigured: " + ", ".join(v["unconfigured"]) + " — api reads/mutations refused]")
    b = v.get("budgets") or {}
    if b:
        m, r = b.get("api_mutation", {}), b.get("api_read", {})
        bits.append(
            f"mutations {m.get('minute')}/{m.get('cap_minute')} per min, {m.get('hour')}/{m.get('cap_hour')} per h"
        )
        bits.append(f"reads {r.get('hour')}/{r.get('cap_hour')} per h")
        for repo, w in (b.get("git_push") or {}).items():
            bits.append(
                f"push {repo} {w.get('minute')}/{w.get('cap_minute')} per min, {w.get('hour')}/{w.get('cap_hour')} per h"
            )
    if v.get("inflight"):
        bits.append(f"in flight: {len(v['inflight'])}")
    if v.get("quarantine"):
        bits.append("quarantined: " + ", ".join(sorted(v["quarantine"])))
    if v.get("login"):
        bits.append(f"login {v['login']}")
    q = v.get("publications") or {}
    if q:
        bits.append(f"publications: {q.get('queue_depth', 0)} in progress, {len(q.get('parked') or [])} parked")
        for line in q.get("parked") or []:
            bits.append(f"  parked: {line}")
    return "; ".join(bits)


def cmd_status(args) -> int:
    try:
        v = _gate().status()
    except Die as e:  # REQUIRED without a dir
        v = {"enabled": False, "error": str(e), "state": "STORE-ERROR"}
    if v.get("enabled") and not v.get("error"):
        v["publications"] = pub_mod.queue_summary()  # local files only: the fleet view makes no remote call
    if args.json:
        print(json.dumps(v, indent=2, sort_keys=True, default=str))
    else:
        print(_summary(v))
    return 0


def _events(g: Gate, since_s: float) -> list[dict]:
    if not g.enabled:
        return []
    cutoff = time.time() - since_s
    out = []
    try:
        with open(g._path(gate_mod.EVENTS)) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = _epoch(e.get("ts"))
                if ts is not None and ts >= cutoff:
                    e["_ts"] = ts
                    out.append(e)
    except OSError:
        pass
    return out


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _peak(ts: list[float], window: int) -> int:
    ts = sorted(ts)
    best = j = 0
    for i, t in enumerate(ts):
        while ts[j] < t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


def cmd_report(args) -> int:
    g = _gate()
    if not g.enabled:
        print(_summary(g.probe()))
        return 0
    since = _since(args.since)
    ev = _events(g, since)
    admits = [e for e in ev if e.get("decision") == "admit"]
    records = [e for e in ev if e.get("decision") == "record"]
    refusals = [e for e in ev if e.get("decision") == "refuse"]
    by_kind: dict[str, list[float]] = defaultdict(list)
    for e in admits:
        by_kind[e.get("kind", "-")].extend([e["_ts"]] * int(e.get("weight") or 1))
    print(f"gate report — last {args.since} — {g.dir}")
    print(_summary(g.status()))
    print()
    print("admitted, by kind (total / peak per minute / peak per hour):")
    for kind in gate_mod.KINDS:
        ts = by_kind.get(kind, [])
        if ts or kind != gate_mod.IDENTITY:
            print(f"  {kind:13} {len(ts):5}  {_peak(ts, 60):4}/min  {_peak(ts, 3600):5}/h")
    print("admitted, by op:")
    for (kind, op), n in sorted(Counter((e.get("kind"), e.get("op")) for e in admits).items(), key=lambda kv: -kv[1]):
        print(f"  {kind:13} {op:24} {n}")
    pushes = Counter(gate_mod.normalize_repo(e.get("target", "")) for e in admits if e.get("kind") == gate_mod.GIT_PUSH)
    if pushes:
        print("pushes per repository:")
        for repo, n in pushes.most_common():
            print(f"  {repo:40} {n}")
    if refusals:
        print("refusals, by reason:")
        for reason, n in Counter(e.get("reason") for e in refusals).most_common():
            print(f"  {reason:16} {n}")
    statuses = Counter(str(e.get("status") or ("ok" if e.get("ok") else "fail")) for e in records)
    print("outcomes: " + ", ".join(f"{k}×{n}" for k, n in statuses.most_common()))
    transitions = [
        e
        for e in ev
        if e.get("decision") == "transition" and e.get("to") in (gate_mod.COOLDOWN, gate_mod.HALTED_MANUAL)
    ]
    if transitions:
        print("cooldowns / halts:")
        for e in transitions:
            print(f"  {e.get('ts')} {e.get('to')} {e.get('reason')}: {e.get('detail') or ''}")
    quarantines = [e for e in ev if e.get("decision") == "quarantine"]
    if quarantines:
        print("quarantines:")
        for e in quarantines:
            print(f"  {e.get('ts')} {e.get('op')}:{e.get('target')} {e.get('reason')}")
    uncertain = [e for e in ev if e.get("decision") == "uncertain"]
    print(f"uncertain writes (in flight at a halt): {len(uncertain)}")
    q = pub_mod.queue_summary()
    print(
        f"publications: queue depth {q['queue_depth']}, {len(q['parked'])} parked, "
        f"{q['uncertain_steps']} uncertain step(s), {q['complete']} complete"
    )
    for line in q["open"]:
        print(f"  in progress: {line}")
    for line in q["parked"]:
        print(f"  parked:      {line}")
    # UNINSTRUMENTED: a real gh the shim spawned with no admission behind it.
    spawns = []
    try:
        with open(g._path(gate_mod.SPAWNS)) as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                ts = _epoch(s.get("ts"))
                if ts is not None and ts >= time.time() - since:
                    spawns.append(s)
    except OSError:
        pass
    tokens = {e.get("op_id") for e in admits}
    bad = [s for s in spawns if not s.get("op_id") or s.get("op_id") not in tokens and s.get("op_id") != "disabled"]
    if bad:
        print(f"UNINSTRUMENTED: {len(bad)} gh spawn(s) by the shim reached GitHub with no admission event:")
        for s in bad[:20]:
            print(f"  {s.get('ts')} {s.get('argv')}")
    else:
        print(f"shim spawns: {len(spawns)}, all matched to an admission")
    return 0


def cmd_halt(args) -> int:
    _gate().halt(args.reason, " ".join(args.detail))
    print(_summary(_gate().probe()))
    return 0


def cmd_resume(args) -> int:
    g = _gate()
    incident = g.resume()
    print("incident: " + json.dumps(incident, sort_keys=True, default=str))
    v = g.probe()
    if v.get("state") == gate_mod.HALTED_MANUAL:
        print(
            "still HALTED_MANUAL: a halt clears only through `tauceti work --clear-halt` (which prints the record first)"
        )
    else:
        print(_summary(v))
    return 0


def cmd_enable(args) -> int:
    _gate().set_disabled(False)
    print(_summary(_gate().probe()))
    return 0


def cmd_disable(args) -> int:
    _gate().set_disabled(True)
    print(_summary(_gate().probe()))
    return 0


def cmd_revalidate(args) -> int:
    if _gate().revalidate(args.op, args.target):
        print(f"quarantine lifted: {args.op}:{gate_mod.normalize_repo(args.target)}")
        return 0
    print(f"no quarantine for {args.op}:{gate_mod.normalize_repo(args.target)}", file=sys.stderr)
    return 1


def cmd_credential(args) -> int:
    """A git credential helper. `get` admits a `git_read`/`git_push` (kind from TAUCETI_GIT_OP, the
    wrapper or shim that runs git sets it; default git_read) unless the caller already holds an
    admission (TAUCETI_GATE_TOKEN), then delegates to the real gh's own helper. Refused: prints
    nothing and exits 1, so git fails cleanly with no credential. `store`/`erase` are ignored."""
    if args.action != "get":
        return 0
    fields = {}
    for line in sys.stdin.read().splitlines():
        k, _, v = line.partition("=")
        if k:
            fields[k.strip()] = v.strip()
    if fields.get("host", "") not in ("github.com", "gist.github.com"):
        return 1
    g = _gate()
    kind = os.environ.get(gate_mod.GIT_OP_ENV) or gate_mod.GIT_READ
    if kind not in (gate_mod.GIT_READ, gate_mod.GIT_PUSH):
        kind = gate_mod.GIT_READ
    path = fields.get("path", "")
    target = gate_mod.normalize_repo(path) if path else "-"
    adm = None
    if g.enabled and not _token_in_flight(g, os.environ.get(gate_mod.TOKEN_ENV, "")):
        try:
            adm = g.admit("credential", target, kind, wait=False)
        except GateRefused as e:
            print(e.message(), file=sys.stderr)
            return 1
    real_gh = os.environ.get("TAUCETI_REAL_GH") or "gh"
    try:
        p = subprocess.run(
            [real_gh, "auth", "git-credential", "get"],
            input="\n".join(f"{k}={v}" for k, v in fields.items()) + "\n\n",
            capture_output=True,
            text=True,
        )
    except OSError as e:
        if adm is not None:
            g.record(adm, Outcome(ok=False, text=str(e)))
        return 1
    if adm is not None:
        g.record(adm, Outcome.from_process(p))
    if p.returncode != 0:
        return 1
    sys.stdout.write(p.stdout)
    return 0


DUPLICATE_RC = 3  # `publication begin comment`: the same reply is already posted — skip, nothing to send


def cmd_publication(args) -> int:
    g = _gate()
    if not g.enabled:
        if args.action in ("create", "begin", "end"):
            print("disabled")  # no ledger without a gate: the scripts run unrecorded, as before
            return 0
        print("gate: disabled — no publication ledger", file=sys.stderr)
        return 0
    if args.action == "create":
        pub = pub_mod.Publication.create(
            args.kind,
            branch=args.branch or "",
            head_sha=args.head_sha or "",
            pr=args.pr,
            repo=args.repo or pub_mod.TAUCETI,
            remote=args.remote or "",
        )
        print(pub.id)
        return 0
    if args.action == "list":
        for pub in pub_mod.list_all():
            print(pub.summary())
        return 0
    if args.action == "prune":
        moved = pub_mod.prune_interrupted()
        print(f"archived {len(moved)} interrupted publication(s) with no attempted step")
        return 0
    pub = pub_mod.Publication.load(args.id)
    if args.action == "show":
        print(json.dumps(dataclasses_asdict(pub), indent=2, sort_keys=True))
        return 0
    if args.action == "begin":
        body = None
        if args.body_file:
            try:
                body = Path(args.body_file).read_text(errors="replace")
            except OSError as e:
                raise Die(f"publication begin: --body-file {args.body_file}: {e}") from None
        try:
            # The wrapper script that will hold the write in flight is this CLI's parent.
            pub.begin(args.step, sha=args.sha or "", body=body, sender_pid=os.getppid(), branch=args.branch or "")
        except pub_mod.StepRefused as e:
            print(e.message() + (f" — {e.detail}" if e.detail else ""), file=sys.stderr)
            return DUPLICATE_RC if e.reason == pub_mod.R_DUPLICATE else gate_mod.REFUSED_RC
        return 0
    if args.action == "end":
        text = args.detail or ""
        if args.detail_file:
            try:
                text = (text + "\n" + Path(args.detail_file).read_text(errors="replace")).strip()
            except OSError:
                pass
        try:
            state = pub.end(args.step, args.status == "ok", remote_id=args.remote_id or "", detail=text)
        except pub_mod.StepRefused as e:
            print(e.message(), file=sys.stderr)
            return gate_mod.REFUSED_RC
        print(state)
        return 0
    raise Die(f"publication: unknown action {args.action}")


def dataclasses_asdict(pub) -> dict:
    import dataclasses

    return dataclasses.asdict(pub)


def cmd_reconcile(args) -> int:
    g = _gate()
    if not g.enabled:
        print("gate: disabled — no publication ledger", file=sys.stderr)
        return 0
    if args.id and args.id != "--all":
        pub = pub_mod.Publication.load(args.id)
        with g.locked():
            if pub.mark_stale_sent():
                pub.save()
        verdicts = pub.reconcile() if pub.has_uncertain else {}
        pub = pub_mod.Publication.load(args.id)
        print(pub.summary() + ("  " + " ".join(f"{k}={v}" for k, v in verdicts.items()) if verdicts else ""))
        return 0 if not pub.has_uncertain else 1
    everything = args.all or args.id == "--all"
    results = pub_mod.reconcile_stale(
        None if everything else os.environ.get("TAUCETI_WORKER_ID"), everything=everything
    )
    if not results:
        print("nothing to reconcile")
        return 0
    rc = 0
    for pub_id, verdicts in results:
        pub = pub_mod.Publication.load(pub_id)
        print(pub.summary() + "  " + " ".join(f"{k}={v}" for k, v in verdicts.items()))
        rc = rc or (1 if pub.has_uncertain else 0)
    return rc


def _token_in_flight(g: Gate, token: str) -> bool:
    if not token:
        return False
    try:
        g._acquire()
        try:
            return any(e.get("token") == token for e in g._budget()["inflight"])
        finally:
            g._release()
    except gate_mod.StoreError:
        return False


def build_parser(prog: str = "tauceti-gate") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("admit")
    a.add_argument("kind", choices=gate_mod.KINDS)
    a.add_argument("op")
    a.add_argument("target")
    a.add_argument("--weight", type=int, default=1)
    a.add_argument("--no-wait", action="store_true", help="refuse at once instead of waiting for a slot")
    a.set_defaults(fn=cmd_admit)
    r = sub.add_parser("record")
    r.add_argument("token")
    r.add_argument("status")
    r.add_argument("--detail", default="")
    r.add_argument("--detail-file", default=None)
    r.add_argument("--header", action="append")
    r.add_argument("--duration-ms", type=int, default=None)
    r.add_argument("--kind", choices=gate_mod.KINDS, default=None)
    r.add_argument("--op", default=None)
    r.add_argument("--target", default=None)
    r.set_defaults(fn=cmd_record)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    rp = sub.add_parser("report")
    rp.add_argument("--since", default="1h")
    rp.set_defaults(fn=cmd_report)
    h = sub.add_parser("halt")
    h.add_argument("reason")
    h.add_argument("detail", nargs="*")
    h.set_defaults(fn=cmd_halt)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    sub.add_parser("enable").set_defaults(fn=cmd_enable)
    sub.add_parser("disable").set_defaults(fn=cmd_disable)
    rv = sub.add_parser("revalidate")
    rv.add_argument("op")
    rv.add_argument("target")
    rv.set_defaults(fn=cmd_revalidate)
    c = sub.add_parser("credential")
    c.add_argument("action", choices=["get", "store", "erase"])
    c.set_defaults(fn=cmd_credential)
    pb = sub.add_parser("publication", help="the publication ledger (design §6)")
    pb.add_argument("action", choices=["create", "begin", "end", "show", "list", "prune"])
    pb.add_argument("id", nargs="?", default=None)
    pb.add_argument("step", nargs="?", default=None)
    pb.add_argument("status", nargs="?", default=None, choices=[None, "ok", "fail"])
    pb.add_argument("--kind", choices=pub_mod.KINDS, default=None)
    pb.add_argument("--branch", default=None)
    pb.add_argument("--head-sha", default=None)
    pb.add_argument("--pr", type=int, default=None)
    pb.add_argument("--repo", default=None)
    pb.add_argument("--remote", default=None)
    pb.add_argument("--sha", default=None)
    pb.add_argument("--body-file", default=None)
    pb.add_argument("--remote-id", default=None)
    pb.add_argument("--detail", default="")
    pb.add_argument("--detail-file", default=None)
    pb.set_defaults(fn=cmd_publication_checked)
    rc = sub.add_parser("reconcile", help="resolve uncertain publication steps with one read each")
    rc.add_argument("id", nargs="?", default=None)
    rc.add_argument("--all", action="store_true")
    rc.set_defaults(fn=cmd_reconcile)
    return p


def cmd_publication_checked(args) -> int:
    # An author publication may be created with no branch and no head yet: the agent names the branch
    # and git-safe-push records it (`begin --branch`); the pushed tip is the push step's `sha`.
    need = {"create": ("kind",), "begin": ("id", "step"), "end": ("id", "step", "status"), "show": ("id",)}
    for field in need.get(args.action, ()):
        if getattr(args, field) in (None, ""):
            raise Die(f"publication {args.action}: {field.replace('_', '-')} is required")
    if args.action == "create" and args.kind != pub_mod.KIND_AUTHOR and not (args.branch and args.head_sha and args.pr):
        raise Die(f"publication create --kind {args.kind}: --branch, --head-sha and --pr are required")
    return cmd_publication(args)


def main(argv: list[str] | None = None, prog: str = "tauceti-gate") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        return args.fn(args)
    except Die as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

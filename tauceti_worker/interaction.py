"""tauceti_worker.interaction — the automated interaction contract (brief §8.1): the three toggles
that keep nonessential GitHub writes off during the pilot, the local incident record that replaces
them, and the local reaction marker that keeps the contest path's bookkeeping working with public
reactions off.

    TAUCETI_STUCK_ISSUES          1 (default) files/refreshes the "Review stuck" tracking issue;
                                  0 writes an incident locally and never calls GitHub
    TAUCETI_REACTIONS             1 (default) claims a contest with a 👀 on GitHub; 0 keeps the claim
                                  in a marker file under the fleet store instead (zero GitHub calls)
    TAUCETI_CONTEST_MAX_EXCHANGES 2 (default): automated contest re-reviews per PR head; at the cap
                                  nothing is posted and an incident says a human is needed

Incidents are JSON files under `$TAUCETI_GATE_DIR/incidents/` (per-worker `state/<id>/incidents/`
when there is no gate), one per (kind, key), rewritten in place so a condition that holds for ten
rounds is one file with a count, not ten issues.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import gate as gate_mod
from .config import log
from .paths import HERE

STUCK_ISSUES_ENV = "TAUCETI_STUCK_ISSUES"
REACTIONS_ENV = "TAUCETI_REACTIONS"
CONTEST_MAX_ENV = "TAUCETI_CONTEST_MAX_EXCHANGES"
INCIDENTS = "incidents"
REACTIONS_DIR = "reactions"


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "no", "false", "off")


def stuck_issues_enabled() -> bool:
    return _flag(STUCK_ISSUES_ENV, True)


def reactions_enabled() -> bool:
    return _flag(REACTIONS_ENV, True)


def contest_max_exchanges() -> int:
    raw = os.environ.get(CONTEST_MAX_ENV, "").strip()
    if not raw:
        return 2
    try:
        return max(0, int(raw))
    except ValueError:
        log(f"{CONTEST_MAX_ENV}={raw!r} is not an integer; using 2")
        return 2


def _local_root() -> Path:
    g = gate_mod.current()
    if g.enabled and g.dir is not None:
        return g.dir
    wid = os.environ.get("TAUCETI_WORKER_ID") or "default"
    return HERE / "state" / wid


def incidents_dir() -> Path:
    return _local_root() / INCIDENTS


def _safe(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:120]


def record_incident(kind: str, key: str, **fields) -> Path | None:
    """Write (or refresh) the local incident `<kind>-<key>.json`. Returns the path, or None when even the
    local write failed (never raises: an incident is a report, not a step)."""
    d = incidents_dir()
    p = d / f"{_safe(kind)}-{_safe(key)}.json"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        d.mkdir(parents=True, exist_ok=True)
        prev = {}
        try:
            prev = json.loads(p.read_text())
        except (OSError, ValueError):
            prev = {}
        rec = {
            "kind": kind,
            "key": key,
            "worker": os.environ.get("TAUCETI_WORKER_ID") or "-",
            "first_at": prev.get("first_at") or now,
            "last_at": now,
            "count": int(prev.get("count") or 0) + 1,
            **fields,
        }
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, p)
        return p
    except OSError as e:
        log(f"incident {kind}/{key}: could not be written under {d}: {e}")
        return None


def list_incidents() -> list[dict]:
    d = incidents_dir()
    out = []
    try:
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


# ---- local reaction markers (TAUCETI_REACTIONS=0) ---------------------------------------------------


def _reaction_path(comment_id: int, emoji: str) -> Path:
    return _local_root() / "cache" / REACTIONS_DIR / f"{comment_id}-{_safe(emoji)}.json"


def local_reaction_add(comment_id: int, emoji: str, login: str) -> bool:
    p = _reaction_path(comment_id, emoji)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"comment_id": comment_id, "content": emoji, "login": login, "at": time.time()}))
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def local_reaction_remove(comment_id: int, emoji: str, login: str) -> bool:
    p = _reaction_path(comment_id, emoji)
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError):
        return True  # none held
    if rec.get("login") != login:
        return True
    p.unlink(missing_ok=True)
    return True


def local_reaction_age(comment_id: int, emoji: str) -> int | None:
    """Seconds since the local marker for (comment, emoji) was written, or None when there is none."""
    p = _reaction_path(comment_id, emoji)
    try:
        rec = json.loads(p.read_text())
        at = float(rec.get("at"))
    except (OSError, ValueError, TypeError):
        return None
    return max(0, int(time.time() - at))

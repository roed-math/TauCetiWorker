#!/usr/bin/env bash
# claim.sh — optional, cooperative task de-contention for Tau Ceti agents.
#
# A claim is a custom git ref `refs/tauceti-claims/<key>` in the work repo, pointing at an orphan
# commit whose message is a JSON lease {owner, expires_at, ...}. Acquire/renew/takeover/release are
# all done with ONE atomic GitHub primitive — `git push --force-with-lease=<ref>:[<oid>]`:
#   * expected EMPTY  → create-only (succeeds iff the ref does not exist)
#   * expected <oid>  → succeeds iff the ref still points at <oid> (compare-and-swap)
# (Validated against real GitHub: a second create is rejected "stale info"; a CAS with the wrong
# old-oid is rejected; with the right old-oid it forces. So races have exactly one winner.)
#
# This is [COOP] in the coordination contract: honoring claims only avoids DUPLICATE work. It is
# NOT the safety mechanism — the branch-level `--force-with-lease` in git-safe-push is. A claim can
# expire (TTL) so a dead holder never blocks anyone; takeover of an expired claim is itself a CAS,
# so two reclaimers can't both win.
#
# Usage:
#   claim.sh acquire <key> [ttl_seconds]   # 0 acquired (or renewed mine) · 1 held by another · 2 error
#   claim.sh renew   <key> [ttl_seconds]   # 0 renewed · 1 lost (taken over / gone) · 2 error
#   claim.sh release <key>                 # 0 released (or wasn't mine / already gone)
#   claim.sh holds   <key>                 # 0 I hold it and it's unexpired · 1 otherwise
#   claim.sh read    <key>                 # print the lease JSON (empty if unclaimed)
#   claim.sh list    [--full]              # list live claim refs (--full fetches each lease)
#   claim.sh gc                            # CAS-delete expired claims
#
# Env: CLAIM_REPO (REQUIRED: the <owner>/<repo> holding the leases; the worker exports it. There is
#      no default, and the canonical TauCetiProject/TauCeti is refused outright — every subcommand
#      that would touch the network exits 2 before any git runs when it is unset or names canonical.
#      Claims are cooperative leases, never a write to the repository the work is for),
#      TAUCETI_WORKER_ID (default host-pid),
#      CLAIM_TTL (default 3600), CLAIM_GITDIR_BASE (per-repo scratch parent),
#      CLAIM_GITDIR (explicit scratch object store override),
#      TAUCETI_CLAIM_HELD (a key the worker's round already holds and heartbeats: `acquire` of that
#      exact key returns 0 at once, with no git traffic — the agent's own claim of the target the
#      worker chose for it is otherwise a second push of a lease that is already ours).
#      TAUCETI_GATE_DIR / TAUCETI_GATE_REQUIRED (the fleet GitHub gate, docs/gate.md: every network
#      subcommand is admitted before git runs — the lease writers as a git_push to CLAIM_REPO, the
#      rest as a git_read — and recorded after; a refusal exits 2 like any other registration failure.
#      A TAUCETI_GATE_TOKEN from the worker means it admitted this call already).
#      TAUCETI_REAL_GIT (the git to run; the agent's PATH leads with a shim of it).
set -uo pipefail
# shellcheck source=gate-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gate-lib.sh"
REAL_GIT="${TAUCETI_REAL_GIT:-git}"
LAST_OUT=""

REPO="${CLAIM_REPO:-}"
URL="https://github.com/$REPO"
WID="${TAUCETI_WORKER_ID:-$(hostname)-$$}"
DEFAULT_TTL="${CLAIM_TTL:-3600}"
GITDIR="${CLAIM_GITDIR:-${CLAIM_GITDIR_BASE:-$HOME/.cache/tauceti-claims}/${REPO//\//__}.git}"
NS="refs/tauceti-claims"
export GIT_AUTHOR_NAME="tauceti-claim" GIT_AUTHOR_EMAIL="claim@tauceti.invalid"
export GIT_COMMITTER_NAME="tauceti-claim" GIT_COMMITTER_EMAIL="claim@tauceti.invalid"

now() { date +%s; }
ref_of() { printf '%s/%s' "$NS" "$1"; }

# require_repo — the one gate every network-touching subcommand passes BEFORE ensure_repo. An unset
# CLAIM_REPO used to default to canonical, so a claim.sh run from any shell without the worker's
# export pushed lease refs at the repository the work is for. Now there is no default, and canonical
# is refused however it is spelled (case, a `.git` suffix, a full https://github.com/ URL).
require_repo() {
    local r
    if [[ -z "$REPO" ]]; then
        echo "claim: CLAIM_REPO is not set — refusing (export CLAIM_REPO=<owner>/<repo>, a claim namespace this account can push to)" >&2
        exit 2
    fi
    r=$(printf '%s' "$REPO" | tr '[:upper:]' '[:lower:]')
    r="${r#https://github.com/}"; r="${r#http://github.com/}"; r="${r#git@github.com:}"
    r="${r%/}"; r="${r%.git}"; r="${r%/}"
    if [[ "$r" == "taucetiproject/tauceti" ]]; then
        echo "claim: CLAIM_REPO=$REPO names the canonical repository — refusing (claims never go there; set CLAIM_REPO=<owner>/<repo> to a namespace this account can push to)" >&2
        exit 2
    fi
}

# A private scratch repo just for building + pushing claim objects (no work-repo checkout needed).
ensure_repo() {
    if [[ ! -d "$GITDIR" ]]; then
        mkdir -p "$(dirname "$GITDIR")"
        "$REAL_GIT" init -q --bare "$GITDIR"
    fi
    "$REAL_GIT" -C "$GITDIR" remote get-url origin >/dev/null 2>&1 \
        || "$REAL_GIT" -C "$GITDIR" remote add origin "$URL"
    "$REAL_GIT" -C "$GITDIR" remote set-url origin "$URL"
}
g() { "$REAL_GIT" -C "$GITDIR" "$@"; }

# admit SUB — the gate, after require_repo and before any git: a refused lease op is "could not be
# registered" (2) to every caller, the same cooperative fail-open as a push the namespace rejected.
admit() {
    case "$1" in
        acquire|renew|release|gc) gate_admit git_push "$1" "$REPO" || return 2;;
        *) gate_admit git_read "$1" "$REPO" || return 2;;
    esac
}

empty_tree() { g hash-object -t tree -w /dev/null; }

# remote_oid REF — current oid of REF on origin, or "" if absent.
remote_oid() { g ls-remote origin "$1" 2>/dev/null | awk 'NR==1{print $1}'; }

# lease_json OID — the JSON lease stored in the orphan commit OID (fetched on demand).
lease_json() {
    local oid="$1"
    g cat-file -e "$oid" 2>/dev/null || g fetch -q --no-tags origin "$2" 2>/dev/null || true
    g cat-file commit "$oid" 2>/dev/null | sed '1,/^$/d'
}

# build_oid JSON — write an orphan commit (empty tree) whose message is JSON; print its oid.
build_oid() { printf '%s' "$1" | g commit-tree "$(empty_tree)"; }

# payload KEY EXPIRES — the lease JSON for a claim I'm taking now.
payload() {
    local n; n=$(now)
    jq -nc --arg s "tauceti-claim/v1" --arg o "$WID" --arg h "$(hostname)" \
        --argjson pid "$$" --argjson aq "$n" --argjson ex "$2" --arg res "$1" \
        --arg observed "${CLAIM_OBSERVED_OID:-}" \
        '{schema:$s, owner:$o, host:$h, pid:$pid, acquired_at:$aq, expires_at:$ex,
          resource:$res, observed_branch_oid:($observed | if . == "" then null else . end)}'
}

# push_cas REF EXPECTED NEWOID — CAS push (EXPECTED="" ⇒ create-only). 0 win, 1 lost/rejected.
push_cas() {
    local out
    out=$(g push --force-with-lease="$1:$2" origin "$3:$1" 2>&1)
    if [[ $? -eq 0 ]]; then return 0; fi
    LAST_OUT="$out"
    grep -qiE 'rejected|stale info|failed to push' <<<"$out" && return 1
    echo "claim: unexpected push error on $1: $out" >&2; return 2
}
push_delete() {
    local out
    out=$(g push --force-with-lease="$1:$2" origin ":$1" 2>&1) && return 0
    LAST_OUT="$out"; return 1
}

cmd_acquire() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner exp n
    if [[ -n "${TAUCETI_CLAIM_HELD:-}" && "$key" == "$TAUCETI_CLAIM_HELD" ]]; then
        echo "claim: $key is already held by this worker's round (its heartbeat renews it) — nothing to push" >&2
        return 0
    fi
    require_repo
    admit acquire || return 2
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref")
    if [[ -n "$cur" ]]; then
        js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
        exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
        if [[ "$owner" != "$WID" && "$exp" =~ ^[0-9]+$ && "$exp" -gt "$n" ]]; then
            return 1   # someone else holds a live lease
        fi
        # mine (renew) or expired (takeover): CAS against the observed oid
        local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")")
        push_cas "$ref" "$cur" "$oid"; return $?
    fi
    local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")")
    push_cas "$ref" "" "$oid"   # create-only
}

cmd_renew() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner n
    require_repo
    admit renew || return 2
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 1
    js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
    [[ "$owner" == "$WID" ]] || return 1   # lost / taken over
    local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")")
    push_cas "$ref" "$cur" "$oid"
}

cmd_release() {
    local key="$1" ref cur js owner
    require_repo
    admit release || return 2
    ref=$(ref_of "$key"); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 0
    js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
    [[ "$owner" == "$WID" ]] || return 0   # not mine — leave it
    push_delete "$ref" "$cur"; return 0
}

cmd_holds() {
    local key="$1" ref cur js owner exp n
    require_repo
    admit holds || return 2
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 1
    js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
    exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
    [[ "$owner" == "$WID" && "$exp" =~ ^[0-9]+$ && "$exp" -gt "$n" ]]
}

cmd_read() {
    local ref cur; require_repo; admit read || return 2; ref=$(ref_of "$1"); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 0
    lease_json "$cur" "$ref"
}

cmd_list() {
    require_repo; admit list || return 2; ensure_repo
    g ls-remote origin "$NS/*" 2>/dev/null | while read -r oid ref; do
        local key="${ref#"$NS"/}"
        if [[ "${1:-}" == "--full" ]]; then
            printf '%s\t%s\n' "$key" "$(lease_json "$oid" "$ref" | tr -d '\n')"
        else
            printf '%s\t%s\n' "$key" "$oid"
        fi
    done
}

cmd_gc() {
    local n; require_repo; admit gc || return 2; n=$(now); ensure_repo
    g ls-remote origin "$NS/*" 2>/dev/null | while read -r oid ref; do
        local js exp; js=$(lease_json "$oid" "$ref"); exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
        if [[ "$exp" =~ ^[0-9]+$ && "$exp" -le "$n" ]]; then
            push_delete "$ref" "$oid" && echo "gc: deleted expired $ref" >&2
        fi
    done
}

cmd="${1:-}"; shift || true
case "$cmd" in
    acquire) cmd_acquire "$@"; rc=$?;;
    renew)   cmd_renew "$@"; rc=$?;;
    release) cmd_release "$@"; rc=$?;;
    holds)   cmd_holds "$@"; rc=$?;;
    read)    cmd_read "$@"; rc=$?;;
    list)    cmd_list "$@"; rc=$?;;
    gc)      cmd_gc "$@"; rc=$?;;
    *) echo "usage: claim.sh {acquire|renew|release|holds|read|list|gc} <key> [ttl]" >&2; exit 64;;
esac
# Record the outcome of the admission this process made (none, when the worker admitted for it, or
# when the held-key short-circuit / require_repo answered before any git ran). 0 and 1 are verdicts
# (mine / another's), not transport failures; 2 carries the last git output for classification.
if [[ "$GATE_OWNED" == 1 ]]; then
    if [[ "$rc" -le 1 ]]; then gate_record ok
    else
        detail=$(mktemp); printf '%s\n' "$LAST_OUT" > "$detail"
        gate_record fail "$detail"; rm -f "$detail"
    fi
fi
exit "$rc"

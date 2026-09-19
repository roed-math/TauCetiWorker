# gate-lib.sh — sourced by claim.sh, git-safe-push, gh-safe-pr-create and the gh/git shims: the shell
# side of the fleet GitHub gate (docs/gate.md). Not executable; source it.
#
#   gate_enabled            0 iff TAUCETI_GATE_DIR is set or TAUCETI_GATE_REQUIRED=1 (else every call
#                           below is a no-op, which keeps upstream behaviour for operators without the gate)
#   gate_admit KIND OP TGT  0 admitted (GATE_TOKEN set; TAUCETI_GATE_TOKEN and, for git kinds,
#                           TAUCETI_GIT_OP exported so a child git/credential helper does not admit
#                           again), 75 refused ("gate: refused (<reason>)" already on stderr).
#                           A TAUCETI_GATE_TOKEN inherited from the caller means the caller admitted
#                           this very operation (the worker's run_claim_sh, the shim): reuse it, and
#                           leave the recording to the caller too.
#   gate_record STATUS [F]  record the outcome of an admission this process made (STATUS is an HTTP
#                           status, ok, fail or rc:<n>; F a file holding the detail text)
#   gate_spawn ARGV...      note a real `gh` spawn in TAUCETI_GATE_DIR/spawns.log for `report`
#
# The CLI is the sibling tauceti-gate (TAUCETI_GATE_CLI overrides, for tests).
GATE_CLI="${TAUCETI_GATE_CLI:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tauceti-gate}"
GATE_TOKEN=""
GATE_OWNED=0

gate_enabled() { [[ -n "${TAUCETI_GATE_DIR:-}" || "${TAUCETI_GATE_REQUIRED:-}" == "1" ]]; }

gate_admit() {
    if [[ -n "${TAUCETI_GATE_TOKEN:-}" ]]; then
        GATE_TOKEN="$TAUCETI_GATE_TOKEN"; GATE_OWNED=0
        return 0
    fi
    if ! gate_enabled; then GATE_TOKEN=""; GATE_OWNED=0; return 0; fi
    local tok
    tok=$("$GATE_CLI" admit "$1" "$2" "$3") || return 75
    GATE_TOKEN="$tok"; GATE_OWNED=1
    export TAUCETI_GATE_TOKEN="$tok"
    case "$1" in git_read|git_push) export TAUCETI_GIT_OP="$1";; esac
    return 0
}

gate_record() {
    [[ "$GATE_OWNED" == 1 && -n "$GATE_TOKEN" ]] || return 0
    if [[ -n "${2:-}" && -f "$2" ]]; then
        "$GATE_CLI" record "$GATE_TOKEN" "$1" --detail-file "$2"
    else
        "$GATE_CLI" record "$GATE_TOKEN" "$1"
    fi
    GATE_OWNED=0; GATE_TOKEN=""
    unset TAUCETI_GATE_TOKEN TAUCETI_GIT_OP
    return 0
}

gate_spawn() {
    [[ -n "${TAUCETI_GATE_DIR:-}" && -d "${TAUCETI_GATE_DIR:-}" ]] || return 0
    local ts argv
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    argv=$(printf '%q ' "$@")
    printf '{"ts":"%s","op_id":"%s","argv":"%s"}\n' "$ts" "${GATE_TOKEN:-${TAUCETI_GATE_TOKEN:-}}" "${argv//\"/\\\"}" \
        >> "$TAUCETI_GATE_DIR/spawns.log" 2>/dev/null || true
}

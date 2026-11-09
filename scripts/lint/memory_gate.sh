#!/usr/bin/env bash
# Each step uses one client process to wait for the admission service.

BUILD_OVERRIDE_GUARD_DIR="${BASH_SOURCE[0]%/*}"
[[ "$BUILD_OVERRIDE_GUARD_DIR" != "${BASH_SOURCE[0]}" ]] || BUILD_OVERRIDE_GUARD_DIR=.
source "$BUILD_OVERRIDE_GUARD_DIR/../build_hold/override_guard.sh" || exit 2
build_override_refusal "${BASH_SOURCE[0]}" lint/memory_gate.sh || exit 2
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"
BUILD_TARGETS_SCRIPT="${BUILD_TARGETS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_targets" && pwd)/build_targets.py}"
BUILDLOG_MEM_ADMIT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_queue" && pwd)/admission_client.py"
BUILDLOG_MEM_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/py"

# Each new wait uses its own identity. Rejoining callers restore it after sourcing.
BUILDLOG_MEM_WAIT_SINCE="" BUILDLOG_MEM_WAIT_FIRST_FILE=""
export -n BUILDLOG_MEM_WAIT_SINCE BUILDLOG_MEM_WAIT_FIRST_FILE

buildlog_memory_python() {
    "$BUILDLOG_MEM_PY" "$@"
}

# Status 0 when the holder directory is missing or readable with no entry.
# This shell must expand patterns unfiltered; any doubt starts Python.
build_hold_nothing_held() {
    [[ -n "${BUILD_HOLD_DIR-x}" ]] || return 1
    [[ "$-" != *f* && -z "${GLOBIGNORE-}" ]] || return 1
    if shopt -q failglob; then return 1; fi
    local holder_dir="${BUILD_HOLD_DIR-$HOME/.local/state/build-hold}" entry
    if [[ ! -e "$holder_dir" && ! -L "$holder_dir" ]]; then return 0; fi
    [[ -d "$holder_dir" && -r "$holder_dir" && -x "$holder_dir" ]] || return 1
    for entry in "$holder_dir"/* "$holder_dir"/.[!.]* "$holder_dir"/..?*; do
        if [[ -e "$entry" || -L "$entry" ]]; then return 1; fi
    done
    return 0
}

# A waiting step has a call id for its wait-time and interrupt records. A verify.sh step
# uses verify's BUILDLOG_CALL_ID. A step from any other caller (cargo-port, an
# agent that runs lint) gets an id of the same form, made once per shell when
# a hold is first seen. The id stays in this shell and is not exported: build
# log rows join BUILDLOG_CALL_ID to verify's call records, and this caller has
# no call record.
BUILDLOG_HOLD_CALL_ID="${BUILDLOG_HOLD_CALL_ID:-}" BUILDLOG_HOLD_CALLER=""

# Linux target slots and their build budget provide admission on the target.
buildlog_target_admitted() {
    [[ "${BUILDLOG_CALLER:-}" == verify-linux ]]
}

# Status 0 when natedev's hold applies to this step's caller. The Mac leg of a
# test (verify-mac) and every run on the Mac answer to the Mac's claims, not
# to natedev's hold. Other callers ask the check when the fast path cannot
# prove that the holder directory is empty.
build_hold_applies() {
    buildlog_target_admitted && return 1
    if [[ -n "${BUILDLOG_CALL_ID:-}" && "${BUILDLOG_CALLER:-}" == verify ]]; then return 0; fi
    [[ "${BUILDLOG_CALLER:-}" != verify-mac && "${OSTYPE:-}" != darwin* ]] || return 1
    return 0
}

# Sets BUILDLOG_HOLD_CALL_ID and BUILDLOG_HOLD_CALLER for build_targets.py.
# BUILDLOG_HOLD_CALLER is empty for verify, so verify's messages keep their text.
build_hold_call() {
    if [[ -n "${BUILDLOG_CALL_ID:-}" && "${BUILDLOG_CALLER:-}" == verify ]]; then
        BUILDLOG_HOLD_CALL_ID=$BUILDLOG_CALL_ID BUILDLOG_HOLD_CALLER=""
        return 0
    fi
    BUILDLOG_HOLD_CALLER="${BUILDLOG_CALLER:-lint}"
    if [[ -z "${BUILDLOG_HOLD_CALL_ID:-}" ]]; then
        BUILDLOG_HOLD_CALL_ID="v$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"
    fi
}

# Only the check's allow status permits local work. Keep hold and leave
# statuses for callers that wait or select another machine.
build_check_record_failure() {
    local reason=$1
    if [[ -n "${BUILDLOG_CHECK_FAILED_FILE:-}" ]]; then
        printf '%s\n' "$reason" > "$BUILDLOG_CHECK_FAILED_FILE" || true
    fi
}

build_check_stop() {
    local status=$1 reason=$2
    build_check_record_failure "$reason"
    printf '%s; step refused\n' "$reason" >&2
    return "$status"
}

build_hold_check_result() {
    local status=$1 check=$2
    case "$check:$status" in
        *:0|check-local:10|register-local:10|wait-local:10|wait-local:12|wait-local:75) return "$status" ;;
    esac
    build_check_stop "$status" "build hold check $check failed (exit $status)"
}

build_hold_wait_local() {
    build_hold_applies || return 0
    if build_hold_nothing_held; then return 0; fi
    build_hold_call
    local waiter_pid status=0 diagnostics output_fd output_pid output_status=0
    local arguments=(wait-local --call-id "$BUILDLOG_HOLD_CALL_ID" --pid "${BUILDLOG_HOLD_PID:-$$}")
    if [[ -n "$BUILDLOG_HOLD_CALLER" ]]; then arguments+=(--caller "$BUILDLOG_HOLD_CALLER"); fi
    # A held natedev cannot start the step, so the leave hook also runs here:
    # status 75 (LINT_LEFT_QUEUE_STATUS) means another machine is free.
    if [[ -n "${LINT_QUEUE_LEAVE_HOOK:-${BUILDLOG_MAC_IDLE_HOOK:-}}" ]]; then
        arguments+=(--leave-hook "${LINT_QUEUE_LEAVE_HOOK:-$BUILDLOG_MAC_IDLE_HOOK}")
    fi
    diagnostics=$(mktemp "${TMPDIR:-/tmp}/hold-check.XXXXXX") || {
        build_check_stop 2 'cannot record build hold check output'
        return 2
    }
    # Keep wait messages visible and check that a refusal included its reason.
    if ! exec {output_fd}> >(tee "$diagnostics" >&2); then
        rm -f "$diagnostics"
        build_check_stop 2 'cannot read build hold check output'
        return 2
    fi
    output_pid=$!
    buildlog_memory_python "$BUILD_TARGETS_SCRIPT" "${arguments[@]}" 2>&"$output_fd" &
    waiter_pid=$!
    exec {output_fd}>&-
    wait "$waiter_pid" || status=$?
    wait "$output_pid" || output_status=$?
    if (( output_status != 0 )); then
        rm -f "$diagnostics"
        build_check_stop 2 'cannot read build hold check output'
        return 2
    fi
    if [[ ( "$status" == 10 || "$status" == 12 ) && ! -s "$diagnostics" ]]; then
        rm -f "$diagnostics"
        build_check_stop "$status" "build hold check wait-local gave no reason (exit $status)"
        return "$status"
    fi
    rm -f "$diagnostics"
    build_hold_check_result "$status" wait-local
}

build_hold_check_local() {
    build_hold_applies || return 0
    if build_hold_nothing_held; then return 0; fi
    build_hold_call
    local status=0
    buildlog_memory_python "$BUILD_TARGETS_SCRIPT" check-local --caller "${BUILDLOG_HOLD_CALLER:-verify}" || status=$?
    build_hold_check_result "$status" check-local
}

buildlog_memory_refuse() {
    BUILDLOG_MEM_OUTCOME=CheckFailed
    build_check_stop 2 "memory admission check failed ($1)"
}

buildlog_memory_event() { :; }

buildlog_wait_for_memory() {
    BUILDLOG_MEM_WAIT_S=0 BUILDLOG_MEM_OUTCOME=MeminfoUnavailable BUILDLOG_MEM_RESERVATION=""
    if buildlog_target_admitted || [[ "${OSTYPE:-}" == darwin* && -z "${BUILDLOG_MEMINFO+x}" ]]; then
        BUILDLOG_MEM_OUTCOME=Granted
        return 0
    fi
    local grant status=0 action=admit owner=$BASHPID
    grant=$(mktemp) || {
        buildlog_memory_refuse 'cannot record admission client output'
        return 2
    }
    BUILDLOG_MEM_WAIT_FROM="${BUILDLOG_MEM_WAIT_SINCE:-${EPOCHSECONDS:-$(date +%s)}}"
    (( $# > 0 )) || action=launch
    (
        set -o pipefail
        LINT_QUEUE_LEAVE_HOOK="${LINT_QUEUE_LEAVE_HOOK:-}" \
        BUILDLOG_MAC_IDLE_HOOK="${BUILDLOG_MAC_IDLE_HOOK:-}" \
        buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" "$action" --pid "$owner" \
            --since "$BUILDLOG_MEM_WAIT_FROM" --sidecar "${BUILDLOG_PEAK:+$BUILDLOG_PEAK.cgroup}" \
            --shell-state "$grant" --wait-events -- "$@" |
            while IFS= read -r event; do buildlog_memory_event "$event"; done
    ) || status=$?
    source "$grant"
    if (( status == 75 )); then
        BUILDLOG_MEM_OUTCOME=LeftQueue
        status=0
    elif (( status != 0 )); then
        if [[ "$BUILDLOG_MEM_OUTCOME" != Refused ]]; then
            buildlog_memory_refuse "admission client failed (exit $status)" || true
        else
            build_check_record_failure 'memory admission client refused the step'
        fi
        BUILDLOG_MEM_OUTCOME=Refused
    fi
    rm -f "$grant"
    return "$status"
}

buildlog_release_memory() {
    local reservation="${BUILDLOG_MEM_RESERVATION:-}"
    BUILDLOG_MEM_RESERVATION=""
    export BUILDLOG_MEM_CLEANUP_TIMING
    [[ -n "$reservation" ]] || return 0
    buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" release "$reservation" "${1:-0}" >/dev/null 2>&1 || true
}

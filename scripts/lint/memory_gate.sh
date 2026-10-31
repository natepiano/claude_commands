#!/usr/bin/env bash
# Admit when MemAvailable covers this step, pending starts, and earlyoom's
# 5% reserve. Running steps use measured resident plus swap memory.
# The gate does not charge their historical peaks again. At
# BUILDLOG_MEM_WAIT_LIMIT_S the gate prints one notice and goes on waiting, and
# the step becomes first in line. BUILDLOG_MEM_WAIT_SINCE (epoch seconds) keeps
# the place of a step that waits again; the gate sets BUILDLOG_MEM_WAIT_FROM. The first check resolves git and history;
# held polls read meminfo, process trees and the ledger under its lock. Release starts
# Python once more. With hana nextest's 5,397 index rows, measured first check
# 431 ms, cached poll 102 ms, and release 97 ms (temporary ledger and real
# read-only index, 2026-10-06 PDT).
#
# LINT_QUEUE_LEAVE_HOOK, when set, is a shell command run on every held poll
# after the first; status 0 ends the wait with BUILDLOG_MEM_OUTCOME=LeftQueue
# and no reservation, so the caller can run the step elsewhere. A poll that
# admits never runs it: a step that can start here starts at once.
# verify's Mac hook atomically claims capacity before returning success.

BUILD_OVERRIDE_GUARD_DIR="${BASH_SOURCE[0]%/*}"
[[ "$BUILD_OVERRIDE_GUARD_DIR" != "${BASH_SOURCE[0]}" ]] || BUILD_OVERRIDE_GUARD_DIR=.
source "$BUILD_OVERRIDE_GUARD_DIR/../build_hold/override_guard.sh" || exit 2
build_override_refusal "${BASH_SOURCE[0]}" lint/memory_gate.sh || exit 2
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"
BUILD_TARGETS_SCRIPT="${BUILD_TARGETS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_targets" && pwd)/build_targets.py}"
BUILDLOG_MEM_ADMIT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/memory_admit.py"
USER_ZONE_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/user_zone.py"

# Queue identity belongs to this caller, never to a parent process's wait.
# Rejoining callers assign these after sourcing, around the one step that
# left the queue. Do not export a completed wait to a child launch or gate.
BUILDLOG_MEM_WAIT_SINCE="" BUILDLOG_MEM_WAIT_FIRST_FILE=""
export -n BUILDLOG_MEM_WAIT_SINCE BUILDLOG_MEM_WAIT_FIRST_FILE

# A rejoined wait reports its full age; a fresh wait retains the local
# elapsed clock (also used by launch callers with no queue identity).
buildlog_memory_wait_time() {
    if [[ -n "${BUILDLOG_MEM_WAIT_SINCE:-}" ]]; then
        BUILDLOG_MEM_WAIT_S=$(( ${EPOCHSECONDS:-$(date +%s)} - BUILDLOG_MEM_WAIT_FROM ))
        (( BUILDLOG_MEM_WAIT_S >= 0 )) || BUILDLOG_MEM_WAIT_S=0
    else
        BUILDLOG_MEM_WAIT_S=$(( SECONDS - $1 ))
    fi
}

buildlog_memory_python() {
    if [[ -x "$HOME/.claude/scripts/lib/py" ]]; then
        "$HOME/.claude/scripts/lib/py" "$@"
    else
        python3 "$@"
    fi
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

# Status 0 when natedev's hold applies to this step's caller. The Mac leg of a
# test (verify-mac) and every run on the Mac answer to the Mac's claims, not
# to natedev's hold. Other callers ask the check when the fast path cannot
# prove that the holder directory is empty.
build_hold_applies() {
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
build_check_stop() {
    local status=$1 reason=$2
    if [[ -n "${BUILDLOG_CHECK_FAILED_FILE:-}" ]]; then
        printf '%s\n' "$reason" > "$BUILDLOG_CHECK_FAILED_FILE" || true
    fi
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
    python3 "$BUILD_TARGETS_SCRIPT" "${arguments[@]}" 2>&"$output_fd" &
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
    python3 "$BUILD_TARGETS_SCRIPT" check-local --caller "${BUILDLOG_HOLD_CALLER:-verify}" || status=$?
    build_hold_check_result "$status" check-local
}

buildlog_memory_refuse() {
    BUILDLOG_MEM_OUTCOME=CheckFailed
    build_check_stop 2 "memory admission check failed ($1)"
}

buildlog_memory_line_inner() {
    local kind=$1 available=$2 threshold=$3 need=$4 source=$5 count=$6 promised=$7 running=$8 reserve=$9 summary=${10}
    local description
    case "$source" in
        measured) description="the 90th percentile of $count measured runs" ;;
        buildlog) description="0.65 of the 90th percentile peak of $count runs" ;;
        pooled) description="the 90th percentile of $count measured runs of this step in every repo" ;;
        shape)
            if [[ "$count" == 0 ]]; then
                description="the same-shape fallback: no earlier builds"
            else
                description="a same-shape promise from $count earlier builds"
            fi ;;
        *) description="no history, so the fallback" ;;
    esac
    if [[ "$kind" == first || "$kind" == periodic ]]; then
        awk -v a="$available" -v t="$threshold" -v n="$need" -v p="$promised" -v m="$running" -v r="$reserve" \
            -v measured="${13:-0}" -v head_need="${14:-0}" -v head_label="${15:-}" -v kind="$kind" -v minutes="$(( BUILDLOG_MEM_WAIT_S / 60 ))" \
            -v since="$(TZ="$(buildlog_memory_python "$USER_ZONE_SCRIPT" 2>/dev/null)" date '+%H:%M %Z')" -v repo="${11:-}" -v step="${12:-build}" -v desc="$description" -v summary="$summary" \
            'BEGIN { subject=(repo == "" ? step : repo " " step); if (kind == "first") printf "waiting for memory since %s: ",since > "/dev/stderr"; else printf "still waiting for memory (%d min): ",minutes > "/dev/stderr"; printf "the machine has %.1f GiB free; a build starts at %.1f (%s needs %.1f GiB",a/1073741824,t/1073741824,subject,n/1073741824 > "/dev/stderr"; if (kind == "first") printf ", %s",desc > "/dev/stderr"; if (head_need>0) printf "; head %s needs %.1f GiB",head_label,head_need/1073741824 > "/dev/stderr"; if (m==0 && summary!="") printf "; %s",summary > "/dev/stderr"; else { printf "; %.1f GiB promised, %.1f GiB measured across %d running steps",p/1073741824,measured/1073741824,m > "/dev/stderr"; if (m>0) printf ": %s",summary > "/dev/stderr"; }; if (r>0) printf "; %.1f GiB kept for earlyoom",r/1073741824 > "/dev/stderr"; print ")" > "/dev/stderr" }'
    elif [[ "$kind" == left ]]; then
        printf 'another machine is free after %d min %d s: leaving the memory wait\n' "$(( BUILDLOG_MEM_WAIT_S / 60 ))" "$(( BUILDLOG_MEM_WAIT_S % 60 ))" >&2
    else
        printf 'memory free after %d min %d s: starting\n' "$(( BUILDLOG_MEM_WAIT_S / 60 ))" "$(( BUILDLOG_MEM_WAIT_S % 60 ))" >&2
    fi
}

buildlog_memory_line() {
    buildlog_memory_line_inner "$@"
}

buildlog_memory_check_launch_limit() {
    # An app launch (no argv) has no reservation and its hook a 960 s timeout: it alone keeps the limit.
    if (( $1 == 0 && (( $2 != 0 && BUILDLOG_MEM_WAIT_S >= $3 ) || $3 == 0) )); then
        buildlog_memory_refuse 'launch wait limit reached without admission'
        return 2
    fi
    return 0
}

buildlog_wait_for_memory() {
    BUILDLOG_MEM_WAIT_S=0 BUILDLOG_MEM_OUTCOME=MeminfoUnavailable BUILDLOG_MEM_RESERVATION=""
    # This admission gate serves Linux. The Mac uses its claim admission.
    if [[ "${OSTYPE:-}" == darwin* && -z "${BUILDLOG_MEMINFO+x}" ]]; then
        BUILDLOG_MEM_OUTCOME=Granted
        return 0
    fi
    local meminfo="${BUILDLOG_MEMINFO:-/proc/meminfo}" live_path line available_kb available result error_line
    local started=0 announced=0 next_notice=60 interval="${BUILDLOG_MEM_POLL_S:-5}" limit="${BUILDLOG_MEM_WAIT_LIMIT_S:-900}"
    local failed_since=-1 retry_limit="${BUILDLOG_MEM_CHECK_RETRY_S:-60}"
    local state threshold need source count promised running reserve summary reservation repo step fresh reason worktree measured head_need head_label fields numeric noticed=0 caller_pid=$BASHPID
    local cached_repo="" cached_step="" cached_worktree="" cached_need=- cached_source="" cached_count=0
    [[ "$interval" =~ ^[0-9]+$ && "$interval" -gt 0 ]] || interval=5
    [[ "$limit" =~ ^[0-9]+$ && "$limit" -ge 0 ]] || limit=900
    [[ "$retry_limit" =~ ^[0-9]+$ ]] || retry_limit=60
    BUILDLOG_MEM_WAIT_FROM="${BUILDLOG_MEM_WAIT_SINCE:-${EPOCHSECONDS:-$(date +%s)}}"
    if [[ -n "${BUILDLOG_MEM_WAIT_FIRST_FILE:-}" && -s "$BUILDLOG_MEM_WAIT_FIRST_FILE" ]]; then
        read -r BUILDLOG_MEM_WAIT_FROM < "$BUILDLOG_MEM_WAIT_FIRST_FILE"
    fi
    [[ "$BUILDLOG_MEM_WAIT_FROM" =~ ^[0-9]+$ ]] || { buildlog_memory_refuse 'invalid first wait time'; return 2; }
    while [[ -r "$meminfo" ]]; do
        if [[ -n "${BUILDLOG_LIVE_MEMORY_TRIPWIRE:-}" ]]; then
            if ! live_path=$(realpath -m -- "$meminfo"); then
                buildlog_memory_refuse 'cannot resolve memory reading path'
                return 2
            fi
            if [[ "$live_path" == /proc/meminfo || "$live_path" == /proc/pressure/memory ]]; then
                result=$(buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" tripwire "$live_path" 2>&1) || true
                error_line=${result##*$'\n'}
                buildlog_memory_refuse "${error_line:-live memory tripwire failed}"
                return 2
            fi
        fi
        available_kb=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^MemAvailable:[[:space:]]+([0-9]+)[[:space:]]+kB[[:space:]]*$ ]]; then
                available_kb="${BASH_REMATCH[1]}"
            fi
        done < "$meminfo" 2>/dev/null || break
        [[ -n "$available_kb" ]] || break
        # A single-write FIFO has no second read for Python. Launch hooks have
        # no reservation, and their alone threshold is always the old 12 GiB.
        if [[ -p "$meminfo" && $# -eq 0 ]] && (( 10#$available_kb >= 12582912 )); then
            BUILDLOG_MEM_OUTCOME=Granted
            break
        fi
        if (( announced != 0 )); then
            buildlog_memory_wait_time "$started"
            if (( $# > 0 && BUILDLOG_MEM_WAIT_S >= limit && noticed == 0 )); then
                noticed=1
                printf 'still waiting for memory after %d min: this step is now first in line, and no build starts without memory\n' "$(( BUILDLOG_MEM_WAIT_S / 60 ))" >&2
            fi
        fi
        if ! result="$(BUILDLOG_MEM_DETAIL=1 buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" check "$meminfo" "$caller_pid" "${BUILDLOG_PEAK:+$BUILDLOG_PEAK.cgroup}" "$BUILDLOG_MEM_WAIT_FROM" "$cached_repo" "$cached_step" "$cached_worktree" "$cached_need" "$cached_source" "$cached_count" "$@" 2>&1)"; then
            error_line=${result##*$'\n'}
            if (( announced != 0 )); then
                buildlog_memory_check_launch_limit "$#" "$announced" "$limit" || return 2
                if (( failed_since < 0 )); then failed_since=$SECONDS; fi
                if (( SECONDS - failed_since < 10#$retry_limit )); then
                    printf 'memory admission check failed (%s); trying again, the step keeps its place\n' "${error_line:-checker returned an error}" >&2
                    sleep "$interval"
                    continue
                fi
            fi
            buildlog_memory_refuse "${error_line:-checker returned an error}"
            return 2
        fi
        failed_since=-1
        fields=${result//[^|]/}
        if [[ "$result" == *$'\n'* || ( ${#fields} != 14 && ${#fields} != 17 ) ]]; then
            buildlog_memory_refuse 'invalid checker output'
            return 2
        fi
        IFS='|' read -r state threshold need source count promised running reserve summary reservation repo step fresh reason worktree measured head_need head_label <<< "$result"
        if [[ ${#fields} == 14 ]]; then measured=0 head_need=0; fi
        case "$state:$reason" in
            admit:reserved|admit:launch|hold:held|hold:launch) ;;
            *) buildlog_memory_refuse "invalid admission answer $state:$reason"; return 2 ;;
        esac
        for numeric in "$threshold" "$need" "$count" "$promised" "$running" "$reserve" "$fresh" "$measured" "$head_need"; do
            if [[ ! "$numeric" =~ ^[0-9]+$ ]]; then
                buildlog_memory_refuse 'invalid memory reading'
                return 2
            fi
        done
        if [[ "$state" == admit && $# -gt 0 && ( "$reason" != reserved || -z "$reservation" ) ]]; then
            buildlog_memory_refuse 'step has no memory reservation'
            return 2
        fi
        available=$fresh
        # A refreshed estimate replaces the shell cache on every answer.
        cached_repo=$repo cached_step=$step cached_worktree=$worktree
        cached_need=$need cached_source=$source cached_count=$count
        if [[ -n "${BUILDLOG_MEM_WAIT_SINCE:-}" ]]; then buildlog_memory_wait_time "$started"; fi
        if [[ "$state" == admit ]]; then
            BUILDLOG_MEM_RESERVATION="$reservation"
            BUILDLOG_MEM_OUTCOME=Granted
            if (( announced != 0 || BUILDLOG_MEM_WAIT_S > 0 )); then
                buildlog_memory_line done "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            fi
            break
        fi
        buildlog_memory_check_launch_limit "$#" "$announced" "$limit" || return 2
        if (( announced == 0 )); then
            if [[ -n "${BUILDLOG_MEM_WAIT_FIRST_FILE:-}" && ! -s "$BUILDLOG_MEM_WAIT_FIRST_FILE" ]]; then
                printf '%s\n' "$BUILDLOG_MEM_WAIT_FROM" > "$BUILDLOG_MEM_WAIT_FIRST_FILE"
            fi
            started=$SECONDS
            announced=1
            buildlog_memory_line first "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary" "$repo" "$step" "${measured:-0}" "${head_need:-0}" "${head_label:-}"
        elif [[ -n "${LINT_QUEUE_LEAVE_HOOK:-${BUILDLOG_MAC_IDLE_HOOK:-}}" ]] \
              && /bin/sh -c "${LINT_QUEUE_LEAVE_HOOK:-$BUILDLOG_MAC_IDLE_HOOK}" </dev/null >/dev/null 2>&1; then
            BUILDLOG_MEM_OUTCOME=LeftQueue
            buildlog_memory_line left "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            break
        elif (( BUILDLOG_MEM_WAIT_S >= next_notice )); then
            buildlog_memory_line periodic "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary" "$repo" "$step" "${measured:-0}" "${head_need:-0}" "${head_label:-}"
            next_notice=$(( next_notice + 60 ))
        fi
        sleep "$interval"
    done
    if (( announced != 0 )) || [[ -n "${BUILDLOG_MEM_WAIT_SINCE:-}" ]]; then
        buildlog_memory_wait_time "$started"
    fi
    case "$BUILDLOG_MEM_OUTCOME" in
        Granted|LeftQueue) return 0 ;;
        *) buildlog_memory_refuse "cannot read MemAvailable from $meminfo" ;;
    esac
}

buildlog_release_memory() {
    local reservation="${BUILDLOG_MEM_RESERVATION:-}"
    BUILDLOG_MEM_RESERVATION=""
    [[ -n "$reservation" ]] || return 0
    buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" release "$reservation" "${1:-0}" >/dev/null 2>&1 || true
}

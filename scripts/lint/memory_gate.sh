#!/usr/bin/env bash
# Admit when MemAvailable covers this step's expected anon peak, unfinished
# growth promised to running builds (memory_admit.joint_promise), and earlyoom's
# 5% reserve. Alone, the old 12 GiB threshold caps the threshold, so a wait ends
# when builds end. No wait ends in a start without memory: at
# BUILDLOG_MEM_WAIT_LIMIT_S the gate prints one notice and goes on waiting, and
# the step becomes first in line. BUILDLOG_MEM_WAIT_SINCE (epoch seconds) keeps
# the place of a step that waits again; the gate sets BUILDLOG_MEM_WAIT_FROM. The first check resolves git and history;
# held polls read only meminfo and the ledger under its lock. Release starts
# Python once more. With hana nextest's 5,397 index rows, measured first check
# 431 ms, cached poll 102 ms, and release 97 ms (temporary ledger and real
# read-only index, 2026-10-06 PDT).
#
# LINT_QUEUE_LEAVE_HOOK, when set, is a shell command run on every held poll
# after the first; status 0 ends the wait with BUILDLOG_MEM_OUTCOME=LeftQueue
# and no reservation, so the caller can run the step elsewhere. A poll that
# admits never runs it: a step that can start here starts at once.
# mac_test.py free-now is written as this hook: local state only, 130 ms.

BUILD_OVERRIDE_GUARD_DIR="${BASH_SOURCE[0]%/*}"
[[ "$BUILD_OVERRIDE_GUARD_DIR" != "${BASH_SOURCE[0]}" ]] || BUILD_OVERRIDE_GUARD_DIR=.
source "$BUILD_OVERRIDE_GUARD_DIR/../build_hold/override_guard.sh" || exit 2
build_override_refusal "${BASH_SOURCE[0]}" lint/memory_gate.sh || exit 2
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"
BUILD_TARGETS_SCRIPT="${BUILD_TARGETS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_targets" && pwd)/build_targets.py}"
BUILDLOG_MEM_ADMIT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/memory_admit.py"
USER_ZONE_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/user_zone.py"

buildlog_memory_python() {
    if [[ -x "$HOME/.claude/scripts/lib/py" ]]; then
        "$HOME/.claude/scripts/lib/py" "$@"
    else
        python3 "$@"
    fi
}

# Status 0 only when the holder directory is a readable directory with no
# entry at all and this shell expands patterns unfiltered; any doubt starts
# the Python, which decides as before.
build_hold_nothing_held() {
    [[ -n "${BUILD_HOLD_DIR-x}" ]] || return 1
    [[ "$-" != *f* && -z "${GLOBIGNORE-}" ]] || return 1
    if shopt -q failglob; then return 1; fi
    local holder_dir="${BUILD_HOLD_DIR-$HOME/.local/state/build-hold}" entry
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
# to natedev's hold. A holder directory that does not exist holds nothing:
# build_hold.read_holders gives the same answer, with no Python start.
build_hold_applies() {
    if [[ -n "${BUILDLOG_CALL_ID:-}" && "${BUILDLOG_CALLER:-}" == verify ]]; then return 0; fi
    [[ "${BUILDLOG_CALLER:-}" != verify-mac && "${OSTYPE:-}" != darwin* ]] || return 1
    local holder_dir="${BUILD_HOLD_DIR-$HOME/.local/state/build-hold}"
    [[ -z "$holder_dir" || -e "$holder_dir" || -L "$holder_dir" ]]
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

build_hold_wait_local() {
    build_hold_applies || return 0
    if build_hold_nothing_held; then return 0; fi
    build_hold_call
    local waiter_pid status=0 arguments=(wait-local --call-id "$BUILDLOG_HOLD_CALL_ID" --pid "${BUILDLOG_HOLD_PID:-$$}")
    if [[ -n "$BUILDLOG_HOLD_CALLER" ]]; then arguments+=(--caller "$BUILDLOG_HOLD_CALLER"); fi
    # A held natedev cannot start the step, so the leave hook also runs here:
    # status 75 (LINT_LEFT_QUEUE_STATUS) means another machine is free.
    if [[ -n "${LINT_QUEUE_LEAVE_HOOK:-}" ]]; then arguments+=(--leave-hook "$LINT_QUEUE_LEAVE_HOOK"); fi
    python3 "$BUILD_TARGETS_SCRIPT" "${arguments[@]}" &
    waiter_pid=$!
    wait "$waiter_pid" || status=$?
    return "$status"
}

build_hold_check_local() {
    build_hold_applies || return 0
    if build_hold_nothing_held; then return 0; fi
    build_hold_call
    python3 "$BUILD_TARGETS_SCRIPT" check-local --caller "${BUILDLOG_HOLD_CALLER:-verify}"
}

buildlog_memory_line() {
    local kind=$1 available=$2 threshold=$3 need=$4 source=$5 count=$6 promised=$7 running=$8 reserve=$9 summary=${10}
    local description
    case "$source" in
        measured) description="the 90th percentile of $count measured runs" ;;
        buildlog) description="0.65 of the 90th percentile peak of $count runs" ;;
        pooled) description="the 90th percentile of $count measured runs of this step in every repo" ;;
        *) description="no history, so the fallback" ;;
    esac
    if [[ "$kind" == first ]]; then
        awk -v a="$available" -v t="$threshold" -v n="$need" -v p="$promised" -v m="$running" -v r="$reserve" \
            -v since="$(TZ="$(buildlog_memory_python "$USER_ZONE_SCRIPT" 2>/dev/null)" date '+%H:%M %Z')" -v repo="${11}" -v step="${12}" -v desc="$description" -v summary="$summary" \
            'BEGIN { subject=(repo == "" ? step : repo " " step); printf "waiting for memory since %s: the machine has %.1f GiB free; a build starts at %.1f (%s needs %.1f GiB, %s", since,a/1073741824,t/1073741824,subject,n/1073741824,desc > "/dev/stderr"; if (m>0) printf "; %.1f GiB still promised to %d running steps: %s",p/1073741824,m,summary > "/dev/stderr"; if (r>0) printf "; %.1f GiB kept for earlyoom",r/1073741824 > "/dev/stderr"; print ")" > "/dev/stderr" }'
    elif [[ "$kind" == periodic ]]; then
        awk -v a="$available" -v t="$threshold" -v m="$(( BUILDLOG_MEM_WAIT_S / 60 ))" \
            'BEGIN { printf "still waiting for memory (%d min): the machine has %.1f GiB free; a build starts at %.1f\n",m,a/1073741824,t/1073741824 > "/dev/stderr" }'
    elif [[ "$kind" == left ]]; then
        printf 'another machine is free after %d min %d s: leaving the memory wait\n' "$(( BUILDLOG_MEM_WAIT_S / 60 ))" "$(( BUILDLOG_MEM_WAIT_S % 60 ))" >&2
    else
        printf 'memory free after %d min %d s: starting\n' "$(( BUILDLOG_MEM_WAIT_S / 60 ))" "$(( BUILDLOG_MEM_WAIT_S % 60 ))" >&2
    fi
}

buildlog_wait_for_memory() {
    BUILDLOG_MEM_WAIT_S=0 BUILDLOG_MEM_OUTCOME=MeminfoUnavailable BUILDLOG_MEM_RESERVATION=""
    local meminfo="${BUILDLOG_MEMINFO:-/proc/meminfo}" line available_kb available result error_line
    local started=0 announced=0 next_notice=60 interval="${BUILDLOG_MEM_POLL_S:-5}" limit="${BUILDLOG_MEM_WAIT_LIMIT_S:-900}"
    local state threshold need source count promised running reserve summary reservation repo step fresh reason worktree noticed=0 caller_pid=$BASHPID
    local cached_repo="" cached_step="" cached_worktree="" cached_need=- cached_source="" cached_count=0
    [[ "$interval" =~ ^[0-9]+$ && "$interval" -gt 0 ]] || interval=5
    [[ "$limit" =~ ^[0-9]+$ && "$limit" -ge 0 ]] || limit=900
    BUILDLOG_MEM_WAIT_FROM="${BUILDLOG_MEM_WAIT_SINCE:-${EPOCHSECONDS:-$(date +%s)}}"
    while [[ -r "$meminfo" ]]; do
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
            BUILDLOG_MEM_WAIT_S=$(( SECONDS - started ))
            if (( $# > 0 && BUILDLOG_MEM_WAIT_S >= limit && noticed == 0 )); then
                noticed=1
                printf 'still waiting for memory after %d min: this step is now first in line, and no build starts without memory\n' "$(( BUILDLOG_MEM_WAIT_S / 60 ))" >&2
            fi
        fi
        if ! result="$(buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" check "$meminfo" "$caller_pid" "${BUILDLOG_PEAK:+$BUILDLOG_PEAK.cgroup}" "$BUILDLOG_MEM_WAIT_FROM" "$cached_repo" "$cached_step" "$cached_worktree" "$cached_need" "$cached_source" "$cached_count" "$@" 2>&1)"; then
            error_line=${result##*$'\n'}
            printf 'memory gate failed (%s); starting anyway\n' "$error_line" >&2
            break
        fi
        IFS='|' read -r state threshold need source count promised running reserve summary reservation repo step fresh reason worktree <<< "$result"
        available=$fresh
        cached_repo=$repo cached_step=$step cached_worktree=$worktree cached_need=$need cached_source=$source cached_count=$count
        if [[ "$state" == admit ]]; then
            BUILDLOG_MEM_RESERVATION="$reservation"
            if [[ "$reason" == untracked ]]; then
                echo 'memory gate could not track this step; starting without a reservation' >&2
            fi
            BUILDLOG_MEM_OUTCOME=Granted
            if (( announced != 0 )); then
                buildlog_memory_line done "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            fi
            break
        fi
        # An app launch (no argv) has no reservation and its hook a 960 s timeout: it alone keeps the limit.
        if (( $# == 0 && announced != 0 && BUILDLOG_MEM_WAIT_S >= limit )) || (( $# == 0 && limit == 0 )); then
            echo 'memory wait limit reached after 15 min; starting the launch anyway' >&2
            BUILDLOG_MEM_OUTCOME=TimedOut
            break
        fi
        if (( announced == 0 )); then
            started=$SECONDS
            announced=1
            buildlog_memory_line first "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary" "$repo" "$step"
        elif [[ -n "${LINT_QUEUE_LEAVE_HOOK:-}" ]] \
              && /bin/sh -c "$LINT_QUEUE_LEAVE_HOOK" </dev/null >/dev/null 2>&1; then
            BUILDLOG_MEM_OUTCOME=LeftQueue
            buildlog_memory_line left "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            break
        elif (( BUILDLOG_MEM_WAIT_S >= next_notice )); then
            buildlog_memory_line periodic "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            next_notice=$(( next_notice + 60 ))
        fi
        sleep "$interval"
    done
    if (( announced != 0 )); then BUILDLOG_MEM_WAIT_S=$(( SECONDS - started )); fi
}

buildlog_release_memory() {
    local reservation="${BUILDLOG_MEM_RESERVATION:-}"
    BUILDLOG_MEM_RESERVATION=""
    [[ -n "$reservation" ]] || return 0
    buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" release "$reservation" "${1:-0}" >/dev/null 2>&1 || true
}

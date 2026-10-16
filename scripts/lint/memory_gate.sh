#!/usr/bin/env bash
# Admit when MemAvailable covers this step's expected anon peak, unfinished
# growth promised to running builds, and earlyoom's 5% reserve. Alone, the
# old 12 GiB threshold caps the wait. The first check resolves git and history;
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
BUILD_HOLD_MARK_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_hold" && pwd)/build_hold.py"
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

build_hold_mark() {
    local holder_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}" holder=""
    for holder in "$holder_dir"/* "$holder_dir"/.[!.]* "$holder_dir"/..?*; do
        [[ -f "$holder" ]] && break
    done
    [[ -f "$holder" ]] || return 0
    local arguments=(mark --state "$1") cycle_id="" generation=""
    if [[ -n "${2:-}" ]]; then arguments+=(--outcome "$2"); fi
    if [[ -n "${BUILDLOG_CALL_ID:-}" && "${BUILDLOG_CALLER:-}" == verify ]]; then
        arguments+=(--call-id "$BUILDLOG_CALL_ID")
        if [[ -n "${BUILDLOG_HOLD_ADMISSION_FILE:-}" && -r "$BUILDLOG_HOLD_ADMISSION_FILE" ]]; then
            IFS=' ' read -r cycle_id generation < "$BUILDLOG_HOLD_ADMISSION_FILE" || true
            [[ -z "$cycle_id" ]] || arguments+=(--cycle-id "$cycle_id")
            [[ -z "$generation" ]] || arguments+=(--generation "$generation")
        fi
    fi
    python3 "$BUILD_HOLD_MARK_SCRIPT" "${arguments[@]}" >/dev/null 2>&1 || true
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

build_hold_wait_local() {
    [[ -n "${BUILDLOG_CALL_ID:-}" && "${BUILDLOG_CALLER:-}" == verify ]] || return 0
    if build_hold_nothing_held; then return 0; fi
    local waiter_pid status=0 arguments=(wait-local --call-id "$BUILDLOG_CALL_ID" --pid "${BUILDLOG_HOLD_PID:-$$}")
    # A held natedev cannot start the step, so the leave hook also runs here:
    # status 75 (LINT_LEFT_QUEUE_STATUS) means another machine is free.
    if [[ -n "${LINT_QUEUE_LEAVE_HOOK:-}" ]]; then arguments+=(--leave-hook "$LINT_QUEUE_LEAVE_HOOK"); fi
    python3 "$BUILD_TARGETS_SCRIPT" "${arguments[@]}" &
    waiter_pid=$!
    wait "$waiter_pid" || status=$?
    return "$status"
}

build_hold_check_admission() {
    [[ -n "${BUILDLOG_CALL_ID:-}" && "${BUILDLOG_CALLER:-}" == verify ]] || return 0
    if build_hold_nothing_held; then return 0; fi
    python3 "$BUILD_TARGETS_SCRIPT" check-admission --call-id "$BUILDLOG_CALL_ID"
}

build_hold_step_started() {
    [[ -n "${BUILDLOG_HOLD_STARTED_FILE:-}" ]] || return 0
    : > "$BUILDLOG_HOLD_STARTED_FILE"
}

buildlog_memory_line() {
    local kind=$1 available=$2 threshold=$3 need=$4 source=$5 count=$6 promised=$7 running=$8 reserve=$9 summary=${10}
    local description
    case "$source" in
        measured) description="the 90th percentile of $count measured runs" ;;
        buildlog) description="0.65 of the 90th percentile peak of $count runs" ;;
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
    local state threshold need source count promised running reserve summary reservation repo step fresh reason worktree forced=0 caller_pid=$BASHPID
    local cached_repo="" cached_step="" cached_worktree="" cached_need=- cached_source="" cached_count=0
    [[ "$interval" =~ ^[0-9]+$ && "$interval" -gt 0 ]] || interval=5
    [[ "$limit" =~ ^[0-9]+$ && "$limit" -ge 0 ]] || limit=900
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
            if (( BUILDLOG_MEM_WAIT_S >= limit )); then forced=1; fi
        fi
        if ! result="$(buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" check "$meminfo" "$caller_pid" "${BUILDLOG_PEAK:+$BUILDLOG_PEAK.cgroup}" "$forced" "$cached_repo" "$cached_step" "$cached_worktree" "$cached_need" "$cached_source" "$cached_count" "$@" 2>&1)"; then
            error_line=${result##*$'\n'}
            printf 'memory gate failed (%s); starting anyway\n' "$error_line" >&2
            break
        fi
        IFS='|' read -r state threshold need source count promised running reserve summary reservation repo step fresh reason worktree <<< "$result"
        available=$fresh
        cached_repo=$repo cached_step=$step cached_worktree=$worktree cached_need=$need cached_source=$source cached_count=$count
        if [[ "$state" == admit || "$forced" == 1 ]]; then
            BUILDLOG_MEM_RESERVATION="$reservation"
            if [[ "$reason" == untracked ]]; then
                echo 'memory gate could not track this step; starting without a reservation' >&2
            fi
            if [[ "$state" == admit ]]; then
                BUILDLOG_MEM_OUTCOME=Granted
                if (( announced != 0 )); then
                    buildlog_memory_line done "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
                fi
            else
                echo 'memory wait limit reached after 15 min; starting anyway' >&2
                BUILDLOG_MEM_OUTCOME=TimedOut
            fi
            break
        fi
        if (( announced == 0 )); then
            started=$SECONDS
            announced=1
            buildlog_memory_line first "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary" "$repo" "$step"
            build_hold_mark WaitingForMemory
        elif [[ -n "${LINT_QUEUE_LEAVE_HOOK:-}" ]] \
              && /bin/sh -c "$LINT_QUEUE_LEAVE_HOOK" </dev/null >/dev/null 2>&1; then
            BUILDLOG_MEM_OUTCOME=LeftQueue
            buildlog_memory_line left "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            break
        elif (( BUILDLOG_MEM_WAIT_S >= next_notice )); then
            buildlog_memory_line periodic "$available" "$threshold" "$need" "$source" "$count" "$promised" "$running" "$reserve" "$summary"
            next_notice=$(( next_notice + 60 ))
        fi
        if (( limit == 0 )); then
            forced=1
        else
            sleep "$interval"
        fi
    done
    if (( announced != 0 )); then BUILDLOG_MEM_WAIT_S=$(( SECONDS - started )); fi
}

buildlog_release_memory() {
    local reservation="${BUILDLOG_MEM_RESERVATION:-}"
    BUILDLOG_MEM_RESERVATION=""
    [[ -n "$reservation" ]] || return 0
    buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" release "$reservation" "${1:-0}" >/dev/null 2>&1 || true
}

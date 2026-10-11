#!/usr/bin/env bash
# Admit when MemAvailable covers this step's expected anon peak, unfinished
# growth promised to running builds, and earlyoom's 5% reserve. Alone, the
# old 12 GiB threshold caps the wait. The first check resolves git and history;
# held polls read only meminfo and the ledger under its lock. Release starts
# Python once more. With hana nextest's 5,397 index rows, measured first check
# 431 ms, cached poll 102 ms, and release 97 ms (temporary ledger and real
# read-only index, 2026-10-06 PDT).

BUILD_HOLD_MARK_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_hold" && pwd)/build_hold.py"
BUILDLOG_MEM_ADMIT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/memory_admit.py"

buildlog_memory_python() {
    if [[ -x "$HOME/.claude/scripts/lib/py" ]]; then
        "$HOME/.claude/scripts/lib/py" "$@"
    else
        python3 "$@"
    fi
}

build_hold_mark() {
    [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]] || return 0
    local holder_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}" holder
    for holder in "$holder_dir"/* "$holder_dir"/.[!.]* "$holder_dir"/..?*; do
        [[ -f "$holder" ]] && break
    done
    [[ -f "$holder" ]] || return 0
    if [[ -n "${2:-}" ]]; then
        python3 "$BUILD_HOLD_MARK_SCRIPT" mark --session-id "$CLAUDE_CODE_SESSION_ID" --state "$1" --outcome "$2" >/dev/null 2>&1 || true
    else
        python3 "$BUILD_HOLD_MARK_SCRIPT" mark --session-id "$CLAUDE_CODE_SESSION_ID" --state "$1" >/dev/null 2>&1 || true
    fi
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
            -v since="$(TZ=America/Los_Angeles date '+%H:%M %Z')" -v repo="${11}" -v step="${12}" -v desc="$description" -v summary="$summary" \
            'BEGIN { subject=(repo == "" ? step : repo " " step); printf "waiting for memory since %s: the machine has %.1f GiB free; a build starts at %.1f (%s needs %.1f GiB, %s", since,a/1073741824,t/1073741824,subject,n/1073741824,desc > "/dev/stderr"; if (m>0) printf "; %.1f GiB still promised to %d running steps: %s",p/1073741824,m,summary > "/dev/stderr"; if (r>0) printf "; %.1f GiB kept for earlyoom",r/1073741824 > "/dev/stderr"; print ")" > "/dev/stderr" }'
    elif [[ "$kind" == periodic ]]; then
        awk -v a="$available" -v t="$threshold" -v m="$(( BUILDLOG_MEM_WAIT_S / 60 ))" \
            'BEGIN { printf "still waiting for memory (%d min): the machine has %.1f GiB free; a build starts at %.1f\n",m,a/1073741824,t/1073741824 > "/dev/stderr" }'
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

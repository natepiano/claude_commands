#!/bin/bash

set -uo pipefail

SNAPSHOT_TOOL="/Users/natemccoy/.claude/scripts/prioritize/snapshot.py"
RENUMBER_TOOL="/Users/natemccoy/.claude/scripts/prioritize/renumber.py"
WRITER_LOCK_TOOL="/Users/natemccoy/.claude/scripts/prioritize/writer_lock.py"
RUNNER_LOCK_TOOL="/Users/natemccoy/.claude/scripts/prioritize/runner_lock.py"
SIGNATURE_TOOL="/Users/natemccoy/.claude/scripts/prioritize/watch_signature.py"
ISSUES_DIR="/Users/natemccoy/rust/hanadocs/issues"
GOALS_FILE="/Users/natemccoy/rust/hanadocs/prioritization goals.md"
CACHE_DIR="/Users/natemccoy/Library/Caches/hanadocs-prioritize"
SUCCESS_SNAPSHOT="$CACHE_DIR/semantic-inputs.json"
STATE_DIR="/tmp/hanadocs-prioritize"
RUNNER_LOCK_FILE="$STATE_DIR/runner.lock"
PENDING_FILE="$STATE_DIR/pending"
EVENT_LOG="$STATE_DIR/events.log"
LAST_STATUS_FILE="$STATE_DIR/last-status"
DEBOUNCE_SECONDS="0.25"
RUNNER_BUSY_EXIT=75
POLL_SECONDS="0.5"
ERROR_RETRY_SECONDS="5"
CONCURRENT_RETRY_SECONDS="0.25"
CONCURRENT_CHANGE_EXIT=3

CANDIDATE_FILE=""
POST_RUN_FILE=""

umask 077
if ! /bin/mkdir -p "$STATE_DIR" "$CACHE_DIR"; then
    /usr/bin/printf 'hanadocs prioritize watcher: could not create runtime directories\n' >&2
    exit 2
fi

timestamp() {
    /bin/date '+%Y-%m-%dT%H:%M:%S%z'
}

log() {
    /usr/bin/printf '[%s] pid=%s %s\n' "$(timestamp)" "$$" "$*" >> "$EVENT_LOG"
}

write_status() {
    local state="$1"
    local result="$2"
    local detail="$3"
    local temporary

    temporary="$(/usr/bin/mktemp "$STATE_DIR/.last-status.XXXXXX")" || return 1
    /usr/bin/printf '%s %s result=%s detail=%s\n' \
        "$state" "$(timestamp)" "$result" "$detail" > "$temporary"
    /bin/mv -f "$temporary" "$LAST_STATUS_FILE"
}

mark_pending() {
    /usr/bin/touch "$PENDING_FILE"
}

discard_temporary_snapshots() {
    [[ -n "$CANDIDATE_FILE" ]] && /bin/rm -f "$CANDIDATE_FILE"
    [[ -n "$POST_RUN_FILE" ]] && /bin/rm -f "$POST_RUN_FILE"
    CANDIDATE_FILE=""
    POST_RUN_FILE=""
}

settled_state_is_current() {
    local check_status
    local settle_snapshot
    local snapshot_status

    if [[ ! -f "$SUCCESS_SNAPSHOT" ]]; then
        return 1
    fi
    settle_snapshot="$(/usr/bin/mktemp "$CACHE_DIR/.semantic-inputs.settle.XXXXXX")" || {
        log "error: could not create settle snapshot"
        return 2
    }

    /usr/bin/python3 "$SNAPSHOT_TOOL" --output "$settle_snapshot" >> "$EVENT_LOG" 2>&1
    snapshot_status=$?
    if (( snapshot_status != 0 )); then
        /bin/rm -f "$settle_snapshot"
        log "error: settle semantic snapshot failed exit=$snapshot_status"
        return 2
    fi
    if ! /usr/bin/cmp -s "$settle_snapshot" "$SUCCESS_SNAPSHOT"; then
        /bin/rm -f "$settle_snapshot"
        return 1
    fi
    /bin/rm -f "$settle_snapshot"

    /usr/bin/python3 "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
    check_status=$?
    if (( check_status == 0 )); then
        return 0
    fi
    if (( check_status == 1 )); then
        return 1
    fi
    log "error: settle rank check failed exit=$check_status"
    return 2
}

cleanup() {
    discard_temporary_snapshots
}

trap cleanup EXIT

debounce_events() {
    while true; do
        /bin/rm -f "$PENDING_FILE"
        /bin/sleep "$DEBOUNCE_SECONDS"
        if [[ ! -e "$PENDING_FILE" ]]; then
            return
        fi
        log "coalescing another filesystem event during debounce"
    done
}

require_runtime() {
    if [[ ! -d "$ISSUES_DIR" ]]; then
        log "error: issues directory missing: $ISSUES_DIR"
        write_status "error" "preflight" "issues-directory-missing" || true
        return 1
    fi
    if [[ ! -f "$GOALS_FILE" ]]; then
        log "error: goals file missing: $GOALS_FILE"
        write_status "error" "preflight" "goals-file-missing" || true
        return 1
    fi
    if [[ ! -f "$SNAPSHOT_TOOL" ]]; then
        log "error: snapshot tool missing: $SNAPSHOT_TOOL"
        write_status "error" "preflight" "snapshot-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$RENUMBER_TOOL" ]]; then
        log "error: renumber tool missing: $RENUMBER_TOOL"
        write_status "error" "preflight" "renumber-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$WRITER_LOCK_TOOL" ]]; then
        log "error: writer lock tool missing: $WRITER_LOCK_TOOL"
        write_status "error" "preflight" "writer-lock-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$RUNNER_LOCK_TOOL" ]]; then
        log "error: runner lock tool missing: $RUNNER_LOCK_TOOL"
        write_status "error" "preflight" "runner-lock-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$SIGNATURE_TOOL" ]]; then
        log "error: watch signature tool missing: $SIGNATURE_TOOL"
        write_status "error" "preflight" "signature-tool-missing" || true
        return 1
    fi
    return 0
}

run_once() {
    local apply_status
    local check_status

    discard_temporary_snapshots
    CANDIDATE_FILE="$(/usr/bin/mktemp "$CACHE_DIR/.semantic-inputs.candidate.XXXXXX")" || {
        log "error: could not create candidate snapshot"
        write_status "error" "snapshot" "candidate-create-failed" || true
        return 2
    }

    /usr/bin/python3 "$SNAPSHOT_TOOL" --output "$CANDIDATE_FILE" >> "$EVENT_LOG" 2>&1
    apply_status=$?
    if (( apply_status != 0 )); then
        log "error: semantic snapshot failed exit=$apply_status"
        write_status "error" "snapshot" "exit-$apply_status" || true
        return "$apply_status"
    fi

    if [[ -f "$SUCCESS_SNAPSHOT" ]] && /usr/bin/cmp -s "$CANDIDATE_FILE" "$SUCCESS_SNAPSHOT"; then
        /bin/rm -f "$CANDIDATE_FILE"
        CANDIDATE_FILE=""
        /usr/bin/python3 "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
        check_status=$?
        if (( check_status == 0 )); then
            log "semantic inputs and generated ranking state unchanged"
            write_status "ok" "no-op" "semantic-inputs-and-ranking-unchanged" || true
            return 0
        fi
        if (( check_status != 1 )); then
            log "error: unchanged-input rank check failed exit=$check_status"
            write_status "error" "rank-check" "exit-$check_status" || true
            return "$check_status"
        fi

        log "semantic inputs unchanged but generated ranking drifted; repairing"
        /usr/bin/python3 "$RENUMBER_TOOL" --apply >> "$EVENT_LOG" 2>&1
        apply_status=$?
        if (( apply_status != 0 )); then
            if (( apply_status == CONCURRENT_CHANGE_EXIT )); then
                log "ranking files changed during generated-state repair; retry required"
                write_status "pending" "rerun" "concurrent-change-during-repair" || true
                return "$apply_status"
            fi
            log "error: generated ranking repair failed exit=$apply_status"
            write_status "error" "rank-repair" "exit-$apply_status" || true
            return "$apply_status"
        fi
        /usr/bin/python3 "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
        check_status=$?
        if (( check_status != 0 )); then
            log "error: generated ranking repair validation failed exit=$check_status"
            write_status "error" "rank-repair-check" "exit-$check_status" || true
            return "$check_status"
        fi
        log "repaired generated ranking state without semantic input changes"
        write_status "ok" "repaired" "score-and-rank-canonical" || true
        return 0
    fi

    log "semantic ranking inputs changed; applying score and rank update"
    /usr/bin/python3 "$RENUMBER_TOOL" --apply >> "$EVENT_LOG" 2>&1
    apply_status=$?
    if (( apply_status != 0 )); then
        if (( apply_status == CONCURRENT_CHANGE_EXIT )); then
            log "ranking files changed during apply; successful snapshot unchanged"
            write_status "pending" "rerun" "concurrent-change-during-apply" || true
            return "$apply_status"
        fi
        log "error: renumber apply failed exit=$apply_status; successful snapshot unchanged"
        write_status "error" "renumber-apply" "exit-$apply_status" || true
        return "$apply_status"
    fi

    /usr/bin/python3 "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
    check_status=$?
    if (( check_status != 0 )); then
        log "error: post-apply validation failed exit=$check_status; successful snapshot unchanged"
        write_status "error" "post-apply-check" "exit-$check_status" || true
        return "$check_status"
    fi

    POST_RUN_FILE="$(/usr/bin/mktemp "$CACHE_DIR/.semantic-inputs.post-run.XXXXXX")" || {
        log "error: could not create post-run snapshot"
        write_status "error" "snapshot" "post-run-create-failed" || true
        return 2
    }
    /usr/bin/python3 "$SNAPSHOT_TOOL" --output "$POST_RUN_FILE" >> "$EVENT_LOG" 2>&1
    apply_status=$?
    if (( apply_status != 0 )); then
        log "error: post-run semantic snapshot failed exit=$apply_status"
        write_status "error" "snapshot" "post-run-exit-$apply_status" || true
        return "$apply_status"
    fi

    if ! /usr/bin/cmp -s "$CANDIDATE_FILE" "$POST_RUN_FILE"; then
        log "ranking inputs changed during renumber; scheduling one fresh pass"
        write_status "pending" "rerun" "inputs-changed-during-pass" || true
        mark_pending
        return 0
    fi

    /bin/rm -f "$POST_RUN_FILE"
    POST_RUN_FILE=""
    if ! /bin/mv -f "$CANDIDATE_FILE" "$SUCCESS_SNAPSHOT"; then
        log "error: could not commit successful semantic snapshot"
        write_status "error" "snapshot-commit" "atomic-move-failed" || true
        return 2
    fi
    CANDIDATE_FILE=""

    log "renumber completed, validated, and committed semantic snapshot"
    write_status "ok" "updated" "score-and-rank-canonical" || true
    return 0
}

run_daemon() {
    local baseline=""
    local observed
    local after
    local confirmed
    local signature_status
    local runner_status
    local settle_status

    if ! require_runtime; then
        return 2
    fi
    log "persistent signature watcher started"
    while true; do
        observed="$(/usr/bin/python3 "$SIGNATURE_TOOL" 2>> "$EVENT_LOG")"
        signature_status=$?
        if (( signature_status != 0 )); then
            log "error: watch signature failed exit=$signature_status; retrying"
            write_status "error" "watch-signature" "exit-$signature_status" || true
            baseline=""
            /bin/sleep "$ERROR_RETRY_SECONDS"
            continue
        fi
        if [[ -n "$baseline" ]] && [[ "$observed" == "$baseline" ]]; then
            /bin/sleep "$POLL_SECONDS"
            continue
        fi

        /bin/bash "$0"
        runner_status=$?
        if (( runner_status != 0 )); then
            if (( runner_status == CONCURRENT_CHANGE_EXIT )); then
                log "files changed during ranking; coalescing and retrying"
                baseline=""
                /bin/sleep "$CONCURRENT_RETRY_SECONDS"
                continue
            fi
            log "error: detected change was not ranked exit=$runner_status; retrying"
            baseline=""
            /bin/sleep "$ERROR_RETRY_SECONDS"
            continue
        fi

        after="$(/usr/bin/python3 "$SIGNATURE_TOOL" 2>> "$EVENT_LOG")"
        signature_status=$?
        if (( signature_status != 0 )); then
            log "error: post-run watch signature failed exit=$signature_status; retrying"
            baseline=""
            /bin/sleep "$ERROR_RETRY_SECONDS"
            continue
        fi
        if [[ "$after" != "$observed" ]]; then
            settled_state_is_current
            settle_status=$?
            confirmed="$(/usr/bin/python3 "$SIGNATURE_TOOL" 2>> "$EVENT_LOG")"
            signature_status=$?
            if (( signature_status != 0 )); then
                log "error: settle watch signature failed exit=$signature_status; retrying"
                write_status "error" "watch-signature" "settle-exit-$signature_status" || true
                baseline=""
                /bin/sleep "$ERROR_RETRY_SECONDS"
                continue
            fi
            if (( settle_status == 0 )) && [[ "$confirmed" == "$after" ]]; then
                log "ranking writes changed file signatures; semantic inputs and ranks remain canonical"
                baseline="$confirmed"
                /bin/sleep "$POLL_SECONDS"
                continue
            fi
            log "watched files changed during ranking or settle verification; starting one fresh pass"
            baseline=""
            continue
        fi
        baseline="$after"
        /bin/sleep "$POLL_SECONDS"
    done
}

if [[ "${1:-}" == "--daemon" ]]; then
    if (( $# != 1 )); then
        log "error: invalid daemon watcher invocation"
        exit 2
    fi
    run_daemon
    exit $?
fi

if [[ "${1:-}" != "--locked" ]]; then
    if (( $# != 0 )); then
        log "error: unsupported watcher arguments: $*"
        exit 2
    fi
    if [[ ! -f "$RUNNER_LOCK_TOOL" ]]; then
        log "error: runner lock tool missing: $RUNNER_LOCK_TOOL"
        write_status "error" "preflight" "runner-lock-tool-missing" || true
        exit 2
    fi

    while true; do
        mark_pending
        /usr/bin/python3 "$RUNNER_LOCK_TOOL" run "$RUNNER_LOCK_FILE" \
            /bin/bash "$0" --locked
        runner_status=$?
        if (( runner_status == RUNNER_BUSY_EXIT )); then
            log "watcher already running; marked one pending rerun"
            exit "$RUNNER_BUSY_EXIT"
        fi
        if [[ -e "$PENDING_FILE" ]]; then
            log "filesystem event arrived during runner lock handoff"
            continue
        fi
        exit "$runner_status"
    done
fi

if (( $# != 1 )); then
    log "error: invalid locked watcher invocation"
    exit 2
fi

if ! require_runtime; then
    exit 2
fi

last_status=0
while true; do
    debounce_events
    run_once
    last_status=$?

    if [[ -e "$PENDING_FILE" ]]; then
        log "pending filesystem event detected; starting one coalesced rerun"
        continue
    fi

    exit "$last_status"
done

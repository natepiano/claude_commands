#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.11.
PY="$SCRIPT_DIR/../lib/py"
SOURCE_DIR="$HOME/.claude/commands"
LOG_DIR="/tmp/claude-to-codex-sync"
LOCK_FILE="$LOG_DIR/runner.lock"
PENDING_FILE="$LOG_DIR/pending"
EVENT_LOG="$LOG_DIR/events.log"
LAST_STATUS_FILE="$LOG_DIR/last-status"
SYNC_SCRIPT="$SCRIPT_DIR/sync.py"

mkdir -p "$LOG_DIR"

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

epoch_ms() {
    "$PY" -c 'import time; print(int(time.time() * 1000))'
}

log() {
    printf '[%s] %s\n' "$(timestamp)" "$*" >> "$EVENT_LOG"
}

if [[ ! -d "$SOURCE_DIR" ]]; then
    log "source directory missing: $SOURCE_DIR"
    exit 1
fi

if [[ ! -f "$SYNC_SCRIPT" ]]; then
    log "sync script missing: $SYNC_SCRIPT"
    exit 1
fi

if [[ "${1:-}" != --locked ]]; then
    status=0
    "$PY" "$SCRIPT_DIR/../prioritize/runner_lock.py" run "$LOCK_FILE" bash "$0" --locked || status=$?
    if (( status == 75 )); then
        touch "$PENDING_FILE"
        log "sync already running; marked pending"
        exit 0
    fi
    exit "$status"
fi

log "starting sync run"

# Coalesce rapid save bursts into one or two actual regenerations.
sleep 1

while true; do
    rm -f "$PENDING_FILE"
    run_started_epoch_ms="$(epoch_ms)"

    if "$PY" "$SYNC_SCRIPT" --force >> "$EVENT_LOG" 2>&1; then
        run_finished_epoch_ms="$(epoch_ms)"
        duration_ms=$(( run_finished_epoch_ms - run_started_epoch_ms ))
        printf 'ok %s\n' "$(timestamp)" > "$LAST_STATUS_FILE"
        log "sync completed successfully duration_ms=$duration_ms"
    else
        status=$?
        run_finished_epoch_ms="$(epoch_ms)"
        duration_ms=$(( run_finished_epoch_ms - run_started_epoch_ms ))
        printf 'error %s exit=%s\n' "$(timestamp)" "$status" > "$LAST_STATUS_FILE"
        log "sync failed with exit code $status duration_ms=$duration_ms"
        exit "$status"
    fi

    if [[ -f "$PENDING_FILE" ]]; then
        log "pending change detected during sync; running again"
        sleep 1
        continue
    fi

    break
done

log "sync run finished"

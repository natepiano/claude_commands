#!/bin/bash
# Warm up Bevy apps by launching briefly then killing
# Reads from clean-fix.conf in the same directory
# Can be run standalone or called from clean-fix.sh
#
# Detection: polls the BRP endpoint until the app responds, then
# lets it run for warmup_run_seconds before sending brp_extras/shutdown.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
WARMUP_CONF="$SCRIPT_DIR/clean-fix.conf"
TIMESTAMP_DIR="$HOME/.local/state/clean-fix"
TARGET_PROJECT="${1:-}"
WARMUP_TIMEOUT=120
WARMUP_PORT=15799
WARMUP_RUN_SECONDS=1

source "$HOME/.cargo/env"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

brp_request() {
    local method="$1"
    curl -s -X POST "http://localhost:${WARMUP_PORT}/jsonrpc" \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"${method}\"}" \
        2>/dev/null
}

brp_is_alive() {
    local response
    response=$(brp_request "bevy/list" 2>/dev/null || true)
    [[ -n "$response" ]]
}

brp_shutdown() {
    brp_request "brp_extras/shutdown" > /dev/null 2>&1 || true
}

# Bevy apps die at window/Metal creation when the screen is locked, so warmup
# can only succeed with an unlocked GUI session. loginwindow maintains
# IOConsoleLocked at the IORegistry root; grep it rather than depend on pyobjc.
screen_locked() {
    ioreg -n Root -d1 2>/dev/null | grep -q '"IOConsoleLocked" = Yes'
}

warmup_run() {
    local name="$1"
    local cargo_args="$2"
    local project_name="$3"

    if screen_locked; then
        log "WARMUP SKIP: $name (screen locked)"
        return
    fi

    log "WARMUP: $name"
    BRP_EXTRAS_PORT=$WARMUP_PORT cargo run $cargo_args > /dev/null 2>&1 &
    local pid=$!

    # Poll BRP endpoint until the app is actually serving
    local countdown=$WARMUP_TIMEOUT
    local running=false
    while (( countdown-- > 0 )); do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        if brp_is_alive; then
            running=true
            break
        fi
        sleep 1
    done

    if $running; then
        log "WARMUP OK: $name (pid $pid, BRP responding on port $WARMUP_PORT)"
        sleep "$WARMUP_RUN_SECONDS"
        log "WARMUP KILLING: $name (pid $pid)"
        brp_shutdown
        # Give it a few seconds to shut down gracefully
        local shutdown_wait=2
        while (( shutdown_wait-- > 0 )); do
            if ! kill -0 "$pid" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        # Force kill if still alive
        if kill -0 "$pid" 2>/dev/null; then
            log "WARMUP FORCE KILL: $name (pid $pid)"
            kill "$pid" 2>/dev/null || true
        fi
    else
        log "WARMUP FAIL: $name (BRP never responded within ${WARMUP_TIMEOUT}s)"
        log "WARMUP KILLING: $name (pid $pid)"
        kill "$pid" 2>/dev/null || true
    fi

    wait "$pid" 2>/dev/null || true

    # Re-touch timestamp so warmup artifacts don't trigger a rebuild
    if [[ -n "$project_name" ]]; then
        mkdir -p "$TIMESTAMP_DIR"
        touch "$TIMESTAMP_DIR/$project_name"
    fi
}

if [[ ! -f "$WARMUP_CONF" ]]; then
    log "WARMUP SKIP: no config at $WARMUP_CONF"
    exit 0
fi

section=""
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"       # strip comments
    line="${line// /}"       # strip spaces
    [[ -z "$line" ]] && continue

    if [[ "$line" =~ ^\[(.+)\]$ ]]; then
        section="${BASH_REMATCH[1]}"
        continue
    fi

    key="${line%%=*}"
    value="${line#*=}"

    case "$section" in
        settings)
            [[ "$key" == "warmup_timeout" ]] && WARMUP_TIMEOUT="$value"
                [[ "$key" == "warmup_run_seconds" ]] && WARMUP_RUN_SECONDS="$value"
            ;;
        cargo_run)
            manifest="$RUST_DIR/$value"
            project_name="${value%%/*}"
            if [[ -n "$TARGET_PROJECT" && "$key" != "$TARGET_PROJECT" && "$project_name" != "$TARGET_PROJECT" ]]; then
                continue
            fi
            if [[ ! -f "$manifest" ]]; then
                log "WARMUP SKIP: $key (no $value)"
                continue
            fi
            warmup_run "$key" "--manifest-path $manifest" "$project_name"
            ;;
        examples)
            manifest="$RUST_DIR/${value%%:*}"
            example="${value#*:}"
            project_name="${value%%/*}"
            if [[ -n "$TARGET_PROJECT" && "$key" != "$TARGET_PROJECT" && "$project_name" != "$TARGET_PROJECT" ]]; then
                continue
            fi
            if [[ ! -f "$manifest" ]]; then
                log "WARMUP SKIP: $key (no ${value%%:*})"
                continue
            fi
            warmup_run "$key example=$example" "--example $example --manifest-path $manifest" "$project_name"
            ;;
    esac
done < "$WARMUP_CONF"

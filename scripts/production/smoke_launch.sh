#!/usr/bin/env bash
# Smoke launch for /showrunner:produce <PromoteMain/>: builds hana at a checkout
# that sits at the sha being promoted, starts it on a test port, waits until BRP
# answers, and shuts it down over BRP. Exits 0 only when every step passed.
#
# Usage: smoke_launch.sh <checkout> <sha> <log> [port]
#
# The app starts with XDG_CONFIG_HOME at an empty scratch directory, the way
# units launch Hana for screenshots, so it never reads or writes the user's
# live scene, keymap or window state. That also avoids the persisted-window-size
# OIT crash. Linux only: on macOS Hana's config path ignores XDG_CONFIG_HOME.
set -euo pipefail

CHECKOUT="${1:?Usage: smoke_launch.sh <checkout> <sha> <log> [port]}"
SHA="${2:?Usage: smoke_launch.sh <checkout> <sha> <log> [port]}"
LOG="${3:?Usage: smoke_launch.sh <checkout> <sha> <log> [port]}"
PORT="${4:-15790}"
READY_SECONDS=300
EXIT_SECONDS=30

: >"$LOG"
log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }
fail() { log "SMOKE FAILED: $*"; exit 1; }

[[ "$(uname -s)" == Linux ]] || fail "Linux only; Hana ignores XDG_CONFIG_HOME on $(uname -s)"
[[ "$PORT" != 15702 ]] || fail "15702 is the user's live Hana"

# The build must be of exactly this sha; checked again after the run.
check_head() {
    local head
    head="$(git -C "$CHECKOUT" rev-parse HEAD)"
    [[ "$head" == "$(git -C "$CHECKOUT" rev-parse "$SHA^{commit}")" ]] || fail "$CHECKOUT is at $head, not $SHA"
    [[ -z "$(git -C "$CHECKOUT" status --porcelain --untracked-files=no)" ]] || fail "$CHECKOUT has tracked changes"
}
check_head

brp() {
    curl --fail --silent --show-error --connect-timeout 1 --max-time 5 \
        --header 'Content-Type: application/json' \
        --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":{}}" \
        "http://127.0.0.1:${PORT}/jsonrpc"
}
brp rpc.discover >/dev/null 2>&1 && fail "port $PORT already answers BRP"

log "build: cargo build -p hana at $CHECKOUT ($SHA)"
direnv exec "$CHECKOUT" cargo build --manifest-path "$CHECKOUT/Cargo.toml" -p hana --bin hana >>"$LOG" 2>&1 \
    || fail "cargo build -p hana"
TARGET_DIR="$(direnv exec "$CHECKOUT" cargo metadata --no-deps --format-version 1 \
    --manifest-path "$CHECKOUT/Cargo.toml" 2>>"$LOG" | jq -r .target_directory)"
BIN="$TARGET_DIR/debug/hana"
[[ -x "$BIN" ]] || fail "no binary at $BIN"

SCRATCH="$(mktemp -d)"
mkdir "$SCRATCH/xdg"
HANA_PID=""
cleanup() {
    if [[ -n "$HANA_PID" ]] && kill -0 "$HANA_PID" 2>/dev/null; then
        kill -TERM -- "-$HANA_PID" 2>/dev/null || true
        sleep 2
        kill -KILL -- "-$HANA_PID" 2>/dev/null || true
    fi
    rm -rf "$SCRATCH"
}
trap cleanup EXIT

# setsid gives the app its own process group, so cleanup reaches every child.
# The binary runs directly, so BEVY_ASSET_ROOT replaces cargo's
# CARGO_MANIFEST_DIR for asset lookup.
log "launch: port $PORT, config $SCRATCH/xdg"
setsid direnv exec "$CHECKOUT" env XDG_CONFIG_HOME="$SCRATCH/xdg" BRP_EXTRAS_PORT="$PORT" \
    BEVY_ASSET_ROOT="$CHECKOUT/crates/hana" RUST_BACKTRACE=1 "$BIN" >>"$LOG" 2>&1 &
HANA_PID=$!

deadline=$((SECONDS + READY_SECONDS))
until brp rpc.discover >/dev/null 2>&1; do
    kill -0 "$HANA_PID" 2>/dev/null || fail "hana exited before BRP answered"
    ((SECONDS < deadline)) || fail "BRP did not answer within ${READY_SECONDS}s"
    sleep 1
done
log "ready: BRP answered on port $PORT"

brp brp_extras/shutdown >>"$LOG" 2>&1 || fail "brp_extras/shutdown request"
deadline=$((SECONDS + EXIT_SECONDS))
while kill -0 "$HANA_PID" 2>/dev/null; do
    ((SECONDS < deadline)) || fail "hana still running ${EXIT_SECONDS}s after shutdown"
    sleep 1
done
status=0
wait "$HANA_PID" || status=$?
HANA_PID=""
((status == 0)) || fail "hana exited with status $status after shutdown"
log "shutdown: hana exited 0"

check_head
log "SMOKE PASSED: $SHA"

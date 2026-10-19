#!/usr/bin/env bash
# heartbeat.sh — Append one liveness line (or a role header) to a heartbeat log.
#
# Beat usage:   heartbeat.sh <file> wrapper <message...>
#   <file>     heartbeat log path (parent directory is created if missing)
#   <message>  present-tense real words naming the activity,
#              e.g. "running clippy for verification"
#
# Header usage: heartbeat.sh <file> header <role> <description>
#   <role>         short role tag, e.g. "implementation (codex/gpt-6.1-sol:xhigh)"
#   <description>  1-2 lines describing this run's responsibility (may contain \n)
#
# Beat line format:   <ISO-8601 local time+offset> [wrapper] <message>
# Header block format:
#   ---- <ISO-8601 local time+offset> [<role>] ----
#   <description>
#
# Single-line appends under PIPE_BUF are atomic, so concurrent wrapper writers
# interleave safely without locking. Header blocks are two writes; launchers
# emit them once at start, before any concurrent writer.
#
# A beat that leaves the log over MAX_BYTES rewrites it to its opening header
# block plus the newest KEEP_BYTES of whole entries, through a temp file and mv
# so a reader never sees it half written. A beat another writer appends during
# the rewrite is lost; the next follows within a minute.

set -euo pipefail

MAX_BYTES=$((256 * 1024))
KEEP_BYTES=$((128 * 1024))
# The first line of a header block or a beat.
ENTRY='^(---- .* ----$|[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T)'

if [[ $# -lt 3 ]]; then
    echo "Usage: heartbeat.sh <file> wrapper <message...>  |  heartbeat.sh <file> header <role> <description>" >&2
    exit 2
fi

FILE="$1"
MODE="$2"
shift 2

mkdir -p "$(dirname "$FILE")"

# The newest entries start at the first header or beat line in the kept tail,
# which drops the partial line the byte cut lands in.
trim() {
    local tmp
    tmp="$(mktemp "${FILE}.XXXXXX")"
    if {
        awk -v entry="$ENTRY" 'NR == 1 && !/^---- / || NR > 1 && $0 ~ entry { exit } { print }' "$FILE"
        tail -c "$KEEP_BYTES" "$FILE" | awk -v entry="$ENTRY" 'NR > 1 && $0 ~ entry { kept = 1 } kept'
    } > "$tmp"; then
        mv -f "$tmp" "$FILE"
    else
        rm -f "$tmp"
    fi
}

case "$MODE" in
    header)
        if [[ $# -ne 2 ]]; then
            echo "heartbeat.sh: header mode needs exactly <role> <description>" >&2
            exit 2
        fi
        printf -- '---- %s [%s] ----\n%s\n' \
            "$(date +%Y-%m-%dT%H:%M:%S%z)" "$1" "$2" >> "$FILE"
        ;;
    wrapper)
        printf '%s [wrapper] %s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$*" >> "$FILE"
        if (( $(wc -c < "$FILE") > MAX_BYTES )); then
            trim
        fi
        ;;
    *)
        echo "heartbeat.sh: mode must be wrapper or header; got '$MODE'" >&2
        exit 2
        ;;
esac

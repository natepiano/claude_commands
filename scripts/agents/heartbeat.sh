#!/usr/bin/env bash
# heartbeat.sh — Append one liveness line to a heartbeat log.
#
# Usage: heartbeat.sh <file> <source> <message...>
#   <file>     heartbeat log path (parent directory is created if missing)
#   <source>   who is beating: "wrapper" (supervising script) or "agent" (the working agent)
#   <message>  present-tense real words naming the activity,
#              e.g. "running clippy for verification"
#
# Line format: <ISO-8601 UTC> <epoch> [<source>] <message>
#
# Single-line appends under PIPE_BUF are atomic, so concurrent wrapper and
# agent writers interleave safely without locking.

set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "Usage: heartbeat.sh <file> <source> <message...>" >&2
    exit 2
fi

FILE="$1"
SOURCE="$2"
shift 2
MESSAGE="$*"

case "$SOURCE" in
    wrapper|agent) ;;
    *)
        echo "heartbeat.sh: source must be wrapper or agent; got '$SOURCE'" >&2
        exit 2
        ;;
esac

mkdir -p "$(dirname "$FILE")"
printf '%s %s [%s] %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(date +%s)" "$SOURCE" "$MESSAGE" >> "$FILE"

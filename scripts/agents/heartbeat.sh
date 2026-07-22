#!/usr/bin/env bash
# heartbeat.sh — Append one liveness line (or a role header) to a heartbeat log.
#
# Beat usage:   heartbeat.sh <file> <source> <message...>
#   <file>     heartbeat log path (parent directory is created if missing)
#   <source>   who is beating: "wrapper" (supervising script) or "agent" (the working agent)
#   <message>  present-tense real words naming the activity,
#              e.g. "running clippy for verification"
#
# Header usage: heartbeat.sh <file> header <role> <description>
#   <role>         short role tag, e.g. "implementation (codex/gpt-5.6-sol:xhigh)"
#   <description>  1-2 lines describing this run's responsibility (may contain \n)
#
# Beat line format:   <ISO-8601 UTC> [<source>] <message>
# Header block format:
#   ---- <ISO-8601 UTC> [<role>] ----
#   <description>
#
# Single-line appends under PIPE_BUF are atomic, so concurrent wrapper and
# agent writers interleave safely without locking. Header blocks are two
# writes; launchers emit them once at start, before any concurrent writer.

set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "Usage: heartbeat.sh <file> <source> <message...>  |  heartbeat.sh <file> header <role> <description>" >&2
    exit 2
fi

FILE="$1"
SOURCE="$2"
shift 2

mkdir -p "$(dirname "$FILE")"

if [[ "$SOURCE" == "header" ]]; then
    if [[ $# -ne 2 ]]; then
        echo "heartbeat.sh: header mode needs exactly <role> <description>" >&2
        exit 2
    fi
    printf -- '---- %s [%s] ----\n%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" >> "$FILE"
    exit 0
fi

MESSAGE="$*"

case "$SOURCE" in
    wrapper|agent) ;;
    *)
        echo "heartbeat.sh: source must be wrapper, agent, or header; got '$SOURCE'" >&2
        exit 2
        ;;
esac

printf '%s [%s] %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SOURCE" "$MESSAGE" >> "$FILE"

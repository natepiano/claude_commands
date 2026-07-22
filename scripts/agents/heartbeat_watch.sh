#!/usr/bin/env bash
# heartbeat_watch.sh — Emit [wrapper] beats with a live activity digest while an
# agent process runs.
#
# Usage: heartbeat_watch.sh <heartbeat_file> <subtask> <agent_pid> <agent_log> [interval_secs]
#        heartbeat_watch.sh --digest <agent_log>     (print one digest and exit; for tests)
#
# Each beat tails <agent_log> and appends a short digest of the latest activity:
# claude stream-json events decode to "Tool: args" or assistant text; codex and
# plain-text logs contribute their last non-empty line. This narrates every
# dispatch — including read-only reviewers that cannot write [agent] lines —
# from the agent family CLI output itself. Exits when the agent pid dies.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

digest() {
    [[ -s "${AGENT_LOG}" ]] || return 0
    python3 - "${AGENT_LOG}" <<'PY'
import json
import re
import sys

log_path = sys.argv[1]
try:
    with open(log_path, "rb") as log:
        log.seek(0, 2)
        size = log.tell()
        log.seek(max(0, size - 16384))
        tail = log.read().decode("utf-8", errors="replace")
except OSError:
    sys.exit(0)

ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
lines = [ansi.sub("", line).strip() for line in tail.splitlines()]
lines = [line for line in lines if line]
if not lines:
    sys.exit(0)


def describe(event):
    kind = event.get("type")
    if kind == "assistant":
        blocks = event.get("message", {}).get("content", [])
        for block in reversed(blocks):
            if block.get("type") == "tool_use":
                name = block.get("name", "tool")
                params = block.get("input", {})
                hint = ""
                for key in ("command", "file_path", "pattern", "description", "prompt"):
                    value = params.get(key)
                    if value:
                        hint = " ".join(str(value).split())
                        break
                return name + ": " + hint if hint else name
            if block.get("type") == "text":
                text = " ".join(block.get("text", "").split())
                if text:
                    return text
        return None
    if kind == "result":
        return "finalizing result"
    return None


message = None
for line in reversed(lines):
    if line.startswith("{"):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        described = describe(event)
        if described:
            message = described
            break
    else:
        message = line
        break

if message:
    print(message[:160])
PY
}

if [[ "${1:-}" == "--digest" ]]; then
    AGENT_LOG="${2:?Usage: heartbeat_watch.sh --digest <agent_log>}"
    digest
    exit 0
fi

HEARTBEAT_FILE="${1:?Usage: heartbeat_watch.sh <heartbeat_file> <subtask> <agent_pid> <agent_log> [interval_secs]}"
SUBTASK="${2:?missing subtask}"
AGENT_PID="${3:?missing agent pid}"
AGENT_LOG="${4:?missing agent log path}"
INTERVAL_SECS="${5:-60}"

waited=0
while kill -0 "${AGENT_PID}" 2>/dev/null; do
    sleep "${INTERVAL_SECS}"
    kill -0 "${AGENT_PID}" 2>/dev/null || exit 0
    waited=$((waited + INTERVAL_SECS))
    activity="$(digest 2>/dev/null || true)"
    if [[ -n "${activity}" ]]; then
        bash "${SCRIPT_DIR}/heartbeat.sh" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent running ${waited}s — ${activity}" || true
    else
        bash "${SCRIPT_DIR}/heartbeat.sh" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent running ${waited}s" || true
    fi
done

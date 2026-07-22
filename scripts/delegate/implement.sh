#!/usr/bin/env bash
# implement.sh — Invoke the configured delegate agent to implement code changes.
#
# Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]
#   role_description — 1-2 lines describing this dispatch's responsibility,
#   written as a header block into the shared heartbeat log
#
# Produces:
#   <session_dir>/impl_status       — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary.txt  — the implementation summary
#   <session_dir>/impl_agent.log    — full agent log
#   <session_dir>/impl_agent        — resolved task, family, agent, and effort
#   <session_dir>/heartbeat.log     — shared liveness log for every dispatch in
#                                     this session: a role header block at start,
#                                     [wrapper] beats every 60s while the agent
#                                     pid is alive (each carrying an activity
#                                     digest from the agent log), and [agent]
#                                     narration lines (prompt-instructed)

set -euo pipefail

SESSION_DIR="${1:?Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/implementation_prompt.md}"
SUBTASK="${4:-implementation}"
ROLE_DESC="${5:-work order at ${PROMPT_FILE}}"
TASK="delegate.${SUBTASK}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUMMARY_FILE="${SESSION_DIR}/impl_summary.txt"
STATUS_FILE="${SESSION_DIR}/impl_status"
LOG_FILE="${SESSION_DIR}/impl_agent.log"
AGENT_FILE="${SESSION_DIR}/impl_agent"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
HEARTBEAT_INTERVAL_SECS=60

echo "implementing" > "${STATUS_FILE}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${SUBTASK} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" write "${WORKING_DIR}" "${PROMPT_FILE}" "${SUMMARY_FILE}" "${LOG_FILE}" &
AGENT_PID=$!

# Wrapper beats with an activity digest from the agent log: proves the process
# is alive and names what it is doing even while blocked in a long tool call.
# [agent] lines still come from the delegate itself, per its prompt.
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${SUBTASK}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent finished" || true
  echo "implemented" > "${STATUS_FILE}"
else
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent exited with code ${AGENT_CODE}" || true
  echo "error" > "${STATUS_FILE}"
  exit "${AGENT_CODE}"
fi

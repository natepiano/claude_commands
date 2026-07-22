#!/usr/bin/env bash
# review.sh — Invoke the configured delegate agent for a read-only review.
#
# Usage: review.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]
#   role_description — 1-2 lines describing this review's responsibility,
#   written as a header block into the shared heartbeat log
#
# Produces:
#   <session_dir>/review_status        — "reviewing" while running, "reviewed" on success, "error" on failure
#   <session_dir>/review_findings.txt  — review findings
#   <session_dir>/review_agent.log     — full agent log
#   <session_dir>/review_agent         — resolved task, family, agent, and effort
#   <session_dir>/heartbeat.log        — shared with implement.sh: role header at
#                                        start + [wrapper] beats every 60s. No
#                                        [agent] lines — the reviewer's read-only
#                                        sandbox cannot write files.

set -euo pipefail

SESSION_DIR="${1:?Usage: review.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/review_prompt.md}"
SUBTASK="${4:-review}"
ROLE_DESC="${5:-blind review of the current diff against its spec}"
TASK="delegate.${SUBTASK}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FINDINGS_FILE="${SESSION_DIR}/review_findings.txt"
STATUS_FILE="${SESSION_DIR}/review_status"
LOG_FILE="${SESSION_DIR}/review_agent.log"
AGENT_FILE="${SESSION_DIR}/review_agent"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
HEARTBEAT_INTERVAL_SECS=60

echo "reviewing" > "${STATUS_FILE}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${SUBTASK} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
AGENT_PID=$!

# Wrapper beat only: the reviewer's read-only sandbox cannot write [agent]
# narration lines, so pid liveness is the sole in-flight signal here.
(
  waited=0
  while kill -0 "${AGENT_PID}" 2>/dev/null; do
    sleep "${HEARTBEAT_INTERVAL_SECS}"
    kill -0 "${AGENT_PID}" 2>/dev/null || exit 0
    waited=$((waited + HEARTBEAT_INTERVAL_SECS))
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent running ${waited}s" || true
  done
) &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent finished" || true
  echo "reviewed" > "${STATUS_FILE}"
else
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent exited with code ${AGENT_CODE}" || true
  echo "error" > "${STATUS_FILE}"
  exit "${AGENT_CODE}"
fi

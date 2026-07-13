#!/usr/bin/env bash
# implement.sh — Invoke the configured friend agent to implement code changes.
#
# Usage: implement.sh <session_dir> [working_dir]
#
# Produces:
#   <session_dir>/impl_status       — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary.txt  — the implementation summary
#   <session_dir>/impl_agent.log    — full agent log
#   <session_dir>/impl_agent        — resolved task, family, agent, and effort

set -euo pipefail

SESSION_DIR="${1:?Usage: implement.sh <session_dir> [working_dir]}"
WORKING_DIR="${2:-$(pwd)}"
TASK="ask_a_friend.implementation"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_FILE="${SESSION_DIR}/implementation_prompt.md"
SUMMARY_FILE="${SESSION_DIR}/impl_summary.txt"
STATUS_FILE="${SESSION_DIR}/impl_status"
LOG_FILE="${SESSION_DIR}/impl_agent.log"
AGENT_FILE="${SESSION_DIR}/impl_agent"

echo "implementing" > "${STATUS_FILE}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" write "${WORKING_DIR}" "${PROMPT_FILE}" "${SUMMARY_FILE}" "${LOG_FILE}"; then
  echo "implemented" > "${STATUS_FILE}"
else
  EXIT_CODE=$?
  echo "error" > "${STATUS_FILE}"
  exit "${EXIT_CODE}"
fi

#!/usr/bin/env bash
# ask_a_friend.sh — Invoke the configured friend agent for a consultation round.
#
# Usage: ask_a_friend.sh <session_dir> [working_dir]
#
# Produces:
#   <session_dir>/status        — "asking" while running, "answered" on success, "error" on failure
#   <session_dir>/answer.txt    — the agent's response
#   <session_dir>/agent.log     — full agent log
#   <session_dir>/consult_agent — resolved task, family, agent, and effort

set -euo pipefail

SESSION_DIR="${1:?Usage: ask_a_friend.sh <session_dir> [working_dir]}"
WORKING_DIR="${2:-$(pwd)}"
TASK="ask_a_friend.consultation"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUESTION_FILE="${SESSION_DIR}/question.md"
ANSWER_FILE="${SESSION_DIR}/answer.txt"
STATUS_FILE="${SESSION_DIR}/status"
LOG_FILE="${SESSION_DIR}/agent.log"
AGENT_FILE="${SESSION_DIR}/consult_agent"

echo "asking" > "${STATUS_FILE}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

# Write mode is deliberate: Codex's read-only sandbox panics in the macOS
# system-configuration crate. agent_exec maps write mode to --full-auto.
if bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" write "${WORKING_DIR}" "${QUESTION_FILE}" "${ANSWER_FILE}" "${LOG_FILE}"; then
  echo "answered" > "${STATUS_FILE}"
else
  EXIT_CODE=$?
  echo "error" > "${STATUS_FILE}"
  exit "${EXIT_CODE}"
fi

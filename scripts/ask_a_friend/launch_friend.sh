#!/usr/bin/env bash
# Start the consultation worker through assign.sh.
# Usage: launch_friend.sh <session_dir> <working_dir>
# Keep the worker identity in staff and the answer in the consultation files.
set -euo pipefail

SESSION_DIR="${1:?Usage: launch_friend.sh <session_dir> <working_dir>}"
WORKING_DIR="${2:?missing working_dir}"
TASK="ask_a_friend.consultation"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENTS_DIR="${SCRIPT_DIR}/../agents"
QUESTION_FILE="${SESSION_DIR}/question.md"
ANSWER_FILE="${SESSION_DIR}/answer.txt"
STATUS_FILE="${SESSION_DIR}/status"
LOG_FILE="${SESSION_DIR}/agent.log"
AGENT_FILE="${SESSION_DIR}/consult_agent"
FRIEND="$(cat "${SESSION_DIR}/friend_name")"

if [[ ! -f "${QUESTION_FILE}" ]]; then
  echo "launch_friend.sh: no question at ${QUESTION_FILE}" >&2
  echo "error" > "${STATUS_FILE}"
  exit 2
fi

source "${AGENTS_DIR}/models_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  cat "${LOG_FILE}" >&2
  exit 1
fi
printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

echo "running" > "${STATUS_FILE}"
echo "friend=${FRIEND} family=${AGENT_FAMILY} agent=${AGENT_MODEL} effort=${AGENT_EFFORT:-default}"
if bash "${SCRIPT_DIR}/../direct/assign.sh" --role review --operation resident \
  --session-dir "${SESSION_DIR}" --new-worker "${FRIEND}" --cwd "${WORKING_DIR}" \
  --prompt "${QUESTION_FILE}" --summary "${ANSWER_FILE}" --log "${LOG_FILE}" \
  --id-file "${SESSION_DIR}/friend_id" --family "${AGENT_FAMILY}" \
  --model "${AGENT_MODEL}" --effort "${AGENT_EFFORT}"; then
  if [[ "${AGENT_FAMILY}" == claude ]]; then
    echo "id=$(cat "${SESSION_DIR}/friend_id")"
    echo "The friend answers through SendMessage."
  else
    echo "ended" > "${STATUS_FILE}"
  fi
else
  EXIT_CODE=$?
  echo "error" > "${STATUS_FILE}"
  exit "${EXIT_CODE}"
fi

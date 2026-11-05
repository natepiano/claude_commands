#!/usr/bin/env bash
# Start the configured consultation worker in the existing run.
# Keep its address in staff and its answer in the consultation artifacts.
set -euo pipefail

SESSION_DIR="${1:?Usage: launch_friend.sh <session_dir> <working_dir>}"
WORKING_DIR="${2:?missing working_dir}"
TASK="ask_a_friend.consultation"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.12.
PY="${SCRIPT_DIR}/../lib/py"
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

case "${AGENT_FAMILY}" in
  claude)
    if ! AGENT_BG_DETACH=1 AGENT_BG_EFFORT="${AGENT_EFFORT}" \
         bash "${AGENTS_DIR}/agent_bg.sh" --run "${SESSION_DIR}" "${FRIEND}" "${WORKING_DIR}" \
           "${QUESTION_FILE}" "${ANSWER_FILE}" "${LOG_FILE}" \
           "${AGENT_MODEL}" >/dev/null; then
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
    echo "running" > "${STATUS_FILE}"
    echo "friend=${FRIEND} family=claude agent=${AGENT_MODEL} effort=${AGENT_EFFORT:-default}"
    echo "The friend answers by SendMessage; follow-ups go to ${FRIEND} the same way."
    ;;
  codex)
    echo "running" > "${STATUS_FILE}"
    echo "friend=${FRIEND} family=codex agent=${AGENT_MODEL} effort=${AGENT_EFFORT:-default}"
    echo "Replies print below; send follow-ups with codex_mesh.py send --to ${FRIEND}."
    if "$PY" "${AGENTS_DIR}/codex_mesh.py" start --resident \
      --session-dir "${SESSION_DIR}" \
      --name "${FRIEND}" \
      --prompt-file "${QUESTION_FILE}" \
      --summary-file "${ANSWER_FILE}" \
      --log-file "${LOG_FILE}" \
      --model "${AGENT_MODEL}" \
      --effort "${AGENT_EFFORT}" \
      --task "${TASK}" --service-tier "${AGENT_SERVICE_TIER:-}"; then
      echo "ended" > "${STATUS_FILE}"
    else
      EXIT_CODE=$?
      echo "error" > "${STATUS_FILE}"
      exit "${EXIT_CODE}"
    fi
    ;;
  *)
    echo "launch_friend.sh: unsupported family '${AGENT_FAMILY}'." >&2
    echo "error" > "${STATUS_FILE}"
    exit 1
    ;;
esac

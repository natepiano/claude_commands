#!/usr/bin/env bash
# review.sh — Invoke the configured delegate agent for a read-only review.
#
# Usage: review.sh <session_dir> [working_dir] [prompt_file] [task]
#                  [role_description] [pass_activity]
#                  [role_description] [pass_activity] [pass_index]
#   role_description — 1-2 lines describing this review's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_activity — short user-facing description for the progress header
#   pass_index — 1 for a phase's broad review, incrementing for each closure
#   review after it. Artifacts are written per index and never overwritten:
#   a run that failed to converge can be read back round by round.
#
# Produces:
#   <session_dir>/review_status            — "reviewing" while running, "reviewed" on success, "error" on failure
#   <session_dir>/review_findings_<N>.txt  — review findings for pass N
#   <session_dir>/review_agent_<N>.log     — full agent log for pass N
#   <session_dir>/review_findings.txt      — symlink to the current pass's findings
#   <session_dir>/review_agent.log         — symlink to the current pass's log
#   <session_dir>/review_agent         — resolved task, family, agent, and effort
#   <session_dir>/review_wrapper_pid   — launcher pid for external reporters
#   <session_dir>/review_agent_pid     — agent_exec pid for external reporters
#   <session_dir>/heartbeat.log        — shared with implement.sh: role header at
#                                        start + [wrapper] beats every 60s, each
#                                        carrying an activity digest decoded from
#                                        the reviewer's streamed log. No [agent]
#                                        lines — the reviewer's read-only sandbox
#                                        cannot write files; its prompt-instructed
#                                        narration arrives via the digest instead.

set -euo pipefail

SESSION_DIR="${1:?Usage: review.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/review_prompt.md}"
SUBTASK="${4:-review}"
ROLE_DESC="${5:-blind review of the current diff against its spec}"
PASS_ACTIVITY="${6:-reviewing the current diff against its work order}"
PASS_INDEX="${7:-1}"
TASK="delegate.${SUBTASK}"

case "${PASS_INDEX}" in
  ''|*[!0-9]*|0) PASS_INDEX=1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FINDINGS_FILE="${SESSION_DIR}/review_findings_${PASS_INDEX}.txt"
STATUS_FILE="${SESSION_DIR}/review_status"
LOG_FILE="${SESSION_DIR}/review_agent_${PASS_INDEX}.log"

# Every reader of the unnumbered paths — the orchestrator's mid-run preemption
# read, the heartbeat digest — keeps working while the per-pass history is kept.
ln -sfn "review_findings_${PASS_INDEX}.txt" "${SESSION_DIR}/review_findings.txt"
ln -sfn "review_agent_${PASS_INDEX}.log" "${SESSION_DIR}/review_agent.log"
AGENT_FILE="${SESSION_DIR}/review_agent"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
HEARTBEAT_INTERVAL_SECS=60

printf '%s\n' "$$" > "${SESSION_DIR}/review_wrapper_pid"
echo "reviewing" > "${STATUS_FILE}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -f "${PROGRESS_STATE}" ]]; then
  if ! python3 "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind review \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}"; then
    echo "ERROR: unable to record the review pass start." >&2
    echo "error" > "${STATUS_FILE}"
    exit 1
  fi
fi

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${SUBTASK} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
AGENT_PID=$!
printf '%s\n' "${AGENT_PID}" > "${SESSION_DIR}/review_agent_pid"

# The reviewer's read-only sandbox cannot write [agent] lines, but it is not
# blind: the wrapper beat carries an activity digest decoded from the
# reviewer's own streamed log (the tool it is running, the file it is reading,
# its prompt-instructed narration lines).
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${SUBTASK}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  echo "reviewed" > "${STATUS_FILE}"
  if [[ -f "${PROGRESS_STATE}" ]]; then
    if ! python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
      echo "ERROR: unable to record the review pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent finished" || true
else
  echo "error" > "${STATUS_FILE}"
  if [[ -f "${PROGRESS_STATE}" ]]; then
    python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      || echo "ERROR: unable to record the review pass error." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent exited with code ${AGENT_CODE}" || true
  exit "${AGENT_CODE}"
fi

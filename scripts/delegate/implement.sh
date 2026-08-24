#!/usr/bin/env bash
# implement.sh — Invoke the configured delegate agent to implement code changes.
#
# Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task]
#                     [role_description] [pass_kind] [pass_activity] [fix_pass]
#   role_description — 1-2 lines describing this dispatch's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_kind — impl, arch, or fix; enables durable progress recording
#   pass_activity — short user-facing description for the progress header
#   fix_pass — required pass count when pass_kind is fix
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
PASS_KIND="${6:-}"
PASS_ACTIVITY="${7:-${ROLE_DESC%%$'\n'*}}"
FIX_PASS="${8:-0}"
TASK="delegate.${SUBTASK}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUMMARY_FILE="${SESSION_DIR}/impl_summary.txt"
STATUS_FILE="${SESSION_DIR}/impl_status"
LOG_FILE="${SESSION_DIR}/impl_agent.log"
AGENT_FILE="${SESSION_DIR}/impl_agent"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
FINDINGS_HELPER="${SCRIPT_DIR}/findings.py"
FINDINGS_STATE="${SESSION_DIR}/findings_state.json"
HEARTBEAT_INTERVAL_SECS=60

echo "implementing" > "${STATUS_FILE}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -n "${PASS_KIND}" && -f "${PROGRESS_STATE}" ]]; then
  if ! PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind "${PASS_KIND}" \
    --fix-pass "${FIX_PASS}" \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}"; then
    echo "ERROR: unable to record the ${PASS_KIND} pass start." >&2
    echo "error" > "${STATUS_FILE}"
    exit 1
  fi
fi

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
  echo "implemented" > "${STATUS_FILE}"
  if [[ -n "${PASS_KIND}" && -f "${PROGRESS_STATE}" ]]; then
    if ! PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
      echo "ERROR: unable to record the ${PASS_KIND} pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  # Only the launcher watches the worker exit, so only the launcher can say a
  # repair landed. Asking the orchestrator to record it later leaves a gap it can
  # be killed or compacted inside, and that gap used to resolve as "fixed" --
  # handing the next review a defect pre-labelled as repaired.
  if [[ "${PASS_KIND}" == "fix" && -f "${FINDINGS_STATE}" ]]; then
    python3 "${FINDINGS_HELPER}" landed --session-dir "${SESSION_DIR}" \
      || echo "ERROR: unable to record the repair round as landed." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent finished" || true
else
  echo "error" > "${STATUS_FILE}"
  if [[ -n "${PASS_KIND}" && -f "${PROGRESS_STATE}" ]]; then
    PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      || echo "ERROR: unable to record the ${PASS_KIND} pass error." >&2
  fi
  # The attempt stands rather than being refunded: a worker that ran and then
  # failed may have left partial edits behind, and the launcher cannot tell.
  if [[ "${PASS_KIND}" == "fix" && -f "${FINDINGS_STATE}" ]]; then
    python3 "${FINDINGS_HELPER}" abandon --session-dir "${SESSION_DIR}" --edits-landed \
      --reason "the ${SUBTASK} worker exited with code ${AGENT_CODE}" \
      || echo "ERROR: unable to record the repair round as abandoned." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent exited with code ${AGENT_CODE}" || true
  exit "${AGENT_CODE}"
fi

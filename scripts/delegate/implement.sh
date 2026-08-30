#!/usr/bin/env bash
# implement.sh — Invoke the configured delegate agent to implement code changes.
#
# Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task]
#                     [role_description] [pass_kind] [pass_activity] [fix_pass]
#                     <team_role>
#   role_description — 1-2 lines describing this dispatch's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_kind — impl, arch, or fix; enables durable progress recording
#   pass_activity — short user-facing description for the progress header
#   fix_pass — required pass count when pass_kind is fix
#   team_role — required; which member of the phase team this is (impl, impl2,
#   test, review). Every dispatch is a team member -- the only two callers are
#   the implementation and fix phases, and both run a team -- so there is one
#   artifact shape rather than a solo shape and a team shape. Several launchers
#   run at once against one session directory, so every artifact below is
#   written per role; only the member the orchestrator gives a pass_kind
#   records a progress pass, because the recorder closes any open pass when a
#   new one starts and three concurrent passes would leave the ledger
#   describing whichever happened to finish last.
#
# Produces:
#   <session_dir>/impl_status_<role>      — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary_<role>.txt — the implementation summary
#   <session_dir>/impl_agent_<role>.log   — full agent log
#   <session_dir>/impl_agent_<role>       — resolved task, family, agent, and effort
#   <session_dir>/board.log         — shared coordination board; this launcher
#                                     posts each member's start and end so peers
#                                     learn of them without the orchestrator,
#                                     which is asleep between progress ticks
#   <session_dir>/heartbeat.log     — shared liveness log for every dispatch in
#                                     this session: a role header block at start,
#                                     [wrapper] beats every 60s while the agent
#                                     pid is alive (each carrying an activity
#                                     digest from the agent log), and [agent]
#                                     narration lines (prompt-instructed)
#   <session_dir>/impl_awake_<role>   — seconds the beat loop has counted for
#                                     this dispatch, which excludes time the
#                                     machine was suspended; stamped on the pass

set -euo pipefail

SESSION_DIR="${1:?Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/implementation_prompt.md}"
SUBTASK="${4:-implementation}"
ROLE_DESC="${5:-work order at ${PROMPT_FILE}}"
PASS_KIND="${6:-}"
PASS_ACTIVITY="${7:-${ROLE_DESC%%$'\n'*}}"
FIX_PASS="${8:-0}"
TEAM_ROLE="${9:?Usage: implement.sh needs a team_role (impl, impl2, test, review) as its 9th argument}"
TASK="delegate.${SUBTASK}"

# The role indexes file paths and board fields, so hold it to a character set
# that can escape neither.
if [[ ! "${TEAM_ROLE}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; then
  echo "ERROR: team_role must be alphanumeric/_- and 1-32 chars; got '${TEAM_ROLE}'." >&2
  exit 2
fi
SLOT="_${TEAM_ROLE}"
BEAT_TAG="${SUBTASK}:${TEAM_ROLE}"
BOARD_AGENT="${TEAM_ROLE}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUMMARY_FILE="${SESSION_DIR}/impl_summary${SLOT}.txt"
STATUS_FILE="${SESSION_DIR}/impl_status${SLOT}"
LOG_FILE="${SESSION_DIR}/impl_agent${SLOT}.log"
AGENT_FILE="${SESSION_DIR}/impl_agent${SLOT}"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
AWAKE_FILE="${SESSION_DIR}/impl_awake${SLOT}"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
FINDINGS_HELPER="${SCRIPT_DIR}/findings.py"
FINDINGS_STATE="${SESSION_DIR}/findings_state.json"
HEARTBEAT_INTERVAL_SECS=60

# -1 says the beat loop counted nothing, which a dispatch shorter than one
# interval always does. The previous dispatch's file is removed before this one
# launches, so a leftover total can never be read as this pass's.
awake_seconds() {
  local counted
  counted="$(cat "${AWAKE_FILE}" 2>/dev/null || true)"
  case "${counted}" in
    ''|*[!0-9]*) echo "-1" ;;
    *) echo "${counted}" ;;
  esac
}

rm -f "${AWAKE_FILE}"
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

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${BEAT_TAG} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

# Announce the member on the shared board. Peers read the board to learn who is
# running in which role; the orchestrator sleeps between progress ticks, so
# nothing else would tell them.
bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" register \
  "${SUBTASK} launcher up (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); status in impl_status${SLOT}" || true

# The delegate's own verify.sh runs inherit these and take the cargo token with
# them, so serialization does not depend on the agent remembering a prompt rule.
export PLAN_DELEGATE_BOARD_DIR="${SESSION_DIR}"
export PLAN_DELEGATE_TEAM_ROLE="${TEAM_ROLE}"

bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" write "${WORKING_DIR}" "${PROMPT_FILE}" "${SUMMARY_FILE}" "${LOG_FILE}" &
AGENT_PID=$!

# Wrapper beats with an activity digest from the agent log: proves the process
# is alive and names what it is doing even while blocked in a long tool call.
# [agent] lines still come from the delegate itself, per its prompt.
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" \
  "${AWAKE_FILE}" &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  echo "implemented" > "${STATUS_FILE}"
  if [[ -n "${PASS_KIND}" && -f "${PROGRESS_STATE}" ]]; then
    if ! PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed \
      --agent-awake-seconds "$(awake_seconds)"; then
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
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  # A member that ends without saying so on the board leaves its peers waiting
  # on work already finished, so the launcher posts it rather than trusting the
  # agent to have posted before it exited.
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" done \
    "${SUBTASK} finished; summary at impl_summary${SLOT}.txt" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
else
  echo "error" > "${STATUS_FILE}"
  if [[ -n "${PASS_KIND}" && -f "${PROGRESS_STATE}" ]]; then
    PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      --agent-awake-seconds "$(awake_seconds)" \
      || echo "ERROR: unable to record the ${PASS_KIND} pass error." >&2
  fi
  # The attempt stands rather than being refunded: a worker that ran and then
  # failed may have left partial edits behind, and the launcher cannot tell.
  if [[ "${PASS_KIND}" == "fix" && -f "${FINDINGS_STATE}" ]]; then
    python3 "${FINDINGS_HELPER}" abandon --session-dir "${SESSION_DIR}" --edits-landed \
      --reason "the ${SUBTASK} worker exited with code ${AGENT_CODE}" \
      || echo "ERROR: unable to record the repair round as abandoned." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  # Same reason as the success path, plus the token: a member killed mid-hold
  # would otherwise hold the cargo token until its hold expired, stalling every
  # peer behind a lock whose owner is already gone.
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
    "${SUBTASK} exited with code ${AGENT_CODE}; this role is down" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
  exit "${AGENT_CODE}"
fi

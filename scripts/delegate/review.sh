#!/usr/bin/env bash
# review.sh — Invoke the configured delegate agent for a read-only review.
#
# Usage: review.sh <session_dir> [working_dir] [prompt_file] [task]
#                  [role_description] [pass_activity] [pass_index]
#                  [early_ready_file]
#   role_description — 1-2 lines describing this review's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_activity — short user-facing description for the progress header
#   pass_index — 1 for a phase's broad review, incrementing for each closure
#   review after it. Artifacts are written per index and never overwritten:
#   a run that failed to converge can be read back round by round.
#   early_ready_file — early-launch mode: the reviewer starts while the
#   implementer is still running, so its start-pass is deferred until this
#   sentinel appears (the recorder closes any active pass as interrupted when
#   a new one starts, and the implementation pass is still open at launch).
#   The orchestrator creates the sentinel after the implementation pass has
#   finished and the final diff is written.
#
# Produces:
#   <session_dir>/review_status            — "reviewing" while running, "reviewed" on success, "error" on failure
#   <session_dir>/review_pid               — this wrapper's pid, so a progress
#                                            report can tell an early-launched
#                                            reviewer that is still working from
#                                            one that was killed
#   <session_dir>/review_findings_<N>.txt  — review findings for pass N
#   <session_dir>/review_agent_<N>.log     — full agent log for pass N
#   <session_dir>/review_findings.txt      — symlink to the current pass's findings
#   <session_dir>/review_agent.log         — symlink to the current pass's log
#   <session_dir>/review_agent         — resolved task, family, agent, and effort
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
EARLY_READY_FILE="${8:-}"
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

echo "reviewing" > "${STATUS_FILE}"
# Only meaningful while an early launch has a row in the stage table, and read
# there only if it was written after that row was armed — so it is written on
# every run, before anything can fail, rather than in the early branch alone.
echo "$$" > "${SESSION_DIR}/review_pid"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

PASS_STARTED=0
record_pass_start() {
  [[ -f "${PROGRESS_STATE}" ]] || return 0
  if ! PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind review \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}"; then
    return 1
  fi
  PASS_STARTED=1
}

if [[ -z "${EARLY_READY_FILE}" ]]; then
  if ! record_pass_start; then
    echo "ERROR: unable to record the review pass start." >&2
    echo "error" > "${STATUS_FILE}"
    exit 1
  fi
fi

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${SUBTASK} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
AGENT_PID=$!

# The reviewer's read-only sandbox cannot write [agent] lines, but it is not
# blind: the wrapper beat carries an activity digest decoded from the
# reviewer's own streamed log (the tool it is running, the file it is reading,
# its prompt-instructed narration lines).
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${SUBTASK}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" &
HEARTBEAT_LOOP_PID=$!

if [[ -n "${EARLY_READY_FILE}" ]]; then
  # The implementation pass is open until the sentinel appears; starting the
  # review pass before then would close it as interrupted.
  while kill -0 "${AGENT_PID}" 2>/dev/null && [[ ! -e "${EARLY_READY_FILE}" ]]; do
    sleep 5
  done
  if [[ -e "${EARLY_READY_FILE}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    if ! record_pass_start; then
      echo "ERROR: unable to record the early review pass start." >&2
      kill "${AGENT_PID}" 2>/dev/null || true
      wait "${AGENT_PID}" 2>/dev/null || true
      kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
      wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
fi

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  echo "reviewed" > "${STATUS_FILE}"
  # A completed early review is a real pass only once the sentinel exists: the
  # worker can beat the 5s poll to the finish, leaving the start unrecorded
  # above. Before the sentinel there is nothing to record. The implementation
  # pass is still open, so start-pass would close it as interrupted and every
  # later progress call would be refused for having no active window; and the
  # verdict is void regardless, because the final diff does not exist yet.
  if [[ "${PASS_STARTED}" -eq 0 && -e "${EARLY_READY_FILE}" ]]; then
    record_pass_start || echo "ERROR: unable to record the review pass start." >&2
  fi
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    if ! PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
      echo "ERROR: unable to record the review pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  if [[ -n "${EARLY_READY_FILE}" && ! -e "${EARLY_READY_FILE}" ]]; then
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${SUBTASK} agent finished before delivery — no pass recorded, verdict void" || true
  else
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent finished" || true
  fi
else
  echo "error" > "${STATUS_FILE}"
  # An early reviewer that failed before its pass started recorded nothing;
  # there is no pass to close.
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    PLAN_DELEGATE_PASS_OWNER=launcher python3 "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      || echo "ERROR: unable to record the review pass error." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${SUBTASK} agent exited with code ${AGENT_CODE}" || true
  exit "${AGENT_CODE}"
fi

#!/usr/bin/env bash
# review.sh — Invoke the configured delegate agent for a read-only review.
#
# Usage: review.sh <session_dir> [working_dir] [prompt_file] [task]
#                  [role_description] [pass_activity] [pass_index] [lens]
#                  [early_ready_file]
#   role_description — 1-2 lines describing this review's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_activity — short user-facing description for the progress header
#   pass_index — 1 for a phase's broad review, incrementing for each closure
#   review after it. Artifacts are written per index and never overwritten:
#   a run that failed to converge can be read back round by round.
#   lens — which reading this reviewer is doing, when a phase's broad review
#   runs all three at once: adversary, conformance, or reach. It suffixes every
#   artifact below, so three concurrent reviewers never overwrite each other,
#   and it selects the seat the pass records under. The seat assignment is
#   fixed rather than meaningful — adversary sits in `review` because that is
#   the column an early launch already claims, and the other two take the seats
#   left. Empty is the single-reviewer layout: unsuffixed names, no seat, which
#   is what a closure review and a solo broad review still run.
#   early_ready_file — early-launch mode: the reviewer starts while the
#   implementer is still running, so its start-pass is deferred until this
#   sentinel appears (the recorder closes any active pass as interrupted when
#   a new one starts, and the implementation pass is still open at launch).
#   The orchestrator creates the sentinel after the implementation pass has
#   finished and the final diff is written.
#
# Produces (every name below takes a `_<lens>` suffix when a lens is given):
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
#   <session_dir>/review_awake         — seconds the beat loop has counted for
#                                        this dispatch, which excludes time the
#                                        machine was suspended; stamped on the
#                                        pass. An early launch starts the loop
#                                        before its pass opens, so the total can
#                                        exceed the pass it is recorded on.

set -euo pipefail

SESSION_DIR="${1:?Usage: review.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/review_prompt.md}"
SUBTASK="${4:-review}"
ROLE_DESC="${5:-blind review of the current diff against its spec}"
PASS_ACTIVITY="${6:-reviewing the current diff against its work order}"
PASS_INDEX="${7:-1}"
LENS="${8:-}"
EARLY_READY_FILE="${9:-}"
TASK="delegate.${SUBTASK}"

case "${PASS_INDEX}" in
  ''|*[!0-9]*|0) PASS_INDEX=1 ;;
esac

# A closed set, like the pass kinds: an unrecognized lens would suffix artifacts
# nobody reads and record a pass under a seat no column renders, both silently.
case "${LENS}" in
  '') TEAM_SLOT='' ;;
  adversary) TEAM_SLOT=review ;;
  conformance) TEAM_SLOT=impl ;;
  reach) TEAM_SLOT=test ;;
  *)
    echo "ERROR: unknown review lens '${LENS}' (adversary, conformance, reach)" >&2
    exit 2
    ;;
esac

SUFFIX="${LENS:+_${LENS}}"
# What the shared heartbeat log tags this reviewer's beats with. Three lenses
# run at once against one log, and `review` on all three lines cannot be read.
BEAT_TAG="${SUBTASK}${LENS:+:${LENS}}"
# The seat this pass records under, so three concurrent reviewers key three pass
# records instead of each closing the last as interrupted. Exported even when the
# lens is absent and the seat with it: the lens decides this launcher's seat, and
# leaving the variable unset instead would let an inherited one decide it, which
# is how a lone reviewer ends up closing a live seat's pass.
export PLAN_DELEGATE_TEAM_ROLE="${TEAM_SLOT}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="${SCRIPT_DIR}/../lib/py"
FINDINGS_FILE="${SESSION_DIR}/review_findings_${PASS_INDEX}${SUFFIX}.txt"
STATUS_FILE="${SESSION_DIR}/review_status${SUFFIX}"
LOG_FILE="${SESSION_DIR}/review_agent_${PASS_INDEX}${SUFFIX}.log"

# Every reader of the unnumbered paths — the orchestrator's mid-run preemption
# read, the heartbeat digest — keeps working while the per-pass history is kept.
ln -sfn "review_findings_${PASS_INDEX}${SUFFIX}.txt" "${SESSION_DIR}/review_findings${SUFFIX}.txt"
ln -sfn "review_agent_${PASS_INDEX}${SUFFIX}.log" "${SESSION_DIR}/review_agent${SUFFIX}.log"
AGENT_FILE="${SESSION_DIR}/review_agent${SUFFIX}"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
AWAKE_FILE="${SESSION_DIR}/review_awake${SUFFIX}"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
HEARTBEAT_INTERVAL_SECS=60

# -1 says the beat loop counted nothing, which a review shorter than one
# interval always does. The previous review's file is removed before this one
# launches, so a leftover total can never be read as this pass's.
awake_seconds() {
  local counted
  counted="$(cat "${AWAKE_FILE}" 2>/dev/null || true)"
  case "${counted}" in
    ''|*[!0-9]*) echo "-1" ;;
    *) echo "${counted}" ;;
  esac
}

# A seated reviewer opens a new occupancy of a chair a writer has just left.
# Without a line of its own the note under the progress table keeps showing the
# words of the delegate that sat there while the code was being written, aged by
# however long the review has run -- a line describing the wrong agent, which
# reads as a stalled one. A lone reviewer holds no seat and posts nothing.
# A blind reviewer is a fresh read-only session with no peers and no address, so
# every line says `mesh=none`: nothing can reach it and nothing should wait to.
board_post() {
  [[ -n "${TEAM_SLOT}" ]] || return 0
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${TEAM_SLOT}" "$1" "$2" || true
}

rm -f "${AWAKE_FILE}"
echo "reviewing" > "${STATUS_FILE}"
# Only meaningful while an early launch is showing in the progress report, and
# read there only if it was written after the review was armed — so it is
# written on every run, before anything can fail, not in the early branch alone.
echo "$$" > "${SESSION_DIR}/review_pid${SUFFIX}"

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  echo "error" > "${STATUS_FILE}"
  exit 1
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

board_post register \
  "${LENS} review up (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); mesh=none; role=review; status in review_status${SUFFIX}"

PASS_STARTED=0
record_pass_start() {
  [[ -f "${PROGRESS_STATE}" ]] || return 0
  if ! PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" start-pass \
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

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${BEAT_TAG} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
AGENT_PID=$!

# The reviewer's read-only sandbox cannot write [agent] lines, but it is not
# blind: the wrapper beat carries an activity digest decoded from the
# reviewer's own streamed log (the tool it is running, the file it is reading,
# its prompt-instructed narration lines).
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" \
  "${AWAKE_FILE}" &
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
    if ! PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed \
      --agent-awake-seconds "$(awake_seconds)"; then
      echo "ERROR: unable to record the review pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  if [[ -n "${EARLY_READY_FILE}" && ! -e "${EARLY_READY_FILE}" ]]; then
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${BEAT_TAG} agent finished before delivery — no pass recorded, verdict void" || true
  else
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  fi
  board_post done \
    "launcher: ${LENS} review finished; findings in review_findings_${PASS_INDEX}${SUFFIX}.txt"
else
  echo "error" > "${STATUS_FILE}"
  # An early reviewer that failed before its pass started recorded nothing;
  # there is no pass to close.
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      --agent-awake-seconds "$(awake_seconds)" \
      || echo "ERROR: unable to record the review pass error." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  board_post blocked \
    "launcher: ${LENS} review exited with code ${AGENT_CODE}; this seat is down"
  exit "${AGENT_CODE}"
fi

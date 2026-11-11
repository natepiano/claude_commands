#!/usr/bin/env bash
# review.sh starts a fresh Claude reviewer.
#
# Usage: review.sh [--phase <id>] [--style-since <commit> [--style-until <commit>]] <session_dir>
#                  [working_dir] [prompt_file] [task]
#                  [role_description] [pass_activity] [pass_index] [lens]
#                  [early_ready_file]
#   role_description — 1-2 lines describing this review's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_activity — short user-facing description for the progress header
#   pass_index — 1 for a phase's broad review, incrementing for each closure
#   review after it. Artifacts are written per index and never overwritten:
#   a run that failed to converge can be read back round by round.
#   lens — which reading this reviewer is doing. The shared lens list is in
#   agents_log.py. It suffixes each artifact,
#   so concurrent reviewers keep separate results. It does not select a worker.
#   Empty is the single-reviewer layout, with unsuffixed artifact names.
#   early_ready_file — early-launch mode: the reviewer starts while the
#   implementer is still running, so its start-pass is deferred until this
#   sentinel appears (the recorder closes any active pass as interrupted when
#   a new one starts, and the implementation pass is still open at launch).
#   The unit director creates the sentinel after the implementation pass has
#   finished and the final diff is written.
#
#   --phase <id> names the phase this review belongs to: start-pass stamps the
#   pass with it, and every per-pass file below is written to
#   <session_dir>/phases/<id>/ instead of <session_dir>, so two phases reviewing
#   at once never overwrite each other. heartbeat.log stays run-wide.
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
#   <session_dir>/heartbeat.log        — shared with assign.py: role header at
#                                        start + [wrapper] beats every 60s, each
#                                        carrying an activity digest decoded from
#                                        the reviewer's streamed log.
#                                        Fresh reviewers have read-only access.

set -euo pipefail
LAUNCHER_COMMAND=(bash "${BASH_SOURCE[0]}" "$@")
"${BASH_SOURCE[0]%/*}/../lib/py" "${BASH_SOURCE[0]%/*}/../direct/assign_gate.py"
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"

# A phase running beside the current one names itself, so start-pass stamps this pass with it.
PHASE_ID=""
STYLE_SINCE=""
STYLE_UNTIL=""
STYLE_INPUT_DIR=""
REQUESTED_LENS=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --lens) REQUESTED_LENS="${2:?--lens requires a lens}" ;;
    --style-since) STYLE_SINCE="${2:?--style-since requires a baseline}" ;;
    --style-until) STYLE_UNTIL="${2:?--style-until requires an end commit}" ;;
    --phase) PHASE_ID="${2:?--phase requires a phase id}" ;;
    *) echo "ERROR: unknown review option '$1'." >&2; exit 2 ;;
  esac
  shift 2
done
SESSION_DIR="${1:?Usage: review.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
# A named phase keeps its review files in its own directory, so two phases reviewing at once
# never write the same pass's findings, status or log. Run-wide files stay in the session.
ARTIFACT_DIR="${SESSION_DIR}"
if [[ -n "${PHASE_ID}" ]]; then
  # The directory keeps only the id's letters, digits, `.`, `_` and `-`: `ad hoc` is `adhoc`.
  PHASE_DIR_NAME="${PHASE_ID//[^[:alnum:]._-]/}"
  if [[ -z "${PHASE_DIR_NAME}" || "${PHASE_DIR_NAME}" == . || "${PHASE_DIR_NAME}" == .. ]]; then
    echo "ERROR: phase id names no directory: ${PHASE_ID}" >&2
    exit 2
  fi
  ARTIFACT_DIR="${SESSION_DIR}/phases/${PHASE_DIR_NAME}"
fi
if [[ -n "${STYLE_SINCE}" && ( -z "${PHASE_ID}" || -n "${9:-}" ) ]]; then
  echo "ERROR: --style-since requires --phase and a final-diff dispatch (no early review)." >&2
  exit 2
fi
if [[ -n "${STYLE_UNTIL}" && -z "${STYLE_SINCE}" ]]; then
  echo "ERROR: --style-until requires --style-since." >&2
  exit 2
fi
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${ARTIFACT_DIR}/review_prompt.md}"
SUBTASK="${4:-review}"
ROLE_DESC="${5:-blind review of the current diff against its spec}"
PASS_ACTIVITY="${6:-reviewing the current diff against its work order}"
PASS_INDEX="${7:-1}"
LENS="${REQUESTED_LENS:-${8:-}}"
EARLY_READY_FILE="${9:-}"
TASK="direct.${SUBTASK}"
# shellcheck source=/dev/null
source "${BASH_SOURCE[0]%/*}/../agents/models_config.sh"
models_launch_configuration || exit 1
agents_resolve_claude_launcher "${TASK}" review.sh || exit $?

case "${PASS_INDEX}" in
  ''|*[!0-9]*|0) PASS_INDEX=1 ;;
esac

# A closed set, like the pass kinds: an unrecognized lens would suffix artifacts
# nobody reads and record a pass under a worker no column renders, both silently.
LENS_SLOT="$("${BASH_SOURCE[0]%/*}/../lib/py" - "${BASH_SOURCE[0]}" "${LENS}" <<'PYLENS'
import sys
from pathlib import Path
with open(Path(sys.argv[1]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.usage.agents_log import PHASE_REVIEW_LENSES
lens = sys.argv[2]
if lens and lens not in PHASE_REVIEW_LENSES:
    print(f"ERROR: unknown review lens '{lens}' ({', '.join(PHASE_REVIEW_LENSES)})", file=sys.stderr)
    sys.exit(2)
print({"adversary": "test", "contract": "impl", "": ""}.get(lens, "review"))
PYLENS
)"

SUFFIX="${LENS:+_${LENS}}"
# Give each lens a separate tag in the shared activity log.
BEAT_TAG="${SUBTASK}${LENS:+:${LENS}}"
# Each lens records its pass in a separate slot.
TEAM_SLOT="${LENS_SLOT}"
export UNIT_TEAM_ROLE="${TEAM_SLOT}"
export UNIT_RUN_DIR="${SESSION_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.12.
PY="${SCRIPT_DIR}/../lib/py"
TURN_FINDINGS_READY=0
PASS_STARTED=0
mkdir -p "${ARTIFACT_DIR}"
# Every dispatch names the run's director in the run folder (unit_runs.py), which the
# production tree reads: a marker names one run per director, and a director runs phases side by side.
FINDINGS_FILE="${ARTIFACT_DIR}/review_findings_${PASS_INDEX}${SUFFIX}.txt"
STATUS_FILE="${ARTIFACT_DIR}/review_status${SUFFIX}"
SLOT_FILE="${ARTIFACT_DIR}/review_slot${SUFFIX}"
rm -f "${SLOT_FILE}"
LOG_FILE="${ARTIFACT_DIR}/review_agent_${PASS_INDEX}${SUFFIX}.log"

AGENT_FILE="${ARTIFACT_DIR}/review_agent${SUFFIX}"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
HEARTBEAT_INTERVAL_SECS=60

write_review_status() {
  local temporary
  temporary="$(mktemp "${ARTIFACT_DIR}/.review-status.XXXXXX")" || return 1
  if ! { printf '%s\n' "$1" > "${temporary}" && mv -f -- "${temporary}" "${STATUS_FILE}"; }; then
    rm -f -- "${temporary}"
    return 1
  fi
}

# Validate before any reviewer starts. UX records launch evidence without a pass window.
if [[ -f "${PROGRESS_STATE}" ]]; then
  if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" check-launch \
    --session-dir "${SESSION_DIR}" --pass-kind review \
    --prompt-file "${PROMPT_FILE}" --lens "${LENS}" \
    ${PHASE_ID:+--phase-id "${PHASE_ID}"}; then
    echo "ERROR: unable to check the review launch." >&2
    write_review_status error
    exit 1
  fi
fi

initialize_review_artifacts() {
  # Every reader of the unnumbered paths — the unit director's mid-run
  # preemption read, the heartbeat digest — keeps working while the per-pass
  # history is kept.
  ln -sfn "review_findings_${PASS_INDEX}${SUFFIX}.txt" "${ARTIFACT_DIR}/review_findings${SUFFIX}.txt"
  ln -sfn "review_agent_${PASS_INDEX}${SUFFIX}.log" "${ARTIFACT_DIR}/review_agent${SUFFIX}.log"
  : > "${FINDINGS_FILE}"
  TURN_FINDINGS_READY=1
  write_review_status reviewing
  printf '%s\n' "${TEAM_SLOT}" > "${SLOT_FILE}"
  # Only meaningful while an early launch is showing in the progress report,
  # and read there only if written after the review was armed.
  echo "$$" > "${ARTIFACT_DIR}/review_pid${SUFFIX}"
}

# Each review registers its slot on the coordination board.
board_post() {
  [[ -n "${TEAM_SLOT}" ]] || return 0
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${TEAM_SLOT}" "$1" "$2" || true
}

finish_style_review() {
  [[ -n "${STYLE_INPUT_DIR}" ]] || return 0
  "$PY" "${SCRIPT_DIR}/../lint/style_review.py" finish \
    --input-dir "${STYLE_INPUT_DIR}" --result "${FINDINGS_FILE}" --dispatch-code "$1"
}

finish_review_exit() {
  local code=$?
  trap - EXIT
  set +e
  if [[ "${code}" -ne 0 && "${TURN_FINDINGS_READY}" -eq 1 ]]; then
    write_review_status error
  fi
  finish_style_review "${code}" || code=1
  exit "${code}"
}

interrupt_review() {
  if [[ -n "${HEARTBEAT_LOOP_PID:-}" ]]; then
    kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
    wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
  fi
  exit "$1"
}

trap finish_review_exit EXIT
trap 'interrupt_review 143' TERM
trap 'interrupt_review 130' INT
trap 'interrupt_review 129' HUP

if [[ -n "${STYLE_SINCE}" ]]; then
  STYLE_DIRECTORY_FILE="${ARTIFACT_DIR}/style_directory.$$.txt"
  "$PY" "${SCRIPT_DIR}/../lint/style_review.py" prepare \
    --working-dir "${WORKING_DIR}" --since "${STYLE_SINCE}" \
    --until "${STYLE_UNTIL}" \
    --phase-dir "${ARTIFACT_DIR}" --prompt "${PROMPT_FILE}" \
    --owner-pid "$$" --pass-name "pass-${PASS_INDEX}${SUFFIX}" --directory-file "${STYLE_DIRECTORY_FILE}"
  STYLE_INPUT_DIR="$(cat "${STYLE_DIRECTORY_FILE}")"
  rm -f "${STYLE_DIRECTORY_FILE}"
  PROMPT_FILE="${STYLE_INPUT_DIR}/prompt.md"
fi

initialize_review_artifacts

source "${SCRIPT_DIR}/../agents/claude_account.sh"
claude_launch_environment workers
if RESOLVED_MODEL="$(_agents_model_slug claude "${AGENT_MODEL}")"; then
  AGENT_MODEL="${RESOLVED_MODEL}"
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

board_post register \
  "${LENS} review up (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); role=review; status in review_status${SUFFIX}"

record_pass_start() {
  # UX time belongs to the director's activity, including early launches.
  [[ "${LENS}" != ux ]] || return 0
  [[ -f "${PROGRESS_STATE}" ]] || return 0
  if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind review \
    --prompt-file "${PROMPT_FILE}" \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}" \
    --launcher-command "$("$PY" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "${LAUNCHER_COMMAND[@]}")" \
    ${PHASE_ID:+--phase-id "${PHASE_ID}"}; then
    return 1
  fi
  PASS_STARTED=1
}

if [[ -z "${EARLY_READY_FILE}" ]]; then
  if ! record_pass_start; then
    echo "ERROR: unable to record the review pass start." >&2
    exit 1
  fi
fi

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${BEAT_TAG} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

# A broad pass reviews the exact pending snapshot it receives. New pushes made
# during this pass stay pending for the next pass.
FAST_TRACK_UNIT=""
FAST_TRACK_COMMITS=()
FAST_TRACK_SCRIPT="${SCRIPT_DIR}/../production/fast_track.py"
if [[ "${PASS_INDEX}" == 1 && "${LENS}" != ux && -f "${PROGRESS_STATE}" ]]; then
  FAST_TRACK_UNIT="$("$PY" - "${PROGRESS_STATE}" <<'PY'
import json
import sys
from pathlib import Path
from scripts.lib import unit_runs
from scripts.production import staff

state = json.loads(Path(sys.argv[1]).read_text())
owner = unit_runs.run_owner(state, staff.local_snapshot())
print(owner.name if isinstance(owner, staff.StaffRow) else "")
PY
)"
  if [[ -n "${FAST_TRACK_UNIT}" ]]; then
    FAST_TRACK_DIFF="${ARTIFACT_DIR}/review_fast_track_${PASS_INDEX}.diff"
    # One atomic snapshot for every lens and retry of this phase's broad pass.
    (
      flock -x 9
      if [[ ! -f "${FAST_TRACK_DIFF}" ]]; then
        SNAPSHOT_TEMP="${FAST_TRACK_DIFF}.$$.tmp"
        trap 'rm -f "${SNAPSHOT_TEMP}"' EXIT
        (cd "${WORKING_DIR}" && "$PY" "${FAST_TRACK_SCRIPT}" --pending --unit "${FAST_TRACK_UNIT}") > "${SNAPSHOT_TEMP}"
        mv "${SNAPSHOT_TEMP}" "${FAST_TRACK_DIFF}"
      fi
    ) 9>"${FAST_TRACK_DIFF}.lock"
    while IFS= read -r commit; do
      [[ -z "${commit}" ]] || FAST_TRACK_COMMITS+=(--commit "${commit}")
    done < <(sed -n 's/^## Fast-track \([0-9a-f]*\) .*/\1/p' "${FAST_TRACK_DIFF}")
    if [[ ${#FAST_TRACK_COMMITS[@]} -gt 0 ]]; then
      REVIEW_PROMPT="${ARTIFACT_DIR}/review_with_fast_track_${PASS_INDEX}${SUFFIX}.$$.md"
      cat "${PROMPT_FILE}" > "${REVIEW_PROMPT}"
      printf '\nReview these pending fast-track fixes against the same rules:\n' >> "${REVIEW_PROMPT}"
      cat "${FAST_TRACK_DIFF}" >> "${REVIEW_PROMPT}"
      PROMPT_FILE="${REVIEW_PROMPT}"
    fi
  fi
fi

AGENT_EXEC_CLAUDE_MODEL="${AGENT_MODEL}" bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
  "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
AGENT_PID=$!

# The wrapper beat carries an activity digest decoded from the reviewer's own
# streamed log (the tool it is running, the file it is reading, and its
# prompt-instructed narration lines).
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" &
HEARTBEAT_LOOP_PID=$!

if [[ -n "${EARLY_READY_FILE}" ]]; then
  # The implementation pass is open until the sentinel appears; starting the
  # review pass before then would close it as interrupted.
  while kill -0 "${AGENT_PID}" 2>/dev/null && [[ ! -e "${EARLY_READY_FILE}" ]]; do
    sleep "${UNIT_EARLY_POLL_SECS:-5}"
  done
  if [[ -e "${EARLY_READY_FILE}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    if ! record_pass_start; then
      echo "ERROR: unable to record the early review pass start." >&2
      kill "${AGENT_PID}" 2>/dev/null || true
      wait "${AGENT_PID}" 2>/dev/null || true
      kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
      wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
      exit 1
    fi
  fi
fi

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  # A completed early review is a real pass only once the sentinel exists: the
  # worker can beat the 5s poll to the finish, leaving the start unrecorded
  # above. Before the sentinel there is nothing to record. The implementation
  # pass is still open, so start-pass would close it as interrupted and every
  # later progress call would be refused for having no active window; and the
  # verdict is void regardless, because the final diff does not exist yet.
  if [[ "${PASS_STARTED}" -eq 0 && -e "${EARLY_READY_FILE}" ]]; then
    if ! record_pass_start; then
      echo "ERROR: unable to record the review pass start." >&2
      exit 1
    fi
  fi
  write_review_status reviewed
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
      echo "ERROR: unable to record the review pass completion." >&2
      exit 1
    fi
    PASS_STARTED=0
  fi
  if [[ -n "${EARLY_READY_FILE}" && ! -e "${EARLY_READY_FILE}" ]]; then
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${BEAT_TAG} agent finished before delivery — no pass recorded, verdict void" || true
  else
    if [[ ${#FAST_TRACK_COMMITS[@]} -gt 0 ]]; then
      if ! (cd "${WORKING_DIR}" && "$PY" "${FAST_TRACK_SCRIPT}" --mark-reviewed \
        --unit "${FAST_TRACK_UNIT}" --pass "${PHASE_ID}/${PASS_INDEX}/${LENS}" "${FAST_TRACK_COMMITS[@]}"); then
        echo "ERROR: unable to record the fast-track review completion." >&2
        exit 1
      fi
    fi
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  fi
  board_post "done" \
    "launcher: ${LENS} review finished; findings in review_findings_${PASS_INDEX}${SUFFIX}.txt"
else
  # An early reviewer that failed before its pass started recorded nothing;
  # there is no pass to close.
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      && PASS_STARTED=0 \
      || echo "ERROR: unable to record the review pass error." >&2
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  board_post blocked \
    "launcher: ${LENS} review exited with code ${AGENT_CODE}; this worker is down"
  exit "${AGENT_CODE}"
fi

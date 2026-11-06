#!/usr/bin/env bash
# Launch the configured Claude worker and record its pass outcome.
# --to follows an existing worker. --phase selects its artifact directory.
# Each team slot has a status, summary, log, and resolved model record.
# The shared board and heartbeat describe each worker during its pass.

set -euo pipefail
LAUNCHER_COMMAND=(bash "${BASH_SOURCE[0]}" "$@")
"${BASH_SOURCE[0]%/*}/../lib/py" "${BASH_SOURCE[0]%/*}/../direct/assign_gate.py"
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"

FOLLOW_TO=""
PHASE_ID=""
FIX_CAUSE=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --to) FOLLOW_TO="${2:?Usage: implement.sh --to <worker> <session_dir> ...}" ;;
    --phase) PHASE_ID="${2:?--phase requires a phase id}" ;;
    --fix-cause) FIX_CAUSE="${2:?--fix-cause requires a failed gate or defect}" ;;
    *) echo "ERROR: unknown implement option '$1'." >&2; exit 2 ;;
  esac
  shift 2
done

SESSION_DIR="${1:?Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
# Keep phase artifacts separate. Shared run records remain in the session.
ARTIFACT_DIR="${SESSION_DIR}"
if [[ -n "${PHASE_ID}" ]]; then
  PHASE_DIR_NAME="${PHASE_ID//[^[:alnum:]._-]/}"
  if [[ -z "${PHASE_DIR_NAME}" || "${PHASE_DIR_NAME}" == . || "${PHASE_DIR_NAME}" == .. ]]; then
    echo "ERROR: phase id names no directory: ${PHASE_ID}" >&2
    exit 2
  fi
  ARTIFACT_DIR="${SESSION_DIR}/phases/${PHASE_DIR_NAME}"
fi
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${ARTIFACT_DIR}/implementation_prompt.md}"
SUBTASK="${4:-impl}"
ROLE_DESC="${5:-work order at ${PROMPT_FILE}}"
PASS_KIND="${6:-}"
PASS_ACTIVITY="${7:-${ROLE_DESC%%$'\n'*}}"
FIX_PASS="${8:-0}"
TEAM_ROLE="${9:?Usage: implement.sh needs a team_role (impl, test, review) as its 9th argument}"

# Require the opening role before the recorder starts a pass.
case "${PASS_KIND}" in
  impl|test|fix|review) ;;
  *)
    echo "ERROR: pass_kind must be impl, test, fix, or review; got '${PASS_KIND}'." >&2
    echo "It is the 6th argument and the worker's opening role. A team dispatch gives one to every worker." >&2
    exit 2
    ;;
esac

if [[ -n "${UNIT_RESOLVES_ROUND+set}" ]]; then
  RESOLVES_ROUND="${UNIT_RESOLVES_ROUND}"
elif [[ "${PASS_KIND}" == "fix" && "${TEAM_ROLE}" == "impl" ]]; then
  RESOLVES_ROUND=1
else
  RESOLVES_ROUND=0
fi
TASK="direct.${SUBTASK}"

if [[ ! "${TEAM_ROLE}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; then
  echo "ERROR: team_role must be alphanumeric/_- and 1-32 chars; got '${TEAM_ROLE}'." >&2
  exit 2
fi
SLOT="_${TEAM_ROLE}"
BEAT_TAG="${SUBTASK}:${TEAM_ROLE}"
BOARD_AGENT="${TEAM_ROLE}"
export UNIT_TEAM_ROLE="${TEAM_ROLE}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MESH_PREFIX="${10:-}"
LEGACY_PREFIX="$(basename "${SESSION_DIR}")"
if [[ -z "${MESH_PREFIX}" ]] && grep -qF "mesh=${LEGACY_PREFIX}-" "${SESSION_DIR}/board.log" 2>/dev/null; then
  MESH_PREFIX="${LEGACY_PREFIX}"
fi
if [[ -n "${FOLLOW_TO}" ]]; then
  MESH_NAME="${FOLLOW_TO}"
else
  MESH_NAME="$(bash "${SCRIPT_DIR}/worker_name.sh" "${WORKING_DIR}" "${TEAM_ROLE}" "${PASS_KIND}" "${MESH_PREFIX}")"
fi

PY="${SCRIPT_DIR}/../lib/py"
SUMMARY_FILE="${ARTIFACT_DIR}/impl_summary${SLOT}.txt"
STATUS_FILE="${ARTIFACT_DIR}/impl_status${SLOT}"
LOG_FILE="${ARTIFACT_DIR}/impl_agent${SLOT}.log"
AGENT_FILE="${ARTIFACT_DIR}/impl_agent${SLOT}"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
FINDINGS_HELPER="${SCRIPT_DIR}/findings.py"
FINDINGS_STATE="${SESSION_DIR}/findings_state.json"
HEARTBEAT_INTERVAL_SECS=60

source "${SCRIPT_DIR}/../agents/models_config.sh"
models_launch_configuration || exit 1
agents_resolve_claude_launcher "${TASK}" implement.sh || exit $?

# Lock identity checks and claim writes.
worker_record() {
  "$PY" - "${1}" "${SESSION_DIR}" "${TEAM_ROLE}" "${MESH_NAME}" \
  "${AGENT_MODEL}" "${SCRIPT_DIR}" "${PASS_KIND}" "${PROMPT_FILE}" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

action, root_text, slot, name, model, script_dir, pass_kind, prompt_text = sys.argv[1:]
root = Path(root_text)
claim_file = root / f"impl_claim_{slot}.json"
sys.path.insert(0, str(Path(script_dir).resolve().parents[1]))
from scripts.production import staff, session_staff, worker_admission
sys.path.insert(0, str(Path(script_dir)))
from worker_state import worker_claim_lock, worker_record_path, worker_status_path

def fail(message):
    print(f"implement.sh: {message}", file=sys.stderr)
    raise SystemExit(2)

def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}

if action == "admit":
    try:
        worker_admission.admit(name, root)
    except worker_admission.AdmissionRefused as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0)

with worker_claim_lock(root):
    identity_file = worker_record_path(root, name, "fix" if pass_kind == "fix" else slot)
    snapshot = staff.local_snapshot()
    member = worker_admission.member(root, name, snapshot)
    reference = member.id if member else ""
    worker_id = member.current_session if member else ""
    if action == "switch":
        if not member:
            raise SystemExit(1)
        if session_staff.worker_family(snapshot, member.id) != "claude":
            raise SystemExit(0)
        raise SystemExit(0 if worker_admission.dispatch_pending(root, name, Path(prompt_text)) else 1)
    elif action == "record":
        if not worker_id:
            raise SystemExit(1)
        identity = {"staff_id": reference, "slot": slot, "model": model}
        identity_file.write_text(json.dumps(identity))
    elif action == "claim":
        identity = read(identity_file)
        expected = {"staff_id": reference, "slot": slot, "model": model}
        if not identity and reference:
            identity_file.write_text(json.dumps(expected))
        print(model)
        status_path = worker_status_path(root, slot)
        claim = read(claim_file)
        pid = claim.get("pid", 0)
        stale_claim = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                stale_claim = True
                claim_file.unlink(missing_ok=True)
            else:
                fail(f"{name} is claimed by launcher {pid}")
        if stale_claim or (status_path.exists() and status_path.read_text().strip() == "implementing"):
            status_path.write_text("error\n")
        claude = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
        try:
            result = subprocess.run([claude, "agents", "--json"], capture_output=True, text=True)
        except OSError:
            fail("cannot read claude agents")
        try:
            rows = json.loads(result.stdout)
        except ValueError:
            fail("cannot read claude agents")
        row = next((row for row in rows if row.get("sessionId") == worker_id), None)
        if row is None or row.get("status") != "idle":
            fail(f"{name} is busy or gone")
        claim_file.write_text(json.dumps({"pid": int(os.environ["FOLLOW_LAUNCHER_PID"]), "worker": name}))
    elif action == "release":
        claim = read(claim_file)
        if claim.get("pid") == int(os.environ.get("FOLLOW_LAUNCHER_PID", "0")):
            claim_file.unlink(missing_ok=True)

PY
}

worker_record admit
"$PY" "${SCRIPT_DIR}/worker_state.py" "${SESSION_DIR}"
mkdir -p "${ARTIFACT_DIR}"

if [[ -n "${FOLLOW_TO}" ]] && worker_record switch; then
  FOLLOW_TO=""
fi

if [[ -n "${FOLLOW_TO}" ]]; then
  export FOLLOW_LAUNCHER_PID="$$"
  trap 'worker_record release' EXIT
  AGENT_MODEL="$(worker_record claim)"
fi

# Claim the phase files before its worker can edit them.
if [[ -n "${PHASE_ID}" ]] && ! "$PY" "${SCRIPT_DIR}/phase_reservation.py" claim \
  --session-dir "${SESSION_DIR}" --phase "${PHASE_ID}" --repo "${WORKING_DIR}" >>"${LOG_FILE}" 2>&1; then
  echo "ERROR: phase ${PHASE_ID} could not claim its files before its worker opens; see ${LOG_FILE}." >&2
  # Keep the failed attempt because the worker can leave partial edits.
  echo "error" > "${STATUS_FILE}"
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
    "launcher: phase ${PHASE_ID} could not claim its files; its worker did not open" || true
  exit 1
fi

: > "${SUMMARY_FILE}"
echo "implementing" > "${STATUS_FILE}"

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -f "${PROGRESS_STATE}" ]]; then
  if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind "${PASS_KIND}" \
    --prompt-file "${PROMPT_FILE}" \
    --fix-cause "${FIX_CAUSE}" \
    --fix-pass "${FIX_PASS}" \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --worker-name "${MESH_NAME}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}" \
    --launcher-command "$("$PY" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "${LAUNCHER_COMMAND[@]}")" \
    ${PHASE_ID:+--phase-id "${PHASE_ID}"}; then
    echo "ERROR: unable to record the ${PASS_KIND} pass start." >&2
    echo "error" > "${STATUS_FILE}"
    bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
      "launcher: unable to record the ${PASS_KIND} pass start" || true
    exit 1
  fi
fi

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${BEAT_TAG} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

# Record the opening role. Later board role changes remain authoritative.
ROLE_FIELD="role=${PASS_KIND}; "
if [[ -n "${FOLLOW_TO}" ]]; then
  LAUNCH_NOTE="follow-up to ${FOLLOW_TO}"
else
  LAUNCH_NOTE="launcher up"
fi
bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" register \
  "${SUBTASK} ${LAUNCH_NOTE} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); mesh=${MESH_NAME}; reach=SendMessage; ${ROLE_FIELD}status in impl_status${SLOT}" || true

export UNIT_RUN_DIR="${SESSION_DIR}"
# Keep temporary test files under this run for later cleanup.
BUILD_FOLDERS_SCRIPT="${SCRIPT_DIR}/../lib/build_folders.py"
TMPDIR="$("$PY" "$BUILD_FOLDERS_SCRIPT" run-temp-path "$SESSION_DIR")"
if [[ ! -d "$TMPDIR" ]]; then
    "$PY" "$BUILD_FOLDERS_SCRIPT" stable --run-temp "$SESSION_DIR" >/dev/null
fi
export TMPDIR

if [[ -n "${FOLLOW_TO}" ]]; then
  FOLLOW_MESSAGE="${ARTIFACT_DIR}/impl_follow_message${SLOT}.$$.txt"
  cat "${PROMPT_FILE}" > "${FOLLOW_MESSAGE}"
  printf '\n\nAs your last act, write impl_summary%s.txt, then post done on the board.\n' "${SLOT}" >> "${FOLLOW_MESSAGE}"
  AGENT_BG_ROLE="${PASS_KIND}" bash "${SCRIPT_DIR}/../agents/agent_bg.sh" --attach --run "${SESSION_DIR}" \
    "${MESH_NAME}" "${WORKING_DIR}" "${FOLLOW_MESSAGE}" "${SUMMARY_FILE}" \
    "${LOG_FILE}" "${AGENT_MODEL}" &
else
  AGENT_BG_ROLE="${PASS_KIND}" AGENT_BG_WORKERS=1 AGENT_BG_EFFORT="${AGENT_EFFORT:-}" bash "${SCRIPT_DIR}/../agents/agent_bg.sh" --run "${SESSION_DIR}" \
    "${MESH_NAME}" "${WORKING_DIR}" "${PROMPT_FILE}" "${SUMMARY_FILE}" \
    "${LOG_FILE}" "${AGENT_MODEL}" &
fi
AGENT_PID=$!
if [[ -z "$FOLLOW_TO" ]]; then
  for ((worker_wait=0; worker_wait<${UNIT_WORKER_WAIT_ATTEMPTS:-30}; worker_wait++)); do
    if worker_record record >/dev/null 2>&1; then
      break
    fi
    kill -0 "$AGENT_PID" 2>/dev/null || break
    [[ "$(ps -o stat= -p "$AGENT_PID" 2>/dev/null || true)" == Z* ]] && break
    sleep "${UNIT_WORKER_POLL_SECS:-1}"
  done
fi

# Include current log activity while the worker process runs.
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?
if [[ -n "${FOLLOW_TO}" ]]; then
  rm -f "${FOLLOW_MESSAGE}"
else
  worker_record record 2>/dev/null || true
fi

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  if [[ -f "${PROGRESS_STATE}" ]]; then
    if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
      echo "ERROR: unable to record the ${PASS_KIND} pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
        "launcher: unable to record the ${PASS_KIND} pass completion" || true
      exit 1
    fi
  fi
  # Record the repair result before the launcher exits.
  if [[ "${RESOLVES_ROUND}" == "1" && -f "${FINDINGS_STATE}" ]]; then
    if ! "$PY" "${FINDINGS_HELPER}" landed --session-dir "${SESSION_DIR}" \
        ${PHASE_ID:+--phase "${PHASE_ID}"}; then
      echo "ERROR: unable to record the repair round as landed." >&2
      echo "error" > "${STATUS_FILE}"
      bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
        "launcher: unable to record the repair round as landed" || true
      exit 1
    fi
  fi
  # Record completion so peers do not depend on a final worker message.
  echo "implemented" > "${STATUS_FILE}"
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" done \
    "launcher: ${SUBTASK} finished; summary at impl_summary${SLOT}.txt" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
else
  LEDGER_CODE=0
  if [[ -f "${PROGRESS_STATE}" ]]; then
    UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      || { echo "ERROR: unable to record the ${PASS_KIND} pass error." >&2; LEDGER_CODE=1; }
  fi
  if [[ "${RESOLVES_ROUND}" == "1" && -f "${FINDINGS_STATE}" ]]; then
    "$PY" "${FINDINGS_HELPER}" abandon --session-dir "${SESSION_DIR}" --edits-landed \
      ${PHASE_ID:+--phase "${PHASE_ID}"} --reason "the ${SUBTASK} worker exited with code ${AGENT_CODE}" \
      || { echo "ERROR: unable to record the repair round as abandoned." >&2; LEDGER_CODE=1; }
  fi
  echo "error" > "${STATUS_FILE}"
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
    "launcher: ${SUBTASK} exited with code ${AGENT_CODE}; this worker is down; ledger_error=${LEDGER_CODE}" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
  exit "${AGENT_CODE}"
fi

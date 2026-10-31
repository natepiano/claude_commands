#!/usr/bin/env bash
# review.sh — Invoke the configured worker agent for a review.
#
# Usage: review.sh [--to <worker> | --slot <new-slot>]
#                  [--phase <id>] <session_dir>
#                  [working_dir] [prompt_file] [task]
#                  [role_description] [pass_activity] [pass_index] [lens]
#                  [early_ready_file]
#   --to reuses an open Codex worker. --slot opens a role-named Codex worker for
#   this review and leaves its thread open for later `--to` dispatches.
#   With no worker or pool flags, a fresh read-only reviewer runs once.
#   Ignored --pool code / --lease flags select the unit's own reviewer when
#   neither --to nor --slot names a worker.
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
#   <session_dir>/review_agent_<N>.log     — full agent log for pass N; a worker's
#                                            pass adds `-<slot>`, so
#                                            two workers on one pass keep separate logs
#   <session_dir>/review_findings.txt      — symlink to the current pass's findings
#   <session_dir>/review_agent.log         — symlink to the current pass's log
#   <session_dir>/review_agent         — resolved task, family, agent, and effort
#   <session_dir>/heartbeat.log        — shared with implement.sh: role header at
#                                        start + [wrapper] beats every 60s, each
#                                        carrying an activity digest decoded from
#                                        the reviewer's streamed log. Fresh
#                                        reviewers are read-only; assigned reviewers
#                                        are checked for an unchanged tree after
#                                        their turn, less any path another open
#                                        phase's files list holds (review_tree.py).

set -euo pipefail
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"

FOLLOW_TO=""
NEW_SLOT=""
POOL_TYPE=""
POOL_LEASE=""
# A phase running beside the current one names itself, so start-pass stamps this pass with it.
PHASE_ID=""
REQUESTED_LENS=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --to) FOLLOW_TO="${2:?--to requires a worker}" ;;
    --slot) NEW_SLOT="${2:?--slot requires a slot}" ;;
    --pool) POOL_TYPE="${2:?--pool requires a type}" ;;
    --lease) POOL_LEASE="${2:?--lease requires slug/unit/phase}" ;;
    --lens) REQUESTED_LENS="${2:?--lens requires a lens}" ;;
    --phase) PHASE_ID="${2:?--phase requires a phase id}" ;;
    *) echo "ERROR: unknown review option '$1'." >&2; exit 2 ;;
  esac
  shift 2
done
if [[ -n "${FOLLOW_TO}" && -n "${NEW_SLOT}" ]]; then
  echo "ERROR: --to excludes --slot." >&2
  exit 2
fi
if [[ "${POOL_TYPE}" == ui ]]; then
  echo 'ERROR: --pool ui is retired; use the fresh Claude helper in <UXReview/> for the design check.' >&2
  exit 2
fi
if [[ -n "${POOL_TYPE}" && "${POOL_TYPE}" != code ]]; then
  echo "ERROR: --pool requires code or ui." >&2
  exit 2
fi
if [[ -n "${POOL_TYPE}" || -n "${POOL_LEASE}" ]]; then
  echo 'review.sh: --pool code and --lease are ignored; using the unit reviewer.' >&2
fi

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
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${ARTIFACT_DIR}/review_prompt.md}"
SUBTASK="${4:-review}"
ROLE_DESC="${5:-blind review of the current diff against its spec}"
PASS_ACTIVITY="${6:-reviewing the current diff against its work order}"
PASS_INDEX="${7:-1}"
LENS="${REQUESTED_LENS:-${8:-}}"
LENS_ARGS=()
if [[ -n "${LENS}" ]]; then
  LENS_ARGS=(--lens "${LENS}")
fi
EARLY_READY_FILE="${9:-}"
TASK="direct.${SUBTASK}"

case "${PASS_INDEX}" in
  ''|*[!0-9]*|0) PASS_INDEX=1 ;;
esac

# A closed set, like the pass kinds: an unrecognized lens would suffix artifacts
# nobody reads and record a pass under a worker no column renders, both silently.
LENS_SLOT="$("${BASH_SOURCE[0]%/*}/../lib/py" - "${BASH_SOURCE[0]}" "${LENS}" <<'PYLENS'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))
from scripts.usage.agents_log import LENSES
lens = sys.argv[2]
if lens and lens not in LENSES:
    print(f"ERROR: unknown review lens '{lens}' ({', '.join(LENSES)})", file=sys.stderr)
    sys.exit(2)
print({"adversary": "test", "contract": "impl", "": ""}.get(lens, "review"))
PYLENS
)"

# An explicit worker keeps its slot. A fresh reviewer uses the lens slot.
if [[ -n "${FOLLOW_TO}" ]]; then
  TEAM_SLOT="${FOLLOW_TO##*-}"
  if [[ ! "${TEAM_SLOT}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; then
    echo "ERROR: cannot derive a valid team slot from review worker '${FOLLOW_TO}'." >&2
    exit 2
  fi
elif [[ -n "${NEW_SLOT}" ]]; then
  TEAM_SLOT="${NEW_SLOT}"
  if [[ ! "${TEAM_SLOT}" =~ ^[A-Za-z][A-Za-z0-9_]{0,31}$ ]]; then
    echo "ERROR: new review slot must be role-named and use 1-32 alphanumeric/_ characters; got '${NEW_SLOT}'." >&2
    exit 2
  fi
else
  TEAM_SLOT="${LENS_SLOT}"
fi

SUFFIX="${LENS:+_${LENS}}"
# Give each lens a separate tag in the shared activity log.
BEAT_TAG="${SUBTASK}${LENS:+:${LENS}}"
# The worker this pass records under, so concurrent reviewers key separate pass
# records instead of closing another worker's pass. Replace any inherited slot;
# ordinary allocation sets the selected review slot after the launch check.
export UNIT_TEAM_ROLE="${TEAM_SLOT}"
export UNIT_RUN_DIR="${SESSION_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${FOLLOW_TO}" ]]; then
  REVIEW_WORKER="${FOLLOW_TO}"
elif [[ -n "${NEW_SLOT}" ]]; then
  REVIEW_WORKER="$(bash "${SCRIPT_DIR}/worker_name.sh" "${WORKING_DIR}" "${NEW_SLOT}" review)"
else
  REVIEW_WORKER=""
fi

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.11.
PY="${SCRIPT_DIR}/../lib/py"
MESH_SESSION_DIR="${SESSION_DIR}"
MESH_CLAIMED=0
TURN_THREAD_ID=""
LEASE_ORDINAL=0
PRIME_HASH=""
TURN_FINDINGS_READY=0
TURN_RAISED=0
TURN_COUNT_READY=0
TURN_RECORD_STATUS="error"
mkdir -p "${ARTIFACT_DIR}"
# Every dispatch names the run's director in the run folder (unit_runs.py), which the
# production tree reads: a marker names one run per director, and a director runs phases side by side.
"$PY" "${SCRIPT_DIR}/../lib/unit_runs.py" record-director "${SESSION_DIR}" || true
FINDINGS_FILE="${ARTIFACT_DIR}/review_findings_${PASS_INDEX}${SUFFIX}.txt"
STATUS_FILE="${ARTIFACT_DIR}/review_status${SUFFIX}"
SLOT_FILE="${ARTIFACT_DIR}/review_slot${SUFFIX}"
rm -f "${SLOT_FILE}"
# Each worker logs on its own: a second worker taking the same pass and lens would
# otherwise truncate the first one's log, and with it the reason that worker failed.
if [[ -n "${REVIEW_WORKER}" ]]; then
  SEAT_SUFFIX="-${TEAM_SLOT}"
else
  SEAT_SUFFIX=""
fi
LOG_FILE="${ARTIFACT_DIR}/review_agent_${PASS_INDEX}${SUFFIX}${SEAT_SUFFIX}.log"
REPLY_FILE="${ARTIFACT_DIR}/review_reply_${PASS_INDEX}${SUFFIX}.$$.txt"
TREE_BASELINE_FILE="${ARTIFACT_DIR}/review_tree_${PASS_INDEX}${SUFFIX}.$$.json"

AGENT_FILE="${ARTIFACT_DIR}/review_agent${SUFFIX}"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
HEARTBEAT_INTERVAL_SECS=60

# Validate before worker allocation, compaction or any reviewer starts. UX has no
# pass window, so this command also records its launch evidence.
if [[ -f "${PROGRESS_STATE}" ]]; then
  if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" check-launch \
    --session-dir "${SESSION_DIR}" --pass-kind review \
    --prompt-file "${PROMPT_FILE}" --lens "${LENS}" \
    ${PHASE_ID:+--phase-id "${PHASE_ID}"}; then
    echo "ERROR: unable to check the review launch." >&2
    echo "error" > "${STATUS_FILE}"
    exit 1
  fi
fi

if [[ -z "${FOLLOW_TO}" && -z "${NEW_SLOT}" && ( -n "${POOL_TYPE}" || -n "${POOL_LEASE}" ) ]]; then
  RESOLVE_ERROR_FILE="${ARTIFACT_DIR}/review_resolve${SUFFIX}.$$.txt"
  if FOLLOW_TO="$("$PY" "${SCRIPT_DIR}/workers.py" resolve --session-dir "${SESSION_DIR}" --working-dir "${WORKING_DIR}" --role review --owner-pid "$$" 2>"${RESOLVE_ERROR_FILE}")"; then
    rm -f "${RESOLVE_ERROR_FILE}"
  else
    RESOLVE_CAUSE="$(tr '\n' ' ' < "${RESOLVE_ERROR_FILE}")"
    rm -f "${RESOLVE_ERROR_FILE}"
    echo "error" > "${STATUS_FILE}"
    echo "ERROR: no unit reviewer: ${RESOLVE_CAUSE% }" >&2
    exit 1
  fi
  REVIEW_WORKER="${FOLLOW_TO}"
  TEAM_SLOT="${FOLLOW_TO##*-}"
  export UNIT_TEAM_ROLE="${TEAM_SLOT}"
fi
# Resolution can change the worker slot after the launch check.
if [[ -n "${REVIEW_WORKER}" ]]; then
  SEAT_SUFFIX="-${TEAM_SLOT}"
else
  SEAT_SUFFIX=""
fi
LOG_FILE="${ARTIFACT_DIR}/review_agent_${PASS_INDEX}${SUFFIX}${SEAT_SUFFIX}.log"

initialize_review_artifacts() {
  # Every reader of the unnumbered paths — the unit director's mid-run
  # preemption read, the heartbeat digest — keeps working while the per-pass
  # history is kept.
  ln -sfn "review_findings_${PASS_INDEX}${SUFFIX}.txt" "${ARTIFACT_DIR}/review_findings${SUFFIX}.txt"
  ln -sfn "review_agent_${PASS_INDEX}${SUFFIX}${SEAT_SUFFIX}.log" "${ARTIFACT_DIR}/review_agent${SUFFIX}.log"
  rm -f "${REPLY_FILE}"
  if [[ -n "${REVIEW_WORKER}" ]]; then
    : > "${FINDINGS_FILE}"
    TURN_FINDINGS_READY=1
  fi
  echo "reviewing" > "${STATUS_FILE}"
  printf '%s\n' "${TEAM_SLOT}" > "${SLOT_FILE}"
  # Only meaningful while an early launch is showing in the progress report,
  # and read there only if written after the review was armed.
  echo "$$" > "${ARTIFACT_DIR}/review_pid${SUFFIX}"
}

# Each review names its persistent worker on the unit board.
board_post() {
  [[ -n "${TEAM_SLOT}" ]] || return 0
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${TEAM_SLOT}" "$1" "$2" || true
}

claim_follow() {
  "$PY" - "${MESH_SESSION_DIR}" "${FOLLOW_TO}" "${SCRIPT_DIR}/../agents/codex_mesh.py" \
    "${BOARD_HELPER}" "${COMPACT_ABOVE_TOKENS}" "${LENS}" "${SESSION_DIR}" "${TEAM_SLOT}" <<'PY'
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

root_text, name, mesh_script, board_helper, compact_above_text, lens, unit_dir, slot = sys.argv[1:]
root = Path(root_text)
compact_above = int(compact_above_text)

def read_roster():
    try:
        value = json.loads((root / "mesh_roster.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

def post(message):
    subprocess.run(
        ["bash", board_helper, "post", unit_dir, slot,
         "status", message],
        capture_output=True,
        text=True,
    )

sys.path.insert(0, str(Path(mesh_script).parent.parent / "direct"))
from worker_state import worker_claim_lock

with worker_claim_lock(root):
    entry = read_roster().get(name, {})
    context_tokens = entry.get("context_tokens") if isinstance(entry, dict) else None
    if isinstance(context_tokens, int) and context_tokens > compact_above:
        compact = subprocess.run(
            [sys.executable, mesh_script, "compact", "--session-dir", root_text,
             "--to", name],
            capture_output=True,
            text=True,
        )
        if compact.returncode == 0:
            post(f"launcher: {compact.stdout.strip() or f'compacted {name}'}")
        else:
            detail = (compact.stderr.strip() or compact.stdout.strip()
                      or f"exit {compact.returncode}").splitlines()[-1]
            # Exit 3: a turn is still live on the worker's thread, so a follow-up would be refused.
            if compact.returncode == 3:
                post(f"launcher: {name} still has a live turn after compaction: {detail}; "
                     "follow-up not sent")
                print(f"review.sh: {name} still has a live turn after compaction; end the worker "
                      f"(codex_mesh.py end --to {name}) and start a fresh one", file=sys.stderr)
                raise SystemExit(2)
            if compact.returncode == 4:
                post(f"launcher: {name}: {detail}; follow-up not sent")
                print(f"review.sh: {name}: roster changed during compaction; follow-up not sent; "
                      f"inspect the worker and use codex_mesh.py reopen --to {name} if idle", file=sys.stderr)
                raise SystemExit(2)
            post(f"launcher: compaction failed for {name}: {detail}; continuing follow-up")
    checked = subprocess.run(
        [sys.executable, mesh_script, "can-follow", "--session-dir", root_text,
         "--to", name, "--claim-pid", os.environ["FOLLOW_LAUNCHER_PID"],
         "--role", "review", *(["--lens", lens] if lens else [])],
        capture_output=True,
        text=True,
    )
    if checked.returncode != 0:
        print(checked.stderr.strip() or f"{name} is busy or not followable", file=sys.stderr)
        raise SystemExit(checked.returncode)
PY
}

cleanup_mesh_review() {
  if [[ "${MESH_CLAIMED}" -eq 1 ]]; then
    "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" release-follow \
      --session-dir "${MESH_SESSION_DIR}" --to "${FOLLOW_TO}" \
      --claim-pid "${FOLLOW_LAUNCHER_PID}" >/dev/null 2>&1 || true
  fi
  rm -f "${REPLY_FILE}" "${TREE_BASELINE_FILE}"
}

# The tree guard keeps per-path entries, so a phase writing beside this one changes only its
# own paths and review_tree.py can leave them out (its docstring has the rule).
capture_tree_baseline() {
  if "$PY" "${SCRIPT_DIR}/review_tree.py" baseline \
    --root "${WORKING_DIR}" --out "${TREE_BASELINE_FILE}" 2>>"${LOG_FILE}"; then
    TREE_BASELINE_READY=1
  else
    TREE_BASELINE_READY=-1
  fi
}

record_new_worker_identity() {
  "$PY" - "${SESSION_DIR}" "${TEAM_SLOT}" "${REVIEW_WORKER}" \
    "${AGENT_FAMILY}" "${DURABLE_WORKER_MODEL}" "${SCRIPT_DIR}" <<'PY'
import fcntl
import json
import sys
from pathlib import Path

root_text, slot, name, family, model, helper_dir = sys.argv[1:]
sys.path.insert(0, helper_dir)
from worker_state import worker_claim_lock, worker_record_path
root = Path(root_text)
with worker_claim_lock(root):
    try:
        roster = json.loads((root / "mesh_roster.json").read_text())
    except (OSError, ValueError):
        roster = {}
    entry = roster.get(name, {}) if isinstance(roster, dict) else {}
    worker_id = entry.get("thread_id", "") if isinstance(entry, dict) else ""
    if not isinstance(worker_id, str) or not worker_id:
        raise SystemExit(1)
    identity = {
        "name": name,
        "slot": slot,
        "family": family,
        "model": model,
        "worker_id": worker_id,
    }
    worker_record_path(root, name, slot, role="review").write_text(json.dumps(identity))
PY
}

count_turn_findings() {
  [[ "${TURN_FINDINGS_READY}" -eq 1 && "${TURN_COUNT_READY}" -eq 0 ]] || return 0
  TURN_RAISED="$("$PY" - "${FINDINGS_FILE}" "${LENS}" <<'PY'
import re
import sys
from pathlib import Path
try:
    findings = Path(sys.argv[1]).read_text(encoding="utf-8")
except OSError:
    findings = ""
if sys.argv[2] == "ux":
    raised = 0
    in_defects = False
    for line in findings.splitlines():
        if not line.strip().startswith("|"):
            in_defects = False
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:]]
        if cells and not cells[-1]:
            cells.pop()
        if cells == ["Shot", "Rule", "Defect", "Fix"]:
            in_defects = True
        elif len(cells) != 4:
            in_defects = False
        elif in_defects and not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            raised += 1
    print(raised)
else:
    print(len(re.findall(r"^\s*\d+[.)]\s+", findings, re.MULTILINE)))
PY
)"
  TURN_COUNT_READY=1
}

refresh_turn_thread_id() {
  TURN_THREAD_ID="$("$PY" - "${MESH_SESSION_DIR}" "${REVIEW_WORKER}" \
    "${TURN_THREAD_ID}" "${TURN_PROGRESS}" <<'PY'
import json
import sys
from pathlib import Path
root, reviewer, known, progress = sys.argv[1:]
try:
    roster = json.loads((Path(root) / "mesh_roster.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    roster = {}
print(roster.get(reviewer, {}).get("thread_id") or known or json.loads(progress).get("thread_id", ""))
PY
)"
}

write_turn_record() {
  UNIT_LIB_DIR="${SCRIPT_DIR%/*}/lib" "$PY" - "${SESSION_DIR}" "${TURN_STARTED_AT}" "$$" "${TURN_RECORD_STATUS}" \
    "" "" "${REVIEW_WORKER}" "${TURN_THREAD_ID}" \
    "${TURN_LEASE}" "${TURN_PROGRESS}" "${LENS}" "${PASS_INDEX}" \
    "${LEASE_ORDINAL}" "${PRIME_HASH}" "${TURN_RAISED}" <<'PY'
import itertools
import json
import os
import sys
import time
from pathlib import Path

(session, started, pid, status, legacy_type, pool, reviewer, thread_id,
 lease, progress, lens, number, ordinal, prime_hash, raised) = sys.argv[1:]
metadata = json.loads(progress)
thread_id = thread_id or metadata.get("thread_id", "")
unit, phase = metadata.get("unit", ""), metadata.get("phase", "")
record = {
    "arm": "own", "pool": pool,
    "reviewer": reviewer, "thread_id": thread_id, "unit": unit, "phase": phase,
    "phase_instance_id": metadata.get("phase_instance_id", ""),
    "history_file": metadata.get("history_file", ""),
    "kind": "ux" if lens == "ux" else "broad" if int(number) == 1 and lens else "closure",
    "lens": lens, "pass": int(number), "started_at": int(started),
    "finished_at": time.time(), "lease_ordinal": int(ordinal), "prime_hash": prime_hash,
    "status": status, "raised": int(raised),
}
sys.path.insert(0, os.environ["UNIT_LIB_DIR"])
from unit_runs import history_root
root = history_root() / "review_turns" / Path(session).name
root.mkdir(parents=True, exist_ok=True)
for sequence in itertools.count():
    try:
        output = (root / f"{started}-{pid}-{sequence}.json").open("x", encoding="utf-8")
    except FileExistsError:
        continue
    with output:
        json.dump(record, output, ensure_ascii=False)
        output.write("\n")
    break
PY
}

finish_review_exit() {
  local code=$?
  trap - EXIT
  set +e
  if [[ "${code}" -ne 0 && "${TURN_FINDINGS_READY}" -eq 1 ]]; then
    echo error > "${STATUS_FILE}"
  fi
  count_turn_findings
  refresh_turn_thread_id
  if ! write_turn_record; then
    echo "ERROR: unable to write review turn record." >&2
    code=1
  fi
  cleanup_mesh_review
  exit "${code}"
}

interrupt_review() {
  TURN_RECORD_STATUS="canceled"
  # The persistent worker keeps its running turn when this wrapper stops.
  if [[ -n "${HEARTBEAT_LOOP_PID:-}" ]]; then
    kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
    wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
  fi
  count_turn_findings
  exit "$1"
}

if [[ -n "${FOLLOW_TO}" || -n "${NEW_SLOT}" ]]; then
  TURN_STARTED_AT="$(date +%s)"
  TURN_LEASE=""
  # The record joins to the phase --phase names, the same phase start-pass stamps:
  # with phases open side by side, the newest open phase is often another one.
  TURN_PROGRESS="$("$PY" - "${PROGRESS_STATE}" "${SESSION_DIR}" "${FOLLOW_TO}" "${PHASE_ID}" <<'PY'
import json
import sys
from pathlib import Path
try:
    state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    state = {}
phase = state.get("phase") or {}
named = [record for record in [phase, *(state.get("open_phases") or [])]
         if sys.argv[4] and record.get("status") == "active" and record.get("id") == sys.argv[4]]
phase = named[0] if named else phase
try:
    roster = json.loads((Path(sys.argv[2]) / "mesh_roster.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    roster = {}
print(json.dumps({"unit": state.get("unit", ""), "phase": phase.get("id", ""),
                  "phase_instance_id": phase.get("instance_id", ""),
                  "history_file": state.get("history_file", ""),
                  "thread_id": roster.get(sys.argv[3], {}).get("thread_id", "")}))
PY
)"
  trap finish_review_exit EXIT
  trap 'interrupt_review 143' TERM
  trap 'interrupt_review 130' INT
  trap 'interrupt_review 129' HUP
fi

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  if [[ -z "${REVIEW_WORKER}" ]]; then
    echo "error" > "${STATUS_FILE}"
  fi
  exit 1
fi

# implement.sh anchors a durable Codex identity to the implementation model
# configured when that worker first opens. A worker opened by review.sh must use
# the same anchor even though this turn runs the review model; otherwise the
# first role switch back to implementation rejects its own retained thread.
# shellcheck disable=SC2153  # agents_resolve sets AGENT_MODEL.
DURABLE_WORKER_MODEL="${AGENT_MODEL}"
if [[ -n "${NEW_SLOT}" ]]; then
  IMPLEMENTATION_PAIR="$(_agents_registry_get "direct.${AGENT_FAMILY}" implementation)"
  if [[ -z "${IMPLEMENTATION_PAIR}" ]]; then
    IMPLEMENTATION_PAIR="$(_agents_registry_get "direct.${AGENT_FAMILY}" impl)"
  fi
  if [[ -z "${IMPLEMENTATION_PAIR}" ]]; then
    echo "ERROR: cannot record review worker ${REVIEW_WORKER}: no implementation model is configured." >&2
    exit 2
  fi
  DURABLE_WORKER_MODEL="${IMPLEMENTATION_PAIR%%:*}"
fi

TREE_BASELINE_READY=0
TURN_IMAGE_ARGS=()

if [[ -n "${REVIEW_WORKER}" ]]; then
  CODEX_MESH="${UNIT_CODEX_MESH:-$(_agents_registry_get direct.options codex_mesh)}"
  if [[ "${AGENT_FAMILY}" != "codex" || "${CODEX_MESH}" != "1" ]]; then
    echo "ERROR: review worker ${REVIEW_WORKER} requires the Codex mesh." >&2
    exit 2
  fi
  if [[ -n "${FOLLOW_TO}" ]]; then
    DIRECT_CONFIG="${UNIT_CONFIG:-${HOME}/.claude/config/direct.conf}"
    COMPACT_ABOVE_TOKENS=""
    if [[ -r "${DIRECT_CONFIG}" ]]; then
      COMPACT_ABOVE_TOKENS="$(sed -n \
        's/^UNIT_COMPACT_ABOVE_TOKENS=\([0-9][0-9]*\).*$/\1/p' \
        "${DIRECT_CONFIG}" | tail -n 1)"
    fi
    if [[ ! "${COMPACT_ABOVE_TOKENS}" =~ ^[0-9]+$ ]] || (( COMPACT_ABOVE_TOKENS <= 0 )); then
      echo "ERROR: UNIT_COMPACT_ABOVE_TOKENS in ${DIRECT_CONFIG} must be a positive integer." >&2
      exit 2
    fi
    export FOLLOW_LAUNCHER_PID="$$"
    claim_follow
    MESH_CLAIMED=1
  fi
  if [[ -z "${EARLY_READY_FILE}" || -e "${EARLY_READY_FILE}" ]]; then
    capture_tree_baseline
  fi
fi
initialize_review_artifacts

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -n "${REVIEW_WORKER}" ]]; then
  MESH_FIELD="mesh=${REVIEW_WORKER}; reach=codex_mesh.py"
  if [[ -n "${FOLLOW_TO}" ]]; then
    LAUNCH_NOTE="follow-up to ${REVIEW_WORKER}"
  else
    LAUNCH_NOTE="new worker ${REVIEW_WORKER}"
  fi
else
  MESH_FIELD="mesh=none"
  LAUNCH_NOTE="up"
fi
board_post register \
  "${LENS} review ${LAUNCH_NOTE} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); ${MESH_FIELD}; role=review; status in review_status${SUFFIX}"

PASS_STARTED=0
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
    --worker-name "${REVIEW_WORKER}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}" \
    ${PHASE_ID:+--phase-id "${PHASE_ID}"}; then
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

# Early mode: the reviewer ends Stage 1 with provisional findings while the
# implementation still runs. A detached run for the reviewer waits until the
# sentinel exists, then sends Stage 2 as the reviewer's next message; the live
# run keeps the reviewer's launcher attached until then. It exits 1, sending
# nothing, if this script exits first.
if [[ -n "${EARLY_READY_FILE}" && -n "${REVIEW_WORKER}" ]]; then
  STAGE_TWO_FILE="${ARTIFACT_DIR}/review_stage2_${PASS_INDEX}${SUFFIX}.$$.txt"
  printf 'Stage 2: the final diff is at %s; review it per your prompt'"'"'s Stage 2.\n' \
    "${EARLY_READY_FILE%.ready}.diff" > "${STAGE_TWO_FILE}"
  # The inner script reads its own $1..$3, so the single quotes are meant (SC2016).
  # shellcheck disable=SC2016
  if ! "$PY" "${MESH_RUNS_SCRIPT:-${SCRIPT_DIR}/../agents/mesh_runs.py}" start \
    --session-dir "${MESH_SESSION_DIR}" --to "${REVIEW_WORKER}" --director-dir "${SESSION_DIR}" \
    --label "final diff ready" --done-message-file "${STAGE_TWO_FILE}" -- \
    bash -c 'while [[ ! -e "$1" ]]; do kill -0 "$2" 2>/dev/null || [[ -e "$1" ]] || exit 1; sleep "$3"; done' \
    stage-two "${EARLY_READY_FILE}" "$$" "${UNIT_EARLY_POLL_SECS:-5}" >>"${LOG_FILE}" 2>&1; then
    echo "ERROR: could not start the Stage 2 run for ${REVIEW_WORKER}; see ${LOG_FILE}." >&2
  fi
fi

TURN_WORKTREE_ARGS=(--cwd "${WORKING_DIR}" --read-only)
if [[ -n "${FOLLOW_TO}" ]]; then
  "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" follow \
    --session-dir "${MESH_SESSION_DIR}" --to "${FOLLOW_TO}" \
    --claim-pid "${FOLLOW_LAUNCHER_PID}" --role review "${LENS_ARGS[@]}" "${TURN_WORKTREE_ARGS[@]}" "${TURN_IMAGE_ARGS[@]}" \
    --message-file "${PROMPT_FILE}" --summary-file "${FINDINGS_FILE}" \
    --reply-file "${REPLY_FILE}" --log-file "${LOG_FILE}" \
    --task "${TASK}" --service-tier "${AGENT_SERVICE_TIER:-}" &
elif [[ -n "${NEW_SLOT}" ]]; then
  "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" start \
    --session-dir "${SESSION_DIR}" --name "${REVIEW_WORKER}" --role review "${LENS_ARGS[@]}" \
    --cwd "${WORKING_DIR}" --sandbox read-only --prompt-file "${PROMPT_FILE}" \
    --summary-file "${FINDINGS_FILE}" --reply-file "${REPLY_FILE}" \
    --log-file "${LOG_FILE}" --task "${TASK}" --service-tier "${AGENT_SERVICE_TIER:-}" &
else
  bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
    "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
fi
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
  if [[ -e "${EARLY_READY_FILE}" && "${TREE_BASELINE_READY}" -eq 0 ]]; then
    capture_tree_baseline
  fi
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

if [[ -n "${NEW_SLOT}" ]] && ! record_new_worker_identity; then
  echo "ERROR: review worker ${REVIEW_WORKER} has no durable worker identity." >&2
  AGENT_CODE=1
fi

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

TREE_CHANGED=0
CHANGED_PATHS=""
if [[ -n "${REVIEW_WORKER}" && "${TREE_BASELINE_READY}" -ne 0 ]]; then
  if [[ "${TREE_BASELINE_READY}" -eq 1 ]] && CHANGED_PATHS="$("$PY" "${SCRIPT_DIR}/review_tree.py" changes \
    --root "${WORKING_DIR}" --baseline "${TREE_BASELINE_FILE}" \
    --session-dir "${SESSION_DIR}" --phase "${PHASE_ID}" 2>>"${LOG_FILE}")"; then
    CHANGED_PATHS="$(tr '\n' ' ' <<<"${CHANGED_PATHS}" | sed 's/[[:space:]]*$//')"
  else
    CHANGED_PATHS="the tree check failed (see ${LOG_FILE})"
  fi
  if [[ -n "${CHANGED_PATHS}" ]]; then
    TREE_CHANGED=1
    echo "ERROR: review worker ${REVIEW_WORKER} changed the working tree: ${CHANGED_PATHS}" >&2
  fi
  rm -f "${TREE_BASELINE_FILE}"
fi

# The durable record stores this count before finish-pass closes the pass.
if [[ -n "${FOLLOW_TO}" || -n "${NEW_SLOT}" ]]; then
  count_turn_findings
fi

if [[ "${AGENT_CODE}" -eq 0 && "${TREE_CHANGED}" -eq 0 ]]; then
  # A completed early review is a real pass only once the sentinel exists: the
  # worker can beat the 5s poll to the finish, leaving the start unrecorded
  # above. Before the sentinel there is nothing to record. The implementation
  # pass is still open, so start-pass would close it as interrupted and every
  # later progress call would be refused for having no active window; and the
  # verdict is void regardless, because the final diff does not exist yet.
  if [[ "${PASS_STARTED}" -eq 0 && -e "${EARLY_READY_FILE}" ]]; then
    if ! record_pass_start; then
      echo "ERROR: unable to record the review pass start." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  echo "reviewed" > "${STATUS_FILE}"
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
      echo "ERROR: unable to record the review pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  if [[ -n "${EARLY_READY_FILE}" && ! -e "${EARLY_READY_FILE}" ]]; then
    TURN_RECORD_STATUS="void"
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${BEAT_TAG} agent finished before delivery — no pass recorded, verdict void" || true
  else
    TURN_RECORD_STATUS="reviewed"
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  fi
  board_post "done" \
    "launcher: ${LENS} review finished; findings in review_findings_${PASS_INDEX}${SUFFIX}.txt"
else
  echo "error" > "${STATUS_FILE}"
  # An early reviewer that failed before its pass started recorded nothing;
  # there is no pass to close.
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      || echo "ERROR: unable to record the review pass error." >&2
  fi
  if [[ "${TREE_CHANGED}" -eq 1 ]]; then
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${BEAT_TAG} review worker ${REVIEW_WORKER} changed the working tree: ${CHANGED_PATHS}" || true
    board_post blocked \
      "launcher: review worker ${REVIEW_WORKER} changed the working tree: ${CHANGED_PATHS}; findings kept in review_findings_${PASS_INDEX}${SUFFIX}.txt"
    exit 1
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  board_post blocked \
    "launcher: ${LENS} review exited with code ${AGENT_CODE}; this worker is down"
  exit "${AGENT_CODE}"
fi

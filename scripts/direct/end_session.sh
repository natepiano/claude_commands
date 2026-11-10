#!/usr/bin/env bash
# end_session.sh — Mark this director's unit run finished.
#
# Usage: end_session.sh
#
# Clears the run pointer that prepare_session.sh stores in staff.
# Cleans each run folder that this session directs.

# Keeps workers and app-servers while their unit director is live. Otherwise,
# pauses Codex workers and stops this run's servers after preserving queues.
# Removes only Claude workers no longer held by a unit or live director.
# Removes the unit's idle standing trees and workspaces.
#
# Deletes each run's scratch copies (remove_run_scratch.py): its verify.sh pass
# records (verify_cache/), whose saved time is already in
# ~/.local/state/verify/saved.jsonl, and every git checkout and cargo target in
# the run folder, at any depth, except any non-retired worker's live command cwd and
# its containing checkout. Tracked run scratch follows the same retention rule.
#
# UNIT_RUN_ROOT, set by tests, replaces both run roots for the scan.
#
# Safe to run when no run is active. The rest of the session directory is left
# alone; ledgers, heartbeat and worker logs stay readable after the run.

set -euo pipefail
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.12.
PY="${BASH_SOURCE[0]%/*}/../lib/py"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DIRECTOR_SESSION_ID="${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-}}"
if [[ -z "${DIRECTOR_SESSION_ID}" ]]; then
  echo "No director session id in the environment; nothing to clear."
  exit 0
fi

STATUS=0
cleanup() {
  if "$@"; then
    return 0
  else
    echo "end_session: failed: $*" >&2
    STATUS=1
  fi
}

# Read the current pointer before and after cleanup.
read_run_pointer() {
  "$PY" -c 'import sys; from scripts.lib import unit_runs; run = unit_runs.open_run(sys.argv[1]); print(run if run is not None else "")' "${DIRECTOR_SESSION_ID}"
}

# Keep the open run available if the run scan fails.
SESSION_DIR=""
if SESSION_DIR="$(read_run_pointer)"; then
  :
else
  echo "end_session: failed: read run pointer" >&2
  STATUS=1
fi
RUNS=()
if [[ -n "${SESSION_DIR}" ]]; then
  RUNS+=("${SESSION_DIR}")
fi
HELD_ARGS=(held "${DIRECTOR_SESSION_ID}")
if [[ -n "${UNIT_RUN_ROOT:-}" ]]; then
  HELD_ARGS+=(--root "${UNIT_RUN_ROOT}")
fi
if HELD="$("$PY" "${SCRIPT_DIR}/../lib/unit_runs.py" "${HELD_ARGS[@]}")"; then
  :
else
  echo "end_session: failed: unit_runs.py held" >&2
  STATUS=1
fi
while IFS= read -r run; do
  if [[ -n "${run}" && "${run}" != "${SESSION_DIR}" ]]; then
    RUNS+=("${run}")
  fi
done <<< "${HELD}"

if (( ${#RUNS[@]} > 0 )); then
  REMOVE_ARGS=()
  for run in "${RUNS[@]}"; do
    REMOVE_ARGS+=(--session-dir "${run}")
  done
  for run in "${RUNS[@]}"; do
    cleanup "$PY" "${SCRIPT_DIR}/../astra/design_roles.py" unbind-run "${run}"
  done
  cleanup "$PY" "${SCRIPT_DIR}/remove_workers.py" "${REMOVE_ARGS[@]}"
  cleanup "$PY" "${SCRIPT_DIR}/remove_run_scratch.py" --tracked "${RUNS[@]}"
  # remove_run_scratch also ends tracked run scratch while keeping live commands' cwd.
  for run in "${RUNS[@]}"; do
    cleanup "$PY" "${SCRIPT_DIR}/../production/staff.py" clear-run "${DIRECTOR_SESSION_ID}" "${run}"
  done
fi

"$PY" "${SCRIPT_DIR}/../copies/copies.py" remove || true

if [[ -n "${SESSION_DIR}" ]]; then
  if CURRENT_RUN="$(read_run_pointer)"; then
    if [[ -z "${CURRENT_RUN}" ]]; then
      echo "Unit run ended; run pointer cleared."
    else
      echo "Unit run pointer remains."
    fi
  else
    echo "end_session: failed: read run pointer" >&2
    STATUS=1
  fi
else
  echo "No active unit run pointer for this session."
fi
exit "${STATUS}"

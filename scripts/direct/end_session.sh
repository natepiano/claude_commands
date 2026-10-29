#!/usr/bin/env bash
# end_session.sh — Mark this Claude session's unit run finished.
#
# Usage: end_session.sh
#
# Removes the run-active marker written by prepare_session.sh. Until it is gone,
# stop-direct-continue.py refuses to let the turn end near the auto-compaction
# threshold, on the assumption that the run still has phases to go.
#
# Acts on every run folder this session directs (`unit_runs.py held`): the
# marker names one, and phases run side by side each keep their own folder,
# which records its director. A run of another director is never touched.
#
# Keeps workers and app-servers while their unit director is live. Otherwise,
# pauses Codex workers and stops this run's servers after preserving queues.
# Removes only Claude workers no longer held by a unit or live director.
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
# repo needs >= 3.10.
PY="${HOME}/.claude/scripts/lib/py"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MESH="${SCRIPT_DIR}/../agents/codex_mesh.py"

if [[ -z "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  echo "No CLAUDE_CODE_SESSION_ID in the environment — nothing to clear."
  exit 0
fi

# The marker is under unit/, or delegate/ for a run started before the rename.
if [[ -n "${UNIT_ACTIVE_DIR:-}" ]]; then
  MARKER="${UNIT_ACTIVE_DIR}/${CLAUDE_CODE_SESSION_ID}"
else
  MARKER="/tmp/claude/unit/active/${CLAUDE_CODE_SESSION_ID}"
  if [[ ! -f "${MARKER}" && -f "/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}" ]]; then
    MARKER="/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}"
  fi
fi

# The marker holds the session directory, which is where the mesh files live.
SESSION_DIR=""
if [[ -f "${MARKER}" ]]; then
  SESSION_DIR="$(head -n 1 "${MARKER}")"
fi

# The marker's run first, kept even when the scan fails, then every other run
# this session directs.
RUNS=()
if [[ -n "${SESSION_DIR}" ]]; then
  RUNS+=("${SESSION_DIR}")
fi
HELD_ARGS=(held "${CLAUDE_CODE_SESSION_ID}")
if [[ -n "${UNIT_RUN_ROOT:-}" ]]; then
  HELD_ARGS+=(--root "${UNIT_RUN_ROOT}")
fi
if [[ -n "${UNIT_ACTIVE_DIR:-}" ]]; then
  HELD_ARGS+=(--active "${UNIT_ACTIVE_DIR}")
fi
HELD="$("$PY" "${SCRIPT_DIR}/../lib/unit_runs.py" "${HELD_ARGS[@]}" || true)"
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
    "$PY" "${SCRIPT_DIR}/../guilds/roles.py" unbind-run "${run}" || exit 1
  done
  "$PY" "${SCRIPT_DIR}/remove_workers.py" "${REMOVE_ARGS[@]}" || true
  for run in "${RUNS[@]}"; do
    if [[ -f "${run}/mesh_roster.json" || -f "${run}/mesh_server.json" || -f "${run}/mesh_retired.json" ]]; then
      "$PY" "${MESH}" stop --pause --session-dir "${run}" || true
    fi
  done
  "$PY" "${SCRIPT_DIR}/remove_run_scratch.py" --tracked "${RUNS[@]}" || true
  # remove_run_scratch also ends tracked run scratch while keeping live commands' cwd.
fi

if [[ -f "${MARKER}" ]]; then
  rm -f "${MARKER}"
  echo "Unit run ended; marker cleared."
else
  echo "No active unit run marker for this session."
fi

#!/usr/bin/env bash
# end_session.sh — Mark this Claude session's unit run finished.
#
# Usage: end_session.sh
#
# Removes the run-active marker written by prepare_session.sh. Until it is gone,
# stop-delegate-continue.py refuses to let the turn end near the auto-compaction
# threshold, on the assumption that the run still has phases to go.
#
# Removes every claude seat this run launched, and any a dead run left alive
# (remove_seats.py). Also stops this run's codex app-server, if one was
# started. That server is detached on purpose -- it has to outlive each
# delegate so a peer can still reach a thread between turns -- so the end of
# the run is the only point that knows nobody needs it any more.
#
# Deletes the run's verify.sh pass records (verify_cache/); the time they saved
# is already in ~/.local/state/verify/saved.jsonl.
#
# Safe to run when no run is active. The rest of the session directory is left
# alone; heartbeat and delegate logs stay readable after the run.

set -euo pipefail

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="${HOME}/.claude/scripts/lib/py"

if [[ -z "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  echo "No CLAUDE_CODE_SESSION_ID in the environment — nothing to clear."
  exit 0
fi

MARKER="/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}"

if [[ -f "${MARKER}" ]]; then
  # The marker holds the session directory, which is where the mesh files live.
  SESSION_DIR="$(head -n 1 "${MARKER}")"
  if [[ -n "${SESSION_DIR}" ]]; then
    "$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/remove_seats.py" \
      --session-dir "${SESSION_DIR}" || true
  fi
  if [[ -n "${SESSION_DIR}" && ( -f "${SESSION_DIR}/mesh_roster.json" || -f "${SESSION_DIR}/mesh_server.json" ) ]]; then
    "$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../agents/codex_mesh.py" \
      stop --session-dir "${SESSION_DIR}" || true
  fi
  if [[ -n "${SESSION_DIR}" && -d "${SESSION_DIR}/verify_cache" ]]; then
    rm -rf -- "${SESSION_DIR}/verify_cache"
  fi
  if [[ -n "${SESSION_DIR}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    zsh "${SCRIPT_DIR}/../message/notifier.sh" remove "delegate-$(basename "${SESSION_DIR}")" \
      >/dev/null 2>&1 || true
  fi
  rm -f "${MARKER}"
  echo "Unit run ended; marker cleared."
else
  echo "No active unit run marker for this session."
fi

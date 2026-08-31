#!/usr/bin/env bash
# end_session.sh — Mark this Claude session's delegate run finished.
#
# Usage: end_session.sh
#
# Removes the run-active marker written by prepare_session.sh. Until it is gone,
# stop-delegate-continue.py refuses to let the turn end near the auto-compaction
# threshold, on the assumption that the run still has phases to go.
#
# Also stops this run's codex app-server, if one was started. That server is
# detached on purpose -- it has to outlive each delegate so a peer can still
# reach a thread between turns -- so the end of the run is the only point that
# knows nobody needs it any more.
#
# Safe to run when no run is active. The session directory itself is left alone;
# heartbeat and delegate logs stay readable after the run.

set -euo pipefail

if [[ -z "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  echo "No CLAUDE_CODE_SESSION_ID in the environment — nothing to clear."
  exit 0
fi

MARKER="/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}"

if [[ -f "${MARKER}" ]]; then
  # The marker holds the session directory, which is where the mesh files live.
  SESSION_DIR="$(head -n 1 "${MARKER}")"
  if [[ -n "${SESSION_DIR}" && -f "${SESSION_DIR}/mesh_server.json" ]]; then
    python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../agents/codex_mesh.py" \
      stop --session-dir "${SESSION_DIR}" || true
  fi
  rm -f "${MARKER}"
  echo "Delegate run ended; marker cleared."
else
  echo "No active delegate run marker for this session."
fi

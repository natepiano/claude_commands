#!/usr/bin/env bash
# prepare_session.sh — Create a unique unit session directory.
#
# Usage: prepare_session.sh
#
# Creates a unique run folder under /tmp/claude/unit/.
# Stores the run pointer in the director's staff row.
# end_session.sh clears the run pointer when the run ends.

set -euo pipefail

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.12.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/remove_workers.py" || true

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
# The unit uses this session folder after preparation exits.
SESSION_DIR="$("${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../lib/build_folders.py" stable --unit-root --name "${SESSION_ID}")"

DIRECTOR_SESSION_ID="${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-}}"
if [[ -n "${DIRECTOR_SESSION_ID}" ]]; then
  if "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../production/staff.py" set-run "$DIRECTOR_SESSION_ID" "$SESSION_DIR"; then
    :
  else
    RUN_POINTER_STATUS=$?
    if (( RUN_POINTER_STATUS != 1 )); then
      exit "${RUN_POINTER_STATUS}"
    fi
  fi
  "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../usage/agents_log.py" append --id "${DIRECTOR_SESSION_ID}" --role director || true
fi

echo "Session ready at ${SESSION_DIR}"

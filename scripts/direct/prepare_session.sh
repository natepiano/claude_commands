#!/usr/bin/env bash
# prepare_session.sh — Create a unique unit session directory.
#
# Usage: prepare_session.sh
#
# Produces:
#   /tmp/claude/unit/<uuid>/   — unique session directory
#   /tmp/claude/unit/active/<claude-session-id>  — run-active marker
#
# Runs started before the rename keep /tmp/claude/delegate/ until they end; a
# new run in the same Claude session replaces that session's marker there.
#
# The marker tells the Stop hook (stop-direct-continue.py) that this Claude
# session is mid-run, so an approaching context limit cannot end the turn and
# strand the run. end_session.sh removes it.
#
# First it removes every worker a dead run left alive (remove_workers.py), so a run
# whose unit director died before its own cleanup never outlives the next start.
#
# Prints the session directory path to stdout (last line) for the caller to capture.

set -euo pipefail

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.11.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/remove_workers.py" || true

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
SESSION_DIR="/tmp/claude/unit/${SESSION_ID}"

mkdir -p "${SESSION_DIR}"

if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  ACTIVE_DIR=/tmp/claude/unit/active
  mkdir -p "${ACTIVE_DIR}"
  printf '%s\n' "${SESSION_DIR}" > "${ACTIVE_DIR}/${CLAUDE_CODE_SESSION_ID}"
  rm -f "/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}"
  "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../usage/agents_log.py" append --id "${CLAUDE_CODE_SESSION_ID}" --role director || true
fi

echo "Session ready at ${SESSION_DIR}"

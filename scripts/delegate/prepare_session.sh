#!/usr/bin/env bash
# prepare_session.sh — Create a unique delegate session directory.
#
# Usage: prepare_session.sh
#
# Produces:
#   /tmp/claude/delegate/<uuid>/   — unique session directory
#   /tmp/claude/delegate/active/<claude-session-id>  — run-active marker
#
# The marker tells the Stop hook (stop-delegate-continue.py) that this Claude
# session is mid-run, so an approaching context limit cannot end the turn and
# strand the run. end_session.sh removes it.
#
# Prints the session directory path to stdout (last line) for the caller to capture.

set -euo pipefail

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
SESSION_DIR="/tmp/claude/delegate/${SESSION_ID}"

mkdir -p "${SESSION_DIR}"

if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  mkdir -p /tmp/claude/delegate/active
  printf '%s\n' "${SESSION_DIR}" > "/tmp/claude/delegate/active/${CLAUDE_CODE_SESSION_ID}"
fi

echo "Session ready at ${SESSION_DIR}"

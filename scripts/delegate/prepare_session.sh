#!/usr/bin/env bash
# prepare_session.sh — Create a unique delegate session directory.
#
# Usage: prepare_session.sh
#
# Produces:
#   /tmp/claude/delegate/<uuid>/   — unique session directory
#
# Prints the session directory path to stdout (last line) for the caller to capture.

set -euo pipefail

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
SESSION_DIR="/tmp/claude/delegate/${SESSION_ID}"

mkdir -p "${SESSION_DIR}"

echo "Session ready at ${SESSION_DIR}"

#!/usr/bin/env bash
# prepare_session.sh — Create a unique ask_a_friend session directory.
#
# Usage: prepare_session.sh
#
# Produces:
#   /tmp/claude/ask_a_friend/<uuid>/          — unique session directory
#   /tmp/claude/ask_a_friend/<uuid>/history.md — empty history file
#
# Prints the session directory path to stdout (last line) for the caller to capture.

set -euo pipefail

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
SESSION_DIR="/tmp/claude/ask_a_friend/${SESSION_ID}"

mkdir -p "${SESSION_DIR}"
: > "${SESSION_DIR}/history.md"

echo "Session ready at ${SESSION_DIR}"

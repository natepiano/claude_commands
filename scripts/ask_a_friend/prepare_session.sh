#!/usr/bin/env bash
# prepare_session.sh — Create a unique ask_a_friend session directory.
#
# Usage: prepare_session.sh
#
# Produces:
#   /tmp/claude/ask_a_friend/<uuid>/             — unique session directory
#   /tmp/claude/ask_a_friend/<uuid>/history.md   — empty history file
#   /tmp/claude/ask_a_friend/<uuid>/friend_name  — the friend's mesh address
#
# Prints the friend name and, as its last line, the session directory path.

set -euo pipefail

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
SESSION_DIR="/tmp/claude/ask_a_friend/${SESSION_ID}"
# An address the caller types: short, unique on this machine, no quoting.
FRIEND_NAME="friend-${SESSION_ID:0:6}"

mkdir -p "${SESSION_DIR}"
: > "${SESSION_DIR}/history.md"
printf '%s\n' "${FRIEND_NAME}" > "${SESSION_DIR}/friend_name"

echo "Friend name: ${FRIEND_NAME}"
echo "Session ready at ${SESSION_DIR}"

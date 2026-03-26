#!/usr/bin/env bash
# prepare_session.sh — Clean and initialize an ask_a_friend session directory.
#
# Usage: prepare_session.sh
#
# Produces:
#   /tmp/claude/ask_a_friend/          — clean session directory
#   /tmp/claude/ask_a_friend/history.md — empty history file

set -euo pipefail

SESSION_DIR="/tmp/claude/ask_a_friend"

rm -rf "${SESSION_DIR}"
mkdir -p "${SESSION_DIR}"
: > "${SESSION_DIR}/history.md"

echo "Session ready at ${SESSION_DIR}"

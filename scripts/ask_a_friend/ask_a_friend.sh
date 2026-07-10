#!/usr/bin/env bash
# ask_a_friend.sh — Invoke Codex CLI to consult on a question, then signal completion.
#
# Usage: ask_a_friend.sh <session_dir> [working_dir]
#
# Expects:
#   <session_dir>/question.md  — the question file (must exist)
#
# Produces:
#   <session_dir>/status       — "asking" while running, "answered" on success, "error" on failure
#   <session_dir>/answer.txt   — codex's response text
#   <session_dir>/codex.log    — full stderr log from codex exec

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../agents/agents_config.sh"

SESSION_DIR="${1:?Usage: ask_a_friend.sh <session_dir> [working_dir]}"
WORKING_DIR="${2:-$(pwd)}"

QUESTION_FILE="${SESSION_DIR}/question.md"
ANSWER_FILE="${SESSION_DIR}/answer.txt"
STATUS_FILE="${SESSION_DIR}/status"
LOG_FILE="${SESSION_DIR}/codex.log"

if [[ ! -f "${QUESTION_FILE}" ]]; then
  echo "error" > "${STATUS_FILE}"
  echo "Question file not found: ${QUESTION_FILE}" > "${LOG_FILE}"
  exit 1
fi

# Signal that we are asking
echo "asking" > "${STATUS_FILE}"

QUESTION=$(cat "${QUESTION_FILE}")

CODEX_MODEL="$(agents_config_model codex)"
CODEX_EFFORT="$(agents_config_effort codex)"
CODEX_ARGS=()
[[ -n "$CODEX_MODEL" ]] && CODEX_ARGS+=(-m "$CODEX_MODEL")
[[ -n "$CODEX_EFFORT" ]] \
  && CODEX_ARGS+=(-c "model_reasoning_effort=\"$CODEX_EFFORT\"")

# Invoke codex exec:
#   --full-auto            — no approval prompts + workspace-write sandbox
#   --ephemeral            — don't persist session (one-shot)
#   -C <dir>               — set working directory for codebase context
#   -o <file>              — write final answer to file
#
# NOTE: Do NOT use --sandbox read-only here. It conflicts with --full-auto
# (which implies --sandbox workspace-write) and blocks macOS SystemConfiguration
# framework access, causing a panic in the system-configuration crate.
if codex exec \
  "${CODEX_ARGS[@]}" \
  --ephemeral \
  --full-auto \
  -C "${WORKING_DIR}" \
  -o "${ANSWER_FILE}" \
  "${QUESTION}" \
  > "${LOG_FILE}" 2>&1; then
  echo "answered" > "${STATUS_FILE}"
else
  EXIT_CODE=$?
  echo "error" > "${STATUS_FILE}"
  echo "codex exec exited with code ${EXIT_CODE}" >> "${LOG_FILE}"
  exit "${EXIT_CODE}"
fi

#!/usr/bin/env bash
# codex_implement.sh — Invoke Codex CLI to implement code changes, then signal completion.
#
# Usage: codex_implement.sh <session_dir> [working_dir]
#
# Expects:
#   <session_dir>/implementation_prompt.md  — the implementation instructions (must exist)
#
# Produces:
#   <session_dir>/impl_status       — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary.txt  — codex's implementation summary
#   <session_dir>/impl_codex.log    — full stderr log from codex exec

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../agents/agents_config.sh"

SESSION_DIR="${1:?Usage: codex_implement.sh <session_dir> [working_dir]}"
WORKING_DIR="${2:-$(pwd)}"

PROMPT_FILE="${SESSION_DIR}/implementation_prompt.md"
SUMMARY_FILE="${SESSION_DIR}/impl_summary.txt"
STATUS_FILE="${SESSION_DIR}/impl_status"
LOG_FILE="${SESSION_DIR}/impl_codex.log"

if [[ ! -f "${PROMPT_FILE}" ]]; then
  echo "error" > "${STATUS_FILE}"
  echo "Implementation prompt not found: ${PROMPT_FILE}" > "${LOG_FILE}"
  exit 1
fi

# Signal that implementation is in progress
echo "implementing" > "${STATUS_FILE}"

PROMPT=$(cat "${PROMPT_FILE}")

CODEX_MODEL="$(agents_config_model codex)"
CODEX_EFFORT="$(agents_config_effort codex)"
CODEX_ARGS=()
[[ -n "$CODEX_MODEL" ]] && CODEX_ARGS+=(-m "$CODEX_MODEL")
[[ -n "$CODEX_EFFORT" ]] \
  && CODEX_ARGS+=(-c "model_reasoning_effort=\"$CODEX_EFFORT\"")

# Invoke codex exec in full-auto mode so it can write files in the working directory.
# -o captures the implementation summary (what was done).
if codex exec \
  "${CODEX_ARGS[@]}" \
  --ephemeral \
  --full-auto \
  -C "${WORKING_DIR}" \
  -o "${SUMMARY_FILE}" \
  "${PROMPT}" \
  > "${LOG_FILE}" 2>&1; then
  echo "implemented" > "${STATUS_FILE}"
else
  EXIT_CODE=$?
  echo "error" > "${STATUS_FILE}"
  echo "codex exec exited with code ${EXIT_CODE}" >> "${LOG_FILE}"
  exit "${EXIT_CODE}"
fi

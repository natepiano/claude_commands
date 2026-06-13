#!/usr/bin/env bash
# codex_review.sh — Invoke a fresh Codex CLI session to review implemented changes (read-only).
#
# Usage: codex_review.sh <session_dir> [working_dir] [prompt_file]
#
# Expects:
#   <prompt_file> (default <session_dir>/review_prompt.md) — the review instructions
#   (spec + diff; deliberately excludes the implementer's summary so the review is blind)
#
# Produces:
#   <session_dir>/review_status        — "reviewing" while running, "reviewed" on success, "error" on failure
#   <session_dir>/review_findings.txt  — codex's review findings
#   <session_dir>/review_codex.log     — full stderr log from codex exec

set -euo pipefail

SESSION_DIR="${1:?Usage: codex_review.sh <session_dir> [working_dir] [prompt_file]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/review_prompt.md}"

FINDINGS_FILE="${SESSION_DIR}/review_findings.txt"
STATUS_FILE="${SESSION_DIR}/review_status"
LOG_FILE="${SESSION_DIR}/review_codex.log"

if [[ ! -f "${PROMPT_FILE}" ]]; then
  echo "error" > "${STATUS_FILE}"
  echo "Review prompt not found: ${PROMPT_FILE}" > "${LOG_FILE}"
  exit 1
fi

echo "reviewing" > "${STATUS_FILE}"

PROMPT=$(cat "${PROMPT_FILE}")

# Read-only sandbox: the reviewer must not modify code.
if codex exec \
  -c model_reasoning_effort='"high"' \
  --ephemeral \
  --sandbox read-only \
  -C "${WORKING_DIR}" \
  -o "${FINDINGS_FILE}" \
  "${PROMPT}" \
  > "${LOG_FILE}" 2>&1; then
  echo "reviewed" > "${STATUS_FILE}"
else
  EXIT_CODE=$?
  echo "error" > "${STATUS_FILE}"
  echo "codex exec exited with code ${EXIT_CODE}" >> "${LOG_FILE}"
  exit "${EXIT_CODE}"
fi

#!/usr/bin/env bash
# codex_review.sh — Invoke a fresh Codex CLI session to review implemented changes (read-only).
#
# Usage: codex_review.sh <session_dir> [working_dir] [prompt_file] [profile]
#
# Expects:
#   <prompt_file> (default <session_dir>/review_prompt.md) — the review instructions
#   (spec + diff; deliberately excludes the implementer's summary so the review is blind)
#   [profile] (default review) — effort profile from config/delegate.conf
#
# Produces:
#   <session_dir>/review_status        — "reviewing" while running, "reviewed" on success, "error" on failure
#   <session_dir>/review_findings.txt  — codex's review findings
#   <session_dir>/review_codex.log     — full stderr log from codex exec
#   <session_dir>/review_agent         — selected profile, agent, model, and effort

set -euo pipefail

SESSION_DIR="${1:?Usage: codex_review.sh <session_dir> [working_dir] [prompt_file] [profile]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/review_prompt.md}"
PROFILE="${4:-review}"

# The profile owns effort; the shared agent registry owns the model.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/delegate_config.sh"
delegate_config_resolve "${PROFILE}"
CODEX_MODEL="${DELEGATE_MODEL}"
CODEX_EFFORT="${DELEGATE_EFFORT}"

FINDINGS_FILE="${SESSION_DIR}/review_findings.txt"
STATUS_FILE="${SESSION_DIR}/review_status"
LOG_FILE="${SESSION_DIR}/review_codex.log"
AGENT_FILE="${SESSION_DIR}/review_agent"

if [[ ! -f "${PROMPT_FILE}" ]]; then
  echo "error" > "${STATUS_FILE}"
  echo "Review prompt not found: ${PROMPT_FILE}" > "${LOG_FILE}"
  exit 1
fi

echo "reviewing" > "${STATUS_FILE}"
printf 'profile=%s\nagent=%s\nmodel=%s\neffort=%s\n' \
  "${PROFILE}" "${DELEGATE_AGENT}" "${CODEX_MODEL}" "${CODEX_EFFORT}" > "${AGENT_FILE}"

PROMPT=$(cat "${PROMPT_FILE}")

# Read-only sandbox: the reviewer must not modify code.
# Model comes from agents.conf; effort comes from the selected delegate profile.
CODEX_ARGS=()
[[ -n "${CODEX_MODEL}" ]] && CODEX_ARGS+=(-m "${CODEX_MODEL}")
[[ -n "${CODEX_EFFORT}" ]] && CODEX_ARGS+=(-c "model_reasoning_effort=\"${CODEX_EFFORT}\"")
if codex exec \
  "${CODEX_ARGS[@]}" \
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

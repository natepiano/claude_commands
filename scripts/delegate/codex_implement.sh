#!/usr/bin/env bash
# codex_implement.sh — Invoke Codex CLI to implement code changes, then signal completion.
#
# Usage: codex_implement.sh <session_dir> [working_dir] [prompt_file] [profile]
#
# Expects:
#   <prompt_file> (default <session_dir>/implementation_prompt.md) — the implementation instructions
#   [profile] (default implementation) — effort profile from config/delegate.conf
#
# Produces:
#   <session_dir>/impl_status       — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary.txt  — codex's implementation summary
#   <session_dir>/impl_codex.log    — full stderr log from codex exec
#   <session_dir>/impl_agent        — selected profile, agent, model, and effort

set -euo pipefail

SESSION_DIR="${1:?Usage: codex_implement.sh <session_dir> [working_dir] [prompt_file] [profile]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/implementation_prompt.md}"
PROFILE="${4:-implementation}"

# The profile owns effort; the shared agent registry owns the model.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/delegate_config.sh"
delegate_config_resolve "${PROFILE}"
CODEX_MODEL="${DELEGATE_MODEL}"
CODEX_EFFORT="${DELEGATE_EFFORT}"

SUMMARY_FILE="${SESSION_DIR}/impl_summary.txt"
STATUS_FILE="${SESSION_DIR}/impl_status"
LOG_FILE="${SESSION_DIR}/impl_codex.log"
AGENT_FILE="${SESSION_DIR}/impl_agent"

if [[ ! -f "${PROMPT_FILE}" ]]; then
  echo "error" > "${STATUS_FILE}"
  echo "Implementation prompt not found: ${PROMPT_FILE}" > "${LOG_FILE}"
  exit 1
fi

echo "implementing" > "${STATUS_FILE}"
printf 'profile=%s\nagent=%s\nmodel=%s\neffort=%s\n' \
  "${PROFILE}" "${DELEGATE_AGENT}" "${CODEX_MODEL}" "${CODEX_EFFORT}" > "${AGENT_FILE}"

PROMPT=$(cat "${PROMPT_FILE}")

# Invoke codex exec in full-auto mode so it can write files in the working directory.
# -o captures the implementation summary (what was done).
# Model comes from agents.conf; effort comes from the selected delegate profile.
CODEX_ARGS=()
[[ -n "${CODEX_MODEL}" ]] && CODEX_ARGS+=(-m "${CODEX_MODEL}")
[[ -n "${CODEX_EFFORT}" ]] && CODEX_ARGS+=(-c "model_reasoning_effort=\"${CODEX_EFFORT}\"")
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

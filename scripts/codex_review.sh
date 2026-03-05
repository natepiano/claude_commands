#!/usr/bin/env bash
# codex_review.sh — Run codex exec review and signal completion.
#
# Usage: codex_review.sh <session_dir> <working_dir> <mode> [mode_arg] [custom_prompt]
#
# Modes:
#   uncommitted                     — review uncommitted changes
#   base <branch>                   — review changes against a base branch
#   file <path> [base_branch]       — review changes to a specific file (diff piped via stdin)
#
# Produces:
#   <session_dir>/status       — "reviewing" while running, "done" on success, "error" on failure
#   <session_dir>/review.txt   — codex's review output
#   <session_dir>/codex.log    — full log from codex exec review

set -euo pipefail

SESSION_DIR="${1:?Usage: codex_review.sh <session_dir> <working_dir> <mode> [mode_arg] [custom_prompt]}"
WORKING_DIR="${2:?Missing working_dir}"
MODE="${3:?Missing mode (uncommitted|base|file)}"
MODE_ARG="${4:-}"
CUSTOM_PROMPT="${5:-}"

REVIEW_FILE="${SESSION_DIR}/review.txt"
STATUS_FILE="${SESSION_DIR}/status"
LOG_FILE="${SESSION_DIR}/codex.log"

echo "reviewing" > "${STATUS_FILE}"

# Run codex from the project directory so it picks up the correct repo context
cd "${WORKING_DIR}"

# Build the codex command — file mode uses `codex exec` (general), others use `codex exec review`
USE_STDIN=false
CMD=(codex exec review -c model_reasoning_effort='"high"' --ephemeral -o "${REVIEW_FILE}")

case "${MODE}" in
  uncommitted)
    CMD+=(--uncommitted)
    ;;
  base)
    if [[ -z "${MODE_ARG}" ]]; then
      echo "error" > "${STATUS_FILE}"
      echo "base mode requires a branch argument" > "${LOG_FILE}"
      exit 1
    fi
    CMD+=(--base "${MODE_ARG}")
    ;;
  file)
    if [[ -z "${MODE_ARG}" ]]; then
      echo "error" > "${STATUS_FILE}"
      echo "file mode requires a file path argument" > "${LOG_FILE}"
      exit 1
    fi
    # Generate the diff for this specific file and pipe via stdin
    FILE_PATH="${MODE_ARG}"
    BASE_BRANCH="${CUSTOM_PROMPT:-}"
    DIFF_FILE="${SESSION_DIR}/file_diff.patch"
    if [[ -n "${BASE_BRANCH}" ]]; then
      git diff "${BASE_BRANCH}"...HEAD -- "${FILE_PATH}" > "${DIFF_FILE}" 2>> "${LOG_FILE}"
    else
      git diff HEAD -- "${FILE_PATH}" > "${DIFF_FILE}" 2>> "${LOG_FILE}"
    fi
    # If diff is empty, file may be untracked or new — diff against /dev/null to show full content
    if [[ ! -s "${DIFF_FILE}" ]]; then
      if [[ -f "${FILE_PATH}" ]]; then
        git diff --no-index /dev/null "${FILE_PATH}" > "${DIFF_FILE}" 2>> "${LOG_FILE}" || true
      fi
    fi
    if [[ ! -s "${DIFF_FILE}" ]]; then
      echo "error" > "${STATUS_FILE}"
      echo "No content found for file: ${FILE_PATH}" > "${LOG_FILE}"
      exit 1
    fi
    USE_STDIN=true
    # File mode uses general `codex exec` instead of `codex exec review` — review subcommand
    # expects git-based modes, not arbitrary file content via stdin
    CMD=(codex exec -c model_reasoning_effort='"high"' --ephemeral --full-auto -o "${REVIEW_FILE}")
    CMD+=("Review the following file content for ${FILE_PATH}. Provide a thorough code/design review with actionable findings.")
    # Clear CUSTOM_PROMPT since it was used as base_branch
    CUSTOM_PROMPT=""
    ;;
  *)
    echo "error" > "${STATUS_FILE}"
    echo "Unknown mode: ${MODE}. Expected: uncommitted, base, file" > "${LOG_FILE}"
    exit 1
    ;;
esac

# Append custom review prompt if provided (not used by file mode)
if [[ -n "${CUSTOM_PROMPT}" ]]; then
  CMD+=("${CUSTOM_PROMPT}")
fi

if [[ "${USE_STDIN}" == "true" ]]; then
  if cat "${DIFF_FILE}" | "${CMD[@]}" > "${LOG_FILE}" 2>&1; then
    echo "done" > "${STATUS_FILE}"
  else
    EXIT_CODE=$?
    echo "error" > "${STATUS_FILE}"
    echo "codex exec review exited with code ${EXIT_CODE}" >> "${LOG_FILE}"
    exit "${EXIT_CODE}"
  fi
else
  if "${CMD[@]}" > "${LOG_FILE}" 2>&1; then
    echo "done" > "${STATUS_FILE}"
  else
    EXIT_CODE=$?
    echo "error" > "${STATUS_FILE}"
    echo "codex exec review exited with code ${EXIT_CODE}" >> "${LOG_FILE}"
    exit "${EXIT_CODE}"
  fi
fi

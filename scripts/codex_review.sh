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

# Build the codex command.
#
# `codex exec review` with --uncommitted/--base does NOT accept a [PROMPT] argument.
# When a custom prompt is provided, fall back to `codex exec` (general) with the diff
# piped via stdin — same approach used for file mode.
USE_STDIN=false
DIFF_FILE="${SESSION_DIR}/diff.patch"

case "${MODE}" in
  uncommitted)
    if [[ -n "${CUSTOM_PROMPT}" ]]; then
      # Generate diff ourselves so we can use `codex exec` with the custom prompt
      { git diff; git diff --staged; } > "${DIFF_FILE}" 2>> "${LOG_FILE}"
      # Append untracked files if any exist
      for f in $(git ls-files --others --exclude-standard 2>/dev/null); do
        git diff --no-index /dev/null "$f" >> "${DIFF_FILE}" 2>/dev/null || true
      done
      USE_STDIN=true
      CMD=(codex exec -c model_reasoning_effort='"high"' --ephemeral --full-auto -o "${REVIEW_FILE}")
      CMD+=("Review the following uncommitted changes. ${CUSTOM_PROMPT}")
    else
      CMD=(codex exec review -c model_reasoning_effort='"high"' --ephemeral -o "${REVIEW_FILE}")
      CMD+=(--uncommitted)
    fi
    ;;
  base)
    if [[ -z "${MODE_ARG}" ]]; then
      echo "error" > "${STATUS_FILE}"
      echo "base mode requires a branch argument" > "${LOG_FILE}"
      exit 1
    fi
    if [[ -n "${CUSTOM_PROMPT}" ]]; then
      git diff "${MODE_ARG}"...HEAD > "${DIFF_FILE}" 2>> "${LOG_FILE}"
      USE_STDIN=true
      CMD=(codex exec -c model_reasoning_effort='"high"' --ephemeral --full-auto -o "${REVIEW_FILE}")
      CMD+=("Review the following changes against ${MODE_ARG}. ${CUSTOM_PROMPT}")
    else
      CMD=(codex exec review -c model_reasoning_effort='"high"' --ephemeral -o "${REVIEW_FILE}")
      CMD+=(--base "${MODE_ARG}")
    fi
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
    CMD=(codex exec -c model_reasoning_effort='"high"' --ephemeral --full-auto -o "${REVIEW_FILE}")
    CMD+=("Review the following file content for ${FILE_PATH}. Provide a thorough code/design review with actionable findings.")
    ;;
  *)
    echo "error" > "${STATUS_FILE}"
    echo "Unknown mode: ${MODE}. Expected: uncommitted, base, file" > "${LOG_FILE}"
    exit 1
    ;;
esac

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

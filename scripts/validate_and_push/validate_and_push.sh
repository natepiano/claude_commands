#!/usr/bin/env bash
set -euo pipefail

# Top-level validate-and-push workflow. Exits with code 2 when a PR branch name
# needs user confirmation; in that case it prints prepare_pr_push.sh JSON. On the
# direct path it pushes and hands CI watching to the agent; it does not block on CI.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Options (both used by /plan:delegate's periodic CI point):
#   --to <branch>          push HEAD to origin/<branch> instead of the current
#                          branch; always the direct path, so a run on the
#                          default branch reaches CI without landing on it
#   --fix-commit <message> commit validation fixes as a new commit with this
#                          message instead of amending the last commit, so a
#                          recorded checkpoint commit is never rewritten
PUSH_TARGET_BRANCH=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --to) PUSH_TARGET_BRANCH="${2:?--to needs a branch name}"; shift 2 ;;
    --fix-commit) export VALIDATE_FIX_COMMIT_MESSAGE="${2:?--fix-commit needs a message}"; shift 2 ;;
    *) echo "validate_and_push.sh: unknown argument: $1" >&2; exit 64 ;;
  esac
done

echo "=== STEP: validation ==="
bash "${SCRIPT_DIR}/run_validation.sh"

if [ -n "$PUSH_TARGET_BRANCH" ]; then
  echo "=== STEP: direct push to ${PUSH_TARGET_BRANCH} ==="
  PUSH_TARGET_BRANCH="$PUSH_TARGET_BRANCH" bash "${SCRIPT_DIR}/push_direct.sh"
  exit $?
fi

echo "=== STEP: choose push path ==="
PUSH_PATH_JSON="$(bash "${SCRIPT_DIR}/choose_push_path.sh")"
printf '%s\n' "$PUSH_PATH_JSON"

if [[ "$PUSH_PATH_JSON" == *'"push_path":"pr"'* ]]; then
  echo "=== STEP: prepare PR branch ==="
  bash "${SCRIPT_DIR}/prepare_pr_push.sh"
  exit 2
fi

echo "=== STEP: direct push ==="
bash "${SCRIPT_DIR}/push_direct.sh"

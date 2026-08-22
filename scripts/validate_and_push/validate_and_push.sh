#!/usr/bin/env bash
set -euo pipefail

# Top-level validate-and-push workflow. Exits with code 2 when a PR branch name
# needs user confirmation; in that case it prints prepare_pr_push.sh JSON. On the
# direct path it pushes and hands CI watching to the agent; it does not block on CI.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== STEP: validation ==="
bash "${SCRIPT_DIR}/run_validation.sh"

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

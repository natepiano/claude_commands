#!/bin/bash
# Writes the confirmation nonce for a worktree deletion. Invoked by the
# /worktree_delete confirm step after the user confirms, so perform_deletion.sh
# will accept the deletion.
# Usage: record_confirmation.sh <worktree_path> <branch_name>

set -euo pipefail

WORKTREE_PATH="${1:-}"
BRANCH_NAME="${2:-}"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" ]]; then
    echo "Error: Usage: record_confirmation.sh <worktree_path> <branch_name>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

wt_write_confirmation "$WORKTREE_PATH" "$BRANCH_NAME"

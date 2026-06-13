#!/bin/bash
# Shared confirmation-gate helpers for worktree deletion.
#
# A path-keyed nonce file couples perform_deletion.sh to the interactive
# confirmation step in worktree_delete.md: the confirm step writes the nonce,
# the deletion script requires and consumes it before doing anything
# destructive. An orchestrator that skips confirmation has no nonce and is
# refused. The file is keyed by the target worktree path, so deletions of
# different worktrees never read each other's nonce.

WT_CONFIRM_DIR="/tmp/claude/worktree_delete"
WT_CONFIRM_TTL_SECONDS=600

# Stable key for a worktree path (strip trailing slash, hash to a filesystem-
# safe name). Writer and reader both derive the key from the same path string.
wt_confirm_key() {
    printf '%s' "${1%/}" | shasum -a 256 | cut -d' ' -f1
}

wt_confirm_file() {
    printf '%s/%s' "$WT_CONFIRM_DIR" "$(wt_confirm_key "$1")"
}

# Record the confirmation nonce after the user has confirmed. Stores the
# absolute path, branch, and creation epoch.
wt_write_confirmation() {
    local path="$1" branch="$2" file
    mkdir -p "$WT_CONFIRM_DIR"
    file="$(wt_confirm_file "$path")"
    printf '%s\n%s\n%s\n' "${path%/}" "$branch" "$(date +%s)" >"$file"
    echo "Confirmation recorded: $file"
}

# Verify and consume the nonce. Refuses (non-zero) if missing, mismatched, or
# stale. Always consumes the file when present so a leftover nonce can't be
# reused.
wt_check_and_consume() {
    local path="$1" branch="$2" file
    file="$(wt_confirm_file "$path")"

    if [[ ! -f "$file" ]]; then
        echo "Error: no confirmation nonce for $path."
        echo "Deletion must go through the /worktree_delete confirmation step."
        return 1
    fi

    local stored_path stored_branch stored_epoch
    { read -r stored_path; read -r stored_branch; read -r stored_epoch; } <"$file"
    rm -f "$file"

    if [[ "$stored_path" != "${path%/}" || "$stored_branch" != "$branch" ]]; then
        echo "Error: confirmation nonce does not match target ($stored_path / $stored_branch)."
        return 1
    fi

    local now age
    now="$(date +%s)"
    age=$((now - stored_epoch))
    if ((age < 0 || age > WT_CONFIRM_TTL_SECONDS)); then
        echo "Error: confirmation nonce is stale (${age}s old, TTL ${WT_CONFIRM_TTL_SECONDS}s). Re-confirm."
        return 1
    fi

    return 0
}

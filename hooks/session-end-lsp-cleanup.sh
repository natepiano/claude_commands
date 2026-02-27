#!/bin/bash

# SessionEnd hook: clean up LSP check lock file for this session
# Also prune any stale lock files older than 24h (crash recovery)

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')

LOCK_DIR="$TMPDIR/claude_lsp_check_locks"

# Clean up this session's lock file
if [[ -n "$SESSION_ID" && -f "$LOCK_DIR/$SESSION_ID.lock" ]]; then
    rm -f "$LOCK_DIR/$SESSION_ID.lock"
fi

# Prune stale lock files older than 24h (in case of crashes)
find "$LOCK_DIR" -name "*.lock" -mmin +1440 -delete 2>/dev/null

echo "{\"systemMessage\": \"🧹 LSP check lock cleaned up\"}"

exit 0

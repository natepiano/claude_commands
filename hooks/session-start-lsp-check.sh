#!/bin/bash

# UserPromptSubmit hook: on first prompt in a Rust project, instruct agent to verify LSP

INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')

if [[ -z "$CWD" ]]; then
    exit 0
fi

# Once per session
LOCK_DIR="$TMPDIR/claude_lsp_check_locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/$SESSION_ID.lock"

if [[ -f "$LOCK_FILE" ]]; then
    exit 0
fi

# Check if this is a Rust project
if [[ ! -f "$CWD/Cargo.toml" ]]; then
    exit 0
fi

# Find a .rs file to check against
RS_FILE=$(find "$CWD/src" -name "*.rs" -maxdepth 2 2>/dev/null | head -1)
if [[ -z "$RS_FILE" ]]; then
    exit 0
fi

# Create lock file
touch "$LOCK_FILE"

AGENT_MSG="IMPORTANT: Before responding to the user, run LSP(documentSymbol) on $RS_FILE to verify rust-analyzer is running. Report the LSP status to the user before doing anything else."

if command -v jq >/dev/null 2>&1; then
    echo "{\"systemMessage\": \"🔧 LSP check requested\", \"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", \"additionalContext\": $(echo -n "$AGENT_MSG" | jq -Rs .)}}"
else
    ESCAPED=$(echo -n "$AGENT_MSG" | sed 's/\\/\\\\/g; s/"/\\"/g')
    echo "{\"systemMessage\": \"🔧 LSP check requested\", \"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", \"additionalContext\": \"$ESCAPED\"}}"
fi

exit 0

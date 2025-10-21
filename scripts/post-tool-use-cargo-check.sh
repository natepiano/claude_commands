#!/bin/bash

# Read the JSON input from stdin
INPUT=$(cat)

# Extract the file path from the JSON input
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_response.filePath // ""')

# Throttling mechanism using mkdir (atomic on all systems)
LOCK_DIR="$TMPDIR/claude_cargo_check_locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/cargo_check.lock"
LOCK_TIMEOUT=30  # seconds

# Clean up function
cleanup() {
    rm -rf "$LOCK_FILE" 2>/dev/null
}

# Check if lock is stale
is_lock_stale() {
    if [ ! -d "$LOCK_FILE" ]; then
        return 1  # No lock exists
    fi

    # Get lock creation time
    if [ -f "$LOCK_FILE/.timestamp" ]; then
        local lock_time=$(cat "$LOCK_FILE/.timestamp")
        local current_time=$(date +%s)
        local lock_age=$((current_time - lock_time))

        if [ $lock_age -gt $LOCK_TIMEOUT ]; then
            return 0  # Stale
        fi
    fi
    return 1  # Not stale
}

# Try to acquire lock
if mkdir "$LOCK_FILE" 2>/dev/null; then
    # We got the lock
    echo $$ > "$LOCK_FILE/pid"
    date +%s > "$LOCK_FILE/.timestamp"
    trap cleanup EXIT
elif is_lock_stale; then
    # Stale lock - force remove and retry
    rm -rf "$LOCK_FILE"
    if mkdir "$LOCK_FILE" 2>/dev/null; then
        echo $$ > "$LOCK_FILE/pid"
        date +%s > "$LOCK_FILE/.timestamp"
        trap cleanup EXIT
    else
        # Race condition - another process got it
        echo '{"systemMessage": "⏭️  cargo check skipped (already running)", "continue": true}'
        exit 0
    fi
else
    # Active lock - skip
    echo '{"systemMessage": "⏭️  cargo check skipped (already running)", "continue": true}'
    exit 0
fi

# Auto-detect bevy_brp project by checking workspace Cargo.toml for specific members
SHOW_ADDITIONAL_CONTEXT=false
WORKSPACE_ROOT=""
SEARCH_DIR=$(dirname "$FILE_PATH")
while [[ "$SEARCH_DIR" != "/" ]]; do
    if [[ -f "$SEARCH_DIR/Cargo.toml" ]]; then
        # Check if this Cargo.toml has the bevy_brp workspace members
        # Look for members array containing all three: extras, mcp, mcp_macros (order independent, multiline safe)
        TOML_CONTENT=$(cat "$SEARCH_DIR/Cargo.toml")
        if echo "$TOML_CONTENT" | grep -q 'members\s*=' && \
           echo "$TOML_CONTENT" | grep -q '"extras"' && \
           echo "$TOML_CONTENT" | grep -q '"mcp"' && \
           echo "$TOML_CONTENT" | grep -q '"mcp_macros"'; then
            WORKSPACE_ROOT="$SEARCH_DIR"
            # Check if the edited file is under mcp/ directory relative to workspace root
            RELATIVE_PATH="${FILE_PATH#$WORKSPACE_ROOT/}"
            if [[ "$RELATIVE_PATH" == mcp/* ]]; then
                SHOW_ADDITIONAL_CONTEXT=true
            fi
            break
        fi
    fi
    SEARCH_DIR=$(dirname "$SEARCH_DIR")
done

# Check if the file has a .rs extension
if [[ ! "$FILE_PATH" =~ \.rs$ ]]; then
    # Not a Rust file - exit silently
    echo '{"continue": true}'
    exit 0
fi

# Find the nearest Cargo.toml by searching up from the file's directory
SEARCH_DIR=$(dirname "$FILE_PATH")
CARGO_DIR=""

while [[ "$SEARCH_DIR" != "/" ]]; do
    if [[ -f "$SEARCH_DIR/Cargo.toml" ]]; then
        CARGO_DIR="$SEARCH_DIR"
        break
    fi
    SEARCH_DIR=$(dirname "$SEARCH_DIR")
done

# If we found a Cargo.toml, run cargo check from that directory
if [[ -n "$CARGO_DIR" ]]; then
    OUTPUT=$(cd "$CARGO_DIR" && cargo check 2>&1)
    CHECK_RESULT=$?
else
    # No Cargo.toml found - try from current directory as fallback
    OUTPUT=$(cargo check 2>&1)
    CHECK_RESULT=$?
fi

if echo "$OUTPUT" | grep -q "could not find \`Cargo.toml\`"; then
    # Not a Rust directory - exit silently
    echo '{"continue": true}'
    exit 0
fi

# Debug: Save raw output to temp file for inspection
echo "$OUTPUT" > /tmp/cargo_check_debug.txt

# Extract errors with messages (exclude "aborting due to" lines)
ERRORS=$(echo "$OUTPUT" | grep -E "^error\[" | grep -v "^error: aborting due to" | sed 's/^error\[[^]]*\]: //')
ERROR_LOCATIONS=$(echo "$OUTPUT" | grep "^error\[" -A 1 | grep "^ *-->" | sed 's/^ *--> //')

# Debug: Log what we found
echo "ERRORS found: $ERRORS" >> /tmp/cargo_check_debug.txt
echo "ERROR_LOCATIONS found: $ERROR_LOCATIONS" >> /tmp/cargo_check_debug.txt

# Extract warnings with messages (exclude summary lines)
WARNINGS=$(echo "$OUTPUT" | grep "^warning:" | grep -v "generated [0-9]* warning" | sed 's/^warning: //')
WARNING_LOCATIONS=$(echo "$OUTPUT" | grep "^warning:" -A 1 | grep "^ *-->" | sed 's/^ *--> //')

# Count errors and warnings (excluding summary lines)
ERROR_COUNT=$(echo "$ERRORS" | grep -c . || echo "0")
WARNING_COUNT=$(echo "$WARNINGS" | grep -c . || echo "0")

# Build messages for both user (systemMessage) and agent (additionalContext)
USER_MESSAGE=""
AGENT_MESSAGE=""

if [ $CHECK_RESULT -eq 0 ]; then
    # Check passed - might have warnings
    if [ -n "$WARNING_LOCATIONS" ]; then
        # Combine warnings with their locations for agent only
        WARNING_LIST=""
        IFS=$'\n'
        WARNING_ARRAY=($WARNINGS)
        LOCATION_ARRAY=($WARNING_LOCATIONS)
        for i in "${!WARNING_ARRAY[@]}"; do
            if [ $i -lt ${#LOCATION_ARRAY[@]} ]; then
                WARNING_LIST="${WARNING_LIST}  ⚠️ ${LOCATION_ARRAY[$i]}: ${WARNING_ARRAY[$i]}\n"
            fi
        done
        # User message just shows simple status
        USER_MESSAGE="✅ cargo check passed with $WARNING_COUNT warning(s)"
        # Agent gets the detailed warnings
        AGENT_MESSAGE="\\nCARGO CHECK WARNINGS:\\n✅ cargo check passed with $WARNING_COUNT warning(s):\\n${WARNING_LIST%\\n}\\n"
    else
        USER_MESSAGE="🚀 cargo check passed"
    fi

    # Run formatter and add status to message
    cargo +nightly fmt >/dev/null 2>&1
    FMT_RESULT=$?
    if [ $FMT_RESULT -eq 0 ]; then
        USER_MESSAGE="$USER_MESSAGE ✨ formatted"
    fi

    # Add agent context for bevy_brp project - use systemMessage since additionalContext may not work
    if [ "$SHOW_ADDITIONAL_CONTEXT" = true ]; then
        USER_MESSAGE="${USER_MESSAGE} 🔧 MCP changes require reinstall"
        AGENT_MESSAGE="${AGENT_MESSAGE}\\nℹ️  MCP Tool Changes Detected:\\nChanges will not be testable until the agent runs \`cargo install --path mcp\`\\nand asks the user to do \`/mcp reconnect brp\`\\n"
    fi
else
    # Check failed - build detailed error message for agent only
    if [ -n "$ERROR_LOCATIONS" ]; then
        # Combine errors with their locations
        ERROR_LIST=""
        IFS=$'\n'
        ERROR_ARRAY=($ERRORS)
        LOCATION_ARRAY=($ERROR_LOCATIONS)
        for i in "${!ERROR_ARRAY[@]}"; do
            if [ $i -lt ${#LOCATION_ARRAY[@]} ]; then
                ERROR_LIST="${ERROR_LIST}  ❌ ${LOCATION_ARRAY[$i]}: ${ERROR_ARRAY[$i]}\n"
            fi
        done
        FULL_ERROR_MESSAGE="💥 cargo check failed with $ERROR_COUNT error(s):\n${ERROR_LIST%\\n}"

        if [ -n "$WARNING_LOCATIONS" ]; then
            WARNING_LIST=""
            WARNING_ARRAY=($WARNINGS)
            WLOCATION_ARRAY=($WARNING_LOCATIONS)
            for i in "${!WARNING_ARRAY[@]}"; do
                if [ $i -lt ${#WLOCATION_ARRAY[@]} ]; then
                    WARNING_LIST="${WARNING_LIST}  ⚠️ ${WLOCATION_ARRAY[$i]}: ${WARNING_ARRAY[$i]}\n"
                fi
            done
            FULL_ERROR_MESSAGE="$FULL_ERROR_MESSAGE\nand $WARNING_COUNT warning(s):\n${WARNING_LIST%\\n}"
        fi
    else
        FULL_ERROR_MESSAGE="💥 cargo check failed"
    fi

    # For failures: show brief message to user, detailed errors/warnings to agent
    USER_MESSAGE="💥 cargo check failed with $ERROR_COUNT error(s)"
    AGENT_MESSAGE="\\nCARGO CHECK FAILED:\\n$FULL_ERROR_MESSAGE\\n"
fi

# Build JSON output with conditional additionalContext
ADDITIONAL_CONTEXT=""
if [ -n "$AGENT_MESSAGE" ]; then
    ADDITIONAL_CONTEXT=", \"hookSpecificOutput\": {\"hookEventName\": \"PostToolUse\", \"additionalContext\": \"$AGENT_MESSAGE\"}"
fi

# Output JSON with proper escaping
if command -v jq >/dev/null 2>&1; then
    echo "{\"continue\": true, \"systemMessage\": $(printf "%b" "$USER_MESSAGE" | jq -Rs .)$ADDITIONAL_CONTEXT}"
else
    # Fallback: escape quotes and preserve newlines as \n
    ESCAPED_MESSAGE=$(printf "%b" "$USER_MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}' | sed 's/\\n$//')
    echo "{\"continue\": true, \"systemMessage\": \"$ESCAPED_MESSAGE\"$ADDITIONAL_CONTEXT}"
fi

# Always exit successfully
exit 0

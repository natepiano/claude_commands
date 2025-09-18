#!/bin/bash

# Read the JSON input from stdin
INPUT=$(cat)

# Extract the file path from the JSON input
# The file path is in tool_input.file_path or tool_response.filePath depending on the tool
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_response.filePath // ""')

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

if [ $CHECK_RESULT -eq 0 ]; then
    # Check passed - might have warnings
    if [ -n "$WARNING_LOCATIONS" ]; then
        # Combine warnings with their locations
        WARNING_LIST=""
        IFS=$'\n'
        WARNING_ARRAY=($WARNINGS)
        LOCATION_ARRAY=($WARNING_LOCATIONS)
        for i in "${!WARNING_ARRAY[@]}"; do
            if [ $i -lt ${#LOCATION_ARRAY[@]} ]; then
                WARNING_LIST="${WARNING_LIST}  ⚠️ ${LOCATION_ARRAY[$i]}: ${WARNING_ARRAY[$i]}\n"
            fi
        done
        MESSAGE="✅ cargo check passed with $WARNING_COUNT warning(s):\n${WARNING_LIST%\\n}"
    else
        MESSAGE="🚀 cargo check passed"
    fi
    cargo +nightly fmt >/dev/null 2>&1
else
    # Check failed - show errors (and warnings if present)
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
        MESSAGE="💥 cargo check failed with $ERROR_COUNT error(s):\n${ERROR_LIST%\\n}"

        if [ -n "$WARNING_LOCATIONS" ]; then
            WARNING_LIST=""
            WARNING_ARRAY=($WARNINGS)
            WLOCATION_ARRAY=($WARNING_LOCATIONS)
            for i in "${!WARNING_ARRAY[@]}"; do
                if [ $i -lt ${#WLOCATION_ARRAY[@]} ]; then
                    WARNING_LIST="${WARNING_LIST}  ⚠️ ${WLOCATION_ARRAY[$i]}: ${WARNING_ARRAY[$i]}\n"
                fi
            done
            MESSAGE="$MESSAGE\nand $WARNING_COUNT warning(s):\n${WARNING_LIST%\\n}"
        fi
    else
        MESSAGE="💥 cargo check failed"
    fi
fi

# Output JSON with systemMessage that will be shown to user
# Use jq to properly escape the message for JSON if available, otherwise use sed
if command -v jq >/dev/null 2>&1; then
    echo "{\"continue\": true, \"systemMessage\": $(printf "%b" "$MESSAGE" | jq -Rs .)}"
else
    # Fallback: escape quotes and preserve newlines as \n
    ESCAPED_MESSAGE=$(printf "%b" "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}' | sed 's/\\n$//')
    echo "{\"continue\": true, \"systemMessage\": \"$ESCAPED_MESSAGE\"}"
fi

# Always exit successfully
exit 0

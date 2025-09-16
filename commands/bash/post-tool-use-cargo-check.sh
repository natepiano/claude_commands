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

# Capture cargo check output
OUTPUT=$(cargo check 2>&1)
CHECK_RESULT=$?

if [ $CHECK_RESULT -eq 0 ]; then
    MESSAGE="🚀 cargo check passed"
    cargo +nightly fmt >/dev/null 2>&1
elif echo "$OUTPUT" | grep -q "could not find \`Cargo.toml\`"; then
    # Not a Rust directory - exit silently
    echo '{"continue": true}'
    exit 0
else
    # Extract error locations (lines starting with -->)
    ERROR_LOCATIONS=$(echo "$OUTPUT" | grep "^ -->" | sed 's/^ --> //')

    if [ -n "$ERROR_LOCATIONS" ]; then
        # Format error locations as a compact list
        MESSAGE="💥 cargo check failed at:
$(echo "$ERROR_LOCATIONS" | sed 's/^/  • /')"
    else
        MESSAGE="💥 cargo check failed"
    fi
fi

# Output JSON with systemMessage that will be shown to user
# Use jq to properly escape the message for JSON if available, otherwise use sed
if command -v jq >/dev/null 2>&1; then
    echo "{\"continue\": true, \"systemMessage\": $(echo "$MESSAGE" | jq -Rs .)}"
else
    # Fallback: escape quotes and preserve newlines as \n
    ESCAPED_MESSAGE=$(echo "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}' | sed 's/\\n$//')
    echo "{\"continue\": true, \"systemMessage\": \"$ESCAPED_MESSAGE\"}"
fi

# Always exit successfully
exit 0

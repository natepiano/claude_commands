#!/bin/bash
# there's a bug when we use this pre-tool use which would be better
# that it shows the output twice which we definitely don't want
# so for now we're just doing this post tool - such a drag

# Check if called with --hook flag (for JSON output to hook system)
HOOK_MODE=false
if [ "$1" = "--hook" ]; then
    HOOK_MODE=true
fi

# Percentage chance to emit acknowledgement (0 or less = never, 100 or more = always)
ACK_CHANCE=25

# Path to acknowledgements file
ACK_FILE="$HOME/.claude/scripts/acknowledgements.txt"


# Check acknowledgement chance first
if [ "$ACK_CHANCE" -le 0 ]; then
    # Don't output anything - continue:true is the default
    exit 0
fi

# Generate random number 1-100 for percentage check
CHANCE_ROLL=$((RANDOM % 100 + 1))

# If roll is higher than our chance, don't emit
if [ "$CHANCE_ROLL" -gt "$ACK_CHANCE" ] && [ "$ACK_CHANCE" -lt 100 ]; then
    # Don't output anything - continue:true is the default
    exit 0
fi

# Check if file exists
if [ ! -f "$ACK_FILE" ]; then
    # Don't output anything - continue:true is the default
    exit 0
fi

# Get random line from file (excluding empty lines)
# Count non-empty lines
LINE_COUNT=$(grep -cv '^$' "$ACK_FILE")

# If no lines, exit
if [ "$LINE_COUNT" -eq 0 ]; then
    # Don't output anything - continue:true is the default
    exit 0
fi

# Generate random number between 1 and LINE_COUNT
RANDOM_LINE=$((RANDOM % LINE_COUNT + 1))

# Get that specific line and trim whitespace
RANDOM_ACK=$(grep -v '^$' "$ACK_FILE" | sed -n "${RANDOM_LINE}p" | tr -d '\n')

# If we got an acknowledgement, output it
if [ -n "$RANDOM_ACK" ]; then
    if [ "$HOOK_MODE" = true ]; then
        # JSON output for hook system
        # Use jq if available for proper JSON escaping
        if command -v jq >/dev/null 2>&1; then
            echo "{\"systemMessage\": $(echo -n "🎯 $RANDOM_ACK" | jq -Rs .)}"
        else
            # Fallback: basic escaping
            ESCAPED=$(echo -n "🎯 $RANDOM_ACK" | sed 's/\\/\\\\/g; s/"/\\"/g')
            echo "{ \"systemMessage\": \"$ESCAPED\"}"
        fi
    else
        # Direct output for command line usage (default)
        echo "🎯 $RANDOM_ACK"
    fi
else
    # Don't output anything - continue:true is the default
    exit 0
fi

exit 0

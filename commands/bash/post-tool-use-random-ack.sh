#!/bin/bash
# there's a bug when we use this pre-tool use which would be better
# that it shows the output twice which we definitely don't want
# so for now we're just doing this post tool - such a drag

# Path to acknowledgements file
ACK_FILE="$HOME/.claude/commands/bash/acknowledgements.txt"


# Check if file exists
if [ ! -f "$ACK_FILE" ]; then
    echo '{"continue": true}'
    exit 0
fi

# Get random line from file (excluding empty lines)
# Count non-empty lines
LINE_COUNT=$(grep -cv '^$' "$ACK_FILE")

# If no lines, exit
if [ "$LINE_COUNT" -eq 0 ]; then
    echo '{"continue": true}'
    exit 0
fi

# Generate random number between 1 and LINE_COUNT
RANDOM_LINE=$((RANDOM % LINE_COUNT + 1))

# Get that specific line and trim whitespace
RANDOM_ACK=$(grep -v '^$' "$ACK_FILE" | sed -n "${RANDOM_LINE}p" | tr -d '\n')

# If we got an acknowledgement, output it as JSON with systemMessage
if [ -n "$RANDOM_ACK" ]; then
    # Use jq if available for proper JSON escaping
    if command -v jq >/dev/null 2>&1; then
        echo "{\"systemMessage\": $(echo -n "🎯 $RANDOM_ACK" | jq -Rs .)}"
    else
        # Fallback: basic escaping
        ESCAPED=$(echo -n "🎯 $RANDOM_ACK" | sed 's/\\/\\\\/g; s/"/\\"/g')
        echo "{ \"systemMessage\": \"$ESCAPED\"}"
    fi
else
    echo '{"continue": true}'
fi

exit 0

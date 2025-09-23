#!/bin/bash

# Auto-compact hook to export session transcript
# This script runs before Claude Code compacts the conversation context

# Read the JSON input from stdin
INPUT=$(cat)

# Extract fields from JSON input
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')
TRIGGER=$(echo "$INPUT" | jq -r '.trigger')
CUSTOM_INSTRUCTIONS=$(echo "$INPUT" | jq -r '.custom_instructions // ""')

# Create exports directory if it doesn't exist
EXPORT_DIR="$HOME/.claude/transcript-exports"
mkdir -p "$EXPORT_DIR"

# Generate timestamp for filename
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# Determine filename based on trigger type
if [ "$TRIGGER" = "auto" ]; then
    EXPORT_FILE="$EXPORT_DIR/auto_compact_${TIMESTAMP}.json"
else
    EXPORT_FILE="$EXPORT_DIR/manual_compact_${TIMESTAMP}.json"
fi

# Copy the transcript file to the export location
if [ -f "$TRANSCRIPT_PATH" ]; then
    cp "$TRANSCRIPT_PATH" "$EXPORT_FILE"

    # Create a metadata file with additional info
    METADATA_FILE="${EXPORT_FILE%.json}_metadata.json"
    cat > "$METADATA_FILE" <<EOF
{
  "session_id": "$SESSION_ID",
  "trigger": "$TRIGGER",
  "timestamp": "$TIMESTAMP",
  "original_path": "$TRANSCRIPT_PATH",
  "custom_instructions": "$CUSTOM_INSTRUCTIONS"
}
EOF

    echo "Session transcript exported to: $EXPORT_FILE"
    echo "Trigger: $TRIGGER"

    # Keep only last 50 exports to prevent disk filling up
    EXPORT_COUNT=$(ls -1 "$EXPORT_DIR"/*.json 2>/dev/null | grep -v _metadata.json | wc -l)
    if [ "$EXPORT_COUNT" -gt 50 ]; then
        # Remove oldest files (keep newest 50)
        ls -1t "$EXPORT_DIR"/*.json | grep -v _metadata.json | tail -n +51 | xargs rm -f 2>/dev/null
        ls -1t "$EXPORT_DIR"/*_metadata.json | tail -n +51 | xargs rm -f 2>/dev/null
    fi
else
    echo "Warning: Transcript file not found at $TRANSCRIPT_PATH"
    exit 1
fi

exit 0
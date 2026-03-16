#!/bin/bash
# Verify that Bevy migration guides directory exists and contains .md files
#
# Usage: verify_migration_guides.sh <guides-dir>
# Exit codes: 0 = success, 1 = error

set -e

if [ $# -ne 1 ]; then
    echo "Error: Usage: verify_migration_guides.sh <guides-dir>" >&2
    exit 1
fi

GUIDES_DIR="$1"

# Check directory exists
if [ ! -d "$GUIDES_DIR" ]; then
    echo "Error: Migration guides directory not found at $GUIDES_DIR" >&2
    echo "The Bevy release may not include migration guides, or the repository structure may have changed." >&2
    exit 1
fi

# Count .md files (excludes .gitkeep and other non-guide files)
COUNT=$(find "$GUIDES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')

if [ "$COUNT" -eq 0 ]; then
    echo "Error: No migration guide (.md) files found in $GUIDES_DIR" >&2
    echo "The Bevy release may not include migration guides." >&2
    exit 1
fi

echo "Found $COUNT migration guide(s)"

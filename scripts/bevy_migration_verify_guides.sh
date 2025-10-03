#!/bin/bash
# Verify that Bevy migration guides directory exists and contains files
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

# Check directory is not empty
if [ -z "$(ls -A "$GUIDES_DIR" 2>/dev/null)" ]; then
    echo "Error: Migration guides directory is empty at $GUIDES_DIR" >&2
    echo "The Bevy release may not include migration guides." >&2
    exit 1
fi

# Success - output count
COUNT=$(ls "$GUIDES_DIR" | wc -l)
echo "Found $COUNT migration guide(s)"

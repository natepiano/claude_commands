#!/bin/bash
# count_pattern.sh - Generic pattern occurrence counter for migration analysis
#
# Usage:
#   count_pattern.sh <pattern> <codebase_path> [file_type]
#   count_pattern.sh --multiple <pattern1> <pattern2> ... -- <codebase_path> [file_type]
#
# Examples:
#   count_pattern.sh "SomeType" /path/to/code rust
#   count_pattern.sh --multiple "bevy/query" "bevy/spawn" -- /path/to/code rust
#
# Output:
#   Single pattern: Just the count number
#   Multiple patterns: JSON with pattern breakdown

set -euo pipefail

if [[ "$1" == "--multiple" ]]; then
    # Multiple pattern mode
    shift
    patterns=()
    while [[ "$1" != "--" ]]; do
        patterns+=("$1")
        shift
    done
    shift # skip the --
    codebase="$1"
    file_type="${2:-rust}"

    echo "{"
    total=0
    for i in "${!patterns[@]}"; do
        pattern="${patterns[$i]}"
        count=$(rg "$pattern" --type "$file_type" "$codebase" -c 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
        total=$((total + count))
        echo "  \"$pattern\": $count"
        [[ $i -lt $((${#patterns[@]} - 1)) ]] && echo ","
    done
    echo "  \"_total\": $total"
    echo "}"
else
    # Single pattern mode
    pattern="$1"
    codebase="$2"
    file_type="${3:-rust}"

    rg "$pattern" --type "$file_type" "$codebase" -c 2>/dev/null | awk -F: '{s+=$2} END {print s+0}'
fi

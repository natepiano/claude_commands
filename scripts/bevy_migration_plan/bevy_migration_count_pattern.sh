#!/bin/bash
# bevy_migration_count_pattern.sh - Generic pattern occurrence counter for migration analysis
#
# Usage:
#   bevy_migration_count_pattern.sh <pattern> <codebase_path> [file_type]
#   bevy_migration_count_pattern.sh --multiple <pattern1> <pattern2> ... -- <codebase_path> [file_type]
#   bevy_migration_count_pattern.sh --verify --pass1-total N --patterns <p1> <p2> ... -- <codebase_path> [file_type]
#
# Examples:
#   bevy_migration_count_pattern.sh "SomeType" /path/to/code rust
#   bevy_migration_count_pattern.sh --multiple "bevy/query" "bevy/spawn" -- /path/to/code rust
#   bevy_migration_count_pattern.sh --verify --pass1-total 92 --patterns "bevy/query" "bevy/spawn" -- /path/to/code rust
#
# Output:
#   Single pattern: Just the count number
#   Multiple patterns: JSON with pattern breakdown
#   Verify mode: JSON with validation results

set -euo pipefail

if [[ "$1" == "--verify" ]]; then
    # Verify mode - validate Pass 2 counts against Pass 1 total
    shift

    # Parse arguments
    if [[ "$1" != "--pass1-total" ]]; then
        echo "Error: --verify requires --pass1-total argument" >&2
        exit 1
    fi
    shift
    pass1_total="$1"
    shift

    if [[ "$1" != "--patterns" ]]; then
        echo "Error: --verify requires --patterns argument" >&2
        exit 1
    fi
    shift

    patterns=()
    while [[ "$1" != "--" ]]; do
        patterns+=("$1")
        shift
    done
    shift # skip the --
    codebase="$1"
    file_type="${2:-rust}"

    # Count all patterns
    echo "{"
    echo "  \"pass1_total\": $pass1_total,"
    echo "  \"breakdown\": {"
    total=0
    for i in "${!patterns[@]}"; do
        pattern="${patterns[$i]}"
        count=$(rg "$pattern" --type "$file_type" "$codebase" -c 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
        total=$((total + count))
        echo "    \"$pattern\": $count"
        [[ $i -lt $((${#patterns[@]} - 1)) ]] && echo "," || echo ""
    done
    echo "  },"
    echo "  \"pass2_total\": $total,"

    # Calculate variance
    if [[ $pass1_total -eq 0 ]]; then
        variance=0
        status="MATCH"
    else
        variance=$(awk "BEGIN {printf \"%.1f\", (($total - $pass1_total) / $pass1_total) * 100}")
        abs_variance=$(awk "BEGIN {printf \"%.1f\", sqrt(($variance)^2)}")
        if (( $(awk "BEGIN {print ($abs_variance <= 20)}") )); then
            status="MATCH"
        else
            status="ANOMALY"
        fi
    fi

    echo "  \"variance_percent\": $variance,"
    echo "  \"status\": \"$status\""
    echo "}"

elif [[ "$1" == "--multiple" ]]; then
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

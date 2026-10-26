#!/usr/bin/env bash
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

count_matches() {
    local matches status
    if matches=$(rg --count-matches --no-filename --type "$file_type" \
        --glob '!vendor/**' --glob '!**/vendor/**' \
        --glob '!target/**' --glob '!**/target/**' -- "$1" "$codebase"); then
        awk '{s+=$1} END {print s+0}' <<< "$matches"
    else
        status=$?
        if [[ $status -eq 1 ]]; then
            printf '0\n'
        else
            return "$status"
        fi
    fi
}

count_patterns() {
    counts=()
    total=0
    for pattern in "${patterns[@]}"; do
        count=$(count_matches "$pattern") || return $?
        counts+=("$count")
        total=$((total + count))
    done
}

json_key() {
    python3 -c 'import json, sys; print(json.dumps(sys.argv[1]), end="")' "$1"
}

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
    count_patterns
    echo "{"
    echo "  \"pass1_total\": $pass1_total,"
    echo "  \"breakdown\": {"
    separator=""
    for i in "${!patterns[@]}"; do
        pattern="${patterns[$i]}"
        printf '%s    %s: %s' "$separator" "$(json_key "$pattern")" "${counts[$i]}"
        separator=$',\n'
    done
    echo ""
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

    count_patterns
    echo "{"
    for i in "${!patterns[@]}"; do
        pattern="${patterns[$i]}"
        printf '  %s: %s,\n' "$(json_key "$pattern")" "${counts[$i]}"
    done
    echo "  \"_total\": $total"
    echo "}"
else
    # Single pattern mode
    pattern="$1"
    codebase="$2"
    file_type="${3:-rust}"

    count_matches "$pattern"
fi

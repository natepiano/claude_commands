#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/agents_config.sh"

usage() {
    echo "Usage: agent_admin.sh [status | <function> <codex|claude> | <function>.<subtask> <agent>[:<effort>]]" >&2
    return 1
}

if [[ "$#" -eq 0 ]]; then
    agents_list_assignments
elif [[ "$#" -eq 1 ]]; then
    [[ "$1" == "status" ]] || usage
    agents_list_assignments
elif [[ "$#" -eq 2 ]]; then
    if [[ "$1" == *.* ]]; then
        agents_set_row "$1" "$2"
    else
        agents_set_assignment "$1" "$2"
    fi
else
    usage
fi

#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/agents_config.sh"

# Unique skill names (the functions in [assignments]), sorted.
list_skills() {
    local assignment key seen=""
    while IFS= read -r assignment; do
        key="${assignment%%=*}"
        key="${key%%.*}"
        case " $seen " in
            *" $key "*) continue ;;
        esac
        seen="$seen $key"
        echo "$key"
    done < <(_agents_config_section_values assignments) | sort
}

# usage [function] — with a function, the examples use that function's real
# subtasks and current agent so they are copy-pasteable.
usage() {
    local fn="${1:-}" switched="${2:-}"
    local ex_fn="delegate" ex_other="claude" ex_row="delegate.escalation"
    local ex_pair="gpt-5.6-sol:max" family row_line
    if [[ -n "$switched" ]]; then
        [[ "$switched" == "codex" ]] && ex_other="claude" || ex_other="codex"
    fi
    if [[ -n "$fn" ]]; then
        family="$(_agents_registry_get assignments "$fn")"
        if [[ -n "$family" ]]; then
            ex_fn="$fn"
            [[ "$family" == "codex" ]] && ex_other="claude" || ex_other="codex"
            row_line=""
            IFS= read -r row_line < <(_agents_config_section_values "$fn.$family") || true
            if [[ -n "$row_line" ]]; then
                ex_row="$fn.${row_line%%=*}"
                ex_pair="$(agents_config_trim "${row_line#*=}")"
            fi
        fi
    fi
    cat <<EOF
Usage: agent_admin.sh [skills | <function> | <family>] | <function> <codex|claude> | <function>.<subtask> <agent>[:<effort>]

  (no args)                print every function, its family, and its resolved rows
  skills                   print the list of configured skills for use with agents
  <function>               print just that function's rows
  <family>                 switch every function to the codex or claude family
  <function> <family>      switch a whole function to the codex or claude family
                           — the only thing that changes which rows are live
  <function>.<subtask> <agent>[:<effort>]
                           edit one row; the agent names its own family, so
                           naming a dormant family's agent edits that row and
                           says so rather than erroring. Omit :<effort> to use
                           the agent CLI default

Examples:
  agent_admin.sh $ex_fn
  agent_admin.sh $ex_other   # switch every function at once
  agent_admin.sh $ex_fn $ex_other
  agent_admin.sh $ex_row $ex_pair
  agent_admin.sh $ex_row ${ex_pair%%:*}   # keep the agent CLI default effort

Functions/subtasks come from [assignments] and [<function>.<family>] in
config/agents.conf; valid agents and efforts from [<family>.agents].
EOF
}

# Accept the user-facing command name while preserving the canonical registry
# key used by team_review's consumers.
if [[ "$#" -ge 1 && "$1" != "skills" ]]; then
    command_name="$1"
    case "$command_name" in
        teamreview)
            shift
            set -- team_review "$@"
            ;;
        teamreview.*)
            shift
            set -- "team_review.${command_name#*.}" "$@"
            ;;
    esac
fi

if [[ "$#" -eq 0 ]]; then
    agents_list_assignments
    echo ""
    usage
elif [[ "$#" -eq 1 ]]; then
    if [[ "$1" == *.* ]]; then
        usage >&2
        exit 1
    fi
    if [[ "$1" == "skills" ]]; then
        list_skills
        exit 0
    fi
    # A bare family name switches everything; no function is ever named for one.
    if _agents_config_has_section "$1.agents"; then
        agents_set_all_assignments "$1"
        echo "# switched every function to $1"
        agents_list_assignments
        echo ""
        usage "" "$1"
        exit 0
    fi
    agents_list_function "$1"
    echo ""
    usage "$1"
elif [[ "$#" -eq 2 ]]; then
    if [[ "$1" == *.* ]]; then
        agents_set_row "$1" "$2"
        fn="${1%%.*}"
        if [[ "$AGENT_ROW_ACTIVE" == "yes" ]]; then
            echo "# updated [$fn.$AGENT_ROW_FAMILY] $1 — live"
        else
            echo "# updated [$fn.$AGENT_ROW_FAMILY] $1 — dormant:" \
                "$1 runs on $AGENT_ROW_ACTIVE_FAMILY. Make it live with:" \
                "agent_admin.sh $fn $AGENT_ROW_FAMILY"
        fi
    else
        agents_set_assignment "$1" "$2"
        fn="$1"
    fi
    agents_list_function "$fn"
    echo ""
    usage "$fn"
else
    usage >&2
    exit 1
fi

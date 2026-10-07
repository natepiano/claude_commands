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
    local ex_fn="delegate" ex_other="claude" ex_row="delegate.review"
    local ex_pair="gpt-5.6-sol:max" family row_line
    if [[ -n "$switched" ]]; then
        [[ "$switched" == "codex" ]] && ex_other="claude" || ex_other="codex"
    fi
    if [[ -n "$fn" ]]; then
        # A caller function's example rows come from the family that would run
        # from here; from outside any session, codex stands in.
        family="$(_agents_concrete_family "$(_agents_registry_get assignments "$fn")")"
        [[ -z "$family" && "$(_agents_registry_get assignments "$fn")" == "$AGENTS_CALLER_ASSIGNMENT" ]] && family="codex"
        if [[ -n "$family" ]]; then
            ex_fn="$fn"
            [[ "$family" == "codex" ]] && ex_other="claude" || ex_other="codex"
            # Take the first row without abandoning the producer mid-write --
            # a single `read` from a process substitution closes the pipe and
            # every later printf in it reports a broken pipe. See the note on
            # _agents_registry_get in agents_config.sh.
            row_line="$(_agents_config_section_values "$fn.$family")"
            row_line="${row_line%%$'\n'*}"
            if [[ -n "$row_line" ]]; then
                ex_row="$fn.${row_line%%=*}"
                ex_pair="$(agents_config_trim "${row_line#*=}")"
            fi
        fi
    fi
    cat <<EOF
Usage: agent_admin.sh [skills | <function> | <family> | <agent> | <tier>] | <function> <codex|claude|agent|tier> | <function>.<subtask> <agent>[:<effort>|tier]

  (no args)                print every function, its family, and its resolved rows
  skills                   print the list of configured skills for use with agents
  <function>               print just that function's rows
  <family>                 switch every function to the codex or claude family
  <agent>                  put every function on one agent, keeping each row's
                           effort; the agent names its family, so this also
                           switches every function to that family
  <function> <family>      switch a whole function to the codex or claude family
                           — the only thing that changes which rows are live.
                           A function assigned 'caller' (ask_a_friend) has no
                           switch: it runs on the family of the agent asking,
                           so both of its row sets are live, one per family
  <function> <agent>       put one function on one agent, keeping each row's
                           effort and switching it to the agent's family
  <function>.<subtask> <agent>[:<effort>]
                           edit one row; the agent names its own family, so
                           naming a dormant family's agent edits that row and
                           says so rather than erroring. Omit :<effort> to use
                           the agent CLI default
  [<function>[.<subtask>]] <tier>
                           set the codex speed tier for every function, one
                           function, or one row: fast, flex, default, pace
                           (codex_pacer.py picks fast or default so the
                           quota lasts to the next weekly reset), or
                           inherit (drop the key; a row then follows its
                           function, a function ~/.codex/config.toml). A wider
                           level clears the row keys beneath it. Claude rows
                           never take a tier: Claude fast mode bills extra usage

Examples:
  agent_admin.sh $ex_fn
  agent_admin.sh $ex_other   # switch every function at once
  agent_admin.sh ${ex_pair%%:*}   # every function on one agent, efforts kept
  agent_admin.sh $ex_fn $ex_other
  agent_admin.sh $ex_row $ex_pair
  agent_admin.sh $ex_row ${ex_pair%%:*}   # keep the agent CLI default effort
  agent_admin.sh $ex_fn fast   # codex speed tier for one function
  agent_admin.sh $ex_row inherit   # the row follows its function's tier

Functions/subtasks come from [assignments] and [<function>.<family>] in
config/agents.conf; valid agents and efforts from [<family>.agents].
EOF
}

is_tier() {
    case "$1" in
        fast|flex|default|pace|inherit) return 0 ;;
    esac
    return 1
}

# What a codex row with no registry tier follows.
codex_config_note() {
    local tier
    tier="$(_agents_codex_config_tier)"
    if [[ -n "$tier" ]]; then
        printf '~/.codex/config.toml: %s' "$tier"
    else
        printf '~/.codex/config.toml, which sets no service_tier'
    fi
}

# Whether a tier written for <function> (or <function>.<subtask>) runs today.
tier_liveness() {
    local fn="$1" subtask="${2:-}" raw
    if [[ -n "$subtask" ]]; then
        raw="$(_agents_active_family "$fn" "$subtask")"
    else
        raw="$(_agents_registry_get assignments "$fn")"
    fi
    if [[ "$raw" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
        echo "live whenever a codex session runs $fn"
    elif [[ "$raw" == "codex" ]]; then
        echo "live"
    else
        echo "dormant: ${fn}${subtask:+.$subtask} runs on $raw. Make it live with: agent_admin.sh $fn codex"
    fi
}

# Set a tier and print the `#` line that reports it.
set_tier() {
    local tier="$1" scope="${2:-}" fn subtask="" dormant=""
    agents_set_service_tier "$tier" "$scope"
    if [[ -z "$scope" ]]; then
        for fn in $AGENT_TIER_FUNCTIONS; do
            [[ "$(_agents_registry_get assignments "$fn")" == "claude" ]] && dormant="$dormant, $fn"
        done
        if [[ "$tier" == "inherit" ]]; then
            echo "# cleared every function's codex tier — codex rows follow $(codex_config_note)"
        elif [[ -n "$dormant" ]]; then
            echo "# set codex_service_tier=$tier for every function — dormant for${dormant#,}: they run on claude"
        else
            echo "# set codex_service_tier=$tier for every function — live"
        fi
        return 0
    fi
    fn="${scope%%.*}"
    [[ "$scope" == *.* ]] && subtask="${scope#*.}"
    if [[ "$tier" != "inherit" ]]; then
        echo "# set [$fn.options] codex_service_tier${subtask:+.$subtask}=$tier — $(tier_liveness "$fn" "$subtask")"
    elif [[ -z "$subtask" ]]; then
        echo "# cleared $fn's codex tier — its codex rows follow $(codex_config_note)"
    elif [[ -n "$(_agents_registry_get "$fn.options" codex_service_tier)" ]]; then
        echo "# cleared $scope's own tier — it follows [$fn.options] codex_service_tier: $(_agents_registry_get "$fn.options" codex_service_tier)"
    else
        echo "# cleared $scope's own tier — it follows $(codex_config_note)"
    fi
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
    if is_tier "$1"; then
        set_tier "$1"
        agents_list_assignments
        echo ""
        usage
        exit 0
    fi
    # A bare family name switches everything; no function is ever named for one.
    if _agents_config_has_section "$1.agents"; then
        agents_set_all_assignments "$1"
        echo "# switched every function to $1"
        for fn in $AGENT_KEPT_FUNCTIONS; do
            echo "# kept $fn on $(_agents_pinned_family "$fn"): its only set"
        done
        agents_list_assignments
        echo ""
        usage "" "$1"
        exit 0
    fi
    # A bare agent name puts every function on it; the agent names its family.
    if [[ -n "$(_agents_agent_families_inline "$1")" ]]; then
        agents_set_model "$1"
        echo "# switched every function to $1 ($AGENT_SWEEP_FAMILY), efforts kept"
        for fn in $AGENT_KEPT_FUNCTIONS; do
            echo "# kept $fn on $(_agents_pinned_family "$fn"): its only set"
        done
        agents_list_assignments
        echo ""
        usage "" "$AGENT_SWEEP_FAMILY"
        exit 0
    fi
    agents_list_function "$1"
    echo ""
    usage "$1"
elif [[ "$#" -eq 2 ]]; then
    if is_tier "$2"; then
        set_tier "$2" "$1"
        fn="${1%%.*}"
    elif [[ "$1" == *.* ]]; then
        agents_set_row "$1" "$2"
        fn="${1%%.*}"
        if [[ "$AGENT_ROW_ASSIGNMENT" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
            echo "# updated [$fn.$AGENT_ROW_FAMILY] $1 — live whenever a $AGENT_ROW_FAMILY session runs $fn"
        elif [[ "$AGENT_ROW_ACTIVE" == "yes" ]]; then
            echo "# updated [$fn.$AGENT_ROW_FAMILY] $1 — live"
        else
            echo "# updated [$fn.$AGENT_ROW_FAMILY] $1 — dormant:" \
                "$1 runs on $AGENT_ROW_ACTIVE_FAMILY. Make it live with:" \
                "agent_admin.sh $fn $AGENT_ROW_FAMILY"
        fi
    elif ! _agents_config_has_section "$2.agents" \
        && [[ -n "$(_agents_agent_families_inline "$2")" ]]; then
        agents_set_model "$2" "$1"
        fn="$1"
        if [[ "$(_agents_registry_get assignments "$fn")" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
            echo "# set [$fn.$AGENT_SWEEP_FAMILY] to $2, efforts kept — live whenever a $AGENT_SWEEP_FAMILY session runs $fn"
        else
            echo "# switched $fn to $2 ($AGENT_SWEEP_FAMILY), efforts kept"
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

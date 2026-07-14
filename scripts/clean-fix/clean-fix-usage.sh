#!/usr/bin/env bash
# Emit the no-argument /clean_fix usage screen.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"
MARKER="#CLEAN_FIX_SKIP#"

source "$SCRIPT_DIR/agent_assignments.sh"

USAGE_COMMANDS=()
USAGE_DESCRIPTIONS=()
USAGE_BREAK_AFTER=()
PROJECT_NAMES=()
PROJECT_KEYS=()
CLEAN_STATUSES=()
STYLE_STATUSES=()
ACTIVE_CHECKOUT_KEYS=()
ACTIVE_CHECKOUT_VALUES=()

add_usage() {
    USAGE_COMMANDS+=("$1")
    USAGE_DESCRIPTIONS+=("$2")
    USAGE_BREAK_AFTER+=("${3:-false}")
}

load_usage_rows() {
    USAGE_COMMANDS=()
    USAGE_DESCRIPTIONS=()
    USAGE_BREAK_AFTER=()

    add_usage "clean_fix run [project]" "Clean/build/warmup and style eval/review/fix. Optional project filters to one target."
    add_usage "clean_fix run clean [project]" "Clean + build + mend + warmup. Optional project filters to one clean target."
    add_usage "clean_fix run style [project]" "Style eval + review + fix worktrees. Optional project filters to one style target." true
    add_usage "clean_fix run_once" "Runs one eval + review + fix pass across all configured style projects, regardless of stage enablement." true
    add_usage "clean_fix add <path-or-project>" "Adds a Rust project to clean and style allowlists. Workspace members use workspace-relative entries." true
    add_usage "clean_fix rename <old> <new>" "Renames a clean-fix project key and migrates history, pending state, and markers." true
    add_usage "clean_fix monitor" "Watches the latest clean-fix log modified in the last 2 hours." true
    add_usage "clean_fix report" "Shows the newest clean-fix report. Use clean_fix list to choose an older report."
    add_usage "clean_fix list" "Lists reportable logs. Same as clean_fix report list." true
    add_usage "clean_fix eval" "Shows eval stage family/agent status. Also works for review and fix."
    add_usage "clean_fix agent" "Shows all stage family, resolved agent, and effort assignments."
    add_usage "/agent cleanfix <family>" "Switches the clean-fix family in the shared agent registry."
    add_usage "/agent cleanfix.<stage> <agent>[:<effort>]" "Edits a clean-fix stage row; stages are style_eval, style_eval_review, and style_fix." true
    add_usage "clean_fix on" "Enables all style stages."
    add_usage "clean_fix off" "Disables all style stages."
    add_usage "clean_fix eval on" "Enables one stage. Also works for review and fix."
    add_usage "clean_fix eval off" "Disables one stage. Also works for review and fix." true
    add_usage "clean_fix skip clean" "Shows targets currently skipped from the clean pass."
    add_usage "clean_fix skip style" "Shows targets currently skipped from the style pass."
    add_usage "clean_fix skip clean <target>..." "Temporarily skips clean targets."
    add_usage "clean_fix skip style <target>..." "Temporarily skips style projects."
    add_usage "clean_fix skip clean enable <target>..." "Re-enables clean targets. Use enable-all to restore every temporary clean skip."
    add_usage "clean_fix skip style enable <target>..." "Re-enables style projects. Use enable-all to restore every temporary style skip."
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    printf '%s' "$value"
}

json_string() {
    printf '"%s"' "$(json_escape "$1")"
}

print_usage_json() {
    local i

    printf '  "usage": [\n'
    for ((i = 0; i < ${#USAGE_COMMANDS[@]}; i++)); do
        if [[ $i -gt 0 ]]; then
            printf ',\n'
        fi
        printf '    {"command": '
        json_string "${USAGE_COMMANDS[$i]}"
        printf ', "what_happens": '
        json_string "${USAGE_DESCRIPTIONS[$i]}"
        printf ', "break_after": %s' "${USAGE_BREAK_AFTER[$i]}"
        printf '}'
    done
    printf '\n  ]'
}

print_stage_json() {
    local label="$1"
    local section="$2"
    local enabled="" family="" agent="" effort="" status=""

    cf_load_stage_assignment "$section" enabled family agent effort
    if [[ "$enabled" == "true" ]]; then
        status="ENABLED"
    else
        status="DISABLED"
    fi
    printf '{"stage": '
    json_string "$label"
    printf ', "status": '
    json_string "$status"
    printf ', "family": '
    json_string "$family"
    printf ', "agent": '
    json_string "${agent:-<default>}"
    printf ', "effort": '
    json_string "${effort:-<default>}"
    printf '}'
}

print_agents_json() {
    printf '  "agents": [\n    '
    print_stage_json "eval" "style_eval"
    printf ',\n    '
    print_stage_json "review" "style_eval_review"
    printf ',\n    '
    print_stage_json "fix" "style_fix"
    printf '\n  ]'
}

entry_key() {
    local entry="$1"
    if [[ "$entry" == */* ]]; then
        printf '%s\n' "${entry##*/}"
    else
        printf '%s\n' "$entry"
    fi
}

checkout_root() {
    local checkout="$1"
    printf '%s\n' "${checkout%%/*}"
}

active_checkout_for_entry() {
    local entry="$1"
    local i
    for ((i = 0; i < ${#ACTIVE_CHECKOUT_KEYS[@]}; i++)); do
        if [[ "${ACTIVE_CHECKOUT_KEYS[$i]}" == "$entry" ]]; then
            printf '%s\n' "${ACTIVE_CHECKOUT_VALUES[$i]}"
            return
        fi
    done
    printf '%s\n' "$entry"
}

project_display_for_entry() {
    local entry="$1"
    local checkout
    checkout="$(active_checkout_for_entry "$entry")"
    if [[ "$checkout" != "$entry" ]]; then
        checkout_root "$checkout"
    else
        entry_key "$entry"
    fi
}

active_redirect_index_for_build_entry() {
    local entry="$1"
    local i checkout root
    for ((i = 0; i < ${#ACTIVE_CHECKOUT_KEYS[@]}; i++)); do
        checkout="${ACTIVE_CHECKOUT_VALUES[$i]}"
        root="$(checkout_root "$checkout")"
        if [[ "$entry" == "${ACTIVE_CHECKOUT_KEYS[$i]}" || "$entry" == "$checkout" || "$entry" == "$root" || "$entry" == "$root/"* ]]; then
            printf '%s\n' "$i"
            return
        fi
    done
    printf '%s\n' "-1"
}

project_key_for_build_entry() {
    local entry="$1"
    local index
    index="$(active_redirect_index_for_build_entry "$entry")"
    if [[ "$index" == "-1" ]]; then
        entry_key "$entry"
    else
        entry_key "${ACTIVE_CHECKOUT_KEYS[$index]}"
    fi
}

project_display_for_build_entry() {
    local entry="$1"
    local index
    index="$(active_redirect_index_for_build_entry "$entry")"
    if [[ "$index" == "-1" ]]; then
        entry_key "$entry"
    else
        checkout_root "${ACTIVE_CHECKOUT_VALUES[$index]}"
    fi
}

find_project_index() {
    local wanted="$1"
    local i
    for ((i = 0; i < ${#PROJECT_NAMES[@]}; i++)); do
        if [[ "${PROJECT_KEYS[$i]}" == "$wanted" ]]; then
            printf '%s\n' "$i"
            return
        fi
    done
    printf '%s\n' "-1"
}

merge_status() {
    local current="$1"
    local incoming="$2"
    if [[ "$current" == "ACTIVE" || "$incoming" == "ACTIVE" ]]; then
        printf '%s\n' "ACTIVE"
    elif [[ "$current" == "SKIP" || "$incoming" == "SKIP" ]]; then
        printf '%s\n' "SKIP"
    else
        printf '%s\n' "-"
    fi
}

set_project_status() {
    local name="$1"
    local key="$2"
    local column="$3"
    local value="$4"
    local found
    found="$(find_project_index "$key")"
    if [[ "$found" == "-1" ]]; then
        PROJECT_NAMES+=("$name")
        PROJECT_KEYS+=("$key")
        CLEAN_STATUSES+=("-")
        STYLE_STATUSES+=("-")
        found=$((${#PROJECT_NAMES[@]} - 1))
    elif [[ "$name" != "$key" && "${PROJECT_NAMES[$found]}" == "$key" ]]; then
        PROJECT_NAMES[$found]="$name"
    fi
    if [[ "$column" == "clean" ]]; then
        CLEAN_STATUSES[$found]="$(merge_status "${CLEAN_STATUSES[$found]}" "$value")"
    else
        STYLE_STATUSES[$found]="$(merge_status "${STYLE_STATUSES[$found]}" "$value")"
    fi
}

sort_projects() {
    local i j
    local tmp_name tmp_key tmp_clean tmp_style

    for ((i = 0; i < ${#PROJECT_NAMES[@]}; i++)); do
        for ((j = i + 1; j < ${#PROJECT_NAMES[@]}; j++)); do
            if [[ "${PROJECT_NAMES[$j]}" < "${PROJECT_NAMES[$i]}" || ( "${PROJECT_NAMES[$j]}" == "${PROJECT_NAMES[$i]}" && "${PROJECT_KEYS[$j]}" < "${PROJECT_KEYS[$i]}" ) ]]; then
                tmp_name="${PROJECT_NAMES[$i]}"
                tmp_key="${PROJECT_KEYS[$i]}"
                tmp_clean="${CLEAN_STATUSES[$i]}"
                tmp_style="${STYLE_STATUSES[$i]}"
                PROJECT_NAMES[$i]="${PROJECT_NAMES[$j]}"
                PROJECT_KEYS[$i]="${PROJECT_KEYS[$j]}"
                CLEAN_STATUSES[$i]="${CLEAN_STATUSES[$j]}"
                STYLE_STATUSES[$i]="${STYLE_STATUSES[$j]}"
                PROJECT_NAMES[$j]="$tmp_name"
                PROJECT_KEYS[$j]="$tmp_key"
                CLEAN_STATUSES[$j]="$tmp_clean"
                STYLE_STATUSES[$j]="$tmp_style"
            fi
        done
    done
}

load_active_checkouts() {
    local line="" section="" body="" key="" value=""

    ACTIVE_CHECKOUT_KEYS=()
    ACTIVE_CHECKOUT_VALUES=()

    while IFS= read -r line || [[ -n "$line" ]]; do
        body="$(cf_trim "$line")"
        if [[ "$body" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "active_checkout" ]] || continue
        body="${body%%#*}"
        body="$(cf_trim "$body")"
        [[ -n "$body" && "$body" == *=* ]] || continue
        key="$(cf_trim "${body%%=*}")"
        value="$(cf_trim "${body#*=}")"
        [[ -n "$key" && -n "$value" ]] || continue
        ACTIVE_CHECKOUT_KEYS+=("$key")
        ACTIVE_CHECKOUT_VALUES+=("$value")
    done < "$CONF_FILE"
}

load_projects() {
    local line="" section="" body="" status="" key="" name=""

    PROJECT_NAMES=()
    PROJECT_KEYS=()
    CLEAN_STATUSES=()
    STYLE_STATUSES=()
    load_active_checkouts

    while IFS= read -r line || [[ -n "$line" ]]; do
        body="$(cf_trim "$line")"
        if [[ "$body" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "build" || "$section" == "projects" ]] || continue

        status="ACTIVE"
        if [[ "$body" == "$MARKER"* ]]; then
            status="SKIP"
            body="${body#"$MARKER"}"
            body="$(cf_trim "$body")"
        fi
        body="${body%%#*}"
        body="$(cf_trim "$body")"
        [[ -n "$body" ]] || continue

        if [[ "$section" == "build" ]]; then
            key="$(project_key_for_build_entry "$body")"
            name="$(project_display_for_build_entry "$body")"
            set_project_status "$name" "$key" "clean" "$status"
        else
            key="$(entry_key "$body")"
            name="$(project_display_for_entry "$body")"
            set_project_status "$name" "$key" "style" "$status"
        fi
    done < "$CONF_FILE"

    sort_projects
}

print_projects_json() {
    local i key_cell

    printf '  "projects": [\n'
    for ((i = 0; i < ${#PROJECT_NAMES[@]}; i++)); do
        if [[ $i -gt 0 ]]; then
            printf ',\n'
        fi
        key_cell="$(project_key_cell "$i")"
        printf '    {"project": '
        json_string "${PROJECT_NAMES[$i]}"
        printf ', "project_key": '
        json_string "$key_cell"
        printf ', "clean": '
        json_string "${CLEAN_STATUSES[$i]}"
        printf ', "style": '
        json_string "${STYLE_STATUSES[$i]}"
        printf '}'
    done
    printf '\n  ]'
}

max_usage_command_width() {
    local max=7
    local i len
    for ((i = 0; i < ${#USAGE_COMMANDS[@]}; i++)); do
        len=${#USAGE_COMMANDS[$i]}
        if ((len > max)); then
            max=$len
        fi
    done
    printf '%s\n' "$max"
}

print_wrapped_row() {
    local left="$1"
    local right="$2"
    local left_width="$3"
    local total_width="$4"
    local right_width=$((total_width - left_width - 2))
    local current_left="$left"
    local line="" word
    local words=()

    IFS=' ' read -r -a words <<< "$right"
    if [[ ${#words[@]} -eq 0 ]]; then
        printf "%-*s\n" "$left_width" "$current_left"
        return
    fi

    for word in "${words[@]}"; do
        if [[ -z "$line" ]]; then
            line="$word"
        elif ((${#line} + 1 + ${#word} <= right_width)); then
            line+=" $word"
        else
            printf "%-*s  %s\n" "$left_width" "$current_left" "$line"
            current_left=""
            line="$word"
        fi
    done
    printf "%-*s  %s\n" "$left_width" "$current_left" "$line"
}

repeat_dash() {
    local count="$1"
    local line=""
    printf -v line '%*s' "$count" ''
    printf '%s' "${line// /-}"
}

print_usage_rule() {
    local left_width="$1"
    local total_width="$2"
    local right_width=$((total_width - left_width - 2))

    repeat_dash "$left_width"
    printf '  '
    repeat_dash "$right_width"
    printf '\n'
}

print_usage_text() {
    local command_width total_width i
    command_width="$(max_usage_command_width)"
    total_width=108

    printf '## Usage\n\n'
    printf '```text\n'
    print_wrapped_row "Command" "What happens" "$command_width" "$total_width"
    print_usage_rule "$command_width" "$total_width"
    for ((i = 0; i < ${#USAGE_COMMANDS[@]}; i++)); do
        print_wrapped_row "${USAGE_COMMANDS[$i]}" "${USAGE_DESCRIPTIONS[$i]}" "$command_width" "$total_width"
        if [[ "${USAGE_BREAK_AFTER[$i]}" == "true" ]]; then
            printf '\n'
        fi
    done
    printf '```\n'
}

print_stage_text() {
    local label="$1"
    local section="$2"
    local enabled="" family="" agent="" effort="" status=""

    cf_load_stage_assignment "$section" enabled family agent effort
    if [[ "$enabled" == "true" ]]; then
        status="ENABLED"
    else
        status="DISABLED"
    fi
    printf "%-7s %-8s %-7s %-12s %s\n" "$label" "$status" "$family" "${agent:-<default>}" "${effort:-<default>}"
}

print_agents_rule() {
    repeat_dash 7
    printf ' '
    repeat_dash 8
    printf ' '
    repeat_dash 7
    printf ' '
    repeat_dash 12
    printf ' '
    repeat_dash 6
    printf '\n'
}

print_agents_text() {
    printf '## Agents\n\n'
    printf '```text\n'
    printf "%-7s %-8s %-7s %-12s %s\n" "Stage" "Status" "Family" "Agent" "Effort"
    print_agents_rule
    print_stage_text "eval" "style_eval"
    print_stage_text "review" "style_eval_review"
    print_stage_text "fix" "style_fix"
    printf '```\n'
}

max_project_width() {
    local max=7
    local i len
    for ((i = 0; i < ${#PROJECT_NAMES[@]}; i++)); do
        len=${#PROJECT_NAMES[$i]}
        if ((len > max)); then
            max=$len
        fi
    done
    printf '%s\n' "$max"
}

max_project_key_width() {
    local max=11
    local i len key_cell
    for ((i = 0; i < ${#PROJECT_KEYS[@]}; i++)); do
        key_cell="$(project_key_cell "$i")"
        len=${#key_cell}
        if ((len > max)); then
            max=$len
        fi
    done
    printf '%s\n' "$max"
}

print_projects_rule() {
    local project_width="$1"
    local key_width="$2"

    repeat_dash "$project_width"
    printf '  '
    repeat_dash "$key_width"
    printf '  '
    repeat_dash 6
    printf '  '
    repeat_dash 5
    printf '\n'
}

project_key_cell() {
    local index="$1"
    if [[ "${PROJECT_KEYS[$index]}" == "${PROJECT_NAMES[$index]}" ]]; then
        printf '%s' ""
    else
        printf '%s' "${PROJECT_KEYS[$index]}"
    fi
}

print_projects_text() {
    local project_width project_key_width i key_cell
    project_width="$(max_project_width)"
    project_key_width="$(max_project_key_width)"

    printf '## Projects\n\n'
    printf '```text\n'
    printf "%-*s  %-*s  %-6s  %s\n" "$project_width" "Project" "$project_key_width" "Project Key" "Clean" "Style"
    print_projects_rule "$project_width" "$project_key_width"
    for ((i = 0; i < ${#PROJECT_NAMES[@]}; i++)); do
        key_cell="$(project_key_cell "$i")"
        printf "%-*s  %-*s  %-6s  %s\n" "$project_width" "${PROJECT_NAMES[$i]}" "$project_key_width" "$key_cell" "${CLEAN_STATUSES[$i]}" "${STYLE_STATUSES[$i]}"
    done
    printf '```\n'
}

print_text() {
    print_usage_text
    printf '\n'
    print_agents_text
    printf '\n'
    print_projects_text
}

print_json() {
    printf '{\n'
    print_usage_json
    printf ',\n'
    print_agents_json
    printf ',\n'
    print_projects_json
    printf '\n}\n'
}

main() {
    load_usage_rows
    load_projects

    case "${1:-}" in
        "")
            print_text
            ;;
        --json)
            shift
            if [[ $# -ne 0 ]]; then
                echo "ERROR: --json does not take additional arguments" >&2
                return 2
            fi
            print_json
            ;;
        *)
            echo "Usage: clean-fix-usage.sh [--json]" >&2
            return 2
            ;;
    esac
}

main "$@"

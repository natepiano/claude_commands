#!/usr/bin/env bash
# Clean-fix stage assignment reader.
#
# Clean-fix owns stage enablement. The global agent registry
# (~/.claude/config/agents.conf) owns family, agent, and effort assignments.

CLEAN_FIX_AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEAN_FIX_AGENT_ASSIGNMENTS_FILE="${CLEAN_FIX_AGENT_ASSIGNMENTS_FILE:-$CLEAN_FIX_AGENT_DIR/agent-assignments.conf}"

source "$CLEAN_FIX_AGENT_DIR/../agents/agents_config.sh"

cf_trim() { agents_config_trim "$1"; }

# cf_resolve_checkout <projects-entry>
# Echo the checkout path clean-fix should evaluate/fix for a [projects] entry:
# its [active_checkout] override if one is set (e.g. a worktree), else the entry
# itself. Identity/history keys derive from the entry, not this path, so a
# redirect keeps a project's history while pointing work at a different checkout.
# Reads the parallel arrays cf_ac_keys / cf_ac_vals, which each caller fills from
# the [active_checkout] section in its own conf parse loop.
cf_resolve_checkout() {
    local entry="$1" i
    for ((i = 0; i < ${#cf_ac_keys[@]}; i++)); do
        if [[ "${cf_ac_keys[$i]}" == "$entry" ]]; then
            printf '%s' "${cf_ac_vals[$i]}"
            return 0
        fi
    done
    printf '%s' "$entry"
}

cf_validate_bool() {
    local section="$1"
    local key="$2"
    local value="$3"
    case "$value" in
        true|false) return 0 ;;
        *)
            echo "ERROR: [$section] $key must be true or false, got: $value" >&2
            return 1
            ;;
    esac
}

# cf_load_stage_assignment <section> <enabled_var> <agent_var> <model_var> <effort_var>
cf_load_stage_assignment() {
    local want_section="$1"
    local enabled_var="$2"
    local agent_var="$3"
    local model_var="$4"
    local effort_var="$5"
    local line stripped section
    local parsed_enabled=""
    local resolved_family resolved_model resolved_effort

    if [[ ! -f "$CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" ]]; then
        echo "ERROR: clean-fix agent assignment file not found: $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
        return 1
    fi

    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(cf_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_section" ]] || continue

        if [[ "$stripped" =~ ^enabled=(.+)$ ]]; then
            parsed_enabled="${BASH_REMATCH[1]}"
        fi
    done < "$CLEAN_FIX_AGENT_ASSIGNMENTS_FILE"

    if [[ -z "$parsed_enabled" ]]; then
        echo "ERROR: [$want_section] enabled must be set to true or false in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
        return 1
    fi
    cf_validate_bool "$want_section" enabled "$parsed_enabled" || return 1
    agents_resolve "cleanfix.$want_section" || return 1
    resolved_family="$AGENT_FAMILY"
    resolved_model="$AGENT_MODEL"
    resolved_effort="$AGENT_EFFORT"

    printf -v "$enabled_var" '%s' "$parsed_enabled"
    printf -v "$agent_var" '%s' "$resolved_family"
    printf -v "$model_var" '%s' "$resolved_model"
    printf -v "$effort_var" '%s' "$resolved_effort"
}

cf_print_stage_assignment() {
    local section="$1"
    local enabled="" agent="" model="" effort=""
    cf_load_stage_assignment "$section" enabled agent model effort || return 1
    printf '%-18s enabled=%-5s agent=%-6s model=%-12s effort=%s\n' \
        "$section" "$enabled" "$agent" "${model:-<default>}" "${effort:-<default>}"
}

cf_print_agent_assignments() {
    echo "clean-fix assignments: $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE"
    echo "global agent registry: $AGENTS_CONFIG_FILE"
    cf_print_stage_assignment style_eval
    cf_print_stage_assignment style_eval_review
    cf_print_stage_assignment style_fix
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    cf_print_agent_assignments
fi

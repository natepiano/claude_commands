#!/usr/bin/env bash
# Shared agent registry and resolver.
#
# ~/.claude/config/agents.conf maps each function to a family, each subtask to
# an agent and optional effort, and each family to its valid agent catalog.
# Consumers resolve task names through agents_resolve or agent_exec.

AGENTS_CONFIG_FILE="${AGENTS_CONFIG_FILE:-$HOME/.claude/config/agents.conf}"
AGENTS_CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_CONFIG_FILE="${CODEX_CONFIG_FILE:-$HOME/.codex/config.toml}"
CODEX_MODELS_CACHE_FILE="${CODEX_MODELS_CACHE_FILE:-$HOME/.codex/models_cache.json}"
CODEX_CATALOG_SYNC_STATE_FILE="${CODEX_CATALOG_SYNC_STATE_FILE:-$HOME/.local/state/codex-agent-catalog-sync/last_success}"

# Keep the materialized registry current between periodic launchd runs.
if [[ -f "$AGENTS_CONFIG_FILE" \
    && -x "$AGENTS_CONFIG_DIR/sync_codex_catalog.sh" \
    && ( ! -f "$CODEX_CATALOG_SYNC_STATE_FILE" \
        || "$CODEX_CONFIG_FILE" -nt "$CODEX_CATALOG_SYNC_STATE_FILE" \
        || "$CODEX_MODELS_CACHE_FILE" -nt "$CODEX_CATALOG_SYNC_STATE_FILE" ) ]]; then
    if ! "$AGENTS_CONFIG_DIR/sync_codex_catalog.sh" >/dev/null; then
        echo "WARNING: Codex catalog sync failed; using $AGENTS_CONFIG_FILE as-is." >&2
    fi
fi

agents_config_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

_agents_config_has_section() {
    local want_section="$1" line stripped section
    [[ -f "$AGENTS_CONFIG_FILE" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(agents_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            [[ "$section" == "$want_section" ]] && return 0
        fi
    done < "$AGENTS_CONFIG_FILE"
    return 1
}

_agents_config_section_values() {
    local want_section="$1" line stripped section
    [[ -f "$AGENTS_CONFIG_FILE" ]] || return 0
    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(agents_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_section" ]] || continue
        printf '%s\n' "$stripped"
    done < "$AGENTS_CONFIG_FILE"
}

_agents_section_keys_inline() {
    local section="$1" line key first=1
    while IFS= read -r line; do
        key="${line%%=*}"
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$key"
            first=0
        else
            printf ', %s' "$key"
        fi
    done < <(_agents_config_section_values "$section")
}

_agents_registry_get() {
    local section="$1" key="$2" line row_key value
    while IFS= read -r line; do
        row_key="${line%%=*}"
        [[ "$row_key" == "$key" ]] || continue
        value="${line#*=}"
        agents_config_trim "$value"
        return 0
    done < <(_agents_config_section_values "$section")
}

_agents_registry_has_key() {
    local section="$1" key="$2" line row_key
    while IFS= read -r line; do
        row_key="${line%%=*}"
        [[ "$row_key" == "$key" ]] && return 0
    done < <(_agents_config_section_values "$section")
    return 1
}

_agents_function_families_inline() {
    local function="$1" family first=1
    for family in codex claude; do
        _agents_config_has_section "$function.$family" || continue
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$family"
            first=0
        else
            printf ', %s' "$family"
        fi
    done
}

# Every family whose catalog lists <agent>, inline for error text. Agent names
# are disjoint across families, so exactly one match names a row's family and
# two means the catalogs collided and the caller must refuse to guess.
_agents_agent_families_inline() {
    local agent="$1" family first=1
    for family in codex claude; do
        _agents_registry_has_key "$family.agents" "$agent" || continue
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$family"
            first=0
        else
            printf ', %s' "$family"
        fi
    done
}

# The family a task resolves through today: an exact-task assignment override
# if one exists, otherwise the function's assignment.
_agents_active_family() {
    local function="$1" subtask="$2" family
    family="$(_agents_registry_get assignments "$function.$subtask")"
    if [[ -z "$family" ]]; then
        family="$(_agents_registry_get assignments "$function")"
    fi
    printf '%s' "$family"
}

_agents_effort_allowed() {
    local allowed="$1" effort="$2" item
    local old_ifs="$IFS"
    IFS=','
    for item in $allowed; do
        item="$(agents_config_trim "$item")"
        if [[ "$item" == "$effort" ]]; then
            IFS="$old_ifs"
            return 0
        fi
    done
    IFS="$old_ifs"
    return 1
}

_agents_validate_pair() {
    local context="$1" family="$2" pair="$3"
    local model effort allowed_efforts allowed_agents

    model="${pair%%:*}"
    effort=""
    if [[ "$pair" == *:* ]]; then
        effort="${pair#*:}"
        if [[ -z "$effort" ]]; then
            echo "ERROR: [$context] effort must not be empty after ':'." >&2
            return 1
        fi
    fi

    if ! _agents_registry_has_key "$family.agents" "$model"; then
        allowed_agents="$(_agents_section_keys_inline "$family.agents")"
        echo "ERROR: [$context] agent '$model' is not allowed for family '$family'." >&2
        echo "       Allowed agents in $AGENTS_CONFIG_FILE [$family.agents]: $allowed_agents" >&2
        return 1
    fi
    allowed_efforts="$(_agents_registry_get "$family.agents" "$model")"
    if [[ -n "$effort" ]] && ! _agents_effort_allowed "$allowed_efforts" "$effort"; then
        echo "ERROR: [$context] effort '$effort' is not allowed for agent '$model'." >&2
        echo "       Allowed efforts in $AGENTS_CONFIG_FILE [$family.agents] $model: $allowed_efforts" >&2
        return 1
    fi

    AGENT_MODEL="$model"
    AGENT_EFFORT="$effort"
}

agents_resolve() {
    local task="$1" function subtask family pair section configured allowed_families

    function="${task%%.*}"
    subtask="${task#*.}"
    if [[ -z "$function" || -z "$subtask" || "$task" == "$function" || "$subtask" == *.* ]]; then
        echo "ERROR: task '$task' must have exactly two segments: <function>.<subtask>." >&2
        return 1
    fi

    family="$(_agents_registry_get assignments "$task")"
    if [[ -z "$family" ]]; then
        family="$(_agents_registry_get assignments "$function")"
    fi
    if [[ -z "$family" ]]; then
        configured="$(_agents_section_keys_inline assignments)"
        echo "ERROR: [$task] no [assignments] entry for '$function'." >&2
        echo "       Configured assignments in $AGENTS_CONFIG_FILE: $configured" >&2
        return 1
    fi

    section="$function.$family"
    if ! _agents_config_has_section "$section"; then
        allowed_families="$(_agents_function_families_inline "$function")"
        echo "ERROR: [$task] missing set section [$section] in $AGENTS_CONFIG_FILE." >&2
        echo "       Allowed families with a configured set: $allowed_families" >&2
        return 1
    fi
    pair="$(_agents_registry_get "$section" "$subtask")"
    if [[ -z "$pair" ]]; then
        configured="$(_agents_section_keys_inline "$section")"
        echo "ERROR: [$task] missing sub-task '$subtask' in [$section]." >&2
        echo "       Allowed sub-tasks: $configured" >&2
        return 1
    fi

    AGENT_FAMILY="$family"
    _agents_validate_pair "$task" "$family" "$pair"
}

agents_resolve_print() {
    local task="$1"
    agents_resolve "$task" || return 1
    printf 'task=%s family=%s agent=%s effort=%s\n' \
        "$task" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"
}

agents_list_assignments() {
    local filter="${1:-}" assignment key family line subtask matched=0
    while IFS= read -r assignment; do
        key="${assignment%%=*}"
        family="${assignment#*=}"
        if [[ -n "$filter" && "$key" != "$filter" && "$key" != "$filter".* ]]; then
            continue
        fi
        matched=1
        if [[ "$key" == *.* ]]; then
            agents_resolve_print "$key" || return 1
            continue
        fi
        while IFS= read -r line; do
            subtask="${line%%=*}"
            if [[ -n "$(_agents_registry_get assignments "$key.$subtask")" ]]; then
                continue
            fi
            agents_resolve_print "$key.$subtask" || return 1
        done < <(_agents_config_section_values "$key.$family")
    done < <(_agents_config_section_values assignments)
    if [[ -n "$filter" && "$matched" -eq 0 ]]; then
        echo "ERROR: no [assignments] entry for '$filter'." >&2
        echo "       Configured assignments in $AGENTS_CONFIG_FILE: $(_agents_section_keys_inline assignments)" >&2
        return 1
    fi
}

# Print every family's rows for one function, then a comment naming the
# active family (plus any per-subtask assignment overrides).
agents_list_function() {
    local function="$1" active family line key subtask pair model effort overrides=""
    local row_active

    active="$(_agents_registry_get assignments "$function")"
    if [[ -z "$active" ]]; then
        echo "ERROR: no [assignments] entry for '$function'." >&2
        echo "       Configured assignments in $AGENTS_CONFIG_FILE: $(_agents_section_keys_inline assignments)" >&2
        return 1
    fi
    for family in codex claude; do
        _agents_config_has_section "$function.$family" || continue
        while IFS= read -r line; do
            subtask="${line%%=*}"
            pair="$(agents_config_trim "${line#*=}")"
            model="${pair%%:*}"
            effort=""
            [[ "$pair" == *:* ]] && effort="${pair#*:}"
            if [[ "$(_agents_active_family "$function" "$subtask")" == "$family" ]]; then
                row_active="yes"
            else
                row_active="no"
            fi
            printf 'task=%s family=%s agent=%s effort=%s active=%s\n' \
                "$function.$subtask" "$family" "$model" "$effort" "$row_active"
        done < <(_agents_config_section_values "$function.$family")
    done
    while IFS= read -r line; do
        key="${line%%=*}"
        [[ "$key" == "$function".* ]] || continue
        overrides="$overrides, ${key#"$function".}=$(agents_config_trim "${line#*=}")"
    done < <(_agents_config_section_values assignments)
    if [[ -n "$overrides" ]]; then
        echo "# current family: $active (overrides:${overrides#,})"
    else
        echo "# current family: $active"
    fi
}

agents_set_assignment() {
    local function="$1" family="$2" section line subtask pair tmp_file allowed_families

    if [[ -z "$function" || "$function" == *.* ]]; then
        echo "ERROR: assignment function must be one segment; got '$function'." >&2
        return 1
    fi
    section="$function.$family"
    if ! _agents_config_has_section "$section"; then
        allowed_families="$(_agents_function_families_inline "$function")"
        echo "ERROR: cannot assign '$function' to '$family': missing [$section]." >&2
        echo "       Allowed families with a configured set: $allowed_families" >&2
        return 1
    fi
    while IFS= read -r line; do
        subtask="${line%%=*}"
        pair="${line#*=}"
        if ! _agents_validate_pair "$function.$subtask" "$family" "$pair"; then
            echo "ERROR: assignment rejected because [$section] row '$subtask' is invalid." >&2
            return 1
        fi
    done < <(_agents_config_section_values "$section")

    tmp_file="$(mktemp "${AGENTS_CONFIG_FILE}.XXXXXX")"
    if ! awk -v fn="$function" -v fam="$family" '
        /^\[assignments\]/ { in_section = 1; print; next }
        /^\[/ { in_section = 0; print; next }
        in_section && index($0, fn "=") == 1 {
            print fn "=" fam
            found = 1
            next
        }
        { print }
        END { if (!found) exit 2 }
    ' "$AGENTS_CONFIG_FILE" > "$tmp_file"; then
        rm -f "$tmp_file"
        echo "ERROR: no [assignments] entry for '$function'; $AGENTS_CONFIG_FILE was not changed." >&2
        return 1
    fi
    mv "$tmp_file" "$AGENTS_CONFIG_FILE"
}

# Edit one row. The agent names its own family, so the row written is the one
# the agent could only ever have meant — never the merely-active one. Sets
# AGENT_ROW_FAMILY (where it landed), AGENT_ROW_ACTIVE_FAMILY (what the task
# resolves through today), and AGENT_ROW_ACTIVE (yes when those agree).
agents_set_row() {
    local task="$1" pair="$2" function subtask family families active
    local section tmp_file configured

    function="${task%%.*}"
    subtask="${task#*.}"
    if [[ -z "$function" || -z "$subtask" || "$task" == "$function" || "$subtask" == *.* ]]; then
        echo "ERROR: task '$task' must have exactly two segments: <function>.<subtask>." >&2
        return 1
    fi

    active="$(_agents_active_family "$function" "$subtask")"
    if [[ -z "$active" ]]; then
        configured="$(_agents_section_keys_inline assignments)"
        echo "ERROR: [$task] no [assignments] entry for '$function'." >&2
        echo "       Configured assignments in $AGENTS_CONFIG_FILE: $configured" >&2
        return 1
    fi

    families="$(_agents_agent_families_inline "${pair%%:*}")"
    if [[ -z "$families" ]]; then
        echo "ERROR: [$task] unknown agent '${pair%%:*}'." >&2
        echo "       Allowed agents in $AGENTS_CONFIG_FILE [codex.agents]: $(_agents_section_keys_inline codex.agents)" >&2
        echo "       Allowed agents in $AGENTS_CONFIG_FILE [claude.agents]: $(_agents_section_keys_inline claude.agents)" >&2
        return 1
    fi
    if [[ "$families" == *,* ]]; then
        echo "ERROR: [$task] agent '${pair%%:*}' is listed by more than one family ($families)." >&2
        echo "       Remove the duplicate in $AGENTS_CONFIG_FILE so an agent names exactly one family." >&2
        return 1
    fi
    family="$families"

    section="$function.$family"
    if ! _agents_config_has_section "$section"; then
        echo "ERROR: [$task] '${pair%%:*}' is a $family agent, but there is no [$section]." >&2
        echo "       Families with a configured set for '$function': $(_agents_function_families_inline "$function")" >&2
        return 1
    fi
    if ! _agents_registry_has_key "$section" "$subtask"; then
        configured="$(_agents_section_keys_inline "$section")"
        echo "ERROR: [$task] missing sub-task '$subtask' in [$section]." >&2
        echo "       Allowed sub-tasks: $configured" >&2
        return 1
    fi
    _agents_validate_pair "$task" "$family" "$pair" || return 1

    tmp_file="$(mktemp "${AGENTS_CONFIG_FILE}.XXXXXX")"
    if ! NEW_PAIR="$pair" awk -v sec="$section" -v row="$subtask" '
        /^\[/ {
            in_section = ($0 == "[" sec "]")
            print
            next
        }
        in_section {
            content = $0
            hash = index(content, "#")
            before_comment = hash ? substr(content, 1, hash - 1) : content
            equals = index(before_comment, "=")
            if (equals && substr(before_comment, 1, equals - 1) == row) {
                match(before_comment, /[[:space:]]*$/)
                spacing = substr(before_comment, RSTART)
                comment = hash ? substr(content, hash) : ""
                print substr(before_comment, 1, equals) ENVIRON["NEW_PAIR"] spacing comment
                found = 1
                next
            }
        }
        { print }
        END { if (!found) exit 2 }
    ' "$AGENTS_CONFIG_FILE" > "$tmp_file"; then
        rm -f "$tmp_file"
        echo "ERROR: no [$section] row '$subtask'; $AGENTS_CONFIG_FILE was not changed." >&2
        return 1
    fi
    mv "$tmp_file" "$AGENTS_CONFIG_FILE"

    AGENT_ROW_FAMILY="$family"
    AGENT_ROW_ACTIVE_FAMILY="$active"
    if [[ "$family" == "$active" ]]; then
        AGENT_ROW_ACTIVE="yes"
    else
        AGENT_ROW_ACTIVE="no"
    fi
}

agents_codex_args() {
    printf '%s %s' '-m' "$AGENT_MODEL"
    if [[ -n "$AGENT_EFFORT" ]]; then
        printf ' %s %s' '-c' "model_reasoning_effort=\"$AGENT_EFFORT\""
    fi
    printf '\n'
}

agents_claude_args() {
    printf '%s %s' '--model' "$AGENT_MODEL"
    if [[ -n "$AGENT_EFFORT" ]]; then
        printf ' %s %s' '--effort' "$AGENT_EFFORT"
    fi
    printf '\n'
}

#!/usr/bin/env bash
# Shared agent registry and resolver.
#
# ~/.claude/config/agents.conf maps each function to a family, each subtask to
# an agent and optional effort, and each family to its valid agent catalog.
# Consumers resolve task names through agents_resolve or agent_exec.

# Bash only, and it has to say so rather than limp along. Sourced into zsh this
# file fails two ways at once: zsh leaves BASH_REMATCH unset, so every section
# lookup below matches nothing and returns empty answers with no error; and zsh
# expands aliases while parsing a sourced file, so an interactive alias named
# after a keyword the read loops use -- `continue` is aliased in this setup --
# turns each loop iteration into a spawned command and the lookup hangs outright.
# Every consumer is a bash script, so refusing costs nothing and a wrong answer
# from a registry lookup is worse than no answer.
if [ -n "${ZSH_VERSION:-}" ]; then
    printf '%s\n' "agents_config.sh: bash only; sourced into zsh it returns empty rows and can hang." >&2
    printf '%s\n' "  use: bash -c 'source ~/.claude/scripts/agents/agents_config.sh; <call>'" >&2
    return 1 2>/dev/null || exit 1
fi

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

# These two return from inside the loop as soon as they find their row, so they
# read the section into a variable first rather than looping over a process
# substitution. Returning early from `while read ... < <(producer)` leaves the
# producer writing into a pipe with no reader, and every remaining printf in
# _agents_config_section_values reports "write error: Broken pipe" on stderr.
# On the Mac that went to /tmp/style-fix-stderr.log and nobody looked; on NixOS
# the systemd journal carries it, which is where the 39 lines per fix run came
# from. The functions below that read their input to the end are fine as-is.
_agents_registry_get() {
    local section="$1" key="$2" values line row_key value
    values="$(_agents_config_section_values "$section")"
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        row_key="${line%%=*}"
        [[ "$row_key" == "$key" ]] || continue
        value="${line#*=}"
        agents_config_trim "$value"
        return 0
    done <<< "$values"
}

_agents_registry_has_key() {
    local section="$1" key="$2" values line row_key
    values="$(_agents_config_section_values "$section")"
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        row_key="${line%%=*}"
        [[ "$row_key" == "$key" ]] && return 0
    done <<< "$values"
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

# Every family with an agent catalog, inline for error text.
_agents_families_inline() {
    local family first=1
    for family in codex claude; do
        _agents_config_has_section "$family.agents" || continue
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$family"
            first=0
        else
            printf ', %s' "$family"
        fi
    done
}

# `caller` in [assignments] means the function runs on the family of the agent
# invoking it, not on a fixed one. ask_a_friend uses it: a friend of the caller's
# own kind is the only friend that can talk back -- claude reaches claude by
# SendMessage, codex reaches codex through codex_mesh.py, and a codex friend has
# no route to a claude caller. A function assigned `caller` has no family switch.
AGENTS_CALLER_ASSIGNMENT="caller"

# The family of the agent running this shell. codex exports CODEX_THREAD_ID into
# every shell it spawns and claude exports CLAUDE_CODE_SESSION_ID; codex wins when
# both are present because a codex process launched from a claude session
# inherits claude's variable and is still codex. AGENTS_CALLER_FAMILY overrides
# detection, for tests and for a launcher that already knows. Empty when nothing
# is detectable.
_agents_caller_family() {
    if [[ -n "${AGENTS_CALLER_FAMILY:-}" ]]; then
        printf '%s' "$AGENTS_CALLER_FAMILY"
    elif [[ -n "${CODEX_THREAD_ID:-}" ]]; then
        printf '%s' codex
    elif [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
        printf '%s' claude
    fi
}

# An assignment value as the family it means right now: `caller` becomes the
# running agent's family (or empty), anything else is already a family.
_agents_concrete_family() {
    local raw="$1"
    if [[ "$raw" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
        _agents_caller_family
    else
        printf '%s' "$raw"
    fi
}

_agents_caller_error() {
    local context="$1"
    echo "ERROR: [$context] is assigned '$AGENTS_CALLER_ASSIGNMENT': it runs on the calling agent's family, and no calling agent is detectable here." >&2
    echo "       Run it from a codex or claude session, or set AGENTS_CALLER_FAMILY=<codex|claude>." >&2
}

# The assignment a task resolves through today: an exact-task override if one
# exists, otherwise the function's. Raw -- may be `caller`.
_agents_active_family() {
    local function="$1" subtask="$2" family
    family="$(_agents_registry_get assignments "$function.$subtask")"
    if [[ -z "$family" ]]; then
        family="$(_agents_registry_get assignments "$function")"
    fi
    printf '%s' "$family"
}

# The concrete family a task runs on right now; empty for an undetectable caller.
_agents_live_family() {
    _agents_concrete_family "$(_agents_active_family "$1" "$2")"
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
    if [[ "$family" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
        family="$(_agents_caller_family)"
        if [[ -z "$family" ]]; then
            _agents_caller_error "$task"
            return 1
        fi
    fi
    _agents_resolve_in_family "$task" "$family"
}

# The part of resolution that follows family selection: section, row, pair.
_agents_resolve_in_family() {
    local task="$1" family="$2" function subtask section pair configured allowed_families

    function="${task%%.*}"
    subtask="${task#*.}"
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

_agents_resolve_print_in_family() {
    local task="$1" family="$2"
    AGENT_FAMILY="$family"
    _agents_resolve_in_family "$task" "$family" || return 1
    printf 'task=%s family=%s agent=%s effort=%s\n' \
        "$task" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"
}

# Print one function's rows in one family, skipping sub-tasks an exact-task
# assignment override shadows (those print once, from their own key).
_agents_list_function_rows() {
    local function="$1" family="$2" line subtask
    while IFS= read -r line; do
        subtask="${line%%=*}"
        if [[ -n "$(_agents_registry_get assignments "$function.$subtask")" ]]; then
            continue
        fi
        _agents_resolve_print_in_family "$function.$subtask" "$family" || return 1
    done < <(_agents_config_section_values "$function.$family")
}

agents_list_assignments() {
    local filter="${1:-}" assignment key family live matched=0
    while IFS= read -r assignment; do
        key="${assignment%%=*}"
        family="$(agents_config_trim "${assignment#*=}")"
        if [[ -n "$filter" && "$key" != "$filter" && "$key" != "$filter".* ]]; then
            continue
        fi
        matched=1
        if [[ "$key" == *.* ]]; then
            agents_resolve_print "$key" || return 1
            continue
        fi
        if [[ "$family" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
            # A caller function has no single answer from outside a session:
            # show the family that would run from here, or both when nothing
            # says which.
            live="$(_agents_caller_family)"
            if [[ -n "$live" ]]; then
                _agents_list_function_rows "$key" "$live" || return 1
                echo "# $key: $AGENTS_CALLER_ASSIGNMENT — the calling agent's family ($live here)"
            else
                for live in codex claude; do
                    _agents_config_has_section "$key.$live" || continue
                    _agents_list_function_rows "$key" "$live" || return 1
                done
                echo "# $key: $AGENTS_CALLER_ASSIGNMENT — the calling agent's family (none detectable here)"
            fi
            continue
        fi
        _agents_list_function_rows "$key" "$family" || return 1
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
    local row_active live

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
            if [[ "$(_agents_live_family "$function" "$subtask")" == "$family" ]]; then
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
    if [[ "$active" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
        live="$(_agents_caller_family)"
        if [[ -n "$live" ]]; then
            active="$AGENTS_CALLER_ASSIGNMENT — the calling agent's family ($live here)"
        else
            active="$AGENTS_CALLER_ASSIGNMENT — the calling agent's family (none detectable here)"
        fi
    fi
    if [[ -n "$overrides" ]]; then
        echo "# current family: $active (overrides:${overrides#,})"
    else
        echo "# current family: $active"
    fi
}

agents_set_assignment() {
    local function="$1" family="$2" section line subtask pair tmp_file allowed_families current

    if [[ -z "$function" || "$function" == *.* ]]; then
        echo "ERROR: assignment function must be one segment; got '$function'." >&2
        return 1
    fi
    current="$(_agents_registry_get assignments "$function")"
    if [[ "$current" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
        echo "ERROR: '$function' is assigned '$AGENTS_CALLER_ASSIGNMENT': it always runs on the calling agent's family and has no switch." >&2
        echo "       Edit its rows instead: agent_admin.sh $function.<subtask> <agent>[:<effort>]" >&2
        return 1
    fi
    if [[ "$family" == "$AGENTS_CALLER_ASSIGNMENT" ]]; then
        echo "ERROR: '$AGENTS_CALLER_ASSIGNMENT' is not a switch target; it is written by hand in $AGENTS_CONFIG_FILE [assignments]." >&2
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

# Switch every [assignments] entry — functions and any exact-task overrides —
# to one family. Validated wholesale first, so a switch that would leave any
# task unresolvable is rejected before a single line is written.
agents_set_all_assignments() {
    local family="$1" line key fn subtask section row seen="" tmp_file

    if [[ -z "$family" ]] || ! _agents_config_has_section "$family.agents"; then
        echo "ERROR: unknown family '$family'." >&2
        echo "       Configured families in $AGENTS_CONFIG_FILE: $(_agents_families_inline)" >&2
        return 1
    fi
    while IFS= read -r line; do
        key="${line%%=*}"
        # A caller function is not switched, so it is not validated against the
        # target either; it already resolves through whichever family asks.
        [[ "$(agents_config_trim "${line#*=}")" == "$AGENTS_CALLER_ASSIGNMENT" ]] && continue
        fn="${key%%.*}"
        section="$fn.$family"
        if ! _agents_config_has_section "$section"; then
            echo "ERROR: cannot assign '$fn' to '$family': missing [$section]." >&2
            echo "       Allowed families with a configured set: $(_agents_function_families_inline "$fn")" >&2
            return 1
        fi
        if [[ "$key" == *.* ]]; then
            subtask="${key#*.}"
            if ! _agents_registry_has_key "$section" "$subtask"; then
                echo "ERROR: cannot assign '$key' to '$family': [$section] has no row '$subtask'." >&2
                echo "       Allowed sub-tasks: $(_agents_section_keys_inline "$section")" >&2
                return 1
            fi
        fi
        case " $seen " in
            *" $section "*) continue ;;
        esac
        seen="$seen $section"
        while IFS= read -r row; do
            if ! _agents_validate_pair "$fn.${row%%=*}" "$family" "${row#*=}"; then
                echo "ERROR: switch rejected because [$section] row '${row%%=*}' is invalid." >&2
                return 1
            fi
        done < <(_agents_config_section_values "$section")
    done < <(_agents_config_section_values assignments)

    tmp_file="$(mktemp "${AGENTS_CONFIG_FILE}.XXXXXX")"
    if ! awk -v fam="$family" -v caller="$AGENTS_CALLER_ASSIGNMENT" '
        /^\[/ {
            in_section = ($0 == "[assignments]")
            print
            next
        }
        in_section {
            content = $0
            hash = index(content, "#")
            before_comment = hash ? substr(content, 1, hash - 1) : content
            equals = index(before_comment, "=")
            if (equals) {
                value = substr(before_comment, equals + 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                found = 1
                if (value == caller) { print; next }
                match(before_comment, /[[:space:]]*$/)
                spacing = substr(before_comment, RSTART)
                comment = hash ? substr(content, hash) : ""
                print substr(before_comment, 1, equals) fam spacing comment
                next
            }
        }
        { print }
        END { if (!found) exit 2 }
    ' "$AGENTS_CONFIG_FILE" > "$tmp_file"; then
        rm -f "$tmp_file"
        echo "ERROR: [assignments] has no entries; $AGENTS_CONFIG_FILE was not changed." >&2
        return 1
    fi
    mv "$tmp_file" "$AGENTS_CONFIG_FILE"
}

# Edit one row. The agent names its own family, so the row written is the one
# the agent could only ever have meant — never the merely-active one. Sets
# AGENT_ROW_FAMILY (where it landed), AGENT_ROW_ASSIGNMENT (the raw assignment,
# `caller` included), AGENT_ROW_ACTIVE_FAMILY (what the task resolves through
# today; empty for an undetectable caller), and AGENT_ROW_ACTIVE (yes when the
# row's family is the one resolving today).
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
    AGENT_ROW_ASSIGNMENT="$active"
    AGENT_ROW_ACTIVE_FAMILY="$(_agents_concrete_family "$active")"
    if [[ "$family" == "$AGENT_ROW_ACTIVE_FAMILY" ]]; then
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

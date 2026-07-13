#!/usr/bin/env bash
# Materialize Codex's selected model and visible catalog in agents.conf.

set -euo pipefail

AGENTS_CONFIG_FILE="${AGENTS_CONFIG_FILE:-$HOME/.claude/config/agents.conf}"
CODEX_CONFIG_FILE="${CODEX_CONFIG_FILE:-$HOME/.codex/config.toml}"
CODEX_MODELS_CACHE_FILE="${CODEX_MODELS_CACHE_FILE:-$HOME/.codex/models_cache.json}"
CODEX_CATALOG_SYNC_STATE_FILE="${CODEX_CATALOG_SYNC_STATE_FILE:-$HOME/.local/state/codex-agent-catalog-sync/last_success}"
CHECK_ONLY=false

if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=true
elif [[ $# -ne 0 ]]; then
    echo "usage: $0 [--check]" >&2
    exit 2
fi

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

registry_section_rows() {
    local section="$1"
    awk -v wanted="$section" '
        /^[[:space:]]*\[/ {
            current = $0
            sub(/^[[:space:]]*\[/, "", current)
            sub(/\][[:space:]]*$/, "", current)
            next
        }
        current == wanted {
            row = $0
            sub(/[[:space:]]*#.*/, "", row)
            sub(/^[[:space:]]*/, "", row)
            sub(/[[:space:]]*$/, "", row)
            if (row != "") print row
        }
    ' "$AGENTS_CONFIG_FILE"
}

refreshed_catalog_has_agent() {
    local wanted="$1" row
    for row in "${refreshed_catalog_rows[@]}"; do
        [[ "${row%%=*}" == "$wanted" ]] && return 0
    done
    return 1
}

cache_has_agent() {
    local wanted="$1" cached
    for cached in "${cache_agents[@]}"; do
        [[ "$cached" == "$wanted" ]] && return 0
    done
    return 1
}

preserve_catalog_row() {
    local old_row="$1" wanted index
    wanted="${old_row%%=*}"
    index=0
    while [[ "$index" -lt "${#catalog_rows[@]}" ]]; do
        if [[ "${catalog_rows[$index]%%=*}" == "$wanted" ]]; then
            catalog_rows[$index]="$old_row"
            return 0
        fi
        index=$((index + 1))
    done
    catalog_rows+=("$old_row")
}

previous_catalog_row() {
    local wanted="$1" row
    for row in "${previous_catalog_rows[@]}"; do
        if [[ "${row%%=*}" == "$wanted" ]]; then
            printf '%s\n' "$row"
            return 0
        fi
    done
    return 1
}

warn_missing_claude_aliases() {
    local claude_path help_text aliases alias
    claude_path="$(command -v claude 2>/dev/null || true)"
    [[ -n "$claude_path" ]] || return 0
    help_text="$("$claude_path" --help 2>/dev/null || true)"
    aliases="$(
        printf '%s\n' "$help_text" | awk '
            /^[[:space:]]*--model([[:space:]]|$)/ {
                in_model = 1
            }
            in_model {
                if (seen && $0 ~ /^[[:space:]]*-[^[:space:]]/) exit
                if ($0 ~ /alias/) in_alias_example = 1
                if (in_alias_example) {
                    example = example " " $0
                    if ($0 ~ /\)/) {
                        while (match(example, /\047[[:alnum:]_.\/-]+\047/)) {
                            print substr(example, RSTART + 1, RLENGTH - 2)
                            example = substr(example, RSTART + RLENGTH)
                        }
                        exit
                    }
                }
                seen = 1
            }
        '
    )"
    [[ -n "$aliases" ]] || return 0

    while IFS= read -r alias; do
        [[ -n "$alias" ]] || continue
        if ! registry_section_rows claude.agents \
            | awk -F= -v wanted="$alias" '$1 == wanted { found = 1 } END { exit !found }'; then
            echo "WARNING: Claude model alias '$alias' is missing from [claude.agents]; add it after choosing its allowed efforts." >&2
        fi
    done <<< "$aliases"
}

command -v jq >/dev/null || fail "jq is required"
[[ -f "$AGENTS_CONFIG_FILE" ]] || fail "agent registry not found: $AGENTS_CONFIG_FILE"
[[ -f "$CODEX_CONFIG_FILE" ]] || fail "Codex config not found: $CODEX_CONFIG_FILE"
[[ -f "$CODEX_MODELS_CACHE_FILE" ]] || fail "Codex model cache not found: $CODEX_MODELS_CACHE_FILE"

codex_model="$({
    awk '
        /^[[:space:]]*\[/ { exit }
        /^[[:space:]]*model[[:space:]]*=[[:space:]]*"/ {
            line = $0
            sub(/^[^=]*=[[:space:]]*"/, "", line)
            sub(/".*$/, "", line)
            print line
            exit
        }
    ' "$CODEX_CONFIG_FILE"
})"
[[ -n "$codex_model" ]] || fail "top-level Codex model is missing from $CODEX_CONFIG_FILE"

models_text="$(
    jq -er '
        def efforts:
            if .supported_reasoning_levels == null then
                []
            elif (.supported_reasoning_levels | type) != "array" then
                error("supported_reasoning_levels must be an array")
            else
                [.supported_reasoning_levels[]
                    | .effort
                    | if type == "string" then . else error("effort must be a string") end]
            end;
        if (.models | type) != "array" then
            error("models must be an array")
        else
            [
                .models[]
                | select(.visibility == "list")
                | select((.slug | type) == "string" and (.slug | length) > 0)
                | {slug: .slug, efforts: efforts}
            ] as $models
            | if ($models | length) == 0 then
                error("visible model catalog is empty")
            elif ([$models[].slug] | unique | length) != ($models | length) then
                error("visible model catalog contains duplicate slugs")
            else
                $models[] | [.slug, (.efforts | join(","))] | @tsv
            end
        end
    ' "$CODEX_MODELS_CACHE_FILE"
)" || fail "invalid Codex model cache: $CODEX_MODELS_CACHE_FILE"

cache_agents=()
cache_agents_text="$(
    jq -r '.models[] | .slug | select(type == "string" and length > 0)' \
        "$CODEX_MODELS_CACHE_FILE"
)"
while IFS= read -r model; do
    [[ -n "$model" ]] && cache_agents+=("$model")
done <<< "$cache_agents_text"

catalog_rows=()
selected_is_visible=false
while IFS=$'\t' read -r model efforts; do
    [[ -n "$model" ]] || continue
    [[ "$model" == "$codex_model" ]] && selected_is_visible=true
    if [[ ! "$model" =~ ^[[:alnum:]][[:alnum:]./_-]*$ ]]; then
        echo "WARNING: skipping Codex model '$model': slug contains unsupported characters." >&2
        continue
    fi
    catalog_rows+=("$model=$efforts")
done <<< "$models_text"

if [[ "$selected_is_visible" == false ]]; then
    if [[ "$codex_model" =~ ^[[:alnum:]][[:alnum:]./_-]*$ ]]; then
        selected_efforts="$(
            jq -er --arg selected "$codex_model" '
                def efforts:
                    if .supported_reasoning_levels == null then
                        []
                    elif (.supported_reasoning_levels | type) != "array" then
                        error("supported_reasoning_levels must be an array")
                    else
                        [.supported_reasoning_levels[]
                            | .effort
                            | if type == "string" then . else error("effort must be a string") end]
                    end;
                first(.models[] | select(.slug == $selected) | efforts) // []
                | join(",")
            ' "$CODEX_MODELS_CACHE_FILE"
        )" || fail "invalid Codex model cache: $CODEX_MODELS_CACHE_FILE"
        catalog_rows=("$codex_model=$selected_efforts" "${catalog_rows[@]}")
    else
        echo "WARNING: skipping selected Codex model '$codex_model': slug contains unsupported characters." >&2
    fi
fi

[[ "${#catalog_rows[@]}" -gt 0 ]] || fail "visible Codex model catalog has no usable slugs"
refreshed_catalog_rows=("${catalog_rows[@]}")

previous_catalog_rows=()
while IFS= read -r row; do
    previous_catalog_rows+=("$row")
done < <(registry_section_rows codex.agents)

assigned_rows="$(
    awk '
        function trim(value) {
            sub(/^[[:space:]]*/, "", value)
            sub(/[[:space:]]*$/, "", value)
            return value
        }
        /^[[:space:]]*\[/ {
            section = $0
            sub(/^[[:space:]]*\[/, "", section)
            sub(/\][[:space:]]*$/, "", section)
            next
        }
        {
            row = $0
            sub(/[[:space:]]*#.*/, "", row)
            row = trim(row)
            if (row == "" || index(row, "=") == 0) next
            key = row
            sub(/=.*/, "", key)
            value = row
            sub(/^[^=]*=/, "", value)
            key = trim(key)
            value = trim(value)
            if (section == "assignments") {
                assignments[key] = value
                next
            }
            if (section ~ /\.codex$/) {
                fn = section
                sub(/\.codex$/, "", fn)
                count++
                tasks[count] = fn "." key
                functions[count] = fn
                pairs[count] = value
            }
        }
        END {
            for (i = 1; i <= count; i++) {
                task = tasks[i]
                fn = functions[i]
                family = assignments[task]
                if (family == "") family = assignments[fn]
                if (family == "codex") print task "\t" pairs[i]
            }
        }
    ' "$AGENTS_CONFIG_FILE"
)"

while IFS=$'\t' read -r task pair; do
    [[ -n "$task" ]] || continue
    assigned_agent="${pair%%:*}"
    if refreshed_catalog_has_agent "$assigned_agent" \
        && cache_has_agent "$assigned_agent"; then
        continue
    fi

    echo "WARNING: $task is assigned to '$assigned_agent', which is gone from the codex catalog — re-point it: /agent $task <agent>[:<effort>], or switch the family: /agent ${task%%.*} <family>" >&2
    if ! old_row="$(previous_catalog_row "$assigned_agent")"; then
        fail "cannot preserve assigned Codex agent '$assigned_agent': no previous [codex.agents] row exists"
    fi
    preserve_catalog_row "$old_row"
done <<< "$assigned_rows"

warn_missing_claude_aliases

tmp_file="$(mktemp "${AGENTS_CONFIG_FILE}.XXXXXX")"
cleanup() {
    rm -f "$tmp_file"
}
trap cleanup EXIT

in_codex_agents=false
saw_codex_agents=false

while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^\[([^]]+)\]$ ]]; then
        if [[ "$in_codex_agents" == true ]]; then
            printf '\n' >> "$tmp_file"
        fi

        section="${BASH_REMATCH[1]}"
        in_codex_agents=false
        if [[ "$section" == "codex.agents" ]]; then
            in_codex_agents=true
            saw_codex_agents=true
            printf '%s\n' "$line" >> "$tmp_file"
            printf '# Generated by scripts/agents/sync_codex_catalog.sh — do not hand-edit.\n' >> "$tmp_file"
            printf '%s\n' "${catalog_rows[@]}" >> "$tmp_file"
            continue
        fi
    fi

    if [[ "$in_codex_agents" == true ]]; then
        continue
    fi
    printf '%s\n' "$line" >> "$tmp_file"
done < "$AGENTS_CONFIG_FILE"

[[ "$saw_codex_agents" == true ]] || fail "[codex.agents] section not found in $AGENTS_CONFIG_FILE"

if cmp -s "$tmp_file" "$AGENTS_CONFIG_FILE"; then
    if [[ "$CHECK_ONLY" == false ]]; then
        mkdir -p "$(dirname "$CODEX_CATALOG_SYNC_STATE_FILE")"
        touch "$CODEX_CATALOG_SYNC_STATE_FILE"
    fi
    echo "Codex agent catalog is current."
    exit 0
fi

if [[ "$CHECK_ONLY" == true ]]; then
    echo "Codex agent catalog is stale: $AGENTS_CONFIG_FILE" >&2
    exit 1
fi

chmod "$(stat -f '%Lp' "$AGENTS_CONFIG_FILE")" "$tmp_file"
mv "$tmp_file" "$AGENTS_CONFIG_FILE"
trap - EXIT
mkdir -p "$(dirname "$CODEX_CATALOG_SYNC_STATE_FILE")"
touch "$CODEX_CATALOG_SYNC_STATE_FILE"
echo "Updated Codex agent catalog: $AGENTS_CONFIG_FILE"

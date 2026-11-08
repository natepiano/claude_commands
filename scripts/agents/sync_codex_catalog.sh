#!/usr/bin/env bash
# Add Codex models and efforts to the shared registry without removing peer rows.

set -euo pipefail

# Use the registry reader without the launch sync gate.
MODELS_CONFIG_TIER_ONLY=1 source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/models_config.sh"
CODEX_CONFIG_FILE="${CODEX_CONFIG_FILE:-$HOME/.codex/config.toml}"
CODEX_MODELS_CACHE_FILE="${CODEX_MODELS_CACHE_FILE:-$HOME/.codex/models_cache.json}"
CODEX_CATALOG_SYNC_STATE_FILE="${CODEX_CATALOG_SYNC_STATE_FILE:-$HOME/.local/state/codex-agent-catalog-sync/last_success}"
CHECK_ONLY=false
# Set when the cache did not name some visible model's efforts. Such a run still
# writes every row it does know, but it must not close the freshness gate.
CATALOG_INCOMPLETE=false

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

# Fresh launch and editor reads can request sync at the same time.
# Use one writer to preserve concurrent registry edits.
LOCK_DIR="$(dirname "$CODEX_CATALOG_SYNC_STATE_FILE")/sync.lock"

release_lock() {
    [[ -n "${LOCK_HELD:-}" ]] && rmdir "$LOCK_DIR" 2>/dev/null
    return 0
}

# models_registry.py requests sync when a catalog source is newer than this state file.
# Keep the gate open when some model efforts remain unknown.
checkpoint_sync_state() {
    if [[ "$CATALOG_INCOMPLETE" == true ]]; then
        echo "Codex agent catalog is incomplete; the next sync will re-read the cache." >&2
        return 0
    fi
    mkdir -p "$(dirname "$CODEX_CATALOG_SYNC_STATE_FILE")"
    touch "$CODEX_CATALOG_SYNC_STATE_FILE"
}

if [[ "$CHECK_ONLY" == false ]]; then
    state_dir="$(dirname "$CODEX_CATALOG_SYNC_STATE_FILE")"
    if ! mkdir_error="$(mkdir -p "$state_dir" 2>&1)"; then
        fail "cannot create the sync state directory $state_dir: $mkdir_error"
    fi
    # A lock left behind by a killed sync would block every later one, so a
    # directory older than the longest plausible run is debris, not an owner.
    if [[ -d "$LOCK_DIR" && -z "$(find "$LOCK_DIR" -maxdepth 0 -mmin -5 2>/dev/null)" ]]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    # `mkdir` fails both when a peer owns the lock and when the state directory
    # is unwritable -- a sandboxed caller can create nothing under it. Only the
    # first is contention, and only the directory now existing proves it. Reading
    # every failure as a busy peer exits 0, which is the one answer that must not
    # be wrong here: the caller then accepts whatever stale catalog is on disk
    # and its `if ! sync` warning never fires.
    if lock_error="$(mkdir "$LOCK_DIR" 2>&1)"; then
        LOCK_HELD=1
        trap release_lock EXIT
    elif [[ -d "$LOCK_DIR" ]]; then
        # Another sync is mid-flight and will refresh both the catalog and the
        # state file. This caller reads a valid conf either way.
        echo "Codex agent catalog sync already running; skipping."
        exit 0
    else
        fail "cannot acquire the sync lock $LOCK_DIR: $lock_error"
    fi
fi

registry_section_rows() {
    _models_table_section_values "$1"
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
        awk '
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
        ' <<< "$help_text"
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
_models_registry_parse || exit 1
[[ -f "$CODEX_CONFIG_FILE" ]] || fail "Codex config not found: $CODEX_CONFIG_FILE"
# Every query below uses the same complete snapshot, even if Codex rewrites the
# live file between queries. Only the shared reader opens/parses that file.
cache_reader="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
cache_text="$("$cache_reader/py" "$cache_reader/codex_models_cache.py" "$CODEX_MODELS_CACHE_FILE")" || exit 1

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
    ' <<< "$cache_text"
)" || fail "invalid Codex model cache: $CODEX_MODELS_CACHE_FILE"

cache_agents=()
cache_agents_text="$(
    jq -r '.models[] | .slug | select(type == "string" and length > 0)' \
        <<< "$cache_text"
)"
while IFS= read -r model; do
    [[ -n "$model" ]] && cache_agents+=("$model")
done <<< "$cache_agents_text"

previous_catalog_rows=()
while IFS= read -r row; do
    previous_catalog_rows+=("$row")
done < <(registry_section_rows codex.agents)

catalog_rows=()
selected_is_visible=false
while IFS=$'\t' read -r model efforts; do
    [[ -n "$model" ]] || continue
    [[ "$model" == "$codex_model" ]] && selected_is_visible=true
    if [[ ! "$model" =~ ^[[:alnum:]][[:alnum:]./_-]*$ ]]; then
        echo "WARNING: skipping Codex model '$model': slug contains unsupported characters." >&2
        continue
    fi
    # Codex fills the cache in stages: a model reaches the visible catalog before
    # its reasoning levels do, and every model that has settled carries at least
    # one. So a visible model with none is a cache mid-write, not a model without
    # efforts, and `<model>=` writes that falsehood into a generated file --
    # _agents_validate_pair then rejects every `<model>:<effort>` against an empty
    # allowed list, which reads as "that effort is wrong" rather than "nobody has
    # said yet". Keep the last row that named efforts if there is one; otherwise
    # leave the model out rather than assert something untrue about it.
    if [[ -z "$efforts" ]]; then
        CATALOG_INCOMPLETE=true
        if prior_row="$(previous_catalog_row "$model")" && [[ "$prior_row" != *= ]]; then
            echo "WARNING: Codex model '$model' has no reasoning levels in $CODEX_MODELS_CACHE_FILE yet; keeping its last known efforts." >&2
            catalog_rows+=("$prior_row")
        else
            echo "WARNING: Codex model '$model' has no reasoning levels in $CODEX_MODELS_CACHE_FILE yet; leaving it out of [codex.agents] until the cache names them." >&2
        fi
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
            ' <<< "$cache_text"
        )" || fail "invalid Codex model cache: $CODEX_MODELS_CACHE_FILE"
        catalog_rows=("$codex_model=$selected_efforts" "${catalog_rows[@]}")
    else
        echo "WARNING: skipping selected Codex model '$codex_model': slug contains unsupported characters." >&2
    fi
fi

[[ "${#catalog_rows[@]}" -gt 0 ]] || fail "visible Codex model catalog has no usable slugs"
refreshed_catalog_rows=("${catalog_rows[@]}")

# A visible release becomes a family target once its effort row is known.
# Equal numeric versions use slug order, independent of cache order.
families_text="$(
    jq -r --arg catalog "$(printf '%s\n' "${catalog_rows[@]}")" '
        ($catalog | split("\n") | map(split("=")[0])) as $known_slugs
        |
        [.models[]
            | select(.visibility == "list")
            | .slug | select(type == "string") | . as $slug
            | select($known_slugs | index($slug))
            | capture("^gpt-(?<version>[0-9]+(?:\\.[0-9]+)*)-(?<family>[a-z][a-z0-9_-]*)$")
            | . + {slug: $slug, version_numbers: (.version | split(".") | map(tonumber))}]
        | group_by(.family)[]
        | max_by([.version_numbers, .slug])
        | "\(.family)=\(.slug)"
    ' <<< "$cache_text"
)" || fail "invalid Codex model cache: $CODEX_MODELS_CACHE_FILE"

assigned_rows="$(
    for index in "${!_models_table_sections[@]}"; do
        section="${_models_table_sections[index]}"
        [[ "$section" == *.codex && -n "${_models_table_keys[index]}" ]] || continue
        function="${section%.codex}"
        task="$function.${_models_table_keys[index]}"
        family="$(_agents_active_family_from_table "$function" "${_models_table_keys[index]}")"
        [[ "$family" != codex ]] || printf '%s\t%s\n' "$task" "${_models_table_values[index]}"
    done
)"

while IFS=$'\t' read -r task pair; do
    [[ -n "$task" ]] || continue
    assigned_agent="${pair%%:*}"
    # A model-family word follows the cache rather than pinning an old slug.
    # Keep a missing family's failure for the resolver's actionable diagnostic.
    if printf '%s\n' "$families_text" | awk -F= -v wanted="$assigned_agent" \
        '$1 == wanted { found = 1 } END { exit !found }'; then
        continue
    fi
    if registry_section_rows codex.families | awk -F= -v wanted="$assigned_agent" \
        '$1 == wanted { found = 1 } END { exit !found }'; then
        echo "WARNING: $task has no visible Codex model for family '$assigned_agent'." >&2
        continue
    fi
    if refreshed_catalog_has_agent "$assigned_agent" \
        && cache_has_agent "$assigned_agent"; then
        continue
    fi

    echo "WARNING: $task is assigned to '$assigned_agent', which is gone from the codex catalog — re-point it: /default_models $task <agent>[:<effort>], or switch the family: /default_models ${task%%.*} <family>" >&2
    if ! old_row="$(previous_catalog_row "$assigned_agent")"; then
        fail "cannot preserve assigned Codex agent '$assigned_agent': no previous [codex.agents] row exists"
    fi
    preserve_catalog_row "$old_row"
done <<< "$assigned_rows"

warn_missing_claude_aliases

tmp_file="$(mktemp "${MODELS_CONFIG_FILE}.XXXXXX")"
cleanup() {
    rm -f "$tmp_file"
    release_lock
}
trap cleanup EXIT

in_generated_section=false
saw_codex_agents=false

while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^\[([^]]+)\]$ ]]; then
        section="${BASH_REMATCH[1]}"
        if [[ "$in_generated_section" == true && "$section" != "codex.families" ]]; then
            printf '\n' >> "$tmp_file"
        fi

        in_generated_section=false
        if [[ "$section" == "codex.agents" ]]; then
            in_generated_section=true
            saw_codex_agents=true
            printf '%s\n' "$line" >> "$tmp_file"
            printf '# Generated by scripts/agents/sync_codex_catalog.sh — do not hand-edit.\n' >> "$tmp_file"
            printf '%s\n' "${catalog_rows[@]}" >> "$tmp_file"
            printf '\n[codex.families]\n' >> "$tmp_file"
            printf '# Generated by scripts/agents/sync_codex_catalog.sh — do not hand-edit.\n' >> "$tmp_file"
            [[ -z "$families_text" ]] || printf '%s\n' "$families_text" >> "$tmp_file"
            continue
        elif [[ "$section" == "codex.families" ]]; then
            # Emitted beside codex.agents, even when absent from the old file.
            in_generated_section=true
            continue
        fi
    fi

    if [[ "$in_generated_section" == true ]]; then
        continue
    fi
    printf '%s\n' "$line" >> "$tmp_file"
done < <(printf '%s' "$MODELS_REGISTRY_BASE")

[[ "$saw_codex_agents" == true ]] || fail "[codex.agents] section not found in $MODELS_CONFIG_FILE"

# Keep models/efforts seen on another machine and never downgrade a family.
merged_file="$(mktemp "${MODELS_CONFIG_FILE}.XXXXXX")"
if ! "$MODELS_CONFIG_DIR/../lib/py" "$MODELS_CONFIG_DIR/../lib/models_registry.py" catalog \
    --before <(printf '%s' "$MODELS_REGISTRY_BASE") --after "$tmp_file" > "$merged_file"; then
    rm -f "$merged_file"
    fail "cannot merge Codex catalog additions"
fi
mv "$merged_file" "$tmp_file"

# Compare catalog data rather than generated whitespace.
catalog_current="$(
    old_agents="$(registry_section_rows codex.agents | LC_ALL=C sort)"
    old_families="$(registry_section_rows codex.families | LC_ALL=C sort)"
    _models_registry_parse --registry "$tmp_file" || exit 1
    if [[ "$old_agents" == "$(registry_section_rows codex.agents | LC_ALL=C sort)" \
        && "$old_families" == "$(registry_section_rows codex.families | LC_ALL=C sort)" ]]; then
        echo true
    else
        echo false
    fi
)" || fail "cannot compare Codex catalog additions"

if [[ "$catalog_current" == true ]]; then
    if [[ "$CHECK_ONLY" == false ]]; then
        # A prior sync may have stopped after writing but before publication.
        models_registry_store "$tmp_file" "add Codex catalog models and efforts" additive-catalog
        checkpoint_sync_state
    fi
    echo "Codex agent catalog is current."
    exit 0
fi

if [[ "$CHECK_ONLY" == true ]]; then
    echo "Codex agent catalog is stale: $MODELS_CONFIG_FILE" >&2
    exit 1
fi

models_registry_store "$tmp_file" "add Codex catalog models and efforts" additive-catalog
trap release_lock EXIT
checkpoint_sync_state
echo "Updated Codex agent catalog: $MODELS_CONFIG_FILE"

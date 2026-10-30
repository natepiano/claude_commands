#!/usr/bin/env bash
# Add Codex models and efforts to the shared registry without removing peer rows.

set -euo pipefail

AGENTS_CONFIG_FILE="${AGENTS_CONFIG_FILE:-$HOME/.claude/config/agents.conf}"
# Source shared registry I/O without recursively triggering this sync.
AGENTS_CONFIG_TIER_ONLY=1 source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agents_config.sh"
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

# One writer at a time. The sync is triggered implicitly by anything that sources
# agents_config.sh, so a stale freshness gate fires it once per caller -- and a
# worker phase sources it three times at once. `mv` is atomic, so no reader
# ever sees a torn file, but concurrent syncs still do identical work three times
# over, and the last to finish writes a copy it read before the others started:
# a row changed by `/agent` inside that window is silently reverted.
LOCK_DIR="$(dirname "$CODEX_CATALOG_SYNC_STATE_FILE")/sync.lock"

release_lock() {
    [[ -n "${LOCK_HELD:-}" ]] && rmdir "$LOCK_DIR" 2>/dev/null
    return 0
}

# The state file is the freshness gate agents_config.sh reads: it re-runs the
# sync while the Codex config or the model cache is newer than it. Closing that
# gate on a run that could not read some model's efforts would record a partial
# answer as the settled one, so an incomplete run leaves the gate open and the
# next caller re-reads the cache -- which is how a model whose efforts land
# seconds after the sync ran gets picked up without anyone noticing the gap.
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
    local section="$1" registry="${2:-$REGISTRY_VIEW}"
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
    ' "$registry"
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
[[ -f "$AGENTS_CONFIG_FILE" ]] || fail "agent registry not found: $AGENTS_CONFIG_FILE"
[[ -f "$CODEX_CONFIG_FILE" ]] || fail "Codex config not found: $CODEX_CONFIG_FILE"
# Every query below uses the same complete snapshot, even if Codex rewrites the
# live file between queries. Only the shared reader opens/parses that file.
cache_reader="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
cache_text="$("$cache_reader/py" "$cache_reader/codex_models_cache.py" "$CODEX_MODELS_CACHE_FILE")" || exit 1
REGISTRY_VIEW="$(mktemp "${AGENTS_CONFIG_FILE}.view.XXXXXX")"
agents_registry_text > "$REGISTRY_VIEW"
AGENTS_REGISTRY_BASE="$(cat "$REGISTRY_VIEW")"
trap 'rm -f "$REGISTRY_VIEW"; release_lock' EXIT

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
    ' "$REGISTRY_VIEW"
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

    echo "WARNING: $task is assigned to '$assigned_agent', which is gone from the codex catalog — re-point it: /agent $task <agent>[:<effort>], or switch the family: /agent ${task%%.*} <family>" >&2
    if ! old_row="$(previous_catalog_row "$assigned_agent")"; then
        fail "cannot preserve assigned Codex agent '$assigned_agent': no previous [codex.agents] row exists"
    fi
    preserve_catalog_row "$old_row"
done <<< "$assigned_rows"

warn_missing_claude_aliases

tmp_file="$(mktemp "${AGENTS_CONFIG_FILE}.XXXXXX")"
cleanup() {
    rm -f "$tmp_file" "$REGISTRY_VIEW"
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
done < "$REGISTRY_VIEW"

[[ "$saw_codex_agents" == true ]] || fail "[codex.agents] section not found in $AGENTS_CONFIG_FILE"

# Keep models/efforts seen on another machine and never downgrade a family.
merged_file="$(mktemp "${AGENTS_CONFIG_FILE}.XXXXXX")"
if ! "$AGENTS_CONFIG_DIR/../lib/py" "$AGENTS_CONFIG_DIR/../lib/agents_registry.py" catalog \
    --before "$REGISTRY_VIEW" --after "$tmp_file" > "$merged_file"; then
    rm -f "$merged_file"
    fail "cannot merge Codex catalog additions"
fi
mv "$merged_file" "$tmp_file"

# Compare catalog data rather than generated whitespace.
catalog_current=true
for section in codex.agents codex.families; do
    if [[ "$(registry_section_rows "$section" "$tmp_file" | LC_ALL=C sort)" \
        != "$(registry_section_rows "$section" | LC_ALL=C sort)" ]]; then
        catalog_current=false
        break
    fi
done

if [[ "$catalog_current" == true ]]; then
    if [[ "$CHECK_ONLY" == false ]]; then
        # A prior sync may have stopped after writing but before publication.
        agents_registry_store "$tmp_file" "add Codex catalog models and efforts" additive-catalog
        checkpoint_sync_state
    fi
    echo "Codex agent catalog is current."
    exit 0
fi

if [[ "$CHECK_ONLY" == true ]]; then
    echo "Codex agent catalog is stale: $AGENTS_CONFIG_FILE" >&2
    exit 1
fi

agents_registry_store "$tmp_file" "add Codex catalog models and efforts" additive-catalog
rm -f "$REGISTRY_VIEW"
trap release_lock EXIT
checkpoint_sync_state
echo "Updated Codex agent catalog: $AGENTS_CONFIG_FILE"

#!/usr/bin/env bash

# Shared definition of this repo's git clean filters, sourced by
# ensure_git_filters.sh (install + repair) and refresh_filtered_index.sh (the
# launchd watcher). One table so the two can never drift.
#
# Fields: <driver name>:<clean script>:<smudge command>:<filtered path>
# Scripts are repo-relative; git runs filters from the working-tree root.
GIT_FILTERS=(
  "claude-settings:scripts/settings/clean_settings_json.sh:scripts/settings/smudge_settings_json.sh:settings.json"
)

# Drivers this repo no longer uses, as <driver name>:<path it filtered>.
# git_filters_retire removes each from .git/config.
RETIRED_GIT_FILTERS=(
  "claude-agents-conf:config/agents.conf"
)

# Split one GIT_FILTERS entry into GF_NAME, GF_CLEAN, GF_SMUDGE, GF_PATH.
git_filters_parse() {
    local entry="$1" rest
    GF_NAME="${entry%%:*}"
    rest="${entry#*:}"
    GF_CLEAN="${rest%%:*}"
    rest="${rest#*:}"
    GF_SMUDGE="${rest%%:*}"
    GF_PATH="${rest#*:}"
}

GIT_FILTERS_LABEL="com.natemccoy.claude-settings-git-refresh"

# Make `git status` agree with `git diff` for a filtered path.
#
# git status does not run clean filters. It compares stat data, so a filtered
# file whose working copy legitimately differs from its committed form reads as
# modified even though `git diff` correctly reports nothing. Refreshing the
# index records stat data for the entry, after which status goes quiet and
# stays quiet.
#
# Only paths whose filtered content already matches the index are touched: a
# genuine change must stay visible, which is the whole point of the check
# against clean_hash below.
git_filters_refresh_path() {
    local repo_root="$1" clean_script="$2" path="$3"
    local clean_hash index_entry index_mode index_hash refreshed_hash

    [[ -f "$repo_root/$path" ]] || return 0
    [[ -x "$repo_root/$clean_script" ]] || return 0

    clean_hash="$(
        "$repo_root/$clean_script" < "$repo_root/$path" 2>/dev/null |
            git -C "$repo_root" hash-object --stdin 2>/dev/null
    )" || return 0
    [[ -n "$clean_hash" ]] || return 0

    index_entry="$(git -C "$repo_root" ls-files --stage -- "$path" 2>/dev/null)" || return 0
    [[ -n "$index_entry" ]] || return 0
    read -r index_mode index_hash _ <<<"$index_entry"

    # A real change to the filtered content — leave it showing.
    [[ "$clean_hash" == "$index_hash" ]] || return 0

    git -C "$repo_root" update-index --refresh -- "$path" >/dev/null 2>&1 || true

    # --refresh should only rewrite stat data, never the blob. Verify, and put
    # the entry back if it ever does otherwise: staging the unfiltered content
    # is the one failure here that would actually lose the filter's protection.
    refreshed_hash="$(git -C "$repo_root" rev-parse --verify --quiet ":$path" 2>/dev/null)" || return 0
    if [[ -n "$refreshed_hash" && "$refreshed_hash" != "$index_hash" ]]; then
        git -C "$repo_root" update-index --cacheinfo "$index_mode" "$index_hash" "$path" 2>/dev/null || true
    fi
}

git_filters_refresh_all() {
    local repo_root="$1" entry
    for entry in "${GIT_FILTERS[@]}"; do
        git_filters_parse "$entry"
        git_filters_refresh_path "$repo_root" "$GF_CLEAN" "$GF_PATH"
    done
}

# Remove each retired driver from .git/config, and make the index see its path
# as it is now.
#
# While the filter ran, the index recorded the stat data of the working file,
# not of the committed blob. With the filter gone, that stat data is wrong in
# both directions: a file that still holds the old local rows matches it, so
# git calls the file clean and `git checkout --` does nothing; a file reset to
# the commit has another size, and git calls a different size modified without
# comparing the bytes, so --refresh and --really-refresh report "needs update".
# Recording the same blob again with --cacheinfo clears the stat data without
# changing what is staged, and the refresh then records the file as it is:
# clean when it matches the commit, modified when it does not. Once only:
# the driver's presence in .git/config is the mark that it has not run.
git_filters_retire() {
    local repo_root="$1" entry name path index_entry index_mode index_hash
    for entry in "${RETIRED_GIT_FILTERS[@]}"; do
        name="${entry%%:*}"
        path="${entry#*:}"
        git -C "$repo_root" config --local --get-regexp "^filter\.$name\." >/dev/null 2>&1 || continue
        index_entry="$(git -C "$repo_root" ls-files --stage -- "$path" 2>/dev/null)" || index_entry=""
        if [[ -n "$index_entry" ]]; then
            read -r index_mode index_hash _ <<<"$index_entry"
            if git -C "$repo_root" update-index --cacheinfo "$index_mode,$index_hash,$path" 2>/dev/null; then
                # No path after --refresh: update-index takes a path there as one to stage.
                git -C "$repo_root" update-index -q --refresh >/dev/null 2>&1 || true
            fi
        fi
        if git -C "$repo_root" config --local --remove-section "filter.$name" 2>/dev/null; then
            echo "ensure_git_filters: removed the retired filter.$name from $repo_root"
        else
            echo "ensure_git_filters: could not remove the retired filter.$name in $repo_root." >&2
        fi
    done
}

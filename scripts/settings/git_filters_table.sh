#!/usr/bin/env bash

# Shared definition of this repo's git clean filters, sourced by
# ensure_git_filters.sh (install + repair) and refresh_filtered_index.sh (the
# launchd watcher). One table so the two can never drift.
#
# Fields: <driver name>:<clean script, repo-relative>:<filtered path>
GIT_FILTERS=(
  "claude-settings:scripts/settings/clean_settings_json.sh:settings.json"
  "claude-agents-conf:scripts/agents/clean_agents_conf.sh:config/agents.conf"
)

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
    local repo_root="$1" entry rest
    for entry in "${GIT_FILTERS[@]}"; do
        rest="${entry#*:}"
        git_filters_refresh_path "$repo_root" "${rest%%:*}" "${rest#*:}"
    done
}

#!/usr/bin/env bash

# The settings.json keys that never reach a commit, and the sidecar that keeps
# them alive across a checkout. Sourced by the clean and smudge filters, the
# watcher, and ensure_git_filters.sh -- one list so none of them can drift.
#
# Why a sidecar exists: git unlinks the old working copy before it runs the
# smudge filter, so the smudge cannot read these keys from the file it is
# replacing. The watcher fires after every write to settings.json and copies
# the keys here; the smudge merges them back into whatever git writes. The
# file is repo-relative and falls under .gitignore's catch-all.

SETTINGS_LOCAL_KEYS_JSON='["model","effortLevel","modelSettings"]'
SETTINGS_LOCAL_KEYS_SIDECAR="settings.local-keys.json"

# jq: keep only the local-only keys of an object.
SETTINGS_LOCAL_KEYS_PICK='with_entries(select(.key as $k | $keys | index($k) != null))'

# jq: append the sidecar keys the input lacks, in the input's own key order.
# An existing key is never touched, so a value the user just changed is not
# rolled back by a stale snapshot, and a file with nothing missing comes out
# byte-identical.
SETTINGS_LOCAL_KEYS_MERGE='. as $cur | . + ($local[0] | with_entries(.key as $k | select(($cur | has($k)) | not)))'

# jq: how many sidecar keys the input lacks.
SETTINGS_LOCAL_KEYS_MISSING='. as $cur | [$local[0] | keys[] | . as $k | select(($cur | has($k)) | not)] | length'

# Copy the local-only keys out of settings.json into the sidecar.
#
# The sidecar mirrors whichever of these keys settings.json currently has, so
# removing an override -- picking the default model, for one, which Claude Code
# records by deleting the `model` key rather than writing one -- propagates
# here and the smudge stops restoring it. That is the only way a key can ever
# be retired; the cost is that a bad write which drops one key takes the backup
# for it within seconds.
#
# The one case that is refused is an ALL-empty result: a bare blob landing in
# the working copy (smudge not installed, or failed) must not erase the whole
# backup that exists to repair it.
settings_local_keys_snapshot() {
    local repo_root="$1" settings sidecar snapshot
    settings="$repo_root/settings.json"
    sidecar="$repo_root/$SETTINGS_LOCAL_KEYS_SIDECAR"
    [[ -f "$settings" ]] || return 0

    snapshot="$(jq --argjson keys "$SETTINGS_LOCAL_KEYS_JSON" \
        "$SETTINGS_LOCAL_KEYS_PICK" "$settings" 2>/dev/null)" || return 0
    [[ -n "$snapshot" && "$snapshot" != "{}" ]] || return 0
    [[ -f "$sidecar" && "$(cat "$sidecar" 2>/dev/null)" == "$snapshot" ]] && return 0

    printf '%s\n' "$snapshot" > "$sidecar.tmp" 2>/dev/null &&
        mv "$sidecar.tmp" "$sidecar" 2>/dev/null
}

# Put back any local-only key the sidecar has and settings.json lacks. This is
# the repair for a checkout that ran before the smudge was installed, and it
# runs at session start, never from the watcher, so a write Claude Code just
# made is never fought.
settings_local_keys_restore() {
    local repo_root="$1" settings sidecar missing merged
    settings="$repo_root/settings.json"
    sidecar="$repo_root/$SETTINGS_LOCAL_KEYS_SIDECAR"
    [[ -f "$settings" && -s "$sidecar" ]] || return 0

    # Decide on keys, not text: jq reformats, so a text comparison would
    # rewrite a file that is only laid out differently.
    missing="$(jq --slurpfile local "$sidecar" \
        "$SETTINGS_LOCAL_KEYS_MISSING" "$settings" 2>/dev/null)" || return 0
    [[ "$missing" =~ ^[0-9]+$ && "$missing" -gt 0 ]] || return 0

    merged="$(jq --slurpfile local "$sidecar" \
        "$SETTINGS_LOCAL_KEYS_MERGE" "$settings" 2>/dev/null)" || return 0
    [[ -n "$merged" ]] || return 0

    printf '%s\n' "$merged" > "$settings.tmp" 2>/dev/null &&
        mv "$settings.tmp" "$settings" 2>/dev/null &&
        echo "ensure_git_filters: restored local-only keys into settings.json from $SETTINGS_LOCAL_KEYS_SIDECAR"
}

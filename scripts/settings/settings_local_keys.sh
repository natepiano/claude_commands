#!/usr/bin/env bash

# The settings.json keys and hook groups that never reach a commit, and the
# sidecar that keeps them alive across a checkout. Sourced by the clean and smudge filters, the
# watcher, and ensure_git_filters.sh -- one list so none of them can drift.
#
# Why a sidecar exists: git unlinks the old working copy before it runs the
# smudge filter, so the smudge cannot read these keys from the file it is
# replacing. The watcher fires after every write to settings.json and copies
# the keys here; the smudge merges them back into whatever git writes. The
# file is repo-relative and falls under .gitignore's catch-all.

SETTINGS_LOCAL_KEYS_JSON='["model","effortLevel","modelSettings"]'
SETTINGS_LOCAL_KEYS_SIDECAR="settings.local-keys.json"

# Hook groups that belong to one machine, matched by a substring of any of
# their commands. iTerm2's Claude Code integration adds a group running
# ~/.config/iterm2/cc-status to every hook event on the Mac -- a path that
# exists nowhere else, so committed it would fail on every event on natedev.
# The sidecar keeps these under its own `hooks` key, holding only the local
# groups, and the merge appends them per event rather than as a whole key.
SETTINGS_LOCAL_HOOK_PATTERNS_JSON='["/.config/iterm2/cc-status"]'

# jq definitions every program below starts with.
SETTINGS_LOCAL_DEFS="def local_hook: any(.hooks[]?; (.command // \"\") as \$c | ${SETTINGS_LOCAL_HOOK_PATTERNS_JSON} | any(. as \$p | \$c | contains(\$p)));
def local_hooks: with_entries(.value |= map(select(local_hook))) | with_entries(select(.value | length > 0));
def shared_hooks: with_entries(.value |= map(select(local_hook | not))) | with_entries(select(.value | length > 0));"

# jq: what the clean filter commits -- the input without its local-only keys
# or local hook groups, its hook events sorted by name. Event order carries no
# meaning, and iTerm2 rewrites the object alphabetically when it adds its
# groups, so an unsorted blob would read as changed after every such write.
SETTINGS_LOCAL_KEYS_STRIP="$SETTINGS_LOCAL_DEFS"'
delpaths($keys | map([.]))
| if has("hooks") then .hooks |= (shared_hooks | to_entries | sort_by(.key) | from_entries) else . end'

# jq: keep only the local-only keys of an object, plus its local hook groups.
SETTINGS_LOCAL_KEYS_PICK="$SETTINGS_LOCAL_DEFS"'
. as $s
| ($s | with_entries(select(.key as $k | $keys | index($k) != null)))
+ ($s.hooks // {} | local_hooks | if length == 0 then {} else {hooks: .} end)'

# jq: append the sidecar keys the input lacks, in the input's own key order,
# then each sidecar hook group its event lacks, at the end of that event.
# An existing key is never touched, so a value the user just changed is not
# rolled back by a stale snapshot, and a file with nothing missing comes out
# byte-identical.
SETTINGS_LOCAL_KEYS_MERGE='. as $cur | ($local[0] // {}) as $l
| . + ($l | del(.hooks) | with_entries(.key as $k | select(($cur | has($k)) | not)))
| reduce (($l.hooks // {}) | to_entries[]) as $e (.;
    reduce $e.value[] as $g (.;
      if ((.hooks[$e.key] // []) | index([$g])) != null then .
      else .hooks[$e.key] += [$g] end))'

# jq: how many sidecar keys and hook groups the input lacks.
SETTINGS_LOCAL_KEYS_MISSING='. as $cur | ($local[0] // {}) as $l
| ([$l | del(.hooks) | keys[] | . as $k | select(($cur | has($k)) | not)] | length)
+ ([($l.hooks // {}) | to_entries[] | .key as $k | .value[] | . as $g
    | select((($cur.hooks[$k] // []) | index([$g])) == null)] | length)'

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

#!/usr/bin/env bash

# jq variables must remain literal inside the programs.
# shellcheck disable=SC2016

# Keep machine settings in a sidecar before checkout replaces settings.json.
# extraKnownMarketplaces stores paths on this machine.
# theme stores the theme for this machine.
# voice stores the voice setting for this machine.
SETTINGS_LOCAL_KEYS_JSON='["model","effortLevel","modelSettings","extraKnownMarketplaces","theme","voice"]'
SETTINGS_LOCAL_KEYS_SIDECAR="settings.local-keys.json"
SETTINGS_LOCAL_KEYS_NOTICES="settings.local-key-notices.json"
_SETTINGS_LOCAL_ROOT="$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd)"

settings_local_keys_baseline() {
    local baseline
    baseline="$(git -C "$_SETTINGS_LOCAL_ROOT" show HEAD:settings.json)" || {
        echo "settings: cannot read committed HEAD:settings.json" >&2
        return 1
    }
    printf '%s\n' "$baseline" | jq -ce 'if type == "object" then keys else error("settings must be an object") end'
}

# Preserve hook groups that run commands on one machine.
SETTINGS_LOCAL_HOOK_PATTERNS_JSON='["/.config/iterm2/cc-status","/.claude/hooks/herdr-agent-state.sh"]'

# Preserve entries from local plugin marketplaces.
SETTINGS_LOCAL_MARKETPLACES_JSON='["nate-local"]'

# jq definitions every program below starts with.
SETTINGS_LOCAL_DEFS="def merged_key: . == \"hooks\" or . == \"enabledPlugins\";
def listed_key: . as \$k | (${SETTINGS_LOCAL_KEYS_JSON} | index(\$k)) != null;
def local_key: listed_key or ((merged_key | not) and (. as \$k | (\$baseline | index(\$k)) == null));
def saved_entries: with_entries(select(.key | merged_key or local_key));
def local_hook: any(.hooks[]?; (.command // \"\") as \$c | ${SETTINGS_LOCAL_HOOK_PATTERNS_JSON} | any(. as \$p | \$c | contains(\$p)));
def local_hooks: with_entries(.value |= map(select(local_hook))) | with_entries(select(.value | length > 0));
def shared_hooks: with_entries(.value |= map(select(local_hook | not))) | with_entries(select(.value | length > 0));
def local_plugin: .key as \$k | ${SETTINGS_LOCAL_MARKETPLACES_JSON} | any(. as \$m | \$k | endswith(\"@\" + \$m));"

# Remove local values and sort shared hook events.
# The clean filter uses this program.
# shellcheck disable=SC2034
SETTINGS_LOCAL_KEYS_STRIP='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'
with_entries(select(.key | local_key | not))
| if has("hooks") then
    (.hooks | length) as $before
    | .hooks |= (shared_hooks | to_entries | sort_by(.key) | from_entries)
    | if $before > 0 and (.hooks | length) == 0 then del(.hooks) else . end
  else . end
| if has("enabledPlugins") then
    (.enabledPlugins | length) as $before
    | .enabledPlugins |= with_entries(select(local_plugin | not))
    | if $before > 0 and (.enabledPlugins | length) == 0 then del(.enabledPlugins) else . end
  else . end'

# jq: keep only the local-only keys of an object, plus its local hook groups.
SETTINGS_LOCAL_KEYS_PICK='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'
. as $s
| ($s | with_entries(select(.key | local_key)))
+ ($s.hooks // {} | local_hooks | if length == 0 then {} else {hooks: .} end)
+ ($s.enabledPlugins // {} | with_entries(select(local_plugin)) | if length == 0 then {} else {enabledPlugins: .} end)'

# Smudge preserves saved local values and uses the incoming keys as its baseline.
# Restore supplies the committed baseline and keeps existing values.
SETTINGS_LOCAL_KEYS_MERGE='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'. as $cur | ($local[0] // {} | saved_entries) as $l
| . + ($l | del(.hooks, .enabledPlugins) | with_entries(.key as $k | select(($ARGS.named.restore != true) or (($cur | has($k)) | not))))
| reduce (($l.enabledPlugins // {}) | to_entries[]) as $p (.;
    if (.enabledPlugins // {} | has($p.key)) then . else .enabledPlugins[$p.key] = $p.value end)
| reduce (($l.hooks // {}) | to_entries[]) as $e (.;
    reduce $e.value[] as $g (.;
      if ((.hooks[$e.key] // []) | index([$g])) != null then .
      else .hooks[$e.key] += [$g] end))'

# jq: how many sidecar keys and hook groups the input lacks.
SETTINGS_LOCAL_KEYS_MISSING='($ARGS.named.baseline // keys) as $baseline | '"$SETTINGS_LOCAL_DEFS"'. as $cur | ($local[0] // {} | saved_entries) as $l
| ([$l | del(.hooks, .enabledPlugins) | keys[] | . as $k | select(($cur | has($k)) | not)] | length)
+ ([($l.enabledPlugins // {}) | keys[] as $k | select(($cur.enabledPlugins // {} | has($k)) | not)] | length)
+ ([($l.hooks // {}) | to_entries[] | .key as $k | .value[] | . as $g
    | select((($cur.hooks[$k] // []) | index([$g])) == null)] | length)'

# Use one atomic replacement for snapshots and restored settings.
_settings_local_write() {
    local path="$1" text="$2" temporary
    [[ ! -d "$path" ]] || { echo "settings: destination is a directory: $path" >&2; return 1; }
    temporary="$(mktemp "$path.XXXXXX")" || return 1
    if ! { printf '%s\n' "$text" > "$temporary" && mv "$temporary" "$path"; }; then
        rm -f "$temporary"
        return 1
    fi
}

# Avoid the interpreter when the ledger needs no change or delivery.
settings_local_keys_notices() {
    local mode="$1" unknown="${2:-[]}" ledger machine work
    ledger="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_NOTICES"
    machine="$(hostname -s)" || return 1
    if [[ -s "$ledger" ]]; then
        work="$(jq --arg machine "$machine" --arg mode "$mode" --argjson keys "$unknown" '
            .[$machine] // {} | . as $history
            | if $mode == "record" then any($keys[]; . as $key | $history[$key] == null)
              else any(.[]; . == "pending") end' "$ledger")" || return 1
        [[ "$work" == true ]] || return 0
    elif [[ "$mode" == deliver || "$unknown" == "[]" ]]; then
        return 0
    fi
    "$_SETTINGS_LOCAL_ROOT/scripts/lib/py" "$_SETTINGS_LOCAL_ROOT/scripts/settings/local_key_notices.py" \
        "$mode" "$_SETTINGS_LOCAL_ROOT" "$machine" "$unknown"
}

# Keep an empty backup only when the previous values are still local.
settings_local_keys_snapshot() {
    local settings sidecar snapshot baseline unknown
    settings="$1"
    sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
    [[ -f "$settings" ]] || return 0
    baseline="$(settings_local_keys_baseline)" || return 1
    snapshot="$(jq --argjson baseline "$baseline" "$SETTINGS_LOCAL_KEYS_PICK" "$settings")" || return 1
    if [[ "$snapshot" == "{}" && ! -e "$sidecar" ]]; then
        return 0
    fi
    if [[ "$snapshot" == "{}" && -s "$sidecar" ]]; then
        snapshot="$(jq --argjson baseline "$baseline" "$SETTINGS_LOCAL_KEYS_PICK" "$sidecar")" || return 1
    fi
    if [[ ! -f "$sidecar" || "$(<"$sidecar")" != "$snapshot" ]]; then
        _settings_local_write "$sidecar" "$snapshot" || return 1
    fi
    unknown="$(printf '%s\n' "$snapshot" | jq "$SETTINGS_LOCAL_DEFS"'keys | map(select(listed_key or merged_key | not))' \
        --argjson baseline "$baseline" -c)" || return 1
    settings_local_keys_notices record "$unknown"
}

# Restore missing values without replacing a recent user edit.
settings_local_keys_restore() {
    local settings sidecar missing merged baseline
    settings="$_SETTINGS_LOCAL_ROOT/settings.json"
    sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
    [[ -f "$settings" && -s "$sidecar" ]] || return 0
    baseline="$(settings_local_keys_baseline)" || return 1
    missing="$(jq --argjson baseline "$baseline" --slurpfile local "$sidecar" \
        "$SETTINGS_LOCAL_KEYS_MISSING" "$settings")" || return 1
    [[ "$missing" -gt 0 ]] || return 0
    merged="$(jq --argjson baseline "$baseline" --argjson restore true --slurpfile local "$sidecar" \
        "$SETTINGS_LOCAL_KEYS_MERGE" "$settings")" || return 1
    _settings_local_write "$settings" "$merged" || return 1
    echo "ensure_git_filters: restored local-only keys into settings.json from $SETTINGS_LOCAL_KEYS_SIDECAR"
}

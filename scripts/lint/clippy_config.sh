#!/usr/bin/env bash
# Reader for config/clippy.conf — the delegation tuning for /clippy: whether the
# lint scan runs in a subagent, and how wide the parallel fix wave may open.
#
# Source it as a library:
#   source ~/.claude/scripts/lint/clippy_config.sh
#   clippy_config_export || run_the_pipeline_inline
#
# Or run it as a CLI:
#   clippy_config.sh            every key, its value, and what it does
#   clippy_config.sh export     CLIPPY_<NAME>=<value> lines; exit 2 on a problem
#   clippy_config.sh get <key>  one resolved value, nothing else
#
# There are no defaults. A missing file, a missing key, a malformed value, or a
# value below its minimum prints every problem found and exits 2 — never a
# substituted value. /clippy reads that exit as delegation unavailable and runs
# the whole pipeline inline, which is what it did before the file existed.
#
# Contrast with lint_config.sh, which reads config/lint.conf: that file says
# which checks run at all, for every consumer, and fails open so a typo cannot
# silently disable a check. This file only says who runs them, so failing
# closed costs a slower run rather than an unrun lint.

CLIPPY_CONFIG_FILE="${CLIPPY_CONFIG_FILE:-$HOME/.claude/config/clippy.conf}"

# Canonical order. Each entry is `name|kind|minimum|what it controls`, where
# kind is `switch` (on/off) or `int`, and minimum applies to int only. A key in
# the config that is not listed here is rejected as unknown.
CLIPPY_CONFIG_KEYS=(
    "SCAN_AGENT|switch|-|run the mend/clippy/doc scan in its own subagent"
    "FANOUT|switch|-|let an approved fix batch split across parallel fixers"
    "MIN_FINDINGS|int|2|total findings before a fan-out is considered"
    "MIN_FILES|int|2|distinct files the findings must cover to be splittable"
    "MAX_AGENTS|int|2|hard ceiling on concurrent fixers"
    "FINDINGS_PER_AGENT|int|1|findings per fixer, used to size the wave"
    "PROGRESS_INTERVAL_SECONDS|int|30|seconds between supervision reports"
)

_clippy_config_key_names() {
    local entry
    for entry in "${CLIPPY_CONFIG_KEYS[@]}"; do
        printf '%s\n' "${entry%%|*}"
    done
}

_clippy_config_is_key() {
    local name
    for name in $(_clippy_config_key_names); do
        [[ "$name" == "$1" ]] && return 0
    done
    return 1
}

# _clippy_config_field <key> <2=kind|3=minimum|4=description>
_clippy_config_field() {
    local entry rest
    for entry in "${CLIPPY_CONFIG_KEYS[@]}"; do
        if [[ "${entry%%|*}" == "$1" ]]; then
            rest="${entry#*|}"
            case "$2" in
                2) printf '%s\n' "${rest%%|*}" ;;
                3) rest="${rest#*|}"; printf '%s\n' "${rest%%|*}" ;;
                *) rest="${rest#*|}"; printf '%s\n' "${rest#*|}" ;;
            esac
            return 0
        fi
    done
    return 1
}

_clippy_config_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

# Raw value for a key, empty when unset. Comments and section headers ignored.
_clippy_config_raw() {
    local want="$1" line key value
    [[ -f "$CLIPPY_CONFIG_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="$(_clippy_config_trim "$line")"
        [[ -z "$line" ]] && continue
        [[ "$line" == \[*\] ]] && continue
        [[ "$line" != *=* ]] && continue
        key="$(_clippy_config_trim "${line%%=*}")"
        [[ "$key" != "$want" ]] && continue
        value="$(_clippy_config_trim "${line#*=}")"
        printf '%s' "$value"
        return 0
    done < "$CLIPPY_CONFIG_FILE"
}

# Every key in the file that this script does not know about.
_clippy_config_unknown_keys() {
    local line key
    [[ -f "$CLIPPY_CONFIG_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="$(_clippy_config_trim "$line")"
        [[ -z "$line" ]] && continue
        [[ "$line" == \[*\] ]] && continue
        [[ "$line" != *=* ]] && continue
        key="$(_clippy_config_trim "${line%%=*}")"
        _clippy_config_is_key "$key" || printf '%s\n' "$key"
    done < "$CLIPPY_CONFIG_FILE"
}

# Validate one key. Prints the resolved value on stdout and returns 0, or prints
# the problem on stderr and returns 1.
_clippy_config_resolve() {
    local key="$1" kind minimum value
    kind="$(_clippy_config_field "$key" 2)"
    minimum="$(_clippy_config_field "$key" 3)"
    value="$(_clippy_config_raw "$key")"

    if [[ -z "$value" ]]; then
        printf '%s: %s is not set\n' "$CLIPPY_CONFIG_FILE" "$key" >&2
        return 1
    fi

    if [[ "$kind" == "switch" ]]; then
        case "$value" in
            on|off)
                printf '%s' "$value"
                return 0
                ;;
            *)
                printf '%s: %s=%s is not on or off\n' \
                    "$CLIPPY_CONFIG_FILE" "$key" "$value" >&2
                return 1
                ;;
        esac
    fi

    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        printf '%s: %s=%s is not a whole number\n' \
            "$CLIPPY_CONFIG_FILE" "$key" "$value" >&2
        return 1
    fi
    if (( value < minimum )); then
        printf '%s: %s=%s is below its minimum of %s\n' \
            "$CLIPPY_CONFIG_FILE" "$key" "$value" "$minimum" >&2
        return 1
    fi
    printf '%s' "$value"
}

# Print CLIPPY_<NAME>=<value> for every key. Reports every problem it finds
# rather than stopping at the first, and returns 1 when there was any.
clippy_config_export() {
    local key value unknown problems=0 output=""

    if [[ ! -f "$CLIPPY_CONFIG_FILE" ]]; then
        printf 'missing config file: %s\n' "$CLIPPY_CONFIG_FILE" >&2
        return 1
    fi

    for unknown in $(_clippy_config_unknown_keys); do
        printf '%s: unknown key: %s\n' "$CLIPPY_CONFIG_FILE" "$unknown" >&2
        problems=1
    done

    for key in $(_clippy_config_key_names); do
        if value="$(_clippy_config_resolve "$key")"; then
            output+="CLIPPY_${key}=${value}"$'\n'
        else
            problems=1
        fi
    done

    (( problems == 0 )) || return 1
    printf '%s' "$output"
}

_clippy_config_status() {
    local key value state
    for key in $(_clippy_config_key_names); do
        if value="$(_clippy_config_resolve "$key" 2>/dev/null)"; then
            state="$value"
        else
            state="INVALID"
        fi
        printf 'key=%s value=%s controls=%s\n' \
            "$key" "$state" "$(_clippy_config_field "$key" 4)"
    done
    if [[ ! -f "$CLIPPY_CONFIG_FILE" ]]; then
        printf '# note: %s does not exist — /clippy runs everything inline\n' \
            "$CLIPPY_CONFIG_FILE"
    fi
}

_clippy_config_usage() {
    cat <<EOF
Usage: clippy_config.sh [export | get <key>]

  (no args)      print every key, its value, and what it controls
  export         print CLIPPY_<NAME>=<value> lines; exit 2 on any problem
  get <key>      print one resolved value; exit 2 if it is not usable

Keys: $(_clippy_config_key_names | tr '\n' ' ')

Edit this file by hand, as with config/delegate.conf. There is no setter: these
are tuning values for one command, where config/lint.conf holds on/off switches
over the checks themselves and has /lint_config to edit them.

Config: $CLIPPY_CONFIG_FILE
EOF
}

# ── CLI ─────────────────────────────────────────────────────────────────────
# Only when executed, not when sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    set -uo pipefail

    case "${1:-}" in
        "")
            _clippy_config_status
            echo ""
            _clippy_config_usage
            ;;
        export)
            clippy_config_export || exit 2
            ;;
        get)
            if [[ $# -ne 2 ]]; then
                _clippy_config_usage >&2
                exit 2
            fi
            if ! _clippy_config_is_key "$2"; then
                printf 'error: unknown key: %s\n' "$2" >&2
                printf 'valid keys: %s\n' "$(_clippy_config_key_names | tr '\n' ' ')" >&2
                exit 2
            fi
            _clippy_config_resolve "$2" || exit 2
            echo ""
            ;;
        --help|-h|help)
            _clippy_config_usage
            ;;
        *)
            _clippy_config_usage >&2
            exit 2
            ;;
    esac
fi

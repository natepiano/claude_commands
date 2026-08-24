#!/usr/bin/env bash
# Reader and editor for config/lint.conf — the on/off switches for each kind of
# lint check. One key per check; its value applies to every consumer, with no
# per-consumer override.
#
# Source it as a library:
#   source ~/.claude/scripts/lint/lint_config.sh
#   lint_config_enabled mend || skip_the_mend_stage
#   lint_config_skip_notice clippy "cargo clippy -p foo"   # loud skip line
#
# Or run it as the /lint_config CLI:
#   lint_config.sh                    status for every check
#   lint_config.sh <op>               status for one check
#   lint_config.sh <op> on|off        set one check
#   lint_config.sh all on|off         set every check
#   lint_config.sh export             LINT_OP_<NAME>=on|off lines
#   lint_config.sh enabled <op>       exit 0 if on, 1 if off (no output)
#
# A missing config file means every check is on.

LINT_CONFIG_FILE="${LINT_CONFIG_FILE:-$HOME/.claude/config/lint.conf}"

# Canonical order. Each entry is `name|what it runs|where it applies`. A key in
# the config that is not listed here is rejected as unknown.
LINT_CONFIG_OPS=(
    "mend|cargo mend check pass and its --fix pass|/clippy mend check/fix · invoke.sh mend (lint CLI, clean-fix, validate_ci mend steps)"
    "style_review|style-guide walk over the uncommitted diff|/clippy style review · /plan:delegate phase-end gate"
    "clippy|cargo clippy|/clippy clippy stage · invoke.sh clippy (lint CLI, clean-fix, verify.sh lint)"
    "doc|cargo doc with -D warnings|/clippy doc stage · invoke.sh doc (lint doc)"
    "fmt|cargo +nightly fmt|/clippy format stage · invoke.sh fmt (lint fmt, verify.sh lint/fmt/final)"
)

_lint_config_op_names() {
    local entry
    for entry in "${LINT_CONFIG_OPS[@]}"; do
        printf '%s\n' "${entry%%|*}"
    done
}

_lint_config_is_op() {
    local name
    for name in $(_lint_config_op_names); do
        [[ "$name" == "$1" ]] && return 0
    done
    return 1
}

_lint_config_unknown() {
    echo "error: unknown lint check: $1" >&2
    echo "valid checks: $(_lint_config_op_names | tr '\n' ' ')" >&2
}

# _lint_config_field <op> <2=what|3=where>
_lint_config_field() {
    local entry rest
    for entry in "${LINT_CONFIG_OPS[@]}"; do
        if [[ "${entry%%|*}" == "$1" ]]; then
            rest="${entry#*|}"
            if [[ "$2" == "2" ]]; then
                printf '%s\n' "${rest%%|*}"
            else
                printf '%s\n' "${rest#*|}"
            fi
            return 0
        fi
    done
    return 1
}

_lint_config_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

# Raw value for a key, empty when unset. Comments and section headers ignored.
_lint_config_raw() {
    local want="$1" line key value
    [[ -f "$LINT_CONFIG_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="$(_lint_config_trim "$line")"
        [[ -z "$line" ]] && continue
        [[ "$line" == \[*\] ]] && continue
        [[ "$line" != *=* ]] && continue
        key="$(_lint_config_trim "${line%%=*}")"
        [[ "$key" != "$want" ]] && continue
        value="$(_lint_config_trim "${line#*=}")"
        printf '%s' "$value"
        return 0
    done < "$LINT_CONFIG_FILE"
}

# Resolved state for a key: on | off. Unset or malformed resolves to on, with a
# warning on stderr for malformed. Failing open is deliberate — a typo in this
# file must not silently disable a check.
lint_config_state() {
    local op="$1" value
    if ! _lint_config_is_op "$op"; then
        _lint_config_unknown "$op"
        return 2
    fi
    value="$(_lint_config_raw "$op")"
    case "$value" in
        on|off) printf '%s' "$value" ;;
        "") printf 'on' ;;
        *)
            echo "warning: $LINT_CONFIG_FILE: $op=$value is not on/off — treating as on" >&2
            printf 'on'
            ;;
    esac
}

# Exit 0 when the check is on, 1 when off, 2 when the name is unknown.
# LINT_CONFIG_FORCE=1 makes every known check report on: a pre-push gate
# (validate_ci.sh) must not silently no-op because a dev-loop switch is off,
# so it sets this per step on every check it does not itself gate.
lint_config_enabled() {
    if [[ "${LINT_CONFIG_FORCE:-0}" == "1" ]]; then
        if ! _lint_config_is_op "$1"; then
            _lint_config_unknown "$1"
            return 2
        fi
        return 0
    fi
    local state
    state="$(lint_config_state "$1")" || return 2
    [[ "$state" == "on" ]]
}

# lint_config_skip_notice <op> <what was skipped>
# Prints the line a caller emits in place of the command it did not run. Loud on
# purpose: a skipped check must never read as a passing one.
lint_config_skip_notice() {
    printf 'SKIPPED: %s — %s=off in %s\n' "$2" "$1" "$LINT_CONFIG_FILE"
}

_lint_config_set() {
    local op="$1" value="$2" tmp
    if ! _lint_config_is_op "$op"; then
        _lint_config_unknown "$op"
        return 1
    fi
    case "$value" in
        on|off) ;;
        *)
            echo "error: value must be on or off, got: $value" >&2
            return 1
            ;;
    esac

    if [[ ! -f "$LINT_CONFIG_FILE" ]]; then
        echo "error: missing config file: $LINT_CONFIG_FILE" >&2
        return 1
    fi

    tmp="$LINT_CONFIG_FILE.tmp.$$"
    if ! awk -v op="$op" -v val="$value" '
        {
            line = $0
            body = line
            sub(/#.*/, "", body)
            key = body
            sub(/=.*/, "", key)
            gsub(/^[ \t]+|[ \t]+$/, "", key)
            if (key == op && body ~ /=/) {
                comment = ""
                if (match(line, /#.*/)) comment = "  " substr(line, RSTART, RLENGTH)
                print op "=" val comment
                found = 1
                next
            }
            print line
        }
        END { if (!found) print op "=" val }
    ' "$LINT_CONFIG_FILE" > "$tmp"; then
        rm -f "$tmp"
        echo "error: failed to rewrite $LINT_CONFIG_FILE" >&2
        return 1
    fi
    mv "$tmp" "$LINT_CONFIG_FILE"
}

_lint_config_status() {
    local op state want="${1:-}"
    if [[ -n "$want" ]] && ! _lint_config_is_op "$want"; then
        _lint_config_unknown "$want"
        return 1
    fi
    for op in $(_lint_config_op_names); do
        if [[ -n "$want" && "$want" != "$op" ]]; then
            continue
        fi
        state="$(lint_config_state "$op")"
        printf 'op=%s state=%s runs=%s applies=%s\n' \
            "$op" "$state" "$(_lint_config_field "$op" 2)" "$(_lint_config_field "$op" 3)"
    done
    if [[ ! -f "$LINT_CONFIG_FILE" ]]; then
        echo "# note: $LINT_CONFIG_FILE does not exist — every check defaults to on"
    fi
}

_lint_config_usage() {
    cat <<EOF
Usage: lint_config.sh [<op>] | <op> on|off | all on|off | export | enabled <op>

  (no args)          print every lint check and its state
  <op>               print one check
  <op> on|off        turn one check on or off
  all on|off         turn every check on or off
  export             print LINT_OP_<NAME>=on|off lines for shell consumers
  enabled <op>       exit 0 if on, 1 if off — no output

Checks: $(_lint_config_op_names | tr '\n' ' ')

Not gated by this file: nextest, verify.sh check/test/example, the workspace
check and test inside verify.sh final, pre_release_checks.sh, and every
validate_ci.sh step except its two cargo-mend steps, which the mend switch
does gate. The gates (validate_ci, pre_release_checks) route through the lint
CLI too but set LINT_CONFIG_FORCE=1 on the steps they never allow to skip.

Examples:
  lint_config.sh
  lint_config.sh mend off
  lint_config.sh mend on
  lint_config.sh doc

Config: $LINT_CONFIG_FILE
EOF
}

# ── CLI ─────────────────────────────────────────────────────────────────────
# Only when executed, not when sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    set -euo pipefail

    case "${1:-}" in
        "")
            _lint_config_status
            echo ""
            _lint_config_usage
            ;;
        export)
            for _op in $(_lint_config_op_names); do
                printf 'LINT_OP_%s=%s\n' \
                    "$(printf '%s' "$_op" | tr '[:lower:]' '[:upper:]')" \
                    "$(lint_config_state "$_op")"
            done
            ;;
        enabled)
            if [[ $# -ne 2 ]]; then
                _lint_config_usage >&2
                exit 2
            fi
            lint_config_enabled "$2"
            ;;
        --help|-h|help)
            _lint_config_usage
            ;;
        all)
            if [[ $# -ne 2 ]]; then
                _lint_config_usage >&2
                exit 2
            fi
            for _op in $(_lint_config_op_names); do
                _lint_config_set "$_op" "$2"
            done
            echo "# updated all checks — $2"
            _lint_config_status
            echo ""
            _lint_config_usage
            ;;
        *)
            if [[ $# -eq 1 ]]; then
                _lint_config_status "$1"
                echo ""
                _lint_config_usage
            elif [[ $# -eq 2 ]]; then
                _lint_config_set "$1" "$2"
                echo "# updated $1 — $2"
                _lint_config_status
                echo ""
                _lint_config_usage
            else
                _lint_config_usage >&2
                exit 2
            fi
            ;;
    esac
fi

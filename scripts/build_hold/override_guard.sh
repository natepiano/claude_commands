# Sourced, never run; bash 3.2 syntax and builtins only.
# With nothing set, a call took 0.03 ms on natedev (2000 calls,
# bash 5.3.9, 2026-10-12).
BUILD_OVERRIDE_VARIABLES="BUILD_HOLD_DIR BUILD_HOLD_RELEASE_DIR MAC_TEST_STATE_DIR MAC_TEST_CONFIG BUILD_TARGETS_SCRIPT BUILD_TARGETS_CONFIG BUILD_TARGETS_NOW PLAN_DELEGATE_ACTIVE_DIR"

# build_override_refusal <file> <its path under scripts/> [<user home>]
build_override_refusal() {
    local file="$1" relative="$2" user_home="${3:-}" name found="" IFS=$' \t\n'
    local home_present="${HOME+x}" home_now="${HOME:-}"
    for name in $BUILD_OVERRIDE_VARIABLES; do
        if [[ -n "${!name+x}" ]]; then found="$name"; break; fi
    done
    if [[ -z "$user_home" ]]; then
        if ! unset HOME 2>/dev/null; then
            printf "%s: HOME is read-only in this shell, so the user's home cannot be read. Run again from a shell where HOME is not read-only.\n" "${file##*/}" >&2
            return 1
        fi
        user_home=~
        if [[ -n "$home_present" ]]; then export HOME="$home_now"; fi
    fi
    if [[ -z "$found" && -n "$home_present" && "$home_now" != "$user_home" ]]; then
        [[ "$home_now" -ef "$user_home" ]] || found=HOME
    fi
    [[ -n "$found" ]] || return 0
    [[ "$file" -ef "$user_home/.claude/scripts/$relative" ]] || return 0
    printf "%s: %s is set to '%s'; the installed scripts under %s do not run with it. Unset it and run again.\n" \
        "${file##*/}" "$found" "${!found}" "$user_home/.claude/scripts" >&2
    return 1
}

# shellcheck shell=sh
# Append one config stem with shell builtins.
conf_record() {
    if [ -n "${CONF_READ_LOG:-}" ]; then
        printf '%s\n' "$1" >> "$CONF_READ_LOG"
    fi
}

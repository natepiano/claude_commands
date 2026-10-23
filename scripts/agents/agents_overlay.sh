#!/usr/bin/env bash
# The agent registry's local overlay: config/agents.local.conf, untracked, holds
# only the rows of this machine that differ from the committed config/agents.conf.
# Readers see the defaults with the overlay over them; every writer stores the
# rows that differ in the overlay and never writes the committed file. A commit
# that changes the defaults therefore never erases local tuning (2026-10-23: a
# clean filter that hid the local rows from git let a fast-forward replace them).
# agents_overlay.awk does the row work.
#
# Sourced by agents_config.sh and sync_codex_catalog.sh after AGENTS_CONFIG_FILE
# is set. Run directly, it moves a full registry's local rows into the overlay:
#
#   agents_overlay.sh migrate --from <registry copy>
#
# The copy is renamed from the pre-rename names ([delegate.*] to [direct.*])
# first. It must not be the committed file itself: reset that to the commit
# first, so the defaults the rows are compared with are the committed ones.

AGENTS_CONFIG_FILE="${AGENTS_CONFIG_FILE:-$HOME/.claude/config/agents.conf}"
AGENTS_LOCAL_CONFIG_FILE="${AGENTS_LOCAL_CONFIG_FILE:-${AGENTS_CONFIG_FILE%.conf}.local.conf}"
AGENTS_OVERLAY_AWK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agents_overlay.awk"

# The registry as readers see it: the defaults with the overlay over them.
agents_registry_text() {
    if [[ -f "$AGENTS_LOCAL_CONFIG_FILE" ]]; then
        awk -v mode=view -f "$AGENTS_OVERLAY_AWK" "$AGENTS_LOCAL_CONFIG_FILE" "$AGENTS_CONFIG_FILE"
    else
        cat "$AGENTS_CONFIG_FILE"
    fi
}

# Store a full registry file: its rows that differ from the defaults become the
# overlay, and the file is removed. No differing row removes the overlay.
agents_registry_store() {
    local registry="$1" tmp_file
    if ! tmp_file="$(mktemp "${AGENTS_LOCAL_CONFIG_FILE}.XXXXXX")"; then
        rm -f "$registry"
        return 1
    fi
    if ! awk -v mode=store -f "$AGENTS_OVERLAY_AWK" "$AGENTS_CONFIG_FILE" "$registry" > "$tmp_file"; then
        rm -f "$tmp_file" "$registry"
        echo "ERROR: cannot write $AGENTS_LOCAL_CONFIG_FILE; the registry was not changed." >&2
        return 1
    fi
    rm -f "$registry"
    if [[ -s "$tmp_file" ]]; then
        chmod 644 "$tmp_file"
        mv "$tmp_file" "$AGENTS_LOCAL_CONFIG_FILE"
    else
        rm -f "$tmp_file" "$AGENTS_LOCAL_CONFIG_FILE"
    fi
}

_agents_overlay_migrate() {
    local source="" renamed
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --from) source="${2:-}"; shift 2 ;;
            *) echo "usage: agents_overlay.sh migrate --from <registry copy>" >&2; return 2 ;;
        esac
    done
    if [[ ! -f "$source" ]]; then
        echo "ERROR: no registry copy at '$source'." >&2
        return 2
    fi
    if [[ "$(cd "$(dirname "$source")" && pwd)/$(basename "$source")" \
        == "$(cd "$(dirname "$AGENTS_CONFIG_FILE")" && pwd)/$(basename "$AGENTS_CONFIG_FILE")" ]]; then
        echo "ERROR: --from is the committed registry. Copy it aside, reset it to the commit, then migrate the copy." >&2
        return 2
    fi
    renamed="$(mktemp "${AGENTS_LOCAL_CONFIG_FILE}.XXXXXX")" || return 1
    awk -v mode=rename -f "$AGENTS_OVERLAY_AWK" "$source" > "$renamed" || { rm -f "$renamed"; return 1; }
    agents_registry_store "$renamed" || return 1
    if [[ -f "$AGENTS_LOCAL_CONFIG_FILE" ]]; then
        echo "Wrote $AGENTS_LOCAL_CONFIG_FILE:"
        cat "$AGENTS_LOCAL_CONFIG_FILE"
    else
        echo "No row differs from $AGENTS_CONFIG_FILE; no overlay written."
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -euo pipefail
    case "${1:-}" in
        migrate) shift; _agents_overlay_migrate "$@" ;;
        *) echo "usage: agents_overlay.sh migrate --from <registry copy>" >&2; exit 2 ;;
    esac
fi

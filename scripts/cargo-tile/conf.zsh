#!/bin/zsh
# rev 66 — bump this comment in every cargo-tile source file on each change
# Shared reader for cargo-tile.conf, plus the idle tint every pane paints
# itself with. Sourced, never executed.
#
#   $TILE_ROOT / $TILE_STATE        this instance's root and state dir
#   tile_conf <key> <default>       prints a whole-second value
#   tile_conf_str <key> <default>   prints a value verbatim (colors)
#   tile_decay_secs                 decay_secs, or the test-mode override
#   tile_test_mode                  true while the grid is in test mode
#   tile_paint <idle|normal> [bg]   repaints the calling session
#
# Every call re-reads the file, so a caller that reads inside its loop picks
# up an edit live. A missing file, missing key or non-numeric value yields the
# default, so a typo can never freeze the grid.

# %x is the file this line lives in, so the conf is found through the
# ~/.cargo-tile/bin symlinks and from the shim copied into a toolchain
TILE_CONF="${${(%):-%x}:A:h}/cargo-tile.conf"

# One cargo-tile instance per root. CARGO_TILE_ROOT points every process
# in an instance at its own state, logs and grid window, so a throwaway
# test grid can run beside the real one without either reaching into the
# other's bookkeeping. TILE_TAG is what a pane advertises in its
# user.cargotile variable and what the orphan sweep matches on — one tag
# shared across roots would let either instance close the other's window.
typeset -g TILE_ROOT="${CARGO_TILE_ROOT:-/tmp/cargo-tile}"
typeset -g TILE_STATE="$TILE_ROOT/state"
typeset -g TILE_ERRLOG="$TILE_ROOT/pane-errors.log"
typeset -g TILE_TAG=grid
[[ "$TILE_ROOT" == /tmp/cargo-tile ]] || TILE_TAG="grid-${TILE_ROOT:t}"

# iTerm2 wants the marker base64-encoded; only the panes need it, so it
# is a function rather than a subshell every caller pays for
tile_tag_b64() { print -rn -- "$TILE_TAG" | base64 }

tile_conf_str() {
    local key="$1" def="$2" val
    val=$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\([^[:space:]#]*\).*/\1/p" \
        "$TILE_CONF" 2>/dev/null | tail -1)
    [[ -n "$val" ]] || val="$def"
    print -r -- "$val"
}

tile_conf() {
    local val
    val=$(tile_conf_str "$1" "$2")
    [[ "$val" == <-> ]] || val="$2"
    print -r -- "$val"
}

# Test mode: `+` and `-` in the summary pane stand in for runs arriving
# and leaving, so growth and decay can be exercised without waiting on
# real builds. It is a marker file rather than an env var because the
# panes are long-lived separate processes — a file is the only thing all
# of them see flip at once. Decay drops to a second in test mode so a
# whole row of panes can be watched falling off instead of timed.
typeset -g TILE_TEST_FLAG="$TILE_STATE/test-mode"
typeset -g TILE_TEST_DECAY=1

tile_test_mode() { [[ -f "$TILE_TEST_FLAG" ]] }

tile_decay_secs() {
    if tile_test_mode; then
        print -r -- "$TILE_TEST_DECAY"
    else
        tile_conf decay_secs 15
    fi
}

# The idle tint has to carry the whole palette, not just the background:
# a grid pane inherits a dark profile, so light green alone would leave
# white-on-light-green text nobody can read. These are the dark-on-light
# counterparts of what a tinted pane actually prints — the summary's cyan
# and magenta headers, the slot's white placeholder and yellow countdown.
# Run output never lands on the tint: a pane with a log in it is painted
# back to the profile. Kept private: only idle_bg is worth tuning.
typeset -g TILE_IDLE_BG_DEFAULT=9fd6a4
typeset -ga TILE_IDLE_PALETTE=(
    fg 1c2a1c   bold 12250f  curbg 2f4a2f
    black 2b3a2b  red 9c2323  green 1f6b2a  yellow 7d5c00
    blue 24549c   magenta 8b2f8b  cyan 1b6f75  white 44544a
    br_black 6b7b6b  br_red bc3a2f  br_green 2f8b3f  br_yellow 9a7100
    br_blue 3465c0   br_magenta a63fa6  br_cyan 27858c  br_white 22321f
)

# iTerm2 holds SetColors on the session, so the tint survives the screen
# clears the panes do on every refresh. It has no matching un-set, which
# is what made the tint stick to panes showing a log: SetColors takes a
# hex value only — `key=default` parses and is then ignored, leaving the
# session exactly as green as it was. The OSC resets (104 palette, 110
# fg, 111 bg, 112 cursor) do land but miss bold and cursor-text, the two
# keys with no reset of their own. RIS is the one thing that puts every
# color back to the profile, and it leaves the session name and the
# cargotile user var alone. It clears the pane, so a caller paints
# normal on the frame it redraws, never after. Paint on transition only.
tile_paint() {  # $1 = idle | normal, $2 = tint already read from the conf
    local mode="$1" bg="${2:-}" i out=""
    if [[ "$mode" != idle ]]; then
        print -rn -- $'\033c'
        return
    fi
    for (( i = 1; i < ${#TILE_IDLE_PALETTE}; i += 2 )); do
        out+=$'\033]1337;SetColors='"${TILE_IDLE_PALETTE[i]}=${TILE_IDLE_PALETTE[i+1]}"$'\007'
    done
    [[ -n "$bg" ]] || bg=$(tile_conf_str idle_bg "$TILE_IDLE_BG_DEFAULT")
    # the cursor block reads as a hole punched in the tint, so its text
    # color is the tint itself rather than a fourth thing to keep in sync
    out+=$'\033]1337;SetColors=curfg='"$bg"$'\007'
    out+=$'\033]1337;SetColors=bg='"$bg"$'\007'
    print -rn -- "$out"
}

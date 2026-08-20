#!/bin/zsh
# rev 53 — bump this comment in every cargo-tile source file on each change
# Shared reader for cargo-tile.conf. Sourced, never executed.
#
#   tile_conf <key> <default>   prints the value
#
# Every call re-reads the file, so a caller that reads inside its loop picks
# up an edit live. A missing file, missing key or non-numeric value yields the
# default, so a typo can never freeze the grid.

# %x is the file this line lives in, so the conf is found through the
# ~/.cargo-tile/bin symlinks and from the shim copied into a toolchain
TILE_CONF="${${(%):-%x}:A:h}/cargo-tile.conf"

tile_conf() {
    local key="$1" def="$2" val
    val=$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\([^[:space:]#]*\).*/\1/p" \
        "$TILE_CONF" 2>/dev/null | tail -1)
    [[ "$val" == <-> ]] || val="$def"
    print -r -- "$val"
}

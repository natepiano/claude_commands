#!/usr/bin/env bash
# m1ddc backend for monitor.sh, for the Mac.
# Called by monitor.sh; not meant to be run directly.
#
# Usage: monitor-macos.sh status
#        monitor-macos.sh switch <mac|linux>
#
# Needs m1ddc (`brew install m1ddc`). Apple Silicon only, which is what this
# Mac is; m1ddc does not support Intel Macs.
set -uo pipefail

M1DDC=${M1DDC:-/opt/homebrew/bin/m1ddc}

# Addressed by EDID rather than by the index m1ddc prints in `display list`,
# because that index shifts as displays come and go and there are three here.
# The separator is `=` -- `edid:` is rejected with "display does not exist",
# which the help text does not make obvious.
DELL_EDID=10AC91D1-0000-0000-0D24-0104B5502178

# Decimal, as m1ddc wants them. Same two inputs the Linux side writes as 0x11
# and 0x1b; see /etc/nixos/modules/monitor.nix for where they came from.
INPUT_LINUX=17 # HDMI 1, from natedev
INPUT_MAC=27   # USB-C, from this Mac

if [[ ! -x "$M1DDC" ]]; then
    echo "monitor: m1ddc not found at $M1DDC -- brew install m1ddc" >&2
    exit 1
fi

# Run m1ddc against the Dell. Output goes to stdout, so callers capture it.
#
# One thing to know about failures here: run locally against a monitor with no
# DDC/CI, m1ddc does NOT report an error. It exits 0 and prints a constant --
# 110 for every feature, with max luminance -128. So a zero exit code is not
# evidence that a read meant anything. That is why current_input below trusts
# only values it recognises. Over ssh the same call fails properly with "DDC
# communication failure", which is the opposite behaviour; do not rely on
# either one.
dell() { "$M1DDC" display "edid=$DELL_EDID" "$@" 2>&1; }

# Prints "linux", "mac", or the raw value when it is neither.
current_input() {
    local out
    out=$(dell get input) || return 1
    case "$out" in
        "$INPUT_LINUX") echo linux ;;
        "$INPUT_MAC") echo mac ;;
        *) echo "$out" ;;
    esac
}

case "${1:-status}" in
    status)
        if input=$(current_input); then
            echo "dell     showing: $input"
        else
            echo "dell     could not be read from here"
        fi
        echo "samsung  no DDC/CI; run 'monitor.sh samsung' for why"
        ;;

    switch)
        dest="${2:?monitor-macos.sh switch needs mac or linux}"
        case "$dest" in
            linux) code=$INPUT_LINUX ;;
            mac) code=$INPUT_MAC ;;
            *)
                echo "monitor: '$dest' is not mac or linux" >&2
                exit 2
                ;;
        esac

        if [[ "$(current_input 2>/dev/null)" == "$dest" ]]; then
            echo "dell is already showing $dest"
            exit 0
        fi

        # No ssh fallback on this side, unlike the Linux one. This Mac can read
        # and write the Dell while the Dell is showing the other machine, so
        # there is never a state it has to reach across to escape.
        if out=$(dell set input "$code"); then
            echo "dell -> $dest"
        else
            echo "monitor: could not switch the Dell" >&2
            echo "$out" >&2
            exit 1
        fi

        if [[ "$dest" == linux ]]; then
            echo
            echo "This Mac can still reach the Dell -- 'monitor.sh dell mac' brings"
            echo "it back from here."
        fi
        ;;

    *)
        echo "monitor-macos.sh: unknown action '${1:-}'" >&2
        exit 2
        ;;
esac

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

# Prints "linux" or "mac" and succeeds. Anything else is printed too, but FAILS
# -- an unrecognised value here is far more likely to be the 110 constant above
# than a real input, and passing it through as though it were a state is how a
# dead monitor gets reported as a working one.
current_input() {
    local out
    out=$(dell get input) || return 1
    case "$out" in
        "$INPUT_LINUX") echo linux ;;
        "$INPUT_MAC") echo mac ;;
        *)
            echo "$out"
            return 1
            ;;
    esac
}

# Wait for the monitor to actually be on the requested input.
#
# This is the only proof a write landed, because m1ddc's exit status is not one
# -- run locally it returns 0 whether or not anything happened. The retries are
# because a panel changing inputs takes a moment to answer for the new one; a
# single immediate read can still report the old value and call a good switch a
# failure.
confirm_input() {
    local want="$1" i
    for i in 1 2 3 4 5; do
        [[ "$(current_input 2>/dev/null)" == "$want" ]] && return 0
        sleep 1
    done
    return 1
}

case "${1:-status}" in
    status)
        if input=$(current_input); then
            echo "dell     showing: $input"
        else
            echo "dell     could not be read${input:+ (m1ddc said '$input')}"
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
        #
        # The write is judged by reading it back, not by m1ddc's exit status,
        # which is 0 either way. Without this a switch against a monitor that
        # is not listening prints "dell -> mac" and returns success.
        out=$(dell set input "$code")
        if confirm_input "$dest"; then
            echo "dell -> $dest"
        else
            echo "monitor: the Dell did not switch to $dest" >&2
            [[ -n "$out" ]] && echo "m1ddc said: $out" >&2
            echo "m1ddc reports success even when nothing happens, so this means" >&2
            echo "the monitor never took the value -- not that the command failed." >&2
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

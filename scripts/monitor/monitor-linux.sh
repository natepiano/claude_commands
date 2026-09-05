#!/usr/bin/env bash
# ddcutil backend for monitor.sh, for the NixOS box (natedev).
# Called by monitor.sh; not meant to be run directly.
#
# Usage: monitor-linux.sh status
#        monitor-linux.sh switch <mac|linux>
#
# Requires ddcutil and /dev/i2c-*, both of which come from
# /etc/nixos/modules/monitor.nix. That module is where the findings behind the
# constants below are written up at length.
set -uo pipefail

# Selected by model, never by display number: ddcutil renumbers displays as
# they appear and disappear, and this machine has two attached.
DELL_MODEL='DELL S3425DW'

# Read off this panel's own capability string rather than a standard code
# table. `ddcutil capabilities` advertises exactly 0x11, 0x12 and 0x1b for
# feature 60 and the OSD lists exactly three inputs, which is what pins 0x1b to
# USB-C. ddcutil prints 0x1b as "Unrecognized value" because MCCS has no name
# for it -- that is the name table coming up empty, not a rejection.
DELL_INPUT_LINUX=0x11 # HDMI 1, from this machine
DELL_INPUT_MAC=0x1b   # USB-C, from the Mac

# The same two inputs as m1ddc wants them: decimal, and named by EDID because a
# display index shifts as displays come and go and the Mac has three.
DELL_EDID=10AC91D1-0000-0000-0D24-0104B5502178
MAC_INPUT_LINUX=17
MAC_INPUT_MAC=27
MAC_SSH_HOST=mac

dell() { ddcutil --model "$DELL_MODEL" "$@" 2>&1; }

# Prints "linux", "mac", or an untranslated code for an input we do not know.
current_input() {
    local out
    out=$(dell getvcp 60) || return 1
    case "$out" in
        *"sl=$DELL_INPUT_LINUX"*) echo linux ;;
        *"sl=$DELL_INPUT_MAC"*) echo mac ;;
        *) echo "${out##*sl=}" ;;
    esac
}

# Drive the Dell from the Mac instead of from here. A backstop, not the norm.
#
# Both machines can reach the Dell regardless of which one it is displaying --
# tested in both directions, including this machine writing to it while the
# Dell was showing the Mac. So the direct path above is expected to work in
# every state, and this branch should never run.
#
# It is kept because the direct path depends on the HDMI link staying up while
# unselected, which is a property of this monitor rather than a guarantee, and
# because the failure it would cover is the one that leaves you unable to get
# your screen back. The ssh path is verified end to end: m1ddc enumerates,
# reads, and writes over ssh.
#
# The cost is that the 1Password agent has to approve the key, so this can sit
# waiting for a tap that never comes. Hence the timeout and the notice.
switch_via_mac() {
    local code="$1"
    echo "the Dell is not reachable from here, so asking the Mac to do it" >&2
    echo "(1Password may ask you to approve the ssh key)" >&2
    timeout 120 ssh "$MAC_SSH_HOST" \
        "PATH=/opt/homebrew/bin:\$PATH m1ddc display edid=$DELL_EDID set input $code" >/dev/null 2>&1
}

case "${1:-status}" in
    status)
        if input=$(current_input); then
            echo "dell     showing: $input"
        else
            # Not the ordinary consequence of the Dell showing the Mac -- this
            # machine reads it fine in that state. Something else is wrong:
            # the monitor asleep, the cable out, i2c gone.
            echo "dell     unreadable (asleep, unplugged, or i2c is gone)"
        fi
        echo "samsung  no DDC/CI; run 'monitor.sh samsung' for why"
        ;;

    switch)
        dest="${2:?monitor-linux.sh switch needs mac or linux}"
        case "$dest" in
            linux)
                code=$DELL_INPUT_LINUX
                mac_code=$MAC_INPUT_LINUX
                ;;
            mac)
                code=$DELL_INPUT_MAC
                mac_code=$MAC_INPUT_MAC
                ;;
            *)
                echo "monitor: '$dest' is not mac or linux" >&2
                exit 2
                ;;
        esac

        if [[ "$(current_input 2>/dev/null)" == "$dest" ]]; then
            echo "dell is already showing $dest"
            exit 0
        fi

        if dell setvcp 60 "$code" >/dev/null; then
            echo "dell -> $dest"
        elif switch_via_mac "$mac_code"; then
            echo "dell -> $dest (via the Mac)"
        else
            echo "monitor: could not switch the Dell, directly or through the Mac" >&2
            echo "run 'monitor.sh dell $dest' on the Mac itself" >&2
            exit 1
        fi

        if [[ "$dest" == mac ]]; then
            # Worth saying out loud, because the screen just went away and the
            # obvious worry is that the way back went with it. It did not.
            echo
            echo "'monitor.sh dell linux' brings it back from here -- this machine can"
            echo "still drive the Dell while the Dell is showing the Mac. Keyboard and"
            echo "mouse never left, over deskflow."
        fi
        ;;

    *)
        echo "monitor-linux.sh: unknown action '${1:-}'" >&2
        exit 2
        ;;
esac

#!/usr/bin/env bash
# Switch which computer a monitor is displaying, over DDC/CI.
#
# Usage: monitor.sh [dell|samsung] [mac|linux]
#        monitor.sh                 -- report which input each monitor is on
#
# Words may come in either order, so "dell mac" and "mac dell" both work.
# Naming no monitor means every switchable one.
#
# This file is the platform-independent half: it works out what was asked and
# hands off to the backend for whichever machine it is running on. The two
# backends address monitors by completely different identifiers -- ddcutil by
# model string, m1ddc by EDID UUID -- so the monitor table lives in each of
# them rather than here.
#
# The Samsung is the exception that belongs here rather than in a backend,
# because it is not a property of either machine: that panel implements no
# DDC/CI at all, so no backend can ever do anything with it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: monitor.sh [dell|samsung] [mac|linux]
       monitor.sh                          report the current input of each monitor

  monitor.sh mac              point every switchable monitor at the Mac
  monitor.sh linux            point every switchable monitor at the Linux box
  monitor.sh dell mac         point the Dell at the Mac
  monitor.sh dell linux       point the Dell at the Linux box

The Samsung C34J79x cannot be switched by software; run `monitor.sh samsung`
for the reason.
EOF
}

# Why there is no software path for this monitor, kept in one place because
# both machines reached the same conclusion independently.
samsung_note() {
    cat <<'EOF'
The Samsung C34J79x has no DDC/CI, so no command on either machine can switch
it. Use its own buttons, or let its auto input detection find the live signal.

This was tested rather than assumed. Its EDID reads perfectly from both
machines, so the wire and the bus are fine -- but i2c address 0x37, where
DDC/CI answers, never responds:

  * from Linux over DisplayPort, ddcutil reports every feature as EIO
  * from the Mac over Thunderbolt, an unrelated stack, m1ddc gets nothing back
  * the monitor was displaying the Mac at the time, so it was not a matter of
    the panel only answering on its active input
  * its OSD has no DDC/CI toggle to turn on

One trap if you ever script against it from the Mac: m1ddc does NOT report the
failure. It exits 0 and prints 110 for every feature, with max luminance -128.
A zero exit code proves nothing there.
EOF
}

TARGET=all
DEST=""

for word in "$@"; do
    case "$word" in
        dell | samsung | all) TARGET="$word" ;;
        mac | macos | darwin) DEST=mac ;;
        linux | natedev) DEST=linux ;;
        -h | --help | help)
            usage
            exit 0
            ;;
        status) ;; # no destination: falls through to the status report below
        *)
            echo "monitor: don't know what '$word' means" >&2
            echo >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "$TARGET" == samsung ]]; then
    samsung_note
    # Asking about it is answerable; asking to switch it is not.
    [[ -n "$DEST" ]] && exit 1
    exit 0
fi

case "$(uname -s)" in
    Darwin) BACKEND="$HERE/monitor-macos.sh" ;;
    Linux) BACKEND="$HERE/monitor-linux.sh" ;;
    *)
        echo "monitor: no backend for $(uname -s)" >&2
        exit 1
        ;;
esac

if [[ ! -x "$BACKEND" ]]; then
    echo "monitor: $BACKEND is missing or not executable" >&2
    exit 1
fi

# No destination means the user asked what the state is, not to change it.
if [[ -z "$DEST" ]]; then
    exec "$BACKEND" status
fi

exec "$BACKEND" switch "$DEST"

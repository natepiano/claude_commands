#!/usr/bin/env bash
# Superseded. The style pipeline's schedule is declared in the nixos flake and
# installed by `rebuild` on both machines:
#
#   /etc/nixos/modules/common/style-fix.nix   the job: fix-trigger.sh every 600s
#   /etc/nixos/modules/linux/style-fix.nix    natedev's 4h TimeoutStartSec
#
# nate.jobs renders that to a systemd user timer on Linux (style-fix.timer,
# `systemctl --user list-timers`) and to a launchd user agent on macOS
# (~/Library/Logs/nate-jobs/style-fix.log). Both exec
# $HOME/.claude/scripts/fix/fix-trigger.sh, so the pipeline itself is
# unchanged — only who schedules it moved.
#
# This script used to bootstrap the hand-written com.natemccoy.style-fix
# plist. Running it now would install a SECOND ten-minute agent alongside the
# nix-managed one, under a different label, and both would fire. That is why
# it refuses rather than warns.

set -euo pipefail

cat >&2 <<'MSG'
setup.sh is superseded: the style-fix schedule is declared in the nixos flake.

  Linux   systemctl --user list-timers style-fix.timer
          systemctl --user status style-fix.service
  macOS   launchctl print "gui/$(id -u)/org.nixos.style-fix"
          tail -f ~/Library/Logs/nate-jobs/style-fix.log

Change the interval in /etc/nixos/modules/common/style-fix.nix and rebuild.
Running this script would install a duplicate agent, so it does nothing.
MSG
exit 1

# Cross-platform pass on claude_commands

Goal: make this repo work on both macOS and NixOS/Linux. Started because hooks
failed on Linux looking for `/opt/homebrew/bin/python3`.

## Key facts

- macOS `python3` on PATH is Apple's `/usr/bin/python3` = **3.9.6**;
  `/opt/homebrew/bin/python3` = **3.13.7**. NixOS `python3` = 3.13.15.
- 30 files here use 3.10+ syntax (`match`, `X | Y`), so 3.9 is not viable. That
  is WHY the brew path was hardcoded -- it was not arbitrary, and a plain
  `#!/usr/bin/env python3` shebang would break the Mac.

## Done

1. `scripts/lib/py` -- new shim; picks a python3 >= 3.10 by VERSION not path.
2. `settings.json` -- 12 hook commands made portable (8 via the shim).
3. `/Users/natemccoy` removed from 16 files, including settings.json and
   .claude/settings.local.json (26 permission/sandbox paths -> `~`): `$HOME` in shell, `~` in docs,
   `Path.home()` in python. `.plist` files left alone -- launchd is macOS-only.
4. BSD-only constructs fixed: `sed -i ''` -> `-i.portbak` + cleanup (accepted by
   both seds), `date -r` -> falls back to GNU `date -d @`, `stat -f` -> falls
   back to `stat -c`.
5. NixOS: jq, fd, basedpyright, uv added to /etc/nixos/modules/development.nix.
6. All hooks smoke-tested on Linux, all rc=0, and confirmed firing live.

## Remaining

### launchd inventory (working through these one at a time)

| # | plist / installer | what it does | trigger | status |
|---|---|---|---|---|
| 1 | scripts/sccache/com.natemccoy.sccache.plist | starts the sccache server | RunAtLoad | **DONE** - no service needed; only SCCACHE_IDLE_TIMEOUT=0 mattered, now in /etc/nixos/modules/development.nix. --start-server is redundant: the client starts the server on first use on both platforms. |
| 2 | scripts/settings/ensure_git_filters.sh (generates its plist inline) | refreshes the filtered settings.json index after a write | WatchPaths | already guards on `command -v launchctl` |
| 3 | scripts/fix/com.natemccoy.style-fix.plist | periodic style-fix pipeline | StartInterval | |
| 4 | scripts/agents/com.natemccoy.codex-agent-catalog-sync.plist | keeps the agent catalog current | RunAtLoad + StartInterval | |
| 5 | scripts/claude_to_codex/com.natemccoy.claude-to-codex-sync.plist | syncs claude config to codex | RunAtLoad + WatchPaths | |
| 6 | scripts/kache/ninja.kunobi.kache-gc.plist | kache garbage collection | KeepAlive + StartInterval | |
| 7 | scripts/prioritize/com.natemccoy.hanadocs-prioritize.plist | hanadocs prioritization watcher | KeepAlive + RunAtLoad | installer/status scripts are launchctl-only |

launchd equivalents on NixOS are systemd user units: RunAtLoad -> `wantedBy =
[ "default.target" ]`, StartInterval -> a `.timer`, WatchPaths -> a `.path`
unit, KeepAlive -> `Restart = "always"`. Declare them in /etc/nixos, not by
writing unit files from a script -- that is the whole point of the machine being
declarative.

- **launchd-only integrations** -- scripts/prioritize/, scripts/settings/
  (git-filter watcher), scripts/sccache/, scripts/agents/, scripts/fix/,
  scripts/claude_to_codex/. launchd does not exist on Linux. Decide per script:
  no-op cleanly, or add systemd user units. `scripts/settings/ensure_git_filters.sh`
  already guards on `command -v launchctl`, which is the pattern to copy.
- **`scripts/release/restore_unreleased.sh`** uses `\n` in a sed replacement,
  which GNU sed expands and BSD sed does not. Pre-existing, not introduced here.
- **Mac-side verification**: none of this has been run on the Mac yet.

## Open question for the .plist files

launchd does not expand `~` or `$HOME` inside `ProgramArguments`, so the Mac
plists genuinely need absolute paths -- which is why `/Users/natemccoy` still
appears in them and nowhere else. Options if that matters: generate each plist
from a template at install time, or leave them, since they are macOS-only files
that Linux never reads.

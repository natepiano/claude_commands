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
| 2 | scripts/settings/ensure_git_filters.sh (generates its plist inline) | refreshes the filtered settings.json index after a write | WatchPaths | **DONE** - /etc/nixos/modules/claude-git-filters.nix is the systemd .path equivalent. refresh_filtered_index.sh no longer pins PATH to homebrew. Verified both ways: the false 'M' is silenced, a real edit still shows. |
| 3 | scripts/fix/com.natemccoy.style-fix.plist | periodic style-fix pipeline | StartInterval | **DONE** - /etc/nixos/modules/style-fix.nix is a systemd .timer + oneshot. Uses OnUnitInactiveSec (gap measured from run END) rather than launchd's fire-regardless StartInterval; the pgrep guard stays for hand-started runs. Verified: fired on its own, ran fix.sh to completion, Result=success. Needed four prerequisites - codex installed (modules/codex.nix), a top-level `model` in ~/.codex/config.toml (modules/codex-config-seed.toml, or catalog sync fails silently), 34 `#!/bin/bash` shebangs converted (NixOS has only /bin/sh), and a guard on `source ~/.cargo/env` (rustup writes it, nix does not; `set -e` killed the run there). |
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

## Tilde expansion: one real gap (2026-09-03)

Claude Code expands `~` in `sandbox.filesystem.allowWrite`, but NOT when it
derives sandbox write paths from an `Edit(~/...)` permission rule -- there the
literal string is used. Confirmed on both platforms (Mac agent on 2.1.259; on
NixOS this session's own sandbox write list carries a literal `~/.claude`).

Consequence of the tilde conversion: `Edit(~/.claude/**)` no longer contributes
a real write path, so sandboxed shell writes under `~/.claude` from another
project cwd fail. `~/Library/Application Support` was unaffected because it has
its own allowWrite line.

FIXED in a15d6b8: `"~/.claude"` added to `sandbox.filesystem.allowWrite` in
settings.json, after `"~/.cargo"`. Committed on the Mac and pulled here, so both
platforms carry it. Note the new entry only reaches a session started after the
pull -- Claude Code freezes its sandbox config at session start.

### settings.json local-only keys survive a pull

FIXED: the clean filter (scripts/settings/clean_settings_json.sh) strips
`model`, `effortLevel`, and `modelSettings`, so they live only in the working
copy. They used to vanish whenever a pull rewrote settings.json, because the
smudge was `cat`. Now the watcher (launchd here, the systemd path unit on
NixOS, both running refresh_filtered_index.sh) copies those keys to
`settings.local-keys.json` after every write, and the smudge
(scripts/settings/smudge_settings_json.sh) merges them back into whatever git
writes. ensure_git_filters.sh installs the smudge at session start and, before
its index refresh, puts back any key the sidecar has and settings.json lacks.
Key list and both jq expressions live in scripts/settings/settings_local_keys.sh.

One-time sequence on NixOS, because the smudge is not installed there until
after the pull that would lose the keys:

    cd ~/.claude
    jq '{model, effortLevel, modelSettings} | with_entries(select(.value != null))' settings.json > settings.local-keys.json
    git pull
    bash scripts/settings/ensure_git_filters.sh </dev/null
    jq -c '{model, effortLevel}' settings.json

The last line should print the values from before the pull. The `</dev/null`
matters only from inside a Claude Code session, where the scripts' stdin drain
otherwise blocks on the tool shell's open pipe.

### Remaining launchd items

4 codex-agent-catalog-sync, 5 claude-to-codex-sync, 6 kache-gc,
7 hanadocs-prioritize. Item 7 is the big one: scripts/prioritize/ carries 77
hardcoded macOS paths (/bin/sleep, /usr/bin/python3, /bin/launchctl).

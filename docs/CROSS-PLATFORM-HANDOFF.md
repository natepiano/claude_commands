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

- **launchd-only integrations** -- scripts/prioritize/, scripts/settings/
  (git-filter watcher), scripts/sccache/, scripts/agents/, scripts/fix/,
  scripts/claude_to_codex/. launchd does not exist on Linux. Decide per script:
  no-op cleanly, or add systemd user units. `scripts/settings/ensure_git_filters.sh`
  already guards on `command -v launchctl`, which is the pattern to copy.
- **`scripts/release/restore_unreleased.sh`** uses `\n` in a sed replacement,
  which GNU sed expands and BSD sed does not. Pre-existing, not introduced here.
- **Mac-side verification**: none of this has been run on the Mac yet.

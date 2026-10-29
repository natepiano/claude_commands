---
description: Show each build target's recorded state; use `--probe` to check the Mac connection.
---

Run `python3 ~/.claude/scripts/build_targets/build_targets.py status` and show its lines unchanged. `$ARGUMENTS` accepts only `--probe`: add it to run `python3 ~/.claude/scripts/build_targets/build_targets.py status --probe`. Without it, this command only reads coordination state. `/build:mac status` instead settles block state and can change it.

Use `/build:queue` for waiting steps, their wait reasons, and running steps' promised and measured memory.

- `free`: start work through its normal gate.
- `running`: let the named job finish. The Mac lists one `running <what> since <time>` per repository's claim, joined by `; `. A Mac kept claim still appears as `running <what> since <time>`; `/build:mac status` shows its leftovers and pid. Confirm its remote work ended before `python3 ~/.claude/scripts/mac_test/mac_test.py release --pid <pid>`.
- `held`: wait for the named holder or release your own with `/build:hold release [target mac]`. A Mac hold past expiry remains until its block file is removed; use `/build:mac unblock`, after `github-warmup` if CI restore failed.
- `unreachable`: run `/build:status --probe` to check the connection, then restore access before retrying Mac work.
- `state unreadable`: inspect the named path. For a holder file under `~/.local/state/build-hold/`, have its holder repeat `/build:hold hold <why>` or release it. For `~/.local/state/build-hold-release/current` or its `cycle.json`, `/build:hold release` sets the damaged record aside. For `~/.local/state/mac-test/block.json`, `run.json` or `claims/<repo>.json`, repair the file or remove the damaged record after checking its holder and any job still running; use `/build:mac block` or `unblock` for a block, or `mac_test.py release --pid <pid>` for a readable kept claim.

---
description: Show build target status.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_targets/build_targets.py status` and show its lines unchanged. `$ARGUMENTS` accepts only `--probe`: add it to run `~/.claude/scripts/lib/py ~/.claude/scripts/build_targets/build_targets.py status --probe`. Without it, this command only reads coordination state. `/build:mac status` instead settles block state and can change it.

Use `/build:queue` for waiting steps, their wait reasons, and running steps' promised and measured memory.

The first line is `MAC IDLE` when no natedev build or test runs on the Mac for the configured alert interval (default 3 minutes) while eligible steps wait. It is bold red in a terminal and plain when piped. The holder clears a block with `/build:mac unblock`. For an idle claim, resume build or test work or release it so waiting steps can take the Mac. For a free Mac, the builds showrunner checks why waiting steps do not take it. The line clears when work starts or no eligible steps wait. `MAC UNREACHABLE` appears immediately for a connection outage; restore access. `MAC IDLE WATCH` names unreadable state or a reading past its configured freshness limit (default 5 minutes); repair the named state or check `buildlog-sample.timer`. This line reads the saved result and local timing config.

- `free`: start work through its normal gate.
- `running`: let the named job finish. The Mac lists one `running <what> since <time>` per repository's claim, joined by `; `. A Mac kept claim still appears as `running <what> since <time>`; `/build:mac status` shows its leftovers and pid. Confirm its remote work ended before `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py release --pid <pid>`.
  Claims and holds with a known holder session add ` (showrunner <name>, unit <name>)` after `since <time>`. Missing relationships show `—`; an unknown holder or failed lookup keeps the original line.
- `held`: wait for the named holder or release your own with `/build:hold release [target mac]`. A Mac hold past expiry remains until its block file is removed; use `/build:mac unblock`, after `github-warmup` if CI restore failed.
- `unreachable`: run `/build:status --probe` to check the connection, then restore access before retrying Mac work.
- `state unreadable`: inspect the named path. For a holder file under `~/.local/state/build-hold/`, have its holder repeat `/build:hold hold <why>` or release it. For `~/.local/state/build-hold-release/current` or its `cycle.json`, `/build:hold release` sets the damaged record aside. For `~/.local/state/mac-test/block.json`, `run.json` or `claims/<repo>.json`, repair the file or remove the damaged record after checking its holder and any job still running; use `/build:mac block` or `unblock` for a block, or `mac_test.py release --pid <pid>` for a readable kept claim.

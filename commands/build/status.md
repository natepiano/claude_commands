---
description: Show build target status.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_targets/build_targets.py status` and show its lines unchanged. `$ARGUMENTS` accepts only `--probe`: add it to run `~/.claude/scripts/lib/py ~/.claude/scripts/build_targets/build_targets.py status --probe`. Without it, this command only reads coordination state. `/build:mac status` instead settles block state and can change it.

Use `/build:page` for waiting steps, their wait reasons, and running steps' promised and measured memory.

The first line is `MAC IDLE` when no natedev build or test runs on the Mac for the configured alert interval (default 3 minutes) while eligible steps wait. It is bold red in a terminal and plain when piped. The holder clears a block with `/build:mac unblock`. For an idle claim, resume build or test work or release it so waiting steps can take the Mac. For a free Mac, the builds showrunner checks why waiting steps do not take it. The line clears when work starts or no eligible steps wait. `MAC UNREACHABLE` appears immediately for a connection outage; restore access. `MAC IDLE WATCH` names unreadable state or a reading past its configured freshness limit (default 5 minutes); repair the named state or check `buildlog-sample.timer`. This line reads the saved result and local timing config.

- `free`: start work through its normal gate.
- `running`: let the named job finish. The Mac lists each repository claim with its job and start time. `/build:mac status` shows remote work that remains after its owner exits. Confirm that the remote work ended. Read the identity fields from the claim record. Run `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`.
  Claims and holds with a known holder session add ` (showrunner <name>, unit <name>)` after `since <time>`. Missing relationships show `—`; an unknown holder or failed lookup keeps the original line.
- `held`: wait for the named holder or release your own with `/build:hold release`. A Mac hold past expiry remains until its block file is removed; use `/build:mac unblock`, after `github-warmup` if CI restore failed.
- `unreachable`: run `/build:status --probe` to check the connection, then restore access before retrying Mac work.
- `state unreadable`: inspect the named path. Repair your natedev record with `/build:hold clear <why>`, or release it.
  `/build:hold release` sets a damaged release record aside.
  Check a damaged Mac claim's holder and running job before repairing its file.
  Use `/build:mac block` or `/build:mac unblock` for a Mac block.
  Release a readable claim with `mac_test.py release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`.

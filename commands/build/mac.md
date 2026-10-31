---
description: Hold the Mac, remove its block, settle and show its state, or audit Rust packages. Args - `block <why> [hours N]`, `unblock`, `status`, or `audit <repo dir>`.
---

`$ARGUMENTS` is `block <why> [hours N]`, `unblock`, `status`, or `audit <repo dir>`.

1. `block <why> [hours N]` runs `python3 ~/.claude/scripts/mac_test/mac_test.py block --holder '<your session name>' --for '<why>' [--hours N]`. A unit director also passes `--showrunner '<showrunner name>'`, which `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py name <production slug>` prints. The block is the Mac's hold; `/build:hold hold <why> target mac` places the same block with its default length. It admits the holder session's own claims, refuses other sessions' new Mac tests and builds, and turns off CI's Mac switch. This applies while pending and active. Existing local work or a CI Mac job finishes first; its free message arrives when that work ends. New CI runs skip macOS and stay green; re-run the listed commits after release. Repeat `block` to renew it.
2. `unblock` runs `python3 ~/.claude/scripts/mac_test/mac_test.py unblock --holder '<your session name>'`. It restores CI's Mac switch only if this block turned it off, then lists skipped runs by branch and their re-run commands. A block remains in force after expiry until its record is removed. If expiry cannot restore CI, run `github-warmup`, then `/build:mac unblock`; the failed restore keeps the block.
3. `status` runs `python3 ~/.claude/scripts/mac_test/mac_test.py status`. It settles block state and may lift an expired block, restore CI, or send pending messages. For a read-only view, use `/build:status`.
4. `audit <repo dir>` runs `python3 ~/.claude/scripts/mac_test/audit.py <repo dir>`. Its table shows compile-time gating: a gated test is not built on the Mac. `Not built on the Mac` names omitted lines.

Run `block`, `unblock`, and `status` with the sandbox off; they write state, call `gh`, and start the block watcher.

## State and recovery

- `free` with no block: start work through its normal claim. `running`: let the job finish; do not remove a live job's claim.
- A kept claim means the claiming process died but its remote work may remain. `status` prints `running: leftovers of <what> on <host> since <time>, kept under pid <pid>`. Confirm the remote work ended, then run `python3 ~/.claude/scripts/mac_test/mac_test.py release --pid <pid>` to clear it by hand. The offload runner checks and ends leftovers before releasing a taken-over claim.
- `blocked` or `block pending`: wait for the holder's release or free message; `unblock` removes your own block. If CI can still use the Mac, run `github-warmup`, then repeat `block`. An expiry with a failed CI restore needs `github-warmup`, then `unblock`.
- `unreachable`: use `/build:status --probe`; restore the connection before remote work.
- `state unreadable`: inspect the named `~/.local/state/mac-test/block.json`, `run.json` or `claims/<repo>.json`. Repair it, or remove that damaged file only after checking its holder and any job still running. Then repeat `block` or `unblock` for a block; use `release --pid <pid>` for a readable kept claim. A natedev holder or release record reported by `/build:status` is handled by `/build:hold`.

## Taking the Mac for a job of your own

Start the job on natedev. `verify.sh test` and `mac_run.sh` claim for themselves; do not claim before calling them. A test that must run on macOS uses `verify.sh test <package> --mac "<why>"`, without a separate claim.

Before the first remote command, run `python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what '<job>' --worktree <repo dir> --wait <seconds>`. The claim is that repository's (`--worktree` defaults to the current directory; outside any repository it claims the whole Mac): another repository's job can run beside it, a second job of the same repository cannot. Continue only after exit 0: 10 means blocked, 11 means busy, and 12 means state unreadable; use the recovery above. When done, run `python3 ~/.claude/scripts/mac_test/mac_test.py release --pid $$`, which removes every claim that pid holds.

A claim with nothing running on the Mac for 15 minutes is released and its holder is told, so claim right before remote work and claim again after a release.

Without `--remote`, the lock is tied to that process; its death frees it. A remote job uses `claim ... --remote <host>` and keeps its claim after the process dies. Exit 13 means takeover of a kept claim and prints `leftovers: <host>`: check those leftovers before starting work. A job started on the Mac cannot take the natedev lock.

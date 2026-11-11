---
description: Hold the Mac or audit Rust packages.
---

`$ARGUMENTS` is `block <why> [hours N]`, `unblock`, `status`, or `audit <repo dir>`.

1. `block <why> [hours N]` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py block --holder '<your session name>' --for '<why>' [--hours N]`. A unit director also passes `--showrunner '<showrunner name>'`, which `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py name <showrunner name>` prints. The block is the Mac's hold; `/build:hold hold <why> target mac` places the same block with its default length. It admits the holder session's own claims, refuses other sessions' new Mac tests and builds, and turns off CI's Mac switch. This applies while pending and active. Existing local work or a CI Mac job finishes first; its free message arrives when that work ends. New CI runs skip macOS and stay green; re-run the listed commits after release. Repeat `block` to renew it.
2. `unblock` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py unblock --holder '<your session name>'`. It restores CI's Mac switch only if this block turned it off, then lists skipped runs by branch and their re-run commands. A block remains in force after expiry until its record is removed. If expiry cannot restore CI, run `github-warmup`, then `/build:mac unblock`; the failed restore keeps the block.
3. `status` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py status`. It settles block state and may lift an expired block, restore CI, or send pending messages. For a read-only view, use `/build:status`.
4. `audit <repo dir>` runs `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/audit.py <repo dir>`. Its table shows compile-time gating: a gated test is not built on the Mac. `Not built on the Mac` names omitted lines.

Run `block`, `unblock`, and `status` with the sandbox off; they write state, call `gh`, and start the block watcher.

## State and recovery

- `free` with no block: start work through its normal claim. `running`: let the job finish; do not remove a live job's claim.
- A kept claim means the claiming process died, but its remote work can remain. `status` shows the host, time, and PID. Confirm that the remote work ended. Read `claim_id`, `episode`, and `attempt` from the claim record. Run `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`. The offload runner ends remote work before it releases a claim that it takes over.
- `blocked` or `block pending`: wait for the holder's release or free message; `unblock` removes your own block. If CI can still use the Mac, run `github-warmup`, then repeat `block`. An expiry with a failed CI restore needs `github-warmup`, then `unblock`.
- `unreachable`: use `/build:status --probe`; restore the connection before remote work.
- `state unreadable`: inspect the named `~/.local/state/mac-test/block.json`, `run.json`, or `claims/<repo>.json`. Check the holder and any running job before you repair or remove a damaged file. Repeat `block` or `unblock` for a block. Release a readable kept claim with `release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`. Use `/build:hold` for a natedev holder or release record.

## Taking the Mac for a job of your own

Start the job on natedev. `verify.sh test` and `mac_run.sh` create their own claims.
Use `verify.sh test [<package>] --mac "<why>"` for a test that must run on macOS. Do not create a separate claim.

Before the first remote command, run `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what '<job>' --worktree <repo dir> --wait <seconds> --identity`. It prints `claim_identity=` with the `claim_id`, `episode` and `attempt` of the claim. The claim is that repository's (`--worktree` defaults to the current directory; outside any repository it claims the whole Mac): another repository's job can run beside it, a second job of the same repository cannot. Continue only after exit 0: 10 means blocked, 11 means busy, and 12 means state unreadable; use the recovery above. When done, run `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py release --pid $$ --claim-id <claim_id> --episode <episode> --attempt <attempt>`, which removes only that claim.

A claim with nothing running on the Mac for 15 minutes is released and its holder is told, so claim right before remote work and claim again after a release.

Without `--remote`, the lock is tied to that process; its death frees it. A remote job uses `claim ... --remote <host>` and keeps its claim after the process dies. Exit 13 means takeover of a kept claim and prints `leftovers: <host>`: check those leftovers before starting work. A job started on the Mac cannot take the natedev lock.

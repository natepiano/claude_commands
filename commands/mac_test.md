---
description: Block Mac work, remove a block, report its state, or audit Rust packages. Args - `block <why> [hours N]`, `unblock`, `status`, or `audit <repo dir>`.
---

`$ARGUMENTS` is `block <why> [hours N]`, `unblock`, `status`, or `audit <repo dir>`.

1. `block <why> [hours N]` runs `python3 ~/.claude/scripts/mac_test/mac_test.py block --holder <your session name> --for '<why>' [--hours N]`. A unit director also passes `--showrunner <its showrunner's session name>`. A block stops new tests and builds on the Mac and turns off CI's Mac switch. New CI runs skip the Mac job and stay green; runs started under the block receive no macOS check. A local test or CI Mac job already running finishes first, and the "Mac is free" message arrives when it does. The block lifts by itself at the reported time. Run `block` again to renew it.
2. `unblock` runs `python3 ~/.claude/scripts/mac_test/mac_test.py unblock --holder <your session name>`.
   It turns CI's Mac switch back on only when this block turned it off, then lists each branch whose CI runs skipped the macOS job and the commit to run again.
3. `status` runs `python3 ~/.claude/scripts/mac_test/mac_test.py status`.
4. `audit <repo dir>` runs `python3 ~/.claude/scripts/mac_test/audit.py <repo dir>`. Read its table as compile-time gating: a gated test does not fail on the Mac; it is not built there. `Not built on the Mac` names every line omitted there.

Run `block`, `unblock`, and `status` with the sandbox off. They write under
`~/.local/state/mac-test`, call `gh`, and start a user service that watches the
block until it becomes active or expires.

## Taking the Mac for a job of your own

Use this section for a job that reaches the Mac over ssh without `verify.sh test` or `mac_run.sh`; both commands take the lock themselves, so claiming first and then calling either makes it report the Mac busy.

Start the job on natedev. Before its first ssh run, execute
`python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what "<what it is>" --wait <seconds>`.
Continue only after exit 0: 10 means the Mac is blocked, 11 means another run
has it, and 12 means the state is unreadable. When the job ends, execute
`python3 ~/.claude/scripts/mac_test/mac_test.py release --pid $$`.

The lock lives on natedev and is tied to that process, so a job that dies frees
it. A job started on the Mac cannot take it.

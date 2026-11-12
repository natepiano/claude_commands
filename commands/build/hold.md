---
description: Hold or release a build target.
---

`$ARGUMENTS` is `hold <why> [--drain] [eta HH:MM] [target <name>]`, `quiet`, `status`, or `release [target <name>]`.
The default target is natedev. Use the IANA zone for the stated ETA.

For a configured SSH target, use `build_hold.py hold --for '<why>' --target <name>`.
Release it with `build_hold.py release --target <name>`. The CLI also accepts the trailing form `target <name>`.
SSH targets refuse `--drain`, `--keep`, `--release-eta`, `--zone` and `release --resume`.
Holds affect only the selected target. Do not send a natedev hold notice for an SSH target.

1. On `hold`, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py hold --for '<why>'`. Your fixed staff ID owns the hold. Add `--keep <pid>` for each run that must continue; repeated holds add to this list, with each PID checked against its start time. The sole holder, kept runs and their children, the user’s own terminal or desktop runs, and shared sccache work may continue. Every other test and build stops at once with the holder and reason, including work already running. Its session gets one release notice; run stopped work again then. `verify.sh` itself continues so eligible tests can go to the Mac; its natedev test children must obey the hold. No run is paused: paused cargo keeps steve slots and cargo locks while wall-clock limits keep running. Pass `--release-eta HH:MM --zone <IANA zone>` only for a measured time later today. Send the printed notice with `/notify_top_level --here`; showrunners forward it. `No release after 2 h: ask me` is an escalation. For `target mac`, add `--target mac`; it runs `mac_test.py block`. Do not send the natedev notice for a Mac block. Mac refuses `--keep`, `--release-eta` and `--zone`.
2. Before a held natedev test, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py quiet`. It waits up to 10 minutes for remaining builds, including every user's `cargo`, `rustc` and `cargo-nextest` processes to end. Load does not delay it; success prints the 1-minute load and five heaviest processes by current CPU over about one second as the baseline. Run it in the background or under Monitor, never in a sleep loop. Exit 1 lists build counts by user: let those builds end, then retry. CI runners are not asked to stop. For the Mac, wait for the block's free message if its answer says work is still running; see `/build:mac`.
3. `status` runs `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py status`. Use `/build:status` for both targets. Before cargo outside `verify.sh`, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py wait`. It returns at once: 0 permits the build (no hold applies, or you are the sole holder); 10 refuses it and records your session for a release notice when known; 12 means the state cannot be read. Start builds only on 0. After 10, wait for release, then check again.
4. Release as soon as the work ends, pass or fail: run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py release`. With another holder in force, it prints `released; still held by ...`. The release removes its holder file first, then records the end and release fields; notices follow only when no holder file remains. A file that exists is always a hold, even if an old cycle says released. The enforcer exits when no hold remains. Every waiting build can then enter its memory gate, which orders the builds. Each recipient gets one notice to start builds; no answer is needed. On `No hold cycle: broadcast this release to every session.`, send the release with `/notify_top_level --here`. On `Not reached, tell them yourself: <session name> (<reason>)`, tell each named session; reasons are `session gone`, `send failed`, or `queued, delivery unconfirmed`. `release --resume` refuses while any holder file exists; use a normal release for that file. For `target mac`, add `--target mac`: it runs `unblock` and prints its answer; `--resume` is refused.

## Drain natedev for a service deploy

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py hold --drain --for '<purpose>'`.
The command applies the hold before it stops waiting runs.
It finds live `verify.sh`, `merge_gate.py`, and `targeted_test.py` runs.
A run works while it has a cargo child, an admission reservation, or a live Mac or AWS claim.
The hold keeps existing working runs and their children until they end.
Checks that permit work allow a caller that the hold keeps, including its descendants.
Status and report readers still show every active hold.
The drain classifies only the outermost gate in each process family.
Nested gates and shell children belong to that gate.
The drain stops each waiting run after it checks the process start time.
For a detached run, it also checks the supervisor command and record path before it sends TERM to the run group.
The drain resolves a relative record path against the supervisor's working directory.
The existing hold log records the stop request before TERM.
The drain also registers the owner for a release notice before TERM.
If a process identity changes, the drain cancels the stop record and removes the detached stop reason.
Pending and cancelled stop records do not count as completed stops.
The supervisor uses the merged record for delivery, so a cancelled stop reason does not reach the owner.
The detached result includes `stopped for <hold purpose>; run again after the release` and the exit code.
The release sends each stopped owner one notice to repeat the run.
A second release sends no second notice.

The command reports every remaining run with its owner and age, including runs that remain alive after TERM.
It prints a heartbeat each 60 seconds until no gate run remains.
Success prints `drained` and returns 0.
INT ends the drain wait and keeps the hold.
The deploy stop script retains its final refusal check.
A hold without `--drain` keeps its existing behavior.

## Build calls during a natedev hold

The Bash PreToolUse hook `scripts/hooks/pre-tool-use-build-hold-guard.sh` starts no Python when the hold directory has no entry or the input has neither `cargo` nor `verify.sh`. Otherwise it passes the input to `pre-tool-use-build-hold-guard.py`. The guard refuses building `cargo` calls and `verify.sh check`, `lint`, `build`, `doc` and `clippy` for a non-holder while any other session’s holder file exists. It uses `scripts/build_hold/hold_applies.py`, which shares `wait-local`'s hold and caller check. The refusal names only holders in force: `natedev builds are held by <holder>. Keep working, writes included. Run tests with verify.sh test: they go to the Mac. Run check, lint and builds after the hold ends.`

The sole holder may build. `verify.sh test` and non-building calls pass. The helper exits 0 with the refusal on stdout, 1 with no output when builds may run, or 2 with the read error on stderr; the hook allows the call on a read error.

## Target state and recovery

- `free`: start your work through its normal gate. `running`: let that job finish; a natedev hold stops tests and builds outside its allow rules, including work already running; a Mac block lets its running job finish.
- `held`: wait for that holder's release, or release your own hold. For a Mac block past its expiry, use `/build:mac unblock`; it stays held until the block file is removed.
- `unreachable`: use `/build:status --probe`; wait for a working connection before trying Mac work.
- `state unreadable`: inspect the named path. A natedev holder can repair its record with `hold --for <why>` or remove its hold with `release`. `/build:hold release` sets a damaged release record aside. For a damaged Mac claim file, check its holder and any running job before you repair or remove it. Use `/build:mac block` or `unblock` for a block. Release a readable kept claim with `mac_test.py release --pid <pid> --claim-id <claim_id> --episode <episode> --attempt <attempt>`.

## Installed-script refusals

- A set hold-directory variable prints `<script>: <NAME> is set to '<value>'; the installed scripts under <dir> do not run with it. Unset it and run again.` Unset that variable before retrying.
- A changed `HOME` prints the same line naming `HOME`. Run from a shell where `HOME` is the user's home.
- A read-only `HOME` prints `<script>: HOME is read-only in this shell, so the user's home cannot be read. Run again from a shell where HOME is not read-only.` Use a shell where `HOME` is the user's home and is not read-only.

# Phase checks

Read at the point of use from `/unit:direct`. Defines `<PhaseChecks/>` in full.

<PhaseChecks>
Each phase runs these checks once at the points named below:

| Check | Runs in | Over |
| --- | --- | --- |
| cargo mend `--fix` | the workers' last `verify.sh lint` | the workspace |
| fmt | the same `lint` | the changed members |
| taplo fmt `--check` | the same `lint` | the workspace TOML |
| clippy `--all-targets --all-features` | the same `lint` | the workspace |
| rustdoc `-D warnings` | `verify.sh doc`, run by the unit director as the last command before the checkpoint commit | the workspace |
| style review | the unit director's main review in <DualReview/>, reading `commands/unit/style_review.md` | the phase's diff from its baseline, untracked files included |

The four lint checks run wherever the phase's workers run `verify.sh lint`.
Test starts as soon as the code is ready, independently of lint.
<VerificationContract/> defines when each result counts.
A phase with no Rust change has no lint run; its style review runs only the
banned-words scan and marks the checklist `Skip (no Rust change)`.

`config/lint.conf` can switch off lint and style checks; it cannot switch off
the checkpoint rustdoc check, even in a phase with no Rust change in a Cargo
repository. An off check is reported `Off`, never clean; do not run a
replacement check.

Every Cargo checkpoint needs a pass for its exact tree. After staging, run
`verify.sh doc` as its own command immediately before the commit. In every
repository, put the checkpoint commit first in its Bash call; commands after
it are allowed. Use a literal or reused subject; an unreadable subject needs
the Cargo doc record too. See <CheckpointCommit/>
(`commands/unit/checkpoint.md`) for all checkpoint cases and message forms.
On doc failure, repair through <FixDispatch/>, stage, and run `doc` again.

Open every finding in the phase's findings ledger and send it to the phase's
workers through <FixDispatch/> repair rounds. After a repair changes the tree,
its worker runs `verify.sh lint` once more if the phase has a lint run. Never
rerun `lint` on an unchanged tree.

A `cargo mend --fix` rollback is a hard stop: stop and keep the tree for
inspection; do not continue checks or repairs past it.

After the last phase, do not repeat lint or style review. Each later Cargo
checkpoint still needs its rustdoc pass under <CheckpointCommit/>.
</PhaseChecks>

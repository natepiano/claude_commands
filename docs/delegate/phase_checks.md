# Phase checks

Read at the point of use from `/unit:direct`. Defines `<PhaseChecks/>` in full.

<PhaseChecks>
Each phase runs these checks once at the points named below:

| Check | Runs in | Over |
| --- | --- | --- |
| cargo mend `--fix` | the seats' last `verify.sh lint` | the workspace |
| fmt | the same `lint` | the changed members |
| taplo fmt `--check` | the same `lint` | the workspace TOML |
| clippy `--all-targets --all-features` | the same `lint` | the workspace |
| rustdoc `-D warnings` | the same `lint` | the changed members |
| style review | the unit director's main review in <DualReview/>, reading `commands/unit/style_review.md` | the phase's diff from its baseline, untracked files included |

The five lint checks run wherever the phase's seats run `verify.sh lint`.
A phase with no Rust change has no lint run; its style review runs only the
banned-words scan and marks the checklist `Skip (no Rust change)`.

`config/lint.conf` switches each check off as today. An off check is reported
`Off`, never clean; do not run a replacement check.

Open every finding in the phase's findings ledger and send it to the phase's
workers through <FixDispatch/> repair rounds. After a repair changes the tree,
its seat runs `verify.sh lint` once more if the phase has a lint run. Never
rerun `lint` on an unchanged tree.

A `cargo mend --fix` rollback is a hard stop: stop and keep the tree for
inspection; do not continue checks or repairs past it.

Nothing after the last phase repeats these checks.
</PhaseChecks>

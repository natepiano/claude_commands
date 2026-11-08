# Verification

<VerificationContract>
Rust workers run only exact prompt lines with this form:
`bash ~/.claude/scripts/direct/verify.sh <command> [<package>] [flags]`.
Omit the package for the whole workspace. Give at most one positional word after the command.

| Intent | Command |
| --- | --- |
| compile feedback | `bash ~/.claude/scripts/direct/verify.sh check [<package>] [--keep-going]` |
| package or workspace tests | `bash ~/.claude/scripts/direct/verify.sh test [<package>]` |
| one integration target alone | `bash ~/.claude/scripts/direct/verify.sh test [<package>] --test <test>` |
| only the tests whose name contains `<name>`, while iterating | `bash ~/.claude/scripts/direct/verify.sh test [<package>] --filter <name> [--filter <name> …]` |
| a test that cannot pass on macOS | `bash ~/.claude/scripts/direct/verify.sh test [<package>] --local "<why it cannot pass on macOS>"` |
| phase lint checks under <PhaseChecks/> | `bash ~/.claude/scripts/direct/verify.sh lint [<package>] [--check]` |
| staged workspace rustdoc | `bash ~/.claude/scripts/direct/verify.sh doc [<package>]` |
| example checks | `bash ~/.claude/scripts/direct/verify.sh example [<package>] [--name <name>]` |
| build a binary or example | `bash ~/.claude/scripts/direct/verify.sh build [<package>] [--bin <name> \| --example <name>]` |
| final gate | `bash ~/.claude/scripts/direct/verify.sh final [<package>]` |
| read a saved workspace gate pass | `bash ~/.claude/scripts/direct/verify.sh final --saved [--tree <tree>]` |

`check` and `build` keep Cargo's workspace selection, also with a named package.
A named package owns feature names without a slash. Without a package, feature
names pass unchanged. `doc` checks the whole staged workspace with the same
feature rule. Its pass record stays the same.

Without `--name`, `example` checks all examples in the selected package or workspace.
With `--name` and no package, the example's member supplies its required features.
`final` checks the whole workspace. A named package limits its nextest run and
prevents a saved pass. `--tree` requires `--saved`.

Use `--local` only for a test that cannot pass on macOS. A reservation reason
is refused. Use `/build:hold hold <why> target mac` for a Mac hold.
Before CI, either machine can answer a test, including merge gates.

Rules:

- Serialization and result authority follow <BuildTokenContract/>: every line
  here takes the `cargo` token on its own, so a run may wait for a peer, and a
  result is a gate only once the slot owning that package's files has posted
  `done`.
- `check` and `test --filter` are feedback while iterating, never a gate.
  Run `check` after each batch of edits, and `test --filter` once per finished
  change, naming all its tests in one call (repeat `--filter`). Never re-run
  either on a tree that has not changed. User, 2026-10-04: workers ran a
  `--filter` after every edit, 619 test runs in six hours.
- Start `test` for every modified package as soon as the code is ready. Never
  wait for lint to finish or for a natedev build hold to end before starting
  tests that can run on the Mac. Run `lint` once after the worker's last edit,
  naming any one modified package, in either order with `test`. Both must pass
  before the phase is done. `lint` rewrites the tree (mend fixes, then format),
  so a `test` run counts only when no later lint changed a file. Read
  `git diff --stat` before and after lint; when lint changed a file, run `test`
  once more on the resulting tree. Never re-run lint or test on an unchanged
  tree. Showrunner, 2026-10-28: tests that waited for lint never reached the
  free Mac while a natedev build hold held the lint.
  Lint is never per crate: one run covers the whole workspace (mend
  and clippy across it, format for every changed member; the unit director
  runs workspace rustdoc with `verify.sh doc` at the checkpoint). A failed
  lint means fix the error it names, wherever it is in the workspace, then lint
  once more. User, 2026-10-01: a Work Order that said "for each crate, lint
  first" cost 13 lint runs of one workspace, all failing on one error. Trace
  changed public APIs,
  traits, registration, and plugin wiring to modified callers. Name an integration target explicitly only to re-run it
  alone. Add example lines only when the phase owns them.
- Tests are the only testing: a passing `test` run proves the package builds.
  Never run `check` or any build alongside a `test` that is going to run anyway;
  `check` exists solely for mid-edit compile feedback.
- A launch is not a time to build or test: `verify.sh example` and any app or
  binary launch compile their own target. Never precede or follow a launch with
  a build, `check`, or `test` pass just to prove it builds.
- Phase prompts never use `final`, raw Cargo, or compose their own Cargo flags.
  Workspace lint coverage belongs to <PhaseChecks/>; <FinalGate/> owns the
  workspace all-targets check and full tests.
- Non-Rust prompts list the project's exact scoped commands under the same
  run-only-what-is-listed rule.
- Type checks are per slot. In a Python project, each slot's prompt gets its
  own line, `basedpyright <the Python files on its Workers line>`, which it
  runs after its last edit and before it posts `done`. Only the footer counts:
  it must read `0 errors, 0 warnings, 0 notes` (the exit status says nothing).
  A plan's folder-wide basedpyright line becomes these per-slot lines; never
  give one slot a run over files a peer owns. User, 2026-10-21: the impl
  worker got the phase-wide run, its 13 warnings were in the test worker's
  files, and the test worker had to be launched again.
- Prove any claimed pre-existing failure on the pre-phase tree before mentioning
  it. Do not stash; use the existing clean tree.
  Use `build_folders.py run --owner O --name N -- CMD...` for a build experiment.
- Re-run suspected environment failures unsandboxed before classifying them.
  Nested Swift `sandbox-exec` failures and missing GPU adapters are environment
  failures, not dependency or code defects.
- A printed `SKIPPED` is skipped, not passed; do not bypass a disabled check
  manually.
- `LINT_OP_STYLE_REVIEW=off` makes the phase style review report `Off`.
</VerificationContract>

<VerificationNarration>
Whenever choosing how a change gets verified — running tests, launching a
binary or example, or dispatching a worker that will — state the choice and
its reason to the user in one line: `Running tests only because …`,
`Launching <binary|example> because …`, or `Worker will run tests only
because …`. The line makes the verification economy visible; it never replaces
recording the result.
</VerificationNarration>

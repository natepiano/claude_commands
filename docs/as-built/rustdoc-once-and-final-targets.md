# Rustdoc at the checkpoint and an example-free final test build

## What it is

Rustdoc runs once per phase, as the unit director's last command before a Rust checkpoint commit. It does not run in every `verify.sh lint` call. A PreToolUse hook refuses a checkpoint commit in a Cargo repository unless a passing rustdoc run exists for the exact tree that the commit writes, so the check cannot be skipped. `lint.conf` cannot switch it off. `verify.sh final` type-checks every example once with `cargo check --all-targets`. Its nextest build no longer builds and links the examples again. A scan before any build refuses an example that holds a test or sets `test = true`, so no test is lost. The reason is fewer and shorter builds with the same guarantees: rustdoc used much worker time on each lint call and caught few errors, and the second example build in `final` added work but no checks.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/direct/verify.sh` | The `doc` verb, `doc_tree`, `doc_record`, `lint` without rustdoc, `final`, `UNTESTED_EXAMPLES_PY`, `TEST_TARGETS_PY`, and the header and usage text for each. |
| `scripts/lint/invoke.sh` | `invoke_doc`: `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --jobs ${LINT_DOC_JOBS:-8}`. |
| `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py` | The checkpoint doc gate. |
| `scripts/hooks/pre-tool-use-change-review-gate.py` | Supplies the `COMMIT`, `SUBJECT` and `ALL` patterns. The doc gate loads this file by path at run time. |
| `settings.json` | Registers the doc gate as a `Bash` PreToolUse hook, directly after the change-review gate. |
| `config/lint.conf` | `doc=on` still exists. It controls only a standalone lint-CLI doc run; `verify.sh doc` ignores it. |
| `commands/unit/checkpoint.md` (`<CheckpointCommit/>`) | The doc step, the own-call rule, and the rules the hook uses to recognize and refuse a commit. |
| `docs/direct/phase_checks.md` (`<PhaseChecks/>`) | Puts rustdoc in `verify.sh doc`, run by the unit director, not in the workers' `lint`. |
| `docs/direct/final_gate_commit.md` (`<FinalGateCommit/>`) | The doc step before the final-gate checkpoint commit. |
| `scripts/direct/test_verify_doc.py` | The doc record, the unchanged index, a failed run, an edit during the run, `doc=off`, and pruning. |
| `scripts/direct/test_verify_final_targets.py` | `final`'s target selection, a workspace with only a bin, the example refusals before any build, and a failed check stopping before nextest. |
| `scripts/direct/test_verify_untested_examples.py` | The example scan: test attributes, directory examples, `test = true`, refusal order, and that a recorded pass does not hide a new test. |
| `scripts/hooks/test_checkpoint_doc_gate.py` | The hook's recognition, tree, directory, message and fail-closed cases. |

### The `doc` verb

`verify.sh doc [--features <list>]` does these steps:

1. It reads the repo top (`git rev-parse --show-toplevel`) and the tree id (`doc_tree`).
2. If `lint.conf` has `doc=off`, it prints one line that says it ignores the switch.
3. It runs `LINT_CONFIG_FORCE=1 invoke_doc --workspace`, through the cargo token and the memory gate. Feature names pass through unchanged.
4. On exit 0, `doc_record` computes the tree id again. If the tree changed during the run, it prints a notice and writes no record.
5. Otherwise it writes `~/.local/state/verify/doc_pass/<tree_id>.json`.

The record has four keys: `repo_top` (resolved path), `tree_id`, `time` (UTC ISO 8601) and `call_id` (the build-log call id, `BUILDLOG_CALL_ID`). The write goes to a temporary file and is then renamed, with fsync on the file and the folder. Each write deletes records older than 14 days. The records are outside the worker session folder, so `end_session.sh` does not delete them. Workers can run `doc` too; their Work Orders do not ask for it.

### The tree id

`doc_tree` copies the real index to a private index in a temporary folder. It then runs `git add -A -- .` and `git write-tree` with `GIT_INDEX_FILE` set to that private index. The result is git's own tree id for every tracked file and every untracked file that is not ignored. A tracked file that is now ignored stays in the tree, because the copy starts from the real index. The real index does not change.

### `lint`

`lint` runs mend, fmt, taplo, clippy fix, fmt and clippy judgment. It does not run rustdoc. It takes `<package>` or `--workspace [--check]` and refuses all other arguments, `--features` included (usage, exit 2).

### The hook

The hook gets every Bash call. It decides in this order:

1. If the raw payload has no `commit` text, it exits before it imports anything.
2. If the tool is not Bash, the command is not a string, or no `git … commit` matches, it allows the call. This check does not use the shared parser, so a broken parser cannot refuse calls that do not commit. A malformed envelope is allowed.
3. It removes backslash-newline pairs, then all quotes and backslashes. If the result has `checkpoint(` anywhere, every commit in the call is a checkpoint.
4. For each `git … commit` match, it reads the message: literal `-m` text, `-F` files, the subject of a reused commit (`--amend`, `-C`, `-c`), and `MERGE_MSG` or `SQUASH_MSG`. A message that it can read and that has no `checkpoint(...):` subject line is not a checkpoint. `--fixup` and `--squash` are not checkpoints. A shell expansion or other text it cannot read makes the message unreadable, and an unreadable message counts as a checkpoint.
5. For a checkpoint, it refuses these calls in every repo:
   - `--git-dir`, `--work-tree`, `GIT_DIR=`, `GIT_INDEX_FILE=` or `GIT_WORK_TREE=` before the commit.
   - Any command before the commit in the same call.
   - A `git -C` directory it cannot resolve: a shell expansion, an unquoted glob, or a quoted or attached tilde.
6. It resolves the repo top. If the top has no `Cargo.toml`, it allows the commit. Outside git, it allows the commit unless the requested folder has a `Cargo.toml`.
7. In a Cargo repo, it refuses path arguments, `--`, `-o`, `--only`, `-i`, `--include`, `-p`, `--patch`, `--interactive` and `--pathspec*`. The commit must take the staged index, or tracked changes with `-a`.
8. It computes the commit's tree with a private index. Without `-a`, it uses the index as it is. With `-a`, it adds `git add -u` (tracked changes and deletions only).
9. It loads `doc_pass/<tree>.json`. The record must exist and be a JSON object. Its `repo_top` must equal the commit's repo top, and its `tree_id` must equal the tree. `time` and `call_id` must be strings.

A refusal is a `permissionDecision: deny` with a reason. The reason names the cause and the fix: run `verify.sh doc` as the last command before the commit, run the commit as its own Bash command, or write a literal subject. If untracked files exist, or unstaged changes without `-a`, the reason also says to commit or remove them. Those files are in the doc tree but not in the commit tree, so the trees do not match. Any error while it checks a Cargo checkpoint refuses the commit: the gate fails closed.

### `final`

`final` runs `cargo check --workspace --all-targets`, then `run_nextest --workspace --tests`. The check type-checks every example once. `--tests` selects each target with `test = true`. By default that is each lib, bin and integration test, and no example. `final` does not pass `--lib`, so a workspace with no library works.

### The example scan

`UNTESTED_EXAMPLES_PY` reads `cargo metadata --no-deps`. It runs before the cache lookup and before any build, for `final` (the whole workspace) and for `verify.sh test <package>` with or without `--features`. It does not run for `--filter` or for a named integration test. For each example target, it scans the source for a line that starts with `#[cfg(test)]` or `#[...test]` (with optional `path::` segments). For an example at `examples/<name>/main.rs`, it scans every `.rs` file in that folder. A commented attribute does not match. Each offender gets two lines on stderr, a reason and a fix:

- `example <name> holds a test: <path>:<line> has <attribute>, and examples carry no tests.` / `move it into <package>'s src/ or tests/, or delete it.`
- `example <name> sets test = true in <manifest>, and examples carry no tests.` / `remove test = true from its [[example]] entry.`

One example can get both refusals; the test refusal comes first. Offenders follow metadata order: package, then target. A blank line separates two offenders. Any offender gives exit 2.

`TEST_TARGETS_PY` picks the targets for `verify.sh test <package>`: `--lib` when the package has a lib, plus its bins and its test-enabled `test` and `bench` targets. It never selects examples. If a target needs features that are off, or another member has a target with the same kind and name, it falls back to `--bins --tests`, with `--lib` when the package has a lib.

## Invariants

- `verify.sh doc` cannot be switched off. It sets `LINT_CONFIG_FORCE=1`, and `lint.conf` `doc=off` only prints a notice.
- `doc` always covers the whole workspace. A change in one member can break the intra-doc links of another member.
- The doc record path is `~/.local/state/verify/doc_pass/<tree_id>.json`, and its keys are `repo_top`, `tree_id`, `time` and `call_id`. `verify.sh` and the hook must agree on both.
- `doc_tree` uses a private index and never changes the real index. It includes untracked, non-ignored files.
- A tree that changes during a `doc` run writes no record. A failed `doc` writes no record.
- `lint` does not run rustdoc and refuses `--features`.
- The hook fails closed for a Cargo checkpoint and fails open for every other call. Its first check must stay cheap: every Bash call reaches it.
- The hook loads `COMMIT`, `SUBJECT` and `ALL` from `pre-tool-use-change-review-gate.py`. A change to those patterns changes both hooks.
- `final` must not link examples, and the example scan must run before any build. Together they make sure no test is lost.
- `final` must not pass `--lib`.
- Tests that pin this: `test_verify_doc.py`, `test_verify_lint_scope.py`, `test_checkpoint_doc_gate.py`, `test_verify_final_targets.py` and `test_verify_untested_examples.py`.
- A repo with no `Cargo.toml`, such as `~/.claude`, needs no doc record, but its checkpoints still follow the own-call rule.

## Calibration / gotchas

- In every repo, the checkpoint commit is the first command in its own Bash call. An earlier command can change the folder or the index. Put `cd`, `git add` and `verify.sh doc` in earlier calls. Commands after the commit, such as a push, are allowed.
- After quote removal, any `checkpoint(` text in the call makes every commit in that call a checkpoint. A body line, an example or an `echo` counts. A heredoc that writes a file which mentions a `git commit` and checkpoint text is refused too; write such files with file tools.
- A shell expansion in a commit message, `$'…'` included, makes the message unreadable. In a Cargo repo an unreadable message counts as a checkpoint. Use literal `-m` text, a `-F` file, or the `"$(cat <<'EOF' … EOF)"` heredoc form.
- The change-review hook refuses Bash text that holds example checkpoint commit text. Write such text with file tools.
- Untracked or unstaged files make the doc tree differ from the commit tree. Commit or remove them before you run `doc`.
- A `doc` rerun after a plan-doc edit compiles nothing new and takes seconds.
- The memory gate can hold a doc step for minutes. A one-file crate waited 15 min for a 2.6 s rustdoc run.
- An example that sets `test = true` makes `final` and `test` refuse until the line is removed. hana main and bevy_hana still have two such lines (fairy_dust and hana_clerestory). hana_clerestory pads the `=`, so a search for `test = true` misses it; search with `test *= *true`.
- A hana build outside its checkout needs its flake shell (`direnv exec <hana checkout> …`). Without it, mold fails with "library not found: gbm".
- Measured on hana init/catalyst (83 examples in 27 packages, natedev): the `final` test build fell from 1,051 build units to 942, and its log held no example unit. Timed 2026-10-28 under a natedev build hold (nothing else compiling): after a touch of `hana_tool_schema` (21 of 27 members and 82 examples depend on it), the test build (`cargo nextest run --no-run`) rebuilt 49 units with examples and 35 without. Two alternated runs each: 124 s and 86 s before, 77 s and 63 s after, a mean of 105 s against 70 s, about 35 s (a third) saved per `final` test build. A first timing without a hold (load 180–220, about 900 s memory-gate waits) was noise.

## Why

Rustdoc ran in every `verify.sh lint` call. It used much worker time and caught few errors. One run per phase, at the checkpoint, keeps the check and removes the repeats. The user required that it be impossible to skip rustdoc in a phase, so a hook enforces it, and the record is keyed to the exact committed tree. An edit after the doc run, or a file left out of the commit, makes the record not match.

The hook is a Claude Code PreToolUse hook, not a git commit-msg hook. A commit-msg hook needs an install in each repo; the PreToolUse hook covers every repo from one `settings.json` entry.

Rustdoc runs on the whole workspace, not on changed members only. A change in one member can break the intra-doc links of another member.

In `final`, `cargo check --all-targets` already type-checks the examples, so linking them again in the test build adds no check. `--tests` alone selects the right targets. These options were ruled out:

- `--lib --bins --tests` fails with "no library targets found" (exit 101) in a workspace with no library.
- Exact per-target selection from metadata needs more code, and `--bins` still links bins that set `test = false`.

An example test that `--tests` does not select would be lost without a signal. The example scan refuses such examples before the build, so the smaller build loses no test.

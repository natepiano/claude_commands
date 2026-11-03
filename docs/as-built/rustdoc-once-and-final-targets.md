# Rustdoc at the checkpoint and an example-free final test build

## What it is

Rustdoc runs once per phase, as the unit director's last command before a Rust checkpoint commit. It does not run in every `verify.sh lint` call. A PreToolUse hook refuses a checkpoint commit in a Cargo repository unless a passing rustdoc run exists for the exact tree that the commit writes, so the check cannot be skipped. `lint.conf` cannot switch it off. `verify.sh final` type-checks every example once with `cargo check --all-targets`. Its nextest build no longer builds and links the examples again. A scan before any build refuses an example that holds a test or sets `test = true`, so no test is lost. The reason is fewer and shorter builds with the same guarantees: rustdoc used much worker time on each lint call and caught few errors, and the second example build in `final` added work but no checks.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/direct/verify.sh` | The `doc` verb and its staged-tree checkout, `doc_tree`, `doc_record`, `lint` without rustdoc, `final`, `UNTESTED_EXAMPLES_PY`, `TEST_TARGETS_PY`, and the header and usage text for each. |
| `scripts/lint/invoke.sh` | `invoke_doc`: `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --jobs ${LINT_DOC_JOBS:-8}`. |
| `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py` | The checkpoint doc gate. |
| `scripts/hooks/pre-tool-use-change-review-gate.py` | Supplies the `SUBJECT` and `ALL` patterns. The doc gate loads this file by path at run time, only after it finds a git commit or merge. |
| `settings.json` | Registers the doc gate as a `Bash` PreToolUse hook, directly after the change-review gate. |
| `config/lint.conf` | `doc=on` still exists. It controls only a standalone lint-CLI doc run; `verify.sh doc` ignores it. |
| `commands/unit/checkpoint.md` (`<CheckpointCommit/>`) | The doc step, the own-call rule, and the rules the hook uses to recognize and refuse a commit. |
| `docs/direct/phase_checks.md` (`<PhaseChecks/>`) | Puts rustdoc in `verify.sh doc`, run by the unit director, not in the workers' `lint`. |
| `docs/direct/final_gate_commit.md` (`<FinalGateCommit/>`) | The doc step before the final-gate checkpoint commit. |
| `scripts/direct/test_verify_doc.py` | The doc record for the staged tree, the unchanged index, a failed run, a staged edit during the run, an unstaged edit during the run, `doc=off`, and pruning. |
| `scripts/direct/test_verify_final_targets.py` | `final`'s target selection, a workspace with only a bin, the example refusals before any build, and a failed check stopping before nextest. |
| `scripts/direct/test_verify_untested_examples.py` | The example scan: test attributes, directory examples, `test = true`, refusal order, and that a recorded pass does not hide a new test. |
| `scripts/hooks/test_checkpoint_doc_gate.py` | The hook's recognition (command words, wrappers, quoted arguments, heredoc bodies), path selection, tree, directory, message and fail-closed cases. |

### The `doc` verb

`verify.sh doc [--features <list>]` does these steps:

1. It reads the repo top (`git rev-parse --show-toplevel`) and the staged tree id (`doc_tree`).
2. It resolves the original repo's target directory: `CARGO_TARGET_DIR` when set (relative to the repo top), otherwise `cargo metadata`'s `target_directory` at the repo top. It exports that absolute target for rustdoc and locks `<target>/verify-doc/<repo-path-hash>/lock` for the whole run. The stable `tree/` there has its own index. The first run checks out all staged files; later runs use `git read-tree -m -u <previous-tree> <new-tree>`, retaining unchanged mtimes and updating changed or deleted files. Untracked build outputs are cleaned from the source checkout. Missing metadata or a failed update triggers one fresh full checkout.
3. If `lint.conf` has `doc=off`, it prints one line that says it ignores the switch.
4. In `tree/`, it runs `LINT_CONFIG_FORCE=1 invoke_doc --workspace`, through the cargo token and the memory gate. Feature names pass through unchanged.
5. On exit 0, `doc_record` computes the staged tree id again. If the staged tree changed during the run, it prints a notice and writes no record.
6. Otherwise it writes `~/.local/state/verify/doc_pass/<tree_id>.json`.

At call start, `verify.sh` reads `[workspace.metadata.dev] features = [...]` once from the repo root `Cargo.toml`. It adds that list to check, test (filtered runs included), final, doc and build without `--release`. Work Order `--features` and a build example's required features are merged without duplicates; package feature names are qualified, and names that contain `/` keep their spelling. Doc keeps workspace feature names unchanged. Release builds and lint receive no dev features. A missing or empty list keeps the existing Cargo arguments; a value other than a list of strings refuses the step with one line.

The checkout and target survive exit, including a failed rustdoc run. Unchanged dependencies reuse artifacts in the repo's target directory; path-source package ids separate the checkout's workspace artifacts from the worktree's. The lock prevents concurrent runs for this repo from sharing its checkout. No checkout goes in tmpfs. Rustdoc never reads the working tree, so unstaged and untracked files do not change the result.

The record has four keys: `repo_top` (the resolved path of the original repo, not the checkout), `tree_id`, `time` (UTC ISO 8601) and `call_id` (the build-log call id, `BUILDLOG_CALL_ID`). The write goes to a temporary file and is then renamed, with fsync on the file and the folder. Each write deletes records older than 14 days. The records are outside the worker session folder, so `end_session.sh` does not delete them. Workers can run `doc` too; their Work Orders do not ask for it.

### The tree id

`doc_tree` copies the real index to a private index in a temporary folder. It then runs `git write-tree` with `GIT_INDEX_FILE` set to that private index. No `git add` runs. The result is git's tree id for the staged index: the tree that a plain `git commit` writes. Unstaged changes and untracked files are not in it. The copy keeps `write-tree` from updating the cache-tree data of the real index, so the real index does not change.

### `lint`

`lint` runs mend, fmt, taplo, clippy fix, fmt and clippy judgment. It does not run rustdoc. It takes `<package>` or `--workspace [--check]` and refuses all other arguments, `--features` included (usage, exit 2).

### The hook

The gate catches the commit forms an agent writes in ordinary use. It is not a shell interpreter and does not stop deliberate hiding, such as a commit inside a script file, an alias or a function defined in an earlier call. Where it cannot read a command that holds checkpoint text, it refuses.

The hook gets every Bash call. It decides in this order:

1. If the raw payload has none of `commit`, `merge`, `eval`, `$` or a backquote, it exits before it imports anything.
2. If the tool is not Bash or the command is not a string, it allows the call. A malformed envelope is allowed.
3. It splits the command into shell words and commands with its own parser. Quotes and escapes stay in each word. Comments and heredoc bodies are skipped. A new command starts at `;`, `&`, `&&`, `||`, `|`, `(`, `)`, a line break, `$(` and a backquote. Command positions also follow `then`, `do`, `else`, `elif`, `if`, `while`, `until`, `!`, `{`, closed groups, and case and function bodies. Compound checkpoints count as preceded commands. `time` is also a wrapper. A command inside `$( )` or backquotes counts as an earlier command.
4. In each command, it skips `VAR=value` assignments, redirections, and the wrappers `env`, `command`, `exec`, `sudo`, `nice` and `time` with their options. The next word must be `git` or a path that ends in `/git`. It skips git global options (`-C`, `-c`, `--git-dir`, `--work-tree`, `--no-pager` and others) up to the subcommand. Only `commit` and `merge` count. A word in the argument of another program, such as `echo`, is never a commit. If no command is a git commit or merge, it allows the call. The change-review parser is not loaded before this point, so a broken parser cannot refuse calls that do not commit.
5. Conditions (`if`/`then`/`elif`/`else`/`fi`), `while`/`until` loops, ordinary `for … in …; do … done` loops, case arms, groups, `!`, `time` and function bodies are modeled. Only command words inside them count: quoted example text is never checkpoint evidence. Opaque commands are `eval`, expanded command words, `sh`/`bash`/`zsh -c`, unmodeled `env` options, `coproc`, `select`, `[[ ]]`, `(( ))` and arithmetic `for`. Their own quote-removed words trigger refusal in every repo when they hold both `checkpoint(` and `git` with `commit` or `merge`: the target is unknown. Without checkpoint text, those git words trigger refusal only in a Cargo cwd. Unrelated opaque calls such as `$EDITOR file` and `eval "$(direnv hook zsh)"` pass. Text in another command never supplies fallback evidence. `env -C <dir>` and `env --chdir=<dir>` resolve the target directory before git's own `-C`; an unresolvable directory is unreadable.
6. For a commit, it reads literal `-m` text, `-F` files, reused subjects (`--amend`, `-C`, `-c`), and `MERGE_MSG` or `SQUASH_MSG`. A readable message is a checkpoint when a line matches `SUBJECT`; unreadable messages count only in Cargo repos. A merge with no message option uses git's ordinary default subject in every repo; `--log` does not supply a message. A literal merge message follows the checkpoint rules in every repo, including the own-call rule. A merge with `-e`/`--edit` or an unreadable `-F` message counts only in a Cargo repo. `--fixup` and `--squash` before `--` are not checkpoints. Commit and merge share a short-option parser: flags in a cluster are read in order until a value-taking letter consumes its attached remainder or the next word. `-S` and commit `-u` take optional attached values only. Thus `-amtext` means `-a` plus message `text`, and letters inside that message never become flags. Unknown short letters make the invocation unreadable under the same scope rules. Parsing options stops at `--`: later words are paths, even when named `--fixup` or `--squash`, and never bypass the tree or record check.
7. For a checkpoint, it refuses these calls in every repo:
   - `--git-dir` or `--work-tree`, or a `GIT_DIR=`, `GIT_INDEX_FILE=` or `GIT_WORK_TREE=` assignment before `git`.
   - Any command before the commit in the same call, on an earlier line, or inside `$( )`.
   - A `git -C` directory it cannot resolve: a shell expansion, an unquoted glob, or a quoted or attached tilde.
8. It resolves the repo top. If the top has no `Cargo.toml`, it allows the commit. Outside git, it allows the commit unless the requested folder has a `Cargo.toml`.
9. In a Cargo repo, it refuses a checkpoint merge, because the merge tree does not exist yet. The merge must finish first; then `doc` judges its staged tree and a commit writes the checkpoint.
10. Paths and selection options must be literal: no shell expansion, unquoted glob or brace, and no leading `~`. It refuses `-p`, `--patch`, `--interactive` and `--pathspec*`, because their tree is not known before the commit runs. It refuses `--only` with `--include`, `-a` with paths, `--only` or `--include`, and `--only` or `--include` without paths (except `--only --amend`).
11. It computes the commit's tree with a private index:
    - No selection: the index as it is.
    - `-a`: the index plus `git add -u` (tracked changes and deletions only).
    - `--only`, `-o` or paths: `HEAD` plus `git add` of the paths.
    - `--include` or `-i`: the index plus `git add` of the paths.
    - `--only --amend` with no paths: the tree of `HEAD`.
12. It loads `doc_pass/<tree>.json`. The record must exist and be a JSON object. Its `repo_top` must equal the commit's repo top, and its `tree_id` must equal the tree. `time` and `call_id` must be strings.

A refusal is a `permissionDecision: deny` with a reason. The reason names the cause and the fix: run `verify.sh doc` as the last command before the commit, run the checkpoint as the first command of its own Bash call, use a literal directory, path or git command, or write a literal subject. Any error while it parses a call that passed step 1, or while it checks a Cargo checkpoint, refuses the call: the gate fails closed.

### `final`

`final` runs `cargo check --workspace --all-targets`, then `run_nextest --workspace --tests`. The check type-checks every example once. `--tests` selects each target with `test = true`. By default that is each lib, bin and integration test, and no example. `final` does not pass `--lib`, so a workspace with no library works.

`final` always runs these commands, even when a prior pass exists. On exit 0,
it records a pass only when the checkout was clean at call start and remains
clean with the same Git tree at exit. Staged changes, unstaged changes and
untracked files prevent a record. A failed run or a changed tree prints one
line and writes no record.

The machine-wide record is `~/.local/state/verify/final_pass/<tree>.json`.
`VERIFY_STATE_DIR` changes the `verify` folder for private state. The record
has `tree`, `head`, `repo` (origin URL, or repo top when there is no origin),
`rustc` (`rustc -vV`), `verify_sha256`, `features` (the merged dev feature
list passed to Cargo), `host`, `status` (0) and `at` (UTC ISO time). The file
and folder are synced after an atomic write. Writes prune JSON records older
than 14 days. The record survives session cleanup and can be reused from a
second checkout of the same tree.

`verify.sh final-pass [<tree>]` reads this proof. Without a tree argument, it
requires the current clean HEAD tree. It prints the JSON record and exits 0
only when the tree, status, compiler text and verify.sh hash match. A missing,
unreadable or different record exits 1 with one reason. This query runs before
environment setup, uses no Cargo or build admission, and needs no unit session.
It never writes state or prunes records.

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
- `doc_tree` writes the staged tree from a private copy of the index. It runs no `git add` and never changes the real index. Unstaged and untracked files are not in the tree.
- `doc` runs rustdoc in a locked, stable staged-tree checkout under the original repo's target directory, never in the working tree or tmpfs. The record's `repo_top` is the original repo top. The private index and checkout persist; unchanged source mtimes and dependency artifacts are reused.
- A staged tree that changes during a `doc` run writes no record. An unstaged edit during the run does not stop the record. A failed `doc` writes no record.
- `lint` does not run rustdoc and refuses `--features`.
- The hook fails closed for Cargo checkpoints and opaque commands with git commit/merge words plus explicit checkpoint text in any cwd (or unreadable messages in a Cargo cwd). Modeled constructs never use quote-removed fallback evidence. Unrelated opaque calls and default merges pass. The first check stays cheap: every Bash call reaches it.
- Only a `git` command word starts a commit or merge. Text in the argument of another program, a comment or a heredoc body is never a commit.
- Every checkpoint in a Cargo repo needs a record for exactly the tree that its options write.
- The hook loads `SUBJECT` and `ALL` from `pre-tool-use-change-review-gate.py`. A change to those patterns changes both hooks. The hook finds git commands with its own shell parser, not with the change-review `COMMIT` pattern.
- `final` must not link examples, and the example scan must run before any build. Together they make sure no test is lost.
- `final` must not pass `--lib`.
- Tests that pin this: `test_verify_doc.py`, the `test_verify_lint_*.py` modules (shared fixture: `verify_lint_scope_test_support.py`), `test_checkpoint_doc_gate.py`, `test_verify_final_targets.py` and `test_verify_untested_examples.py`.
- A repo with no `Cargo.toml`, such as `~/.claude`, needs no doc record, but its checkpoints still follow the own-call rule.

## Calibration / gotchas

- In every repo, the checkpoint commit is the first command in its own Bash call. An earlier command can change the folder or the index. Put `cd`, `git add` and `verify.sh doc` in earlier calls. Commands after the commit, such as a push, are allowed. A line break also ends a command, so a command on an earlier line is an earlier command.
- Only the commit's own message makes it a checkpoint. Checkpoint text in an `echo`, in another command's quoted argument, or in a heredoc that writes a file does not make a commit.
- A shell expansion in a commit message, `$'…'` included, makes the message unreadable. In a Cargo repo an unreadable message counts as a checkpoint. Use literal `-m` text, a `-F` file, or the `"$(cat <<'EOF' … EOF)"` heredoc form. The hook cannot read the heredoc form, so it counts as a checkpoint.
- A merge with no message option uses git's ordinary default subject, even with `--log` or a stale `MERGE_MSG`. Literal checkpoint merge messages follow the own-call rule in every repo. A Cargo checkpoint merge must finish with `--no-commit`; then run `doc` and commit the staged tree with the checkpoint subject.
- Opaque explicit checkpoint commands are refused even from a non-Cargo cwd: `eval 'git -C <cargo repo> commit -m "checkpoint(x): p"'` cannot establish its target. Without checkpoint text, opaque git commit/merge words refuse only in a Cargo cwd. Modeled conditions, loops, groups, case arms and function bodies inspect command words, so quoted examples inside them pass.
- The change-review hook refuses Bash text that holds example checkpoint commit text. Write such text with file tools.
- `doc` judges only the staged tree. A plain `git commit` also leaves out unstaged and untracked files, so they need no action. With `-a`, `--only`, `--include` or paths, the commit writes working changes that `doc` did not judge. The record then matches only if those changes were staged before `doc`.
- `doc` always uses the original repo's target directory and a stable checkout below it. Later staged trees preserve unchanged source mtimes; the private checkout lock covers the whole doc run. Missing or damaged checkout metadata gets one fresh checkout, not a shared or temporary source tree.
- The memory gate can hold a doc step for minutes. A one-file crate waited 15 min for a 2.6 s rustdoc run.
- An example that sets `test = true` makes `final` and `test` refuse until the line is removed. hana main and bevy_hana still have two such lines (fairy_dust and hana_clerestory). hana_clerestory pads the `=`, so a search for `test = true` misses it; search with `test *= *true`.
- A hana build outside its checkout needs its flake shell (`direnv exec <hana checkout> …`). Without it, mold fails with "library not found: gbm".
- Measured on hana init/catalyst (83 examples in 27 packages, natedev): the `final` test build fell from 1,051 build units to 942, and its log held no example unit. Timed 2026-10-28 under a natedev build hold (nothing else compiling): after a touch of `hana_tool_schema` (21 of 27 members and 82 examples depend on it), the test build (`cargo nextest run --no-run`) rebuilt 49 units with examples and 35 without. Two alternated runs each: 124 s and 86 s before, 77 s and 63 s after, a mean of 105 s against 70 s, about 35 s (a third) saved per `final` test build. A first timing without a hold (load 180–220, about 900 s memory-gate waits) was noise.

## Why

Rustdoc ran in every `verify.sh lint` call. It used much worker time and caught few errors. One run per phase, at the checkpoint, keeps the check and removes the repeats. The user required that it be impossible to skip rustdoc in a phase, so a hook enforces it, and the record is keyed to the exact committed tree. Rustdoc runs on a checkout of the staged tree, so it judges what a plain commit writes and nothing else. A staged edit after the doc run makes the record not match. An unstaged or untracked file is in neither tree, so it cannot change the verdict.

The hook is a Claude Code PreToolUse hook, not a git commit-msg hook. A commit-msg hook needs an install in each repo; the PreToolUse hook covers every repo from one `settings.json` entry.

Rustdoc runs on the whole workspace, not on changed members only. A change in one member can break the intra-doc links of another member.

In `final`, `cargo check --all-targets` already type-checks the examples, so linking them again in the test build adds no check. `--tests` alone selects the right targets. These options were ruled out:

- `--lib --bins --tests` fails with "no library targets found" (exit 101) in a workspace with no library.
- Exact per-target selection from metadata needs more code, and `--bins` still links bins that set `test = false`.

An example test that `--tests` does not select would be lost without a signal. The example scan refuses such examples before the build, so the smaller build loses no test.

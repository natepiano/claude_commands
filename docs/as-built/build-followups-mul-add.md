# Fused multiply-add: lint fix

## What it is

`verify.sh lint` runs `cargo clippy --fix` before its clippy judgment. The fix pass applies every automatic fix that clippy and rustc offer at the crate's own lint levels. Multiply-adds are one of these fixes: `a * b + c` becomes `a.mul_add(b, c)`. This fix pass is the only multiply-add correction mechanism. No Claude or Codex edit runs a multiply-add hook.

The nightly Rust release check enables FMA for its Linux clippy build. Buildlog records a Codex worker's own thread as its session. The last hana measurement passed both limits.

## How it works

### Files and contracts

| File | Role |
| --- | --- |
| `scripts/direct/verify.sh` | Runs the clippy fix passes, formatting, cache rekey and the clippy judgment. |
| `scripts/lint/invoke.sh` | `invoke_clippy_fix` runs the fix pass with the judgment's arguments. |
| `scripts/buildlog/rust_release.py` | Sets the release trial's Linux clippy FMA flags. |
| `scripts/buildlog/record.py` | Supplies worker, worker session and agent session for step and call records. |
| `scripts/direct/test_verify_lint_scope.py`, `test_verify_lint_fixes.py`, `test_verify_lint_fix_limit.py`, `test_verify_lint_cache.py`, `test_verify_lint_edits.py`, `test_verify_lint_taplo.py`, `test_verify_lint_tool_failures.py` | Use the shared `verify_lint_scope_test_support.py` fixture and fake Cargo to check scope, fix order, the pass limit, cache keys and formatter failures. |
| `scripts/style/test_style_sync.py` | Checks the Cargo calls of a workspace lint, the fix pass and both formats included. |
| `scripts/buildlog/test_rust_release.py`, `test_record.py` | Check release flags and agent-session precedence with isolated inputs. |

### Edit checks

Claude's `Edit|MultiEdit|Write` PostToolUse group runs basedpyright and fn-length only. Codex `apply_patch` runs fn-length after the edit and berth before it. See [the function-length hook](build-followups-fn-length-hook.md#function-length-hook) for the edit parser, Rust scanner, Cargo lint reader and Codex installation rules.

### Runtime flags and attribution

The release check sets `CARGO_INCREMENTAL=0` for clippy. It sets `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"`. The build runs in the release trial's clone. Rustflags enter compilation hashes. A changed flag causes a rebuild on the next build that uses it.

`record.py` chooses `CODEX_THREAD_ID`, then `CLAUDE_CODE_SESSION_ID`, then no session. A Codex worker can inherit its director's Claude variable. This order selects the worker's own thread. Old rows keep their recorded identities. Comparisons by worker and session must not join intervals across an identity change.

### Lint-step fix

The normal `verify.sh lint` path runs mend, fmt and Taplo first. Then the clippy stage runs this loop, at most three times:

1. `invoke_clippy_fix --workspace --all-targets --all-features --keep-going`. This is `cargo clippy --fix --allow-dirty --allow-staged --allow-no-vcs <scope> -- -D warnings <extra clippy args>`, through `run`. It has the same scope, lint levels, gating and sandbox detection as the judgment.
2. `fmt_cargo "${FMT_SCOPE[@]}"`, which formats what the fix wrote.
3. A compare of `tree_key` before and after the pass. A pass that changes nothing ends the loop. So a clean tree costs one fix pass.

After the loop, lint recomputes `VERDICT_KEY` when it records a run. Then it runs the judgment, `cargo clippy --workspace --all-targets --all-features --keep-going -- -D warnings`. The judgment's status is lint's status. Rustdoc runs separately through `verify.sh doc` at the checkpoint. The `style-lint: clippy start` and `passed` lines stay.

The fix pass never sets lint's status. lint ignores its exit code. A tree with compile errors makes the fix pass fail, and the judgment reports those errors.

The loop repeats because one fix can expose a new fix. For example, a `mul_add` rewrite can make a function eligible for `const`. A failed judgment is cached under the fixed tree. With one pass only, every later call would hit that cache and never apply the second fix. If the third pass still changes the tree, lint sets `CLIPPY_FIX_LIMIT_REACHED`. Then `lint_failure_is_the_tree` does not cache a failed judgment, so the next call can fix more. A passing judgment at the limit is cached as usual. An empty `tree_key` never ends the loop early, because it cannot prove that the tree stopped changing.

`lint --workspace --check`, `example`, `check`, `test`, `final` and the release check never run a fix pass. Clippy off prints the existing single `SKIPPED` line and runs neither the fix nor the judgment. The cache key includes `verify.sh`, `invoke.sh` and the lint configuration.

A fix enters the calling worktree as an uncommitted edit. Buildlog records each fix pass as a clippy step with `--fix` in its argv.

## Invariants

- Keep `clippy::suboptimal_flops` enabled in Cargo.
- The fix pass uses the judgment's scope and lint levels. Source `#[allow]` attributes hold: the fix pass does not change code under them.
- The fix pass never sets lint's status. The judgment is unchanged and its status stands.
- Run at most three fix passes. Do not cache a failure after a third pass that changed the tree.
- `--check` and the other verbs never rewrite.
- Measurement uses the last judging step per call as its verdict. A judging step has no `--fix` in its argv.
- Tests use fake Cargo and temporary state. They never inspect a live hana tree. Python interfaces have no `Any` or file-level type ignores. Shell stays Bash 3.2 compatible.

## Calibration / gotchas

### FMA code generation

On natedev's Ryzen 9 9950X with Rust 1.99.0, default x86-64 has no FMA target feature. Scalar `mul_add` calls Rust's linked-in `compiler_builtins` `fmaf` or `fma`. Runtime dispatch uses hardware FMA on this CPU. Indirect calls and lost vectorization add cost. With `x86-64-v3`, scalar code emits `vfmadd213ss` or its f64 equivalent. Slices use packed `vfmadd` on `ymm`. `znver5` also emits `zmm` vectors. The Mac's aarch64 target emits scalar `fmadd` and vector `fmla` by default.

The fused slice improved 74.6× for f32 and 38.9× for f64 with v3. A dependent chain held in registers improved about 2.5× for both. A `black_box` inside each chain step adds a stack round trip that hides call cost. Fused outputs were bit-identical across a million fixed-seed triples for default, v3 and znver5. Plain `a * b + c` still uses separate multiply and add instructions. Replacing it with `mul_add` changes rounding.

Count FMA references per function outside `compiler_builtins`. Its runtime implementation makes a whole-binary instruction count nonzero even in a default build. GOT loads followed by register-indirect calls can evade a name-only count. Debug builds can call `<f32>::mul_add` out of line. Inspect the callee too. Hana debug binaries changed from seven `fmaf`/`fma` references in four functions and no other FMA instructions to no such references and 80 FMA instructions in ten functions.

### Fix limits

- **Scope of fixes.** The fix pass applies every machine-applicable fix that fires at the crate's levels, not only multiply-adds. A probe on a deny-level crate also removed an unused import and a needless `return`, and added `const` to a function. A `MaybeIncorrect` suggestion, for example one inside a macro body, stays unchanged.
- **Rollback.** When a target's fixes do not compile, cargo rolls back that target's fixes, keeps the other targets' fixes, and still exits 0. The judgment then reports what remains.
- **Pass limit.** Three changing passes can leave fixes for the judgment to report. The next lint call continues from the changed tree.
- **Measurement.** Read a call's full sequence before selecting its final judgment. Exclude steps with `--fix` in the argv. The index can lag the log after a process exits. Do not measure a call until its completion record exists.

## Why

FMA avoids fallback calls and permits vectorization. `x86-64-v3` enables AVX2 and FMA without selecting one CPU model. Explicit `mul_add` makes fused rounding visible. `cargo clippy --fix` uses clippy's own automatic suggestions at the crate's own levels, and the judgment checks the fixed tree. It replaced a JSON replay that applied only `suboptimal_flops` suggestions: the user asked for one fix step that also handles the other automatic fixes.

## Measured outcome

The limits are 1.979 target failures per 100 final clippy verdicts and 0.100 sole-cause worker-hours per day. The last hana measurement ran from 2026-10-17 16:59:51 to 2026-10-18 16:59:51 EDT, under the JSON replay route that the fix pass replaced. It found 147 final verdicts, 0 of them failed with `suboptimal_flops`: both limits PASS. Earlier measurements, with the edit hook or with one replay per call, were between 4.0 and 8.7 target failures per 100 verdicts.

The measurement scripts are in `~/.local/state/mul-add-hook/lint-rounds/`. Run `python3 -B synthetic_checks.py`, then `measure.py`, `commits.py` and `audit.py` from that folder. They read the buildlog index in read-only mode. Their judging-step query excludes `--message-format=json` replays. To measure the fix pass, it must also exclude steps with `--fix` in the argv.

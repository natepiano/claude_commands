# Fused multiply-add: runtime flags and Rust edit hooks

## What it is

A Rust PostToolUse hook in `~/.claude` tells Claude and Codex to rewrite locally provable scalar float multiply-adds as `mul_add`. It shares its payload parser, Rust scanner and Cargo lint reader with the function-length hook. Both hooks run after an edit and return the correction to the editing agent. The nightly Rust release check also enables FMA for its Linux clippy build, and buildlog records a Codex worker's own thread as its session. The hooks fire on both machines, but the measured hana lint failures still exceed their reduction targets.

## How it works

### Files and contracts

| File | Role |
| --- | --- |
| `scripts/hooks/mul_add_lib.py` | Scalar float proof, multiply-add discovery, rewrite text and `suboptimal_flops` scope. |
| `scripts/hooks/fn_length_lib.py` | Shared edit parser, Rust tokenization and delimiter pairing, Cargo lint reader, multiply-add target-root check, and function-length checker. |
| `scripts/hooks/post-tool-use-mul-add.py` | Reads the applied files, aggregates findings, logs them and returns one block. |
| `scripts/hooks/post-tool-use-fn-length.py` | Uses the same edit parser; reports every function over its package's limit. |
| `scripts/hooks/codex_hooks.py` | Installs, trusts and checks both Codex `apply_patch` handlers. |
| `settings.json` | Claude's `Edit\|MultiEdit\|Write` group: basedpyright, fn-length, then mul_add. |
| `scripts/buildlog/rust_release.py` | `trial_release` sets the clippy environment's FMA target flags. |
| `scripts/buildlog/record.py` | `who()` supplies worker, delegate session and agent session for step and call records. |
| `scripts/hooks/test_mul_add.py`, `test_fn_length.py`, `test_codex_hooks.py` | Synthetic packages, payloads, scope and exemption cases, error handling, scaling and a stub Codex app-server. |
| `scripts/buildlog/test_rust_release.py`, `test_record.py` | Pin the clippy flags and agent-session precedence with isolated fixtures. |

The public entry points keep checking results separate from the reason a file can be checked:

```python
applied_rust_edits(raw: str) -> AppliedRustEdits | IgnoredEdit
applied_patch_rust_files(patch: str) -> tuple[str, ...]
read_clippy_lint(rs_file: os.PathLike[str] | str, lint: str,
                group: str) -> ConfiguredClippyLint | UnavailableClippyLint
suboptimal_flops_scope(rs_file: os.PathLike[str] | str) -> SuboptimalFlopsLintScope
float_mul_add_findings(rs_file: os.PathLike[str] | str
                       ) -> tuple[SuboptimalFlopsLintScope, list[FloatMulAddFinding]]
long_functions(rs_file: os.PathLike[str] | str
               ) -> tuple[TooManyLinesLintScope, list[FunctionLength]]
```

- `AppliedRustEdits(agent, tool, cwd, files)` means one successful edit left Rust files to inspect, in payload order. `IgnoredEdit` means there is no supported Rust edit to inspect.
- `ConfiguredClippyLint(level, package_dir)` carries a discovered package's effective lint level. `ClippyLintLevel` distinguishes `ALLOW`, `EXPECT`, `WARN`, `DENY`, `FORBID` and `UNSET`; `UnavailableClippyLint` distinguishes `NO_PACKAGE` from `UNREADABLE`.
- `SuboptimalFlopsLintScope.state` uses the existing `ScopeState` enum: `ENABLED`, `DISABLED`, `EXEMPT` or `NO_PACKAGE`. `EXEMPT` covers a potentially no_std target or an uncertain crate root. Only `ENABLED` scans expressions.
- `FloatMulAddFinding(line, a, b, c, expr, rewrite)` carries the source location, product operands, addend, original span and proposed replacement through read-only properties. `TooManyLinesLintScope(enabled, threshold)` carries the long-function limit; `FunctionLength(name, line, lines, exempt)` carries a measured function and its exemption.
- `RustToken(text, start, end)` retains source offsets for the shared scanner. These small scanner and finding types use `__slots__`; importing dataclasses on every edit would consume too much of the hook's budget.

### Edit → scan → correction

1. **Claude `Write`/`Edit`/`MultiEdit`.** The hook reads JSON on stdin, takes `tool_input.file_path` and `cwd`, and tags the edit `agent="claude"`. Both entries return before importing the shared parser when the raw text contains neither `.rs` nor a Unicode escape.
2. **Codex `apply_patch`.** The same parser reads patch text from `tool_input.command`. Add and Update headers supply files; Move replaces an Update path with its destination; Delete is skipped. Paths are deduplicated, filtered to `.rs`, resolved against `realpath(cwd)` and tagged `agent="codex"`.
3. **Check final contents.** Each existing affected file is scanned in full. The multiply-add entry first skips files without `*` or without both an additive operator and a product. Findings stay in file and source order. The fn-length entry calls `long_functions`; its complete counting rules are in [the function-length hook](build-followups-fn-length-hook.md).
4. **Return one block per hook.** All files are scanned before any block record is appended. The JSON has `decision: "block"`, `continue: true`, a `reason` ending `The edit was applied.`, a short `systemMessage`, and `hookSpecificOutput.additionalContext`. A multiply-add reason reads `<file>:<line>: write <rewrite> for <expr> (clippy::suboptimal_flops)`; a long-function reason names the function, count and limit, then says `split it now`. The edit remains on disk.

The multiply-add log is `blocks.jsonl` under `MUL_ADD_HOOK_STATE`, else `~/.local/state/mul-add-hook`. It writes one record per finding: `at`, `agent`, `tool`, `cwd`, resolved absolute `file`, `line`, `expression`. The fn-length log uses `FN_LENGTH_HOOK_STATE`, else `~/.local/state/fn-length-hook`, with one record per affected file and a `threshold` plus `functions: [{name, line, lines}]`. Log timestamps are UTC; append failures are silent and do not change the block.

### Scope and Codex registration

`read_clippy_lint` finds the nearest package manifest and follows `lints.workspace = true`, including an explicit `package.workspace` path. An explicit `suboptimal_flops` entry wins over `nursery`; `too_many_lines` wins over `pedantic`. String levels and `{level = …}` entries are accepted. Warn, deny and forbid enable checking; an absent or unreadable configuration passes. The function-length threshold comes from `too-many-lines-threshold` in an ancestor `clippy.toml` or `.clippy.toml`, default 100; an invalid or negative threshold disables it.

For multiply-add, `target_may_be_no_std` checks the edited target's root. A std `src/main.rs` is checked beside a no_std library; binary, test, example and bench targets use their own roots. Ordinary library modules inspect available `lib.rs`/`main.rs` roots and pass if either may be no_std. A missing root, `build.rs`, or a file outside the supported target directories passes as exempt. This root exemption belongs to multiply-add; the function-length checker shares the lint reader and scanner but does not call it.

`codex_hooks.py install|check` manages two `CodexHook` handlers, fn-length and mul_add, with matcher `apply_patch` and timeout 10 seconds. Each command string equals its Claude registration. `LocatedHook` carries the app-server's key, current hash, trust status and enabled state; `MissingHook` identifies an absent handler.

`install` atomically appends missing groups to `CODEX_HOME/hooks.json` (default `~/.codex`), preserving existing indexes and trust. Through `codex app-server`, it calls `initialize`, `hooks/list`, then `config/batchWrite` to upsert each untrusted or modified handler's `hooks.state` trusted hash and lists again. A trusted, enabled duplicate wins. `check` lists without writing. Both return 0 only when both handlers are trusted and enabled; otherwise they name `absent`, `untrusted`, `modified` or `disabled`, or the file/RPC error. The app-server conversation has a 30-second deadline and its child is stopped on exit; binary and home overrides resolve before it changes directory.

### Runtime flags and attribution

The release check's clippy environment sets `CARGO_INCREMENTAL=0` and `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"`. Its build takes place in the release trial's clone. Rustflags enter compilation hashes, so an existing target rebuilds organically on its next build after a flag change.

`record.py` chooses `CODEX_THREAD_ID`, then `CLAUDE_CODE_SESSION_ID`, then no session. A Codex worker inherits its director's Claude variable, so this precedence identifies the worker itself. Claude attribution and `caller` are unchanged; existing rows retain their recorded identities. Comparisons grouped by worker/session must keep records from either side of an attribution change separate.

## Invariants

- Keep `suboptimal_flops` enabled in Cargo. The hook supplements clippy with immediate feedback and honors its local exemptions.
- Unknown payloads, missing/unreadable configuration and internal errors pass the edit. A multiply-add scan with unbalanced delimiters passes with at most one `mul_add hook error: <type>: <message>` system message and exit 0. The fn-length checker skips unclosed bodies; complete earlier functions remain measurable.
- One parser and scanner serve both agents. Scan every affected file before logging; a later file's scan error must leave no records for an undelivered block.
- A block record proves a finding, not delivery: it is appended before the block prints. Delivery needs the agent's block text as well, and a missing record cannot prove the hook did not fire because logging can fail silently.
- Tests isolate hook state, Codex home and buildlog state, substitute a Codex stub, and never invoke real cargo or inspect a live hana working tree. Python interfaces stay typed, with no `Any` or file-level type ignores.

## Calibration / gotchas

### FMA code generation

On natedev's Ryzen 9 9950X with Rust 1.99.0, default x86-64 has no FMA target feature. Scalar `mul_add` calls Rust's linked-in `compiler_builtins` `fmaf`/`fma`, whose runtime dispatch uses hardware FMA on this CPU; the overhead is indirect calls and lost vectorization. With `x86-64-v3`, scalar code emits `vfmadd213ss`/the f64 equivalent, and slices use packed `vfmadd` on `ymm`. `znver5` also emits `zmm` vectors. Plain `a * b + c` remains separate multiply and add instructions. The Mac's aarch64 target emits scalar `fmadd` and vector `fmla` by default.

The fused slice improved 74.6× for f32 and 38.9× for f64 with v3; a register-resident dependent chain improved about 2.5× for both. A `black_box` inside every chain iteration introduces a stack round trip that hides the call overhead. Default, v3 and znver5 fused outputs were bit-identical across a million fixed-seed triples of each type; this says nothing about replacing separately rounded plain arithmetic with fused arithmetic, or libraries whose intrinsic selection changes with target features.

Count FMA references per function outside compiler_builtins. Its own runtime FMA implementation makes a whole-binary instruction count nonzero even in a default build, and GOT loads followed by register-indirect calls evade a name-only call count. Debug builds may still call `<f32>::mul_add` out of line: the proof is in the callee. Real hana debug binaries changed from 7 `fmaf`/`fma` references in 4 functions and no FMA instructions outside compiler_builtins to no such references and 80 FMA instructions in 10 functions.

### Detector proof and exemptions

- **Shapes.** `a * b + c`, `c + a * b`, both subtraction orders, `x += a * b` and `x -= a * b`. Subtraction negates the receiver or addend as needed; compound assignment becomes an assignment. Additive chains retain the applicable full span; unary minus is preserved.
- **Proof.** A float literal in a product is enough only when its other operand is compatible. Non-float annotations, references, iterator bindings, pattern/closure bindings, recognized vector values and non-float constants veto it. Without a literal, both product operands need locally known scalar float types or casts. A field chain's last field must be declared `f32`/`f64` in a struct or enum in the same file; without a literal, that field name must also be absent from non-float declarations. The addend's fields face the same scalar-field check.
- **Bindings.** An ordinary `let x = if … else …` is not a let-else pattern. File `const`/`static` names typed exactly `f32`/`f64` supply scalar proof unless a local binding shadows them. A non-float file declaration wins a same-name collision; a function-local constant affects its own function.
- **Exemptions.** Const items, `const fn`, const blocks and static/const initializers; macro definitions and invocations; a parenthesized sum used as a `.sqrt()` receiver; and `allow`/`expect`, including `cfg_attr`, for the lint or `nursery` on the item, enclosing item or file. Called/indexed operands and unsupported field/path continuations are skipped whole, never shortened into a different expression.
- **Literal receiver.** `2.0 * v + c` becomes `v.mul_add(2.0, c)` because an unsuffixed literal receiver can fail with E0689. Two unsuffixed product literals are skipped.
- **Known gaps.** A let-else initializer containing a nested semicolon can evade the pattern exclusion. `#![cfg_attr(c, no_std, allow(x))]` is not recognized as no_std when no_std is not the last argument. The scanner has no cross-file type inference or macro expansion; shell edits and later rustfmt growth bypass the edit hooks.
- **Recall.** The detector caught 37 of 291 historical positions in stubs with a crate root, and 17 of 18 oracle diagnostics with no unmatched findings. Those stubs omit declarations that can supply types or veto a finding, so their recall does not establish reach in a real file. A standalone copied file or a fixture without `src/lib.rs`/`main.rs` can be exempt regardless of its expression.
- **Cost and scaling.** Rust edit overhead is about 14 ms CPU over bare Python; non-Rust entries avoid scanner/typing imports. `test_many_findings_scan_scales_with_file_length` takes the minimum of five wall-time samples at each size, 200 and 800 repeated finding lines. Every sample verifies the finding count and last line; the larger scan must take less than 10× the smaller. Linear growth is about 4×, quadratic about 16×, and minima reduce scheduling noise under load.

## Why

FMA improves runtime by eliminating the fallback calls and allowing vectorization. `x86-64-v3` enables AVX2/FMA with a portable target rather than tying binaries to one CPU. Explicit `mul_add` keeps fused rounding deliberate. The edit hook moves a repair into the editing turn, while local proof and exemptions limit false blocks. Sharing the parser and scanner keeps Claude and Codex behavior aligned; lazy imports and small classes keep a hook paid on every edit affordable. Separate logs retain the evidence needed to distinguish a hook that fired from an expression it could not reach.

## Measured outcome

**Detector reach, live check.** The wider detector, which reports every likely multiply-add on an edited line with a conditional message, reached the live natedev hooks at **T_reach = 2026-10-12 11:13:42 PDT (America/Los_Angeles, UTC-07:00)**, the committer time of merge `b22c1218`. In separate scratch crates outside every repository, Codex `apply_patch` and Claude `Edit` each received one block when adding `let centre_row = top + metrics.button.height * 0.5;`. Both blocks read, with the scratch path normalized: `<file>:3: if top + metrics.button.height * 0.5 is f32 or f64 arithmetic, write metrics.button.height.mul_add(0.5, top) (clippy::suboptimal_flops). Leave integer and vector arithmetic as it is. The edit was applied.` A second `apply_patch` changed only `let top` and received no block; Claude's second `Edit` did the same and received no block. The live `blocks.jsonl` contains one matching record per block, tagged `codex` and `claude`. `python3 scripts/hooks/codex_hooks.py check` exited 0 and reported both handlers trusted.

`cost.py`, `loop.py`, `clippy_fail_lints.py` and `tml_lengths.py` cover the same 96-hour window, **2026-10-06 22:44:05 to 2026-10-10 22:44:05 PDT**, containing **763 hana clippy steps, 250 failed**.

| Measure | `suboptimal_flops` baseline | Current | `too_many_lines` baseline | Current |
| --- | ---: | ---: | ---: | ---: |
| Failed steps per 100 | 7.916 | **8.126** (62 steps); ≤1.979: **FAIL** | 13.3 | **3.539** (27 steps); ≤3.3: **FAIL** |
| Sole-cause failed steps per 100 | 2.573 | 1.573 (12 steps) | 4.9 | 0.393 (3 steps) |
| Sole-cause worker-hours per day | 0.402 | **0.576**; ≤0.100: **FAIL** | 1.07 | **0.116**; ≤0.27: **PASS** |
| Claude/Codex controls on natedev/Mac | — | **4/4 PASS** | — | **4/4 PASS** |
| Overall verdict | — | **FAIL** | — | **FAIL** |

Neither lint meets the required 75% reduction in both failure rate and worker time. All eight controls have the delivered block text and a matching hook log record, proving that both hooks fire for both agents on both machines.

For multiply-add, **0 of 159 diagnostics** match an earlier block on the same file, line and expression; **145** have no earlier block in that file at all. The 40 diagnostics with matching committed source yield no detector findings (33 enabled, 7 exempt); the remaining 119 snippet-only cases yield four findings in three failed steps. Confirmed real-file reach is **0/763**; snippet-only reach is **3/763 = 0.393 per 100**, with actual reach unresolved where file declarations are missing. For long functions, **5 of 27** diagnostics match an earlier block naming that function; **22** remain unknown.

**Hana's CI on `origin/main` still lacks `-C target-cpu=x86-64-v3`.** Its Linux flags contain only the mold linker flag; the nightly release check already includes both mold and v3.

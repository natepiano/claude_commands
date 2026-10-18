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

### Lint-step rewrite

Only `verify.sh lint` repairs multiply-adds. After mend, formatting and Taplo, it runs the normal workspace clippy judgment (`--workspace --all-targets --all-features`, with `-D warnings`) while capturing its output. A pass continues to rustdoc. A failure without the documentation link `#suboptimal_flops` returns clippy's status; matching the link accepts both diagnostic wordings.

On a matching failure, `invoke_clippy_mul_add_json` in `scripts/lint/invoke.sh` invokes the judging entry point with one additional Cargo argument:

```sh
cargo clippy --workspace --all-targets --all-features --message-format=json -- -D warnings
```

This step runs through `run`, preserving its argv and call ID in buildlog. Its stdout is captured as JSON. Both `verify.sh`'s `run` wrapper and `invoke.sh`'s `run_once` preserve stdout and stderr separately for this helper: the command banner and Cargo stderr reach stderr, and the retained logs include both streams. The stderr logger reads a temporary FIFO as an ordinary background process, which is joined before a caller reads or removes its log; this uses Bash 3.2 constructs. Stdout streams to the caller at once and is also saved to a temporary file, appended to the log only after stderr has drained: two loggers appending to one file interleaved chunks that are not line-aligned, so a JSON record could land inside a stderr line (seen at load 100–400). The log therefore holds stderr first, then stdout. Both output paths return the command's own status even if a logger fails. Other invocations keep their existing output path. An exit of 101 is expected when `-D warnings` reports the original lint again; exits other than 0 or 101, a missing Cargo `build-finished` record, or malformed JSON fail the rewrite and retain the first judgment's status.

`scripts/lint/apply_mul_add.py --workspace-root <root> <json file>` selects only `compiler-message` records with code `clippy::suboptimal_flops`. From their spans and direct children's spans, it takes only `MachineApplicable` suggestions with a replacement. Resolved paths must remain inside the workspace root, including through symlinks. Exact file/range/replacement duplicates count once; overlapping spans keep the first diagnostic's span and count later conflicts as skipped. Replacements operate on byte offsets from last to first, preserving multibyte UTF-8 before a span. The applier validates the complete stream, requiring a Cargo `build-finished` record before editing. It exits 0 after changes, 3 (`NOTHING_APPLIED`, clear of argparse's usage status 2) without changes, and 1 on an incomplete stream or a read/write failure. The shell branches on this status, without parsing the report text. All files and ranges are read and checked before writing; each changed file is replaced through a temporary file so a failed write leaves that file intact. Another file may already have been changed when a later file cannot be written; lint fails and does not record that tree as a pass.

The applier prints `verify.sh: rewrote <n> multiply-add(s) in <m> file(s)`, with `(<k> skipped: overlapping)` when needed. A successful nonempty rewrite formats `FMT_SCOPE` again, recomputes the cache's `VERDICT_KEY` when recording a run, and invokes the normal judging clippy once more. That judgment's status is authoritative. Zero rewrites print `verify.sh: clippy's multiply-add rewrite changed nothing` and retain the first judgment's status. Clippy off produces the existing single `SKIPPED` line. `example`, `check`, `test` and `final` keep their existing paths. The cache key includes the applier as well as `verify.sh`, `invoke.sh` and lint configuration.

The JSON route follows three unsuccessful `cargo clippy --fix` probes: allowing warnings and the five Clippy groups, then enabling the target with `-W`, with `-D`, or with `-D` plus `--cap-lints=deny`. Each command exited 0 without a `Fixed` line or a source change, despite those flags appearing in Clippy's dep-info. JSON selection removes reliance on lint-level combinations: unrelated suggestions are never applied, and source-level `#[allow(clippy::suboptimal_flops)]` has already removed the target diagnostic.

The isolated probe uses a package denying `clippy::nursery`, with an ordinary `f64` multiply-add, an allowed multiply-add, an unused import and a machine-applicable `useless_format` case. Its helper command, source comparison and count check are kept in `~/.local/state/mul-add-hook/lint-rewrite/probe-2026-10-16/` (`probe.sh`, `inspect_json.py`, `check_probe.py`); the crate and generated build output were deleted after inspection. The JSON preflight emitted the target's `MachineApplicable` suggestion and separate diagnostics for both other lints. Both judging and JSON commands exited 101 under `-D warnings`. The full probe changed only the ordinary expression, from `a * b + c` to `a.mul_add(b, c)`, and printed `verify.sh: rewrote 1 multiply-add(s) in 1 file(s)`. Duplicate lib/test suggestions counted once; the allowed expression, unused import and `useless_format` source stayed unchanged. The source diff is kept there as `probe.diff`. The probe through `invoke.sh`'s `run` function also passed the exact source and count checks; the separate `verify.sh` wrapper is exercised by the fake-Cargo lint-scope suite.

The measurement marker is `--message-format=json` in the replay step's argv; judging steps omit it. `verify.sh` saves the successful applier's count line as `<call_id>.log` in `~/.local/state/mul-add-hook/lint-rewrite/`. `MUL_ADD_REWRITE_LOG_DIR` overrides that location for both writer and watcher; otherwise the writer also honors `MUL_ADD_HOOK_STATE`. Retention errors print a notice and do not fail lint. With `BUILDLOG_OFF=1`, no extra log is saved.

The external `~/.local/state/mul-add-hook/lint-rewrite/watch.py` reads Git and the SQLite index without changing either. `first-fire <checkpoint>` returns 1 until the checkpoint is in live `~/.claude` `HEAD`; it derives `T_lint` from the first containing commit on that history's first-parent chain. It then finds a hana `verify.sh lint` call starting at or after `T_lint`, with a completed JSON step (status 0 or 101) followed by a passing judgment in the same call, and returns 0 with the call ID, start in America/New_York and UTC, and the retained rewrite count. Missing retained output is reported as unavailable; an unreadable index returns 3. `window <checkpoint> <hours>` prints `T_lint` and the due time in both zones, returning 0 at or after the due time and 1 before it.

The first passing live rewrite was hana lint call `v20261017T014232-1432240-10080`, started 2026-10-16 21:42:32.409 EDT (America/New_York; 2026-10-17T01:42:32.409Z). Its initial clippy judgment failed with `#suboptimal_flops` (status 101); the JSON replay supplied the machine-applicable suggestion, and the saved count reads `verify.sh: rewrote 1 multiply-add(s) in 1 file(s)`. Formatting, the final clippy judgment and the lint call passed (status 0). The diagnostic identified `crates/hana_diegetic/src/layout/element.rs:2511`, replacing `field.text_style.size() - 3.0 * millimeters_to_points` with `3.0f32.mul_add(-millimeters_to_points, field.text_style.size())`. At 23:28 EDT on 2026-10-16 the rewrite sat uncommitted in the `widget-enhancements` worktree (`~/rust/widget-enhancements`, line 2511), whose branch still pointed at the call's starting commit `6be388c2976fe57e2168043c8b5835d49b6aee77`; the rewrite lands in the tree, and the agent that ran lint commits it with its own work.

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

**Rewrite rounds, re-measure.** The window starts at **T_rounds = 2026-10-17 16:59:51 EDT (2026-10-17T20:59:51Z)**. It ends at **2026-10-18 16:59:51 EDT (2026-10-18T20:59:51Z)**. These times use America/New_York, UTC-04:00. The start is included. The end is excluded. The span is 24 hours.

| Measure | Rewrite rounds, re-measure | Limit / verdict | Source |
| --- | --- | --- | --- |
| Final clippy verdicts | **147 total; 42 failed; 0 failed with `suboptimal_flops`** | — | `measurements.json`: `verdict_steps`, `failed_verdicts`, `suboptimal_verdicts` |
| Target failures per 100 verdicts | **0 / 147 × 100 = 0.000** | ≤1.979: **PASS** | `failed_per_100`, `failed_verdict` |
| Sole-cause seat-hours per day | **(0 + 0) / 3,600 / 1 = 0.000**; no sole-cause failures | ≤0.100: **PASS** | `wall_seconds`, `gap_seconds`, `span_days`, `seat_verdict` |
| Same window, earlier per-step rule | **184 clippy steps; 76 failed; 26 with the target: 26 / 184 × 100 = 14.130** | Includes JSON replay diagnostics | `total_steps`, `failed_steps`, `per_step_suboptimal` |
| Complete selected call sequences | **185 clippy steps; 159 judging steps; 12 judgments with the target** | Includes a final judgment after the window end | `cohort_clippy_steps`, `judging_steps`, `judging_suboptimal` |
| JSON replay cost and result | **26 replays in 12 calls; 37 recorded fixes; 2,285.385 s added clippy time** | **3 calls** ended with a passing clippy verdict; those calls contain **6 replays** | `rewrite_steps`, `rewrite_calls`, `rewrite_fixes`, `rewrite_clippy_seconds`, `rewrite_passing_calls`, `rewrite_passing_verdicts` |
| Applied rounds and cap stops | **14 rounds: 10 calls with one round; 2 calls with two rounds; 0 cap stops; 0 unknown round counts** | All 12 count logs are available | `rewrite_rounds`, `rounds_per_call`, `cap_stops`, `rewrite_missing_count_logs` |
| Earlier single-replay result | **5 / 124 × 100 = 4.032; (956 + 657.026) / 3,600 / 1 = 0.448 seat-hours/day** | Both FAIL | “Lint-step rewrite, re-measure” below |

Both limits **PASS**. No final failed verdict contains the target lint. Thus, no remaining target cause has a failed verdict to rank (`commits.json`: empty `causes` and `diagnostic_groups`; `remaining_warnings` is empty). The other 42 failed verdicts do not contain this lint. There are no sole-cause call times or follow-up intervals to sample. The call-wall median, interval median and interval p75 are unavailable. Their sums are zero. These results support a proposal to retire the edit hook. This phase removes nothing. The showrunner must decide on the proposal.

The count logs give these applied rounds and fixes. The total sums each saved count line once. A fix can be one stage of a chained expression's rewrite.

| Call ID | Applied rounds | Recorded fixes |
| --- | ---: | ---: |
| `v20261017T214057-354858-16777` | 1 | 1 |
| `v20261017T232129-2960842-22` | 1 | 2 |
| `v20261017T234353-416992-2893` | 2 | 8 |
| `v20261018T020451-517360-16995` | 2 | 3 |
| `v20261018T083822-549247-14928` | 1 | 1 |
| `v20261018T091812-2139493-11979` | 1 | 6 |
| `v20261018T114747-905865-29270` | 1 | 2 |
| `v20261018T151118-961065-30865` | 1 | 3 |
| `v20261018T163123-3605214-502` | 1 | 1 |
| `v20261018T190237-4074915-16270` | 1 | 2 |
| `v20261018T194506-1016913-20905` | 1 | 3 |
| `v20261018T202042-2903290-354` | 1 | 5 |
| **Total** | **14** | **37** |

The last call's first judgment started at **2026-10-18 16:42:39.178 EDT (2026-10-18T20:42:39.178Z)**. Its final judgment started at **2026-10-18 17:10:09.085 EDT (2026-10-18T21:10:09.085Z)**. The final judgment failed on other errors. It had no target warning. The cohort includes that final judgment even though it started after the window end.

The final pipeline completed after every selected call had a completion record. **`audit.json` reports PASS**. It agrees on the selected verdict IDs, both limit verdicts, cost sums, rounds, fixes, replay times and cap stops. The synthetic checks report **PASS**. All four copied or new Python files report **0 errors, 0 warnings, 0 notes** under basedpyright.

The source scripts and JSON are in `~/.local/state/mul-add-hook/lint-rounds/`. They are copies of the earlier `lint-rewrite/` scripts with the changes below. A call enters the cohort when its first judging clippy step starts inside the window. The scripts read all steps in that call, including steps after the window ends. They use its last judging step as the verdict. JSON replay steps are not verdicts. A step with no call ID stands alone. Both the measurement and the audit reject a selected call with no completion record.

Run `python3 -B ~/.local/state/mul-add-hook/lint-rounds/synthetic_checks.py` for the synthetic checks. Then run `measure.py`, `commits.py` and `audit.py` from that folder with `python3 -B`. `measure.py` writes `measurements.json` and `warnings.json`. `commits.py` reads them and writes `commits.json`. It keeps each call's full replay history in order. It does not match Git commits. The rewrite enters the calling tree before any commit. `audit.py` reads the index, gzip logs and count logs again. It uses its own SQL window ranks and count parser. It writes `audit.json`.

Each script opens the buildlog index in read-only mode. Each reads saved count logs from `~/.local/state/mul-add-hook/lint-rewrite/`. The scripts do not change those inputs. The measurement reads each call's count log once. It sums the fixes in all saved lines. Each line counts as one applied round. A replay with no saved line adds no known round. A missing log makes that call's round count unknown. Five saved lines count as one cap stop. Four lines and a failed fifth replay do not count as a cap stop. Replay duration is the added JSON clippy cost. The scripts sum it apart from judgment duration.

The synthetic checks use a temporary SQLite index, gzip logs and count logs. They run all three copied scripts. They check two applied rounds and an empty replay, five applied rounds, and four applied rounds with a failed fifth replay. They keep a round-one rewrite with a later failure apart from a call with no recorded rewrite. They select a final judgment after the window end when its first judgment starts inside. They also check missing counts, separate steps with no call ID, both bounds, and open or missing call records. The checks delete their temporary inputs.

The SQL below selects the cohort and its complete clippy sequences. Bind each placeholder pair to the UTC start and exclusive end above. `measure.py` orders judgments by precise start time and step ID. It keeps the last judgment per call. `commits.py` uses the same query without `s.step='clippy'` to read all stages. The independent audit query is in `audit.json`.

```sql
WITH first_judgments AS (
 SELECT CASE WHEN COALESCE(call_id,'')='' THEN 'step:'||id
             ELSE 'call:'||call_id END AS cohort_key,
        MIN(julianday(started_at)) AS first_start
 FROM steps
 WHERE repo='hana' AND step='clippy'
   AND argv NOT LIKE '%--message-format=json%'
 GROUP BY cohort_key
), cohort AS (
 SELECT cohort_key FROM first_judgments
 WHERE datetime(first_start)>=datetime(?)
   AND datetime(first_start)<datetime(?)
)
SELECT s.* FROM steps s JOIN cohort c ON c.cohort_key=
 CASE WHEN COALESCE(s.call_id,'')='' THEN 'step:'||s.id
      ELSE 'call:'||s.call_id END
WHERE s.repo='hana' AND s.step='clippy'
ORDER BY julianday(s.started_at),s.id;
```

For call costs, use the same CTE and replace its final `SELECT` with this query. This query has a second pair of bounds. It also reads selected calls whose call start precedes the window. The follow-up rule stays the same as before. The next lint call must share worktree, worker and session. It must start inside the window. Its end must fall within 1,800 seconds after the failed call's end.

```sql
SELECT * FROM calls WHERE repo='hana'
 AND ('call:'||id IN (SELECT cohort_key FROM cohort)
      OR (tool='verify.sh' AND verb='lint'
          AND datetime(started_at)>=datetime(?)
          AND datetime(started_at)<datetime(?)))
ORDER BY julianday(started_at),id;
```

The earlier per-step comparison uses its own bounded query. It counts each step whose start is inside the window, including JSON replays.

```sql
SELECT * FROM steps WHERE repo='hana' AND step='clippy'
 AND datetime(started_at)>=datetime(?)
 AND datetime(started_at)<datetime(?)
ORDER BY datetime(started_at),id;
```

**Lint-step rewrite, re-measure.** The complete 24-hour window is **2026-10-16 16:09:48 EDT (2026-10-16T20:09:48Z) ≤ start < 2026-10-17 16:09:48 EDT (2026-10-17T20:09:48Z)**, in America/New_York, UTC-04:00. It starts at T_lint, including the calls before the first passing rewrite. The verdict unit is the last judging clippy step in each `call_id`; JSON replay steps are excluded, and a step without a call ID stands alone. Sources in this subsection are the adapted scripts and generated JSON in `~/.local/state/mul-add-hook/lint-rewrite/`.

| Measure | Lint-step rewrite, re-measure | Limit / verdict | Source |
| --- | --- | --- | --- |
| Final clippy verdicts | **124 total; 39 failed; 5 failed with `suboptimal_flops`** | — | `measure.py` → `measurements.json`: `verdict_steps`, `failed_verdicts`, `suboptimal_verdicts` |
| Target failures per 100 verdicts | **5 / 124 × 100 = 4.032** | ≤1.979: **FAIL** | `measure.py`: `failed_per_100`, `failed_verdict` |
| Sole-cause seat-hours per day | **(956 + 657.026) / 3,600 / 1 = 0.448**; one sole-cause call | ≤0.100: **FAIL** | `measure.py`: `wall_seconds`, `gap_seconds`, `span_days`, `sole_call_ids`, `seat_hours_per_day` |
| Same window, earlier per-step rule | **151 clippy steps; 66 failed; 32 with the target: 32 / 151 × 100 = 21.192** | Includes replay diagnostics | `measure.py`: `total_steps`, `failed_steps`, `per_step_suboptimal` |
| Judging steps before grouping by call | **137 steps; 18 with the target** | Excludes JSON replays | `measure.py`: `judging_steps`, `judging_suboptimal` |
| JSON replay cost and result | **14 replays; 28 recorded fixes; 3 followed by a passing final clippy verdict; 1,275.100 s added clippy time** | One replay has no saved count | `measure.py`: `rewrite_steps`, `rewrite_fixes`, `rewrite_passing_verdicts`, `rewrite_clippy_seconds`, `rewrite_missing_count_logs` |
| Earlier detector-reach measurement | **8.235 target failures per 100 steps; 1.104 sole-cause seat-hours/day** | Both FAIL | “Detector reach, re-measure” below; its earlier 96-hour window and per-step unit differ |

Both limits **FAIL**. The limit counts failed verdicts, so the causes are ranked by the five failed verdicts, not by warnings:

| Cause | Failed verdicts | Evidence |
| --- | --- | --- |
| **Overlapping suggestions in a chained sum** (`a * x + b * y + c * z`): clippy suggests an outer and an inner `mul_add` over the same bytes; the applier takes the outer one and skips the inner, which the final judgment then reports | **2**, and the whole seat-time failure | `v20261016T214258-406393-16864`: count "1 skipped: overlapping", one residual at `crates/hana/src/tool/rear_patch_panel/mod.rs:3967`; `v20261017T102205-1529647-3466`: count "3 skipped: overlapping", three residuals in `tests/arrange_glyph/envelope.rs` and `selection/patch_edit/session.rs` |
| **Suggestions inside a macro body**: `MaybeIncorrect`, so the applier's `MachineApplicable` requirement leaves them unchanged | **2** | `v20261017T054451-3834727-27451` at `hana_mimesis_tools/src/show.rs:205` (no saved count); `v20261017T160057-1031444-24087` at `show.rs:777`, one expression reported nine times |
| **Crates the failing run never reached**: the first clippy run stopped when `hana_conduit` failed, so the replay saw only that crate; after the rewrite the final run reached `hana` and the test targets and failed there | **1** | `v20261017T094538-3264439-10778`: the first and replay runs report only `hana_conduit/src/routing/draped.rs`; the final run reports `hana/src/conduit/cable.rs` and `tests/cable_motion/jack_entry.rs` |

The largest remaining cause is the **overlap skip**: it ties macro bodies on failed verdicts and alone accounts for the seat-time failure. Counted by warnings instead, macro bodies would lead with 10 of the **18 target warnings**, but nine of those ten are one expression repeated. Overall, **17 warnings in four verdicts** remain after a recorded positive rewrite; **one warning in one verdict** has no retained positive count (`commits.py` → `commits.json`: `diagnostic_groups`, `causes`, `remaining_warnings`, `replay_targets`). Warning totals include repeated diagnostics; they count neither distinct expressions nor distinct edits. The missing count log cannot alone distinguish zero edits from a failed log save. These results do not support retiring the edit hook.

The seat-time failure comes from call `v20261016T214258-406393-16864`: its final judgment retains one warning at `crates/hana/src/tool/rear_patch_panel/mod.rs:3967`. The replay supplied overlapping outer and inner multiply-add suggestions; the saved count reports one fix and one overlap skipped, leaving the inner expression for the final judgment (`commits.json`: `replay_targets`, `remaining_warnings`; that call's saved `.log`). Its **956 s** call wall time plus **657.026 s** to the next lint call's end totals **1,613.026 s**. As before, the next call must share worktree, worker and session, start inside the window, and end within **1,800 s** after the failed call ends. There is one accepted interval: call-wall median **956 s**, interval median and p75 **657.026 s** (`measurements.json`: `loops`, `call_wall_median_seconds`, `gap_median_seconds`, `gap_p75_seconds`). Replay time above is the added JSON clippy cost, reported separately from this sole-cause seat-time metric.

Reproduce by running `python3 -B ~/.local/state/mul-add-hook/lint-rewrite/measure.py`, then `commits.py` and `audit.py` from the same directory. `measure.py` reads the index in read-only mode, decompresses failed logs individually in memory, and reads saved `<call_id>.log` counts; it writes `measurements.json` and `warnings.json`. The adapted `commits.py` reads those outputs and bounded step/log records to write `commits.json` with call sequences and residual diagnostics. It follows calls rather than Git commits because rewrites enter the calling worktree as uncommitted edits. `audit.py` independently ranks verdicts in SQL and rereads diagnostic logs and saved counts; **`audit.json` reports PASS** for the counts, sums, selected verdict IDs and both limit verdicts. All copied scripts report **0 errors, 0 warnings, 0 notes** under basedpyright.

The complete input queries are below; each pair of placeholders binds the UTC start and exclusive end stated above. `measure.py` sorts rows by precise start time and ID, removes replay argv, and keeps the last judgment per call. Seat-time calculations select `tool='verify.sh' AND verb='lint'` from the bounded calls. `commits.py` uses the third query for stage order; `audit.py` records its independent `ROW_NUMBER` verdict query in `audit.json`.

```sql
SELECT * FROM steps WHERE repo='hana' AND step='clippy'
  AND datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)
  ORDER BY datetime(started_at),id;
SELECT * FROM calls WHERE repo='hana'
  AND datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)
  ORDER BY datetime(started_at),id;
SELECT * FROM steps WHERE repo='hana'
  AND datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)
  ORDER BY datetime(started_at),id;
```

**Detector reach, live check.** The wider detector, which reports every likely multiply-add on an edited line with a conditional message, reached the live natedev hooks at **T_reach = 2026-10-12 14:13:42 EDT (America/New_York, UTC-04:00; 2026-10-12T18:13:42Z)**, the committer time of merge `b22c1218`. In separate scratch crates outside every repository, Codex `apply_patch` and Claude `Edit` each received one block when adding `let centre_row = top + metrics.button.height * 0.5;`. Both blocks read, with the scratch path normalized: `<file>:3: if top + metrics.button.height * 0.5 is f32 or f64 arithmetic, write metrics.button.height.mul_add(0.5, top) (clippy::suboptimal_flops). Leave integer and vector arithmetic as it is. The edit was applied.` A second `apply_patch` changed only `let top` and received no block; Claude's second `Edit` did the same and received no block. The live `blocks.jsonl` contains one matching record per block, tagged `codex` and `claude`. `python3 scripts/hooks/codex_hooks.py check` exited 0 and reported both handlers trusted.

**Detector reach, re-measure.** The complete window is **2026-10-12 14:13:42 EDT (2026-10-12T18:13:42Z) ≤ start < 2026-10-16 14:13:42 EDT (2026-10-16T18:13:42Z)**, in America/New_York, UTC-04:00. The fixed span is 96 hours. Sources below name the scripts kept with their outputs in `~/.local/state/mul-add-hook/reach-2026-10-16/` (each reads and writes beside itself); comparison values and limits are those specified by the plan's phase-4 Work Order.

| Measure | Detector reach, re-measure | Limit / verdict | Source |
| --- | --- | --- | --- |
| Hana clippy steps | 850 total; 295 failed; all failed logs available and parsed | — | `measure.py`: `total_steps`, `failed_steps`, `missing_logs`, `no_error_diagnostic` |
| Failed steps containing `suboptimal_flops`, per 100 | **70 / 850 × 100 = 8.235**; baseline 7.916, first re-measure 8.126, since go-live comparison 8.67 | ≤1.979: **FAIL** | `measure.py`: `suboptimal_steps`, `failed_per_100` |
| Sole-cause seat-hours per day | **1.104**; 16 sole-cause steps and 16 lint calls; baseline 0.402, first re-measure 0.576 | ≤0.100: **FAIL** | `measure.py`: `sole_steps`, `sole_calls_measured`, `seat_hours_per_day` |
| Warnings with an earlier block for the same file and expression | **1 of 227**; **226** without a match | — | `measure.py` → `warnings.json`: `matches`, `category` |
| Warnings without a match, split by today's detector on a stub | **208 unknown** (stub reported); **18 skipped** (stub not reported) | — | `measure.py`: `categories` |
| Rules behind skipped stubs | 9 `VECTOR` exclusions; 5 `.powi(2)` cases without a supported product root; 2 `.sqrt()` receiver exemptions; 1 macro invocation; 1 truncated excerpt whose `...` tokens interrupt the expression | — | `measure.py` → `warnings.json`: `category`, `source`, `diagnostic` |
| Block records in the window, across repositories | **39: Codex 39, Claude 0**. Excludes the **2** phase-3 records under `live_check_claude/` and `live_check_codex/` | — | `measure.py`: `blocks`, `blocks_by_agent`, `controls_excluded` |
| Block followed by a matching `mul_add` in the next commit touching its file | **At least 17 of all 39 records inspected**; 2 retain the original expression, 8 changed or relocated expressions remain unverified, 12 have no next touching commit within the window | — | `commits.py` → `commits.json`: `outcomes`, per-record commit and rewrite locations |

Both limits **FAIL**. The largest remaining category is unknown: the detector reports the stubs for **208 warnings in 66 of the 70 affected steps**, without an earlier matching block. This category also occurs in **15 of the 16 sole-cause failures (30 warnings)** (`measure.py`: `category_steps`, `sole_categories`). A stub finding establishes that the current detector can report the excerpt; it does not identify why the hook left no earlier record. The line may not have been written by the edit, the edit may have bypassed the hooks, or the log append may have failed. Stubs omit file context, and the truncated excerpt cannot establish the real-file exclusion. Warning counts include repeats across steps; step counts for different categories can overlap.

The seat-time calculation retains the earlier `derive.py` / `loop.py` definition: sum the sole-cause failed lint-call wall time and the accepted intervals from failed-call end to next lint-call end, convert seconds to hours, and divide by the window's days. `measure.py` records **8,776 s** of call wall time plus **7,123.248 s** of accepted intervals, totaling **15,899.248 s / 3,600 / 4 = 1.104 seat-hours/day**; the sole-cause clippy steps themselves sum to **2,856.016 s**. Next calls must share worktree, worker and session, start within the same window, and finish within **1,800 s** of the failed call's end. The live index calls the seat column `worker`. There are **12** accepted intervals (10 next calls passed or reused, 2 failed) and **4** without a follow-up inside the cap and window. Call-wall median is **572 s**; interval median is **478.684 s**, and interval p75 is **783.432 s**, using sorted index `3*n//4` (`measure.py`: `wall_seconds`, `gap_seconds`, `seat_seconds`, `sole_clippy_seconds`, `next_outcomes`, `no_followup`, `call_wall_median_seconds`, `gap_median_seconds`, `gap_p75_seconds`). Empty samples produce unavailable medians and percentiles, with their sums still reported.

Reproduce with `python3 ~/.local/state/mul-add-hook/reach-2026-10-16/measure.py`, then `python3 ~/.local/state/mul-add-hook/reach-2026-10-16/commits.py`; `audit.py` there re-checks the counts and sums independently. The first script adapts the archived `totals.py`, `clippy_fail_lints.py`, `cost.py`, `loop.py`, `extract.py`, `scan_residuals.py` and `gate.py`; it reads `~/.local/state/buildlog/index.sqlite` in read-only mode and decompresses one failed step's gzip log at a time in memory. It writes `measurements.json`, `warnings.json` and `blocks.json`; no archived generated JSON is reused. Its complete input queries are below, with both placeholders bound to `2026-10-12T18:13:42Z` and `2026-10-16T18:13:42Z`, respectively:

```sql
SELECT * FROM steps WHERE repo='hana' AND step='clippy'
  AND datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)
  ORDER BY datetime(started_at),id;
SELECT * FROM calls WHERE repo='hana' AND tool='verify.sh' AND verb='lint'
  AND datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)
  ORDER BY datetime(started_at),id;
```

`measure.py` reads `~/.local/state/mul-add-hook/blocks.jsonl`, excludes the named controls, and compares absolute normalized file paths and detector `expr` strings from `mul_add_lib.mul_add_findings` on balanced stubs, with all stub lines selected. It applies the detector's whitespace/comment compaction to older raw block expressions and requires a block time before the warning's step start; line number and message wording are not match keys. Earlier-match lookup permits records before the window; window block counts use both bounds. `commits.py` reads only the `blocks.json` written by `measure.py`, then runs `git --no-optional-locks -C <recorded cwd> log --first-parent --full-history --format='%H %cI' --since=2026-10-12T18:13:42Z --until=2026-10-16T18:13:42Z HEAD -- <file>` and `git show <commit>:<file>`. It reapplies the exact bounds to committer timestamps and selects the earliest later touching commit in that worktree's current first-parent history. It checks the expression's rewrite across line movement, including substitutions whose alias, import or constant declaration is recorded in `commits.json`; an unrelated `mul_add` at the old line number is insufficient. Unverified changes and absent commits are not counted as repairs, and the confirmed count does not establish that the block caused the repair.

The earlier `cost.py`, `loop.py`, `clippy_fail_lints.py` and `tml_lengths.py` cover an earlier 96-hour window, **2026-10-07 01:44:05 EDT (2026-10-07T05:44:05Z) to 2026-10-11 01:44:05 EDT (2026-10-11T05:44:05Z)**, containing **763 hana clippy steps, 250 failed**.

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

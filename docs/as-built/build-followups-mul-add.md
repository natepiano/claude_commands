# Fused multiply-add: lint rewrites and Rust edit hooks

## What it is

`verify.sh lint` applies Clippy's automatic multiply-add fixes before its final judgment. It runs up to five rewrite rounds to reach inner sums and more workspace crates. A Rust PostToolUse hook also tells Claude and Codex about likely multiply-adds on lines they write. The hook gives a conditional correction because it cannot prove every type. The latest hana measurement passes both limits. The edit hook stays installed while the showrunner decides on its proposed retirement.

The hook shares its edit parser, Rust scanner and Cargo lint reader with the function-length hook. The nightly Rust release check enables FMA for its Linux clippy build. Buildlog records a Codex worker's own thread as its session.

## How it works

### Files and contracts

| File | Role |
| --- | --- |
| `scripts/delegate/verify.sh` | Runs the lint judgments, rewrite rounds, formatting and cache rekey. |
| `scripts/lint/invoke.sh` | Runs and logs Cargo steps. Keeps JSON stdout separate from stderr. |
| `scripts/lint/apply_mul_add.py` | Applies only Clippy's automatic `suboptimal_flops` suggestions. |
| `scripts/hooks/mul_add_lib.py` | Finds likely multiply-adds. Applies evidence rules, lint scope and exemptions. Builds correction text. |
| `scripts/hooks/fn_length_lib.py` | Shares the edit parser, scanner, lint reader and target-root check. Also checks function length. |
| `scripts/hooks/post-tool-use-mul-add.py` | Scans written lines, collects findings and returns one block. |
| `scripts/hooks/post-tool-use-fn-length.py` | Uses the same parser. Checks whole files against their function-length limits. |
| `scripts/hooks/codex_hooks.py` | Installs and trusts the Codex `apply_patch` handlers. |
| `settings.json` | Runs basedpyright, fn-length and mul_add in Claude's `Edit\|MultiEdit\|Write` group. |
| `scripts/buildlog/rust_release.py` | Sets the release trial's Linux clippy FMA flags. |
| `scripts/buildlog/record.py` | Supplies worker, delegate session and agent session for step and call records. |
| `scripts/delegate/test_verify_lint_scope.py` | Uses fake Cargo to check lint scope, rounds, counts, cache keys and output streams. |
| `scripts/lint/test_apply_mul_add.py` | Checks lint selection, byte edits, duplicates, overlaps and file bounds. |
| `scripts/hooks/test_mul_add.py`, `test_fn_length.py`, `test_codex_hooks.py` | Check payloads, scope, exemptions, errors, scaling and Codex registration with synthetic inputs. |
| `scripts/buildlog/test_rust_release.py`, `test_record.py` | Check release flags and agent-session precedence with isolated inputs. |

The public hook entry points are:

```python
applied_rust_edits(raw: str) -> AppliedRustEdits | IgnoredEdit
applied_patch_rust_files(patch: str) -> tuple[str, ...]
written_lines(source: str, replacements: tuple[Replacement, ...]) -> frozenset[int]
read_clippy_lint(rs_file: os.PathLike[str] | str, lint: str,
                group: str) -> ConfiguredClippyLint | UnavailableClippyLint
suboptimal_flops_scope(rs_file: os.PathLike[str] | str) -> SuboptimalFlopsLintScope
mul_add_findings(rs_file: os.PathLike[str] | str,
                 written: frozenset[int] | None = None
                 ) -> tuple[SuboptimalFlopsLintScope, list[MulAddFinding]]
long_functions(rs_file: os.PathLike[str] | str
               ) -> tuple[TooManyLinesLintScope, list[FunctionLength]]
```

`AppliedRustEdits(agent, tool, cwd, files, replacements)` holds the Rust files from one supported edit in payload order. `Replacement` is an `(old, new)` text pair. Each file has its own tuple of pairs. `IgnoredEdit` means no supported Rust edit needs a scan. `written_lines` returns 1-based line numbers. At the detector boundary, `written=None` selects all lines. The scan uses `SourceLineSelection.ALL` for that state.

`ConfiguredClippyLint(level, package_dir)` holds the package's lint level. `ClippyLintLevel` has `ALLOW`, `EXPECT`, `WARN`, `DENY`, `FORBID` and `UNSET`. `UnavailableClippyLint` has `NO_PACKAGE` and `UNREADABLE`. `SuboptimalFlopsLintScope.state` uses `ScopeState`: `ENABLED`, `DISABLED`, `EXEMPT` or `NO_PACKAGE`. Only `ENABLED` scans expressions. `EXEMPT` covers a possible no_std target or an uncertain crate root.

`MulAddFinding(line, a, b, c, expr, rewrite)` holds the source line, product operands, addend, expression text and correction. Its properties are read-only. `TooManyLinesLintScope(enabled, threshold)` holds the function-length limit. `FunctionLength(name, line, lines, exempt)` holds one measured function. `RustToken(text, start, end)` keeps source offsets. These scanner and finding classes use `__slots__` to limit import cost on each edit.

### Edit → scan → correction

1. **Read the edit.** Both hook entries return before importing the parser when the raw text has neither `.rs` nor a Unicode escape. Claude `Edit`, `MultiEdit` and `Write` supply `tool_input.file_path` and `cwd`. Codex `apply_patch` supplies patch text in `tool_input.command`.
2. **Keep replacements.** An `Edit` supplies one `(old, new)` pair. `MultiEdit` supplies one per entry. `Write` supplies `("", content)`. A patch supplies one pair per added file or update chunk. `*** Move to:` carries the pairs to the new path. Delete headers add no file. Rust paths are deduplicated in payload order.
3. **Select written lines.** The hook resolves paths against `realpath(cwd)` and reads each existing file. It skips files with no `*`. It also skips files with neither `+` nor `-`. `written_lines` removes the common leading and trailing lines from each pair. It maps the remaining nonempty new lines to every occurrence of the new text in the file. If the new text is absent, it selects lines whose stripped text occurs in the new text but not the old text.
4. **Scan.** `mul_add_findings` scans function bodies with comments and strings blanked. It keeps a finding if any line in its source span was written. The function-length hook checks the whole file. Its rules are in [the function-length hook](build-followups-fn-length-hook.md).
5. **Return one block per hook.** Each hook scans all files before it appends any block record. Its JSON has `decision: "block"`, `continue: true`, `reason`, `systemMessage` and `hookSpecificOutput.additionalContext`. The edit stays on disk.

The multiply-add description is `<file>:<line>: if <expr> is f32 or f64 arithmetic, write <rewrite> (clippy::suboptimal_flops)`. The reason joins descriptions with `; ` and ends with `. Leave integer and vector arithmetic as it is. The edit was applied.` Additional context joins the descriptions with newlines. The system message reports the number of possible expressions and the affected files. The function-length reason names the function, count and limit, then says `split it now`.

The multiply-add log is `blocks.jsonl` under `MUL_ADD_HOOK_STATE`, else `~/.local/state/mul-add-hook`. It writes one record per expression: `at`, `agent`, `tool`, `cwd`, absolute resolved `file`, `line`, `expression`. Each record has its own UTC time. Records from one response share no ID. The function-length log uses `FN_LENGTH_HOOK_STATE`, else `~/.local/state/fn-length-hook`. It writes one record per affected file with `threshold` and `functions: [{name, line, lines}]`. Append errors do not change the block.

### Detector evidence and exemptions

`ArithmeticTokens` finds product chains next to binary `+` or `-`, or on the whole right side of `+=` or `-=`. The last operator in a chain of `*`, `/` and `%` must be `*`. Parentheses around a product do not hide it. One sum gives one finding. When both sides are products, the left product supplies the receiver. Calls, paths, indexes, casts and macro invocation arguments can occur in a candidate. The scanner skips `macro_rules!` definitions.

The detector checks evidence from the whole sum:

- Float literals and `_FLOAT_EVIDENCE` allow sums that also have integer evidence.
- Integer evidence skips a sum with no float evidence. It includes integer terms or factors, integer casts, `.len()`, `.count()` and a sum used as a whole index. Integer literals inside indexes, call arguments and tuple fields do not count as terms.
- `VECTOR` matches and bare names with non-float or vector evidence skip a sum. A dotted vector match followed by `()` and a scalar member does not count as vector evidence. `_SCALAR_RESULT` names `.x`, `.y`, `.z`, `.w`, `.width`, `.height`, `.depth`, `.length`, `.0` and `.1`.

`BareNameEvidence` uses the last `let` before the sum in the same function. This includes an unannotated `let`. With no local binding, it uses the parameter annotation or a file `const` or `static` typed other than `f32` or `f64`. A nested function's bindings do not affect its parent. `NameArithmeticEvidence.UNKNOWN` and `FLOAT` disprove nothing. `NONFLOAT` and `VECTOR` skip the sum. The hook needs no same-file field proof. Pattern, destructuring and let-else exclusions do not apply.

Const items, `const fn`, const blocks and static initializers are exempt. So are sums used as `.sqrt()` receivers and items with `allow` or `expect` for the lint or `nursery`. These attributes also apply through enclosing items, file attributes and `cfg_attr`. A `'static` lifetime gives no exemption. An unsuffixed literal receiver swaps with its other product operand. Two unsuffixed product literals give no finding.

The correction uses Clippy's form. For example, `c - a * b` gives `a.mul_add(-b, c)`. `a * b - c` gives `a.mul_add(b, -c)`. `x += a * b` gives `x = a.mul_add(b, x)`. A product chain gives `(a * b).mul_add(c, d)`. `_receiver` and `_negated` add parentheses where needed. A generic call keeps its whole callee. `_compact` removes comments and reduces whitespace outside literals to one space in `expr` and `rewrite`.

### Scope and Codex registration

`read_clippy_lint` finds the nearest package manifest. It follows `lints.workspace = true`, including an explicit `package.workspace` path. An explicit `suboptimal_flops` entry wins over `nursery`. `too_many_lines` wins over `pedantic`. String levels and `{level = …}` entries are accepted. Warn, deny and forbid enable checking. Missing or unreadable configuration passes the edit. The function-length threshold comes from an ancestor `clippy.toml` or `.clippy.toml`. Its default is 100. An invalid or negative threshold disables that check.

For multiply-add, `target_may_be_no_std` checks the edited target's root. A std `src/main.rs` is checked beside a no_std library. Binary, test, example and bench targets use their own roots. Other library modules inspect available `lib.rs` and `main.rs` roots. They pass if either can be no_std. A missing root, `build.rs` or a path outside supported target folders passes as exempt. This exemption applies only to multiply-add.

`codex_hooks.py install|check` manages three `CodexHook` handlers for `apply_patch`. The fn-length and mul_add handlers use `PostToolUse` with a 10-second timeout. The berth handler uses `PreToolUse` with a 30-second timeout. Command strings match the Claude registrations where present. `LocatedHook` holds the handler key, hash, trust state and enabled state. `MissingHook` means the handler is absent.

`install` adds missing groups to `CODEX_HOME/hooks.json`, else `~/.codex/hooks.json`, by atomic replacement. It keeps existing indexes and trust. Through `codex app-server`, it calls `initialize`, `hooks/list` and `config/batchWrite` to trust each untrusted or modified handler. It then lists again. A trusted enabled duplicate wins. `check` lists without writing. Both return 0 only when all three handlers are trusted and enabled. Other results name the state or file/RPC error. The app-server has a 30-second deadline. Its child stops on exit. Binary and home overrides resolve before a directory change.

### Runtime flags and attribution

The release check sets `CARGO_INCREMENTAL=0` for clippy. It sets `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=mold -C target-cpu=x86-64-v3"`. The build runs in the release trial's clone. Rustflags enter compilation hashes. A changed flag causes a rebuild on the next build that uses it.

`record.py` chooses `CODEX_THREAD_ID`, then `CLAUDE_CODE_SESSION_ID`, then no session. A Codex worker can inherit its director's Claude variable. This order selects the worker's own thread. Old rows keep their recorded identities. Comparisons by worker and session must not join intervals across an identity change.

### Lint-step rewrite

The normal `verify.sh lint` path repairs multiply-adds after mend, formatting and Taplo. It runs the first workspace clippy judgment and captures its output. A pass continues to rustdoc. A failure without `#suboptimal_flops` returns that judgment's status. The link matches both diagnostic wordings.

A matching failure starts up to five rounds. Each round calls `invoke_clippy_mul_add_json` from `scripts/lint/invoke.sh`. It uses the same workspace arguments with one added Cargo argument:

```sh
cargo clippy --workspace --all-targets --all-features --keep-going --message-format=json -- -D warnings
```

All clippy judgments and replays in this rewrite path use `--keep-going`. The flag is set at the lint call sites in `verify.sh`. `invoke_clippy` does not add it. Independent crates can still compile after another crate fails. A later round can reach dependent crates after the earlier round fixes their dependency. `lint --workspace --check` reports findings without rewrites. `example`, `check`, `test` and `final` do not enter the rewrite loop.

Each replay runs through `run`. Buildlog records its argv and `call_id`. `--message-format=json` marks a replay. Judging steps omit that marker. A replay status of 0 or 101 lets the applier read the stream. Status 101 is normal under `-D warnings` when diagnostics remain. Any other replay status ends the loop with `verify.sh: clippy's multiply-add rewrite failed; see the output above`.

`scripts/lint/apply_mul_add.py --workspace-root <root> <json file>` selects only `compiler-message` records with code `clippy::suboptimal_flops`. It reads spans in `message.spans` and direct `children[].spans`. It takes only `MachineApplicable` suggestions with a replacement. Source-level `#[allow(clippy::suboptimal_flops)]` removes the diagnostic before this step. Macro-body suggestions marked `MaybeIncorrect` stay unchanged.

The applier resolves each path inside the workspace root, including symlinks. It drops exact file/range/replacement duplicates. For overlaps, it keeps the first span and counts later conflicts as skipped. It edits bytes from the last span to the first. This preserves offsets after a UTF-8 prefix or a longer replacement. It checks the whole stream for `build-finished`. It reads and checks all files and ranges before any write. Each changed file uses a temporary file and atomic replacement. A failed write leaves that file intact. Earlier files can already have changed.

The applier returns 0 after a rewrite, 3 (`NOTHING_APPLIED`) without changes, or 1 for an incomplete stream or read/write error. The shell uses this status. It does not parse the count text. Status 0 prints `verify.sh: rewrote <n> multiply-add(s) in <m> file(s)`. An overlap adds `(<k> skipped: overlapping)`. The next round reads the changed tree. This lets it fix an inner sum skipped in an earlier round.

Status 3 ends the loop. An applier error also ends it and prints the failure line. Edits from earlier rounds stay after a later failure. If no round returned 0, lint prints `verify.sh: clippy's multiply-add rewrite changed nothing` and returns the first judgment's status. A first-round failure prints both the failure line and `changed nothing`. Partial edits from an applier error can remain even with no successful round.

After five applied rounds, lint prints `verify.sh: stopped the multiply-add rewrite after 5 rounds`. If any round returned 0, lint formats `FMT_SCOPE` once more after the loop. It recomputes `VERDICT_KEY` when recording a run. It then runs one final clippy judgment. That judgment's status stands, even after a later-round failure. A passing judgment continues to rustdoc. Clippy off keeps the existing single `SKIPPED` line and runs no replay. The cache key includes `verify.sh`, `invoke.sh`, the applier and lint configuration.

Each applied round prints one count line. Lint saves those lines in order to `<call_id>.log` under `~/.local/state/mul-add-hook/lint-rewrite/`. `MUL_ADD_REWRITE_LOG_DIR` overrides the folder for both writer and watcher. The writer also honors `MUL_ADD_HOOK_STATE` when the override is absent. A failed save prints a notice and does not fail lint. `BUILDLOG_OFF=1` disables this extra log. The count is the sum of the fixes in every saved line. A rewrite enters the calling worktree as an uncommitted edit. No separate Hana commit marks it.

`run_json_stdout` keeps Cargo JSON alone on stdout through both `verify.sh`'s `run` and `invoke.sh`'s `run_once`. The banner and Cargo stderr reach stderr. A background `tee` reads a temporary FIFO for stderr. Stdout streams to the caller and a temporary file. After the stderr process ends, the helper appends the stdout file to the log. The log holds stderr first, then stdout. All output is complete when the helper returns. It uses Bash 3.2 constructs. Both output paths return the command's status even if a logger fails. Other invocations keep their existing output path.

The JSON route selects one lint without changing lint levels. A `cargo clippy --fix` probe with only the target enabled applied nothing on this machine. The isolated JSON probe applied only the ordinary `f64` multiply-add. It left an allowed multiply-add, an unused import and `useless_format` unchanged. It counted duplicate lib/test diagnostics once. The probe files and source diff are in `~/.local/state/mul-add-hook/lint-rewrite/probe-2026-10-16/`.

`~/.local/state/mul-add-hook/lint-rewrite/watch.py` reads Git and the index without changing either. `first-fire <checkpoint>` returns 1 until live `~/.claude` `HEAD` contains the checkpoint. It derives the start time from the first containing commit on the first-parent chain. It returns 0 for a hana lint call with a completed replay followed by a passing judgment. It prints the call ID, start in America/New_York and UTC, and the sum of saved count lines. A missing count is unavailable. An unreadable index returns 3. `window <checkpoint> <hours>` prints the live start and due time in both zones. It returns 0 at or after the due time and 1 before it.

## Invariants

- Keep `clippy::suboptimal_flops` enabled in Cargo. The edit hook honors local exemptions and gives a conditional correction.
- Only written expression lines trigger the multiply-add hook. The function-length hook still checks whole files.
- Unknown payloads, missing or unreadable configuration and internal errors pass the edit. An unbalanced multiply-add scan returns at most one `mul_add hook error: <type>: <message>` system message and exit 0. The function-length checker skips unclosed bodies but keeps complete earlier functions.
- One parser and scanner serve both agents. Scan every affected file before logging any block. A later scan error must leave no records for a block that was not returned.
- A block record proves a finding, not delivery. The append occurs before the response. A missing record does not prove the hook stayed silent because logging can fail.
- Apply only the target lint's `MachineApplicable` spans inside the workspace root. Do not apply `MaybeIncorrect` macro-body suggestions.
- Stop after at most five replays. Keep earlier edits after a later failure. After any successful round, format and rekey before the one final judgment.
- Keep JSON stdout separate. Complete both logs before returning. Keep the command's status even if `tee` fails. Preserve Bash 3.2 support.
- Save count lines once per applied round, in order. Measurement uses the last judging step per call as its verdict.
- Tests use temporary hook state, Codex home and buildlog state. They use stubs for Cargo and Codex. They never inspect a live hana tree. Python interfaces have no `Any` or file-level type ignores.

## Calibration / gotchas

### FMA code generation

On natedev's Ryzen 9 9950X with Rust 1.99.0, default x86-64 has no FMA target feature. Scalar `mul_add` calls Rust's linked-in `compiler_builtins` `fmaf` or `fma`. Runtime dispatch uses hardware FMA on this CPU. Indirect calls and lost vectorization add cost. With `x86-64-v3`, scalar code emits `vfmadd213ss` or its f64 equivalent. Slices use packed `vfmadd` on `ymm`. `znver5` also emits `zmm` vectors. The Mac's aarch64 target emits scalar `fmadd` and vector `fmla` by default.

The fused slice improved 74.6× for f32 and 38.9× for f64 with v3. A dependent chain held in registers improved about 2.5× for both. A `black_box` inside each chain step adds a stack round trip that hides call cost. Fused outputs were bit-identical across a million fixed-seed triples for default, v3 and znver5. Plain `a * b + c` still uses separate multiply and add instructions. Replacing it with `mul_add` changes rounding.

Count FMA references per function outside `compiler_builtins`. Its runtime implementation makes a whole-binary instruction count nonzero even in a default build. GOT loads followed by register-indirect calls can evade a name-only count. Debug builds can call `<f32>::mul_add` out of line. Inspect the callee too. Hana debug binaries changed from seven `fmaf`/`fma` references in four functions and no other FMA instructions to no such references and 80 FMA instructions in ten functions.

### Detector and rewrite limits

- **Line selection.** Lines end at `"\n"`. Patch input first converts CRLF to LF. `splitlines()` would also split Unicode separators inside Rust strings. Common lines between two changed parts of one replacement still count as written. The finding's source span supplies its last line because `expr` has no newlines.
- **Evidence limits.** The detector cannot infer types across files or expand macros. It can report vectors, points, colors, references and integers that have no evidence to exclude them. A name's last `let` wins anywhere in its function. Block scopes are not modelled. `.min(`, `.max(`, `.left`, `.size()` and generic type tokens can exclude scalar sums.
- **Exempt roots.** A copied file without `src/lib.rs` or `main.rs` can be exempt. `#![cfg_attr(c, no_std, allow(x))]` is not recognized as no_std when it is not the last argument.
- **Hook coverage.** Shell edits bypass the edit hooks. Formatting can grow a line after its edit. Clippy's rewrite loop provides a second chance when it runs.
- **Rounds.** Overlapping spans need later diagnostics on the changed tree. The five-round cap can leave work for the final judgment. A failed replay adds no count line. An applier failure can leave edits in files it wrote before the failure.
- **Replay status.** A complete replay can exit 101 under `-D warnings`. The applier still requires `build-finished`. A missing count log cannot distinguish no fixes from a failed save.
- **Log order.** Two `tee -a` writers can insert a JSON chunk inside a stderr line. Spooling stdout prevents this. Logs keep complete lines but do not keep the time order between streams. Named descriptor redirections and waiting on process-substitution PIDs do not work on Bash 3.2.
- **Measurement.** Read a call's full sequence before selecting its final judgment. The index can lag the log after a process exits. Do not measure a selected call until its completion record exists. Rank remaining causes by failed verdicts because warnings can repeat the same expression.
- **Cost and scaling.** Non-Rust edits avoid scanner imports. `test_many_findings_scan_scales_with_file_length` takes the minimum of five wall-time samples for 200 and 800 finding lines. Each sample checks the count and last line. The larger scan must take less than 10× the smaller. Minima reduce scheduling noise.

## Why

FMA avoids fallback calls and permits vectorization. `x86-64-v3` enables AVX2 and FMA without selecting one CPU model. Explicit `mul_add` makes fused rounding visible. The edit hook gives an early conditional correction. The lint loop uses Clippy's own automatic suggestions and checks the final tree. Shared parsing keeps Claude and Codex aligned. Lazy imports and small scanner classes limit per-edit cost. Separate count logs keep rewrite evidence when buildlog drops a successful step's output.

## Measured outcome

**Rewrite rounds, re-measure.** The window starts at **T_rounds = 2026-10-17 16:59:51 EDT (2026-10-17T20:59:51Z)**. It ends at **2026-10-18 16:59:51 EDT (2026-10-18T20:59:51Z)**. These times use America/New_York, UTC-04:00. The start is included. The end is excluded. The span is 24 hours.

| Measure | Rewrite rounds, re-measure | Limit / verdict | Source |
| --- | --- | --- | --- |
| Final clippy verdicts | **147 total; 42 failed; 0 failed with `suboptimal_flops`** | — | `measurements.json`: `verdict_steps`, `failed_verdicts`, `suboptimal_verdicts` |
| Target failures per 100 verdicts | **0 / 147 × 100 = 0.000** | ≤1.979: **PASS** | `failed_per_100`, `failed_verdict` |
| Sole-cause worker-hours per day | **(0 + 0) / 3,600 / 1 = 0.000**; no sole-cause failures | ≤0.100: **PASS** | `wall_seconds`, `gap_seconds`, `span_days`, `seat_verdict` |
| Same window, earlier per-step rule | **184 clippy steps; 76 failed; 26 with the target: 26 / 184 × 100 = 14.130** | Includes JSON replay diagnostics | `total_steps`, `failed_steps`, `per_step_suboptimal` |
| Complete selected call sequences | **185 clippy steps; 159 judging steps; 12 judgments with the target** | Includes a final judgment after the window end | `cohort_clippy_steps`, `judging_steps`, `judging_suboptimal` |
| JSON replay cost and result | **26 replays in 12 calls; 37 recorded fixes; 2,285.385 s added clippy time** | **3 calls** ended with a passing clippy verdict; those calls contain **6 replays** | `rewrite_steps`, `rewrite_calls`, `rewrite_fixes`, `rewrite_clippy_seconds`, `rewrite_passing_calls`, `rewrite_passing_verdicts` |
| Applied rounds and cap stops | **14 rounds: 10 calls with one round; 2 calls with two rounds; 0 cap stops; 0 unknown round counts** | All 12 count logs are available | `rewrite_rounds`, `rounds_per_call`, `cap_stops`, `rewrite_missing_count_logs` |

Both limits **PASS**. No final failed verdict contains the target lint. Thus, no remaining target cause has a failed verdict to rank (`commits.json`: empty `causes` and `diagnostic_groups`; `remaining_warnings` is empty). The other 42 failed verdicts do not contain this lint. There are no sole-cause call times or follow-up intervals to sample. The call-wall median, interval median and interval p75 are unavailable. Their sums are zero. These results support a proposal to retire the edit hook. The edit hook stays installed. The showrunner must decide on the proposal.

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

The source scripts and JSON are in `~/.local/state/mul-add-hook/lint-rounds/`. They use the per-call verdict rule below. A call enters the cohort when its first judging clippy step starts inside the window. The scripts read all steps in that call, including steps after the window ends. They use its last judging step as the verdict. JSON replay steps are not verdicts. A step with no call ID stands alone. Both the measurement and the audit reject a selected call with no completion record.

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

Sole-cause verdicts have `suboptimal_flops` as their only error lint. Worker cost sums their failed lint calls' wall times and accepted follow-up intervals. Each interval runs from the failed call's end to the next lint call's end. Divide the sum in seconds by 3,600 and the window's days to get worker-hours per day.

For call costs, use the same CTE and replace its final `SELECT` with this query. This query has a second pair of bounds. It also reads selected calls whose call start precedes the window. The next lint call must share worktree, worker and session. It must start inside the window. Its end must fall within 1,800 seconds after the failed call's end.

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

### Earlier measurements

The limits are 1.979 target failures per 100 verdicts and 0.100 sole-cause worker-hours per day. Earlier detector measurements counted clippy steps. The single-replay measurement first used the final judgment per call. Warning counts can repeat one expression, so causes use failed verdict counts. A dash means the source did not measure that figure.

| Window in EDT | Target failures per 100 | Sole-cause worker-hours per day | Rate / cost verdicts | Change measured |
| --- | ---: | ---: | --- | --- |
| 2026-10-02 12:09:00 to 2026-10-06 11:39:09 | 7.916 | 0.402 | FAIL / FAIL against the later limits | Baseline before the edit hook. |
| 2026-10-07 01:44:05 to 2026-10-11 01:44:05 | 8.126 | 0.576 | FAIL / FAIL | Hook with local float proof on both agents and machines. |
| 2026-10-07 01:44:05 to 2026-10-12 11:02 | 8.670 | — | FAIL / unavailable | Hook reach check before the wider detector. |
| 2026-10-12 14:13:42 to 2026-10-16 14:13:42 | 8.235 | 1.104 | FAIL / FAIL | Likely multiply-adds on written lines. |
| 2026-10-16 16:09:48 to 2026-10-17 16:09:48 | 4.032 | 0.448 | FAIL / FAIL | One JSON replay and apply per lint call. |

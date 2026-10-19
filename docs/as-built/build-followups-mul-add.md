# Fused multiply-add: lint rewrite

## What it is

`verify.sh lint` applies Clippy's automatic multiply-add fixes before its final judgment. It runs up to five rewrite rounds to reach inner sums and more workspace crates. This lint rewrite is the only multiply-add correction mechanism. No Claude or Codex edit runs a multiply-add hook.

The nightly Rust release check enables FMA for its Linux clippy build. Buildlog records a Codex worker's own thread as its session. The latest hana measurement passes both limits.

## How it works

### Files and contracts

| File | Role |
| --- | --- |
| `scripts/delegate/verify.sh` | Runs the lint judgments, rewrite rounds, formatting and cache rekey. |
| `scripts/lint/invoke.sh` | Runs and logs Cargo steps. Keeps JSON stdout separate from stderr. |
| `scripts/lint/apply_mul_add.py` | Applies only Clippy's automatic `suboptimal_flops` suggestions. |
| `scripts/buildlog/rust_release.py` | Sets the release trial's Linux clippy FMA flags. |
| `scripts/buildlog/record.py` | Supplies worker, delegate session and agent session for step and call records. |
| `scripts/delegate/test_verify_lint_scope.py` | Uses fake Cargo to check lint scope, rounds, counts, cache keys and output streams. |
| `scripts/lint/test_apply_mul_add.py` | Checks lint selection, byte edits, duplicates, overlaps and file bounds. |
| `scripts/buildlog/test_rust_release.py`, `test_record.py` | Check release flags and agent-session precedence with isolated inputs. |

### Edit checks

Claude's `Edit|MultiEdit|Write` PostToolUse group runs basedpyright and fn-length only. Codex `apply_patch` runs fn-length after the edit and berth before it. See [the function-length hook](build-followups-fn-length-hook.md#function-length-hook) for the edit parser, Rust scanner, Cargo lint reader and Codex installation rules.

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

- Keep `clippy::suboptimal_flops` enabled in Cargo.
- Apply only the target lint's `MachineApplicable` spans inside the workspace root. Do not apply `MaybeIncorrect` macro-body suggestions.
- Stop after at most five replays. Keep earlier edits after a later failure. After any successful round, format and rekey before the one final judgment.
- Keep JSON stdout separate. Complete both logs before returning. Keep the command's status even if `tee` fails. Preserve Bash 3.2 support.
- Save count lines once per applied round, in order. Measurement uses the last judging step per call as its verdict.
- Tests use temporary buildlog state and Cargo stubs. They never inspect a live hana tree. Python interfaces have no `Any` or file-level type ignores.

## Calibration / gotchas

### FMA code generation

On natedev's Ryzen 9 9950X with Rust 1.99.0, default x86-64 has no FMA target feature. Scalar `mul_add` calls Rust's linked-in `compiler_builtins` `fmaf` or `fma`. Runtime dispatch uses hardware FMA on this CPU. Indirect calls and lost vectorization add cost. With `x86-64-v3`, scalar code emits `vfmadd213ss` or its f64 equivalent. Slices use packed `vfmadd` on `ymm`. `znver5` also emits `zmm` vectors. The Mac's aarch64 target emits scalar `fmadd` and vector `fmla` by default.

The fused slice improved 74.6× for f32 and 38.9× for f64 with v3. A dependent chain held in registers improved about 2.5× for both. A `black_box` inside each chain step adds a stack round trip that hides call cost. Fused outputs were bit-identical across a million fixed-seed triples for default, v3 and znver5. Plain `a * b + c` still uses separate multiply and add instructions. Replacing it with `mul_add` changes rounding.

Count FMA references per function outside `compiler_builtins`. Its runtime implementation makes a whole-binary instruction count nonzero even in a default build. GOT loads followed by register-indirect calls can evade a name-only count. Debug builds can call `<f32>::mul_add` out of line. Inspect the callee too. Hana debug binaries changed from seven `fmaf`/`fma` references in four functions and no other FMA instructions to no such references and 80 FMA instructions in ten functions.

### Rewrite limits

- **Rounds.** Overlapping spans need later diagnostics on the changed tree. The five-round cap can leave work for the final judgment. A failed replay adds no count line. An applier failure can leave edits in files it wrote before the failure.
- **Replay status.** A complete replay can exit 101 under `-D warnings`. The applier still requires `build-finished`. A missing count log cannot distinguish no fixes from a failed save.
- **Log order.** Two `tee -a` writers can insert a JSON chunk inside a stderr line. Spooling stdout prevents this. Logs keep complete lines but do not keep the time order between streams. Named descriptor redirections and waiting on process-substitution PIDs do not work on Bash 3.2.
- **Measurement.** Read a call's full sequence before selecting its final judgment. The index can lag the log after a process exits. Do not measure a selected call until its completion record exists. Rank remaining causes by failed verdicts because warnings can repeat the same expression.

## Why

FMA avoids fallback calls and permits vectorization. `x86-64-v3` enables AVX2 and FMA without selecting one CPU model. Explicit `mul_add` makes fused rounding visible. The lint loop uses Clippy's own automatic suggestions and checks the final tree. Separate count logs keep rewrite evidence when buildlog drops a successful step's output.

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

Both limits **PASS**. No final failed verdict contains the target lint. Thus, no remaining target cause has a failed verdict to rank (`commits.json`: empty `causes` and `diagnostic_groups`; `remaining_warnings` is empty). The other 42 failed verdicts do not contain this lint. There are no sole-cause call times or follow-up intervals to sample. The call-wall median, interval median and interval p75 are unavailable. Their sums are zero. The re-measure passes both limits, so the edit hook is retired.

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

The final pipeline completed after every selected call had a completion record. **`audit.json` reports PASS**. It agrees on the selected verdict IDs, both limit verdicts, cost sums, rounds, fixes, replay times and cap stops. The synthetic checks report **PASS**. The measurement scripts report **0 errors, 0 warnings, 0 notes** under basedpyright.

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

# Test width: one hana test process per physical core, proved by a switchback trial

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Every nextest step that `invoke.sh` runs names its width (`--test-threads`), and the memory gate reserves per width. Then a 72-hour switchback trial alternates natedev between 16 (physical cores) and 32 (logical) in 100-minute blocks. A scorecard, frozen before the trial starts, then proves or disproves that memory wait in hana test steps falls by half without slower runs. The verdict keeps or reverts the width, and a 48-hour rollout check confirms it.

> **As-built disposition: create**

> **Production: build-followups** — unit `test-threads-unit-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-09 16:47 PDT

User, 2026-10-09 16:4x PDT, approving the nightly 2026-10-08 rust main proposal (~/.local/state/nightly-review/2026-10-08/rust.md: run hana tests at one nextest process per physical core, --test-threads 16 on natedev, Mac 12, so the memory gate's per-step reservation falls from 11.5 GiB): "i'm okay with implementing this as long as you put in a measurement regime - so it may require its own unit director to take care of this and then set up the measurement regime that proves or disproves whether it works"

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts every Claude session runs. This plan labels each nextest step's width, keys the memory gate by width, adds the trial scorecard `scripts/buildlog/width_trial.py`, runs the trial and acts on its verdict. Worktree `/home/natepiano/worktrees/claude-build-followups-test-threads-unit`, branch `build-followups-test-threads-unit` (unit `test-threads-unit-unit`); name both in every dispatch.
- **Project started:** 2026-10-10T00:02:20.467+00:00
- **Stack:** bash (`scripts/lint/invoke.sh` is sourced by `verify.sh`, the `lint` CLI and `pre_release_checks.sh` under `set -euo pipefail`; the Mac runs bash 3.2: no `EPOCHSECONDS`, no associative arrays, `${arr[@]+"${arr[@]}"}` for empty arrays); Python 3 standard library (`sqlite3`, `json`, `random`, `statistics`, `hashlib`, `argparse`, `unittest`), checked by basedpyright.
- **Layout:** `scripts/lint/` (`invoke.sh`, `memory_gate.sh`, `memory_admit.py`, their tests) · `scripts/buildlog/` (`store.py`, the new `width_trial.py` and `test_width_trial.py`) · `docs/as-built/build-memory-admission.md` (the gate's record) · outside the repo, read-only: `~/.local/state/buildlog/index.sqlite`, `~/.local/state/buildlog/admission/anon_peaks.jsonl`; edited at verdicts only: `~/.local/state/nightly-review/ledger.md` line 25.
- **Key files:**
  - `scripts/lint/invoke.sh` — header 1-11: the single policy layer; nothing outside `scripts/lint/` composes nextest policy flags. `run_once` 196-257 (memory gate, then the step; `BUILDLOG_START` is reset after the wait, so a step's `started_at` is after its memory wait). `run_nextest` 303-306: `require_nextest; run cargo nextest run "$@"`. `EPOCHREALTIME`/perl fallback in `buildlog_now` 83-90 is the bash 3.2 pattern.
  - `scripts/lint/memory_admit.py` — `Reservation` 59-68, `expected_peak(repo, step, host, now, root)` 133-166 (measured tier: ≥ 5 `anon_peaks.jsonl` records matching `(host, repo, step)` in `HISTORY_DAYS` 14, p90 via `percentile`; then the index tier × `ANON_SHARE`; then `FALLBACK` 12 GiB), `check` 233-268 (writes the reservation row; argv is passed on every poll, the profile is cached by `memory_gate.sh`), `release` 271-293 (appends `{host, repo, step, worktree, anon_peak_bytes, status, ended_at}`).
  - `scripts/lint/memory_gate.sh` — unchanged; passes the step's argv to `memory_admit.py check` on each poll.
  - `scripts/lint/test_memory_admit.py` — `add_measured` 58 and the expected-peak cases 76-100 are the fixture pattern. `scripts/lint/test_invoke_scope.py` 110-172 — sourcing `invoke.sh` in `bash -c 'source "$1"; …'` with stand-ins.
  - `scripts/buildlog/store.py` — `root()` 33 (`BUILDLOG_DIR` moves it, for tests), `INDEX_NAME` 23. Index table `steps` columns used: `host, started_at, ended_at, duration_s, mem_wait_s, step, argv (JSON list), repo, worktree_name, caller, call_id, status, finished_s (build seconds; None when no Finished line), tests_run`; table `tests`: `step_id, host, repo, status ('passed' | 'failed' | 'timed_out')`.
  - `scripts/delegate/verify.sh` — exports `BUILDLOG_CALL_ID` 575 (one per call, recorded as `steps.call_id`); `compose_nextest_args` 475-537; `test` arm 1086-1088 and `final` 1182 call `run_nextest`. Its pass key hashes `invoke.sh`, so every pass record misses once after each `invoke.sh` change.
  - Evidence, read-only: `~/.local/state/nightly-review/2026-10-08/rust.md` and `work/rust/{anon_by_kind,memwait,shape,reservation_sim}.py`.
- **Test lanes:** `scripts/lint/` → `test_memory_admit.py`, `test_nextest_width.py` (new) · `scripts/buildlog/` → `test_width_trial.py` (new). Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` and `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` (the production's merge tests) from the worktree root.
- **Lint:** `basedpyright <files>`: a pass ends `0 errors, 0 warnings, 0 notes` (it exits 3: `pyrightconfig.json` names a `.venv` no checkout has; `scripts/buildlog` already has `scripts/lint` on its `extraPaths`). `bash -n scripts/lint/invoke.sh`. Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Facts (checked 2026-10-09):**
  - natedev: 16 physical cores, 32 logical (`lscpu -p=core,socket`, `nproc`). The Mac: `hw.physicalcpu` 12, `hw.logicalcpu` 12, so both widths are 12 there and the Mac has no memory gate (no `/proc/meminfo`).
  - nextest: `-j, --test-threads <N>` is the test width (`--build-jobs` is cargo's); the CLI overrides `NEXTEST_TEST_THREADS` and the profile; hana's `.config/nextest.toml` sets none.
  - hana's whole-package `verify.sh test hana` argv is `cargo nextest run --no-fail-fast --workspace --bin hana -E package(hana)` (277 of 279 since 10-08; the other 2 are one `--test` target). Since 10-07: p50 exec (`duration_s − finished_s`) 211 s, sd of log 0.39, 48% fail (ordinary iteration). natedev runs 180–200 a day and about 1,450 hana nextest steps a day; memory wait in them was 30.8 h on 10-07 and 59.5 h on 10-08.
  - GitHub CI and hand-run `cargo nextest` do not go through `invoke.sh` and keep nextest's default width (the user's approval covers `invoke.sh` callers only).
- **Trial design (pre-registered; binds Phases 1, 3 and 4):**
  - **Arms:** `physical` (16 on natedev) and `logical` (32). A step's arm is the `--test-threads` value in its argv; a step without one is `unset` (before the trial).
  - **Assignment, a switchback:** block = ⌊epoch seconds / 6000⌋ (100 minutes); an even block runs `physical`, an odd one `logical`. A day holds 14.4 blocks, so a clock hour changes arm from day to day.
  - **Window W:** `[T0, T0 + 72 h)`. T0 is when the trial's commit reaches `~/.claude` main on natedev (`git -C ~/.claude reflog --date=iso-strict main`, the entry that brings it in). A step is in W when its request time, `started_at − mem_wait_s`, is.
  - **Population:** natedev, `step = 'nextest'`, arm `physical` or `logical`. **whole-hana** = `caller = 'verify'`, `repo = 'hana'`, the argv's `-E` value is exactly `package(hana)`, and the argv holds no `--test`.
  - **W, primary:** mean `mem_wait_s` per hana nextest step (any caller, any selection), leaving out steps whose request time falls in the first 900 s of its block (carry-over from the other arm; 900 s is the gate's wait limit). Ratio `physical / logical`.
  - **P, mechanism:** p50 anon peak of whole-hana runs, from `anon_peaks.jsonl` joined on `call_id` and `step`. Target: `physical` ≤ 6.5 GiB.
  - **R, mechanism:** the gate's own reservation, `memory_admit.expected_peak("hana", "nextest", "natedev", W's end, root, test_threads=16)` (and `=32`). Target: 16 ≤ 9.0 GiB.
  - **T, guard:** p50 of `duration_s − finished_s` of whole-hana runs with `finished_s` set. Ratio `physical / logical`.
  - **F, guard:** share of whole-hana runs with `status ≠ 0`. Difference in points.
  - **Reported without a rule:** timed-out tests per 100 whole-hana runs; mean memory wait per gated step over every natedev step; blocks, steps and whole-hana runs per arm.
  - **Intervals:** 90% percentile intervals from 2,000 block-bootstrap resamples (each arm's blocks drawn with replacement, same count), `random.Random(20261009)`.
  - **Minimum sample:** ≥ 60 whole-hana runs and ≥ 15 blocks per arm; when short at W's end, W extends by 24 h once, then reads as it stands.
  - **Rules:** T fails when its ratio > 1.11 and its interval's low end > 1.00. F fails when its difference > +5 points and its interval's low end > 0. W is `proven` when its ratio ≤ 0.50 and its interval's high end < 1.00; `partial` when the high end < 1.00 and the ratio > 0.50; `none` when the high end ≥ 1.00.
  - **Action:** no guard fails and W `proven` or `partial` → keep: `LINT_TEST_WIDTH=physical`. W `none` → revert: `LINT_TEST_WIDTH=logical` (labels stay, so later measures still work). A guard fails while W is `proven` or `partial` → the trade goes to the user through natedev with both numbers, and the trial runs on until they answer. P and R do not change the action; a keep with either unmet names the mismatch.
- **Invariants:**
  - **Pre-registration** (the user: "proves or disproves"). After T0 no phase changes a threshold, a window, a metric's definition or `width_trial.py`, except to fix a defect; a fix is stated in that phase's As-built with the scorecard's output before and after it.
  - The build log and `anon_peaks.jsonl` are evidence: this unit never edits or deletes a record in them. The proposal's "remove the hana records at install" is replaced by the per-width key (my call: it keeps the baseline and gives each arm an exact reservation; reverting is one Spec line).
  - The caller's width wins: a `--test-threads`, `--test-threads=…`, `-j`, `-j…` in the arguments, or `NEXTEST_TEST_THREADS` in the environment, means `invoke.sh` adds nothing.
  - Width selection never fails, prints or changes a status: a core count it cannot read adds no flag.
  - Tests never read the live `~/.local/state/buildlog` (`BUILDLOG_DIR` points at a temp dir) and never run real `cargo`.
  - Python: no `Any`, a `TypedDict` per JSON shape, no file-level ignores.
  - Times shown to the user are America/New_York (the user, 2026-10-12; America/Los_Angeles before that); the scorecard prints PDT, and build-log stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 3 | T0 + 72 h (+ 24 h if the sample is short) | the clock, after the showrunner has fast-forwarded Phase 2 to `~/.claude` main |

While G1 holds, each time the unit director wakes it runs `width_trial.py report --since <T0> --until <now>` (seconds) and messages natedev only when T or F fails by the Rules with at least 30 whole-hana runs per arm.

## Phases

### Phase 1 — The gate's records name their width and call, and the scorecard is frozen  · status: done

#### As-built

- `memory_admit.test_threads(argv) -> int | None` reads `--test-threads N`, `--test-threads=N`, `-j N`, `-jN`; absent or non-integer (`num-cpus`) is `None`.
- `expected_peak(repo, step, host, now, root, test_threads=None)` matches width exactly in both tiers (measured: `anon_peaks.jsonl` records with equal `test_threads`, unlabeled records matching `None`; index: steps whose argv width is equal) and reads only history before `now` (measured `ended_at < now`, index `started_at < now`), so R reads the same at any later time. `check` passes `test_threads(argv)`.
- `check` writes `test_threads` and `call_id` (`BUILDLOG_CALL_ID`, else `None`) into each reservation row; `release` copies both into the `anon_peaks.jsonl` record. The gate's printed line is unchanged.
- `width_trial.py report --since --until` (ISO-8601 with offset; until accepts `now`) prints the pre-registered scorecard: arms `physical` (16) / `logical` (32) / `unset` (any other width or none); `BLOCK_S` 6000 with a 900 s washout applied to W only; W/P/R/T/F with 90% block-bootstrap intervals (2,000 resamples, seed 20261009); the sample line; reported-only rows (timed-out per 100, all-steps gated wait); verdict `keep` / `revert` / `trade-off` / `no verdict`. The header carries the script's own sha256 (first 12 hex). Exit 0 on a report, 2 on a usage error.
- `Metric = ComparedMetric(physical, logical, comparison, interval) | UncomparedMetric(physical, logical, reason)`, reason one of needs both arms, zero denominator, no resamples. A one-arm window prints `—` where a value needs the other arm and `verdict: no verdict — needs both arms`.
- The sample minimum (60 whole-hana runs, 15 blocks) counts only the physical and logical arms when either is present. W and the all-steps wait skip NULL `mem_wait_s`; the all-steps row counts only gated steps (`cargo [+tc] fmt` and `*/sweep.py` exempt, as in `invoke.sh`). P pairs each attempt of a call with its own anon peak by end time when the counts match; a retried call missing a peak gives no P for any attempt.

**Files:**
- `scripts/lint/memory_admit.py` — width parsing, per-width `expected_peak` bounded before `now`, `test_threads`/`call_id` in reservations and anon peaks.
- `scripts/buildlog/width_trial.py` — the frozen scorecard.
- `scripts/lint/test_memory_admit.py`, `scripts/buildlog/test_width_trial.py` — tests for both.
- `docs/as-built/build-memory-admission.md` — `need` and release name width matching and the two record fields.

**Binds later work:**
- Pre-registration sha256 that the readout checks: `width_trial.py` a35ac69482d24493fe2ad5a9e0e975376c5de4dc92650cccf47bb613b2282fc9, `memory_admit.py` 1ecd402ab4ee4f0d5df19716bb406d17aa0cf1e4864040faad67f623e95a486b.
- Baseline the readout compares against: window 2026-10-07 00:00 – 2026-10-09 17:33 PDT, unset arm only: 39 blocks, 3,551 hana steps, 344 whole-hana runs; W 101.8 s, P —, R 11.51 GiB, T 211 s, F 48.5%, timed-out 5.2 per 100, all-steps gated wait 53.4 s; no verdict.
- `test_threads(argv)` keys each width's reservation, so the trial's width labels give each arm its own measured history, which the rollout check reads for R; `BLOCK_S` equals `invoke.sh`'s switchback block.

**Gotchas:**
- `verify.sh` retries a memory-killed step once under the same `BUILDLOG_CALL_ID`, so `call_id` alone does not identify one attempt.
- `invoke.sh`'s memory-gate exempt set and `width_trial._uses_memory_gate` must stay in step.

**Ruled out:** removing hana records at install — the per-width key keeps the baseline and gives each arm its own reservation.

### Phase 2 — The trial starts: each nextest step runs at 16 or 32 by block  · status: done

#### As-built

- `invoke.sh` holds the mode line `LINT_TEST_WIDTH=trial` (values `trial`, `physical`, `logical`) beside `run_nextest`; its comment names the modes and `width_trial.py`'s `BLOCK_S` as the block length's twin.
- `nextest_physical_cores [sysfs_root]` sets `LINT_PHYSICAL_CORES` once per shell: on Linux the count of distinct `physical_package_id:core_id` pairs under `${1:-/sys/devices/system/cpu}/cpu[0-9]*/topology/`, read with `read`; on darwin `sysctl -n hw.physicalcpu`. `nextest_logical_cores` sets `LINT_LOGICAL_CORES` once from `getconf _NPROCESSORS_ONLN`. A value that is not a positive integer stays empty.
- `nextest_width_arm EPOCH` sets `LINT_TEST_WIDTH_ARM` to `physical` when `(EPOCH / 6000) % 2 == 0`, else `logical`, and prints nothing.
- `run_nextest` runs `cargo nextest run --test-threads <N> "$@"`, N from the mode's arm (`trial` reads `${EPOCHSECONDS:-$(date +%s)}`). It adds nothing when `NEXTEST_TEST_THREADS` is set, when an argument before `--` is `--test-threads`, `--test-threads=*`, `-j` or `-j*`, or when the core count is unreadable, and the status passes through unchanged. Arguments after `--` belong to the test binaries and never suppress the width.
- natedev's arms are 16 and 32; the Mac reads 12 for both, and `bash -n` passes under its bash 3.2.

**Files:**
- `scripts/lint/invoke.sh` — the mode line, the width functions and `run_nextest`.
- `scripts/lint/test_nextest_width.py` — core counts and their cache, arm boundaries, trial parity, fixed modes, caller overrides, `--` handling, status passthrough.
- `scripts/delegate/test_verify_mac_offload.py` — the local run's argv carries the width pair.

**Binds later work:**
- The verdict changes only the value on the mode line; `test_nextest_width.py` sets the mode in every case, so it passes under any default.
- `memory_admit.test_threads(argv)` reads the first width in argv, so the width `invoke.sh` adds before the caller's arguments is the one each reservation, anon-peak record and the scorecard see.
- T0 is when this phase's checkpoint reaches `~/.claude` main: the reflog entry that brings it in.

**Gotchas:**
- `~/rust/rust-template` is a cargo-generate template with no `Cargo.toml`; a live width check runs on a real crate such as `~/rust/cargo-liner` (at an even block it ran `--test-threads 16`, 4,679 tests passed, and its anon-peak record carried `test_threads` 16).
- A step run outside `verify.sh` records `call_id` None; whole-hana runs all come through `verify.sh`.

**Ruled out:** reading the last width in argv in `memory_admit` or `width_trial` — the first-width rule already reads `invoke.sh`'s flag and keeps the frozen hashes.

### Phase 3 — Readout: the verdict, and the width it sets  · status: done

#### As-built

- The 72 h trial's pre-registered scorecard over W = `2026-10-09T18:05:32-07:00` – `2026-10-12T18:05:32-07:00` (2026-10-09 18:05 PDT – 2026-10-12 18:05 PDT; 2026-10-09 21:05 EDT – 2026-10-12 21:05 EDT) returns `verdict: revert — W none`; sample ok, no extension.
- Action rule: W `none` → revert → `scripts/lint/invoke.sh` holds `LINT_TEST_WIDTH=logical`. Every natedev nextest step runs `--test-threads <logical core count>` (32, nextest's own default) and records width `logical`; `test_nextest_width.py` pins each mode. G2 and the rollout check are dropped. The width functions and the `trial`/`physical` modes remain, so switching back is one value.
- Hashes: `memory_admit.py` on `~/.claude` main matches `1ecd402a…`; `width_trial.py` on main is `e80d583f…`, not the frozen `a35ac694…`. Main's script (commit 397d3125, a display-zone change: `_clock` reads `user_zone()`) prints identical output apart from the header line; the verdict is read from the frozen script.
- Ledger line 25 of `~/.local/state/nightly-review/ledger.md` carries `; trial 2026-10-09–2026-10-12: revert, W 1.05 (0.53–2.27), P 7.06 GiB, R 9.80 GiB, T 1.13, F −7.0pt; logical (~/.claude 9cd72746)`; natedev has the verdict line and table.

Frozen script output (`width_trial.py report --since 2026-10-09T18:05:32-07:00 --until 2026-10-12T18:05:32-07:00`):
```
Width trial natedev 2026-10-09 18:05 PDT – 2026-10-12 18:05 PDT (72.0 h) · width_trial.py sha256 a35ac69482d2
arm        blocks  hana steps  whole-hana runs
physical       22       1,393              114
logical        22       1,451              103
sample: ok
metric                                        physical    logical     ratio    90% interval rule                          result
W memory wait per hana test step (s)              19.2       18.2      1.05       0.53–2.27 ≤ 0.50, high < 1.00           none
P whole-hana anon peak p50 (GiB)                  7.06       8.69      0.81       0.75–0.91 physical ≤ 6.50               unmet
R gate reservation, hana nextest (GiB)            9.80       9.70         —               — physical ≤ 9.00               unmet
T whole-hana exec p50 (s)                          372        330      1.13       1.00–1.21 fails > 1.11 and low > 1      pass
F whole-hana failed runs (%)                      38.6       45.6    −7.0pt     −17.9–5.1pt fails > +5pt and low > 0      pass
  timed-out tests per 100 whole-hana runs          0.0        0.0
  memory wait per gated step, all natedev         13.3       11.6
verdict: revert — W none
```
Main's script header line: `Width trial natedev 2026-10-09 21:05 EDT – 2026-10-12 21:05 EDT (72.0 h) · width_trial.py sha256 e80d583f28d1`.

In-window merges to `~/.claude` main touching `verify.sh`, `invoke.sh`, `memory_gate.sh` or `record.py` (none edits the hashed files or the width code; routing changes move how many runs reach natedev, for both arms alike):
- `44584b49` 2026-10-10T14:20:53-07:00 — Mac-first routing in `verify.sh`, one field in `record.py`.
- `1fc62564` 2026-10-11T18:19:36-07:00 — `build_hold_mark` credits the owning session.
- `0fc140c9` 2026-10-11T22:17:24-07:00 — a local `verify.sh` step waits out a natedev build hold, recorded only as `BUILDLOG_HOLD_WAIT_S`; `run_nextest` waits before it picks its width.
- `87444ee6` 2026-10-12T04:37:21-07:00 — a test goes to the first free target.

Build holds inside W stay in the readout (they reach both arms): hana's on 2026-10-11; tile-fixes' for about 20 min from 2026-10-12T11:37-07:00; hana's from 2026-10-12T13:42-07:00.

**Files:**
- `scripts/lint/invoke.sh` — the nextest width mode line, `LINT_TEST_WIDTH=logical`, and its comment naming the three modes (`trial` alternates physical and logical by block; `physical` or `logical` fixes the width) and `BLOCK_S` in `width_trial.py` as the block length's twin.

**Binds later work:** `invoke.sh` runs `logical`; the rollout check and G2 are gone; the hash freeze on `width_trial.py` and `memory_admit.py` ended with the readout.

**Gotchas:**
- T read as failing by the Rules at the 64.2 h check (2026-10-12T10:19-07:00) and passed at 72 h (low end exactly 1.00); interim reads of a pre-registered trial move.
- P and R stay unmet under physical: 16 threads cut the whole-hana anon peak p50 by about a fifth but do not lower the gate's reservation, which follows the gate's own recorded peaks.

**Ruled out:**
- Keeping 16 threads: no memory-wait gain (W ratio 1.05, interval 0.53–2.27) to pay for 13% slower whole-hana runs.
- Reading the verdict from main's changed script: the frozen script is the pre-registered instrument.

### Phase 4 — The local lint covers what CI's Clippy job covers  · status: done

#### Work Order

**Goal:** `verify.sh lint` runs clippy with the targets and features CI's Clippy job uses, so code behind a non-default feature, a bench and an example fail locally before CI, and the cost on hana is measured before and after.

**Spec:**
- In the `lint` arm of `scripts/delegate/verify.sh`, the clippy stage becomes `invoke_clippy --workspace --all-targets --all-features`. It takes no `FEATURE_FLAGS`: `--all-features` covers a trailing `--features`, which still reaches mend and rustdoc.
- This is CI's command. Job `clippy` in `~/rust/hana/.github/workflows/ci.yml` runs `cargo clippy --workspace --all-targets --all-features -- -D warnings`, the other repos' workflows hold the same line, and `scripts/release/pre_release_checks.sh` already runs it at the push gate, so the dev loop and the push gate share one all-features set of check artifacts. CI's second pass, for the Windows target, stays CI's alone.
- The `lint` arm also runs `taplo fmt --check` from the workspace root, the check form of the step `scripts/validate_and_push/validate_ci.sh` runs (`run_autofix_step "taplo" taplo fmt`), so taplo loads the same workspace `taplo.toml`; an unformatted `.toml` fails lint and the output names the file. Source: the showrunner, 2026-10-12 (reported by the cargo-liner showrunner): a unit's `.toml` edits pass lint and are reformatted into a separate fix commit at merge, as 933ab2ce did to cargo-tile's theme files. Tests pin it like the clippy stage: a stand-in `taplo` records its argv and working directory, and a failing stand-in fails `lint`.
- `check`, `test`, `example`, `final`, mend, fmt and rustdoc keep their scope. The `example` verb stays: one example, while iterating.
- Comments and usage in `verify.sh` say what is true after the change: the header (the dev-loop target pinning, `Nothing below final uses --all-targets`, `with default features`), the `lint` and `--features` usage lines, the comment in `compose_nextest_args` on what the lint half compiles under clippy, and the `example` arm's `lint never sees an example`.
- Tests run the real `verify.sh` with a stand-in `cargo` that answers `cargo metadata` from synthetic JSON and records each call's argv (the pattern in `scripts/delegate/test_verify_mac_offload.py`; `test_verify_release_ack.py` stubs the lint-config reader and silences skip notices, so it does not fit), and the real lint-config reader over a temporary `lint.conf`: `lint <pkg>` gives clippy exactly `--workspace --all-targets --all-features -- -D warnings`; `lint <pkg> --features a` gives clippy the same argv and gives mend and rustdoc the qualified `--features`; `example` argv is unchanged; with clippy off in `lint.conf` the skip line names the new command.
- The unit director measures, after the seats finish; no seat runs this. In a detached scratch worktree of `~/rust/hana`, removed after:
  - At hana's main tip, `verify.sh lint hana --no-cache` with `~/.claude` main's script (before) and with this branch's script (after); every measured run passes `--no-cache`, because touching a file leaves the content-based pass key unchanged and a cached pass replays without running clippy. For each: one run, then one more after touching one source file under `crates/hana` (the dev-loop case). The after script's first run also pays the one-time all-features dependency check; report it apart.
  - At `af5958758eebabca33a6123bfbfb38809be0c29b` (hana `init/catalyst`, CI run 38306974911): the before script passes and the after script fails on the missing `#[must_use]` in hana_diegetic's `bench_support`.
  - Per run: the `clippy` step's `duration_s`, `finished_s` and `mem_wait_s` from the build log, its anon peak from `anon_peaks.jsonl` by `call_id`, and the gate's reservation `memory_admit.expected_peak("hana", "clippy", "natedev", …)` before and after. The table goes in the As-built and the checkpoint notice.
- A touched-file lint that takes more than twice as long, or an anon peak more than 2 GiB higher, goes to natedev with the table before the checkpoint, beside the narrower choice: the dev-loop lint unchanged and CI's clippy run once per checkpoint by the unit director (natedev confirmed the two limits and the scratch worktree, 2026-10-12).

**Files:**
- `scripts/delegate/verify.sh` — the clippy stage, the comments and the usage text.
- `scripts/delegate/test_verify_lint_scope.py` — the tests, a new file.
- `docs/plans/build-followups-test-threads-unit.md` — the As-built.

**Seats:** `1 writer + 1 tester` — `impl` owns `verify.sh`; `test` owns `test_verify_lint_scope.py`.

**Constraints from prior phases:** Source: natedev, 2026-10-12, from hana: CI run 38306974911 failed Clippy on code behind the non-default feature `bench_support`, which `lint`'s `--workspace --lib --bins --tests` with default features never compiles. Phase 3: merge `build-followups` into the branch first; build-report's phases 2, 3, 5 and 10 changed `verify.sh`. Phase 3's verdict was revert: `invoke.sh` runs nextest at `logical` width, the rollout phase is dropped, and the trial's hash freeze on `memory_admit.py` and `width_trial.py` ended with that readout. This phase edits neither: the clippy reservation key is unchanged, so the gate learns the wider peak from its own records. hana's features that reach dependencies under `--all-features`: `hana/hotpatching` (`bevy/hotpatching`), `tween` (`dep:bevy_tween`), `monitor-probe` (`dep:tracing`), `hana_video/test` (`zbus/bus-impl`, `zbus/p2p`); the first means a second check of the bevy tree. `verify.sh`'s pass key hashes these scripts, so every recorded lint pass misses once after the change. `verify.sh` waits out a natedev build hold by itself. Delegation Context's line numbers in `verify.sh` and `invoke.sh` predate build-report's changes and this plan's width code (`run_nextest` now picks the width before it calls nextest); find each place by name. The same gap outside this phase, sent to natedev: `scripts/lint/lint clippy` (`/clippy`, the fix pipeline) passes `--all-targets` with the caller's features, and `verify.sh test` runs default features where CI's nextest runs `--all-features`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_verify_lint_scope.py'` and `python3 -m unittest discover -s scripts/delegate -p 'test_*.py'` green; basedpyright clean on the new file; `bash -n scripts/delegate/verify.sh`; the after script passes on hana's main tip and fails on `af59587` as the Spec says; the As-built holds the before and after table.

# Nextest width and lint scope

## What it is

Two policies in the cargo layer that every session's builds go through. First, every `cargo nextest run` that `scripts/lint/invoke.sh` starts names its test width (`--test-threads N`), chosen by one mode line and divided among the local runs in flight. The memory gate keys its measured reservation by that width, and `scripts/buildlog/width_trial.py` scores a switchback trial between one test process per physical core and one per logical core. Width `logical` is the setting: a 72-hour trial found no memory-wait gain at physical width and slower hana runs. Second, `verify.sh lint` uses CI's clippy target scope (`--workspace --all-targets --all-features`). It adds `--keep-going` and can repair multiply-add warnings before its final judgment. It also checks TOML formatting with taplo.

## How it works

### Nextest width (`scripts/lint/invoke.sh`)

`LINT_TEST_WIDTH` sits beside `run_nextest` and takes one of three values:

| Mode | Width |
| --- | --- |
| `trial` | alternates by 100-minute block: `physical` in an even block, `logical` in an odd one |
| `physical` | physical core count |
| `logical` (current) | logical core count: 32 on natedev, nextest's own default there |

- `nextest_physical_cores [sysfs_root]` sets `LINT_PHYSICAL_CORES` once per shell. On Linux it counts distinct `physical_package_id:core_id` pairs under `${1:-/sys/devices/system/cpu}/cpu[0-9]*/topology/`; on darwin it reads `sysctl -n hw.physicalcpu`. A value that is not a positive integer leaves it empty.
- `nextest_logical_cores` sets `LINT_LOGICAL_CORES` once from `getconf _NPROCESSORS_ONLN`, with the same rule.
- `nextest_width_arm EPOCH` sets `LINT_TEST_WIDTH_ARM` to `physical` when `(EPOCH / 6000) % 2 == 0`, else `logical`. `trial` mode passes `${EPOCHSECONDS:-$(date +%s)}`.
- `run_nextest` runs `require_nextest`, waits out any build hold (`build_hold_wait_local`), then picks the width and runs `cargo nextest run --test-threads <N> "$@"`. It adds no flag when `NEXTEST_TEST_THREADS` is set, when an argument before `--` is `--test-threads`, `--test-threads=*`, `-j` or `-j*`, or when the core count is unreadable. Arguments after `--` belong to the test binaries and never suppress the width. The exit status passes through unchanged.

### Shared width (`scripts/lint/nextest_budget.py`)

In `physical` and `logical` mode, local runs share the mode's width. `run_nextest_shared WIDTH ARGS...` opens fd 9 on `<name>.new` in `${BUILDLOG_DIR:-~/.local/state/buildlog}/admission/nextest/` and runs `nextest_budget.py claim DIR NAME WIDTH 9`. Under the directory's `lock`, the claim flocks fd 9, renames the marker to `<name>.run`, counts the `.run` markers still locked (itself included), unlinks the unlocked ones, and prints `shared_width(full, runs) = min(full, max(FLOOR, full // runs))`, `FLOOR` 4. The run then goes through `run` with `--test-threads <shared>` and fd 9 closed for the step, and the marker is removed after it.

- The lock belongs to the file the shell opened, so it ends with the shell: a killed run's marker is unlocked, and the next claim neither counts it nor keeps it. A `.new` older than `PENDING_GRACE_S` (600 s) is a shell killed before it claimed.
- On natedev: alone 32, 2 runs 16, 3 runs 10, 4 runs 8, 8 or more 4. On the Mac (12): alone 12, 2 runs 6, 3 or more 4.
- A width is fixed when the run starts; runs that start later do not narrow it, and a run that outlives the others does not widen.
- Any failure (no directory, the helper failing or printing a non-number) runs at the full width.
- `trial` mode is not shared: `width_trial.py` names the arm by width (16 physical, 32 logical), and a shared 16 would read as physical.

natedev has 16 physical and 32 logical cores. The Mac reads 12 for both and has no memory gate. GitHub CI and hand-run `cargo nextest` do not go through `invoke.sh` and keep nextest's default width.

A step records its width through its argv: `invoke.sh` puts `--test-threads N` before the caller's arguments, so the first width in argv is the one the build log's `steps.argv`, the memory gate's reservation row and the `anon_peaks.jsonl` record all carry. A shared run records the width it ran at.

### Per-width reservation (`scripts/lint/memory_admit.py`)

- `test_threads(argv) -> int | None` returns the first width in argv, from `--test-threads N`, `--test-threads=N`, `-j N` or `-jN`; absent or non-integer (`num-cpus`) is `None`.
- `expected_peak(repo, step, host, now, root, test_threads=None)` reads only history before `now`, so a later call for a past `now` returns the same value. Its measured tier (≥ 5 `anon_peaks.jsonl` records in `HISTORY_DAYS` 14, p90) matches `test_threads` exactly: a width-16 step reads width-16 records, an unlabeled step reads unlabeled records. Its index tier (build-log `peak_mem_bytes` × `ANON_SHARE` 0.65) and `FALLBACK` (12 GiB) do not look at width.
- `check` passes `test_threads(argv)` to `expected_peak` and writes `test_threads` and `call_id` (`BUILDLOG_CALL_ID`, else `None`) into each reservation row; `release` copies both into the `anon_peaks.jsonl` record.

### Trial scorecard (`scripts/buildlog/width_trial.py`)

`width_trial.py report --since <ISO-8601 with offset> --until <ISO-8601 | now>` prints the switchback scorecard for natedev. Exit 0 on a report, 2 on a usage error.

- `load_steps` reads `step = 'nextest'` rows from the build-log index (`store.root() / INDEX_NAME`, moved by `BUILDLOG_DIR`) and selects them by request time, `started_at − mem_wait_s`. A step's arm is `test_threads(argv)`: 16 is `physical`, 32 `logical`, none `unset`, any other width its number. Its block is `request // BLOCK_S` (6000 s).
- **whole-hana** run: `caller = 'verify'`, `repo = 'hana'`, the `-E` value is exactly `package(hana)`, and no `--test` in argv.
- Metrics, `physical` against `logical`:

| Metric | Definition | Rule |
| --- | --- | --- |
| W | mean `mem_wait_s` per hana nextest step, any caller; steps requested in a block's first 900 s (`WASHOUT_S`) left out; NULL waits skipped | `proven` at ratio ≤ 0.50 and interval high < 1.00; `partial` at high < 1.00; `none` at high ≥ 1.00 |
| P | p50 anon peak of whole-hana runs, from `anon_peaks.jsonl` joined on `call_id` | target physical ≤ 6.5 GiB, no effect on the verdict |
| R | `expected_peak("hana", "nextest", host, until, root, width)` for 16 and 32 | target physical ≤ 9.0 GiB, no effect on the verdict |
| T | p50 of `duration_s − finished_s` over whole-hana runs with `finished_s` set | fails at ratio > 1.11 and interval low > 1.00 |
| F | share of whole-hana runs with `status ≠ 0`, difference in points | fails at difference > +5 and interval low > 0 |

- Reported without a rule: timed-out tests per 100 whole-hana runs (`tests.status = 'timed_out'`), and mean memory wait per gated step over every natedev step, each block assigned the arm most of its nextest steps ran.
- Intervals: 90% percentile intervals from 2,000 block-bootstrap resamples (each arm's blocks drawn with replacement, same count), `random.Random(20261009)`.
- Sample: ≥ 60 whole-hana runs (`MIN_RUNS`) and ≥ 15 blocks (`MIN_BLOCKS`) per arm, counting only `physical` and `logical` when either is present.
- `verdict(w, t, f)`: W `none` → `revert`; W `proven`/`partial` with T or F failing → `trade-off`; otherwise `keep`. A metric that cannot compare (`UncomparedMetric`, reason `needs both arms`, `zero denominator` or `no resamples`) gives `no verdict`, and the row prints `—` where a value needs the other arm.
- P pairs each attempt of a call with its own anon peak by end time when the counts match; a retried call missing a peak gives no P for any of its attempts.
- The header carries the script's own sha256 (first 12 hex) and the window in the user's zone (`user_zone()`).

### CI-scope lint (`scripts/delegate/verify.sh`, `lint` arm)

Order: `require_taplo`, mend `--fix`, nightly fmt of the package and changed members, taplo, clippy, rustdoc on changed members.

- Clippy: `invoke_clippy --workspace --all-targets --all-features --keep-going` (`-- -D warnings` from `invoke.sh`). The target scope matches CI's `clippy` job and `scripts/release/pre_release_checks.sh`. The lint call sites add `--keep-going`; `invoke.sh` does not add it. Clippy takes no `FEATURE_FLAGS`; a trailing `--features` reaches mend and rustdoc only. CI's Windows-target clippy pass has no local counterpart. `check`, `test`, `example`, `final`, mend, fmt and rustdoc keep their own scope.
- A failed clippy judgment with `#suboptimal_flops` starts up to five JSON replay and apply rounds. The applier takes only that lint's `MachineApplicable` suggestions. After at least one applied round, lint formats once, updates the cache key and judges again. Earlier edits stay if a later round fails. See [Lint-step rewrite](build-followups-mul-add.md#lint-step-rewrite) for the stop rules and count logs.
- Taplo: `run taplo fmt --check` from `workspace_root` (from `cargo metadata`), so taplo loads the workspace `taplo.toml` whatever directory lint starts in. `config/lint.conf` does not gate it; it gates only the cargo stages. An unformatted `.toml` fails lint, names the file, and is cached like any lint failure.
- `require_taplo` exits 2 with `taplo is required` before any stage when taplo is not on `PATH`. A taplo panic (exit 101) exits 3 on macOS, with a message to re-run unsandboxed, and 2 elsewhere.
- `buildlog_step_compiles` in `invoke.sh` returns 1 for `taplo` (as for `cargo [+tc] fmt` and `*/sweep.py`), so taplo skips the build-hold wait and the memory gate.

## Invariants

- `invoke.sh` is the only place nextest policy flags are composed; callers own scope.
- The caller's width wins: a width flag before `--`, or `NEXTEST_TEST_THREADS`, means `invoke.sh` adds nothing. Such a run takes no marker and is not counted by others.
- No counter: a run is in flight while its marker is locked, and nothing a killed run leaves behind is counted.
- Width selection never fails, prints or changes a status; an unreadable core count adds no flag, and a failed claim runs at the full width.
- `invoke.sh` adds its width before the caller's arguments, and `memory_admit.test_threads` and `width_trial` read the first width in argv. Change both or neither.
- The trial block in `nextest_width_arm` (6000 s) equals `BLOCK_S` in `width_trial.py`.
- `invoke.sh`'s ungated set (`buildlog_step_compiles`) and `width_trial._uses_memory_gate` decide which steps count as gated; they must agree. Both exempt `cargo [+tc] fmt`, `*/sweep.py` and `taplo`, so none of those counts in the all-steps gated-wait row.
- The build log and `anon_peaks.jsonl` are evidence: nothing edits or deletes their records. Per-width keys replace any need to drop stale records.
- `verify.sh lint`'s clippy target scope equals CI's `clippy` job and `pre_release_checks.sh`; change all three together. Its first judgment, JSON replays and final judgment all use `--keep-going`.
- Tooling failures (exit 2, exit 3, killed runs) are never cached; only a lint failure of the tree is.
- `invoke.sh` must parse under the Mac's bash 3.2 (no `EPOCHSECONDS`, no associative arrays, `${arr[@]+"${arr[@]}"}` for empty arrays).
- Tests never read the live `~/.local/state/buildlog` (`BUILDLOG_DIR` points at a temp dir) and never run real `cargo`. `test_nextest_width.py` sets the mode in every case, so it passes under any `LINT_TEST_WIDTH` default.

## Calibration / gotchas

- Switching width is one value: `LINT_TEST_WIDTH`. The width functions and the `trial`/`physical` modes stay in place for that.
- `verify.sh` retries a memory-killed step once under the same `BUILDLOG_CALL_ID`, so `call_id` alone does not identify one attempt.
- A step run outside `verify.sh` records `call_id` None; whole-hana runs all come through `verify.sh`.
- `verify.sh`'s pass key hashes content and these scripts: touching a file replays a cached pass, so a timing run passes `--no-cache`, and every pass record misses once after an `invoke.sh` change.
- Under `--all-features` hana pulls `bevy/hotpatching`, so the first lint in a target directory checks the bevy tree a second time (159 s against 128 s on hana main).
- Any `verify.sh` test fixture that runs `lint` needs `workspace_root` in its `cargo metadata` answer and a `taplo` on `PATH`.
- `~/rust/rust-template` is a cargo-generate template with no `Cargo.toml`; a live width check runs on a real crate such as `~/rust/cargo-liner`.
- Interim reads of the scorecard move: T read as failing at 64 h of the 72 h trial and passed at 72 h (low end exactly 1.00).
- Physical width cuts the whole-hana anon peak p50 by about a fifth but does not lower the gate's reservation, which follows the gate's own recorded peaks.

## Why

**Width stays `logical`.** The user approved one test process per physical core (16 on natedev) on condition that a measurement prove or disprove it. The trial ran 2026-10-09 18:05 – 2026-10-12 18:05 PDT, with every threshold fixed before it started and the scorecard's hash checked at the readout:

| Metric | physical | logical | ratio | 90% interval | result |
| --- | --- | --- | --- | --- | --- |
| W memory wait per hana test step (s) | 19.2 | 18.2 | 1.05 | 0.53–2.27 | none |
| P whole-hana anon peak p50 (GiB) | 7.06 | 8.69 | 0.81 | 0.75–0.91 | unmet |
| R gate reservation (GiB) | 9.80 | 9.70 | — | — | unmet |
| T whole-hana exec p50 (s) | 372 | 330 | 1.13 | 1.00–1.21 | pass |
| F whole-hana failed runs (%) | 38.6 | 45.6 | −7.0 pt | −17.9–5.1 pt | pass |

Sample: 22 blocks per arm; 114 and 103 whole-hana runs. Verdict `revert`: W shows no memory-wait gain, and whole-hana runs are 13% slower at 16 threads. The widths stay labeled so a later measure can read them.

The per-width key replaced deleting hana's old anon-peak records: it keeps the baseline and gives each width its own measured reservation.

**Lint runs CI's clippy.** A narrower local clippy (`--lib --bins --tests`, default features) passed on trees CI's Clippy job failed, for example a missing `#[must_use]` in hana_diegetic's `bench_support`. Measured on hana, `verify.sh lint hana --no-cache`, clippy step, memory wait 0 in every run:

| Run | Narrow scope (s) | CI scope (s) | Anon peak (GiB) |
| --- | --- | --- | --- |
| first lint | 127.8 | 159.2 | 4.00 → 3.98 |
| touched file | 15.2 | 52.8 | 2.84 → 2.77 |

The touched-file lint is 3.5× slower (37 s more), with no higher anon peak. That cost was accepted on 2026-10-13: one lint that fails where CI fails is simpler than two lints of which one passes where CI fails, and a CI round trip costs far more than 37 s per lint. Taplo joins the same arm, run as the push gate runs it, so TOML formatting fails locally too.

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
  - Times shown to the user are America/Los_Angeles; build-log stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 3 | T0 + 72 h (+ 24 h if the sample is short) | the clock, after the showrunner has fast-forwarded Phase 2 to `~/.claude` main |
| G2 | Phase 4 | T1 + 48 h, where T1 is when Phase 3's width reaches `~/.claude` main | the clock; dropped when the verdict reverts |

While G1 holds, each time the unit director wakes it runs `width_trial.py report --since <T0> --until <now>` (seconds) and messages natedev only when T or F fails by the Rules with at least 30 whole-hana runs per arm.

## Phases

### Phase 1 — The gate's records name their width and call, and the scorecard is frozen  · status: done

#### Work Order

**Goal:** the memory gate keys its reservation by test width and records each peak with its width and call id, with no change in behavior while no step names a width. `width_trial.py report` prints the trial's scorecard, and its pre-change baseline is recorded with the script's hash.

**Spec:**

`scripts/lint/memory_admit.py`:
- `def test_threads(argv: list[str]) -> int | None`: the integer after `--test-threads` or `-j`, or in `--test-threads=N` / `-jN`; `None` when absent or not an integer (`num-cpus`).
- `Reservation` gains `test_threads: int | None` and `call_id: str | None` (`os.environ.get("BUILDLOG_CALL_ID") or None`), both set in `check` from its argv and environment on every admit.
- `expected_peak(repo, step, host, now, root, test_threads: int | None = None)`: the measured tier keeps a record only when `item.get("test_threads") == test_threads` as well (old records have no field, so they match `None`). The index tier and fallback are unchanged. `check` passes `test_threads(argv)`.
- `release` writes `"test_threads"` and `"call_id"` into the `anon_peaks.jsonl` record.
- `main`'s output line is unchanged.

`scripts/buildlog/width_trial.py` (new), CLI `report --since <ISO-8601> --until <ISO-8601>` (each with an offset; `--until now` allowed). Reads `store.root()`'s index (`?mode=ro` URI) and `admission/anon_peaks.jsonl`; imports `memory_admit` (sys.path insert of `scripts/lint`, as `memory_admit.py` inserts `scripts/buildlog`) for `expected_peak` and `percentile`. It computes every metric in Delegation Context → Trial design exactly as defined there:

```python
BLOCK_S = 6000          # invoke.sh's switchback block; keep the two equal
WASHOUT_S = 900
WIDTHS = {16: "physical", 32: "logical"}   # natedev; any other width prints as its number

class Step(TypedDict):
    request: float; block: int; width: int | None; whole_hana: bool; hana: bool
    wait: float; exec_s: float | None; failed: bool; call_id: str | None; step_id: str
class Interval(NamedTuple): low: float; high: float
def load_steps(index: Path, since: float, until: float, host: str) -> list[Step]
def anon_by_call(path: Path, host: str) -> dict[str, int]          # call_id -> anon peak (nextest records)
def block_bootstrap(arms: tuple[list[Step], list[Step]], stat: Callable[[list[Step]], float | None],
                    combine: Callable[[float, float], float], rng: random.Random, n: int = 2000) -> Interval
def verdict(w: Metric, t: Metric, f: Metric) -> Literal["keep", "revert", "trade-off", "no verdict"]
```

Output, plain text, every time in PDT, numbers to 2 decimals (GiB), whole seconds or 1 decimal (%):
```
Width trial natedev 2026-10-09 21:04 PDT – 2026-10-12 21:04 PDT (72.0 h) · width_trial.py sha256 1a2b3c4d5e6f
arm       blocks  hana steps  whole-hana runs
physical  22      1,402       260
logical   21      1,388       255
sample: ok
metric                                     physical  logical  ratio   90% interval   rule                        result
W memory wait per hana test step (s)       41.0      120.3    0.34    0.22–0.51      ≤ 0.50, high < 1.00         proven
P whole-hana anon peak p50 (GiB)           5.80      11.10    0.52    0.49–0.55      physical ≤ 6.50             met
R gate reservation, hana nextest (GiB)     8.91      11.35    —       —              physical ≤ 9.00             met
T whole-hana exec p50 (s)                  215       205      1.05    0.98–1.12      fails > 1.11 and low > 1    pass
F whole-hana failed runs (%)               47.1      48.0     −0.9pt  −7.0–5.2pt     fails > +5pt and low > 0    pass
  timed-out tests per 100 whole-hana runs  0.4       0.8
  memory wait per gated step, all natedev  22.0      51.0
verdict: keep — W proven; T and F pass
```
- With one arm (or only `unset`), it prints that arm's rows and values, `—` where a value needs the other arm or `call_id`, and `verdict: no verdict — needs both arms`. A short sample prints `sample: short — <arm> has <n> whole-hana runs (< 60)` or `… <n> blocks (< 15)`.
- The header's hash is SHA-256 of the script's own bytes (first 12 hex digits).
- Exit 0 on a report, 2 on a usage error.

`docs/as-built/build-memory-admission.md` — "The memory gate" `need` bullet (32) and the release sentence (57): the measured tier also matches the step's `--test-threads` (none matches none), and each record carries `test_threads` and `call_id`.

**Files:**
- `scripts/lint/memory_admit.py` — the width key and the two record fields.
- `scripts/buildlog/width_trial.py` — new.
- `docs/as-built/build-memory-admission.md` — two sentences (also touches; owner build-report-unit).
- `scripts/lint/test_memory_admit.py` — width cases; `scripts/buildlog/test_width_trial.py` — new.

**Seats:** `1 writer + 1 tester` — one small gate change and one new script, nothing to split; the Trial design and the signatures above are enough to test against.
- `impl` — `scripts/lint/memory_admit.py`, `scripts/buildlog/width_trial.py`, `docs/as-built/build-memory-admission.md`
- `test` — `scripts/lint/test_memory_admit.py`: `test_threads` parses all four forms and `num-cpus`; five records at width 16 and five unlabeled give different measured needs for `16` and `None`; `release` writes both fields with `BUILDLOG_CALL_ID` set. `scripts/buildlog/test_width_trial.py`: a temp `BUILDLOG_DIR` index and `anon_peaks.jsonl` built from known steps in known blocks; each metric's value; a step in a block's first 900 s left out of W only; the `call_id` join; the same seed gives the same interval twice; each verdict branch (proven, partial, none, a failed T, a failed F with W proven → `trade-off`); a one-arm window prints `no verdict`; a short sample line.

**Constraints from prior phases:** none.

**Acceptance gate:** `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` and `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; `basedpyright scripts/lint/memory_admit.py scripts/buildlog/width_trial.py scripts/lint/test_memory_admit.py scripts/buildlog/test_width_trial.py` clean. Live: `~/.claude/scripts/lib/py scripts/buildlog/width_trial.py report --since 2026-10-07T00:00-07:00 --until now` prints the `unset` arm (W, T, F, R; P `—`). The As-built records that output with the SHA-256 of `width_trial.py` and `memory_admit.py`, the pre-registration Phase 3 checks.

### Phase 2 — The trial starts: each nextest step runs at 16 or 32 by block  · status: todo

#### Work Order

**Goal:** every nextest step `invoke.sh` runs names its width: natedev alternates 16 and 32 by 100-minute block, the Mac runs 12, and a caller's own width is kept.

**Spec:**

`scripts/lint/invoke.sh`, beside `run_nextest`:
- `LINT_TEST_WIDTH=trial` — the mode: `trial` (block parity), `physical`, `logical`. A comment names Phase 3 as the line that sets the verdict, and `width_trial.py`'s `BLOCK_S` as the twin of the block length.
- `nextest_physical_cores [sysfs_root]` sets `LINT_PHYSICAL_CORES` once per shell: on Linux, the number of distinct `physical_package_id:core_id` pairs under `${1:-/sys/devices/system/cpu}/cpu[0-9]*/topology/`, read with `read` (no forks, no associative arrays); on darwin `sysctl -n hw.physicalcpu`. `nextest_logical_cores` sets `LINT_LOGICAL_CORES` once: `getconf _NPROCESSORS_ONLN`. A value that is empty or not a positive integer stays empty.
- `nextest_width_arm EPOCH` prints nothing and sets `LINT_TEST_WIDTH_ARM` to `physical` when `(EPOCH / 6000) % 2 == 0`, else `logical`.
- `run_nextest`: unless an argument is `--test-threads`, `--test-threads=*`, `-j` or `-j*`, or `NEXTEST_TEST_THREADS` is set: pick the arm (`trial`: `nextest_width_arm "${EPOCHSECONDS:-$(date +%s)}"`; otherwise the mode itself), take that core count, and when it is set run `cargo nextest run --test-threads <N> "$@"`. The flag goes before `"$@"`, since a caller's `--` hands what follows to the test binaries. Otherwise run as today.

**Files:**
- `scripts/lint/invoke.sh` — the width functions and `run_nextest`.
- `scripts/lint/test_nextest_width.py` — new.

**Seats:** `1 writer + 1 tester` — one function group in one file; the tester writes from the rules above.
- `impl` — `scripts/lint/invoke.sh`
- `test` — `scripts/lint/test_nextest_width.py`, sourcing `invoke.sh` as `test_invoke_scope.py` does, with a stand-in `cargo` on `PATH` that prints its argv and `run` replaced by a function that prints its argv: a fake sysfs root with 4 CPUs on 2 cores gives 2; `nextest_width_arm` at 0, 5999, 6000 and 12000; `trial` at an even and an odd block gives `--test-threads 2` and the logical count; `physical` and `logical` modes; `-j 4`, `--test-threads=3` and `NEXTEST_TEST_THREADS=5` add nothing; a trailing `-- --nocapture` stays after the flag; an unreadable core count adds nothing and the status passes through

**Constraints from prior phases:** Phase 1: `memory_admit.test_threads(argv)` reads `--test-threads N`, so each width keys its own reservation from the first step; each new width starts with no measured records and uses the index tier until it has five. `width_trial.py`'s `BLOCK_S = 6000`; arm names `physical` (16) and `logical` (32).

**Acceptance gate:** `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` green; `basedpyright scripts/lint/test_nextest_width.py` clean; `bash -n scripts/lint/invoke.sh`. Live on natedev: `bash -c 'source <worktree>/scripts/lint/invoke.sh; cd ~/rust/rust-template && run_nextest --workspace'` runs with `--test-threads 16` or `32` (the current block's), the build log step's argv shows it, and its `anon_peaks.jsonl` record carries `test_threads`; over `ssh mac` (prints `rc=`), `bash -n` on the worktree's file passes. After the showrunner fast-forwards it to main: T0 from the reflog, sent to natedev with G1's time, and the first organic hana nextest steps carry the flag.

### Phase 3 — Readout: the verdict, and the width it sets  · status: todo

#### Work Order

**Blocked by:** G1.

**Goal:** the scorecard over W gives the verdict, the width it calls for is set, and the verdict reaches natedev and the ledger.

**Spec:**
- The SHA-256 of `width_trial.py` and `memory_admit.py` on `~/.claude` main equals Phase 1's As-built; a mismatch stops the phase and goes to natedev.
- Run `report --since <T0> --until <T0 + 72 h>`. On `sample: short`, extend once to `T0 + 96 h` (end the turn blocked until then), and read that.
- Apply the Action rule: keep → `LINT_TEST_WIDTH=physical`; revert → `LINT_TEST_WIDTH=logical`, and G2 is dropped; trade-off → send natedev `--need decision` with W, T and F and both choices, change nothing, and end the turn blocked until the answer.
- Ledger line 25 (`~/.local/state/nightly-review/ledger.md`): append `; trial <T0 date>–<end date>: <verdict>, W <ratio> (<interval>), P <GiB>, R <GiB>, T <ratio>, F <points>pt; <width> (~/.claude <sha>)`.
- natedev gets the verdict line and the table.

**Files:**
- `scripts/lint/invoke.sh` — the mode line.
- `docs/plans/build-followups-test-threads-unit.md` — the As-built table.

**Seats:** `1 writer` — `impl` runs the scorecard, changes one line and reports.

**Constraints from prior phases:** Phase 1: the scorecard, the frozen hashes and the `unset` baseline. Phase 2: T0, the mode variable `LINT_TEST_WIDTH` and its values.

**Acceptance gate:** the hashes match; the report exits 0; the As-built holds the full output, the action and its rule; for keep or revert, `bash -n scripts/lint/invoke.sh` and `python3 -m unittest discover -s scripts/lint -p 'test_nextest_width.py'` green with that mode.

### Phase 4 — Rollout check: the kept width holds with every step on it  · status: todo

#### Work Order

**Blocked by:** G2.

**Goal:** 48 hours with every natedev step at 16 show the reservation and the waits the trial's physical arm showed.

**Spec:**
- Run `report --since <T1> --until <T1 + 48 h>` (one arm).
- Holds when R ≤ 9.0 GiB, W's mean is at most the trial's physical-arm interval's high end, and T's p50 is at most the trial's logical p50 × 1.11. Any miss goes to natedev with the numbers; nothing reverts on its own.
- Ledger line 25: append `; rollout <T1 date>–<end date>: <holds | misses …>`. natedev gets the line.

**Files:**
- `docs/plans/build-followups-test-threads-unit.md` — the As-built.

**Seats:** `1 writer` — `impl` runs the report and records it.

**Constraints from prior phases:** Phase 3: the verdict, T1 and the trial's per-arm values and intervals.

**Acceptance gate:** the report exits 0; the As-built states each condition with its number.

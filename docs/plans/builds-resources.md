# builds-resources

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Tests fake every memory reading the memory gate makes, and a tripwire fails a test that reads live machine memory.

> **Production: builds** — unit `resources-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 10:07 EDT

Own what a build may use and when it starts: memory admission and the memory queue, build holds, build targets and slots, the cargo wrapper, and the /tmp and disk sweeps. First work: test_brp_launch_gate.py waits on the live machine's memory under load (natedev, 2026-10-29: module took 169 s, median 6 s; two tests printed 'waiting for memory'). Its setUp fakes BUILDLOG_MEMINFO but not BUILDLOG_MEMORY_PRESSURE. Fake both readings in every test, add a guard that fails a test when the memory gate would read live machine state, and check the other memory-gate callers' tests for the same leak.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, docs and Python and shell scripts that run every session. This unit owns what a build may use and when it starts (production doc, Units → Owns).
- **Project started:** 2026-10-29T14:07:00+00:00
- **Stack:** Python 3.13, standard library only; basedpyright strict (`pyrightconfig.json`); unittest. Bash for the memory gate and hooks.
- **Layout:**
  - `scripts/lint/memory_admit.py` — the admission checker. `read_meminfo(path)` :563 reads `MemAvailable`/`MemTotal`; its path comes from the shell (`check <meminfo> …`) or from `BUILDLOG_MEMINFO` (`read_queue` :958). `read_pressure()` :578 reads `BUILDLOG_MEMORY_PRESSURE`, default `/proc/pressure/memory`, and holds every step when `full avg10` > 10.0. It swallows `OSError`/`ValueError`. Callers: `check` :781 and :822, `read_queue` :961 (the `/build:queue` view). `main` :1095.
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory` :179. It reads `MemAvailable` itself from `${BUILDLOG_MEMINFO:-/proc/meminfo}` (:186–:200), then starts `memory_admit.py check`. A checker that exits non-zero is refused with its last output line (:214–:217, `buildlog_memory_refuse` :153). On darwin with `BUILDLOG_MEMINFO` unset it grants at once and reads nothing (:182).
  - `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — sources `memory_gate.sh` and calls `buildlog_wait_for_memory` with no arguments (a launch: no reservation).
  - `scripts/hooks/test_brp_launch_gate.py` — `setUp` :47 writes `meminfo` with `MemAvailable: 67108864 kB` only and sets `BUILDLOG_MEMINFO`; `BUILDLOG_MEMORY_PRESSURE` is unset, so `read_pressure` reads natedev's `/proc/pressure/memory`. Under load (`full avg10` > 10) every launch test holds and prints `waiting for memory`. `test_low_memory_wait_leaves_old_release_record_unchanged` :139 rewrites the same `meminfo` file.
  - `scripts/production/merge_gate.py` — `test_module_environment` :514 gives each test module a private `BUILDLOG_DIR` and the build-tool tripwires (`cargo`, `rustc`, `rustdoc`, `rustup`, `cargo-nextest` first on `PATH`, each appends its argv to `<root>/build-tool-calls.jsonl` and exits 99). `finish_gate_job` :536 reads that file, appends `a test started real <tool>: <args>` to the output, and sets status 1 and `GateJobResult.build_tool_started` :143. :856 never reruns a tripped module alone. Not in any unit's Owns cell: natedev showrunner's file.
  - `scripts/production/affected_modules.py` :20 `SHARED_TEST_HELPERS` — fixture modules that other test modules import; a change to one runs the full isolation check. natedev showrunner's file.
  - Import idiom for a test helper: `from scripts.lib.nix_env_test_support import NixFixture` (`scripts/direct/test_verify_nix_shell.py` :13); the gate runs each module with the repo root as working directory.
- **Survey (2026-10-29, `grep -c` per test module that names the gate, `invoke.sh`, `verify.sh` or the launch hook):**
  - Fake both readings: `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_queue.py`.
  - Fake `BUILDLOG_MEMINFO` but not `BUILDLOG_MEMORY_PRESSURE` (pressure leaks): `scripts/hooks/test_brp_launch_gate.py`, `scripts/build_hold/test_build_hold.py` (this unit); `scripts/buildlog/test_record.py` (report-unit); `scripts/buildlog/test_mac_idle.py`, `scripts/direct/test_verify_checks_fail_closed.py`, `test_verify_doc.py`, `test_verify_lint_scope.py`, `test_verify_mac_offload.py`, `test_verify_release_ack.py`, `test_verify_token_wait.py`, `test_verify_untested_examples.py` (verify-unit); `scripts/production/test_dailies_render_holds.py` (natedev showrunner).
  - Fake `BUILDLOG_MEMORY_PRESSURE` but not `BUILDLOG_MEMINFO`: `scripts/direct/test_verify_hold_wait.py` (verify-unit) — the meminfo read may be stubbed; the tripwire decides.
  - Fake neither but run `verify.sh`, `invoke.sh` or the gate: about 30 more modules (`scripts/direct/test_verify_memory_kill.py`, `test_verify_final_pass.py`, `test_verify_nix_shell.py`, `scripts/hooks/test_build_hold_guard.py`, `scripts/build_hold/test_hold_enforcer.py`, `scripts/lint/test_invoke_scope.py` and others). Many stub the gate or stop before it; a static count cannot tell, so Phase 2 uses the tripwire to find the set.
- **Out of scope (unit director's call, 2026-10-29; a one-line change to the tripwire if wrong):** the process table (`BUILDLOG_PROC`, `process_start_time` :501). The gate reads it only for the pids that the test's private ledger names, so machine load cannot change an admission through it. The build budget is a constant unless `BUILDLOG_BUILD_BUDGET_GIB` is set: not live.
- **Test lanes:** `test_*.py` beside each module.
- **Build:** none (Python, shell and markdown).
- **Test:** each changed module with `python3 -m unittest discover -s <dir> -p '<module>'` from the worktree root. Then the merge gate: `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <worktree> --tests-only`, run from `~/.claude` main after a merge of `origin/builds` (memory: merge origin before every gate). Check `build_hold.py status` first; no tests during a natedev build hold that this unit does not hold.
- **Lint:** `basedpyright scripts/lint scripts/hooks scripts/build_hold scripts/production` from the worktree root, plus the directory of every other changed module; pass is its `0 errors, 0 warnings, 0 notes` line (exit status says nothing). `bash -n scripts/lint/memory_gate.sh`.
- **Invariants:**
  - Admission and hold check errors stop the step; only an explicit admission answer starts it (`docs/as-built/build-memory-admission.md` → Invariants).
  - Outside a test the tripwire does nothing: it acts only when `BUILDLOG_LIVE_MEMORY_TRIPWIRE` is set, and no installed caller sets it. A live session's gate behaves as before, byte for byte in its output.
  - Tests never read the real cgroup tree, `/proc/meminfo` or `/proc/pressure/memory` (as-built Invariants; this plan adds the pressure file).
  - Files of other owners go through cargo-berth's normal flow, and the checkpoint notice names each owner: `also touches <paths> (owner <unit>), tested against <owner origin tip>, clean` (production rule, Owners in natedev).
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — A tripwire for live memory readings, and the BRP launch tests fake both  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `test_brp_launch_gate.py` runs in its idle time when natedev is under load, because it fakes both memory readings; and a memory gate read of `/proc/meminfo` or `/proc/pressure/memory` in a test that set the tripwire refuses with a named cause and leaves a record.

**Spec:**

1. **The tripwire variable.** `BUILDLOG_LIVE_MEMORY_TRIPWIRE` holds the path of a record file. When it is set and not empty, a memory reading whose path resolves (`os.path.realpath`; in bash `realpath -m` or `readlink -f`) to `/proc/meminfo` or `/proc/pressure/memory` is a live reading. A live reading:
   - appends one JSON line to the record file under `fcntl.flock` (bash: `flock` on the file, or a single `printf >>` write of one line): `{"reading": "<live path>", "argv": [<process argv>], "pid": <pid>}`;
   - does not read the file;
   - stops the caller with this text: `live memory reading <live path> in a test; fake BUILDLOG_MEMINFO and BUILDLOG_MEMORY_PRESSURE (scripts/lint/memory_test_support.py)`.
   With the variable unset or empty, every reading is unchanged.
2. **Python side** (`memory_admit.py`). One function checks a reading path and records a live one; `read_meminfo` and `read_pressure` call it before they open the file. A live reading raises `LiveMemoryReading` (a new `RuntimeError` subclass, so `read_pressure`'s `except (OSError, ValueError, StopIteration)` does not swallow it). `main` catches `LiveMemoryReading`, prints the text of item 1 as its last stderr line, and exits 2; the shell gate then refuses with that line (`memory admission check failed (live memory reading …)`). In-process callers (`read_queue`, tests) get the exception.
3. **Shell side** (`memory_gate.sh`). Before the `MemAvailable` read at :193, the same check runs on `$meminfo`. A live reading records the line and calls `buildlog_memory_refuse` with the text of item 1, then returns 2. The darwin early grant (:182) stays first and reads nothing.
4. **The fake helper** `scripts/lint/memory_test_support.py` (new). `fake_memory_readings(root: Path, *, available_kib: int = 67108864, total_kib: int = 67108864, full_avg10: float = 0.0) -> dict[str, str]` writes `root/"meminfo"` (`MemTotal:` and `MemAvailable:` lines in kB) and `root/"pressure"` (a `some` and a `full` line in the `/proc/pressure/memory` form, `avg10=` from `full_avg10`), and returns `BUILDLOG_MEMINFO`, `BUILDLOG_MEMORY_PRESSURE` and `BUILDLOG_LIVE_MEMORY_TRIPWIRE`. The tripwire value is `os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"]` when the caller's environment has one (so the merge gate's record file stays the one in use), else `root/"live-memory-reads.jsonl"`. A test that changes a reading rewrites the returned files. Add the path to `SHARED_TEST_HELPERS` in `scripts/production/affected_modules.py`.
5. **The BRP launch tests.** `test_brp_launch_gate.py` `setUp` takes its memory variables from `fake_memory_readings(self.root)` in place of its own `meminfo` file. `test_low_memory_wait_leaves_old_release_record_unchanged` keeps its low `MemAvailable` value (8388608 kB) and writes it to the returned `BUILDLOG_MEMINFO` file. Each test's assertions stay as they are.
6. **This unit's other memory tests.** `scripts/build_hold/test_build_hold.py` takes its memory variables from the helper. `scripts/lint/test_memory_admit.py` keeps its own fakes but sets `BUILDLOG_LIVE_MEMORY_TRIPWIRE` wherever it starts the gate or the checker, and moves any in-process read of the default paths to a faked path.
7. **Tests of the tripwire** in `scripts/lint/test_memory_admit.py`:
   - with the tripwire set and `BUILDLOG_MEMORY_PRESSURE` unset, `read_pressure()` raises `LiveMemoryReading` and the record file has one line with `"reading": "/proc/pressure/memory"`;
   - the same for `read_meminfo(Path("/proc/meminfo"))`, and for a symlink in the test folder that points to `/proc/meminfo`;
   - with the tripwire set and both readings faked, `check` and `read_queue` admit as before and the record file is absent or empty;
   - with the tripwire unset, `read_pressure()` with `BUILDLOG_MEMORY_PRESSURE` unset still returns a `Pressure` (live behavior unchanged; the test asserts the type only);
   - `buildlog_wait_for_memory` in bash with the tripwire set and `BUILDLOG_MEMINFO` unset returns 2, prints `memory admission check failed (live memory reading /proc/meminfo in a test; …)`, and writes one record line; with `BUILDLOG_MEMINFO` faked and `BUILDLOG_MEMORY_PRESSURE` unset it returns 2 with the `/proc/pressure/memory` text from the checker.
   And in `scripts/hooks/test_brp_launch_gate.py`: a launch with the tripwire set and `BUILDLOG_MEMORY_PRESSURE` removed from the environment is denied, and its `permissionDecisionReason` ends with the `/proc/pressure/memory` text.
8. **Docs.** `docs/as-built/build-memory-admission.md`: the tests invariant names `/proc/pressure/memory` and `BUILDLOG_MEMORY_PRESSURE`, says that tests take both from `scripts/lint/memory_test_support.py`, and describes the tripwire in one sentence; a gotcha line records the 2026-10-29 case (module 169 s, median 6 s, pressure unfaked).

**Files:**
- `scripts/lint/memory_admit.py` — the path check, `LiveMemoryReading`, `main`.
- `scripts/lint/memory_gate.sh` — the shell check before the `MemAvailable` read.
- `scripts/lint/memory_test_support.py` (new) — `fake_memory_readings`.
- `scripts/lint/test_memory_admit.py` — the tripwire tests and the tripwire in its environments.
- `scripts/hooks/test_brp_launch_gate.py` — the helper in `setUp`, the denial test.
- `scripts/build_hold/test_build_hold.py` — the helper.
- `scripts/production/affected_modules.py` — one `SHARED_TEST_HELPERS` entry (owner: natedev showrunner).
- `docs/as-built/build-memory-admission.md` — item 8.

**Workers:** `1 writer + 1 tester`
- `impl` — `memory_admit.py`, `memory_gate.sh`, `memory_test_support.py`, the `setUp` changes in `test_brp_launch_gate.py` and `test_build_hold.py`, `affected_modules.py`, the as-built doc.
- `test` — the tests of item 7, written from this Spec alone.

**Constraints from prior phases:** none.

**Acceptance gate:**
- The merge gate green (Test, Delegation Context).
- `basedpyright` over the Lint directories reports `0 errors, 0 warnings, 0 notes`; `bash -n scripts/lint/memory_gate.sh` exits 0.
- `env BUILDLOG_LIVE_MEMORY_TRIPWIRE=<scratch>/trip.jsonl python3 -m unittest discover -s scripts/hooks -p test_brp_launch_gate.py` run three times on natedev: each run passes, prints no `waiting for memory`, and leaves `trip.jsonl` absent or empty. The summary gives each run's time next to the live `full avg10` value of `/proc/pressure/memory` at that time.
- Change-Review line: the BRP launch tests fake both memory readings, and a test that reads live memory refuses | `cd ~/.claude && env -u BUILDLOG_MEMORY_PRESSURE BUILDLOG_LIVE_MEMORY_TRIPWIRE=/tmp/claude/trip.jsonl bash -c 'source scripts/lint/memory_gate.sh; buildlog_wait_for_memory'; cat /tmp/claude/trip.jsonl` | the refusal text names `/proc/meminfo`, and the file has one record line.

### Phase 2 — The merge gate sets the tripwire for every test module, and every caller's tests fake both readings  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** every test module in the merge gate runs with the tripwire set; a module that reads live memory fails the gate with the reading named, and no module in the suite does.

**Spec:**

1. **The gate sets the tripwire.** `test_module_environment` adds `BUILDLOG_LIVE_MEMORY_TRIPWIRE=<root>/live-memory-reads.jsonl` (`root` is the module's temporary folder). The `types` suite does not get it.
2. **The gate fails a tripped module.** `finish_gate_job` reads `live-memory-reads.jsonl` like `build-tool-calls.jsonl`: for each line it appends `<module>: a test read live memory <reading>: <shlex-joined argv>` to the output, and any line sets status 1. Rename `GateJobResult.build_tool_started` to `tripwire` (true for a build-tool call or a live memory reading); :856 keeps a tripped module out of the alone rerun, as it does now for build tools. Update every reader of the old field name.
3. **Find the set.** With items 1–2 in place, run the full merge gate (`--full`, not the affected-only selection). Every module that fails with `a test read live memory` goes on the list in the phase report, with its owner (Delegation Context survey).
4. **Fix each module on the list.** Each module takes its memory variables from `fake_memory_readings` (Phase 1). Where a module already writes its own meminfo for a test value (for example `test_record.py` :246 compares `MemAvailable: 12582912 kB`), it keeps that value and writes it to the helper's file, or keeps its own file and adds the helper's `BUILDLOG_MEMORY_PRESSURE` entry. Assertions stay as they are. A module whose fix needs more than adding the helper goes to the unit director, who routes it to its owner with the gate output; it does not block the rest.
5. **The pressure-only leaks the survey names** get the helper's `BUILDLOG_MEMORY_PRESSURE` entry even when the gate run in item 3 does not trip them (a run on an idle machine still reads live pressure without a wait): `scripts/buildlog/test_record.py`, `scripts/buildlog/test_mac_idle.py`, `scripts/direct/test_verify_checks_fail_closed.py`, `test_verify_doc.py`, `test_verify_lint_scope.py`, `test_verify_mac_offload.py`, `test_verify_release_ack.py`, `test_verify_token_wait.py`, `test_verify_untested_examples.py`, `scripts/production/test_dailies_render_holds.py`. The tripwire trips on the read itself, not on a wait, so item 3 lists them on any machine; this item is the check that it did.
6. **A test of the gate** in `scripts/production/test_merge_gate.py` (or the module that tests `finish_gate_job` now): a module folder whose `live-memory-reads.jsonl` has one line gives status 1, the `a test read live memory` line and `tripwire` true; an empty or absent file leaves status as the module returned it.
7. **Docs.** `docs/as-built/merge-gate-and-test-speed.md`: the tripwire line names the live memory tripwire next to the build-tool tripwires. `docs/as-built/build-memory-admission.md`: the tests invariant says that the merge gate sets the tripwire for every module.

**Files:**
- `scripts/production/merge_gate.py` — items 1–2 (owner: natedev showrunner).
- `scripts/production/test_merge_gate.py` — item 6 (owner: natedev showrunner).
- Each module item 3 lists, and the modules of item 5: verify-unit (`scripts/direct/test_verify_*.py`, `scripts/buildlog/test_mac_idle.py`, `scripts/lint/test_invoke_scope.py`), report-unit (`scripts/buildlog/test_record.py` and any `scripts/buildlog/` or `scripts/build_queue/` module listed), natedev showrunner (`scripts/production/test_dailies_render_holds.py` and any other `scripts/production/` module listed), this unit (`scripts/hooks/`, `scripts/build_hold/`, `scripts/build_targets/`, `scripts/lint/` modules it owns).
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md` — item 7.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–5 and 7: the gate change, the full gate run, and each module's environment change. The writer reports the item 3 list with each module's owner and the failing test names.
- `test` — item 6, written from this Spec alone.

**Showrunner approval (2026-10-29):** edits to other owners' test files only add the helper to each test; nothing else in those files changes. verify-unit is a natedev unit until natedev transfers it: trial-merge its files against `origin/natedev-verify` and `origin/natedev`, and name each owner in the notice's also-touches line.

**Constraints from prior phases:** Phase 1's `fake_memory_readings` signature and its rule that an inherited `BUILDLOG_LIVE_MEMORY_TRIPWIRE` stays in use; the refusal text of Phase 1 item 1.

**Acceptance gate:**
- A `--full` merge gate run green with no `a test read live memory` line; the summary gives the item 3 list before the fixes and its count after (0).
- `basedpyright` over the Lint directories and the directory of every changed module reports `0 errors, 0 warnings, 0 notes`.
- The checkpoint notice has one `also touches` line per owner (verify-unit, report-unit, natedev showrunner), each with `git merge-tree --write-tree HEAD origin/<owner branch>` clean, and a trial merge against `origin/natedev`.
- Module times for `test_brp_launch_gate.py`, `test_record.py` and `test_verify_token_wait.py` from this gate run next to their medians from `/build:report`.
- Change-Review line: the merge gate fails a test module that reads live machine memory | `cd ~/.claude && env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` after a one-line edit in a scratch copy that removes `BUILDLOG_MEMORY_PRESSURE` from `test_brp_launch_gate.py` | the module fails with `a test read live memory /proc/pressure/memory: …`.

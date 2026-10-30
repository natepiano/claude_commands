# builds-resources — Next

## Items to consider

- [ ] **Memory pressure is sampled every 5–10 s, not only at each admission poll**
  - Target: `scripts/lint/memory_admit.py` (pressure reading), the buildlog sampler, `docs/as-built/build-memory-admission.md`
  - Why needed: the pressure hold (full avg10 > 5.0) is judged only from readings taken when a step polls, so the record has no steady avg10 series to tell how long holds last or to replay another limit against a freeze.
  - Completion condition: `/proc/pressure/memory` some and full avg10 are recorded every 5–10 s on natedev with a time stamp, kept for at least 7 days, and the pressure replay reads them; a test fakes the pressure file and checks the series.
  - Revealed by: the pressure hold question during Phase 16 (builds showrunner 2026-10-30: limit 5.0 stays, the sampler is `next`)

- [ ] **The sweep stops reading its old removal file** (not before 2026-12-05)
  - Target: `scripts/lint/sweep.py` (`read_sweep_removals`, `parse_sweep_removals`, `sweep_removals_path`), `docs/as-built/build-targets.md`
  - Why needed: after Phase 4 the build-log `sweep_trim` row is the one record of a trim and nothing writes `~/.local/state/lint-sweep/removals.jsonl`; the read-only legacy reader is a second source to keep only until its 35-day retention has passed.
  - Completion condition: on or after 2026-12-05 the reader and the file are deleted, `sweep.py rebuilds` reads only the journal and the build-log rows, and `test_sweep.py` passes.
  - Revealed by: Phase 25 review (Phase 4 Spec 6; builds showrunner 2026-10-30: next, dated on or after 2026-12-05)

- [ ] **Measure EASY backfill against the deployed Rule A queue policy**
  - Start after Phase 31 has run on main for a day; bound the cohort at any later queue-priority or head-policy change.
  - Compare EASY with the deployed ordered walk, including Rule A and recovery spacing, and report additional wait savings and head delay using the same admission function.
  - Revealed by: Phase 30; baseline corrected after Phase 31's Work Order.

- [ ] **Make the build_targets.py call from verify.sh cheap: it costs 199 ms of CPU on every step**
  - Why needed: verify measured `build_targets.py` at 199 ms of CPU each time `verify.sh` starts it, on every step; it is the most costly helper.
  - Completion condition: the call uses fewer imports or a lighter entry path; the change reports before and after CPU figures for the same call, and the build_targets and verify tests pass.
  - Revealed by: verify-unit measurement (builds showrunner 2026-10-30: next)

- [ ] **After a build folder that clones share is trimmed, the build log says which repository's builds it slowed down and why**
  - Target: `scripts/lib/scratch.py`, `scripts/lint/sweep.py`, report-unit's step attribution, and their tests
  - Why needed: clones share one lasting build folder per repository since Phase 4, and no saved row says which repository's build a trim slowed down; `target_dir` reads `CARGO_TARGET_DIR`, not the shared folder
  - Completion condition: a trim row carries the repository identity, and the rebuild report names the repository and the trim for a rebuild that followed it
  - Revealed by: Phase 4 (builds showrunner 2026-10-30: next; waits on report-unit 4f)

- [ ] **The working-set summary tells a Node script apart from a Node task or one-line program**
  - Target: `scripts/build_targets/working_set.py`, `scripts/build_targets/test_working_set.py`
  - Why needed: `NO_NODE_ENTRY` is an empty string, so a missing entry script and a real value read the same
  - Completion condition: the classification carries a named no-entry state, and a test pins a `node --run` task and a `node -e` program as no-entry
  - Revealed by: Phase 28 (builds showrunner 2026-10-30: next)

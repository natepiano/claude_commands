# builds-resources — Next

## Items to consider

- [ ] **Record memory PSI avg10 every 5–10 seconds, independently of admission passes.**
  - Target: `scripts/buildlog/sample.py`, `scripts/lint/memory_admit.py` (`read_pressure`), `scripts/build_queue/admission_service.py`, their tests, and `docs/as-built/build-memory-admission.md`.
  - Why needed: The service reads PSI during admission passes. The buildlog sampler records cumulative stalls, but it does not record the required avg10 series.
  - Completion condition: `/proc/pressure/memory` some and full avg10 are recorded every 5–10 s on natedev with a time stamp, kept for at least 7 days, and the pressure replay reads them; a test fakes the pressure file and checks the series.
  - Revealed by: the pressure hold question during Phase 16 (builds showrunner 2026-10-30: limit 5.0 stays, the sampler is `next`)

- [ ] **The sweep stops reading its old removal file** (not before 2026-12-05)
  - Target: `scripts/lint/sweep.py` (`read_sweep_removals`, `parse_sweep_removals`, `sweep_removals_path`), `docs/as-built/build-targets.md`
  - Why needed: after Phase 4 the build-log `sweep_trim` row is the one record of a trim and nothing writes `~/.local/state/lint-sweep/removals.jsonl`; the read-only legacy reader is a second source to keep only until its 35-day retention has passed.
  - Completion condition: on or after 2026-12-05 the reader and the file are deleted, `sweep.py rebuilds` reads only the journal and the build-log rows, and `test_sweep.py` passes.
  - Revealed by: Phase 25 review (Phase 4 Spec 6; builds showrunner 2026-10-30: next, dated on or after 2026-12-05)

- [ ] **Measure EASY backfill against the deployed Rule A queue policy**
  - Start after both Phase 39 checkpoints reach main and seven days exist within one unchanged policy and configuration span. Calibrate the deployed model first. Stop comparison if calibration fails.
  - Bound the cohort at changes to queue priority, head policy, estimation, running charge, budget, or headroom settings.
  - Compare EASY with the deployed ordered walk, including Rule A and recovery spacing.
  - Use the shipped `head_policy` for the deployed baseline. Add any alternate analysis rule explicitly; Phase 20 provides no alternate rule argument.
  - Report additional wait savings and head delay.
  - Revealed by: Phase 30; baseline corrected after Phase 31's Work Order and Phase 39.

- [ ] **Make the build_targets.py call from verify.sh cheap: it costs 199 ms of CPU on every step**
  - Why needed: verify measured `build_targets.py` at 199 ms of CPU each time `verify.sh` starts it, on every step; it is the most costly helper.
  - Completion condition: the call uses fewer imports or a lighter entry path; the change reports before and after CPU figures for the same call, and the build_targets and verify tests pass.
  - Revealed by: verify-unit measurement (builds showrunner 2026-10-30: next)

- [ ] **Match trims against each step's recorded Cargo output roots**
  - Target: `scripts/lint/sweep.py` (`RebuildStep`, `read_rebuild_evidence`, `_rebuild_causes`), `scripts/lint/build_shape.py`, and their tests.
  - Dependency: Phase 62's shared root types and recorded-root safety rules.
  - Why needed: `_rebuild_causes()` still matches trims through saved workspace mappings.
  - Completion condition: retain each attempt's identity and root pair in the existing locked-row reader.
  - Reuse the shared root decoding. Never infer an attempt's roots from the workspace aggregate returned by `recorded_output_roots`.
  - Keep unknown, relative, and override-affected roots explicit. Do not attribute a trim through an uncertain root.
  - Keep the `store.locked_rows` reads; do not create or refresh the index during rebuild reporting.
  - Test removed copies, changed mappings, repeated attempts with different roots, unknown roots, relative roots, overrides, and a missing index.
  - Add no second mapping, join, or recorder.
  - Revealed by: Phases 4, 11, 17, 58, and 62.

- [ ] **The working-set summary tells a Node script apart from a Node task or one-line program**
  - Target: `scripts/build_targets/working_set.py`, `scripts/build_targets/test_working_set.py`
  - Why needed: `NO_NODE_ENTRY` is an empty string, so a missing entry script and a real value read the same
  - Completion condition: the classification carries a named no-entry state, and a test pins a `node --run` task and a `node -e` program as no-entry
  - Revealed by: Phase 28 (builds showrunner 2026-10-30: next)

- [ ] **A stopped sweep plan ends with one line, and a plan run prints a short summary**
  - Target: `scripts/lint/tmp_sweep.py` (`--plan`), its tests
  - Why needed: Ctrl-C during `tmp_sweep.py --plan` prints a Python traceback, and a plan run prints 79,530 lines, so the plan cannot be read on screen
  - Completion condition: Ctrl-C ends with one line and exit 130; a plan run prints a short summary (counts and bytes per group) and the path of the full plan file; a test pins both
  - Revealed by: builds C617 (builds showrunner 2026-10-30: next)

- [ ] **The user's typed queue bump works from any pane**
  - Target: `mods/unit-board/hooks/register.tsx`, `mods/unit-board/tests/queue.test.ts`, the shared queue-priority writer and its tests, and `commands/build/queue.md`.
  - Completion condition: typed `/build:queue bump <unit>` from any pane sets or replaces persistent priority through the existing writer, without a session-role check on the plugin route; tests cover a non-showrunner pane and replacement, while direct priority commands retain their existing caller checks.
  - Revealed by: Phase 26; approved by builds in `S/next_items_pending.md`.


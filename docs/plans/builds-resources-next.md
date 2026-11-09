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

- [ ] **Measure EASY backfill against the deployed captured-room rule**
  - Start after deployment of the current rule and seven days within one unchanged policy and configuration span.
  - Calibrate the deployed model first. Stop comparison if calibration fails.
  - Bound the cohort at changes to priority, head policy, estimation, memory charges, slots, spacing, safety settings or headroom.
  - Use the shipped `head_policy` and `admission_walk` for the baseline.
  - Include head protection, continuation slots, the starts-alone exception and start spacing.
  - Report additional wait savings and head delay.
  - Revealed by: Phase 30; baseline corrected against the current admission rule.

- [ ] **Make the build_targets.py call from verify.sh cheap: it costs 199 ms of CPU on every step**
  - Why needed: verify measured `build_targets.py` at 199 ms of CPU each time `verify.sh` starts it, on every step; it is the most costly helper.
  - Completion condition: the call uses fewer imports or a lighter entry path; the change reports before and after CPU figures for the same call, and the build_targets and verify tests pass.
  - Revealed by: verify-unit measurement (builds showrunner 2026-10-30: next)

- [ ] **Match trims against each step's recorded Cargo output roots**
  - Target: `scripts/lint/sweep.py` (`RebuildStep`, `read_rebuild_evidence`, `_rebuild_causes`), `scripts/buildlog/step_outputs.py`, and their tests.
  - Dependency: Phase 64's shared root decoding and row facts, with Phase 62's recorded-root safety rules.
  - Why needed: `_rebuild_causes()` still matches trims through saved workspace mappings.
  - Completion condition: retain each attempt's identity and root pair in the existing locked-row reader.
  - Reuse root decoding from `step_outputs.py` while retaining attempt identity in the existing locked-row reader. Do not use host-wide distinct rows or saved workspace mappings as attempt evidence.
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
  - Why needed: Ctrl-C during `tmp_sweep.py --plan` prints a Python traceback. `write_cleanup_plan()` prints every entry. Phase 72 changed the kept entries; the current output line count is not measured.
  - Completion condition: Ctrl-C ends with one line and exit 130; a plan run prints a short summary (counts and bytes per group) and the path of the full plan file; a test pins both
  - Revealed by: builds C617 (builds showrunner 2026-10-30: next)

- [ ] **The user's typed queue bump accepts a unit name**
  - Target: `scripts/build_queue/build_queue.py`, its selector and tests, `mods/unit-board/tests/queue.test.ts`, and `commands/build/queue.md`.
  - Completion condition: `/build:queue bump <unit>` selects that unit's waiting step and sets or replaces priority through the existing writer.
  - Test a unit name that differs from every session label and folder name.
  - Preserve position, session and folder selectors.
  - Preserve access from any pane and the current direct-command behavior.
  - Test unit selection from a non-showrunner pane and replacement of existing priority.
  - Revealed by: Phase 26; narrowed after the existing pane route and writer tests satisfied the access requirement.

- [ ] **One owner for Cargo target detection in scripts/lib/cargo_output.py** (after the held 63-64 hash lands)
  - Target: move `RUSTC_INFO`, `FINGERPRINT_DIR`, `has_cargo_tag`, `build_trees`, `is_cargo_target`, `enclosing_cargo_target` and its two result types from `scripts/lint/sweep.py` to `scripts/lib/cargo_output.py`; switch every caller (`sweep.py`, `scripts/buildlog/disk.py`, `scripts/buildlog/memory.py`) and their tests.
  - Why needed: `memory.py` loads the whole sweeper to ask whether a folder is Cargo output, and the sweeper imports buildlog modules back. One owner in `scripts/lib` removes that cycle.
  - Completion condition: no copy of the detection is left in `sweep.py`; `memory.py` imports it at the top and drops its run-time `import sweep`. This replaces the Phase 67 import rule for `memory.py`. The change reports `memory.py` import time before and after, and the sweep, memory, disk and offload tests pass.
  - Revealed by: Phase 70 simplify review F008 (builds showrunner 2026-11-05: next; run after the held 63-64 hash lands, so `sweep.py` changes in one place at a time)

- [ ] **Measure the ANON_SHARE estimate share again after the legacy rows expire** (on or after 2026-11-12)
  - Target: repeat the Phase 19 lifecycle measurement and `expected_peak` replay on a copy of natedev state.
  - Update `#### Phase 19 source shares` in `docs/as-built/build-memory-admission.md`.
  - Report retained coverage and shares by step kind. Keep recorded admissions separate from reconstructed estimates.
  - Phase 19 measured 10/4,267 reconstructed estimates using `ANON_SHARE`, excluding caps. Recorded admissions covered only 16.30 hours.
  - If the reconstructed share excluding caps reaches 5%, create a self-contained Work Order for the measured remaining matching gaps.
  - Require replay evidence before narrowing or removing indexed tiers.
  - Preserve same-shape build rules, compile selection, the single history decode and bounded evidence queries.
  - Preserve historical `buildlog` decoding and unknown source text through semantic source types.
  - Revealed by: Phase 19; the showrunner defined "small" as under 5% of estimates.
- [ ] **The folder audit names the maker of a folder made by mktemp**
  - Target: `scripts/lint/folder_audit.py` `makers()` and the guard's `creation_targets()`.
  - Today a `mktemp -d` target resolves to its template path with `X`s, so the folder prints `maker unknown`.
  - Prove the folder by a template match (same parent, same fixed part, same count of random characters) or by pairing the command with its tool result path.
  - Keep the exact-path proof rule for every other command. Add no second command parser.
  - Revealed by: Phase 79; the builds showrunner placed it in next items on 2026-11-07.

## Dropped

- **The floor pass keeps an existing hana `target/external-client`** — dropped by the builds showrunner 2026-11-05: the measured cause (fresh-clone CI points) is already gone, and 1 eviction in 3 days does not pay for 1.3 GB per checkout (Phase 73).

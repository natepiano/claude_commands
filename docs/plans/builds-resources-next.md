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

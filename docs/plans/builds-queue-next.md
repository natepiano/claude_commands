# builds-queue — Next

## Items to consider

- [ ] **Derive the admission code version from imports**
  - Target: `scripts/build_queue/admission_code.py` (`ADMISSION_CODE_PATHS`) and its readers in `test_admission_protocol.py` and `test_admission_service.py`.
  - Why needed: `ADMISSION_CODE_PATHS` is a hand list of the files that make the admission code version. It drifts each time an import moves. natest Phase 22 adds `buildlog/repository.py` by hand because `record.py` now imports it.
  - Completion condition: the entry files stay explicit, the shell callers included. Their local Python dependencies, package initializers such as `scripts/buildlog/__init__.py` included, are found from their imports without running the imported modules. Tests prove that a new local import and a shell caller change each alter the version with no list edit.
  - Revealed by: builds showrunner design note, 2026-11-05 (during Phase 10s).

- [ ] **The admission service reads each fact once per pass**
  - Target: `scripts/build_queue/admission_service.py`, `scripts/build_queue/scope_activity.py`, and their admission helpers.
  - Why needed: `SystemEvidence` already captures reservation lifetimes for reuse. `ScopeActivityReader.run()` still repeats ancestor traversal. `read_system()` reads test-phase evidence for every reservation.
  - Completion condition: measure the remaining repeated reads and remove only demonstrated duplication. Preserve priority refresh, run identity, decisions, and the slot behavior in `_end`.
  - Revealed by: 10x simplify review, items 39-43, 2026-11-05; updated after Phase 23.

- [ ] **queue_fixture.head_reading stops changing the reading after the pass**
  - Target: `scripts/build_queue/queue_fixture.py` and its about 60 tests.
  - Why needed: `head_reading` changes `reading.ordered` after the pass, so a test can show a state the pass did not produce.
  - Completion condition: the fields are in the waiter row before the pass; the tests keep their assertions.
  - Revealed by: 10x simplify review, item 46, 2026-11-05.

- [ ] **One reader for CI slice member programs**
  - Target: `member_programs` (reads `comm` for CI classification) and `ScopeActivityReader` (reads the cmdline base name).
  - Why needed: two readers answer one question with different sources.
  - Completion condition: one reader, with proof that CI classification and each caller's error behavior stay the same.
  - Revealed by: 10x simplify review, item 18, ledger F014, 2026-11-05.

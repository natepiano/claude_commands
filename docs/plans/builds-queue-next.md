# builds-queue — Next

## Items to consider

- [ ] **Derive the admission code version from imports**
  - Target: `scripts/build_queue/admission_code.py` (`ADMISSION_CODE_PATHS`) and its readers in `test_admission_protocol.py` and `test_admission_service.py`.
  - Why needed: `ADMISSION_CODE_PATHS` is a hand list of the files that make the admission code version. It drifts each time an import moves. natest Phase 22 adds `buildlog/repository.py` by hand because `record.py` now imports it.
  - Completion condition: no hand list exists. The code version is the set of local modules that the admission entry modules import, found from their imports. A test proves that a new import of a local module changes the set with no list edit.
  - Revealed by: builds showrunner design note, 2026-11-05 (during Phase 10s).

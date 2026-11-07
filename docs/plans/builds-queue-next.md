# builds-queue — Next

## Items to consider

- [ ] **Derive the admission code version from imports**
  - Target: `scripts/build_queue/admission_code.py` (`ADMISSION_CODE_PATHS`) and its readers in `test_admission_protocol.py` and `test_admission_service.py`.
  - Why needed: `ADMISSION_CODE_PATHS` is a hand list of the files that make the admission code version. It drifts each time an import moves. natest Phase 22 adds `buildlog/repository.py` by hand because `record.py` now imports it.
  - Completion condition: the entry files stay explicit, the shell callers included. Their local Python dependencies, package initializers such as `scripts/buildlog/__init__.py` included, are found from their imports without running the imported modules. Tests prove that a new local import and a shell caller change each alter the version with no list edit.
  - Revealed by: builds showrunner design note, 2026-11-05 (during Phase 10s).

- [ ] **build_hold.py loads no admission server code at import**
  - Target: `scripts/build_hold/build_hold.py:40-46` (module-level `scripts.build_queue.admission_service` import); its one use is `admitted_processes()` at `:570-573` (`admission_service.read_state`).
  - Why needed: `build_targets.py:142` and `targets.py:21` import `build_hold`, so `eligible`, `reserve`, `register-local`, `wait-local` and `check-local` load `admission_service` and `memory_admit` even when they read no admission state. natest Phase 24 import profile (one `eligible` run): `build_hold` 328 ms cumulative, of which `admission_service` 275 ms; `admission_protocol` 161 ms; `memory_admit` 143 ms. The routing test module starts these commands 35 or more times. Measured by natest 2026-11-05: a scratch copy that moved the runtime import into `admitted_processes()` (TYPE_CHECKING import kept) ran `test_verify_ssh_targets_routing.py` in 3 pairs beside the unchanged tree (load 32-62); median 107.2 s -> 96.1 s wall (9.6 s, about 9%), 9.8 s CPU; pairs 147.1 -> 137.5, 107.2 -> 96.1, 83.6 -> 76.5 s; all 13 tests passed on both sides. Scratch diff: `/tmp/claude/unit/2113de4f-9913-485a-9a51-61558bfd81db/phases/24/profile/impl3/repair1/scratch_proposal.diff` (copy kept at `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/phases/next_build_hold/scratch_proposal.diff`). This does not prove every hold and admission caller stays correct: the phase runs the full build_hold, build_targets and admission suites.
  - Completion condition: the service import moves into `admitted_processes()`, or the state read gets a home that needs no service import; no test patches `build_hold.admission_service`. Then tell natest the builds commit; natest measures `test_verify_ssh_targets_routing.py` and `test_verify_lint_judgment.py` again. This item adds no import assertion: natest withdrew that proposed check on 2026-11-05 (a new check goes to the user first) and adds it after this item lands only if the user approves.
  - Waits for: the builds `build_hold.py` freeze (natedev's send.py cleanup) to end; builds sends natedev's main hash.
  - Revealed by: natest proposal, 2026-11-05 (sent with Phase 10o).

- [ ] **The admission service reads each fact once per pass**
  - Target: `scripts/lint/memory_admit.py`, `scripts/build_queue/admission_service.py`, `scripts/build_queue/scope_activity.py`.
  - Why needed: each scoped reservation's step lifetime is read about four extra times per pass. Priority labels, `ScopeActivityReader.run()` and the build run of a reservation with no `RunIdentity` also repeat, and `observed_test_phase(row)` runs before `test_phase_history` accepts the step.
  - Completion condition: one read of each per pass, or once per reservation for a stored run; decisions stay the same. A stored run does not see later changes, so check the slot effect in `_end`.
  - Revealed by: 10x simplify review, items 39-43, 2026-11-05.

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

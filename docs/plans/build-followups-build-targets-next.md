# Build targets: each machine takes jobs by its own rules, and a hold stops one machine — Next

## Items to consider

- [ ] **A second ssh build machine gets its own lock storage**
  - Target: `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`, `scripts/build_targets/targets.py`
  - Why needed: every `ssh` target shares the Mac's claim, block and backoff state; a second ssh machine needs independent storage before it can run beside the Mac, as the plan's opening states.
  - Completion condition: two configured ssh targets claim, block and back off independently, with the Mac's existing state paths unchanged; a two-target isolation test passes.
  - Revealed by: Phase 1

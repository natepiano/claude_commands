# Build targets: each machine takes jobs by its own rules, and a hold stops one machine — Next

## Items to consider

- [ ] **A second ssh build machine gets its own lock storage**
  - Target: `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`, `scripts/build_targets/targets.py`, `scripts/buildlog/holds.py`
  - Why needed: every `ssh` target shares the Mac's claim, block and backoff state; a second ssh machine needs independent storage before it can run beside the Mac, as the plan's opening states.
  - Completion condition: two configured ssh targets claim, block and back off independently, with the Mac's existing state paths unchanged; a two-target isolation test passes, and each target's block writes its hold events under its own target name (today `mac_test.start_hold_recorder` writes `mac` and `holds.HoldTarget` is `Literal["mac", "natedev"]`), which both reports read.
  - Revealed by: Phase 1

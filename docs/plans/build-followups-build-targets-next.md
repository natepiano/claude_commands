# Build targets: each machine takes jobs by its own rules, and a hold stops one machine — Next

## Items to consider

- [ ] **A second ssh build machine gets its own lock storage**
  - Target: `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`, `scripts/build_targets/target_config.py`, `scripts/build_targets/targets.py`, `scripts/build_hold/build_hold.py`, `scripts/buildlog/record.py`, `scripts/buildlog/holds.py`, `scripts/buildlog/report.py`, `scripts/buildlog/mac_report.py`
  - Why needed: every `ssh` target shares the Mac's claim, block and backoff state. The hold command also recognizes only `mac` and `natedev`, and the event writer rejects other target names.
  - Completion condition: two configured ssh targets claim, block and back off independently, with the Mac's existing state paths unchanged. `/build:hold hold|release --target <name>` operates on each configured ssh target independently. Each target's block writes accepted hold events under its own target name, and both reports read its current state and recorded intervals. Tests cover storage isolation, named-target hold/release dispatch, and event recording through both reports.
  - Revealed by: Phase 1; command dispatch and config-reader ownership updated after Phase 15.

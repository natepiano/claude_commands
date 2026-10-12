# Feature log: five crates' pending changelog bullets are copied into the log — Next

## Items to consider

- [ ] **An entry that is pending for a crate can no longer be given an exact version of that crate by a correction.**
  - Target: `scripts/feature_log/feature_log.py` (`amend --backfill --release`)
  - Why needed: an entry holds a crate as pending or as released, never both; today `amend --backfill --release X@1.2.3` on a note that holds `X@Unreleased` writes both, and the entry then prints in two release sections.
  - Completion condition: that call is refused before any write with one stderr line, and a test pins it.
  - Revealed by: Phase 1 (an entry can be marked pending or split, and the comparison covers only change headings)

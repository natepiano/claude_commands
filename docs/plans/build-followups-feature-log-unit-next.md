# Feature log: one note per current result, written only by a script — Next

## Items to consider

- [ ] **The hand-written Unreleased bullets of five published crates are copied into the log as pending entries**
  - Target: the vault's feature log for cargo-tile, hana_clerestory, tui_pane, hana_rigging and bevy_brp_mcp, written through `scripts/feature_log/feature_log.py`
  - Why needed: these crates' changelogs hold Unreleased bullets written by hand before live logging began (counted 2026-10-12: cargo-tile 163, hana_clerestory 31, tui_pane 23, hana_rigging 5, bevy_brp_mcp 1) and the log holds no pending entry for them, so `/release` reports every bullet as a difference for these crates and never stamps their version into the log (`docs/as-built/feature-log.md`, "Changelog export and release stamp"). The user decided on 2026-10-12, relayed by the showrunner: "yes feature log should copy those notes into the log". The unit director reads that as one pending entry per bullet, under the bullet's changelog heading and with the bullet's text as the entry's summary, since the comparison matches summaries per heading. The backfill already wrote notes for the behavior behind many of these bullets, without a pending marker, and no command marks an existing note pending (`amend --release` refuses `Unreleased`), so the run also sets what happens to a backfilled note that describes the same behavior as a copied bullet.
  - Completion condition: `feature_log.py changelog --crate <crate> --version Unreleased --compare <its CHANGELOG.md>` prints `match` for each of the five crates.
  - Revealed by: Phase 11

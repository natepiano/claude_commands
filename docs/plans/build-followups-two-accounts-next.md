# Two accounts: showrunners on one Claude account, unit directors on another — Next

## Items to consider

- [ ] **All consumers import shared whoami modules by one name.**
  - Target: scripts/whoami, scripts/accounts, scripts/agents, scripts/message, scripts/production, scripts/herdr and scripts/shutdown
  - Why needed: sibling and package imports coexist; agent_accounts, account, note_file, session_table and run_out register both names to preserve shared type identity and patched settings
  - Completion condition: all consumers import each shared whoami module by one name, the two-name registrations are removed, and the affected suites pass with shared type identity and note-directory relocation preserved
  - Revealed by: Phases 12 and 21; herdr and shutdown consumers found at Phase 61
- [ ] **A failed restart shows its saved screen.**
  - Target: scripts/shutdown (`shutdown status` and the session-side notice of a failed restart), reading `PromptNotSubmitted.screen` from the `<move note>.prompt-outcome.json` receipt; also the live-session status line `restart prompt not confirmed; session is running — check it` in `shutdown._shutdown_record_lines`, which skips `settle.status_record_lines` (Phase 70), so a screen added only there never shows for a live `restart failed` record
  - Why needed: phase 69 saves the last screen a failed opening-prompt submit saw, but no user surface shows it, so diagnosing `restart prompt not submitted` means opening the receipt by hand
  - Completion condition: `shutdown status` or the session-side notice prints the saved screen for a failed restart, and the phone push stays unchanged, since a push carries only what the user must do
  - Revealed by: Phase 69 plan review; showrunner approved 2026-10-18

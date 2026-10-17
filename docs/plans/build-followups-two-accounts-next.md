# Two accounts: showrunners on one Claude account, unit directors on another — Next

## Items to consider

- [ ] **All consumers import shared whoami modules by one name.**
  - Target: scripts/whoami, scripts/accounts, scripts/agents, scripts/message, scripts/production, scripts/herdr and scripts/shutdown
  - Why needed: sibling and package imports coexist; agent_accounts, account, note_file, session_table and run_out register both names to preserve shared type identity and patched settings
  - Completion condition: all consumers import each shared whoami module by one name, the two-name registrations are removed, and the affected suites pass with shared type identity and note-directory relocation preserved
  - Revealed by: Phases 12 and 21; herdr and shutdown consumers found at Phase 61

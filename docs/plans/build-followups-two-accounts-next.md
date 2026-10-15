# Two accounts: showrunners on one Claude account, unit directors on another — Next

## Items to consider

- [ ] **Account, alert, worker, roster and production scripts import shared whoami modules by one name.**
  - Target: scripts/whoami, scripts/accounts, scripts/agents, scripts/message and scripts/production
  - Why needed: sibling and package imports coexist; agent_accounts, account, note_file and session_table register both names to preserve shared type identity and patched settings
  - Completion condition: all consumers import each shared whoami module by one name, the two-name registrations are removed, and the affected suites pass with shared type identity and note-directory relocation preserved
  - Revealed by: Phases 12 and 21

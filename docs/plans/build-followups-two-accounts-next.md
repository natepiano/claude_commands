# Two accounts: showrunners on one Claude account, unit directors on another — Next

## Items to consider

- [ ] **Account, alert, worker, roster and production scripts import shared whoami modules by one name.**
  - Target: scripts/whoami, scripts/accounts, scripts/agents, scripts/message and scripts/production
  - Why needed: sibling and package imports coexist; agent_accounts, account, note_file and session_table register both names to preserve shared type identity and patched settings
  - Completion condition: all consumers import each shared whoami module by one name, the two-name registrations are removed, and the affected suites pass with shared type identity and note-directory relocation preserved
  - Revealed by: Phases 12 and 21
- [ ] **Swap rows name a live run that has no holder marker by its launcher's session.**
  - Target: scripts/accounts/accounts.py `_workers_swap_lines`, scripts/agents/codex_mesh.py run holders
  - Why needed: live runs whose holder never wrote an active marker print `unknown session (<run folder>)` although a live launcher's parent Claude session names showrunner · unit
  - Completion condition: such a run's rows read `showrunner · unit · worker`, resolved from the live launcher's ancestry; a test pins it
  - Revealed by: Phase 36 live swap dry run (showrunner: next)

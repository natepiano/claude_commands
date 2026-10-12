# Two accounts: showrunners on one Claude account, unit directors on another — Next

## Items to consider

- [ ] **The account and alert tools load their shared account code one way, so a shared type is never treated as two different types.**
  - Target: the repository's account, alert and seat scripts
  - Why needed: scripts load the shared whoami modules under two names; the alerts phase made one shared module safe under both, and every other shared module stays exposed to the same failed type check
  - Completion condition: every script imports those modules by one name, the two-name registration is removed, and the suites pass
  - Revealed by: Phase 12

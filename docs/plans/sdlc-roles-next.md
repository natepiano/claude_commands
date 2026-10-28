# sdlc-roles — Next

## Items to consider

- [ ] **The name count also reads the warning text that shell scripts print to stderr through heredocs**
  - Target: `scripts/production/roles_audit.py` (shell output extraction)
  - Why needed: `cat >&2 <<EOF` bodies are skipped, so shell name counts are lower bounds
  - Completion condition: fixtures prove stderr `cat` heredocs count in Names and their retired uses report their physical source lines in `check`, while non-output heredocs stay excluded; record any changed lexical denominator apart from the Phase 4 scan speed-up
  - Revealed by: Phase 1

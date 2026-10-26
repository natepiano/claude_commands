# sdlc-roles — Next

## Items to consider

- [ ] **The name count also reads the warning text that shell scripts print to stderr through heredocs**
  - Target: `scripts/production/roles_audit.py` (shell output extraction)
  - Why needed: `cat >&2 <<EOF` bodies are skipped, so shell name counts are lower bounds
  - Completion condition: a fixture with a stderr heredoc counts its names; the baseline header no longer lists this limit
  - Revealed by: Phase 1

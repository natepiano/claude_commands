# sdlc-devtools-tests — Next

## Items to consider

- [ ] **The mesh queue grace is set in config, not in code**
  - Target: `scripts/agents/codex_mesh.py` (`QUEUE_GRACE_SECS`, `queue_grace_secs()`), `config/agents.conf`
  - Why needed: the user's no-hard-coded-config rule (2026-10-31): a tunable lives in one config place and is read at use. The 3 s production grace is still a code literal; `CODEX_MESH_QUEUE_GRACE_SECS` is only a test override. models-unit asked for one shared mesh setting reader and a `[codex_mesh.options] queue_grace_secs` line in `config/agents.conf`. The showrunner ruled (2026-10-31): the 3.0 was there before the rule, so it moves only when someone next has a reason to change that code.
  - Completion condition: the grace reads `[codex_mesh.options] queue_grace_secs` from agents.conf at use through one shared mesh setting reader, the env var stays a test override, no code copy of 3.0 remains, and tests cover a set, a missing and an invalid config value.
  - Revealed by: Phase 6b (models-unit review of b4b490e4; showrunner ruling 2026-10-31).

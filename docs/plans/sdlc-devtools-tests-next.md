# sdlc-devtools-tests — Next

## Items to consider

- [ ] **The mesh queue grace is set in config, not in code**
  - Target: `scripts/agents/codex_mesh.py` (`QUEUE_GRACE_SECS`, `queue_grace_secs()`), `config/agents.conf`
  - Why needed: the user's no-hard-coded-config rule (2026-10-31): a tunable lives in one config place and is read at use. The 3 s production grace is still a code literal; `CODEX_MESH_QUEUE_GRACE_SECS` is only a test override. models-unit asked for one shared mesh setting reader and a `[codex_mesh.options] queue_grace_secs` line in `config/agents.conf`. The showrunner ruled (2026-10-31): the 3.0 was there before the rule, so it moves only when someone next has a reason to change that code.
  - Completion condition: `queue_grace_secs()` reads `queue_grace_secs` through `conf.read("agents").number("queue_grace_secs")` at use. The configuration places `# range: 0..` directly above that key under `[codex_mesh.options]`. No second parser or code copy of 3.0 remains. `CODEX_MESH_QUEUE_GRACE_SECS` remains a test override. Tests use `CONF_DIR` and cover configured values, valid-history recovery, and refusal without usable history. Land the unchanged configuration value before the consumer.
  - Revealed by: Phase 6b (models-unit review of b4b490e4; showrunner ruling 2026-10-31).
- [ ] **Retire says when it closes the unit's own pane**
  - Target: `scripts/production/retire_unit.py`, `scripts/production/test_retire_unit.py`
  - Why needed: the dry run names the pane closure, but a successful real closure (`retire_unit.py:959`) prints no confirmation.
  - Completion condition: a successful pane closure prints exactly `closed: pane <pane id> (shell <pid>)` once; a failed closure prints no success line; a completed rerun still prints only `<unit> already retired`. Fixture tests cover all three outcomes.
  - Revealed by: Phase 7k

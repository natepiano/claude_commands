# sdlc-devtools-tests — Next

## Items to consider

- [ ] **Read the mesh queue grace from configuration**
  - Target: `scripts/agents/codex_mesh.py` (`QUEUE_GRACE_SECS`, `queue_grace_secs()`), `config/models.conf`.
  - Why needed: the production grace is still a code literal, against the user's no-hard-coded-config rule (2026-10-31). Keep the showrunner's 2026-10-31 timing decision.
  - Completion condition: `queue_grace_secs()` reads `conf.read("models").number("queue_grace_secs")` at use. `# range: 0..` sits directly above the key under `[codex_mesh.options]`. No code copy of `3.0` remains. `CODEX_MESH_QUEUE_GRACE_SECS` stays the test override. `CONF_DIR` tests cover configured values, valid-history recovery, and refusal without usable history. Land the unchanged configuration value before the consumer.
  - Revealed by: Phase 6b; target corrected by the Phase 3t architect review (the registry moved to `config/models.conf`).
- [ ] **A simplify lens test leaves no child process alive**
  - Target: `scripts/direct/test_simplify_lens.py`
  - Why needed: an `agent_exec` child of this test stayed alive for 8 hours after the test ended.
  - Completion condition: the test module ends with no child process left; a test checks the process tree after the module ends.
  - Revealed by: Phase 3t (sdlc [3129d2dd] placed it next).
- [ ] **One pane scan for rename displays**
  - Target: `scripts/production/rename_unit.py` (`repair_displays`), `scripts/herdr/herdr.py` (`Herdr.retitle_row`)
  - Why needed: both functions scan panes; `rename._set_displays` already delegates. Two scans are a copy.
  - Completion condition: one pane scan serves both. The native-session proof and the anomaly output stay.
  - Revealed by: herdr-unit (sdlc [16a2cfa0] placed it next, after 3t).
- [ ] **Carry the planned port and responsibilities into unit launch**
  - Target: `commands/producer/to_production.md`, `commands/showrunner/produce.md` (`LaunchUnits`), `scripts/production/test_add_unit.py`.
  - Why needed: `add_unit.py` already accepts and records both fields (`add_unit.py:1297`, `test_add_unit.py:216`). The documented launch command (`produce.md:420`) does not carry them from planning.
  - Completion condition: preserve both fields through the planning handoff. Pass `--port` and repeated `--responsibility` options to `add_unit.py`. Keep saved `.unit.toml` precedence. Test the handoff and final record.
  - Revealed by: herdr-unit (sdlc [16a2cfa0], [7694b0f3]); Phase 3y architect review.

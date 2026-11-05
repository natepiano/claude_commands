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
- [ ] **A new unit launch records its port and responsibilities**
  - Target: `scripts/production/production_lifecycle.py`, the launch path from `produce.md`
  - Why needed: port and responsibilities have no record between `to_production` and launch. herdr 9ae12b0be already finds plans from staff rows and `.unit.toml`.
  - Completion condition: launch takes `--port` and `--responsibility` and records both; a test covers the record.
  - Revealed by: herdr-unit (sdlc [16a2cfa0], [7694b0f3]).

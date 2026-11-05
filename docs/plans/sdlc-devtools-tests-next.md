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
- [ ] **Delete `PausedSeat.turn_live`**
  - Target: `scripts/lib/resume_message.py:97` (`PausedSeat.turn_live`) and its readers at lines 38 and 54.
  - Why needed: the field repeats the Resumable/Idle tag. The only production caller passes only Resumable seats, so it is always true.
  - Completion condition: the field is deleted; resume output is unchanged for every caller.
  - Revealed by: models-unit Phase 3s simplify lens (sdlc [89f9ddb9]): do it in the next phase that touches `resume_worker_command`; leave 3t 738d508a3 as is.
- [ ] **Cut the Git process starts in fast_track and the copies refresh**
  - Target: `scripts/production/fast_track.py` (`reconcile` and its Git calls), `scripts/production/test_fast_track.py`, `scripts/copies/copies.py` (remote refresh).
  - Why needed: build speed is a top priority. The natest P33 profile measured: `test_fast_track.py` drives 2937 Git calls in `fast_track.py` (27.6 s). `reconcile` repeats `rev-parse` (898), `update-ref` (777) and `cat-file` (283). The `copies.py` remote refresh makes 403 fetches (870 calls, 10.1 s).
  - Completion condition: one injected Git runner serves `fast_track.py`, so tests start fewer processes. `reconcile` reads each ref and object once. The remote refresh fetches each remote once. Fewer measured Git calls and less time in a new profile. Add no check.
  - Revealed by: builds natest P33 profile (sdlc [125256f1]); after 3t/3y.
- [ ] **Delete the move-conductor env copy in fast_track**
  - Target: `scripts/production/fast_track.py:421` (the `PYTHONUNBUFFERED`, `REFUSAL_WATCH_INVENTORY`, `STAFF_OPERATION_FD` filter).
  - Why needed: the filter is a copy of the three move-conductor env names, a work-around. `stop_resume._pane_command` is the one place.
  - Completion condition: the filter is deleted; the fast-track check environment comes from `os.environ` as is; the fast_track tests pass.
  - Revealed by: sdlc [24514b9d]; after shutdown's env-leak fix 1d06ff2f0 lands (after 3y).

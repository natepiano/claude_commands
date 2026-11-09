# sdlc-devtools-tests — Next

## Items to consider

- [ ] **A simplify lens test leaves no child process alive**
  - Target: `scripts/direct/test_simplify_lens.py`
  - Why needed: an `agent_exec` child of this test stayed alive for 8 hours after the test ended.
  - Completion condition: the test module ends with no child process left; a test checks the process tree after the module ends.
  - Revealed by: Phase 3t (sdlc [3129d2dd] placed it next).
- [ ] **One pane scan for rename displays**
  - Target: `scripts/agents/shutdown.py` (`rename_repair_displays`), `scripts/herdr/herdr.py` (`Herdr.retitle_row`, `Herdr.scan_row_panes`).
  - Why needed: `rename_repair_displays` and `retitle_row` use separate implementations to match panes to native sessions.
  - Completion condition: both functions use one pane-scan implementation. Keep session-record updates, pane titles, tab labels, workspace labels, native-session proof and anomaly output. Test these behaviors through rename.
  - Revealed by: herdr-unit (sdlc [16a2cfa0] placed it next, after 3t).
- [ ] **Carry the planned port and responsibilities into unit launch**
  - Target: `commands/producer/to_production.md`, `commands/showrunner/produce.md` (`LaunchUnits`), `scripts/production/test_add_unit.py`.
  - Why needed: `add_unit.py` already accepts and records both fields (`add_unit.py:1297`, `test_add_unit.py:216`). The documented launch command (`produce.md:420`) does not carry them from planning.
  - Completion condition: preserve both fields through the planning handoff. Pass `--port` and repeated `--responsibility` options to `add_unit.py`. Keep saved `.unit.toml` precedence. Test the handoff and final record.
  - Revealed by: herdr-unit (sdlc [16a2cfa0], [7694b0f3]); Phase 3y architect review.
- [ ] **Use the existing active and idle worker resume states**
  - Target: `scripts/agents/inventory.py` (`worker_resumes`), `scripts/lib/resume_message.py` (`PausedSeat`, `resume_worker_command`), and their tests.
  - Why needed: `Resumable` and `Idle` already distinguish the states, but `PausedSeat.turn_live` repeats that distinction.
  - Completion condition: remove `turn_live` and use the existing states. Preserve active-worker commands, idle-worker text, and existing threads. Test both states through `stop_resume`.
  - Revealed by: models-unit Phase 3s simplify lens; target corrected by the Phase 8g architect review.
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

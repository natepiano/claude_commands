# Producers

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** morty, brain and dwight are producers: staff kind `producer`, one command each, one models line each, one start path, and recovery after a herdr restart or a reboot.

> **As-built disposition: amend** — `docs/as-built/staff-accessors.md`, `docs/as-built/herdr-restore.md` and `docs/as-built/shutdown-and-restart.md` record the result.

## Source

The sdlc showrunner, 2026-11-10 [5aa0861f], with the user's go. User words: morty, brain and dwight are producers. Each is an individual operator with its own job. 'producer' is only a way to manage them.

1. Staff kind 'producer' replaces kind 'agent'. morty and brain move to it (migrate once at deploy, through staff code). Update the staff.py agent rules. brain's row has no home now; give it one.
2. Commands in a folder: commands/producer/morty.md, brain.md, dwight.md, so /producer:morty, /producer:brain, /producer:dwight. /associate-producer becomes /producer:morty. Rename every 'associate producer' reference. No shared role doc.
3. config/models.conf gets a [producers] section, one line each: `<name>=<family>:<model>:<effort>`.
4. Account by family: a codex producer starts on the account of the workers group; a claude producer starts on the account of the showrunners group. A 'move' of that group moves its producers too.
5. One start path: 'resume morty', 'resume brain', 'resume dwight' each start the producer in its own tab of the herdr workspace 'producers', on its family account, in its same conversation. A herdr restart and a reboot bring back all three through that same path, with no hand step.
6. The user renamed the herdr workspace 'morty' to 'producers'. Change any code or config that still names the 'morty' workspace.
7. dwight: staff row (kind producer), /producer:dwight, doc at ~/rust/hanadocs/dwight/, first start. Its job is 'strategist'. Write only the role name and the management lines; add no invented duties.

Rules: tests use no live units. Clean up all test output.

The sdlc showrunner, 2026-11-10 [387687f7], the user's model lines:

```
morty=codex:astra:high
brain=codex:astra:medium
dwight=claude:fable:high
```

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-10T21:46:00-05:00
- **Stack:** Python 3.13, shell, herdr, Codex and Claude sessions.
- **Layout:** `scripts/production/` (staff), `scripts/agents/` (stop, resume, restart), `scripts/accounts/` (accounts and moves), `scripts/herdr/` (workspaces and panes), `scripts/maintenance/` (herdr upkeep), `config/`, `commands/`, `docs/`.
- **Other repository:** `~/rust/hanadocs` — the user's notes. Use `git -C`; never cd into it.
- **Key facts (research, item97, 2026-11-10):**
  - `scripts/production/staff.py:36` `Kind` holds `agent`. Rules at 904 (no parent), 933 (worktree), 935 (doc), 951 (root row), 1186 (staff path), plus 421, 1379, 1381, 1713, 1734, 1822, 1920, 1973.
  - Callers of kind `agent`: `staff_sources.py:424,431`, `agents/restart.py:371`, `agents/shutdown_record.py:32`, `message/send.py:3551`.
  - Row field `home` is the account configuration folder (`herdr/pane_launch.py:53`). morty has `home: /home/natepiano/.codex2`. brain has no `home`.
  - `agents/member_account.py:24` sends every Codex row to the workers account; `:26` sends other Claude rows to the units account.
  - Codex worker swaps move only `kind="worker"` rows (`accounts/accounts.py:2994`). The Claude group membership (`accounts.py:5170`) holds no agent rows.
  - `herdr/herdr.py:3210-3216`: a row with no saved position uses its own name as the workspace.
  - Automatic boot restore is not installed (`docs/as-built/herdr-restore.md:131`). `herdr-upkeep` (`/etc/nixos/modules/linux/herdr-upkeep.nix`) runs `scripts/maintenance/herdr_upkeep.py` on a timer.
  - `config/models.conf` rows use `task=model:effort`; `scripts/lib/models_registry.py` parses sections.
- **Test:** `env -C /home/natepiano/worktrees/devtools HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` at the phase gate; each worker runs the test modules for its files.
- **Lint:** `basedpyright <the changed Python files>` gives `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - Tests use fixture staff, fixture homes and a fake herdr. They never touch a live unit, pane, account or staff file.
  - Each test removes its output (tempfile.TemporaryDirectory).
  - Old saved data gets a one-off conversion program, run once at deploy and then deleted. The main code reads no old shape.
  - No hand staff write. Staff changes go through staff code.
  - Changeable names (the workspace name, models) live in config, not in code.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — Staff kind producer replaces agent  · status: done

**Depends on:** none

#### As-built

- Staff kind `producer` replaces kind `agent` in `scripts/production/staff.py`, `staff_sources.py`, `agents/restart.py`, `agents/shutdown_record.py` and `message/send.py`. A producer row has no parent, may hold a worktree and a doc, may be a root row, and starts a staff path.
- Tests, `commands/agents.md` and the herdr-restore and shutdown-and-restart as-builts use the word producer.
- Deploy: `convert_producers.py`, outside the repository, changes morty and brain to `producer` through the staff transaction and gives brain the workers account home. A second run changes nothing. It runs once on natedev and the Mac right after the stack lands, then is deleted.

### Phase 2 — One command and one models line per producer  · status: done

**Depends on:** none (runs beside Phase 1; no shared file)

#### As-built

- `/associate-producer` is now `/producer:morty` (`commands/producer/morty.md`); its operations doc is `docs/producer/morty-operations.md`. `/producer:brain` and `/producer:dwight` hold the name, the job name and the management lines only.
- The Codex sync names the folder command `producer-morty`.
- `config/models.conf` `[producers]` holds `morty=codex:astra:high`, `brain=codex:astra:medium`, `dwight=claude:fable:high`. `models_registry.launch_settings("producers", name)` reads and checks them.
- hanadocs `morty/README.md` and `morty/checkins.json` name the new paths, uncommitted in hanadocs.

### Phase 3 — One start path and account moves for producers  · status: done

**Depends on:** Phase 1, Phase 2

#### As-built

- `resume <producer>` starts the producer in its own tab of the `producers` workspace (`config/staff.conf` `PRODUCER_WORKSPACE`), on its family account, with its `[producers]` model and effort, in the row's `current_session`. An explicit `--to` still wins.
- Account by family: a Codex producer uses the workers account, a Claude producer uses the showrunners account.
- A workers swap moves the eligible Codex producers; a showrunners group move moves the eligible Claude producers. `accounts.eligible_producers` is the one selector (active, unpaused, local, family). Each move writes the row's `home`.
- `accounts.producer_settings` reads one producer's `[producers]` line. A bad line leaves that producer out, with one warning per operation (`settings_report_scope`).
- `pane_launch.write` owns the retained producer binding. `pane_launch.producer_start` is the one writer of the start contract, written before each retained-launch start, exec included.
- Resume compares the source and destination pane locations before it lists panes, so a resume in its own pane makes no pane list call. `pane_launch.read_entries` returns no entries and makes no lock file when the record folder is absent.

**Files:**
- `scripts/agents/member_account.py` — account by family
- `scripts/agents/restart.py` — `producer_launch`, `producer_resume`
- `scripts/agents/stop_resume.py` — producer resume and move
- `scripts/herdr/herdr.py` — `row_workspace` for a producer
- `scripts/herdr/pane_launch.py` — producer binding, start contract, publication lock
- `scripts/accounts/accounts.py` — `eligible_producers`, `producer_settings`, producer selection in swaps and group moves
- `config/staff.conf` — `PRODUCER_WORKSPACE=producers`

**Binds later work:** `home` records an account; it does not prove that a start finished. `pane_launch.write` owns retained producer bindings and `pane_launch.producer_start` owns start-contract writes. Automatic recovery uses `producer_settings` inside one `settings_report_scope`.

**Gotchas:**
- A move writes the target `home` before the start is confirmed.
- Restore reads `[producers]` for every producer row; a test fixture row of kind `producer` needs a `[producers]` line.
- The `.publication.lock` is shared by every pane record writer. Lock tests must find their target lock by file identity, not by the first exclusive lock.
- The cloud gate runner's host name is not the selection machine; fixtures use the selection machine.
- Run the full merge gate, not only the touched test modules: restore, watchdog and lock tests break outside them.

**Ruled out:** a producer-only resume, move or restart branch — producers use the shared path, with only family and model from `[producers]`.

### Phase 3a — A group move rerun finishes a producer that exited early  · status: done

**Depends on:** Phase 3

Moved out of phase 3 by the hard landing rule (repair rounds 6 and 7), with sdlc's go [03774015]. Findings F009, F014, F015, F017 and F018 of phase 3.

#### Work Order

**Goal:** a repeated group move or workers swap starts every producer that the first run stopped and did not bring up, also when the producer exited early after its `home` was written.

**Spec:**

1. Today the move writes the target `home` before the move confirms the start. A producer that exits in that window keeps the target `home`, so a rerun skips it and reports success with zero sessions.
2. A rerun selects each eligible stopped producer, also when its saved `home` already matches the target. It skips only a producer verified live on the target account. Unknown liveness is an explicit failure. Keep the pause and machine limits. `home` is account data, not proof that a move finished.
3. Producers use the same selection code as units and showrunners. Only the family and model come from `[producers]`.
4. Correct the `source unavailable` row in `RestartTests.test_saved_tab_reuses_only_the_targets_shell_and_splits_beside_a_sibling` (`scripts/agents/test_restart.py`). Its destination-close mock always returns False. Make the callback record and perform the fixture close. Require zero destination close calls when the source is unavailable.
5. Add a row to the existing group move test in `scripts/agents/test_account_move.py`: the first move writes the target `home`, then start confirmation fails; a repeated move makes another start attempt. Keep the Codex swap row in `scripts/accounts/test_accounts.py` as a control. Each new row fails on the phase 3 code.
6. Delete the producer-only branches that the shared path can carry (F017):
   - `stop_resume.py` move check (near line 1235): a unit or showrunner with no matching tab skips the target-account check and reaches `_stop`; only a producer gets the check. Use one rule for all rows.
   - `member_account.py` `repair_home` (near line 41) repairs only producer homes.
   - `stop_resume.py` (near line 921) keeps a producer-only `home` write condition.
   - `restart.py` `producer_resume` (near line 186) repeats the saved policy, remote-control and plugin handling of `pane_record.resume_arguments`.
   Only the family and model come from `[producers]`. Add rows to the existing tests for units and showrunners with no matching tab.
7. Correct the Codex row in `StopResumeTests.test_own_empty_pane_executes_the_saved_launch` (`scripts/agents/test_stop_resume.py`). Its fake `os.execvpe` publishes a binding, and its `SystemExit` runs the contract cleanup; a real exec does neither. Model a successful exec with no launch publication and no Python cleanup after the exec. Verify the next resume from the state that the real exec leaves (F018).

**Constraints from prior phases:**
- `home` records an account. It does not prove that a start finished.
- `pane_launch.write` owns retained producer bindings.
- `pane_launch.producer_start` owns start-contract writes.
- Keep the source and destination session qualification during a pane close.
- Keep a committed destination when the source inspection or close fails.

**Files:**
- `scripts/accounts/accounts.py` — rerun selection
- `scripts/agents/stop_resume.py` — move start confirmation, `home` write order, the shared move check
- `scripts/agents/member_account.py` — shared `repair_home`
- `scripts/agents/restart.py` — shared resume arguments
- `scripts/agents/pane_record.py` — shared `resume_arguments`
- `scripts/herdr/pane_launch.py` — existing publication and start-contract ownership
- `scripts/agents/test_stop_resume.py` — rows and the exec fixture
- `scripts/agents/test_account_move.py` — rows
- `scripts/accounts/test_accounts.py` — rows
- `scripts/agents/test_restart.py` — the F007 row
- `scripts/agents/test_pane_record.py` — retained argument rows
- `scripts/herdr/test_pane_launch.py` — publication rows, when `pane_launch.py` changes

**Workers:** 1 writer.

**State:** the existing row `home`, the retained launch binding and the transient start contract. Add no new persistent state.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`.

### Phase 4 — A herdr restart and a reboot bring the producers back  · status: pending

**Depends on:** Phase 3a

#### Work Order

**Goal:** after a herdr restart or a reboot, each producer comes back through the Phase 3 start path, with no hand step.

**Spec:**

1. The herdr upkeep run first brings the shared herdr server back through the existing server restore, because resume needs pane access.
2. It then selects the active, local, unpaused producer rows from staff and calls `stop_resume.resume(str(row.id))`, the same code as `resume <name>`.
3. A paused producer stays down. The pause is the user's off switch.
4. It reads settings with `accounts.producer_settings` inside one `settings_report_scope` per upkeep run. A bad `[producers]` line skips that producer with one warning; the others still start.
5. A start that fails is reported once per upkeep run and retried on the next upkeep run. One failed producer does not stop the others.
6. Starts use the existing operation deadlines and stay inside the upkeep worker budget in `config/herdr_upkeep.toml` (60 seconds today; one start can take 20 seconds).
7. Keep the layout save and the detection-rule refresh.
8. If the upkeep timer does not run at boot on natedev, name the nixos change for the showrunner; do not push nixos.
9. Add rows to the existing upkeep tests with a fake herdr: an absent server, both families, a paused row, a remote row, a bad settings line, a failed start, and a repeated upkeep run.

**Constraints from prior phases:**
- Phase 3 gives the account choice, the `[producers]` launch settings, the same conversation and the producer tab. Phase 3a gives the one shared resume and move path. Add the automatic start through that path only.
- Start admitted staff rows only. Phase 5 owns a first start.
- Keep the difference between a stopped and an unknown liveness. Reuse the existing liveness evidence and launch functions.
- Add no new checks and no stored failure record.
- The nixos module starts `herdr-upkeep` from a graphical login. That alone does not prove a start before login.

**Files:**
- `scripts/maintenance/herdr_upkeep.py` — the automatic start and the existing upkeep operations
- `scripts/maintenance/test_herdr_upkeep.py` — rows
- `config/herdr_upkeep.toml` — the existing cadence and worker budget
- `scripts/agents/stop_resume.py` — the shared `resume` entry and its result
- `scripts/agents/test_stop_resume.py` — shared resume rows
- `scripts/accounts/accounts.py` — `producer_settings` and `settings_report_scope`
- `scripts/herdr/herdr.py` — the existing server restore
- `scripts/herdr/test_herdr.py` — the existing server tests

**Workers:** 1 writer.

**State:** none.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`. A test row proves that another eligible producer starts when one producer fails. A test row proves that a later upkeep run retries the failed producer and does not start a second copy of a running conversation.

### Phase 5 — dwight's first start  · status: pending

**Depends on:** Phase 4

#### Work Order

**Goal:** dwight has a doc folder, a staff row of kind producer, and a live first conversation in the `producers` workspace.

**Spec:**

1. Create `~/rust/hanadocs/dwight/README.md` with the role name (strategist) and the management lines only.
2. The shared resume entry recognizes a configured producer that has no staff row. It starts a new conversation with no resume argument, with the `[producers]` family, model and effort, the account rule and the producer workspace.
3. It proves the native identity through the existing herdr path, admits the row with `staff.ensure(..., runtime_id=<proved identity>)`, and completes the retained publication through `pane_launch.staff_admitted`. Later resumes use the row's `current_session`.
4. Start dwight. Report the model the conversation runs. The user expects Fable 5.2.

**Constraints from prior phases:**
- `StaffRow.current_session` stays required (`scripts/production/staff.py:330`). A first-start request is its own type, separate from an admitted `StaffRow`; do not use an optional identity.
- After an interrupted admission, reuse the existing native identity and launch evidence.
- Keep the family and model choice apart from the shared launch code.
- Run the tests with fixture staff and a fake herdr before the live dwight start.

**Files:**
- `scripts/agents/stop_resume.py` — first-start selection at the shared resume entry
- `scripts/agents/restart.py` — shared launch arguments
- `scripts/agents/member_account.py` — the existing account choice
- `scripts/agents/shutdown.py` — command routing
- `scripts/herdr/herdr.py` — the existing launch and native identity proof
- `scripts/herdr/pane_launch.py` — `staff_admitted` publication
- `scripts/production/staff.py` — the existing admission transaction
- `scripts/production/add_showrunner.py` — read only: the existing admission pattern
- `scripts/agents/test_stop_resume.py` — first start and the next resume
- `scripts/agents/test_shutdown_cli.py` — `resume dwight` routing
- `scripts/production/test_staff.py` — admission rows
- `~/rust/hanadocs/dwight/README.md` — the role name and the management lines

**Workers:** 1 writer.

**State:** a new staff row for dwight.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`. Fixture tests cover the first admission, a failed admission and the next resume of the same conversation. The live start reports the model identifier, and `resume dwight` reaches the same live conversation.

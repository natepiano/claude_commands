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

#### Work Order

**Goal:** `resume <producer>` starts the producer in its own tab of the `producers` workspace, on its family account, with its `[producers]` model and effort, in its same conversation; a group move moves its producers.

**Spec:**

1. A producer row reads its family, model and effort from `[producers]`.
2. Account by family: a codex producer uses the workers account; a claude producer uses the showrunners account. An explicit `--to` still wins.
3. The workspace name is a config value, `producers`. Each producer has its own tab, named for the producer. No code names a `morty` workspace; the test fixture at `scripts/agents/test_pane_record.py:246` uses `producers`.
4. Resume keeps the same conversation (the row's `current_session`).
5. A workers swap moves the codex producers with the workers. A showrunners group move moves the claude producers. Each move updates the row's `home`.
6. Add rows to the existing tests the research lists (resume account, workspace, group move). Add no new test file unless no test covers the behavior.

**Files:**
- `scripts/agents/member_account.py` — account by family
- `scripts/agents/restart.py` — model and effort from `[producers]`
- `scripts/agents/stop_resume.py` — resume and move of a producer row
- `scripts/herdr/herdr.py` — `row_workspace` for a producer
- `scripts/accounts/accounts.py` — group moves and workers swaps take producers
- `config/staff.conf` — `PRODUCER_WORKSPACE=producers`
- `scripts/agents/test_restart.py` — rows
- `scripts/agents/test_stop_resume.py` — rows
- `scripts/agents/test_pane_record.py` — the `producers` fixture
- `scripts/herdr/test_herdr.py` — rows
- `scripts/accounts/test_accounts.py` — rows
- `scripts/agents/test_account_move.py` — rows

**Workers:** 1 writer.

**State:** none beyond the row `home` that a move writes now.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`.

### Phase 3a — A group move rerun finishes a producer that exited early  · status: pending

**Depends on:** Phase 3

Moved out of phase 3 by the hard landing rule (repair rounds 6 and 7), with sdlc's go [03774015]. Findings F009, F014, F015, F017 and F018 of phase 3.

#### Work Order

**Goal:** a repeated group move or workers swap starts every producer that the first run stopped and did not bring up, also when the producer exited early after its `home` was written.

**Spec:**

1. Today the move writes the target `home` before the move confirms the start. A producer that exits in that window keeps the target `home`, so a rerun skips it and reports success with zero sessions.
2. A rerun selects each eligible stopped producer, also when its saved `home` already matches the target. It skips only a producer verified live on the target account. Unknown liveness is an explicit failure. Keep the pause and machine limits. `home` is account data, not proof that a move finished.
3. Producers use the same selection code as units and showrunners. Only the family and model come from `[producers]`.
4. The phase 3 F007 source-unavailable row in `scripts/agents/test_restart.py` (near line 2017) uses a destination-close mock that always returns False. Make the callback record and perform the fixture close, and assert that source unavailability makes no destination close call.
5. Add a row to the existing group move test in `scripts/agents/test_account_move.py`: the first move writes the target `home`, then start confirmation fails; a repeated move makes another start attempt. Keep the Codex swap row in `scripts/accounts/test_accounts.py` as a control. Each new row fails on the phase 3 code.
6. Delete the producer-only branches that the shared path can carry (F017):
   - `stop_resume.py` move check (near line 1235): a unit or showrunner with no matching tab skips the target-account check and reaches `_stop`; only a producer gets the check. Use one rule for all rows.
   - `member_account.py` `repair_home` (near line 41) repairs only producer homes.
   - `stop_resume.py` (near line 921) keeps a producer-only `home` write condition.
   - `restart.py` `producer_resume` (near line 186) repeats the saved policy, remote-control and plugin handling of `pane_record.resume_arguments`.
   Only the family and model come from `[producers]`. Add rows to the existing tests for units and showrunners with no matching tab.
7. The Codex own-pane exec test in `scripts/agents/test_stop_resume.py` (near line 1193) uses a fake `os.execvpe` that publishes a binding, and its `SystemExit` runs the contract cleanup. A real exec does neither. Make the fake match the real exec (F018).

**Files:**
- `scripts/accounts/accounts.py` — rerun selection
- `scripts/agents/stop_resume.py` — move start confirmation, `home` write order, the shared move check
- `scripts/agents/member_account.py` — shared `repair_home`
- `scripts/agents/restart.py` — shared resume arguments
- `scripts/agents/test_stop_resume.py` — rows and the exec fixture
- `scripts/agents/test_account_move.py` — rows
- `scripts/accounts/test_accounts.py` — rows
- `scripts/agents/test_restart.py` — the F007 row

**Workers:** 1 writer.

**State:** the row `home`.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`.

### Phase 4 — A herdr restart and a reboot bring the producers back  · status: pending

**Depends on:** Phase 3

#### Work Order

**Goal:** after a herdr restart or a reboot, each producer comes back through the Phase 3 start path, with no hand step.

**Spec:**

1. The herdr upkeep run starts each producer that is not live and not paused, through the same code as `resume <name>`.
2. A paused producer stays down. The pause is the user's off switch.
3. A start that fails is reported once and retried on the next upkeep run. One failed producer does not stop the others.
4. If the upkeep timer does not run at boot on natedev, name the nixos change for the showrunner; do not push nixos.
5. Add rows to the existing restore tests with a fake herdr.

**Workers:** 1 writer.

**State:** none.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`.

### Phase 5 — dwight's first start  · status: pending

**Depends on:** Phase 4

#### Work Order

**Goal:** dwight has a doc folder, a staff row of kind producer, and a live first conversation in the `producers` workspace.

**Spec:**

1. Create `~/rust/hanadocs/dwight/README.md` with the role name (strategist) and the management lines only.
2. The first start of a producer with no conversation starts a new one, proves its identity, and admits its staff row through staff code. It uses the same start path after that.
3. Start dwight. Report the model the conversation runs. The user expects Fable 5.2.

**Workers:** 1 writer.

**State:** a new staff row for dwight.

**Acceptance gate:** the Test command passes; basedpyright gives `0 errors, 0 warnings, 0 notes`; `resume dwight` reaches the live conversation.

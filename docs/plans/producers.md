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

#### As-built

- A repeated group move or workers swap selects each eligible stopped producer, also when its `home` already names the target. It skips a member only when the member is verified live on the target account.
- `stop_resume.LiveAccountUnproved` (a `ResumeFailed`) marks a live process whose account cannot be read. A move turns it into `StopFailed` ("live process account is unreadable") with no stop, no start and no `home` change.
- Live units and showrunners keep their earlier group-move selection. `eligible_producers` limits only producers.
- Producers, units and showrunners share one move check, one `member_account.repair_home` and one `home` write rule. `set_home` runs only when the home changes.
- `restart.producer_resume` takes only the family and model from `[producers]` and builds the rest with `pane_record.configured_resume_arguments`. `pane_launch` reads `--effort` as an option with a value.

**Files:**
- `scripts/accounts/accounts.py` — `group_move_members` selection
- `scripts/agents/stop_resume.py` — `LiveAccountUnproved`, the shared move check, `home` writes on change only
- `scripts/agents/member_account.py` — `repair_home` for every row kind
- `scripts/agents/restart.py`, `scripts/agents/pane_record.py` — shared resume arguments
- `scripts/herdr/pane_launch.py` — `--effort` value flag

**Binds later work:** `stop_resume.resume` returns `LiveAccountUnproved` for a live Codex producer whose process account cannot be read; that producer is live.

**Gotchas:**
- A test row for a live member needs process evidence in the fake herdr. A unit row needs a parent showrunner row.
- A fake `os.execvpe` runs the exit callback only in the forked child; the parent fails on a second exec.

### Phase 4 — A herdr restart and a reboot bring the producers back  · status: done

**Depends on:** Phase 3a

#### As-built

- Each herdr upkeep run restores the shared herdr server, saves the layout, refreshes the detection rules, then calls `stop_resume.resume(str(row.id))` for each producer from `accounts.producer_upkeep_members()`, inside one `settings_report_scope`.
- `accounts.producer_upkeep_members()` returns the eligible Claude and Codex producers (active, local, unpaused) from the local staff snapshot, without rows that have an exit request. It returns `()` when staff is absent.
- A failed start, a `ResumeFailed` result, an unknown session or any `Exception` from one producer gives one stderr line; the other producers still start, and the next upkeep run tries again. No failure record is stored.
- The start order rotates by the wall-clock interval count, so each producer gets the first place in turn.
- `run_job` passes `HERDR_UPKEEP_DEADLINE` to the worker. Each start runs under `min(run deadline, now + [shutdown] START_SECONDS)`; at the run deadline the loop stops with "producer budget ended".
- A paused producer stays down: the pause is the user's off switch.

**Files:**
- `scripts/maintenance/herdr_upkeep.py` — `upkeep` producer resume, rotation and deadlines
- `scripts/maintenance/test_herdr_upkeep.py` — `test_producer_restore_retries_failures_and_keeps_live_sessions`
- `scripts/accounts/accounts.py` — `producer_upkeep_members`

**Binds later work:** the upkeep timer resumes each admitted, active, local, unpaused producer about every two minutes through `stop_resume.resume`; a resume of a live producer starts nothing.

**Gotchas:**
- The nixos module starts `herdr-upkeep` from a graphical login. A start before login needs `users.users.${username}.linger = true` and `nate.jobs.herdr-upkeep.needsDesktop = false`.
- With four or more producers and a run spacing that is a multiple of producers × interval, the rotation can give the same producer the first place each run.

### Phase 5 — dwight's first start  · status: done

**Depends on:** Phase 4

#### As-built

- `stop_resume.resume(<name>)` starts a configured producer that has no staff row as a `ProducerFirstStart`, separate from an admitted `StaffRow`. `_first_producer_resume` opens the producer's tab in the `producers` workspace with no resume argument, using `restart.producer_first_arguments` (the `[producers]` family, model and effort) and `member_account.producer_account`.
- It proves the native identity through `Herdr.producer_admission_launch`, admits the row with `staff.ensure(..., runtime_id=<proved identity>)` and publishes the retained pane through `pane_launch.staff_admitted`. Later resumes use the row's `current_session`.
- A `PendingProducerAdmission` record at `_producer_admission_path(name)` holds the reserved pane until the admission ends. A rerun finishes an interrupted admission and opens no second conversation.
- A lost reserved pane gets a new pane; the retained entry moves to it without `pid` and `proc_start`, and the lost pane's launch is retired.
- A shell is reused only when its pane record has no entry, or the entry is this producer's pending identity. A shell that holds another session's launch is never taken; a new tab opens.
- `_resume` checks the admission record before the registry read. A registry error with a pending admission returns `ResumeFailed` and keeps the record; without one, the resume falls back to the generic path.
- The producer folder is `PRODUCER_FOLDER_ROOT/<name>` (`config/staff.conf`, default `~/rust/hanadocs`). dwight has `~/rust/hanadocs/dwight/README.md` (role strategist), a staff row and a live first conversation on Fable 5.1 with high effort.

**Files:**
- `scripts/agents/stop_resume.py` — producer first start, admission record, pane reservation and recovery
- `scripts/herdr/herdr.py` — `agent_alias_script(kind)` for every agent launch; `Herdr.producer_admission_launch`
- `scripts/agents/restart.py` — `producer_first_arguments`
- `scripts/agents/member_account.py` — `producer_account`
- `config/staff.conf` — `PRODUCER_FOLDER_ROOT`
- `scripts/agents/test_stop_resume.py` — `test_interrupted_producer_admission_reuses_its_live_native_conversation` (lost, replaced, unpublished, unrecorded, foreign and native ownership, broken registry rows); the absent-registry row in `test_resume_of_a_live_target_is_an_idempotent_success`
- `scripts/agents/test_shutdown_cli.py` — `resume dwight` routing

**Gotchas:**
- Check the admission record before the registry read, or an unreadable registry reports success for an incomplete admission.
- Never write a pane record entry over another session's retained launch.

**Ruled out:** Fable 5.2 for dwight — the `fable` alias moves to 5.2 when it ships.

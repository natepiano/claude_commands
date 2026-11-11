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

#### Work Order

**Goal:** staff knows kind `producer` and not kind `agent`; morty and brain become producer rows through a one-off conversion at deploy.

**Spec:**

1. Replace kind `agent` with `producer` in `staff.py` and in every caller, test and doc the research lists. Keep each rule as it is for the new kind: no parent, may hold a worktree and a doc, may be a root row, starts a staff path.
2. Rename the words "agent row" and "freestanding agent" to "producer" in code, help text and docs where they name this kind.
3. Write a conversion program outside the repository, at `/tmp/claude/unit/bd19678b-c7b1-4e24-be9e-133ecfe24c7a/convert_producers.py`. It uses staff code to change each `agent` row to `producer`, and gives brain `home` = the home of the current workers account. It prints each changed row and exits 0 when no `agent` row is left. A second run changes nothing.
4. Test the conversion program against a fixture staff file in a temporary directory. Do not commit that test.
5. Add no reader for the old `agent` kind in the main code.

**Files:**
- `scripts/production/staff.py` — the kind and its rules
- `scripts/production/staff_sources.py` — active producers
- `scripts/agents/restart.py` — pane search
- `scripts/agents/shutdown_record.py` — location
- `scripts/message/send.py` — repeat-notice owner
- `scripts/production/test_staff.py` — fixtures and rows
- `scripts/agents/test_restart.py` — fixtures
- `scripts/agents/test_stop_resume.py` — fixtures
- `scripts/agents/test_pane_record.py` — fixtures
- `scripts/agents/test_pane_restore.py` — fixtures
- `scripts/herdr/test_herdr.py` — fixtures
- `scripts/herdr/test_pane_launch.py` — fixtures
- `scripts/message/test_roster.py` — fixtures
- `scripts/message/test_send.py` — fixtures
- `scripts/hooks/test_codex_queue_drain.py` — fixtures
- `scripts/production/test_worker.py` — fixtures
- `scripts/production/test_unit_effort.py` — fixtures
- `scripts/build_targets/test_working_set.py` — fixtures
- `scripts/hana_shot/test_hana_shot.py` — fixtures
- `scripts/direct/test_progress_history.py` — fixtures
- `commands/agents.md` — the word for the kind
- `docs/as-built/herdr-restore.md` — the word for the kind
- `docs/as-built/shutdown-and-restart.md` — the word for the kind

**Workers:** 1 writer.

**State:** the staff row kind. The deploy runs the conversion once on natedev and on the Mac, right after the stack lands on main, then deletes it.

**Acceptance gate:**
- The Test command passes.
- basedpyright gives `0 errors, 0 warnings, 0 notes` on the changed Python files.
- The conversion program run against a copy of the live staff file in a temporary directory changes morty and brain and nothing else.

### Phase 2 — One command and one models line per producer  · status: done

**Depends on:** none (runs beside Phase 1; no shared file)

#### Work Order

**Goal:** /producer:morty, /producer:brain and /producer:dwight exist, the old name is gone, and `config/models.conf` holds the three producer lines.

**Spec:**

1. `git mv commands/associate-producer.md commands/producer/morty.md`. Its text calls morty nate's producer, and the user the executive producer.
2. `git mv docs/associate-producer/operations.md docs/producer/morty-operations.md`. Fix every link to it.
3. Write `commands/producer/brain.md` and `commands/producer/dwight.md`. Each holds the producer's name, its job name (dwight: strategist; brain: the job its current docs name, else only the name), and the management lines: how it starts (`resume <name>`), its family account, its workspace tab. Add no invented duties. Write no shared role doc.
4. Rename every "associate producer" and "associate-producer" reference: `commands/showrunner/produce.md:126`, `docs/as-built/staff-accessors.md:99`, the command's own links, and `~/rust/hanadocs/morty/README.md` (git -C).
5. Add to `config/models.conf` a `[producers]` section with the three lines from the Source, one line each, in the form `<name>=<family>:<model>:<effort>`. Extend `models_registry.py` to read and check that form, and add a row to its existing test.
6. Name the code that syncs commands into the Codex skill list. If it maps a command folder to a `folder:name` skill, show that `commands/producer/morty.md` gives `producer:morty`. The director checks morty's live skill list after the stack lands.

**Files:**
- `commands/associate-producer.md` — moves to `commands/producer/morty.md`
- `commands/producer/brain.md` — new
- `commands/producer/dwight.md` — new
- `docs/associate-producer/operations.md` — moves to `docs/producer/morty-operations.md`
- `commands/showrunner/produce.md` — the reference
- `docs/as-built/staff-accessors.md` — the reference
- `config/models.conf` — the `[producers]` section
- `scripts/lib/models_registry.py` — reads the producer lines
- `scripts/lib/test_models_registry.py` — a row for the producer lines
- `~/rust/hanadocs/morty/README.md` — the references

**Workers:** 1 writer.

**State:** none.

**Acceptance gate:**
- The Test command passes.
- basedpyright gives `0 errors, 0 warnings, 0 notes` on the changed Python files.
- `rg -i 'associate.producer'` in both repositories finds nothing.

### Phase 3 — One start path and account moves for producers  · status: pending

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

**Workers:** 1 writer.

**State:** none beyond the row `home` that a move writes now.

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

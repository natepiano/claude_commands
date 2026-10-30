# Workers in herdr panes, and one staff record

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Approved to build on 2026-10-30 by the user, through the sdlc showrunner, before a plan review; the user reviews an overview in the morning. It adds one staff record of every showrunner, unit director and worker, retires `codex_mesh`, and runs every unit worker as an agent in a herdr pane in its director's tab. It renames `agents.conf` to `models.conf` and `/agent` to `/default_models`.

> **Production: natedev** — unit `herdr-unit`; production doc `docs/plans/natedev-production.md`. The unit moves to sdlc later; checkpoints go to sdlc.

Written by herdr-unit on 2026-10-29 for the sdlc showrunner, from the user's request relayed the same night. Team review 2026-10-30 (four lenses and the sdlc showrunner's review) and the user's later answers are folded in. The inventory was read from `~/.claude` main at 7e85a74b.

## The user's request

- Retire `codex_mesh` and use herdr itself for workers.
- Workers open as panes inside each unit director's tab, stacked on the right. The director gets 2/3 of the width and the worker column 1/3. Active workers split the column evenly.
- Unit directors still send messages to other sessions through the messaging framework. herdr lets a director type into its workers directly, and lets the user see every worker.
- To change a model or effort, someone runs `/model` in the session.
- Starting a session may become a herdr command, so that a lot of infrastructure can retire.
- Update the showrunner and unit director instructions (their skills), and clean up the old things.
- Rename `agents.conf` to `models.conf` everywhere, and `/agent` to a name with no clash, for example `/default_models`.
- (2026-10-30) One command starts or resumes a worker by name, with model and effort looked up for its role.
- (2026-10-30) Add workers to the record of showrunners and unit directors, so one record shows the full production staff, with a stable unique id for each so a rename is easy. This record is a resource for other work too.
- (2026-10-30) The record maps each showrunner's, unit's and worker's short name to an id, and holds who reports to whom. A workspace and the showrunner's tab show the showrunner's short name, a unit's tab the unit's, and a worker's right pane the worker's. "The manifest keeps it all together. what do we need more than that? we already have things working well"

## Delegation Context

- **Project:** `~/.claude` scripts — the agent tooling. This plan adds the staff record, moves unit workers from `codex_mesh` app-server threads to herdr panes, and renames the model registry.
- **Stack:** Python 3.11+ through the `scripts/lib/py` shim, bash, `unittest`, basedpyright; herdr 0.9.5 CLI; the interactive Claude Code and Codex CLIs.
- **Layout:**
  - `scripts/production/` — production tools: `showrunners.py`, `production_units.py`, `live_units.py`, `unit_lookup.py`, `add_unit.py`, `add_showrunner.py`, `retire_unit.py`, `rename_unit.py`, `transfer_unit.py`, `rename.py`, `promote_*.py`, `resume_unit.py`, `production_tree.py`, `broadcast.py`, `codex_winddown.py`; the new `staff.py`
  - `scripts/herdr/` — `herdr.py` (the one herdr client), the new `worker_panes.py`
  - `scripts/direct/` — the launchers `implement.sh`, `review.sh`, `assign.sh`/`assign.py`, `workers.py`, `worker_state.py`, `remove_workers.py`, `mesh_runs.py`, `board.sh`, `progress_history.py`
  - `scripts/agents/` — `codex_mesh.py`, `agents_config.sh`, `agent_admin.sh`, `agent_exec.sh`, `sync_codex_catalog.sh`
  - `scripts/message/` — `send.py`, `roster.py`, `sessions.py`
  - `scripts/shutdown/` — `inventory.py`, `stop.py`, `restart.py`, `shutdown_record.py`, `pane_record.py`, `pane_restore.py`
  - `scripts/lib/` — `agents_registry.py`, `unit_runs.py`, `names.py`
  - `config/` — `agents.conf` (renamed `models.conf` in Phase 3), `direct.conf`, `names.toml`
  - `commands/`, `docs/direct/`, `docs/as-built/`, `skills/herdr/SKILL.md`
- **Key files:**
  - `scripts/production/showrunners.py` — showrunners from update timers (`registered_showrunners` :216, `current_name` :341, `parent_of` :427, `attach` :528, `detach` :557; parent files under `~/.local/state/showrunner/<slug>/parent`)
  - `scripts/production/production_units.py` — a production doc's Units table (`unit_table` :59, `unit_is_retired` :100, `SessionMarks` :84)
  - `scripts/production/unit_lookup.py` — units found by herdr pane marks `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID` (:30-31; `every_marked_unit` :316, `marked_units` :383, `marked_unit` :392)
  - `scripts/direct/workers.py` — the unit worker ledger under `~/.local/state/unit-workers/` (:529, `ledger_path` :1564), retired in the cleanup phase
  - `scripts/herdr/herdr.py` — `launch_agent` :3113, `launch_prepared_agent` :3165 (opens a tab today), `_agent_name` :3092 (truncates to 32 characters), `_herdr_resumes_agents` :1987, `Herdr.prompt` :1721
  - `scripts/agents/codex_mesh.py` — 8652 lines, deleted in the cleanup phase
  - `docs/as-built/herdr-adoption.md`, `docs/as-built/long-lived-workers.md`, `docs/as-built/agent-registry.md`, `docs/as-built/shutdown-and-restart.md` — how herdr, workers, the registry and shutdown work now
  - This plan's `## Reference` section — the job table (what the mesh does today and what replaces each job), the risks and the copies this plan retires
- **Test lanes:** none (no Rust crate); tests sit beside their modules as `test_*.py`.
- **Build:** none (scripts).
- **Test:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --tests-only --checkout <tree>`, run from `~/.claude` main's copy, after a merge of `origin/natedev` (memory: merge before a gate). Quick loop: `~/.claude/scripts/lib/py -m unittest discover -s <dir> -p '<test file>'` for the phase's own tests.
- **Lint:** `basedpyright <changed Python files> <every caller module of a changed file>` — pass is `0 errors, 0 warnings`; `bash -n` and `shellcheck` with no new findings for each changed `.sh`.
- **Invariants:**
  - **The Three Gods.** Simple, Fast, Beautiful, in that order, with correctness as the floor (`~/.claude/docs/decision_criteria.md` → `<ThreeGods/>`). Every writer builds to them and every reviewer judges by them.
  - **The four sdlc lines (2026-10-30), in every work order and review prompt this plan sends:**
    1. Good engineering first, not after the user notices. Before a worker writes code, ask: is there already one place for this? Use it, or make it, then call it.
    2. No copies. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. Reviews flag it; you send it back. A test helper that each file writes for itself goes into one shared helper.
    3. When many things break the same way, the copy is the cause. Name it first in your report, with the one-place fix, before anyone asks.
    4. Fix at the cause, never a patch at each site.
  - **Shown names need the user's go.** The user set the workspace, tab and pane names (2026-10-30): a workspace and the showrunner's own tab show the showrunner's short name, a unit's tab shows the unit's short name, and a worker's right pane shows the worker's short name (decision 13). No name anywhere joins production, unit and role. This plan changes no workspace or tab name.
  - **Rerun-safe tools (user, 2026-10-30).** Each tool this plan adds or changes that changes shared state in steps (start-or-resume, staff record writes, seed, pane open and close, restore, transfer, rename, retire, cleanup) is safe to rerun. After a stop or a failure at any step, a rerun ends in the same state as one clean run. A rerun of a finished run reports done and changes nothing. Each such tool gets one rerun test in its phase's Acceptance gate; read-only tools need none. The phase review lens checks it.
  - **The worker transport is pinned per run.** `WORKER_TRANSPORT=mesh|herdr` in `config/direct.conf` is read at run start and pinned in the run folder. A run that started on the mesh finishes on the mesh. Every lifecycle caller (dispatch, messages, detached delivery, shutdown, rename, transfer, retire, cleanup) reads the run's pin through one resolver, `unit_runs.worker_transport(run)`.
  - **Tests never touch the user's tabs.** herdr tests run in a probe tab of `agents` (memory: one herdr session; `herdr.py` refuses other servers) and close it at the end. No test runs `herdr` against a live director's tab.
  - **Build hold.** Before any test or gate, check `build_hold.py status`; while a natedev hold that is not ours is in force (02:00-05:00 tonight), run nothing that builds or tests. Edits continue.
  - **Owners.** The models unit owns `agents_registry.py` and the registry writers; the shutdown unit owns `scripts/shutdown/`; the accounts unit owns `scripts/accounts/` and account moves. A phase that changes their files names the owner in its checkpoint notice and agrees the change with them first. The user owns `/etc/nixos` (questions list item 1).

## Decisions made in this plan

Each item is my call under the decision criteria unless it names the user. A one-line plan change reverses it.

1. **One staff record (user, 2026-10-30).** `scripts/production/staff.py` and one file, `~/.local/state/staff/staff.json` on natedev, hold every showrunner, unit director and worker: a stable id, a short name, the kind, and the parent row's id. So the record maps each short name to an id and holds the relationships (worker → unit → showrunner); production and unit come from the parent chain. A rename changes only the name and a transfer only the parent; the id never changes. There is no separate worker ledger: the unit worker ledger, `mesh_roster.json`, the run `workers` log and the slot claim files retire into this record. Live state (pane, socket, running or not) is never stored; it comes from herdr and the session records at read time. No such record exists today: the hierarchy is joined at each read from update timers, parent files, Units tables, pane marks and the worker ledgers (Reference → Staff record readers).
2. **One command starts or resumes a worker (user, 2026-10-30).** `worker_panes.py start-or-resume <short name> <role>`. A worker's herdr agent name and its Claude or Codex session name are its staff id. Both Claude Code and Codex resume a conversation by its name (user, 2026-10-30), so the command resumes by id and keeps no conversation id. At every start or resume it looks up kind, model, effort, service tier and sandbox for the role in the model registry, and only then (user, 2026-10-30, option A): a `/model` typed by hand holds, no dispatch compares the model, and a config change does not restart a running worker. This replaces 710a6adb's per-turn lookup. A worker's short name is its role and the lowest number free under its unit, for example `impl1`, then `impl2`; it is set once, when the row is made.
3. **Idle panes close at phase end (user, 2026-10-30).** The conversation stays. To bring a worker back, the director calls `start-or-resume` again. This settles the keep-open question.
4. **The worker's final reply signals completion; herdr's state only wakes the launcher.** Every dispatch has a dispatch id. The prompt file ends with: "End your final reply with the line `DONE <dispatch id>`." The launcher reads the final reply from the worker's own transcript and writes the status file only when it carries that line. The launcher writes the status file today (`implement.sh:629`); the worker writes only its summary. This works the same for read-only reviewers, which cannot write a file (`review.sh:689` starts them with `--sandbox read-only`): the launcher writes their findings file from the reply, as the mesh does today.
5. **The launcher's whole wait is one background `herdr agent prompt <id> "<line>" --wait`, with no `--timeout` and no `--until`.** herdr then waits for its default settled set, `idle`, `done` or `blocked`, after it sees the turn start (herdr skill, "For normal agent work, `--wait` is enough"). `blocked` wakes the launcher, so a worker at an approval dialog never stalls the director. The director runs the launcher in the background, as today, and the task notification is the wake-up.
6. **A dispatch refuses a busy worker; a message queues.** A dispatch is a new pass that a launcher waits on, so it needs an `idle` or `done` worker, under the worker's dispatch lock. A message (`send.py`, a detached-run result, review Stage 2) is typed at once, also during a turn: Codex and Claude queue typed input. A blocked or closed worker's message goes to its director. `pending.json` drops.
7. **A prompt goes in by file path, not as pasted text.** The launcher types one line: `Read <prompt path> and do the work it gives.`
8. **No launcher compaction, and no "Continue where you stopped." retry.** Interactive Codex and Claude compact by themselves.
9. **Codex stays a worker kind.** herdr starts `--kind codex` and `--kind claude` agents. The default worker kind per role stays where it is set today.
10. **`herdr.py` stays the one herdr client.** Launch into a split pane and layout changes are new `herdr.py` primitives; `worker_panes.py` composes them and holds only `start-or-resume`, `dispatch`, `close`, `layout` and `list`. One-line agent calls (`herdr agent send-keys <id> esc` to interrupt, `herdr agent read`, a message's `herdr agent prompt`) run directly and are not wrapped.
11. **The three launcher commands stay; their shared work moves to one place.** `implement.sh`, `review.sh` and `assign.sh` keep their arguments and their recorder, status-file and board work. Start, dispatch, wait and receipt live once in `worker_panes.py dispatch`. The cleanup phase merges the launchers when they are thin.
12. **The transport switch lives in `config/direct.conf`, with an end date.** `WORKER_TRANSPORT` is migration state, not a model default, so it stays out of `models.conf`. The cleanup phase deletes it, no later than 7 days after the default switch.
13. **The right pane shows the worker's short name (user, 2026-10-30).** For example `impl1`, with no production or unit name.

## Questions for the user

1. **The NixOS edits (yours).** (a) `modules/common/claude-git-filters.nix:35` `watchPaths`: `agents.conf` → `models.conf`. It is functional: until it changes, the git filter does not refresh on a `models.conf` change. (b) Comments in `modules/common/codex-catalog-sync.nix`, `modules/common/codex.nix:47`, `modules/common/shell.nix:139` (`/agent`) and `README.md:410`. (c) `modules/common/herdr-skill-local.md:7`: the rule "start a new agent in a new tab, not a split pane" must permit worker panes in the director's tab (`skills/herdr/SKILL.md:221` is built from it). (d) The sign-in path unit (natedev) and the launchd agent (Mac) that run `codex_mesh.py signin-changed`: they retire at cleanup, or point at the pane restart if Phase 4's Codex sign-in probe is negative. (e) Only if Phase 4's probe shows that herdr's own resume does not keep each pane's `--env` (its account): add `[session] resume_agents_on_restore = false` to `~/.config/herdr/config.toml` on natedev and the Mac, so our restore owns split tabs; if the probe is positive, herdr restores split tabs and our per-pane restore code retires for them. Options: you write them; or herdr-unit writes one diff file per edit for you to apply, as the builds unit did with `build-followups-memory-nixos-*.diff`. Recommendation: diff files; apply (a) when the rename phase lands, (c) and (e) before the default switch, the rest at cleanup.

## Phases

**Critical path.** Phase 3 waits on the models unit, and no other phase needs it: pane work reads model and effort through the registry API under either file name. The director runs Phases 4-13 when Phase 3 is still blocked, and runs Phase 3 when the models unit lands. The critical path is Phase 1 → 4 → 5 → 6 → 7 → 8 → 9 → 11 → 13. Phases 2, 10 and 12 run beside it; Phase 11 also needs Phase 10 and questions items 1(c) and 1(e).

### Phase 1 — The staff record  · status: todo

#### Work Order

**Goal:** `scripts/production/staff.py` holds the showrunners, unit directors and workers of both machines in one file on natedev, each with a short name, a stable id and its parent's id; it answers lookups by id, by path (`sdlc/models/impl1`) and by kind, parent, name and role; and `staff.py seed` fills it from today's sources with no change to a shown name.

**Spec:**

1. **One module, one file.** New module `scripts/production/staff.py`. Its file is `~/.local/state/staff/staff.json`; the env var `STAFF_FILE` overrides the path (tests). One file for both machines, on natedev, the owner machine. On the Mac, every `staff.py` read and write runs on natedev through `ssh -o BatchMode=yes -o ConnectTimeout=10 <remote.other_machine()> ~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py <verb> --json`, the call `roster.peer_snapshot` makes (`roster.py:1897-1900`). The Mac keeps no copy. When natedev cannot be reached, a Mac call returns `Unreadable("natedev not reached")` and writes nothing. The owner machine is one constant in `staff.py`, `OWNER_MACHINE = "natedev"`, compared with `machine.machine_name()` (item 8).
2. **File form.** `{"schema": 1, "rows": [<row>, ...]}`, rows sorted by `id`, written with `json.dumps(..., indent=2, sort_keys=True)` and a final newline, so the same content always gives the same bytes. Writes take `fcntl.flock` on `<file>.lock`, write a temp file in the same folder, `fsync`, then `os.replace`. Reads take no lock. A file that does not parse, or has another `schema`, gives `Unreadable(path, reason)` and no write overwrites it.
3. **The row.** A frozen dataclass with `to_json()` and `from_json()` (no `Any`; parse with typed checks):
   ```python
   Kind = Literal["showrunner", "unit", "worker"]

   @dataclass(frozen=True)
   class StaffRow:
       id: str        # "s" + uuid4().hex[:11]; never changes, never reused, never derived from a name
       kind: Kind
       name: str      # the short name: a showrunner's workspace and tab, a unit's tab, a worker's pane label
       parent: str    # the parent row's id: worker -> its unit, unit -> its showrunner,
                      # showrunner -> its parent showrunner, or ""
       role: str      # worker: impl | test | review | astra | friend (the model lookup); "" for the others
       retired: bool
   ```
   The id is 12 characters of lower-case letters and digits led by `s`, so it matches herdr's agent-name rule `^[a-z][a-z0-9_-]{0,31}$` and herdr never cuts it (`herdr._agent_name`, `herdr.py:3092`). It is a worker's herdr agent name and its session name. A worker's unit is `by_id(row.parent)` and that unit's showrunner is `by_id(unit.parent)`: the parent chain gives production and unit, so the row stores neither.
4. **Uniqueness.** Among rows that are not retired, a unit or worker `name` is unique among rows of the same kind with the same `parent` (so `impl1` exists under many units), and a showrunner `name` is unique among showrunners. The kind is part of the rule, so a promote can add the new showrunner row before it retires the unit row of the same name. A write that would break the rule returns `Refused(reason)` and writes nothing.
5. **Read API.**
   ```python
   def load(path: Path | None = None) -> Staff | Unreadable
   def by_id(staff_id: str, path: Path | None = None) -> StaffRow | NotFound | Unreadable
   def by_path(names: str, path: Path | None = None) -> StaffRow | NotFound | Unreadable
   def find(*, kind: Kind | None = None, parent: str | None = None, name: str | None = None,
            role: str | None = None, retired: bool = False, path: Path | None = None) -> list[StaffRow] | Unreadable
   ```
   `by_path` takes short names, showrunner/unit/worker, of rows that are not retired: `sdlc`, `sdlc/models`, `sdlc/models/impl1`.
6. **Write API.**
   - `ensure(kind, *, name, parent="", role="") -> StaffRow | Refused | Unreadable`: returns the row that is not retired with that kind and name (and, for a unit or a worker, that parent), unchanged, when it exists; else adds it with a new id. A worker needs a `role`, and its name must be its role and a number (`impl1`); another form, or a row of that name with another role, is refused.
   - `rename(staff_id, new_name) -> StaffRow | Refused | NotFound | Unreadable`: the same name returns the row unchanged; a name that breaks item 4 is refused. Only `name` changes.
   - `move(staff_id, new_parent) -> StaffRow | Refused | NotFound | Unreadable`: a transfer moves a unit to another showrunner; an attach or a detach moves a showrunner under another one, or to `""`. The same parent returns the row unchanged. A parent that does not exist, is retired or is of the wrong kind is refused, and so is a move that breaks item 4 or makes a loop. Only `parent` changes; the rows under it follow through the id.
   - `retire(staff_id) -> StaffRow | NotFound | Unreadable`: retires the row and, for a unit, its worker rows, in one write. A retired row stays retired; a second call changes nothing.
7. **Worker names.** A worker's short name is its role and the lowest number free under its unit: `staff.next_name(unit_id, role) -> str` gives `impl1`, and `impl2` once `impl1` exists. The caller passes that name to `ensure`, so `ensure` stays keyed by name and a rerun makes no second worker. The name is set once, when the row is made, and it is the pane label.
8. **One machine name.** New `scripts/lib/machine.py` with `machine_name() -> str` (`socket.gethostname().split(".", 1)[0]`). Delete the copies at `scripts/message/roster.py:1829` and `scripts/production/stall_watch.py:623`; both call `machine.machine_name()`.
9. **`staff.py seed`** makes the showrunner and unit rows match today's sources. Each short name comes from the source that names its workspace or tab today, so no shown name changes.
   - Showrunners: `showrunners.registered_showrunners()` (`showrunners.py:216`) reads this machine's update timers. Each gives `ensure("showrunner", name=showrunners.herdr_space(slug, runner["doc"]))`. `herdr_space` (`showrunners.py:347`) is the doc's `Showrunner session:` line, else the slug, and it names the production's workspace today (`add_showrunner.py:153`, `add_unit.py:958`, `restart.py:736`); `add_showrunner.py:148, 153` names the showrunner's session and its own tab with the slug, the same name (workspace `natedev`, tab `natedev`). Then `move(id, <the id of the showrunner row for showrunners.parent_of(slug)>)` (`:427`), or `move(id, "")` with no parent; a parent with no row yet leaves `parent` as it is until a later seed.
   - Units: for each showrunner's production doc (`runner["doc"]`), read the Units table with `production_units.unit_table` (`production_units.py:59`); skip a row where `unit_is_retired(cells, SessionMarks(slug))` (`:100`); add a unit only when its `Worktree` cell names a folder that exists on this machine. Each gives `ensure("unit", parent=<the showrunner's id>, name=unit_names.name(<Unit cell>))`. `unit_names.name` (`unit_names.py:18`) drops the `-unit` ending; `add_unit.py:326` names a new unit with it, and `add_unit.py:953, 959` start the unit's session under that name (`-n`) and title its tab with it (`herdr-unit` has the tab `herdr`). A `/rename` by hand retitles the tab (`scripts/hooks/herdr_tab_title.py`) but not the row; only the writers rename a row (Phase 2).
   - Retire: a unit row under a showrunner whose doc this seed read retires when the Units table marks it retired or no longer lists it. Seed never retires a showrunner row: it reads only this machine's update timers and a row names no machine, so a missing timer does not prove that a production ended. `retire` does that (Phase 2's tools, or `staff.py retire <id>`). A source that cannot be read (an `OSError`, an unreadable doc) changes none of its rows and prints one stderr line naming it.
   - Workers are not seeded: mesh workers stay in the unit worker ledger until their unit switches to panes, and `start-or-resume` adds pane workers (Phase 5). Self-made call: the record lacks mesh workers for the migration days; a reader that needs them still reads the ledger until the readers phase.
10. **CLI.** `staff.py show [--kind K] [--under <id or path>] [--role R] [--retired] [--json]` prints one aligned row per staff row (id, kind, path, role), sorted by path; the path is the row's short names from its showrunner down (`sdlc/models/impl1`). `staff.py get <id or path> [--json]` exits 1 when not found. `staff.py ensure|rename|move|retire|seed [--dry-run]` call the write API and print the resulting row as JSON; a call that changes nothing says `unchanged` on stderr. Exit codes: 0 done, 1 not found, 2 refused or unreadable, with the reason on stderr.
11. **Scope note (author's call).** This phase moves no reader and no writer; Phase 2 moves the writers and the readers phase moves the readers. Until then nothing reads the record, so its rows can trail the old sources without harm, and `seed` catches them up.

**Files:**
- `scripts/production/staff.py` — new: the row, the read and write API, `next_name`, `OWNER_MACHINE`, `seed`, the CLI.
- `scripts/lib/machine.py` — new: `machine_name()`.
- `scripts/message/roster.py` — `machine_name` at :1829 replaced by `machine.machine_name()`.
- `scripts/production/stall_watch.py` — `machine_name` at :623 replaced by `machine.machine_name()`.
- `scripts/production/test_staff.py` — new: the tests in the Acceptance gate, with a fake `ssh` on `PATH` for the Mac path.
- Read only: `scripts/production/showrunners.py`, `scripts/production/production_units.py`, `scripts/production/unit_names.py` (the seed sources named above).

**Workers:** `2 writers` — no test lane; the tests split from the module.
- `impl` — `scripts/production/staff.py`, `scripts/lib/machine.py`, `scripts/message/roster.py`, `scripts/production/stall_watch.py`; hub: `scripts/production/staff.py` (the API the tests import).
- `test` (opens as impl) — `scripts/production/test_staff.py`, written from the Spec alone, with the seed sources pointed at fixture folders through `STAFF_FILE`, `NOTIFIER_STATE_DIR` and `SHOWRUNNER_STATE_DIR`.

**State:** `~/.local/state/staff/staff.json` and `staff.json.lock` (new). `seed` writes rows. A crash between the temp write and `os.replace` leaves the old file whole.

**Constraints from prior phases:** none.

**Acceptance gate:**
- `basedpyright scripts/production/staff.py scripts/production/test_staff.py scripts/lib/machine.py scripts/message/roster.py scripts/production/stall_watch.py` and each caller module of `roster.machine_name` and `stall_watch.machine_name`: `0 errors, 0 warnings`.
- `~/.claude/scripts/lib/py -m unittest discover -s scripts/production -p 'test_staff.py'` green, with these tests:
  - The rerun test (Invariants → Rerun-safe tools): each write verb run twice gives the file bytes of one run, and the second run says `unchanged`. It covers `ensure` of a showrunner, a unit and a worker (a worker ensured twice under one unit gives one row), `rename`, `move`, `retire`, and `seed` on fixture timers, parent files and a fixture production doc. A `seed` stopped after its first row write and run again gives the bytes of one clean run.
  - A new worker under a unit takes `impl1`, then `impl2` (`next_name`, then `ensure`); a worker name of another form is refused.
  - The same short name under two units is allowed; under one unit it is refused, and the file is unchanged.
  - `by_id`, `by_path("sdlc/models/impl1")` and `find` by kind, parent, name and role return the expected rows; a retired row is found only with `retired=True`.
  - `rename` keeps the id; `move` changes only `parent`, and the moved unit's workers then resolve through `by_path` under the new showrunner.
  - Every new id matches herdr's agent-name rule `^[a-z][a-z0-9_-]{0,31}$`.
  - Two processes that call `ensure` with the same kind, parent and name at the same time give one row.
  - An exception raised before `os.replace` leaves the old file bytes.
  - On a machine that is not `OWNER_MACHINE`, `find` and `ensure` run the fake `ssh` command with the verb and `--json`, and a failed `ssh` gives `Unreadable` and no local file.
  - A fixture doc that cannot be read leaves its rows and prints one stderr line.
- The phase's full gate (Delegation Context → Test) green, run outside the build hold.
- Behavior: `staff.py seed` on natedev and on the Mac, then `staff.py show` on either machine, lists both machines' showrunners and units, each with an id and with the names their workspaces and tabs show now; a second `seed` says `unchanged` and leaves `staff.json` byte for byte the same.

### Phase 2 — Staff record writers  · status: todo

#### Work Order

**Goal:** every tool that adds, renames, moves or retires a showrunner or unit also writes the staff record, so the record stays current without `seed`.

**Spec:** Each tool calls the `staff.py` write API after its own change succeeds, and a failed staff write fails the tool with the reason (the tool's own change stays; rerunning the tool, or `staff.py seed`, completes it). Each tool finds its row with `by_path` from the names it already holds. Add and resume → `ensure`: `add_showrunner.py` the showrunner row; `add_unit.py` and `resume_unit.py` the unit row. Rename → `rename(id, new_name)`: `rename.py` the showrunner row, `rename_unit.py` the unit row; the workspace or tab title changes as today. Transfer → `move(id, new_parent)`: `transfer_unit.py` moves the unit row. Attach and detach → `move` of the showrunner row: `showrunners.py attach`/`detach` (:528, :557). Retire → `retire_unit.py` calls `retire`, which retires the unit and its workers. Promote → `ensure` the new row, then `retire` the old one: `promote_showrunner.py` ensures the showrunner row and retires the unit row; `promote_unit.py` ensures the unit row. A rename or a transfer never touches a worker row, a herdr agent or a session: workers keep their ids and follow through `parent`. No reader changes in this phase.

**Files:** `scripts/production/add_showrunner.py`, `showrunners.py`, `add_unit.py`, `resume_unit.py`, `retire_unit.py`, `rename_unit.py`, `rename.py`, `transfer_unit.py`, `promote_unit.py`, `promote_showrunner.py`; their tests (`test_*.py` and `*_test_support.py` beside each); `scripts/production/test_staff.py` (shared fixture helper only, if two test files need it).

**Workers:** `2 writers` — split by tool group.
- `impl` — add, resume, retire, promote tools and their tests; hub: `scripts/production/test_staff.py` (the one staff fixture helper).
- `test` (opens as impl) — `showrunners.py`, rename, rename_unit, transfer_unit and their tests.

**State:** rows in `~/.local/state/staff/staff.json`.

**Constraints from prior phases:** Phase 1 built `staff.py` (`ensure`, `rename`, `move`, `retire`, `by_path`; `STAFF_FILE` for tests).

**Acceptance gate:** basedpyright 0/0 on changed files and callers; each tool's tests assert the staff row after the tool runs; each tool's rerun test (Invariants → Rerun-safe tools): the tool stopped after its own change and before its staff write, then run again, gives the file of one clean run, and a rerun of a finished run reports done and changes nothing; full gate green.

### Phase 3 — Rename agents.conf to models.conf and /agent to /default_models  · status: todo

**Blocked by:** the models unit's registry rewrite (`agents_registry.py` with `RegistryRow` and atomic edits; overlay files removed) and its shared config test fixture, landed on main.

#### Work Order

**Goal:** one name everywhere: `config/models.conf`, `MODELS_CONFIG_FILE`, `models_config.sh`, `models_registry.py`, `/default_models`; no live reference to the old names remains.

**Spec:** One sweep in one commit, no fallback that reads an old name, agreed with the models unit before the edit. Renames: `config/agents.conf` → `config/models.conf`; `AGENTS_CONFIG_FILE` → `MODELS_CONFIG_FILE` (the overlay env var drops with the overlay); `scripts/agents/agents_config.sh` → `models_config.sh`; `scripts/lib/agents_registry.py` → `models_registry.py`; `commands/agent.md` → `commands/default_models.md`; `scripts/agents/agent_admin.sh` → `default_models.sh`; the models unit's shared fixture follows the file. `models_registry.py` is the one parser and the one path authority: the Python path constants (`progress_history.DEFAULT_AGENTS_CONFIG_PATH`, `context_usage.DEFAULT_AGENTS_CONFIG_PATH`, `codex_pacer.AGENTS_CONF`, `quota_alert.REGISTRY`) and the line parsers (`codex_pacer.py:440`, `quota_alert.py:704`, `context_usage.py:269`, `progress_history.py`) call it; bash callers get resolved values from one `models_registry.py` call per launch that prints shell assignments, so bash parses no registry line. The full usage list is Reference → Rename inventory; build the manifest from a repository search, not from that list alone. `affected_modules.py:109` names `agents_config.sh` and changes to `models_config.sh`. Add `scripts/agents/test_no_old_model_names.py`: it fails on any live (non-history) reference to `agents.conf`, `AGENTS_CONFIG_FILE`, `agents_config.sh`, `agents_registry` or the `/agent` command. Send the user the NixOS diff files (questions list item 1).

**Files:** Reference → Rename inventory, plus `scripts/agents/test_no_old_model_names.py` (new).

**Workers:** `2 writers` — split by tree. `impl` — `config/`, `scripts/agents/`, `scripts/lib/`, `scripts/direct/`, `commands/`; hub: `scripts/lib/models_registry.py`. `test` (opens as impl) — `scripts/whoami/`, `scripts/hooks/`, `scripts/astra/`, `scripts/history/`, `scripts/production/`, `scripts/notify/`, `scripts/cli_agent/`, `scripts/ask_a_friend/`, docs, and the new test.

**State:** `config/agents.conf` moves to `config/models.conf`.

**Constraints from prior phases:** none from Phases 1-2.

**Acceptance gate:** `test_no_old_model_names.py` green; the rerun test (Invariants → Rerun-safe tools): a sweep stopped partway and run again gives the files of one clean run, and a second run of a finished sweep changes no file; basedpyright 0/0 on changed files and callers; full gate green.

### Phase 4 — herdr primitives for worker panes, and probes  · status: todo

#### Work Order

**Goal:** `herdr.py` can start an agent in a new split pane and set split ratios, and the probe answers are recorded in this plan.

**Spec:** Add a split destination to `launch_prepared_agent` (`herdr.py:3165`): `SplitPane(of_pane, direction, ratio)` beside today's new tab, with the same environment proof, launch claim, prompt-once proof and cleanup. Add `Herdr.split_ratio(pane, ratio)` over the socket's `layout.set_split_ratio`, and `Herdr.label_pane(pane, text)`. Every split uses `--no-focus`. Probes, in a probe tab of `agents`, written into Reference → Probe results: which side `--ratio` measures; whether a close rebalances the rest; Codex and Claude queue a message typed mid-turn; herdr's Codex state over a 10-minute turn; prompt by path; both CLIs open an image by path from a prompt file; the exact Codex input that names a new session (help shows no `--name` flag; Claude uses `--name`); whether herdr's `resume_agents_on_restore` keeps each pane's `--env` (a positive answer retires our split-tab restore code; a negative one needs questions list item 1(e)); whether interactive Codex picks up a new `auth.json` by itself (a positive answer retires the sign-in restart, Reference job 8).

**Files:** `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr*.py` (the existing launch tests), this plan's Reference → Probe results.

**Workers:** `1 writer + 1 tester` — `impl`: `herdr.py`; `test` (opens as impl): the tests and the probes.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** basedpyright 0/0; launch tests cover the split destination and a failed start closing its pane; the rerun test (Invariants → Rerun-safe tools): a split launch stopped after its pane opens and before the prompt proof, then run again, leaves one pane; probe results written; full gate green.

### Phase 5 — worker_panes.py: start-or-resume, close, layout, list  · status: todo

#### Work Order

**Goal:** `worker_panes.py start-or-resume <short name> <role>` starts or resumes a worker in its director's tab with the registry's settings, and `close` refuses a pane that still has work.

**Spec:** `start-or-resume <short name> <role>`, inside the calling director's unit (the unit row from the director's run): `staff.ensure("worker", parent=<unit id>, name=<short name>, role=<role>)`; when the worker's pane is open, report done and change nothing (model and effort are looked up only at start or resume; a `/model` typed by hand holds; decision 2); when no pane is open, split the column (first worker: the director pane, `right`, ratio 0.667; later ones: the lowest worker pane, `down`), start the agent with herdr agent name = the row's id and pane label = the short name, with `--resume <id>` (Claude) or `codex resume <id>` when a conversation of that name exists, else a new session named with the id, then `layout`. The account comes from the accounts code as today (Codex: the `workers` role). The pane env carries `STAFF_ID=<id>`, `UNIT_RUN_DIR`, `UNIT_TEAM_ROLE`, `TMPDIR` and the dev shell through `launch_prepared_agent`. `close` passes only `can_close`: dispatch lock free; the last turn ends with a `DONE` line whose pass is recorded complete; no input after it; state `idle` or `done`; no live detached run of the worker. Otherwise it keeps the pane and names the blocker. `layout`: under a per-tab lock, set every split in the worker column to an even share; it changes only panes whose agent name is the id of a worker row whose parent is this tab's unit, leaves other panes as they are, and gives focus back where it was. `list`: `herdr agent list` joined with `staff.find` on the agent name. A rename or a transfer of the unit or its showrunner changes no worker pane, agent or session (Phase 2).

**Files:** `scripts/herdr/worker_panes.py` (new), `scripts/herdr/test_worker_panes.py` (new).

**Workers:** `1 writer + 1 tester` — `impl`: `worker_panes.py`; `test` (opens as impl): `test_worker_panes.py` against a probe tab.

**State:** staff rows; panes; `~/.local/state/staff/locks/<id>.lock`.

**Constraints from prior phases:** Phase 1 `staff.py`; model, effort, tier and sandbox come from the models unit's registry API (`RegistryRow` lookups), called by module whatever the file is named (`agents_registry.py` before Phase 3, `models_registry.py` after); Phase 4 `SplitPane`, `split_ratio`, `label_pane` and the probe results.

**Acceptance gate:** tests: start, resume by id, the agent name is the id and the pane label the short name, `close` refused with work left, a foreign pane untouched; the rerun test (Invariants → Rerun-safe tools): `start-or-resume` stopped after the row write, after the split or after the agent start, then run again, gives one row and one pane, as one clean run; on an open pane it reports done and changes nothing; `layout` and `close` run twice give the same panes and ratios; basedpyright 0/0; full gate green.

### Phase 6 — Dispatch and the launcher switch, landed dark  · status: todo

#### Work Order

**Goal:** with `WORKER_TRANSPORT=herdr` pinned in a run, `implement.sh`, `review.sh` and `assign.sh` dispatch through `worker_panes.py dispatch`; the default stays `mesh`.

**Spec:** `dispatch`: take the worker's dispatch lock (non-blocking; held → exit "busy"); `start-or-resume`; check `idle` or `done`; append "End your final reply with the line `DONE <dispatch id>`." to the prompt file; run `herdr agent prompt <id> "Read <prompt path> and do the work it gives." --wait` (no timeout, no `--until`); then read the final reply from the worker's own transcript: a `DONE <dispatch id>` line → complete (write the reply file; for a reviewer, the findings file); `blocked` → exit 4; settled without the line while `herdr agent get` says `working` → `herdr agent wait <id>` again; settled without the line → exit 3 and the director reads the pane; `agent_prompt_stalled` or a timeout → never resubmit, exit 5. The launchers keep their status, recorder, board and summary work, and write `implemented` only on complete. `unit_runs.worker_transport(run)` reads `WORKER_TRANSPORT` from `config/direct.conf` at run start and pins it in the run folder. Restart: `inventory.py` keeps recording `launcher_command` (:861); a rebuilt launcher waits for the same dispatch id. Several implementers use `impl1`, `impl2`. Transport-aware text in `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<FixDispatch/>`, `<PhaseCleanup/>`) lands with this phase, branched once on the run's pin.

**Files:** `scripts/herdr/worker_panes.py`, `scripts/direct/implement.sh`, `review.sh`, `assign.py`, `scripts/lib/unit_runs.py`, `config/direct.conf`, `commands/unit/direct.md`, launcher tests.

**Workers:** `2 writers` — `impl`: `worker_panes.py`, `unit_runs.py`, `direct.conf`; hub: `worker_panes.py`. `test` (opens as impl): the three launchers, `direct.md`, launcher tests.

**State:** run pin file; dispatch locks.

**Constraints from prior phases:** Phase 5 `start-or-resume`, `can_close`, locks.

**Acceptance gate:** tests for long, fast, blocked, false-idle and stalled turns; two launchers at once get one dispatch and one "busy"; the rerun test (Invariants → Rerun-safe tools): a launcher stopped after it types the prompt and run again with the same dispatch id waits for the same pass and types nothing, and a rerun after the pass is complete reports done; a mesh-pinned run is unchanged; full gate green.

### Phase 7 — Messages, detached runs and review Stage 2  · status: todo

#### Work Order

**Goal:** messages, detached-run results and review Stage 2 reach a pane worker, and none is lost.

**Spec:** `send.py` `worker:<unit>/<short name>` resolves through `staff.by_path` (under the sender's showrunner; `worker:<showrunner>/<unit>/<short name>` from outside it) to the worker's id, its herdr agent name, and types the text with `herdr agent prompt <id> <text>`; `agent_blocked` or no pane → the message goes to the director with one line saying why. `mesh_runs.py` becomes `detached_runs.py`: a run finds its worker by the `STAFF_ID` in its environment and types its result into the pane. `assign.py` types Stage 2 with its own `DONE <dispatch id>-2` line, and the review launcher waits for that line. Usage refusals and settled-without-`DONE` turns go through one classifier in the accounts refusals layer (transcript evidence first, screen text as fallback); the pass status says `refused`, the pass stays open, and the director re-dispatches it after the move.

**Files:** `scripts/message/send.py`, `scripts/direct/mesh_runs.py` → `detached_runs.py`, `scripts/direct/assign.py`, the refusals classifier module (agreed with the accounts unit), their tests.

**Workers:** `2 writers` — `impl`: `send.py`, `detached_runs.py`; `test` (opens as impl): `assign.py`, the classifier, tests.

**State:** none new.

**Constraints from prior phases:** Phase 6 `dispatch` and exit codes.

**Acceptance gate:** tests: a message during a turn arrives; a blocked or closed worker's message reaches the director; Stage 2 waits for its own line; the rerun test (Invariants → Rerun-safe tools): a detached run's delivery run twice types its result once; full gate green.

### Phase 8 — Shutdown and restore of split tabs (shutdown unit)  · status: todo

#### Work Order

**Goal:** a shutdown stops worker panes inside the bounded deadline, and a restart brings each director tab back with its workers resumed.

**Spec:** Before the voluntary window, the shutdown record lists each worker pane: staff id, name, pane, pid and start time, scope, run, dispatch id when busy; `BoundedOwner` holds them under their director, and success needs a fresh observation that they are gone. Restore follows Phase 4's probe (questions list item 1(e)): herdr's own resume keeps env → nothing to build; else one locked tab transaction rebuilds the director, then each worker with `start-or-resume`, then rebuilt launchers. Resume texts come from one builder, `worker_resume_prompt(cause, was_busy)`, used by shutdown, moves and launchers. Tab sort sorts a multi-pane tab by its director pane (`herdr.py:902-1300`).

**Files:** `scripts/shutdown/inventory.py`, `stop.py`, `restart.py`, `shutdown_record.py`, `pane_restore.py`, `scripts/herdr/herdr.py` (tab sort), tests.

**Workers:** `2 writers` — `impl`: stop and record; `test` (opens as impl): restart, restore, tab sort.

**State:** shutdown records.

**Constraints from prior phases:** Phases 4-6.

**Acceptance gate:** shutdown tests with worker panes inside 66 s; the rerun test (Invariants → Rerun-safe tools): a restart stopped after the director tab or after the first worker, then run again, gives one set of panes, and a rerun of a finished restart reports done and changes nothing; full gate green.

### Phase 9 — Account moves for pane workers (accounts unit)  · status: todo

#### Work Order

**Goal:** an account move relaunches each worker pane on the new account without a lost or forked conversation.

**Spec:** Under the worker's dispatch lock: wait for or interrupt the turn, close the pane, prove the process gone, copy the conversation files to the new home and check them, `start-or-resume` on the new account, check the id resumes. On failure resume once on the source account and report. Retire `_retire_workers_servers` and `leftover_server_lines`.

**Files:** `scripts/accounts/accounts.py`, `scripts/whoami/quota_alert.py`, `next_moves.py`, `worker_panes.py` (call only), tests.

**Workers:** `1 writer + 1 tester` — `impl`: accounts code; `test` (opens as impl): tests.

**State:** account homes; moves log.

**Constraints from prior phases:** Phases 5-7.

**Acceptance gate:** move tests (busy, idle, failure fallback); the rerun test (Invariants → Rerun-safe tools): a move stopped after the pane closes or after the copy, then run again, ends on the new account with one pane and one conversation, and a rerun of a finished move reports done; full gate green.

### Phase 10 — Astra roles, ask-a-friend, displays and instructions  · status: todo

#### Work Order

**Goal:** Astra roles and ask-a-friend run in panes, every display reads workers from the staff record and herdr, and every instruction file describes panes.

**Spec:** A held Astra role is a pane in the requesting director's tab (`design_roles.py` keeps its fences, loses its mesh calls); ask-a-friend opens a pane beside the caller and closes it at `end`. `roster.py`, `production_tree.py`, `codex_winddown.py`, `waste_watch.py`, `hierarchies/*`, `board_json.py` read workers from `staff.find` and `herdr agent list`. Instruction edits: Reference → Instruction edits.

**Files:** `scripts/astra/design_roles.py`, `scripts/ask_a_friend/*`, the display modules named above, Reference → Instruction edits.

**Workers:** `2 writers` — `impl`: code; `test` (opens as impl): docs and commands.

**State:** none.

**Constraints from prior phases:** Phases 5-7.

**Acceptance gate:** tests for each changed module; the rerun test (Invariants → Rerun-safe tools): an Astra role or ask-a-friend pane open, and its close, each run twice give one pane and then none; no instruction file names a mesh verb outside a mesh-pinned branch; full gate green.

### Phase 11 — Pilot and default switch  · status: todo

#### Work Order

**Goal:** herdr-unit's own next run works on panes, then the default becomes `herdr` and units switch at their run boundaries, Mac units after a Mac probe of Phases 4-5.

**Spec:** Pilot one run; record any defect as a repair before the switch. Then set `WORKER_TRANSPORT=herdr` in `config/direct.conf`. Needs questions list items 1(c) and 1(e) answered.

**Files:** `config/direct.conf`.

**Workers:** `1 writer` — one line; the pilot is the director's.

**State:** `config/direct.conf`.

**Constraints from prior phases:** Phases 4-10.

**Acceptance gate:** the pilot run's phases finish on panes; Mac probe passes; full gate green.

### Phase 12 — Staff record readers  · status: todo

#### Work Order

**Goal:** every reader of who exists reads the staff record; live state still comes from herdr and session records.

**Spec:** Move each reader in Reference → Staff record readers to `staff.find`/`by_path`/`by_id`; parent files move into the showrunner row's `parent`; pane marks stay as the live link to a unit's pane. Readers on the Mac get both machines' rows through the one file on natedev.

**Files:** Reference → Staff record readers.

**Workers:** `2 writers` — `impl`: `scripts/production/`; `test` (opens as impl): `scripts/message/`, `scripts/shutdown/`, other modules.

**State:** none.

**Constraints from prior phases:** Phases 1-2.

**Acceptance gate:** each moved reader's tests green; full gate green.

### Phase 13 — Cleanup  · status: todo

#### Work Order

**Goal:** the mesh, the review pool, the old worker records and `WORKER_TRANSPORT` are gone, no later than 7 days after the default switch.

**Spec:** Run only after a check that no run folder pins `mesh` and no mesh process is alive. Delete: Reference → Cleanup list. Then merge `implement.sh`, `review.sh` and `assign.sh` into one launcher, `scripts/direct/launch.py`, keeping the three command names as thin entry points for their arguments (decision 11).

**Files:** Reference → Cleanup list.

**Workers:** `2 writers` — `impl`: code; `test` (opens as impl): docs and tests.

**State:** deletes state on disk (listed).

**Constraints from prior phases:** all.

**Acceptance gate:** `grep` finds no live `codex_mesh` reference; the rerun test (Invariants → Rerun-safe tools): the state deletion stopped partway and run again ends with the deletions of one clean run, and a rerun after it finished reports done; full gate green.

## Reference

### Job table (main at 7e85a74b)

| # | Job today (codex_mesh.py unless named) | After |
| --- | --- | --- |
| 1 | App-server per run (`serve`, `idle-stop`, `sweep`) | Drop |
| 2-3 | `start`, `follow` (:3308, :3408) | `start-or-resume` + `dispatch` (Phases 5-6) |
| 4, 15 | Follow and slot claims (:3762; `impl_claim_*.json`) | Per-worker dispatch lock |
| 5-6 | Compaction, "Continue where you stopped." | Drop (decision 8) |
| 7 | Server retry | A failed start closes its pane and tries once in a new pane |
| 8 | `signin-changed` (:7673) | Phase 4 probe; positive → drop, else resume restart |
| 9 | `send`, `pending.json` | Decision 6 (Phase 7) |
| 10-11 | `steer`/`interrupt`, `end` | `herdr agent send-keys <id> esc`; `close` |
| 12 | `stop --pause` | Shutdown stops panes (Phase 8) |
| 13-14 | `reopen`/`adopt`; unit worker ledger (workers.py:422-545) | `start-or-resume`; the staff record |
| 16 | Pass recording in launchers | Keep |
| 17 | `mesh_runs.py` | `detached_runs.py` (Phase 7) |
| 18 | Wait markers | Drop markers; recorder waits stay |
| 19-20 | Usage limits; account moves | Phases 7 and 9 |
| 21 | Model per turn (710a6adb) | Lookup at start or resume only; a `/model` by hand holds (decision 2) |
| 22-23 | Review pool; unit reviewers | Drop the pool; reviewers are panes |
| 24-25 | Astra roles; ask-a-friend | Panes (Phase 10) |
| 26 | `board.log` | Keep; `reach=herdr` |
| 27, 29 | Roster; displays | Staff record + `herdr agent list` |
| 28 | Shutdown and restart of workers | Phase 8 |

### Staff record readers (move in Phase 12; workers in Phases 6-10)

- Showrunner tree: `showrunners.py:216, 398, 415, 427, 432, 491, 576`; `production_tree.py:111-127, 287`.
- Units: `unit_lookup.py:316, 383, 392`; `production_units.py:59`; `live_units.py:32`.
- Workers: `workers.py:529, 1564`; `remove_workers.py:65`; `assign.py:604`; `hierarchies/worker_owners.py:493, 606, 642`; `retire_unit.py:292`; `production_tree.py:156, 169, 231, 256`.
- Joined readers: `message/roster.py:906, 958, 1050, 1123, 1185, 1344, 1694`; `production/broadcast.py:105-129` (through roster); `direct/board_json.py:371, 427`; `direct/phase_table.py:1664`; `shutdown/inventory.py:422, 427, 908, 1693`; `shutdown/stop.py:409`; `shutdown/restart.py:2848`; `production/codex_winddown.py:290, 304, 332`; `dailies_input.py:300, 1032`; `production_lifecycle.py:173, 353`; `promote_showrunner.py:124`; `rename.py:528, 645`; `rename_unit.py:240, 257, 273, 300, 673, 884, 982`; `rename_outside.py:143, 176`; `resume_unit.py:130`; `retire_unit.py:435`; `transfer_unit.py:459, 505`; `stall_watch.py:755, 771`; `unit_effort.py:206`; `waiting.py:153`; `add_showrunner.py:242`; `add_unit.py:1025`; `accounts/unit_watchdog.py:335`; `astra/design_roles.py:489`; `hierarchies/measure.py:1413-1416`; `nightly_review/nightly_review.py:122, 252, 273`; `style/add_style.py:333`; `style/style_plan.py:2952`; `usage/waste_watch.py:181`; `whoami/agent_accounts.py:239`.

### Rename inventory (Phase 3)

Config: `config/agents.conf`, `config/README.md:7, 14, 15, 20`, `.gitignore:23-26`. Commands: `agent.md`, `ask_a_friend.md:9`, `history.md:113`, `plan/phase_review.md:173`, `unit/direct.md:28`, `showrunner/add_unit.md:31`, `showrunner/promote_unit.md:18`, `api_review.md`, `module_review.md`, `team_review.md`; Codex mirrors regenerate through `claude_to_codex/sync.py`. Scripts: `agents_config.sh`, `sync_codex_catalog.sh`, `agent_admin.sh:95`, `agent_bg.sh:217, 253`, `agent_exec.sh:78`, `ask_a_friend/launch_friend.sh:54`, `production/add_unit.py:506`, `production/unit_effort.py:61-73`, `notify/review.py:667`, `production/affected_modules.py:109`, `progress_history.py:209, 283, 304`, `assign.py:400`, `demo_refusals.sh:20-24`, `hooks/context_usage.py`, `whoami/codex_pacer.py:6, 58, 440, 523`, `whoami/quota_alert.py:248, 723`, `whoami/profile_notes_pass.py:252`, `astra/design_roles.py:528`, `cli_agent/cli_agent.sh:6, 75`, `history/history.py:115, 121`, and their tests. Docs: the as-built docs that name the file, `docs/direct/launch_implementation.md:24, 72`. Leave: `scripts/settings/git_filters_table.sh:16` (history). NixOS: questions list item 1.

### Instruction edits (Phases 6 and 10)

`commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<CodexDispatchWait/>`, `<PauseForMove/>`, `<FixDispatch/>`, `<PhaseCleanup/>`, `<HeldAstraRoles/>`, `<ToolingContract/>`); `docs/direct/write_prompt_contract.md` (`<PhaseMesh/>` → `<PhasePanes/>`); `docs/direct/launch_implementation.md`, `dual_review.md`, `phase_end.md`, `run_phase_review.md`; `commands/unit/report.md`; `commands/showrunner/produce.md:287, 390, 476, 534, 1122-1123` and one paragraph on where workers show; `commands/showrunner/accounts.md`, `rename_unit.md`, `retire_unit.md`, `transfer_unit.md`; `commands/message.md`; `commands/codex_winddown.md`; `commands/shutdown.md`; `commands/ask_a_friend.md`; `commands/astra_review.md:32` (still starts `mesh_runs.py`); `docs/quota_alerts.md:19, 27, 39`; `commands/shot_report.md:11`; `skills/herdr/SKILL.md:109, 221` (through the NixOS file, questions item 1); `docs/production_format.md:39, 166`; `docs/accounts.md`. A final search for mesh paths outside history ends the phase.

### Cleanup list (Phase 13)

the three launchers merged into one (decision 11); `codex_mesh.py` and its tests; `review_pool.py` and its state; the mesh parts of `workers.py` and the unit worker ledger; `mesh_roster.json`, `*.pending.json`, `mesh_*` run files; follow-claim code; `launch_friend.sh`, `end_friend.sh`; `[direct.options] codex_mesh`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, `WORKER_TRANSPORT`; shutdown's `CodexServer`, `codex_seats`, `PausedSeat`, `_CODEX_HEADLESS`, mesh pause and paused-workers note; accounts' server lines; display mesh code (`production_tree.py`, `process_table.py`, `waste_watch.py`, `hierarchies/*`, `shot_report/transcripts.py:1680`, `lint/tmp_sweep.py`); showrunner parent files once Phase 12 moved them; the Codex app-server homes; docs: rewrite `long-lived-workers.md`, remove `review-pool*.md`, update `herdr-adoption.md`, `session-roster.md`, `agent-registry.md`, `shutdown-and-restart.md`, `unit-board.md`, `usage-ledger.md`.

### Probe results (Phase 4 writes them)

none yet.

### Risks

1. herdr reads a Codex pane as `idle` mid-turn: only a `DONE <dispatch id>` line completes a pass (decision 4).
2. A typed prompt stalls: one short line; never resubmit blindly.
3. Reboot before Phase 8: no real unit uses panes before Phase 11.
4. Account moves copy conversation files under a lock (Phase 9).
5. Screen room: three or more workers in a 1/3 column are short; idle panes close; `herdr pane zoom` shows one.
6. The user and a director type into one pane at once: a message queues; a dispatch needs `idle`.
7. Two transports for days: the run pin and one resolver; cleanup checks no run pins `mesh`.

### Copies this plan retires

| Copy today | The one place after |
| --- | --- |
| Hierarchy joined at each read from timers, parent files, Units tables, marks, worker ledgers, `mesh_roster.json`, run `workers` log, claim files | `staff.py` (Phases 1, 2, 12) |
| `machine_name` in `roster.py:1829` and `stall_watch.py:623` | `scripts/lib/machine.py` (Phase 1) |
| Config path per module; line parsers in Python and bash | `models_registry.py` (Phase 3) |
| herdr launch paths: `codex_mesh start`, `agent_exec.sh` fresh review, `launch_friend.sh`, `herdr.launch_agent` | `herdr.launch_prepared_agent` with a split destination (Phases 4-5) |
| Dispatch logic in three launchers | `worker_panes.py dispatch` (Phase 6) |
| Resume texts (`stop.py:332-346`, `restart.py`, the mesh's) | `worker_resume_prompt(cause, was_busy)` (Phase 8) |
| Usage-refusal matchers (`codex_mesh.QUOTA_REFUSAL`, `refusal_watch.py`, `settle.py`) | one classifier in the refusals layer (Phase 7) |
| Test files that write their own `agents.conf` | the models unit's shared fixture (Phase 3) |

## Review record (team review 2026-10-30)

All four reviewer outputs were present. Claims about code were checked on `~/.claude` main at 7e85a74b.

- R1 — Wrong wait semantics (`--timeout 60000`, `blocked` left out, no real completion guard: the launcher writes status, `implement.sh:629`). Correctness 1, risk 6, user impact 1, showrunner 2. Decisions 4-5; Phase 6.
- R2 — Racy dispatch without claims. Correctness 2, architecture 1, user impact 4. Per-worker dispatch lock (Phases 5-6).
- R3 — Busy refusal vs typed messages queue. Architecture 1, user impact 4, showrunner 3. Decision 6.
- R4 — Closed panes lose messages and detached results. Correctness 3, risk 1, risk 8. `can_close` gate and director fallback (Phases 5, 7); a durable inbox dropped: nothing is outstanding when a pane may close.
- R5 — Restart abandons the launcher. Correctness 4. `launcher_command` kept; rebuilt launcher waits for the same dispatch id (Phases 6, 8).
- R6 — Early review Stage 2 approves early. Correctness 5. Stage 2 has its own `DONE` line (Phase 7).
- R7 — Bounded shutdown has no worker-pane ownership. Risk 2. Phase 8.
- R8 — Restore has two owners; `pane_restore.py` closes a whole tab (verified :95-105). Risk 3, user impact 3, showrunner 5. Phase 4 probe, Phase 8, questions item 1(e).
- R9 — Pilot before its safety parts. Risk 4, user impact 6. Pilot moved to Phase 11; instructions branch on the pin from Phase 6.
- R10 — Account moves not fenced. Risk 5. Phase 9.
- R11 — `/model` vs resume; service tier dropped. Risk 10, user impact 5. Lookup at every start or resume includes tier; the user chose start or resume only (decision 2).
- R12 — Per-dispatch settings (cwd, sandbox, image, env). Correctness 8. `start-or-resume` compares and resumes; images by path (Phases 4-5).
- R13 — Ledger states and competing records. Correctness 6, 7, 9; architecture 3. Replaced by the staff record (user 2026-10-30); owed continuation kept as a `refused` pass (Phase 7).
- R14 — Names over 32 characters, order-dependent, silent truncation (verified `herdr.py:3092`). Correctness 10, architecture 9, risk 11, user impact 8. Answered by the user (2026-10-30): short names in the staff record, and the id as the herdr agent name and the session name, so no name is joined or cut (decision 2, Phase 1 spec items 3 and 7). This replaced the joined name form and its refuse placeholder.
- R15 — Three launcher copies. Architecture 2. Decision 11.
- R16 — `worker_panes.py` as a second herdr client. Architecture 4, risk 7, showrunner 3. Decision 10; Phase 4.
- R17 — Layout ownership and focus. Architecture 5, user impact 7. Phase 5 `layout`.
- R18 — Usage detection parsers and silent failure. Architecture 6, risk 9. One classifier (Phase 7).
- R19 — Two registry parsers and paths; models unit's rewrite. Architecture 7, showrunner 1. Phase 3, blocked on the models unit.
- R20 — Transport key in `models.conf`, no end date. Architecture 8, user impact 10, showrunner 7. Decision 12.
- R21 — Rename inventory misses callers; `affected_modules.py:109` (verified). Architecture 10. Reference → Rename inventory; Phase 3 guard test.
- R22 — Resume texts are not one constant. Architecture 11. Phase 8 builder.
- R23 — Instruction inventory misses files (verified `astra_review.md:32`). User impact 9. Reference → Instruction edits.
- R24 — Phase cleanup can close a newer user turn. User impact 2. `can_close` (Phase 5).
- R25 — Read-only reviewers cannot write a status file (verified `review.sh:689`). Showrunner 7. Decision 4.
- R26 — Numbered names for several implementers. Showrunner 7. Short names `impl1`, `impl2` under one unit (decision 2, Phase 1 spec item 7).
- R27 — Keep idle panes open. Showrunner 4. Settled by the user: close (decision 3).
- R28 — Dropping per-turn resolve undoes 710a6adb. Showrunner 7. The user chose option A: lookup at start or resume only (decision 2).
- R29 — Probe: interactive Codex and a new `auth.json`. Showrunner 6. Phase 4.

Refuted: none. Dropped: none.

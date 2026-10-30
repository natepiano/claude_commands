# Workers in herdr panes, and one staff record

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Approved to build on 2026-10-30 by the user, through the sdlc showrunner, before a plan review; the user reviews an overview in the morning. It adds one staff record of every showrunner, unit director and worker, retires `codex_mesh`, and runs every unit worker as an agent in a herdr pane in its director's tab. It renames `agents.conf` to `models.conf` and `/agent` to `/default_models`.

> **Production: natedev** — unit `herdr-unit`; production doc `docs/plans/natedev-production.md`. The unit moves to sdlc later; checkpoints go to sdlc.

Written by herdr-unit on 2026-10-29 for the sdlc showrunner, from the user's request relayed the same night. Team review 2026-10-30 (four lenses and the sdlc showrunner's review) and the user's later answers are folded in. A second team review on 2026-10-30 (simplicity first, correctness, rerun safety, API and types) is folded in too; see `## Review record`. The inventory was read from `~/.claude` main at 7e85a74b; `herdr.py` lines are at de7eada0.

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
  - `scripts/production/` — production tools: `showrunners.py`, `production_units.py`, `production_lifecycle.py`, `live_units.py`, `unit_lookup.py`, `add_unit.py`, `add_showrunner.py`, `retire_unit.py`, `rename_unit.py`, `transfer_unit.py`, `rename.py`, `promote_*.py`, `resume_unit.py`, `production_tree.py`, `broadcast.py`, `codex_winddown.py`; the new `staff.py` and `staff_sources.py`
  - `scripts/herdr/` — `herdr.py` (the one herdr client), the new `worker_panes.py`
  - `scripts/direct/` — the launchers `implement.sh`, `review.sh`, `assign.sh`/`assign.py`, `workers.py`, `worker_state.py`, `remove_workers.py`, `mesh_runs.py`, `board.sh`, `progress_history.py`
  - `scripts/agents/` — `codex_mesh.py`, `agents_config.sh`, `agent_admin.sh`, `agent_exec.sh`, `sync_codex_catalog.sh`
  - `scripts/message/` — `send.py`, `roster.py`, `sessions.py`
  - `scripts/shutdown/` — `inventory.py`, `stop.py`, `restart.py`, `shutdown_record.py`, `pane_record.py`, `pane_restore.py`, `remote.py`
  - `scripts/lib/` — `agents_registry.py`, `unit_runs.py`, `names.py`
  - `config/` — `agents.conf` (renamed `models.conf` in Phase 3), `direct.conf`, `names.toml`
  - `commands/`, `docs/direct/`, `docs/as-built/`, `skills/herdr/SKILL.md`
- **Key files:**
  - `scripts/production/showrunners.py` — showrunners from update timers (`registered_showrunners` :216, `current_name` :341, `parent_of` :427, `attach` :528, `detach` :557; parent files under `~/.local/state/showrunner/<slug>/parent`)
  - `scripts/production/production_units.py` — a production doc's Units table (`unit_table` :59, `unit_is_retired` :100, `SessionMarks` :84)
  - `scripts/production/unit_lookup.py` — units found by herdr pane marks `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID` (:30-31; `every_marked_unit` :316, `marked_units` :383, `marked_unit` :392)
  - `scripts/direct/workers.py` — the unit worker ledger under `~/.local/state/unit-workers/` (:529, `ledger_path` :1564), retired in the cleanup phase
  - `scripts/herdr/herdr.py` — `launch_agent` :3115, `launch_prepared_agent` :3178 (opens a tab today), `_agent_name` :3094 (truncates to 32 characters), `_herdr_resumes_agents` :1987, `Herdr.prompt` :1721, `Herdr.retitle` :1207, `agent_status: str` :738, :778, :786
  - `scripts/shutdown/pane_record.py` — the pane launch record: the SessionStart hook (`scripts/hooks/session-start-pane-record.py:37`) writes one entry per Claude launch in herdr, with the launch `-n` name and the env names in `KEPT_NAMES` and `KEPT_PREFIXES` (:48-49); `pane_restore.py:83-92` relaunches from it
  - `scripts/shutdown/remote.py` — `other_machine()` :25, the one rule for which workstation this is
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
  - **Rerun-safe tools (user, 2026-10-30).** Each tool this plan adds or changes that changes shared state in steps (start-or-resume, dispatch, staff record writes, seed, pane open and close, shutdown and restore, account moves, transfer, rename, retire, wrap) is safe to rerun. After a stop or a failure at any step, a rerun ends in the same state as one clean run. A rerun of a finished run reports done and changes nothing. Each such tool gets one rerun test in its phase's Acceptance gate; read-only tools need none. The phase review lens checks it.
  - **The worker transport is pinned per run.** `WORKER_TRANSPORT=mesh|herdr` in `config/direct.conf` is read at run start and pinned in the run folder. A run that started on the mesh finishes on the mesh. Every lifecycle caller (dispatch, messages, detached delivery, shutdown, rename, transfer, retire, cleanup) reads the run's pin through one resolver, `unit_runs.worker_transport(run)`.
  - **Tests never touch the user's tabs.** herdr tests run in a probe tab of `agents` (memory: one herdr session; `herdr.py` refuses other servers) and close it at the end. No test runs `herdr` against a live director's tab.
  - **Build hold.** Before any test or gate, check `build_hold.py status`; while a natedev hold that is not ours is in force (02:00-05:00 tonight), run nothing that builds or tests. Edits continue.
  - **Owners.** The models unit owns `agents_registry.py` and the registry writers; the shutdown unit owns `scripts/shutdown/`; the accounts unit owns `scripts/accounts/` and account moves. A phase that changes their files names the owner in its checkpoint notice and agrees the change with them first. The user owns `/etc/nixos` (questions list item 1).

## Decisions made in this plan

Each item is my call under the decision criteria unless it names the user. A one-line plan change reverses it.

1. **One staff record (user, 2026-10-30).** `scripts/production/staff.py` and one file, `~/.local/state/staff/staff.json` on natedev, hold every showrunner, unit director and worker: a stable id, a short name, the kind, and the parent row's id. So the record maps each short name to an id and holds the relationships (worker → unit → showrunner); production and unit come from the parent chain. A rename changes only the name and a transfer only the parent; the id never changes. There is no separate worker ledger: the unit worker ledger, `mesh_roster.json`, the run `workers` log and the slot claim files retire into this record. Live state (pane, socket, running or not) is never stored; it comes from herdr and the session records at read time. No such record exists today: the hierarchy is joined at each read from update timers, parent files, Units tables, pane marks and the worker ledgers (Reference → Staff record readers).
2. **One command starts or resumes a worker (user, 2026-10-30).** `worker_panes.py start-or-resume <short name>`. The role is the part of the name before the number (Phase 1 item 7), so the command takes no role argument. A worker's herdr agent name and its Claude or Codex session name are its staff id. Both Claude Code and Codex resume a conversation by its name (user, 2026-10-30), so the command resumes by id and keeps no conversation id. At every start or resume it looks up kind, model, effort, service tier and sandbox for that role in the model registry, and only then (user, 2026-10-30, option A): a `/model` typed by hand holds, no dispatch compares the model, and a config change does not restart a running worker. This replaces 710a6adb's per-turn lookup. The director picks the number: `impl1`, then `impl2` for a second implementer. The name is set once, when the row is made.
3. **Idle panes close at phase end (user, 2026-10-30).** The conversation stays. To bring a worker back, the director calls `start-or-resume` again. This settles the keep-open question.
4. **The worker's final reply signals completion, and the transcript is the receipt; herdr's state only wakes the launcher.** Every dispatch has a dispatch id, and the one line the launcher types carries it (decision 7). The worker's own transcript then holds the proof of each step: the typed line with the id proves the submission, and a final reply that ends with `DONE <dispatch id>` proves completion. The launcher writes the status file only from that reply. A rerun reads the transcript before it types anything, so it never types a pass twice (Phase 6). No other record of a submission exists, and the prompt file is never changed. The launcher writes the status file today (`implement.sh:629`); the worker writes only its summary. This works the same for read-only reviewers, which cannot write a file (`review.sh:689` starts them with `--sandbox read-only`): the launcher writes their findings file from the reply, as the mesh does today.
5. **The launcher's whole wait is one background `herdr agent prompt <id> "<line>" --wait`, with no `--timeout` and no `--until`.** herdr then waits for its default settled set, `idle`, `done` or `blocked`, after it sees the turn start (herdr skill, "For normal agent work, `--wait` is enough"). `blocked` wakes the launcher, so a worker at an approval dialog never stalls the director. The director runs the launcher in the background, as today, and the task notification is the wake-up.
6. **A dispatch refuses a busy worker; a message queues.** A dispatch is a new pass that a launcher waits on, so it needs an `idle` or `done` worker, under the worker's dispatch lock. A message (`send.py`, a detached-run result, review Stage 2) is typed at once, also during a turn: Codex and Claude queue typed input. A blocked or closed worker's message goes to its director. `pending.json` drops.
7. **A prompt goes in by file path, not as pasted text.** The launcher types one line: `Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>.`
8. **No launcher compaction, and no "Continue where you stopped." retry.** Interactive Codex and Claude compact by themselves.
9. **Codex stays a worker kind.** herdr starts `--kind codex` and `--kind claude` agents. The default worker kind per role stays where it is set today.
10. **`herdr.py` stays the one herdr client.** Launch into a split pane, layout changes and the typed agent state `AgentStatus` are new `herdr.py` primitives (Phase 4). `worker_panes.py` composes them and holds only `start_or_resume`, `dispatch`, `close` and `observe`, the one join of worker rows and herdr panes that every display reads (Phases 5, 10); its `_layout` is a private helper. One-line agent calls (`herdr agent send-keys <id> esc` to interrupt, `herdr agent read`, a message's `herdr agent prompt`) run directly and are not wrapped.
11. **The three launcher commands stay; their shared work moves to one place.** `implement.sh`, `review.sh` and `assign.sh` keep their arguments and their recorder, status-file and board work. Start, dispatch, wait and receipt live once in `worker_panes.py dispatch`. The cleanup phase merges the launchers when they are thin.
12. **The transport switch lives in `config/direct.conf`, with an end date.** `WORKER_TRANSPORT` is migration state, not a model default, so it stays out of `models.conf`. The cleanup phase deletes it, no later than 7 days after the default switch.
13. **The right pane shows the worker's short name (user, 2026-10-30).** For example `impl1`, with no production or unit name.
14. **The staff row is the one name (sdlc showrunner, 2026-10-30).** A hand `/rename` in a unit director's pane writes the row, and the tab title follows the row. Every launch and every restore of a member takes its name from its row (Phase 2). A showrunner's name is also its workspace's, which `Herdr.retitle` does not rename, so `/showrunner:rename` (`rename.py`) is the one way to rename a showrunner. A worker's name is set once (decision 13).

## Questions for the user

1. **The NixOS edits (yours).** (a) `modules/common/claude-git-filters.nix:35` `watchPaths`: `agents.conf` → `models.conf`. It is functional: until it changes, the git filter does not refresh on a `models.conf` change. (b) Comments in `modules/common/codex-catalog-sync.nix`, `modules/common/codex.nix:47`, `modules/common/shell.nix:139` (`/agent`) and `README.md:410`. (c) `modules/common/herdr-skill-local.md:7`: the rule "start a new agent in a new tab, not a split pane" must permit worker panes in the director's tab (`skills/herdr/SKILL.md:221` is built from it). (d) The sign-in path unit (natedev) and the launchd agent (Mac) that run `codex_mesh.py signin-changed`: they retire at cleanup, or point at the pane restart if Phase 4's Codex sign-in probe is negative. (e) Only if Phase 4's probe shows that herdr's own resume does not keep each pane's `--env` (its account): add `[session] resume_agents_on_restore = false` to `~/.config/herdr/config.toml` on natedev and the Mac, so our restore owns split tabs; if the probe is positive, herdr restores split tabs and our per-pane restore code retires for them. Options: you write them; or herdr-unit writes one diff file per edit for you to apply, as the builds unit did with `build-followups-memory-nixos-*.diff`. Recommendation: diff files; apply (a) when the rename phase lands, (c) and (e) before the default switch, the rest at cleanup.

## Phases

**Critical path.** Phase 3 waits on the models unit, and no other phase needs it: pane work reads model and effort through the registry API under either file name. The director runs Phases 4-13 when Phase 3 is still blocked, and runs Phase 3 when the models unit lands. The critical path is Phase 1 → 2 → 5 → 6 → 7 → 8 → 9 → 11 → 13: no worker pane opens before the writers keep the unit rows current and every director pane carries its `STAFF_ID` (Phase 2). Phase 4 runs beside Phases 1-2 and lands before Phase 5. Phases 10 and 12 run beside the path; Phase 11 also needs Phase 10 and questions items 1(c) and 1(e).

### Phase 1 — The staff record  · status: todo

#### Work Order

**Goal:** `scripts/production/staff.py` holds the showrunners, unit directors and workers of both machines in one file on natedev, each with a short name, a stable id and its parent's id; it answers lookups by id, by path (`sdlc/models/impl1`) and by kind and parent; and `staff_sources.py seed` fills it from each machine's own sources, matching each member by its id first, with no change to a shown name.

**Spec:**

1. **Two modules, one file.** `scripts/production/staff.py` holds the record: the row, the rules, the read and write API and its CLI. It imports none of today's sources, so the readers that move to it (Phase 12) make no import loop. `scripts/production/staff_sources.py` reads today's sources: `member` (item 5) and `seed` (item 8). The file is `~/.local/state/staff/staff.json`; the env var `STAFF_FILE` overrides the path (tests), and no function takes a path. One file for both machines, on natedev. `OWNER_MACHINE = "natedev"` is one constant in `staff.py`. This machine is the owner when `remote.other_machine()` (`scripts/shutdown/remote.py:25`) is not `OWNER_MACHINE`; on the Mac that call names `natedev`, the ssh target. On the Mac, every `staff.py` read and write runs on natedev through `ssh -o BatchMode=yes -o ConnectTimeout=10 natedev ~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py <verb> --json`, the call `roster.peer_snapshot` makes (`roster.py:1897-1900`). The Mac keeps no copy. When natedev cannot be reached, a Mac call raises `StaffUnreadable("natedev not reached")` and writes nothing.
2. **File form and the write transaction.** `{"schema": 1, "rows": [<row>, ...]}`, rows sorted by `id`, written with `json.dumps(..., indent=2, sort_keys=True)` and a final newline, so the same content always gives the same bytes. Every write takes `fcntl.flock` on `<file>.lock` before it loads the file, and holds it through the checks (item 4), id creation, the temp file write in the same folder, `fsync`, `os.replace` and an `fsync` of the folder. Under the lock, a write first removes temp files that a killed writer left. Reads take no lock; they see the whole old file or the whole new one. A file that does not parse, or has another `schema`, raises `StaffUnreadable(reason)`, and no write replaces it.
3. **The row.** A frozen dataclass with `to_json()` and `from_json()` (no `Any`; parse with typed checks):
   ```python
   StaffId = NewType("StaffId", str)
   Kind = Literal["showrunner", "unit", "worker"]

   @dataclass(frozen=True)
   class StaffRow:
       id: StaffId              # "s" + uuid4().hex[:11]; never changes, never reused, never made from a name
       kind: Kind
       name: str                # the short name: a showrunner's workspace and tab, a unit's tab, a worker's pane label
       parent: StaffId | None   # worker -> its unit, unit -> its showrunner, showrunner -> its parent showrunner or None
   ```
   The id is 12 characters of lower-case letters and digits led by `s`, so it matches herdr's agent-name rule `^[a-z][a-z0-9_-]{0,31}$` and herdr never cuts it (`herdr._agent_name`, `herdr.py:3094`). It is a worker's herdr agent name and its session name. A worker's role is the part of its name before the number (item 7); the row stores it nowhere else. The parent chain gives a worker's unit and showrunner, so the row stores neither. A retired member has no row (item 6).
4. **Rules every write checks**, under the lock, on the rows it would write. The graph: a worker's parent is a unit row, a unit's parent is a showrunner row, a showrunner's parent is `None` or a showrunner row, and no parent chain loops. The names: a unit or worker `name` is unique among rows of its kind with the same `parent` (so `impl1` exists under many units), and a showrunner `name` is unique among showrunners. The kind is part of the name rule, so a promote can add the new showrunner row before it removes the unit row of the same name. A write that breaks a rule raises `StaffRefused(reason)` and writes nothing.
5. **Read API.** Each read raises `StaffUnreadable`, a subclass of `OSError` (other unreadable sources here raise `OSError`, `showrunners.registered_showrunners` :216), when the file cannot be read.
   ```python
   # staff.py
   def by_id(staff_id: StaffId) -> StaffRow | None
   def by_path(names: str) -> StaffRow | None        # "sdlc", "sdlc/models", "sdlc/models/impl1"
   def find(*, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]
   # staff_sources.py
   def member(slug: str, unit: str | None = None) -> StaffRow | None
   ```
   `member` is the one way a tool or `seed` finds the row of a production's showrunner (`unit=None`) or of one of its units (`unit` is the Units table's `Unit` cell). It matches by id first: the `STAFF_ID` in the member's newest pane launch record on this machine (`pane_record.read(herdr.shared_session())`; Phase 2 adds `STAFF_ID` to `KEPT_NAMES`). A unit's entry is the one whose environment carries `SHOWRUNNER_UNIT=<slug>` and `SHOWRUNNER_UNIT_ID=<unit>`; a showrunner's entry is the one whose `session_id` is the session its update timer is addressed to. When that id names a row of the right kind, that row is the member, whatever its name is now (a hand `/rename` wrote it, Phase 2). Only a member with no such id is matched by name: the showrunner row named `showrunners.herdr_space(slug, doc)` (`showrunners.py:347`), or the unit row named `unit_names.name(<unit>)` (`unit_names.py:18`) under that showrunner's row. A hand `/rename` writes a row only from a pane with a `STAFF_ID`, so a member matched by name has a row that no hand `/rename` changed.
6. **Write API.** Each write returns its row and raises `StaffRefused`, `StaffNotFound(staff_id)` or `StaffUnreadable`. The CLI (item 9) is the one place that maps them to exit codes.
   - `ensure(kind, *, name, parent=None) -> StaffRow`: returns the row with that kind, name and parent, unchanged, when it exists; else adds it with a new id. A worker's name must parse as a worker name (item 7).
   - `rename(staff_id, new_name) -> StaffRow`: a showrunner or a unit; a worker is refused, since its name is set once. The same name returns the row unchanged. Only `name` changes.
   - `move(staff_id, new_parent) -> StaffRow`: a transfer moves a unit to another showrunner; an attach or a detach moves a showrunner under another one, or to `None`. A worker is refused. The same parent returns the row unchanged. Only `parent` changes; the rows under it follow through the id.
   - `retire(staff_id) -> StaffRow | None`: removes the row in one write. A unit goes with its worker rows. A showrunner moves its child showrunners to `None`, and is refused while a unit row is under it. A second call finds no row, returns `None` and changes nothing.
7. **Worker names.** `WorkerName.parse(name)` takes a role and a positive number, for example `impl1`; any other form is refused. `WorkerRole` is a `StrEnum` of the roles, named as the model registry names them: `impl`, `test`, `fix`, `review` (`[direct.*]`) and `designer`, `reviewer` (`[design_roles.*]`). The director picks the number: `impl1`, then `impl2` for a second implementer. The name is set once, when the row is made, and it is the pane label. An ask-a-friend pane is not staff (Phase 10).
8. **`staff_sources.py seed`** makes the showrunner and unit rows match this machine's sources. Each short name comes from the source that names its workspace or tab today, so no shown name changes. Seed reads this machine's sources into one snapshot, then `staff.apply_seed(snapshot)` applies it in one write under the lock. On natedev it applies there. On the Mac, the snapshot goes to natedev on stdin through the same ssh call as other writes (`staff.py apply-seed --json`), so the Mac's timers, docs, worktrees and pane records are read on the Mac.
   - Showrunners: `showrunners.registered_showrunners()` (`showrunners.py:216`) reads this machine's update timers. Each showrunner is its `member(slug)` row, kept with its name; else `ensure("showrunner", name=showrunners.herdr_space(slug, runner["doc"]))`. `herdr_space` is the doc's `Showrunner session:` line, else the slug, and it names the production's workspace today (`add_showrunner.py:153`, `add_unit.py:958`, `restart.py:736`); `add_showrunner.py:148, 153` names the showrunner's session and its own tab with the slug, the same name. Then its parent is the row of `showrunners.parent_of(slug)` (`:427`), or `None` with no parent; a parent with no row yet leaves `parent` as it is until a later seed.
   - Units: for each showrunner's production doc (`runner["doc"]`), read the Units table with `production_units.unit_table` (`production_units.py:59`); skip a row where `unit_is_retired(cells, SessionMarks(slug))` (`:100`); take a unit only when its `Worktree` cell names a folder that exists on this machine. Each unit is its `member(slug, <Unit cell>)` row, kept with its name, and its parent is set to the showrunner's row; else `ensure("unit", parent=<the showrunner's id>, name=unit_names.name(<Unit cell>))`. `add_unit.py:326` names a new unit with `unit_names.name`, and `add_unit.py:953, 959` start the unit's session under that name (`-n`) and title its tab with it (`herdr-unit` has the tab `herdr`).
   - Remove: under a showrunner whose doc this seed read, a unit row that no taken Units row matched (by id or by name) is retired: the Units table marks it retired or no longer lists it. Seed never retires a showrunner row: it reads only this machine's timers, and a missing timer does not prove that a production ended; wrap retires it (Phase 2). A source that cannot be read (an `OSError`, an unreadable doc) changes none of its rows and prints one stderr line naming it.
   - Workers are not seeded: mesh workers stay in the unit worker ledger until their unit switches to panes, and `start-or-resume` adds pane workers (Phase 5). Self-made call: the record lacks mesh workers for the migration days; a reader that needs them still reads the ledger until the readers phase.
9. **CLI.** `staff.py show [--kind K] [--under <id or path>] [--json]` prints one aligned row per staff row (id, kind, path), sorted by path; the path is the row's short names from its showrunner down (`sdlc/models/impl1`). `staff.py get <id or path> [--json]` exits 1 when not found. The write verbs `staff.py ensure|rename|move|retire|apply-seed --json` are the Mac transport (item 1): the Python API on the Mac calls them over ssh, and no instruction tells a person to run them. `staff_sources.py seed [--dry-run]` prints each row it would add, keep, move or retire. A call that changes nothing says `unchanged` on stderr. Exit codes: 0 done, 1 not found, 2 refused or unreadable, with the reason on stderr.
10. **Scope note (author's call).** This phase moves no reader and no writer; Phase 2 moves the writers and the readers phase moves the readers. Until then nothing reads the record, so its rows can trail the old sources without harm, and `seed` catches them up.

**Files:**
- `scripts/production/staff.py` — new: the row, `WorkerName`, `WorkerRole`, the rules, the read and write API, `apply_seed`, `OWNER_MACHINE`, the exceptions, the CLI.
- `scripts/production/staff_sources.py` — new: `member`, `seed`.
- `scripts/production/test_staff.py`, `scripts/production/test_staff_sources.py` — new: the tests in the Acceptance gate, with a fake `ssh` on `PATH` for the Mac path.
- Read only: `scripts/production/showrunners.py`, `scripts/production/production_units.py`, `scripts/production/unit_names.py`, `scripts/shutdown/pane_record.py`, `scripts/shutdown/remote.py` (the sources named above).

**Workers:** `2 writers` — no test lane; the tests split from the modules.
- `impl` — `scripts/production/staff.py`, `scripts/production/staff_sources.py`; hub: `scripts/production/staff.py` (the API the tests import).
- `test` (opens as impl) — `test_staff.py` and `test_staff_sources.py`, written from the Spec alone, with the sources pointed at fixture folders through `STAFF_FILE`, `NOTIFIER_STATE_DIR`, `SHOWRUNNER_STATE_DIR` and `HERDR_PANE_RECORD_DIR`.

**State:** `~/.local/state/staff/staff.json` and `staff.json.lock` (new). `seed` writes rows. A crash between the temp write and `os.replace` leaves the old file whole.

**Constraints from prior phases:** none.

**Acceptance gate:**
- `basedpyright scripts/production/staff.py scripts/production/staff_sources.py scripts/production/test_staff.py scripts/production/test_staff_sources.py`: `0 errors, 0 warnings`.
- `~/.claude/scripts/lib/py -m unittest discover -s scripts/production -p 'test_staff*.py'` green, with these tests:
  - The rerun test (Invariants → Rerun-safe tools): each write verb run twice gives the file bytes of one run, and the second run says `unchanged`. It covers `ensure` of a showrunner, a unit and a worker (a worker ensured twice under one unit gives one row), `rename`, `move`, `retire` (the second call returns `None`), and `seed` on fixture timers, parent files, a fixture production doc and fixture pane launch records.
  - The seed id test: a unit row renamed as a hand `/rename` does (`rename(id, "panes")`), with a fixture pane launch record whose environment carries its `STAFF_ID` and its unit marks, then `seed` run twice: the unit keeps its id, the name `panes` and one row, nothing is retired, and the second run says `unchanged`. The same for a showrunner row renamed with `rename`, matched by the session id of its timer. A unit with no `STAFF_ID` in any record is matched by name.
  - The Mac seed test: on a machine that is not `OWNER_MACHINE`, `seed` reads the Mac fixture folders and sends that snapshot to the fake `ssh`; the rows written come from the Mac fixtures, not from the owner's.
  - `impl1`, `test1`, `fix1`, `review1`, `designer1` and `reviewer1` parse; `impl`, `impl0` and `bob1` are refused.
  - The same short name under two units is allowed; under one unit it is refused, and the file is unchanged.
  - A worker under a showrunner, a unit under a unit or under a missing id, a move that makes a loop, a rename or a move of a worker, and the retire of a showrunner with a unit under it are refused, and the file is unchanged.
  - `by_id`, `by_path("sdlc/models/impl1")` and `find` by kind and parent return the expected rows; a retired unit and its workers are gone.
  - `rename` keeps the id; `move` changes only `parent`, and the moved unit's workers then resolve through `by_path` under the new showrunner.
  - Every new id matches herdr's agent-name rule `^[a-z][a-z0-9_-]{0,31}$`.
  - Two processes that call `ensure` with the same kind, parent and name at the same time both return the same id, and the file has one row. Two processes that write two different changes at the same time both land.
  - An exception raised before `os.replace` leaves the old file bytes; a temp file that a killed writer left is removed by the next write.
  - On a machine that is not `OWNER_MACHINE`, `by_id`, `find` and `ensure` run the fake `ssh` command with the verb and `--json`, and a failed `ssh` raises `StaffUnreadable` and writes no local file.
  - A fixture doc that cannot be read leaves its rows and prints one stderr line.
- The phase's full gate (Delegation Context → Test) green, run outside the build hold.
- Behavior: `staff_sources.py seed` on natedev and on the Mac, then `staff.py show` on either machine, lists both machines' showrunners and units, each with an id and with the names their workspaces and tabs show now; a second `seed` says `unchanged` and leaves `staff.json` byte for byte the same.

### Phase 2 — Staff record writers  · status: todo

#### Work Order

**Goal:** every tool that adds, renames, moves or retires a showrunner or unit writes the staff record as one named step of its own work, so the record stays current without `seed`; a rerun after a stop at any step ends as one clean run; and every launch of a member takes its name from its row.

**Spec:**

1. **Find the row by id.** Before its first change, each tool finds its member's row with `staff_sources.member(slug, unit)` (Phase 1 item 5), which matches by the `STAFF_ID` in the pane launch record first. A tool with a journal (`rename.py`, `rename_unit.py`, `transfer_unit.py`) writes that id into its journal at its first step, and a rerun reads the id from the journal, because a stop partway can leave the old name and marks changed. No tool finds a row by a name or a path.
2. **The staff step, checked before any change.**
   - An add, a rename or a move is written after the tool's own checks and before its first change. The write checks Phase 1's rules under the lock (item 4) and raises `StaffRefused` before anything else changed, so a refused rename, transfer or attach (a name taken, a parent of the wrong kind, a loop) leaves no source changed. The tool prints the reason and exits 2.
   - A removal is written after the tool's last change that needs the row, because until then the tool finds the unit's workers through their rows. A unit's `retire` is never refused (Phase 1 item 6).
   - `StaffUnreadable` (natedev not reached from the Mac) stops the tool at its staff step with the reason; no step after it ran.
   - A rerun repeats the staff step, which returns the row unchanged when the change already holds (Phase 1 item 6), and goes on. Rerunning the same tool completes it; `seed` is not the recovery path.
3. **The writers.**
   - Add and resume → `ensure`: `add_showrunner.py` the showrunner row, named `showrunners.herdr_space(slug, doc)`; `add_unit.py` and `resume_unit.py` (through `add_unit.py --resume`) the unit row under the showrunner's row, named `unit_names.name(<Unit cell>)`. A unit whose row `retire` removed gets a new row and a new id when it resumes, as a new unit does (Phase 1 item 6).
   - Rename → `rename(id, new_name)`: `rename.py` the showrunner row, `rename_unit.py` the unit row; the workspace and tab titles change as today.
   - Transfer → `move(id, new_parent)`: `transfer_unit.py` moves the unit row.
   - Attach and detach → `move` of the showrunner row: `showrunners.py` `attach` (:528) and `detach` (:557). Both set a desired state: an attach whose parent file already names that parent, or a detach with no parent file, goes on to the staff step and reports done when the row already matches. Neither refuses its own finished work.
   - Retire → `retire_unit.py` calls `retire(<unit id>)` after its last change; the call removes the unit row and its worker rows.
   - Wrap → `production_lifecycle.py` `wrap` (:412) calls `retire` for each unit row it retired (each unit in its loop, :461-474), then `retire` for the showrunner row, which moves its child showrunners to `None` (Phase 1 item 6). These calls come after the unit loop and before the update timer goes (:476), because `member(slug)` finds the showrunner through its timer. A permanent unit (`Plan` cell `permanent`, :119) outlives the wrap: its row stays, so the showrunner row stays too (`retire` refuses a showrunner with a unit under it), and wrap prints one line that names them.
   - Promote → `ensure` the new row before the first change, `retire` the old row after the last: `promote_showrunner.py` adds the showrunner row and removes the unit row; `promote_unit.py` adds the unit row. The relaunch carries the new row's `STAFF_ID`.
   - A rename or a transfer never touches a worker row, a herdr agent or a session: workers keep their ids and follow through `parent`. No reader changes in this phase.
4. **Launch names come from the row.** `add_unit.py` (:953, :959; `resume_unit.py` through it) and `add_showrunner.py` (:148, :153) pass the row's name for `-n` and the tab title. `pane_restore.py` today relaunches with the `-n` name saved at launch (`claude_arguments` :84-86) and titles the tab with the pane label or that name (:101), so a restart would undo a hand `/rename`. For an entry whose environment carries `STAFF_ID`, restore takes the name from `staff.by_id(STAFF_ID)` for `-n`, `--remote-control` and the title. When the record cannot be read (natedev not reached from the Mac) or holds no such row, restore uses the saved name and prints one line saying so, so a restart never waits on natedev. `pane_restore.py` is the shutdown unit's file (Invariants → Owners).
5. **A hand `/rename` writes the row of a unit (sdlc showrunner, 2026-10-30; decision 14).** The staff row is the one name of each member; a tab title that is not its row's name is a second name for one member, a copy. Today the Stop hook `scripts/hooks/herdr_tab_title.py` reads the session's name from its session record (`current_name` :28-36) and gives that name to the pane and the tab (`sync` :39-51, through `herdr.retitle`, `herdr.py:1207`); the row stays as it was. The hook changes on unit panes only: a pane whose env has `STAFF_ID` and `SHOWRUNNER_UNIT_ID` and no `UNIT_TEAM_ROLE`. When the session name is not the pane label, the hook calls `staff.rename(STAFF_ID, <session name>)` and gives the returned row's name to the pane and the tab. A turn with no rename makes no staff call (on the Mac each staff call is an ssh to natedev, Phase 1 item 1). A refused rename changes no title, and the session record keeps the refused name until the next launch, which takes the row's name (item 4). The hook never stops the turn (`main` :54-60). Other panes:
   - A showrunner pane (`STAFF_ID` and no unit marks): the hook makes no staff call and changes no title. `/showrunner:rename` (`rename.py`) renames the row, the workspace and the tab together (decision 14).
   - A worker pane (`UNIT_TEAM_ROLE`, Phase 5): unchanged. Its session name is its id (decision 2), and its label is set once at start (decision 13).
   - A pane with no `STAFF_ID` (an ad hoc session, or a member launched before this phase) has no row, so the hook retitles it as today.
   Nothing else stores a shown name.
6. **The member id in the pane env.** No mark carries a staff id today. A unit pane carries `SHOWRUNNER_UNIT` (the production slug) and `SHOWRUNNER_UNIT_ID` (the unit name, for example `herdr-unit`, not a staff id), set at `add_unit.py:923-924` (keys at `unit_lookup.py:30-31`). A showrunner pane carries neither (`add_showrunner.py:149`). So the unit launch (`add_unit.py:923`; `resume_unit.py` also runs it, through `add_unit.py --resume`) and the showrunner launch (`add_showrunner.py:149`) also set `STAFF_ID=<the row's id>`, the key that Phase 5 gives each worker pane. A restart keeps only the names in `KEPT_NAMES` and the `SHOWRUNNER_` prefix (`scripts/shutdown/pane_record.py:48-49`, read back at `pane_restore.py:98`), so `STAFF_ID` joins `KEPT_NAMES` (the shutdown unit's file; Invariants → Owners). `herdr.unmarked_environment` (`herdr.py:835-837`) drops `STAFF_ID` with the unit marks, so a herdr server started from a member's pane never gives that id to other panes. A session started before this phase has no `STAFF_ID` until its next launch.

**Files:** `scripts/production/add_showrunner.py`, `showrunners.py`, `add_unit.py`, `resume_unit.py`, `retire_unit.py`, `rename_unit.py`, `rename.py`, `transfer_unit.py`, `promote_unit.py`, `promote_showrunner.py`, `production_lifecycle.py`; `scripts/hooks/herdr_tab_title.py` and `scripts/hooks/test_herdr_tab_title.py`; `scripts/herdr/herdr.py` (`unmarked_environment`); `scripts/shutdown/pane_record.py` (`KEPT_NAMES`) and `scripts/shutdown/pane_restore.py` (the restore name), both the shutdown unit's; their tests (`test_*.py` and `*_test_support.py` beside each); `scripts/production/test_staff.py` (shared fixture helper only, if two test files need it). Read only: `scripts/production/staff.py`, `staff_sources.py`.

**Workers:** `2 writers` — split by tool group.
- `impl` — add, resume, retire, promote and wrap tools, the `STAFF_ID` launch env (`herdr.py`, `pane_record.py`), the restore name (`pane_restore.py`) and their tests; hub: `scripts/production/test_staff.py` (the one staff fixture helper).
- `test` (opens as impl) — `showrunners.py`, `rename.py`, `rename_unit.py`, `transfer_unit.py`, the hand `/rename` hook (`herdr_tab_title.py`) and their tests.

**State:** rows in `~/.local/state/staff/staff.json`; the staff id in the journals of `rename.py`, `rename_unit.py` and `transfer_unit.py`.

**Constraints from prior phases:** Phase 1 built `staff.py` (`ensure`, `rename`, `move`, `retire`, `by_id`, the exceptions `StaffRefused`, `StaffNotFound`, `StaffUnreadable`; `STAFF_FILE` for tests) and `staff_sources.member`.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Each tool's tests assert the staff row after the tool runs.
- The refusal test: a rename to a taken name, a transfer to a showrunner that already has a unit of that name, and an attach that makes a loop each raise `StaffRefused` and leave every source and the staff file unchanged.
- One rerun test per tool (Invariants → Rerun-safe tools), named `test_<tool>_rerun`: the tool stopped just before its staff step and just after it, then run again, gives the sources and the staff file of one clean run; a rerun of a finished run reports done and changes nothing. An attach of an attached showrunner and a detach of a detached one report done and change nothing.
- The hand-rename test: a unit renamed by its hook, then `transfer_unit.py` and `retire_unit.py`, with its director offline, each find its row by the `STAFF_ID` in the saved pane launch record.
- The wrap test: the rows of the wrapped units and of the showrunner go, and its child showrunners move to `None`; a permanent unit's row and its showrunner's row stay, with one line naming them; wrap stopped after the first unit row's `retire`, and again after the showrunner row's, then run again, ends as one clean wrap.
- The hook test in `test_herdr_tab_title.py`: a hand `/rename` of a unit session (a fixture session record, a pane env with `STAFF_ID` and unit marks, a fake herdr) changes its row's name, keeps its id, and the tab and pane then show the row's name; a refused rename changes no title; a showrunner pane and a worker pane make no staff call and no title change; a rerun with the same name makes no staff write and no herdr rename.
- The restore test: an entry with `STAFF_ID` whose row was renamed relaunches with the row's name for `-n` and the title; with the record unreadable it uses the saved name and prints one line.
- Full gate green.

### Phase 3 — Rename agents.conf to models.conf and /agent to /default_models  · status: todo

**Blocked by:** the models unit's registry rewrite (`agents_registry.py` with `RegistryRow` and atomic edits; overlay files removed) and its shared config test fixture, landed on main.

#### Work Order

**Goal:** one name everywhere: `config/models.conf`, `MODELS_CONFIG_FILE`, `models_config.sh`, `models_registry.py`, `/default_models`; no live reference to the old names remains.

**Spec:** One sweep in one commit, no fallback that reads an old name, agreed with the models unit before the edit. Renames: `config/agents.conf` → `config/models.conf`; `AGENTS_CONFIG_FILE` → `MODELS_CONFIG_FILE` (the overlay env var drops with the overlay); `scripts/agents/agents_config.sh` → `models_config.sh`; `scripts/lib/agents_registry.py` → `models_registry.py`; `commands/agent.md` → `commands/default_models.md`; `scripts/agents/agent_admin.sh` → `default_models.sh`; the models unit's shared fixture follows the file. `models_registry.py` is the one parser and the one path authority: the Python path constants (`progress_history.DEFAULT_AGENTS_CONFIG_PATH`, `context_usage.DEFAULT_AGENTS_CONFIG_PATH`, `codex_pacer.AGENTS_CONF`, `quota_alert.REGISTRY`) and the line parsers (`codex_pacer.py:440`, `quota_alert.py:704`, `context_usage.py:269`, `progress_history.py`) call it; bash callers get resolved values from one `models_registry.py` call per launch that prints shell assignments, so bash parses no registry line. The full usage list is Reference → Rename inventory; build the manifest from a repository search, not from that list alone. `affected_modules.py:109` names `agents_config.sh` and changes to `models_config.sh`. Add `scripts/agents/test_no_old_model_names.py`: it fails on any live (non-history) reference to `agents.conf`, `AGENTS_CONFIG_FILE`, `agents_config.sh`, `agents_registry` or the `/agent` command. Send the user the NixOS diff files (questions list item 1).

**Files:** Reference → Rename inventory, plus `scripts/agents/test_no_old_model_names.py` (new).

**Workers:** `2 writers` — split by tree. `impl` — `config/`, `scripts/agents/`, `scripts/lib/`, `scripts/direct/`, `commands/`; hub: `scripts/lib/models_registry.py`. `test` (opens as impl) — `scripts/whoami/`, `scripts/hooks/`, `scripts/astra/`, `scripts/history/`, `scripts/production/`, `scripts/notify/`, `scripts/cli_agent/`, `scripts/ask_a_friend/`, docs, and the new test.

**State:** `config/agents.conf` moves to `config/models.conf`.

**Constraints from prior phases:** none from Phases 1-2.

**Acceptance gate:** `test_no_old_model_names.py` green; basedpyright 0/0 on changed files and callers; full gate green. No rerun test: the sweep is one commit, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

### Phase 4 — herdr primitives for worker panes, and probes  · status: todo

#### Work Order

**Goal:** `herdr.py` can start an agent in a new split pane, set split ratios and read agent states as one typed value, and the probe answers are recorded in this plan.

**Spec:** `launch_prepared_agent` (`herdr.py:3178`) takes `destination: NewTab | SplitPane` in place of its tab arguments. `NewTab(space, title)` holds today's tab arguments, and every caller (`launch_agent` :3115 and the others) passes one. `SplitPane(of_pane, direction, ratio, label)` takes `direction: Literal["right", "down"]`, refuses a ratio outside (0, 1) when it is made, and labels the new pane before the agent starts, so a launch killed after the split leaves a pane that `start-or-resume` finds by its label (Phase 5). Both destinations keep the same environment proof, launch claim, prompt-once proof and cleanup. Add `Herdr.split_ratio(pane, ratio)` over the socket's `layout.set_split_ratio`, and `Herdr.label_pane(pane, text)`. Every split uses `--no-focus`. `AgentStatus`, a `StrEnum` of herdr's agent states (`working`, `idle`, `done`, `blocked` and the others herdr reports), replaces `agent_status: str` (:738, :778, :786) and is decoded once where herdr's JSON is read; a state herdr adds later decodes as `AgentStatus.UNKNOWN`, which no caller reads as settled. Probes, in a probe tab of `agents`, written into Reference → Probe results: which side `--ratio` measures; whether a close rebalances the rest; Codex and Claude queue a message typed mid-turn; herdr's Codex state over a 10-minute turn; prompt by path; both CLIs open an image by path from a prompt file; the exact Codex input that names a new session (help shows no `--name` flag; Claude uses `--name`); whether herdr's `resume_agents_on_restore` keeps each pane's `--env` (a positive answer retires our split-tab restore code; a negative one needs questions list item 1(e)); whether interactive Codex picks up a new `auth.json` by itself (a positive answer retires the sign-in restart, Reference job 8).

**Files:** `scripts/herdr/herdr.py`, `scripts/shutdown/restart.py` (the two `launch_prepared_agent` calls, :1197 and :2784, pass `NewTab`; the shutdown unit's file), `scripts/herdr/test_herdr*.py` (the existing launch tests), this plan's Reference → Probe results.

**Workers:** `1 writer + 1 tester` — `impl`: `herdr.py`, `restart.py`; `test` (opens as impl): the tests and the probes.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** basedpyright 0/0 on `herdr.py` and every caller module; launch tests cover both destinations, a ratio outside (0, 1) refused when the `SplitPane` is made, the label set before the agent starts, and a failed start closing its pane; `AgentStatus` decodes each state herdr reports, and an unknown one as `UNKNOWN`; probe results written; full gate green. A launch killed after the split is reconciled by `start-or-resume`, which holds the worker's key; its rerun test is in Phase 5.

### Phase 5 — worker_panes.py: start-or-resume, close, observe  · status: todo

#### Work Order

**Goal:** `worker_panes.py start-or-resume <short name>` starts or resumes a worker in its director's tab with the registry's settings for the role in its name; `close` refuses a pane that still has work; `observe` is the one join of worker rows and herdr panes.

**Spec:**

1. **API and CLI.**
   ```python
   # worker_panes.py
   @dataclass(frozen=True)
   class WorkerPane:
       row: StaffRow
       pane: str | None                   # herdr pane id; None when no pane is open
       status: herdr.AgentStatus | None   # None when no pane is open

   @dataclass(frozen=True)
   class Closed: ...

   @dataclass(frozen=True)
   class Kept:
       blocker: str

   class ExitCode(IntEnum):
       OK = 0
       REFUSED = 2
       KEPT = 7          # Phase 6 adds the dispatch outcomes

   def start_or_resume(unit: StaffId, name: WorkerName) -> WorkerPane
   def close(unit: StaffId, name: WorkerName) -> Closed | Kept
   def observe(unit: StaffId) -> list[WorkerPane]
   ```
   The CLI is `worker_panes.py start-or-resume <short name>`, `close <short name>` and `list [--json]`, and `ExitCode` is its one table of exit codes. Each command addresses a worker by its short name within the caller's unit: the row named by the caller's `STAFF_ID` (its pane env, Phase 2), which must be a unit row; any other caller gets `REFUSED`. `close` gives `KEPT` with the blocker on stderr. `list` prints `observe` for that unit.
2. **`start_or_resume`.** `staff.ensure("worker", parent=<unit id>, name=<short name>)` gives the row; two callers at once get one row (Phase 1 item 2). Then, under the worker lock `~/.local/state/staff/locks/<id>.lock`, held through pane reconciliation, the launch and the launch proof:
   - **Reconcile.** In the director's tab, find this worker's panes: a pane whose herdr agent name is the row's id, and a pane with the worker's label that runs no agent (a launch killed after the split, Phase 4). When one pane runs a live agent named with the id, it is the worker: report done and change nothing (model and effort are looked up only at start or resume; a `/model` typed by hand holds; decision 2). Close every other pane found, and only those.
   - **Start.** With no live pane left, split the column (first worker: the director pane, `right`, ratio 0.667; later ones: the lowest worker pane, `down`) through `launch_prepared_agent` with a `SplitPane` whose label is the short name. The herdr agent name is the row's id. The agent resumes with `--resume <id>` (Claude) or `codex resume <id>` when a conversation of that name exists, else starts a new session named with the id (Phase 4 probe).
   - **Settings.** Kind, model, effort, service tier and sandbox come from the registry row of the role in the name: `[direct.<role>]` for `impl`, `test`, `fix` and `review`, `[design_roles.<role>]` for `designer` and `reviewer`. One private function, `_registry_row(role)`, holds this mapping. The account comes from the accounts code as today (Codex: the `workers` role).
   - **Env.** The pane env carries `STAFF_ID=<id>`, `UNIT_RUN_DIR`, `UNIT_TEAM_ROLE=<role>`, `TMPDIR` and the dev shell.
   - Then `_layout`.
3. **`close`**, under the worker lock, passes only `can_close`: dispatch lock free; the last turn ends with the `DONE` line of its last dispatch (decision 4) and the launcher recorded that pass complete; no input after it; state `idle` or `done`; no live detached run of the worker. Otherwise it keeps the pane and names the blocker. It then closes the pane and runs `_layout`. A close that finds no pane runs `_layout` and reports done.
4. **`_layout`** (private): under a per-tab lock, set every split in the worker column to an even share. It changes only panes whose agent name is the id of a worker row whose parent is this tab's unit, leaves other panes as they are, and gives focus back where it was. Even shares already in place change nothing.
5. **`observe(unit)`**: `staff.find(kind="worker", parent=unit)` joined with `herdr agent list` on the agent name, with `AgentStatus` from Phase 4. It is the one join: `list`, `close`, messages (Phase 7), shutdown (Phase 8), account moves (Phase 9) and every display (Phase 10) read it, and no other module joins staff rows with herdr panes.
6. A rename or a transfer of the unit or its showrunner changes no worker pane, agent or session (Phase 2).

**Files:** `scripts/herdr/worker_panes.py` (new), `scripts/herdr/test_worker_panes.py` (new).

**Workers:** `1 writer + 1 tester` — `impl`: `worker_panes.py`; `test` (opens as impl): `test_worker_panes.py` against a probe tab.

**State:** staff rows; panes; `~/.local/state/staff/locks/<id>.lock`.

**Constraints from prior phases:** Phase 1 `staff.py` (`ensure`, `find`, `WorkerName`, `WorkerRole`, `StaffId`); Phase 2 `STAFF_ID` on every director pane and unit rows kept current by the writers; model, effort, tier and sandbox come from the models unit's registry API (`RegistryRow` lookups), called by module whatever the file is named (`agents_registry.py` before Phase 3, `models_registry.py` after); Phase 4 `SplitPane` with its label, `split_ratio`, `label_pane`, `AgentStatus` and the probe results.

**Acceptance gate:**
- basedpyright 0/0 on the new files.
- Tests: start; resume by id; the agent name is the id, the pane label the short name, and the settings the role's registry row (`impl1` → `[direct.impl]`, `fix1` → `[direct.fix]`, `designer1` → `[design_roles.designer]`); a caller whose `STAFF_ID` is not a unit row is refused; two `start-or-resume` calls at once for one name give one row and one pane; `close` refused with work left; a foreign pane untouched; `observe` returns each worker row with its pane and status, and `None` for a closed one.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_start_rerun_after_row_write`; `test_start_rerun_after_split` (the labeled pane with no agent is closed, and one pane opens); `test_start_rerun_after_agent_start` (the live agent is kept, and no second pane opens); `test_start_rerun_after_launch_proof` (on an open pane it reports done and changes nothing); `test_close_rerun_after_pane_close_before_layout` (the rerun gives the panes and ratios of one clean close); a rerun of a finished close reports done and changes nothing.
- Full gate green.

### Phase 6 — Dispatch and the launcher switch, landed dark  · status: todo

#### Work Order

**Goal:** with `WORKER_TRANSPORT=herdr` pinned in a run, `implement.sh`, `review.sh` and `assign.sh` dispatch through `worker_panes.py dispatch`, whose receipt is the worker's transcript; the default stays `mesh`.

**Spec:**

1. **API and CLI.** `dispatch(unit: StaffId, name: WorkerName, prompt: Path, dispatch_id: str) -> ExitCode`; CLI `worker_panes.py dispatch <short name> <prompt path> <dispatch id>`. Phase 5's `ExitCode` gains `NO_DONE = 3`, `BLOCKED = 4`, `STALLED = 5` and `BUSY = 6`; `OK` means complete.
2. **The transcript is the receipt (decision 4).** `dispatch` keeps no record of its own of a submission. Its steps:
   1. Take the worker's dispatch lock (non-blocking; held → `BUSY`).
   2. `start_or_resume`.
   3. Read the worker's own transcript:
      - a final reply that ends with `DONE <dispatch id>` → finish: write the reply file (for a reviewer, the findings file) and return `OK`; type nothing;
      - the typed line with this dispatch id and no such reply yet → the pass went in: go to the wait (step 5); type nothing;
      - no line with this dispatch id → the state must be `idle` or `done`, else `BUSY` (a rerun later finds the line when the turn was this pass); then type (step 4).
   4. Type one line, `herdr agent prompt <id> "Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>." --wait` (no timeout, no `--until`; decisions 5, 7). The prompt file is never changed.
   5. Wait. When herdr settles, read the transcript again: the `DONE <dispatch id>` reply → finish as in step 3; `blocked` → `BLOCKED`; settled without the line while `herdr agent get` says `working` → `herdr agent wait <id>` again; settled without the line → `NO_DONE`, and the director reads the pane; `agent_prompt_stalled` or a timeout → `STALLED`, and the line is never typed again.
3. **The launchers** keep their status, recorder, board and summary work, and write `implemented` only on `OK`. `unit_runs.worker_transport(run)` reads `WORKER_TRANSPORT` from `config/direct.conf` at run start and pins it in the run folder.
4. **Restart.** `inventory.py` keeps recording `launcher_command` (:861). A rebuilt launcher runs with the same dispatch id, so it reads the transcript first and types nothing when the line is already there (Phase 8).
5. Several implementers use `impl1`, `impl2`. Transport-aware text in `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<FixDispatch/>`, `<PhaseCleanup/>`) lands with this phase, branched once on the run's pin.

**Files:** `scripts/herdr/worker_panes.py`, `scripts/direct/implement.sh`, `review.sh`, `assign.py`, `scripts/lib/unit_runs.py`, `config/direct.conf`, `commands/unit/direct.md`, launcher tests, `scripts/herdr/test_worker_panes.py`.

**Workers:** `2 writers` — `impl`: `worker_panes.py`, `unit_runs.py`, `direct.conf`; hub: `worker_panes.py`. `test` (opens as impl): the three launchers, `direct.md`, launcher tests.

**State:** run pin file; dispatch locks. No submission record: the transcript holds it.

**Constraints from prior phases:** Phase 5 `start_or_resume`, `can_close`, `ExitCode`, locks.

**Acceptance gate:**
- basedpyright 0/0 on changed Python files and every caller module; `bash -n` and `shellcheck` with no new findings on the changed `.sh` files.
- Tests for long, fast, blocked, false-idle and stalled turns; two launchers at once get one dispatch and one `BUSY`; a mesh-pinned run is unchanged.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_dispatch_rerun_before_prompt` (stopped after the lock and the start, before the line is typed: the rerun types it once); `test_dispatch_rerun_after_prompt` (stopped after the line is typed: the rerun finds it in the transcript, waits for the same pass and types nothing); `test_dispatch_rerun_after_done` (stopped after the `DONE` reply and before the reply file: the rerun writes the reply file and types nothing); a rerun of a finished dispatch returns `OK` and changes nothing. After each, the prompt file is byte for byte the same.
- Full gate green.

### Phase 7 — Messages, detached runs and review Stage 2  · status: todo

#### Work Order

**Goal:** messages, detached-run results and review Stage 2 reach a pane worker, and none is lost.

**Spec:** `send.py` `worker:<unit>/<short name>` is an address a person or a director types, so it resolves through `staff.by_path` (under the sender's showrunner; `worker:<showrunner>/<unit>/<short name>` from outside it) to the worker's id, its herdr agent name. The pane comes from `worker_panes.observe` (Phase 5), and the text goes in with `herdr agent prompt <id> <text>`; `agent_blocked` or no pane → the message goes to the director with one line saying why. `mesh_runs.py` becomes `detached_runs.py`: a run finds its worker by the `STAFF_ID` in its environment and types its result into the pane. The typed result carries the run id, and a delivery first reads the worker's transcript and types nothing when that line is already there (decision 4). `assign.py` types Stage 2 through `dispatch` with the dispatch id `<dispatch id>-2`, so its line, its `DONE <dispatch id>-2` reply and its rerun follow Phase 6, and the review launcher waits for that line. Usage refusals and settled-without-`DONE` turns go through one classifier in the accounts refusals layer (transcript evidence first, screen text as fallback); the pass status says `refused`, the pass stays open, and the director re-dispatches it after the move.

**Files:** `scripts/message/send.py`, `scripts/direct/mesh_runs.py` → `detached_runs.py`, `scripts/direct/assign.py`, the refusals classifier module (agreed with the accounts unit), their tests.

**Workers:** `2 writers` — `impl`: `send.py`, `detached_runs.py`; `test` (opens as impl): `assign.py`, the classifier, tests.

**State:** none new.

**Constraints from prior phases:** Phase 5 `observe`; Phase 6 `dispatch`, its transcript receipt and `ExitCode`.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; tests: a message during a turn arrives; a blocked or closed worker's message reaches the director; Stage 2 waits for its own line; rerun tests (Invariants → Rerun-safe tools): `test_delivery_rerun_after_type` (a detached run's delivery stopped after it types its result, then run again, types nothing), a delivery run twice types its result once, and a Stage 2 rerun after its line types nothing; full gate green.

### Phase 8 — Shutdown and restore of split tabs (shutdown unit)  · status: todo

#### Work Order

**Goal:** a shutdown stops worker panes inside the bounded deadline, and a restart brings each director tab back with its workers resumed.

**Spec:**

1. **Shutdown.** Before the voluntary window, the shutdown record lists each worker pane from `worker_panes.observe` (Phase 5): staff id, pane, pid and start time, scope, run, and the dispatch id and `launcher_command` when busy. It stores no name: a reader takes the name from the row. `BoundedOwner` holds the worker panes under their director, and success needs a fresh observation that they are gone. A rerun of a stopped shutdown reads the record, stops the panes still alive, and writes no second entry for a pane already listed.
2. **Restore** follows Phase 4's probe (questions list item 1(e)). If herdr's own resume keeps each pane's env, herdr brings the panes back and our code builds no pane. Otherwise one locked tab transaction rebuilds the director (its name from its row, Phase 2), then each worker with `start-or-resume`.
3. **One launcher step ends both branches.** After the panes settle, for each worker the record lists as busy, restore runs its recorded `launcher_command` once with the same dispatch id. `dispatch` reads the transcript first (Phase 6), so the one launcher finishes a pass that ended, waits on a live one, or types the line once when it never went in. A busy worker's resume text names its dispatch id, so the reply that finishes the pass ends with the same `DONE <dispatch id>` line.
4. Resume texts come from one builder, `worker_resume_prompt(cause, was_busy, dispatch_id)`, used by shutdown, moves and launchers. Tab sort sorts a multi-pane tab by its director pane (`herdr.py:902-1300`).

**Files:** `scripts/shutdown/inventory.py`, `stop.py`, `restart.py`, `shutdown_record.py`, `pane_restore.py`, `scripts/herdr/herdr.py` (tab sort), tests.

**Workers:** `2 writers` — `impl`: stop and record; `test` (opens as impl): restart, restore, tab sort.

**State:** shutdown records.

**Constraints from prior phases:** Phase 2 (names from rows; `STAFF_ID` in `KEPT_NAMES`); Phase 4 (the restore probe); Phase 5 (`start_or_resume`, `observe`); Phase 6 (`dispatch` reads the transcript first).

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Shutdown tests with worker panes inside 66 s; restore tests for both probe branches; a restored director takes its row's name.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_shutdown_rerun_after_first_worker_stop` (the rerun stops the rest and the record lists each pane once); `test_restart_rerun_after_director_tab` and `test_restart_rerun_after_first_worker` (one set of panes); `test_restore_rerun_after_agent_resume` (stopped after a busy worker's agent resumes and before its launcher starts: the rerun starts one launcher, which types nothing when the line is in the transcript); a rerun of a finished restart reports done and changes nothing.
- Full gate green.

### Phase 9 — Account moves for pane workers (accounts unit)  · status: todo

#### Work Order

**Goal:** an account move relaunches each worker pane on the new account without a lost or forked conversation.

**Spec:** Each worker move has a move id and one entry in the moves log, keyed by staff id and move id, that names its last finished step. Under the worker's dispatch lock:
1. Wait for or interrupt the turn; close the pane; prove the process gone with a fresh observation.
2. Copy the conversation files into a staging folder in the target home, on the same file system; check each file's size and hash against the source; `fsync` the files and the folder.
3. Publish each checked file with `os.replace` from the staging folder, then `fsync` the target folder. A file is never seen half copied.
4. `start-or-resume` on the new account, check that the id resumes, and record success.
On failure, before it resumes the worker on the source account, the move proves with a fresh observation that no process on the target owns the conversation; then it resumes once on the source and reports. A rerun goes on from the last finished step: a published copy is not copied again, and a target that already runs the conversation is kept, so no source starts beside it. Retire `_retire_workers_servers` and `leftover_server_lines`.

**Files:** `scripts/accounts/accounts.py`, `scripts/whoami/quota_alert.py`, `next_moves.py`, `worker_panes.py` (call only), tests.

**Workers:** `1 writer + 1 tester` — `impl`: accounts code; `test` (opens as impl): tests.

**State:** account homes; moves log.

**Constraints from prior phases:** Phases 5-7.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Move tests: busy, idle, and the failure fallback, which starts no source while a target process owns the conversation.
- Rerun tests (Invariants → Rerun-safe tools), each ending on the new account with one pane and one conversation, as one clean move: `test_move_rerun_mid_copy` (a partial staging copy is never published; the rerun copies again and publishes once); `test_move_rerun_after_publish` (stopped before the target starts); `test_move_rerun_after_target_resume` (stopped before the success record: the rerun keeps the target and starts no source); a rerun of a finished move reports done and changes nothing.
- Full gate green.

### Phase 10 — Astra roles, ask-a-friend, displays and instructions  · status: todo

#### Work Order

**Goal:** Astra roles and ask-a-friend run in panes, every display reads workers through `worker_panes.observe`, and every instruction file describes panes.

**Spec:**
1. **Astra roles.** A held Astra role that a unit director asks for is a worker under its unit, `designer1` or `reviewer1`, started with `start-or-resume`, which reads the `[design_roles.<role>]` row and its sandbox (Phase 5). `design_roles.py` keeps its fences and loses its mesh calls. A worker's parent is always a unit (Phase 1 item 4), so an Astra role that any other caller asks for (a showrunner, an ad hoc session) is a pane beside the caller that is not staff, as ask-a-friend is.
2. **Ask-a-friend is not staff** (Phase 1 item 7). It opens a pane beside the caller through `launch_prepared_agent` with a `SplitPane` and the `[ask_a_friend.consultation]` row, writes no staff row, and closes its pane at `end`. A rerun of an open stopped after the split closes the labeled pane with no agent and opens one; a rerun of `end` finds no pane and reports done.
3. **Displays.** `roster.py`, `production_tree.py`, `codex_winddown.py`, `waste_watch.py`, `hierarchies/*` and `board_json.py` read workers from `worker_panes.observe` (Phase 5), the one join; none of them joins staff rows with `herdr agent list` itself.
4. Instruction edits: Reference → Instruction edits.

**Files:** `scripts/astra/design_roles.py`, `scripts/ask_a_friend/*`, the display modules named above, Reference → Instruction edits.

**Workers:** `2 writers` — `impl`: code; `test` (opens as impl): docs and commands.

**State:** none.

**Constraints from prior phases:** Phases 5-7; Phase 1 `WorkerRole` (`designer`, `reviewer`); Phase 4 `SplitPane`.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Tests for each changed module; `designer1` and `reviewer1` under a unit read their `[design_roles.*]` rows and sandboxes; an Astra role or ask-a-friend asked for by a showrunner opens a pane and makes no staff row; no display module calls `herdr agent list` outside `worker_panes.observe`.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_astra_open_rerun_after_split` (one pane); `test_friend_open_rerun_after_split` (one pane); `test_friend_end_rerun_after_close` (no pane, reports done); each open and close run twice gives one pane and then none.
- No instruction file names a mesh verb outside a mesh-pinned branch.
- Full gate green.

### Phase 11 — Pilot and default switch  · status: todo

#### Work Order

**Goal:** herdr-unit's own next run works on panes, then the default becomes `herdr` and units switch at their run boundaries, Mac units after a Mac probe of Phases 4-5.

**Spec:** Pilot one run; record any defect as a repair before the switch. Then set `WORKER_TRANSPORT=herdr` in `config/direct.conf`. Needs questions list items 1(c) and 1(e) answered.

**Files:** `config/direct.conf`.

**Workers:** `1 writer` — one line; the pilot is the director's.

**State:** `config/direct.conf`.

**Constraints from prior phases:** Phases 2 and 4-10.

**Acceptance gate:** the pilot run's phases finish on panes; Mac probe passes; full gate green.

### Phase 12 — Staff record readers  · status: todo

#### Work Order

**Goal:** every reader of who exists reads the staff record, and the parent-file writers write the row; live state still comes from herdr and session records.

**Spec:**
1. **Readers.** Move each reader in Reference → Staff record readers to `staff.find` and `staff.by_id`. A reader that starts from a production slug or a Units-table name finds its row with `staff_sources.member` (Phase 1 item 5); `by_path` serves only a name a person types. Readers on the Mac get both machines' rows through the one file on natedev.
2. **Parent files move into the showrunner row's `parent`, readers and writers together.** `showrunners.py` `edges` (:415) and `parent_of` (:427) read the row; `attach` (:528), `detach` (:557) and `release_edges` (:582, which wrap calls) write only the row, through Phase 2's `move` and `retire`. After this phase no code writes or reads a parent file, so nothing makes one again; Phase 13 deletes the old files.
3. Pane marks stay as the live link from a Units row to its unit's pane (`member` reads them).

**Files:** Reference → Staff record readers, and `scripts/production/showrunners.py` (`attach`, `detach`, `release_edges`), with their tests.

**Workers:** `2 writers` — `impl`: `scripts/production/`; `test` (opens as impl): `scripts/message/`, `scripts/shutdown/`, other modules.

**State:** none.

**Constraints from prior phases:** Phases 1-2.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; each moved reader's tests green; Phase 2's attach, detach and wrap tests, run with no parent-state folder, pass and make no parent file; full gate green.

### Phase 13 — Cleanup  · status: todo

#### Work Order

**Goal:** the mesh, the review pool, the old worker records and `WORKER_TRANSPORT` are gone, no later than 7 days after the default switch.

**Spec:** Run only after a check that no run folder pins `mesh` and no mesh process is alive. The check cannot go stale: after Phase 11 the default is `herdr` and no tool writes `mesh`, so no new run pins `mesh` and no mesh process starts. Delete: Reference → Cleanup list. Then merge `implement.sh`, `review.sh` and `assign.sh` into one launcher, `scripts/direct/launch.py`, keeping the three command names as thin entry points for their arguments (decision 11).

**Files:** Reference → Cleanup list.

**Workers:** `2 writers` — `impl`: code; `test` (opens as impl): docs and tests.

**State:** deletes state on disk (listed).

**Constraints from prior phases:** all.

**Acceptance gate:** `grep` finds no live `codex_mesh` reference; nothing in Reference → Cleanup list is left; basedpyright 0/0 on changed files and every caller module; full gate green. No rerun test: the cleanup is one commit and a one-time deletion, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

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
| 27, 29 | Roster; displays | `worker_panes.observe`: the staff record joined with `herdr agent list` (Phases 5, 10) |
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
4. Account moves copy conversation files under a lock into a staging folder, check their hashes and publish them with `os.replace` (Phase 9).
5. Screen room: three or more workers in a 1/3 column are short; idle panes close; `herdr pane zoom` shows one.
6. The user and a director type into one pane at once: a message queues; a dispatch needs `idle`.
7. Two transports for days: the run pin and one resolver; cleanup checks no run pins `mesh`.

### Copies this plan retires

| Copy today | The one place after |
| --- | --- |
| Hierarchy joined at each read from timers, parent files, Units tables, marks, worker ledgers, `mesh_roster.json`, run `workers` log, claim files | `staff.py` (Phases 1, 2, 12) |
| A member's shown name: the session record, the saved `-n` in the pane launch record, the tab title | the staff row (Phase 2) |
| The staff-to-pane join in each display | `worker_panes.observe` (Phases 5, 10) |
| Agent states as strings in each caller | `herdr.AgentStatus` (Phase 4) |
| Config path per module; line parsers in Python and bash | `models_registry.py` (Phase 3) |
| herdr launch paths: `codex_mesh start`, `agent_exec.sh` fresh review, `launch_friend.sh`, `herdr.launch_agent` | `herdr.launch_prepared_agent` with a split destination (Phases 4-5) |
| Dispatch logic in three launchers | `worker_panes.py dispatch` (Phase 6) |
| Resume texts (`stop.py:332-346`, `restart.py`, the mesh's) | `worker_resume_prompt(cause, was_busy)` (Phase 8) |
| Usage-refusal matchers (`codex_mesh.QUOTA_REFUSAL`, `refusal_watch.py`, `settle.py`) | one classifier in the refusals layer (Phase 7) |
| Test files that write their own `agents.conf` | the models unit's shared fixture (Phase 3) |

## Review record

Team review, cycle 1, 2026-10-30. Four lenses: simplicity (the lead lens), correctness, rerun safety, API and types. The first team review of the same day (R1-R29) is recorded in this file at commit 07d9e3c9.

Applied:
- Simplicity 3, in part — the shutdown record keeps a worker's staff id and no name (Phase 8).
- Simplicity 4, in part — the hook acts on unit panes only; `rename.py` is the one showrunner rename; launches and restore take the row's name (Phase 2, decision 14).
- Simplicity 5 — the row is id, kind, name and parent; the role comes from the name; `next_name` is cut; `retire` and wrap remove rows (Phases 1, 2, 5; decision 2).
- Simplicity 6 — one graph rule checked under the lock on every write (Phase 1 item 4).
- Simplicity 7, in part — no function takes a path; the write verbs are only the Mac transport (Phase 1 items 1, 9).
- Simplicity 9 — Phases 3 and 13 have no rerun test; they are commits, not tools.
- Simplicity 11 — the new machine module is cut; the owner check uses `remote.other_machine()` (Phase 1 item 1); the copies-table row is gone.
- Correctness 1 — `seed` matches by `STAFF_ID` first, by name only without one (Phase 1 items 5, 8).
- Correctness 2 — each tool finds its row with `member()`, and the tools with a journal keep the id in it (Phase 2 item 1).
- Correctness 4, in part — restore takes the row's name over the saved `-n` (`pane_restore.py:84-86`); a showrunner hand rename changes nothing (Phase 2 items 4, 5).
- Correctness 5 — the Mac reads its own sources and sends one snapshot to natedev (Phase 1 item 8).
- Correctness 6 — the staff step is checked before any change; refusal tests; rerunning the tool completes it (Phase 2 item 2).
- Correctness 7 — wrap removes its unit rows and its showrunner row, and detaches child showrunners (Phase 2 item 3).
- Correctness 8 — the critical path is 1 → 2 → 5; Phases 5, 8 and 11 name Phase 2 in their constraints.
- Correctness 9 — the transcript is the receipt: the typed line carries the dispatch id, and a rerun reads the transcript first (decisions 4, 7; Phase 6).
- Correctness 10 — done means one pane with a live agent named with the id; other panes of the worker are closed (Phase 5).
- Correctness 11 — `WorkerRole` holds `fix`, `designer`, `reviewer`; an Astra role from a unit is a worker, from any other caller a pane that is not staff; ask-a-friend is not staff (Phases 1, 5, 10).
- Correctness 12 — Phase 12 moves the parent-file writers with the readers.
- Rerun 1 — the lock is held from load to replace, with a folder `fsync` and temp-file cleanup (Phase 1 item 2).
- Rerun 2 — the Mac snapshot (Phase 1 item 8).
- Rerun 3 — the dispatch id is in the typed line, the transcript is read first, the prompt file is not changed (Phase 6).
- Rerun 4 — the id is found before the first change; attach and detach set a desired state (Phase 2).
- Rerun 6 — `seed` matches by id; recovery is a rerun of the tool, not `seed` (Phases 1, 2).
- Rerun 7 — the worker lock is held from reconciliation to launch proof; the split labels the pane, so a killed launch is found (Phases 4, 5).
- Rerun 8 — staging copy, hash check, `os.replace` publish, and a target check before the fallback (Phase 9).
- Rerun 9 — one launcher step ends both restore branches (Phase 8).
- Rerun 10 — a parent must exist and be of the right kind, checked under the lock (Phase 1 item 4).
- Rerun 11 — named rerun tests with their stop points in Phases 2, 5, 6, 7, 8, 9 and 10.
- API 1, in part — `WorkerRole` uses the registry's role names, and one function maps a role to its registry row (Phase 5).
- API 3 — `start-or-resume <short name>`; `WorkerName.parse`; `ensure` under the lock (Phases 1, 5).
- API 4 — `rename` and `move` refuse a worker; showrunner retire is defined (Phase 1 item 6).
- API 5 — `herdr.AgentStatus` (Phase 4); `worker_panes.observe` is the one join (Phases 5, 10; decision 10).
- API 6 — typed signatures, `Closed | Kept`, one `ExitCode` table, workers addressed by short name, `_layout` private (Phases 5, 6; decision 10).
- API 7 — `StaffUnreadable`, `StaffRefused`, `StaffNotFound`; the CLI maps them to exit codes (Phase 1 items 5, 6).
- API 8 — `destination: NewTab | SplitPane` (Phase 4).
- API 9 — no public load, no path parameters, no `find(name=)`, no retired flag (Phase 1 item 5).

Dropped:
- Simplicity 1 (one cutover, no `seed`) — moving every reader and writer in one phase is too large for one dispatch; id-first `seed`, the Mac snapshot and Phase 2 before any pane close the trailing-record risk.
- Simplicity 2 (no `WORKER_TRANSPORT`) — units end their runs at different times, so a drain stops every unit at once; the pin lets each unit switch at its own run boundary, and Phase 13 deletes it.
- Simplicity 3, the rest (remove the `SHOWRUNNER_*` marks) — `member()` uses them to go from a Units row to the pane launch record that holds the `STAFF_ID`.
- Simplicity 4, the rest (write the session record back on a refusal) — that record is Claude Code's file; the next launch takes the row's name.
- Simplicity 7, the rest (delete `STAFF_FILE`) — with no path parameters it is the one test injection point, and the subprocess and fake-ssh tests need it.
- Simplicity 8 (`launch.py` in Phase 6) — the launchers keep their mesh branch until Phase 13, so an early merge ports code that Phase 13 deletes; dispatch is in one place from Phase 6.
- Simplicity 10 and API 10 (the overview) — the overview is another file, outside this edit; herdr-unit updates it to this plan.
- Correctness 3 (`restore()` of a retired row) — a retired row is removed (Simplicity 5), and retire ends the unit's workers, so a resumed unit is a new member and is owed no worker conversation.
- Correctness 4, the rest (a hand rename of a worker) — a worker's name is set once (decision 13), and its session name is its id.
- Correctness 9, the rest (the pass recorder as the journal) — the transcript already holds the submission; a second record would be a copy of it.
- API 2 (three row classes) — one four-field row with the graph rule checked under the lock gives the same guarantee with one type.
- Rerun 2, the rest (natedev pulls the Mac snapshot) — the Mac sends it through the one ssh write path, with the same rows.
- Rerun 5 (a cleanup fence) — after Phase 11 nothing pins `mesh`, so the check cannot go stale; cleanup is not a shipped tool.
- Rerun 6, the rest (`seed` refuses after a cutover marker) — id-first matching means a later `seed` cannot undo a rename.

Refuted: none. Questions for the user: none.

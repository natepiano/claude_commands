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
- **Project started:** 2026-10-30T06:38:57.873+00:00
- **Stack:** Python 3.11+ through the `scripts/lib/py` shim, bash, `unittest`, basedpyright; herdr 0.9.5 CLI; the interactive Claude Code and Codex CLIs.
- **Layout:**
  - `scripts/production/` — production tools: `showrunners.py`, `production_units.py`, `production_lifecycle.py`, `live_units.py`, `unit_lookup.py`, `add_unit.py`, `add_showrunner.py`, `retire_unit.py`, `rename_unit.py`, `transfer_unit.py`, `rename.py`, `promote_*.py`, `resume_unit.py`, `production_tree.py`, `broadcast.py`, `codex_winddown.py`; the new `staff.py` and `staff_sources.py`
  - `scripts/herdr/` — `herdr.py` (the one herdr client), the new `worker_panes.py`
  - `scripts/direct/` — the launchers `implement.sh`, `review.sh`, `assign.sh`/`assign.py`, `workers.py`, `worker_state.py`, `remove_workers.py`, `board.sh`, `progress_history.py`
  - `scripts/agents/` — `codex_mesh.py`, `mesh_runs.py`, `agents_config.sh`, `agent_admin.sh`, `agent_exec.sh`, `sync_codex_catalog.sh`
  - `scripts/message/` — `send.py`, `roster.py`, `sessions.py`
  - `scripts/shutdown/` — `inventory.py`, `stop.py`, `restart.py`, `shutdown_record.py`, `pane_record.py`, `pane_restore.py`, `remote.py`
  - `scripts/lib/` — `agents_registry.py`, `unit_runs.py`, `names.py`, `atomic_file.py`
  - `config/` — `agents.conf` (renamed `models.conf` in Phase 5), `direct.conf`, `names.toml`
  - `commands/`, `docs/direct/`, `docs/as-built/`; `skills/herdr/SKILL.md` is git-ignored and built from the NixOS file
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
  - `scripts/lib/atomic_file.py` — the one file writer: `write_text_atomic(path, text)` and `sync_folder(folder)`; `write_bytes_atomic` comes in Phase 11; the unit-board unit's file
  - `scripts/production/staff.py` — the staff record: row, rules, read and write API, CLI (new in Phase 1; `apply_seed` in Phase 2)
  - `scripts/production/staff_sources.py` — `member` and `seed` over today's sources (new in Phase 2; `seed` stops setting a showrunner parent in Phase 16)
  - `scripts/herdr/worker_panes.py` — `start_or_resume`, `close`, `observe` (new in Phase 7), `dispatch` (Phase 8)
  - `scripts/herdr/side_pane.py` — the pane beside a caller that is not staff, for ask-a-friend and Astra (new in Phase 12)
  - `scripts/agents/detached_runs.py` — detached runs that type their result into the worker pane (renamed from `mesh_runs.py` in Phase 9)
  - `scripts/direct/launch.py` — the one launcher behind `implement.sh`, `review.sh` and `assign.sh` (new in Phase 19)
  - `scripts/lib/unit_runs.py` — run folders; `worker_transport(run)` and the run pin (added in Phase 8; they and the run `workers` log reader go in Phase 18)
  - `config/direct.conf` — `WORKER_TRANSPORT` (`mesh` in Phase 8, `herdr` in Phase 15, deleted in Phase 18)
  - `commands/unit/direct.md` — the unit director instructions (transport-aware text in Phase 8, pane text in Phase 14)
  - `scripts/shutdown/stop.py` — shutdown stop; `worker_resume_prompt` (Phase 10), mesh pause removed (Phase 18); the shutdown unit's file
  - `scripts/accounts/refusal_watch.py` — `transcript_refusal` (:464), the one usage-refusal classifier (Codex transcripts and screen text in Phase 9); the accounts unit's file
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
  - **One file writer.** Every file write uses `scripts/lib/atomic_file.py`. `write_text_atomic(path, text)` writes a temp file beside the target, syncs it, replaces the target with `os.replace` and syncs the folder; `sync_folder(folder)` does the folder step alone. Its temp files are `.<name>.<random>`, with no `.tmp` suffix, so a cleanup of a killed writer's temp files uses the glob `.<name>.*`. No phase makes its own temp-file, `fsync` and replace steps. A binary writer goes into `atomic_file.py` (Phase 11), not into a second module. The unit-board unit owns `atomic_file.py`; a phase that changes it agrees the change with that unit first.
  - **Owners.** The models unit owns `agents_registry.py` and the registry writers; the shutdown unit owns `scripts/shutdown/`; the accounts unit owns `scripts/accounts/` and account moves. A phase that changes their files names the owner in its checkpoint notice and agrees the change with them first. The user owns `/etc/nixos` (questions list item 1).

## Decisions made in this plan

Each item is my call under the decision criteria unless it names the user. A one-line plan change reverses it.

1. **One staff record (user, 2026-10-30).** `scripts/production/staff.py` and one file, `~/.local/state/staff/staff.json` on natedev, hold every showrunner, unit director and worker: a stable id, a short name, the kind, and the parent row's id. So the record maps each short name to an id and holds the relationships (worker → unit → showrunner); production and unit come from the parent chain. A rename changes only the name and a transfer only the parent; the id never changes. There is no separate worker ledger: the unit worker ledger, `mesh_roster.json`, the run `workers` log and the slot claim files retire into this record. Live state (pane, socket, running or not) is never stored; it comes from herdr and the session records at read time. No such record exists today: the hierarchy is joined at each read from update timers, parent files, Units tables, pane marks and the worker ledgers (Reference → Staff record readers).
2. **One command starts or resumes a worker (user, 2026-10-30).** `worker_panes.py start-or-resume <short name>`. The role is the part of the name before the number (Phase 1 As-built), so the command takes no role argument. A worker's herdr agent name and its Claude or Codex session name are its staff id. Both Claude Code and Codex resume a conversation by its name (user, 2026-10-30), so the command resumes by id and keeps no conversation id. At every start or resume it looks up kind, model, effort, service tier and sandbox for that role in the model registry, and only then (user, 2026-10-30, option A): a `/model` typed by hand holds, no dispatch compares the model, and a config change does not restart a running worker. This replaces 710a6adb's per-turn lookup. The director picks the number: `impl1`, then `impl2` for a second implementer. The name is set once, when the row is made.
3. **Idle panes close at phase end (user, 2026-10-30).** The conversation stays. To bring a worker back, the director calls `start-or-resume` again. This settles the keep-open question.
4. **The worker's final reply signals completion, and the transcript is the receipt; herdr's state only wakes the launcher.** Every dispatch has a dispatch id, and the one line the launcher types carries it (decision 7). The worker's own transcript then holds the proof of each step: the typed line with the id proves the submission, and a final reply that ends with `DONE <dispatch id>` proves completion. The launcher writes the status file only from that reply. A rerun reads the transcript before it types anything, so it never types a pass twice (Phase 8). No other record of a submission exists, and the prompt file is never changed. The launcher writes the status file today (`implement.sh:629`); the worker writes only its summary. This works the same for read-only reviewers, which cannot write a file (`review.sh:689` starts them with `--sandbox read-only`): the launcher writes their findings file from the reply, as the mesh does today.
5. **The launcher's whole wait is one background `herdr agent prompt <id> "<line>" --wait`, with no `--timeout` and no `--until`.** herdr then waits for its default settled set, `idle`, `done` or `blocked`, after it sees the turn start (herdr skill, "For normal agent work, `--wait` is enough"). `blocked` wakes the launcher, so a worker at an approval dialog never stalls the director. The director runs the launcher in the background, as today, and the task notification is the wake-up.
6. **A dispatch refuses a busy worker; a message queues.** A dispatch is a new pass that a launcher waits on, so it needs an `idle` or `done` worker, under the worker's dispatch lock. A message (`send.py`, a detached-run result, review Stage 2) is typed at once, also during a turn: Codex and Claude queue typed input. A blocked or closed worker's message goes to its director. `pending.json` drops.
7. **A prompt goes in by file path, not as pasted text.** The launcher types one line: `Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>.`
8. **No launcher compaction, and no "Continue where you stopped." retry.** Interactive Codex and Claude compact by themselves.
9. **Codex stays a worker kind.** herdr starts `--kind codex` and `--kind claude` agents. The default worker kind per role stays where it is set today.
10. **`herdr.py` stays the one herdr client.** Launch into a split pane, layout changes and the typed agent state `AgentStatus` are new `herdr.py` primitives (Phase 6). `worker_panes.py` composes them and holds only `start_or_resume`, `dispatch`, `close` and `observe`, the one join of worker rows and herdr panes that every display reads (Phases 7, 13); its `_layout` is a private helper. One-line agent calls (`herdr agent send-keys <id> esc` to interrupt, `herdr agent read`, a message's `herdr agent prompt`) run directly and are not wrapped.
11. **The three launcher commands stay; their shared work moves to one place.** `implement.sh`, `review.sh` and `assign.sh` keep their arguments and their recorder, status-file and board work. Start, dispatch, wait and receipt live once in `worker_panes.py dispatch`. Phase 19 merges the launchers when they are thin.
12. **The transport switch lives in `config/direct.conf`, with an end date.** `WORKER_TRANSPORT` is migration state, not a model default, so it stays out of `models.conf`. The cleanup phase deletes it, no later than 7 days after the default switch.
13. **The right pane shows the worker's short name (user, 2026-10-30).** For example `impl1`, with no production or unit name.
14. **The staff row is the one name (sdlc showrunner, 2026-10-30).** A unit's name changes only through `/showrunner:rename_unit` (sdlc, changed 2026-10-30). The hook cannot stop Claude Code's `/rename`, so on a mismatch it keeps the tab on the row's name and prints one line with the rename_unit command. Every launch and every restore of a member takes its name from its row (Phase 4). A showrunner's name is also its workspace's, which `Herdr.retitle` does not rename, so `/showrunner:rename` (`rename.py`) is the one way to rename a showrunner. A worker's name is set once (decision 13).
15. **One way to find a row: `staff_sources.member` (herdr-unit, 2026-10-30; showrunner, 2026-10-30).** `staff_sources.py` imports `showrunners.py` (Phase 2 As-built), so `showrunners.py` never imports `staff_sources.py`, and no lazy import or copy of `member` goes around that rule. `staff.py` imports none of today's sources (Phase 1 As-built). Row names are workspace and tab names, not slugs: seed keeps slug `sdlc` as row `tools` (`scripts/production/test_staff_sources.py:28`), so no code finds a showrunner or unit row with `staff.by_path(<slug>)`. Code that needs a member's row calls `staff_sources.member(selector, staff.snapshot())`; a `showrunners.py` function that works on rows takes the row or the `StaffSnapshot` from its caller and never looks a member up itself. Tests use a slug that differs from its workspace name. One exception (showrunner, 2026-10-30): `showrunners.py main`'s attach and detach branch imports `showrunner_parent` inside that branch, so users keep one command; the module has no load-time import of it. The showrunner tree readers live in `showrunner_tree.py`, which imports `staff`, `staff_sources` and `showrunners` (sdlc, 2026-10-30; Phase 3c item 5); `showrunners.py main` imports it inside the branch that needs it, under the same exception.
16. **The row stays four fields; a reader keeps its timer fields (herdr-unit, 2026-10-30).** The row is id, kind, short name and parent: the user's design, a map from short name to id and who reports to whom (Phase 1 As-built). It gains no session, socket, zone, doc or Units-table field. A reader moves to the record only for who exists and who reports to whom. It keeps reading timer fields (session, socket, zone, doc) from the update timer, keyed by the row: a showrunner row's name is its slug (decision 15), so the `registered_showrunners()` entry of that slug, or `showrunners.named(<slug>)`, gives them. A unit keeps its Units-table cells (Worktree, Plan) in the doc and its pane and running state in herdr, keyed by its row the same way. A row with no timer on this machine has no timer fields here, and the reader shows it without them. A reader that needs nothing from the record is out of the lists of Phases 16 and 17 and does not change (Reference → Staff record readers → Not moved): a source that `seed` reads; a reader of only timer fields, live state or Units-table cells of a member it already knows; and a reader that reaches the tree only through `showrunners.py` functions, which Phase 16 moves to the row. The worker and mesh sources move with the worker readers or go at cleanup.

## Questions for the user

1. **The NixOS edits (settled 2026-10-30: natedev makes them and tells the user when to rebuild; herdr-unit tells natedev when each is due; no diff files for the user).** (a) `modules/common/claude-git-filters.nix:35` `watchPaths`: `agents.conf` → `models.conf`. It is functional: until it changes, the git filter does not refresh on a `models.conf` change. (b) Comments in `modules/common/codex-catalog-sync.nix`, `modules/common/codex.nix:47`, `modules/common/shell.nix:139` (`/agent`) and `README.md:410`. (c) `modules/common/herdr-skill-local.md:7`: the rule "start a new agent in a new tab, not a split pane" must permit worker panes in the director's tab (`skills/herdr/SKILL.md:221` is built from it). (d) The sign-in path unit (natedev) and the launchd agent (Mac) that run `codex_mesh.py signin-changed`: they retire at cleanup, or point at the pane restart if Phase 6's Codex sign-in probe is negative. (e) Only if Phase 6's probe shows that herdr's own resume does not keep each pane's `--env` (its account): add `[session] resume_agents_on_restore = false` to `~/.config/herdr/config.toml` on natedev and the Mac, so our restore owns split tabs; if the probe is positive, herdr restores split tabs and our per-pane restore code retires for them. Options: you write them; or herdr-unit writes one diff file per edit for you to apply, as the builds unit did with `build-followups-memory-nixos-*.diff`. Recommendation: diff files; apply (a) when the rename phase lands, (c) and (e) before the default switch, the rest at cleanup.

## Phases

**Critical path.** Phase 5 waits on the models unit, and no other phase needs it: pane work reads model and effort through the registry API under either file name. The director runs Phases 6-19 when Phase 5 is still blocked, and runs Phase 5 when the models unit lands. The critical path is Phase 1 → 2 → 3 → 4 → 7 → 8 → 9 → 10 → 11 → 14 → 15 → 18 → 19: no worker pane opens before the writers keep the unit rows current (Phase 3) and every director pane carries its `STAFF_ID` (Phase 4). Phase 6 runs beside Phases 1-4 and lands before Phase 7. Phases 12 and 13 run beside Phases 10 and 11. Phases 16 and 17 run beside the path after Phases 3 and 4. Phase 15 also needs questions items 1(c) and 1(e).

### Phase 1 — The staff record  · status: done

**Depends on:** none

#### As-built

- `scripts/production/staff.py` holds the staff record of both machines in one file on natedev: `~/.local/state/staff/staff.json`, with the lock `staff.json.lock`. `STAFF_FILE` sets the path for tests; no function takes a path. The form is `{"schema": 1, "rows": [...]}`, rows sorted by `id`, written with `json.dumps(..., indent=2, sort_keys=True)` and a final newline, so the same content always gives the same bytes. The module imports `scripts.shutdown.remote` and `scripts.lib.atomic_file` after one repo-root `sys.path` insert, and no source module.
- Types: `StaffRow(id, kind, name, parent)` is a frozen dataclass with `to_json()` and `from_json()`; `StaffId = NewType("StaffId", str)`; `Kind = Literal["showrunner", "unit", "worker"]`; `WorkerRole` is a `StrEnum` (`impl`, `test`, `fix`, `review`, `designer`, `reviewer`); `WorkerName.parse(name)` takes a role and a positive number (`impl1`) and refuses every other form. An id is `"s" + uuid4().hex[:11]`: it matches herdr's agent-name rule `^[a-z][a-z0-9_-]{0,31}$`, never changes and is never used again. The row stores no role, unit or showrunner; the worker name and the parent chain give them. A retired member has no row.
- Read API: `by_id(staff_id) -> StaffRow | None`, `by_path(names) -> StaffRow | None` (`"sdlc"`, `"sdlc/models"`, `"sdlc/models/impl1"`) and `find(*, kind=None, parent=None) -> list[StaffRow]`. A path walks showrunner, unit, worker; a showrunner's path is its own name, also under another showrunner. Reads take no lock. A missing file, a file that does not parse, another `schema` or a file that breaks a rule raises `StaffUnreadable`, a subclass of `OSError`. No write replaces a bad file; the first write makes a missing one.
- Write API: `ensure(kind, *, name, parent=None) -> StaffRow`, `rename(staff_id, new_name) -> StaffRow`, `move(staff_id, new_parent) -> StaffRow` and `retire(staff_id) -> StaffRow | None`. Each raises `StaffRefused`, `StaffNotFound(staff_id)` or `StaffUnreadable`. A rerun returns the same row and leaves the same bytes. `rename` and `move` refuse a worker; a worker name is set once. Each write is one transaction under `fcntl.flock` on `<file>.lock`: it removes stale temp files, loads the file, applies the edit, checks the rules on all rows, then writes through `atomic_file.write_text_atomic`. The rules: a worker's parent is a unit, a unit's parent is a showrunner, a showrunner's parent is `None` or a showrunner, and no chain loops; a unit or worker name is unique per kind and parent, and a showrunner name is unique among showrunners; a short name is not empty, does not have the id form, and has no `/`, no control character and no leading or trailing space.
- Mac: `OWNER_MACHINE = "natedev"`. This machine is the owner when `remote.other_machine()` is not `OWNER_MACHINE`. On the Mac each call is one ssh call (`_remote`: `ssh -o BatchMode=yes -o ConnectTimeout=10 natedev ~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py <verb> ... --json`, 20 s timeout), and the Mac keeps no copy. Every Mac read takes one `show --json` snapshot and applies the natedev filters. Each name goes as one `--name=<value>` word, and `move` sends `--parent=<id|none>`. Exit 255, or a failure with no reason on stderr, raises `StaffUnreadable("natedev not reached")` and writes nothing.
- CLI: `staff.py show [--kind K] [--under <id or path>] [--json]` prints one aligned row (id, kind, path) per member, sorted by path. `staff.py get <id or path> [--json]` prints `id kind name`. The write verbs `ensure|rename|move|retire --json` are the Mac transport only; their help is hidden. A write that changes nothing prints `unchanged` on stderr. Exit 0 done, 1 not found, 2 refused or unreadable, with the reason on stderr; on exit 2 the reason starts with `StaffRefused: ` or `StaffUnreadable: `.

**Files:**
- `scripts/production/staff.py` — the row types, the rules, the read and write API, `OWNER_MACHINE`, the exceptions and the CLI.
- `scripts/production/test_staff.py` — its tests; `StaffTests.setUp` points `STAFF_FILE` at a temp file and puts a fake `ssh` on `PATH`.
- `scripts/lib/atomic_file.py` — the one text writer (owner: unit-board unit). `write_text_atomic(path, text)` writes a temp file `.<name>.<random>` beside the target, runs `fsync`, gives it the target's mode (or `0o666` minus the umask), runs `os.replace`, then syncs the folder. `sync_folder(folder)` does the folder sync alone. `scripts/lib/durable.py` is gone.
- `scripts/lib/test_atomic_file.py` — the writer tests.

**Binds later work:**
- Every later file write uses `atomic_file.write_text_atomic`; no module adds a second writer.
- A reader of the record finds a member with `by_path` and the kind walk; a nested showrunner has its own name as its path.
- When natedev is not reached, a Mac call raises `StaffUnreadable`. A consumer shows it as a problem line and never reads it as an empty list.
- Unit retire also removes the unit's worker rows, so the **Staff record writers** tools keep a unit row that still has worker rows.

**Gotchas:**
- `retire(<unit id>)` removes the unit row and all its worker rows in one write; a caller checks for worker rows first. `retire` of a showrunner with a unit under it raises `StaffRefused`; its child showrunners move to `None`.
- `atomic_file` temp files have no `.tmp` suffix, so the stale-temp glob is `.<name>.*`. Before it reads, `_transaction` deletes every `.staff.json.*` file under the write lock.
- A write that changes nothing still syncs the folder, because an earlier `os.replace` can succeed before its folder sync fails.
- `by_id` and `by_path` return `StaffRow | None`; each consumer converts `None` into its own type at its call.
- An `Assertion-Change:` line names `<path>::<function>` with no class part, or the checkpoint hook does not match it.

**Ruled out:** a second writer module (`scripts/lib/durable.py` was removed); a copy of the record on the Mac; Mac reads that send `get` or filters over ssh; a positional destination for `move`.

### Phase 2 — Staff record sources and seed  · status: done

**Depends on:** Phase 1

#### As-built

- `scripts/production/staff_sources.py` holds `member` and `seed`. It imports `staff.py` and today's sources (`showrunners.py`, `production_units.py`, `unit_names.py`, `pane_record.py`); none of them imports it (decision 15).
- `member(selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember`, with `MemberSelector = ShowrunnerMember | UnitMember`, `ShowrunnerMember(slug)`, `UnitMember(slug, unit)` (`unit` is the Units table's `Unit` cell), `MemberFound(row)` and `NoSuchMember(selector)`. It reads rows only from `record`, and no `StaffRow | None` leaves the module. Each call reads this machine's pane launch record with `pane_record.read_strict(herdr.shared_session())` and the timers with `showrunners.read_sources(unreadable=UnreadableTimerPolicy.SKIP)`; a pane record that cannot be read or is malformed raises `StaffUnreadable` that names the file.
- `member` matches by id first. It takes the newest launch (by `written_at`) whose `session:<session_id>` is the showrunner timer's target, or whose environment carries `SHOWRUNNER_UNIT=<slug>` and `SHOWRUNNER_UNIT_ID=<unit>`. When that launch has a `STAFF_ID`, only the row of that id and kind matches, else `NoSuchMember`; a name is not tried. Without a `STAFF_ID` it matches by name: the showrunner row `showrunners.space_from_text(slug, <doc text>)` (the doc's `Showrunner session:` line, else the slug), or the unit row at `<showrunner row name>/<unit_names.name(unit)>`. A showrunner slug with no readable timer gives `NoSuchMember`, and its units match only by id.
- `staff_sources.py seed [--dry-run]` holds `staff.operation()` from its first source read until `apply_seed` returns. It takes one `staff.snapshot()` (`StaffAbsent` reads as an empty record; any other unreadable record exits 2), reads the pane record and the timers once, and builds one `staff.SeedInput`. `staff.apply_seed` applies it in one write under the write lock; on the Mac the input goes to natedev on stdin through `staff.py apply-seed --json`. It prints `add|keep|move|retire <id or key> <kind> <name>` per member. `--dry-run` writes nothing. A run that changes nothing prints `unchanged` on stderr. Exit codes come from `staff.cli_result`, the one mapping: 0 done, 1 not found, 2 refused or unreadable.
- Seed showrunners: one per readable timer; a matched row keeps its name. The parent comes from `showrunners.stored_parent(slug)` through `showrunners.parent_edges`: a parent whose timer is not on this machine keeps the current edge (`KeepSeedParent`), and no edge sets `None` (`RootSeedParent`). Seed units: each Units row that is not retired and whose `Worktree` folder exists on this machine; a matched row keeps its name and moves under the showrunner row. A matched unit whose folder is not on this machine keeps its row unchanged.
- Seed retires a unit row only on positive evidence: `unit_is_retired` with a local `Worktree` folder or a retired `Plan` cell, or the doc of a showrunner whose every active Units row matched a row no longer lists it. An absent `Worktree` folder is not evidence. A unit row with worker rows stays and prints `keep <id> unit <name>`. `apply_seed` checks each `SeedRetirement(staff_id, parent)` again under the write lock and keeps a row whose parent changed or that has worker rows. Seed never retires a showrunner row and never adds a worker row: mesh workers stay in the unit worker ledger. A production whose doc, Units table or parent file cannot be read changes none of its rows and prints one line, `seed: <slug> (<doc>): <error>`; a timer that cannot be read is skipped with one line.
- `staff.py` adds `StaffSnapshot(rows)` with `by_id(staff_id) -> StaffRow | None`, `by_path(names) -> StaffRow | None` and `find(*, kind=None, parent=None) -> list[StaffRow]`, and `snapshot() -> StaffSnapshot`: one read of the file, on the Mac one `show --json` call. The module `by_id`, `by_path` and `find` call `snapshot()` once each, with one copy of each lookup rule. Seed types: `SeedMember(key, kind, name, match: StaffId | NewSeedMember, parent: SeedParent | KeepSeedParent | RootSeedParent)`, `SeedRetirement(staff_id, parent)` and `SeedInput(members, retire)` with `to_json` and `from_json`; `apply_seed(seed_input) -> StaffSnapshot`. `StaffAbsent(StaffUnreadable)` means the record file is missing; `_remote` maps the `StaffAbsent: ` prefix. Non-JSON `get` prints the row as `show` does, with the path from `_paths`.
- `staff.operation() -> AbstractContextManager[None]` holds `fcntl.flock` on `operation.lock` beside the staff file on this machine (`~/.local/state/staff/operation.lock`). It raises `StaffUnreadable` only for a lock error; an error from the body passes through unchanged. On natedev each CLI write verb (`ensure`, `rename`, `move`, `retire`, `apply-seed`) runs inside `operation()`, so a Mac write waits for a natedev operation. The module write functions do not take it. Code takes the operation lock first and never inside the write lock.
- `showrunners.py`: `ShowrunnerSource(timer, document)`; `_source(instance)` is the one per-timer read (conf and doc). `read_sources(state_dir=None, *, unreadable: UnreadableTimerPolicy)` takes `UnreadableTimerPolicy.RAISE` or `SKIP`. `registered_showrunners` passes `RAISE`, so a timer conf that cannot be read stops registry discovery and `refuse_running_children` fails closed. `stored_parent(child)` gives `""` with no parent file and raises on one that cannot be read; `parent_edges(parents)` applies the tree's local-endpoint and cycle rules to read evidence; `space_from_text(slug, text)` is `herdr_space` on text already read.
- `pane_record.py`: `read_strict(session)` checks each entry (`session_id` and `written_at` strings, a string `environment`) and raises `ValueError` that names the path and pane; only an absent file reads as no entries. `read` keeps its lenient results.
- Nothing reads the record yet; `seed` catches its rows up with the sources.

**Files:**
- `scripts/production/staff_sources.py` — the selectors and results, `member`, `seed` and its CLI.
- `scripts/production/staff.py` — the snapshot, the operation lock, `StaffAbsent`, the seed types, `apply_seed` and its `apply-seed` verb, `cli_result`, and the path in `get`.
- `scripts/production/showrunners.py` — the timer and doc reads above.
- `scripts/shutdown/pane_record.py` — `read_strict`.
- `scripts/production/staff_test_support.py` — `StaffTestCase`, the one staff test fixture: a temp `STAFF_FILE`, the fake `ssh` (`FAKE_SSH`) with its call log, the switch between owner and Mac (a `remote.other_machine` patch) with a separate owner state folder, and source fixture builders. `grep -l FAKE_SSH scripts/production` lists only this file.
- `scripts/production/test_staff.py`, `scripts/production/test_staff_sources.py`, `scripts/production/test_showrunners.py` — the tests.

**Binds later work:**
- Code finds a showrunner or unit row only with `staff_sources.member(selector, staff.snapshot())`. `showrunners.py` never imports `staff_sources`; a `showrunners.py` function that works on rows takes the row or the `StaffSnapshot` from its caller.
- Staff evidence reads pane records with `pane_record.read_strict`, never the lenient `read`; the **Shutdown and restore of split tabs** edits to `pane_record.py` keep its per-entry checks.
- Each `read_sources` caller picks `UnreadableTimerPolicy` on purpose: lifecycle checks keep `RAISE`; seed and `member` use `SKIP`.

**Gotchas:**
- Row names are workspace and tab names, not slugs: slug `sdlc` is row `tools`, `performance` is `fps`, `tool-based-ui` is `hana`. Tests use a slug that differs from its workspace name.
- `operation()` does not nest: a second `operation()` in the same process or in a child process opens the lock file again and waits on the first holder.
- Each `member` call reads the pane record and every timer and doc again; seed reads them once through `_MemberSources`.
- `showrunners._source` is the one per-timer read; `test_unit_lookup.py` counts timer reads there.
- Seed reads only this machine's timers. The Mac has no showrunner timers today, so a Mac seed finds no production, and a unit whose `Worktree` folder exists only on the Mac gets no row from seed.

**Ruled out:** `staff.by_path(<slug>)` to find a member's row — `staff_sources.member` is the one lookup.

### Phase 3 — Staff record writers  · status: done

**Depends on:** Phase 1, Phase 2

#### Work Order

**Goal:** every tool that adds, renames, moves or retires a showrunner or unit writes the staff record as one named step of its own work, so the record stays current without `seed`; and a rerun after a stop at any step ends as one clean run.

**Spec:**

1. **Find the row by id.** Before its first change, each tool finds its member's row with `staff_sources.member(<selector>, staff.snapshot())` (Phase 2 As-built), where the selector is `ShowrunnerMember(slug)` or `UnitMember(slug, unit)`. `member` matches by the `STAFF_ID` in the pane launch record first; until Phase 4 sets `STAFF_ID` in member panes, it matches by name. A tool that finds more than one member takes one `staff.snapshot()` for all of them. On `NoSuchMember`: add and resume call `ensure` (item 3); retire, wrap and promote skip that `retire` and go on; rename and transfer print the reason and exit 2 before any change. A tool with a journal (`rename.py`, `rename_unit.py`, `transfer_unit.py`) writes that id into its journal at its first step, and a rerun reads the id from the journal, because a stop partway can leave the old name and marks changed. No tool finds a row by a name or a path (decision 15). `showrunners.py` has no load-time import of `staff_sources`, so its functions never look a row up: `showrunner_parent.py` (item 3) finds the rows for attach and detach.
2. **The staff step, checked before any change.**
   - An add, a rename or a move is written after the tool's own checks and before its first change. The write checks Phase 1's rules under the lock (item 4) and raises `StaffRefused` before anything else changed, so a refused rename, transfer or attach (a name taken, a parent of the wrong kind, a loop) leaves no source changed. The tool prints the reason and exits 2.
   - A removal is written after the tool's last change that needs the row, because until then the tool finds the unit's workers through their rows. A unit's `retire` is never refused (Phase 1 As-built).
   - `StaffUnreadable` (natedev not reached from the Mac) stops the tool at its staff step with the reason; no step after it ran.
   - A rerun repeats the staff step, which returns the row unchanged when the change already holds (Phase 1 As-built), and goes on. Rerunning the same tool completes it; `seed` is not the recovery path.
   - **The operation lock.** Each tool holds `staff.operation()` (Phase 2 As-built) from its staff step to the source update that matches it: the doc's Units row, the update timer or the parent file, in the order the tool does them. So a `seed` never runs between the two: it waits, then reads the updated source. The tool takes the operation lock before any staff write, never inside the write lock. On the Mac the tool holds the Mac's lock, and each staff write it sends over ssh takes natedev's lock on natedev (Phase 2 As-built).
   - **A unit row with worker rows stays.** `retire_unit.py`, promote and wrap never retire a unit row that has worker rows. At the staff step the tool reads them with `staff.find(kind="worker", parent=<unit id>)`. When there are any, it keeps the unit row and its worker rows, prints one line that names each worker row, does its other steps, and exits 1. Phase 10 stops those workers and retires their rows.
3. **The writers.**
   - Add and resume → `ensure`: `add_showrunner.py` the showrunner row, named `showrunners.herdr_space(slug, doc)`; `add_unit.py` and `resume_unit.py` (through `add_unit.py --resume`) the unit row under the showrunner's row, named `unit_names.name(<Unit cell>)`. A unit whose row `retire` removed gets a new row and a new id when it resumes, as a new unit does (Phase 1 As-built).
   - Rename → `rename(id, new_name)`: `rename.py` the showrunner row, `rename_unit.py` the unit row; the workspace and tab titles change as today.
   - Transfer → `move(id, new_parent)`: `transfer_unit.py` moves the unit row.
   - Attach and detach → `move` of the showrunner row (showrunner, 2026-10-30). The command stays `showrunners.py attach <child> [parent]` and `detach <child>`, with the same arguments, output and notices; no caller or doc learns a second name. The code lives in the new module `scripts/production/showrunner_parent.py`: it holds `staff.operation()`, finds the child and parent rows with `member` from one snapshot, `move`s the child row (to the parent row, or to `None`), then calls `showrunners.attach(child, parent)` (:588) or `showrunners.detach(child)` (:614), which change only the parent file, then tells the two productions as `main` does today (`tell_others`). `showrunners.py main` (:669) imports `showrunner_parent` inside its attach and detach branch only and calls it; `showrunners.py` has no load-time import of it, so there is no cycle. Run as a script, `showrunners.py` is `__main__`, so `showrunner_parent` gets the `showrunners` module by its own import: the branch hands over the parsed arguments and nothing else, and no state of `__main__` is used after the call. Both verbs set a desired state: an attach whose parent file already names that parent, or a detach with no parent file, goes on to the staff step and reports done when the row already matches. Neither refuses its own finished work.
   - Retire → `retire_unit.py` calls `retire(<unit id>)` after its last change, for a unit row with no worker rows (item 2).
   - Wrap → `production_lifecycle.py` `wrap` (:427). After the unit loop (:477-490) and before the update timer goes (:491-494; `member(ShowrunnerMember(slug), …)` finds the showrunner through its timer), wrap does these steps in this order:
     1. **The release capture.** `showrunners.release_edges` (:588) splits in two: `edges_to_release(slug)` reads the edges, and `release_edges(slug, released)` removes the parent files that `released` names. `WrapRelease` replaces `Released` (:573):
        ```python
        # showrunners.py
        @dataclass(frozen=True)
        class ReportedTo:
            parent: str                       # the slug it reported to

        @dataclass(frozen=True)
        class Root: ...

        @dataclass(frozen=True)
        class WrapRelease:
            former_parent: ReportedTo | Root
            former_children: tuple[str, ...]  # the slugs that reported to it
            told: tuple[str, ...]             # the slugs told so far

        def edges_to_release(slug: str) -> WrapRelease   # told is empty
        def release_edges(slug: str, released: WrapRelease) -> None
        ```
        Wrap writes the capture to `~/.local/state/showrunner/<slug>/wrap_release.json` with `atomic_file.write_text_atomic`, before any `staff.retire` and before the timer goes (:493). A rerun that finds the capture reads it and does not call `edges_to_release` again. `former_children` leaves out the style children that `detach_style_children` (:385) detaches and tells, as today's order (:495-496) does.
     2. **The rows.** `retire` for each unit row that the loop retired, then `retire` for the showrunner row, which moves its child showrunners to `None` (Phase 1 As-built).
     3. **The release.** After the timer goes, wrap's `release_edges` (:397) reads the capture, calls `showrunners.release_edges(slug, released)`, and tells each production in `former_parent` and `former_children` that is not in `told`. After each tell it writes the capture again with that slug added to `told`, so a rerun tells each production once. `tidy_state` (:415) then removes the slug's state folder and the capture with it.
     A permanent unit (`Plan` cell `permanent`, :121) outlives the wrap: its row stays, so the showrunner row stays too (`retire` refuses a showrunner with a unit under it), and wrap prints one line that names them. On that path wrap also `move`s the showrunner row and each child showrunner row to `None`, so no edge to or from the wrapped production is left in the rows. A unit row kept for its worker rows (item 2) keeps the showrunner row in the same way.
     **Pending retirement.** When worker rows keep a row, wrap still removes the timer (a wrapped production keeps no timer) and calls `staff.defer_retire(<the ids it could not retire>)`. `staff.py` keeps those ids in the record's `pending_retire` list (sorted; an absent list reads as empty; the schema stays 1) and shows them as `StaffSnapshot.pending_retire`. `staff.retire_pending() -> tuple[StaffId, ...]` retires each listed id that can go now (a unit with no worker rows, then a showrunner with no unit under it), drops an id that has no row, and returns the ids still held. A wrap retry, also on an already wrapped doc (`wrap_production`, :337), calls it and prints one line that names each id still held. `defer-retire` and `retire-pending` are hidden Mac-transport verbs, as the other writes are.
   - Promote → `ensure` the new row before the first change, `retire` the old row after the last: `promote_showrunner.py` adds the showrunner row and removes the unit row; `/showrunner:promote_unit` has no script of its own: it runs `add_unit.py --resume`, whose `ensure` adds the unit row.
   - A rename or a transfer never touches a worker row, a herdr agent or a session: workers keep their ids and follow through `parent`. No reader changes in this phase.

**Files:**
- `scripts/production/add_showrunner.py`, `scripts/production/add_unit.py`, `scripts/production/resume_unit.py` — `ensure` the row.
- `scripts/production/showrunners.py` — `attach` (:588) and `detach` (:614) change only the parent file, and `main`'s attach and detach branch imports `showrunner_parent` inside the branch and calls it; `WrapRelease`, `ReportedTo`, `Root`, `edges_to_release` and `release_edges(slug, released)` (:588) replace `Released` and the old `release_edges`.
- `scripts/production/retire_unit.py`, `scripts/production/rename_unit.py`, `scripts/production/rename.py`, `scripts/production/transfer_unit.py` — `retire`, `rename` and `move`; the staff id in the journals.
- `scripts/production/promote_showrunner.py`, `scripts/production/production_lifecycle.py` — promote and wrap rows; wrap's release capture `wrap_release.json`. `/showrunner:promote_unit` has no script: it runs `add_unit.py --resume`.
- `scripts/production/test_add_showrunner.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_add_unit_resume.py`, `scripts/production/test_resume_unit.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_rename_unit_rerun.py`, `scripts/production/test_rename.py`, `scripts/production/test_rename_rerun.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/test_showrunners.py`, `scripts/production/test_promote_showrunner.py`, `scripts/production/test_production_lifecycle.py` — each tool's row, refusal and `test_<tool>_rerun` tests.
- `scripts/production/add_showrunner_test_support.py`, `scripts/production/add_unit_test_support.py`, `scripts/production/rename_unit_test_support.py`, `scripts/production/rename_test_support.py`, `scripts/production/transfer_unit_test_support.py`, `scripts/production/production_lifecycle_test_support.py` — fixtures point at one temp `STAFF_FILE`.
- `scripts/production/staff_test_support.py` — read only: the one staff test fixture (Phase 2); no test file keeps its own copy.
- `scripts/production/staff.py` — `operation()` joins an inherited lock (the showrunner rulings below); `pending_retire`, `defer_retire`, `retire_pending` and their hidden verbs.
- `scripts/production/test_staff.py` — the lock-join and pending-retirement tests.
- `scripts/production/showrunner_parent.py`, `scripts/production/test_showrunner_parent.py` — the attach and detach code behind `showrunners.py attach|detach` (new).
- `scripts/production/staff_sources.py`, `scripts/lib/atomic_file.py` — read only.

**Workers:** `2 writers` — split by tool group.
- `impl` — add, resume, retire, promote and wrap tools and their tests; `staff.py` (the lock join and pending retirement) and `test_staff.py`.
- `test` (opens as impl) — `showrunners.py`, `showrunner_parent.py`, `rename.py`, `rename_unit.py`, `transfer_unit.py` and their tests; hub: `scripts/production/showrunners.py` (`WrapRelease`, `edges_to_release` and `release_edges`, which wrap calls).

**State:** rows and the `pending_retire` list in `~/.local/state/staff/staff.json`; the staff id in the journals of `rename.py`, `rename_unit.py` and `transfer_unit.py`; wrap's release capture `~/.local/state/showrunner/<slug>/wrap_release.json`, which `tidy_state` removes at the end of the wrap.

**Constraints from prior phases:** Phase 1 built `staff.py` (`ensure`, `rename`, `move`, `retire`, `by_id`, `by_path`, `find`, the exceptions `StaffRefused`, `StaffNotFound`, `StaffUnreadable`; `STAFF_FILE` for tests). Phase 2 built `staff_sources.member(selector, record) -> MemberFound | NoSuchMember` with the selectors `ShowrunnerMember(slug)` and `UnitMember(slug, unit)`, which matches by `STAFF_ID` first and by name without one; `staff.snapshot() -> StaffSnapshot` (one record read, with `by_id`, `by_path` and `find`; it raises `StaffUnreadable`); `staff.operation()`, the per-machine operation lock `~/.local/state/staff/operation.lock`, taken before the write lock; and `seed`, which holds the operation lock while it runs, retires a unit row only on doc evidence that the unit is gone, and never retires a unit row with worker rows. `scripts/production/staff_test_support.py` holds the one staff test fixture (temp `STAFF_FILE`, `FAKE_SSH`, the switch between owner and Mac). Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer).

**Row lookup, nested locks and wrap with workers left (showrunner, 2026-10-30):**
- One way to find a row: every writer and reader here finds a showrunner or unit row only through `staff_sources.member`; nothing calls `staff.by_path(slug)` for a member. Row names are workspace and tab names, not slugs: slug `sdlc` has row name `tools` (`scripts/production/test_staff_sources.py:28`).
- `staff.operation()` joins across processes. The outer holder passes its lock fd and its owner in the environment. A child's `operation()` joins only when the inherited fd really is the lock: it `fstat`s the fd, compares `st_dev` and `st_ino` with the lock file, and confirms the fd holds the `flock`. The environment value alone is never proof; when the check fails, the child takes the lock as usual. `resume_unit`, `promote_unit` and nested attach hold one lock from their start through the retirement-mark clear (`scripts/production/resume_unit.py:253`), so a seed cannot retire a row that `add_unit` just ensured.
- Wrap with worker rows left: when worker rows keep a unit or showrunner row alive, wrap still removes the timer (a wrapped production keeps no timer) and writes the ids it could not retire as a pending retirement in the staff record; tidy keeps it; a later wrap retry retires those ids once their workers stop, also on an already wrapped doc. Phase 10 keeps this record across shutdown and restore.

**Acceptance gate:**
- A slug that differs from its workspace name (slug `sdlc`, row `tools`): attach, detach, rename and wrap all find the row through `staff_sources.member`.
- A subprocess test: `resume_unit` running `add_unit` while a `seed` runs; the seed never retires the ensured row, and nothing deadlocks. A child given a wrong or unlocked fd in the environment takes the lock itself.
- Wrap with worker rows left, then the workers stop, then a wrap retry: the pending ids retire, also when the doc is already wrapped.
- `showrunners.py attach <child> <parent>` and `detach <child>`, run as subprocesses end to end, move the staff row and write or remove the parent file; `showrunners.py` has no load-time import of `showrunner_parent` or `staff_sources`.
- basedpyright 0/0 on changed files and every caller module.
- Each tool's tests assert the staff row after the tool runs.
- The refusal test: a rename to a taken name, a transfer to a showrunner that already has a unit of that name, and an attach that makes a loop each raise `StaffRefused` and leave every source and the staff file unchanged.
- One rerun test per tool (Invariants → Rerun-safe tools), named `test_<tool>_rerun`: the tool stopped just before its staff step and just after it, then run again, gives the sources and the staff file of one clean run; a rerun of a finished run reports done and changes nothing. An attach of an attached showrunner and a detach of a detached one report done and change nothing.
- The wrap tests, ordinary and permanent: in an ordinary wrap the rows of the wrapped units and of the showrunner go, and its child showrunners move to `None`; in a permanent wrap a permanent unit's row and its showrunner's row stay, with one line naming them, and the showrunner row and each child showrunner row move to `None`. For each, wrap stopped after the capture and before the release (after the first unit row's `retire`, and again after the showrunner row's), then run again, reads `wrap_release.json`, tells each former parent and child once, and ends as one clean wrap.
- The worker-row test: `retire_unit.py`, promote and wrap, each on a unit row with a worker row, keep the unit row and the worker row, print one line that names the worker row, and exit 1.
- The interleaving test: a tool stopped between its staff step (a `move` of a unit row) and its source update (the Units row) holds `staff.operation()`; a `seed` started then waits, and after the tool ends the seed keeps the unit row under its new showrunner.
- The `NoSuchMember` test: with no matching row, add and resume make one with `ensure`; retire, wrap and promote skip that `retire`; rename and transfer exit 2 and change no source.
- Full gate green.

### Phase 3b — Staff move command  · status: done

**Depends on:** Phase 3

Inserted 2026-10-30 (user, via the showrunner). Work Order: `staff.py move <unit> <showrunner>` changes one field, the unit row's parent, and moves the unit's live tab into the new showrunner's workspace through herdr.py's existing pane move; `transfer_unit.py` becomes that move plus one message to the unit and one to each showrunner. No stop, no restart.


### Phase 3c — Staff record is the only source (batch 1: seed and showrunner tree)  · status: done

#### As-built

`staff.json` is the only source of who reports to whom. No code reads `~/.local/state/showrunner/<child>/parent`.
- `showrunner_tree.py` owns every showrunner-tree read: `parent_of`, `children_of`, `descendants_of`, `root_of`, `user_facing`, `highest_running`, `tree_lines`, `edges`, `parent_edges`, `stored_children`, `check_attach`, `edges_to_release`, `release_edges`. They read the rows' `parent` through `staff.snapshot()` and `staff_sources.member()`. `stored_parent` is gone; callers call `parent_of`.
- `showrunners.py` keeps timers, addresses and messages. It takes no `StaffSnapshot`, never imports `staff_sources`, and re-exports no moved name. Its `main` imports `showrunner_tree` inside the attach and detach branch.
- `staff_sources.seed` is a one-time import. It adds a row only for a member the record lacks. It never changes a row's parent, deletes, retires or moves. No snapshot read calls `_collect`. An unreadable legacy parent file leaves its production unchanged. The docstring names the removal date, 2026-11-13.
- `staff_sources.unit_parent` reads a unit's parent from `staff.json`.
- `production_tree.py --json` prints `{"schema":1,"edges":[...]}` with the same bytes, order and fields as before, from the record.
- The read API is `staff.snapshot()`, `find`, `by_id` and `staff_sources.member()`. There is no second reader, no parent cache and no copy of a tree walk.

**Files:**
- `scripts/production/showrunner_tree.py` — the tree readers (`test_showrunner_tree.py`).
- `scripts/production/staff_sources.py` — member match and import-only `seed`.
- `scripts/production/showrunners.py` — timers, addresses, messages.
- `scripts/production/staff_test_support.py` — `showrunner_relationship`, the fixture helper for staff rows.
- Callers that import `showrunner_tree`: `showrunner_parent.py`, `add_showrunner.py`, `production_lifecycle.py`, `rename.py`, `rename_outside.py`, `production_tree.py`, `change_review.py`, `dailies_input.py`, `proposals_folder.py`, `update_registration.py`, `scripts/direct/board_json.py`, `scripts/style/add_style.py`.

**Binds later work:** Later tree readers call `showrunner_tree`, never `showrunners` or `staff_sources`, and reuse `staff_sources.unit_parent`. Fixtures write parents through `staff_test_support.showrunner_relationship`. The unread parent-file writers and the seed importer stay until a later change removes them, due 2026-11-13.

**Gotchas:**
- A fixture with no staff rows fails; one cause broke about 150 tests.
- Lazy imports in `showrunners.py main` raised basedpyright import cycles.
- The assertion guard files a moved assertion under its nested helper name.

### Phase 3e — Tab reader for stop and resume  · status: done

#### As-built

- `PaneIdentity` has `workspace_id: str` and `session_id: str | None`. `session_id` comes from `agent_session.value` when `agent_session` is an object with a text `value`, else `None`. A wrong type raises `HerdrFailure`.
- `TabSession(pane_id, label, workspace, session_id)` is a frozen dataclass. `workspace` is the workspace label.
- `Herdr.tab_sessions() -> list[TabSession]` uses `panes()` plus one `workspace list` read. It leaves out a pane with no label.
- `Herdr.session_of(label: str) -> TabSession | NoPane | SeveralPanes` shares `_match_label` with `lookup`. A tab whose Claude has exited still returns its session id.
- `fake_herdr.py` answers `pane list` in the live shape, keeps `agent_session` after the agent exits, and logs each start's real outcome in `<command-log>.agent-starts`.

**Files:**
- `scripts/herdr/herdr.py` — the reader; `fake_herdr.py` — the fake; `test_herdr.py` — its tests.
- `scripts/shutdown/test_pane_restore.py`, `test_stop.py`, `scripts/production/test_promote_showrunner.py` — typed pane fixtures.

**Binds later work:** Stop and resume read tabs only through `tab_sessions` and `session_of`. `session_id` of `None` means no conversation is known; resume passes `--resume` only when a session id is known. The shutdown unit's checkpoint deletes the duplicate `pane_restore._restored_panes` and `RestoredPane`.

**Gotchas:** The two new `PaneIdentity` fields are required, so every typed pane fixture must set them.

**Ruled out:** a second pane-list parser.

### Phase 3f — One showrunner lookup  · status: done

#### As-built

`_MemberSources.showrunner_members(record) -> dict[str, StaffRow]` in `staff_sources.py` is the one answer to which row each production slug has. The public `showrunner_members` and `unit_parent` both call it; `unit_parent` picks the slug whose row is the staff parent, and raises `StaffUnreadable` when that slug is not unique. `_unit_member` answers a different question and stays.

**Files:**
- `scripts/production/staff_sources.py` — the lookup.
- `scripts/production/test_staff_sources.py` — agreement after a transfer and one read per `unit_parent` call.

**Ruled out:** a launcher and pane-move resume here; the shutdown unit's typed resume through `Herdr.run_in_pane` and `accounts.py exec <account>` replaced it.

### Phase 3j — Transfer journal removed  · status: todo

**Depends on:** Phase 3f, Phase 3h

Split from 3f by the plan review (2026-10-30): the journal still guards work that later reader batches replace. Packaging only; the deliverable is unchanged.

#### Work Order

**Goal:** a transfer is the staff move plus its messages; `transfer_unit.py` keeps no journal.

**Spec:** Delete `journal`, `STEPS`, `_load`, `_migrate_journal`, `_load_fields`, `_save`, `_completed_requests`, `_resumed`, `_check_journals`, and `staff_move.PinnedUnit` when nothing else uses it, with their tests and doc lines. Before each deletion, confirm the step it guards has no consumer left: the Units-table commits (3g stops reading parent from the table), the stall-state move (3h), the design-role state and the review-pool lease (3h). A step that still has a consumer stays as one rerun-safe call, and the checkpoint names it. Inventory: Reference → Transfer journal inventory.

**Files:**
- `scripts/production/transfer_unit.py`
- `scripts/production/test_transfer_unit.py`
- `scripts/production/transfer_unit_test_support.py`
- `scripts/production/rename_test_support.py`
- `scripts/production/staff_move.py`
- `docs/as-built/unit-transfer.md`
- `commands/showrunner/transfer_unit.md`

**Workers:** `1 writer`.

**State:** the old `transfer_unit.json` files are no longer read or written.

**Constraints from prior phases:** Phase 3b staff move; 3g membership from rows; 3h stall state by staff id and review pool by parent row. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:** basedpyright 0/0 on changed files and callers; a transfer run twice ends as one clean transfer and tells each party once; the consumers of stall, review-pool and design-role state read the right values after a transfer; `test_transfer*`, `test_staff*`, `test_rename*` suites pass.

### Phase 3g — Staff readers switch (batch 2: unit membership)  · status: done

**Depends on:** Phase 3f

#### Work Order

**Goal:** a production's units come from the staff rows, and every unit and showrunner launch carries its row's `STAFF_ID`.

**Spec:**

1. **Identity first.** Phase 4 item 3 ships here, because this join needs it: `add_unit.py`, `add_showrunner.py` and `resume_unit.py` set `STAFF_ID=<row id>`; `STAFF_ID` joins `pane_record.KEPT_NAMES`; `herdr.unmarked_environment` drops it.
2. **Membership.** `unit_lookup.py` (`marked_units`, `marked_unit`, `group_marked_units`, `session_unit_marks`, `_herdr_marked`), `production_units.py` (`live_unit_table`, `retired_units`, `unit_is_retired`) and `live_units.py` read `find(kind="unit", parent=<showrunner id>)`. Panes join by `STAFF_ID`. The Units table stops carrying the parent and keeps the Worktree and Plan cells. `production_tree.unit_edges` already reads row parents; leave it.
3. **One compatibility boundary for old panes.** A pane with no `STAFF_ID` joins by its `SHOWRUNNER_UNIT_ID` name alone, through `staff_sources.member`, and ignores its production mark, which a transfer leaves stale. Two rows with that name give a problem line and no match. This fallback goes with the importer on 2026-11-13 (Phase 16).
4. **One snapshot-aware resolver.** `staff_sources` imports `production_units`, which imports `unit_lookup`, so lower readers cannot import it. The row-parent answer moves to one function that takes the caller's `StaffSnapshot`, below `production_units` in the import order, and `staff_sources.unit_parent` calls it. A run makes one owner read.

**Files:**
- `scripts/production/unit_lookup.py`
- `scripts/production/production_units.py`
- `scripts/production/live_units.py`
- `scripts/production/staff_sources.py`
- `scripts/production/add_unit.py`
- `scripts/production/add_showrunner.py`
- `scripts/production/resume_unit.py`
- `scripts/shutdown/pane_record.py`
- `scripts/herdr/herdr.py`
- `scripts/production/staff_test_support.py`
- `scripts/production/test_unit_lookup.py`
- `scripts/production/test_live_units.py`
- `scripts/production/test_staff_sources.py`
- `scripts/production/test_add_unit.py`
- `scripts/production/test_add_showrunner.py`
- `scripts/shutdown/test_pane_record.py`
- `scripts/herdr/test_herdr.py`

**Workers:** `1 writer + 1 tester` — `impl`: the unit-reader APIs, their callers, the launches and the resolver. `test`: the regression tests, and it alone changes `staff_test_support.py`.

**State:** `STAFF_ID` in pane launch records.

**Constraints from prior phases:** 3c staff row parents and `showrunner_tree`; the cargo-handler contract stays byte-identical. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:** basedpyright 0/0 on changed files and callers; tests for a legacy pane with no `STAFF_ID`, an offline unit, and a transferred unit whose pane has a stale production mark; an import test that loads `unit_lookup` and `staff_sources` in both orders; one owner read per run on the Mac (fake `ssh` log); `test_staff*`, `test_unit_lookup*`, `test_live_units*`, `test_add_*`, `test_pane*`, `test_herdr*` suites pass.

### Phase 3h — Staff readers switch (batch 3: stall watch and review pool)  · status: todo

**Depends on:** Phase 3g

#### Work Order

**Goal:** stall state is keyed by staff id, so no rename or transfer moves it; `review_pool.py` finds a unit's holder as its parent row.

**Spec:** `stall_watch.py` keys its stretch files, prompt files and notification keys by staff id. A rerun-safe migration renames each existing file to its staff-id name and cancels and re-sends an alert under the new key only when the old key is live, so elapsed time and cancellation survive; it runs before the moves are deleted. The moves in `rename_unit.py`, `rename.py` and `transfer_unit.py` then go. `review_pool.py` finds a unit's holder as its parent row; `pool.json` keeps its path and bytes.

**Files:**
- `scripts/production/stall_watch.py`
- `scripts/production/test_stall_watch.py`
- `scripts/production/rename_unit.py`
- `scripts/production/test_rename_unit.py`
- `scripts/production/rename.py`
- `scripts/production/test_rename.py`
- `scripts/production/transfer_unit.py`
- `scripts/production/test_transfer_unit.py`
- `scripts/direct/review_pool.py`
- `scripts/direct/test_review_pool.py`

**Workers:** `2 writers` — `impl`: `stall_watch.py` and the three move removals; `test` (opens as impl): `review_pool.py` and the tests.

**State:** stall files move to staff-id names, once.

**Constraints from prior phases:** 3g membership and the snapshot-aware resolver. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:** basedpyright 0/0 on changed files and callers; an active alert keeps its elapsed time and still cancels after a rename and after a transfer; the migration run twice equals one clean run; `pool.json` is byte-identical; `test_stall*`, `test_rename*`, `test_transfer*`, `test_review_pool*` suites pass.

### Phase 3i — Staff readers switch (batch 4: no showrunner from a folder or branch name)  · status: todo

**Depends on:** Phase 3h

#### Work Order

**Goal:** no code derives a unit's showrunner from a worktree path, branch, plan header or doc field; the row's parent answers it.

**Spec:** Switch `assertion_guard.py`, `change_review.py`, `board_json.py`, `usage_report.py`, `waste_watch.py`, `outcomes.py`, `push_audit.py`, `proposals_folder.py`, `phase_table.py`, `rca.py` and `statusline.sh`. Two questions stay separate: **current owner** reads the row; **history** (who owned a past commit or run) keeps what the artifact recorded and survives a retired row. A worktree or branch finds its row through one function that reads the existing artifact metadata, so prefixed folders and transferred units resolve before Phase 3d renames anything.

**Files:**
- `scripts/direct/assertion_guard.py`
- `scripts/direct/test_assertion_guard.py`
- `scripts/production/change_review.py`
- `scripts/production/test_change_review.py`
- `scripts/direct/board_json.py`
- `scripts/direct/test_board_json.py`
- `scripts/usage/usage_report.py`
- `scripts/usage/test_usage_report.py`
- `scripts/usage/waste_watch.py`
- `scripts/usage/test_waste_watch.py`
- `scripts/usage/outcomes.py`
- `scripts/usage/test_outcomes.py`
- `scripts/hierarchies/push_audit.py`
- `scripts/hierarchies/test_push_audit.py`
- `scripts/production/proposals_folder.py`
- `scripts/production/test_proposals_table.py`
- `scripts/direct/phase_table.py`
- `scripts/direct/test_phase_table.py`
- `scripts/message/rca.py`
- `scripts/message/test_rca.py`
- `scripts/statusline/statusline.sh`
- `scripts/statusline/test_statusline.py`

**Workers:** `2 writers` — split by tree. `impl`: `scripts/direct/`, `scripts/production/`, `scripts/statusline/`. `test` (opens as impl): `scripts/usage/`, `scripts/hierarchies/`, `scripts/message/`.

**State:** none.

**Constraints from prior phases:** 3g resolver and membership. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:** basedpyright 0/0 on changed files and callers; tests for a prefixed path, a transferred unit, and history whose row has retired; a per-directory test run for each tree; the status line's time stays under its current budget.

### Phase 3d — Unit folder names  · status: done

**Depends on:** none — runs beside 3g in its own worktree, `~/worktrees/claude-natedev-herdr-names` on branch `natedev-herdr-names`, with its own workers and checkpoint; 3g's commits merge into it as they land (user, via sdlc, 2026-10-30)

Inserted 2026-10-30 (user, via the showrunners).

#### Work Order

**Goal:** a unit's short name is unique; its worktree and branch are `~/worktrees/<short name>` and `<short name>`, computed from the row and stored nowhere.

**Spec:** `staff.py` refuses a unit short name that already exists. `staff._validate` runs on every load and today permits one unit name under two showrunners, so a tightened check could make the record unreadable: first `staff_sources seed --dry-run`-style discovery lists collisions, one controlled rename clears them, and then the check goes on, with `test_names_are_unique_within_kind_and_parent` changed under an `Assertion-Change:` line. `add_unit` makes the worktree and the branch from the short name. `/showrunner:add_unit` says unit names are unique. A one-step rename moves an existing unit's folder, branch and session history through the rename path, only at the unit's phase boundary and on its showrunner's word, one unit at a time.

A hand `/rename` never changes the row (decision 14), so paths stay purely computed from the row.

The rename is safe to rerun (user, via sdlc, 2026-10-30). `/showrunner:rename_unit style style` makes sure that the worktree is `~/worktrees/style`, the branch is `style`, and the tab and the session are named `style`. It changes nothing when all four already match, and it repairs only the parts that do not. A rename to the same name is the normal repair path. `unit_names.worktree` builds `~/worktrees/<short name>`, not `<checkout>-<stem>`. Two dry runs refuse today and must pass: `rename_unit.py plan natedev style style` ("build-followups has no readable update timer": the slug resolves to a retired production) and `rename_unit.py plan sdlc models models` (the `rename_unit.json` journal "no matching staff member for legacy staff_id"). shutdown's Phase 9 rewrites rename_unit's steps; each part has one owner, as agreed with shutdown before dispatch.

**Files:**
- `scripts/production/unit_names.py`
- `scripts/production/test_unit_names.py`
- `scripts/production/test_rename_unit_rerun.py`
- `scripts/production/rename_unit_test_support.py`
- `scripts/production/staff.py`
- `scripts/production/test_staff.py`
- `scripts/production/add_unit.py`
- `scripts/production/test_add_unit.py`
- `scripts/production/rename_unit.py`
- `scripts/production/test_rename_unit.py`
- `commands/showrunner/add_unit.md`

**Workers:** `2 writers` — `impl`: `staff.py`, `add_unit.py`, `add_unit.md`; `test` (opens as impl): `rename_unit.py` and the tests.

**State:** rows renamed by the collision migration.

**Constraints from prior phases:** 3i finds a worktree's row from artifact metadata. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:** basedpyright 0/0; collision discovery on a record with a duplicate; a record with a duplicate stays readable until migrated; resume, retire and transfer pass after a rename; `test_staff*`, `test_add_unit*`, `test_rename_unit*`, `test_unit_names` suites pass; both dry runs above pass; a same-name rename on a real unit reports nothing to change, live, before the notice.

### Phase 3k — Branch and Worktree cells removed  · status: todo

**Depends on:** 3d, 3g, 3i — those phases change the Units readers this phase edits.

Inserted 2026-10-30 (sdlc showrunner, from the 3d contract review).

#### Work Order

**Goal:** the Branch and Worktree cells are gone; every reader computes them through unit_names.

**Spec:** Phase 3d computes a unit's branch (`unit_names.branch`) and worktree (`unit_names.worktree`) from its short name, but each Units row still stores Branch and Worktree cells. Until this phase, add_unit writes them equal to the computed names and a same-name rename repairs a cell that differs. This phase removes both cells from every Units table and from the production format. Every reader that reads them (add_unit, rename_unit, resume, retire, transfer, unit lookup, the shutdown and restore paths) computes the names through unit_names instead. One place for the rule; no reader keeps its own copy.

**Files:** to be listed at phase start from a search for the Branch and Worktree cell reads.

**State:** Units tables in every production doc lose two columns.

**Acceptance gate:** no code reads a Branch or Worktree cell; the affected suites pass; basedpyright 0/0; a real `rename_unit.py plan` and `add_unit.py --check` on a real unit print the computed names.

### Phase 3l — Workers on the staff record; production_tree removed  · status: todo

**Depends on:** 3g — 3g is the fix for today's handler pane; this phase replaces the reader it fixes.

Inserted 2026-10-30. User (via the sdlc showrunner): "production_tree.py must go away" and "codex can be started and stopped the same way as with claude resumes by name or id". The sdlc showrunner accepted this design on 2026-10-30.

#### Work Order

**Goal:** every worker is a staff member. The handler reads who reports to whom from one staff command. A Codex worker is stopped and resumed by the same path as a Claude session, by name or staff id. `production_tree.py` is gone.

**Spec:**

1. **Worker rows.** Each worker gets a new staff id from `staff.ensure` (`s…`, as every other row), kind `worker`, a name and a parent. The Codex thread id is a run detail: a replaced thread (`codex_mesh.py` `_resume_worker_thread` replacement start, a fresh thread after a failed compaction) keeps its row. **One record holds a worker's thread id** (sdlc showrunner, 2026-10-30: no copies of one fact): the unit ledger entry, keyed by staff id. Every other record keys by staff id and looks the thread up there (holders listed by the models unit):
   - the run roster (`mesh_roster.json`): staff id, server and run details; no thread id.
   - the held-message inbox (`_pending_for_thread`, one file per thread today; a mismatch drops held messages with no error): one file per staff id.
   - `mesh_retired.json`: removed. A replaced or ended thread is a `replaced` / `ended` event in the unit ledger (`note_backend` already logs `replaced`), and run end reads the threads to close from those events.
   - detached runs (`mesh_runs.py:155` matches `CODEX_THREAD_ID`): the record holds the staff id; the match maps the env's thread id to a staff id through the ledger.
   - the review pool records: staff id.
   - the design roles' held threads (`design_roles.py:139-149`, `:1288`): staff id.
   - the shutdown pause records (`shutdown_record.py:486`): staff id.
   Two copies stay, both Codex's own and outside these scripts: the app-server's thread store, and the `CODEX_THREAD_ID` that Codex sets in its worker's environment. progress_history and usage keep old thread ids as history, not as a live fact. Lock order (models unit, F024, 2026-10-31): "unit ledger, design/pool, membership, lifecycle, inbox, roster". At each admission and retirement point, `ensure_worker` / `retire` run while the run-level membership lock is held, after the lifecycle, inbox and roster locks are released, so a bulk stop cannot run between the roster write and the staff row. A staff lock is never held while membership is taken; a lock of staff.py's own sits after membership and before lifecycle, and the lock-order guard in `test_codex_mesh` checks it. These call sites start from models' F024 checkpoint. `WorkerName.parse` (`staff.py:70`) accepts the live names (`impl`, `impl2`, `<project>-impl`, the Astra instance names). A worker's parent is a unit, or a showrunner for a showrunner-held Astra role (today `staff.py:317` refuses it). One writer, `staff.ensure_worker` and `staff.retire`, is called from each one open and end point:
   - Codex: open `codex_mesh.open_unit_worker` and `start`; end `codex_mesh end`, `stop` (`_mark_all_roster_ended`), `workers.end_unit`; adoption `workers.adopt` moves the parent to the adopting unit.
   - Claude workers (production_tree's `_claude_edges` today): their one open point in `implement.sh` / `assign.sh` and their one end point, `remove_workers.py`.
   - Astra roles: `design_roles.open_role` and `design_roles.end`.
   - Pool reviewers: the row's parent is the pool's holder, never the unit that leases it. The current lease is a live detail, shown as who the reviewer serves now.
   A unit move or rename keeps the rows, because the unit's row id does not change. A rerun-safe seed adds rows for the workers open now, from the unit ledgers, rosters, the review pool and the Astra records; a second run changes nothing.
2. **Busy and idle are read live, never stored:** the server pid, the pool's `busy_pid`, the Astra exec pid. `astra_roles.py:248` reads a stored roster status today; that stops.
3. **Owner fields go.** The rosters and the unit ledger keep thread and run details only. The run `director` file, the pool's `lease.unit` and the Astra record's `manager` stop answering who owns a worker; staff answers it.
4. **One stop and one resume path.** `shutdown.py stop <name|staff id>` and `resume <name|staff id>` take a worker as they take a Claude session. For a Codex worker, stop pauses its thread and resume follows or reopens it on its account. This replaces, as paths anyone runs: the resume message's seat lines (`implement.sh --to <seat>`, `stop_resume.py:313`), `workers.pause_run` / `pause_unit`, and `codex_mesh reopen` / `resume-account-move --to`. `move_sessions` and `stop_session` go through the same per-member path. The old commands stay as internals only: no user or skill text names them, no second caller reaches them, and a check fails on any new caller.
5. **One handler command:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py show --json --live` prints `{"schema": 2, "inputs": [<absolute path of every file this run read>], "members": [{"id", "kind", "name", "parent": <an id in this list, null only for the root showrunner>, "role": showrunner|unit|impl|test|review|fix|astra-designer|astra-reviewer, "identity": {"claude": <session id>} | {"codex": <thread id>}, "pool": {"state": busy|idle, "serves": <staff id or null>}}]}`. `pool` is present only on review-pool members; no other member carries a state. Agreed with handler-unit 2026-10-30: one tagged identity, parents inside the list, `inputs` so the handler keeps no copy of the file list, Astra roles kept in the output (the handler skips roles it does not know). The handler switches its reader in step (`cargo-handler` `hierarchy.rs:602-690`, the exact argv match at `command_inputs.rs:212-217`, the config line in `~/.config/cargo-handler/config.toml`).
6. **Delete `production_tree.py`,** `test_production_tree.py`, the `design_roles.py:510` import (`showrunner_sessions` moves to the staff live read) and every doc that names it (`showrunner-automation.md`, `long-lived-workers.md`, `sdlc-roles.md`, this plan's mentions). No wrapper or copy remains.

**Owners:** `codex_mesh.py` and `workers.py` are the models unit's: herdr workers write the call sites, and models reviews the diff before the merge. `stop` / `_mark_all_roster_ended` overlaps models' open follow-up F024, so tell models before that function changes. `stop_resume.py` and `shutdown.py` are the shutdown unit's: shutdown writes its side against the herdr resolver (name or staff id → Claude session | Codex worker | not found), on its branch after its p7 merges (p7 deletes `stop.py` and `stop_session`). The handler reader is handler-unit's.

**Files:** to be listed at phase start from the open and end points above and a search for `production_tree`.

**Acceptance gate:** no file names `production_tree`; the affected suites pass; basedpyright 0/0; the real handler pane shows every session and worker correct after a real unit move and a real new worker; one real Codex worker is stopped and resumed by name and by staff id.

### Phase 3m — Open change rows follow a unit move  · status: todo

**Depends on:** 3b (the staff move command). Runs beside 3l: its files are `staff_move.py`, `change_review.py` and their tests.

Inserted 2026-10-30. User (via the sdlc showrunner): land the simplification changes overnight, this one with them.

#### Work Order

**Goal:** after a unit moves to another showrunner, each of its rows still under `## To review` names the new production, on the list the new production uses. Today the row keeps the old Production cell, so the user checks it under the wrong production.

**Spec:**

1. **Which rows are the unit's.** A row is the moved unit's when `change_review.trace` finds that its merge brings in a checkpoint of the unit's branch (the `named` rule first, then `every` with one owner). A row that traces to more than one owner, or to none, stays as it is and is reported by row id and reason.
2. **One writer.** `staff_move.move_unit` calls one change-review function after the staff parent changes, beside the tab move. It reuses the cell writer `rename_production` uses (`_edit` with the receiving production doc's stamp); no second table writer. When the receiving production's list is another file, the row moves to that list's `## To review` with its id; otherwise only the Production cell changes.
3. **Rows under `## Reviewed` never change.**
4. **Rerun-safe.** A second run of the same move changes nothing and prints `already matches`. A move interrupted after the staff change and before the rows changed finishes the rows on the next run of the same move, because the staff record then shows the unit at its new parent.
5. **A list that cannot be read or written** leaves the staff move done and reports the rows not moved, with the command to rerun; it never undoes the staff move.

**Files:** `scripts/production/staff_move.py`, `scripts/production/change_review.py`, `scripts/production/test_staff_move.py`, `scripts/production/test_change_review.py`.

**Acceptance gate:** the suites for both files pass; basedpyright 0/0; one real move of a throwaway unit that has a real open row shows the row under the new production, and a second run changes nothing.

### Phase 4 — Member names come from the staff row  · status: todo

**Depends on:** Phase 1, Phase 3, Phase 3g

#### Work Order

**Goal:** every launch and every restore of a member takes its name from its row, and a hand `/rename` in a unit director's pane leaves the row and the tab as they are and prints the rename_unit command.

**Spec:**

1. **Launch names come from the row.** `add_unit.py` (:957, :969; `resume_unit.py` through it) and `add_showrunner.py` (:148, :153) pass the row's name for `-n` and the tab title. `pane_restore.py` today relaunches with the `-n` name saved at launch (`claude_arguments` :83-88) and titles the tab with the pane label or that name (:99), so a restart would undo a hand `/rename`. `restore` (:108) takes one `staff.snapshot()` before its pane loop, when at least one entry's environment carries `STAFF_ID`; for each such entry it takes the name from `record.by_id(STAFF_ID)` for `-n`, `--remote-control` and the title. When `snapshot()` raises `StaffUnreadable` (natedev not reached from the Mac), restore makes no second call: every entry uses its saved name, and restore prints one line saying so. An entry whose row the snapshot does not hold uses its saved name, and restore prints one line that names the entry. `pane_restore.py` is the shutdown unit's file (Invariants → Owners).
2. **A hand `/rename` of a unit changes nothing (sdlc showrunner, 2026-10-30; decision 14).** The staff row is the one name of each member, and a unit's name changes only through `/showrunner:rename_unit`. The Stop hook `scripts/hooks/herdr_tab_title.py` reads the session's name from its session record (`current_name`). On a unit pane (`STAFF_ID` and `SHOWRUNNER_UNIT_ID`, no `UNIT_TEAM_ROLE`) it compares that name with the pane label. On a mismatch it keeps the tab and pane on the row's name, makes no staff write, and prints one line with the `/showrunner:rename_unit` command. The session record keeps the typed name until the next launch, which takes the row's name (item 1). The hook never stops the turn (`main`). Other panes:
   - A showrunner pane (`STAFF_ID` and no unit marks): the hook makes no staff call and changes no title. `/showrunner:rename` (`rename.py`) renames the row, the workspace and the tab together (decision 14).
   - A worker pane (`UNIT_TEAM_ROLE`, Phase 7): unchanged. Its session name is its id (decision 2), and its label is set once at start (decision 13).
   - A pane with no `STAFF_ID` (an ad hoc session, or a member launched before this phase) has no row, so the hook retitles it as today.
   Nothing else stores a shown name.
3. **The member id in the pane env ships in Phase 3g; keep this as its record.** Before 3g, no mark carried a staff id. A unit pane carries `SHOWRUNNER_UNIT` (the production slug) and `SHOWRUNNER_UNIT_ID` (the unit name, for example `herdr-unit`, not a staff id), set at `add_unit.py:927-928` (keys at `unit_lookup.py:30-31`). A showrunner pane carries neither (`add_showrunner.py:149`). So the unit launch (`add_unit.py:927`; `resume_unit.py` also runs it, through `add_unit.py --resume`) and the showrunner launch (`add_showrunner.py:149`) also set `STAFF_ID=<the row's id>`, the key that Phase 7 gives each worker pane. A promote relaunches through these launches (`promote_showrunner.py` through `add_showrunner.py`, `/showrunner:promote_unit` through `add_unit.py --resume`), so the relaunch carries the new row's `STAFF_ID`. A restart keeps only the names in `KEPT_NAMES` and the `SHOWRUNNER_` prefix (`scripts/shutdown/pane_record.py:48-49`, read back at `pane_restore.py:98`), so `STAFF_ID` joins `KEPT_NAMES` (the shutdown unit's file; Invariants → Owners). `herdr.unmarked_environment` (`herdr.py:835-837`) drops `STAFF_ID` with the unit marks, so a herdr server started from a member's pane never gives that id to other panes. A session started before this phase has no `STAFF_ID` until its next launch.

**Files:**
- `scripts/production/add_showrunner.py`, `scripts/production/add_unit.py`, `scripts/production/resume_unit.py` — the row's name for `-n` and the tab title; `STAFF_ID` in the launch env.
- `scripts/hooks/herdr_tab_title.py`, `scripts/hooks/test_herdr_tab_title.py` — a hand `/rename` of a unit keeps the row's name and prints the command.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — `unmarked_environment` (:835-837) drops `STAFF_ID`.
- `scripts/shutdown/pane_record.py`, `scripts/shutdown/pane_restore.py`, `scripts/shutdown/test_pane_record.py`, `scripts/shutdown/test_pane_restore.py` — `KEPT_NAMES` and the restore name; the shutdown unit's files.
- `scripts/production/test_add_showrunner.py`, `scripts/production/test_add_unit.py` — the launch name and `STAFF_ID` tests.
- `scripts/production/staff.py`, `scripts/production/staff_sources.py`, `scripts/production/staff_test_support.py`, `scripts/lib/atomic_file.py` — read only.

**Workers:** `2 writers` — split by launch path and hook.
- `impl` — the launch env and names: `add_unit.py`, `add_showrunner.py`, `resume_unit.py`, `herdr.py`, `pane_record.py`, `pane_restore.py` and their tests.
- `test` (opens as impl) — the hook (`herdr_tab_title.py`, `test_herdr_tab_title.py`).

**State:** `STAFF_ID` in the pane env and the pane launch records.

**Constraints from prior phases:** Phase 1 `staff.py` (`rename`, `by_id`; `StaffRefused`, `StaffUnreadable`; on the Mac each call is an ssh to natedev, item 1). Phase 2 `staff_sources.member` reads the `STAFF_ID` of a member's newest pane launch record first. Phase 2 `staff.snapshot() -> StaffSnapshot` is one record read (on the Mac one ssh call), with `by_id`; it raises `StaffUnreadable`. Phase 3: every tool that adds, renames, moves or retires a member writes its row, so `add_unit.py`, `add_showrunner.py` and `resume_unit.py` hold the row before they launch. `scripts/production/staff_test_support.py` (Phase 2) holds the one staff test fixture (temp `STAFF_FILE`, `FAKE_SSH` with its call log, the switch between owner and Mac); the tests of this phase use it and keep no copy. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). `pane_record.py` `_save` (:93) makes its own temp file and `os.replace` (:94-98); this phase changes `pane_record.py`, so `_save` calls `atomic_file.write_text_atomic` instead. Shutdown's checkpoint (2026-10-30): every resume runs in the session's own tab with `Herdr.run_in_pane` through `accounts.py exec <account>`, and `Herdr.tab_sessions` is the one pane reader. Ordinary and split restores keep conversation id, account environment, identity, tab label and position, and their tests show it.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- The launch test: `add_unit.py` and `add_showrunner.py` launch with the row's name for `-n` and the tab title and with `STAFF_ID=<the row's id>` in the env; `STAFF_ID` is in `KEPT_NAMES`, and `herdr.unmarked_environment` drops it.
- The hook test in `test_herdr_tab_title.py`: a hand `/rename` of a unit session (a fixture session record, a pane env with `STAFF_ID` and unit marks, a fake herdr) makes no staff call, keeps the tab and pane on the row's name, and prints one line with the rename_unit command; a showrunner pane and a worker pane make no staff call and no title change; a rerun with the same name changes nothing.
- The restore tests: an entry with `STAFF_ID` whose row was renamed relaunches with the row's name for `-n` and the title. On a machine that is not `OWNER_MACHINE`, a restore of several entries with `STAFF_ID` makes one call to the fake `ssh` (its log). With the fake `ssh` failing, restore makes that one call, then every entry uses its saved name, and restore prints one line.
- The writer test: `pane_record.py` `_save` writes through `atomic_file.write_text_atomic`, and `grep -n os.replace scripts/shutdown/pane_record.py` finds nothing.
- Full gate green.

### Phase 5 — Rename agents.conf to models.conf and /agent to /default_models  · status: todo

**Depends on:** none

**Blocked by:** the models unit's registry rewrite (`agents_registry.py` with `RegistryRow` and atomic edits; overlay files removed) and its shared config test fixture, landed on main.

#### Work Order

**Goal:** one name everywhere: `config/models.conf`, `MODELS_CONFIG_FILE`, `models_config.sh`, `models_registry.py`, `/default_models`; no live reference to the old names remains.

**Spec:** One sweep in one commit, no fallback that reads an old name, agreed with the models unit before the edit. Renames: `config/agents.conf` → `config/models.conf`; `AGENTS_CONFIG_FILE` → `MODELS_CONFIG_FILE` (the overlay env var drops with the overlay); `scripts/agents/agents_config.sh` → `models_config.sh`; `scripts/lib/agents_registry.py` → `models_registry.py`; `commands/agent.md` → `commands/default_models.md`; `scripts/agents/agent_admin.sh` → `default_models.sh`; the models unit's shared fixture follows the file. `models_registry.py` is the one parser and the one path authority: the Python path constants (`progress_history.DEFAULT_AGENTS_CONFIG_PATH`, `context_usage.DEFAULT_AGENTS_CONFIG_PATH`, `codex_pacer.AGENTS_CONF`, `quota_alert.REGISTRY`) and the line parsers (`codex_pacer.py:440`, `quota_alert.py:704`, `context_usage.py:269`, `progress_history.py`) call it; bash callers get resolved values from one `models_registry.py` call per launch that prints shell assignments, so bash parses no registry line. The full usage list is Reference → Rename inventory; build the manifest from a repository search, not from that list alone. `affected_modules.py:109` names `agents_config.sh` and changes to `models_config.sh`. Add `scripts/agents/test_no_old_model_names.py`: it fails on any live (non-history) reference to `agents.conf`, `AGENTS_CONFIG_FILE`, `agents_config.sh`, `agents_registry` or the `/agent` command. Send the user the NixOS diff files (questions list item 1).

**Files:**
- `config/agents.conf`, `config/README.md`, `.gitignore` — the file moves to `config/models.conf`; its references follow.
- `scripts/lib/agents_registry.py`, `scripts/lib/test_agents_registry.py`, `scripts/lib/test_agents_registry.sh`, `scripts/lib/test_agents_registry.conf` — move to `models_registry` names; the one parser and path authority.
- `scripts/agents/agents_config.sh`, `scripts/agents/test_agents_config.sh`, `scripts/agents/agent_admin.sh` — move to `models_config.sh`, `test_models_config.sh`, `default_models.sh`.
- `scripts/agents/agent_bg.sh`, `scripts/agents/agent_exec.sh`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/agents/sync_codex_catalog.sh`, `scripts/agents/test_sync_codex_catalog.sh`, `scripts/agents/test_launcher_tasks.sh`, `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py` — references.
- `scripts/agents/test_no_old_model_names.py` — new: the old-name check.
- `commands/agent.md` — moves to `commands/default_models.md`.
- `commands/ask_a_friend.md`, `commands/history.md`, `commands/plan/phase_review.md`, `commands/unit/direct.md`, `commands/showrunner/add_unit.md`, `commands/showrunner/promote_unit.md`, `commands/api_review.md`, `commands/module_review.md`, `commands/team_review.md` — references.
- `scripts/direct/assign.py`, `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/progress_history.py`, `scripts/direct/demo_refusals.sh`, `scripts/direct/recorder_fixture.py`, `scripts/direct/implement_launcher_test_support.py`, `scripts/direct/review_launcher_test_support.py`, `scripts/direct/test_assign_launcher.py`, `scripts/direct/test_implement_launcher_follow.py`, `scripts/direct/test_progress_history.py`, `scripts/direct/test_workers.py` — references.
- `scripts/production/add_unit.py`, `scripts/production/add_unit_test_support.py`, `scripts/production/unit_effort.py`, `scripts/production/test_unit_effort.py`, `scripts/production/affected_modules.py`, `scripts/production/test_affected_modules.py`, `scripts/production/test_stall_watch.py` — references.
- `scripts/whoami/codex_pacer.py`, `scripts/whoami/quota_alert.py`, `scripts/whoami/quota_alert_test_support.py`, `scripts/whoami/profile_notes_pass.py`, `scripts/whoami/test_profile_notes_pass.py`, `scripts/whoami/test_codex_alert_accounts.py`, `scripts/whoami/test_quota_alert_cancellation.py` — references and line parsers.
- `scripts/hooks/context_usage.py`, `scripts/hooks/test_stop_direct_continue.py`, `scripts/notify/review.py`, `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py`, `scripts/cli_agent/cli_agent.sh`, `scripts/history/history.py`, `scripts/ask_a_friend/launch_friend.sh`, `scripts/lib/test_nix_env_launches.py`, `scripts/style/test_add_style.py` — references.
- `docs/direct/launch_implementation.md`, `docs/as-built/agent-registry.md`, `docs/as-built/director-model-study.md`, `docs/as-built/long-lived-workers.md`, `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/message-clarity.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/style-showrunner.md`, `docs/as-built/unit-progress-history.md` — references.

**Workers:** `2 writers` — split by tree. `impl` — `config/`, `scripts/agents/`, `scripts/lib/`, `scripts/direct/`, `commands/`; hub: `scripts/lib/models_registry.py`. `test` (opens as impl) — `scripts/whoami/`, `scripts/hooks/`, `scripts/astra/`, `scripts/history/`, `scripts/production/`, `scripts/notify/`, `scripts/cli_agent/`, `scripts/ask_a_friend/`, docs, and the new test.

**State:** `config/agents.conf` moves to `config/models.conf`.

**Constraints from prior phases:** none from Phases 1-4.

**Acceptance gate:** `test_no_old_model_names.py` green; basedpyright 0/0 on changed files and callers; full gate green. No rerun test: the sweep is one commit, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

### Phase 6 — herdr primitives for worker panes, and probes  · status: todo

**Depends on:** none

#### Work Order

**Goal:** `herdr.py` can start an agent in a new split pane, set split ratios and read agent states as one typed value, and the probe answers are recorded in this plan.

**Spec:** `launch_prepared_agent` (`herdr.py:3178`) takes `destination: NewTab | SplitPane` in place of its tab arguments. `NewTab(space, title)` holds today's tab arguments, and every caller (`launch_agent` :3115 and the others) passes one. `SplitPane(of_pane, direction, ratio, label)` takes `direction: Literal["right", "down"]`, refuses a ratio outside (0, 1) when it is made, and labels the new pane before the agent starts, so a launch killed after the split leaves a pane that `start-or-resume` finds by its label (Phase 7). Both destinations keep the same environment proof, launch claim, prompt-once proof and cleanup. Add `Herdr.split_ratio(pane, ratio)` over the socket's `layout.set_split_ratio`, and `Herdr.label_pane(pane, text)`. Every split uses `--no-focus`. `AgentStatus`, a `StrEnum` of herdr's agent states (`working`, `idle`, `done`, `blocked` and the others herdr reports), replaces `agent_status: str` (:738, :778, :786) and is decoded once where herdr's JSON is read; a state herdr adds later decodes as `AgentStatus.UNKNOWN`, which no caller reads as settled. Probes, in a probe tab of `agents`, written into Reference → Probe results: which side `--ratio` measures; whether a close rebalances the rest; Codex and Claude queue a message typed mid-turn; herdr's Codex state over a 10-minute turn; prompt by path; both CLIs open an image by path from a prompt file; the exact Codex input that names a new session (help shows no `--name` flag; Claude uses `--name`); whether herdr's `resume_agents_on_restore` keeps each pane's `--env` (a positive answer retires our split-tab restore code; a negative one needs questions list item 1(e)); whether interactive Codex picks up a new `auth.json` by itself (a positive answer retires the sign-in restart, Reference job 8).

**Files:**
- `scripts/herdr/herdr.py` — `NewTab | SplitPane`, `split_ratio`, `label_pane`, `AgentStatus`.
- `scripts/shutdown/restart.py` — the two `launch_prepared_agent` calls (:1197, :2784) pass `NewTab`; the shutdown unit's file.
- `scripts/production/session_pane.py`, `scripts/production/stall_watch.py`, `scripts/message/roster.py` — the `agent_status` callers (:31-33, :801, :1820) take `AgentStatus`.
- `scripts/herdr/test_herdr.py` — the launch and decode tests.
- `docs/plans/herdr-worker-panes.md` — Reference → Probe results.

**Workers:** `2 writers` — `impl`: `herdr.py`, `restart.py`, the `agent_status` callers; `test` (opens as impl): the tests and the probes.

**State:** none.

**Constraints from prior phases:** Shutdown's checkpoint (2026-10-30): every resume runs in the session's own tab with `Herdr.run_in_pane` through `accounts.py exec <account>`, and `Herdr.tab_sessions` is the one pane reader. Ordinary and split restores keep conversation id, account environment, identity, tab label and position, and their tests show it.

**Acceptance gate:** basedpyright 0/0 on `herdr.py` and every caller module; launch tests cover both destinations, a ratio outside (0, 1) refused when the `SplitPane` is made, the label set before the agent starts, and a failed start closing its pane; `AgentStatus` decodes each state herdr reports, and an unknown one as `UNKNOWN`; probe results written; full gate green. A launch killed after the split is reconciled by `start-or-resume`, which holds the worker's key; its rerun test is in Phase 7.

### Phase 7 — worker_panes.py: start-or-resume, close, observe  · status: todo

**Depends on:** Phase 1, Phase 3, Phase 4, Phase 6

#### Work Order

**Goal:** `worker_panes.py start-or-resume <short name>` starts or resumes a worker in its director's tab with the registry's settings for the role in its name; `close` refuses a pane that still has work; `observe` is the one join of worker rows and herdr panes.

**Spec:**

1. **API and CLI.**
   ```python
   # worker_panes.py
   @dataclass(frozen=True)
   class OpenWorker:
       row: StaffRow
       pane: str                          # herdr pane id
       status: herdr.AgentStatus

   @dataclass(frozen=True)
   class ClosedWorker:
       row: StaffRow                      # no pane is open

   WorkerPane = OpenWorker | ClosedWorker

   @dataclass(frozen=True)
   class Closed: ...

   @dataclass(frozen=True)
   class Kept:
       blocker: str

   class ExitCode(IntEnum):
       OK = 0
       REFUSED = 2
       KEPT = 7          # Phase 8 adds the dispatch outcomes

   def start_or_resume(unit: StaffId, name: WorkerName) -> OpenWorker
   def close(unit: StaffId, name: WorkerName) -> Closed | Kept
   def observe(unit: StaffId, record: staff.StaffSnapshot) -> list[WorkerPane]
   ```
   The CLI is `worker_panes.py start-or-resume <short name>`, `close <short name>` and `list [--json]`, and `ExitCode` is its one table of exit codes. Each command addresses a worker by its short name within the caller's unit: the row named by the caller's `STAFF_ID` (its pane env, Phase 4), which must be a unit row; any other caller gets `REFUSED`. `close` gives `KEPT` with the blocker on stderr. `list` prints `observe` for that unit, with one `staff.snapshot()`.
2. **`start_or_resume`.** `staff.ensure("worker", parent=<unit id>, name=<short name>)` gives the row; two callers at once get one row (Phase 1 As-built). Then, under the worker lock `~/.local/state/staff/locks/<id>.lock`, held through pane reconciliation, the launch and the launch proof:
   - **Reconcile.** In the director's tab, find this worker's panes: a pane whose herdr agent name is the row's id, and a pane with the worker's label that runs no agent (a launch killed after the split, Phase 6). When one pane runs a live agent named with the id, it is the worker: report done and change nothing (model and effort are looked up only at start or resume; a `/model` typed by hand holds; decision 2). Close every other pane found, and only those.
   - **Start.** With no live pane left, split the column (first worker: the director pane, `right`, ratio 0.667; later ones: the lowest worker pane, `down`) through `launch_prepared_agent` with a `SplitPane` whose label is the short name. The herdr agent name is the row's id. The agent resumes with `--resume <id>` (Claude) or `codex resume <id>` when a conversation of that name exists, else starts a new session named with the id (Phase 6 probe).
   - **Settings.** Kind, model, effort, service tier and sandbox come from the registry row of the role in the name: `[direct.<role>]` for `impl`, `test`, `fix` and `review`, `[design_roles.<role>]` for `designer` and `reviewer`. One private function, `_registry_row(role)`, holds this mapping. The account comes from the accounts code as today (Codex: the `workers` role).
   - **Env.** The pane env carries `STAFF_ID=<id>`, `UNIT_RUN_DIR`, `UNIT_TEAM_ROLE=<role>`, `TMPDIR` and the dev shell.
   - Then `_layout`.
3. **`close`**, under the worker lock, passes only `can_close`: dispatch lock free; the last turn ends with the `DONE` line of its last dispatch (decision 4) and the launcher recorded that pass complete; no input after it; state `idle` or `done`; no live detached run of the worker. Otherwise it keeps the pane and names the blocker. It then closes the pane and runs `_layout`. A close that finds no pane runs `_layout` and reports done.
4. **`_layout`** (private): under a per-tab lock, set every split in the worker column to an even share. It changes only panes whose agent name is the id of a worker row whose parent is this tab's unit, leaves other panes as they are, and gives focus back where it was. Even shares already in place change nothing.
5. **`observe(unit, record)`**: `record.find(kind="worker", parent=unit)` (Phase 2 As-built) joined with `herdr agent list` on the agent name, with `AgentStatus` from Phase 6. A row with a live pane gives `OpenWorker(row, pane, status)`; a row with no pane gives `ClosedWorker(row)`, so no `None` carries the closed state. The caller takes `record` from `staff.snapshot()`, one read for all the units it observes. It is the one join: `list`, `close`, messages (Phase 9), shutdown (Phase 10), account moves (Phase 11) and every display (Phase 13) read it, and no other module joins staff rows with herdr panes.
6. A rename or a transfer of the unit or its showrunner changes no worker pane, agent or session (Phase 3).

**Files:**
- `scripts/herdr/worker_panes.py` — new.
- `scripts/herdr/test_worker_panes.py` — new.
- `scripts/production/staff.py`, `scripts/lib/agents_registry.py` — read only: the staff API and the registry rows (`models_registry.py` after Phase 5).

**Workers:** `2 writers` — `impl`: `worker_panes.py`; `test` (opens as impl): `test_worker_panes.py` against a probe tab.

**State:** staff rows; panes; `~/.local/state/staff/locks/<id>.lock`.

**Constraints from prior phases:** Phase 1 `staff.py` (`ensure`, `find`, `WorkerName`, `WorkerRole`, `StaffId`); Phase 2 `staff.snapshot() -> StaffSnapshot` (one record read, with `by_id`, `by_path` and `find`; it raises `StaffUnreadable`); Phase 3 unit rows kept current by the writers; Phase 4 `STAFF_ID` on every director pane; model, effort, tier and sandbox come from the models unit's registry API (`RegistryRow` lookups), called by module whatever the file is named (`agents_registry.py` before Phase 5, `models_registry.py` after); Phase 6 `SplitPane` with its label, `split_ratio`, `label_pane`, `AgentStatus` and the probe results.

**Acceptance gate:**
- basedpyright 0/0 on the new files.
- Tests: start; resume by id; the agent name is the id, the pane label the short name, and the settings the role's registry row (`impl1` → `[direct.impl]`, `fix1` → `[direct.fix]`, `designer1` → `[design_roles.designer]`); a caller whose `STAFF_ID` is not a unit row is refused; two `start-or-resume` calls at once for one name give one row and one pane; `close` refused with work left; a foreign pane untouched; `start_or_resume` returns an `OpenWorker`; `observe(unit, record)` returns an `OpenWorker` with its pane and status for each open worker and a `ClosedWorker` for a closed one, and reads its rows only from `record`.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_start_rerun_after_row_write`; `test_start_rerun_after_split` (the labeled pane with no agent is closed, and one pane opens); `test_start_rerun_after_agent_start` (the live agent is kept, and no second pane opens); `test_start_rerun_after_launch_proof` (on an open pane it reports done and changes nothing); `test_close_rerun_after_pane_close_before_layout` (the rerun gives the panes and ratios of one clean close); a rerun of a finished close reports done and changes nothing.
- Full gate green.

### Phase 8 — Dispatch and the launcher switch, landed dark  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** with `WORKER_TRANSPORT=herdr` pinned in a run, `implement.sh`, `review.sh` and `assign.sh` dispatch through `worker_panes.py dispatch`, whose receipt is the worker's transcript; the default stays `mesh`.

**Spec:**

1. **API and CLI.** `dispatch(unit: StaffId, name: WorkerName, prompt: Path, dispatch_id: str) -> ExitCode`; CLI `worker_panes.py dispatch <short name> <prompt path> <dispatch id>`. Phase 7's `ExitCode` gains `NO_DONE = 3`, `BLOCKED = 4`, `STALLED = 5` and `BUSY = 6`; `OK` means complete.
2. **The transcript is the receipt (decision 4).** `dispatch` keeps no record of its own of a submission. Its steps:
   1. Take the worker's dispatch lock (non-blocking; held → `BUSY`).
   2. `start_or_resume`, which gives an `OpenWorker`.
   3. Read the worker's own transcript:
      - a final reply that ends with `DONE <dispatch id>` → finish: write the reply file (for a reviewer, the findings file) and return `OK`; type nothing;
      - the typed line with this dispatch id and no such reply yet → the pass went in: go to the wait (step 5); type nothing;
      - no line with this dispatch id → the `OpenWorker`'s `status` must be `idle` or `done`, else `BUSY` (a rerun later finds the line when the turn was this pass); then type (step 4).
   4. Type one line, `herdr agent prompt <id> "Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>." --wait` (no timeout, no `--until`; decisions 5, 7). The prompt file is never changed.
   5. Wait. When herdr settles, read the transcript again: the `DONE <dispatch id>` reply → finish as in step 3; `blocked` → `BLOCKED`; settled without the line while `herdr agent get` says `working` → `herdr agent wait <id>` again; settled without the line → `NO_DONE`, and the director reads the pane; `agent_prompt_stalled` or a timeout → `STALLED`, and the line is never typed again.
3. **The launchers** keep their status, recorder, board and summary work, and write `implemented` only on `OK`. `unit_runs.worker_transport(run)` reads `WORKER_TRANSPORT` from `config/direct.conf` at run start and pins it in the run folder.
4. **Restart.** `inventory.py` keeps recording `launcher_command` (:861). A rebuilt launcher runs with the same dispatch id, so it reads the transcript first and types nothing when the line is already there (Phase 10).
5. Several implementers use `impl1`, `impl2`. Transport-aware text in `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<FixDispatch/>`, `<PhaseCleanup/>`) lands with this phase, branched once on the run's pin.

**Files:**
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — `dispatch` and its tests.
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/assign.py` — the launcher switch; `assign.sh` execs `assign.py`.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — `worker_transport(run)`.
- `config/direct.conf` — `WORKER_TRANSPORT=mesh`.
- `commands/unit/direct.md` — the transport-aware text.
- `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py`, `scripts/direct/test_assign_launcher.py` — the launcher tests.

**Workers:** `2 writers` — `impl`: `worker_panes.py`, `unit_runs.py`, `direct.conf`; hub: `worker_panes.py`. `test` (opens as impl): the three launchers, `direct.md`, launcher tests.

**State:** run pin file; dispatch locks. No submission record: the transcript holds it.

**Constraints from prior phases:** Phase 7 `start_or_resume(unit, name) -> OpenWorker` (`row`, `pane`, `status: herdr.AgentStatus`), `observe(unit, record) -> list[OpenWorker | ClosedWorker]`, `can_close`, `ExitCode`, locks. Every file write this phase adds or changes (the reply file, the findings file, the run pin) calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer).

**Acceptance gate:**
- basedpyright 0/0 on changed Python files and every caller module; `bash -n` and `shellcheck` with no new findings on the changed `.sh` files.
- Tests for long, fast, blocked, false-idle and stalled turns; two launchers at once get one dispatch and one `BUSY`; a mesh-pinned run is unchanged.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_dispatch_rerun_before_prompt` (stopped after the lock and the start, before the line is typed: the rerun types it once); `test_dispatch_rerun_after_prompt` (stopped after the line is typed: the rerun finds it in the transcript, waits for the same pass and types nothing); `test_dispatch_rerun_after_done` (stopped after the `DONE` reply and before the reply file: the rerun writes the reply file and types nothing); a rerun of a finished dispatch returns `OK` and changes nothing. After each, the prompt file is byte for byte the same.
- Full gate green.

### Phase 9 — Messages, detached runs and review Stage 2  · status: todo

**Depends on:** Phase 7, Phase 8

#### Work Order

**Goal:** messages, detached-run results and review Stage 2 reach a pane worker, and none is lost.

**Spec:** `send.py` `worker:<unit>/<short name>` is an address a person or a director types, so `send.py` takes one `staff.snapshot()` and resolves the address through its `by_path` (under the sender's showrunner; `worker:<showrunner>/<unit>/<short name>` from outside it) to the worker's id, its herdr agent name. An address that names no worker row is refused (`REFUSED`, exit 2), and nothing goes to the director. The worker comes from `worker_panes.observe(<unit id>, record)` (Phase 7) on the same snapshot: an `OpenWorker` gets the text with `herdr agent prompt <id> <text>`; `agent_blocked` or a `ClosedWorker` → the message goes to the director with one line saying why. `scripts/agents/mesh_runs.py` becomes `scripts/agents/detached_runs.py`: a run finds its worker by the `STAFF_ID` in its environment and types its result into the pane. The typed result carries the run id, and a delivery first reads the worker's transcript and types nothing when that line is already there (decision 4). `assign.py` types Stage 2 through `dispatch` with the dispatch id `<dispatch id>-2`, so its line, its `DONE <dispatch id>-2` reply and its rerun follow Phase 8, and the review launcher waits for that line. Usage refusals and settled-without-`DONE` turns go through one classifier, `refusal_watch.transcript_refusal` (`scripts/accounts/refusal_watch.py:464`), which `settle.py` already calls (:121): it gains Codex transcripts and pane screen text as the fallback (transcript evidence first); the pass status says `refused`, the pass stays open, and the director re-dispatches it after the move.

**Files:**
- `scripts/message/send.py`, `scripts/message/test_send.py` — `worker:` addresses.
- `scripts/agents/mesh_runs.py`, `scripts/agents/test_mesh_runs.py` — become `scripts/agents/detached_runs.py` and its test; every `mesh_runs` reference follows in the same commit.
- `scripts/direct/assign.py`, `scripts/direct/test_assign_launcher.py` — Stage 2 through `dispatch`.
- `scripts/accounts/refusal_watch.py`, `scripts/accounts/test_refusal_watch.py` — the one classifier: `transcript_refusal` (:464) gains Codex transcripts and screen text as the fallback; agreed with the accounts unit.
- `scripts/herdr/worker_panes.py`, `scripts/production/staff.py`, `scripts/lib/atomic_file.py` — read only: `observe`, `snapshot` and the one file writer.

**Workers:** `2 writers` — `impl`: `send.py`, `detached_runs.py`; `test` (opens as impl): `assign.py`, the classifier, tests.

**State:** none new.

**Constraints from prior phases:** Phase 2 `staff.snapshot()`; Phase 7 `observe(unit, record) -> list[OpenWorker | ClosedWorker]`; Phase 8 `dispatch`, its transcript receipt and `ExitCode`. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). `send.py` has two own writers, `RELAY_EXEC` (:212, its `os.replace` at :219) and `write_atomic` (:589, its `os.replace` at :592); a write this phase adds goes through `atomic_file`, not through them.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; tests: a message during a turn arrives; a blocked or closed worker's message reaches the director; a `worker:` address that names no row is refused with exit 2 and reaches no one; Stage 2 waits for its own line; rerun tests (Invariants → Rerun-safe tools): `test_delivery_rerun_after_type` (a detached run's delivery stopped after it types its result, then run again, types nothing), a delivery run twice types its result once, and a Stage 2 rerun after its line types nothing; full gate green.

### Phase 10 — Shutdown and restore of split tabs (shutdown unit)  · status: todo

**Depends on:** Phase 4, Phase 6, Phase 7, Phase 8, Phase 9

#### Work Order

**Goal:** a shutdown stops worker panes inside the bounded deadline, and a restart brings each director tab back with its workers resumed.

**Spec:**

1. **Shutdown.** Before the voluntary window, the shutdown record lists each worker pane from `worker_panes.observe(<unit id>, record)` (Phase 7), with `record` from one `staff.snapshot()` for the whole shutdown: for each `OpenWorker`, its staff id, pane, pid and start time, scope, run, and the dispatch id and `launcher_command` when busy. A `ClosedWorker` has no pane to list. It stores no name: a reader takes the name from the row. `BoundedOwner` holds the worker panes under their director, and success needs a fresh observation that they are gone. A rerun of a stopped shutdown reads the record, stops the panes still alive, and writes no second entry for a pane already listed.
2. **Restore** follows Phase 6's probe (questions list item 1(e)). If herdr's own resume keeps each pane's env, herdr brings the panes back and our code builds no pane. Otherwise one locked tab transaction rebuilds the director (its name from its row, Phase 4), then each worker with `start-or-resume`.
3. **One launcher step ends both branches.** After the panes settle, for each worker the record lists as busy, restore runs its recorded `launcher_command` once with the same dispatch id. `dispatch` reads the transcript first (Phase 8), so the one launcher finishes a pass that ended, waits on a live one, or types the line once when it never went in. A busy worker's resume text names its dispatch id, so the reply that finishes the pass ends with the same `DONE <dispatch id>` line.
4. Resume texts come from one builder, `worker_resume_prompt(cause, was_busy, dispatch_id)` in `scripts/shutdown/stop.py`, where `RESUME_PROMPT` and `IDLE_RESUME_PROMPT` live today (:332-339), used by shutdown, moves and launchers. Tab sort sorts a multi-pane tab by its director pane (`herdr.py:902-1300`).
5. **A unit's workers, stopped for good.** `stop.stop_unit_workers(unit: StaffId, record: staff.StaffSnapshot) -> list[str]` in `scripts/shutdown/stop.py`: for each worker row of `observe(unit, record)`, it finds the pane from the row, stops it, gets a fresh observation that the pane and its process are gone, and then calls `staff.retire(<worker id>)`. It returns one line for each worker that is still alive, and that worker keeps its row. It needs no shutdown record and no live director, and it finds a Codex worker by its row, also when the worker runs outside the unit's worktree. A rerun finds only the rows still there, so each worker is stopped and retired once.
   - `retire_unit.py` `stop_rest` (:632-636) calls it in place of `workers.end_unit` (:636). A mesh-pinned run (`unit_runs.worker_transport(run)`) keeps `end_unit` until Phase 18.
   - `production_lifecycle.py` `wrap` calls it for each unit before the unit's staff step, and `promote_showrunner.py` calls it for the old unit before its `retire`.
   - So Phase 3's rule (a unit row with worker rows stays, exit 1) applies only to a worker that is still alive.

**Files:**
- `scripts/shutdown/inventory.py`, `scripts/shutdown/stop.py`, `scripts/shutdown/shutdown_record.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/pane_restore.py` — stop, record, restart and restore; `worker_resume_prompt` replaces `RESUME_PROMPT` and `IDLE_RESUME_PROMPT` in `stop.py` (:332-339); `stop_unit_workers` (new) in `stop.py`.
- `scripts/production/retire_unit.py`, `scripts/production/production_lifecycle.py`, `scripts/production/promote_showrunner.py` — call `stop_unit_workers`.
- `scripts/production/test_retire_unit.py`, `scripts/production/test_production_lifecycle.py`, `scripts/production/test_promote_showrunner.py` — their tests.
- `scripts/lib/unit_runs.py`, `scripts/production/staff.py`, `scripts/herdr/worker_panes.py`, `scripts/lib/atomic_file.py` — read only.
- `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_stop.py`, `scripts/shutdown/test_bounded_stop.py`, `scripts/shutdown/test_shutdown_record.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_pane_restore.py` — the tests.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — tab sort (`sort_tabs` :1287).

**Workers:** `2 writers` — split by stop and restore.
- `impl` — stop and record (`inventory.py`, `stop.py`, `shutdown_record.py`), `stop_unit_workers` and its callers (`retire_unit.py`, `production_lifecycle.py`, `promote_showrunner.py`), and their tests; hub: `scripts/shutdown/stop.py` (`stop_unit_workers` and `worker_resume_prompt`, which the callers and restore use).
- `test` (opens as impl) — restart, restore, tab sort (`restart.py`, `pane_restore.py`, `herdr.py`) and their tests.

**State:** shutdown records; worker rows that `stop_unit_workers` retires.

**Constraints from prior phases:** Phase 2 `staff.snapshot()`; Phase 3 (`retire_unit.py`, promote and wrap keep a unit row with worker rows, print them and exit 1); Phase 4 (names from rows; `STAFF_ID` in `KEPT_NAMES`); Phase 6 (the restore probe); Phase 7 (`start_or_resume(unit, name) -> OpenWorker`, `observe(unit, record) -> list[OpenWorker | ClosedWorker]`); Phase 8 (`dispatch` reads the transcript first); Phase 9 (a busy review Stage 2 pass has the dispatch id `<dispatch id>-2`, and `detached_runs.py` finds its worker by `STAFF_ID` and reads the transcript before it types, so both reach a restored pane). Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). Phase 3's pending retirement (ids a wrap could not retire while worker rows were left) lives in the staff record; shutdown and restore keep it unchanged. `shutdown_record.py` `_write_record` (:1042) makes its own temp file (`mkstemp` at :1043) and `os.replace` (:1053); this phase changes the shutdown record, so `_write_record` calls `atomic_file.write_text_atomic` instead. Shutdown's checkpoint (2026-10-30): every resume runs in the session's own tab with `Herdr.run_in_pane` through `accounts.py exec <account>`, and `Herdr.tab_sessions` is the one pane reader. Ordinary and split restores keep conversation id, account environment, identity, tab label and position, and their tests show it.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Shutdown tests with worker panes inside 66 s; restore tests for both probe branches; a restored director takes its row's name.
- The worker stop test: a crashed director, no shutdown record, and a Codex worker whose working folder is outside the unit's worktree; `retire_unit.py` stops the worker through `stop_unit_workers`, proves the pane and the process gone, retires the worker row, and then retires the unit row. A mesh-pinned run still calls `workers.end_unit`. `wrap` and promote stop each unit's workers the same way before their staff step.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_shutdown_rerun_after_first_worker_stop` (the rerun stops the rest and the record lists each pane once); `test_restart_rerun_after_director_tab` and `test_restart_rerun_after_first_worker` (one set of panes); `test_restore_rerun_after_agent_resume` (stopped after a busy worker's agent resumes and before its launcher starts: the rerun starts one launcher, which types nothing when the line is in the transcript); `test_stop_unit_workers_rerun_after_pane_stop` (stopped after a pane stops and before its `retire`: the rerun proves it gone, retires its row once, and stops the rest); a rerun of a finished restart reports done and changes nothing.
- Full gate green.

### Phase 11 — Account moves for pane workers (accounts unit)  · status: todo

**Depends on:** Phase 7, Phase 8, Phase 9, Phase 10

#### Work Order

**Goal:** an account move relaunches each worker pane on the new account without a lost or forked conversation.

**Spec:** Each worker move has a move id and one entry in the moves log, keyed by staff id and move id, that names its last finished step. Under the worker's dispatch lock:
1. Wait for or interrupt the turn; close the pane; prove the process gone with a fresh observation.
2. Read each conversation file from the source home, with its size and hash.
3. Publish each file in the target home with `atomic_file.write_bytes_atomic(path, data)`, then check the published file's size and hash against the source. The move makes no staging folder and calls no `os.replace` of its own. A file is never seen half copied.
4. `start-or-resume` on the new account, check that the id resumes, and record success.
On failure, before it resumes the worker on the source account, the move proves with a fresh observation that no process on the target owns the conversation; then it resumes once on the source and reports. A rerun goes on from the last finished step: a published file whose size and hash match the source is not written again, and a target that already runs the conversation is kept, so no source starts beside it. A mesh-pinned run keeps `_retire_workers_servers` and `leftover_server_lines` (`codex_mesh.py:7989`) until cleanup (Reference → Cleanup list: accounts' server lines); the move reads each run's pin through `unit_runs.worker_transport(run)`.

**The bytes writer.** In `scripts/lib/atomic_file.py`, `write_bytes_atomic(path: Path, data: bytes) -> None` writes bytes as `write_text_atomic` writes text. The two share one private publish step: the temp file `.<name>.<random>` beside the target, `fsync`, the mode, `os.replace` and `sync_folder`. `write_text_atomic` encodes its text and calls that step, so the steps have one copy. `atomic_file.py` is the unit-board unit's file: agree the change with that unit first (Delegation Context → One file writer).

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py` — the worker move; `_retire_workers_servers` (:3517).
- `scripts/accounts/next_moves.py`, `scripts/accounts/test_next_moves.py`, `scripts/whoami/quota_alert.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py` — the move callers.
- `scripts/lib/atomic_file.py`, `scripts/lib/test_atomic_file.py` — `write_bytes_atomic` and the one publish step that it shares with `write_text_atomic`; the unit-board unit's files.
- `scripts/herdr/worker_panes.py`, `scripts/shutdown/stop.py` — read only: `start_or_resume` and `worker_resume_prompt`.

**Workers:** `2 writers` — `impl`: accounts code and `atomic_file.py`; hub: `scripts/lib/atomic_file.py` (`write_bytes_atomic`, which the move calls). `test` (opens as impl): tests, `test_atomic_file.py` included.

**State:** account homes; moves log.

**Constraints from prior phases:** Phases 7-9; Phase 7 `start_or_resume(unit, name) -> OpenWorker` and `observe(unit, record) -> list[OpenWorker | ClosedWorker]`; Phase 10 `worker_resume_prompt(cause, was_busy, dispatch_id)` in `scripts/shutdown/stop.py`, the one resume-text builder, and its fresh observation that a pane is gone. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`, `write_bytes_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). `accounts.py` `_atomic_write` (:2464, its `os.replace` at :2478) is an own writer; the worker move does not call it.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Move tests: busy, idle, and the failure fallback, which starts no source while a target process owns the conversation.
- `test_atomic_file.py`: `write_bytes_atomic` publishes the bytes and keeps the target's mode as `write_text_atomic` does; a write stopped before its `os.replace` leaves the target as it was.
- Rerun tests (Invariants → Rerun-safe tools), each ending on the new account with one pane and one conversation, as one clean move: `test_move_rerun_mid_copy` (stopped inside a `write_bytes_atomic` call: no partial file is published; the rerun writes the file once); `test_move_rerun_after_publish` (stopped before the target starts); `test_move_rerun_after_target_resume` (stopped before the success record: the rerun keeps the target and starts no source); a rerun of a finished move reports done and changes nothing.
- Full gate green.

### Phase 12 — Astra and ask-a-friend in panes  · status: todo

**Depends on:** Phase 1, Phase 6, Phase 7, Phase 8, Phase 9

#### Work Order

**Goal:** Astra roles and ask-a-friend run in panes: a unit director's Astra role is a worker under its unit, and any other caller's Astra role or friend is a pane beside the caller that is not staff.

**Spec:**
1. **Astra roles.** A held Astra role that a unit director asks for is a worker under its unit, `designer1` or `reviewer1`, started with `start-or-resume`, which reads the `[design_roles.<role>]` row and its sandbox (Phase 7). `design_roles.py` keeps its fences and loses its mesh calls. A worker's parent is always a unit (Phase 1 As-built), so an Astra role that any other caller asks for (a showrunner, an ad hoc session) is a pane beside the caller that is not staff, as ask-a-friend is.
2. **Ask-a-friend is not staff** (Phase 1 As-built). It opens a pane beside the caller through `scripts/herdr/side_pane.py` (new; `open <label>` and `close <label>`), which calls `launch_prepared_agent` with a `SplitPane` and the `[ask_a_friend.consultation]` row, writes no staff row, and closes its pane at `end`. A rerun of an open stopped after the split closes the labeled pane with no agent and opens one; a rerun of `end` finds no pane and reports done.
3. The command text that starts them (`commands/ask_a_friend.md`, `commands/astra_review.md`) changes in Phase 14.

**Files:**
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py` — Astra roles.
- `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py` — new: the one pane beside a caller that is not staff, for ask-a-friend and for an Astra role from a caller that is not a unit director.

**Workers:** `2 writers` — split by module.
- `impl` — `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py`; hub: `scripts/herdr/side_pane.py` (`design_roles.py` calls it for a caller that is not a unit director).
- `test` (opens as impl) — `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py`.

**State:** panes; a unit's Astra role is a worker row through `start-or-resume`; a side pane writes no row.

**Constraints from prior phases:** Phases 7-9; Phase 1 `WorkerRole` (`designer`, `reviewer`); Phase 6 `SplitPane`; Phase 7 `start_or_resume(unit, name) -> OpenWorker`, and `observe(unit, record)` gives `OpenWorker(row, pane, status)` or `ClosedWorker(row)` for each worker row, with `record` from one `staff.snapshot()` (Phase 2).

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Tests for each changed module; `designer1` and `reviewer1` under a unit read their `[design_roles.*]` rows and sandboxes; an Astra role or ask-a-friend asked for by a showrunner opens a pane and makes no staff row.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_astra_open_rerun_after_split` (one pane); `test_friend_open_rerun_after_split` (one pane); `test_friend_end_rerun_after_close` (no pane, reports done); each open and close run twice gives one pane and then none.
- Full gate green.

### Phase 13 — Displays read workers through observe  · status: todo

**Depends on:** Phase 7, Phase 8, Phase 9

#### Work Order

**Goal:** every display reads workers through `worker_panes.observe`, the one join of worker rows and herdr panes.

**Spec:**
1. **Displays.** `roster.py`, `production_tree.py`, `codex_winddown.py`, `waste_watch.py`, `hierarchies/worker_owners.py`, `hierarchies/measure.py` and `board_json.py` read workers from `worker_panes.observe(unit, record)` (Phase 7), the one join; none of them joins staff rows with `herdr agent list` itself. An `OpenWorker` shows its pane and status; a `ClosedWorker` shows as closed.
2. **One snapshot per run.** Each display takes one `staff.snapshot()` per run and passes it to every `member` and `observe` call.
3. **Problems are shown, never an empty list.** When `snapshot()` raises `StaffUnreadable` (natedev not reached, or a malformed record), the display shows the reason as a problem line and shows no empty worker list in its place: `roster.py` adds it to `Roster.problems` (`roster.py:386`, through `_problem` :835), `production_tree.py` prints it with `_warn` (:84), and each other display prints it on stderr. A member that gives `NoSuchMember` gets its own problem line, which names the member.

**Files:**
- `scripts/message/roster.py`, `scripts/production/production_tree.py`, `scripts/production/codex_winddown.py`, `scripts/usage/waste_watch.py`, `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/measure.py`, `scripts/direct/board_json.py` — the displays.
- `scripts/message/test_roster.py`, `scripts/production/test_production_tree.py`, `scripts/production/test_codex_winddown.py`, `scripts/usage/test_waste_watch.py`, `scripts/hierarchies/test_worker_owners.py`, `scripts/hierarchies/test_measure.py`, `scripts/direct/test_board_json.py` — their tests.
- `scripts/herdr/worker_panes.py`, `scripts/production/staff.py`, `scripts/production/staff_sources.py` — read only: `observe` (Phase 7), `snapshot` and `member` (Phase 2).

**Workers:** `2 writers` — split by module group.
- `impl` — `roster.py`, `production_tree.py`, `codex_winddown.py`, `board_json.py` and their tests.
- `test` (opens as impl) — `waste_watch.py`, `hierarchies/worker_owners.py`, `hierarchies/measure.py` and their tests.

**State:** none.

**Constraints from prior phases:** Phases 7-9; Phase 7 `worker_panes.observe(unit, record)` returns an `OpenWorker(row, pane, status: herdr.AgentStatus)` for each open worker and a `ClosedWorker(row)` for a closed one; Phase 2 `staff.snapshot() -> StaffSnapshot` is one record read and raises `StaffUnreadable`, and `staff_sources.member(selector, record)` gives `MemberFound | NoSuchMember`. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Tests for each changed module; no display module calls `herdr agent list` outside `worker_panes.observe`.
- Problem tests for each display: an unreachable owner (the fake `ssh` fails), a malformed record and a missing member each give a problem line that names the cause, and no empty worker list; a run makes one `snapshot()` call (on the Mac, one call in the fake `ssh` log).
- No rerun test: displays only read (Invariants → Rerun-safe tools).
- Full gate green.

### Phase 14 — Instructions describe panes  · status: todo

**Depends on:** Phase 10, Phase 11, Phase 12, Phase 13

#### Work Order

**Goal:** every instruction file describes panes, and mesh text stays only inside a mesh-pinned branch.

**Spec:**
1. Instruction edits: Reference → Instruction edits. The text describes the code that Phases 10-13 built (Invariants: docs match current reality).

**Files:**
- `commands/unit/direct.md`, `docs/direct/write_prompt_contract.md`, `docs/direct/launch_implementation.md`, `docs/direct/dual_review.md`, `docs/direct/phase_end.md`, `docs/direct/run_phase_review.md`, `commands/unit/report.md`, `commands/showrunner/produce.md`, `commands/showrunner/accounts.md`, `commands/showrunner/rename_unit.md`, `commands/showrunner/retire_unit.md`, `commands/showrunner/transfer_unit.md`, `commands/message.md`, `commands/codex_winddown.md`, `commands/shutdown.md`, `commands/ask_a_friend.md`, `commands/astra_review.md`, `commands/shot_report.md`, `docs/quota_alerts.md`, `docs/production_format.md`, `docs/accounts.md` — Reference → Instruction edits.

**Workers:** `2 writers` — split by file group.
- `impl` — `commands/unit/direct.md` and `docs/direct/*`.
- `test` (opens as impl) — the other commands and docs.

**State:** none.

**Constraints from prior phases:** Phase 8 landed the transport-aware text in `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<FixDispatch/>`, `<PhaseCleanup/>`), branched once on the run's pin. Phase 9 `send.py worker:<unit>/<short name>` and `detached_runs.py`; Phase 10 shutdown and restore of split tabs; Phase 11 account moves of pane workers; Phase 12 Astra roles in panes and `side_pane.py open|close` for ask-a-friend; Phase 13 displays read workers through `observe`.

**Acceptance gate:** No instruction file names a mesh verb outside a mesh-pinned branch; a final search for mesh paths outside history finds none; full gate green. No basedpyright: no Python changes. No rerun test: text edits, not a tool.

### Phase 15 — Pilot and default switch  · status: todo

**Depends on:** Phase 3, Phase 4, Phase 6, Phase 7, Phase 8, Phase 9, Phase 10, Phase 11, Phase 12, Phase 13, Phase 14

#### Work Order

**Goal:** herdr-unit's own next run works on panes, then the default becomes `herdr` and units switch at their run boundaries, Mac units after a Mac probe of Phases 6-7.

**Spec:** Pilot one run; record any defect as a repair before the switch. Then set `WORKER_TRANSPORT=herdr` in `config/direct.conf`. Needs questions list items 1(c) and 1(e) answered.

**Files:**
- `config/direct.conf` — `WORKER_TRANSPORT=herdr`.

**Workers:** `1 writer` — one line; the pilot is the director's.

**State:** `config/direct.conf`.

**Constraints from prior phases:** Phases 3, 4 and 6-14.

**Acceptance gate:** the pilot run's phases finish on panes; Mac probe passes; full gate green.

### Phase 16 — Parent files and the seed importer removed  · status: todo

**Depends on:** Phase 2, Phase 3, Phase 3g

#### Work Order

**Goal:** no code reads or writes a showrunner parent file, and the seed importer is gone by 2026-11-13. Phase 3c already moved the tree readers.

**Spec:**
1. **Parent files.** Remove what remains: `showrunner_parent.py`'s parent-file operations, the parent-file moves in `rename.py`, and `showrunner_tree.release_edges`. Wrap keeps telling each former parent and child from its `wrap_release.json` capture.
2. **Importer, due 2026-11-13.** Remove the `staff_sources` `seed` entry point, `_collect`, `_print_plan` and `_seed_member`, and the old-pane fallback from Phase 3g. `member`, `showrunner_members` and `unit_parent` stay.

**Files:**
- `scripts/production/showrunner_parent.py`
- `scripts/production/test_showrunner_parent.py`
- `scripts/production/showrunner_tree.py`
- `scripts/production/test_showrunner_tree.py`
- `scripts/production/rename.py`
- `scripts/production/test_rename.py`
- `scripts/production/production_lifecycle.py`
- `scripts/production/test_production_lifecycle.py`
- `scripts/production/staff_sources.py`
- `scripts/production/test_staff_sources.py`

**Workers:** `1 writer + 1 tester` — `impl`: parent-file removal, lifecycle integration, importer removal. `test`: attach, rename and wrap regressions through the shared staff fixture.

**State:** parent files are no longer written or read.

**Constraints from prior phases:** Phase 3c `showrunner_tree` owns every tree reader. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.

**Acceptance gate:** basedpyright 0/0 on changed files and callers; attach, detach, rename and wrap tests run with no parent-state folder and make no parent file; a wrap stopped after its showrunner row's `retire`, then run again, tells each former parent and child once from `wrap_release.json`; `member` and `unit_parent` still resolve; a search finds no `seed` entry point; full gate green.

### Phase 17 — Unit and joined readers read the record  · status: todo

**Depends on:** Phase 4, Phase 16, Phase 3i

#### Work Order

**Goal:** every unit reader and joined reader that batches 3g to 3i did not move reads who exists and who reports to whom from the staff record; live state still comes from herdr and session records.

**Spec:**
1. **Readers.** Move each reader in Reference → Staff record readers → Units and Joined readers to one `staff.snapshot()` per run and its `find` and `by_id`, for who exists and who reports to whom only (decision 16). `StaffUnreadable` is a problem line that names the cause, never an empty list: `roster.py` adds it to `Roster.problems` (`roster.py:386`, through `_problem` :835), and each other reader prints it on stderr. A member that gives `NoSuchMember` gets its own problem line, which names the member. It keeps reading timer fields (session, socket, zone, doc) from the timer, and Units-table cells from the doc, keyed by the row. The readers under Not moved do not change. A reader that starts from a production slug or a Units-table name finds its row with `staff_sources.member` (Phase 2 As-built), and a `showrunners.py` function that works on rows takes the row or the `StaffSnapshot` from its caller, because `showrunners.py` never imports `staff_sources` (decision 15); `by_path` serves only a name a person types. Readers on the Mac get both machines' rows through the one file on natedev.
2. Pane marks stay as the live link from a Units row to its unit's pane (`member` reads them).

**Files:**
- `scripts/production/broadcast.py`, `scripts/production/codex_winddown.py`, `scripts/production/dailies_input.py`, `scripts/production/production_lifecycle.py`, `scripts/production/rename_unit.py`, `scripts/production/unit_effort.py`, `scripts/production/waiting.py` — the production readers in Reference → Staff record readers.
- `scripts/message/roster.py`, `scripts/shutdown/inventory.py`, `scripts/accounts/unit_watchdog.py`, `scripts/hierarchies/measure.py`, `scripts/nightly_review/nightly_review.py`, `scripts/style/style_plan.py` — the other readers; `inventory.py` is the shutdown unit's file and `unit_watchdog.py` the accounts unit's (Invariants → Owners).
- `scripts/message/test_roster.py`, `scripts/production/test_broadcast.py`, `scripts/shutdown/test_inventory.py`, `scripts/production/test_codex_winddown.py`, `scripts/production/test_dailies_input.py`, `scripts/production/test_production_lifecycle.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_unit_effort.py`, `scripts/production/test_waiting.py`, `scripts/accounts/test_unit_watchdog.py`, `scripts/hierarchies/test_measure.py`, `scripts/nightly_review/test_nightly_review.py`, `scripts/style/test_style_plan.py` — the readers' tests.
- `scripts/production/staff.py`, `scripts/production/staff_sources.py` — read only: `snapshot` and `member` (Phase 2).

**Workers:** `2 writers` — split by tree.
- `impl` — the `scripts/production/` readers and their tests.
- `test` (opens as impl) — the other readers and their tests.

**State:** none.

**Constraints from prior phases:** Phase 1 `staff.find`, `by_id`, `by_path`; Phase 2 `staff.snapshot() -> StaffSnapshot` (one record read; it raises `StaffUnreadable`, also for a malformed record) and `staff_sources.member(selector, record) -> MemberFound | NoSuchMember`, by `STAFF_ID` first, then by name; Phase 4 every unit and showrunner pane carries its `STAFF_ID`, so `member` matches by id; Phase 16 moved the showrunner tree (`parent_of`, `children_of`, `tree_lines`, `production_tree.py`) to the rows, and `production_lifecycle.py` `refuse_running_children` calls `children_of`. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot. Phases 3g to 3i moved `live_units`, `stall_watch`, `board_json`, `waste_watch` and `unit_lookup`; Reference's old exclusion of seed-source readers no longer applies to `unit_lookup`; `production_tree.unit_edges` already reads row parents.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; each moved reader's tests green; problem tests: an unreachable owner (the fake `ssh` fails), a malformed record and a missing member each give a problem line that names the cause, and no empty list; a run makes one `snapshot()` call; full gate green. No rerun test: these readers write nothing (Invariants → Rerun-safe tools).

### Phase 18 — Cleanup  · status: todo

**Depends on:** Phase 15, Phase 16, Phase 17

#### Work Order

**Goal:** the mesh, the review pool, the old worker records and `WORKER_TRANSPORT` are gone, no later than 7 days after the default switch.

**Spec:** Run only after a check that no run folder pins `mesh` and no mesh process is alive. The check cannot go stale: after Phase 15 the default is `herdr` and no tool writes `mesh`, so no new run pins `mesh` and no mesh process starts. Delete: Reference → Cleanup list. The launchers (`implement.sh`, `review.sh`, `assign.py`) lose their mesh branch and their `WORKER_TRANSPORT` switch. In `scripts/lib/unit_runs.py`, `worker_transport`, the run pin and the run `workers` log reader go. Phase 19 merges the launchers.

**Files:**
- `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py` — deleted.
- `scripts/direct/review_pool.py`, `scripts/direct/review_pool_report.py`, `scripts/direct/test_review_pool.py`, `scripts/direct/test_review_pool_report.py`, `scripts/direct/test_review_launcher_pool.py` — deleted.
- `scripts/direct/workers.py`, `scripts/direct/worker_state.py`, `scripts/direct/remove_workers.py`, `scripts/production/retire_unit.py` — the mesh parts and the unit worker ledger go; in `retire_unit.py` `stop_rest` (:632-636) the mesh-pinned `workers.end_unit` branch goes.
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/assign.py` — the mesh branch and the `WORKER_TRANSPORT` switch go.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — `worker_transport`, the run pin and the run `workers` log reader (`_claude_worker` :134-150) go.
- `scripts/ask_a_friend/launch_friend.sh`, `scripts/ask_a_friend/end_friend.sh` — deleted.
- `config/agents.conf`, `config/direct.conf` — `[direct.options] codex_mesh`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, `WORKER_TRANSPORT` go (`config/models.conf` after Phase 5).
- `scripts/shutdown/inventory.py`, `scripts/shutdown/stop.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/session_types.py` — `CodexServer`, `codex_seats`, `PausedSeat`, `_CODEX_HEADLESS`, mesh pause.
- `scripts/accounts/accounts.py` — server lines.
- `scripts/production/production_tree.py`, `scripts/message/process_table.py`, `scripts/usage/waste_watch.py`, `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/measure.py`, `scripts/shot_report/transcripts.py`, `scripts/lint/tmp_sweep.py` — display mesh code.
- `docs/as-built/long-lived-workers.md`, `docs/as-built/review-pool.md`, `docs/as-built/review-pool-probes.md`, `docs/as-built/herdr-adoption.md`, `docs/as-built/session-roster.md`, `docs/as-built/agent-registry.md`, `docs/as-built/shutdown-and-restart.md`, `docs/as-built/unit-board.md`, `docs/as-built/usage-ledger.md` — Reference → Cleanup list docs.

**Workers:** `2 writers` — `impl`: code; `test` (opens as impl): docs and tests.

**State:** deletes state on disk (listed).

**Constraints from prior phases:** Phase 15 set the default to `herdr`, so no new run pins `mesh`; Phases 16-17 moved every reader and parent-file writer to the record. Phases 1-4 and 6-14 are done through those three. Phase 5 is not needed: cleanup edits whichever registry file name is current. Phase 10 kept `workers.end_unit` in `retire_unit.py` `stop_rest` for a mesh-pinned run only; that branch goes here, and `stop.stop_unit_workers` is then the one path that stops a unit's workers.

**Acceptance gate:** `grep` finds no live `codex_mesh` or `WORKER_TRANSPORT` reference; nothing in Reference → Cleanup list is left; basedpyright 0/0 on changed files and every caller module; full gate green. No rerun test: the cleanup is one commit and a one-time deletion, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

### Phase 19 — One launcher  · status: todo

**Depends on:** Phase 18

#### Work Order

**Goal:** one launcher, `scripts/direct/launch.py`, holds the work of `implement.sh`, `review.sh` and `assign.sh`/`assign.py`, and the three command names stay as thin entry points for their arguments.

**Spec:** Merge `implement.sh`, `review.sh` and `assign.sh`/`assign.py` into one launcher, `scripts/direct/launch.py`, keeping the three command names as thin entry points for their arguments (decision 11). After Phase 18 each launcher holds only its arguments, its recorder, status-file and board work, and its call to `worker_panes.py dispatch`; `launch.py` holds that work once.

**Files:**
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/assign.sh`, `scripts/direct/assign.py` — thin entry points for their arguments.
- `scripts/direct/launch.py` — new: the one launcher.
- `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py`, `scripts/direct/test_assign_launcher.py` — the launcher tests.

**Workers:** `2 writers` — split by file group.
- `impl` — `scripts/direct/launch.py`; hub: `scripts/direct/launch.py` (the entry points call it).
- `test` (opens as impl) — the three entry points and the launcher tests.

**State:** none new.

**Constraints from prior phases:** Phase 18 removed the mesh branch and the `WORKER_TRANSPORT` switch from the launchers, and `unit_runs.worker_transport`; Phase 8 put start, dispatch, wait and receipt in `worker_panes.py dispatch` (decision 11); Phase 9 `assign.py` types review Stage 2 through `dispatch` with the dispatch id `<dispatch id>-2`. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer).

**Acceptance gate:** basedpyright 0/0 on `launch.py`, `assign.py` and every caller module; `bash -n` and `shellcheck` with no new findings on the changed `.sh` files; the launcher tests green; full gate green. No rerun test: the merge is one commit, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

## Reference

### Job table (main at 7e85a74b)

| # | Job today (codex_mesh.py unless named) | After |
| --- | --- | --- |
| 1 | App-server per run (`serve`, `idle-stop`, `sweep`) | Drop |
| 2-3 | `start`, `follow` (:3308, :3408) | `start-or-resume` + `dispatch` (Phases 7-8) |
| 4, 15 | Follow and slot claims (:3762; `impl_claim_*.json`) | Per-worker dispatch lock |
| 5-6 | Compaction, "Continue where you stopped." | Drop (decision 8) |
| 7 | Server retry | A failed start closes its pane and tries once in a new pane |
| 8 | `signin-changed` (:7673) | Phase 6 probe; positive → drop, else resume restart |
| 9 | `send`, `pending.json` | Decision 6 (Phase 9) |
| 10-11 | `steer`/`interrupt`, `end` | `herdr agent send-keys <id> esc`; `close` |
| 12 | `stop --pause` | Shutdown stops panes (Phase 10) |
| 13-14 | `reopen`/`adopt`; unit worker ledger (workers.py:422-545) | `start-or-resume`; the staff record |
| 16 | Pass recording in launchers | Keep |
| 17 | `mesh_runs.py` | `detached_runs.py` (Phase 9) |
| 18 | Wait markers | Drop markers; recorder waits stay |
| 19-20 | Usage limits; account moves | Phases 9 and 11 |
| 21 | Model per turn (710a6adb) | Lookup at start or resume only; a `/model` by hand holds (decision 2) |
| 22-23 | Review pool; unit reviewers | Drop the pool; reviewers are panes |
| 24-25 | Astra roles; ask-a-friend | Panes (Phase 12) |
| 26 | `board.log` | Keep; `reach=herdr` |
| 27, 29 | Roster; displays | `worker_panes.observe`: the staff record joined with `herdr agent list` (Phases 7, 13) |
| 28 | Shutdown and restart of workers | Phase 10 |

### Staff record readers (showrunner tree in Phase 16, units and joined readers in Phase 17; workers in Phases 8-14)

- Showrunner tree: `showrunners.py:427, 432, 491` (`parent_of`, `children_of`, `tree_lines`); the parent-file reads `showrunners.py:398, 415, 576` (`_stored_parent`, `edges`, `stored_children`) and their caller `production_lifecycle.py:350` (`refuse_running_children`), which go with the parent files (Phase 16 item 2); `production_tree.py:127, 287` (`unit_edges`, `tree`).
- Units: `live_units.py:32`.
- Workers: `workers.py:529, 1564`; `remove_workers.py:65`; `assign.py:604`; `hierarchies/worker_owners.py:493, 606, 642`; `retire_unit.py:292`; `production_tree.py:156, 169, 231, 256`; the worker and mesh sources `message/roster.py:1123, 1185, 1344, 1694`, `shutdown/inventory.py:908` (`codex_seats`) and `production/rename.py:528` (`_rename_unit_workers`), which move with the worker readers or go at cleanup.
- Joined readers: `message/roster.py:906, 958, 1050`; `production/broadcast.py:105-129` (through roster); `direct/board_json.py:371, 427`; `shutdown/inventory.py:422, 427, 1693`; `production/codex_winddown.py:290, 304, 332`; `dailies_input.py:300`; `production_lifecycle.py:173`; `rename_unit.py:240`; `stall_watch.py:755, 771`; `unit_effort.py:206`; `waiting.py:153`; `accounts/unit_watchdog.py:335`; `hierarchies/measure.py:1413-1416`; `nightly_review/nightly_review.py:122, 252`; `style/style_plan.py:2952`; `usage/waste_watch.py:181`.
- Not moved (decision 16): these need nothing from the record and do not change.
  - Sources that `seed` reads: `showrunners.py:216` (`registered_showrunners`); `unit_lookup.py:316, 383` (the pane marks); `production_units.py:59` (`unit_table`); `production_tree.py:119` (`marked_units`).
  - Timer fields, live state or Units-table cells of a member the caller already knows: `unit_lookup.py:392`; `direct/phase_table.py:1664`; `shutdown/stop.py:409`; `shutdown/restart.py:2848`; `promote_showrunner.py:124`; `rename.py:645`; `rename_unit.py:257, 273, 300, 673, 884, 982`; `resume_unit.py:130`; `retire_unit.py:435`; `transfer_unit.py:459, 505`; `add_unit.py:1025`; `astra/design_roles.py:489`; `nightly_review/nightly_review.py:273`; `whoami/agent_accounts.py:239` (one zone from any timer; it is also on the import chain of `pane_record.py`, decision 15).
  - The tree through `showrunners.py` functions only, which Phase 16 moves to the row: `production_tree.py:111` (`edges`); `dailies_input.py:1032` (`root_of`, `children_of`); `rename_outside.py:143, 176` (`children_of`); `add_showrunner.py:242` and `style/add_style.py:333` (`parent_of`).

### Rename inventory (Phase 5)

Config: `config/agents.conf`, `config/README.md:7, 14, 15, 20`, `.gitignore:23-26`. Commands: `agent.md`, `ask_a_friend.md:9`, `history.md:113`, `plan/phase_review.md:173`, `unit/direct.md:28`, `showrunner/add_unit.md:31`, `showrunner/promote_unit.md:18`, `api_review.md`, `module_review.md`, `team_review.md`; Codex mirrors regenerate through `claude_to_codex/sync.py`. Scripts: `agents_config.sh`, `sync_codex_catalog.sh`, `agent_admin.sh:95`, `agent_bg.sh:217, 253`, `agent_exec.sh:78`, `ask_a_friend/launch_friend.sh:54`, `production/add_unit.py:506`, `production/unit_effort.py:61-73`, `notify/review.py:667`, `production/affected_modules.py:109`, `progress_history.py:209, 283, 304`, `assign.py:400`, `demo_refusals.sh:20-24`, `hooks/context_usage.py`, `whoami/codex_pacer.py:6, 58, 440, 523`, `whoami/quota_alert.py:248, 723`, `whoami/profile_notes_pass.py:252`, `astra/design_roles.py:528`, `cli_agent/cli_agent.sh:6, 75`, `history/history.py:115, 121`, and their tests. Docs: the as-built docs that name the file, `docs/direct/launch_implementation.md:24, 72`. Leave: `scripts/settings/git_filters_table.sh:16` (history), `scripts/settings/test_git_filters_retire.py`. NixOS: questions list item 1.

### Instruction edits (Phases 8 and 14)

`commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<CodexDispatchWait/>`, `<PauseForMove/>`, `<FixDispatch/>`, `<PhaseCleanup/>`, `<HeldAstraRoles/>`, `<ToolingContract/>`); `docs/direct/write_prompt_contract.md` (`<PhaseMesh/>` → `<PhasePanes/>`); `docs/direct/launch_implementation.md`, `dual_review.md`, `phase_end.md`, `run_phase_review.md`; `commands/unit/report.md`; `commands/showrunner/produce.md:287, 390, 476, 534, 1122-1123` and one paragraph on where workers show; `commands/showrunner/accounts.md`, `rename_unit.md`, `retire_unit.md`, `transfer_unit.md`; `commands/message.md`; `commands/codex_winddown.md`; `commands/shutdown.md`; `commands/ask_a_friend.md`; `commands/astra_review.md:32` (still starts `mesh_runs.py`); `docs/quota_alerts.md:19, 27, 39`; `commands/shot_report.md:11`; `skills/herdr/SKILL.md:109, 221`, a git-ignored home-manager file, changes only through the NixOS file (questions item 1); `docs/production_format.md:39, 166`; `docs/accounts.md`. A final search for mesh paths outside history ends the phase.

### Cleanup list (Phase 18)

`codex_mesh.py` and its tests; `review_pool.py` and its state; the mesh parts of `workers.py` and the unit worker ledger; `scripts/direct/remove_workers.py`; the run `workers` log (`<run>/workers`, read at `scripts/lib/unit_runs.py:136-145`, `remove_workers.py:65`, `worker_state.py:50`); `mesh_roster.json`, `*.pending.json`, `mesh_*` run files; follow-claim code; `launch_friend.sh`, `end_friend.sh`; `[direct.options] codex_mesh`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, `WORKER_TRANSPORT`; shutdown's `CodexServer`, `codex_seats`, `PausedSeat`, `_CODEX_HEADLESS`, mesh pause and paused-workers note; accounts' server lines; display mesh code (`production_tree.py`, `process_table.py`, `waste_watch.py`, `hierarchies/*`, `shot_report/transcripts.py:1680`, `lint/tmp_sweep.py`); showrunner parent files once Phase 16 moved them; the Codex app-server homes; docs: rewrite `long-lived-workers.md`, remove `review-pool*.md`, update `herdr-adoption.md`, `session-roster.md`, `agent-registry.md`, `shutdown-and-restart.md`, `unit-board.md`, `usage-ledger.md`.

### Probe results (Phase 6 writes them)

none yet.

### Risks

1. herdr reads a Codex pane as `idle` mid-turn: only a `DONE <dispatch id>` line completes a pass (decision 4).
2. A typed prompt stalls: one short line; never resubmit blindly.
3. Reboot before Phase 10: no real unit uses panes before Phase 15.
4. Account moves copy conversation files under a lock, publish each one through the `atomic_file` publish step (`write_bytes_atomic`) and check its hash (Phase 11).
5. Screen room: three or more workers in a 1/3 column are short; idle panes close; `herdr pane zoom` shows one.
6. The user and a director type into one pane at once: a message queues; a dispatch needs `idle`.
7. Two transports for days: the run pin and one resolver; cleanup checks no run pins `mesh`.

### Copies this plan retires

| Copy today | The one place after |
| --- | --- |
| Hierarchy joined at each read from timers, parent files, Units tables, marks, worker ledgers, `mesh_roster.json`, run `workers` log, claim files | `staff.py` (Phases 1, 3, 16, 17) |
| A member's shown name: the session record, the saved `-n` in the pane launch record, the tab title | the staff row (Phase 4) |
| The staff-to-pane join in each display | `worker_panes.observe` (Phases 7, 13) |
| Agent states as strings in each caller | `herdr.AgentStatus` (Phase 6) |
| Config path per module; line parsers in Python and bash | `models_registry.py` (Phase 5) |
| herdr launch paths: `codex_mesh start`, `agent_exec.sh` fresh review, `launch_friend.sh`, `herdr.launch_agent` | `herdr.launch_prepared_agent` with a split destination (Phases 6-7) |
| Dispatch logic in three launchers | `worker_panes.py dispatch` (Phase 8) |
| Resume texts (`stop.py:332-346`, `restart.py`, the mesh's) | `worker_resume_prompt(cause, was_busy, dispatch_id)` in `stop.py` (Phase 10) |
| Usage-refusal matchers (`codex_mesh.QUOTA_REFUSAL`, `refusal_watch.py`, `settle.py`) | one classifier in the refusals layer (Phase 9) |
| Test files that write their own `agents.conf` | the models unit's shared fixture (Phase 5) |

### Transfer journal inventory

- transfer_unit.py journal: journal(slug) :529 = ~/.local/state/showrunner/<from>/transfer_unit.json; UnitTransfer.step + progress receipts :53; STEPS :517 (commit_plans, commit_to, commit_from, move_state, tell_unit, tell_from, tell_to, verify); _load :533, _migrate_journal :539, _load_fields :549, _save :571, _completed_requests :586, _resumed :593; _check_journals :172; plan :629, apply :640/_apply :650, verify :690, describe :623, _commit_only :216, _check_doc :191; CLI :710. staff_move.PinnedUnit :40 exists for the journal. No other module imports journal names. Docs: commands/showrunner/transfer_unit.md:27,44; docs/as-built/unit-transfer.md:54-66.
- Journal tests: test_transfer_unit.py :78 :102 :110 :126 :134 :143 :154 :166 :179 :199 :235 :281 :342 :347 :381(step numbers) :256(patches STEPS); transfer_unit_test_support.py assert_transfer_resumed :291, pause_before_source_commit :346; rename_test_support legacy_staff_journal :45, stop_at_staff_write :260.

## Review record

Team review, cycle 1, 2026-10-30. Four lenses: simplicity (the lead lens), correctness, rerun safety, API and types. The first team review of the same day (R1-R29) is recorded in this file at commit 07d9e3c9.

Applied:
- Simplicity 3, in part — the shutdown record keeps a worker's staff id and no name (Phase 10).
- Simplicity 4, in part — the hook acts on unit panes only and writes no row; `rename.py` is the one showrunner rename; launches and restore take the row's name (Phase 4, decision 14).
- Simplicity 5 — the row is id, kind, name and parent; the role comes from the name; `next_name` is cut; `retire` and wrap remove rows (Phases 1, 3, 7; decision 2).
- Simplicity 6 — one graph rule checked under the lock on every write (Phase 1 As-built).
- Simplicity 7, in part — no function takes a path; the write verbs are only the Mac transport (Phase 1 As-built).
- Simplicity 9 — Phases 5, 18 and 19 have no rerun test; they are commits, not tools.
- Simplicity 11 — the new machine module is cut; the owner check uses `remote.other_machine()` (Phase 1 As-built); the copies-table row is gone.
- Correctness 1 — `seed` matches by `STAFF_ID` first, by name only without one (Phase 2 items 2, 3).
- Correctness 2 — each tool finds its row with `member()`, and the tools with a journal keep the id in it (Phase 3 item 1).
- Correctness 4, in part — restore takes the row's name over the saved `-n` (`pane_restore.py:84-86`); a showrunner hand rename changes nothing (Phase 4 items 1, 2).
- Correctness 5 — the Mac reads its own sources and sends one snapshot to natedev (Phase 2 item 3).
- Correctness 6 — the staff step is checked before any change; refusal tests; rerunning the tool completes it (Phase 3 item 2).
- Correctness 7 — wrap removes its unit rows and its showrunner row, and detaches child showrunners (Phase 3 item 3).
- Correctness 8 — the critical path is 1 → 2 → 3 → 4 → 7; Phases 7, 10 and 15 name Phase 4 in their constraints.
- Correctness 9 — the transcript is the receipt: the typed line carries the dispatch id, and a rerun reads the transcript first (decisions 4, 7; Phase 8).
- Correctness 10 — done means one pane with a live agent named with the id; other panes of the worker are closed (Phase 7).
- Correctness 11 — `WorkerRole` holds `fix`, `designer`, `reviewer`; an Astra role from a unit is a worker, from any other caller a pane that is not staff; ask-a-friend is not staff (Phases 1, 7, 12).
- Correctness 12 — Phase 16 moves the parent-file writers with the readers.
- Rerun 1 — the lock is held from load to replace, with a folder `fsync` and temp-file cleanup (Phase 1 As-built).
- Rerun 2 — the Mac snapshot (Phase 2 item 3).
- Rerun 3 — the dispatch id is in the typed line, the transcript is read first, the prompt file is not changed (Phase 8).
- Rerun 4 — the id is found before the first change; attach and detach set a desired state (Phase 3).
- Rerun 6 — `seed` matches by id; recovery is a rerun of the tool, not `seed` (Phases 2, 3).
- Rerun 7 — the worker lock is held from reconciliation to launch proof; the split labels the pane, so a killed launch is found (Phases 6, 7).
- Rerun 8 — staging copy, hash check, `os.replace` publish, and a target check before the fallback (Phase 11).
- Rerun 9 — one launcher step ends both restore branches (Phase 10).
- Rerun 10 — a parent must exist and be of the right kind, checked under the lock (Phase 1 As-built).
- Rerun 11 — named rerun tests with their stop points in Phases 3, 7, 8, 9, 10, 11 and 12.
- API 1, in part — `WorkerRole` uses the registry's role names, and one function maps a role to its registry row (Phase 7).
- API 3 — `start-or-resume <short name>`; `WorkerName.parse`; `ensure` under the lock (Phases 1, 7).
- API 4 — `rename` and `move` refuse a worker; showrunner retire is defined (Phase 1 As-built).
- API 5 — `herdr.AgentStatus` (Phase 6); `worker_panes.observe` is the one join (Phases 7, 13; decision 10).
- API 6 — typed signatures, `Closed | Kept`, one `ExitCode` table, workers addressed by short name, `_layout` private (Phases 7, 8; decision 10).
- API 7 — `StaffUnreadable`, `StaffRefused`, `StaffNotFound`; the CLI maps them to exit codes (Phase 1 As-built).
- API 8 — `destination: NewTab | SplitPane` (Phase 6).
- API 9 — no public load, no path parameters, no `find(name=)`, no retired flag (Phase 1 As-built).

Dropped:
- Simplicity 1 (one cutover, no `seed`) — moving every reader and writer in one phase is too large for one dispatch; id-first `seed`, the Mac snapshot and Phases 3 and 4 before any pane close the trailing-record risk.
- Simplicity 2 (no `WORKER_TRANSPORT`) — units end their runs at different times, so a drain stops every unit at once; the pin lets each unit switch at its own run boundary, and Phase 18 deletes it.
- Simplicity 3, the rest (remove the `SHOWRUNNER_*` marks) — `member()` uses them to go from a Units row to the pane launch record that holds the `STAFF_ID`.
- Simplicity 4, the rest (write the session record back on a refusal) — that record is Claude Code's file; the next launch takes the row's name.
- Simplicity 7, the rest (delete `STAFF_FILE`) — with no path parameters it is the one test injection point, and the subprocess and fake-ssh tests need it.
- Simplicity 8 (`launch.py` in Phase 8) — the launchers keep their mesh branch until Phase 18, so an early merge ports code that Phase 18 deletes; dispatch is in one place from Phase 8.
- Simplicity 10 and API 10 (the overview) — the overview is another file, outside this edit; herdr-unit updates it to this plan.
- Correctness 3 (`restore()` of a retired row) — a retired row is removed (Simplicity 5), and retire ends the unit's workers, so a resumed unit is a new member and is owed no worker conversation.
- Correctness 4, the rest (a hand rename of a worker) — a worker's name is set once (decision 13), and its session name is its id.
- Correctness 9, the rest (the pass recorder as the journal) — the transcript already holds the submission; a second record would be a copy of it.
- API 2 (three row classes) — one four-field row with the graph rule checked under the lock gives the same guarantee with one type.
- Rerun 2, the rest (natedev pulls the Mac snapshot) — the Mac sends it through the one ssh write path, with the same rows.
- Rerun 5 (a cleanup fence) — after Phase 15 nothing pins `mesh`, so the check cannot go stale; cleanup is not a shipped tool.
- Rerun 6, the rest (`seed` refuses after a cutover marker) — id-first matching means a later `seed` cannot undo a rename.

Refuted: none. Questions for the user: none.

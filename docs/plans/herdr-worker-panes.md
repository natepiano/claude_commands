# Workers in herdr panes, and one staff record

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Approved to build on 2026-10-30 by the user, through the sdlc showrunner, before a plan review; the user reviews an overview in the morning. It adds one staff record of every showrunner, unit director and worker, retires `codex_mesh`, and runs every unit worker as an agent in a herdr pane in its director's tab. It renames `agents.conf` to `models.conf` and `/agent` to `/default_models`.


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

- **Project:** `~/.claude` scripts provide the agent tools. This plan adds staff and moves workers from mesh threads to herdr panes. Devtools-unit 3t owns launcher consolidation.
- **Project started:** 2026-10-30T06:38:57.873+00:00
- **Stack:** Python 3.11+ through the `scripts/lib/py` shim, bash, `unittest`, basedpyright; herdr 0.9.5 CLI; the interactive Claude Code and Codex CLIs.
- **Outside reader:** cargo-liner reads `staff.json` directly. A change to the stored shape sends a notice to that reader with a representative stored-record fixture.
- **Layout:**
  - `scripts/production/` — production tools: `showrunners.py`, `production_units.py`, `production_lifecycle.py`, `live_units.py`, `unit_lookup.py`, `session_staff.py`, `session_labels.py`, `add_unit.py`, `add_showrunner.py`, `retire_unit.py`, `rename_unit.py`, `transfer_unit.py`, `rename.py`, `promote_*.py`, `resume_unit.py`, `production_tree.py` (deleted in Phase 4b), `broadcast.py`, `codex_winddown.py`; `staff.py` and `staff_sources.py`
  - `scripts/herdr/` — `herdr.py` (the one herdr client), the new `worker_panes.py`
  - `scripts/direct/` — the launchers `implement.sh`, `review.sh`, `assign.sh`/`assign.py`, `workers.py`, `worker_state.py`, `remove_workers.py`, `board.sh`, `progress_history.py`, `findings.py`
  - `scripts/agents/` — `codex_mesh.py`, `worker_lifecycle.py` (from models checkpoint 3b67af71), `mesh_runs.py`, `agent_exec.sh`
  - Not in this unit's scope (models-unit owns them from 2026-11-03): `scripts/agents/default_models.sh`, `scripts/agents/models_config.sh`, `scripts/lib/models_registry.py`, `scripts/agents/sync_codex_catalog.sh`, `config/models.conf`. A phase here that must change one sends the change to models-unit.
  - `scripts/message/` — `send.py`, `roster.py`, `sessions.py`
  - `scripts/shutdown/` — `shutdown.py` (the CLI), `stop_resume.py` (stop, resume and move of one session), `inventory.py`, `restart.py`, `shutdown_record.py`, `pane_record.py`, `pane_restore.py`, `remote.py`; `stop.py` is gone
  - `scripts/lib/` — `unit_runs.py`, `names.py`, `atomic_file.py`, and the existing configuration reader `conf.py`.
  - `config/` — `direct.conf`, `names.toml`; `staff.conf` (new in Phase 3n), `production.conf` (new in Phase 3r), `shutdown.conf` (new in Phase 10)
  - `commands/`, `docs/direct/`, `docs/as-built/`; `skills/herdr/SKILL.md` is git-ignored and built from the NixOS file
- **Key files:**
  - `scripts/production/showrunners.py` — showrunners from update timers (`registered_showrunners` :216, `current_name` :341, `parent_of` :427, `attach` :528, `detach` :557; parent files under `~/.local/state/showrunner/<slug>/parent`, deleted in Phase 4d)
  - `scripts/production/production_units.py` — a production doc's Units table (`unit_table` :59, `unit_is_retired` :100, `SessionMarks` :84); deleted in Phase 4c
  - `scripts/production/showrunner_parent.py` — the showrunner parent files and attach and detach; its parent-file operations are deleted in Phase 4d
  - `scripts/production/unit_config.py` — the one reader and writer of `.unit.toml` (`UnitConfig`, `read`, `write`, `park`, `restore`, `discard_parked`, `place`; new in Phase 3q)
  - `scripts/lib/git_exclude.py` — `ensure_excluded(checkout, pattern)`, the one writer of a repository's shared `info/exclude` (new in Phase 3q)
  - `scripts/production/unit_lookup.py` — units found by herdr pane marks; it re-exports the joins of `session_staff.py` (`unit_parent` :252 is the snapshot-aware row-parent resolver)
  - `scripts/production/session_staff.py` (matches a running session to its staff row; "unit marks" is a retired term, say "session labels") — the one copy of the staff-backed identity joins: `ShowrunnerMember`, `UnitMember`, `ObservedUnit`, `MemberFound | NoSuchMember`, `MemberSources` (:367), `member_sources(*, dependencies=...)` (:442), the pane marks `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID`. `unit_lookup` and `staff_sources` import from it, so lower readers use the same join with no import cycle and no second module identity.
  - `scripts/direct/workers.py` — the unit worker ledger under `~/.local/state/unit-workers/` (:535, `ledger_path` :1632), retired in the cleanup phase; `running_director_sessions(registry)` (:1524, running directors newest first) and `resolve_live_unit_director(run, record, sources, running, dependencies)` (:1534, a run's director from supplied evidence); the models unit's file
  - `scripts/herdr/herdr.py` — `launch_agent` :3354, `launch_prepared_agent` :3417 (opens a tab today), `_agent_name` :3333 (truncates to 32 characters), `_herdr_resumes_agents` :2091, `Herdr.prompt` :1820, `Herdr.retitle` :1279, `Herdr.panes()` :1088 (every pane with `pane_id`, `label`, `terminal_id`, `workspace_id`, `session_id`), `Herdr.pane_activity(pane)` :1127 (`PaneAtShell | PaneRunning`), `Herdr.run_in_pane` :1080, `agent_status: str` :773, :813, :821
  - `scripts/shutdown/pane_record.py` — the pane launch record: the SessionStart hook (`scripts/hooks/session-start-pane-record.py:37`) writes one entry per Claude launch in herdr, with the launch `-n` name and the env names in `KEPT_NAMES` and `KEPT_PREFIXES` (:48-49); `pane_restore.py:83-92` relaunches from it
  - `scripts/shutdown/remote.py` — `other_machine()` :25, the one rule for which workstation this is
  - `scripts/agents/codex_mesh.py` — 8652 lines, deleted in the cleanup phase
  - `docs/as-built/herdr-adoption.md`, `docs/as-built/long-lived-workers.md`, `docs/as-built/agent-registry.md`, `docs/as-built/shutdown-and-restart.md` — how herdr, workers, the registry and shutdown work now
  - This plan's `## Reference` section — the job table (what the mesh does today and what replaces each job), the risks and the copies this plan retires
  - `scripts/lib/atomic_file.py` — the one file writer: `write_text_atomic(path, text)` and `sync_folder(folder)`; `write_bytes_atomic` comes in Phase 11; the unit-board unit's file
  - `scripts/production/staff.py` owns the seven-field `StaffRow`: `id`, `kind`, `name`, `parent`, `current_session`, `worktree`, and `doc`.
    `ensure_worker(identity, name, parent)` stores no role.
    Start and selection take the dispatch role as an argument; restore reads the last dispatch record.
    `StaffSnapshot.backends` derives backend information from rows; no stored backend record exists.
    Staff storage stays schema 2; live output stays schema 3.
    `scripts/direct/progress_history.py` owns the existing dispatch record: the per-slot pass with role `kind` and identity `instance_id`.
  - `scripts/production/staff_sources.py` — `member` and `seed` over today's sources (new in Phase 2; `fill` in Phase 3q; the seed importer and `fill` go in Phase 4d); `live()` (Phase 3l), the reader behind `staff.py show --json --live`, the handler's command
  - `scripts/lib/conf.py` supplies `conf.read(...).number(...)`, committed-value recovery, and `Unusable`; private configuration uses `CONF_DIR`.
  - `scripts/herdr/worker_panes.py` supplies `start_or_resume`, `close`, and `observe` in Phase 7, then `dispatch` in Phase 8.
    `observe` consumes `Herdr.scan_row_panes(record, panes)`, which returns `RowPaneScan`; it adds no identity join.
    `WorkerPane` has five states: `OpenWorker`, `StoppedWorker`, `ClosedWorker`, `ProcessUnknownWorker`, and `UncertainWorker`.
  - `scripts/herdr/side_pane.py` — the pane beside a caller outside the staff hierarchy, for ask-a-friend and for such a caller's Astra role (new in Phase 12)
  - `scripts/agents/detached_runs.py` — detached runs that type their result into the worker pane (renamed from `mesh_runs.py` in Phase 19; sdlc, 2026-11-05)
  - `scripts/direct/assign.sh` — the one door that starts a worker (decision 11); devtools 3t merges `implement.sh` and `review.sh` into it
  - `scripts/lib/unit_runs.py` — run folders; `worker_transport(run)` and the run pin (added in Phase 8; they and the run `workers` log reader go in Phase 19)
  - `config/direct.conf` holds `WORKER_TRANSPORT`: mesh in Phase 8, herdr in Phase 15, then removal in Phase 19.
    Phase 7 reads `WORKER_SPLIT_RATIO` at each split through `conf.read("direct").number("WORKER_SPLIT_RATIO")`.
  - `commands/unit/direct.md` — the unit director instructions (transport-aware text in Phase 8, pane text in Phase 14)
  - `scripts/shutdown/stop_resume.py` — stop, resume and move of one session: `stop` :97, `resume_message` :178 (the one resume-text builder), `resume` :216, `move_sessions` :405, `move` :463; `STOP_GRACE_SECONDS` and `KILL_GRACE_SECONDS` (:42-43) move to `config/shutdown.conf` in Phase 10; the per-member worker stop and resume come in Phase 3n, `stop_unit_workers` in Phase 10, mesh pause removed in Phase 19; the shutdown unit's file
  - `scripts/accounts/refusal_watch.py` — `transcript_refusal` (:464), the one usage-refusal classifier (Codex transcripts and screen text in Phase 9); the accounts unit's file
- **Test lanes:** no Rust crate. The test lane is the `test_*.py` files, the `*_test_support.py` fixtures and the fixture folders beside each module. In a `1 writer + 1 tester` phase, `test` owns only those files and writes them from the Spec.
- **Build:** none (scripts).
- **Test:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/targeted_test.py --checkout <tree>`, run from `~/.claude` main's copy, after a merge of `origin/natedev` (memory: merge before a gate).
  For each phase test, run `~/.claude/scripts/lib/py -m unittest discover -s <dir> -p '<test file>'`.
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
  - **Lifecycle reruns keep the pane (user, 2026-10-31).** A rerun of move, rename, stop, resume, down or restart changes nothing, keeps the same tab, pane and terminal, and exits 0. No recovery closes and recreates a pane or split. Each such phase's gate has a real rerun proof and one operation stopped partway that finishes on rerun.
  - **Staff design (2026-10-31; user via the sdlc showrunner).** A unit's staff row holds its worktree (machine and path); its repository and branch come from git in that worktree. Its plan, port and responsibilities are in an untracked `.unit.toml` at the worktree root, listed in the repository's shared `info/exclude` (`git_exclude.ensure_excluded`). Retire moves the file to `~/.local/state/staff/units/<staff id>.toml`; `resume_unit` moves it back. A showrunner row holds its production doc. No code works out a folder, a branch or a parent from a name. Owns, the Units table, the `showrunners.json` lists, `Showrunner session`, `Parent production`, `Formerly` and the review pool are deleted. The pane-first order below supersedes the old staff-first order (sdlc, 2026-10-31 15:5x EDT). Folder moves follow 3r. Panes use 3n and 4; broad staff conversions, store moves and deletes follow useful panes. The rename rebuild stays last among staff steps (4g). Phase 3h replaces copied `STAFF_ID` marks with native runtime lookup. Todo phases use that contract even where an earlier completed phase records the old mark.
  - **One identity join.** Staff-backed identity joins live once, in `scripts/production/session_staff.py`; `session_labels.py` owns display labels. A command takes one `staff.snapshot()` and one `session_staff.member_sources(dependencies=<one staff.RecordInputs>)` and passes both through every call. Running directors come from `workers.running_director_sessions`, a run's director from `workers.resolve_live_unit_director` on the same evidence. No reader takes a second owner snapshot or keeps its own join.
  - **No hard-coded config (user, 2026-10-31).** Limits, ratios, and times live in `config/*.conf` and use the existing `scripts/lib/conf.py` reader.
    Read numbers through `conf.read(...).number(...)`; handle `Unusable` and use `CONF_DIR` for private configuration.
    Code supplies no default and adds no reader.
    `worker_lifecycle.operation_deadline` can shorten a configured wait; each wait uses `min(configured limit, remaining deadline)`.
    Each value has a test that changes configuration without changing code.
  - **The worker transport is pinned per run.** `WORKER_TRANSPORT=mesh|herdr` in `config/direct.conf` is read at run start and pinned in the run folder. A run that started on the mesh finishes on the mesh. Every lifecycle caller (dispatch, messages, detached delivery, shutdown, rename, transfer, retire, cleanup) reads the run's pin through one resolver, `unit_runs.worker_transport(run)`.
  - **Tests never touch the user's tabs.** herdr tests run in a probe tab of `agents` (memory: one herdr session; `herdr.py` refuses other servers) and close it at the end. No test runs `herdr` against a live director's tab.
  - **Build hold.** Before any test or gate, check `build_hold.py status`; while a natedev hold that is not ours is in force (02:00-05:00 tonight), run nothing that builds or tests. Edits continue.
  - **One file writer.** Every file write uses `scripts/lib/atomic_file.py`. `write_text_atomic(path, text)` writes a temp file beside the target, syncs it, replaces the target with `os.replace` and syncs the folder; `sync_folder(folder)` does the folder step alone. Its temp files are `.<name>.<pid>.<random>.tmp` (`atomic_file.py:42`), so a cleanup of a killed writer's temp files uses the glob `.<name>.*.tmp`. No phase makes its own temp-file, `fsync` and replace steps. `write_bytes_atomic(path, data)` (`atomic_file.py:85`) is the binary writer; no second module. The unit-board unit owns `atomic_file.py`; a phase that changes it agrees the change with that unit first.
  - **Owners.** The models unit owns `models_registry.py`, `config/models.conf` and the registry writers; the shutdown unit owns `scripts/shutdown/`; the accounts unit owns `scripts/accounts/` and account moves. A phase that changes their files names the owner in its checkpoint notice and agrees the change with them first. The user owns `/etc/nixos` (questions list item 1).

- **Constraints for todo phases (sdlc, 2026-11-01).** Phase 3h's native runtime identity supersedes earlier copied-mark contracts. `WorkerStartAllowed` permits backend creation; only `PublishedWorker` permits dispatch success. A row's `id` is fixed: the first native session or thread id. A live clear or a Codex thread replacement changes only the row's `current_session`, through `staff.update_current_session(old, new)`, in one locked atomic staff write; no journal or store registry. Ownership, references, child links and locks use the fixed `id`; live observation, transcripts and backend resume use `current_session`. A stored reference resolves by fixed id, then by current session; an observed runtime id matches only `current_session`. Every fixture that tests identity makes the two differ (Phase 3h review, 2026-11-01). A late hook is visible as a missing-row session. Migration writes reference stores first and staff last and is deleted in Phase 19. Current membership omits retired/wrapped members; cleanup retains their live, unknown and duplicate evidence. Retire, wrap and promote keep stopped workers for restore. Only worker end and permanent unit removal delete worker rows. Phase 8 owns `worker.py`; devtools 3t deletes old launcher names after callers switch.
- **Isolation gate (Phase 3h review, 2026-11-01).** Every automated gate runs as `env HERDR_BIN=/nonexistent/herdr -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY <command>`, so a test that reaches herdr or live staff fails in its own phase, not at the merge. Real proofs run separately, under their named probe steps. Hook and subprocess fixtures keep their scratch-path overrides (never a `patch.dict(..., clear=True)` that drops them). A removed or changed assertion needs one `Assertion-Change: <path>::<test> — <why>` line. The hub files `staff.py`, `session_staff.py`, `showrunners.py`, `herdr.py` and the rename helpers have one writer at a time across the rename and pane tracks; a phase that needs one waits for the holder's landing.

## Decisions made in this plan

Each item is my call under the decision criteria unless it names the user. A one-line plan change reverses it.

1. **One staff record (user, 2026-10-30).** `scripts/production/staff.py` owns `~/.local/state/staff/staff.json` on natedev.
   Each `StaffRow` has seven fields: `id`, `kind`, `name`, `parent`, `current_session`, `worktree`, and `doc`.
   The parent chain defines the hierarchy; names and parents can change while `id` stays fixed.
   Runtime replacement updates `current_session`; observation and resume use it.
   Staff replaces the worker ledger, roster, run worker log, and slot claims after their consumers migrate.
   Pane, socket, and running state come from live readers.
   `StaffSnapshot.backends` derives backend information; staff stores no role or separate backend record.
   Stored schema 2 and live schema 3 remain distinct.
2. **One command starts or resumes a worker (user, 2026-10-30).** `worker_panes.py start-or-resume <short name> --role <role>` takes the dispatch role explicitly.
   Every worker selection call also takes the role; restore reads the worker's last dispatch record.
   `staff.ensure_worker(identity, name, parent)` stores no role; names never determine roles.
   Launch settings supply the role's model, effort, service tier, sandbox, and permission mode through the existing registry and role policies.
   An open agent keeps its pane when the requested launch policy matches.
   A different policy requires `can_close`; the shared stop path then resumes `current_session` in the same pane and terminal.
   A blocker returns `StartRefused` without changes.
   A hand-entered `/model` stays for the same role; a configuration edit alone does not restart an agent.
   The director selects each short name, such as `impl1` or `impl2`.
   Herdr's agent name remains a display label; operations address the proved pane and current native session.
3. **Idle panes close at phase end (user, 2026-10-30).** A pane closes when its role is idle (its dispatch is done and nothing is queued) and reopens by `current_session` at the next dispatch. This applies to workers, designers and reviewers (user, 2026-11-03 via sdlc: the codex-mesh retires). The conversation stays. To bring a worker back, the director calls `start-or-resume` again. This settles the keep-open question.
4. **The worker's final reply signals completion, and the transcript is the receipt; herdr's state only wakes the launcher.** Every dispatch has a dispatch id, and the one line the launcher types carries it (decision 7). The worker's own transcript then holds the proof of each step: the typed line with the id proves the submission, and a final reply that ends with `DONE <dispatch id>` proves completion. The launcher writes the status file only from that reply. A rerun reads the transcript before it types anything, so it never types a pass twice (Phase 8). No other record of a submission exists, and the prompt file is never changed. The launcher writes the status file today (`implement.sh:629`); the worker writes only its summary. This works the same for read-only reviewers, which cannot write a file (`review.sh:689` starts them with `--sandbox read-only`): the launcher writes their findings file from the reply, as the mesh does today.
5. **The launcher's whole wait is one background `herdr agent prompt <id> "<line>" --wait`, with no `--timeout` and no `--until`.** herdr then waits for its default settled set, `idle`, `done` or `blocked`, after it sees the turn start (herdr skill, "For normal agent work, `--wait` is enough"). `blocked` wakes the launcher, so a worker at an approval dialog never stalls the director. The director runs the launcher in the background, as today, and the task notification is the wake-up.
6. **A dispatch refuses a busy worker; a message queues.** A dispatch is a new pass that a launcher waits on, so it needs an `idle` or `done` worker, under the worker's dispatch lock. A message (`send.py`, a detached-run result, review Stage 2) is typed at once, also during a turn: Codex and Claude queue typed input. A blocked or closed worker's message goes to its director. `pending.json` drops.
7. **A prompt goes in by file path, not as pasted text.** The launcher types one line: `Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>.`
8. **No launcher compaction, and no "Continue where you stopped." retry.** Interactive Codex and Claude compact by themselves. A Codex pane worker takes `-c model_auto_compact_token_limit=<UNIT_COMPACT_ABOVE_TOKENS>` at launch, because Codex otherwise waits for its 475,000-token window (showrunner ruling, 2026-11-06).
9. **Codex stays a worker kind.** herdr starts `--kind codex` and `--kind claude` agents. The default worker kind per role stays where it is set today.
10. **`herdr.py` stays the one herdr client.** `Herdr.panes()` remains the shared pane parser.
    `Herdr._row_panes` owns the native-session join and returns `RowPaneScan(found, unknown)`.
    `UnknownPane` carries the pane id, tab id, and reason; the scan owns its stderr diagnostic.
    `record_layout`, `retitle_row`, `move_row_tabs`, and `worker_panes.observe` consume this result.
    `WorkerPane` has five states: `OpenWorker`, `StoppedWorker`, `ClosedWorker`, `ProcessUnknownWorker`, and `UncertainWorker`.
    Labels support stopped-pane recovery; they do not prove live ownership.
    `worker_panes.py` composes start, dispatch, close, and observation; `_layout` remains private.
    Direct agent calls remain direct; no wrapper adds another herdr client.
11. **One door starts a worker: `assign.sh`, run as a background shell of the director's own session (user rule 2026-11-04 10:45: "it should be impossible to start a worker in more than one way"; sdlc ruling).** Devtools-unit guards `implement.sh`, `review.sh`, and raw mesh starts so only `assign.sh` can call them.
    Detached starts remain refused.
    Phase 7 supplies pane primitives; Phase 8 switches launch routing and deletes the replaced transport paths.
    Devtools-unit 3t owns launcher consolidation and deletion of the old launcher names.
    This plan adds no second launch command.
    Start, dispatch, wait, and receipt handling remain shared behind `assign.sh`.
    Changes to `scripts/direct` launchers require coordination with devtools-unit.
12. **The transport switch lives in `config/direct.conf`, with an end date.** `WORKER_TRANSPORT` is migration state, not a model default, so it stays out of `models.conf`. The cleanup phase deletes it, no later than 7 days after the default switch.
13. **The right pane shows the worker's short name (user, 2026-10-30).** For example `impl1`, with no production or unit name.
14. **The staff row is the one name (sdlc showrunner, 2026-10-30).** A unit's name changes only through `/showrunner:rename_unit` (sdlc, changed 2026-10-30). The hook cannot stop Claude Code's `/rename`, so on a mismatch it keeps the tab on the row's name and prints one line with the rename_unit command. Every launch and every restore of a member takes its name from its row (Phase 4). A showrunner's name is also its workspace's, which `Herdr.retitle` does not rename, so `/showrunner:rename` (`rename.py`) is the one way to rename a showrunner. A worker's name is set once (decision 13).
15. **One way to find a row: the `unit_marks` join (herdr-unit, 2026-10-30; showrunner, 2026-10-30).** The join is `unit_marks.MemberSources.member` (`unit_marks.py:77`, built by `member_sources` :136); `staff_sources.member` (`staff_sources.py:42`) is its entry point and keeps no join of its own (Delegation Context → One identity join). `unit_marks.py` and `staff_sources.py` import `showrunners.py`, so `showrunners.py` never imports either, and no lazy import or copy of `member` goes around that rule. `staff.py` imports none of today's sources (Phase 1 As-built). Row names are workspace and tab names, not slugs: seed keeps slug `sdlc` as row `tools` (`scripts/production/test_staff_sources.py:28`), so no code finds a showrunner or unit row with `staff.by_path(<slug>)`. Code that needs a member's row calls `staff_sources.member(selector, staff.snapshot())`; a `showrunners.py` function that works on rows takes the row or the `StaffSnapshot` from its caller and never looks a member up itself. Tests use a slug that differs from its workspace name. One exception (showrunner, 2026-10-30): `showrunners.py main`'s attach and detach branch imports `showrunner_parent` inside that branch, so users keep one command; the module has no load-time import of it. The showrunner tree readers live in `showrunner_tree.py`, which imports `staff`, `staff_sources` and `showrunners` (sdlc, 2026-10-30; Phase 3c item 5); `showrunners.py main` imports it inside the branch that needs it, under the same exception.
16. **The row holds what the user's design needs (herdr-unit, 2026-10-30; worker role, sdlc 2026-10-31; corrected 2026-10-31 for decision 17).** `StaffRow` has seven fields: `id`, `kind`, `name`, `parent`, `current_session`, `worktree`, and `doc`.
    `id` stays fixed; `current_session` follows native session replacement.
    The row stores no role, pane, socket, zone, or Units-table value.
    Start and selection take a role argument; restore reads the last dispatch record.
    `StaffSnapshot.backends` derives backend information without a stored backend section.
    A retired unit retains `RemovedWorktree`; a wrapped production retains `WrappedDoc`.
    A timer joins by staff id; names never substitute for identity.
    A unit's repository and branch come from Git; its plan, port, and responsibilities come from `.unit.toml`.
    Pane and running state come from herdr.
    A missing local timer provides no local timer fields.
    Staff storage stays schema 2; live output stays schema 3.
17. **Staff design (user via the sdlc showrunner, 2026-10-31).** The unit row holds its worktree, and repository and branch come from git in it. Plan, port and responsibilities are in an untracked `.unit.toml` at the worktree root; retire parks it under `~/.local/state/staff/units/<staff id>.toml` and `resume_unit` puts it back. No folder is worked out from a name. Owns, the Units table, the `showrunners.json` lists, `Showrunner session`, `Parent production`, `Formerly` and the review pool are deleted. The approved pane-first order is in Phases → Critical path (sdlc, 2026-10-31 15:5x EDT). After each landing all live tools still work. Shared gates cover the exact final batch. Each state change keeps its real proof and rerun checks. Each delete names a check that proves nothing still reads the thing. Retired units are backfilled, and a missing fact is listed for sdlc, never filled from a likely value. Panes carry no `STAFF_ID`. Native runtime identity selects the staff row. The pane launch record adds no copied staff identity. The term "unit marks" retires: text says "session labels" (the unit name and staff id a session carries), and `unit_marks.py` becomes `session_labels.py` in Phase 3s (user via sdlc, 2026-10-31).
18. **A herdr pane starts in its worktree's dev shell (C380, models-unit via sdlc, 2026-11-03).** When the codex-mesh retires, a worker, designer or reviewer pane starts inside its worktree's direnv dev shell, as the Codex app-servers do today. One test covers it.
19. **A run result reaches its owner after the launcher exits (models-unit via sdlc, 2026-11-03).** Today the mesh relay holds a detached-run result for an attached launcher, and drops it when no launcher is attached. A worker pane's run result goes to its owner with no launcher attached. One test covers it. Evidence: session cc8dddb8, runs 20261103T193124, 20261103T164054 and 20261103T200357-1248016-79f7.

## Questions for the user

1. **The NixOS edits (settled 2026-10-30: natedev makes them and tells the user when to rebuild; herdr-unit tells natedev when each is due; no diff files for the user).** (a) `modules/common/claude-git-filters.nix:35` `watchPaths`: `agents.conf` → `models.conf`. It is functional: until it changes, the git filter does not refresh on a `models.conf` change. (b) Comments in `modules/common/codex-catalog-sync.nix`, `modules/common/codex.nix:47`, `modules/common/shell.nix:139` (`/agent`) and `README.md:410`. (c) `modules/common/herdr-skill-local.md:7`: the rule "start a new agent in a new tab, not a split pane" must permit worker panes in the director's tab (`skills/herdr/SKILL.md:221` is built from it). (d) The sign-in path unit (natedev) and the launchd agent (Mac) that run `codex_mesh.py signin-changed`: they retire at cleanup, or point at the pane restart if Phase 6's Codex sign-in probe is negative. (e) Only if Phase 6's probe shows that herdr's own resume does not keep each pane's `--env` (its account): add `[session] resume_agents_on_restore = false` to `~/.config/herdr/config.toml` on natedev and the Mac, so our restore owns split tabs; if the probe is positive, herdr restores split tabs and our per-pane restore code retires for them. **Status (2026-11-03):** (a) and (b) are done in the NixOS source: natedev applied the Phase 5 edits to `/etc/nixos` main with the Phase 5 merge (main `731dd21aa`). natedev owns the rebuild notice and the check that the installed git-filter job watches `config/models.conf`; the repository's old-name test cannot see the installed job. Phase 10's probe result is negative, so (e) applies before the default switch (Phase 15). (d) is due at cleanup (Phase 19). herdr-unit tells natedev when it is due. **Status (2026-11-05):** natedev made (c) on NixOS main dd1873d and (e) in `~/.config/herdr/config.toml` on natedev and the Mac; the user rebuild and the next server start remain.
2. **Phase 9a, a result cut off between pane input and its typed mark (settled by sdlc, 2026-11-05; no user question).** Take (a): a rerun types it again. It adds no saved mark and no check; a worker that sees one result twice loses nothing.

## Phases

**Critical path (sdlc, 2026-10-31 15:5x EDT).** The showrunner approved `fast_plan.md` for the user: "i have stopped nearly all work ... i am sick of waiting". The supplied message gives no exact minute beyond `15:5x`. Pane-first order and shared gates replace the staff-first order and the rule that every phase lands alone. All checks stay. There is no build.

Phase 3q → 3r → actual unit folder renames. Use the corrected current command, one unit at a time at its phase boundary. Keep its stop, restart and finished-rerun proofs. Neither these moves nor Phase 6 waits for 4g. Phase 6 starts at once beside 3q/3r and the models work. Phase 3n still waits for 3q and models F024 (`3b67af71`). Phase 4 follows 3r, with one owner for its shared files. Then 3n + 4 + 6 → 7 → 8 → 9 → shared 10/12/13 → shared 11/14 → 15 → drain and audited 18 → 19. Phase 11 consumes 10's resume contract. Phase 14 can be drafted beside 11 and is checked against its final code.

**Rename track (sdlc ruling, 2026-11-01; replaces the earlier staff order).** After Phase 3h lands on main: the live move (`migrate_staff_ids.py` backup, dry run, real run, rerun; `staff_check` twice; a real `/clear`, a Codex thread replacement and a `herdr-up` rerun) → 3a (`unit <id>`) → the readers a rename would break: 3k, 3s, then 3t, 3u and 3v, each landing alone, 3w text with each landing, and Phase 4 → the records that hold a unit name (sdlc-rename-places.md): 3x, 3y, 3z, 4a → the deletions rename needs: 4b, 4c (after the Port audit 4m into `.unit.toml`), 4d, 4e → 4g, with 4j (no ad-hoc workspace, right after the live move), 4h (folder move, after 4j) and 4i (questions, after 3a) landing before it, rename on the row for every member kind and role. The old rewriting rename is never built; 3f8339f5 stays unmerged and gives only its tab retitle, kept-pane resume and finished-journal rerun. 4f (transfer) follows 4c and is not a rename prerequisite. No rename-track phase waits for Phase 15.

**Pane track, alongside.** 3n + 4 + 6 → 7 → 8 → 9 → shared 10/12/13 → shared 11/14 → 15 → drain and audited 18 → 19. Phase 5 still waits on the models unit. A hub file shared by both tracks has one writer at a time (Delegation Context → Isolation gate).

Targets: the old `fast_plan.md` dates are past. Each landing sends its own time to sdlc; the rename-track time is in the 2026-11-01 message to sdlc.

**Three kinds of order (sdlc, 2026-10-31 15:5x EDT).** A **Depends on** line distinguishes real inputs from scheduling and file ownership.
- *Data prerequisites:* 3n needs 3h's native-id admission, 3q's tunables and models F024. `tunables` comes from 3q, not 3n. Phase 7 needs 3n's role, backend, deadline and stop APIs, Phase 4's names and Phase 6's primitives. Phase 13 uses the existing `session_staff` join; 3s extends it and keeps `session_labels` for labels. 4a needs 3t's doc reads and 3z's remaining stores. 4d needs 4c's last `fill --dry-run` audit. 4e needs 4d's fallback removal. 4f needs 4c's table removal. No pane phase needs the full 4g rebuild.
- *Landing and gate rules:* 3t, 3u and 3v land one at a time, each with its own gate on its exact landing (their Depends on lines: "Lands alone"); ready 10/12/13 share a gate, then 11/14 share a gate. Merge each ready group as one batch and gate that exact final batch. Keep every phase's tests, types, review lenses, live probes, interruption and finished-rerun checks. A fix changes the batch; its required checks cover the final code. Both reviewers can review ready file groups at once and verify fixes. No phase uses a pass from an earlier tip. Delete barriers: 4b-4e wait for the conversions that read or hold what they delete (3s-3z, 4, 4a), not for Phase 15, and Phase 3h's native-id and location completeness proof on a fresh snapshot: no unresolved member identity or unfilled location. The worker ledger gets no migration; rename keeps its small file move for it until Phase 19 deletes the ledger after drain (sdlc, 2026-11-01). 4d and 4e name the completeness proof because they remove the fallback and labels. Phase 19 instead waits for no mesh pins or processes, no legacy consumers, and its history proof. It does not wait for 4g or unrelated staff stores.
- *File ownership:* each shared file has one writer. Publish the needed API first. Shared-file work can land in one ready batch; it does not make all later work wait for a whole unrelated phase. Live folder moves and store migrations still run one at a time. Each store keeps its own commit, stopped-run recovery proof and unchanged-rerun proof.

| Phases | Parallel file groups | Files with one writer |
|---|---|---|
| 3q, 3n | schema and backend API; `.unit.toml` and exclusion; migration; worker callers; outside integrations | `staff.py`, `staff_sources.py`, shared fixtures |
| 3r, 3t | unit lifecycle; showrunner lifecycle; merge tools; report tools | `add_unit.py`, `production_lifecycle.py` |
| 3u, 3v | direct; usage and hierarchies; messaging; status line and nightly review; each owning unit's tree | `session_labels.py`, `herdr.py`, shared shutdown files |
| 3w | showrunner commands; producer and unit commands; format docs | each command file and its Codex mirror |
| 3x-4a | one group per store, with every caller and command text of that store | rename rewriters, `showrunners.py`, shared report modules |
| 4b-4g | independent delete groups and production-doc branches; unit and showrunner rename groups after the shared APIs settle | `session_labels.py`, rename modules, `unit_move.py`, each production doc |

Parallel preparation is allowed across phases. Each store keeps its own landing and real proof. Other ready steps can share the exact-batch gates above. No consumer or deletion proof is dropped. Phase 15 also needs questions items 1(c) and 1(e).

**Old phases and their new homes (2026-10-31).** The staff design (decision 17) replaced these todo phases:
- 3d (unit folder names) — landed on the names branch at 3f8339f5 and stays unmerged. The row holds the worktree (3q), so no folder comes from a name. Actual folder moves use the corrected command after 3r. The command rebuild waits until 4g.
- 3h (stall watch and review pool) — stall state keyed by staff id in 3x; the pool is deleted in 3p.
- 3i (no showrunner from a folder or branch name) — the resolver in 3s; its readers in 3t, 3u and 3v.
- 3j (transfer journal removed) — 4f.
- 3k (Branch and Worktree cells removed) — the worktree on the row in 3q, the branch from git in 3s, the Units table deleted in 4c.
- 3m (open change rows follow a unit move) — a unit's open change rows hold the unit's staff id in 3x, so they follow its parent.
- 16 (parent files and the seed importer removed) — 4d.
- 17 (unit and joined readers read the record) — 3s, 3t, 3u and 3v.

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

### Phase 3g — Staff readers switch (batch 2: unit membership)  · status: done

**Depends on:** Phase 3f

#### As-built

- Unit membership comes from the staff record. `production_units` (`live_unit_table(lines, slug, *, record)`), `unit_lookup` (`marked_units(slug, *, record)`, `marked_unit`), `dailies_input` and the roster `SessionMarks` take one `StaffSnapshot`. A run makes one owner read. The name lookup is scoped to one production and has no global fallback.
- Unit and showrunner launches carry `STAFF_ID=<row id>` (`add_unit`, `add_showrunner`; `resume_unit` gets its row from `add_unit.ensure_staff_unit`). `STAFF_ID` is in `pane_record.KEPT_NAMES` and `herdr.MEMBER_MARKS`, and `herdr.unmarked_environment` drops it. Panes join rows by `STAFF_ID`.
- A pane with no `STAFF_ID` joins by its `SHOWRUNNER_UNIT_ID` name alone through `staff_sources.member(selector, record)` and ignores its production mark. Two rows with that name give a problem line and no match.
- `unit_lookup.unit_parent(unit, session_id, *, record, sources)` is the one snapshot-aware row-parent resolver. `staff_sources.unit_parent(unit, session_id)` calls it with `staff.snapshot()`.
- `rename_unit.stop_unit` journals `pane_session`, `pane_id`, `terminal_id` and `StopIdentityEvidence` (`RECORDED` | `LEGACY_UNKNOWN`). Recovery is one decision, `_stopped_pane`, over one `Herdr.panes()` read and a fresh `pane_activity`. It refuses a moved pane, a duplicate terminal, another conversation, foreground work or a missing terminal id; the user settles the pane by hand and runs the rename again.
- An unreadable herdr pane list keeps the launch-time place.

**Files:**
- `scripts/production/unit_lookup.py`, `production_units.py`, `staff_sources.py`, `roster.py`, `dailies_input.py` — unit membership from the staff record
- `scripts/production/add_unit.py`, `add_showrunner.py` — launches set `STAFF_ID`
- `scripts/shutdown/pane_record.py`, `scripts/herdr/herdr.py` — `STAFF_ID` kept in launch records, dropped from unmarked environments
- `scripts/production/rename_unit.py` — rename journal pane identity and stop recovery
- `scripts/herdr/fake_herdr.py`, `scripts/production/fake_unit_host.py` — fakes follow herdr 0.9.5 identity rules; the fake has no agent-stop command
- `scripts/production/test_rename_unit_pane_shell.py` — stop recovery refusals

**Binds later work:** A rename of a live unit reads the journal identity shape; a legacy journal with a pending stop refuses with "settle the pane by hand". Worker panes and split-tab shutdown track a pane across a move by `terminal_id`, and close a pane only after a fresh `pane_activity` reads `PaneAtShell`. The replacement for `production_tree.py` keeps the one-owner-read-per-run rule. The no-`STAFF_ID` fallback goes away with the importer on 2026-11-13.

**Gotchas:**
- herdr 0.9.5: an agent exit clears the pane conversation id. `terminal_id` is stable, one per pane. A cross-workspace move changes the pane id, but processes keep the old pane id in their environment. herdr has no `herdr agent stop`.
- `Herdr.panes()` turns an omitted `terminal_id` into `""`. An empty `terminal_id` is incomplete evidence, never absence.
- A transfer leaves the pane production mark stale, so the name fallback must not read it.

**Ruled out:** pane id as identity across a move; a global name fallback outside the production.

### Phase 3l — Workers on the staff record (staff side)  · status: done

**Depends on:** 3g — 3g is the fix for today's handler pane; this phase replaces the reader it fixes.

Inserted 2026-10-30. User (via the sdlc showrunner): "production_tree.py must go away" and "codex can be started and stopped the same way as with claude resumes by name or id". The sdlc showrunner accepted this design on 2026-10-30.

Split 2026-10-31 (unit director, packaging): this phase is the staff side: the worker rows with their writer, names, parents and seed, and the handler command. The call sites, the live reads, the owner fields, the one stop and resume path, the handler switch and the removal of `production_tree.py` are Phase 3n, which waits for the models unit's F024 checkpoint.

#### Work Order

**Goal:** every worker is a staff member, and one staff command prints who reports to whom for the handler.

**Spec:**

1. **Worker rows.** Each worker gets a new staff id from `staff.ensure` (`s…`, as every other row), kind `worker`, a name and a parent. The row holds no thread id: a replaced thread (`codex_mesh.py` `_resume_worker_thread` replacement start, a fresh thread after a failed compaction) keeps its row. `WorkerName.parse` (`staff.py:70`) accepts the live names (`impl`, `impl2`, `<project>-impl`, the Astra instance names). A worker's parent is a unit, or a showrunner for a showrunner-held Astra role (today `staff.py:317` refuses it). One writer, `staff.ensure_worker` and `staff.retire`. Its lock sits after the run-level membership lock and before lifecycle (models unit, F024, 2026-10-31), so the Phase 3n call sites can call it while they hold membership. A pool reviewer's parent is the pool's holder, never the unit that leases it. A unit move or rename keeps the rows, because the unit's row id does not change. A rerun-safe seed adds rows for the workers open now, from the unit ledgers, rosters, the review pool and the Astra records; a second run changes nothing.
2. **One handler command:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py show --json --live` prints `{"schema": 2, "inputs": [<absolute path of every file this run read>], "members": [{"id", "kind", "name", "parent": <an id in this list, null only for the root showrunner>, "role": showrunner|unit|impl|test|review|fix|astra-designer|astra-reviewer, "identity": {"claude": <session id>} | {"codex": <thread id>}, "pool": {"state": busy|idle, "serves": <staff id or null>}}]}`. `pool` is present only on review-pool members; no other member carries a state. Agreed with handler-unit 2026-10-30: one tagged identity, parents inside the list, `inputs` so the handler keeps no copy of the file list, Astra roles kept in the output (the handler skips roles it does not know).

**Files:**
- `scripts/production/staff.py` — `ensure_worker`, worker `retire`, `WorkerName.parse`, worker parents, `show --json --live`
- `scripts/production/staff_sources.py` — the rerun-safe worker seed read from the unit ledgers, rosters, review pool and Astra records
- `scripts/direct/workers.py` — one live unit-director read shared with the seed
- `scripts/production/unit_lookup.py`, `scripts/production/showrunners.py`, `scripts/herdr/herdr.py`, `scripts/shutdown/pane_record.py`, `scripts/message/sessions.py`, `scripts/lib/unit_runs.py` — each reader reports the files it read, for `inputs`
- `scripts/production/staff_test_support.py`, `scripts/production/update_registration_test_support.py`
- `scripts/production/test_staff.py`
- `scripts/production/test_staff_sources.py`

**Acceptance gate:** the affected suites pass; basedpyright 0/0; `staff.py show --json --live` prints schema 2 on a fixture staff record; a second seed run changes nothing.

### Phase 3m — Worker rows now: each admission writes the row; a fill adds the open workers  · status: done

**The worker command (user, 2026-10-31).** This design replaces the admission part of the Work Order below; the worker fill stays, for workers live now only.

User-approved design, 2026-10-31 ~16:55 EDT ("yes go for workers and agents").

One script: scripts/production/worker.py. /unit:worker and /showrunner:worker both run it. The parent comes from the caller's own STAFF_ID (unit director -> its unit row; showrunner -> its showrunner row); nobody types a parent; no name pattern becomes a parent.

Verbs:
- list: the caller's current workers: name, role, busy | idle | stopped.
- assign <role> <prompt>: gives work to the first idle worker in that role (impl, then impl2, ...); resumes a stopped one. When every worker in the role is busy it refuses with one line naming them. It never opens a new worker by itself, except the first use rule below.
- add <role>: one more worker in the role (impl2 after impl). Writes the staff row first; if the row cannot be written, starts nothing (fail closed).
- end <name>: stops the worker and deletes its row. No history in staff (logs stay in run folders).
- restore: brings back every worker that has a row on its same Codex thread or Claude session. A running worker is left alone; a rerun changes nothing, exit 0, one line.
- check: read-only; exit 0 only when every row has a running or resumable worker; prints each wrong row.

Rules:
- The rows are the current worker set. No ended state: ending deletes the row.
- Default team, made on a unit's first assign: impl, fix, review, test (user, 2026-10-31). Astra roles are made on first assign.
- Names: role, then role2, role3, using the lowest free number.
- Row holds name, role, parent, backend identity (Codex thread id + Codex home, or Claude session id).
- Only worker.py starts workers. A Codex or Claude agent started any other way is not a worker: no row, and check ignores it.
- Restore points: `agents up` / `agents restart` (today shutdown.py restart, also reached by herdr-up.sh) call worker.py restore for each session they bring back; retire_unit stops the unit's workers and keeps their rows; resume_unit calls worker.py restore.
- Transport (Codex mesh today, herdr panes later) stays inside worker.py; no command changes when panes land.

Goes after worker.py lands (next step): implement.sh, review.sh, assign.sh/assign.py, workers.py (its selection moves into worker.py: one selector), remove_workers.py, the worker use of agent_bg.sh and design_roles.open_role. /unit:direct and the showrunner commands call /unit:worker and /showrunner:worker.
The one-time worker fill adds rows only for workers live now.


**Depends on:** 3l (`staff.ensure_worker`) and 3q. Not on models F024: `ensure_worker` takes only `staff.json.lock` (`staff.py:734`).

#### Work Order

User order (via sdlc, 2026-10-31 16:2x EDT): "no put workers in staff now" and "the most important thing is getting the staff file correct - everything flows from it". This step goes ahead of 3r's other work and Phase 6. Rows are added only. No reader switches. F024's lock order stays for 3n's end and stop paths.

Goal: every open worker of every live unit has a staff row (kind worker, parent = its unit's row), and each new admission writes its row.

Spec:
1. Admission writes. At each worker admission point call `staff.ensure_worker(name, parent)` (scripts/production/staff.py:734; it takes only staff.json.lock): the Codex open (`codex_mesh.open_unit_worker`, scripts/agents/codex_mesh.py:6450, and the start that opens a thread), the Claude worker open in `assign.py` / `assign.sh`, and the Astra open `design_roles.open_role` (scripts/astra/design_roles.py:721). Call it while no roster, inbox or lifecycle lock is held (the docstring at staff.py:735). The parent is the unit's staff row, found with `staff_sources.member(UnitMember(slug, unit), staff.snapshot())` from the run's own unit facts or the director's `STAFF_ID`; never from a name pattern. A rerun of the same admission is a no-op (ensure). Fail closed (user, 2026-10-31: "without fail"): the open writes the row first; when the parent cannot be found or the staff write refuses, the open refuses with one line `staff: <worker>: not opened: <reason>`, exit 2, and starts nothing. Every open path shares the one admission function. Each raw worker start takes a typed `WorkerAdmitted` value that only `ensure_worker` returns, so the type checker refuses a start without a row, and a repo-scan test fails on any raw start outside the start functions. A parent is a unit or a showrunner (showrunner-held Astra roles).
2. Worker fill. New `scripts/production/worker_fill.py` (`fill` and `fill --dry-run`): read the current worker ledgers of each live unit (the unit worker ledger and roster, docs/as-built/long-lived-workers.md; the Astra role records) and add a row for each open worker under its unit. A worker whose unit or name cannot be placed is written nowhere and listed as `<source>:<line or key> <worker>: <what is missing>`; exit 1 while the list is not empty, after writing every complete row. A rerun of a finished fill prints one line `unchanged` and exits 0. Showrunner-held Astra roles: list them (parent rule waits for sdlc), do not write.
3. No reader changes. `staff.py show` lists the workers under their units.

Files: scripts/agents/codex_mesh.py (models unit's file: keep the diff small; models reviews it before merge), scripts/direct/assign.py, scripts/astra/design_roles.py, scripts/production/worker_fill.py (new), scripts/production/test_worker_fill.py (new), scripts/agents/test_codex_mesh.py, scripts/astra/test_design_roles.py, scripts/direct/test_assign_launcher.py. Not staff_sources.py (3r holds it).

Acceptance gate: basedpyright 0/0 on changed files and every caller module; named suites pass; tests: admission writes the row with the right parent, a rerun writes nothing, a missing parent opens the worker and prints the line, the fill places each ledger worker, lists the unplaceable ones and exits 1, a finished rerun prints `unchanged` exits 0 and changes no byte; real run: `worker_fill.py fill --dry-run` then `fill` on the live staff file, `staff.py show` lists the workers under their units, a real rerun prints one line and exits 0.

**One assign command (user, 2026-10-31).** `assign` (`scripts/direct/assign.py`, `assign.sh`) is the only way a worker is added to a unit or a showrunner. It writes the staff row first and starts nothing when it cannot. A Codex or Claude agent started any other way is not a worker: it gets no row, and the staff check ignores it. The old launch scripts go (user, 2026-10-31: "we get rid of the former (ancient) delegate or review or whatever other scripts exist"): after 3m, one step lists every script that starts or dispatches a worker, moves its callers and command docs to `assign`, and deletes it.

### Phase 3n — Workers on the staff record (call sites)  · status: done (models-unit, 19e69a3eb; shrink d963d30aa)

#### As-built

models-unit built this phase to the staff-lifecycle spec. Its as-built is `docs/as-built/long-lived-workers.md`. In short:
- The staff row is the one record of a worker. Rosters, the worker ledger, run records, inboxes and design-role records store no thread id, cwd, owner or family.
- `StaffRow.parent` is `ReportsTo(id) | StaffRoot`. `staff.ensure_worker(identity, name, parent)` publishes a worker at the `worker_admission.py` boundary. Adoption is one write of the worker row's `parent`.
- No row carries a role, no `"backends"` section is stored, the storage schema stays 2, and there is no `adopt_worker`. `StaffSnapshot.backends` finds a backend from `current_session`.
- `config/staff.conf` holds `OPERATION_LOCK_SECONDS` and `REMOTE_SECONDS`; each wait uses the smaller of the configured limit and the remaining operation deadline.

### Phase 3p — Review pool deleted  · status: done

**Depends on:** none. Order it with the handler unit (it reads the `pool` key of `staff.py show --json --live`) and the models unit (`workers.py` is its file).

#### Work Order

**Goal:** there is no review pool: no code, state, flag or rule text. Each unit's reviews use its own `review` and `review2` workers (user, 2026-10-31: "there should be no such thing as a review pool").

**Spec:**
1. **Find the last writer first.** `~/.local/state/review-pool/sdlc/pool.json` changed at 07:09 on 2026-10-31, but `review.sh:94` already ignores `--pool code` and `--lease`. Before any delete, find the script or process that wrote it (the callers in item 4, `pool.lock` holders, the mesh logs) and move it to the unit's own reviewer. The checkpoint notice names it.
2. **No live lease.** Before any state goes, one check reads every `~/.local/state/review-pool/*/pool.json` and prints one line for each reviewer whose `busy_pid` is a live process or whose `lease` names a review that still runs. The state is deleted only when the check prints nothing. Then the pool's template and reviewer threads (the `thread_id` values in each `pool.json`) end through `codex_mesh.py end`, and `~/.local/state/review-pool` is removed one production folder at a time. The check and the removal are one throwaway command in the unit's session folder, not a shipped tool.
3. **Delete** `scripts/direct/review_pool.py`, `scripts/direct/review_pool_report.py`, `test_review_pool.py`, `test_review_pool_report.py`, `test_review_launcher_pool.py`, `config/review_pool/ui.md` (`review_pool.py:337` is its only reader; `config/review/code.md` stays when another reader is found, else it goes too), `docs/as-built/review-pool.md` and `docs/as-built/review-pool-probes.md`.
4. **Remove every caller:** `scripts/direct/workers.py` (:38, :1313-1320); `scripts/production/staff_sources.py` (:26, :498-517: the `pool` key leaves `show --json --live`, its schema number goes up by one, agreed with the handler unit first); `scripts/production/rename.py` (`REVIEW_POOL_STATE` :58, the `review_pool` move :75, :252, :404); `scripts/production/rename_unit.py` (:58, :621-622); `scripts/production/retire_unit.py` (:16, :80, :364); `scripts/production/transfer_unit.py` (:50 and the journal's pool step); `scripts/agents/mesh_runs.py:138`; `scripts/hierarchies/worker_owners.py:486`; `scripts/message/roster.py:830, 1349`; `scripts/production/unit_rename_sweep.py:24`; and the fixtures `review_launcher_test_support.py`, `rename_test_support.py`, `rename_unit_test_support.py`, `transfer_unit_test_support.py`, `staff_test_support.py`.
5. **Flags and text.** `review.sh` loses `--pool` and `--lease` (:12, :73-94); a caller that passes one gets exit 2 and one line that names the flag. No command or doc tells anyone to use them. The rule text goes from `commands/showrunner/retire_unit.md:29`, `commands/showrunner/transfer_unit.md:33, 44` and `docs/direct/phase_end.md:49`.

**Files:**
- `scripts/direct/review_pool.py`, `scripts/direct/review_pool_report.py`, `scripts/direct/test_review_pool.py`, `scripts/direct/test_review_pool_report.py`, `scripts/direct/test_review_launcher_pool.py`, `config/review_pool/ui.md`, `docs/as-built/review-pool.md`, `docs/as-built/review-pool-probes.md` — deleted.
- `scripts/direct/review.sh`, `scripts/direct/workers.py`, `scripts/production/staff_sources.py`, `scripts/production/rename.py`, `scripts/production/rename_unit.py`, `scripts/production/retire_unit.py`, `scripts/production/transfer_unit.py`, `scripts/production/unit_rename_sweep.py`, `scripts/agents/mesh_runs.py`, `scripts/hierarchies/worker_owners.py`, `scripts/message/roster.py`, `scripts/production/production_tree.py`, `scripts/shutdown/stop_resume.py` — the pool parts go (`production_tree.py` stays until Phase 4b, so its pool reader goes here).
- `scripts/production/role_terms.toml` — the review pool term entries go.
- `commands/showrunner/retire_unit.md`, `commands/showrunner/transfer_unit.md`, `docs/direct/phase_end.md` — the rule text goes.
- `scripts/direct/review_launcher_test_support.py`, `scripts/production/rename_test_support.py`, `scripts/production/rename_unit_test_support.py`, `scripts/production/transfer_unit_test_support.py`, `scripts/production/staff_test_support.py` — pool fixtures go.
- `scripts/direct/test_review_launcher.py`, `scripts/direct/test_workers.py`, `scripts/production/test_staff_sources.py`, `scripts/production/test_rename.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/test_unit_rename_sweep.py`, `scripts/agents/test_mesh_runs.py`, `scripts/hierarchies/test_worker_owners.py`, `scripts/message/test_roster.py` — pool tests go.

**Workers:** `1 writer + 1 tester`
- `impl` — every non-test file above; hub: `scripts/production/staff_sources.py` (the `show --json --live` schema the handler reads).
- `test` — the fixtures and `test_*.py` files above.

**State:** removes `~/.local/state/review-pool` and ends the pool's Codex threads, after the live-lease check.

**Constraints from prior phases:** Delegation Context → Staff design (2026-10-31). Phase 3l built the `pool` member key of `show --json --live`; it goes here.

**Acceptance gate:** `grep -rnE 'review_pool|review-pool|REVIEW_POOL|--pool|--lease' scripts commands config docs/direct | grep -v '^scripts/feature_log/'` finds nothing (the feature-log CLI has an unrelated `--pool` flag); `grep -n pool scripts/production/production_tree.py` finds nothing; `test_review_launcher*`, `test_workers*`, `test_staff*`, `test_rename*`, `test_retire_unit*`, `test_transfer*`, `test_roster*` and `test_mesh_runs*` pass; basedpyright 0/0 on changed files and every caller module; the live-lease check on natedev prints what it found, and a fixture with a live `busy_pid` stops the removal; the removal stopped after the first production folder and run again removes the rest and exits 0, and a rerun after it changes nothing and exits 0; one real review by a unit's own `review` worker passes after the delete; the real handler pane shows the hierarchy with no `pool` key; full gate green.

### Phase 3q — The staff row holds the worktree and the doc; `.unit.toml`; fill of live members  · status: done

#### As-built

- Schema 2 holds `PresentWorktree | RemovedWorktree | WorktreeUnfilled` on units and `LiveDoc | WrappedDoc | DocUnfilled` on showrunners. Schema 1 remains readable. Unfilled fields stay absent on a write. Setters and lookups are in `staff.py`.
- `unit_config.py` is the one `.unit.toml` and parked-file reader and writer. It preserves plan, port and responsibilities, verifies publication, and refuses unequal recovery files. Park and restore keep the source until the lifecycle step finishes.
- `git_exclude.py` plans the shared exclude entries once for preview and publication. It refuses a tracked file or an effective negation. Lifecycle and style use this writer. `tunables.py` owns the `KEY=value` parser and required numeric reads.
- The live fill starts at each existing staff row. A timer selects a showrunner's live merge checkout and doc. A unit takes its location from that doc's Units row or its proved live session folder when the row is stale. The fill refuses unequal existing fields, lists unfilled rows with reasons, continues complete rows, and resumes a stopped row-to-file publication. A finished rerun changes nothing. Failed timer evidence keeps its durable owner id, including nested showrunners.
- The stored showrunner doc is the one retired source. Scratch copies, other worktrees and branch-tip copies cannot override it. Missing owners and historical facts stay listed. New retired rows belong to Phase 3h; this phase has no pending writer assignment.

### Phase 3k — `.unit.toml` writers emit taplo-formatted text  · status: done (models-unit 3ed6b4e3c on sdlc; live proof by style-unit)

**Evidence (style-unit, 2026-11-04):** `taplo fmt --check` passes on 49 of 49 `.unit.toml` files; a `staff_sources` dry-run prints unchanged on natedev and on the Mac.

**Owner:** models-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3q. Independent of rename order; lands before the configuration-completeness audits (4c, 4d). Live repair (sdlc, 2026-10-31): the 16:24 fill wrote `.unit.toml` in a format that `taplo fmt --check` rejects under each repository's own `taplo.toml` (`align_entries`, `reorder_keys`).

#### Work Order

**Goal:** record any missing live acceptance evidence for the formatter shipped in `3ed6b4e3c`.

**Spec:**
1. Keep the shipped `unit_config._publish` formatted-byte comparison and its repository-formatting tests.
2. Record `taplo fmt --check .unit.toml` in each filled worktree on natedev and the Mac.
3. Record a second fill rerun that prints `unchanged` and changes no bytes.

**Files:**
- `scripts/production/unit_config.py` — read only: live acceptance evidence only; no formatter implementation remains.

**Workers:** `1 writer` — `impl` records the live evidence.

**Acceptance gate:** record any missing live proof; keep the shipped formatting and unchanged-rerun tests green.

### Phase 4k — The tracked TOML taplo test fails on the host  · status: done

**Depends on:** none. natedev ask, 2026-11-02 (messaging finding F009). `test_verify_lint_taplo.py` `test_badly_formatted_tracked_toml_with_spaces_and_newlines_still_fails_lint` (from the 35b13cff0 lint scope change) fails when a Codex worker runs it detached with `mesh_runs.py start`. taplo prints its INFO lines and no ERROR line, so the assertion on "not properly formatted" fails. Attached runs pass.

#### Work Order

1. Reproduce the failure detached, as messaging ran it.
2. Find why taplo prints no ERROR line there, and fix the test at the cause. The test must still prove that taplo flagged the tracked newline file.
3. The test passes attached and detached.

**Result (2026-11-02):** not reproduced. The test passes attached and detached on main 750f8cfcf, and attached on messaging's tree bc0fe153a. taplo 0.10.0 prints the ERROR line with exit 1 in every probe. No test change: the test passes against the real tool. A recurrence needs the run record while it is fresh, for the taplo child status.

### Phase 3r — Writers keep the worktree, the doc and `.unit.toml` current; retired rows filled  · status: done

**Depends on:** 3q.

#### Work Order

**Goal:** every tool that adds, retires, resumes, wraps, promotes or renames a unit or a showrunner writes the row fields and `.unit.toml`; retired units and wrapped productions keep their rows, the readers count only active rows, and the retired backfill fills them; retire parks the file and resume brings it back with its session facts kept until the launch is proven; every launch, resume and move this phase names leaves a pane launch record that carries `STAFF_ID`. The Units table is still written too, until Phase 4c.

**Spec:**
1. **Admission writes the field in the same transaction.** `staff.ensure` takes the new member's `worktree` (unit) or `doc` (showrunner), so a stop between the row and its setter cannot happen. `add_unit.py` makes the worktree first (`ensure_worktree`, :792), then writes the row with its `PresentWorktree`, writes `.unit.toml` from `--plan`, `--standby`, `--permanent`, `--port` and a new `--responsibility <name>` (repeatable), and calls `git_exclude.ensure_excluded` with its two checks (Phase 3q item 4). An existing `.unit.toml` with other values is refused with one line that names the key, as `row_is_present` (:428) refuses a changed Port today. `--resume` keeps the file. `add_showrunner.py` and `promote_showrunner.py` write the showrunner row with its `LiveDoc`. In text and messages that this phase changes, "unit marks" and "marked unit" become "session labels" (`add_unit.py:1001` "the marked unit's live session" says "the session that carries the unit's session label"; `test_add_unit_rerun.py:109` follows).
2. **Active rows first.** Before any retired row exists, the readers count only active rows: `unit_marks.production_members` (:216) and `production_units.ProductionMembers` (:95-117) take only unit rows with `PresentWorktree` or `WorktreeUnfilled`, and only showrunner rows with `LiveDoc` or `DocUnfilled`; every other `staff.find(kind="unit")` and `find(kind="showrunner")` caller that lists members (a search at dispatch lists them in the checkpoint notice) does the same through one helper, `staff.active(record, kind)`. A retired unit is listed only where a reader lists retired units today.
3. **Retired rows stay (user, 2026-10-31; this supersedes the Phase 1 rule "a retired member has no row").** `retire_unit.py:768` sets `RemovedWorktree` in place of `staff.retire(member.row.id)`; `staff.retire_pending()` (`staff.py:672`) and the `pending_retire` list set `RemovedWorktree` when a unit's last worker leaves, and never delete the row; wrap (`production_lifecycle.py wrap_production`) sets `RemovedWorktree` on each unit and `WrappedDoc` on the showrunner and keeps every row and parent (wrap's parent retention in `wrap_release.json` keeps working); a promotion that ends a unit row keeps it with `RemovedWorktree`. `staff.retire` stays for worker rows only and refuses a unit or showrunner row.
4. **`retire_unit.py`** reads the branch from git in the worktree, finds the session (`last_session`) and its folder (`transcript_resume_folder` over `transcript_locations`, :454, :460), and calls `unit_config.park` with `[retired]` (date, session, folder, branch) before it removes the worktree and its branches; then it sets `RemovedWorktree`. A rerun after the park and before the worktree removal removes the worktree and changes nothing else; a rerun after the removal sets the field.
5. **`resume_unit.py`** refuses before any change, with one line, when neither file exists: `<unit>: no .unit.toml in <worktree> or ~/.local/state/staff/units/<staff id>.toml; run staff_sources.py fill --retired or ask the showrunner`. Else, in order: remake the worktree on the parked `branch` from the merge branch; `unit_config.restore`; set `PresentWorktree`; launch with the parked session and folder; and only when the launch is proven (the new pane launch record carries `STAFF_ID=<the row's id>` and the session runs) `unit_config.discard_parked`. A stop at any step keeps the parked file, so the rerun has the session and folder; a rerun finds the finished steps by the worktree, the field and the running session, and does only the rest. A rerun of a finished resume changes nothing and exits 0.
6. **Wrap** parks each unit's file and sets `RemovedWorktree` before its staff step (item 3).
7. **Rename keeps the fields current until Phase 4g.** Today's rename moves folders, so between Phase 3q and Phase 4g it keeps the new fields true: `rename_unit.py` `_move_git` (:536) calls `staff.set_worktree` with the new path in the same journal step, after the `git worktree move` and before the step is marked done; `rename.py`'s doc move calls `staff.set_doc` the same way. A rerun after the move and before the set sets the field. A transfer changes only the parent and writes no field.
8. **Retired backfill (`staff_sources.py fill --retired`).** It runs after items 2-7 land, with the Phase 3q manifest and the same "no likely value" rule:
   - Each retired Units row (the plan cell starts with a retired note): a unit row under its production's showrunner row when none exists, `RemovedWorktree` from the Worktree cell, and a parked `.unit.toml` with `[retired]`: the date and session from the note, the branch from the Branch cell, and the folder from the note or, when the note has none, from the transcript folder of that session (`retire_unit.transcript_locations` and `transcript_resume_folder`). A note with no session id takes `retire_unit.last_session` (`resume_unit.py:104`).
   - A wrapped production with no showrunner row gets one with `WrappedDoc` and no timer, so its retired units have a parent.
   - A row with a missing fact (no session found, no Branch cell, no folder found) is written nowhere and listed for sdlc. The run exits 1 while the list is not empty.
9. **Tunables.** `retire_unit.py`'s `POLL_SECONDS` (:84), `TERM_WAIT_SECONDS` (:86) and the 900 s subprocess timeout (:150) move to a new `config/production.conf`, read at use through `tunables.number`; each wait is `min(configured limit, remaining operation deadline)`, and each value has a changed-config test.
10. Every write here is rerun-safe through Phase 3q's writers; a rerun of a finished tool changes nothing and exits 0.
11. **Every launch path records `STAFF_ID` (sdlc showrunner, 2026-10-31).** Measured on 2026-10-31: 0 of 418 pane launch records under `~/.local/state/herdr-panes/` carry `STAFF_ID`, and the unit-board merged board reads it and finds nothing. The user saw a session's record show `unassigned` in place of its showrunner: herdr-unit's own row has its parent, but its pane carries no `STAFF_ID`, so the reader cannot join the session to the row. `add_unit.py:907` and `add_showrunner.py:198` put it in the launch environment, and `pane_record.KEPT_NAMES` (`pane_record.py:50`) keeps it. Cause, measured on natedev 2026-10-31 (herdr-unit): `restart.py` `_resume_parts` (:764-765) built the relaunch environment from the account, `ENABLE_TOOL_SEARCH` and the probe mark only, so the 11:59 restart started every session with no `STAFF_ID`, `SHOWRUNNER_UNIT` or `SHOWRUNNER_UNIT_ID`. The SessionStart hook (`session-start-pane-record.py:37` through `pane_record.launch` :263) then wrote each pane record from the unmarked process, the 12:09-12:13 account moves relaunched from those records (`stop_resume._start_session` :205), and each compaction or `/clear` wrote the record again, which undid the record backfill. `herdr.launch_agent` and `agent_base_environment` (:3378) are not the cause: they apply the marks the caller passes. The shutdown unit's hotfix (2026-10-31) fixes it at the cause: one `pane_record` helper is the one source of the labels for every relaunch path (`restart._resume_parts`, `stop_resume.resume` and `_start_session`, `relaunch.py`, `pane_restore._resume` :61); `pane_record.write` keeps a conversation's labels when the hook writes from an unmarked process; a repair command writes the records and starts nothing; `herdr_processes` heals at read time. This phase changes neither `herdr.py` nor the hook. It proves the paths it owns: `add_unit.py` (a new unit, and `--resume`, which `resume_unit.py` and `/showrunner:promote_unit` run) and `add_showrunner.py` (which `promote_showrunner.py` runs) put `STAFF_ID` in the launch environment (`add_unit.py:907`, `add_showrunner.py:198`), and the account move (`stop_resume.move` :463 and `move_sessions` :405) gives the new record the same `STAFF_ID` as the old one through the shutdown unit's helper. Phase 4 keeps the names part (item 3 there). **The shutdown unit owns the restart and relaunch path (`restart.py`, `relaunch.py`, `pane_restore.py`, `pane_record.py`, `stop_resume.resume` and `_start_session`), the record repair and the relaunch of live sessions, which the showrunners time; this phase does not plan them.** Their backfill proof (Phases → Three kinds of order) is a prerequisite of Phases 4d and 4e. A change to `stop_resume.py` for the move is agreed with the shutdown unit first (Invariants → Owners).

**Files:**
- `scripts/production/staff.py` — `ensure` with the field, `active`, `retire` for workers only, `retire_pending` keeps the row.
- `scripts/production/session_staff.py`, `scripts/production/production_units.py` — the active filter.
- `scripts/production/add_unit.py`, `scripts/production/add_showrunner.py`, `scripts/production/promote_showrunner.py`, `scripts/production/retire_unit.py`, `scripts/production/resume_unit.py`, `scripts/production/production_lifecycle.py`, `scripts/production/rename_unit.py`, `scripts/production/rename.py` — the writes.
- `scripts/production/staff_sources.py` — `fill --retired`.
- `config/production.conf` — new: the retire time limits.
- `commands/showrunner/add_unit.md`, `commands/showrunner/retire_unit.md`, `commands/showrunner/resume_unit.md` — `--responsibility` and the park and restore lines.
- `scripts/production/test_add_unit.py`, `scripts/production/test_add_unit_rerun.py`, `scripts/production/test_add_showrunner.py`, `scripts/production/test_promote_showrunner.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_resume_unit.py`, `scripts/production/test_production_lifecycle.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_rename.py`, `scripts/production/test_staff.py`, `scripts/production/test_staff_sources.py`, `scripts/production/add_unit_test_support.py` — fixtures and regressions.
- `scripts/herdr/herdr.py`, `scripts/hooks/session-start-pane-record.py`, `scripts/shutdown/stop_resume.py` — read only for item 11 (the cause is fixed by the shutdown unit's hotfix); `stop_resume.py` is the shutdown unit's file.
- `scripts/herdr/test_herdr.py`, `scripts/shutdown/test_stop_resume.py` — item 11 tests.
- `scripts/production/unit_config.py`, `scripts/lib/git_exclude.py`, `scripts/lib/tunables.py`, `scripts/shutdown/pane_record.py` — read only.

**Workers:** `1 writer + 1 tester`
- `impl` — the tools, the three command files and the item 11 files; hub: `scripts/production/add_unit.py` (resume and promote launch through it).
- `test` — the `test_*.py` files and `add_unit_test_support.py`.

**State:** row fields, `.unit.toml` files, parked files; retired and wrapped rows kept; worktree removal and remaking; `STAFF_ID` in the pane launch records of new launches and moves.

**Constraints from prior phases:** Phase 3q (`set_worktree`, `set_doc`, the unfilled states, `unit_config.park`, `restore`, `discard_parked`, `place`, `git_exclude.ensure_excluded`, `tunables`, the fill and its manifest). Phase 3 (each tool writes its row and checks the staff step before any change). Delegation Context → Staff design (2026-10-31), Rerun-safe tools, Lifecycle reruns keep the pane, No hard-coded config.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; the named suites pass; the active-unit list of `live_units.py` and of the roster on natedev is the same before and after the retired backfill (the backfill adds no active unit); a real add, retire and resume of a throwaway unit: the file sits in the worktree, then in `~/.local/state/staff/units/<staff id>.toml`, then back, the row's worktree state follows, the row stays after the retire, and the resumed worktree is on the branch it had; the backfill proof: the parked file of `guild-process-unit` holds session `842c2149-c59b-4abd-934c-ba61d14944ad`, its folder (from the transcript folder) and its branch, and a real `resume_unit.py sdlc guild-process-unit --dry-run` passes from the parked file alone; a resume with neither file refuses with the line above and changes nothing; a retire stopped after the park, a resume stopped after the restore and a resume stopped after the launch and before `discard_parked` each finish on rerun, and the last keeps the session facts until then; a real `rename_unit.py` of a throwaway unit leaves the row's worktree on the moved folder, and one stopped after the folder move sets it on rerun; `fill --retired` lists every missing fact for sdlc, a stop after one unit finishes on rerun, and a rerun of a finished run prints `unchanged` and exits 0; a rerun of each finished tool changes nothing and exits 0; `staff_sources.py fill --dry-run` then reports `unchanged`; a changed `config/production.conf` value takes effect with no code change; item 11: a real `add_unit.py` launch of a throwaway unit, a real `add_showrunner.py` launch, a real `resume_unit.py` and a real account move each leave a pane launch record whose environment carries `STAFF_ID=<the row's id>` (read from `~/.local/state/herdr-panes/`), a real relaunch of herdr-unit (or a throwaway unit) then shows its right showrunner, not `unassigned`, in each session reader that printed `unassigned` (`roster.py`, `usage_report.py`; the checkpoint notice names the one the user saw), and a rerun of each changes nothing and exits 0; full gate green.

### Phase 3i — Read-only staff check lists every wrong row  · status: done

**Depends on:** 3m. Runs beside 3r (sdlc showrunner, 2026-11-01); lands after it, since both change `staff_sources.py`.

#### Work Order

**Goal:** one read-only command, `staff_check.py`, lists every wrong staff row and exits 0 only when every row is right, so sdlc can see what is still wrong.

**Spec (sdlc showrunner, 2026-11-01):**
1. Safe to rerun: it writes no file, row, mark or lock.
2. One row per live session: each live Claude or Codex session that carries a `STAFF_ID` names exactly one existing row, and one row has at most one live session.
3. Each parent is live where the row is live, and is the correct kind. A record the staff reader refuses (a missing parent, a wrong parent kind) exits 2 with the reader's reason.
4. Each unit row's worktree, and each showrunner row's doc, exists and belongs to that row; a retired row's `RemovedWorktree` is right.
5. Each live worker is under its unit's row. Worker liveness is the shared scanner's answer (`staff_sources.observe_workers`), the same one the fill uses.
6. It lists wrong rows, one line each (`<row id> <name>: <what is wrong>`), then one count line; evidence it cannot settle is listed as uncertain, never filled in. Exit 1 when it lists a line, 2 when a source cannot be read.
7. A new file that reads staff through the one existing reader (`staff.snapshot()` and the existing joins), with no copy of their logic. Any strict read it needs leaves the existing callers' behavior as it is.

**Files:** new `scripts/production/staff_check.py` and `scripts/production/test_staff_check.py`; the smallest accessors in `scripts/production/staff_sources.py`, `scripts/production/session_staff.py`, `scripts/production/showrunners.py`, `scripts/herdr/herdr.py`.

**Workers:** `1 writer` — `fix`, in its own clone.

**Acceptance gate:** basedpyright 0/0 on changed files and their callers; `test_staff_check.py`, `test_staff.py`, `test_staff_sources.py` pass; a real run on natedev lists only real wrong rows (its output goes to sdlc); two runs print the same bytes and change nothing on disk.

### Phase 3j — Today's staff passes the staff check  · status: done

#### As-built

- `staff_check.py` treats a Codex app-server process as a host of worker threads, never a second session of a row; Codex top-level value options (`--local-provider`, `--remote`, `--remote-auth-token-env`) come before the subcommand and are skipped with their values.
- The retired fill in `staff_sources.py` takes each owner from its showrunner row and that row's bound document; a document bound under a scratch root gives one line and adds nothing (`source_allowed()` is the one scratch-root check, `showrunners.SCRATCH_ROOT`). Its dry run lists the retired rows it would add.
- Live data, under the staff lock with a backup before each write: the three moved unit rows carry the ids their live sessions carry, under sdlc; 82 paused-worker rows and 4 empty worktrees are filled; the organon duplicate row is merged into geometry-material.

**Files:**
- `scripts/production/staff_check.py` — the one staff check.
- `scripts/production/staff_sources.py` — row-outward fill and the retired preview.

**Binds later work:** the retired fill's one source rule: the document a showrunner row names is the only source, checked by `source_allowed()`. Retired rows the dry run lists, and historical facts with no evidence, stay listed with no row until the native session-id work writes them.

**Gotchas:**
- A wrapped showrunner's name can differ from its document slug (`fps` → `performance-production.md`).
- A resume that does not reuse the saved `STAFF_ID` makes a duplicate unit row.

**Ruled out:** a store registry or identity journal for `/clear`; keeping the old launcher names as thin entry points.

### Phase 3o — The staff check skips a delegate run with no server  · status: done

**Depends on:** 3j

#### As-built

`_WorkerSources.skip_serverless_delegate(run)` in `scripts/production/staff_sources.py` marks a run under `unit_runs.FORMER_RUN_ROOT` that has no `mesh_server.json` as inactive and adds it to `skipped_runs`, before any roster of that run is read. `ledger()` calls it before the HeldBy roster read, and `run_live` calls it first. Only `FileNotFoundError` proves absence; any other stat error stays uncertain, never a skip. The skip shows in the one existing `worker_fill.py` line (`len(skipped_runs) + len(skipped_ledgers)`); `staff_check.py` prints nothing for it. A unit run, and a delegate run with `mesh_server.json`, keep their behavior.

**Files:**
- `scripts/production/staff_sources.py` — the shared skip helper.
- `scripts/production/test_staff_sources.py` — serverless delegate run and HeldBy ledger tests; a unit run with no server stays uncertain.
- `scripts/production/test_staff_check.py` — no line without a server; the role problem still shows with a live server.

**Gotchas:** `collect()` reads worker ledgers before run folders, so a run check placed only in `run_live` misses a HeldBy worker.

**Ruled out:** a new skip line kind.

### Phase 3h — Staff row ids are the Claude session and Codex thread ids  · status: done

#### As-built

- A staff row's `id` is the member's first native id: the Claude registry `sessionId` or the Codex backend thread id. It never changes. `current_session` holds the live native id. Staff generates no id, and no process exports `STAFF_ID`.
- `staff.update_current_session(old, new)` changes only `current_session`, in one atomic `staff.json` write under the staff lock. Parent links keep the fixed ids, so a `/clear` changes no child row. A repeated update changes nothing.
- `StaffSnapshot.by_current_session(session)` is the strict runtime join. `StaffSnapshot.by_member_reference(reference)` resolves a stored reference (launch record, restart record): fixed id first, then current session. `unit_parent` raises `StaffUnreadable` when the reference names a row that is not a unit, and falls back to the unit name when no row matches. The accounts board uses the same rule and keeps its unit-mark fallback.
- `worker_admission.WorkerStartAllowed` proves the read-only admission checks passed; `PublishedWorker` proves one staff transaction published the native id and row. Every raw starter takes `WorkerStartAllowed`; dispatch and roster publication take `PublishedWorker`. A new Claude worker, unit or showrunner starts with no `--session-id`; its SessionStart input and process registry supply the id.
- `scripts/hooks/staff_session_identity.py` runs on SessionStart and SessionEnd with `clear`. It proves the old id from the pane's launch observation (Claude pid and process start) and the new id from hook input and the registry, then calls `update_current_session`. SessionEnd only reads. With no proof it changes no row and prints one reason; `staff_check` lists the new session as incomplete until the update lands.
- `scripts/production/migrate_staff_ids.py` is a one-time move: backup, staff operation lock, dry run, reference stores first and `staff.json` last; a finished rerun prints `unchanged`. Members with missing facts stay listed and get no row.

**Files:**
- `scripts/production/staff.py` — `update_current_session`, `by_current_session`, `by_member_reference`.
- `scripts/production/worker_admission.py` — `WorkerStartAllowed`, `PublishedWorker`.
- `scripts/production/session_staff.py`, `scripts/production/unit_lookup.py`, `scripts/production/staff_sources.py`, `scripts/accounts/accounts.py` — runtime and stored-reference joins.
- `scripts/hooks/staff_session_identity.py`, `settings.json` — the clear hook and its SessionEnd entry.
- `scripts/production/migrate_staff_ids.py`, `scripts/production/test_migrate_staff_ids.py` — the one-time migration.

**Binds later work:** A reader joins a live session by `current_session` and a stored reference by `by_member_reference`; none reads `STAFF_ID`. The live migration runs on main before the `unit <id>` lookup phase. The phase that removes one-time scripts deletes `migrate_staff_ids.py`.

**Gotchas:** Tests that reach herdr or real staff state fail only under `HERDR_BIN=/nonexistent/herdr`; gate with it. A read-only lookup must not clean up records. A checkpoint that changes test assertions needs one `Assertion-Change:` line per test. A code change that reads a new record shape and the live move that writes that shape land in one step: 3h's `_ensure_row` compare ran on main before the migration, and worker admissions in that gap were refused (sdlc, 2026-11-01).

**Ruled out:** rekeying the row id on `/clear`; an alias table or `STAFF_ID` copy; a persisted clear handoff journal.

### Phase 3a — `unit <id>` tells the user about one member  · status: done

#### As-built

- `unit <id or unique prefix>` prints ten lines for one staff row: Name, Kind, Id, Parent, Machine, Worktree, Branch, Plan, Live tab and Current session. It takes one `staff.snapshot()` and writes nothing.
- Matching: `record.by_member_reference(target)` wins; otherwise prefix matches over `id` and `current_session`, one per row. No match prints `unknown id: <id>`; several print `ambiguous id: <id>: <up to 5 "name (id)", sorted by id>, and N more`. Both exit 1. A usage error exits 2; an unreadable staff record prints `unavailable: <cause>` and exits 1.
- Each field is a value, `not applicable` or `unavailable: <cause>`, never blank. A worker shows its parent's worktree with ` (inherited)`. A retired unit whose folder is gone shows its parked branch with ` (parked)` and a Live tab of `not applicable`.
- The Live tab reads herdr's native session of each pane. Panes it cannot read print one stderr line, `anomaly: N panes unreadable (<names>); not blocking`; with no match the field reads `none found (N panes unreadable)`.
- A member on the other machine is read over ssh (`remote.other_machine()`), which runs `local_details` there with the rows as JSON; a failed read gives `unavailable: <cause>` fields.
- `~/.local/bin/unit` links to `~/.claude/scripts/production/unit` on natedev and the Mac.

**Files:**
- `scripts/production/unit_command.py` — `main`, `describe`, `read_details`, `local_details`, `read_tab`, `read_branch`, `read_config`, `MemberDetails`.
- `scripts/production/unit` — the executable entry; it runs `unit_command.py` through `~/.claude/scripts/lib/py`.
- `scripts/production/test_unit_command.py` — its tests, with fixtures where `id` differs from `current_session`.

**Binds later work:** `unit` and `staff_check` are read-only and never call `staff.update_current_session`; the Codex thread reconciliation owns that write. Unreadable panes are counted and reported, never a refusal.

**Gotchas:** `.unit.toml` reads use `cleanup=False`, so a lookup never removes temporary files. Branch reads use `git --no-optional-locks`.

### Phase 4n — Reconcile a pane's current Codex thread  · status: done

**Owner:** herdr-unit. Landed in ecbad8b15 (2026-11-02).

**As built:** `Herdr.native_pane_process(pane, kind, timeout)` in `scripts/herdr/herdr.py` returns the one proved foreground `NativeAgentProcess` (identity, start time, launch environment). `native_session` reads through it. Five tests in `scripts/herdr/test_herdr.py` cover pid selection, the open rollout, the launch fallback, a changed process and failed evidence.

**Scope cut (sdlc, 2026-11-02):** the restart reconcile step was removed. Restart keeps the stored thread of a Codex pane row. No source proves an idle Codex window's thread:
1. A Codex window attached to the managed app-server daemon holds no rollout file, even during a turn; the daemon holds it.
2. An idle in-process window closes its rollout between writes.
3. The Codex v0.166 state and history databases, session_meta, writer locks, logs and the daemon's `thread/loaded/list` have no client pid.

**Known gap:** a Codex pane's thread change (`/resume`, `/new`) is not seen until Codex offers a read-only thread query. The upstream ask is a proposal in `herdr-worker-panes-next.md`.

### Phase 3s — The one join answers from the row  · status: done

#### As-built

- `session_staff.RowResolver` holds one `StaffSnapshot` per command run and resolves each row's facts: `unit_worktree(row) -> PresentWorktree | RemovedWorktree | WorktreeUnfilled`, `unit_branch(row) -> Branch | BranchUnreadable | LocationUnfilled | RetiredFactsUnavailable` (git; one ssh call per remote machine for all its units), `unit_config(row) -> UnitConfig | ConfigMissing | NoUnitConfig | LocationUnfilled | RetiredFactsUnavailable`, `production_doc(row) -> ProductionDoc | DocumentUnavailable | LocationUnfilled`, `merge_branch(row) -> Branch | BranchUnreadable`, `unit_state(row) -> ActiveUnit | RetiredUnit | LocationUnfilled` and `member_by_folder(machine, path) -> MemberFound | NoSuchMember` (selector `FolderMember`). No other module runs these git reads.
- `showrunners.timer_for(row, snapshot) -> Timer | NoTimer` joins a timer to a row by doc (`by_doc`), never by name or slug.
- The membership readers `unit_lookup`, `live_units`, `production_units`, `staff_sources.member` / `live()`, `dailies_input` and the `roster` SessionMarks read the unit's worktree, plan and port from the row and `.unit.toml`. The Units table keeps only how a unit name is shown (`unit_table.unit_spellings`). Plan paths are absolute, the board's contract.
- A removed worktree's branch and configuration come only from its parked `RetiredSession`. A row with an unfilled, unreadable or unavailable fact gives one problem line that names the unit and the cause, and the reader goes on; it never gives an empty list. Unreadable and remote-unavailable evidence stays distinct from confirmed absence.
- `unit_match` takes a reference selector (`StoredReference(id) | LiveSession(id) | ByName`) and a parent selector (`RequiredParent(row) | AnyParent`). `ObservedUnit.staff_id` is `LiveSession(id) | NoSession`. `unit_match` and `staff_check` take the owner by doc first (`by_doc`, then `by_member_reference`). `staff_check` gets typed problems from `unit_metadata`, not captured stderr.
- `accounts.py` calls `unit_match` with `RequiredParent`; its by-mark fallback stays.

**Files:**
- `scripts/production/session_staff.py` — the one membership join, `RowResolver` and the `unit_match` selectors.
- `scripts/production/staff.py` — the worktree and doc states (`PresentWorktree`, `RemovedWorktree`, `WorktreeUnfilled`, `DocUnfilled`).
- `scripts/production/session_labels.py` — display labels through the resolver.
- `scripts/production/showrunners.py` — `timer_for`.
- `scripts/production/unit_table.py` — `unit_spellings`, the shown spelling of each unit name.
- `scripts/production/unit_lookup.py`, `live_units.py`, `production_units.py`, `staff_sources.py`, `dailies_input.py`, `staff_check.py`, `unit_config.py`, `scripts/message/roster.py` — membership from rows.
- `scripts/production/staff_test_support.py` — fixture rows that record the worktree and doc.

**Binds later work:**
- Every reader takes one `RowResolver` per command run, never one per unit, so the remote branch read stays one ssh call per machine.
- The transitional outcomes (`WorktreeUnfilled`, `DocUnfilled`, `LocationUnfilled`, `RetiredFactsUnavailable`) stay until the completeness gate removes the unfilled variants. Runtime read failures (`BranchUnreadable`, `DocumentUnavailable`) stay after it.
- The phases that make production tools read the row and `.unit.toml` land one at a time.
- `staff.json` has an outside reader (cargo-liner). A change to its shape needs a notice to that reader.

**Gotchas:**
- A strict staff reader breaks staff-row fixtures in the whole repository, not only in `scripts/production`. Search every fixture that writes staff rows before dispatch, and run the merge gate early.
- After this phase, `grep -rnE 'unit_marks|unit marks|marked unit'` finds only `production_tree.py`, `test_production_tree.py` and the mark-reader names.

**Ruled out:** a problem line only where a reader lists units — the row is the authority, so each reader prints the line.

### Phase 3t — Production tools read the row and `.unit.toml`  · status: moved: devtools-unit (sdlc, 2026-11-05)

**Owner:** devtools-unit (sdlc split, 2026-11-02 08:23).

**Progress (sdlc, 2026-11-05):** the first part is cc5723bd5 in devtools' combined hash. On sdlc 4441fbcfe devtools finds only `add_unit.py:1155`, `owns_fill.py` and `resume_unit.RetiredUnit` left. The item 1 file list below is from ac680222 and is stale.

**Depends on:** 3s and Phase 4. Lands alone; before the record and deletion stages.

#### Work Order

**Goal:** the production tools in `scripts/production/` read the worktree, branch, plan, port, retired state, doc and merge branch through the Phase 3s resolver, not from a Units table cell, a retired note, a timer's doc or a doc field.

**Spec:**
1. **Readers.** Every non-test file in `scripts/production/` that this search finds, except the ones later steps rebuild or delete (`rename.py`, `rename_unit.py`, `rename_outside.py`, `rename_checkout.py`, `rename_state.py`, `unit_rename_sweep.py`, `transfer_unit.py`, `showrunner_parent.py`, `showrunner_tree.py`, `production_units.py`, `units_table.py`): `grep -rlE 'unit_table|units_table|## Units|production_units|unit_is_retired|Showrunner session|space_from_text|former_slugs|Formerly|Parent production' scripts/production`. A timer read for session, socket, zone or interval is allowed (decision 16), so the search does not forbid `registered_showrunners`; a timer's doc read is found by the gate test below. Phase 4r deleted `staff_check`. At ac680222 it lists `add_showrunner`, `add_unit`, `change_review`, `ci_points`, `codex_winddown`, `completion_receipt`, `footer_counts`, `log_backup`, `merge_checkpoint`, `merge_gate`, `next_item`, `production_lifecycle`, `promote_showrunner`, `proposals_folder`, `proposals_table`, `prune_phase_notes`, `resume_unit`, `retire_unit`, `roles_audit`, `stall_watch`, `style_units`, `unit_run`, `update_registration`, `waiting`.
2. Each reads who exists and who reports to whom from one snapshot, and each fact from the resolver. A timer still gives its session, socket, zone and interval through `showrunners.timer_for` (decision 16); its doc comes from the showrunner row. The excluded files keep their `production_units` import until Phase 4c moves it. `resume_unit.py` and `retire_unit.py` stop reading the retired note: the parked file holds it. `add_unit.py` finds an existing unit by its row, not by its Units row.
3. The Owns scope check of `merge_checkpoint.py` (:243-248, :583) is not changed here; Phase 4b deletes it.
4. History stays history: a reader that reports who owned a past commit or run keeps what the artifact recorded (old 3i rule).
5. **The resume recovery type states its guarantee (moved here from the Phase 3r closure review, 2026-11-01).** `resume_unit.py:89` holds `recovery: UnitConfig | NoRetiredSession`. A `UnitConfig` can lack the retirement facts, and the sentinel means only that no recovery file is outstanding, so the callers keep the guarantee by procedure. Replace it with one named recovery type whose retirement facts are mandatory, and a separate `NoRecoveryRequired` state. A test pins that a recovery value without the retirement facts cannot be made.

**Files:**
- `scripts/production/{add_showrunner,add_unit,change_review,ci_points,codex_winddown,completion_receipt,footer_counts,log_backup,merge_checkpoint,merge_gate,next_item,production_lifecycle,promote_showrunner,proposals_folder,proposals_table,prune_phase_notes,resume_unit,retire_unit,roles_audit,stall_watch,style_units,unit_run,update_registration,waiting}.py` — the readers item 1 lists (ac680222); a reader the search finds at dispatch joins them.
- `scripts/production/test_{add_showrunner,add_unit,change_review,ci_points,codex_winddown,completion_receipt,footer_counts,log_backup,merge_checkpoint,merge_gate,next_item,production_lifecycle,promote_showrunner,proposals_table,prune_phase_notes,resume_unit,retire_unit,roles_audit,stall_watch,style_units,unit_run,update_registration,waiting}.py` — their tests.
- `scripts/production/{add_unit,production_lifecycle,merge_checkpoint,dailies_input,update_registration,staff}_test_support.py` — fixtures.
- `scripts/production/session_staff.py`, `scripts/production/session_labels.py` — read only.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/production_lifecycle.py` (wrap, attach and the lifecycle reads that others call).
- `test` — the tests and fixtures.

**State:** none.

**Constraints from prior phases:** Phase 3s resolver; Phase 3r writers. Delegation Context → Staff design (2026-10-31), One identity join.
- Phase 3h review (2026-11-01): regression: `add_unit --resume --check` with a dead pending opening reports it and writes nothing; the test checks the pending file bytes unchanged. Keep the planned change of `recovery: UnitConfig | NoRetiredSession` to guaranteed retirement facts and `NoRecoveryRequired`.
- Phase 3h review (2026-11-01): `proposals_folder.py:32` returns `ProposalsFolder(path) | ProposalsUnconfigured`, not `Path | None`.
- Phase 3h review (2026-11-01): `add_unit` stops writing the `> **Production:**` header only after 3u's header readers switch; 4b deletes the headers.
- Phase 3s review (2026-11-02): a strict reader breaks fixtures in the whole repository, not only its own folder (3s: 11 modules in accounts, astra, buildlog, direct, shutdown and style). Before dispatch, grep every test fixture that writes staff rows (`staff_test_support` helpers and hand-built rows) and put the affected test files in **Files**. Run the merge gate early, before the final landing. `staff_test_support.py` has one writer at a time.
- Phase 3s review (2026-11-02): use `session_staff.RowResolver` (one `StaffSnapshot` and one fact resolver per command run) and its `member_by_folder` with the `FolderMember` selector. A resolver for each unit loses the one-ssh-call remote batch; test several remote units in one run.
- Phase 3s review (2026-11-02): the search gate has two temporary exceptions: `former_slugs` in `session_staff.py` and `space_from_text` in `session_labels.py`. Phase 4d deletes both.
- Phase 3s review (2026-11-02): `staff_sources.unit_parent` still selects a name lookup with `session_id=""`. Replace it with the existing selector types (`StoredReference(id) | LiveSession(id) | ByName`).
- Phase 3s review (2026-11-02): outcomes to keep visible, with tests: a failed git read gives `BranchUnreadable` (never retirement or an empty complete list); missing and malformed parked files give `RetiredFactsUnavailable`; missing, unreadable and remote-unavailable docs give `DocumentUnavailable` with an incomplete result; timerless membership, wrapped productions and remote evidence give `NoTimer` without the loss of the staff owner, and unavailable evidence stays distinct from confirmed absence.

**Acceptance gate:** the search in item 1, less the excluded files, finds nothing; a test whose timer doc differs from the row's doc shows that every changed reader takes the row's doc; basedpyright 0/0 on changed files and every caller module; each changed module's suite passes; a real `full_test.py` and a real `add_unit.py --check` on a real unit read the row's worktree and branch; a real `resume_unit.py sdlc guild-process-unit --dry-run` passes; full gate green. No rerun test: only reads change.

**Acceptance gate (checker):** the staff resolver and reader tests (`test_staff.py`, `test_staff_sources.py`; Phase 4r deleted `staff_check.py`) run with no Units table and no timer doc metadata; rows, git and `.unit.toml` supply current facts. A remote read failure stays uncertain; confirmed absence remains a different diagnostic. This test must pass before 4c removes tables or 4d removes their parser.

### Phase 3u — Other readers in this repository's shared trees read the row  · status: done

**Depends on:** 3s. Lands alone.

#### As-built

- The readers in `scripts/direct/`, `scripts/usage/`, `scripts/hierarchies/`, `scripts/message/` and `scripts/nightly_review/` take unit and showrunner ownership from the staff row: one `session_staff.RowResolver` per command run, `member_by_folder` for a caller that knows its folder, the native session or thread id for a pane. A run's recorded production and unit stay as history.
- `showrunners.read_sources` takes an `on_failure` callback. `showrunners.timer_for` skips one unreadable timer and raises only when no timer is readable, so a bad timer removes only its own row and gives one problem line.
- `nightly_review.py` selects its unit from config (`NightlyUnit`, `NightlyUnitNotSelected`) and launches `add_unit.py` with `--permanent`, `--standby` or `--plan`. Unknown ownership is an `OSError` refusal line.
- `workers._run_unit` uses only the run's own evidence. `phase_table.read_production` validates the Merge branch, Showrunner checkout and Log fields. A unit name converts to its id once.
- The status line is unchanged: it reads the unit's parent from the staff row in its `jq` join and matches a timer to its showrunner by the timer's native session target.
- The item 1 search (`unit_table|units_table|## Units|production_units|unit_is_retired|SHOWRUNNER_UNIT|PRODUCTION_MARK|UNIT_MARK\b|Showrunner session|former_slugs|Parent production` and the `Production:` header) finds only helpers that read no membership: `unit_table.Refusal`, `unit_table.cell_value`, `unit_table_cells` (`open_step`, `run_events`), and `phase_table`'s `former_slugs` use for retained note ownership.

**Files:**
- `scripts/message/{roster,unit_labels,escalate,rca}.py`, `scripts/usage/{outcomes,usage_report,waste_watch}.py`, `scripts/hierarchies/{measure,push_audit}.py`, `scripts/direct/{assertion_guard,board_json,phase_table,plan_history,progress_history,workers}.py`, `scripts/production/{showrunners,dailies_input}.py`, `scripts/nightly_review/nightly_review.py` — the row readers.
- Their tests, and the caller fixtures `scripts/direct/{assertion_guard_test_support,review_launcher_test_support}.py`, `scripts/direct/test_{workers,verify_lint_guard_key}.py`, `scripts/production/test_{dailies_input,showrunners,prune_phase_notes,rename_phase_notes}.py`, `scripts/hooks/test_direct_turn_wait_hooks.py`, `scripts/whoami/test_five_hour.py`.

**Gotchas:** a strict staff-row reader needs a private staff row in each fixture in every tree, so run the merge gate early. A candidate unit's own folder row is not evidence of a run's owner. Do not apply the unit id suffix conversion twice.

**Ruled out:** a `STAFF_READ_SECONDS` limit for the status line (the staff read is a local file; a `conf.py` read costs 0.058 s against a 0.0165 s line).

### Phase 3v — Readers in other units' files read the row  · status: done

**Owner:** each owner of the files it reads (sdlc split, 2026-11-02 08:23).

**Depends on:** 3s. Each owner lands its part alone; before 4e deletes marks. The bevy part lands before 3y.

#### Work Order

**Goal:** the readers in the shutdown, accounts, style, builds, Bevy upgrade, whoami and herdr files use the row through the Phase 3s resolver.

**Spec:**
1. **Readers** (non-test files of `grep -rlE '<the pattern of Phase 3u item 1>' scripts/shutdown scripts/accounts scripts/style scripts/bevy_upgrade scripts/whoami scripts/herdr scripts/build_targets`). At ac680222: shutdown `inventory.py`, `pane_record.py`, `remote_control.py`, `restart.py`, `stop_resume.py`; accounts `accounts.py`, `unit_watchdog.py`; style `add_style.py`, `coverage_report.py`, `style_plan.py`, `style_proposals.py`, `style_sync.py`, `unit_files.py`, `unit_plans.py`; `bevy_upgrade.py`, `upgrade_record.py`; whoami `agent_accounts.py`, `profile_notes_pass.py`, `quota_alert.py`; herdr `herdr.py`, `herdr_sessions.py`.
2. **Working set owner (builds unit).** `scripts/build_targets/working_set.py` names a process's owner from `working_set_owners.json`, a snapshot of the Owns cells. It now names the unit whose present worktree holds the process's working folder (`member_by_folder`), else `unassigned`. The snapshot file goes in Phase 4b.
3. Pane marks still exist; this step only stops reading them where the row answers. Phase 4e stops setting them.
4. Text in the files this phase changes says "session labels", not "unit marks" (`inventory.py:1700`, `test_inventory.py:1956`, `herdr.py:3361`).

**Files:**
- `scripts/shutdown/{inventory,pane_record,remote_control,restart,stop_resume}.py`, `scripts/accounts/{accounts,unit_watchdog}.py`, `scripts/style/{add_style,coverage_report,style_plan,style_proposals,style_sync,unit_files,unit_plans}.py`, `scripts/bevy_upgrade/{bevy_upgrade,upgrade_record}.py`, `scripts/whoami/{agent_accounts,profile_notes_pass,quota_alert}.py`, `scripts/herdr/{herdr,herdr_sessions}.py`, `scripts/build_targets/working_set.py` — the readers item 1 lists (ac680222); a reader the search finds at dispatch joins them.
- `scripts/shutdown/test_{inventory,pane_record,remote_control,restart,stop_resume}.py`, `scripts/accounts/test_{accounts,unit_watchdog}.py`, `scripts/style/test_{add_style,coverage_report,style_plan,style_proposals,style_sync,unit_files}.py`, `scripts/bevy_upgrade/test_upgrade_record.py`, `scripts/whoami/test_{agent_accounts,profile_notes_pass,quota_alert}.py`, `scripts/herdr/test_{herdr,herdr_sessions}.py`, `scripts/build_targets/test_working_set.py` — their tests.
- `scripts/style/style_plan_test_support.py`, `scripts/whoami/quota_alert_test_support.py`, `scripts/bevy_upgrade/cli_test_support.py` — fixtures.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: none.
- `test` — the tests and fixtures.

**State:** none.

**Constraints from prior phases:** Phase 3s resolver. Owners: shutdown (`scripts/shutdown/`), accounts (`scripts/accounts/`, `scripts/whoami/`), style (`scripts/style/`), builds (`scripts/build_targets/`); each agrees its part before dispatch and names it in the checkpoint notice. Delegation Context → Staff design (2026-10-31).
- Phase 3h review (2026-11-01): accounts-status regression: a launch record with a fixed id, a different current session and no usable unit mark still counts the unit. Keep the shutdown, style and builds owner agreements.
- Phase 3s review (2026-11-02): a strict reader breaks fixtures in the whole repository. Grep every staff-row fixture before dispatch, list the affected test files in **Files**, and run the merge gate early. `staff_test_support.py` has one writer at a time.
- Phase 3s review (2026-11-02): use `session_staff.RowResolver` and its `member_by_folder` with the `FolderMember` selector; one snapshot and one resolver per command run.

**Acceptance gate:** the search in item 1 finds nothing outside `herdr.py`'s mark setter (Phase 4e); basedpyright 0/0 on changed files and every caller module; each tree's suite passes; a real `working_set.py` run names the owning unit of a running gate by its worktree; full gate green. No rerun test: only reads change.

### Phase 3w — Skills and commands describe the row and `.unit.toml`  · status: done

**Depends on:** 3s for the interface. Text lands with the command it describes, including the 4b header, Hub files and Gates deletions; before 4g.

#### Work Order

**Goal:** no command, skill or format doc tells an agent to read or write a Units table, Owns, a retired note, a parent file, `Showrunner session`, `Formerly`, `Parent production`, the showrunner list of `showrunners.json` or a pane mark; each says where the fact is now.

**Spec:** Search `grep -rnE 'Units table|Units row|## Units|\bOwns\b|Showrunner session|Formerly|Parent production|showrunners\.json|SHOWRUNNER_UNIT|retired note|/parent\b' commands docs/direct docs/production_format.md docs/style_production.md docs/accounts.md`. At ac680222 it lists `commands/showrunner/` `attach`, `add_unit`, `dailies`, `produce` (:118 Owns), `promote_showrunner`, `promote_unit`, `proposal`, `question`, `rename`, `rename_unit`, `resume_unit`, `retire_unit`, `transfer_unit`; `commands/unit/direct.md`, `commands/unit/question.md`; `commands/producer/to_production.md:112`; `docs/production_format.md` (:143 the Units table, :234 Owns); `docs/style_production.md`; `docs/accounts.md`. Each line says: a unit is its staff row; its worktree is the row's field; repository and branch come from git there; plan, port and responsibilities are in `.unit.toml` at the worktree root, never tracked; a retired unit's file is in `~/.local/state/staff/units/<staff id>.toml`. `/producer:to_production` writes `.unit.toml` content in place of Units rows. "Unit marks" becomes "session labels" in every file this phase changes (`commands/showrunner/dailies.md:215`). The store paths and keys in command text (for example `STATE_DIR`, `UPDATES`, `PROMPT_FILE` in `produce.md:37-49`) are not this phase's: each changes with its store in Phases 3x-4a. Text in Simplified Technical English; `/succinct_style` applies.

**Files:**
- `commands/showrunner/{attach,add_unit,dailies,produce,promote_showrunner,promote_unit,proposal,question,rename,rename_unit,resume_unit,retire_unit,transfer_unit}.md`, `commands/unit/direct.md`, `commands/unit/question.md`, `commands/producer/to_production.md` — the command files the search lists (ac680222); a file the search finds at dispatch joins them.
- `docs/production_format.md`, `docs/style_production.md`, `docs/accounts.md` — the doc files the search lists.

**Workers:** `2 writers` — split by tree.
- `impl` — `commands/showrunner/`.
- `test` (opens as impl) — `commands/producer/`, `commands/unit/`, `docs/direct/`, `docs/production_format.md`, `docs/style_production.md`, `docs/accounts.md`.

**State:** none.

**Constraints from prior phases:** Phases 3q-3v. Codex mirrors regenerate through `claude_to_codex/sync.py`.
- Phase 3h review (2026-11-01): the search and the acceptance gate also cover `Production:` headers, Hub files and Gates, not only Units/Owns.

**Acceptance gate:** the search finds nothing that tells an agent to use the old source; `grep -rn 'unit marks' commands docs/direct` finds nothing; the Codex mirrors regenerate clean; the command-file tests (`scripts/claude_to_codex` suite) pass; full gate green.

### Phase 3x — Small stores keyed by staff id  · status: moved: devtools-unit (sdlc, 2026-11-04)

**Owner:** devtools-unit; its stall part goes to messaging-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3t, 3u, 3v (records stage of the rename track). One writer owns `change_review.py`, `stall_watch.py`, `board_json.py`, `phase_table.py` and `herdr.py` at a time.

#### Work Order

**Goal:** six small stores that hold a unit or production name hold the staff id; no rename or transfer rewrites them, and the rewriter of each goes.

**Spec:** For each store: readers take the id from the row; a rerun-safe migration renames each old file or rewrites each old key to the id once (a key whose name has no row is listed, never filled from a likely row); then the rewriter goes. **One store, one landing (2026-10-31, packaging).** Each numbered store is its own landing step, in the order listed: one commit with its readers, its writers, its command text and its Codex mirror (`claude_to_codex/sync.py`), its migration and its rewriter's removal, then its own real proof before the next live store migration starts (other stores can be prepared in parallel): the real migration on natedev (and the Mac where the store exists) stopped after its first file and run again ends equal to one clean run, and a rerun of the finished migration changes nothing and exits 0. The inventory of each store, done before its edit and given in the checkpoint notice, lists its path, its internal keys, every writer, every reader, every command and skill line (`grep -rn '<store path or key>' scripts commands ~/.codex/skills/generated-from-claude`) and its rewriter. Smallest first:
1. **Change review cell** (`change_review.py:932` `rename_production`; 2 readers): an open row that a unit owns holds the unit's staff id, and its production is the unit row's parent at read time, so it follows a transfer with no rewrite (this replaces old Phase 3m); an open row that the production owns holds the showrunner's staff id; a reviewed row keeps what it recorded (history). `/change_review` shows the row's names.
2. **Phase note `unit:`** (`phase_table.py:1811`, `rename_unit` :2070): the note holds the unit's staff id.
3. **Saved tab titles — dropped (user, 2026-11-01).** No title rewrite: `rename_saved_tab` is gone. `remember_row_tab` and `saved_row_tab` bind a saved tab by its fixed row id, so a rename changes nothing in the saved layout. `SavedLayoutTests` keeps the test that restart uses the current row name with no change to the saved id or position (review, 2026-11-04).
4. **Board cache** (`~/.local/state/unit-board/<slug>.json`, `board_json.py:424`; moved by `rename_outside.py:64`): `<showrunner staff id>.json`. The unit-board plugin's reader switches in the same step (the unit-board unit's file).
5. **Proposal `unit:`** (`proposal_issue.py:261`; 4 readers): the issue field holds the staff id, and readers show the row's name.
6. **Stall stretch** (`stall_watch.py:412` `stretch_path`; 4 readers): stretch files, prompt files and notification keys by staff id. The migration cancels and re-sends a live alert under the new key, so elapsed time and cancellation survive (old Phase 3h). The moves in `rename_unit.py`, `rename.py` and `transfer_unit.py` go. `PROMPT_ALERT_SECONDS = 60` (`stall_watch.py:66`) moves to `config/production.conf`, read at use through `tunables`, with a changed-config test.

**Files:**
- `config/production.conf` — `PROMPT_ALERT_SECONDS`.
- `scripts/production/change_review.py`, `scripts/direct/phase_table.py`, `scripts/herdr/herdr.py`, `scripts/direct/board_json.py`, `scripts/production/proposal_issue.py`, `scripts/production/stall_watch.py`, `scripts/production/transfer_unit.py`, `commands/change_review.md` — the stores' writers and readers.
- `scripts/production/test_change_review.py`, `scripts/direct/test_phase_table.py`, `scripts/herdr/test_herdr.py`, `scripts/direct/test_board_json.py`, `scripts/production/test_proposal_issue.py`, `scripts/production/test_stall_watch.py`, `scripts/production/test_transfer_unit.py` — their tests.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/change_review.py`.
- `test` — the tests.

**Rename-file owner:** Phase 3y owns `rename_outside.py`, `rename_unit.py`, `rename.py`, and their tests and fixtures.
Its changes land with each store's readers, writers, migration, and rewriter removal.
Other workers do not edit these shared files.

**State:** each store moves to staff-id keys, once.

**Constraints from prior phases:** Phase 3s resolver. Delegation Context → Staff design (2026-10-31), Rerun-safe tools.
- Phase 3h review (2026-11-01): item 2 also removes `showrunner:` and `production_file:` from the phase note (read from staff). Item 5: `unit:`, `proposed_by:` and `approved_in:` hold fixed ids; `showrunner:` and `plan:` go. Each with all its readers and its own store proof.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; each store's suite passes; a search finds no rename or transfer code that rewrites these six stores; each store's own real proof (the rule above), one landing per store; a changed `PROMPT_ALERT_SECONDS` takes effect with no code change; a transferred unit's open change row shows under its new production, a production-owned row stays, and a reviewed row keeps its old production; an active stall alert keeps its elapsed time and still cancels after a real rename and a real transfer; a real `/change_review` shows a moved unit's open row under the new production with no rewrite; full gate green.

**Constraints from prior phases (native ids):** `staff.update_current_session` changes only `current_session` in one locked atomic write.
The fixed row id, child links, and store keys stay unchanged.
Add no store registry, identity alias, or live-rekey journal.
Parked files and retired references belong to stopped units.
The one-time identity migration writes reference stores first and staff last.
It supports reruns and is deleted in Phase 19.
Test a `/clear` during migration: resolve current ownership again, report unavailable evidence, and preserve fixed store keys.
Make no unproved write and create no second row.

### Phase 3y — Middle stores keyed by staff id  · status: done

**Store landing (devtools-unit 4i, via sdlc, 2026-11-02):** devtools 3x lands stores 1, 2, 4 and 5 only. A store lands after 3y removes its rewriter, or after a test shows the old rewriter leaves the staff-id key alone and a real rename exits 0. Each store notice names its rewriter.

**Depends on:** 3s for data, 3t, and the bevy part of 3v; records stage of the rename track. One owner handles `rename_outside.py`, `rename_unit.py` and `rename.py`. 3y does the rename-file part of 3x, 3z and 4a.

#### Work Order

**Goal:** seven more stores hold the staff id, and their rewriters go.

**Spec:** The same rule as Phase 3x, one store per landing with its own real proof, for:
1. **`/next` pins** (`~/.local/state/next/*-<slug>.json`, `rename_outside.py:45` `pin_moves`; 5 readers).
2. **Dailies counts** (`config/dailies.conf`, keys `<production slug>.<label>`; read by `dailies_counts.py`, `dailies_input.py`, `dailies_render.py`, `commands/showrunner/dailies.md`): keys `<showrunner staff id>.<label>`. The counts stay (user, 2026-10-10).
3. **Design holder — deleted, not migrated (user, 2026-11-01).** `HeldDesign.holder` (`design_roles.py:180`), `resolve_holder` and holder authorization go; the helpers are worker rows and their staff parent says who they work for. Keep design fences and document context.
4. **The showrunner state folder** (`~/.local/state/showrunner/<slug>/`: `session/*`, `prompt.txt` and every other file in it; `rename_state.py:302` `rename_scratch`; 8 readers) and **the reports** (`~/.local/state/showrunner/reports/<slug>.json`): the folder is `showrunner/<staff id>/` and the report `reports/<staff id>.json`. `/showrunner:produce` names them in text (`produce.md:37-49`: `STATE_DIR`, `UPDATES`, `PROMPT_FILE`, outstanding items, reports and the unit-name JSON fields), and that text and its Codex mirror change in this landing.
5. **Outstanding items** (`~/.local/state/showrunner/outstanding/<slug>*`, `tidy_wrapped.py:31`; 8 readers).
6. **Production switches** (`showrunner/footers-off`, `counts`, `review-paused`; `tidy_wrapped.py`, `review_pause.py`, `hooks/showrunner_footer.py`).
7. **Bevy watch** (`~/.local/state/bevy-upgrade/<slug>.json`, `upgrade_record.py:113`; about 10 readers) **and its notifier job** (the `upgrade_schedule.instance(<slug>)` folder under the notifier state, which `rename_outside.py:53` `bevy_job` moves today): both by staff id, in one landing.
8. Text in the files this phase changes says "session labels", not "unit marks" (`design_roles.py:508`).

**Files:**
- `scripts/production/rename_outside.py`, `scripts/production/next_item.py`, `scripts/production/dailies_counts.py`, `scripts/production/dailies_input.py`, `scripts/production/dailies_render.py`, `config/dailies.conf`, `commands/showrunner/dailies.md`, `scripts/astra/design_roles.py`, `scripts/production/rename_state.py`, `scripts/production/tidy_wrapped.py`, `scripts/production/review_pause.py`, `scripts/hooks/showrunner_footer.py`, `scripts/bevy_upgrade/upgrade_record.py`, `scripts/bevy_upgrade/bevy_upgrade.py`, `scripts/bevy_upgrade/upgrade_schedule.py`, `scripts/production/rename.py`, `scripts/production/rename_unit.py`, `commands/showrunner/produce.md` — the stores' writers and readers; every other reader that `grep -rn '<store path>' scripts` finds joins them.
- `scripts/production/test_{next_item,dailies_counts,dailies_input,dailies_render,rename_state,tidy_wrapped,review_pause,rename,rename_unit}.py`, `scripts/astra/test_design_roles.py`, `scripts/hooks/test_showrunner_footer.py`, `scripts/bevy_upgrade/test_upgrade_record.py` — their tests.
- `scripts/production/dailies_input_test_support.py`, `scripts/production/rename_test_support.py`, `scripts/production/rename_unit_test_support.py` — fixtures.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/rename_outside.py` (four stores lose their move there).
- `test` — the tests and fixtures.

**State:** each store moves to staff-id keys, once.

**Constraints from prior phases:** Phase 3x migration pattern. The Astra design files are the guild unit's; agree that part first.
- Phase 3h review (2026-11-01): dailies memory is keyed by fixed id; the showrunner's unit table holds the fixed id with the name from staff; free-text notes are left alone. Test a design whose helpers never opened.

**Acceptance gate:** for each of the seven stores, its own landing and real proof (Phase 3x rule): its suite passes, its rewriter is gone, its real migration stopped after the first file finishes on rerun and a rerun of the finished one changes nothing and exits 0; a real dailies run shows the same counts as before; a real `/showrunner:produce` session finds its state, prompt and report under the staff id; the Bevy notifier job fires once under the staff id; the Codex mirrors regenerate clean; full gate green.

**Constraints from prior phases (native ids):** `staff.update_current_session` changes only `current_session` in one locked atomic write.
The fixed row id, child links, and store keys stay unchanged.
Add no store registry, identity alias, or live-rekey journal.
Parked files and retired references belong to stopped units.
The one-time identity migration writes reference stores first and staff last.
It supports reruns and is deleted in Phase 19.
Test a `/clear` during migration: resolve current ownership again, report unavailable evidence, and preserve fixed store keys.
Make no unproved write and create no second row.

**Shared rename files:** this writer owns the rename-file changes for 3x, 3z, and 4a, including their tests and fixtures.
Each change lands inside its store's landing, with the migration and rewriter removal.

### Phase 3d — Store migrations continue past anomalies  · status: done

**As built:** `migrate_showrunner_state.py` and `migrate_middle_stores.py` follow staff-lifecycle.md section 4. A unit reference with no staff row prints one `anomaly:` line and the file goes on; `dailies_judgment.json` drops the entry (its reader refuses unknown units), every other file keeps the old text. One file never stops its folder. A folder, report or outstanding file with no showrunner row stays in place. Two old names with one staff id merge `*.history.jsonl` and `*.answered.jsonl` by their `time` field; an empty `.lock` goes. Both end with `moved N; left in place M; unavailable K` and exit 1 only for real read or write errors of an owned item. Rehearsal on copies: natedev showrunner_state moved 3454 (cargo-tile, demo left in place), outstanding moved 30 (nested-g1, nested-g2, hanadocs-info left in place), unavailable 0; the Mac has no showrunner state.

### Phase 3z — Shutdown and style stores keyed by staff id  · status: done

**Owner:** shutdown-unit and style-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3s; records stage of the rename track. Shared rename files have one owner. `restart.py` has one writer at a time: 4n and Phase 4 are done, so 3z writes it.

#### Work Order

**Goal:** shutdown records and style tickets hold the unit's staff id, and their rewriters go. The drained worker ledger retires in Phase 19 without a path migration.

**Spec:** The two store moves keep the Phase 3x rule, one store per landing with its own real proof. The ledger is retired separately:
1. **Worker ledger:** no key or path migration. Phase 19 owns direct retirement after drain. Keep its consumer and history proofs there. Current ledger paths and their rename rewriters stay until that audited retirement. The models unit reviews its owned files there.
2. **Shutdown records' unit name** (rewritten by `rename_unit.py:952` `_restart_record`; about 10 readers in `scripts/shutdown/`): the record uses the backend's native resume id, with no extra identity field and readers take the name from the row. The shutdown unit writes its side.
3. **Style tickets** (`style_ledger.py:3530` `rename_unit`; 9 readers): the ticket holds the unit's staff id. The style unit writes its side.

**Files:**
- `scripts/shutdown/shutdown_record.py`, `scripts/shutdown/inventory.py`, `scripts/shutdown/restart.py`, `scripts/style/style_ledger.py`, `scripts/production/transfer_unit.py` — the stores' writers and readers; every other reader that `grep -rn '<store path>' scripts` finds joins them.
- `scripts/shutdown/test_shutdown_record.py`, `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_restart.py`, `scripts/style/test_style_ledger.py`, `scripts/style/test_style_ledger_tickets.py`, `scripts/production/test_transfer_unit.py` — their tests.
- `scripts/production/staff_test_support.py`, `scripts/production/transfer_unit_test_support.py` — fixtures.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files of this repository's own trees; hub: `scripts/shutdown/shutdown_record.py`.
- `test` — the tests and fixtures of the files `impl` owns.
- The shutdown unit and the style unit each land their own files as a separate owner landing (stage two), after this phase lands (review, 2026-11-04).

**Rename-file owner:** Phase 3y owns `rename_outside.py`, `rename_unit.py`, `rename.py`, and their tests and fixtures.
Its changes land with each store's readers, writers, migration, and rewriter removal.
Other workers do not edit these shared files.

**State:** shutdown and style stores move to staff-id keys, once. Ledger state is unchanged here.

**Constraints from prior phases:** Phase 3n (backend-identity record; the ledger keeps history events only); Phase 3x migration pattern. Changing `fake_unit_host.py`, `transfer_unit_test_support.py` or `LayoutHerdr` runs the whole `scripts/production` suite in the gate; a live-to-stopped fixture step updates the foreground evidence and keeps every other reply, such as an installed close failure.
- Phase 3h review (2026-11-01): `style_ledger.next_ticket_id(unit)` (`style_ledger.py:2728`, `<unit>-N`) becomes one global counter (`14`, not `herdr-unit-14`; user: "this is basic database design"), with a migration of ticket references; the holder is the unit's fixed id.
- Phase 3h review (2026-11-01; sdlc ruling): the worker ledger gets no key or path migration. Rename keeps its existing small file move for the ledger until Phase 19 deletes the ledger after drain. No new code for the ledger.
- Phase 3h review (2026-11-01): the shutdown record holds the resume id with the name from staff; resume prompt text reads the folder from staff when sent.
- Phase 3h review (2026-11-01): workers: `impl` — the shutdown and style source; `test` — their tests.

**Acceptance gate:** for each of the two moved stores, its own landing and real proof (Phase 3x rule): a real migration stopped after the first file finishes on rerun, and a rerun of the finished one changes nothing and exits 0; a real shutdown and restart of a probe tab of `agents` reads its record by staff id; full gate green.

**Constraints from prior phases (native ids):** `staff.update_current_session` changes only `current_session` in one locked atomic write.
The fixed row id, child links, and store keys stay unchanged.
Add no store registry, identity alias, or live-rekey journal.
Parked files and retired references belong to stopped units.
The one-time identity migration writes reference stores first and staff last.
It supports reruns and is deleted in Phase 19.
Test a `/clear` during migration: resolve current ownership again, report unavailable evidence, and preserve fixed store keys.
Make no unproved write and create no second row.

### Phase 4p — A zombie app-server is not live  · status: done

**Owner:** herdr-unit (sdlc, 2026-11-02; the user asked to resume two items this defect blocks).

#### Work Order

**Goal:** `codex_mesh.py` counts a zombie app-server as dead, and the launcher reaps each app-server it retires.

**Spec:**
1. `_pid_alive` returns False when `/proc/<pid>/stat` shows state `Z`. The EPERM rule stays.
2. The launcher reaps each child app-server it retires; a pid that is not its child is ignored.

**Files:** `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py`.

**Workers:** `1 writer`. Code only: Phase 45 and every worker thread keep running.

**Acceptance gate:** one test for each item; `test_codex_mesh.py` passes; basedpyright 0/0 on both files. natedev merges the hash with `gm.sh`.

### Phase 4q — A relaunch refuses only a provably live turn  · status: done

#### As-built

- `codex_mesh._attach_and_run` refuses a worker relaunch only when the old turn's recorded launcher process is alive (`_pid_alive`). The refusal says "has a live turn", or "thread state unknown (<reason>); recorded launcher is alive; use codex_mesh.py end --to <name> before relaunching" when the thread state cannot be read.
- With a dead or missing launcher, a turn in progress or an unknown thread state gets one cleanup attempt (`_end_unwatched_turn`) and one stderr line that names the stale turn. The line says "interrupt requested" only when an interrupt was sent, else "cleanup attempted; interrupt not confirmed". The relaunch then continues with a fresh `thread/start`.
- `RelaunchAllowed` and `RelaunchBlockedByLiveTurn` carry `interrupt_requested: bool`. A failed cleanup log write prints one stderr line and the relaunch continues.
- `_pid_alive` returns False for a pid of 0 or less, so a missing `launcher_pid` is not live.

**Files:**
- `scripts/agents/codex_mesh.py` — the relaunch refusal and the stale-turn cleanup.
- `scripts/agents/test_codex_mesh.py` — live, dead and missing launcher cases, for an in-progress turn and an unknown thread state.

**Gotchas:**
- `os.kill(0, 0)` signals the process group and succeeds, so a pid check without the `pid <= 0` guard counts a missing launcher as live.
- `thread/resume` hangs every fake-server fixture in `test_codex_mesh.py`; the relaunch starts a fresh thread.

**Ruled out:** continuing on the same thread after a stale turn — `thread/resume` hangs the fixtures, and the old relaunch flows already start a fresh thread.

### Phase 4 — Member names come from the staff row  · status: done (models-unit, main 530258351)

**Owner:** models-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** Phase 1, Phase 3, Phase 3g, Phase 3r, Phase 3h, and 4h on main (it lands before 3t and 4e). Readers stage of the rename track and input to Phase 7. One owner handles `add_unit.py`, `resume_unit.py`, `add_showrunner.py`, `herdr.py`, `pane_launch.py`, and `pane_record.py`.

#### Work Order

**Goal:** every launch and every restore of a member takes its name from its row, and a hand `/rename` in a unit director's pane leaves the row and the tab as they are and prints the rename_unit command.

**Spec:**

1. **Launch and restore names.** `add_unit.py`, `add_showrunner.py` and `resume_unit.py` use the row's name for `-n` and tab titles. A restore takes one snapshot, selects each row by its native resume id, and reads the current name. No launch or restart record sets or preserves `STAFF_ID`. On an unreadable snapshot, reuse each saved display name and print one problem line; on a missing row, name that entry and use its saved name. Never select a row by a saved name.
2. **A hand `/rename`.** `herdr_tab_title.py` resolves the session by its native runtime id. For a unit, keep its tab and pane on the row name and print `/showrunner:rename_unit` on a mismatch; write no staff row. The session record may keep the typed name until its next launch. Showrunner titles change only through the production rename command. Worker labels stay their short names. A session with no row keeps the existing ad hoc title behavior. Row kind, not inherited marks, separates these cases.
3. **One runtime join.** Read Claude identity from the exact process registry or hook input and Codex identity from the thread runtime. Use the Phase 3h resolver once per snapshot. A hook that runs while a clear handoff is incomplete lists that missing-row state and changes no staff record. Keep the shutdown unit's resume identity and account evidence; do not add an identity environment variable.

**Files:**
- `scripts/production/add_showrunner.py`, `scripts/production/add_unit.py`, `scripts/production/resume_unit.py` — the row's name for `-n` and the tab title; native identity lookup.
- `scripts/hooks/herdr_tab_title.py`, `scripts/hooks/test_herdr_tab_title.py` — a hand `/rename` of a unit keeps the row's name and prints the command.
- `scripts/herdr/herdr.py`, `scripts/herdr/pane_launch.py`, `scripts/herdr/test_herdr.py` — environment isolation prevents inherited identities.
- `scripts/shutdown/pane_record.py`, `scripts/shutdown/pane_restore.py`, `scripts/shutdown/test_pane_record.py`, `scripts/shutdown/test_pane_restore.py` — native resume id and restore name; the shutdown unit's files.
- `scripts/production/test_add_showrunner.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_resume_unit.py` — launch-name and native-id tests.
- `scripts/production/staff.py`, `scripts/production/staff_sources.py`, `scripts/production/staff_test_support.py`, `scripts/lib/atomic_file.py` — read only.
- `scripts/shutdown/restart.py`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_stop_resume.py` — the launch-name audit (Phase 3h review).

**Workers:** `2 writers` — split by launch path and hook.
- `impl` — launch names and runtime joins in `add_unit.py`, `add_showrunner.py`, `resume_unit.py`, `herdr.py`, and `pane_launch.py`.
  It also owns `pane_record.py`, `pane_restore.py`, `restart.py`, `stop_resume.py`, and the tests of every file it owns, the launch tests included (review, 2026-11-04).
- `test` (opens as impl) — the hook (`herdr_tab_title.py`, `test_herdr_tab_title.py`).

**State:** names shown by launches, hooks and restores. No new identity state.

**Constraints from prior phases:** Phase 3h owns native identity and the shared runtime join; Phase 3r owns current rows and configuration. Shutdown owns restart and pane records. Use one snapshot, including one owner ssh read on the Mac. Use `staff_test_support.py` and the shared atomic writer. This phase does not repeat identity backfill. Phase 4f: `herdr_tab_title.sync` already titles tabs; keep its tests and limit work to launch and restore. Own a director transfer-to-restart test: the old shell stays in the source group and restart opens the row in the new group from its saved binding. Workers: drop the title work 4f finished from the partition.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 3h review (2026-11-01): native admission and most restore identity shipped in 3h; do not repeat them. Keep `pane_record.resume_launch`'s fixed-reference to current-session conversion. Test a renamed row whose current session differs from its id.

**Acceptance gate:** basedpyright 0/0 on changed files and callers. Launch tests prove row names and no `STAFF_ID`. Hook tests cover unit mismatch, showrunner, worker, missing row and delayed clear; no hook writes staff. Restore tests use a renamed native-id row, several Mac entries with one ssh read, remote failure and a missing row. The pane writer uses `atomic_file`; no own `os.replace` stays. A real throwaway unit rename and restart keeps its tab, pane and terminal and takes its new row name; a finished rerun changes nothing. A real hand `/rename` prints the unit rename command and keeps the row name. Full gate green.

**Retained launch contract:** preserve `pane_launch`'s validated kind, strict retirement, and restore completion checks.

**Claude live proof safety:** use an owned throwaway probe and a private `STAFF_FILE` copy.
Answer trust only for the exact probe source or destination under `/home/natepiano/scratch/herdrcheck/`; refuse every other path.
Confirm the selected Yes option before Enter; verify prompt disappearance and record any repeated prompt.
Before the rerun snapshot, require the resumed pid, its process that runs `current_session` in the destination, and a confirmed terminal; a Claude launch has no launch record (review, 2026-11-04).
Require two equal reads one second apart; save both snapshots and named byte/time differences with text diffs.
Compare operation-owned staff fields and check for probe ids or scratch paths in live staff.
Record live hashes; an unrelated hash change alone does not prove a probe write.

### Phase 4a — Run owner and showrunner timers keyed by staff id  · status: moved: devtools-unit (sdlc, 2026-11-04)

**Owner:** devtools-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3t (the doc from the row), 3u, 3z. Records stage of the rename track.

#### Work Order

**Goal:** the two largest name-keyed stores hold the staff id: the run owner in progress history and the update timer folders.

**Spec:** The same rule as Phase 3x, one store per landing with its own real proof, for:
1. **Run owner** (`progress_history_state.json`, `progress_history.py:420` writes `production` and `unit`; about 15 readers, found by `grep -rln 'progress_history_state' scripts`): new runs record the unit's staff id; old runs keep their names as history.
2. **Showrunner timers** (`~/.local/state/notifier/showrunner-<slug>/`, written by `update_registration.py`, read by `notifier.sh`, `statusline.sh`, `showrunners.registered_showrunners` and about 37 files): the folder is `showrunner-<staff id>`, and its `CHECK` line passes the staff id, so `production_check.sh` reads the doc from the row. The timer keeps its session, socket, zone and interval (decision 16), and `showrunners.timer_for` reads the folder by staff id, so its doc join (Phase 3s) goes. The search for readers covers `commands` and the Codex mirror too (`produce.md`'s `UPDATES`). The messaging unit owns `notifier.sh`; agree it first.

**Files:**
- `scripts/direct/progress_history.py`, `scripts/production/update_registration.py`, `scripts/message/notifier.sh`, `scripts/production/production_check.sh`, `scripts/production/showrunners.py`, `scripts/statusline/statusline.sh`, — the two stores' writers and readers; every other reader that a search for the two paths finds joins them.
- `scripts/direct/test_progress_history.py`, `scripts/production/test_update_registration.py`, `scripts/message/test_notifier.py`, `scripts/production/test_showrunners.py`, `scripts/statusline/test_statusline.py`, `scripts/production/test_rename.py` — their tests.
- `scripts/production/update_registration_test_support.py` — fixtures.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — the active run record (Phase 3h review).

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/showrunners.py` (`registered_showrunners`, the timer reader every caller uses).
- `test` — the tests and fixtures.

**Rename-file owner:** Phase 3y owns `rename_outside.py`, `rename_unit.py`, `rename.py`, and their tests and fixtures.
Its changes land with each store's readers, writers, migration, and rewriter removal.
Other workers do not edit these shared files.

**State:** timer folders and the progress state move to staff-id keys, once.

**Constraints from prior phases:** Phase 3x migration pattern; Phase 3t reads the doc from the showrunner row.
- Phase 3h review (2026-11-01): item 1 also covers the active run record and its worktree and branch consumers: the run points at the unit's fixed id, the worktree comes from the row and the branch from git; finished runs keep their paths as history. Run-folder paths stay the same across `/clear`. Keep the native-session and socket resolution that shipped.
- Phase 3s review (2026-11-02): test timerless membership, wrapped productions and remote evidence: `NoTimer` never removes the staff owner, and unavailable evidence stays distinct from confirmed absence.

**Acceptance gate:** for each of the two stores, its own landing and real proof (Phase 3x rule); every running showrunner's timer fires once after the migration and the dailies and status line show it; a migration stopped after the first timer folder finishes on rerun; full gate green.

**Constraints from prior phases (native ids):** `staff.update_current_session` changes only `current_session` in one locked atomic write.
The fixed row id, child links, and store keys stay unchanged.
Add no store registry, identity alias, or live-rekey journal.
Parked files and retired references belong to stopped units.
The one-time identity migration writes reference stores first and staff last.
It supports reruns and is deleted in Phase 19.
Test a `/clear` during migration: resolve current ownership again, report unavailable evidence, and preserve fixed store keys.
Make no unproved write and create no second row.

### Phase 4r — Stop, resume, restart and move refuse only a catastrophe  · status: done

#### As-built

- herdr-unit's stop, resume, restart and move code refuses only for the 5 catastrophes in `staff-lifecycle.md` section 4: the target folder exists; a same-row concurrent operation; a second copy of a running session; uncommitted work would be deleted; a signal, keystroke or pane close would reach a process that is not the target. Each kept refusal names its catastrophe. Unknown or stale evidence prints one named anomaly line and the action continues.
- `herdr.check_target(target, *, action: Literal["signal", "keys", "close"], place, shell, terminal, text)` is the one target check: the target process must run the row's `current_session`. `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)` and `Herdr.close_pane(pane, *, target) -> bool` take the target and call it.
- A Claude launch writes no pane launch record; a pane is found as the herdr pane whose process runs `current_session`. `Herdr.start_agent` still writes the Codex compatibility record.
- `herdr.native_process` reads a Codex thread from the open rollout, then the environment, then the resume argument. The resume argument identifies the launch thread only.
- The saved tab layout (`layout-agents.json`) binds each tab by staff row id; restart reads the title from the row `name`. `remember_row_tab` keeps a row's saved index in the destination workspace when the row is already bound there; new and moved rows take the live index.
- A Codex worker comes back after its app-server dies (D3).

**Files:**
- `scripts/herdr/herdr.py`, `scripts/herdr/pane_launch.py`, `scripts/herdr/herdr_sessions.py`, `scripts/herdr/place.py` — the kept refusals and the target check.
- `scripts/production/worker_admission.py` — no dead reader or role allowlist.
- `scripts/herdr/test_herdr.py`, `scripts/herdr/test_herdr_sessions.py`, `scripts/herdr/fake_herdr.py`, `scripts/shutdown/test_pane_restore.py` — tests.
- Deleted: `scripts/herdr/trust_survey.py`, `scripts/herdr/test_one_session.py`, `scripts/production/staff_check.py`, `scripts/production/test_staff_check.py`.

**Binds later work:** structural faults (a missing terminal, an unreadable pane list, a duplicate pane) are anomalies, not refusals; each destructive action goes through `check_target`. Folder moves and resumes with no launch record restart through `restart.start_row`.

**Ruled out:** a pane found by herdr's `agent_session` value alone; a stale value with a shell in the foreground proves nothing. Phase 14a: with `codex` as the one foreground process, the value proves the pane.

### Phase 4b — `production_tree.py` deleted  · status: done

**Scope (sdlc, 2026-11-03):** only the `production_tree.py` deletion stays here. The Owns and Hub deletions moved to Phase 4c, which devtools-unit owns.

**Depends on:** the handler unit's reader switch (cargo-liner Phase 57c; Phase 3n item 6). On 2026-11-04 `~/.config/cargo-handler/config.toml:19` still runs `production_tree.py --json`. 4b is only the `production_tree.py` deletion and is off the rename track: it lands when 57c lands (sdlc, 2026-11-04).

#### Work Order

**Goal:** `production_tree.py` is gone, and the handler reads the hierarchy from staff.

**Spec:** First, `grep -rn production_tree scripts commands ~/.config/cargo-handler` finds no caller outside the deleted files, and the handler config names `staff.py show --json --live`. Then `production_tree.py`, `test_production_tree.py` and every live doc mention (`showrunner-automation.md`, `long-lived-workers.md`, this plan's live mentions) go. The one code caller found on 2026-11-04 is `scripts/astra/design_roles.py:560` (an import); it switches to staff. In `scripts/direct/assertion_guard_test_support.py` and `scripts/production/test_tidy_wrapped.py`, `production_tree` is only a local variable name for a worktree path: not a dependency, and it stays. No wrapper or copy remains.

**Files:**
- `scripts/production/production_tree.py`, `scripts/production/test_production_tree.py` — deleted.
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py` — the caller.
- `docs/as-built/showrunner-automation.md`, `docs/as-built/long-lived-workers.md` — the `production_tree` mentions go.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files.
- `test` — the tests and fixtures.

**State:** none.

**Acceptance gate:** no executable reference remains: `grep -rnE 'import production_tree|from production_tree|production_tree\.py' scripts commands docs/direct docs/as-built ~/.config/cargo-handler` finds nothing (a local variable named `production_tree` is not a reference); the real handler pane still shows the hierarchy; the suites pass; full gate green. No rerun test: a delete.

### Phase 4o — The Production plan header and the Gates table deleted  · status: moved: devtools-unit (sdlc, 2026-11-04)

**Depends on:** 4c (the Hub files and Owns deletions in the same `merge_checkpoint.py`; moved from 4b, review 2026-11-04); 3w (the text); 3y (the rename tools read the row); devtools-unit's cuts of `rename_unit.py` and `transfer_unit.py` (4v, then rename and transfer), merged first. Lands before 4g, which assumes both are gone.

#### Work Order

**Goal:** a unit's production comes from its staff row's parent, not from a `> **Production:**` plan header, and no tool reads a Gates table.

**Constraints from prior phases:** Changing `fake_unit_host.py`, `transfer_unit_test_support.py` or `LayoutHerdr` runs the whole `scripts/production` suite in the gate; a live-to-stopped fixture step updates the foreground evidence and keeps every other reply, such as an installed close failure.

**Spec:**
1. **Production header.** Every reader of the header switches in this phase, `/unit:direct` `<ProductionUnit/>` and `transfer_unit.py` step 2 included: a unit's production comes from its staff parent (sdlc, 2026-11-03), not from the plan header. `rename.py`, `rename_unit.py` and `transfer_unit.py` stop writing and rewriting the header. Live plan headers lose the line.
2. **Gates table.** `merge_checkpoint.gate_names` (`merge_checkpoint.py:698`), its table format, its command generation and the live doc sections go. A start-order wait becomes an owed action in the showrunner's notes; merge order uses `cargo-berth sequence`.
3. `merge_checkpoint.scope` keeps its staged-file, shrink and as-built checks.

**Files:**
- `scripts/production/merge_checkpoint.py`, `scripts/production/test_merge_checkpoint.py`, `scripts/production/merge_checkpoint_test_support.py` — the Gates reader.
- `scripts/production/rename.py`, `scripts/production/rename_unit.py`, `scripts/production/transfer_unit.py` — the header writers.
- `scripts/production/test_rename.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_rename_unit_style.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/rename_test_support.py`, `scripts/production/rename_unit_test_support.py`, `scripts/production/transfer_unit_test_support.py` — their tests and fixtures.
- `commands/unit/direct.md`, `docs/production_format.md`, `commands/showrunner/promote_unit.md`, `commands/showrunner/transfer_unit.md`, `commands/unit/question.md`, `commands/unit/ci.md`, `commands/change_review.md` — the header and Gates text (`<ProductionUnit/>` in `direct.md`).
- `docs/plans/*.md` — the live header lines and Gates tables.
- `scripts/direct/test_workers.py`, `scripts/direct/test_progress_history.py`, `scripts/direct/test_plan_history.py` — fixtures that carry the header.

**Workers:** `1 writer + 1 tester` — `impl`: the non-test files; `test`: the tests and fixtures.

**State:** none.

**Acceptance gate:** `grep -rnE '\*\*Production:\*\*|gate_names|## Gates' scripts commands docs/direct docs/production_format.md` finds nothing; a unit director in a real production finds its showrunner from the row; a real merge checkpoint runs with the staged-file, shrink and as-built checks; the suites pass; full gate green. No rerun test: a delete.

### Phase 4m — Port audit: every unit's port is in its `.unit.toml`  · status: done (models-unit 42a7ef32d on sdlc; live proof by style-unit)

**Evidence (style-unit, 2026-11-04):** the audit reads 13 AppPort, 54 NoAppPort and 1 ConfigMissing rows and writes nothing. 4d deleted the audit after this run. Tables: /tmp/claude/scratch/style-unit-ports-evidence/.

**Owner:** models-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3q (`.unit.toml` exists) and 3k (its writers emit taplo-formatted text). 4c depends on it (Rename track).

#### Work Order

**Goal:** before 4c deletes the Units table, every live and retired unit has its port in its `.unit.toml`, from a recorded source, never from a likely value.

**Spec:**
1. List each staff unit row with its configuration port and the Port cell from its bound production document.
2. Read active configuration from the worktree and retired configuration from its parked file; use `cleanup=False` for every audit read.
3. Read remote configuration on its recorded machine; record the source path and bound-document evidence.
4. Report `AppPort`, valid `NoAppPort`, `ConfigMissing`, or `unavailable: <cause>`; never treat missing configuration as `NoAppPort`.
5. List a missing or different port for sdlc; the audit writes nothing and guesses no value.
6. Add one guarded port repair in `unit_config`, taking the recorded old value and sdlc's settled value.
7. Require the recorded old value before publication; accept an already settled value as `unchanged`, and refuse any third value.
8. Use this repair for active and parked configuration; preserve retirement facts, responsibilities, and repository formatting.
9. Publish through the shared atomic writer; a rerun after interruption finishes once.
10. After repair, a rerun of the audit prints no gap and exits 0.

**Files:**
- `scripts/production/audit_unit_ports.py` — new: the read-only audit command.
- `scripts/production/unit_config.py` — the guarded port repair.
- `scripts/production/test_audit_unit_ports.py`, `scripts/production/test_unit_config.py` — tests and fixtures.
- `scripts/production/staff.py`, `scripts/production/production_doc.py`, `scripts/shutdown/remote.py` — read only.

**Workers:** `1 writer + 1 tester`
- `impl` — the audit command and guarded repair; hub: `scripts/production/unit_config.py`.
- `test` — their tests and fixtures.

**Acceptance gate:** the audit prints no gap on natedev and the Mac; a rerun changes nothing and exits 0; full gate green.

**Acceptance gate (port evidence):** test valid `NoAppPort`, missing configuration, bound-document evidence, parked files, and unavailable remote reads.
Interrupt the guarded repair before and after publication; reruns preserve retirement facts and responsibilities.
Test unchanged reruns and refusal when the recorded old value differs.

### Phase 4c — Units table deleted  · status: moved: devtools-unit owns it as its 4u, "Units tables are gone" (sdlc, 2026-11-03; merge expected 2026-11-04)

**Hand-off (sdlc, 2026-11-03):** the work order is `phases/4c/work_order_for_devtools.md` in the herdr run folder. Rulings: the Port audit copies each cell into `.unit.toml`, then deletes the cell; the row order is the `staff.json` row order; historical members with no staff row drop; the Owns and Hub deletions move here from Phase 4b.

**Depends on:** 4m (the Port audit); 3h's native-id and location completeness proof.

**WRAPPED (devtools-unit 4i, via sdlc, 2026-11-02):** `production_units.py:27` holds `WRAPPED`, and `tidy_wrapped.py`, `staff_sources.py` and `staff_test_support.py` import it. When 4c rebuilds or deletes `production_units.py`, move `WRAPPED` to one leaf module and switch all three importers in the same commit. The leaf module must not import `update_registration` or `tidy_wrapped`, because that makes an import cycle.

#### Work Order

**Goal:** no production doc has a Units table and no code or skill reads or writes one; a unit exists as its staff row with its `.unit.toml`.

**Spec:** First the importers that later steps rebuild stop needing the table module: `Production`, `read_production`, `production_field` and `Refusal` move to the existing `scripts/production/production_doc.py` (keep its current optional-field and promotion API; the doc reader no longer requires a table), and `rename.py`, `rename_unit.py`, `rename_checkout.py`, `transfer_unit.py` (`transfer_unit.py:34` imports `Production` and `read_production`) and every other importer import them from there; their Units-table steps (cell edits, table commits) go, and the rest of their work stays until Phases 4f and 4g. Then the last fill audit: `staff_sources.py fill --dry-run` and `fill --retired --dry-run` report `unchanged` with an empty missing-facts list for each doc still holding a table. Save a table-removal handoff under `~/.local/state/staff/table-removal.json`: doc path, machine, merge branch, audited revision or ignored-doc content hash, and pending or removed state. Use the shared atomic writer. Before removing each table, verify that its source still equals the audited input; changed input needs a new audit. After removal, record the removal commit or ignored-doc content hash. On a retry, prove already removed docs by that recorded result and audit only pending docs. If stopped after a commit but before the handoff write, identify the exact expected removal diff from the audited revision, then record it; refuse any other diff. Never feed an already removed table to the importer. Keep the handoff until 4d deletes the importer. Keep the parser in `scripts/production/unit_table.py` for pending audits and the importer only; no normal reader imports it. Phase 4d deletes that parser and `production_units.py` with the importer; `add_unit.py` writes no row and makes no plan commit for one (`append_row` :737, `row_is_present` :428, `desired_row` :418); `retire_unit.py` and `resume_unit.py` make no Units-table commit; `add_unit.NIGHTLY_REVIEW_UNIT` (:83), `rename_unit.RESERVED` (:80) and `nightly_review.NIGHTLY_UNIT` (:224) go, as the `nightly-review` responsibility marks the unit; the `## Units` section leaves `docs/production_format.md`; one commit on each production's merge branch removes the table from its doc, after its pending audited input and removal handoff pass the checks above.

**Files:**
- `scripts/production/remove_unit_tables.py`, `scripts/production/test_remove_unit_tables.py` — resumable audit and branch-removal handoff.
- `scripts/production/unit_table.py`, `scripts/production/test_unit_table.py` — importer and pending audits only; delete in 4d.
- `scripts/production/production_doc.py`, `scripts/production/test_production_doc.py` — extend the existing module: the doc-level API from `production_units.py`.
- `scripts/production/production_units.py` — only the importer's parser stays.
- `scripts/production/rename.py`, `scripts/production/rename_checkout.py`, `scripts/production/transfer_unit.py` — import from `production_doc.py`; their table steps go.
- `scripts/production/add_unit.py`, `scripts/production/retire_unit.py`, `scripts/production/resume_unit.py`, `scripts/production/rename_unit.py`, `scripts/nightly_review/nightly_review.py`, `docs/production_format.md` — the table parts go.
- `docs/plans/*-production.md` — the table goes, on each merge branch and in the Mac productions' docs.
- `scripts/production/test_rename.py`, `scripts/production/test_rename_checkout.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_resume_unit.py`, `scripts/production/test_rename_unit.py`, `scripts/nightly_review/test_nightly_review.py` — their tests.
- `scripts/production/add_unit_test_support.py`, `scripts/production/rename_unit_test_support.py` — fixtures.
- `scripts/production/unit_lookup.py`, `scripts/production/live_units.py` — drop the table-ordering import from `production_units` (Phase 3s review).

**Workers:** `1 writer + 1 tester`
- `impl` — code and docs; hub: `scripts/production/add_unit.py`.
- `test` — the tests and fixtures.

**State:** audit/removal handoff; the Units table leaves every production doc, one commit per merge branch.

**Constraints from prior phases:** Phases 3t-3w switched every reader; the Owns and Hub deletions moved here from 4b (review 2026-11-04).
- Phase 3u (2026-11-02): `board_json.py`, `phase_table.py` and `measure.py` still import `unit_table.Refusal`, `unit_table.cell_value` and `unit_table_cells.open_step` / `run_events`. They read no membership. Move them with `Refusal` before the table module goes.
- Phase 3h review (2026-11-01): Port's home is `UnitConfig.port: AppPort | NoAppPort` in `.unit.toml` (`unit_config.py:42`), which `add_unit.configuration_for` and the fill already write and retire parks. Before the table goes, audit every Port cell into that field (a value that differs is listed for sdlc, never filled) and switch the remaining Port readers through 3s; test active and retired reads, rename and transfer with no table and no new store.
- Phase 3h review (2026-11-01): keep the audited-source hashes, interrupted-commit recovery and the handoff to 4d; acceptance tells unproved retained-member facts from historical members that never had a row.
- Phase 3s review (2026-11-02): `unit_lookup.py` and `live_units.py` still import table ordering from `production_units`. Remove that dependency here, with importer tests.

**Acceptance gate:** `grep -rnE 'unit_table|units_table|production_units|## Units|Units table|Units row|NIGHTLY_REVIEW_UNIT|NIGHTLY_UNIT' scripts commands docs/direct docs/production_format.md` finds only the importer, its parser, the pending-audit helper and their tests; a real rename and a real transfer of a throwaway unit still work; a real add, retire and resume of a throwaway unit work with no table; the doc removal stopped after one merge branch finishes on rerun, and a rerun after it changes nothing and exits 0; full gate green.

**Acceptance gate (handoff):** stop after the audit, after the first branch removal and after its commit before recording completion. Each retry equals one clean run, never audits an already removed table, and refuses a changed unaudited doc. The checker row/config-only regression remains green. A completed rerun changes no byte.

### Phase 4d — Showrunner doc fields, parent files and the seed importer deleted  · status: moved: devtools-unit (sdlc, 2026-11-04)

**Depends on:** 4c (its last fill audit); 3x; 3n (backend record); 4a (timer conversion); 3h's native-id and location completeness proof.

#### Work Order

**Goal:** a showrunner is its row: its id links to its doc and merge branch, and it has one name. `Showrunner session`, `Formerly`, `Parent production`, parent files, the showrunner list of `showrunners.json`, `tmux-names` and the seed importer are gone. This absorbs old Phase 16.

**Spec:**
1. **Doc fields.** `worker_admission.showrunner_member` stops reading `former_slugs`; a recorded rename journal still resolves its exact old-to-current holder id through the shared resolver. A missing or conflicting journal refuses; no name-only replacement is added.  `showrunners.space_from_text` (:403) and `former_slugs` (:356) and their callers (`phase_table.py:1707`) go; `rename_outside.py`, `rename_checkout.py`, `change_review.py`, `add_style.py`, `unit_files.py` stop reading or writing `Parent production`; one commit per merge branch removes the three lines from each doc. The row name is the one showrunner name: seed's slug-to-name split (`sdlc` → `tools`) ends with one rename of the row or the slug, decided by the showrunner, and the slug is used only for the doc file name and the merge branch.
2. **Parent files.** `showrunner_parent.py`'s parent-file operations, `rename.py:520` and `showrunner_tree.release_edges` go; `~/.local/state/showrunner/*/parent` is removed. Wrap keeps telling each former parent and child from `wrap_release.json`.
3. **`showrunners.json`.** Its showrunner list is already unread (`showrunners.py:110`). Its five settings (`threshold_percent`, `repeat_minutes`, `stall_minutes`, `faults_to`, `always`; read by `stall_watch.py:748`, `escalate.py:436`, `quota_alert.py:601`) move to `config/showrunners.conf`, read at use through `tunables`; `faults_to` and `always` hold staff ids; `showrunners.defaults()` (:88) goes (no hard-coded config). The file is removed.
4. **`~/.local/state/tmux-names`** has no reader; it is removed.
5. **Importer (old Phase 16 item 2).** The `staff_sources` `seed` and `fill` entry points, `_collect`, `_print_plan`, `_seed_member`, the legacy name/mark fallback replaced by Phase 3h native runtime lookup and `scripts/production/production_units.py` (the parser only they read) go. `member`, `showrunner_members` and `unit_parent` stay. The unfilled row states (`WorktreeUnfilled`, `DocUnfilled`, Phase 3q item 2) go: `from_json` then requires the field on its kind, after a check that `staff.json` holds no unfilled row.

**Files:**
- `scripts/production/worker_admission.py`, `scripts/production/test_worker_admission.py`, `scripts/production/test_worker_fill.py` — remove Formerly reads; retain proved rename-journal handling.
- `scripts/production/remove_unit_tables.py`, `scripts/production/test_remove_unit_tables.py` — remove the 4c helper after its handoff proves every table removal.
- `scripts/production/showrunners.py`, `scripts/production/showrunner_parent.py`, `scripts/production/showrunner_tree.py`, `scripts/production/rename.py`, `scripts/production/rename_outside.py`, `scripts/production/rename_checkout.py`, `scripts/production/change_review.py`, `scripts/production/staff_sources.py`, `scripts/production/stall_watch.py`, `scripts/direct/phase_table.py`, `scripts/message/escalate.py`, `scripts/whoami/quota_alert.py`, `scripts/style/add_style.py`, `scripts/style/unit_files.py` — the field, parent-file and list readers go.
- `config/showrunners.conf` — new.
- `scripts/production/production_units.py`, `scripts/production/unit_table.py`, `scripts/production/test_unit_table.py`, `scripts/production/staff.py`, `scripts/production/test_staff.py` — the parser modules go; the unfilled states go.
- `scripts/production/test_{showrunners,showrunner_parent,showrunner_tree,rename,rename_checkout,change_review,staff_sources,stall_watch}.py`, `scripts/direct/test_phase_table.py`, `scripts/message/test_escalate.py`, `scripts/whoami/test_quota_alert.py`, `scripts/style/test_add_style.py`, `scripts/style/test_unit_files.py` — their tests.
- `scripts/production/showrunner_registry_test_support.py`, `scripts/production/rename_test_support.py`, `scripts/production/staff_test_support.py` — fixtures.
- `scripts/production/session_staff.py`, `scripts/production/session_labels.py` — remove the aliases and unfilled variants (Phase 3s review).

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/showrunners.py`.
- `test` — the tests and fixtures.

**State:** parent files, `showrunners.json`, `tmux-names` removed; three doc lines removed per merge branch.

**Constraints from prior phases:** Phase 3t reads the doc from the row; Phase 4a timers by staff id; Phase 3n's backend record is in place before the importer goes.
- Phase 3h review (2026-11-01): the doc that stays holds settings — UX guide, Proposals, Change review, Updates, Showrunner checkout, Source plans — plus the user's rules, which only the user's word changes. Decisions and close-out move to the showrunner's notes; Gates goes in 4b. Keep the fresh completeness gate before unfilled states and importers go. This is metadata only: no showrunner, workspace or tab name changes (shown names need the user's own go).
- Phase 3s review (2026-11-02): remove the `session_staff.py` and `session_labels.py` aliases (`former_slugs`, `space_from_text`) and the unfilled variants (`WorktreeUnfilled`, `DocUnfilled`, `LocationUnfilled`) only after the stored-location check passes, with importer tests.
- Phase 3s review (2026-11-02): `RetiredFactsUnavailable`, `BranchUnreadable` and `DocumentUnavailable` stay after the completeness gate: a stored location cannot guarantee a later file or git read (`RowResolver.unit_config`). Keep their tests.
- Phase 3s review (2026-11-02): the required-location change sends a notice to cargo-liner (an outside `staff.json` reader) with a representative stored-record fixture.

**Acceptance gate:** `grep -rnE 'Showrunner session|Formerly|former_slugs|space_from_text|Parent production|showrunners\.json|tmux-names|/parent"|release_edges|production_units|WorktreeUnfilled|DocUnfilled' scripts commands docs/direct docs/production_format.md` finds nothing; `grep -nE '^def (seed|fill)\b|"(seed|fill)"' scripts/production/staff_sources.py` finds nothing (a search for `def fill` alone also finds unrelated `change_review.py` and `proposal_issue.py` functions); a schema test refuses a unit row with no `worktree`; attach, detach, rename and wrap tests make no parent file; a wrap stopped after its showrunner row's step tells each former parent and child once on rerun; a changed `stall_minutes` takes effect with no code change; a test shows a new worker in `staff.py show --json --live` with no seed run; the doc-line removal stopped after one branch finishes on rerun; full gate green.

**Acceptance gate (identity and consumers):** require the 3h completeness proof on a fresh snapshot with no unresolved runtime identity or unfilled location before deleting fallbacks. Admission and worker-fill tests resolve a renamed holder using its journal with no Formerly field, and refuse ambiguous or absent journal evidence. Consume 4c's completed handoff without reading removed tables; remove that handoff and helper only after all removals are proved.

### Phase 4e — Legacy pane marks deleted; native runtime ids select rows  · status: done

**Depends on:** 3v and every reader of the marks; 4d (fallback removal); 3h's native-id and location completeness proof.

#### Work Order

**Goal:** native runtime ids select members; pane environments and restart records carry no copied staff identity mark.

**Spec:**
1. Stop setting `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID` in `add_unit.py`; remove `unit_lookup.PRODUCTION_MARK` and `UNIT_MARK`.
2. Remove `SHOWRUNNER_` from `pane_launch.KEPT_PREFIXES`; `pane_launch.py` owns mark constants and recording logic.
3. Keep no copied staff identity in `herdr.MEMBER_MARKS` or `unmarked_environment`.
4. Select the hook's row kind through one `staff.snapshot()`; remove the marks from `commands/showrunner/promote_unit.md`.
5. Switch `every_marked_unit`, `MarkedProductions`, `session_unit_marks`, `group_marked_units`, and their callers to native runtime identities.
6. Rename these APIs to `every_labeled_session`, `LabeledProductions`, `session_labels_of`, and `group_labeled_sessions`; remove text that says "unit marks".
7. Existing panes keep their old marks until their next launch; no reader reads those marks.

**Files:**
- `scripts/production/add_unit.py`, `scripts/production/unit_lookup.py`, `scripts/production/session_labels.py`, `scripts/production/session_staff.py`, `scripts/shutdown/pane_record.py`, `scripts/herdr/pane_launch.py`, `scripts/herdr/herdr.py`, `scripts/hooks/herdr_tab_title.py`, `commands/showrunner/promote_unit.md` — the mark writers and readers go.
- `scripts/production/test_add_unit.py`, `scripts/production/test_unit_lookup.py`, `scripts/shutdown/test_pane_record.py`, `scripts/herdr/test_herdr.py`, `scripts/hooks/test_herdr_tab_title.py` — their tests.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/session_labels.py`.
- `test` — the tests.

**State:** new launches carry fewer env names.

**Constraints from prior phases:** Phases 3u and 3v stopped every read; Phase 4 names launches and the hook. The shutdown unit owns `pane_record.py`; agree it first.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 3h review (2026-11-01): the scope is legacy-label removal: `SHOWRUNNER_*` marks and mark-oriented APIs. The `STAFF_ID` migration shipped in 3h; drop it from this phase. Acceptance: stored references with changed current sessions, missing rows, remote failure, and retained unknown and duplicate process evidence; removing a fallback never turns incomplete membership into confirmed absence.
- Phase 3s review (2026-11-02): after 3s the known search hits are `production_tree.py`, `test_production_tree.py` and the mark-reader names. Convert `MarkedUnit`'s empty identity and process fields to named states during the API change. Workers: `1 writer + 1 tester`; `session_staff.py` is the source hub, and the caller fixtures are in the test lane.

**Acceptance gate:** `grep -rnE 'SHOWRUNNER_UNIT|PRODUCTION_MARK|UNIT_MARK\b|unit_marks|marked_unit|unit marks|marked unit' scripts commands docs/direct docs/as-built` finds nothing; the suite of each caller module passes; a real unit launch carries no `STAFF_ID`, and a real restart selects its row by native resume id and takes its current name; full gate green.

**Retained launch contract:** preserve `pane_launch`'s validated kind, strict retirement, and restore completion checks.

### Phase 4f — A transfer moves the unit's tabs into the new production's group  · status: done

#### As-built

- `Herdr.move_row_tabs(row_id, *, record=...) -> bool` in `herdr.py` moves the unit row and the rows whose `parent` is the unit into the workspace named by the parent row's `name`. It returns `False` only when every row is already in place.
- Membership is native evidence: `Herdr._row_panes` takes a pane when its native process runs the row's `current_session`, or when its one foreground process is `codex` and its `agent_session` equals `current_session` (Phase 14a). A label, a title or a pane mark never makes a pane a member.
- A tab moves as one unit. `_move_row_tab` replays the `layout.export` tree in the new workspace, keeps terminals and focus, and orders tabs with `tab.move`. A zoomed tab is refused with one line. Public pane ids can change; terminals stay.
- A stopped row, also a director whose shell pane stays after exit, gets a saved binding in the new workspace and does not move. Restart opens it there.
- Unknown pane evidence prints one anomaly line and skips that row.
- `Herdr.retitle_row(row_id) -> bool` retitles each pane of the row to its `name`, with worker tab protection.
- `staff_move._place_tab` calls `move_row_tabs`; the tab-label lookup and its result types are gone.

**Files:**
- `scripts/herdr/herdr.py` — `move_row_tabs`, `_move_row_tab`, `_row_panes`, `retitle_row`.
- `scripts/production/staff_move.py` — `_place_tab` call.
- `scripts/hooks/herdr_tab_title.py` — tab titles through the shared helpers.
- `scripts/production/fake_unit_host.py`, `scripts/production/transfer_unit_test_support.py` — `PaneShellFixture`: live claude with native identity; `stopped()` resets only that pane's process-info reply.
- `scripts/herdr/test_herdr.py`, `scripts/herdr/test_herdr_sessions.py`, `scripts/production/test_staff_move.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/test_transfer_unit_pane_shell.py`, `scripts/hooks/test_herdr_tab_title.py` — the tests.

**Binds later work:** a pane is a row member only through `Herdr._row_panes`; a stopped row gets a saved binding, not a move; read pane locations fresh after a transfer, because a rebuild can change public pane ids.

**Gotchas:** accepted limit: an interrupted multi-pane rebuild can lose the split ratio and leave panes in the source workspace; the anomaly line names the tab and the panes left behind. A fixture that changes `fake_unit_host.py` breaks transfer, rename and retire tests far from it: run the whole `scripts/production` suite.

**Ruled out:** a move journal (devtools deleted the journal); marks or labels as membership evidence.

### Phase 4h — A unit folder moves with no rename  · status: done

#### As-built

- `unit_move.py move-folder --unit <staff id> --to <path>` moves one worktree without renaming at a boundary with no open run phase.
- `atomic_file` writes `~/.local/state/staff/folder_moves/<staff id>.json` before changes. The record holds both paths and each step's completion state, without copied unit facts.
- The move stops the session and retains its pane. `git worktree move` moves the folder. The move updates the staff row, launch `cwd`, and run folder through `unit_move.move_run`. The same `current_session` resumes in the same pane through Claude `--resume <id>` or Codex `resume <thread id>`. A stopped unit stays stopped.
- Resume reads the backend kind from the retained launch record. An unreadable startup process produces `PendingFolderStartup`; the deadline requests `rerun move-folder` because the resumed session lacks proof.
- A rerun completes unfinished steps, including recovery before the old tab closes. A finished rerun prints `unchanged` and exits 0.
- Live proofs cover a stopped unit and Claude with the same session, pane, and terminal in the new folder. Both finished reruns print `unchanged`.

**Files:**

- `scripts/production/unit_move.py`, `scripts/production/staff.py` — move records, recovery, and the staff worktree path.
- `scripts/herdr/herdr.py`, `scripts/herdr/pane_launch.py`, `scripts/shutdown/pane_record.py` — process evidence, resume, and retained launch records.
- `commands/showrunner/rename_unit.md` — command text.
- `scripts/production/test_unit_move.py`, `scripts/shutdown/test_pane_record.py`, `scripts/production/rename_test_support.py` — move tests, launch-record tests, and fixtures.

**Binds later work:** **Rename rebuilt on the row** reuses the move record, retained pane, and retained backend kind. Shared retirement requires confirmed pane closure through `pane_launch.retire_launch`. **One live, stopped or unknown reader for restore, restart and stop** owns live Codex, failed-close, and `herdr-up` proofs. It also owns app-server hosted-thread coverage and the anomaly rule.

**Gotchas:** Unknown processes produce notes named by herdr tab, then session, then pid. Unknown process evidence never causes refusal; refusal requires proof that the target is live. Startup process evidence can omit argv. Claude can repeat the trust prompt for a throwaway folder. Trust handling checks the exact scratch folder. Rerun comparisons require two equal settled launch reads. Concurrent staff writes prevent a hash comparison alone from proving a probe write.

### Phase 4i — Questions are their own type  · status: done (devtools-unit, main eb762177b)

**Owner:** devtools-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3a. It lands before 4g, because a rename rewrites no questions (sdlc, 2026-11-01). The user asked for this through Morty.

#### Work Order

**Goal:** an open question is its own record type beside proposals, in the same store, and the asker is held by fixed staff id.

**Spec:**
1. Filing, review, queues, answers and relay (`update_registration.py:667`), question counts, and the deferred and review states treat a question as its own type. The asker is its fixed staff id, never a name.
2. `/next question` stays as it is.
3. One table command in the form of `/showrunner:proposals` lists the open questions. Its data is in the proposals store.

**Files:**
- `scripts/production/update_registration.py`, `scripts/production/test_update_registration.py` — the question type.
- `commands/showrunner/proposals.md`, `commands/showrunner/question.md`, `commands/showrunner/questions.md` — the table command (new) beside the proposals table; their Codex mirrors regenerate, and a reader that `grep -rn question scripts/production` finds joins them.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/update_registration.py`.
- `test` — the tests and fixtures.

**State:** open questions in the proposals store, keyed by the asker's fixed id.

**Constraints from prior phases:** Phase 3h fixed ids. A reader joins a stored reference by `by_member_reference`.

**Acceptance gate:** a transferred or renamed asker still gets the answer; ordinary reminders keep their behavior; the table command lists a real open question; `/next question` behaves as before; a rerun of the store change changes nothing and exits 0; full gate green.

### Phase 4j — No ad-hoc workspace  · status: done

**Depends on:** the 3h live move. Not on 3s-4h. The user, through natedev (sdlc C48, 2026-11-01): "there should not be an ad-hoc". It lands before 4h.

#### Work Order

**Goal:** no code opens or names an `ad-hoc` workspace. A unit opens in its parent showrunner's production workspace. A standalone session opens in its own workspace, labeled with its tab name.

**Spec:**
1. `scripts/herdr/herdr_sessions.py`: delete `ADHOC_SPACE`. A standalone start or resume opens its tab in a workspace whose label is the tab name. `Herdr.open_tab` creates that workspace when no workspace has that label.
2. `scripts/shutdown/restart.py` `herdr_space`: a showrunner keeps its production workspace. A unit takes the production workspace of its parent row (`showrunners.herdr_space`, as `add_unit.py:974` does). Any other session takes a workspace labeled with its own name. The fixed parent row is the only source for a unit; a name or folder is never used to find the parent.
3. The standalone workspace rule is the director's call (sdlc may rule otherwise): one tab for each standalone session, and no shared catch-all workspace.
4. Live tabs do not move. On 2026-11-01 the survey found 0 tabs in `ad-hoc` and all 46 live units in their production workspace.
5. The code and `docs/as-built/herdr-adoption.md` (lines 5, 84, 105, 203) change in the same commit.

**Files:**
- `scripts/herdr/herdr_sessions.py`, `scripts/shutdown/restart.py`, `docs/as-built/herdr-adoption.md` — the rule and its text.
- `scripts/herdr/test_herdr_sessions.py`, `scripts/shutdown/test_restart.py`, `scripts/herdr/test_herdr.py`, `scripts/accounts/test_launch_accounts.py` — tests and fixtures that name `ad-hoc`.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/herdr/herdr_sessions.py`.
- `test` — the tests and fixtures.

**State:** none stored. The workspace comes from the staff row at launch.

**Constraints from prior phases:** Phase 3h fixed ids; a unit's parent is its row's `parent`. Tabs show the short session name only. Every design works for Claude and Codex in any role and account. `scripts/shutdown/restart.py` is a shared shutdown file: tell the shutdown unit before the edit lands.

**Acceptance gate:** `git grep -n '"ad-hoc"' -- scripts` finds nothing outside a test that proves the name is gone; a throwaway standalone session opens in a workspace with its tab name; a restart of a throwaway standalone session and a throwaway unit puts each in its rule's workspace; a rerun of each opens no second tab; full gate green.

### Phase 4g — Rename rebuilt on the row  · status: done

**Depends on:** 4o, 4c, 4d, 4e, 4h, 4i, and the records stage (3x, 3y, 3z, 4a). Last step of the rename track. No pane phase waits for it. Hash 3f8339f5 stays unmerged; reuse only its tab retitle, kept-pane resume and finished-journal rerun.

#### Work Order

**Goal:** a unit rename changes the row's name and the live tab and session name; a showrunner rename changes the row and the workspace name; moving a unit's folder and branch is one further optional step. The rename sweep and most of the rename code go.

**Spec:**
1. **Unit rename** (`rename_unit.py`): `staff.rename` of the row, `Herdr.retitle` of the tab and pane, and the session name at the next launch (Phase 4). No store, doc, prose or Units cell is rewritten: they hold the staff id. Hub files, Gates and plan headers are deleted before this phase (4c, 4o), so nothing lists them (`EDITED_COLUMNS` :82 and `_plan_text` :497 go). Same-name rename is the repair path: it changes only the parts that differ and prints `unchanged` when all match.
2. **Folder and branch move** (as built, sdlc rule 2026-11-04, from the user): a new name moves the folder and the branch by `unit_names.swap_stem` in the same parent (`cargo-liner-port` becomes `cargo-liner-x`). A folder or branch that does not end with the old name keeps its place and prints one anomaly line. `--move-worktree`, `--to-folder` and `--to-branch` are deleted.
   `unit_move.move_folder` stops the session found by `current_session`, runs `git worktree move` and `git branch -m`, calls `staff.set_worktree`, then `restart.start_row`. A journal (target name, destination, branch, Git admin path) is written before the first staff name write; a rerun finishes a pending journal first.
   Same name means the name is equal and no rename journal is pending. A rerun after a complete rename prints only `unchanged`.
   A stopped unit updates its row and stays stopped.
   Restart recovery is by hand (herdr-unit, 2026-11-04, Astra review of F006; no stored intent): when the move stops a row and cannot start it again, it prints one anomaly line with `shutdown.py restart --only <staff-id>` at once. A rerun cannot tell why a row is stopped, so it prints one information line with that command for each stopped row of the unit and starts nothing. A row stopped before the move stays stopped.
   The only refusals are the 5 in `staff-lifecycle.md` section 4 (target folder exists, same-row concurrent operation, a second copy of a running session, uncommitted work deleted, a signal to a non-target process).
3. **Showrunner rename** (`rename.py`): `staff.rename` and the workspace and tab names; timers and stores already hold the id. A rename to the name the row already holds still repairs a live session name, tab or workspace that differs from the row (sdlc, 2026-11-01, from the user on C48: the fps row's session runs as `performance`; the user runs `rename fps fps`, not a unit). It prints `unchanged` only when the row, session name, tab and workspace all agree. The same rule holds for a unit rename. Moving the doc file and merge branch is the same optional further step, with `--to-doc <path>` and `--to-branch <name>` journaled first and `staff.set_doc` after the move.
4. **Tunables.** `rename_unit.py`'s `LIVE_WAIT_SECONDS` (:86, with its `RENAME_UNIT_LIVE_WAIT` environment override, which goes), `RUN_STOP_WAIT_SECONDS` (:89), `POLL_SECONDS` (:90) and the 900 s subprocess timeout (:671) move to `config/production.conf`, read at use through `tunables`, each as `min(configured limit, remaining deadline)` with a changed-config test.
5. **Delete** `unit_rename_sweep.py` and its test, the `verify` sweep of `rename_unit.py`, `rename_state.py`'s rewriters, `rename_outside.py` and every move step left in `rename.py` and `rename_unit.py`; the rename journal keeps only the steps above.

**Files:**
- `scripts/production/rename_unit.py`, `scripts/production/rename.py`, `scripts/production/rename_state.py`, `scripts/production/rename_outside.py`, `scripts/production/rename_checkout.py`, `scripts/production/unit_move.py`, `commands/showrunner/rename_unit.md`, `commands/showrunner/rename.md`, `commands/unit/rename.md` — the rename and its text.
- `scripts/production/unit_rename_sweep.py`, `scripts/production/test_unit_rename_sweep.py` — deleted.
- `config/production.conf` — the rename time limits.
- `scripts/production/test_rename_unit.py`, `scripts/production/test_rename.py`, `scripts/production/test_rename_state.py`, `scripts/production/test_rename_checkout.py`, `scripts/production/test_unit_move.py` — their tests.
- `scripts/production/rename_test_support.py`, `scripts/production/rename_unit_test_support.py`, `scripts/production/rename_rerun_test_support.py` — fixtures.
- `scripts/production/staff.py`, `scripts/production/test_staff.py` — `staff.rename` for workers (Phase 3h review).
- `scripts/production/resume_unit.py`, `scripts/nightly_review/nightly_review.py` — transcript lookup by session id (`resume_unit.py` already finds it by session id; check only).

**Workers:** `1 writer + 1 tester`
- `rename` — the non-test files; hub: `scripts/production/rename_unit.py`. The 3y rewriter removals in these files go to `rewriters` (one worker per item, user 2026-11-04); each writer posts its functions before it edits a shared rename file.
- `renametest` — the tests and fixtures.

**Steps by input (sdlc, 2026-11-04: build each step when its inputs land; land 4g right after its last input):**
- Step A, no unlanded input, starts at once: Spec item 3's worker rule (`staff.rename` stops refusing workers; tab label through `retitle_row`), the transcript check, and the same-name repair rule (unit and showrunner) on the current row and `Herdr.retitle`.
- Step B, with 3y on the same files and writer: Spec item 2 (folder and branch move on `unit_move.move_folder`) and item 4 (tunables).
- Step C, after 3x, 3y, 3z and 4a: Spec item 5 deletions of the rewriters and the sweep.
- Step D, after 4c, 4o, 4d and 4e: `EDITED_COLUMNS`, `_plan_text` and every mark or doc-field edit go. Then the acceptance gate and the landing.

**State:** row names, tab and workspace names; on `--move-worktree`, the folder and branch.

**Constraints from prior phases:** Phases 3x-4a keyed every store by staff id; 4c-4e deleted the table, doc fields and marks; Phase 4 takes launch names from the row. Lifecycle reruns keep the pane. Phase 4f: use `retitle_row` with its worker tab protection; do not select panes around `retitle`. Recheck which phase deletes the header and the Gates table before you write the Spec.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 3h review (2026-11-01): no trust or transcript move: the accounts unit trusts `~/worktrees` once on every Claude account (a prerequisite, not a rename step), and `resume_unit.py:184` and `nightly_review.py:153` find a transcript by session id, not from the folder.
- Phase 3h review (2026-11-01): worker rename in every role: `staff.rename` (`staff.py:940`) stops refusing workers. A Claude or Codex worker under a unit or a showrunner changes its row name and tab label; the saved layout binds the fixed row id, so it does not change (review, 2026-11-04); its fixed id, current session and recorded role do not change. A stopped worker renames with no live tab. Tests cover both backends and both holder kinds.

**Acceptance gate:** `grep -rnE 'unit_rename_sweep|rename_scratch|pin_moves|_rewrite_children' scripts` finds nothing; `/showrunner:rename_unit style style` on a real unit whose names agree prints `unchanged`; a same-name rename of a throwaway showrunner and a throwaway unit whose live session name and tab differ from the row sets both to the row's name, and its rerun prints `unchanged`; a real rename of a throwaway unit changes its row, tab and pane label and keeps the same pane and terminal; a real `--move-worktree` stopped after the folder move finishes on rerun, the row's worktree is the new folder, and the unit relaunches there in the same pane; a real showrunner doc move sets the row's doc; a changed `config/production.conf` rename value takes effect with no code change; a rerun of each finished rename changes nothing and exits 0; resume, retire and transfer pass after a rename; full gate green.

**Acceptance gate (folder recovery):** extend `test_unit_move.py` interruption tests through the branch change and each folder step.
Test Claude and Codex with no launch record, a stopped unit, and the resume of `current_session` in the destination.
A finished rename rerun changes no bytes or times.

**Claude live proof safety:** use an owned throwaway probe and a private `STAFF_FILE` copy.
Answer trust only for the exact probe source or destination under `/home/natepiano/scratch/herdrcheck/`; refuse every other path.
Confirm the selected Yes option before Enter; verify prompt disappearance and record any repeated prompt.
Before the rerun snapshot, require the resumed pid, its process that runs `current_session` in the destination, and a confirmed terminal; a Claude launch has no launch record (review, 2026-11-04).
Require two equal reads one second apart; save both snapshots and named byte/time differences with text diffs.
Compare operation-owned staff fields and check for probe ids or scratch paths in live staff.
Record live hashes; an unrelated hash change alone does not prove a probe write.

### Phase 4s — natedev copies the staff record to the Mac after each write  · status: done

**Depends on:** none; sdlc asked for it on 2026-11-04 (the user said yes).

**Work Order:**

Give cargo-handler on the Mac a read-only copy of the staff record (the user said yes, via sdlc, 2026-11-04).
1. In scripts/production/staff.py, after each successful staff write on natedev, copy the record file to the Mac at the same path (`~/.local/state/staff/staff.json`).
2. The copy on the Mac is replaced by atomic rename: write a temporary file in the same folder, then `mv` it over the record. A reader never sees a partial file.
3. natedev stays the only writer. The Mac staff.py keeps forwarding every write to natedev, as now. Do not change Mac reads.
4. Copy only when this process writes the default record on natedev (`OWNER_MACHINE`, no STAFF_FILE override). A test or private STAFF_FILE never copies.
5. A staff write never waits for the network and never fails because of the copy. A failed copy prints exactly one anomaly line (for example `staff: Mac copy failed: <reason>`) and the write still succeeds.
6. The newest record wins: two writes close together must never leave the older file on the Mac. Read the file to send at send time, under one lock, or give an equal guarantee.
7. Find the Mac host the way the code already does (scripts/shutdown/remote.py, `remote.other_machine()` and its peers). Do not hard-code a host name; if a tunable is needed, read it from config at use.
8. Add tests: the copy runs after a default-record write on natedev; it does not run with STAFF_FILE or on the Mac; a failed copy prints one line and the write succeeds; the remote command replaces by atomic rename; newest-wins. Use a fake ssh/runner, never a real ssh.
9. Name the cost you add to each write in your summary.
Files: scripts/production/staff.py (only the write path and a new copy function) and scripts/production/test_staff.py (or a new scripts/production/test_staff_mac_copy.py). Report to the unit director; it sends the hash to sdlc.

### Phase 4t — A test keeps the staff accessor list complete  · status: done

#### As-built

The accessor test scans tracked code and commands for staff imports, including `import_module`, record paths, and `staff.py` commands.
It reads the table's `File` column and reports missing accessors with source lines, plus listed files that no longer exist.
The accessor table records paths, access modes, and staff calls.

**Files:** `scripts/production/test_staff_accessors.py`; `docs/as-built/staff-accessors.md`.

**Binds later work:** Each new staff accessor needs a table row.
**Gotchas:** The scan excludes `test_*.py` and `docs/plans/`; it includes support files and scans Markdown commands only.
**Ruled out:** A second accessor list in the test.

### Phase 4u — The input gate accepts a bridge_status turn  · status: done

**Depends on:** none; accounts-unit finding, 2026-11-04.

**Work Order:**

Fix the input readiness gate (accounts-unit finding, 2026-11-04).
1. scripts/herdr/herdr.py `prompt_input_state` (about line 1975-1990) calls a session ready when herdr reports `interactive_ready`, or when the transcript holds a `system` turn with the text "/remote-control is active".
2. A resumed transcript now holds a `bridge_status` system turn instead of that text. A live session then fails the gate with "destination never became ready for input".
3. Look at real transcripts read-only to learn the exact shape of a `bridge_status` turn: grep only the `type` and `subtype` fields; print no message text and no user content. Transcripts are under ~/.claude*/projects/*/*.jsonl.
4. Make the gate also accept that `bridge_status` turn. Keep one predicate for "Remote Control is on in this transcript"; do not add a second copy of the rule.
5. Search for the same "/remote-control is active" transcript or pane rule in other files (scripts/production/add_unit.py, scripts/shutdown/remote_control.py). Do not edit files outside herdr.py and its test; list each other site and whether it has the same defect in your summary, so the unit director can send it to the owner.
6. Add a regression test in scripts/herdr/test_herdr.py that fails without the fix: a transcript with only a `bridge_status` turn reads as ready.
herdr.py is shared with 3v and 4e writers: post the function you take on the board before you edit it.
Files: scripts/herdr/herdr.py (`prompt_input_state` and a small helper beside it) and scripts/herdr/test_herdr.py.

### Phase 4x — Rename recovery keeps staged document content  · status: done

#### As-built

`DocBranchMove.staged_blob` holds `StagedDocumentBlob(mode, object_id)` or `NoStagedDocumentBlob`; the journal saves one stage-0 source entry before the move.
Recovery stages the destination through `git update-index --add --cacheinfo` before removing the source index entry; working bytes remain unchanged.
Old journals without `staged_blob`, and sources without one stage-0 entry, use `git add` recovery.
Malformed `staged_blob` fields raise `RenameRefused` with the journal refusal message and exit 1.

**Files:** `scripts/production/rename.py`; `scripts/production/test_rename.py`.

**Gotchas:** The destination needs an index reference before source removal because Git can prune an unreferenced staged blob.

### Phase 4y — Rename edges after Step B  · status: done

#### As-built

- `pending_move(unit: staff.StaffId) -> FolderMove | PendingRename | NoPendingMove` distinguishes folder moves from pending renames.
- A pending folder move finishes before rename; displays precede the rename's folder and branch move through `unit_names.swap_stem`.
- Equal names without a journal write and move nothing; a complete rerun prints only `unchanged`, while completed recovery prints `done`.
- Each unmatched folder or branch remains in place with one anomaly line; display errors and unfilled worktrees also report anomalies.
- `HANA_TEST_OWNER` uses `--owner`, otherwise the caller's staff unit or parent unit; missing or unreadable staff gives an empty owner.
- Hana retains `CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID` markers; the isolated herdr test copies all `scripts/lib` except `__pycache__` and `test_*`.

**Files:**
- `scripts/production/rename_unit.py`, `scripts/production/unit_move.py` — rename and recovery.
- `scripts/production/test_rename_unit.py`, `scripts/production/test_unit_move.py` — rename and recovery tests.
- `scripts/hana_shot/hana_shot.py`, `scripts/hana_shot/test_hana_shot.py` — owner selection.
- `scripts/hana_instances/test_hana_instances.py`, `docs/as-built/hana-test-windows.md` — native markers and owner rules.
- `scripts/herdr/test_herdr.py` — complete library copy.

**Binds later work:** Rename callers retain the result lines, anomalies, and restart advice.
**Gotchas:** Recovery requires matching journal source, destination, Git admin path, repository, and branch; another linked worktree cannot substitute.
Pending recovery retains notices and restart advice; only a complete state has silent move handling.
A subprocess timeout prints one `error:` line and exits 1.
**Ruled out:** A new display layer for rename results.

### Phase 4z — A launcher and its agent child are one identity  · status: done

#### As-built

`unit_lookup._herdr_labeled` treats a launcher and its Claude or Codex descendant as one identity when their pane and native runtime id match.
The descendant holds the identity; separate trees or panes retain the duplicate anomaly.
An unreadable ancestry retains that process's evidence while other walks continue.
The Darwin process snapshot runs only when an agent process is present.

**Files:** `scripts/production/unit_lookup.py`; `scripts/production/test_unit_lookup.py`.

**Gotchas:** An inherited environment id does not prove the current conversation; an unregistered Codex descendant remains unknown.
**Ruled out:** A second ancestry walk in `session_staff.py`.

### Phase 5b — The saved layout binds rows from the native process scan  · status: done

#### As-built

`Herdr.record_layout` uses `Herdr._row_panes(record, panes) -> (found, known)` once to bind each proved row to its pane's tab.
An `agent_session` mark binds nothing without a proved native process, or, for Codex, `codex` as the one foreground process (Phase 14a).
Unknown evidence binds nothing, retains saved placement, and skips the retired-row filter for that save.
Each save makes one `pane process-info` call per listed pane.

**Files:** `scripts/herdr/herdr.py` holds layout binding; `scripts/herdr/test_herdr.py` covers stale marks, absent native processes, and unknown evidence; `scripts/herdr/test_herdr_sessions.py` supplies process replies.

**Binds later work:** Pane fixtures supply `pane process-info` replies; missing replies produce an unknown-evidence line on stderr.

**Gotchas:** Unknown evidence does not refresh the saved `seen` time of a binding; Phase 5c keeps such a binding past the age limit.

### Phase 5c — Unknown pane evidence keeps the saved place past the age limit  · status: done

#### As-built

`merged_order(saved, live, now, *, expire=True)` and `merged_layout(..., *, expire=True)` drop a saved entry unseen for longer than `LAYOUT_KEEP_SECONDS` only when `expire` is true.
`Herdr.record_layout` passes `expire=known`, so a save with unknown pane evidence keeps every saved workspace and tab binding in its place.
A save with every pane known keeps the age rule.

**Files:**
- `scripts/herdr/herdr.py` — the `expire` keyword and its caller.
- `scripts/herdr/test_herdr.py` — unknown evidence keeps old bindings; known evidence expires them.

**Binds later work:** A change to how `record_layout` decides `known` passes the same value to `expire`.

### Phase 4l — One live, stopped or unknown reader for restore, restart and stop  · status: moved: shutdown-unit (sdlc, 2026-11-04)

**Depends on:** 4h (its `_session_without_foreground_agent` in `unit_move.py` is the model) and 4g (the user put rename first; sdlc ruling 2026-11-02 07:03). No ETA yet.

#### Work Order

**Goal:** share one conversation reader across folder moves, restore, restart, and stop.

**Spec:** build the one reader in the Reader contract below, for Claude and Codex. Switch each caller in the constraints list to it.

**Constraints from prior phases:** Phase 4h: `herdr.native_process` is the one process identity reader; `running_codex_threads` reads through it; staff `current_session` is the one fact. sdlc ruling 2026-11-04: staff's `current_session` is the identity. The environment thread is only what was launched and never proof. A mismatch prints one anomaly line and the operation goes on. Test `_pane_sessions`, transfer, `retitle` and the target check against this rule. `_live_session_ids` and `_session_without_foreground_agent` no longer exist; the restart consumer is `restart.start_row`.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Found by Astra in the 4h round 7 review, present at HEAD 3f960361f, not caused by 4h:
  1. `scripts/shutdown/pane_restore.py:83` builds its live set from `sessions.live_sessions()` and `running_sessions()`. These drop unreadable Claude records, so unknown reads as stopped and an idle retained pane can resume a conversation that runs elsewhere.
  2. `scripts/shutdown/restart.py:2148` `_live_session_ids()` has the same gap on an ordinary restart launch.
  3. `scripts/shutdown/stop_resume.py:146,225` returns `NotRunning` from Claude evidence only and accepts a Codex identity.
- Build one reader that returns live, stopped or unknown for one conversation, for Claude and Codex. Each caller above and `unit_move.py` switches to it. herdr-unit owns these callers for this phase (sdlc ruling).
- Only proof that the target conversation itself is live refuses (user, 2026-11-02: "your system should only work with what it knows about and report anomalies"). Unknown evidence prints one note that names the process (herdr tab name, then session name, then pid) and continues, for Claude and Codex targets. Use the 4h note in `herdr.running_codex_threads`.
- Moved from 4h (hard landing, user 2026-11-02): `running_codex_threads` uses `staff_sources.codex_thread_host`, which excludes threads that a Codex app-server hosts, so such a live thread is not seen (Astra, 4h round 8).

**Acceptance gate:** a target proven live refuses restore and restart; unreadable evidence prints the named note and does not refuse; an ordinary restart keeps the saved-process check; an app-server hosted live thread is seen; `shutdown stop` on a live Codex identity refuses or stops it, never reports success while it runs; a stopped Claude and a stopped Codex still restore and restart; moved from 4h, live on the host: a Codex unit folder move resumes the same thread in the same pane and terminal, a failed old-tab close then a rerun closes the extra tab, and a `herdr-up` restart rerun after a move keeps the pid, pane and session; the 11-module suite and the full gate are green.

**Reader contract:** return distinct `LiveConversation`, `StoppedConversation`, and `UnknownConversation(reason)` outcomes for the target conversation.
Unknown evidence never becomes `StoppedFolderSession` or proves absence.
Reuse `herdr.native_process` for process identity and `codex_mesh.loaded_threads(run, deadline)` for app-server threads.
Include `unit_move.py` as a caller beside restore, restart, and stop.
Callers print the named anomaly for unknown evidence and continue from known facts; they do not convert unknown into stopped proof.
A missing terminal, a duplicate pane or an ownership conflict is not a refusal: the action goes through `check_target`, the unproved target is left alone with one anomaly line, and known work continues (review, 2026-11-04).
**Evidence variants (sdlc, 2026-11-04):** `NativeAgentProcess.identity` (`herdr.py:2941`, a bare `str`) becomes a typed evidence union: `RolloutThread(id)` (an open rollout), `EnvironmentThread(id)` (the process environment) and `LaunchArgument(id)` (only the resume argument). Only the first two prove the current conversation; `LaunchArgument` identifies the launch thread for recovery. Test launch identity separately from verified current identity.

**Files:**
- `scripts/herdr/herdr.py` — the shared conversation reader.
- `scripts/production/unit_move.py`, `scripts/shutdown/pane_restore.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/stop_resume.py` — callers.
- `scripts/agents/codex_mesh.py` — read only: `loaded_threads`.
- `scripts/herdr/test_herdr.py`, `scripts/production/test_unit_move.py`, `scripts/shutdown/test_pane_restore.py` — tests.
- `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_stop_resume.py` — tests.

**Workers:** `1 writer + 1 tester`
- `impl` — the shared reader and callers; hub: `scripts/herdr/herdr.py`.
- `test` — their tests and fixtures.

**Acceptance gate (anomaly surface):** test named stderr notes for restore, restart, stop, and folder moves.
Test tab-name, session-name, and pid fallback, including failed name reads.
Unknown evidence continues; a target proved live and structural faults still refuse.

**Claude live proof safety:** use an owned throwaway probe and a private `STAFF_FILE` copy.
Answer trust only for the exact probe source or destination under `/home/natepiano/scratch/herdrcheck/`; refuse every other path.
Confirm the selected Yes option before Enter; verify prompt disappearance and record any repeated prompt.
Before the rerun snapshot, require the resumed pid, its process that runs `current_session` in the destination, and a confirmed terminal; a Claude launch has no launch record (review, 2026-11-04).
Require two equal reads one second apart; save both snapshots and named byte/time differences with text diffs.
Compare operation-owned staff fields and check for probe ids or scratch paths in live staff.
Record live hashes; an unrelated hash change alone does not prove a probe write.

### Phase 5 — Rename agents.conf to models.conf and /agent to /default_models  · status: done

#### As-built

- The model registry is `config/models.conf`; `MODELS_CONFIG_FILE` overrides its path. `scripts/lib/models_registry.py` is the one Python parser and path authority (`registry_path()`, `RegistryRow`); the Python callers (`progress_history.py`, `context_usage.py`, `codex_pacer.py`, `quota_alert.py`) call it.
- Bash reads the registry through `scripts/agents/models_config.sh`, which takes shell assignments from `models_registry.py shell`. The command is `/default_models` (`commands/default_models.md`, `scripts/agents/default_models.sh`).
- `scripts/agents/test_no_old_model_names.py` fails on a live reference to `agents.conf`, `AGENTS_CONFIG_FILE`, `agents_config.sh`, `agents_registry` or the `/agent` command. It matches whole names only and exempts `docs/plans/`, `docs/as-built/director-model-study-results.json` and the two files in `RETIRED_FILTER_HISTORY`.
- `scripts/settings/git_filters_table.sh` keeps the `claude-agents-conf:config/agents.conf` entry, so an older checkout that indexes the old path still retires its filter.
- From 2026-11-03 models-unit owns `models_registry.py`, `models_config.sh`, `default_models.sh`, `sync_codex_catalog.sh` and `config/models.conf`.

**Files:**
- `config/models.conf` — the registry.
- `scripts/lib/models_registry.py`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py` — the parser, its tests and the shared fixture.
- `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh` — the bash reader and the `/default_models` command script.
- `scripts/agents/test_no_old_model_names.py` — the old-name check.
- `scripts/agents/test_models_conf_publish.py` — the publish tests.

**Binds later work:** `AGENTS_CALLER_FAMILY` and `AGENTS_CALLER_ASSIGNMENT` keep their names. Bash still splits raw rows and picks its default path; the bash reader work belongs to models-unit.

**Gotchas:** the NixOS git-filter watch path (`modules/common/claude-git-filters.nix`) ships with the main merge; the repository's old-name test cannot see the installed job.

**Ruled out:** a fallback that reads an old file or variable name.

### Phase 5a — Bash reads resolved registry values from one call  · status: moved
**Moved:** to models-unit (the user, through natedev, 2026-11-03): the models unit under sdlc owns all `/default_models` changes. No new work on it here. No worker started; no branch. Findings F002, F003, F004, F006, F008 and architect findings 1-3 went to models-unit.

**Depends on:** Phase 5

The Phase 5 review found these after the hard landing (repair round 3). None is a regression: before Phase 5, bash parsed the rows itself and picked the path itself. They move here from Phase 5 (findings F002, F003, F004, F006, F008).

#### Work Order

**Goal:** bash parses no registry line and picks no registry path; one `models_registry.py` call per launch gives bash every value it reads.

**Spec:**
1. `models_registry.py shell` reads the file from `registry_path()` itself, not from stdin. `models_config.sh:22` and `sync_codex_catalog.sh:6` stop choosing the default path.
2. `shell_assignments()` prints resolved section, key and value arrays from the parser, not raw row text. `_models_table_get` and `_models_table_has_key` compare keys only. The two awk parsers in `sync_codex_catalog.sh` (`:88`, `:332`) read the same arrays. A row `direct = codex` resolves in bash as it does in Python.
3. One launch starts the parser once: `implement.sh:206` reads `codex_mesh` from the table that `agents_resolve` already loaded.
4. `test_no_old_model_names.py` finds an old name that sentence punctuation follows (`Use /agent.`).
5. `models_registry.py` commit subject and temporary file prefixes use `models`, not `agents`.

**Files:** `scripts/lib/models_registry.py`, `scripts/lib/test_models_registry.py`, `scripts/agents/models_config.sh`, `scripts/agents/test_models_config.sh`, `scripts/agents/sync_codex_catalog.sh`, `scripts/agents/test_sync_codex_catalog.sh`, `scripts/direct/implement.sh`, `scripts/agents/test_no_old_model_names.py`.

**Workers:** `1 writer + 1 tester` — `impl`: the source files; `test`: the test files.

**State:** bash holds resolved registry values only.

**Acceptance gate:** a bash test that `direct = codex` resolves; a test that one launch starts the parser once; a test that `MODELS_CONFIG_FILE` and the default path both reach bash through `registry_path()`; the old-name test finds `Use /agent.`; the changed test files and basedpyright 0/0/0 on changed files and callers; full gate green.

### Phase 6 — herdr primitives for worker panes, and probes  · status: done

**Owner:** shutdown-unit (user approved through sdlc, 2026-11-02; it runs beside the rename).

**Depends on:** existing herdr launch and pane APIs (Phases 3e and 3g). Start at once. No dependency on 3n or 3s-4g.

#### Work Order

**Goal:** `herdr.py` can start an agent in a new split pane or in an existing pane at its shell, set split ratios and read agent states as one typed value, and the probe answers are recorded in this plan.

**Spec:** `launch_prepared_agent` (`herdr.py:3417`) takes `destination: NewTab | SplitPane | ExistingPane` in place of its tab arguments. `NewTab(space, title)` holds today's tab arguments, and every caller (`launch_agent` :3354 and the others) passes one. `SplitPane(of_pane, direction, ratio, label)` takes `direction: Literal["right", "down"]`, refuses a ratio outside (0, 1) when it is made, and labels the new pane before the agent starts, so a launch killed after the split leaves a pane that `start-or-resume` finds by its label and reuses (Phase 7). `ExistingPane(pane, terminal)` starts the agent in a pane that is already open, so a recovery keeps its pane and terminal: it first reads `Herdr.panes()` (:1088) once and a fresh `Herdr.pane_activity(pane)` (:1127), and refuses, with the reason, unless exactly one pane has that `terminal_id`, it is that pane, and the pane reads `PaneAtShell` (:687). All three destinations keep the same environment proof, launch claim, prompt-once proof and cleanup; the cleanup of a failed start in an `ExistingPane` leaves the pane open at its shell. Add `Herdr.split_ratio(pane, ratio)` over the socket's `layout.set_split_ratio`, and `Herdr.label_pane(pane, text)`. Every split uses `--no-focus`. `AgentStatus`, a `StrEnum` of herdr's agent states (`working`, `idle`, `done`, `blocked` and the others herdr reports), replaces `agent_status: str` (:773, :813, :821) and is decoded once where herdr's JSON is read; a state herdr adds later decodes as `AgentStatus.UNKNOWN`, which no caller reads as settled. Probes, in a probe tab of `agents`, written into Reference → Probe results: which side `--ratio` measures; whether a close rebalances the rest; whether a label set by `label_pane` stays after the pane's agent exits and after a cross-workspace move (Phase 7 finds a stopped worker's shell by it); Codex and Claude queue a message typed mid-turn; herdr's Codex state over a 10-minute turn; prompt by path; both CLIs open an image by path from a prompt file; the exact Codex input that names a new session (help shows no `--name` flag; Claude uses `--name`); whether herdr's `resume_agents_on_restore` keeps each pane's `--env` (a positive answer retires our split-tab restore code; a negative one needs questions list item 1(e)); whether interactive Codex picks up a new `auth.json` by itself (a positive answer retires the sign-in restart, Reference job 8).

**Files:**
- `scripts/herdr/herdr.py` — `NewTab | SplitPane | ExistingPane`, `split_ratio`, `label_pane`, `AgentStatus`, and confirmed pane close.
- `scripts/herdr/pane_launch.py` — read only: strict `retire_launch`.
- `scripts/shutdown/restart.py` — the two `launch_prepared_agent` calls (:1180, :2741) pass `NewTab`; the shutdown unit's file.
- `scripts/production/session_pane.py`, `scripts/production/stall_watch.py`, `scripts/message/roster.py` — the `agent_status` callers (`session_pane.py:31-33`, `stall_watch.py:803`, `roster.py:1436`) take `AgentStatus`.
- `scripts/herdr/test_herdr.py` — the launch and decode tests.
- `docs/plans/herdr-worker-panes.md` — Reference → Probe results.

**Workers:** `2 writers` — `impl`: `herdr.py`, `restart.py`, the `agent_status` callers; `test` (opens as impl): the tests and the probes.

**State:** none.

**Constraints from prior phases:** Shutdown's checkpoint (2026-10-30): every resume runs in the session's own tab with `Herdr.run_in_pane` (:1080) through `accounts.py exec <account>`. Ordinary and split restores keep conversation id, account environment, identity, tab label and position, and their tests show it. Phase 3g: herdr 0.9.5 clears a pane's conversation id when its agent exits; `terminal_id` is stable, one per pane; a cross-workspace move changes the pane id; `Herdr.panes()` turns an omitted `terminal_id` into `""`, which is incomplete evidence, never absence; a pane is closed only after a fresh `pane_activity` reads `PaneAtShell` (`rename_unit._stopped_pane`, `rename_unit.py:738`, is the pattern).
- Phase 3h review (2026-11-01): `Herdr.native_session` (`herdr.py:1600`) calls `staff.runtime_id(pid)`, which reads Claude registry records only; add a probe that finds a Codex pane's live thread (its open rollout file, as Phase 3a proves) before Phase 7 uses it. `launch_prepared_agent` is at `herdr.py:3494`. A failed new pane is closed; an `ExistingPane` failure keeps the shell.

**Acceptance gate:** basedpyright 0/0 on `herdr.py` and every caller module; launch tests cover all three destinations, an `ExistingPane` refused when a fresh read finds its terminal on another pane, no terminal, or foreground work, a ratio outside (0, 1) refused when the `SplitPane` is made, the label set before the agent starts, and a failed start closing its pane; `AgentStatus` decodes each state herdr reports, and an unknown one as `UNKNOWN`; probe results written; full gate green. A launch killed after the split is reconciled by `start-or-resume`, which holds the worker's key; its rerun test is in Phase 7.

**Confirmed pane close:** build one shared operation in `herdr.py` that closes one selected pane and confirms its absence.
Use `pane_launch.retire_launch` after confirmed closure, with the source path and exact publication.
Never close the tab or a sibling pane to close this pane.
A failed close keeps its record; a failed record write remains recoverable on rerun.
Use this operation for failed new-pane starts; keep an `ExistingPane` shell open after a failed start.
Phases 7 and 10-12 consume this operation instead of adding close logic.

**Acceptance gate (close contract):** test failed starts, worker closure, restore, and account moves.
Each test preserves the director and sibling panes and leaves one retained replacement launch.
Test failed close and interrupted retirement, then rerun without another launch.

### Phase 6a — A darwin reader behind `startup_processes`  · status: done

#### As-built

- `Herdr.startup_processes` and `_startup_processes` read process facts through one reader from `_startup_process_reader(timeout)`: `_LinuxStartupProcesses` on Linux (`/proc`) and `_DarwinStartupProcesses` on darwin. The result type is the same on both platforms.
- The darwin reader takes one `ps -axo pid=,ppid=,state=,lstart=` snapshot with `LC_ALL=C TZ=UTC` for start times and children. It reads the command with `ps -o command= -p <pid>` for a pid outside `_darwin_processes` (another user's process) and the foreground group with `ps -o tpgid= -p <pid>`. Every `ps` call takes the caller's timeout.
- `_StartupProcessGone` marks a zombie or a pid absent from the snapshot; the walk skips it. Any other read error makes the walk unreadable. An agent entry sets `agent_present` even when its pid is gone or missing.
- The Linux reader walks each thread's `task/<tid>/children` list in numeric thread order and keeps the children read before a later thread list goes missing.
- The reader reads only pid, parent, state, command and start time; it prints no environment values.

**Files:**

- `scripts/herdr/herdr.py` — the two readers, `_StartupProcessGone`, `_darwin_start_time`, `_darwin_processes(complete=, timeout=)`.
- `scripts/herdr/test_herdr.py` — darwin and Linux reader tests.

**Gotchas:** On darwin, `AgentProcessIdentity.start_ticks` holds UTC epoch seconds from `ps lstart`, not clock ticks, so a pid reused in the same second keeps the same token. `ps lstart` is parsed with `%a %b %d %H:%M:%S %Y` and needs `LC_ALL=C`.

**Ruled out:** a `ps` read for each pid in the walk — two reads race with process exit.

### Phase 6b — The herdr suite passes on the Mac  · status: done

#### As-built

- The herdr suite (`scripts/herdr`) passes on the Mac and on natedev: 381 tests, 0 failures, 0 errors. The source is unchanged; each of the ten Mac reds was a test or fixture that assumed Linux.
- `native_process_commands(pid)` in `test_herdr.py` is the one fixture for darwin process facts. It answers `ps -o lstart=,tty= -p <pid>` and `lsof -a -p <pid> -Fn` and patches `sessions.process_start` for that one pid. Any other pid raises `AssertionError`.
- The fresh-identity cleanup test replaces `herdr.os` with a namespace that has only `kill`, so the test takes the signal path without pidfd on every host.
- `fake_herdr.py` reads the fixture process tree from `HERDR_PROC` on every host, so it sees a fixture agent exit on darwin too.
- The staff-marks test in `test_herdr_sessions.py` patches `process_start` for its Codex fixture.

**Files:**
- `scripts/herdr/test_herdr.py` — `native_process_commands`, the darwin-safe cleanup test.
- `scripts/herdr/test_herdr_sessions.py` — the staff-marks fixture start time.
- `scripts/herdr/fake_herdr.py` — the fixture process tree on every host.

**Binds later work:** a change to the darwin process identity read changes `native_process_commands` in the same place and keeps its one-pid guard.

**Gotchas:** run the Mac suite from `scripts/herdr` on a copy of `scripts` and `config`, with `PYTHONPATH` at the copy root and `HERDR_BIN=/nonexistent/herdr`. Judge a Mac run only by its `Ran`/`OK`/`FAILED` lines; a relayed exit code can read 0 for a failed run.

**Ruled out:** a regression test for the fixture's own pid guard — it would test a mock it configures.

### Phase 6c — The startup reader keeps one deadline and a strong Mac process generation  · status: done

#### As-built

- `start_command` passes one launch deadline, a `_ProcessReadDeadline`, to `_startup_processes`. Each darwin `ps` call gets only the time that remains, and a read the deadline stops makes the walk unreadable, never gone. Callers outside startup pass a float and keep their per-call timeout; for them `_darwin_processes` skips a pid whose read times out.
- On darwin, `AgentProcessIdentity.start_ticks: int | ProcessGenerationUnavailable` holds the libproc start time in microseconds, read with `proc_pidinfo(PROC_PIDTBSDINFO)` through `ctypes` into `_DarwinProcessBSDInfo`. A failed read is `ProcessGenerationUnavailable`. Linux keeps its start ticks.
- Cleanup sends no signal on weak generation evidence: `_require_startup_generations` raises `failed agent generation is unavailable; pane left open`, and the pane stays.
- A pid that reads a different strong generation leaves the tracked set and must prove its agent command again before it is signalled.
- `_track_startup_identity` keeps the strong identity when a later read of the same pid is weak, so a reparented agent stays tracked.

**Files:**
- `scripts/herdr/herdr.py` — the startup reader, the darwin libproc identity and the cleanup guard.
- `scripts/herdr/test_herdr.py` — deadline, pid-reuse, weak-read, reparent and fallback-timeout tests; `native_process_commands` answers the libproc read for its one pid.

**Binds later work:** a failed start with weak generation evidence keeps its pane, maybe with the agent still running; a rerun reaches it only through `ExistingPane`, which refuses unless the pane reads `PaneAtShell`. Compare darwin identities only by equality.

**Gotchas:** `proc_bsdinfo` is 136 bytes on the Mac: `pbi_pid` at offset 12, `pbi_start_tvsec` at 120, `pbi_start_tvusec` at 128. Only a `_ProcessReadDeadline` caller re-raises a per-pid `TimeoutExpired`. On darwin, `start_ticks` holds microseconds, not ticks.

**Ruled out:** `ps lstart` seconds as the darwin generation — a pid reused in the same second reads the same.

### Phase 7 — worker_panes.py: start-or-resume, close, observe  · status: done

#### As-built

- `observe` uses one supplied `StaffSnapshot` and one native pane scan across workspaces; its five states distinguish live, stopped, absent, unknown, and structural evidence.
- `start_or_resume` serializes admission by parent and name, publishes after native identity proof, and resumes the same conversation in its retained pane.
- Explicit roles select registry settings and sandbox policy; matching policies retain the agent, while policy changes require `can_close` and preserve the terminal.
- `close` preserves busy panes, stops a proved agent through `restart.stop_row`, and confirms pane closure after fresh terminal and shell checks.
- The first split opens right of the parent; later splits open below the lowest matching pane, using `WORKER_SPLIT_RATIO=0.667` within `0.1..0.9`.
- `_layout` uses per-tab locks and explicit tree paths to give matching panes equal shares; foreign panes retain their layout.

```python
WorkerPane = OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker
WorkerStart = OpenWorker | IncompleteStart | AdmissionRefused | UnpublishedWorker | ProcessUnknownWorker | UncertainWorker | StartRefused

def start_or_resume(parent: StaffId, name: WorkerName, role: WorkerRole | WorkerRoleUnavailable, *,
                    account: ConfiguredWorkerAccount | DestinationWorkerAccount = ConfiguredWorkerAccount.CURRENT) -> WorkerStart
def close(parent: StaffId, name: WorkerName) -> Closed | Kept | UncertainWorker
def can_close(worker: OpenWorker | StoppedWorker) -> Closed | Kept
def observe(parent: StaffId, record: staff.StaffSnapshot) -> list[WorkerPane]
def Herdr.scan_row_panes(self, record: staff.StaffSnapshot, panes: list[dict[str, object]]) -> RowPaneScan
def Herdr.save_layout(self, *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE,
                      scan: RowPaneScan | ScanAtUse = _SCAN_AT_USE) -> None
def Herdr.record_layout(self, *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE,
                        scan: RowPaneScan | ScanAtUse = _SCAN_AT_USE) -> None
def launch_settings(section: str, key: str) -> LaunchSettings
```

| Type | Fields or values |
| --- | --- |
| `OpenWorker` | `row: staff.StaffRow`, `pane: str`, `terminal: str`, `status: herdr.AgentStatus` |
| `StoppedWorker` | `row: staff.StaffRow`, `pane: str`, `terminal: str` |
| `ClosedWorker` | `row: staff.StaffRow` |
| `UncertainWorker` | `row: staff.StaffRow`, `reason: str` |
| `ProcessUnknownWorker` | `row: staff.StaffRow`, `pane: str`, `terminal: str`, `reason: str` |
| `AdmissionRefused` | `reason: str` |
| `IncompleteStart` | `row: staff.StaffRow`, `pane: str`, `reason: str` |
| `UnpublishedWorker` | `pane: str`, `cause: str` |
| `StartRefused` | `reason: StartRefusal`, `detail: str` |
| `StartRefusal` | `UNKNOWN_ROLE`, `ROLE_UNAVAILABLE`, `INVALID_ROLE_CONFIGURATION`, `BUSY_ROLE_CHANGE`, `ACCOUNT_UNAVAILABLE`, `PANE_NOT_AT_SHELL` |
| `Closed`, `Kept` | No fields; `blocker: str`, respectively |
| `WorkerRoleUnavailable` | `reason: str` |
| `ConfiguredWorkerAccount`, `DestinationWorkerAccount` | `CURRENT`; `label: str`, respectively |
| `LaunchPolicy` | `settings: models_registry.LaunchSettings`, `sandbox: str`, `permission: str` |
| `LaunchSettings` | `kind: Literal["claude", "codex"]`, `model: str`, `effort: str`, `service_tier: str` |
| `UnknownPane` | `pane: str`, `tab: str`, `reason: str` |
| `RowPaneScan` | `found: dict[staff.StaffId, list[dict[str, object]]]`, `unknown: list[UnknownPane]` |
| `ScanAtUse` | No fields; the layout operation reads current pane evidence |
| `ExitCode` | `OK=0`, `REFUSED=2`, `KEPT=7`, `UNCERTAIN=8` |

**Files:**

- `scripts/herdr/worker_panes.py` — lifecycle API, observation states, account selection, and `start-or-resume <name> --role <role> [--account <label>]`, `close <name>`, `list [--json]` commands.
- `scripts/herdr/herdr.py` — `scan_row_panes`, retained `UnknownPane` evidence, and shared scan consumers for layout, titles, and tab moves.
- `scripts/lib/models_registry.py` — typed `launch_settings` through the existing shell resolver.
- `scripts/astra/design_roles.py` — shared `DESIGNER_SANDBOX` for launch and `access_policy`.
- `config/direct.conf` — split ratio and its range annotation.
- `scripts/herdr/test_worker_panes.py` — lifecycle, identity, account, configuration, and split-tree tests.
- `scripts/herdr/test_herdr.py`, `scripts/lib/test_models_registry.py` — scan consumers and registry tests.
- `docs/as-built/staff-accessors.md` — the accessor entry for `worker_panes.py`.

**Binds later work:**

- `Dispatch and the launcher switch, landed dark` consumes observation states and all seven `WorkerStart` variants; `ExitCode.OK` prints `done`.
- Its dispatch contract requires lock, prompt-free start and publication, dispatch registration with staff id and role, transcript read, then input.
- Its role comparison requires `progress_history.last_dispatch(worker_id)` for the last completed dispatch; the shipped by-name recorder reader still requires replacement.
- Its lock contract requires one helper keyed by fixed staff id, held through dispatch; the holder supplies its hold to `start_or_resume`.
- Its close contract requires separate `CloseAllowed` and `Closed` states; `Kept` prints its blocker, exits 7, and retains row, pane, and conversation.
- `StartRefused` exits 2 without input or row deletion; incomplete starts, failed publication, and unknown evidence permit no unproved delivery or duplicate backend.
- The source delta is +783/-28. This phase joins the source group that ends at `Cleanup`; the group must net delete by then. Existing launchers retain their transport.

**Gotchas:**

- Result dataclasses are frozen; each union preserves the meaning of missing evidence without a bare optional result.
- Direct roles use `[direct.<kind>]`; astra roles use `[design_roles.<kind>]`, with read-only policies for `review` and `astra-reviewer`.
- `astra-designer` uses `DESIGNER_SANDBOX`; `impl`, `test`, and `fix` use `WORKER_SANDBOX`; the registry supplies no sandbox.
- Split configuration uses the shared reader and committed-value recovery; unusable effective values refuse before any split.
- `CODEX_THREAD_ID` alone cannot prove the current conversation after `/new`; process generations compare only by equality.

- Native process evidence proves ownership. So does `agent_session` with `codex` as the one foreground process (Phase 14a). Labels, titles, and inherited environment values do not prove ownership.
- Labels recover stopped panes only; unknown evidence never proves absence or permits a replacement split.
- Destructive operations require the observed pane id and `current_session` target; display names truncate at 32 characters.
- Missing terminals, duplicate identities, and ownership conflicts retain panes; proved unrelated operations remain usable.
- The shared scan emits each unknown-pane diagnostic once; layout completeness requires `not scan.unknown`.
- Live closure requires a free dispatch lock, no live detached run, idle/done status, recorded completion, and a final assistant `DONE <dispatch-id>` line.
- Stopped closure still requires free dispatch locks and no live detached run; only confirmed closure retires an applicable Codex compatibility record.
- The launcher holds `<run>/phases/<phase>/dispatch.lock`; the shipped checks also inspect `<run>/dispatch.lock`.
- `restart.stop_row` currently receives no deadline; policy changes and closure require `deadline=` before herdr becomes the default transport.
- `layout.set_split_ratio` requires the tree path; herdr's `split_ratio` command changes the nearest split.
- Same-role reuse preserves a manually selected model; explicit destination accounts use existing readiness checks and the same native conversation.
- `UNIT_TEAM_ROLE` contains the slot name; staff rows contain no role, pane, or terminal, and launch environments exclude `STAFF_ID`.
- `PaneIdentity.session_id` and `TabSession.session_id` retain `str | None` for existing shutdown consumers.
- `worker_panes.py` still imports `mesh_runs` and sandbox constants from `codex_mesh`; their later removal includes these imports and their tests.

**Ruled out:** `PaneConversation` and `NoPaneConversation`; `Herdr.split_ratio`; an alias for `Herdr._row_panes`; a second role or launch-policy store.

### Phase 8 — Dispatch and the launcher switch, landed dark  · status: done

#### As-built

- `worker.py` provides `list`, `add`, `end`, `restore`, `check`, and `assign`; the caller's native identity selects its unit or showrunner parent.
- `assign` matches a retained dispatch before selection, reuses idle or resumable slots in role-number order, and refuses all-busy selection with names and exit 2.
- Initial assignment attempts the default `impl`, `fix`, `review`, and `test` team; Astra roles start on demand, and `add` uses the lowest free name.
- Dispatch holds the fixed-id lock through prompt-free startup, publication, recorder registration, transcript inspection, input, and completion; the transcript alone records submission and completion.
- `implement.sh`, `review.sh`, and `assign.py` use the shared command for herdr-pinned runs; `WORKER_TRANSPORT=mesh` remains the default.
- `restore` retains running conversations and reports `unchanged` for a completed rerun; `end` retires a row only after `Closed`.

```python
DispatchReceipt = DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown
# DispatchComplete(reply: str); DispatchEvidenceUnknown(reason: str); other receipt variants have no fields.
def dispatch(parent: StaffId, name: WorkerName, role: WorkerRole, prompt: Path, dispatch_id: str = "", *,
             session_dir: Path, slot: str, phase: str, kind: str, launcher_command: list[str],
             lens: str = "", reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, fix_cause: str = "",
             fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW) -> ExitCode
def dispatch_receipt(row: staff.StaffRow, line: str, dispatch_id: str) -> DispatchReceipt
def dispatch_worker(session_dir: Path, *, slot: str, phase: str, role: staff.WorkerRole, prompt: Path, launcher_command: list[str]) -> MatchedDispatch | NoMatchedDispatch
def begin_dispatch(session_dir: Path, *, slot: str, phase: str, role: staff.WorkerRole, worker_name: str, launcher_command: list[str], prompt: Path, kind: str, lens: str, fix_cause: str = "", fix_pass: int = 0, activity: str = "") -> str
def worker_lock(parent: StaffId, name: WorkerName, *, blocking: bool = False) -> Generator[WorkerLockHold | Kept]
def start_or_resume(parent: StaffId, name: WorkerName, role: WorkerRole | WorkerRoleUnavailable, *, account: ConfiguredWorkerAccount | DestinationWorkerAccount = ConfiguredWorkerAccount.CURRENT, lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> WorkerStart
def can_close(worker: OpenWorker | StoppedWorker, *, lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> CloseAllowed | Kept
def close(parent: StaffId, name: WorkerName, *, lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> Closed | Kept | UncertainWorker
def stage_two_completion_id(dispatch_id: str) -> str
def worker_transport(run: Path) -> Literal["mesh", "herdr"]
```

**Files:**

- `scripts/production/worker.py`, `scripts/production/test_worker.py` — command API, selection, dispatch, typed receipts, and behavior tests.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — shared locks, role access, close permission, completion ids, and tests.
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — dispatch matching, durable recorder entries, launcher arguments, six roles, and tests.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py`, `config/direct.conf` — locked transport pin, default, and tests.
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/assign.py`, and their launcher tests — transport branches and success-only completion reporting.
- `scripts/direct/remove_workers.py`, `scripts/direct/test_remove_workers.py`, `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py` — per-run removal, held Astra routing, and tests.
- `commands/unit/worker.md`, `commands/showrunner/worker.md`, `commands/unit/direct.md`, `docs/as-built/staff-accessors.md` — command contracts, transport instructions, and accessor entries.

**Binds later work:**

- `Messages, detached runs and review Stage 2` retains `NO_DONE` for stopped or closed states after submission and `UNCERTAIN` for unknown evidence.
- Its Stage 2 uses `stage_two_completion_id(instance_id)`, which appends `-2`; readiness events gate the second input, and both stages use transcript receipts.
- `MatchedDispatch(worker_name: str, instance_id: str)` and fieldless `NoMatchedDispatch` preserve unnamed retries before selection; matching active or completed recorder entries retain their id.
- `launcher_command` stores an argument list before input; replies, recorder state, and the transport pin use `atomic_file.write_text_atomic`.
- The source delta is +1088/-126; the combined source balance is +1717, with a net deletion requirement at the transport drain.

**Gotchas:**

- `ExitCode` defines `OK=0`, `REFUSED=2`, `NO_DONE=3`, `BLOCKED=4`, `STALLED=5`, `BUSY=6`, `KEPT=7`, and `UNCERTAIN=8`; only matching final `DONE <dispatch-id>` replies complete dispatch.
- Only `OpenWorker` permits input; `StartRefused` and `AdmissionRefused` return 2, while incomplete startup, failed publication, and unknown evidence return 8 without input.
- Incomplete transcripts without submission yield `DispatchEvidenceUnknown`; submitted turns receive no second prompt, including after stalls or launcher loss.
- `WorkerLockHold.bind` acquires the fixed-id lock before creation serialization ends; startup and close accept the holder's lock without rejecting that holder.
- Fieldless `CloseAllowed` permits closure; fieldless `Closed` proves completion. `Kept` prints its blocker, exits 7, and retains the row, pane, and conversation.
- Default-team failures print `anomaly: <role>: <detail>` and continue; only the selected slot requires proved readiness. Input uses the observed pane and `check_target`.
- `dispatch_role` still uses `_passes` and `worker_pass` by name; `last_dispatch` and fixed `worker_id` registration remain unavailable. Staff rows contain no role.
- `restart.stop_row` still receives no deadline; `Pilot and default switch` requires deadline integration before herdr becomes the default.
- `remove_workers.run_workers` limits removal to run membership; herdr-pinned members avoid mesh removal. Astra `_pane_binding` returns the binding, and recovery checks only the selected slot.
- `worker_transport` retains `<run>/worker_transport` under `worker_transport.lock`; mesh branches remain available, and simplify retains its existing launch path.
- `list` separates unavailable roles from observation states; `check` reports wrong rows and exits 0 or 1. Unproved rows retain their panes while unrelated proved operations continue.

**Ruled out:** additional checks absent from an explicit requirement; refusal of herdr `--model` or `--effort`; a second `last_dispatch` reader; a separate submission store.

### Phase 9 — Messages, detached runs and review Stage 2  · status: done

#### As-built

- `send.py` resolves `worker:<unit>/<short name>` and `worker:<showrunner>/<unit>/<short name>` through one `staff.snapshot()`; an address with no worker row returns `REFUSED`, exit 2.
- An `OpenWorker` receives text through `Herdr.prompt` with the observed pane and current-session target; other observation variants route the text and one reason line to the holder, and `ProcessUnknownWorker` also prints one anomaly line.
- `mesh_runs.deliver` finds the worker from its native session and the current staff snapshot, reads the transcript for the run's result line, and types nothing when that line exists. A per-run lock serializes delivery.
- A mesh-pinned run keeps its `MeshCaller` and `codex_mesh.hold_message` path; a mesh-pinned `send.py` address keeps `MeshWorker`.
- Review Stage 2 runs through `worker.py` from `assign.py` with the id `<dispatch id>-2`, and the launcher waits for that `DONE`. The mesh Stage 2 branch stays under the pin.
- `refusal_watch.transcript_refusal` reads Claude and Codex transcripts first and takes `screen_text` only when transcript evidence is unavailable. No caller outside `refusal_watch.py` exists.
- `mesh_runs.py supervise` starts each command in the folder that started the run.

**Files:**

- `scripts/message/send.py`, `scripts/message/test_send.py` — `worker:` addresses and pane or holder delivery.
- `scripts/agents/mesh_runs.py`, `scripts/agents/test_mesh_runs.py` — transcript-first result delivery and the start folder.
- `scripts/agents/mesh_run_process.py` — standard-library `process_args` and `supervises`; it recognizes a supervisor by the literal name `mesh_runs.py`.
- `scripts/direct/test_assign_launcher.py` — Stage 2 behavior through `dispatch`.
- `scripts/accounts/refusal_watch.py`, `scripts/accounts/test_refusal_watch.py` — the one classifier.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — `worker_run` and close after a detached run.
- `scripts/lib/unit_runs.py`, `scripts/direct/workers.py` — transport imports inside `worker_transport`; run membership.

**Binds later work:**

- `result_text` and `deliver` take `int | None`; `None` means that the supervisor disappeared without an exit result.
- `Delivery after an interrupted pane input` deletes `pane_result_typed`: without a transcript line, a rerun types the result again.
- `assign.py` carries `require_live_director`, held operations (`PRIME`, `FOLLOW`, `RESIDENT`) and recovery operations (`ADOPT`, `RESUME_ACCOUNT_MOVE`). `assign_gate.called_by_assign` walks process parents through `LAUNCHERS` and `PYTHON_HOPS`.
- `dispatch_role` reads by worker name until `progress_history.last_dispatch` reaches the merge branch.

**Gotchas:**

- `collect.reader_payload` ships `unit_runs.py` alone to the Mac. Its top-level imports stay standard-library.
- A file-list basedpyright run does not find import cycles; only the gate's scope finds them. `mesh_runs` and `worker_panes` import through `mesh_run_process.py` for this reason.
- Running several test modules in one process fails `test_add_showrunner`; the gate runs each module alone.
- `execute_simplify` replaces `assign.py` with `review.sh`, so `assign_gate` refuses that route until the owner's fix lands.

**Ruled out:** a stored mark as the delivery rule (a worker that sees one result twice loses nothing); a second classifier; a check that the Work Order does not name.

### Phase 9a — Delivery after an interrupted pane input  · status: done

#### As-built

- `mesh_runs` delivery checks the transcript for the run's result line. With no line, a rerun types the result again and finishes the run. `pane_result_typed` is gone.
- `Herdr.codex_client_session(pane)` reads the current thread from the staff row named by the client's tab. It is the one lookup for a bare Codex daemon-client pane.
- `Herdr._codex_launch` reads that method when the pane has no agent session, and takes the process start and environment from the process. A pane with an agent session keeps the process-evidence path.
- `pane_record.seed` calls the method, so `pane_record.py` no longer reads staff for this and `herdr.py` does not import it.
- Source: +19/-25.

**Files:**

- `scripts/agents/mesh_runs.py`, `scripts/agents/test_mesh_runs.py` — the mark is gone; three delivery tests state the retype rule.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — the method and `_codex_launch`; `test_bare_codex_launch_uses_the_tabs_current_staff_thread`.
- `scripts/shutdown/pane_record.py`, `scripts/shutdown/test_pane_record.py` — `seed` calls the method.
- `scripts/shutdown/test_pane_restore.py` — the Codex publication fixture answers the pane list.
- `docs/as-built/herdr-restore.md`, `docs/as-built/staff-accessors.md` — state the current lookup and delivery rule.

**Binds later work:**

- `Herdr.codex_launch` now reads `panes()`. A fixture that patches `result` must answer `pane list` as well as `pane process-info`.
- A worker can see one result twice after a stop between the pane input and the transcript line. That is the sdlc ruling.

**Gotchas:**

- Only the merge gate found the fixture fault; the file-scoped runs did not include `test_pane_restore`.

**Ruled out:** a saved mark or a check (sdlc: "a worker that sees one result twice loses nothing"); a second lookup for a bare pane.

### Phase 10 — Shutdown and restore of split tabs (shutdown unit)  · status: moved: shutdown-unit (code on sdlc; the real proof remains)

**Depends on:** Phase 3n, Phase 4, Phase 6, Phase 7, Phase 8, Phase 9. Shared gate with Phases 10/12/13 over the exact final batch.

#### Work Order

**Goal:** a shutdown stops worker panes inside the configured bound and keeps each pane, and a restart brings each director tab back with its workers resumed.

**Status note (herdr-unit, 2026-11-06):** the code is on sdlc: `58eb12b03` stops retained worker panes within the shared shutdown deadline, and `482724c69` restarts stopped split tabs in their retained panes. The real proof stays with shutdown-unit: `1 writer + 1 tester`. `impl` runs the installed stop, down and restart proof and repairs what it finds. `test` proves an interrupted rerun, with an unchanged pane, terminal, identity and retained row. Do not dispatch the implementation below again.

**Spec:**

1. **Shutdown.** Take one `staff.snapshot()` for the whole shutdown.
   Observe workers through Phase 7's shared reader.
   List each live worker row id once in the shutdown record before the voluntary window.
   Do not copy names, pane ids, terminals, process identities, scope, run, dispatch ids, or launcher commands.
   Read names and parents from staff; recover runtime locations from fresh process evidence.
   Read busy-pass identity and launcher arguments from the existing progress pass, as Phase 8 specifies.
   Omit `ClosedWorker` from the live list.
   Retain unknown and uncertain worker ids with their reasons; report them as not proved stopped.
   Stop each proved worker through `restart.stop_row` and `stop_resume.stop`.
   Keep its pane at the shell and retain its conversation.
   Include worker stops within the director's bounded shutdown attempt.
   Pass `deadline=<monotonic>` through shutdown-unit's integration, commit `b76d3b028`.
   Fresh observation must prove `PaneAtShell` on the same terminal and no live current-session process before stop success.
   Read configuration through `conf.read("shutdown")` at each stop or resume.
   Move `STOP_GRACE_SECONDS=2` and `KILL_GRACE_SECONDS=5` from `stop_resume.py` into `config/shutdown.conf`.
   Update `side_pane.close` to read `STOP_GRACE_SECONDS` through `conf.read("shutdown")`; convert its tests to private configuration.
   The file also holds `MOVE_STOP_MARGIN_SECONDS=3`, `START_SECONDS=20`, and `STAY_SECONDS=10`.
   Read stop values through `conf.read("shutdown").text(...)` and `float` because they carry `# whole:` limits and tests use fractional values.
   Read start and stay durations through `.number(...)` with their `# range:` limits.
   Check effective positive values before any signal or start.
   Use `CONF_DIR` for private test configuration.
   Preserve committed-value recovery and its warning; report `conf.Unusable` before acting when recovery cannot supply a usable value.
   Add no reader or code default.
   Limit each wait to the smaller configured time and remaining operation deadline.
   Stop one director's workers concurrently. After the reads before the signal, the batch takes one stop grace plus one kill grace and the final observation. The reads do not use the grace limits.
   On rerun, stop only agents still alive and keep one record entry per worker.
2. **Restore.** Use Phase 6's environment-preservation probe to choose the existing restore path.
   If herdr preserves each pane's environment, reuse its restored panes.
   Otherwise restore the director and worker splits within the existing locked tab transaction.
   Read the director's name from its row.
   Restore workers through `worker.py restore`.
   Read each role through `progress_history.last_dispatch(worker_id)` with the worker's fixed staff id.
   Pass that role explicitly to every selection and start call.
   Return `WorkerRoleUnavailable(reason)` for a missing dispatch role without creating another worker.
   Reuse a `StoppedWorker` through `ExistingPane`; create a split only for a proved `ClosedWorker`.
   Record only worker ids and their column order here; keep busy-pass facts in the existing progress pass.
   Member stop and resume use shutdown-unit's operations and Phase 4l's conversation reader.
3. **Launcher recovery.** Read each busy worker's active pass from `progress_history_state.json`.
   Use its `instance_id` as the dispatch id and saved `launcher_command` as the argument list.
   Read the role through `progress_history.last_dispatch(worker_id)` with the worker's fixed staff id.
   Run the saved command once with that same pass, including when the original launcher is absent.
   Phase 8 reads the transcript before submission.
   It finishes a completed pass, continues a live pass, or types once when the line was never submitted.
   Include that dispatch id in the busy worker's resume text and final `DONE` line.
4. Shutdown, moves, and launchers use the one builder, `resume_message` in `scripts/lib/resume_message.py`. `PaneWorkerResume`, `BusyPass` and `AccountMove` already exist there, and account moves already use them. Connect shutdown and restore to these types, and do not make new ones. Keep the existing `PausedSeat` branch for mesh-pinned runs through Phase 19.
   Add no second resume-text builder.
   Reuse `Herdr.sort_tabs` for multi-pane tab order.
5. **Resumable worker stop.** `stop_resume.stop_unit_workers(unit: StaffId, record: staff.StaffSnapshot) -> list[str]` uses the shared per-member stop, proves each agent stopped or reports its uncertainty, and keeps every worker row and native backend identity. A stopped pane can stay at its shell; a pane may close only after fresh terminal and shell proof. Retire, wrap and promote call this resumable operation, so unit resume or the surviving holder can call `worker.py restore` on the same sessions. They never call worker end or `staff.retire` on these workers. A proved live target or structural fault blocks destructive worktree removal; unknown process evidence prints the named note.
6. **Permanent ending.** Only `worker.py end` and permanent unit removal stop and delete worker rows. They share the end operation, preserve history outside staff and never delete a proved live worker or bypass a structural fault. A rerun finishes remaining workers once. Restore callers (`restart.py` `start_row` and `restart`, which serve agents up/restart and shutdown restart, and `resume_unit.py`) call `worker.restore(parent)` after proving the parent session. A finished restore prints `unchanged`, exits 0 and keeps ids, panes and terminals.

7. **Delete replaced paths.** Delete superseded pane-worker inventory, stop, restore, and resume-message code from the changed shutdown and lifecycle files.
   Route pane workers through the shared observation, stop, restore, and resume-message APIs.
   Retain `inventory.codex_seats`, `CodexServer`, `PausedSeat`, mesh pause, and their callers only for mesh-pinned runs until Phase 19.
   Only these retained branches join the group ending at Phase 19.

**Files:**

- `scripts/shutdown/inventory.py`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/shutdown_record.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/pane_restore.py` — stop, record, restart and restore; delete their superseded pane-worker paths; `stop_unit_workers` (new) and the config read of `STOP_GRACE_SECONDS`, `KILL_GRACE_SECONDS`, `START_SECONDS` and `STAY_SECONDS` in `stop_resume.py`.
- `scripts/shutdown/session_types.py` — retain the mesh-pinned server and paused-worker types until Phase 19, and the status types that `inventory.py` uses.
- `scripts/lib/resume_message.py`, `scripts/lib/test_relaunch.py` — the shared builder, pane workers, and complete account-move values; delete superseded pane resume-text branches.
- `config/shutdown.conf` — holds `STOP_GRACE_SECONDS=2`, `KILL_GRACE_SECONDS=5`, `START_SECONDS=20`, `STAY_SECONDS=10`, with matching `# range:` limits. Keep `MOVE_STOP_MARGIN_SECONDS`: `scripts/accounts/pane_moves.py` requires it.
- `scripts/production/retire_unit.py`, `scripts/production/production_lifecycle.py`, `scripts/production/promote_showrunner.py` — resumable worker stop; delete replaced pane lifecycle branches.
- `scripts/production/resume_unit.py`, `scripts/production/worker.py`, `scripts/production/test_resume_unit.py`, `scripts/production/test_worker.py` — shared restore and permanent-end integration.
- `scripts/production/test_retire_unit.py`, `scripts/production/test_production_lifecycle.py`, `scripts/production/test_promote_showrunner.py` — their tests.
- `scripts/direct/progress_history.py`, `scripts/direct/recorder_state.py` — consume the last dispatch role, active pass identity, and saved launcher arguments; this phase leaves their writer unchanged.
- `scripts/lib/unit_runs.py`, `scripts/lib/conf.py`, `scripts/production/staff.py`, `scripts/herdr/worker_panes.py`, `scripts/lib/atomic_file.py` — read only.
- `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_stop_resume.py`, `scripts/shutdown/test_shutdown_record.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_pane_restore.py` — the tests.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — tab sort (`sort_tabs`).

**Workers:** `2 writers` — split by stop and restore.
- `impl` — stop and record (`inventory.py`, `stop_resume.py`, `shutdown_record.py`, `config/shutdown.conf`, `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py`).
  Own `stop_unit_workers`, its lifecycle callers, `resume_unit.py`, `worker.py`, `scripts/lib/resume_message.py`, and their tests.
  Own the process-info fixture changes in those tests.
  hub: `scripts/shutdown/stop_resume.py` (`stop_unit_workers`, which lifecycle callers use).
- `test` (opens as impl) — restart, restore, tab sort (`restart.py`, `pane_restore.py`, `herdr.py`) and their tests.
  Own the process-info fixture changes in those tests.

**State:** shutdown records; retained stopped worker rows; deletion only through permanent end or unit removal.

**Constraints from prior phases:** Roles come from fixed-id dispatch history: `progress_history.last_dispatch(row.id)` (Phase 14d). Keep saved-pass recovery through a restored live director. Phase 2 `staff.snapshot()`; Phase 3r retains removed unit rows. Retire, promote, and wrap stop workers for resume. Only a proved live target blocks removal; a structural fault leaves that worker alone with one anomaly line (review, 2026-11-04). Phase 4 names from rows; Phase 3h native runtime lookup with no copied staff identity; Phase 3n (native worker admission and backend discovery; no stored role); Phase 6 (the restore probe; `ExistingPane`); Phase 7 (`start_or_resume(parent, name, role) -> WorkerStart`, `close`, `observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]`, each open or stopped pane with its `terminal`); Phase 8 (`dispatch` reads the transcript first); Phase 9 (a busy review Stage 2 pass has the dispatch id `<dispatch id>-2`, and `mesh_runs.py` finds its worker by native runtime identity and reads the transcript before it types, so both reach a restored pane). Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). Phase 3's pending retirement (ids a wrap could not retire while worker rows were left) lives in the staff record; shutdown and restore keep it unchanged. `shutdown_record.py` `_write_record` (:1154) makes its own temp file (`mkstemp` at :1156) and `os.replace` (:1167); this phase changes the shutdown record, so `_write_record` calls `atomic_file.write_text_atomic` instead, inside the same `_record_deadline` checks. Shutdown's checkpoint (2026-10-30): every resume runs in the session's own tab with `Herdr.run_in_pane` (`herdr.py:1080`) through `accounts.py exec <account>`. Ordinary and split restores keep conversation id, account environment, identity, tab label and position, and their tests show it. Phase 4f: multi-pane tab order is `Herdr.sort_tabs` (herdr.py, about line 1662), with `tab.move`; reuse it. Own the worker transfer-to-restart test: the old shell stays in the source group and restart opens the worker in the new group. Changing `fake_unit_host.py`, `transfer_unit_test_support.py` or `LayoutHerdr` runs the whole `scripts/production` suite in the gate; a live-to-stopped fixture step updates the foreground evidence and keeps every other reply, such as an installed close failure.
- Phase 14a (herdr-unit, 2026-11-06): a resumed Codex reports no `agent_session` before its first prompt, and `Herdr.pane_native_process` reads that state as `UnreportedResume`. Preserve this pane proof through worker stop and restore. Test a resumed Codex before its next prompt.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): `staff.promote_unit` (`staff.py:869`) changes the row's kind and keeps its id and children; test that workers restore under the promoted holder after a `/clear`. The stop, record and lifecycle tests go to the first writer, restart and restore tests to the second.

**Constraints from Phase 5b:**
`Herdr.record_layout` in `scripts/herdr/herdr.py` reads `pane process-info` for every listed pane.
Every fake herdr used by this phase answers that command for each listed pane.
Use native agent facts for rows that must retain saved bindings; use known shell facts for panes without agents.
Use `pane_process_reply` in `scripts/herdr/test_herdr_sessions.py` as the fixture model.
Keep saved rows and existing assertions unchanged.
Each test owner makes the fixture changes in that owner's test files.

**Constraints from shutdown and role integration:**
Use `stop_resume.stop`, `restart.stop_row`, and `restart.start_row` for member lifecycle operations.
Pass `deadline=<monotonic>` through their shutdown-unit integration, commit `b76d3b028`.
Require Phase 4l's shared `LiveConversation | StoppedConversation | UnknownConversation` reader before dispatch.
Unknown evidence prints one anomaly line and supplies no stopped proof.
The `unit_move.py` deadline next item remains separate.
Phase 3n stores no role and admits workers through `staff.ensure_worker(identity, name, parent)`.
Pass the role explicitly to `start_or_resume` and every worker selection call.
Read the role through `progress_history.last_dispatch(worker_id)` with the worker's fixed staff id.
The reader returns the role, phase, and pass id from existing pass state and history.
`scripts/direct/progress_history.py` owns that pass and its `instance_id` in `progress_history_state.json`.
Phase 8 or devtools 3t owns this reader; add no role store.
Keep the staff specification at seven fields; add no role to `StaffRow`, `ensure_worker`, or admission.
Return `WorkerRoleUnavailable(reason)` when restore cannot find a dispatch record with a role.
Keep that result distinct from admission refusal and backend-unavailable results.
Never derive a role from a worker name.
Phase 7 owns explicit account selection in `start_or_resume`.
Phase 8 keeps the dispatch id as the active pass `instance_id` in `progress_history_state.json`.
That pass also owns the role in `kind` and `launcher_command`; shutdown records copy none of these facts.
Restore reads both after launcher loss and reuses the transcript receipt.
Keep `PausedSeat` and its resume messages for mesh-pinned runs until Phase 19.
Staff storage stays schema 2; live output stays schema 3.

**Acceptance gate (outcomes):**
Test all five `WorkerPane` variants: `OpenWorker`, `StoppedWorker`, `ClosedWorker`, `ProcessUnknownWorker`, and `UncertainWorker`.
Unknown process evidence supplies no stopped or absent proof.
A structural fault leaves that worker unchanged and permits unrelated proved work.
Test all seven `WorkerStart` variants: `OpenWorker`, `IncompleteStart`, `AdmissionRefused`, `UnpublishedWorker`, `ProcessUnknownWorker`, `UncertainWorker`, and `StartRefused`.
Test `StartRefused.reason` and `StartRefused.detail`, exit 2, no prompt delivery, and no row deletion.
Only `OpenWorker` permits prompt delivery.
An incomplete start keeps its pane and sends no work.
Admission refusal preserves its exact refusal line and exit 2.
Failed publication keeps the backend and makes no replacement row or pane.
Unknown process evidence prints one anomaly line and supplies no stopped or absent proof.
A structural fault leaves that worker unchanged and permits unrelated proved work.
Test shutdown and restore with unknown evidence; report that worker as not proved stopped.
Test a lost launcher with no live ancestor and the same saved pass id and command. `assign.main` requires a live director (`require_live_director`), so recovery first restores the director, then runs the saved command from that director's session shell. Keep that requirement.
Test both pane-worker resume messages and the retained mesh `PausedSeat` messages.
Test private `CONF_DIR`, effective range limits, recovered committed values, and unusable values without signals or starts.

**Acceptance gate:**

Record added and deleted source lines for this phase in its checkpoint message.
Require a source decrease here unless the named pinned branches require the group ending at Phase 19.
Do not delete unrelated code to meet a count.
Test restore from a finished last dispatch and missing-role output without staff role storage.

- Record-free recovery (sdlc, 2026-11-04): a crashed Claude worker or director with no launch record resumes from its transcript; a worker not proved stopped, or still running, is named in the shutdown report. The record holds only live row ids, and a test reads it back.
- basedpyright 0/0 on changed files and every caller module.
- Shutdown tests with worker panes inside the configured bound (the reads before the signal, one `STOP_GRACE_SECONDS + KILL_GRACE_SECONDS`, and the observation); a changed `STOP_GRACE_SECONDS` and `KILL_GRACE_SECONDS` in a test `shutdown.conf` (`CONF_DIR`) change the bound with no code change, and committed-value recovery keeps its warning.
  An unusable effective value stops before any signal with the file and key in the line; a changed `START_SECONDS` or `STAY_SECONDS` in that file changes the resume wait with no code change; an `UncertainWorker` is reported as not stopped; restore tests for both probe branches; a restored director takes its row's name; a restore with a worker pane still at its shell reuses that pane and its terminal.
- Real proofs (Invariants → Lifecycle reruns keep the pane), in a probe tab of `agents` with real worker panes: a rerun of a finished stop, down and restart each changes nothing, keeps the same tab, pane id and `terminal_id`, and exits 0; one real shutdown stopped after its first worker stop finishes on rerun, with each pane listed once.
- The worker stop test: a crashed director, no shutdown record, and a Codex worker whose working folder is outside the unit's worktree; `retire_unit.py` stops the worker through `stop_unit_workers`, proves the agent stopped, keeps the worker row and native id, and marks the retained unit RemovedWorktree. Retirement skips Codex rows and retains their rows and native threads. `wrap` and promote stop each unit's workers the same way before their staff step.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_shutdown_rerun_after_first_worker_stop` (the rerun stops the rest and the record lists each pane once); `test_restart_rerun_after_director_tab` and `test_restart_rerun_after_first_worker` (one set of panes); `test_restore_rerun_after_agent_resume` (stopped after a busy worker's agent resumes and before its launcher starts: the rerun starts one launcher, which types nothing when the line is in the transcript); `test_stop_unit_workers_rerun_after_pane_stop` (stopped after a pane stops and before recording stop completion: the rerun proves it stopped, keeps its row, and stops the rest); a rerun of a finished restart reports done and changes nothing.
- Full gate green.

**Acceptance gate (retained workers):** retire, wrap and promote stop without deleting workers; restore resumes the same ids and parents. A crashed director and an outside-worktree worker remain stoppable from row evidence. Permanent unit removal and worker end delete only after stopped proof. Interrupt each stop/restore boundary; retries preserve every resumable row and finish once.

**Pane close contract:** use Phase 6's confirmed pane close and `pane_launch.retire_launch`.
Preserve the director and sibling panes; test one retained launch after replacement and recovery after failed closure.

**Process anomaly rule:** Unknown process evidence prints one anomaly line and supplies no stopped or absent proof.
Use the herdr tab name, session name, or pid to identify the process, in that order.
A failed name read does not refuse the operation.
Leave an unproved worker unchanged and continue unrelated proved work.
Signal or close only through the shared target check.
Refuse destructive work only when the required target is proved live.
Test named output, continued known work, and protection of sibling panes.

**Resume-message contract:** replace independent optional `moved_from` and `moved_to` arguments with `NoAccountMove | AccountMove(source, destination)`.
Update every caller through the shared builder and preserve existing complete messages.
The stop writer owns `scripts/lib/resume_message.py` and its tests; the restore writer consumes its API.

### Phase 11 — Account moves for pane workers (accounts unit)  · status: moved: accounts-unit (code on sdlc; the real proof remains)

**Depends on:** Phase 3n, Phase 7, Phase 8, Phase 9, Phase 10.

#### Work Order

**Goal:** Resume each worker on the destination account in its own pane with the same conversation.

**Status note (herdr-unit, 2026-11-06):** the move code landed at `b3ee5dcaa` with `scripts/accounts/pane_moves.py`. The real proof below stays with accounts-unit: `1 writer + 1 tester`. `impl` runs the user-operated account-move proof and repairs what it finds. `test` records same-conversation, same-terminal, finished-rerun and source-recovery evidence. Do not dispatch the implementation again. Phase 14f keeps the role of a refused pass in `last_dispatch`.

1. Keep the `accounts.move_workers` signature and its callers.
2. Check the family through `restart_refusal`.
3. Read the role through `progress_history.last_dispatch` when available, or `worker_panes.dispatch_role` on this tree.
4. Leave a worker with no recorded role unchanged, with one line.
5. Acquire `worker_panes.worker_lock` without blocking.
6. Leave a `Kept` worker unchanged, with one line, and continue other workers.
7. Read fresh evidence through `observe`.
8. Leave `UncertainWorker` and `ProcessUnknownWorker` unchanged, with one anomaly line.
9. Keep an `OpenWorker` already on the target and record success.
10. Stop an `OpenWorker` on the source through `restart.stop_row`.
11. Use the deadline from `config/shutdown.conf`.
12. Require fresh `StoppedWorker` evidence on the same terminal before the destination launch.
13. Resume a `StoppedWorker` through `start_or_resume` with the destination account and the held lock.
14. Resume a `ClosedWorker` only when `start_or_resume` keeps its conversation.
15. Report a closed worker as not moved if that API cannot keep its conversation.
16. Send `resume_message` through the shared target check only for an open worker with an active pass.
17. Include `AccountMove(source, target)` and `ResumeWorkers(panes=[PaneWorkerResume(staff_id, BusyPass(dispatch_id))])`.
18. Send no text to an idle worker.
19. On another start result, read fresh evidence before recovery.
20. Resume the source once in the same pane only with proof that no target process owns the conversation.
21. Report the destination result and the source recovery result.

Numbered homes share `sessions`, `archived_sessions`, `session_index.jsonl`, and `projects`.
The move copies no conversation files and publishes no duplicate file.
It uses no byte writer, size comparison, or hash comparison.
It creates no step record and performs no post-move check.
A rerun uses fresh evidence and keeps a target that already runs the conversation.
`ClosedWorker` retains its staff row and current session; `start_or_resume` resumes that session in a new worker pane.

**Log:** Use the existing requested, confirmed, and failed entries in `scripts/accounts/moves.py`.
The subject is the staff id; requested entries also name the terminal.

**Refused pass:** Mark a pass `refused` when `refusal_watch.transcript_refusal` classifies its transcript as refused.
Keep its pass id and leave the pass open for redispatch after the account move.
A refused pass prevents pane closure and a role change.
Do not use `finish-pass` for this mark.
The existing account notice remains the user surface.

**Readers:** Use the shared live-account readers for both Claude and Codex.
Correct `accounts.session_on_account` and `stop_resume.running_on_account` where they miss Codex.
Add no second live-account reader.

**Deletes:** Remove the stop-and-pending path and server retirement from pane-worker moves.
Keep `ResumePending` for normal moves; it stays after Phase 19 deletes the mesh.
Runs pinned through `unit_runs.worker_transport(run)` retain their mesh path until Phase 19.
Change `worker_panes.py` only through a plain public rename when needed.
Keep the explicit role for the destination and for source recovery.

**Tests:** Use fake processes, fake panes, temporary projects, and a temporary `HOME`.
Cover all five `WorkerPane` variants: `OpenWorker`, `StoppedWorker`, `ClosedWorker`, `ProcessUnknownWorker`, and `UncertainWorker`.
Cover all seven `WorkerStart` variants: `OpenWorker`, `IncompleteStart`, `AdmissionRefused`, `UnpublishedWorker`, `ProcessUnknownWorker`, `UncertainWorker`, and `StartRefused`.
Check `StartRefused.reason`, `StartRefused.detail`, exit 2, no prompt delivery, and no row deletion.
Check the busy pass message and the idle worker's lack of input.
Check source recovery with no live target, changed dispatch roles, missing roles, and a held worker lock.
Check a completed rerun, a mesh-pinned run, sibling panes, the director pane, and the Codex account reader.
Test the route from transcript refusal to the pass mark and the move.
Remove the copy, publication, and atomic failure tests for this move.
Keep the shared bytes-writer tests unchanged.
Record added and deleted source lines; explain retained mesh code until Phase 19.
Require basedpyright with zero errors and zero warnings on changed files and callers.

**Real proof:** The user moves a worker in a probe tab.
The move keeps the conversation and the stopped pane's terminal.
A rerun of a finished move starts no process and changes nothing.

### Phase 12 — Astra and ask-a-friend in panes  · status: done

**Depends on:** Phase 1, Phase 3n, Phase 6, Phase 7, Phase 8, Phase 9. Shared gate with Phases 10/12/13 over the exact final batch.

#### As-built

- `scripts/herdr/side_pane.py` runs a consultation beside its caller: `open <label> [--question-file] [--cwd]`, `send <label> --message-file`, `close <label>`. It writes no staff row. The label and `terminal_id` in the caller's tab prove the pane, so a friend needs no native identity.
- `open` calls `launch_prepared_agent` with a `SplitPane`, or an `ExistingPane` for a labelled pane at its shell, and the `consultation` key of `[ask_a_friend.<caller family>]`. It starts with no opening prompt, then sends the question through `Herdr.prompt_terminal`.
- `close` ends the agent, then closes the pane after a second read finds the same terminal at its shell. A rerun finds no pane and exits 0. An unproved process prints one anomaly line, keeps the pane and exits with the uncertain code.
- `commands/ask_a_friend.md` and the script callers use `side_pane.py`. `launch_friend.sh` and `test_launcher_tasks.sh` are gone.
- A held Astra role opens through `worker.py` and `worker_panes` under one worker row, with its role passed explicitly. `design_roles.py` has no held-role mesh launch, follow or end path. `assign.py` has no `PRIME`, `FOLLOW` or `RESIDENT` operation.
- `worker.py assign` and `end` take `--parent`, so a held role keeps its one parent under any caller. `end --session-dir` reads the pass from the dispatch run.
- An ad hoc Astra review outside the staff hierarchy keeps its one-turn `codex exec` branch of `astra_review.py`.
- `herdr_tab_title.sync` leaves a non-staff pane that shares its tab untitled.
- `Herdr.close_retained_pane` returns a bool. `worker_panes` exposes `launch_policy`, `launch_arguments` and `launch_environment`.
- Source, production code: +438/-554.

**Files:**

- `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py` — the consultation pane.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — `pane_agent_process(pane, terminal)`, `prompt_terminal`, `close_retained_pane`.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — the public launch helpers; `dispatch_role` and `close` take a run.
- `scripts/astra/design_roles.py`, `scripts/astra/astra_roles.py`, `scripts/astra/astra_review.py`, `scripts/astra/design_role_test_support.py`, and their tests — held roles through the worker command.
- `scripts/production/worker.py`, `scripts/production/unit_move.py` — `--parent`, `end --session-dir`; `unit_move` discards the close result.
- `scripts/hooks/herdr_tab_title.py`, `scripts/hooks/test_herdr_tab_title.py` — the shared-tab rule.
- `scripts/direct/assign.py`, `scripts/direct/assign_gate.py`, `scripts/direct/test_assign_gate.py` — the removed operations.
- `commands/ask_a_friend.md`, `config/direct.conf`, `docs/as-built/agent-registry.md`, `docs/as-built/staff-accessors.md`, `docs/plans/build-followups-worker-seats.md` — callers, the opening deadline and the docs.

**Binds later work:**

- `dispatch_role` and a closure after a policy change read the holder's run until `progress_history.last_dispatch` arrives. A role recorded by a private dispatch reads as unavailable at restore.
- The installed Stop hook runs from `~/.claude`, so `herdr_tab_title.py` cannot act on a real friend's Stop until it lands on main.
- `side_pane.close` reads the configured stop time through `stop_resume.stop_grace_seconds()`.
- A consultation is a labelled pane. A staff worker is a row. Nothing reads a friend through staff.

**Gotchas:**

- A real Claude placeholder and a real borderless Codex composer both fail the composer check, so a friend's first text goes through `prompt_terminal`.
- A real Codex holds no open rollout, and a daemon-attached Codex reports no `agent_session`.
- A Codex tool shell holds `NO_COLOR=1`. A launch that forwards it makes a Claude pane read its placeholder as typed text.

**Ruled out:** Astra roles in `side_pane`, and a pane for an ad hoc Astra review, until the reply wait and close exist (sdlc); a native identity for a friend; a composer check before a friend's opening question.

### Phase 12a — A process that exits during the worker scan drops out  · status: done

**Depends on:** none

**Spec:** sdlc [36919f96]: a defect here reds merge gates, so this small fix goes ahead of Phase 14a, built from sdlc faf2a074d. A Codex process that exits during the `staff_sources` scan leaves the scan. A process counts as exited when a new process table no longer lists its pid. A live process with an unreadable environment still marks the scan incomplete. The `test_remove_workers` fixture does not read the host's processes.

**Files:** `scripts/production/staff_sources.py`, `scripts/production/test_staff_sources.py`, `scripts/direct/test_remove_workers.py`.

**Acceptance gate:** `test_staff_sources` proves both results of the scan; `test_remove_workers` runs with an empty process table; the production, direct, shutdown and message suites pass.

### Phase 12b — One run with an unreadable owner does not stop worker removal  · status: done

**Depends on:** Phase 12a

**Spec:** sdlc [32b50fe2], defect from builds: `StaffUnreadable` is an `OSError`, and `remove_workers.removal_targets` catches only `ValueError`. One run whose owner row cannot be read stops the whole removal. Under "report anomalies, never block", that run prints one anomaly line and the removal goes on.

**Files:** `scripts/direct/remove_workers.py`, `scripts/direct/test_remove_workers.py`.

**Acceptance gate:** a test proves that a run with an unreadable owner prints its anomaly line, and that the workers of another run are removed. The direct suite passes.

### Phase 13 — Displays read workers through observe  · status: moved: hierarchies-unit (sdlc, 2026-11-05)

**Depends on:** Phase 7, Phase 8, Phase 9. Shared gate with Phases 10/12/13 over the exact final batch.

#### Work Order

**Goal:** every display reads workers through `worker_panes.observe`, the one join of worker rows and herdr panes.

**Spec:**

1. **Displays.** Read workers through `worker_panes.observe(parent, record)` in each listed display.
   Keep this as the one staff-to-pane join.
   `staff_sources.live` supplies `staff.py show --json --live`; Phase 3n already removed `production_tree.py`.
   Show `OpenWorker` with its pane and status.
   Show `StoppedWorker` as stopped and `ClosedWorker` as closed.
   Show `ProcessUnknownWorker` with its known pane and reason as unknown, never as stopped or absent.
   Show `UncertainWorker` as incomplete with its reason, never as closed.
2. **One snapshot per run.** Each display takes one `staff.snapshot()` per run and passes it to every `member` and `observe` call.
3. **Problems are shown, never an empty list.** When `snapshot()` raises `StaffUnreadable` (natedev not reached, or a malformed record), the display shows the reason as a problem line and shows no empty worker list in its place: `roster.py` adds it to `Roster.problems` (through `_problem`), `staff_sources.live` puts it in its output as a problem entry with the cause (the handler shows it as incomplete, Phase 3n item 6) and on stderr, and each other display prints it on stderr. A member that gives `NoSuchMember` gets its own problem line, which names the member.

4. **Delete replaced display paths.** Delete mesh discovery, lookup, and display paths that the shared observation replaces in this phase.
   Update the process classifier and worker ownership, measurement, transcript, and temporary-file readers with their displays.
   Remove obsolete mesh labels and comments from the converted paths.
   Preserve required historical transcript reads and their behavior assertions.
   Keep no second pane-worker discovery path.
5. **Retained mesh paths.** Keep only the following branches for existing mesh-pinned runs until Phase 19:
   1. The mesh roster and server display branches in `roster.py` and the mesh roster branch in `board_json.py`.
   2. The mesh start/follow recognition in `process_table.is_codex_agent`.
   3. Mesh roster collection and route checks in `worker_owners.py`, including `mesh_end` and `mesh_cleanup`.
   4. Mesh roster guards in `tmp_sweep.run_folder_guards`, `worker_use`, and `cleanup_end_reason`.
   Guard those branches with the existing run pin; preserve their safety checks until the pinned runs finish.
   Only these named display branches join the group ending at Phase 19.
   Phase 19 owns their deletion; this phase owns every other replaced display path.

**Files:**

- `scripts/message/roster.py`, `scripts/production/staff_sources.py`, `scripts/production/codex_winddown.py`, `scripts/direct/board_json.py` — convert displays and delete replaced mesh paths.
  Keep only the named mesh-pinned roster and board branches until Phase 19.
- `scripts/message/process_table.py` — convert process classification; retain only mesh start/follow recognition for pinned runs until Phase 19.
- `scripts/usage/waste_watch.py`, `scripts/hierarchies/measure.py` — delete replaced mesh display paths, labels, and comments in this phase.
- `scripts/hierarchies/worker_owners.py` — delete replaced mesh ownership discovery.
  Retain only the named mesh roster and end/cleanup route branches for pinned runs until Phase 19.
- `scripts/shot_report/transcripts.py` — remove replaced live mesh handling in this phase; preserve required historical transcript reads.
- `scripts/lint/tmp_sweep.py` — replace mesh-only worker discovery with the shared worker evidence.
  Retain only the named mesh roster guards for pinned runs until Phase 19.
- `scripts/message/test_roster.py`, `scripts/production/test_staff_sources.py`, `scripts/production/test_codex_winddown.py`, `scripts/direct/test_board_json.py` — display tests.
- `scripts/message/test_process_table.py`, `scripts/usage/test_waste_watch.py`, `scripts/hierarchies/test_worker_owners.py`, `scripts/hierarchies/test_measure.py` — process and ownership tests.
- `scripts/shot_report/test_transcripts.py`, `scripts/lint/test_tmp_sweep.py` — preserve history and cleanup behavior during conversion.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — split the pane read out of `_evidence`, with its tests. Phases 14 and 15 need this checkpoint.
- `scripts/production/staff.py`, `scripts/production/session_staff.py`, `scripts/production/session_labels.py` — read only: `observe` (Phase 7), `snapshot` (Phase 2), the one membership join and display labels. Phase 3s extends the existing resolver; no module rename is needed.

**Workers:** `2 writers` — split by module group.
- `impl` — `roster.py`, `staff_sources.py`, `codex_winddown.py`, `board_json.py`, `message/process_table.py`, and their tests.
  Own their replacement deletions and named pinned branches.
  hub: `scripts/production/staff_sources.py` (`live`, the handler's command).
- `test` (opens as impl) — `waste_watch.py`, `hierarchies/worker_owners.py`, `hierarchies/measure.py`, `shot_report/transcripts.py`, `lint/tmp_sweep.py`, and their tests.
  Own their replacement deletions and named pinned branches.

**State:** none.

**Constraints from prior phases:** Phases 7-9; Phase 7 `worker_panes.observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]` (`OpenWorker(row, pane, terminal, status: herdr.AgentStatus)`, `StoppedWorker(row, pane, terminal)`, `ClosedWorker(row)`, `ProcessUnknownWorker(row, pane, terminal, reason)`, `UncertainWorker(row, reason)`); Phase 2 `staff.snapshot() -> StaffSnapshot` is one record read and raises `StaffUnreadable`; the member join is `session_staff.MemberSources.member` (`staff_sources.member` is its entry point), giving `MemberFound | NoSuchMember`, with one `member_sources(dependencies=...)` per command (Delegation Context → One identity join); Phase 3n `live()` keeps schema 3 wire fields and meanings and shows partial results as incomplete. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.
- Phase 3h review (2026-11-01): `observe` plus native-session evidence shows a live session with no published row (a held or failed clear hook) as incomplete in roster and handler output; the status line and accounts show their unavailable state; never invent an owner or report an empty set as complete.
- Phase 3s review (2026-11-02): test every state of Phase 7's `WorkerPane` observation union, `ProcessUnknownWorker` included. `UnpublishedWorker` is a `WorkerStart` result only: a failed publication makes no staff row, so no display observes it. Startup callers (Phases 8, 10, 11, 12) test every `WorkerStart` state.

**Acceptance gate (outcomes):**
Test all five `WorkerPane` variants: `OpenWorker`, `StoppedWorker`, `ClosedWorker`, `ProcessUnknownWorker`, and `UncertainWorker`.
Unknown process evidence supplies no stopped or absent proof.
A structural fault leaves that worker unchanged and permits unrelated proved work.

**Acceptance gate:**

Record this phase's added and deleted source lines in its checkpoint message.
Require a source decrease, apart from the named mesh-pinned branches in the group ending at Phase 19.
List each retained branch and its final deletion owner in the checkpoint message.
Delete no unrelated code to meet a count.
Require no replaced display mesh path outside the named retained branches.
Preserve historical transcript behavior and safety checks for pinned runs.

- basedpyright 0/0 on changed files and every caller module.
- Tests for each changed module; no display module calls `herdr agent list` outside `worker_panes.observe`.
- `test_staff_sources.py`: `staff.py show --json --live` shows each of the five `observe` states for a pane worker, with an `UncertainWorker` shown as incomplete with its reason; its output stays schema 3. Inactive membership stays hidden; cleanup readers retain live, unknown and duplicate evidence.
- Problem tests for each display, `staff_sources.live` included: an unreachable owner (the fake `ssh` fails), a malformed record and a missing member each give a problem line that names the cause, and no empty worker list; a run makes one `snapshot()` call (on the Mac, one call in the fake `ssh` log).
- No rerun test: displays only read (Invariants → Rerun-safe tools).
- Full gate green.

### Phase 14 — Instructions describe panes  · status: done

**Depends on:** Phase 10, Phase 11, Phase 12, Phase 13 behavior. Draft beside Phase 11 from its fixed contract; check final text and code together in the shared 11/14 gate.

#### As-built

- The listed commands and docs describe panes. Each live mesh instruction requires an existing mesh-pinned run, and that guarded text stays until the mesh goes.
- The text describes a retry of the original launcher command for a fresh unpublished worker, detached results, review Stage 2, one `unchanged` line for a completed rerun, a second review lens through `--to review2`, a director-run `findings.py landed`, and the `worker.py end` configuration refusal with exit 7.
- `worker_panes.launch_policy` reads `UNIT_COMPACT_ABOVE_TOKENS` from `config/direct.conf`. `_policy_arguments` adds `-c model_auto_compact_token_limit=<n>` for Codex. `LaunchPolicy.compact_above_tokens` is `int | ClaudeCompaction`, and a Claude launch does not change. An unusable value returns `StartRefused(INVALID_ROLE_CONFIGURATION)`. `side_pane._policy` reads the same key and converts `conf.Unusable` to `SidePaneRefused`.
- `worker_panes.launch_arguments(policy, identity, directory)` adds `--cd <directory>` on a Codex resume, after `--no-daemon`.

**Files:**
- `scripts/herdr/worker_panes.py`, `scripts/herdr/side_pane.py`, `scripts/herdr/test_worker_panes.py`, `scripts/herdr/test_side_pane.py`, `docs/as-built/long-lived-workers.md` — the compaction option and the resume folder.
- `commands/unit/direct.md`, `commands/unit/worker.md`, `commands/unit/report.md`, `docs/direct/`, `commands/showrunner/`, `commands/message.md`, `commands/codex_winddown.md`, `commands/shutdown.md`, `commands/ask_a_friend.md`, `commands/astra_review.md`, `commands/shot_report.md`, `docs/quota_alerts.md`, `docs/production_format.md`, `docs/accounts.md` — pane text.

**Binds later work:** `UNIT_COMPACT_ABOVE_TOKENS` stays when the mesh goes, because the pane launch reads it. The command mirror regenerates through `scripts/claude_to_codex/sync.py` once this text is on main. New text uses the name `targeted_test` once the builds rename is on the merge branch.

**Gotchas:** A resumed Codex takes its recorded thread folder unless `--cd` is given. A same-role dispatch keeps a running process, so a worker from before the option keeps running without it.

**Ruled out:** A restart of a same-role worker that lacks the option, because a rerun recovers an active pass through that process.

### Phase 14a — A fresh Codex pane worker publishes from its session report  · status: done

**Depends on:** Phase 7, Phase 8, Phase 12

#### As-built

- `worker_panes.launch_arguments` passes `--no-daemon` for every Codex pane launch: a daemon-attached Codex reports no `agent_session` after a turn.
- A fresh Codex worker takes one first turn. `worker_panes.first_turn_session` sends `WORKER_FIRST_TURN_TEXT`, waits up to `WORKER_FIRST_TURN_SECONDS` for the pane's `agent_session`, and `start_or_resume` publishes the row with that value. A failure returns `UnpublishedWorker` and keeps the pane; a rerun publishes once from that pane.
- A known identity keeps its `resume` argument and takes no first turn.
- `Herdr.pane_native_process` is the one reader of pane membership. One foreground `codex` whose `agent_session` equals the row's `current_session` proves the pane, and an open rollout proves it. `codex resume A` proves it when the report equals A (`PaneSessionReport`) or is empty (`UnreportedResume`). A report that names another session, or an unread pane row, proves nothing.
- `native_process(pid, kind)` keeps `LaunchArgument` for its process-only readers.
- `herdr._pane_environment` removes TERM and COLORTERM at a new tab, a split and an existing pane, exports and removals both. `child_environment` removes `NO_COLOR` and keeps TERM.
- `PANE_AGENT_START_SECONDS` in `config/direct.conf` is the one launch deadline for a worker and a side pane. An unusable value returns `StartRefused(INVALID_ROLE_CONFIGURATION)`.
- Measured idle memory, Pss (Rss) MiB: `--no-daemon` worker alone 236.72 (348.23), mean of two 181.93 (309.95), daemon-attached window 574.96 (663.45), daemon 665.85 (668.37).

**Files:**
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py`, `scripts/herdr/fake_herdr.py` — the one reader, the pane boundary filter and their tests.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — the launch argument, the first turn, the publish and the deadline.
- `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py` — the shared deadline key.
- `config/direct.conf` — the first-turn text, the wait deadline and the launch deadline.
- `scripts/production/test_worker.py`, `scripts/direct/test_assign_launcher.py`, `scripts/production/rename_test_support.py` — callers against a fresh Codex fixture.
- `docs/as-built/herdr-restore.md` — the rule.

**Binds later work:** A resumed Codex reports no `agent_session` before its first prompt, so stop, restore, destination resume and rollback accept `UnreportedResume`. A failed fresh start records no pass; recovery reruns the original launcher command.

**Gotchas:**
- A Codex tool shell sets TERM=dumb. `/etc/zshrc:70` then turns off zle, zsh does not reset the raw tty a stopped Codex leaves, and the CR from `pane run` never ends a line.
- herdr keeps a pane's old `agent_session` after the agent exits, so a report with a shell in the foreground proves nothing.
- A real Codex holds no open rollout.
- A Codex parent still passes LC_ALL, LC_CTYPE, LANG, GIT_PAGER, GH_PAGER, CODEX_CI and an arg0 PATH entry to a pane. No failure is measured from them.

**Ruled out:** a TERM filter in `child_environment`, because it also reaches `prepared.unset_environment` and a resume would unset TERM in the pane; a daemon-attached Codex worker.

### Phase 14b — An Astra review reaches a pane through assign.sh  · status: done

**Depends on:** Phase 12, Phase 14a, the devtools 8d stack on sdlc

#### As-built

- `assign.sh --role review --operation astra-review` is the one start path for a managed Astra review. `astra_review._managed_review` makes one such call.
- `assign.execute_astra_review` reads `unit_runs.worker_transport(run)`. For `mesh` it runs the prime, follow and end body. For `herdr` it calls `execute_astra_review_herdr`.
- `execute_astra_review_herdr` records start-run and start-phase `astra-review`, runs `worker.py add astra-reviewer`, holds the role with `astra_roles.hold("reviewer", id, pid=os.getpid())`, runs `worker.py assign` with the summary path as the reply, records finish-pass, runs `worker.py end` and releases the hold.
- A failed end returns 1 and keeps the hold while `assign.sh` runs. The staff row stays, and `worker.py list` shows it.
- The saved launcher command is the `assign.sh` command.
- `assign.py` has no held design, `PRIME`, `FOLLOW` or `RESIDENT` operation. `design_roles.py` has one worker-command path for a held role, with the sdlc creation fence, recovery refusal and end fence.

**Files:**
- `scripts/direct/assign.py`, `scripts/direct/test_assign_gate.py`, `scripts/direct/test_assign_launcher.py`, `scripts/direct/test_assign_launcher_review.py` — the operation, the transport branch and `PaneAstraReviewTests`.
- `scripts/astra/astra_review.py`, `scripts/astra/test_astra_review.py` — the one call and its tests.
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py` — the one worker-command path.
- `docs/as-built/agent-registry.md` — the start path on both transports.

**Binds later work:** `WORKER_TRANSPORT` is mesh, so no real caller reaches the herdr branch until the pilot. An Astra review folder is a separate run, and its pin reads the configured default.

**Gotchas:** The hold is an exec hold, and it ends with the `assign.sh` process. `astra_roles.hold` records hold `pid_start`.

**Ruled out:** a second start path through worker add in `astra_review.py`; a retire fix in this plan, because hierarchies Phase 23 prints an anomaly for an unrelated Codex process.

### Phase 14e — An unknown pane in another tab leaves a parent's workers alone  · status: done

**Depends on:** Phase 14b

#### Work Order

**Goal:** Morty's bare panes do not make every worker uncertain, and 1bd733716's red on sdlc ends. Showrunner ruling, 2026-11-06: "a pane that is not the parent's cannot make the parent's workers uncertain; evidence that touches the parent's own workers still can."

**Spec:** In `worker_panes._parent_evidence`, a worker with no native match and no labelled pane is `UncertainWorker("pane evidence is incomplete; absence is unproved")` when an unknown pane's tab is one of the parent's tabs. Otherwise it is `ClosedWorker`. Phase 14a removed this branch, which made the three Hierarchies Phase 22 tests red on the merged tree. An unknown pane in another tab changes nothing for this parent. An unknown pane that carries the parent's name as its label adds its tab to the parent's tabs. So a parent whose own pane read fails keeps its absent workers uncertain (showrunner ruling, 2026-11-06). A pane with a worker's label in another tab does not count, because worker names repeat across parents.

**Files:**
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — the rule, and the one assertion that changes under the ruling.

**Workers:** `1 writer`

**State:** none.

**Constraints from prior phases:** Phase 14d follows this phase. The tree for this phase is a private clone, because Phase 14d's writers hold the unit worktree.

**Acceptance gate:** `test_observe_parents_shares_one_read_and_keeps_each_parents_worker_states` shows an unknown pane in another parent's tab leaves this parent's absent worker closed. `test_roster.PaneRosterTests.test_pane_workers_keep_five_states_and_replace_native_session_entries` and `test_staff_sources.StaffSourcesTests.test_live_pane_workers_keep_all_five_states_and_report_unknown_evidence` stay as written, because their unknown pane is in the parent's tab. Live, read only: `worker_panes.observe` shows no worker uncertain from Morty's unknown panes. The full gate passes before the hash goes to the showrunner. `test_unknown_pane_in_the_parents_tab_leaves_an_absent_worker_uncertain` and `test_unreadable_parent_pane_leaves_an_absent_worker_uncertain` pin the two cases that stay uncertain.

### Phase 14d — The worker list reads a mesh-pinned run  · status: done

**Depends on:** Phase 14e

#### As-built

- `worker.py _read_workers` is the one worker read for `list`, `check` and `restore`, and it honors the run pin. For a mesh-pinned caller that holds a run, it reads `board_json.workers(run, sources, record=record)`. A caller that holds no run keeps the pane read.
- A mesh row prints name, role, state and activity. `--json` adds `id` and `current_session` from the staff row of that name. `check` prints each `incomplete` row and exits 1 only when one exists. `restore` opens no pane and prints `unchanged`.
- `worker_panes.dispatch_role` reads `progress_history.last_dispatch(row.id)`, the newest open or finished dispatch, so a role survives `start-phase`. `worker_pass` stays for the readers of an open pass. `WorkerRoleUnavailable` stays.
- `progress_history.dispatch_reads` and one read of each run root and the staff record per call give `worker.py list` 2.1 s for 56 rows.
- `worker_panes._start_locked` and `close` pass `deadline=<monotonic>` to `restart.stop_row`. `close` reads its stop budget under the locks. On `conf.Unusable` it returns `Kept`, the worker command prints the refusal and exits 7, and the row stays.
- A pane pass ends only when the worker is done. `worker.dispatch_receipt` returns `DispatchWaitingForRuns(runs, has_live_run)` while a run is live or a result was delivered, and `DispatchAwaitingReply` otherwise. `_wait_for_dispatch` waits on file changes, bounded by `herdr.PROGRAM_TIMEOUT_SECONDS`, and reads the worker state at each wake. An ended worker with no reply and no live run returns `NO_DONE` after one bounded wait with no file change and one more state read. A run with no result sent to this worker holds nothing.
- `astra_review.run_review` writes `unit_runs.parent_transport(caller.current_session)` into the new run folder's `worker_transport`, so a review from a mesh-pinned caller stays on the mesh.
- `test_add_showrunner.py` `setUp` pins `local_machine` to natedev. The product code stays.

**Files:**
- `scripts/production/worker.py`, `scripts/production/test_worker.py` — `_read_workers`, `dispatch_receipt`, `_dispatch_turn`, `_wait_for_dispatch`.
- `scripts/direct/board_json.py`, `scripts/direct/test_board_json.py`, `mods/unit-board/types/board.d.ts` — the optional `name` on a mesh row, and its role from `task`.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — `dispatch_role`, the stop deadline and `close`.
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — `last_dispatch`, `dispatch_reads`.
- `scripts/agents/mesh_runs.py` — `worker_runs(since=)`, `delivered_worker_results`.
- `scripts/astra/astra_review.py`, `scripts/astra/test_astra_review.py` — the Astra run's pin.
- `commands/unit/worker.md` — `list`, `check` and `restore` for a mesh-pinned run.
- `scripts/production/test_add_showrunner.py` — the fixture machine name.

**Binds later work:** `last_dispatch` is the one reader of a worker's role. `NO_DONE` after a quiet wait is exit 3, and the recovery is the saved launcher command. The installed launcher keeps its older pass-end behavior until this phase is on main.

**Gotchas:** A delivery wakes the launcher before the pane status changes, so a timer alone gives an early `NO_DONE`.

**Ruled out:** `UNCERTAIN` for a quiet ended worker, because the recovery is the same saved command.

### Phase 14f — Pane workers take every address, and a closed descriptor refuses no delivery  · status: done

**Depends on:** Phase 9, Phase 14d.

#### As-built

- `send.py --help` and the module usage name `worker:<unit>/<worker>` for a pane worker, and `worker:<run id>/<worker>` only for a worker in a mesh-pinned run.
- `send.resolve_recipient` selects from the resolved row and the run pin. A staff worker in a pane-pinned run, held on this machine, takes `PaneWorkerRecipient` with or without the `worker:` prefix, so `codex:<thread id>` reaches the pane. A mesh-pinned worker keeps `CodexWorkerRecipient`. A holder on the other machine keeps `RemoteDelivery`.
- `worker_panes.worker_run(parent, *, record=RunStaffAtUse.LOCAL)` takes a staff snapshot and reads `ROSTER_RUN_ROOT`, so `send.py` holds no copy of that read.
- `herdr._open_codex_rollouts` skips a descriptor that closes during the listing. `codex_rollout_threads` reads the rollouts again when one closes, inside `PROGRAM_TIMEOUT_SECONDS`. A pane with another session stays refused.
- `progress_history.last_dispatch` includes a `refused` pass, so a worker whose newest pass is refused keeps that pass's role and id.

**Files:**
- `scripts/message/send.py`, `scripts/message/test_send.py` — the route and the help text.
- `scripts/herdr/worker_panes.py` — `worker_run`.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — the rollout proof.
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — `last_dispatch`.

**Gotchas:** The final listing may differ from the listing that was read only as a strict subset. A new or replaced descriptor refuses.

**Ruled out:** A retry of every changed listing, because a new descriptor must refuse.

### Phase 14g — A worker from an earlier phase can be ended  · status: done

**Depends on:** none. It lands before the switch, because the switch ends each open pane worker (user, 2026-11-06: "you have numerous open panes").

#### As-built

- `worker_panes.can_close` reads the resolved run's pass through `worker_pass`. With no pass there, it reads `progress_history.last_dispatch(row.id)`, the one reader across the parent's runs. So `worker.py end` ends an idle pane worker whose last pass finished in an earlier phase.
- Either read must give status `completed`, and its `instance_id` is the dispatch id. The idle checks, the agent status, the transcript's complete records and the last `DONE <id>` or `DONE <id>-2` line stay as they were. A newer pass with any other status keeps the worker open.
- A stopped worker passes on the idle checks alone.

**Files:**
- `scripts/herdr/worker_panes.py` — `can_close`.
- `scripts/herdr/test_worker_panes.py` — a pass across a phase boundary, a newer error pass, an explicit run outside the run roots, and a showrunner's worker.

**Gotchas:** `last_dispatch` scans only the run roots and `UNIT_RUN_DIR`, and `unit_runs.run_owner` accepts only a unit. So a pass from an earlier phase of a private run or of a showrunner's run stays `Kept`.

**Ruled out:** `last_dispatch` as the only read, because it refuses an explicit private run and a showrunner's worker.

### Phase 14h — A friend launches with the worker policy  · status: done

**Depends on:** Phase 14g, because both phases change `scripts/herdr/worker_panes.py`.

#### As-built

- `worker_panes.worker_policy(settings)` returns the `LaunchPolicy` of a worker that writes files, for the supplied model settings. `launch_policy(role)` and `worker_policy` share `_launch_policy(settings, sandbox)`, which holds the permission and the compaction read.
- `side_pane._policy` calls `worker_policy` with the `ask_a_friend.consultation` settings. `side_pane.py` names no sandbox and no permission mode. It still turns `conf.Unusable` into `SidePaneRefused`.
- `commands/ask_a_friend.md` states that the friend has worker permissions, and that step 2's instruction to change files only after an explicit request holds it back.
- `docs/as-built/agent-registry.md` states the friend's policy.

**Files:**
- `scripts/herdr/worker_panes.py` — `worker_policy`, `_launch_policy`.
- `scripts/herdr/side_pane.py` — `_policy`.
- `scripts/herdr/test_side_pane.py` — both families' permissions.
- `commands/ask_a_friend.md`, `docs/as-built/agent-registry.md` — the statement of the policy.

**Gotchas:** Plan mode allows an allowed Bash command, so a plan-mode friend can reply with `send.py`. Whether it stops at a prompt depends on the model's path. A reply with no prompt does not prove the policy.

### Phase 15 — Pilot and default switch  · status: done

#### As-built

- `config/direct.conf` sets `WORKER_TRANSPORT=herdr`, so a new run pins herdr at its first `unit_runs.worker_transport(run)` read. A run that already pins mesh keeps its pin for its whole life.
- Before the switch, a one-off program pinned each natedev run that held mesh records to mesh (42 runs; none on the Mac).
- `worker.py` dispatch waits up to `WORKER_SETTLE_SECONDS` (`config/direct.conf`, 30) for a worker that still reads `working` before it returns busy (exit 6).
- `Herdr.wait_turn_end` matches `done` or `idle`. The first turn in `worker_panes.py` reads the session report, then waits for the turn end.
- `commands/unit/direct.md` holds the recovery text for a quiet turn with no DONE line (exit 3) and for a friend wait.
- Mesh test setups write their own `worker_transport` pin, because the default is `herdr`.

**Files:**
- `config/direct.conf` — `WORKER_TRANSPORT=herdr`; `WORKER_SETTLE_SECONDS=30`.
- `scripts/production/worker.py`, `scripts/production/test_worker.py` — the dispatch settle wait and its tests.
- `scripts/herdr/herdr.py`, `scripts/herdr/worker_panes.py`, `scripts/herdr/test_herdr.py`, `scripts/herdr/test_worker_panes.py` — the turn end wait and its tests.
- `scripts/herdr/test_side_pane.py` — the fixture sets `HERDR_PLATFORM`, so the fake process reader also runs on the Mac.
- `commands/unit/direct.md` — the exit 3 recovery text.
- `scripts/agents/test_codex_mesh.py`, `scripts/agents/test_mesh_runs.py`, `scripts/message/test_roster.py`, `scripts/direct/assign_claude_test_support.py`, `scripts/direct/assign_launcher_test_support.py`, `scripts/direct/test_board_json.py`, `scripts/direct/test_mesh_tree_cancel.py`, `scripts/direct/test_remove_workers.py`, `scripts/direct/verify_hold_wait_test_support.py`, `scripts/direct/verify_token_wait_test_support.py` — mesh test setups name their transport.

**Binds later work:** mesh pins do not drain by themselves, because a unit keeps one run across a whole plan; the cleanup prepare phase ends them through a run handoff at a phase boundary. Live workers and prompts type `python3 ~/.claude/scripts/agents/mesh_runs.py start`, so the rename waits until no session can type the old path. A friend wait that returns exit 3 keeps its pass: wait for DONE and rerun the saved launcher with no message.

**Gotchas:** a pane in the focused tab of the focused workspace ends its turn at `idle`, never at `done`; an unfocused pane ends at `done`. Fixtures can read the real `/tmp/claude/unit` through `unit_runs.held_by_session` when no root override is given.

**Ruled out:** changing the pin of an existing run, because the pin contract is immutable and worker state would mix two recovery paths.

### Phase 16 — Prepare the cleanup  · status: done

#### As-built

- `unit_runs.worker_transport(run)` refuses a new mesh pin: one stderr line, exit 2, and no pin file. An existing pin of either value reads unchanged.
- `commands/unit/direct.md` <LongLivedWorkers/> holds the five-step run handoff for a run that pins mesh, at its next phase start: finish or record the pass, keep history and bindings, `finish-run --status stopped` then `end_session.sh`, one `ps` read for leftover mesh processes, then a new run through `prepare_session.sh`.
- `board.sh post|read|role` use `$UNIT_RUN_DIR` when the first argument after the verb is not an absolute path. With neither, the call exits with "<cmd> needs <session_dir> or UNIT_RUN_DIR". Worker prompts in `docs/direct/write_prompt_contract.md` use the folder-free form; a mesh pin keeps the folder form.
- `worker_panes._start_locked` (:731-739) has a same-run check. A retained unit `OpenWorker` with a matching policy and no dispatch in the current run reads `native_pane_process(...).environment["UNIT_RUN_DIR"]`. When it names another run, the worker restarts in place: stop, then resume the same conversation in the same pane. An unreadable environment returns uncertain ("running launch environment is unproved").
- `scripts/usage/activity.py` counts the folder-free `board.sh post <slot> status` as a status post.
- `mesh_runs.py start` finds a pane caller through its identity with no folder argument; it needed no change.

**Files:**
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — the pin refusal and its tests.
- `commands/unit/direct.md` — the run handoff rule.
- `scripts/direct/board.sh`, `scripts/direct/test_board_text.py` — the folder-free post, read and role.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — the same-run check and the in-place restart.
- `docs/direct/write_prompt_contract.md` — the folder-free board commands.
- `scripts/usage/activity.py`, `scripts/usage/test_activity.py`, `docs/as-built/usage-ledger.md` — the status post classifier.
- `scripts/agents/test_mesh_runs.py`, `scripts/message/test_roster.py` — mesh fixtures write their own pin; nested `StaffTestCase` cleanup uses one `ExitStack`.

**Binds later work:** an edit of the `_start_locked` branch keeps the same-run check. The live proof of the in-place restart at a run handoff runs before the mesh drain gate. Deleting the mesh also deletes the pin refusal and every existing-mesh-pin branch in the command text.

**Gotchas:** dispatch history cannot prove the run of a running process: after an in-place restart, the newest dispatch still names the old run until the next dispatch, so the check reads the launch environment for that gap only. On the Mac the environment read uses `ps eww` (`darwin_process_environment`), which has no live proof.

**Ruled out:** the dispatch record as the only evidence of the run, because it lags an in-place restart.

### Phase 17 — Pane launch paths for held roles and friends  · status: done

#### As-built

- `assign.sh` has one pane launch for staff workers, friends, held design roles and the ad hoc Astra review. `assign.py` routes `--target staff-worker | consultation-pane | durable-design-role` to explicit targets `worker_panes.StaffWorker | ConsultationPane | DurableDesignRole`, each launched with an explicit `LaunchPolicy`. The `codex exec` branch of `astra_review.py` is gone.
- The ad hoc Astra review opens a read-only Codex consultation pane through `worker_panes.astra_review_policy(model, effort)`: the model and effort come from `astra_review.MODEL` and `REASONING` through `--model` and `--effort`, approval never, compaction at `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS` (`config/direct.conf`). The `design_roles` family does not change it. Held reviewers use `launch_policy` and `UNIT_COMPACT_ABOVE_TOKENS`.
- `worker.consultation_reply` waits until the transcript records the question, then waits for the turn end, then reads only the reply after the question. A question that is never accepted raises "question was not accepted"; the pane closes and no report is written. An unproved close exits 1 and keeps the report file.
- `side_pane.open(..., delivery: ReplyToCaller | TranscriptReply)`: only a friend question carries the caller's send.py reply line. `worker_panes.REPLY_TO_CALLER` is the one default.
- `worker.transcript_receipt(source, line, DoneReply | FinalReply)` is the one transcript receipt for dispatch and consultation. `worker.dispatch(..., started: OpenWorker | StartAtDispatch)`: a caller that already started the worker passes it, so one dispatch starts its worker once.
- `execute_durable` takes no working directory; `design_roles.py` passes no `--cwd`. A durable launch without a prompt only starts the worker.
- `herdr.PaneNotRead` (`PANE_NOT_READ`) names a pane row that was not read. `Herdr.native_session` and `native_pane_process` take a keyword-only `pane_row: Mapping | PaneNotRead`.
- `test_worker_panes.PaneWorld` writes process stat files under `HERDR_PROC`, or under its own temporary folder when that is not set.

**Files:**
- `scripts/direct/assign.py`, `scripts/direct/assign_gate.py`, `scripts/direct/test_assign_launcher.py`, `scripts/direct/test_assign_launcher_review.py` — the pane targets, the consultation and durable paths, and the mesh `MeshWorker` hold test.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — targets, launch policies, `REPLY_TO_CALLER` and the `PaneWorld` fixture.
- `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py` — friend and consultation panes with explicit delivery.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — `PaneNotRead` and the pane-row native reads.
- `scripts/production/worker.py`, `scripts/production/test_worker.py` — transcript receipt, consultation reply and dispatch start state.
- `scripts/astra/astra_review.py`, `astra_roles.py`, `design_roles.py`, `design_role_test_support.py` and their tests — Astra review and held roles through `assign.sh`.
- `scripts/production/test_rename_unit_sessions.py` — the native pane read fake takes `pane_row`.
- `config/direct.conf` — `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS`.
- `commands/unit/direct.md`, `commands/astra_review.md`, `commands/ask_a_friend.md` — the consultation and friend pane text.

**Binds later work:** the mesh deletion keeps `astra_review_policy`, the held-role `launch_policy`, `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS`, both delivery variants and the consultation failure tests; the mesh Astra dispatch stays until the mesh deletion.

**Gotchas:** `PaneWorld` is shared by test modules in accounts, shutdown, production, message and hooks, so a fixture change needs their suites too. Some fakes outside the herdr folder still have the old `native_session` signature (`shutdown/test_pane_restore.py:156`, `production/test_unit_command.py:39`); they pass because no caller gives them `pane_row`. basedpyright over `scripts/message` reports 21 `reportImportCycles` errors that were already there before this phase.

**Ruled out:** a durable `--cwd` check against the parent worktree (no approved check); a reply-wait loop beside the dispatch receipt.

### Phase 18 — An unproved review close names its report, reason and pane  · status: done

#### As-built

- `assign.execute_consultation` keeps the open pane as `side_pane.AbsentSidePane | side_pane.OpenSidePane`. When `side_pane.close` returns `SidePaneCloseUnavailable`, it prints `assign.sh: consultation close unproved: <reason>; pane <pane id> (<label>) stays open` to stderr and returns 1.
- `astra_review.main` prints `Saved: <review path>` to stderr after the `Log:` line in its failure branch when the report holds text. The exit code does not change. The `Codex said:` tail carries the `assign.sh` close line, because `_managed_review` writes stderr to the log.

**Files:**
- `scripts/direct/assign.py` — the consultation close line.
- `scripts/astra/astra_review.py` — the `Saved:` line on failure.
- `scripts/astra/test_astra_review.py` — `test_unproved_consultation_close_fails_and_preserves_the_report` checks exit 1, the kept report, the `Saved:` line, the close reason and the retained pane id.

**Binds later work:** the mesh deletion keeps both stderr lines and `test_unproved_consultation_close_fails_and_preserves_the_report`.

**Gotchas:** in `test_astra_review.py`, a held reviewer's mesh server record starts a real process. It exits through the worker fixture, after the `PaneWorld` `os.kill` guard ends; an earlier exit hits the guard with "signal target is outside the fixture".

**Ruled out:** a new check on the close result (sdlc: add no check).

### Phase 19 — Delete the mesh and rename the detached runs  · status: moved: hierarchies-unit (sdlc, 2026-11-07)

**Depends on:** Phase 16, Phase 17, Phase 18, Phase 21, Phase 22, Phase 23, Phase 24, Phase 25, Phase 26, Phase 27, Phase 28, Phase 29, and the drain gate below (sdlc, 2026-11-07: Phase 27, Phase 28 and Phase 29 land first).
Phase 26 meets the drain gate, and this phase follows it at once (sdlc, 2026-11-07). The Phase 26 one-off program ran from the director's scratch folder; it is not in the repository, so this phase deletes nothing for it. End state (user): assign uses panes only, and no record of a mode exists.
Require the devtools-unit 3t checkpoint, with the `implement.sh` and `review.sh` Codex branches deleted, merged to sdlc.
Unrelated staff stores and deletions can follow panes without the whole-4g barrier.

**Drain gate (director-owned, before dispatch).** Phase 16's handoff rule ends each mesh-pinned run. The director records this evidence in the run folder; no writer does this work:
1. The switch time (the Phase 15 checkpoint as it lands on sdlc; top hash `58b31dd2b` replaced the undone `3ab83fc6a`) and the cleanup deadline, 7 days later.
2. No active run under `/tmp/claude/unit` or `/tmp/claude/delegate` pins mesh, on both machines.
3. No `mesh_runs.py supervise`, `codex_mesh.py` or `codex app-server` process of a mesh run is alive, on both machines. This includes every detached supervisor of a pane worker, because the rename removes the old script path and config.
4. Every session that can still type a cached `mesh_runs.py` command has ended or started a new run. No forwarder file stays at the old path (herdr-unit call, sdlc agreed 2026-11-06; user: "i never approved the word mesh").
5. The inventory of worker ledgers, rosters, consumers and history readers. Every live consumer reads staff membership and the discovered backend evidence: `StaffSnapshot.backends` and `session_staff.worker_backend(record, identity) -> RecordedWorker | WorkerBackendUnavailable`. These readers derive backend evidence from `current_session` and store no backend record. Staff storage stays schema 2; live output stays schema 3. Include `staff_sources.live`, messages, shutdown, accounts and displays.
6. Required history stays readable from its durable history source. Native-id and worker-fill completeness are recorded.
7. The installed external `signin-changed` consumers on both machines.
Missing evidence for any step blocks dispatch. A completed folder keeps its pin file until the history proof; then delete those pin files.

#### Work Order

**Goal:** the mesh, the old worker records and `WORKER_TRANSPORT` are gone, and no live name holds the word mesh.

**Spec:**

1. Delete the ledger directly; add no ledger migration or intermediate staff-id path conversion.
2. Remove the retained mesh `PausedSeat` resume-message branch. Search its callers and tests; preserve pane-worker and complete account-move messages. `inventory.worker_resumes` (`inventory.py:790`) builds `PausedSeat` for Claude children too (models 3s: `impl_worker_*.json`, `impl_status_<slot>`). Give the Claude child resume its own type before you delete `PausedSeat`. Delete `_codex_servers` (`inventory.py:822`) and its callers.
3. Delete only the retained mesh-pinned paths and state in the explicit Files below.
   Phases 7–13 deleted their own replaced paths; do not assign those deletions here again.
   Treat Reference → Cleanup list as an inventory; exclude entries already deleted by their replacement phases.
   The models unit reviews retained `workers.py` and `codex_mesh.py` code.
4. Delete the one-time tools and obsolete command tests after the drain gate's completeness proof. Search collector imports and command callers before removal.
5. Remove the mesh branch and `WORKER_TRANSPORT` selection from `assign.py`. devtools 3t owns the `implement.sh` and `review.sh` Codex branches (sdlc ruling, 2026-11-05).
   Remove the mesh-pinned phase `dispatch.lock` checks; retain the fixed-id worker lock.
   Move the sandbox constants imported by `worker_panes.py` from `codex_mesh.py` into `worker_panes.py` before deleting the mesh. Keep one owner for these constants and preserve their values.
   Before deleting `codex_mesh.py`, list each live import and give each retained function one owner: the Stage 2 file watchers, `Herdr.loaded_threads`, the refusal marker, the detached-run helpers and the side-pane sandbox constant.
   Delete `ADOPT` and `RESUME_ACCOUNT_MOVE` from `assign.py` with the mesh. Delete the mesh Astra dispatch that Phase 17 kept.
6. Remove `worker_transport` and the run pin from `unit_runs.py`. `unit_runs.parent_transport` has two more callers: `astra_review.py:106` and `session_staff.py:233`. Remove both calls with it. `unit_runs.py` keeps standard-library imports at the top for the Mac usage bootstrap; `test_collect_mac.py` is its consumer test.
7. Test interrupted state retirement and a finished rerun that prints `unchanged` and changes nothing.
8. Leave launcher consolidation to devtools 3t.
9. Delete the old held-role paths: `MeshWorker` and its branches (`astra_roles.py:44`), the design mesh roster reads and edits (`design_roles._edit_roster` and `_roster`, `design_roles.py:535`, `:543`), `HeldRole.codex_home` (`astra_roles.py:79`), and the held and resident paths of `codex_mesh.py` (:2736). Old hold files and old designs change once, through a one-off program at deploy.
10. **Keep the launcher fixture (builds, via sdlc, 2026-11-06).** `scripts/agents/test_codex_mesh.py` also supplies the fixture of other test modules: `setUpModule`, `THREAD_ID`, `FixtureLoopBudget`, `LifecycleTestClock` and `StubAppServer`. Before you delete that file, move each name that a kept test still reads into a kept test support file, and update each importer. Importers today: `scripts/direct/test_assign_launcher_reuse.py`, `test_assign_launcher_identity.py`, `test_assign_launcher.py`, `test_assign_launcher_review.py`, `test_assign_gate.py`, `test_assign_claude_door.py`, `test_launcher_waits.py`, `test_implement_launcher_compaction.py`, `test_workers.py`, `assign_launcher_test_support.py`, and `scripts/astra/test_astra_review.py`.
11. **The rename (user, 2026-11-06, through queue-unit: "the config needs to match the script name - that's approvbed" and "i never approved the word mesh").** In one commit:
    1. `scripts/agents/mesh_runs.py` becomes `detached_runs.py`, with its test.
    2. `config/mesh_runs.conf` becomes `config/detached_runs.conf`.
    3. `scripts/agents/mesh_run_process.py` becomes `detached_run_process.py`. Its `supervises` matches the new script name.
    4. The `mesh_runs/` record folder becomes `detached_runs/`. Move no live supervisor's record folder: the drain gate proves none is live. Keep completed logs readable, because records store absolute log paths (`mesh_runs.py:407`).
    5. `scripts/direct/test_mesh_tree_cancel.py` becomes `test_detached_tree_cancel.py`, and its cancel coverage stays.
    Update every caller in the Files below. Add no forwarder and no new check.
12. **Types.** `mesh_runs._run_worker` (`:209`) returns `StaffRow | None`: use a found worker and an unavailable worker with a reason. `result_text` and `deliver` take `int | None`, where `None` means that the supervisor disappeared: use one type for a completed command and one for a lost supervisor, and convert the record field at the reader. `worker_panes.start_or_resume` (`:685`) and `worker.dispatch` (`:236`) take `policy: LaunchPolicy | None`, where `None` means "resolve the role policy": use `UseRolePolicy | LaunchPolicy`. Keep the tests for the role default and the explicit Astra policy.
13. **Tell queue-unit at landing (queue-unit, 2026-11-06).** When this phase is on the builds branch, tell queue-unit. queue-unit then sets the group name `builds-detached.slice` in the renamed script. This phase changes nothing for it.
14. **Astra reviews run in an owned pane worker (builds, via sdlc [0a14929b], 2026-11-07).** `/astra_review` from the builds showrunner ran on a `codex_mesh` app-server with no owner (`/tmp/claude/astra/20261107T184024Z-115551/codex.log`: "this run has no owner because progress_history_state.json and mesh_roster.json name no unit, production or staff row"). The cause is the pin that `astra_review.run_review` writes (`scripts/astra/astra_review.py:106`) from `unit_runs.parent_transport`. With the pin gone (Spec item 6), a `staff-worker` review goes through the herdr pane worker, with the calling session's staff row as the parent of its `astra-reviewer<pid>` worker. All astra callers (`astra_review.py`, `astra_work.py` and `design_roles.py`) use this one path; the roles unit that owned astra is retired, so herdr owns it. A test proves that a review called from a showrunner session opens a pane worker whose parent is that showrunner's row, and reaches no `codex_mesh` code.

**Files:**
- `scripts/production/worker_fill.py`, `scripts/production/test_worker_fill.py`, `scripts/production/migrate_staff_ids.py`, `scripts/production/test_migrate_staff_ids.py` — delete the one-time tools after the completeness and history proofs.
  Remove their command callers and collector imports.
  Phase 8k of `docs/plans/sdlc-devtools-tests.md` removed the `agent_bg.sh` call to `worker_fill.py coordinate-claude` and the `assign_gate.PYTHON_HOPS` entry. The worker command replaces `scripts/direct/claude_worker.py`.
- `scripts/ask_a_friend/test_end_friend.py` — retire only the remaining `worker_fill` command tests with that tool; preserve shared consumer tests.
- `scripts/astra/astra_roles.py`, `scripts/astra/test_astra_roles.py`, `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py` — Spec item 9.
- `scripts/astra/astra_review.py`, `scripts/astra/test_astra_review.py` — Spec item 6: remove the `worker_transport` pin write (`astra_review.py:106`); Spec item 14: the owned pane worker test.
- `scripts/astra/astra_work.py`, `scripts/astra/test_astra_work.py` — Spec item 14: any astra Codex turn uses the same owned path.
- `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py` — delete the mesh allocation, launch, follow-claim and server paths retained for pinned runs; Spec item 10 first.
- `scripts/direct/end_session.sh`, `scripts/direct/test_end_session.py` — `end_session.sh:38` and `:92` call `codex_mesh.py`; delete those calls with the mesh.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — own the sandbox constants after mesh deletion. Remove any remaining mesh-pinned phase-lock checks; retain the fixed-id worker lock. Test startup without `codex_mesh` and preserve each role's sandbox. Update the detached-run read (`:482-483`) and the inline sandbox imports (`:537`, `:549`, `:558`, `:564`). Keep `astra_review_policy` (fixed read-only Codex with the supplied model and effort, `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS`) and the held-role `launch_policy` with their tests.
- `scripts/direct/workers.py`, `scripts/direct/test_workers.py`, `scripts/direct/worker_state.py`, `scripts/direct/remove_workers.py`, `scripts/production/retire_unit.py` — delete only the pinned mesh allocation, worker-removal paths and unit worker ledger. In `retire_unit.py` `stop_rest` (:632-636) the mesh-pinned `workers.end_unit` branch goes. Update the `mesh_run_process` import and helper calls (`workers.py:29`, `:696`, `:699`, `:770`) and the record folder (`retire_unit.py:37`, `:332`).
- `scripts/production/worker.py`, `scripts/production/test_worker.py` — move their imports of deleted or renamed modules (`worker.py:18`, `:174-176`, `:286`, `:300-309`). Keep the Stage 2, detached-result, quiet-wait and closure tests after the helpers move.
- `scripts/direct/assign.py`, `scripts/direct/test_assign_launcher.py` — delete the pinned dispatch, follow-claim, wait, Stage 2 branches and `WORKER_TRANSPORT` switch. Delete the mesh Astra prime, follow and end branch of `execute_astra_review` and its mesh-only tests; retain `ASTRA_REVIEW` and `execute_astra_review_herdr`. Update the rules text (`assign.py:46`). `implement.sh` and `review.sh` belong to devtools 3t.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py`, `scripts/usage/test_collect_mac.py` — `worker_transport` and the run pin go.
- `config/models.conf`, `config/direct.conf` — `[direct.options] codex_mesh`, `UNIT_CODEX_MESH` and `WORKER_TRANSPORT` go. `UNIT_COMPACT_ABOVE_TOKENS` and `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS` stay, because the pane launch reads them. models-unit owns `config/models.conf`: send it the `[direct.options]` change.
- `scripts/shutdown/inventory.py`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/session_types.py` — Spec item 2: delete `_codex_servers`, `CodexServer`, the mesh `PausedSeat` path, mesh pause and their callers; keep the Claude child resume under its own type.
- `scripts/accounts/accounts.py` — delete retained mesh-pinned server handling and server display lines.
- `scripts/lib/resume_message.py`, `scripts/lib/test_resume_message.py` — remove the `PausedSeat` branch and obsolete mesh command tests. Preserve pane-worker messages, Claude child resume and complete account-move messages. Keep `ResumePending` for normal moves.
- `scripts/message/send.py`, `scripts/message/test_send.py` — delete the pinned `MeshWorker` delivery retained by Phase 9.
- `scripts/agents/mesh_runs.py`, `scripts/agents/test_mesh_runs.py`, `scripts/agents/mesh_run_process.py`, `config/mesh_runs.conf` — Spec items 11 and 12. Delete the pinned `MeshCaller` and `codex_mesh.hold_message` delivery retained by Phase 9. Test worker close after the rename; keep live-run blockers and completed close behavior.
- `scripts/direct/verify.sh` — Spec item 11: the detached run call (`:1085`).
- `scripts/build_queue/scope_activity.py` — Spec item 11: the script-name recognition (`:146`).
- `commands/astra_review.md`, `docs/direct/write_prompt_contract.md` — Spec item 11: the command and prompt text (`astra_review.md:39-44`; `write_prompt_contract.md:40`, `:42`, `:55`), and <WorkerRuns/> in `commands/unit/direct.md`. Remove every branch that applies only to an existing `mesh` pin: in `commands/unit/direct.md` (the Phase 16 run handoff steps in <LongLivedWorkers/> included), `docs/direct/write_prompt_contract.md` (sections 2, 3 and 4, <PhaseTeam/>, <CoordinationBoard/>, <PhaseMesh/>) and `docs/direct/dual_review.md`, and the mesh folder-form note at `scripts/direct/board.sh:99`. Remove them before you regenerate the command mirror with `scripts/claude_to_codex/sync.py`.
- `docs/as-built/build-memory-admission.md`, `docs/as-built/staff-accessors.md` — Spec item 11: the docs (`build-memory-admission.md:489`, `:605`, `:809`; `staff-accessors.md:68`; also `herdr-adoption.md:353`).
- `scripts/production/test_rename_unit_processes.py`, `scripts/production/test_unit_lookup.py`, `scripts/direct/test_verify_verbs.py`, `scripts/direct/test_remove_run_scratch.py`, `scripts/direct/test_verify_token_detach.py`, `scripts/direct/test_mesh_tree_cancel.py`, `scripts/direct/verify_tree_cancel_test_support.py`, `scripts/direct/verify_token_wait_test_support.py` — Spec item 11: the tests and support files of the renamed names. `codex_mesh.py` (`:231`, `:235`, `:2316`) and `tmp_sweep.py` (`:857`) are named in their own entries.
- `scripts/production/staff.py`, `scripts/production/session_staff.py` — inspect staff membership and discovered backend evidence. Remove the `unit_runs.parent_transport` filter (`session_staff.py:233`); leave the other readers unchanged.
- `scripts/production/staff_sources.py`, `scripts/production/test_staff_sources.py` — inspect Phase 13's converted display; assign no replacement deletion again. Preserve `RecordedWorker | WorkerBackendUnavailable` and incomplete output.
- `scripts/message/roster.py`, `scripts/message/test_roster.py`, `scripts/direct/board_json.py`, `scripts/direct/test_board_json.py` — delete only Phase 13's retained mesh-pinned roster and server branches.
- `scripts/message/process_table.py`, `scripts/message/test_process_table.py` — delete only retained mesh start/follow recognition.
- `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/test_worker_owners.py` — delete only retained mesh roster and end/cleanup route branches.
- `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py` — delete only retained mesh roster guards, and rename the detached record folder guard. Phase 13 owns the other display deletions; retain its historical transcript reads.
- `docs/as-built/long-lived-workers.md`, `docs/as-built/herdr-adoption.md`, `docs/as-built/session-roster.md`, `docs/as-built/agent-registry.md`, `docs/as-built/shutdown-and-restart.md`, `docs/as-built/unit-board.md`, `docs/as-built/usage-ledger.md` — Reference → Cleanup list docs.

**Workers:** `2 writers` — `impl`: `assign.py`, `astra_review.py`, `PaneWorld`, the pane and worker launch integration, the detached modules, `verify.sh`, the config, the command text and their tests; `test` (opens as impl): the state consumers with `session_staff.py`, shutdown, accounts, messages, `workers.py`, lifecycle cleanup, the sweep guards, the docs and their tests. The two writers agree on the retained helper interfaces before they edit. Each file has one owner; a shared fixture edit goes through its owner.

**State:** deletes state on disk (listed).

**Constraints from prior phases:** Phases 3n and 6-14 supply the pane APIs and consumer changes, and Phase 17 the held-role and friend launch. Phase 3p deleted the review pool. Broad staff conversions and 4g are not prerequisites; the drain gate's proofs replace that blanket order. Phase 5 is not needed: cleanup edits whichever registry file name is current. Phase 10 kept `workers.end_unit` in `retire_unit.py` `stop_rest` for a mesh-pinned run only; that branch goes here, and `stop_resume.stop_unit_workers` is then the one path that stops a unit's workers.
- Phase 3h review (2026-11-01): `migrate_staff_ids.py` and its test go only after the live move, completeness and history proof.
- Phase 15 outcomes this phase keeps: `test_busy_worker_receives_no_prompt` and the done and idle settle tests; `test_a_worker_can_close_after_its_completed_pass_crosses_a_phase_boundary` and the confirmed-end tests. A new run keeps its usable worker pane after the selector and pin go.
- Phase 16: a retained unit worker with a matching policy reads its agent process launch environment only when it has no dispatch in the current run (`worker_panes.py:810-816`); dispatch history alone cannot prove the running environment after an in-place restart. When `UNIT_RUN_DIR` names another run, the worker restarts in place with the same pane and conversation. Keep this, and keep the tests at `scripts/herdr/test_worker_panes.py:811-887`. Before the drain gate, the director records one live run handoff that shows the unchanged pane and conversation. Removing `worker_transport` also removes the new-mesh-pin refusal (exit 2); `prepare_session.sh` already reads that exit, and the herdr default needs no new user surface.
- Phase 17: one pane launch in `assign.py` with targets `StaffWorker | ConsultationPane | DurableDesignRole`. Keep `side_pane.open` delivery `ReplyToCaller | TranscriptReply`: only a friend question carries the send.py reply line. Keep the consultation transcript wait `worker.consultation_reply` and the one receipt `worker.transcript_receipt`. A durable launch takes no `--cwd`; add no caller worktree override. `StartAtDispatch | OpenWorker` and `herdr.PaneNotRead` stay internal. `PaneWorld` is shared by test modules in accounts, shutdown, production, message and hooks: a fixture change runs their suites too. `scripts/direct/implement.sh` and `review.sh` are already deleted on sdlc (devtools 8i).
- Phase 18: an unproved consultation close exits 1, keeps the report and prints `Saved: <report path>` and `assign.sh: consultation close unproved: <reason>; pane <pane id> (<label>) stays open` to stderr. Keep both lines and `test_unproved_consultation_close_fails_and_preserves_the_report` when the mesh Astra branch goes.
- sdlc merge 2fc784207: `write_mesh_server_record` and `owned_server_record` start a real process and stop it at cleanup. In `test_astra_review.py` the record exits through the worker fixture, after the `PaneWorld` `os.kill` guard ends; keep that order when the mesh server record goes.
- Phase 21: every Codex pane launch passes `-c notice.hide_rate_limit_model_nudge=true` (`worker_panes._policy_arguments`), and `send.deliver_worker_text` submits with `Herdr.prompt(..., proof=SubmitStarts(...))`; a stall goes to the holder. Keep both when the mesh goes.
- Phase 22: `worker._dispatch_turn` completes a DONE dispatch whose only pending runs are finished, with one `anomaly: <worker>: run <run id> finished, but its result is not in the transcript` line per run. Keep it and its two `test_worker.py` tests when `mesh_runs.py` becomes `detached_runs.py`.
- Phase 23: `Herdr.wait_for_prompt_input` sends `ctrl+s` once when a ready Claude composer holds a draft (`PromptInputHoldsDraft`). Keep it in the one submit path.
- Phase 24: a worker's herdr agent is `<unit>-<worker>` (`worker_panes._start_locked`, `agent_name=`); `_agent_name` hashes a name above 32 characters. Keep the staff lookup on the session id and the worker name.
- Phase 25: a herdr launch with a phase runs `assign_claude.claim_phase` before `worker.py assign` (not for `review`). Keep the one claim call when the launch path changes.
- Phase 26: every unfinished run pins herdr; a finished run can still hold a `mesh` pin file. Phase 19 removes the reader, so no pin file is read after it.
- Phase 30: herdr adds no draft restore step after a submit; Claude keeps the draft itself. A draft lost in a stop and resume is the agents unit's fix.
- Phase 27: only a worker with an active dispatch reopens on restore, restart or a move; an idle worker stays closed until its next assign. Keep this rule when the mesh branch goes.
- Phase 33: `worker_pass` picks the newest matching pass by `started_at`, never the last in dict order. Keep this when the short mesh slots go.
- Phase 32: every launch destination runs `unalias <kind>` through `Herdr.run_in_pane` before `agent start`. Keep the one step when the launch path changes; a test fake pane shows an empty shell first.
- Phase 35: a pane worker's Codex launch carries no `model_auto_compact_token_limit`. Do not bring back an in-turn compaction option; `UNIT_COMPACT_ABOVE_TOKENS` is for compaction between turns only.
- Phase 37: `send.py` delivers to a herdr Codex pane through `CodexPaneRecipient` and `_prompt_pane`, by name and by `codex:<thread>`. When the `codex_mesh` route goes, a `codex:` address with no daemon and no pane still fails, and a staff row with no live entry still gets `QueuedStaffRecipient`.
- Phase 39: a Codex pane with no herdr `agent_session` gets its thread from `native_pane_process(...).current_identity` in `roster._herdr_codex_panes`. When the `codex_mesh` route goes, keep that fallback; a pane address does not need a daemon.
- Phase 38: an assign judges one worker with `worker_panes.observe_worker` over one full identity scan. When the `codex_mesh` route goes, keep that rule; the mesh selection must not filter panes by label.
- Phase 40: a Codex worker's thread comes only from its pane process, and `close --interrupt` quits before the worker lock. When the `codex_mesh` route goes, keep both; no lookup by tab name.

**Acceptance gate:** Verify that the devtools-unit 3t checkpoint is merged to sdlc before the final reference search.
Verify that its `implement.sh` and `review.sh` Codex branches are deleted.
Then search for live `codex_mesh`, `WORKER_TRANSPORT` and `mesh_runs` references; require no matches.
Require no live file, config or run folder name with the word mesh.
Require no retained mesh compatibility path.
Test worker startup after deleting `codex_mesh`; preserve each role's sandbox without importing the removed module.
Test after the rename: a detached `verify.sh` run, queue process recognition, scratch protection, completed-log readability and worker-close blocking.
Require basedpyright 0/0 on changed files and every caller module, except the `reportImportCycles` errors that exist at 890bf9e7e (21 over `scripts/direct scripts/astra scripts/agents scripts/message scripts/production scripts/herdr scripts/usage scripts/lib`, through `session_staff.py:113` -> `send.py` -> `roster.py`). Add no new cycle, and report the remaining count.
Keep the consultation failure tests `test_consultation_that_never_accepts_the_question_fails_and_closes_without_a_report` and `test_unproved_consultation_close_fails_and_preserves_the_report` green: exit 1, the pane closes, and a written report stays.
Require the full gate to pass.
Remove the code in one commit.
Prove interrupted ledger retirement recovers through a real stopped run.
Prove a completed rerun changes nothing and exits 0.
Add no ledger migration merely to delete the ledger.

**Acceptance gate (backend and resume consumers):**
Test a live backend, unavailable backend, and required historical conversation through the existing readers before ledger deletion.
Keep staff storage schema 2 and live output schema 3.
Search `resume_message.py`, its tests, and every caller for obsolete mesh launcher commands and `PausedSeat`.
Delete only obsolete branch tests; retain pane-worker, Claude child resume and complete account-move behavior tests.
Record added and deleted source lines for Phase 19 in its checkpoint message.
For each named pinned-path group, report its total added and deleted source lines through this phase.
Require a source decrease for each complete group.
Earlier phases record their own replacement deletions; count no deletion twice.
Delete no unrelated code to meet a count.

**Acceptance gate (one-time tools):** no live import or command names `worker_fill.py` or `migrate_staff_ids.py`; delete their command tests only with the tools, keep shared collector tests for consumers that remain. Native-id completeness and history proofs are recorded before deletion. No new ledger migration is added.

### Phase 20 — One launcher  · status: moved: devtools-unit (sdlc, 2026-11-05)

**Owner:** devtools-unit Phase 3t. sdlc ruling 2026-11-05: the duplicate-wrapper deletes (`implement.sh` and `review.sh` and their Codex branches) move to devtools 3t. herdr does no work in this phase.

**Depends on:** devtools-unit owns the order of its Phase 3t.
This moved work does not depend on herdr Phase 19.
Herdr Phase 19 requires its Codex-branch deletion checkpoint merged to sdlc.

#### Work Order

**Goal:** Use `assign.sh` and its `assign.py` implementation as the one launcher.
Merge `implement.sh` and `review.sh`, convert every repository caller, and delete the obsolete names.

**Spec:**

1. Agree the `scripts/direct` edit window with devtools-unit before editing shared launcher files.
2. Merge arguments, recorder, status-file, board, and `worker.py` calls into `assign.py` behind `assign.sh`.
3. Convert repository callers and command text to `assign.sh`.
4. Regenerate Codex mirrors from their source commands through `scripts/claude_to_codex/sync.py`.
5. Move existing behavior assertions into the retained assign launcher tests before deleting obsolete launcher test modules.
   Keep pass recording, admission, publication, review Stage 2, and board behavior.
6. Delete `implement.sh` and `review.sh` after their callers change.
7. Retain a compatibility entry only for a named caller outside this repository with a recorded owner.
   Generated mirrors do not require compatibility entry points.
8. Search resume messages and their tests with all other launcher callers.
9. Add no launcher name or wrapper layer.

**Files:**

- `scripts/direct/implement.sh`, `scripts/direct/review.sh` — deleted after their callers switch.
- `scripts/direct/assign.sh`, `scripts/direct/assign.py` — the one command; they take the merged work.
- `commands/unit/direct.md`, `commands/plan/phase_review.md`, `commands/showrunner/produce.md`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/inventory.py`, `scripts/agents/detached_runs.py`, `scripts/direct/progress_history.py`, `scripts/lib/unit_runs.py`, `scripts/lib/resume_message.py` — switch old launcher callers and text. Regenerate the command mirrors through `scripts/claude_to_codex/sync.py`.
- `scripts/shutdown/test_stop_resume.py`, `scripts/shutdown/test_inventory.py`, `scripts/direct/test_progress_history.py`, `scripts/lib/test_unit_runs.py`, `scripts/agents/test_detached_runs.py`, `scripts/lib/test_resume_message.py` — caller regressions.
- `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py`, `scripts/direct/test_assign_launcher.py` — the launcher tests.

**Workers:** `2 writers` — split by file group.
- `impl` — `scripts/direct/assign.sh`, `scripts/direct/assign.py`, `scripts/direct/progress_history.py`, and their tests.
  It merges and deletes `implement.sh` and `review.sh` with their obsolete launcher tests.
  hub: `scripts/direct/assign.py` (the one launcher implementation).
- `test` (opens as impl) — switch commands, shutdown, resume-message, detached-run, and unit-run callers.
  Regenerate mirrors and update caller tests.
  This writer leaves the launcher writer's files unchanged.

**State:** none new.

**Constraints from prior phases:** Devtools-unit owns the launcher merge order and deletes the `implement.sh` and `review.sh` Codex branches.
Do not wait for herdr Phase 19.
Phase 8 puts start, dispatch, wait, and receipt in `worker.py`.
Phase 9 dispatches review Stage 2 through `assign.py` with the dispatch id `<dispatch id>-2`.
Use `scripts/lib/atomic_file.py` for every added or changed file write.
Use `write_text_atomic`, or `sync_folder` for the folder step alone.
Add no private temporary-file, `fsync`, or replace sequence.
- Phase 3h review (2026-11-01): keep 3h's admission and publication behavior and the launcher's recorder, board and review behavior.

**Acceptance gate:** basedpyright 0/0/0 on `assign.py`, `progress_history.py`, and every changed caller module; `bash -n` and `shellcheck` with no new findings on the changed `.sh` files; the launcher tests green; full gate green. No rerun test: the merge is one commit, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

**Acceptance gate (one command):** Search code, commands, resume messages, tests, and regenerated mirrors.
No live caller names `implement.sh` or `review.sh`; list historical text separately.
All repository callers enter through `assign.sh`, which executes `assign.py`.
Search `resume_message.py` and its tests with the other caller files.
Keep no compatibility wrapper without a named external caller. The one launcher still records each pass once and uses `worker.py`; affected caller and command-mirror suites pass.

### Phase 21 — A detached run result reaches an idle pane worker  · status: done

#### As-built

- `worker_panes._policy_arguments`, Codex branch, passes `-c notice.hide_rate_limit_model_nudge=true` on every Codex pane launch and resume. Near the weekly limit, the Codex TUI otherwise shows a model-switch popup ("… for lower credit usage?") after a turn; typed text and Enter answer it, change the thread model and effort, and start no turn.
- `herdr.SubmitUnproved | SubmitStarts(timeout_ms)`, default `SUBMIT_UNPROVED`. `Herdr.prompt(pane, text, *, target, proof=...)` and `_prompt` add `--wait --until working --until blocked --timeout <ms>` for `SubmitStarts`; herdr then fails with `agent_prompt_stalled` when no turn starts within 5 s. Every other caller keeps `SubmitUnproved`.
- `send.deliver_worker_text` submits with `proof=SubmitStarts(<timeout ms>)`. A `HerdrFailure`, the stall included, goes to the holder fallback: `Worker <name>: <reason>` plus the text. `mesh_runs.deliver` does not change.

**Files:**
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — the nudge flag and its launch and resume assertions.
- `scripts/herdr/herdr.py` — the submit proof types and the `--wait` arguments.
- `scripts/message/send.py`, `scripts/message/test_send.py` — the proved worker message and the stall fallback test.

**Binds later work:** the mesh deletion keeps the nudge flag, `SubmitStarts` and the stall fallback.

**Gotchas:** `submit_opening_prompt` proves only Claude sessions: `TranscriptCursor` reads Claude `projects` files, and the composer parser and recipient check read Claude screens and the Claude session registry. A running Codex worker has the nudge flag only after its next launch or restart; the launch passes `--model`, so a restart also restores a model that the popup changed. `Herdr.run` caps each call at 10 s, and the 5 s stall fits inside it.

**Ruled out:** `submit_opening_prompt` for Codex workers (Claude-only proof); a retry or a transcript wait loop in the message path (sdlc: no new check).

### Phase 22 — A lost detached result does not hold a finished pass  · status: done

#### As-built

- `worker.DispatchWaitingForRuns` carries `reply: DispatchSubmitted | DispatchComplete | DispatchAwaitingReply`, the transcript receipt that `dispatch_receipt` read beside the pending runs.
- `_dispatch_turn`, at the quiet-wait return: when the receipt waits only on finished runs (`has_live_run` false), its `reply` is `DispatchComplete` and the worker is idle or done, it prints `anomaly: <worker>: run <run id> finished, but its result is not in the transcript` once per run to stderr and returns the reply. Every other case keeps `ExitCode.NO_DONE`. A live run still holds the pass.
- `transcript_receipt`, `mesh_runs.deliver`, `delivered_worker_results` and `live_runs` do not change.

**Files:**
- `scripts/production/worker.py` — the receipt field and the quiet-wait completion.
- `scripts/production/test_worker.py` — `test_done_with_a_result_absent_from_the_transcript_completes_after_its_run_finishes` (both subtests return OK with one anomaly line; the live subtest waits until its run finishes) and `test_a_reply_without_done_with_a_finished_run_absent_from_the_transcript_returns_no_done`.

**Binds later work:** the mesh deletion keeps the finished-run completion and its anomaly line when `mesh_runs.py` becomes `detached_runs.py`.

**Gotchas:** the decision comes after one quiet wait, so a result that is still on its way reaches the transcript first. A worker reply without DONE still returns `NO_DONE` with no anomaly line.

**Ruled out:** a new check, retry or wait (sdlc: "Add no new check"); hand edits of run records.

### Phase 23 — A draft in the composer does not block an opening prompt  · status: done

#### As-built

- `herdr.PromptInputHoldsDraft(screen)`: `Herdr.prompt_input_state` returns it when the pane is ready and its composer holds text. Every other result does not change.
- `Herdr.wait_for_prompt_input`, on `PromptInputHoldsDraft`, sends `ctrl+s` once per call (`self._send_keys(pane, "ctrl+s")`), then keeps waiting for the empty composer. A second draft in the same call waits to the deadline: "destination never became ready for input".
- Claude Code 2.1.319 binds `ctrl+s` to `chat:stash`. After a submit that is not a slash command, Claude puts the stashed draft back and shows "Draft restored". After a slash command, the draft stays in the stash; `ctrl+s` on an empty composer restores it.
- `check_target`, the "destination input contains other text" failures after the first typing, and the retype and Enter logic do not change. `submit_opening_prompt` stays the one submit path.

**Files:**
- `scripts/herdr/herdr.py` — the draft state and the one stash key.
- `scripts/herdr/test_herdr.py` — `test_opening_prompt_stashes_a_draft_once_before_typing_and_submission`, `test_a_second_draft_waits_until_the_deadline_without_a_second_stash`, `test_untracked_ready_pane_with_text_in_the_composer_holds_a_draft`; `test_other_text_in_the_composer_is_never_overwritten` now covers a failed stash.

**Gotchas:** herdr accepts the key name `ctrl+s` and sends byte 0x13; `ctrl-s` and `C-s` exit 1 with `invalid_key` (director proof, 2026-11-07). In a plain shell pane 0x13 is XOFF and stops the output until `ctrl+q`. A stash that already holds text is replaced.

**Ruled out:** a second submit path for drafts; a new check or retry (sdlc: "Keep one submit path. Add no new check.").

### Phase 24 — Long worker names stay unique agent names  · status: done

#### As-built

- `herdr._agent_name`: a mapped name of 32 characters or fewer does not change. A longer name becomes its first 23 characters, `-`, and the first 8 hex digits of the SHA-256 of the mapped name. It is the one place for every caller.
- `herdr.AgentNotStarted(PromptNotSubmitted)`: the `agent start` command failed, and no agent runs in the pane. `Herdr.start_agent` returns it from its `except HerdrFailure` branch. Callers that match `PromptNotSubmitted` keep their behavior.
- `launch_prepared_agent`, on `AgentNotStarted` into a `NewTab` or `SplitPane`, closes the pane that it opened and prints `herdr <name>:<pane>: agent start failed: <reason>; pane closed`. An `ExistingPane` stays open.
- `herdr.AgentNameSource.MEMBER_REFERENCE` and the `agent_name` keyword of `launch_prepared_agent` and `Herdr.launch`: the server agent name can differ from the staff reference. `worker_panes._start_locked` passes `agent_name=f"{owner.name}-{name.value}"`, so a worker's herdr agent is `<unit>-<worker>` (user, through sdlc, 2026-11-07). The pane label and the staff worker name do not change. Units, showrunners and friends keep their agent names. The other herdr agent calls address the agent by pane.

**Files:**
- `scripts/herdr/herdr.py` — the hash tail, `AgentNotStarted`, the pane close, `AgentNameSource`.
- `scripts/herdr/worker_panes.py` — the `<unit>-<worker>` agent name.
- `scripts/herdr/test_herdr.py` — the repinned name cases, the three verify names, the `SplitPane` close and `ExistingPane` keep.
- `scripts/herdr/test_worker_panes.py` — `test_each_parent_uses_its_name_for_the_server_agent_and_keeps_the_worker_name`.
- `scripts/production/test_worker.py` — `test_resume_workers_names_the_active_pass_and_idle_worker` compares without order: staff rows sort by id, and the fixture ids now come from the server agent name.

**Gotchas:** the fake backend id in `PaneWorld.launch` comes from the server agent name, so two parents with a worker `impl` get two backend ids.

**Ruled out:** a change to names of 32 characters or fewer other than the worker form; a refusal of a taken name (sdlc: "add no refusal").

### Phase 25 — A herdr launch claims its phase files before the worker opens  · status: done

#### As-built

- `assign_claude.claim_phase(run, phase, cwd, log, status, slot, env)` is the one place for the phase file claim. It runs `phase_reservation.py claim --session-dir <run> --phase <id> --repo <cwd>` and writes its output to the launch log. The claim writes `phase_baseline`, `phase_baseline_status` and the reservation state.
- A refused claim prints `ERROR: phase <id> could not claim its files before its worker opens; see <log>.`, writes `error` to the status file, posts a `blocked` board line and returns `False`. The launcher then exits 1, and no worker opens.
- `assign_claude._run` and `assign.execute_herdr` both call it. `execute_herdr` calls it for every role except `review`, after it writes the prompt files and before `worker.py assign` runs. A launch with no phase makes no claim.
- So the checkpoint backstop `record-active` finds the phase baseline for a herdr phase.

**Files:**
- `scripts/direct/assign_claude.py` — `claim_phase`; `_run` calls it.
- `scripts/direct/assign.py` — `execute_herdr` calls it.
- `scripts/direct/test_assign_launcher.py` — `test_herdr_assignment_claims_the_phase_before_the_worker_assignment`, `test_herdr_assignment_refuses_a_failed_phase_claim_before_the_worker_assignment`, `test_herdr_review_assigns_the_worker_without_a_phase_claim`.

**Ruled out:** a change to `phase_reservation.py`; a new check, retry or wait.

### Phase 26 — Live runs move to pane workers  · status: done (operation)

#### As-built

- An operation, not a code change (user, through sdlc, 2026-11-07: "this should be able to happen right now"). The one-off program ran from the director's scratch folder on natedev. No repository file holds it, so Phase 19 has no program to delete.
- For each unfinished run that pinned mesh, the program read the roster seats against the staff worker rows, and changed only a quiet run: no live launcher and no live detached run. Under `worker_transport.lock` it wrote `herdr` into `worker_transport`, then ended the run's app-servers with the non-pause `codex_mesh.py stop`. The staff worker rows and the Codex rollouts stay, so each worker resumes its same thread in a pane at its next assign.
- The director ran it again until the last pass printed `pass: 0 moved, 0 app-server stops, 1 mesh pins remain on finished runs`. A finished run keeps its old pin; Phase 19 removes the pin file reader.
- Spec item 10 (a `commands/unit/direct.md` line) is not written: Phase 19 removes the mesh text of <LongLivedWorkers/> that it would qualify.

**Ruled out:** a code-only switch of the pin under a live run (`mesh_runs.deliver` reads the pin when a detached run delivers).

### Phase 27 — Only working workers hold a pane  · status: done

#### As-built

A pane holds a worker only while it works. After a director accepts a pass and starts no fix round or next dispatch at once, it closes the pane with `~/.claude/scripts/lib/py ~/.claude/scripts/herdr/worker_panes.py close <name>`. The close keeps the staff row and the thread; the next `assign.sh --to <name>` resumes that thread in a new pane on the current role account. A busy worker or a live detached run keeps its pane until that work ends.

- `worker.restore` (`scripts/production/worker.py`) reopens only a worker with an `ActiveDispatch` from `progress_history.active_dispatch`, through `_resume_launcher`; an idle worker stays closed. `ActiveDispatchUnavailable` prints an anomaly and makes the result `UNCERTAIN`. The shutdown restart and `pane_restore` reach this same function.
- `pane_moves._move_locked` (`scripts/accounts/pane_moves.py`) moves only a worker with an active dispatch. It closes an idle worker's open or stopped pane and returns `nothing to move`.

**Files:**
- `scripts/production/worker.py` — `restore` with the active-dispatch rule.
- `scripts/accounts/pane_moves.py` — the move rule for idle workers.
- `commands/unit/direct.md`, `commands/unit/worker.md` — the close-after-accept rule.
- `docs/as-built/herdr-adoption.md`, `docs/as-built/long-lived-workers.md` — the same rule.
- `scripts/production/test_worker.py`, `scripts/accounts/test_accounts.py`, `scripts/shutdown/test_pane_restore.py`, `scripts/shutdown/test_restart.py` — busy and idle restore and move cases.

**Gotchas:** a test that expects a worker to reopen needs an active dispatch with a `launcher_command` (`progress_history.begin_dispatch`) and a patched `worker._resume_launcher`; without a live director session, `_resume_launcher` prints "director session is unavailable".

### Phase 28 — A director answers a worker's question in its pane  · status: todo

**Depends on:** Phase 27 and Phase 29 (they edit `scripts/production/worker.py`, `commands/unit/direct.md` and `docs/as-built/herdr-adoption.md`). It lands before Phase 19 (sdlc, 2026-11-07).

The user decided worker question item 9 (through sdlc, 2026-11-07): "When a Codex worker stops on a question (any question, such as request_user_input for a file outside its paths), the unit director may use herdr to type an answer into the worker's pane, and the worker continues. No prompt ban." Today a worker that stops on a question has herdr status `blocked`. The dispatch path returns exit 6 (busy) when it finds a blocked agent before submission (`scripts/production/worker.py:338-341`), and exit 4 (blocked) when the wait sees it (`:372`, `_delivery_failure` `:395`). Neither exit names the question, and the director has no command to answer it.

#### Work Order

**Goal:** a worker's question reaches the director with its text and pane. The director answers it with one command. The worker continues, and the pass completes through the existing launcher.

**Spec:**

1. A blocked worker gives exit 4 (`ExitCode.BLOCKED`) on every path, including the pre-submission path at `worker.py:338-341`, which returns exit 6 today. Add no new exit code.
2. Before it returns exit 4, `worker.py` prints `question: <worker> pane <pane>` and then the question text: the pane's last visible lines through the existing `Herdr.read` (`herdr.py:2427`). It prints no other new line.
3. One new command: `worker.py answer <name> --text "<answer>"`. It types the text into that worker's pane and submits it, through the existing herdr pane input path that detached results use. It refuses only a worker that is not this parent's, with the existing refusal text.
4. After the answer, the director reruns the saved launcher command (`launcher_command` in `<run>/progress_history_state.json`). The rerun reads the transcript, does not type the dispatch again, and waits for the `DONE <dispatch id>` reply. That is today's rerun; do not add another wait.
5. `commands/unit/direct.md` <LongLivedWorkers/> and `commands/unit/worker.md`: exit 4 now means the worker asks a question or is blocked. Read the `question:` lines, answer with `worker.py answer`, then rerun the saved launcher command. Remove the exit 6 wording for a blocked worker. `docs/as-built/herdr-adoption.md` says the same.
6. Add no check, refusal or validation that this list does not name.

**Files:** `scripts/production/worker.py`, `scripts/production/test_worker.py`, `commands/unit/direct.md`, `commands/unit/worker.md`, `docs/as-built/herdr-adoption.md`.

**Workers:** `1 writer`.

**Acceptance gate:**
- `scripts.production.test_worker` passes. New tests: a blocked agent before submission gives exit 4 and the `question:` lines; `worker.py answer` types the text into the right pane and refuses another parent's worker; a rerun after the answer submits nothing and completes on the DONE reply.
- basedpyright on the two Python files reads `0 errors, 0 warnings, 0 notes`.
- The full gate passes.
- Director proof: in a test run under `/home/natepiano/scratch/herdrcheck/`, a worker asked to edit a file outside its paths stops on the question; the director answers through `worker.py answer`, reruns the launcher, and the pass completes.


### Phase 29 — A worker's run is the run of its own dispatch  · status: todo

**Depends on:** Phase 27 (it edits `scripts/production/worker.py` and `scripts/accounts/pane_moves.py`, which call `worker_run`). It lands before Phase 28 and Phase 19 (herdr, 2026-11-07: a live defect).

aws-unit, 2026-11-07 (through sdlc [4c690675]): a dispatch in run 8bf65047 exited 3 while its worker kept working, the worker's detached runs went to run 2b37ffed, and `worker_panes.py close` then refused with "launcher has not recorded a completed pass". One cause: `worker_panes.worker_run(parent)` (`scripts/herdr/worker_panes.py:431`) returns `unit_runs.held(...)[0]`, the director's first held run in folder-name order. A director with two run folders gets the wrong one for every caller: `mesh_runs.caller_from_environment` (`scripts/agents/mesh_runs.py:178`), `worker.py restore`, `worker_panes.close`, and the account moves (`pane_moves.run_for_worker`). The existing index `session_staff.worker_run_index(record)` (`scripts/production/session_staff.py:212`) already maps each worker row to the runs that hold its passes.

#### Work Order

**Goal:** each caller reads a worker's run from that worker's own dispatch, never from the order of run folders.

**Spec:**

1. `worker_run` takes the worker row. It returns the run of the worker's newest pass from the existing worker-to-run evidence (`session_staff.worker_run_index` or the reader under it). Do not add a second index.
2. A worker with no pass in any run keeps today's answer (the director's first held run, else the fallback folder). `start_or_resume` picks a run before a new worker row exists: keep a parent path for `NewWorkerSlot`, and use the worker evidence only for an existing row.
3. Change every caller of `worker_run` to pass the worker row: `mesh_runs.caller_from_environment`, `worker.py restore`, `worker_panes.close` (its `HolderRun.CURRENT` default), `pane_moves.run_for_worker`, and each other caller that `grep -rn "worker_run(" scripts` finds. A caller that asks only about the director's own run (such as the transport checks in `stop_resume.py` and `restart.py`) keeps the parent form; name each such caller in your summary (herdr, 2026-11-08).
4. Add no check, refusal or validation that this list does not name.

**Files:**
- `scripts/herdr/worker_panes.py`
- `scripts/herdr/test_worker_panes.py`
- `scripts/agents/mesh_runs.py`
- `scripts/agents/test_mesh_runs.py`
- `scripts/production/worker.py`
- `scripts/production/test_worker.py`
- `scripts/accounts/pane_moves.py`
- `scripts/accounts/test_accounts.py`
- `scripts/message/send.py`
- `scripts/message/test_send.py`
- `scripts/production/retire_unit.py`
- `scripts/production/test_retire_unit.py`
- `scripts/shutdown/restart.py`
- `scripts/shutdown/test_restart.py`
- `scripts/shutdown/stop_resume.py`
- `scripts/shutdown/test_stop_resume.py`
- `scripts/production/staff_test_support.py`

**Workers:** `1 writer`.

**Acceptance gate:**
- New tests: a director with two runs whose names sort with the worker's run second; a first admission of a new worker while the director holds two runs; a detached run start routes to the worker's run; close reads the completed pass there and closes; restore and a move read the active dispatch there.
- The changed test modules pass; basedpyright on the changed Python files reads `0 errors, 0 warnings, 0 notes`.
- The full gate passes.


### Phase 30 — A stashed draft comes back after a delivered prompt  · status: done

#### As-built

`Herdr.submit_opening_prompt` adds no draft restore step. A live measurement of Claude Code 2.1.319 in 7 cases with no stop (idle, generating, SessionStart hooks, a running tool, two queued prompts) shows that Claude keeps or restores a stashed draft by itself after a submit. `PromptSubmissionTests.test_proved_submission_returns_without_waiting_for_the_turn_or_restoring_the_stash` proves that a proved submission returns at once: it does not wait for the turn and does not send `ctrl+s` again.

**Files:**
- `scripts/herdr/test_herdr.py` — the submission test above.

**Gotchas:** a stop and resume loses the draft: the SIGTERM in `stop_resume._stop` (`scripts/shutdown/stop_resume.py:332`) ends the Claude process that holds it. The agents unit owns that fix (its pause and exit phase); herdr adds only a pane-read helper if that unit asks for one.

**Ruled out:** a `ctrl+s` restore after a proved submit — it made each delivery wait for the turn end.

### Phase 31 — A message to a pane worker goes to its pane  · status: todo

**Depends on:** Phase 29 and Phase 28 (it runs after 28: hierarchies Phase 30 waits on 28 and 29; herdr, 2026-11-08 [0124959b]). Phase 19 waits for this phase.

sdlc reported a defect from hana (2026-11-07): `send.py --codex` to an idle pane worker fails with exit 3, `FAILED: codex_mesh.py send: codex_mesh: tool-based-ui-demo-fix2 is ended; message not delivered`. `send.py` (`codex`, `scripts/message/send.py:2234`) still sends every Codex message through `codex_mesh.py send`, which reads the old mesh roster entry as ended. sdlc's rule: `send.py` delivers to a pane worker through the one pane delivery function that assign uses, with no second copy. The `codex_mesh` route goes with Phase 19.

The second case (a Codex session in a pane, such as morty) moved to Phase 37 (sdlc [787d4c6f], 2026-11-07: start it now). This phase reuses the pane recipient that Phase 37 adds.

**Constraint from Phase 37:** `send.py` has one pane submit helper, `_prompt_pane(place, text, *, target, timeout)` (herdr `prompt` with `SubmitStarts`); `deliver_worker_text` and `CodexPaneRecipient` both call it. Route a pane worker's `--codex` message through `deliver_worker_text` or that helper; add no second submit. A staff row with no live roster entry resolves to `ClaudeRecipient` only when the notifier registry holds its running Claude record; otherwise to `QueuedStaffRecipient`. An explicit `codex:` address with no live daemon or pane fails before that queue.

**Constraint from Phase 32:** every launch destination runs `Herdr.run_in_pane`, which waits for an empty shell. A test fake pane shows an empty shell before the agent starts, or the test patches `Herdr.run_in_pane` in setUp.

The Work Order is written at the phase start.

### Phase 33 — A worker's newest pass decides its close  · status: done

#### As-built

`worker_pass` (`scripts/herdr/worker_panes.py:450`) returns the matching recorder pass with the largest `started_at`. It still matches on `worker_name` or `team_slot`. A pass with no number for `started_at` counts as the oldest. So an older pass under a pre-handoff short slot (for example `test`) no longer hides the newer pass under the full worker name, and `close` accepts an idle worker after a mesh handoff.

**Files:**
- `scripts/herdr/worker_panes.py` — `worker_pass`.
- `scripts/herdr/test_worker_panes.py` — `test_close_uses_the_newest_pass_when_an_older_short_slot_is_last`, with subtests for a numeric, a missing and a text `started_at`.

**Gotchas:** dict order in the recorder `pass` map is insertion order, not start order; never take the last match.

### Phase 35 — A long worker thread does not compact in a loop  · status: done

#### As-built

A pane worker's Codex launch has no in-turn compaction option. `worker_panes._policy_arguments` passes `--model`, `--sandbox`, `--ask-for-approval never`, `-c model_reasoning_effort=…`, `-c notice.hide_rate_limit_model_nudge=true` and the service tier; Codex keeps its own full-window compaction limit. Cause: the old `-c model_auto_compact_token_limit=<UNIT_COMPACT_ABOVE_TOKENS>` made a long thread whose floor after compaction was near 100,000 tokens compact after each tool call (demo's `tool-based-ui-demo-test`, 2.5 hours with no edit). `LaunchPolicy` holds `settings`, `sandbox` and `permission`; `ClaudeCompaction` and `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS` are gone. `UNIT_COMPACT_ABOVE_TOKENS` stays for idle mesh-worker compaction (`assign.py`). No between-turn compaction runs for pane workers until Phase 34 item 5.

**Files:**
- `scripts/herdr/worker_panes.py` — `LaunchPolicy`, `_launch_policy`, `astra_review_policy`, `_policy_arguments`.
- `config/direct.conf` — the `ASTRA_REVIEW_COMPACT_ABOVE_TOKENS` block is removed.
- Tests: `scripts/herdr/test_worker_panes.py`, `test_side_pane.py`, `scripts/astra/test_astra_review.py`, `test_design_roles.py` check that the option is absent.
- Docs: `commands/unit/direct.md` (LongLivedWorkers), `docs/as-built/long-lived-workers.md`.

### Phase 37 — A message reaches a Codex session in a pane  · status: done

#### As-built

`send.py --to morty` and `send.py --to codex:<thread>` deliver to an interactive Codex session in a herdr pane. `Herdr.agents` decodes each agent's `agent_session` into `ReportedAgentSession(session_id)` (kind `id`) or `NoReportedAgentSession`; a malformed report raises. `roster._herdr_codex_panes` reads `worker_panes.agent_inventory`, and a Codex pane with a reported id becomes `Messageable("codex:<thread>")` with `thread=CodexThread(<thread>)` and `pane=HerdrPlace(session, pane)`; the snapshot keeps `pane`. A pane with no reported id stays `CannotReceiveMessages`. A staff row matches that entry through its thread in `_staff_transport_matches`.

`CodexPaneRecipient(address, place, thread_id)` delivers through `_prompt_pane(place, text, *, target, timeout)`, the one herdr `prompt` call with `SubmitStarts`; `deliver_worker_text` uses the same helper. A herdr error reports `FAILED: <error>`.

`resolve_recipient` order after no roster match:
1. `worker:` → usage error.
2. `codex:` → `RecipientLookupFailed`; an explicit `codex:` address with no live daemon or pane keeps its failure.
3. A staff row → `ClaudeRecipient("session:<id>")` when the notifier registry holds its running Claude record; else `QueuedStaffRecipient`, which prints `QUEUED: no live Claude or Codex session answers to <name> (<id>), so it is kept for it` and keeps the same `session-<id>` queue file.
4. `session:` → `ClaudeRecipient`.

Live check on 2026-11-07 17:00 EST: both sends printed `SENT: codex:01a18dbb-… at agents:w2C:p1`, and both texts showed in morty's pane.

**Files:**
- `scripts/herdr/herdr.py` — `ReportedAgentSession`, `NoReportedAgentSession`, `AgentOccupant.agent_session`, the decode in `Herdr.agents`.
- `scripts/message/roster.py` — `NoHerdrPane`, `RosterEntry.pane`, the pane thread in `_herdr_codex_panes`, snapshot `pane`.
- `scripts/message/send.py` — `CodexPaneRecipient`, `QueuedStaffRecipient`, `_prompt_pane`, the fallback order.
- `scripts/production/rename_test_support.py` — the fake agent row carries `agent_session`.
- Tests: `scripts/herdr/test_herdr.py` (session decode, absent and malformed reports), `scripts/message/test_roster.py` (pane thread and snapshot pane), `scripts/message/test_send.py` (`test_codex_pane_agent_resolves_by_staff_and_thread_and_submits_once`, `test_codex_pane_failure_reports_the_herdr_error`, `test_absent_staff_recipient_keeps_its_queue_until_claude_returns`).

**Binds later work:** a message to a pane worker goes through `_prompt_pane` or `deliver_worker_text`; add no second submit. A test fake for `Herdr.agents` rows sets `agent_session`.

**Gotchas:** roster test fakes omit live Claude rows, so a staff fallback change needs a full `test_send` run, not selected tests.

### Phase 39 — A message reaches morty when herdr reports no session · status: done

#### As-built

`roster._herdr_codex_panes` addresses a Codex pane that `herdr agent list` reports without an `agent_session`. For that pane it reads `herdr.Herdr(<session>).native_pane_process(<pane>, "codex", herdr.PROGRAM_TIMEOUT_SECONDS).current_identity`, the same identity that `check_target` proves before delivery. The entry becomes `Messageable("codex:<thread>")` with `thread=CodexThread(<thread>)` and its `pane`. A reported session wins and costs no process read. A read that raises `HerdrFailure`, `OSError`, `ValueError` or `subprocess.SubprocessError` (a 10-second timeout included) leaves the entry `NoCodexThread` and `CannotReceiveMessages`; the rest of the roster still builds.

Live check on 2026-11-07 22:56 EST: herdr reported no session for morty's pane w2C:p1, and `send.py --to morty` and `--to codex:01a18dbb-…` both printed `SENT: codex:01a18dbb-… at agents:w2C:p1`.

**Files:**
- `scripts/message/roster.py` — the process-identity fallback in `_herdr_codex_panes`.
- Tests: `scripts/message/test_roster.py` (`test_codex_pane_without_a_session_report_uses_its_process_identity`: absent report, failed read, reported session with no process read), `scripts/message/test_send.py` (`PaneWorkerMessageTests.codex_agent` builds its entry through the roster with no session report).

**Binds later work:** code that needs a Codex pane's thread reads `native_pane_process(...).current_identity` when `agent_session` is absent; it never relies on `agent_session` alone.

**Gotchas:** herdr gives no `agent_session` for some resumed Codex panes, such as morty's pane and a resumed worker's new pane. Messages queued for a Codex staff row before this fix stay in its `session-<id>` queue file; no hook drains them.

### Phase 40 — A pane worker can always be closed and followed · status: done

#### As-built

`worker_panes.py close` closes an idle worker whose last pass is canceled, and a worker whose pass still reads `active` when its transcript ends with the dispatch DONE line (`_completed_turn`); the done-worker role change needs that case. A worker in `labelled recovery pane runs other work` is closed through `_close_target`: plain close makes every refusal check (lock, idle checks, live detached runs) first, then sends `/quit` through `Herdr.quit_agent`, then closes. `close --interrupt <name>` sends `/quit` first, without the worker lock, because a following launcher holds that lock until the turn ends; it then takes the lock, blocking, and closes. The row and thread stay.

A Codex launch reads its thread only from its own process (`native_process(pid, "codex")`). `Herdr.codex_client_session`, which read the staff row named by the tab, is gone; it bound new workers to another row's thread (the `01a1b` reports from cargo-liner). `pane_record.seed` uses the same process read. Under `launch_rollout_proof`, `native_process` does not fall back to a herdr-reported session.

`_dispatch_turn` reads the dispatch receipt before the busy check, so a prompt delivered during the settle keeps its follower and never exits 6. `_start_locked` lets a done worker take another role. `worker.py restore` skips open workers. `mesh_runs.seat_name` resolves a herdr slot through its active pass first (`worker_name`, `worker_id`), then by name. `phase_reservation` claims read the recorded phase checkout.

**Files:**
- `scripts/herdr/worker_panes.py` — `_completed_turn`, `_close_target`, `close(..., interrupt=)`, `close --interrupt`.
- `scripts/herdr/herdr.py` — `Herdr.quit_agent`; `_codex_launch` reads the process only.
- `scripts/production/worker.py` — receipt before the busy check; restore skips open workers.
- `scripts/agents/mesh_runs.py` — `seat_name`.
- `scripts/direct/phase_reservation.py` — `_claim_command` reads the recorded checkout.
- `scripts/shutdown/pane_record.py` — `seed` reads the pane's process.
- Tests: `test_worker_panes.py`, `test_herdr.py`, `test_worker.py` (`test_a_prompt_delivered_during_settle_keeps_its_follower`, `test_done_worker_can_take_a_fix_while_its_recorder_pass_is_active`), `test_mesh_runs.py`, `test_phase_reservation.py`, `test_pane_record.py`.

**Binds later work:** a stop of a followed turn acts before the worker lock; every other close path checks before it quits. A thread comes from the pane's own process, never from a staff row found by tab name. Linux process fixtures set `HERDR_PLATFORM=linux`.

**Gotchas:** live check 2026-11-08 02:17 EST: `close --interrupt` on a working reviewer printed `done` in 7 s, and its follower exited 3.

### Phase 41 — A pane worker compacts at the cheaper limit · status: todo

**Depends on:** Phase 40 (both edit the pane worker launch). sdlc [6ed41bd7], [82aaadcb], 2026-11-07.

Report: A pane worker's Codex launch sets the auto-compact limit from config, near 200k tokens, well above the post-compaction floor (about 100k). Report the value and the measured floor to sdlc. Cost measure (natedev via sdlc [82aaadcb], 2026-11-07): since Phase 35, pane workers compact near the full 500k window; a fix worker ran 61 requests between 250k and 343k tokens. Measure quota per request at a 200k limit and at full-window compaction, put the cheaper point in config, and send sdlc both numbers with the hash.

The Work Order is written at the phase start.

### Phase 38 — One worker's assign reads only its own pane and row · status: done

#### As-built

An assign judges only the worker it uses. `worker_panes.observe_selected(parent, record, names)` and `observe_worker(parent, row, record)` read the pane list once and run one identity scan over all panes, so a relabelled worker pane and the parent pane are still found. `_parent_evidence(..., selection=SelectedWorkers(names) | AllWorkers.CURRENT)` limits only the judgment. For a selected row, an unreadable pane makes absence unproved only when the pane carries that worker's label (`<name>` or `<parent>-<name>`) in the parent's tabs. Other sessions' unreadable panes and closed sibling rows do not change the selected row's state or the exit code. `observe_worker` returns `WorkerNotObserved.CURRENT` when the row is gone.

`observe` keeps the full per-row rule, so `worker.py list` and `restore` still show a sibling's uncertain state on its own line. `select_worker` judges every candidate from one read. `_dispatch_turn` and the second dispatch stage use `observe_worker`. A named assign does not open the default team. `PaneEvidence.layout_terminals` carries the labelled sibling panes in the parent's tabs, so `_layout` and `_destination` keep the column split even.

`herdr.launch_rollout_proof()` sets a `RolloutProofPhase.LAUNCH` context. Inside it, `codex_rollout_threads` accepts rollout descriptors that open before its deadline; `_start_locked` wraps the launch identity read and the fresh evidence read with it. Outside it, a new descriptor before the final listing still raises `current Codex rollout or process changed`.

**Files:**
- `scripts/herdr/worker_panes.py` — `SelectedWorkers`, `AllWorkers`, `WorkerNotObserved`, `observe_selected`, `observe_worker`, `PaneEvidence.layout_terminals`.
- `scripts/herdr/herdr.py` — `RolloutProofPhase`, `launch_rollout_proof`, the launch case in `codex_rollout_threads`.
- `scripts/production/worker.py` — `select_worker`, `_dispatch_turn` and `dispatch` read one worker; `assign` opens the team only with no name.
- Tests: `scripts/production/test_worker.py` (`test_named_assign_depends_on_its_own_pane_and_keeps_sibling_list_states`, `test_named_assign_resumes_while_its_codex_rollout_opens`, one pane-list read per automatic selection), `scripts/herdr/test_worker_panes.py` (`observe_worker` equals `observe` for a relabelled pane), `scripts/herdr/test_herdr.py` (`test_launch_proof_accepts_a_rollout_descriptor_opening_during_the_read`).

**Binds later work:** code that acts on one worker judges it with `observe_worker`; `observe` stays the conservative rule for list and restore. Pane selection never filters panes by label before the identity scan.

**Gotchas:** a test that intercepts the dispatch state read patches `worker_panes.observe_worker`, not `observe`.

**Ruled out:** filtering panes by label or session before the scan — it missed a relabelled live worker and picked another unit's same-named pane.

### Phase 32 — A launch keeps the caller's Codex home  · status: done

#### As-built

`launch_prepared_agent` (`scripts/herdr/herdr.py`) runs `unalias <kind> 2>/dev/null;` through `Herdr.run_in_pane` in the destination pane before `agent start`, for an `ExistingPane`, a `NewTab` and a `SplitPane`. An `ExistingPane` also gets the environment exports in the same script and its terminal. Cause: the pane's interactive zsh has `alias codex='… accounts.py exec workers'`, and `accounts.exec_account` sets `CODEX_HOME` to the workers account. Before, only `ExistingPane` removed the alias, so a new tab or split started Codex on the workers account. A live proof ran in `zz-proof-codexhome` with its folders under `~/scratch/herdrcheck/codexhome-*` (13:17 and 13:20 EST).

**Files:**
- `scripts/herdr/herdr.py` — `launch_prepared_agent`.
- `scripts/herdr/test_herdr.py` — the `NewTab` and `SplitPane` launches remove the alias through the same step.
- Test fixtures: `scripts/production/add_showrunner_test_support.py` and `add_unit_test_support.py` make a fake zsh process for each new pane (`STUB_STATE/pane-shell`) and report the agent only when its parent is that shell. `scripts/herdr/test_herdr_sessions.py`, `scripts/lib/test_nix_env_launches.py`, `scripts/shutdown/test_pane_restore.py`, `test_restart.py` and `scripts/production/test_add_showrunner.py` patch `Herdr.run_in_pane` in setUp.

**Gotchas:** `run_in_pane` waits until the pane shows an empty shell. A fake pane that answers `pane process-info` with the agent already in the foreground fails with "would send keys to … other live work".

### Phase 34 — A worker's turn end tells the director; no shell waits  · status: todo

**Depends on:** Phase 28 (the question path), and the agents unit's phase 15b on sdlc (sdlc [4bc87048], 2026-11-07: 15b owns `scripts/production/worker.py` and its test until it merges; build this phase's `worker.py` edits on it). sdlc accepted both launch-line hook calls. Phase 19 moved to hierarchies-unit and does not wait for this phase (sdlc [e5bdea18], 2026-11-07): hierarchies moves the Stage 2 file watchers out of `codex_mesh` under its Spec item 5. Build this phase's wait-loop deletion on that move.

The user (2026-11-07, through sdlc [4fdbe4b7]): "yes i definitely want this - keeping a sahell around to watch is inelegant and doesn't satisfy my beauty requirements". sdlc's design:

1. A turn-end hook saves the worker's result and sends the director a message. For Codex it is the `notify` program, given on the launch command line (`-c notify=[...]`), not in `~/.codex*/config.toml`. A Claude worker gets the same through its Stop hook, given at launch with `--settings`, not in a settings file.
2. A turn that ends without the dispatch `DONE` line is a question: the same hook sends the director the question (herdr pane state confirms the worker waits).
3. `assign.sh` types the job into the pane and returns at once. No shell waits on the worker.
4. Every caller moves at once; no record keeps the waiting-shell mode. Delete the wait loop in this phase (`worker._wait_for_dispatch` and `_wait_for_ready`, `scripts/production/worker.py:290-316`).
5. **Compact an idle worker before new work (Phase 35).** Before `worker.dispatch` types a job, compact a Codex worker whose recorded context is above `UNIT_COMPACT_ABOVE_TOKENS` (`config/direct.conf:65`), between turns. Phase 35 removed the in-turn limit that made long threads compact in a loop.
6. **The pass rule of Phase 22 (C1210) under the hook (user, through sdlc [41b8d0c7], 2026-11-07).** A `DONE` pass whose only open runs are finished completes, with one `run <id> finished, but its result is not in the transcript` line.
    1. The hook decides that a pass is done with the one rule in `worker._dispatch_turn` (`dispatch_receipt`). Call it; do not write a second copy.
    2. A turn end with `DONE` while a background run still runs is not a result yet. The run's own completion checks the pass again when it finishes.
    3. A finished run whose result never reached the transcript puts the anomaly line in the director's message, and the pass completes.
    4. One test for each of 6.1, 6.2 and 6.3.

7. **Submission and completion are two outcomes (plan review after Phase 32).** Today `assign.py` reads a successful dispatch return as completion, and its Astra path ends the worker and releases the role. Give submission and completion named outcomes. The completion actions (finish the pass, end or release a held role) move to the hook. Tests: after submission returns, the pass stays active and the role stays held.
8. **One completion rule (plan review after Phase 32).** The C1210 exception is in `_dispatch_turn` after a quiet wait, not in `dispatch_receipt`, which returns `DispatchWaitingForRuns` when a finished run's result is absent. Extract one completion decision before the wait is deleted. The hook and a detached run's completion both call it; 6.4's tests go through both.
9. **Default policy is a named state.** `worker.dispatch` and `worker_panes.start_or_resume` take `LaunchPolicy | None`, where `None` means the role's configured policy. When this phase changes those signatures, replace `None` with a named state (for example `ConfiguredRolePolicy`).

**Constraint from Phase 39:** herdr gives some resumed Codex panes no `agent_session`. Read a pane's thread through `Herdr.native_pane_process(...).current_identity` when the report is absent; do not rely on `agent_session` alone.

**Constraint from Phase 32:** every launch destination runs `Herdr.run_in_pane`, which waits for an empty shell. A hook launch fixture shows an empty shell before the agent starts, or the test patches `Herdr.run_in_pane` in setUp.

**Restructure (sdlc [a3ae4ca3], 2026-11-07):** fix-rounds-unit cuts `commands/unit/direct.md` to a core and moves mode-only sections to files read on demand. Merge sdlc after it lands, then put this phase's `direct.md` edits into the new layout.

**Files (first list; the Work Order at the phase start settles it):** `scripts/production/worker.py` and `test_worker.py`; `scripts/direct/assign.py`, `assign.sh`, `progress_history.py` and their tests; `scripts/herdr/worker_panes.py` and `test_worker_panes.py`; one new turn-end hook script in `scripts/herdr/` with its test; `commands/unit/direct.md`, `docs/direct/launch_implementation.md`, `docs/direct/dual_review.md`; `docs/as-built/long-lived-workers.md` and `herdr-adoption.md`.

## Reference

### Job table (main at 7e85a74b)

| # | Job today (codex_mesh.py unless named) | After |
| --- | --- | --- |
| 1 | App-server per run (`serve`, `idle-stop`, `sweep`) | Drop |
| 2-3 | `start`, `follow` (:3308, :3408) | `start-or-resume` + `dispatch` (Phases 7-8) |
| 4, 15 | Follow and slot claims (:3762; `impl_claim_*.json`) | Per-worker dispatch lock |
| 5-6 | Compaction, "Continue where you stopped." | Drop (decision 8) |
| 7 | Server retry | A failed start leaves its labelled pane at its shell; a rerun starts the agent once more in that same pane (`ExistingPane`, Phase 7) |
| 8 | `signin-changed` (:7673) | Phase 6 probe; positive → drop, else resume restart |
| 9 | `send`, `pending.json` | Decision 6 (Phase 9) |
| 10-11 | `steer`/`interrupt`, `end` | `herdr agent send-keys <id> esc`; `close` |
| 12 | `stop --pause` | Shutdown stops panes (Phase 10) |
| 13-14 | `reopen`/`adopt`; unit worker ledger (workers.py:422-545) | `start-or-resume`; the staff record |
| 16 | Pass recording in launchers | Keep |
| 17 | `mesh_runs.py` | `detached_runs.py` (Phase 19) |
| 18 | Wait markers | Drop markers; recorder waits stay |
| 19-20 | Usage limits; account moves | Phases 9 and 11 |
| 21 | Model per turn (710a6adb) | Lookup at start or resume only; a `/model` by hand holds (decision 2) |
| 22-23 | Review pool; unit reviewers | The pool is deleted (Phase 3p); each unit's `review` and `review2` workers are panes |
| 24-25 | Astra roles; ask-a-friend | Panes (Phase 12) |
| 26 | `board.log` | Keep; `reach=herdr` |
| 27, 29 | Roster; displays | `worker_panes.observe`: the staff record joined with `herdr agent list` (Phases 7, 13) |
| 28 | Shutdown and restart of workers | Phase 10 |

### Staff record readers (found at 7e85a74b; the tree moved in Phase 3c, units and joined readers move in Phases 3s-3v, parent files go in Phase 4d; workers in Phases 3n and 8-14)

- Showrunner tree: `showrunners.py:427, 432, 491` (`parent_of`, `children_of`, `tree_lines`); the parent-file reads `showrunners.py:398, 415, 576` (`_stored_parent`, `edges`, `stored_children`) and their caller `production_lifecycle.py:350` (`refuse_running_children`), which go with the parent files (Phase 4d item 2); `production_tree.py:127, 287` (`unit_edges`, `tree`; the file goes in Phase 3n).
- Units: `live_units.py:32`.
- Workers: `workers.py:529, 1564`; `remove_workers.py:65`; `assign.py:604`; `hierarchies/worker_owners.py:493, 606, 642`; `retire_unit.py:292`; `production_tree.py:156, 169, 231, 256`; the worker and mesh sources `message/roster.py:1123, 1185, 1344, 1694`, `shutdown/inventory.py:908` (`codex_seats`) and `production/rename.py:528` (`_rename_unit_workers`), which move with the worker readers or go at cleanup.
- Joined readers: `message/roster.py:906, 958, 1050`; `production/broadcast.py:105-129` (through roster); `direct/board_json.py:371, 427`; `shutdown/inventory.py:422, 427, 1693`; `production/codex_winddown.py:290, 304, 332`; `dailies_input.py:300`; `production_lifecycle.py:173`; `rename_unit.py:240`; `stall_watch.py:755, 771`; `unit_effort.py:206`; `waiting.py:153`; `accounts/unit_watchdog.py:335`; `hierarchies/measure.py:1413-1416`; `nightly_review/nightly_review.py:122, 252`; `style/style_plan.py:2952`; `usage/waste_watch.py:181`.
- Not moved by the first switch (decision 16): these needed nothing from the record. Under decision 17 the searches of Phases 3t-3v list again every reader of Units-table cells, timer docs and pane marks, so a file below changes when its phase's search lists it.
  - Sources that `seed` reads: `showrunners.py:216` (`registered_showrunners`); `unit_lookup.py:316, 383` (the pane marks); `production_units.py:59` (`unit_table`); `production_tree.py:119` (`marked_units`).
  - Timer fields, live state or Units-table cells of a member the caller already knows: `unit_lookup.py:392`; `direct/phase_table.py:1664`; `shutdown/stop.py:409`; `shutdown/restart.py:2848`; `promote_showrunner.py:124`; `rename.py:645`; `rename_unit.py:257, 273, 300, 673, 884, 982`; `resume_unit.py:130`; `retire_unit.py:435`; `transfer_unit.py:459, 505`; `add_unit.py:1025`; `astra/design_roles.py:489`; `nightly_review/nightly_review.py:273`; `whoami/agent_accounts.py:239` (one zone from any timer; it is also on the import chain of `pane_record.py`, decision 15).
  - The tree through `showrunners.py` functions only, which read the row since Phase 3c: `production_tree.py:111` (`edges`); `dailies_input.py:1032` (`root_of`, `children_of`); `rename_outside.py:143, 176` (`children_of`); `add_showrunner.py:242` and `style/add_style.py:333` (`parent_of`).

### Rename inventory (Phase 5)

Config: `config/agents.conf`, `config/README.md:7, 14, 15, 20`, `.gitignore:23-26`. Commands: `agent.md`, `ask_a_friend.md:9`, `history.md:113`, `plan/phase_review.md:173`, `unit/direct.md:28`, `showrunner/add_unit.md:31`, `showrunner/promote_unit.md:18`, `api_review.md`, `module_review.md`, `team_review.md`; Codex mirrors regenerate through `claude_to_codex/sync.py`. Scripts: `agents_config.sh`, `sync_codex_catalog.sh`, `agent_admin.sh:95`, `agent_bg.sh:217, 253`, `agent_exec.sh:78`, `ask_a_friend/launch_friend.sh:54`, `production/add_unit.py:506`, `production/unit_effort.py:61-73`, `notify/review.py:667`, `production/affected_modules.py:109`, `progress_history.py:209, 283, 304`, `assign.py:400`, `demo_refusals.sh:20-24`, `hooks/context_usage.py`, `whoami/codex_pacer.py:6, 58, 440, 523`, `whoami/quota_alert.py:248, 723`, `whoami/profile_notes_pass.py:252`, `astra/design_roles.py:528`, `cli_agent/cli_agent.sh:6, 75`, `history/history.py:115, 121`, and their tests. Docs: the as-built docs that name the file, `docs/direct/launch_implementation.md:24, 72`. Leave: `scripts/settings/git_filters_table.sh:16` (history), `scripts/settings/test_git_filters_retire.py`. NixOS: questions list item 1.

### Instruction edits (Phases 8 and 14)

`commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<CodexDispatchWait/>`, `<PauseForMove/>`, `<FixDispatch/>`, `<PhaseCleanup/>`, `<HeldAstraRoles/>`, `<ToolingContract/>`); `docs/direct/write_prompt_contract.md` (`<PhaseMesh/>` → `<PhasePanes/>`); `docs/direct/launch_implementation.md`, `dual_review.md`, `phase_end.md`, `run_phase_review.md`; `commands/unit/report.md`; `commands/showrunner/produce.md:287, 390, 476, 534, 1122-1123` and one paragraph on where workers show; `commands/showrunner/accounts.md`, `rename_unit.md`, `retire_unit.md`, `transfer_unit.md`; `commands/message.md`; `commands/codex_winddown.md`; `commands/shutdown.md`; `commands/ask_a_friend.md`; `commands/astra_review.md:32` (still starts `mesh_runs.py`); `docs/quota_alerts.md:19, 27, 39`; `commands/shot_report.md:11`; `skills/herdr/SKILL.md:109, 221`, a git-ignored home-manager file, changes only through the NixOS file (questions item 1); `docs/production_format.md:39, 166`; `docs/accounts.md`. A final search for mesh paths outside history ends the phase.

### Cleanup list (Phase 19)

`codex_mesh.py` and its tests; the mesh parts of `workers.py` and the unit worker ledger; `scripts/direct/remove_workers.py`; the run `workers` log (`<run>/workers`, read at `scripts/lib/unit_runs.py:136-145`, `remove_workers.py:65`, `worker_state.py:50`); `mesh_roster.json`, `*.pending.json`, `mesh_*` run files; follow-claim code; `[direct.options] codex_mesh`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, `WORKER_TRANSPORT`; shutdown's `CodexServer`, the mesh parts of `codex_seats` and `PausedSeat` (keep the Claude child resume branch, models 3s), `_CODEX_HEADLESS`, mesh pause and paused-workers note; accounts' server lines; display mesh code (the mesh-source readers of `staff_sources.py`; `production_tree.py` is already gone in Phase 3n; `process_table.py`, `waste_watch.py`, `hierarchies/*`, `shot_report/transcripts.py:1680`, `lint/tmp_sweep.py`); the Codex app-server homes; docs: rewrite `long-lived-workers.md`, update `herdr-adoption.md`, `session-roster.md`, `agent-registry.md`, `shutdown-and-restart.md`, `unit-board.md`, `usage-ledger.md`.

### Probe results (Phase 6 writes them)

Measured 2026-11-02 on natedev, herdr 0.9.5 (protocol 22), in probe workspaces of `agents` and in a private herdr server. The probes did not restart the shared `agents` server and did not replace a live credential. Each probe workspace was closed after use.

| Probe | Result | Who uses it |
| --- | --- | --- |
| Which side `--ratio` measures | The original pane: the left side of a `right` split, the top of a `down` split. `pane split --direction right --ratio 0.3` gave the original pane 27 of 91 columns. | Phase 7 split sizes |
| `split_ratio` on a nested split | It changes the nearest split that holds the pane. The root split kept its ratio 0.3. | Phase 7 sets each split by its tree path with `layout.set_split_ratio`; `split_ratio` is removed |
| A close rebalances the rest | No. The closed branch collapses into its sibling. The other split ratios stay. | Phase 7, Phase 10 |
| Label after the agent exits | Kept. After `/exit`, Codex and Claude panes kept their `label_pane` label and their `terminal_id`. | Phase 7 finds a stopped worker's shell by its label |
| Label after a cross-workspace move | Kept, with the same `terminal_id`. The pane id changes (`w1P:p2` became `w1Q:p1`). | Phase 7 |
| A message typed mid-turn | Both CLIs processed a second prompt sent right after the first. That the second arrived during an active turn is not proved. herdr's state read `idle` right after the prompt, so state reads can lag a turn. | Decision 4: only a `DONE <dispatch id>` line ends a pass |
| herdr's Codex state over a 10-minute turn | `working` at the start, `done` at the end: 605 s, `Worked for 10m 5s`. The probe read the start and the end only. | Phase 7 |
| Prompt by path, and an image named in a prompt file | Both CLIs read `/tmp/.../prompt.txt` and opened the image it named (Claude about 9 s, Codex about 22 s). This is the interactive prompt-file workflow, not a CLI flag. | Phase 7 |
| The Codex input that names a session | `/rename <name>` typed in the session. `codex --help` shows no `--name`. Claude takes `--name <name>` at start. | Phase 7 |
| `native_session` for a Codex pane | Fails before the first turn ("Codex launch identity is unavailable"). After the first turn it returns the thread id, from the rollout file Codex opens. | Phase 7 must handle the interval before the first turn |
| Confirmed pane close | It closes only the target pane. The sibling in the same tab stays with its record. A rerun changes nothing. | Phases 7, 10-12 |
| `resume_agents_on_restore` keeps each pane's `--env` | **Negative.** With `resume_agents_on_restore = true`, a private server stop and start brought back the shell only: `agent get` gave `agent_not_found`, and the pane-only `--env` marker was absent from the restored shell. | Phase 10: our split-tab restore stays; questions list item 1(e) applies |
| Interactive Codex picks up a new `auth.json` by itself | Not proved. After a private `auth.json` was replaced with an invalid key, the running Codex still answered. That shows it kept its old credentials, not that it read new ones. | Reference job 8: the sign-in restart stays |

Probe logs: `/tmp/claude/unit/32b26811-c966-489a-8843-ba1bd158abce/mesh_runs/` runs `20261102T111252-3094000-c5b2` (start and prompt probes), `20261102T112920-297660-034d` (confirmed close) and `20261102T120803-3311603-53df` (10-minute turn, restore, `auth.json`). They are kept only while that run folder exists.

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
| Hierarchy joined at each read from timers, parent files, Units tables, Owns lines, marks, worker ledgers, `mesh_roster.json`, run `workers` log, claim files | `staff.py` (Phases 1, 3, 3q-4g) |
| A member's shown name: the session record, the saved `-n` in the pane launch record, the tab title | the staff row (Phase 4) |
| The staff-to-pane join in each display | `worker_panes.observe` (Phases 7, 13) |
| Agent states as strings in each caller | `herdr.AgentStatus` (Phase 6) |
| Config path per module; line parsers in Python and bash | `models_registry.py` (Phase 5; models-unit's bash reader work, moved from Phase 5a) |
| herdr launch paths: `codex_mesh start`, `agent_exec.sh` fresh review, `launch_friend.sh`, `herdr.launch_agent` | `herdr.launch_prepared_agent` with a split destination (Phases 6-7) |
| Dispatch logic in three launchers | `worker_panes.py dispatch` (Phase 8) |
| Resume texts (`restart.py`, the mesh's, the seat lines of `stop_resume.resume_message`) | `stop_resume.resume_message` (`stop_resume.py:178`), the one builder, with `PaneWorkerResume` (Phase 10; seat lines removed in Phase 3n) |
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
- Simplicity 2 (no `WORKER_TRANSPORT`) — units end their runs at different times, so a drain stops every unit at once; the pin lets each unit switch at its own run boundary, and Phase 19 deletes it.
- Simplicity 3, the rest (remove the `SHOWRUNNER_*` marks) — `member()` uses them to go from a Units row to the pane launch record that holds the `STAFF_ID`.
- Simplicity 4, the rest (write the session record back on a refusal) — that record is Claude Code's file; the next launch takes the row's name.
- Simplicity 7, the rest (delete `STAFF_FILE`) — with no path parameters it is the one test injection point, and the subprocess and fake-ssh tests need it.
- Simplicity 8 (`launch.py` in Phase 8) — the launchers keep their mesh branch until Phase 19, so an early merge ports code that Phase 19 deletes; dispatch is in one place from Phase 8.
- Simplicity 10 and API 10 (the overview) — the overview is another file, outside this edit; herdr-unit updates it to this plan.
- Correctness 3 (`restore()` of a retired row) — a retired row is removed (Simplicity 5), and retire ends the unit's workers, so a resumed unit is a new member and is owed no worker conversation.
- Correctness 4, the rest (a hand rename of a worker) — a worker's name is set once (decision 13), and its session name is its id.
- Correctness 9, the rest (the pass recorder as the journal) — the transcript already holds the submission; a second record would be a copy of it.
- API 2 (three row classes) — one four-field row with the graph rule checked under the lock gives the same guarantee with one type.
- Rerun 2, the rest (natedev pulls the Mac snapshot) — the Mac sends it through the one ssh write path, with the same rows.
- Rerun 5 (a cleanup fence) — after Phase 15 nothing pins `mesh`, so the check cannot go stale; cleanup is not a shipped tool.
- Rerun 6, the rest (`seed` refuses after a cutover marker) — id-first matching means a later `seed` cannot undo a rename.

Refuted: none. Questions for the user: none.

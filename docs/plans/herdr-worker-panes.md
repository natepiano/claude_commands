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
- **Outside reader:** cargo-liner reads `staff.json` directly. A change to the stored shape sends a notice to that reader with a representative stored-record fixture.
- **Layout:**
  - `scripts/production/` — production tools: `showrunners.py`, `production_units.py`, `production_lifecycle.py`, `live_units.py`, `unit_lookup.py`, `session_staff.py`, `session_labels.py`, `add_unit.py`, `add_showrunner.py`, `retire_unit.py`, `rename_unit.py`, `transfer_unit.py`, `rename.py`, `promote_*.py`, `resume_unit.py`, `production_tree.py` (deleted in Phase 4b), `broadcast.py`, `codex_winddown.py`; `staff.py` and `staff_sources.py`
  - `scripts/herdr/` — `herdr.py` (the one herdr client), the new `worker_panes.py`
  - `scripts/direct/` — the launchers `implement.sh`, `review.sh`, `assign.sh`/`assign.py`, `workers.py`, `worker_state.py`, `remove_workers.py`, `board.sh`, `progress_history.py`, `findings.py`
  - `scripts/agents/` — `codex_mesh.py`, `worker_lifecycle.py` (from models checkpoint 3b67af71), `mesh_runs.py`, `agent_exec.sh`
  - Not in this unit's scope (models-unit owns them from 2026-11-03): `scripts/agents/default_models.sh`, `scripts/agents/models_config.sh`, `scripts/lib/models_registry.py`, `scripts/agents/sync_codex_catalog.sh`, `config/models.conf`. A phase here that must change one sends the change to models-unit.
  - `scripts/message/` — `send.py`, `roster.py`, `sessions.py`
  - `scripts/shutdown/` — `shutdown.py` (the CLI), `stop_resume.py` (stop, resume and move of one session), `inventory.py`, `restart.py`, `shutdown_record.py`, `pane_record.py`, `pane_restore.py`, `remote.py`; `stop.py` is gone
  - `scripts/lib/` — `unit_runs.py`, `names.py`, `atomic_file.py`; `tunables.py` (new in Phase 3q)
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
  - `scripts/production/staff.py` — the staff record: row, rules, read and write API, CLI (new in Phase 1; `apply_seed` in Phase 2; `ensure_worker` in Phase 3l). From Phase 3n a worker row also holds the worker's recorded role (`WorkerRole`), written once by `ensure_worker(name, parent, role)` at admission; every role reader takes it from the row, and no code parses a role from a name (`WorkerName.parse` checks shape only). The backend-identity record (Phase 3n) holds backend details only.
  - `scripts/production/staff_sources.py` — `member` and `seed` over today's sources (new in Phase 2; `fill` in Phase 3q; the seed importer and `fill` go in Phase 4d); `live()` (Phase 3l), the reader behind `staff.py show --json --live`, the handler's command
  - `scripts/lib/tunables.py` — the one reader of tunable values in `config/*.conf` (new in Phase 3q)
  - `scripts/herdr/worker_panes.py` — `start_or_resume`, `close`, `observe` (new in Phase 7), `dispatch` (Phase 8); `observe` is the one join of worker rows and herdr panes (`OpenWorker | StoppedWorker | ClosedWorker | UncertainWorker`)
  - `scripts/herdr/side_pane.py` — the pane beside a caller outside the staff hierarchy, for ask-a-friend and for such a caller's Astra role (new in Phase 12)
  - `scripts/agents/detached_runs.py` — detached runs that type their result into the worker pane (renamed from `mesh_runs.py` in Phase 9)
  - `scripts/direct/assign.sh` — the one door that starts a worker (decision 11); Phase 19 merges `implement.sh` and `review.sh` into it
  - `scripts/lib/unit_runs.py` — run folders; `worker_transport(run)` and the run pin (added in Phase 8; they and the run `workers` log reader go in Phase 18)
  - `config/direct.conf` — `WORKER_TRANSPORT` (`mesh` in Phase 8, `herdr` in Phase 15, deleted in Phase 18); `WORKER_SPLIT_RATIO` (Phase 7), read at each split through `tunables.number`
  - `commands/unit/direct.md` — the unit director instructions (transport-aware text in Phase 8, pane text in Phase 14)
  - `scripts/shutdown/stop_resume.py` — stop, resume and move of one session: `stop` :97, `resume_message` :178 (the one resume-text builder), `resume` :216, `move_sessions` :405, `move` :463; `STOP_GRACE_SECONDS` and `KILL_GRACE_SECONDS` (:42-43) move to `config/shutdown.conf` in Phase 10; the per-member worker stop and resume come in Phase 3n, `stop_unit_workers` in Phase 10, mesh pause removed in Phase 18; the shutdown unit's file
  - `scripts/accounts/refusal_watch.py` — `transcript_refusal` (:464), the one usage-refusal classifier (Codex transcripts and screen text in Phase 9); the accounts unit's file
- **Test lanes:** no Rust crate. The test lane is the `test_*.py` files, the `*_test_support.py` fixtures and the fixture folders beside each module. In a `1 writer + 1 tester` phase, `test` owns only those files and writes them from the Spec.
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
  - **Lifecycle reruns keep the pane (user, 2026-10-31).** A rerun of move, rename, stop, resume, down or restart changes nothing, keeps the same tab, pane and terminal, and exits 0. No recovery closes and recreates a pane or split. Each such phase's gate has a real rerun proof and one operation stopped partway that finishes on rerun.
  - **Staff design (2026-10-31; user via the sdlc showrunner).** A unit's staff row holds its worktree (machine and path); its repository and branch come from git in that worktree. Its plan, port and responsibilities are in an untracked `.unit.toml` at the worktree root, listed in the repository's shared `info/exclude` (`git_exclude.ensure_excluded`). Retire moves the file to `~/.local/state/staff/units/<staff id>.toml`; `resume_unit` moves it back. A showrunner row holds its production doc. No code works out a folder, a branch or a parent from a name. Owns, the Units table, the `showrunners.json` lists, `Showrunner session`, `Parent production`, `Formerly` and the review pool are deleted. The pane-first order below supersedes the old staff-first order (sdlc, 2026-10-31 15:5x EDT). Folder moves follow 3r. Panes use 3n and 4; broad staff conversions, store moves and deletes follow useful panes. The rename rebuild stays last among staff steps (4g). Phase 3h replaces copied `STAFF_ID` marks with native runtime lookup. Todo phases use that contract even where an earlier completed phase records the old mark.
  - **One identity join.** Staff-backed identity joins live once, in `scripts/production/session_staff.py`; `session_labels.py` owns display labels. A command takes one `staff.snapshot()` and one `session_staff.member_sources(dependencies=<one staff.RecordInputs>)` and passes both through every call. Running directors come from `workers.running_director_sessions`, a run's director from `workers.resolve_live_unit_director` on the same evidence. No reader takes a second owner snapshot or keeps its own join.
  - **No hard-coded config (user, 2026-10-31).** A tunable (a limit, a ratio, a time) lives in a `config/*.conf` file and is read at use through `scripts/lib/tunables.py`; code holds no default. An operation deadline (`worker_lifecycle.operation_deadline`) can shorten a configured time, never extend it: each wait is `min(configured limit, remaining deadline)`. Each such value gets a test that a changed config value takes effect with no code change.
  - **The worker transport is pinned per run.** `WORKER_TRANSPORT=mesh|herdr` in `config/direct.conf` is read at run start and pinned in the run folder. A run that started on the mesh finishes on the mesh. Every lifecycle caller (dispatch, messages, detached delivery, shutdown, rename, transfer, retire, cleanup) reads the run's pin through one resolver, `unit_runs.worker_transport(run)`.
  - **Tests never touch the user's tabs.** herdr tests run in a probe tab of `agents` (memory: one herdr session; `herdr.py` refuses other servers) and close it at the end. No test runs `herdr` against a live director's tab.
  - **Build hold.** Before any test or gate, check `build_hold.py status`; while a natedev hold that is not ours is in force (02:00-05:00 tonight), run nothing that builds or tests. Edits continue.
  - **One file writer.** Every file write uses `scripts/lib/atomic_file.py`. `write_text_atomic(path, text)` writes a temp file beside the target, syncs it, replaces the target with `os.replace` and syncs the folder; `sync_folder(folder)` does the folder step alone. Its temp files are `.<name>.<pid>.<random>.tmp` (`atomic_file.py:42`), so a cleanup of a killed writer's temp files uses the glob `.<name>.*.tmp`. No phase makes its own temp-file, `fsync` and replace steps. `write_bytes_atomic(path, data)` (`atomic_file.py:85`) is the binary writer; no second module. The unit-board unit owns `atomic_file.py`; a phase that changes it agrees the change with that unit first.
  - **Owners.** The models unit owns `models_registry.py`, `config/models.conf` and the registry writers; the shutdown unit owns `scripts/shutdown/`; the accounts unit owns `scripts/accounts/` and account moves. A phase that changes their files names the owner in its checkpoint notice and agrees the change with them first. The user owns `/etc/nixos` (questions list item 1).

- **Constraints for todo phases (sdlc, 2026-11-01).** Phase 3h's native runtime identity supersedes earlier copied-mark contracts. `WorkerStartAllowed` permits backend creation; only `PublishedWorker` permits dispatch success. A row's `id` is fixed: the first native session or thread id. A live clear or a Codex thread replacement changes only the row's `current_session`, through `staff.update_current_session(old, new)`, in one locked atomic staff write; no journal or store registry. Ownership, references, child links and locks use the fixed `id`; live observation, transcripts and backend resume use `current_session`. A stored reference resolves by fixed id, then by current session; an observed runtime id matches only `current_session`. Every fixture that tests identity makes the two differ (Phase 3h review, 2026-11-01). A late hook is visible as a missing-row session. Migration writes reference stores first and staff last and is deleted in Phase 18. Current membership omits retired/wrapped members; cleanup retains their live, unknown and duplicate evidence. Retire, wrap and promote keep stopped workers for restore. Only worker end and permanent unit removal delete worker rows. Phase 8 owns `worker.py`; Phase 19 deletes old launcher names after callers switch.
- **Isolation gate (Phase 3h review, 2026-11-01).** Every automated gate runs as `env HERDR_BIN=/nonexistent/herdr -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY <command>`, so a test that reaches herdr or live staff fails in its own phase, not at the merge. Real proofs run separately, under their named probe steps. Hook and subprocess fixtures keep their scratch-path overrides (never a `patch.dict(..., clear=True)` that drops them). A removed or changed assertion needs one `Assertion-Change: <path>::<test> — <why>` line. The hub files `staff.py`, `session_staff.py`, `showrunners.py`, `herdr.py` and the rename helpers have one writer at a time across the rename and pane tracks; a phase that needs one waits for the holder's landing.

## Decisions made in this plan

Each item is my call under the decision criteria unless it names the user. A one-line plan change reverses it.

1. **One staff record (user, 2026-10-30).** `scripts/production/staff.py` and one file, `~/.local/state/staff/staff.json` on natedev, hold every showrunner, unit director and worker: a stable id, a short name, the kind, and the parent row's id. So the record maps each short name to an id and holds the relationships (worker → unit → showrunner); production and unit come from the parent chain. A rename changes only the name and a transfer only the parent; the id never changes. There is no separate worker ledger: the unit worker ledger, `mesh_roster.json`, the run `workers` log and the slot claim files retire into this record. Live state (pane, socket, running or not) is never stored; it comes from herdr and the session records at read time. No such record exists today: the hierarchy is joined at each read from update timers, parent files, Units tables, pane marks and the worker ledgers (Reference → Staff record readers).
2. **One command starts or resumes a worker (user, 2026-10-30).** `worker_panes.py start-or-resume <short name> --role <role>`. The role is the recorded role on the worker's staff row, written once at admission by `staff.ensure_worker(name, parent, role)` (Phase 3n); no code parses a role from the name, and a launcher name such as `impl2` can hold any recorded role. A worker's herdr agent name and its Claude or Codex session name are its staff id. Both Claude Code and Codex resume a conversation by its name (user, 2026-10-30), so the command resumes by id and keeps no conversation id. At every start or resume it looks up kind, model, effort, service tier and sandbox for that role in the model registry, and only then (user, 2026-10-30, option A): a `/model` typed by hand holds, no dispatch compares the model, and a config change does not restart a running worker. This replaces 710a6adb's per-turn lookup. The director picks the number: `impl1`, then `impl2` for a second implementer. The name is set once, when the row is made.
3. **Idle panes close at phase end (user, 2026-10-30).** A pane closes when its role is idle (its dispatch is done and nothing is queued) and reopens by `current_session` at the next dispatch. This applies to workers, designers and reviewers (user, 2026-11-03 via sdlc: the codex-mesh retires). The conversation stays. To bring a worker back, the director calls `start-or-resume` again. This settles the keep-open question.
4. **The worker's final reply signals completion, and the transcript is the receipt; herdr's state only wakes the launcher.** Every dispatch has a dispatch id, and the one line the launcher types carries it (decision 7). The worker's own transcript then holds the proof of each step: the typed line with the id proves the submission, and a final reply that ends with `DONE <dispatch id>` proves completion. The launcher writes the status file only from that reply. A rerun reads the transcript before it types anything, so it never types a pass twice (Phase 8). No other record of a submission exists, and the prompt file is never changed. The launcher writes the status file today (`implement.sh:629`); the worker writes only its summary. This works the same for read-only reviewers, which cannot write a file (`review.sh:689` starts them with `--sandbox read-only`): the launcher writes their findings file from the reply, as the mesh does today.
5. **The launcher's whole wait is one background `herdr agent prompt <id> "<line>" --wait`, with no `--timeout` and no `--until`.** herdr then waits for its default settled set, `idle`, `done` or `blocked`, after it sees the turn start (herdr skill, "For normal agent work, `--wait` is enough"). `blocked` wakes the launcher, so a worker at an approval dialog never stalls the director. The director runs the launcher in the background, as today, and the task notification is the wake-up.
6. **A dispatch refuses a busy worker; a message queues.** A dispatch is a new pass that a launcher waits on, so it needs an `idle` or `done` worker, under the worker's dispatch lock. A message (`send.py`, a detached-run result, review Stage 2) is typed at once, also during a turn: Codex and Claude queue typed input. A blocked or closed worker's message goes to its director. `pending.json` drops.
7. **A prompt goes in by file path, not as pasted text.** The launcher types one line: `Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>.`
8. **No launcher compaction, and no "Continue where you stopped." retry.** Interactive Codex and Claude compact by themselves.
9. **Codex stays a worker kind.** herdr starts `--kind codex` and `--kind claude` agents. The default worker kind per role stays where it is set today.
10. **`herdr.py` stays the one herdr client.** Launch into a split pane or into an existing pane at its shell, layout changes and the typed agent state `AgentStatus` are new `herdr.py` primitives (Phase 6). `Herdr.panes()` stays the one pane-list parser, with many callers. `worker_panes.py` composes them and holds only `start_or_resume`, `dispatch`, `close` and `observe`. `observe` is the one join of worker rows and herdr panes that every worker display reads (Phases 7, 13): it joins on complete pane evidence and the stable `terminal_id`, and gives `OpenWorker`, `StoppedWorker`, `ClosedWorker` or `UncertainWorker`. Its `_layout` is a private helper. One-line agent calls (`herdr agent send-keys <id> esc` to interrupt, `herdr agent read`, a message's `herdr agent prompt`) run directly and are not wrapped.
11. **One door starts a worker: `assign.sh`, run as a background shell of the director's own session (user rule 2026-11-04 10:45: "it should be impossible to start a worker in more than one way"; sdlc ruling).** devtools-unit makes `implement.sh`, `review.sh` and the `codex_mesh.py` start, follow, reopen, fork, adopt, prime and resume subcommands refuse unless `assign.sh` called them, and refuses detached starts, with a guard test. Worker panes (Phases 7, 8, 19) change what is behind `assign.sh`; they never add a second door. Start, dispatch, wait and receipt live once in `worker_panes.py dispatch`, which only `assign.sh` calls. Edits to the `scripts/direct` launchers go through devtools-unit first.
12. **The transport switch lives in `config/direct.conf`, with an end date.** `WORKER_TRANSPORT` is migration state, not a model default, so it stays out of `models.conf`. The cleanup phase deletes it, no later than 7 days after the default switch.
13. **The right pane shows the worker's short name (user, 2026-10-30).** For example `impl1`, with no production or unit name.
14. **The staff row is the one name (sdlc showrunner, 2026-10-30).** A unit's name changes only through `/showrunner:rename_unit` (sdlc, changed 2026-10-30). The hook cannot stop Claude Code's `/rename`, so on a mismatch it keeps the tab on the row's name and prints one line with the rename_unit command. Every launch and every restore of a member takes its name from its row (Phase 4). A showrunner's name is also its workspace's, which `Herdr.retitle` does not rename, so `/showrunner:rename` (`rename.py`) is the one way to rename a showrunner. A worker's name is set once (decision 13).
15. **One way to find a row: the `unit_marks` join (herdr-unit, 2026-10-30; showrunner, 2026-10-30).** The join is `unit_marks.MemberSources.member` (`unit_marks.py:77`, built by `member_sources` :136); `staff_sources.member` (`staff_sources.py:42`) is its entry point and keeps no join of its own (Delegation Context → One identity join). `unit_marks.py` and `staff_sources.py` import `showrunners.py`, so `showrunners.py` never imports either, and no lazy import or copy of `member` goes around that rule. `staff.py` imports none of today's sources (Phase 1 As-built). Row names are workspace and tab names, not slugs: seed keeps slug `sdlc` as row `tools` (`scripts/production/test_staff_sources.py:28`), so no code finds a showrunner or unit row with `staff.by_path(<slug>)`. Code that needs a member's row calls `staff_sources.member(selector, staff.snapshot())`; a `showrunners.py` function that works on rows takes the row or the `StaffSnapshot` from its caller and never looks a member up itself. Tests use a slug that differs from its workspace name. One exception (showrunner, 2026-10-30): `showrunners.py main`'s attach and detach branch imports `showrunner_parent` inside that branch, so users keep one command; the module has no load-time import of it. The showrunner tree readers live in `showrunner_tree.py`, which imports `staff`, `staff_sources` and `showrunners` (sdlc, 2026-10-30; Phase 3c item 5); `showrunners.py main` imports it inside the branch that needs it, under the same exception.
16. **The row holds what the user's design needs (herdr-unit, 2026-10-30; worker role, sdlc 2026-10-31; corrected 2026-10-31 for decision 17).** The row is id, kind, short name and parent: a map from short name to id and who reports to whom (Phase 1 As-built). A worker row also holds its recorded role, set once at admission (Phase 3n). Phase 3q adds two fields: a unit row holds its worktree (`PresentWorktree | RemovedWorktree`) and a showrunner row holds its production doc (`LiveDoc | WrappedDoc`). The row gains no session, socket, zone or Units-table field; a worker's backend identity is a separate `"backends"` section of the same `staff.json` (Phase 3n), not a row field. A retired unit keeps its row with `RemovedWorktree`, and a wrapped production keeps its showrunner row with `WrappedDoc` (user, 2026-10-31); this supersedes the Phase 1 rule that a retired member has no row (Phase 3r). A reader keeps reading the timer fields (session, socket, zone, interval) from the update timer, joined to the row by the doc, never by a name: a row's name is not its slug (decision 15, `sdlc` → `tools`), so until Phase 4a the one join is `showrunners.timer_for(row, snapshot)`, which matches the timer whose doc is the row's doc (`StaffSnapshot.by_doc`); from Phase 4a the timer folder is keyed by the staff id and the join reads it directly. Its doc comes from the row. Every reader phase keeps a fixture whose slug differs from its row name. A unit's branch comes from git in its worktree, its plan and port from `.unit.toml`, and its pane and running state from herdr. A row with no timer on this machine has no timer fields here, and the reader shows it without them. The Units-table cells and the old Phases 16 and 17 are no longer the plan: Phases 3s-3w move the readers, and Phase 4c deletes the Units table.
17. **Staff design (user via the sdlc showrunner, 2026-10-31).** The unit row holds its worktree, and repository and branch come from git in it. Plan, port and responsibilities are in an untracked `.unit.toml` at the worktree root; retire parks it under `~/.local/state/staff/units/<staff id>.toml` and `resume_unit` puts it back. No folder is worked out from a name. Owns, the Units table, the `showrunners.json` lists, `Showrunner session`, `Parent production`, `Formerly` and the review pool are deleted. The approved pane-first order is in Phases → Critical path (sdlc, 2026-10-31 15:5x EDT). After each landing all live tools still work. Shared gates cover the exact final batch. Each state change keeps its real proof and rerun checks. Each delete names a check that proves nothing still reads the thing. Retired units are backfilled, and a missing fact is listed for sdlc, never filled from a likely value. Only `STAFF_ID` stays in the pane environment and the pane launch record (Delegation Context → Staff design (2026-10-31)). The term "unit marks" retires: text says "session labels" (the unit name and staff id a session carries), and `unit_marks.py` becomes `session_labels.py` in Phase 3s (user via sdlc, 2026-10-31).
18. **A herdr pane starts in its worktree's dev shell (C380, models-unit via sdlc, 2026-11-03).** When the codex-mesh retires, a worker, designer or reviewer pane starts inside its worktree's direnv dev shell, as the Codex app-servers do today. One test covers it.
19. **A run result reaches its owner after the launcher exits (models-unit via sdlc, 2026-11-03).** Today the mesh relay holds a detached-run result for an attached launcher, and drops it when no launcher is attached. A worker pane's run result goes to its owner with no launcher attached. One test covers it. Evidence: session cc8dddb8, runs 20261103T193124, 20261103T164054 and 20261103T200357-1248016-79f7.

## Questions for the user

1. **The NixOS edits (settled 2026-10-30: natedev makes them and tells the user when to rebuild; herdr-unit tells natedev when each is due; no diff files for the user).** (a) `modules/common/claude-git-filters.nix:35` `watchPaths`: `agents.conf` → `models.conf`. It is functional: until it changes, the git filter does not refresh on a `models.conf` change. (b) Comments in `modules/common/codex-catalog-sync.nix`, `modules/common/codex.nix:47`, `modules/common/shell.nix:139` (`/agent`) and `README.md:410`. (c) `modules/common/herdr-skill-local.md:7`: the rule "start a new agent in a new tab, not a split pane" must permit worker panes in the director's tab (`skills/herdr/SKILL.md:221` is built from it). (d) The sign-in path unit (natedev) and the launchd agent (Mac) that run `codex_mesh.py signin-changed`: they retire at cleanup, or point at the pane restart if Phase 6's Codex sign-in probe is negative. (e) Only if Phase 6's probe shows that herdr's own resume does not keep each pane's `--env` (its account): add `[session] resume_agents_on_restore = false` to `~/.config/herdr/config.toml` on natedev and the Mac, so our restore owns split tabs; if the probe is positive, herdr restores split tabs and our per-pane restore code retires for them. **Status (2026-11-03):** (a) and (b) are done in the NixOS source: natedev applied the Phase 5 edits to `/etc/nixos` main with the Phase 5 merge (main `731dd21aa`). natedev owns the rebuild notice and the check that the installed git-filter job watches `config/models.conf`; the repository's old-name test cannot see the installed job. Phase 10's probe result is negative, so (e) applies before the default switch (Phase 15). (c) is due before the default switch; (d) at cleanup (Phase 18). herdr-unit tells natedev when each is due.

## Phases

**Critical path (sdlc, 2026-10-31 15:5x EDT).** The showrunner approved `fast_plan.md` for the user: "i have stopped nearly all work ... i am sick of waiting". The supplied message gives no exact minute beyond `15:5x`. Pane-first order and shared gates replace the staff-first order and the rule that every phase lands alone. All checks stay. There is no build.

Phase 3q → 3r → actual unit folder renames. Use the corrected current command, one unit at a time at its phase boundary. Keep its stop, restart and finished-rerun proofs. Neither these moves nor Phase 6 waits for 4g. Phase 6 starts at once beside 3q/3r and the models work. Phase 3n still waits for 3q and models F024 (`3b67af71`). Phase 4 follows 3r, with one owner for its shared files. Then 3n + 4 + 6 → 7 → 8 → 9 → shared 10/12/13 → shared 11/14 → 15 → drain and audited 18 → 19. Phase 11 consumes 10's resume contract. Phase 14 can be drafted beside 11 and is checked against its final code.

**Rename track (sdlc ruling, 2026-11-01; replaces the earlier staff order).** After Phase 3h lands on main: the live move (`migrate_staff_ids.py` backup, dry run, real run, rerun; `staff_check` twice; a real `/clear`, a Codex thread replacement and a `herdr-up` rerun) → 3a (`unit <id>`) → the readers a rename would break: 3k, 3s, then 3t, 3u and 3v, each landing alone, 3w text with each landing, and Phase 4 → the records that hold a unit name (sdlc-rename-places.md): 3x, 3y, 3z, 4a → the deletions rename needs: 4b, 4c (after the Port audit 4m into `.unit.toml`), 4d, 4e → 4g, with 4j (no ad-hoc workspace, right after the live move), 4h (folder move, after 4j) and 4i (questions, after 3a) landing before it, rename on the row for every member kind and role. The old rewriting rename is never built; 3f8339f5 stays unmerged and gives only its tab retitle, kept-pane resume and finished-journal rerun. 4f (transfer) follows 4c and is not a rename prerequisite. No rename-track phase waits for Phase 15.

**Pane track, alongside.** 3n + 4 + 6 → 7 → 8 → 9 → shared 10/12/13 → shared 11/14 → 15 → drain and audited 18 → 19. Phase 5 still waits on the models unit. A hub file shared by both tracks has one writer at a time (Delegation Context → Isolation gate).

Targets: the old `fast_plan.md` dates are past. Each landing sends its own time to sdlc; the rename-track time is in the 2026-11-01 message to sdlc.

**Three kinds of order (sdlc, 2026-10-31 15:5x EDT).** A **Depends on** line distinguishes real inputs from scheduling and file ownership.
- *Data prerequisites:* 3n needs 3h's native-id admission, 3q's tunables and models F024. `tunables` comes from 3q, not 3n. Phase 7 needs 3n's role, backend, deadline and stop APIs, Phase 4's names and Phase 6's primitives. Phase 13 uses the existing `session_staff` join; 3s extends it and keeps `session_labels` for labels. 4a needs 3t's doc reads and 3z's remaining stores. 4d needs 4c's last `fill --dry-run` audit. 4e needs 4d's fallback removal. 4f needs 4c's table removal. No pane phase needs the full 4g rebuild.
- *Landing and gate rules:* 3t, 3u and 3v land one at a time, each with its own gate on its exact landing (their Depends on lines: "Lands alone"); ready 10/12/13 share a gate, then 11/14 share a gate. Merge each ready group as one batch and gate that exact final batch. Keep every phase's tests, types, review lenses, live probes, interruption and finished-rerun checks. A fix changes the batch; its required checks cover the final code. Both reviewers can review ready file groups at once and verify fixes. No phase uses a pass from an earlier tip. Delete barriers: 4b-4e wait for the conversions that read or hold what they delete (3s-3z, 4, 4a), not for Phase 15, and Phase 3h's native-id and location completeness proof on a fresh snapshot: no unresolved member identity or unfilled location. The worker ledger gets no migration; rename keeps its small file move for it until Phase 18 deletes the ledger after drain (sdlc, 2026-11-01). 4d and 4e name the completeness proof because they remove the fallback and labels. Phase 18 instead waits for no mesh pins or processes, no legacy consumers, and its history proof. It does not wait for 4g or unrelated staff stores.
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

### Phase 3n — Workers on the staff record (call sites)  · status: todo

**Owner:** models-unit, after its Phase 4 and 4m (sdlc, 2026-11-02). `staff.py` and `staff_sources.py` keep one writer at a time: herdr-unit and models-unit settle each window on the berth board.

**Depends on:** 3h, 3l, 3p, 3q (staff storage schema changes with roles; live output remains schema 3), and the models unit's F024 checkpoint 3b67af71 on `origin/natedev-models` (the lock order, `worker_lifecycle.acquire_flock` and the `stop` path): merge it before any edit.

Split from 3l on 2026-10-31; the same user ask and the same sdlc acceptance as 3l.

#### Work Order

**Goal:** keep Phase 3h's native-id admission and finish worker roles, permanent end, adoption, backend metadata, deadlines and reader conversion. The handler reads who reports to whom from the 3l staff command. A Codex worker is stopped and resumed by the same path as a Claude session, by name or staff id. `production_tree.py` has no caller left; Phase 4b deletes it.

**Spec:**

1. **Remaining call sites.** A worker lives as long as its unit director, across runs (user 2026-10-28). Each call site finds a worker through its staff row, also from a later run of the same director; a finished run never makes the row wrong (sdlc ruling, 2026-11-02 08:44). Preserve the existing shared admission in `worker_admission.py`; do not build a second open path. All starters already switch together in 3h. Integrate its `WorkerStartAllowed` and `PublishedWorker` types with the remaining role, adoption and permanent-end work:
   - Codex: open `codex_mesh.open_unit_worker` and `start`; permanent end `codex_mesh end` and `workers.end_unit`; a resumable `stop` keeps the row; adoption `workers.adopt` calls the staff adoption below.
   - Claude workers: their one open point in `implement.sh` / `assign.sh` and their one end point, `remove_workers.py`.
   - Astra roles: `design_roles.open_role` and `design_roles.end`. A showrunner-held Astra role is a worker row under its showrunner (3l seeds and shows it).
   **The recorded role is on the worker's staff row.** `staff.WorkerRole` (new: `impl | test | review | fix | astra-designer | astra-reviewer`). `ensure_worker(native_id: StaffId, name: str, parent: StaffId, role: WorkerRole) -> StaffRow` publishes it at the 3h boundary; each admission point passes the role its producer records today (the board role, the ledger entry's `role`, the design role). The role is set once, like the name: an `ensure_worker` of an existing row with another role raises `StaffRefused` and changes nothing. `StaffRow` gains `role: WorkerRole | NotAWorker`, where the singleton `NotAWorker` is the value of every showrunner and unit row. In `staff.json` only a worker row carries a `"role"` key; `from_json` requires it on a worker row and refuses it on any other row, and the conversion happens only there. The record goes to the next schema (the staff file is schema 2 before this role change; `live()` already emits schema 3). The first seed run after this phase reads the 3q schema file, gives each worker row its role from the same producer records in one transaction and writes the next schema; a worker row with no recorded role in any producer stops that seed with one line naming the row and writes nothing. A second seed run changes nothing. `staff_sources.LiveRole` becomes `WorkerRole | Literal["showrunner", "unit"]`, and `live()` takes a worker's role from its row, so `_role` (`staff_sources.py:206`) goes. No code parses a role from a name: `WorkerName.parse` (`staff.py:63`) checks shape only.
   **Locks (models checkpoint 3b67af71).** The order is "unit ledger, design/pool, membership, lifecycle, server, inbox, roster"; the pool lock goes with the pool in Phase 3p, so the order is "unit ledger, design, membership, ..." from then on. At each admission and retirement point, `ensure_worker` / `retire` run while the run-level membership lock is held, after the lifecycle, server, inbox and roster locks are released, so a bulk stop cannot run between the roster write and the staff row. A staff lock is never held while membership is taken; the lock-order guard in `test_codex_mesh` checks it. Every lock wait uses `worker_lifecycle.acquire_flock` with the operation deadline; `_server_lock(session_dir, *, deadline=NO_LOCK_DEADLINE)` takes it the same way, and in a shutdown an omitted deadline takes the operation deadline (`effective_deadline`). Only `OperationDeadlineExpired` is re-raised; an ordinary socket timeout keeps its recovery.
   **The staff transaction obeys the deadline.** `staff._record_transaction` (`staff.py:532`) takes `staff.json.lock` with a blocking `fcntl.flock` today (:537); it moves to `worker_lifecycle.acquire_flock` with the operation deadline. Every time limit in `staff.py` moves to `config/staff.conf`, read at each use through `tunables.number`, and each wait uses `min(configured limit, remaining operation deadline)`, so a deadline only shortens a wait: `OPERATION_LOCK_SECONDS` (today `OPERATION_LOCK_TIMEOUT_SECONDS = 30.0`, :28) for `staff.operation()`'s wait (`_wait_for_operation_lock`, :281); `OPERATION_POLL_SECONDS` for that wait's poll interval; `REMOTE_SECONDS` for the Mac's ssh call (`_remote`, :439, `timeout=20` today), which also bounds the ssh connect time. Each value has a changed-config test.
   **One adoption operation.** `staff.adopt_worker(worker: StaffId, new_parent: StaffId) -> StaffRow` changes only the worker row's parent and keeps its id and name, in one staff transaction, run under both runs' membership locks (`worker_lifecycle.lock_memberships(source, destination)`) with the lock rules above. `staff.move` keeps refusing a worker (`staff.py:624`). A rerun after the parent changed changes nothing, prints `unchanged` and exits 0.
   **The tunables reader** is Phase 3q's `scripts/lib/tunables.py` (moved there on 2026-10-31, packaging: 3q, 3r and 3u read config through it). `STAFF_CONFIG` is the test override for `config/staff.conf`.
   **Semantic types** (`~/.claude/docs/type_design.md`). `StaffRow.parent` becomes `ReportsTo(id: StaffId) | StaffRoot`, where `StaffRoot` means a top showrunner; `ensure` and `move` take that type where they take `StaffId | None` today; `find(parent=)` keeps its `StaffId` filter. `WorkerObservation.identity` becomes `ClaudeSession(session_id) | CodexThread(thread_id) | BackendUnavailable`. Nulls and dicts convert only at the JSON boundary (`StaffRow.to_json` / `from_json`, `live()`); `live()` keeps its existing schema 3 wire fields and meanings; the staff file schema is a separate contract. Pin both decoders with fixtures before the role change.
2. **One record holds a worker's backend identity (sdlc showrunner, 2026-10-31).** The backend identities are one `"backends"` section of `staff.json`, keyed by staff id, beside `"rows"`; they cover unit and showrunner-held workers. One file is one atomic publish: `_record_transaction` writes rows and backends in the one `write_text_atomic`, and `staff.snapshot()` reads both from the one file, so no reader sees a row and a backend from two generations. (Two files beside each other cannot be replaced together; a second file would need a generation protocol for no gain.) The section is written only by the staff worker writer (`ensure_worker`, a backend replace for a replaced thread, and `retire`, in the same staff transaction). It holds backend details only: the Codex home, keyed by the native row id. No backend kind is stored: the family comes from `current_session` (staff-lifecycle.md section 2; sdlc, 2026-11-04); no second session or thread id value; no name, parent, kind or role, which stay on the row in `staff.json`. The role migration fills it from proved producer evidence under the 3h publication rules; a second run changes nothing. Missing facts stay listed, never inferred from names. When a reader moves to it, no other live copy of the thread id stays in a roster or ledger; history events may keep theirs. The herdr unit switches all callers (holders listed by the models unit):
   - `staff_sources.live` and the seed: identities from the record.
   - the unit ledger: keeps `replaced` / `ended` events as history only.
   - the run roster (`mesh_roster.json`): staff id, server and run details; no thread id.
   - the held-message inbox (`_pending_for_thread`, one file per thread today; a mismatch drops held messages with no error): one file per staff id.
   - `mesh_retired.json`: removed. A replaced or ended thread is a `replaced` / `ended` history event in the unit ledger (`note_backend` already logs `replaced`), and run end reads the threads to close from those events.
   - detached runs (`mesh_runs.py:155` matches `CODEX_THREAD_ID`): the caller's runtime id selects its row directly; no extra identity copy is stored.
   - the design roles' held threads (`design_roles.py:139-149`, `:1288`): staff id.
   - the shutdown pause records (`shutdown_record.py:486`): staff id.
   - the direct readers of a roster entry's `thread_id`: `roster.py:1279` (worker threads of the roster), `hierarchies/worker_owners.py:498` (it reports a missing identity as a problem) and `shutdown/inventory.py:930` (the paused seats of a shutdown): each uses the native row id as the session or thread identity and reads only backend details from the backend section. `roster.py` is the messaging unit's file and `inventory.py` the shutdown unit's; each agrees its change first.
   **Every consumer moves before any live copy goes.** Before the first roster or ledger copy of a thread id is removed, `grep -rnE "thread_id|CODEX_THREAD_ID|_pending_for_thread|held_thread" scripts --include=*.py` (non-test files) lists every field read and every decoder; each one reads the backend section, or is a history reader that the checkpoint notice names. The copies go in the same landing as the last reader's switch.
   Two copies stay, both Codex's own and outside these scripts: the app-server's thread store, and the `CODEX_THREAD_ID` that Codex sets in its worker's environment. progress_history and usage keep old thread ids as history, not as a live fact.
3. **Busy and idle are read live, never stored:** the server pid and the Astra exec pid. `astra_roles.py:248` reads a stored roster status today; that stops.
4. **Owner fields go.** The rosters and the unit ledger keep thread and run details only. The run `director` file and the Astra record's `manager` stop answering who owns a worker; staff answers it.
5. **One stop and one resume path, per member.** `shutdown.py stop <name|staff id>` and `resume <name|staff id>` (`stop_resume.stop` :97, `resume` :216) take a worker as they take a Claude session. For a Codex worker, stop pauses its thread and resume follows or reopens it on its account. This replaces the seat commands in `scripts/lib/resume_message.py`'s `resume_message` (`implement.sh --to <seat>`). It also replaces `workers.pause_run`, `pause_unit`, `codex_mesh reopen`, and `resume-account-move --to` as public paths. `move_sessions` (:405) and `move` (:463) go through the same per-member path. The split-tab shutdown record and restore are Phase 10's work, not this phase's. The old commands stay as internals only: no user or skill text names them, no second caller reaches them, and a check fails on any new caller. Each stop and each resume is safe to run twice: the rerun changes nothing, keeps the same tab, pane and terminal, and exits 0 (user, 2026-10-31); the proof shows the rerun.
6. **Handler switch.** The handler switches its reader in step (`cargo-handler` `hierarchy.rs:602-690`, the exact argv match at `command_inputs.rs:212-217`, `[census] hierarchy_command` in `~/.config/cargo-handler/config.toml`, which names `production_tree.py --json` today).
   - **Live refresh.** Busy and idle change with no file change (a server pid or an Astra exec pid ends), so the handler reruns the command at an interval read from its config at run time (a new `[census]` key, handler-unit's file), and also on a change to any `inputs` path. The watch covers the remote owner's inputs and a missing input file that appears later.
   - **Partial results are shown.** `live()` today prints a problem on stderr and leaves out a worker whose backend is unavailable or whose parent has no identity. Each of these shows in the handler as visibly incomplete, never as a silent omission. The exact output form is agreed with handler-unit before dispatch.
7. **`production_tree.py` loses its last callers.** The `design_roles.py:517` import goes (`showrunner_sessions` moves to the staff live read), and the handler reads `staff.py show --json --live` (item 6). The file stays, unread, until Phase 4b deletes it with its check, so the handler keeps a working reader until its switch has landed.

**Owners:** `codex_mesh.py`, `worker_lifecycle.py` and `workers.py` are the models unit's: herdr workers write the call sites, and models reviews the diff before the merge. `stop` / `_mark_all_roster_ended` overlaps models' F024, so tell models before that function changes. `stop_resume.py` and `shutdown.py` are the shutdown unit's: shutdown writes its side against the herdr resolver (name or staff id → Claude session | Codex worker | not found). The handler reader and its config are handler-unit's.

**Files:**
- `scripts/production/staff.py` — the deadline, `adopt_worker`, `WorkerRole` and the row's `role`, `ensure_worker(native_id, name, parent, role)`, `ReportsTo | StaffRoot`, the backend-identity record writer, `OPERATION_LOCK_SECONDS` read.
- `scripts/production/staff_sources.py` — `live()` and the seed read the backend-identity record and the row's role; identity types; partial results.
- `config/staff.conf` — new (`OPERATION_LOCK_SECONDS=30`, `OPERATION_POLL_SECONDS`, `REMOTE_SECONDS=20`).
- `scripts/direct/assign.py`, `scripts/direct/implement.sh`, `scripts/direct/remove_workers.py`, `scripts/direct/workers.py` — call sites and readers.
- `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py` — call sites and thread holders.
- `scripts/astra/design_roles.py`, `scripts/astra/astra_roles.py` — call sites, held threads, live busy state.
- `scripts/message/roster.py`, `scripts/hierarchies/worker_owners.py`, `scripts/shutdown/inventory.py` — `thread_id` readers move to the backend section (messaging and shutdown units agree their parts).
- `scripts/message/test_roster.py`, `scripts/hierarchies/test_worker_owners.py`, `scripts/shutdown/test_inventory.py` — their tests.
- `scripts/production/staff_test_support.py`, `scripts/production/test_staff.py`, `scripts/production/test_staff_operation.py`, `scripts/production/test_staff_sources.py`, `scripts/agents/test_codex_mesh.py`, `scripts/direct/test_workers.py`, `scripts/astra/test_design_roles.py` — fixtures and regressions.
- `scripts/production/staff_move.py`, `scripts/production/test_staff_move.py` — `move_unit` placement keeps backend metadata; its regression.
- `scripts/production/worker_admission.py` — integrate 3h permission and publication with role and adoption.
- `scripts/production/session_staff.py` — current identity join; preserve retained cleanup evidence.
- `scripts/agents/worker_lifecycle.py` — read only.
- `scripts/shutdown/stop_resume.py`, `scripts/shutdown/shutdown.py`, `scripts/shutdown/shutdown_record.py` — the shutdown unit's stage-two work; the `cargo-handler` reader and config are handler-unit's.

**Workers:** `1 writer + 1 tester` — stage one, this unit.
- `impl` — source integration: every non-test file above in this repository except the other units' files; hub: `scripts/production/staff.py` (every call site needs its writer API).
- `test` — fixtures and regressions: `staff_test_support.py` and the `test_*.py` files above.
- Stage two is other units' work: the shutdown unit's stop and resume side, and handler-unit's reader switch.

**Constraints from prior phases:** Phase 3h: native row ids, `WorkerStartAllowed` and `PublishedWorker`, with no copied identity. Phase 3q: `tunables.py`. Phase 3l: worker `retire`, `WorkerName.parse` (shape only), `staff.py show --json --live` (schema 3 with `inputs`; staff storage has its own version before this role change), the seed; `staff_sources.WorkerObservation`, `BackendUnavailable`. Phase 3p: the review pool is gone. Delegation Context → One identity join, No hard-coded config, Lifecycle reruns keep the pane. Phase 4f: `staff_move.move_unit` builds its placement snapshot from rows; keep backend metadata through it and test that placement leaves that metadata unchanged (add `staff_move.py` to the consumer inventory).
- Phase 3h review (2026-11-01): item 2's roster, hierarchy and shutdown consumers take the runtime session from `current_session`; the fixed `id` stays the `"backends"` key and the inbox key. `ensure_worker` today is `ensure_worker(name, parent, *, runtime_id)` with `id` from the first runtime id; update every signature and line reference against today's `staff.py`. Keep 3h's admission boundary (`worker_admission.py`); do not reopen starters. Test a Codex thread replacement: worker ownership and held messages unchanged.
- Phase 3s review (2026-11-02): `staff.json` has an outside reader, cargo-liner. The role and backend schema change sends a notice to that reader and adds a representative stored-record fixture to its consumer check. The live-output contract stays separate.
- Phase 3s review (2026-11-02): the handler reads `staff.json` directly and live pane state from herdr commands. No work on staff read speed (user, 2026-11-02: "herdr shouldn't focus on making staff fast - we should just read the staff page directly").

**Acceptance gate:** `grep -rn production_tree scripts commands ~/.config/cargo-handler` finds only `production_tree.py`, `test_production_tree.py` and history docs; the item 2 search finds no live reader of a roster or ledger thread id; the affected suites pass; basedpyright 0/0 on changed files and every caller module; a test stops a write between rows and backends and shows that the file holds both old or both new, for admission, a backend replace and a retire; a test holds `staff.json.lock` during an interrupted stop and the stop ends at its deadline with `OperationDeadlineExpired`; a changed `OPERATION_LOCK_SECONDS`, `OPERATION_POLL_SECONDS` and `REMOTE_SECONDS` each take effect with no code change, and a deadline shorter than `REMOTE_SECONDS` ends the ssh call at the deadline while a longer one leaves `REMOTE_SECONDS`; `adopt_worker` stopped after its staff write, then rerun, ends as one clean adoption, and a rerun of a finished one changes nothing and exits 0; a launcher name whose recorded role differs from its name suffix (row `impl2` with role `review`) keeps its recorded role in its row and in `live()`; an `ensure_worker` of that row with another role is refused and changes nothing; a schema test reads a worker row with `"role"` and refuses a worker row without it and a unit row with it; a showrunner-held Astra worker keeps its row and identity; a test of a partial hierarchy shows it as incomplete; a real handler test shows busy become idle after a pid exit; watch tests for a remote owner input and a missing input that appears later; the real handler pane shows every session and worker correct after a real unit move and a real new worker; one real Codex worker is stopped and resumed by name and by staff id, and each rerun changes nothing, keeps the same tab, pane and terminal, and exits 0.

**Acceptance gate (admission and observation):** tests keep the exact exit-2 admission refusal and prove no dispatch on failed publication. Preserve `live()` schema 3 and its partial-result surface; do not use its version as the staff storage version. Keep `skipped N not current`; unresolved current workers stay listed with exit 1. A resumable stop keeps the row. Permanent end removes it only after stopped evidence; adoption keeps the native id.

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

### Phase 3k — `.unit.toml` writers emit taplo-formatted text  · status: todo

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

### Phase 3t — Production tools read the row and `.unit.toml`  · status: todo

**Owner:** devtools-unit (sdlc split, 2026-11-02 08:23).

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

**Acceptance gate:** the search in item 1, less the excluded files, finds nothing; a test whose timer doc differs from the row's doc shows that every changed reader takes the row's doc; basedpyright 0/0 on changed files and every caller module; each changed module's suite passes; a real `merge_gate.py --tests-only` and a real `add_unit.py --check` on a real unit read the row's worktree and branch; a real `resume_unit.py sdlc guild-process-unit --dry-run` passes; full gate green. No rerun test: only reads change.

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

### Phase 3v — Readers in other units' files read the row  · status: todo

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
It supports reruns and is deleted in Phase 18.
Test a `/clear` during migration: resolve current ownership again, report unavailable evidence, and preserve fixed store keys.
Make no unproved write and create no second row.

### Phase 3y — Middle stores keyed by staff id  · status: todo

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
It supports reruns and is deleted in Phase 18.
Test a `/clear` during migration: resolve current ownership again, report unavailable evidence, and preserve fixed store keys.
Make no unproved write and create no second row.

**Shared rename files:** this writer owns the rename-file changes for 3x, 3z, and 4a, including their tests and fixtures.
Each change lands inside its store's landing, with the migration and rewriter removal.

### Phase 3z — Shutdown and style stores keyed by staff id  · status: todo

**Owner:** shutdown-unit and style-unit (sdlc split, 2026-11-02 08:23).

**Depends on:** 3s; records stage of the rename track. Shared rename files have one owner. `restart.py` has one writer at a time: 4n and Phase 4 are done, so 3z writes it.

#### Work Order

**Goal:** shutdown records and style tickets hold the unit's staff id, and their rewriters go. The drained worker ledger retires in Phase 18 without a path migration.

**Spec:** The two store moves keep the Phase 3x rule, one store per landing with its own real proof. The ledger is retired separately:
1. **Worker ledger:** no key or path migration. Phase 18 owns direct retirement after drain. Keep its consumer and history proofs there. Current ledger paths and their rename rewriters stay until that audited retirement. The models unit reviews its owned files there.
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
- Phase 3h review (2026-11-01; sdlc ruling): the worker ledger gets no key or path migration. Rename keeps its existing small file move for the ledger until Phase 18 deletes the ledger after drain. No new code for the ledger.
- Phase 3h review (2026-11-01): the shutdown record holds the resume id with the name from staff; resume prompt text reads the folder from staff when sent.
- Phase 3h review (2026-11-01): workers: `impl` — the shutdown and style source; `test` — their tests.

**Acceptance gate:** for each of the two moved stores, its own landing and real proof (Phase 3x rule): a real migration stopped after the first file finishes on rerun, and a rerun of the finished one changes nothing and exits 0; a real shutdown and restart of a probe tab of `agents` reads its record by staff id; full gate green.

**Constraints from prior phases (native ids):** `staff.update_current_session` changes only `current_session` in one locked atomic write.
The fixed row id, child links, and store keys stay unchanged.
Add no store registry, identity alias, or live-rekey journal.
Parked files and retired references belong to stopped units.
The one-time identity migration writes reference stores first and staff last.
It supports reruns and is deleted in Phase 18.
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
It supports reruns and is deleted in Phase 18.
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

**Ruled out:** a pane found by herdr's `agent_session` value; it can be stale.

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

### Phase 4m — Port audit: every unit's port is in its `.unit.toml`  · status: todo

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

### Phase 4e — Legacy pane marks deleted; native runtime ids select rows  · status: todo

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
- Membership is native identity only: `Herdr._row_panes` takes a pane when its native process runs the row's `current_session`. A label, a title, a pane mark or herdr's `agent_session` never makes a pane a member.
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

### Phase 4g — Rename rebuilt on the row  · status: todo

**Depends on:** 4o, 4c, 4d, 4e, 4h, 4i, and the records stage (3x, 3y, 3z, 4a). Last step of the rename track. No pane phase waits for it. Hash 3f8339f5 stays unmerged; reuse only its tab retitle, kept-pane resume and finished-journal rerun.

#### Work Order

**Goal:** a unit rename changes the row's name and the live tab and session name; a showrunner rename changes the row and the workspace name; moving a unit's folder and branch is one further optional step. The rename sweep and most of the rename code go.

**Spec:**
1. **Unit rename** (`rename_unit.py`): `staff.rename` of the row, `Herdr.retitle` of the tab and pane, and the session name at the next launch (Phase 4). No store, doc, prose or Units cell is rewritten: they hold the staff id. Hub files, Gates and plan headers are deleted before this phase (4c, 4o), so nothing lists them (`EDITED_COLUMNS` :82 and `_plan_text` :497 go). Same-name rename is the repair path: it changes only the parts that differ and prints `unchanged` when all match.
2. **Folder and branch move** (`rename_unit.py --move-worktree --to-folder <path> --to-branch <name>`), rebuilt on staff and process evidence (sdlc, 2026-11-04):
   Require both arguments. Use the current devtools `unit_move.move_folder` operation (phase 4h): stop the session found by `current_session`, `git worktree move`, `staff.set_worktree`, then `restart.start_row`.
   Run `git branch -m` while the session is stopped. A rerun reads the folder and the branch from git and the row; no move journal, launch-record `cwd`, retained launch kind or `PendingFolderStartup` exists.
   A stopped unit updates its row and stays stopped.
   Each interrupted step finishes on rerun; a finished move prints `unchanged`.
   The only refusals are the 5 in `staff-lifecycle.md` section 4 (target folder exists, same-row concurrent operation, a second copy of a running session, uncommitted work deleted, a signal to a non-target process). Section 3 item 5: the move is optional and runs only on `--move-worktree`.
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

### Phase 4t — A test keeps the staff accessor list complete  · status: todo

**Depends on:** sdlc's docs/as-built/staff-accessors.md for its final run; the test is built first.

**Work Order:**

Write a test that fails when docs/as-built/staff-accessors.md misses a file that reads or writes the staff record (sdlc, 2026-11-04).
1. sdlc commits docs/as-built/staff-accessors.md soon. It is not in the tree yet. Build the test now against the rules below; the unit director merges the doc when it lands and tells you.
2. A file reads or writes the staff record when it imports the `staff` module (`import staff`, `from staff import`, `from scripts.production import staff`, `from scripts.production.staff import`), or names the record path `.local/state/staff/staff.json` or `staff.json` under the staff state folder, or runs `staff.py` as a command. Look at the tree with grep and make the rule match what you find; say in your summary which forms you found and which you count.
3. Scan tracked files under scripts/, commands/ and docs/ that are code or commands (.py, .sh, .md commands). Skip test files and test support only if the doc's rule says so; otherwise count them. Keep the scan fast: one pass, no subprocess per file.
4. The doc lists each accessor as a repo-relative path in backticks. The test reads every backticked path in the doc. It fails with one line per missing file, naming the file and the line that accesses the staff record. It also fails on a listed path that no longer exists.
5. Until the doc lands, the test skips with the message `staff-accessors.md not in tree yet` — never passes silently. Remove this skip once the doc is merged (the unit director tells you).
6. Prove the test with a temporary doc copy that misses one file: it must fail and name that file.
Files: scripts/production/test_staff_accessors.py (new). Edit nothing else.

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

### Phase 4l — One live, stopped or unknown reader for restore, restart and stop  · status: todo

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

### Phase 7 — worker_panes.py: start-or-resume, close, observe  · status: todo

**Owner:** models-unit, after 3n (sdlc, 2026-11-02). herdr-unit and models-unit settle each `herdr.py` window on the berth board.

**Pane track rule (sdlc, 2026-11-02):** each next pane-track phase goes to herdr-unit or models-unit, whichever is free first, by the dependencies. A phase owned by another unit (10 shutdown, 11 accounts) stays with that unit. Each checkpoint notice gives the through-Phase-12 date.

**Depends on:** Phase 1, Phase 3, Phase 3n, Phase 4, Phase 6, Phase 6c

#### Work Order

**Goal:** `worker_panes.py start-or-resume <short name> --role <role>` starts or resumes a worker in its parent's tab with the registry's settings for the role recorded on its staff row; `close` refuses a pane that still has work; `observe` joins worker rows and panes, with separate process anomalies and structural faults.

**Spec:**

1. **API and CLI.**
   ```python
   # worker_panes.py
   @dataclass(frozen=True)
   class OpenWorker:
       row: StaffRow
       pane: str                          # herdr pane id at this read
       terminal: str                      # herdr terminal_id, stable for the pane's life
       status: herdr.AgentStatus

   @dataclass(frozen=True)
   class StoppedWorker:
       row: StaffRow
       pane: str                          # the worker's labelled pane, at its shell
       terminal: str                      # no agent runs in it

   @dataclass(frozen=True)
   class ClosedWorker:
       row: StaffRow                      # complete evidence: no pane holds the worker

   @dataclass(frozen=True)
   class UncertainWorker:
       row: StaffRow
       reason: str                        # a structural fault prevents the operation

   @dataclass(frozen=True)
   class ProcessUnknownWorker:
       row: StaffRow
       pane: str
       terminal: str
       reason: str                        # process evidence is unknown

   @dataclass(frozen=True)
   class AdmissionRefused:
       reason: str                        # Phase 3h admission refused; no backend, no row; kept only if it prevents a catastrophe (sdlc, 2026-11-03)

   @dataclass(frozen=True)
   class IncompleteStart:
       row: StaffRow
       pane: str
       reason: str                        # the agent started but its startup did not finish (`AgentLaunched(..., PromptNotSubmitted(...))`); pane and agent stay (sdlc, 2026-11-04)

   @dataclass(frozen=True)
   class UnpublishedWorker:
       pane: str
       cause: str                         # backend made, staff publish failed; keep the pane, no row

   WorkerPane = OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker   # observation
   WorkerStart = OpenWorker | IncompleteStart | AdmissionRefused | UnpublishedWorker | ProcessUnknownWorker | UncertainWorker   # startup

   @dataclass(frozen=True)
   class Closed: ...

   @dataclass(frozen=True)
   class Kept:
       blocker: str

   class ExitCode(IntEnum):
       OK = 0
       REFUSED = 2
       KEPT = 7          # Phase 8 adds the dispatch outcomes 3-6
       UNCERTAIN = 8

   def start_or_resume(parent: StaffId, name: WorkerName, role: staff.WorkerRole) -> WorkerStart
   def close(parent: StaffId, name: WorkerName) -> Closed | Kept | UncertainWorker
   def observe(parent: StaffId, record: staff.StaffSnapshot) -> list[WorkerPane]
   ```
   The CLI is `worker_panes.py start-or-resume <short name> --role <role>`, `close <short name>` and `list [--json]`, and `ExitCode` is its one table of exit codes. Each command addresses a worker by its short name within the caller's parent row: the row selected by the caller's native runtime id (Phase 3h), which must be a unit row or a showrunner row (a showrunner holds its own Astra role, Phase 12); any other caller gets `REFUSED`. `close` gives `KEPT` with the blocker on stderr. An `UncertainWorker` gives `UNCERTAIN` with its reason and the hand step on stderr. `list` prints `observe` for that parent, with one `staff.snapshot()`.
2. **The pane evidence and the join, `observe(parent, record)`.** Read `Herdr.panes()` and `Herdr.agents()` once. Join by pane id to obtain each pane's terminal, label, conversation id, and agent. Structural evidence requires a successful pane read and a non-empty `terminal_id` on every pane. An empty terminal is incomplete evidence, never absence (Phase 3g). For each row of `record.find(kind="worker", parent=parent)` (Phase 2 As-built), in the parent's tab:
   - `OpenWorker(row, pane, terminal, status)`: exactly one pane has the worker's label, and its runtime conversation id equals `current_session`; `status` is its `AgentStatus` (Phase 6).
   - `StoppedWorker(row, pane, terminal)`: exactly one pane has the label, no agent carries that runtime conversation id, and a fresh `Herdr.pane_activity(pane)` (:1127) reads `PaneAtShell`. An exited agent leaves its shell open, and herdr clears its conversation id (Phase 3g).
   - `ClosedWorker(row)`: no pane has the label and no agent anywhere carries that runtime conversation id.
   - `UncertainWorker(row, reason)`: every other case, each with its own reason: the pane list is unavailable; incomplete terminal identity; duplicate panes; or a proved ownership conflict.
   - `ProcessUnknownWorker(row, pane, terminal, reason)`: the location is known, but process identity or activity is unreadable.
     Print the named anomaly and continue from known facts; this state never proves a stopped worker.
   Pane conversation state is a union, `PaneConversation(id) | NoPaneConversation`; it replaces the bare `str | None` of `PaneIdentity.session_id` and `TabSession.session_id` in `herdr.py` (sdlc, 2026-11-04). No `None` carries a state. The caller takes `record` from `staff.snapshot()`, one read for all the parents it observes. It is the one join: `list`, `close`, messages (Phase 9), shutdown (Phase 10), account moves (Phase 11) and every worker display (Phase 13) read it, and no other module joins staff rows with herdr panes. `Herdr.panes()` stays the one pane-list parser for its other callers.
3. **`start_or_resume`.** Resolve an existing published native-id row first. For a new worker, the Phase 3h admission gives `WorkerStartAllowed`; only backend creation followed by staff publication gives `PublishedWorker`. No row id is allocated before that boundary. Two callers serialize on parent and worker name before creation, so they get one backend and one row. A recorded-role mismatch is `REFUSED` with no change. All settings come from `row.role`, never from the name. Then, under the worker lock `~/.local/state/staff/locks/<id>.lock`, held through reconciliation, the launch and the launch proof, one fresh `observe` of the row decides:
   - `OpenWorker`: it is the worker; report done and change nothing (model and effort are looked up only at start or resume; a `/model` typed by hand holds; decision 2).
   - `StoppedWorker` (a launch killed after the split, or an agent that exited): start the agent in that pane with `launch_prepared_agent` and `ExistingPane(pane, terminal)` (Phase 6). The pane, its label and its terminal stay.
   - `ClosedWorker`: split the column (first worker: the parent's pane, `right`; later ones: the lowest worker pane, `down`) through `launch_prepared_agent` with a `SplitPane` whose label is the short name. The first split's ratio is `WORKER_SPLIT_RATIO` in `config/direct.conf` (`0.667` today), read at each split through `tunables.number(<direct.conf>, "WORKER_SPLIT_RATIO", minimum=0.1, maximum=0.9)` (Phase 3n; `UNIT_CONFIG` overrides the path in tests). Code holds no default; a `TunableUnusable` is `REFUSED` with its line, before any split.
   - `ProcessUnknownWorker`: print the named anomaly and continue with the shared reader; never treat unknown evidence as stopped proof.
   - `UncertainWorker`: return it and change nothing. No recovery closes and recreates a pane or a split (Delegation Context → Lifecycle reruns keep the pane).
   - **Launch and address.** Resume the recorded native id with `--resume <id>` or `codex resume <id>`. A fresh backend supplies its own id through 3h admission. No pane or terminal is stored on the row (sdlc, 2026-11-04; spec section 2): each operation finds the pane in one fresh `observe` from the process that runs `current_session`. A missing or duplicate terminal is uncertain, never a reason to open a replacement. Herdr's agent name is only a display label: its helper normalizes and cuts at 32 characters. Every prompt, wait, stop and lookup addresses the pane id from that observation and passes `current_session` as its `target`. Never pass a full runtime id as an agent-name address. A stop after the split but before publication reconciles the labelled pane under the parent/name lock, proves its backend id and publishes once.
   - **Settings.** Kind, model, effort and service tier come from one typed call that models-unit adds to `models_registry.py` in this phase: `launch_settings(section, key) -> LaunchSettings(kind, model, effort, service_tier)`, with tests that each worker role resolves as the bash launchers resolve it today. Section and key: `impl`, `test`, `fix` or `review` in `[direct.<kind>]` of `config/models.conf`; `designer` or `reviewer` in `[design_roles.<kind>]` for `astra-designer` and `astra-reviewer`. The registry holds no sandbox, so the sandbox comes from the recorded role: `review` and `astra-reviewer` read-only (`READ_ONLY_SANDBOX`); `astra-designer` from its `design_roles.py` access policy (`launch_sandbox`); `impl`, `test` and `fix` the `WORKER_SANDBOX` that `codex_mesh.py:323` uses today. One private function, `_registry_row(role: staff.WorkerRole)`, holds the role to section, key and sandbox mapping. The account comes from the accounts code as today (Codex: the `workers` role).
   - **Env.** The pane env carries `UNIT_TEAM_ROLE=<recorded role>` and no `STAFF_ID`, `UNIT_RUN_DIR` when the parent is a unit, `TMPDIR` and the dev shell.
   - Then `_layout`.
4. **`close`**, under the worker lock, takes one fresh `observe` of the row:
   - `ProcessUnknownWorker`: print the named anomaly and continue known work; close only through the confirmed pane-close operation.
   - `ClosedWorker`: run `_layout` and report done.
   - `UncertainWorker`: return it and change nothing.
   - `OpenWorker`: it passes only `can_close`: dispatch lock free; the last turn ends with the `DONE` line of its last dispatch (decision 4) and the launcher recorded that pass complete; no input after it; state `idle` or `done`; no live detached run of the worker. Otherwise it keeps the pane and names the blocker. It then ends the agent through the per-member stop path (`shutdown.py stop <staff id>`, Phase 3n; the conversation stays) and goes on as below.
   - `StoppedWorker` (also after that stop): dispatch lock free and no live detached run, else `Kept`. A second `Herdr.panes()` read must find the same `terminal` on exactly one pane, and a fresh `pane_activity` of that pane must read `PaneAtShell`; only then call Phase 6's confirmed pane close (and retire a Codex compatibility record if one exists), then `_layout`. A moved, missing or busy terminal gives `UncertainWorker` with that reason, and nothing is closed.
5. **`_layout`** (private): under a per-tab lock, set every split in the worker column to an even share. It changes only panes that `observe` gives as `OpenWorker` or `StoppedWorker` of a worker row whose parent is this tab's parent row, found by their `terminal`; it leaves other panes as they are, and gives focus back where it was. Even shares already in place change nothing.
6. A rename or a transfer of the unit or its showrunner changes no worker pane, agent or session (Phase 3).

**Files:**
- `scripts/herdr/worker_panes.py` — new.
- `config/direct.conf` — `WORKER_SPLIT_RATIO=0.667`, with a comment line.
- `scripts/herdr/test_worker_panes.py` — new.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — `PaneConversation | NoPaneConversation` (sdlc, 2026-11-04).
- `scripts/herdr/fake_herdr.py` — the fake's `agent list` and `pane process-info` answers that the tests need.
- `scripts/lib/models_registry.py`, `scripts/lib/test_models_registry.py` — `launch_settings` and `LaunchSettings` (models-unit's files).
- `scripts/production/staff.py`, `scripts/production/worker_admission.py`, `scripts/lib/tunables.py`, `scripts/shutdown/stop_resume.py` — read only: the staff API, the tunables reader and the per-member stop.

**Workers:** `1 writer + 1 tester` — split by source and test lane.
- `impl` — `scripts/herdr/worker_panes.py`, `scripts/herdr/herdr.py` (the pane conversation union), `scripts/lib/models_registry.py`, `config/direct.conf`; hub: `scripts/herdr/worker_panes.py` (the API every test calls).
- `test` — `scripts/herdr/test_worker_panes.py`, `scripts/herdr/test_herdr.py`, `scripts/lib/test_models_registry.py` and `scripts/herdr/fake_herdr.py`, written from the Spec, against a probe tab and the fake.

**State:** staff rows; panes; `~/.local/state/staff/locks/<id>.lock`.

**Constraints from prior phases:** Phase 1 `staff.py` (`find`, `WorkerName`, `StaffId`); Phase 3n `staff.ensure_worker(native_id, name, parent, role)`, `WorkerRole` and the row's `role`, `tunables.number` and `TunableUnusable`, and the per-member stop `shutdown.py stop <staff id>`; Phase 3g pane facts (Phase 6 Constraints); Phase 2 `staff.snapshot() -> StaffSnapshot` (one record read, with `by_id`, `by_path` and `find`; it raises `StaffUnreadable`); Phase 3 unit rows kept current by the writers; Phase 3h native runtime lookup; Phase 4 row names; model, effort, tier and sandbox come from the models unit's registry API (`RegistryRow` lookups), in `models_registry.py` (Phase 5 names; `AGENTS_CALLER_FAMILY` and `AGENTS_CALLER_ASSIGNMENT` keep their names); Phase 6 `SplitPane` with its label, `ExistingPane(pane, terminal)`, `split_ratio`, `label_pane`, `AgentStatus` and the probe results (the label probe included). Phase 4f: a pane is a row member only when its native process runs the row's `current_session` (`Herdr._row_panes`); a label, a title or a pane mark never makes a pane a member and never permits a close; a stopped row gets a saved binding, not a move. Phase 4f `move_row_tabs` rebuilds a shared tab, so a transfer can change public pane ids while it keeps terminals, and an interrupted rebuild (F008) can leave worker panes in a source tab: read pane locations fresh across workspaces after a transfer, never from before it.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): `start_or_resume` also returns `AdmissionRefused(reason)` and `UnpublishedWorker(pane, cause)` (backend made, staff publish failed: keep the pane, dispatch nothing, make no row), from 3h's `WorkerStartAllowed` / `PublishedWorker`. Observation and resume compare `current_session`.
- Phase 3s review (2026-11-02): a Codex worker's `OpenWorker` matches the Codex conversation id (thread id); a Claude worker's matches the Claude session id. `herdr.native_process` falls back to `CODEX_THREAD_ID` when no rollout is open, and that value can name the launch thread after `/new`. It is not proof of the current thread: give unknown evidence, and add an idle-thread-change test that opens no other backend.
- Phase 3s review (2026-11-02): publish complete result unions in this phase, `AdmissionRefused` and `UnpublishedWorker` included, in the signatures. Phases 8-13 handle each one with tests: failed publication, unknown process evidence, and no unproved delivery. `IncompleteStart` gets no dispatch, no success report and no other backend (sdlc, 2026-11-04).
- Phase 6c (2026-11-03), changed by Phase 4r: a start whose startup fails returns `AgentLaunched(..., PromptNotSubmitted(...))` and keeps the pane; `start_or_resume` gives `IncompleteStart`. A failed start whose agent generation cannot be proved gives `failed agent generation is unavailable; pane left open`, sends no signal and keeps the pane, possibly with the agent still running. A rerun of `start-or-resume` that finds that pane by its label goes through `ExistingPane`, which refuses unless the pane reads `PaneAtShell`; report that refusal with its reason and never close the pane. On darwin, `AgentProcessIdentity.start_ticks` holds libproc start microseconds or `ProcessGenerationUnavailable`; compare identities only by equality.

**Acceptance gate:**
- basedpyright 0/0 on the new files and every caller module.
- Tests: start; resume by id; the row id is the native runtime id, the pane found from its process addresses the worker, and the pane label is the short name, and the settings the recorded role's registry row (`impl1` with role `impl` → `[direct.<kind>] impl`, `fix1` with role `fix` → `fix`, `designer1` with role `astra-designer` → `[design_roles.<kind>] designer`); a launcher name whose recorded role differs from its name (`impl2` with role `review`) gets the `review` row; a `--role` that differs from the row's recorded role is refused and changes nothing; a caller whose native runtime id selects a showrunner row starts a worker under it; a caller whose native runtime id selects a worker row is refused; two `start-or-resume` calls at once for one name give one row and one pane; `close` refused with work left; a foreign pane untouched.
- Join tests: `observe(parent, record)` reads its rows only from `record` and gives each state: `OpenWorker` with pane, terminal and status; `StoppedWorker` for a labelled pane at its shell after its agent exits (conversation id cleared); `ClosedWorker`; and `UncertainWorker` with its own reason for an empty `terminal_id`, a failed herdr read, two labelled panes, an agent on another pane, and a proved ownership conflict. Unknown process evidence has its separate note and does not cause `UNCERTAIN`. `start_or_resume` and `close` change nothing on an `UncertainWorker` and exit `UNCERTAIN`. `close` re-reads the pane by `terminal` and a fresh `pane_activity` before the confirmed pane close, and a terminal that moved between the two reads is not closed.
- Config test: a changed `WORKER_SPLIT_RATIO` in a test `direct.conf` (`UNIT_CONFIG`) sets the first split's ratio with no code change; a missing or bad key is refused before any split.
- Rerun tests (Invariants → Rerun-safe tools and Lifecycle reruns keep the pane), each compared with one clean run, each showing the same tab, pane id and `terminal_id` after the rerun: `test_start_rerun_after_row_write`; `test_start_rerun_after_split` (the labelled pane with no agent is reused through `ExistingPane`; no pane closes and no second split opens); `test_start_rerun_after_agent_exit` (an agent that exited is started again in its own pane); `test_start_rerun_after_agent_start` (the live agent is kept, and no second pane opens); `test_start_rerun_after_launch_proof` (on an open pane it reports done and changes nothing); `test_close_rerun_after_agent_stop` (stopped after the agent ends and before the confirmed pane close: the rerun closes that terminal's pane once); `test_close_rerun_after_pane_close_before_layout` (the rerun gives the panes and ratios of one clean close); a rerun of a finished start or close reports done, changes nothing and exits 0.
- Full gate green.

**Acceptance gate (native ids):** use two runtime ids with the same first 32 characters and prove that pane-id prompts, waits and stops reach only the selected worker. Test the 3h permission/publication boundary and failed publication after backend creation. Test a cross-workspace move with a changed pane id: the next `observe` finds the pane from its process and addresses only that pane. Test `IncompleteStart`: the pane stays and nothing is dispatched. No success returns an unpublished worker; no env or record recreates `STAFF_ID`.

**Pane close contract:** use Phase 6's confirmed pane close; `pane_launch.retire_launch` only for a Codex compatibility record.
Preserve the director and sibling panes; test one retained launch after replacement and recovery after failed closure.

**Process anomaly rule:** unknown process evidence prints a named note and does not refuse the operation.
Resolve the name from the herdr tab, then the session registry, then use the pid.
A failed name read does not refuse.
Keep unknown process evidence separate from `UncertainWorker`, which represents a structural fault.
Refuse only for a target proved live when the requested step requires it stopped.
A structural fault (a missing terminal, duplicate panes, an ownership conflict) gives `UncertainWorker`: that worker is left alone with one anomaly line, and other work continues (review, 2026-11-04).
Unknown evidence supplies no permission to signal an unowned process or close a sibling pane.
Test continuation after unknown evidence and after a structural fault, named stderr notes, and refusal for a proved live target.

### Phase 8 — Dispatch and the launcher switch, landed dark  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** `scripts/production/worker.py` is the one worker command. With `WORKER_TRANSPORT=herdr` pinned in a run, `implement.sh`, `review.sh` and `assign.sh` dispatch through `worker.py`, whose receipt is the worker's transcript; the default stays `mesh`.

**Spec:**

0. **Worker command, panes only.** `scripts/production/worker.py` is the entry point for `/unit:worker` and `/showrunner:worker`. Resolve the parent from the caller's native runtime identity; nobody supplies a parent or derives it from a name. `list` shows current workers with name, recorded `WorkerRole` and `BusyWorker | IdleWorker | StoppedWorker` state. `assign <role> <prompt>` selects the first idle worker in role-number order, or resumes a stopped worker; if all are busy, print their names and refuse, exit 2. `add <role>` creates the lowest free name (`role`, `role2`, `role3`) through Phase 3h admission. `end <name>` stops the worker and deletes its row, retaining history outside staff. `restore` resumes all retained workers on their recorded session or thread, leaves running workers alone, and on a finished rerun prints `unchanged`, exits 0 and changes nothing. `check` is read-only, lists each wrong worker, and exits 0 only if every row has a running or resumable worker; wrong rows exit 1. Admission refusal keeps the exact `staff: <worker>: not opened: <reason>` line and exit 2; no work is sent after a refusal.
   The first assignment creates the default impl/fix/review/test team; Astra roles are created on their first assignment. There is one selector and one managed pane-worker start path. Unrelated agents get no worker row. `worker.py` owns selection, reuse and admission orchestration; `worker_panes.py` owns pane start/observe/close primitives. The existing implement, review and assign launchers call `worker.py` inside and keep working through Phase 18. Worker removal and held Astra roles use this same command API. Phase 10 integrates agents up/restart, shutdown restart and unit resume with `worker.py restore`. Retire, wrap and promote stop workers for resume and keep their rows; only worker end and unit removal delete them.

1. **API and CLI.** `worker.dispatch(parent: StaffId, name: WorkerName, role: staff.WorkerRole, prompt: Path, dispatch_id: str) -> ExitCode`; command dispatch goes through `worker.py assign`, using the one selector. Pane operations use the Phase 7 primitives; no second selector is added. Each launcher passes the role explicitly: the role it already records for the pass (`implement.sh` `impl`, `test` or `fix`; `review.sh` `review`; `assign.py` the role of each slot it assigns). No launcher or `dispatch` parses a role from the name. Phase 7's `ExitCode` gains `NO_DONE = 3`, `BLOCKED = 4`, `STALLED = 5` and `BUSY = 6`; `OK` means complete.
2. **The transcript is the receipt (decision 4).** `dispatch` keeps no record of its own of a submission. Its steps:
   1. Take the worker's dispatch lock (non-blocking; held → `BUSY`).
   2. `start_or_resume(parent, name, role)`: an `OpenWorker` goes on; an `UncertainWorker` returns `UNCERTAIN` with its reason and types nothing; a role that differs from the row's recorded role returns `REFUSED`.
   3. Read the worker's own transcript:
      - a final reply that ends with `DONE <dispatch id>` → finish: write the reply file (for a reviewer, the findings file) and return `OK`; type nothing;
      - the typed line with this dispatch id and no such reply yet → the pass went in: go to the wait (step 5); type nothing;
      - no line with this dispatch id → the `OpenWorker`'s `status` must be `idle` or `done`, else `BUSY` (a rerun later finds the line when the turn was this pass); then type (step 4).
   4. Type one line, `herdr agent prompt <pane id> "Read <prompt path> and do the work it gives. End your final reply with the line DONE <dispatch id>." --wait` (no timeout, no `--until`; decisions 5, 7). The prompt file is never changed.
   5. Wait. When herdr settles, read the transcript again: the `DONE <dispatch id>` reply → finish as in step 3; `blocked` → `BLOCKED`; settled without the line while `herdr` status for the observed pane id says `working` → `herdr agent wait <pane id>` again; settled without the line → `NO_DONE`, and the director reads the pane; `agent_prompt_stalled` or a timeout → `STALLED`, and the line is never typed again.
3. **The launchers** keep their status, recorder, board and summary work, and write `implemented` only on `OK`. `unit_runs.worker_transport(run)` reads `WORKER_TRANSPORT` from `config/direct.conf` at run start and pins it in the run folder.
4. **Restart.** `inventory.py` keeps recording `launcher_command` (:861). A rebuilt launcher runs with the same dispatch id, so it reads the transcript first and types nothing when the line is already there (Phase 10).
5. Several implementers use the lowest free names `impl`, `impl2`, as the command contract requires. Transport-aware text in `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<FixDispatch/>`, `<PhaseCleanup/>`) lands with this phase, branched once on the run's pin.

**Files:**
- `scripts/production/worker.py`, `scripts/production/test_worker.py` — new: command API, selector, admission, dispatch and command tests.
- `commands/unit/worker.md`, `commands/showrunner/worker.md` — the two command entry points.
- `scripts/herdr/worker_panes.py`, `scripts/herdr/test_worker_panes.py` — pane primitives used by the command.
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/assign.sh`, `scripts/direct/assign.py`, `scripts/direct/remove_workers.py`, `scripts/astra/design_roles.py` — the launcher switch; `assign.sh` execs `assign.py`.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — `worker_transport(run)`.
- `config/direct.conf` — `WORKER_TRANSPORT=mesh`.
- `commands/unit/direct.md` — the transport-aware text.
- `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py`, `scripts/direct/test_assign_launcher.py`, `scripts/direct/test_remove_workers.py`, `scripts/astra/test_design_roles.py` — the launcher tests.

**Workers:** `2 writers` with disjoint files.
- `impl` — `worker.py`, `test_worker.py`, pane primitives and their tests, `unit_runs.py`, `test_unit_runs.py`, `direct.conf`, and the two worker command files; hub: `worker.py` (the one command API).
- `test` (opens as impl) — implement/review/assign launchers, worker removal and held Astra callers, their tests and `commands/unit/direct.md`. These callers use the API; they own no selector.

**State:** run pin file; dispatch locks. No submission record: the transcript holds it.

**Constraints from prior phases:** Phase 3n: the role is recorded on the worker's staff row, set once by `staff.ensure_worker(native_id, name, parent, role)`; `WorkerName.parse` checks shape only. Phase 7 `start_or_resume(parent, name, role) -> WorkerStart` (`OpenWorker`: `row`, `pane`, `terminal`, `status: herdr.AgentStatus`), `observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]`, `can_close`, `ExitCode` (with `UNCERTAIN = 8`), locks. Every file write this phase adds or changes (the reply file, the findings file, the run pin) calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). Phase 4f: a worker turn that ends during a detached run can replace its summary with one line; test that such a turn produces no completed pass and does not replace the full report.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): dispatch, restore and check take Phase 7's outcomes; an unpublished backend is not a missing worker.

**Acceptance gate:**
- basedpyright 0/0 on changed Python files and every caller module; `bash -n` and `shellcheck` with no new findings on the changed `.sh` files.
- Tests for long, fast, blocked, false-idle and stalled turns; two launchers at once get one dispatch and one `BUSY`; a mesh-pinned run is unchanged; a launcher dispatch to a worker whose name suffix differs from its recorded role (`impl2` with role `review`, passed `--role review`) uses the `review` settings, and the same dispatch passed `--role impl` is refused and types nothing; an `UncertainWorker` gives `UNCERTAIN` and types nothing.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_dispatch_rerun_before_prompt` (stopped after the lock and the start, before the line is typed: the rerun types it once); `test_dispatch_rerun_after_prompt` (stopped after the line is typed: the rerun finds it in the transcript, waits for the same pass and types nothing); `test_dispatch_rerun_after_done` (stopped after the `DONE` reply and before the reply file: the rerun writes the reply file and types nothing); a rerun of a finished dispatch returns `OK` and changes nothing. After each, the prompt file is byte for byte the same.
- Full gate green.

**Acceptance gate (worker command):** test fail-closed add before start and publication refusal after native-id creation; lowest free name; all-busy assign refusal with names; stopped-worker reuse; restore idempotence on the same ids; check exits 0/1; worker end deletion versus resumable stop; an unrelated start makes no row. All old launch commands continue through the one API until Phase 19. Native-id lookup and observed pane-id addressing are used throughout.

**Process anomaly rule:** unknown process evidence prints a named note and does not refuse the operation.
Resolve the name from the herdr tab, then the session registry, then use the pid.
A failed name read does not refuse.
Keep unknown process evidence separate from `UncertainWorker`, which represents a structural fault.
Refuse for a target proved live when the requested step requires it stopped, or for a structural fault.
Structural faults include a missing terminal, duplicate panes, and an ownership conflict.
Unknown evidence supplies no permission to signal an unowned process or close a sibling pane.
Test continuation after unknown evidence, named stderr notes, and refusal for a proved live target or structural fault.

### Phase 9 — Messages, detached runs and review Stage 2  · status: todo

**Depends on:** Phase 7, Phase 8

#### Work Order

**Goal:** messages, detached-run results and review Stage 2 reach a pane worker, and none is lost.

**Spec:** `send.py` `worker:<unit>/<short name>` is an address a person or a director types, so `send.py` takes one `staff.snapshot()` and resolves the address through its `by_path` (under the sender's showrunner; `worker:<showrunner>/<unit>/<short name>` from outside it) to its native row id. An address that names no worker row is refused (`REFUSED`, exit 2), and nothing goes to the director. The worker comes from `worker_panes.observe(<unit id>, record)` (Phase 7) on the same snapshot: an `OpenWorker` gets the text with `herdr agent prompt <pane id> <text>`; `agent_blocked`, a `StoppedWorker`, a `ClosedWorker` or an `UncertainWorker` (its reason in the line) → the message goes to the director with one line saying why. `scripts/agents/mesh_runs.py` becomes `scripts/agents/detached_runs.py`: a run finds its worker by its native Claude runtime session id or `CODEX_THREAD_ID` and types its result into the pane. The typed result carries the run id, and a delivery first reads the worker's transcript and types nothing when that line is already there (decision 4). `assign.py` calls `worker.py` for Stage 2 through `dispatch` with the dispatch id `<dispatch id>-2`, so its line, its `DONE <dispatch id>-2` reply and its rerun follow Phase 8, and the review launcher waits for that line. Usage refusals and settled-without-`DONE` turns go through one classifier, `refusal_watch.transcript_refusal` (`scripts/accounts/refusal_watch.py:464`), which `settle.py` already calls (:121): it gains Codex transcripts and pane screen text as the fallback (transcript evidence first); the pass status says `refused`, the pass stays open, and the director re-dispatches it after the move.

**Files:**
- `scripts/message/send.py`, `scripts/message/test_send.py` — `worker:` addresses.
- `scripts/agents/mesh_runs.py`, `scripts/agents/test_mesh_runs.py` — become `scripts/agents/detached_runs.py` and its test; every `mesh_runs` reference follows in the same commit.
- `scripts/direct/assign.py`, `scripts/direct/test_assign_launcher.py` — Stage 2 through `dispatch`.
- `scripts/accounts/refusal_watch.py`, `scripts/accounts/test_refusal_watch.py` — the one classifier: `transcript_refusal` (:464) gains Codex transcripts and screen text as the fallback; agreed with the accounts unit.
- `scripts/herdr/worker_panes.py`, `scripts/production/staff.py`, `scripts/lib/atomic_file.py` — read only: `observe`, `snapshot` and the one file writer.

**Workers:** `2 writers` — `impl`: `send.py`, `detached_runs.py` and their tests; `test` (opens as impl): `assign.py`, the classifier and their tests (review, 2026-11-04).

**State:** none new.

**Constraints from prior phases:** Phase 2 `staff.snapshot()`; Phase 7 `observe(parent, record) -> list[WorkerPane]` (`ProcessUnknownWorker` included); Phase 8 `worker.dispatch`, its transcript receipt and `ExitCode`. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). `send.py` has two own writers, `RELAY_EXEC` (:212, its `os.replace` at :219) and `write_atomic` (:589, its `os.replace` at :592); a write this phase adds goes through `atomic_file`, not through them. Phase 4f: test result delivery after the launcher exits, and completion only after the matching `DONE` reply.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): the address is `worker:<holder>/<name>`, where the holder is the unit or showrunner row, resolved through the staff parent; a blocked or closed worker's message goes to its holder.
- Phase 3s review (2026-11-02): `worker:<unit>/<short name>` extends `resolve_staff_recipient` in `scripts/message/send.py` (main 11de522d6), with no other lookup. Merge origin/main with that commit before dispatch. Keep its ambiguity and transport handling.
- Phase 3s review (2026-11-02): handle `ProcessUnknownWorker` (Phase 7) with a test.

**Acceptance gate:** basedpyright 0/0 on changed files and every caller module; tests: a message during a turn arrives; a blocked, stopped, closed or uncertain worker's message reaches the director, with the reason in its line; a `worker:` address that names no row is refused with exit 2 and reaches no one; Stage 2 waits for its own line; rerun tests (Invariants → Rerun-safe tools): `test_delivery_rerun_after_type` (a detached run's delivery stopped after it types its result, then run again, types nothing), a delivery run twice types its result once, and a Stage 2 rerun after its line types nothing; full gate green.

### Phase 10 — Shutdown and restore of split tabs (shutdown unit)  · status: todo

**Depends on:** Phase 3n, Phase 4, Phase 6, Phase 7, Phase 8, Phase 9. Shared gate with Phases 10/12/13 over the exact final batch.

#### Work Order

**Goal:** a shutdown stops worker panes inside the configured bound and keeps each pane, and a restart brings each director tab back with its workers resumed.

**Spec:**

1. **Shutdown.** Before the voluntary window, the shutdown record lists only the live worker row ids, taken from `worker_panes.observe(<parent id>, record)` (Phase 7) with `record` from one `staff.snapshot()` for the whole shutdown (staff-lifecycle.md section 5; sdlc, 2026-11-04). Pane, terminal, pid, start time, scope and run are not copied: restore finds them again from staff and process evidence. The one dispatch fact a busy worker needs, its dispatch id and launcher command, stays in its owner, the Phase 8 dispatch record, and restore reads it from there. A `ClosedWorker` is not listed. An `UncertainWorker` or `ProcessUnknownWorker` is listed with its reason, stopped by nothing, and reported as not proved stopped. The record stores no name: a reader takes the name from the row. Each worker's agent ends through the per-member stop (Phase 3n item 5), and its pane stays open at its shell, so a stop and its rerun keep the same tab, pane and terminal. The worker stops count in their director's bounded attempt (the entry's `stop_deadline`, `shutdown_record.py:533`; `bounded_attempt_finished` :624), and success needs a fresh observation of each listed worker: its `terminal` found again on one pane that reads `PaneAtShell`, and no live process. **The bound comes from config.** `STOP_GRACE_SECONDS` and `KILL_GRACE_SECONDS` (`stop_resume.py:42-43`, used at :133 and :143) move to `config/shutdown.conf` (new: `STOP_GRACE_SECONDS=2`, `KILL_GRACE_SECONDS=5`), read at each stop through `tunables.number` (Phase 3n; `SHUTDOWN_CONFIG` overrides the path in tests); the constants go, and code holds no default. The resume tunables move with them: `START_SECONDS`, `STAY_SECONDS` and `RESUME_ATTEMPTS` (`stop_resume.py:44-46`, used at :281, :342 and :275) go to the same file (`START_SECONDS=20`, `STAY_SECONDS=10`, `RESUME_ATTEMPTS=3`), read at each resume the same way. An operation deadline can shorten the times, never extend them (Delegation Context → No hard-coded config). The worker stops of one director run at once, so the set ends inside one `STOP_GRACE_SECONDS + KILL_GRACE_SECONDS` plus the fresh observation. A rerun of a stopped shutdown reads the record, stops the agents still alive, and writes no second entry for a pane already listed.
2. **Restore** follows Phase 6's probe (questions list item 1(e)). If herdr's own resume keeps each pane's env, herdr brings the panes back and our code builds no pane. Otherwise one locked tab transaction rebuilds the director (its name from its row, Phase 4), then each worker with `start-or-resume` and the row's recorded role. A worker pane that is still open at its shell (`StoppedWorker`) is reused through `ExistingPane` (Phase 7), so no restore closes and recreates a pane or split; only a `ClosedWorker` gets a new split. The split-tab record (the listed workers, their order in the column and their busy passes) is this phase's; the per-member stop and resume are Phase 3n's.
3. **One launcher step ends both branches.** After the panes settle, for each worker the record lists as busy, restore runs the `launcher_command` from its Phase 8 dispatch record once with the same dispatch id. `dispatch` reads the transcript first (Phase 8), so the one launcher finishes a pass that ended, waits on a live one, or types the line once when it never went in. A busy worker's resume text names its dispatch id, so the reply that finishes the pass ends with the same `DONE <dispatch id>` line.
4. Shutdown, moves, and launchers use the one builder, `resume_message` in `scripts/lib/resume_message.py`. Its `workers: ResumeWorkers` gains the pane workers: `PaneWorkerResume(staff_id: StaffId, turn: BusyPass | IdleWorker)`, where `BusyPass(dispatch_id)` makes the text name that dispatch id. Phase 3n removes its seat lines; no second resume-text builder appears. `Herdr.sort_tabs` (`herdr.py:1380`) sorts a multi-pane tab by its director pane.
5. **Resumable worker stop.** `stop_resume.stop_unit_workers(unit: StaffId, record: staff.StaffSnapshot) -> list[str]` uses the shared per-member stop, proves each agent stopped or reports its uncertainty, and keeps every worker row and native backend identity. A stopped pane can stay at its shell; a pane may close only after fresh terminal and shell proof. Retire, wrap and promote call this resumable operation, so unit resume or the surviving holder can call `worker.py restore` on the same sessions. They never call worker end or `staff.retire` on these workers. A proved live target or structural fault blocks destructive worktree removal; unknown process evidence prints the named note.
6. **Permanent ending.** Only `worker.py end` and permanent unit removal stop and delete worker rows. They share the end operation, preserve history outside staff and never delete a proved live worker or bypass a structural fault. A rerun finishes remaining workers once. Restore callers (`restart.py` `up_result` and `restart`, which serve agents up/restart and shutdown restart, and `resume_unit.py`) call `worker.restore(parent)` after proving the parent session. A finished restore prints `unchanged`, exits 0 and keeps ids, panes and terminals.

**Files:**
- `scripts/shutdown/inventory.py`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/shutdown_record.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/pane_restore.py` — stop, record, restart and restore; `stop_unit_workers` (new) and the config read of `STOP_GRACE_SECONDS`, `KILL_GRACE_SECONDS`, `START_SECONDS`, `STAY_SECONDS` and `RESUME_ATTEMPTS` in `stop_resume.py`.
- `scripts/lib/resume_message.py`, `scripts/lib/test_resume_message.py` — the shared builder, pane workers, and complete account-move values.
- `config/shutdown.conf` — new: `STOP_GRACE_SECONDS=2`, `KILL_GRACE_SECONDS=5`, `START_SECONDS=20`, `STAY_SECONDS=10`, `RESUME_ATTEMPTS=3`, with a comment line each.
- `scripts/production/retire_unit.py`, `scripts/production/production_lifecycle.py`, `scripts/production/promote_showrunner.py` — resumable worker stop.
- `scripts/production/resume_unit.py`, `scripts/production/worker.py`, `scripts/production/test_resume_unit.py`, `scripts/production/test_worker.py` — shared restore and permanent-end integration.
- `scripts/production/test_retire_unit.py`, `scripts/production/test_production_lifecycle.py`, `scripts/production/test_promote_showrunner.py` — their tests.
- `scripts/lib/unit_runs.py`, `scripts/lib/tunables.py`, `scripts/production/staff.py`, `scripts/herdr/worker_panes.py`, `scripts/lib/atomic_file.py` — read only.
- `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_stop_resume.py`, `scripts/shutdown/test_shutdown_record.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_pane_restore.py` — the tests.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — tab sort (`sort_tabs` :1380).

**Workers:** `2 writers` — split by stop and restore.
- `impl` — stop and record (`inventory.py`, `stop_resume.py`, `shutdown_record.py`, `config/shutdown.conf`), `stop_unit_workers` and its callers (`retire_unit.py`, `production_lifecycle.py`, `promote_showrunner.py`), plus `resume_unit.py`, `worker.py`, `scripts/lib/resume_message.py`, and their tests; hub: `scripts/shutdown/stop_resume.py` (`stop_unit_workers`, which lifecycle callers use).
- `test` (opens as impl) — restart, restore, tab sort (`restart.py`, `pane_restore.py`, `herdr.py`) and their tests.

**State:** shutdown records; retained stopped worker rows; deletion only through permanent end or unit removal.

**Constraints from prior phases:** Phase 2 `staff.snapshot()`; Phase 3r retains removed unit rows. Retire, promote, and wrap stop workers for resume. Only a proved live target blocks removal; a structural fault leaves that worker alone with one anomaly line (review, 2026-11-04). Phase 4 names from rows; Phase 3h native runtime lookup with no copied staff identity; Phase 3n (the per-member stop and resume by name or staff id; the recorded role on the row; `tunables.number`); Phase 6 (the restore probe; `ExistingPane`); Phase 7 (`start_or_resume(parent, name, role) -> WorkerStart`, `close`, `observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]`, each open or stopped pane with its `terminal`); Phase 8 (`dispatch` reads the transcript first); Phase 9 (a busy review Stage 2 pass has the dispatch id `<dispatch id>-2`, and `detached_runs.py` finds its worker by native runtime identity and reads the transcript before it types, so both reach a restored pane). Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). Phase 3's pending retirement (ids a wrap could not retire while worker rows were left) lives in the staff record; shutdown and restore keep it unchanged. `shutdown_record.py` `_write_record` (:1154) makes its own temp file (`mkstemp` at :1156) and `os.replace` (:1167); this phase changes the shutdown record, so `_write_record` calls `atomic_file.write_text_atomic` instead, inside the same `_record_deadline` checks. Shutdown's checkpoint (2026-10-30): every resume runs in the session's own tab with `Herdr.run_in_pane` (`herdr.py:1080`) through `accounts.py exec <account>`. Ordinary and split restores keep conversation id, account environment, identity, tab label and position, and their tests show it. Phase 4f: multi-pane tab order is `Herdr.sort_tabs` (herdr.py, about line 1662), with `tab.move`; reuse it. Own the worker transfer-to-restart test: the old shell stays in the source group and restart opens the worker in the new group. Changing `fake_unit_host.py`, `transfer_unit_test_support.py` or `LayoutHerdr` runs the whole `scripts/production` suite in the gate; a live-to-stopped fixture step updates the foreground evidence and keeps every other reply, such as an installed close failure.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): `staff.promote_unit` (`staff.py:869`) changes the row's kind and keeps its id and children; test that workers restore under the promoted holder after a `/clear`. The stop, record and lifecycle tests go to the first writer, restart and restore tests to the second.

**Acceptance gate:**
- Record-free recovery (sdlc, 2026-11-04): a crashed Claude worker or director with no launch record resumes from its transcript; a worker not proved stopped, or still running, is named in the shutdown report. The record holds only live row ids, and a test reads it back.
- basedpyright 0/0 on changed files and every caller module.
- Shutdown tests with worker panes inside the configured bound (one `STOP_GRACE_SECONDS + KILL_GRACE_SECONDS` plus the observation); a changed `STOP_GRACE_SECONDS` and `KILL_GRACE_SECONDS` in a test `shutdown.conf` (`SHUTDOWN_CONFIG`) change the bound with no code change, and a missing key stops the shutdown before any signal with one line naming the file and key; a changed `START_SECONDS`, `STAY_SECONDS` or `RESUME_ATTEMPTS` in that file changes the resume wait or attempt count with no code change; an `UncertainWorker` is reported as not stopped; restore tests for both probe branches; a restored director takes its row's name; a restore with a worker pane still at its shell reuses that pane and its terminal.
- Real proofs (Invariants → Lifecycle reruns keep the pane), in a probe tab of `agents` with real worker panes: a rerun of a finished stop, down and restart each changes nothing, keeps the same tab, pane id and `terminal_id`, and exits 0; one real shutdown stopped after its first worker stop finishes on rerun, with each pane listed once.
- The worker stop test: a crashed director, no shutdown record, and a Codex worker whose working folder is outside the unit's worktree; `retire_unit.py` stops the worker through `stop_unit_workers`, proves the agent stopped, keeps the worker row and native id, and marks the retained unit RemovedWorktree. A mesh-pinned run still calls `workers.end_unit`. `wrap` and promote stop each unit's workers the same way before their staff step.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_shutdown_rerun_after_first_worker_stop` (the rerun stops the rest and the record lists each pane once); `test_restart_rerun_after_director_tab` and `test_restart_rerun_after_first_worker` (one set of panes); `test_restore_rerun_after_agent_resume` (stopped after a busy worker's agent resumes and before its launcher starts: the rerun starts one launcher, which types nothing when the line is in the transcript); `test_stop_unit_workers_rerun_after_pane_stop` (stopped after a pane stops and before recording stop completion: the rerun proves it stopped, keeps its row, and stops the rest); a rerun of a finished restart reports done and changes nothing.
- Full gate green.

**Acceptance gate (retained workers):** retire, wrap and promote stop without deleting workers; restore resumes the same ids and parents. A crashed director and an outside-worktree worker remain stoppable from row evidence. Permanent unit removal and worker end delete only after stopped proof. Interrupt each stop/restore boundary; retries preserve every resumable row and finish once.

**Pane close contract:** use Phase 6's confirmed pane close and `pane_launch.retire_launch`.
Preserve the director and sibling panes; test one retained launch after replacement and recovery after failed closure.

**Process anomaly rule:** unknown process evidence prints a named note and does not refuse the operation.
Resolve the name from the herdr tab, then the session registry, then use the pid.
A failed name read does not refuse.
Keep unknown process evidence separate from `UncertainWorker`, which represents a structural fault.
Refuse for a target proved live when the requested step requires it stopped, or for a structural fault.
Structural faults include a missing terminal, duplicate panes, and an ownership conflict.
Unknown evidence supplies no permission to signal an unowned process or close a sibling pane.
Test continuation after unknown evidence, named stderr notes, and refusal for a proved live target or structural fault.

**Resume-message contract:** replace independent optional `moved_from` and `moved_to` arguments with `NoAccountMove | AccountMove(source, destination)`.
Update every caller through the shared builder and preserve existing complete messages.
The stop writer owns `scripts/lib/resume_message.py` and its tests; the restore writer consumes its API.

### Phase 11 — Account moves for pane workers (accounts unit)  · status: todo

**Depends on:** Phase 3n, Phase 7, Phase 8, Phase 9, Phase 10. Shared final-batch gate with Phase 14; consume the existing atomic bytes writer.

#### Work Order

**Goal:** an account move relaunches each worker on the new account in its own pane, without a lost or forked conversation.

**Spec:** Each worker move has a move id and one entry in the moves log, keyed by staff id and move id, that names its last finished step and the worker's `terminal`. Under the worker's dispatch lock:
1. Wait for or interrupt the turn; end the agent through the per-member stop (Phase 3n item 5) and keep the pane open at its shell; prove with a fresh `observe` (Phase 7) that the worker is a `StoppedWorker` on the same `terminal` and its process is gone. An `UncertainWorker` stops the move before any copy, with its reason, and changes nothing.
2. Read each conversation file from the source home, with its size and hash.
3. Publish each file in the target home with `atomic_file.write_bytes_atomic(path, data)`, then check the published file's size and hash against the source. The move makes no staging folder and calls no `os.replace` of its own. A file is never seen half copied.
4. `start-or-resume` with the row's recorded role on the new account, which starts the agent in the same pane through `ExistingPane` (Phase 7), check that the id resumes on the same `terminal`, and record success.
No step closes and recreates the pane (Delegation Context → Lifecycle reruns keep the pane). On failure, before it resumes the worker on the source account, the move proves with a fresh observation that no process on the target owns the conversation; then it resumes once on the source in the same pane and reports. A rerun goes on from the last finished step: a published file whose size and hash match the source is not written again, and a target that already runs the conversation is kept, so no source starts beside it. A mesh-pinned run keeps `_retire_workers_servers` (`accounts.py:3358`) and `leftover_server_lines` (`codex_mesh.py:8377`) until cleanup (Reference → Cleanup list: accounts' server lines); the move reads each run's pin through `unit_runs.worker_transport(run)`.

**The bytes writer.** Reuse the existing `atomic_file.write_bytes_atomic` and its shared publication step; add no writer implementation.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py` — the worker move; `_retire_workers_servers` (:3358).
- `scripts/accounts/next_moves.py`, `scripts/accounts/test_next_moves.py`, `scripts/whoami/quota_alert.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py` — the move callers.
- `scripts/lib/atomic_file.py` — read only: the existing `write_bytes_atomic` with its publication failure contract.
- `scripts/herdr/worker_panes.py`, `scripts/shutdown/stop_resume.py`, `scripts/lib/resume_message.py` — read only: pane operations, per-member stop, and resume text.

**Workers:** `1 writer + 1 tester` — split by source and test lane.
- `impl` — `scripts/accounts/accounts.py`, `scripts/accounts/next_moves.py`, `scripts/whoami/quota_alert.py`; hub: `scripts/accounts/accounts.py` (the account move).
- `test` — `scripts/accounts/test_accounts.py`, `scripts/accounts/test_next_moves.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py`, written from the Spec.

**State:** account homes; moves log.

**Constraints from prior phases:** Phases 7-9; Phase 3n the per-member stop and resume, and the recorded role on the row; Phase 7 `start_or_resume(parent, name, role) -> WorkerStart` and `observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]`, with `ExistingPane` reuse of a `StoppedWorker` pane; Phase 10 `scripts/lib/resume_message.py`'s `resume_message`, the one resume-text builder, with its `PaneWorkerResume`, and its fresh observation that a worker's process is gone and its pane is at its shell. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`, `write_bytes_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer). `accounts.py` `_atomic_write` (:2481, its `os.replace` at :2497) is an own writer; the worker move does not call it.
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): `atomic_file.write_bytes_atomic` and the shared publish step exist, with distinct pre-publication and post-replacement failures; do not build them again. The hub is the accounts move; test move recovery against those failures.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Move tests: busy, idle, and the failure fallback, which starts no source while a target process owns the conversation; an `UncertainWorker` stops the move before any copy.
- Account-move tests cover both existing atomic failure outcomes: before publication and after replacement.
  Keep the existing bytes-writer mode and publication tests unchanged.
- Real proofs (Invariants → Lifecycle reruns keep the pane), in a probe tab of `agents` with a real worker pane: a real move keeps the same tab, pane id and `terminal_id`; a rerun of the finished move changes nothing and exits 0; one real move stopped after its publish step finishes on rerun in the same pane.
- Rerun tests (Invariants → Rerun-safe tools), each ending on the new account with the same pane and terminal and one conversation, as one clean move: `test_move_rerun_mid_copy` (stopped inside a `write_bytes_atomic` call: no partial file is published; the rerun writes the file once); `test_move_rerun_after_publish` (stopped before the target starts); `test_move_rerun_after_target_resume` (stopped before the success record: the rerun keeps the target and starts no source); a rerun of a finished move reports done and changes nothing.
- Full gate green.

**Pane close contract:** use Phase 6's confirmed pane close and `pane_launch.retire_launch`.
Preserve the director and sibling panes; test one retained launch after replacement and recovery after failed closure.

**Process anomaly rule:** unknown process evidence prints a named note and does not refuse the operation.
Resolve the name from the herdr tab, then the session registry, then use the pid.
A failed name read does not refuse.
Keep unknown process evidence separate from `UncertainWorker`, which represents a structural fault.
Refuse for a target proved live when the requested step requires it stopped, or for a structural fault.
Structural faults include a missing terminal, duplicate panes, and an ownership conflict.
Unknown evidence supplies no permission to signal an unowned process or close a sibling pane.
Test continuation after unknown evidence, named stderr notes, and refusal for a proved live target or structural fault.

**Resume-message contract:** consume Phase 10's `NoAccountMove | AccountMove(source, destination)` read-only.
Keep complete move messages unchanged; test account-move recovery before publication and after replacement.

### Phase 12 — Astra and ask-a-friend in panes  · status: todo

**Depends on:** Phase 1, Phase 3n, Phase 6, Phase 7, Phase 8, Phase 9. Shared gate with Phases 10/12/13 over the exact final batch.

#### Work Order

**Goal:** Astra roles and ask-a-friend run in panes: an Astra role held by a unit director or a showrunner is a worker under that holder, and an Astra role or friend for a caller outside the staff hierarchy is a pane beside the caller that is not staff.

**Spec:**
1. **Astra roles.** A held Astra role that a unit director or a showrunner asks for is a worker row under that holder, with the recorded role `astra-designer` or `astra-reviewer` (Phase 3n admits it; 3l seeds and shows a showrunner-held role as a worker under its showrunner). It starts with `start_or_resume(<holder id>, name, role)`, which reads the `[design_roles.<kind>]` key `designer` or `reviewer` and its sandbox (Phase 7). A role that already has a row keeps that row, its id, its name and its backend identity: no second row is made. `design_roles.py` keeps its fences and loses its mesh calls. Only a caller outside the staff hierarchy (an ad hoc session whose native runtime id has no staff row) gets its Astra role as a pane beside it that is not staff, as ask-a-friend does.
2. **Ask-a-friend is not staff** (Phase 1 As-built). It opens a pane beside the caller through `scripts/herdr/side_pane.py` (new; `open <label>` and `close <label>`), which calls `launch_prepared_agent` with a `SplitPane` and the `consultation` key of `[ask_a_friend.<caller family>]` (`[assignments] ask_a_friend=caller`: the caller's family from its staff row, or from its runtime id for a caller outside the staff hierarchy), writes no staff row, and closes its pane at `end`. It finds its pane by its label in the caller's tab with one `Herdr.panes()` read, by the same evidence rules as Phase 7 (a non-empty `terminal_id`, one pane with the label, a fresh `pane_activity`). A rerun of an open stopped after the split reuses the labelled pane at its shell through `ExistingPane(pane, terminal)`: no pane closes and no second split opens. Structural faults refuse with the reason; unknown process evidence prints the named anomaly and continues. `close` ends the agent, then closes the pane only after a second read finds the same terminal and a fresh `pane_activity` reads `PaneAtShell`. A rerun of `end` finds no pane and reports done.
3. **The friend conversation.** `side_pane.py` takes over every step that `launch_friend.sh` and `end_friend.sh` do today, before Phase 18 deletes them: `open <label> --question-file <path>` starts the friend with its opening question; `send <label> --message-file <path>` types a follow-up into the same pane; the friend replies to the caller with `send.py --to <caller>` for both families; `close <label>` ends the friend. The friend keeps the caller's family. The two scripts become thin callers of `side_pane.py` in this phase, so `commands/ask_a_friend.md` keeps working until Phase 14 changes its text.
4. The command text that starts them (`commands/ask_a_friend.md`, `commands/astra_review.md`) changes in Phase 14.

**Files:**
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py` — Astra roles.
- `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py` — new: the one pane beside a caller outside the staff hierarchy, for ask-a-friend and for such a caller's Astra role.
- `scripts/ask_a_friend/launch_friend.sh`, `scripts/ask_a_friend/end_friend.sh` — call `side_pane.py open` and `close` (deleted in Phase 18).
- `scripts/production/staff.py`, `scripts/herdr/worker_panes.py`, `scripts/herdr/herdr.py` — read only.

**Workers:** `2 writers` — split by module.
- `impl` — `scripts/herdr/side_pane.py`, `scripts/herdr/test_side_pane.py`, `scripts/ask_a_friend/launch_friend.sh`, `scripts/ask_a_friend/end_friend.sh`; hub: `scripts/herdr/side_pane.py` (`design_roles.py` calls it for a caller outside the staff hierarchy).
- `test` (opens as impl) — `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py`.

**State:** panes; a unit director's or showrunner's Astra role is a worker row through `start_or_resume`; a side pane writes no row.

**Constraints from prior phases:** Phases 7-9; Phase 3l (a showrunner-held Astra role is seeded and shown as a worker under its showrunner); Phase 3n `WorkerRole` (`astra-designer`, `astra-reviewer`) recorded on the row, `ensure_worker(name, parent, role)`, the backend-identity record; Phase 6 `SplitPane`, `ExistingPane`; Phase 7 `start_or_resume(parent, name, role) -> WorkerStart` with a unit or showrunner parent, and `observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]`, with `record` from one `staff.snapshot()` (Phase 2).
- Phase 4r (review, 2026-11-04): a Claude launch writes no pane launch record; only `Herdr.start_agent` still writes the Codex compatibility record. Assert `current_session`, the process generation, the terminal and a confirmed close, not a launch publication. Keep a compatibility-record test only where a caller still reads that record.
- Phase 4r (review, 2026-11-04): a missing terminal, an unreadable pane list or a duplicate pane is not an operation refusal. Each destructive action goes through `herdr.check_target`; an unproved target is left alone with one anomaly line, and unrelated known work continues. Test that case.
- Phase 4r (review, 2026-11-04): pane input and close take the current-session target: `Herdr.prompt(pane, text, *, target)`, `Herdr.send_keys(pane, *keys, target)`, `Herdr.close_pane(pane, *, target) -> bool`. Carry the target through each operation. Test a changed `current_session`, a reused pane and sibling protection.
- Phase 3h review (2026-11-01): held Astra roles go through Phase 8's `worker.py`, not `start_or_resume` directly; keep row identity and recorded role; the non-staff side-pane path stays separate.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Tests for each changed module; `designer1` and `reviewer1` under a unit read their `[design_roles.<kind>]` keys and sandboxes from their recorded roles; a showrunner's Astra role starts under the showrunner, keeps its one row, id and backend identity, and makes no second row; an Astra role or ask-a-friend asked for by an ad hoc session outside the staff hierarchy opens a side pane and makes no staff row; ask-a-friend from a showrunner opens a side pane and makes no staff row; a friend's opening question, a follow-up and its reply reach the caller, for a Claude caller and a Codex caller; `close` ends the friend and its pane.
- Real proofs (Invariants → Lifecycle reruns keep the pane), in a probe tab of `agents`: a rerun of a finished open keeps the same tab, pane id and `terminal_id` and exits 0; one open stopped after its split finishes on rerun in that same pane.
- Rerun tests (Invariants → Rerun-safe tools), each compared with one clean run: `test_astra_open_rerun_after_split` (the labelled pane is reused; one pane, same terminal); `test_friend_open_rerun_after_split` (the labelled pane is reused; one pane, same terminal); `test_friend_end_rerun_after_close` (no pane, reports done); each open and close run twice gives one pane and then none.
- Full gate green.

**Pane close contract:** use Phase 6's confirmed pane close and `pane_launch.retire_launch`.
Preserve the director and sibling panes; test one retained launch after replacement and recovery after failed closure.

**Process anomaly rule:** unknown process evidence prints a named note and does not refuse the operation.
Resolve the name from the herdr tab, then the session registry, then use the pid.
A failed name read does not refuse.
Keep unknown process evidence separate from `UncertainWorker`, which represents a structural fault.
Refuse for a target proved live when the requested step requires it stopped, or for a structural fault.
Structural faults include a missing terminal, duplicate panes, and an ownership conflict.
Unknown evidence supplies no permission to signal an unowned process or close a sibling pane.
Test continuation after unknown evidence, named stderr notes, and refusal for a proved live target or structural fault.

### Phase 13 — Displays read workers through observe  · status: todo

**Depends on:** Phase 7, Phase 8, Phase 9. Shared gate with Phases 10/12/13 over the exact final batch.

#### Work Order

**Goal:** every display reads workers through `worker_panes.observe`, the one join of worker rows and herdr panes.

**Spec:**
1. **Displays.** `roster.py`, `staff_sources.live` (`staff.py show --json --live`, the handler's command; it replaced `production_tree.py`, which Phase 3n deletes), `codex_winddown.py`, `waste_watch.py`, `hierarchies/worker_owners.py`, `hierarchies/measure.py` and `board_json.py` read pane workers from `worker_panes.observe(parent, record)` (Phase 7), the one join; none of them joins staff rows with `herdr agent list` itself. An `OpenWorker` shows its pane and status; a `StoppedWorker` shows as stopped; a `ClosedWorker` shows as closed; an `UncertainWorker` shows as uncertain with its reason, never as closed.
2. **One snapshot per run.** Each display takes one `staff.snapshot()` per run and passes it to every `member` and `observe` call.
3. **Problems are shown, never an empty list.** When `snapshot()` raises `StaffUnreadable` (natedev not reached, or a malformed record), the display shows the reason as a problem line and shows no empty worker list in its place: `roster.py` adds it to `Roster.problems` (through `_problem`), `staff_sources.live` puts it in its output as a problem entry with the cause (the handler shows it as incomplete, Phase 3n item 6) and on stderr, and each other display prints it on stderr. A member that gives `NoSuchMember` gets its own problem line, which names the member.

**Files:**
- `scripts/message/roster.py`, `scripts/production/staff_sources.py`, `scripts/production/codex_winddown.py`, `scripts/usage/waste_watch.py`, `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/measure.py`, `scripts/direct/board_json.py` — the displays; in `staff_sources.py`, `live`.
- `scripts/message/test_roster.py`, `scripts/production/test_staff_sources.py`, `scripts/production/test_codex_winddown.py`, `scripts/usage/test_waste_watch.py`, `scripts/hierarchies/test_worker_owners.py`, `scripts/hierarchies/test_measure.py`, `scripts/direct/test_board_json.py` — their tests.
- `scripts/herdr/worker_panes.py`, `scripts/production/staff.py`, `scripts/production/session_staff.py`, `scripts/production/session_labels.py` — read only: `observe` (Phase 7), `snapshot` (Phase 2), the one membership join and display labels. Phase 3s extends the existing resolver; no module rename is needed.

**Workers:** `2 writers` — split by module group.
- `impl` — `roster.py`, `staff_sources.py`, `codex_winddown.py`, `board_json.py` and their tests; hub: `scripts/production/staff_sources.py` (`live`, the handler's command).
- `test` (opens as impl) — `waste_watch.py`, `hierarchies/worker_owners.py`, `hierarchies/measure.py` and their tests.

**State:** none.

**Constraints from prior phases:** Phases 7-9; Phase 7 `worker_panes.observe(parent, record) -> list[OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker]` (`OpenWorker(row, pane, terminal, status: herdr.AgentStatus)`, `StoppedWorker(row, pane, terminal)`, `ClosedWorker(row)`, `UncertainWorker(row, reason)`); Phase 2 `staff.snapshot() -> StaffSnapshot` is one record read and raises `StaffUnreadable`; the member join is `session_staff.MemberSources.member` (`staff_sources.member` is its entry point), giving `MemberFound | NoSuchMember`, with one `member_sources(dependencies=...)` per command (Delegation Context → One identity join); Phase 3n `live()` keeps schema 3 wire fields and meanings and shows partial results as incomplete. `staff_test_support.showrunner_relationship` is the one fixture for a showrunner edge, and `showrunner_tree`'s snapshot-taking functions are the one way to read the tree: a run takes one snapshot.
- Phase 3h review (2026-11-01): `observe` plus native-session evidence shows a live session with no published row (a held or failed clear hook) as incomplete in roster and handler output; the status line and accounts show their unavailable state; never invent an owner or report an empty set as complete.
- Phase 3s review (2026-11-02): test every state of Phase 7's `WorkerPane` observation union, `ProcessUnknownWorker` included. `UnpublishedWorker` is a `WorkerStart` result only: a failed publication makes no staff row, so no display observes it. Startup callers (Phases 8, 10, 11, 12) test every `WorkerStart` state.

**Acceptance gate:**
- basedpyright 0/0 on changed files and every caller module.
- Tests for each changed module; no display module calls `herdr agent list` outside `worker_panes.observe`.
- `test_staff_sources.py`: `staff.py show --json --live` shows each of the four `observe` states for a pane worker, with an `UncertainWorker` shown as incomplete with its reason; its output stays schema 3. Inactive membership stays hidden; cleanup readers retain live, unknown and duplicate evidence.
- Problem tests for each display, `staff_sources.live` included: an unreachable owner (the fake `ssh` fails), a malformed record and a missing member each give a problem line that names the cause, and no empty worker list; a run makes one `snapshot()` call (on the Mac, one call in the fake `ssh` log).
- No rerun test: displays only read (Invariants → Rerun-safe tools).
- Full gate green.

### Phase 14 — Instructions describe panes  · status: todo

**Depends on:** Phase 10, Phase 11, Phase 12, Phase 13 behavior. Draft beside Phase 11 from its fixed contract; check final text and code together in the shared 11/14 gate.

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

**Constraints from prior phases:** Phase 8 landed the transport-aware text in `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerRuns/>`, `<WorkerLaunchFailure/>`, `<FixDispatch/>`, `<PhaseCleanup/>`), branched once on the run's pin. Phase 9 `send.py worker:<unit>/<short name>` and `detached_runs.py`; Phase 10 shutdown and restore of split tabs; Phase 11 account moves of pane workers; Phase 12 Astra roles in panes and `side_pane.py open|close` for ask-a-friend; Phase 13 displays read workers through `observe`. Phase 4f accepted limit (F008): an interrupted multi-pane rebuild can lose the split ratio and leave panes in the source workspace; the anomaly line names the tab and the panes. The instructions tell the user to inspect those panes; a retry does not rebuild the split.
- Phase 3h review (2026-11-01): keep the 4b header, Hub files and Gates deletions; do not generate them again.

**Acceptance gate:** No instruction file names a mesh verb outside a mesh-pinned branch; a final search for mesh paths outside history finds none; full gate green. No basedpyright: no Python changes. No rerun test: text edits, not a tool.

### Phase 15 — Pilot and default switch  · status: todo

**Depends on:** Phase 3, Phase 3n, Phase 4, Phase 6, Phase 7, Phase 8, Phase 9, Phase 10, Phase 11, Phase 12, Phase 13, Phase 14

#### Work Order

**Goal:** herdr-unit's own next run works on panes, then the default becomes `herdr` and units switch at their run boundaries, Mac units after a Mac probe of Phases 6-7.

**Spec:** Pilot one run; record any defect as a repair before the switch. Then set `WORKER_TRANSPORT=herdr` in `config/direct.conf`. Needs questions list items 1(c) and 1(e) answered.

**Files:**
- `config/direct.conf` — `WORKER_TRANSPORT=herdr`.

**Workers:** `1 writer` — one line; the pilot is the director's.

**State:** `config/direct.conf`.

**Constraints from prior phases:** Phases 3, 4 and 6-14.
- Phase 3h review (2026-11-01): the pilot and default switch gate only the pane track, not the rename track.

**Acceptance gate:** the pilot run's phases finish on panes; Mac probe passes; full gate green.

### Phase 18 — Cleanup  · status: todo

**Depends on:** Phase 3n, Phase 15; no mesh pins or live mesh processes; no legacy consumers; ledger history proof. Cut the whole-4g barrier. Unrelated staff stores and deletes can follow panes.

#### Work Order

**Goal:** the mesh, the old worker records and `WORKER_TRANSPORT` are gone, no later than 7 days after the default switch.

**Spec:** First, `unit_runs.worker_transport(run)` refuses to pin `mesh` for a new run (one line, exit 2), so no new mesh pin can appear. Then run only after a check that no run folder pins `mesh` and no mesh process is alive. The unit ledger goes only after the backend-identity record (Phase 3n item 2) is the reader of every consumer, `staff_sources.live` included: a search finds no reader of a ledger thread id. Retire the ledger directly; do not move its paths to staff-id keys first. Inventory each ledger and roster, all consumers and history readers before deletion. Prove no live consumer reads ledger thread ids or paths, including `staff_sources.live`, messages, shutdown, accounts and displays. Keep required history readable in its durable history source and prove it before deleting ledger state. Missing history or consumer evidence blocks deletion. The models unit reviews `workers.py` and `codex_mesh.py`. State retirement must finish after a stop and report unchanged on a finished rerun. Delete: Reference → Cleanup list. Delete the one-time worker fill and native-id migration with their obsolete command tests after recording their completeness results. Search their shared collector imports and every command caller before deletion. The launchers (`implement.sh`, `review.sh`, `assign.py`) lose their mesh branch and their `WORKER_TRANSPORT` switch. In `scripts/lib/unit_runs.py`, `worker_transport`, the run pin and the run `workers` log reader go. Phase 19 moves their shared work to one launcher, switches callers and deletes the old names.

**Files:**
- `scripts/production/worker_fill.py`, `scripts/production/test_worker_fill.py`, `scripts/production/migrate_staff_ids.py`, `scripts/production/test_migrate_staff_ids.py` — delete one-time tools after completeness and history proofs; remove their command callers and collector imports.
- `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py` — deleted.
- `scripts/direct/workers.py`, `scripts/direct/worker_state.py`, `scripts/direct/remove_workers.py`, `scripts/production/retire_unit.py` — the mesh parts and the unit worker ledger go; in `retire_unit.py` `stop_rest` (:632-636) the mesh-pinned `workers.end_unit` branch goes.
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/assign.py` — the mesh branch and the `WORKER_TRANSPORT` switch go.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — `worker_transport`, the run pin and the run `workers` log reader (`_claude_worker` :134-150) go.
- `scripts/ask_a_friend/launch_friend.sh`, `scripts/ask_a_friend/end_friend.sh` — deleted.
- `config/models.conf`, `config/direct.conf` — `[direct.options] codex_mesh`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, `WORKER_TRANSPORT` go. models-unit owns `config/models.conf`: send it the `[direct.options]` change.
- `scripts/shutdown/inventory.py`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/session_types.py` — `CodexServer`, `codex_seats`, `PausedSeat`, `_CODEX_HEADLESS`, mesh pause.
- `scripts/accounts/accounts.py` — server lines.
- `scripts/production/staff_sources.py`, `scripts/production/test_staff_sources.py` — the mesh-source readers go; `live()` reads only the staff record and the backend-identity record.
- `scripts/message/process_table.py`, `scripts/usage/waste_watch.py`, `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/measure.py`, `scripts/shot_report/transcripts.py`, `scripts/lint/tmp_sweep.py` — display mesh code.
- `docs/as-built/long-lived-workers.md`, `docs/as-built/herdr-adoption.md`, `docs/as-built/session-roster.md`, `docs/as-built/agent-registry.md`, `docs/as-built/shutdown-and-restart.md`, `docs/as-built/unit-board.md`, `docs/as-built/usage-ledger.md` — Reference → Cleanup list docs.

**Workers:** `2 writers` — `impl`: code; `test` (opens as impl): docs and tests.

**State:** deletes state on disk (listed).

**Constraints from prior phases:** Phase 15 set the default to `herdr`; cleanup also refuses new mesh pins before it drains old ones. Phases 3n and 6-14 supply the pane APIs and consumer changes. Phase 3p deleted the review pool. Broad staff conversions and 4g are not prerequisites. The explicit drain, consumer and history proofs above replace that blanket order. Phase 5 is not needed: cleanup edits whichever registry file name is current. Phase 10 kept `workers.end_unit` in `retire_unit.py` `stop_rest` for a mesh-pinned run only; that branch goes here, and `stop_resume.stop_unit_workers` is then the one path that stops a unit's workers.
- Phase 3h review (2026-11-01): `migrate_staff_ids.py` and its test go only after the live move, completeness and history proof.

**Acceptance gate:** `grep` finds no live `codex_mesh` or `WORKER_TRANSPORT` reference; nothing in Reference → Cleanup list is left; basedpyright 0/0 on changed files and every caller module; full gate green. Code removal is one commit. Direct ledger state retirement has a real stopped-run recovery proof and a finished rerun that changes nothing and exits 0. Consumer and history proofs stay required. No ledger migration is added merely to delete it.

**Acceptance gate (one-time tools):** no live import or command names `worker_fill.py` or `migrate_staff_ids.py`; delete their command tests only with the tools, keep shared collector tests for consumers that remain. Native-id completeness and history proofs are recorded before deletion. No new ledger migration is added.

### Phase 19 — One launcher  · status: todo

**Depends on:** Phase 18

#### Work Order

**Goal:** `assign.sh`, the one door (decision 11), holds the work of `implement.sh` and `review.sh`, and every repository caller uses it; the inner launcher names are deleted. No new launcher name is added. Agree each `scripts/direct` launcher edit with devtools-unit first.

**Spec:** Merge `implement.sh` and `review.sh` into `assign.sh`/`assign.py`, the one door, switching every caller and then deleting the old files. A compatibility entry point stays only for a named caller outside this repository; record that caller and its owner before making an exception. Generated Codex mirrors switch with their source commands and are not an independent compatibility requirement. After Phase 18 each launcher holds only its arguments, its recorder, status-file and board work, and its call to `worker.py`; `launch.py` holds that work once.

**Files:**
- `scripts/direct/implement.sh`, `scripts/direct/review.sh` — deleted after their callers switch.
- `scripts/direct/assign.sh`, `scripts/direct/assign.py` — the one door; they take the merged work.
- `commands/unit/direct.md`, `commands/plan/phase_review.md`, `commands/showrunner/produce.md`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/inventory.py`, `scripts/agents/detached_runs.py`, `scripts/direct/progress_history.py`, `scripts/lib/unit_runs.py` — switch old launcher callers and text. Regenerate the command mirrors through `scripts/claude_to_codex/sync.py`.
- `scripts/shutdown/test_stop_resume.py`, `scripts/shutdown/test_inventory.py`, `scripts/direct/test_progress_history.py`, `scripts/lib/test_unit_runs.py`, `scripts/agents/test_detached_runs.py` — caller regressions.
- `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py`, `scripts/direct/test_assign_launcher.py` — the launcher tests.

**Workers:** `2 writers` — split by file group.
- `impl` — `scripts/direct/launch.py` and `scripts/direct/progress_history.py`, plus their tests; hub: `scripts/direct/launch.py` (the sole launcher).
- `test` (opens as impl) — delete the old launcher files; switch commands, shutdown, detached-run and unit-run callers, regenerate mirrors, and update their tests. It does not edit the API writer's files.

**State:** none new.

**Constraints from prior phases:** Phase 18 removed the mesh branch and the `WORKER_TRANSPORT` switch from the launchers, and `unit_runs.worker_transport`; Phase 8 put start, dispatch, wait and receipt in `worker.py` (decision 11); Phase 9 `assign.py` types review Stage 2 through `dispatch` with the dispatch id `<dispatch id>-2`. Every file write this phase adds or changes calls `scripts/lib/atomic_file.py` (`write_text_atomic`; `sync_folder` for the folder step alone) and makes no temp-file, `fsync` and replace steps of its own (Delegation Context → One file writer).
- Phase 3h review (2026-11-01): keep 3h's admission and publication behavior and the launcher's recorder, board and review behavior.

**Acceptance gate:** basedpyright 0/0 on `launch.py` and every remaining caller module; `bash -n` and `shellcheck` with no new findings on the changed `.sh` files; the launcher tests green; full gate green. No rerun test: the merge is one commit, not a tool that changes shared state in steps (Invariants → Rerun-safe tools).

**Acceptance gate (one command):** a search of code, commands and regenerated mirrors finds no live `implement.sh`, `review.sh`, `assign.sh` or `assign.py` call; historical text is named separately. No wrapper stays without a named external caller. The one launcher still records each pass once and uses `worker.py`; affected caller and command-mirror suites pass.

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
| 17 | `mesh_runs.py` | `detached_runs.py` (Phase 9) |
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

### Cleanup list (Phase 18)

`codex_mesh.py` and its tests; the mesh parts of `workers.py` and the unit worker ledger; `scripts/direct/remove_workers.py`; the run `workers` log (`<run>/workers`, read at `scripts/lib/unit_runs.py:136-145`, `remove_workers.py:65`, `worker_state.py:50`); `mesh_roster.json`, `*.pending.json`, `mesh_*` run files; follow-claim code; `launch_friend.sh`, `end_friend.sh`; `[direct.options] codex_mesh`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, `WORKER_TRANSPORT`; shutdown's `CodexServer`, `codex_seats`, `PausedSeat`, `_CODEX_HEADLESS`, mesh pause and paused-workers note; accounts' server lines; display mesh code (the mesh-source readers of `staff_sources.py`; `production_tree.py` is already gone in Phase 3n; `process_table.py`, `waste_watch.py`, `hierarchies/*`, `shot_report/transcripts.py:1680`, `lint/tmp_sweep.py`); the Codex app-server homes; docs: rewrite `long-lived-workers.md`, update `herdr-adoption.md`, `session-roster.md`, `agent-registry.md`, `shutdown-and-restart.md`, `unit-board.md`, `usage-ledger.md`.

### Probe results (Phase 6 writes them)

Measured 2026-11-02 on natedev, herdr 0.9.5 (protocol 22), in probe workspaces of `agents` and in a private herdr server. The probes did not restart the shared `agents` server and did not replace a live credential. Each probe workspace was closed after use.

| Probe | Result | Who uses it |
| --- | --- | --- |
| Which side `--ratio` measures | The original pane: the left side of a `right` split, the top of a `down` split. `pane split --direction right --ratio 0.3` gave the original pane 27 of 91 columns. | Phase 7 split sizes |
| `split_ratio` on a nested split | It changes the nearest split that holds the pane. The root split kept its ratio 0.3. | Phase 7 |
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

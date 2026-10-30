# Workers in herdr panes

> **Status: DESIGN PLAN — for showrunner review, then the user.** Retire `codex_mesh` and run every unit worker as an agent in a herdr pane in its director's tab. Rename `agents.conf` to `models.conf` and `/agent` to `/default_models`. Nothing is built yet.

Written by herdr-unit on 2026-10-29 for the sdlc showrunner. Source: the user's request relayed by the sdlc showrunner the same night. The inventory was read from `~/.claude` main at 7e85a74b. The natedev-herdr worktree is 16 commits behind main. Its launchers pin model and effort from the roster, where main resolves them per turn. Work starts from main's version.

## The user's request

- Retire `codex_mesh` and use herdr itself for workers.
- Workers open as panes inside each unit director's tab, stacked on the right. The director gets 2/3 of the width and the worker column 1/3. Active workers split the column evenly.
- Unit directors still send messages to other sessions through the messaging framework. herdr lets a director type into its workers directly, and lets the user see every worker.
- To change a model or effort, someone runs `/model` in the session.
- Starting a session may become a herdr command, so that a lot of infrastructure can retire.
- Update the showrunner and unit director instructions (their skills), and clean up the old things.
- Rename `agents.conf` to `models.conf` everywhere, and `/agent` to a name with no clash, for example `/default_models`.

## Decisions made in this plan

Each item below is my call under the decision criteria. A one-line plan change reverses it.

1. **The three launcher commands stay; only what they call changes.** `implement.sh`, `review.sh` and `assign.sh` keep their arguments. They keep their recorder, ledger, status-file and board work, and call herdr instead of the mesh. The `/unit:direct` contracts that name them then change little. Risk: the launchers stay three files. When the cleanup shows they are thin, the cleanup phase merges them into one.
2. **A worker's own files signal completion; herdr's state only wakes the director.** A worker is done when it writes its status and summary files, as today. A herdr `idle`/`done` state wakes the director, which then reads those files. Reason: herdr can read a Codex pane as `idle` or `unknown` in the middle of a turn. Its state alone must never close a pass.
3. **The director waits on a background `herdr agent wait`, with no poll loop.** The launcher starts the turn with `herdr agent prompt`, then blocks in `herdr agent wait <name> --until idle --until done`. The director runs the launcher in the background, as today, and the task notification is the wake-up.
4. **A prompt goes in by file path, not as pasted text.** The launcher types one line: `Read <prompt path> and do the work it gives.` Long pasted prompts are where a terminal UI fails.
5. **Idle worker panes close at the end of each phase; the conversation is kept.** The user said the column splits "for whichever are active". A worker keeps its conversation across phases and runs, as today (workers persist with their director). Its pane closes when its phase work ends. The next dispatch opens a new pane and resumes that conversation (`codex resume <id>` or `claude --resume <id>`). Cost: a few seconds of start time per reopen.
6. **No launcher compaction, and no "Continue where you stopped." retry.** Interactive Codex and Claude compact by themselves when their context fills. The launcher compaction was already best-effort ("costs tokens, not correctness").
7. **Codex stays a worker kind.** herdr starts `--kind codex` and `--kind claude` agents. `models.conf` keeps its Codex sections; the default worker kind per role stays where it is set today.

## 1. What codex_mesh and its launchers do today, and who calls each part

`scripts/agents/codex_mesh.py` (8652 lines) is called by 29 scripts, 4 commands, 27 docs and 1 config file, and by 36 test files besides. It is imported as a module by `assign.py`, `workers.py`, `mesh_runs.py`, `astra/design_roles.py`, `accounts.py` and `hierarchies`.

| # | Job | Where | Callers |
| --- | --- | --- | --- |
| 1 | App-server per run: start, watcher, idle stop, sweep (`serve`, `idle-stop`, `sweep`; `mesh_server.json`, `mesh_retired.json`, `mesh_server.log`, watcher locks) | codex_mesh.py:2637, 2742, 2461, 7486, 7533 | Every verb starts the server itself; `retire_unit.py:338` |
| 2 | Open a worker thread (`start`; `mesh_roster.json`) | codex_mesh.py:3308 | implement.sh:543, review.sh:687, launch_friend.sh:81 |
| 3 | Another turn on an idle worker (`follow`; drains `<name>.pending.json`) | codex_mesh.py:3408 | implement.sh:524, review.sh:680, assign.py:524, 581 |
| 4 | Follow claims (`can-follow`, `release-follow`) | codex_mesh.py:3762, 3784 | implement.sh, review.sh, assign.py |
| 5 | Compaction before a follow-up above `UNIT_COMPACT_ABOVE_TOKENS` (`compact`) | codex_mesh.py:3643; implement.sh:336 | implement.sh, review.sh, assign.py |
| 6 | "Continue where you stopped." after a context overflow | codex_mesh.py:4526 | inside the mesh |
| 7 | Retry on a new app-server after a fast failure or a stale sign-in | codex_mesh.py:3174, 2446 | inside `start` and `ensure_server` |
| 8 | Codex sign-in change: retire old servers and tell the holder (`signin-changed`) | codex_mesh.py:7673 | a NixOS path unit and a launchd agent |
| 9 | Messages to a worker; held when no launcher is attached (`send`, `<name>.pending.json`) | codex_mesh.py:5525, 2184 | send.py:1266 (`worker:` addresses), assign.py:272 (review Stage 2) |
| 10 | Steer or interrupt a running turn (`steer`, `interrupt`) | codex_mesh.py:5564, 5612 | the director by hand |
| 11 | End a worker (`end`) | codex_mesh.py:5679 | workers.py:1683, hierarchies/worker_owners.py:635, end_friend.sh |
| 12 | Pause or stop every worker of a run (`stop --pause`) | codex_mesh.py:6228 | end_session.sh:92, shutdown/stop.py:377, 1433, retire_unit.py:638, rename_unit.py:724 |
| 13 | Reopen an ended worker, or carry one into a new run (`reopen`, `adopt`) | codex_mesh.py:5865, 6130 | workers.py:1042, 875; assign.py:397 |
| 14 | Unit worker ledger: lasting identity per production/unit/role (HeldBy, Paused, Adopted, Retired) | workers.py:422-545; `~/.local/state/unit-workers/` | codex_mesh, assign.py:604, review.sh:233, retire/rename/transfer_unit, rename.py, remove_workers.py |
| 15 | Run worker log and slot claims (`<run>/workers`, `impl_worker_*.json`, `impl_claim_*.json`) | worker_state.py:48; implement.sh:219-400 | implement.sh, roster.py |
| 16 | Pass and ledger recording, status and summary files, `UNIT_RESOLVES_ROUND` | implement.sh:419-676; review.sh; assign.py | `/unit:direct` |
| 17 | Detached long runs and result delivery into the worker's turn (`mesh_runs.py start/supervise/deliver`; "no launcher is attached" message to the director) | mesh_runs.py:300, 437, 523 | verify.sh:868, review.sh:669, shutdown/stop.py:1138, retire_unit.py:286 |
| 18 | Wait records (`mesh-wait.<name>.<kind>` markers; recorder `start-wait`/`finish-wait`) | codex_mesh.py:3244 | mesh_runs, design_roles.py, verify.sh |
| 19 | Usage-limit and capacity handling: detect, report to `agent_notes.py`, wait and resume an owed turn | codex_mesh.py:3203, 3275, 4562, 4658, 4628 | inside `start`/`follow` |
| 20 | Account moves of the Codex workers home (`resume-account-move`, `retire_workers_servers`, `_move_worker`) | codex_mesh.py:7847, 8095, 4208 | accounts.py:3517-3650, quota_alert.py, next_moves.py |
| 21 | Model and effort per turn from `[direct.codex]`, service tier | codex_mesh.py:5407, 5378 | inside `_start_turn`; `UNIT_CODEX_MESH` override |
| 22 | Review pool (`prime`, `fork`, `set-instructions`, `reset`; `review_pool.py`) | codex_mesh.py:8181-8390; review_pool.py | retire/transfer/rename_unit, workers.py `transfer-reviewers`. The pool takes no reviews today (review-pool.md:3). review.sh ignores `--pool` |
| 23 | Unit reviewers: ledger workers with the review role, kept across rounds | workers.py `resolve --role review` | assign.sh, review.sh |
| 24 | Held Astra design roles on a mesh | astra/design_roles.py:560, 929, 1246 | astra_review, roles.py |
| 25 | Ask-a-friend resident worker | ask_a_friend/launch_friend.sh, end_friend.sh | `/ask_a_friend` |
| 26 | Coordination board (`board.log`): register, status and done lines, cargo token | board.sh:572 | launchers, verify.sh, workers' prompts; read by progress_history, board_json, production_tree, roster, shutdown/inventory |
| 27 | Session roster lists workers (`_mesh_records`, pool and design meshes) | message/roster.py:1344, 1643, 1711, 1763 | /message, broadcast.py `--workers`, about 12 importers |
| 28 | Shutdown and restart of workers: `codex_seats`, `PausedSeat`, `CodexServer`, resume prompts, the "Codex workers are paused" restart note | shutdown/inventory.py:892-955, stop.py:306-385, 1119-1191, 1327-1440, record.py:466-499, restart.py:614-632, 2242-2395 | `shutdown.py down/restart`, account moves |
| 29 | Displays and counters: production tree edges, Codex wind-down count, waste watch, hierarchies, transcripts, tmp sweep, process table | production_tree.py, codex_winddown.py, waste_watch.py, hierarchies/*, shot_report/transcripts.py:1680, lint/tmp_sweep.py, process_table.py:52 | showrunner tools |

The word "worker" has a second meaning in the shutdown code: Claude background sessions (`kind: "worker"` in the inventory, `SessionShutdown.workers`). This plan does not change those.

## 2. The herdr replacement or a drop, per job

One new module, `scripts/herdr/worker_panes.py`, holds every worker pane operation: `open`, `prompt`, `wait`, `interrupt`, `close`, `list`, `layout`. The launchers, send.py, mesh_runs, the roster and shutdown call it. Nothing else runs `herdr` against a worker pane.

| # | Replacement |
| --- | --- |
| 1 | **Drop.** Each worker is its own Codex or Claude process in a pane, so there is no server to start, watch or sweep. |
| 2 | `worker_panes.py open`: split the column (section 3), then `herdr agent start <name> --kind codex\|claude --pane <id> -- <model, effort and sandbox args from models.conf>`. The pane env carries the account (`CODEX_HOME`, or `accounts.named_account_environment` for Claude), `UNIT_RUN_DIR` and `UNIT_TEAM_ROLE`. |
| 3 | `worker_panes.py prompt`: check `herdr agent get` shows `idle` or `done`, then `herdr agent prompt <name> "Read <prompt path> and do the work it gives." --wait --timeout 60000` to confirm the turn started. On `agent_prompt_stalled`, read the pane once before any second try. |
| 4 | **Drop the claim files.** Only the unit's director prompts its own panes. The idle check in 3 refuses a busy worker. |
| 5, 6 | **Drop** (decision 6). |
| 7 | **Drop the server retry.** A failed `agent start` closes its pane and tries once more in a new pane. A second failure is a launch failure under `<WorkerLaunchFailure/>`, rewritten for panes. |
| 8 | **Replace with a check at turn end.** When a turn ends with the stale-token text, `worker_panes.py wait` restarts that pane's agent with resume on the current sign-in, and the launcher reports it. The sign-in path unit and launchd agent retire (nix: the user's change). |
| 9 | `send.py` `worker:<unit>/<role>` resolves to the pane's agent name and types the message with `herdr agent prompt`. Codex and Claude both queue a message typed during a turn, so nothing is held. **Drop** `pending.json`. |
| 10 | `worker_panes.py interrupt`: `herdr agent send-keys <name> esc`, then an optional prompt. |
| 11 | `worker_panes.py close`: `herdr pane close`, then even out the column (section 3). |
| 12 | Shutdown stops worker panes like any other agent pane (job 28). **Drop** `stop --pause`. |
| 13, 14 | **Keep a smaller ledger.** One file per production/unit maps each role to its agent name, kind, conversation id, model and effort, with state `open` or `closed`. `reopen` becomes `open` with resume. Adopt, transfer and rename carry the file and move the panes with the director's tab (`herdr pane move`). |
| 15 | **Drop** the slot claim files. Keep the run worker log only where a reader still needs it (cleanup check). |
| 16 | **Keep unchanged** in the launchers. |
| 17 | **Keep `mesh_runs.py`, renamed `detached_runs.py`.** A run finds its worker from `HERDR_PANE_ID` and the ledger, not `CODEX_THREAD_ID`. Delivery types the result into the worker's pane (`herdr agent prompt`). The "no launcher is attached" message to the director drops, because typing needs no launcher. |
| 18 | **Keep** the recorder waits, called by `worker_panes.py wait` and detached runs. **Drop** the marker files. |
| 19 | `worker_panes.py wait` reads the pane's last lines after each turn. A usage-limit text reports to `agent_notes.py blocked` as today and queues the workers move (job 20). **Drop** our capacity wait and retry: the CLIs retry an overloaded server themselves. |
| 20 | An account move relaunches each worker pane on the new account. Wait for or interrupt its turn, carry the conversation file to the new home (the step `_move_worker` does today), close the pane, open a new one with the new env and resume, then type "Continue where you stopped." if a turn was cut. The accounts unit owns this. |
| 21 | Start args come from `models.conf`. A running worker changes model with `/model`, typed by the user or by the director (risk 7). **Drop** per-turn resolution and the service-tier code. |
| 22 | **Drop** the review pool and its state. |
| 23 | A review worker is a pane like any other, its role `review`, started read-only (`codex -s read-only`, or Claude in plan mode). It is kept across rounds as today. |
| 24 | A held Astra role is a pane in the requesting director's tab, named for its role. design_roles.py keeps its fences and saved prompts and replies, and loses its mesh calls. |
| 25 | Ask-a-friend opens a pane beside the caller and closes it at `end`. |
| 26 | **Keep.** Register lines say `reach=herdr` in place of `reach=codex_mesh.py`. |
| 27 | The roster reads worker panes from `herdr agent list` joined with the ledger. Pool and design meshes drop out. |
| 28 | Shutdown records each tab's pane layout and each worker's conversation id. Restart rebuilds the tab with the worker panes and resumes each worker. A worker that was mid-turn gets "Continue where you stopped." `codex_seats`, `PausedSeat`, `CodexServer` and the resume-prompt files drop. The shutdown unit owns this. |
| 29 | Each display reads the ledger and `herdr agent list`. `codex_winddown.py` counts working Codex panes. The process-table mesh match drops. |

**Session start.** Directors and showrunners already start through `herdr.launch_agent` (`add_unit.py`, `add_showrunner.py`, `restart.py`). After this plan, workers, reviewers, friends and Astra roles start through the same call into a split pane. Then herdr is the only way a session starts, apart from the user's manual `accounts.py exec`. That retires `agent_exec.sh`'s fresh-review path, the friend launcher and every app-server home.

## 3. The pane layout

- **The director's tab.** With no worker, the director pane takes the full width. The first worker splits it: `herdr pane split <director pane> --direction right --ratio 0.667 --no-focus`. The director keeps the left 2/3 and the worker column takes the right 1/3. A probe confirms which side `--ratio` measures.
- **Adding a worker.** It splits the lowest worker pane down. `worker_panes.py layout` then sets every split in the column to an even share, so k workers each get 1/k of the column. The CLI has no equalize command. The socket's `layout.set_split_ratio` (or one `layout.apply` of the whole tab) sets exact ratios.
- **Closing a worker.** The pane closes, the column evens out again, and when the last worker closes the director takes the full width. Each operation ends with an even column, so the order of open and close does not matter.
- **Focus.** Every split uses `--no-focus`, so the user's focus never moves.
- **Opening and closing.** `implement.sh`, `review.sh` and `assign.sh` open a pane on dispatch when the role has none. Idle panes close at PhaseEnd's worker cleanup point (decision 5), and `end_session.sh` closes the rest. A pane the user opened is never closed by these tools.
- **Names — a proposal, the user's go needed before anything changes.**
  - Agent name: `<unit>-<role>` (`herdr-impl`, `herdr-test`, `herdr-review`). herdr allows `[a-z][a-z0-9_-]{0,31}`, unique on the server. Today's mesh names (`claude-natedev-herdr-impl`) exceed 32 characters for longer unit names. When two productions have a unit of the same name, the second takes its production prefix.
  - Pane border label: the role alone (`impl`, `test`, `review`). The tab title already names the unit.
  - Outside tabs (roster, worker names in messages) the joined form `production · unit · job` stays as the user picked it.
- **What else must change for split tabs:**
  - Tab sort leaves every multi-pane tab unsorted today (`WorkspacesKeptForSplitTabs`, herdr.py:902-1300). It must sort by the director pane.
  - The herdr skill's local rule "start a new agent in a new tab, not a split pane" changes to permit worker panes in the director's tab.

## 4. Rename agents.conf to models.conf, and /agent to /default_models

**When.** After the models unit lands its current change to `/agent` and the `agents.conf` writers (the overlay removal, ETA about 04:45). I agree the list below with the models unit before any edit. It is one mechanical sweep in one commit, with no fallback that reads the old name.

**What is renamed.**

- `config/agents.conf` → `config/models.conf`. The overlay name follows from the main name (`agents_overlay.sh:20`, `agents_registry.overlay_path()`), so it becomes `models.local.conf` by itself if the overlay still exists.
- Env vars `AGENTS_CONFIG_FILE` → `MODELS_CONFIG_FILE` and `AGENTS_LOCAL_CONFIG_FILE` → `MODELS_LOCAL_CONFIG_FILE`. These are used in about 30 files, at about 115 places.
- Code files:
  - `scripts/agents/agents_config.sh` → `models_config.sh`
  - `agents_overlay.sh`/`.awk` → `models_overlay.*`
  - `scripts/lib/agents_registry.py` → `models_registry.py`
  - The path constants: `progress_history.DEFAULT_AGENTS_CONFIG_PATH`, `context_usage.DEFAULT_AGENTS_CONFIG_PATH`, `codex_pacer.AGENTS_CONF` (with `profile_notes_pass.py:252`) and `quota_alert.REGISTRY`.
  - The rename uses one name everywhere, so a reader never maps two names to one file.
- `commands/agent.md` → `commands/default_models.md`. `scripts/agents/agent_admin.sh` → `default_models.sh`. Nothing named `default_models` exists, and it clashes with neither Claude Code's `/agents` nor `/model`.

**Every usage (main at 7e85a74b).**

| Area | Files |
| --- | --- |
| Config and ignore | `config/agents.conf`, `config/README.md:7, 14, 15, 20`, `.gitignore:23-26` (all three patterns) |
| Commands | `agent.md`, `ask_a_friend.md:9`, `history.md:113`, `plan/phase_review.md:173`, `unit/direct.md:28`, `showrunner/add_unit.md:31`, `showrunner/promote_unit.md:18`. The generated Codex skill mirrors (`~/.codex/skills/generated-from-claude/*`) regenerate through `claude_to_codex/sync.py` |
| scripts/agents | `agents_config.sh` (default path :22, about 34 error strings, 5 `mktemp` sites, comment :466), `agents_overlay.sh`/`.awk` (header text :104), `sync_codex_catalog.sh:2, 6, 35, 189, 396, 405`, `agent_admin.sh:95`, `agent_bg.sh:217, 253`, `codex_mesh.py:5387` (removed with the mesh). Tests: `test_agent_exec.py/.sh`, `test_agents_config.sh` (including fail messages that name `/agent`), `test_agents_overlay.py`, `test_launcher_tasks.sh`, `test_sync_codex_catalog.sh:12, 13, 478, 480` (they assert the `/agent` warning text) |
| scripts/direct | `progress_history.py:209, 283, 304`, `assign.py:400`, `demo_refusals.sh:20-24`. Tests: `implement_launcher_test_support.py`, `review_launcher_test_support.py`, `recorder_fixture.py`, `test_assign_launcher.py`, `test_implement_launcher_follow.py`, `test_progress_history.py` (11 sites), `test_workers.py` |
| Other code | `hooks/context_usage.py`, `whoami/codex_pacer.py:6, 58, 440, 523`, `whoami/quota_alert.py:248, 723`, `whoami/profile_notes_pass.py:252`, `astra/design_roles.py:528`, `cli_agent/cli_agent.sh:6, 75`, `history/history.py:115, 121` |
| Other tests | `hooks/test_stop_direct_continue.py`, `whoami/quota_alert_test_support.py`, `test_codex_alert_accounts.py`, `test_profile_notes_pass.py`, `test_quota_alert_cancellation.py` (glob `agents.conf.*`), `astra/test_design_roles.py`, `production/add_unit_test_support.py`, `test_affected_modules.py`, `test_stall_watch.py`, `test_unit_effort.py`, `style/test_add_style.py` |
| Docs | as-built: `agent-registry.md` (10 sites plus 15 `/agent`), `showrunner-automation.md`, `message-clarity.md`, `director-model-study.md` (plus its results JSON), `long-lived-workers.md`, `merge-gate-and-test-speed.md`, `style-showrunner.md`, `unit-progress-history.md`; `docs/direct/launch_implementation.md:24, 72`. Finished plans keep their text as history. |
| Leave as is | `scripts/settings/git_filters_table.sh:16` and its test: a record of a retired filter |
| /etc/nixos (not ours to edit; the change goes to the user) | `modules/common/claude-git-filters.nix:35` (`watchPaths`, functional), `codex-catalog-sync.nix` comments, `codex.nix:47`, `shell.nix:139` (`/agent`), `README.md:410` |

`affected_modules.py` already treats any `config/*.conf` change as a full-suite trigger, so it needs no edit.

## 5. Skill and instruction edits

There is no `/unit:delegate` command and no `docs/delegate` folder on main (7e85a74b). The unit director's files are `commands/unit/direct.md` and `docs/direct/*.md`, and those are the ones edited.

- **`commands/unit/direct.md`**:
  - Rewrite `<LongLivedWorkers/>` for panes: open, resume, close at phase end, no compaction.
  - `<WorkerRuns/>`: delivery into the pane, and cancel by record.
  - `<WorkerLaunchFailure/>`: drop the app-server recovery and keep the one retry in a new pane.
  - `<CodexDispatchWait/>`: only for a Codex director, if one remains.
  - `<PauseForMove/>`: the mover stops and restores worker panes.
  - `<FixDispatch/>` dispatch lines, `<PhaseCleanup/>` (close idle panes), `<HeldAstraRoles/>` (Astra panes), `<ToolingContract/>`.
- **`docs/direct/write_prompt_contract.md`**: `<PhaseMesh/>` becomes `<PhasePanes/>`, and item 3 now says detached runs type their result into the pane. 75 lines name Codex, mesh or worker; each is checked.
- **`docs/direct/launch_implementation.md`, `dual_review.md`, `phase_end.md`, `run_phase_review.md`**: the `assign.sh` and `review.sh` lines lose the pool and mesh text.
- **`commands/unit/report.md`**: the worker table reads the ledger and herdr.
- **`commands/showrunner/produce.md`**: lines 287, 390 (unit reviewers), 476, 534 and 1122-1123 ("leave Codex workers running"). Add one short paragraph that tells the user where workers show: the right column of each director's tab.
- **`commands/showrunner/accounts.md`, `rename_unit.md`, `retire_unit.md`, `transfer_unit.md`**: worker moves become pane relaunches. `commands/message.md`: the `worker:` address. `commands/codex_winddown.md`, `commands/shutdown.md`. `commands/agent.md` becomes `default_models.md` (section 4). `commands/ask_a_friend.md`.
- **`skills/herdr/SKILL.md`**: the local rules for worker panes in the director's tab.
- **`docs/production_format.md:39, 166`; `docs/accounts.md`** ("Swapping the Codex workers account", "Automatic Codex moves").

## 6. Cleanup list

Each item is removed in the cleanup phase, after no run uses the mesh (section 7, step 7).

- **Code to delete:**
  - `scripts/agents/codex_mesh.py` and `test_codex_mesh.py`.
  - `scripts/direct/review_pool.py`, its tests, and `config/review/code.md` if nothing else reads it.
  - The mesh parts of `workers.py`: pause, adopt, allocation, `transfer-reviewers`, `open_unit_worker`.
  - The follow-claim code in the three launchers.
  - `ask_a_friend/launch_friend.sh` and `end_friend.sh`, replaced.
- **Config:**
  - `[direct.options] codex_mesh` and the `UNIT_CODEX_MESH` env var.
  - `UNIT_COMPACT_ABOVE_TOKENS` in `config/direct.conf`.
  - The `implement.sh` permission text in `settings.json:442`, if the launcher's name changes.
- **Shutdown code** (shutdown unit):
  - `CodexServer`, `codex_seats`, `PausedSeat` and `_CODEX_HEADLESS` in `inventory.py`, `session_types.py` and `shutdown_record.py`.
  - The mesh pause and server code in `stop.py`.
  - The paused-workers note and resume-command builder in `restart.py`.
  - The Codex server lines in `shutdown.py status`.
  - The "Codex workers are paused" test in `test_account_move.py`.
- **Accounts code** (accounts unit): `_retire_workers_servers`, `leftover_server_lines` and the roster scan in `accounts.py --detail`.
- **Displays:**
  - The mesh edges in `production_tree.py`.
  - The mesh match in `process_table.py`.
  - The waits and compaction in `waste_watch.py`.
  - The mesh `end` in `hierarchies/worker_owners.py`; `hierarchies/measure.py`.
  - `shot_report/transcripts.py:1680`.
  - The `mesh_runs` path in `lint/tmp_sweep.py`.
  - `board_json.py` worker rows.
- **State on disk:**
  - `mesh_*` files and `*.pending.json` in old run folders under `/tmp/claude/unit/`.
  - `~/.local/state/review-pool/`.
  - The Codex app-server homes used only by the mesh.
  - The ledger files, rewritten to the small form.
- **Outside the repo (the user's changes):**
  - The sign-in path unit (NixOS) and the launchd agent (Mac) that run `signin-changed`.
- **Docs:**
  - Rewrite `as-built/long-lived-workers.md` as the pane design.
  - Remove `review-pool.md` and `review-pool-probes.md`.
  - Change the herdr-adoption "Why: Codex workers stay headless" to the new decision, with the reason.
  - Update `session-roster.md`, `agent-registry.md` ("Addressable codex workers"), `shutdown-and-restart.md`, `unit-board.md` and `usage-ledger.md`.

## 7. Migration order, so running units keep working

The transport is a per-run choice: `[direct.options] worker_transport = mesh|herdr` in `models.conf`. It is read at run start and pinned in the run folder. A run that started on the mesh finishes on the mesh, and units move over one at a time at a run boundary. Both transports live side by side until step 7.

| Step | Work | Owner (proposed) | Needs |
| --- | --- | --- | --- |
| 1 | **Rename** `agents.conf` → `models.conf`, `/agent` → `/default_models` (section 4), including the models unit's shared test fixture | models unit or herdr-unit, agreed with models | models unit's overlay removal and shared fixture landed |
| 2 | **`worker_panes.py`**: open, prompt, wait, interrupt, close, list, layout, with tests against a probe tab of `agents`. Probes: the `--ratio` side; whether closing a pane rebalances the rest; Codex queuing a message typed mid-turn; herdr's Codex state over a long turn; prompt by path; resume after close | herdr-unit | — (can run beside step 1) |
| 3 | **Shutdown and restore of split tabs**: record each tab's pane layout and worker conversation ids, rebuild and resume on restart, sort multi-pane tabs by the director pane. Settle with the user whether to set herdr's `resume_agents_on_restore = false` (natedev's config has no `[session]` section today, so herdr's own resume is on) | shutdown unit | step 2 |
| 4 | **Transport switch** in `implement.sh`, `review.sh`, `assign.sh`; `detached_runs.py` delivery; send.py `worker:`; roster; the small ledger. Pilot: herdr-unit's own next run | herdr-unit | steps 2, 3 |
| 5 | **Account moves and usage limits** for pane workers (section 2, rows 19-20) | accounts unit | step 4 |
| 6 | **Astra roles and ask-a-friend** in panes; skill and doc edits (section 5); then switch units over one by one at their run boundaries, Mac units included | herdr-unit with the guilds and sdlc owners of those files | steps 4, 5 |
| 7 | **Cleanup** (section 6), after a check that no run folder pins `worker_transport = mesh` and no mesh process is alive | herdr-unit, shutdown unit, accounts unit for their parts | every unit switched |

A reboot before step 3 lands would lose worker panes, so no real unit uses panes before step 3.

## 8. Risks

1. **herdr reads a Codex pane wrong** (`idle` or `unknown` mid-turn). This could close a pass early. Decision 2 guards against it: only the worker's status file ends a pass. Step 2 measures herdr's Codex state over long turns.
2. **Typed prompts stall or garble** in a terminal UI. A prompt is one short line that names a file. `--wait` reports a stall, and the launcher reads the pane before any second try.
3. **Workers lost on a reboot** until split-tab restore lands (step 3). herdr's own resume is on today on natedev, and our restore clears herdr's restored layout. These two must agree before any unit uses panes.
4. **Account moves:** the pane env is fixed when the pane is made, and Codex keeps conversations under `CODEX_HOME`. A move must carry the conversation file and open a new pane. This is the hardest port, and it is why it has its own step.
5. **Usage limits are no longer retried by our code.** The director learns of a limit only from the end-of-turn read in `worker_panes.py wait`. A limit text that the read does not match leaves a worker idle with no alert. The match uses the same text `codex_mesh` matches today, with tests.
6. **Screen room.** Three or more workers in a 1/3 column on a laptop screen are short. Idle panes closing at phase end (decision 5) keeps the count low. Zoom (`herdr pane zoom`) shows one pane at full size.
7. **`/model` in Codex is a picker, not a one-word command.** A director script that drives it with keys is fragile. The director changes a running Codex worker's model by restarting its pane with resume and new args. The user can type `/model` by hand at any time.
8. **The user and the director typing into one pane at the same moment.** The director checks `idle` before typing, and a user's typing during a turn queues like any message. herdr can't lock a pane against the user, and it should not.
9. **Shutdown ownership.** Each worker pane is its own process in its own scope. The bounded stop must record worker panes as owned by their director, or a stop reports them as unknown and fails (the round 8 conservative rule). This is the shutdown unit's part of step 3.
10. **Name length and uniqueness.** herdr names are at most 32 characters and unique on the server. The proposed `<unit>-<role>` names need the user's go, and a check that refuses a clash.
11. **Mac parity.** Mac directors run in the Mac's herdr. Step 6 switches Mac units only after a Mac probe of steps 2-3.
12. **Two transports at once** for days. A run pins its transport at start, and the cleanup checks that no run pins the mesh before anything is deleted.
13. **The NixOS edits** (git filter `watchPaths`, sign-in units) are the user's. Until the user changes the git filter watch path, the filter does not refresh when `models.conf` changes. The rename step sends the user the exact edit.

## 9. Copies this plan retires, and the one place for each

The sdlc standard (2026-10-30) applies to every step. Each work order and review prompt this plan sends carries these four lines beside the Three Gods:

1. Good engineering first, not after the user notices. Before a worker writes code, ask: is there already one place for this? Use it, or make it, then call it.
2. No copies. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. Reviews flag it; you send it back. A test helper that each file writes for itself goes into one shared helper.
3. When many things break the same way, the copy is the cause. Name it first in your report, with the one-place fix, before anyone asks.
4. Fix at the cause, never a patch at each site.

| Copy today | Where the copies are | The one place after this plan | Step |
| --- | --- | --- | --- |
| Test files that write their own `agents.conf` | 26 test and fixture files under `scripts/` (launcher test supports, `recorder_fixture.py`, `test_progress_history.py`, `add_unit_test_support.py`, `quota_alert_test_support.py`, `test_design_roles.py`, …) | The models unit's shared `agents.conf` test fixture (its main-red fix, 2026-10-30, before 02:00). This plan adds no second fixture. Step 1 renames that fixture with the file (`models.conf`, `MODELS_CONFIG_FILE`) and checks that no test still writes its own config | models unit (fixture), 1 (rename) |
| The config file's path, stated per module | `progress_history.DEFAULT_AGENTS_CONFIG_PATH`, `context_usage.DEFAULT_AGENTS_CONFIG_PATH`, `codex_pacer.AGENTS_CONF`, `quota_alert.REGISTRY`, the bash default in `agents_config.sh:22`, `agents_overlay.sh:19`, `sync_codex_catalog.sh:6` | `models_registry.registry_path()` in Python, and `models_config.sh` exports the one path for bash | 1 |
| Line parsers of the config | `codex_pacer.py:440`, `quota_alert.py:704`, `context_usage.py:269`, `progress_history.py` each split `registry_text()` lines themselves; `agents_config.sh` parses again in bash | `models_registry.py` gives typed lookups (`resolve(function, subtask)`), and every Python caller uses them. The bash resolver stays as the one bash parser, and a shared test feeds both the same file so they can't drift | 1 |
| Worker identity records | `mesh_roster.json`, the unit worker ledger, the run `workers` log, `impl_worker_*.json`, `impl_claim_*.json` | The small ledger (section 2, rows 13-15), read through `worker_panes.py` | 4 |
| herdr calls against worker panes | would spread over the launchers, send.py, detached runs, roster and shutdown | `worker_panes.py` alone; the others call it | 2 |
| Session start paths | `codex_mesh start`, `agent_exec.sh` fresh review, `launch_friend.sh`, `herdr.launch_agent` | `herdr.launch_agent`, with `worker_panes.py open` as its split-pane form | 4, 6 |
| Codex process detection | `process_table.is_codex_agent`, `codex_winddown.py`, `inventory._CODEX_HEADLESS`, `stop._mesh_server_roots` | `herdr agent list` joined with the ledger, through `worker_panes.py list` | 7 |
| Resume and continue texts | `stop.RESUME_PROMPT`/`IDLE_RESUME_PROMPT`, `restart.restart_note`, the mesh's "Continue where you stopped." | One constant in `worker_panes.py`, used by shutdown, moves and the launchers | 3, 5 |
| Usage-limit text matching | `codex_mesh.QUOTA_REFUSAL`, and matchers in `whoami` | One matcher, used by `worker_panes.py wait` and `quota_alert` | 5 |

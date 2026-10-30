# Workers in herdr panes: overview

This is a short form of the full plan, `docs/plans/herdr-worker-panes.md` (commit a1e504a3). The plan adds one staff record of every showrunner, unit director and worker. It moves each unit worker from a `codex_mesh` app-server thread to an agent in a herdr pane in its director's tab, and then deletes `codex_mesh`. It renames `agents.conf` to `models.conf` and `/agent` to `/default_models`. The items go from most to least fundamental: each item uses only items above it. Each item names its plan phase. The NixOS edits are yours.

1. **Staff record** (Phase 1)
   - Does: `staff.py` keeps one file for both machines, `~/.local/state/staff/staff.json` on natedev (the Mac reads and writes it over ssh, as the roster reaches the other machine). Each showrunner, unit director and worker has one row: a stable id, its short name, its kind and its parent's id. So the record maps each short name to an id and holds who reports to whom (worker → unit → showrunner). A worker's short name is its role and number, for example `impl1`; each unit can have an `impl1`. Lookups go by id or by path, for example `sdlc/models/impl1`. Live state (pane, running or not) is not stored; it comes from herdr and the session records at read time. `staff.py seed` fills showrunner and unit rows from today's sources, with the names their workspaces and tabs show now. It also adds `scripts/lib/machine.py`, the one `machine_name()`.
   - Replaces: no record exists today. Each reader joins update timers, parent files, Units tables, pane marks and worker ledgers. Later items move the unit worker ledger, `mesh_roster.json`, the run `workers` log and the slot claim files into this record.
   - Open choice: none. You gave the design (2026-10-30).
   - Depends on: none.
2. **Staff record writers** (Phase 2)
   - Does: each tool that adds, renames, moves or retires a showrunner or unit also writes the record (`add_unit.py`, `rename_unit.py`, `transfer_unit.py`, `retire_unit.py`, `showrunners.py attach`, and others). A rename changes one row's name. A transfer, an attach or a detach changes one row's parent. Nothing else moves: workers keep their ids and follow through the parent link. After this, the record stays current without `seed`.
   - Replaces: nothing is removed. Today these tools write only their own sources.
   - Open choice: none.
   - Depends on: 1.
3. **models.conf and /default_models** (Phase 3)
   - Does: one sweep in one commit renames `agents.conf` to `models.conf`, `/agent` to `/default_models`, `agents_registry.py` to `models_registry.py`, and the env var and shell scripts to match. `models_registry.py` is the one parser and the one path to the file. Bash gets its values from one call per launch. A new test fails on any live use of an old name.
   - Replaces: the old names; the path constants and line parsers in four Python modules; line parsing in bash; test files that write their own `agents.conf` (they use the models unit's shared fixture).
   - Open choice: none in code. The phase waits for the models unit's registry rewrite on main. The NixOS edits are yours (question 1).
   - Depends on: none in this list.
   - Not on the critical path: pane work reads model and effort through the models unit's registry API under either file name. Critical path: Phase 1 → 4 → 5 → 6 → 7 → 8 → 9 → 11 → 13; Phases 2, 3, 10 and 12 run beside it.
4. **herdr split-pane primitives and probes** (Phase 4)
   - Does: `herdr.py` starts an agent in a new split pane, with the same checks as a new tab today. It sets split ratios and pane labels. A split does not take focus. Probes in a probe tab record how herdr and both CLIs act: ratios, typing during a turn, long Codex turns, prompt and image by path, how to name a new Codex session, restore with each pane's env, and Codex sign-in pickup.
   - Replaces: today a launch opens a new tab only. Over time the other launch paths (`codex_mesh start`, the `agent_exec.sh` fresh review, `launch_friend.sh`) go. `herdr.py` stays the one herdr client.
   - Open choice: none. The probe results decide herdr's own restore and the Codex sign-in restart.
   - Depends on: none.
5. **Worker column layout** (Phase 5, `worker_panes.py layout`)
   - Does: in a director's tab, the director pane gets 2/3 of the width. The worker column on the right gets 1/3, and the workers split it evenly. Layout changes only the panes of this unit's workers, which it finds through the record by their ids, and gives focus back. Each worker pane shows the worker's short name as its label, for example `impl1` (your choice).
   - Replaces: workers have no pane today; they run as `codex_mesh` app-server threads, not in a tab.
   - Open choice: none.
   - Depends on: 1, 4.
6. **Start or resume a worker** (Phase 5, `worker_panes.py start-or-resume`, `close`, `list`)
   - Does: `start-or-resume <short name> <role>` adds the worker row under the director's unit, looks up kind, model, effort, service tier and sandbox for the role in `models.conf`, and opens the pane. The herdr agent and the Claude or Codex session are named by the worker's id, so a rename or a transfer never touches them. It resumes the conversation by the id, else starts a new session with that name. Model and effort are read only then: a `/model` you type by hand holds, and a config change does not restart a running worker (your choice). A worker can be Codex or Claude; the default kind per role does not change. `close` closes only a pane with no work left. Idle panes close at phase end and the conversation stays. `list` joins `herdr agent list` with the record.
   - Replaces: mesh `start`, `reopen` and `adopt`; the unit worker ledger.
   - Open choice: none.
   - Depends on: 1, 3, 4, 5.
7. **Dispatch and the launcher switch, off by default** (Phase 6)
   - Does: `worker_panes.py dispatch` takes the worker's dispatch lock, calls `start-or-resume`, and types one line: "Read <prompt path> and do the work it gives." The launcher waits with one background `herdr agent prompt --wait`. A pass is complete only when the worker's final reply ends with `DONE <dispatch id>`. The launcher then writes the status file, and for a reviewer the findings file. `blocked` (an approval dialog) wakes the launcher, so the director does not stall. A dispatch to a busy worker exits "busy". `implement.sh`, `review.sh` and `assign.sh` keep their arguments and call this one dispatch. `WORKER_TRANSPORT=mesh|herdr` in `config/direct.conf` is pinned per run, so a run that starts on the mesh ends on the mesh. The default stays `mesh`. The unit director instructions branch once on the pin.
   - Replaces: mesh `follow` and the slot claims; launcher compaction and the "Continue where you stopped." retry (the interactive CLIs compact by themselves); the dispatch code in three launchers.
   - Open choice: none.
   - Depends on: 5, 6.
8. **Messages, detached runs and review Stage 2** (Phase 7)
   - Does: `send.py worker:<unit>/<short name>` types a message into the pane at once, also during a turn; the CLI queues it. A message to a blocked or closed worker goes to its director, with one line that says why. `mesh_runs.py` becomes `detached_runs.py`, which types a run's result into the worker's pane. Review Stage 2 waits for its own `DONE <dispatch id>-2` line. One classifier in the accounts refusals layer finds usage refusals. The pass then shows `refused` and stays open, and the director dispatches it again after the account move.
   - Replaces: mesh `send` and `pending.json`; `mesh_runs.py`; three usage-refusal matchers (`codex_mesh.QUOTA_REFUSAL`, `refusal_watch.py`, `settle.py`).
   - Open choice: none.
   - Depends on: 1, 7.
9. **Shutdown and restore of split tabs** (Phase 8, shutdown unit)
   - Does: the shutdown record lists each worker pane under its director, and shutdown stops the panes inside the 66 s deadline. A restart brings back each director tab with its workers resumed and their launchers started again. One builder, `worker_resume_prompt(cause, was_busy)`, writes every resume text. Tab sort sorts a tab with several panes by its director pane.
   - Replaces: mesh `stop --pause` (shutdown's mesh code goes at cleanup); the resume texts in `stop.py`, `restart.py` and the mesh.
   - Open choice: none in code. The Phase 4 probe decides. If herdr keeps each pane's env, herdr restores split tabs and our per-pane restore retires for them. If not, the herdr config line in question 1(e) goes in before the default switch, and our restore owns split tabs.
   - Depends on: 4, 6, 7.
10. **Account moves for pane workers** (Phase 9, accounts unit)
    - Does: under the worker's dispatch lock, a move waits for the turn or interrupts it, closes the pane, checks that the process is gone, copies the conversation files to the new account home, and resumes by id there. If this fails, it resumes once on the old account and reports.
    - Replaces: the mesh server steps in account moves (`_retire_workers_servers`, `leftover_server_lines`).
    - Open choice: none.
    - Depends on: 6, 7, 8.
11. **Astra roles, ask-a-friend, displays and instructions** (Phase 10)
    - Does: a held Astra role is a pane in the requesting director's tab. Ask-a-friend opens a pane beside the caller and closes it at `end`. Displays (`roster.py`, `production_tree.py`, `codex_winddown.py`, `waste_watch.py`, `board_json.py`, `hierarchies/*`) read workers from the staff record and `herdr agent list`. The showrunner and unit director skills, commands and docs describe panes.
    - Replaces: mesh calls in `design_roles.py`; `launch_friend.sh` and `end_friend.sh` (deleted at cleanup); mesh reads in displays; mesh text in instructions.
    - Open choice: none.
    - Depends on: 1, 6, 7, 8.
12. **Pilot and default switch** (Phase 11)
    - Does: herdr-unit does its own next run on panes. Each defect found gets a repair before the switch. Then `WORKER_TRANSPORT=herdr` becomes the default, and each unit switches at its next run start. Mac units switch after a Mac probe of items 4 to 6.
    - Replaces: `mesh` as the default transport.
    - Open choice: it needs your answers to question 1(c), the herdr skill rule, and 1(e), the herdr config line.
    - Depends on: 4 to 11.
13. **Readers move to the staff record** (Phase 12)
    - Does: every reader of who exists reads the staff record, also across machines where a reader reads the other machine today. Live state still comes from herdr and the session records. Pane marks stay as the live link to a unit's pane. Worker readers move earlier, in items 7 to 11.
    - Replaces: the joins at each read in 39 modules (plan Reference, "Staff record readers"). Showrunner parent files move into the row's `parent` field.
    - Open choice: none.
    - Depends on: 1, 2.
14. **Cleanup** (Phase 13)
    - Does: after a check that no run pins `mesh` and no mesh process runs, it deletes the mesh code and its state, no later than 7 days after the default switch. It updates the as-built docs.
    - Replaces: nothing new; it deletes `codex_mesh.py` (8652 lines), `review_pool.py`, the unit worker ledger, `mesh_roster.json`, `*.pending.json`, the follow-claim code, `launch_friend.sh`, `end_friend.sh`, `WORKER_TRANSPORT`, `UNIT_CODEX_MESH`, `UNIT_COMPACT_ABOVE_TOKENS`, the mesh code in shutdown, accounts and displays, and the Codex app-server homes.
    - Open choice: the NixOS sign-in units (question 1(d)) retire here, or point at a pane restart if the sign-in probe is negative.
    - Depends on: all items above.
   - Also: the three launchers merge into one, `scripts/direct/launch.py`; the three command names stay as thin entry points.

Settled by the user (2026-10-30): the staff record holds a short name and an id for each showrunner, unit and worker, and who reports to whom, and workspace and tab names stay as they are; the pane label is the worker's short name (`impl1`); model and effort are looked up once, at start or resume.

Your rule (2026-10-30): each tool that changes shared state in steps is safe to rerun, and each gets one rerun test.

## Questions for you

1. **The NixOS edits (yours).** (a) `claude-git-filters.nix` `watchPaths` to `models.conf`; the git filter does not refresh until it changes. (b) Comments that name `agents.conf` or `/agent`. (c) The herdr skill rule "new agent in a new tab, not a split pane" must allow worker panes. (d) The sign-in units that run `codex_mesh.py signin-changed`. (e) Only if the Phase 4 probe shows that herdr does not keep each pane's env: add `[session] resume_agents_on_restore = false` to `~/.config/herdr/config.toml` on natedev and the Mac. Recommendation: herdr-unit writes one diff file per edit for you to apply: (a) when the rename lands, (c) and (e) before the default switch, the rest at cleanup.

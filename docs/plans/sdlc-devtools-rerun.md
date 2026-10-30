# sdlc-devtools-rerun

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Make the production tools safe to run again after a stop or a failure part way. Each phase fixes one cause in one shared place, moves every copy onto it, and adds a check that refuses a new copy. Phase 1 is the file writer.

> **Production: sdlc** — unit `devtools-unit`; production doc `docs/plans/sdlc-production.md`

## Source

The user, through the sdlc showrunner, 2026-10-30: "all of our tools need to be idempotent", "i mean tools of this nature" (stop, restart, move, transfer, retire, rename, merge, record steps), "only for things that benefit from idempotency". The user, 2026-10-30: no copies without the user's approval; when many things break the same way, the copy is the cause.

**What `docs/plans/sdlc-rerun-sweep.md` covers, and where this plan differs.** The sweep (sdlc-phases Phase 16, read at `097b1a0d`) lists 123 rows, one per fault site, routed to each owner. Since then owners fixed many rows one site at a time (checkpoints 782115f4 unit-board 19 rows, fae386a7 and de48febf rename/rename_unit/retire/transfer, c11604e2 merge_checkpoint, b142c615 / 35e8ce87 / a6246f7d messaging, 12b4948b style, 0bf90614 herdr, 97aa0200 and 8dc7b914 hierarchies, 208025a5 accounts, bed23082 efficiency, 5a832e6f screenshot). sdlc-phases Phase 17 takes five rows and builds `scripts/lib/process_start.py` with `check_owner`; Phase 20 takes the verify.sh wait id. This plan does not repeat the rows. It groups faults by cause, and for each cause names the one place that fixes every site, so a per-site fix is not written again. It also adds faults the sweep did not list (inventory rows marked *new*).

**Recheck of the sweep rows on origin/main `c9381f22`** (three read-only checkers, 2026-10-30; table in the scratchpad file `rerun_sweep_check_main_c9381f22.md`): of 123 rows, 76 are fixed, 3 partly fixed and 44 open. sdlc-phases Phase 17 holds 5 open rows (`assign.py:363`, `findings.py:1218`, `verify.sh:2065`, `verify.sh:1157`, `end_session.sh:87`). The other rows go to the phases below by class, when the phase's goal fixes them; a row with one site and no shared cause goes to its owner:

| Class | Open rows (main line) | Phase |
|---|---|---|
| A in-place write | `memory_admit.py:2290`, `sweep.py:2142`, `codex_mesh.py:1465`, `:2027`, `workers.py:560` | 1 |
| E linked worktree deleted before git forgets it | `remove_run_scratch.py:118`, `folder_audit.py:256` | 2 |
| E other delete or move before success, one site each | `scratch.py:202`, `codex_hooks.py:237`, `build_hold.py:1173`, `buildlog/record.py:497`, `send.py:1213`; `review_pool.py:439` closes with the `--cleanup-only` route below | owner |
| C pid only; H owner is the shell, not its group | `astra_roles.py:276`, `implement.sh:312`, `review_pool.py:144`; `memory_admit.py:1451` | 3 |
| B claim with no owner | `board.sh:329`, `design_roles.py:753`, `feature_log/inventory.py:566` (partly), `settle.py:791` (partly: plain `down` gives no expiry; held: user decision pending), `mesh_runs.py:258`, `codex_mesh.py:8233` | 4 |
| A exclusive create, then write | `accounts.py:881`, `astra_work.py:167`, `tmp_sweep.py:1140`, `review_pool.py:359` | 5 |
| D notice marked before it is sent | `send.py:949`, `send.py:1614`, `settle.py:1843` (held: user decision pending), `main_sync.py:122` | 6 |
| F rerun refuses its own partial output, one site each | `role_map.py:587`, `refusal_watch.py:949`, `design_roles.py:927`, `:1009`, `:1438`, `merge_gate.py:1145`, `:1057` | owner |
| D, H state and its event or side effect in two steps | `progress_history.py:1474`, `:5696`, `settle.py:846` (held: user decision pending), `workers.py:1681`, `worker_owners.py:506` (partly) | 8 |

**Route now, outside the phases:** `hierarchies/worker_owners.py:720` passes `--cleanup-only` to `codex_mesh.py end`, which has no such flag yet, so every cleanup retry exits 2. The tests use a stand-in cleanup, so none catches it. hierarchies-unit keeps the flag, because a plain `end` races with a reopened worker, and models-unit adds `codex_mesh.py end --cleanup-only` in its mesh follow-up (sdlc showrunner, 2026-10-30). That flag closes the sweep rows `worker_owners.py:449`, `workers.py:1604` and `review_pool.py:439`. Owner: models-unit.

**Held: user decision pending** (sdlc showrunner, 2026-10-30). The user has a natedev proposal open that may delete shutdown, transfer, rename and restart code (stop records, conductor, transfer journal, long rename steps). Every row and caller in that code is marked held below, and no phase touches it until the showrunner gives the answer: `scripts/shutdown/` (launch_permission, pane_record, restart, settle, shutdown_record), `lib/relaunch.py`, `production/transfer_unit.py`, `production/rename.py`, `production/rename_unit.py`.

Read on origin/main `c9381f22` (2026-10-30 12:41 EDT). Counts come from an AST and grep scan of `scripts/` without test files, fixtures and `*_test_support.py`.

### Inventory: tools of this nature

Q1 stop-and-rerun finishes · Q2 live check, not stored status · Q3 failure keeps what a rerun needs · Q4 one failure does not stop unrelated steps · Q5 each mark has an owner or expiry and a clear command. "ok" = no fault found in this read.

| Tool and lasting state | Q1 | Q2 | Q3 | Q4 | Q5 | Rank | Phase |
|---|---|---|---|---|---|---|---|
| `shutdown/launch_permission.py` launch claims `.record.launch-<id>.tmp` (*new*) | no: claim written in place, `:213`, `:229`, `:261` | no: pid only, `:230` | ok | ok | no: a launcher killed before its `finally` (`:236`) leaves the claim; no clear command; `down --all` reads it every time (`settle.py:894`) | at risk | 1, 4 (held: user decision pending) |
| `direct/workers.py` roster | no: seek, truncate, dump in place, `:558-563` | sweep row `:1604` (stored `ended`) | no: a stop after truncate loses every worker's record | ok | ok | unsafe | 1 |
| `agents/codex_mesh.py` roster, inbox | no: 17 in-place `truncate()` sites | sweep rows | no (sweep `:1443`, `:2006`) | ok | sweep `:8169` prime claim | unsafe; retires in herdr-worker-panes Phase 18 | 1 only |
| `message/send.py` `--key` claim (*new*) | no: `claim()` records the key before delivery, `:931-950` | ok | no: only an emergency send forgets a failed claim, `:2212-2213`; any other failed keyed send is skipped for its whole window | ok | no clear for a non-emergency claim but `reopen` | at risk | 6 |
| 7 "notified" marks saved before the send (sweep rows: `five_hour.py:92`, `codex_winddown.py:233`, `main_sync.py:122`, `send.py:1389`, `stall_watch.py:679`, `settle.py:1568`, `dailies_input.py:1095`) | no, where still open | ok | — | — | no expiry | at risk; some fixed per site | 6 |
| `production/transfer_unit.py` journal (*new* fault) | at risk: `_commit_only` (`:248-254`) adopts an earlier commit only when it is HEAD; after another merge-branch commit the rerun commits clean paths again (prove with a test) | ok: stop and relaunch read live state | ok | ok | ok | at risk | 7 (held: user decision pending) |
| `production/rename.py`, `rename_unit.py` journals | ok since fae386a7 | ok | ok | ok | ok | safe; 3 copies of one step runner (`rename.py:597-650` step names, `rename_unit.py:1147-1290` and `transfer_unit.py:573-636` step counts) | 7 (held: user decision pending) |
| doc commit or adopt: `add_unit.py:789` `commit_paths` (used by `retire_unit.py:702`, `resume_unit.py:231`), `transfer_unit.py:248`, `rename_unit.py:540`, `rename.py:585` and `:792`, `production_lifecycle.py:222-231`, `promote_showrunner.py:116` | mixed: three adoption rules (status clean, HEAD subject, subject in log) | — | — | — | — | at risk; 6 copies | 7 (transfer_unit, rename_unit, rename: held: user decision pending) |
| linked worktree removal: `remove_run_scratch.py`, `folder_audit.py:256-260`, `retire_unit.py:710`, `production_lifecycle.py:558`, `waiting.py:298`, `style_sync.py:284`, `perform_deletion.sh:58-62`, `lib/scratch.py:263` | — | — | no in two (sweep `remove_run_scratch.py:118`, `folder_audit.py:256`): folder deleted before git forgets it | — | — | at risk; 8 copies | 2 |
| exclusive-create content files: `astra_work.py:167`, `tmp_sweep.py:1140`, `astra_review.py:184`, `accounts.py:883`, `issue_score/blind.py:111`, `score_issues.py:195`, `:250` | no: a stop leaves a part file that every rerun refuses | — | — | — | — | at risk | 5 |
| claims and lock dirs that outlive their process: `mesh_runs.py:260` `.reap`, `five_hour.py:230`, `board.sh` lock dir (sweep `:329`), `sync_codex_catalog.sh:73`, `run_sync.sh:43`, `feature_log/inventory.py:564` | no | no | — | — | no owner, expiry or clear | unsafe where open | 4 |
| stored pid checks `os.kill(pid, 0)` / `kill -0`: 31 files; Phase 17 moves 10 | — | no in 21 files outside Phase 17 (sweep `review_pool.py:144`, `astra_roles.py:276`, `implement.sh:311`) | — | — | — | at risk | 3 |
| `build_hold/build_hold.py` release (sweep `:1173`) | no: `release --resume` needs a holder file (`:757`, `:1325`); no checkpoint since the sweep | ok | no | ok | ok | unsafe; natedev builds owns | route |
| `direct/progress_history.py` start-phase (sweep `:1480`, now `:1475`), turn wait (`:5634`) | no: state saved before the event | ok | ok | ok | ok | unsafe; roles-unit owns | 8 |
| `shutdown/settle.py` hold registration, timers, stopped status (sweep `:786`, `:833`, `:1217`) | no (`:846`, `:1843`) | ok | — | — | partly (`:791`) | at risk; accounts-unit owns | 4, 6, 8 (held: user decision pending) |
| `production/merge_gate.py` push after merge (sweep `:1124`, `:1000`) | no (`:1145`, `:1057`) | — | — | — | — | unsafe; natedev builds owns | route |

**The shared causes (the copy is the cause):**

- **File writer.** `scripts/lib/atomic_file.py` `write_text_atomic` exists, and 21 files call it. 98 files (127 functions) write a temp file and replace the target with their own code; 22 of them use one fixed temp name (`with_suffix(".tmp")`) that two writers share. 25 `seek(0)` + `truncate()` in-place rewrites remain in 6 files (codex_mesh 17, design_roles 3, workers 2, shot_report, sweep, worker_names). More in-place JSON writes: `launch_permission.py:213`, `:229`, `:261`; `assign.py:330`, `:350`, `:399`; `progress_history.py:5697`; `silence.py:130`; `review_regime.py:234`; `shot_report.py:901`. The herdr-worker-panes plan already sets this rule for its own phases ("One file writer").
- **Owner check.** `scripts/lib/process_start.py` `check_owner` (sdlc-phases Phase 17, todo). 21 files keep a stored-pid probe outside its Files list.
- **Claims.** No shared claim helper. 71 files use `flock`, which the kernel releases when the process ends; about 8 claims and lock dirs outlive their process with no owner check.
- **Notices.** `send.py --key --repeat-minutes` is the one place for "send once", but it keeps a failed non-emergency claim; callers keep their own "notified" marks beside it.
- **Lifecycle steps.** Three step runners and six doc-commit rules.
- **Worktree removal.** Eight removal paths; `lib/scratch.py` already documents the safe form (`git worktree remove --force`).

**Not in this plan.** Open rows with one site and no shared cause go to their owners through the sweep: class E `scratch.py:202` (adopt), `codex_hooks.py:237`, `build_hold.py:1173` (release), `buildlog/record.py:497`, `send.py:1213`; class F `role_map.py:587`, `refusal_watch.py:949`, `design_roles.py:927`, `:1009`, `:1438`, `merge_gate.py:1145`, `:1057` (push after merge). codex_mesh fixes beyond the file writer: the mesh retires in herdr-worker-panes Phase 18 (the user's request to retire it), so only one-line writer moves go there.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Stack:** Python 3.13, standard library only; bash; Markdown.
- **Key files:**
  - `scripts/lib/atomic_file.py` — the one file writer (owner: unit-board-unit; agree any change with it first)
  - `scripts/lib/process_start.py` — the one owner check (sdlc-phases Phase 17; owner: phases-unit)
  - `scripts/lib/rerun_test_support.py` — the one rerun test harness: disposable HOME and repo, one-shot `git` and command stops (`fail_git`, `stop_command`)
  - `scripts/lib/scratch.py` — scratch folders and the safe worktree removal
  - `docs/plans/sdlc-rerun-sweep.md` — the per-site rows and their owners
- **Test:** each module's own suite, `python3 -m unittest discover -s scripts/<folder> -p '<test file>'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only --full`, from the worktree root, after `build_hold.py status` shows no natedev hold that is not ours.
- **Lint:** `basedpyright <changed Python files and every caller module>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - One place for each thing (user, 2026-10-30). A phase moves every copy of its cause onto the shared place, deletes the copy, and adds a check that refuses a new one. No forwarding wrapper that only calls the shared place.
  - After a stop or a failure at any step, a rerun ends in the same state as one clean run. A rerun of a finished run reports done and changes nothing (user, 2026-10-30, as in herdr-worker-panes "Rerun-safe tools").
  - Each rerun test uses `scripts/lib/rerun_test_support.py` or the module's own existing fixture. No second fault-injection harness.
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, `~/.cargo/bin`).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - Owners: natedev owns `verify.sh`, `merge_gate.py` and `build_hold.py` (ask natedev before an edit, memory `devtools-unit-goals`); shutdown-unit owns `scripts/shutdown/`; accounts-unit owns `scripts/accounts/`; models-unit owns `scripts/agents/`. A phase that changes their files agrees the edit with the owner first and names each owner in the checkpoint notice.
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — Every state file write goes through the one file writer  · status: todo
**Depends on:** the sdlc-devtools-tests plan is done (sdlc showrunner, 2026-10-30: this plan runs after it); sdlc-phases Phase 20 (b8afa569: `write_text_atomic` keyword `create_parent`) on main

#### Work Order

**Goal:** a stop at any line of any state write leaves the old file or the new file, never part of one; every write uses `scripts/lib/atomic_file.py`; and a test refuses a new local copy.

**Spec:**

- The one place: `write_text_atomic(path, text, *, create_parent=True)` in `scripts/lib/atomic_file.py` (temp file beside the target from `mkstemp`, `fsync`, mode kept, `os.replace`, folder `fsync`; `create_parent=False` leaves a missing folder an error). The phase builds on it and adds no second writer (sdlc showrunner, 2026-10-30). Its behavior does not change. A caller that writes bytes of text writes that text through it. A caller whose bytes are not text goes to the sdlc showrunner before the Work Order; the phase adds no writer for it.
- **Copies.** Each function outside `atomic_file.py` that writes a temp file and then replaces the target with it (`os.replace`, `Path.replace`, `Path.rename`) becomes one call to the shared writer. The caller keeps its own serialization at the call, for example `atomic_file.write_text_atomic(path, json.dumps(value, indent=2) + "\n")`, so no file changes its bytes. A local writer function (`_atomic`, `_atomic_write`, `write_atomic`, `atomic_json`, `atomic_text`, `atomic_write_text`, `write_json`, `_write_json`, `_atomic_json`, `replace_text`, `_replace`) is deleted, and its callers call the shared writer. The fixed temp names (`<name>.tmp`) go with the copies.
- **In-place rewrites.** A rewrite that truncates the data file (`seek(0)` + `truncate()`, or `write_text` on a state file that a later run reads back) moves to the shared writer. These callers hold `flock` on the data file itself, and a replace gives the path a new inode, so a lock on the old inode no longer serializes writers. Each such caller first moves its lock to a sibling `<name>.lock` file that it never replaces, then writes with the shared writer under that lock. Readers that took the lock to read take the sibling lock.
- **Copies the sweep fixes added** move too: `quota_alert._write_state` (fixed `.state.tmp`), `detection_rules` `staged_receipt`, `worker_names` lines 77-86, `silence.release`, `hana_shot._write_launch_state`, `issue_score/client._write_pending`, `style_admin.replace_text`, `bulk_rename.atomic_json` and `BatchIntent.recover`, `five_hour.save_delivery`, `codex_winddown._save_quiet_notice_state`.
- **Out of scope** (author's scope note): append-only journals (`a` mode, one line per write, readers skip a part last line); moves of whole files or folders that are not a write (archives, `*.removing` renames); exclusive-create files (Phase 5); shell scripts (`mv "$tmp" "$target"`), which have no shared writer yet.
- **The check.** New `scripts/lib/test_one_file_writer.py` parses every `scripts/**/*.py` except test files, fixtures and `*_test_support.py`. It refuses (1) a function, other than in `atomic_file.py`, in which one name is both written (`write_text`, `write_bytes`, `open(..., "w"|"wb")`, `os.fdopen`, `mkstemp`) and passed as the source of `os.replace`, `Path.replace` or `Path.rename`; and (2) a `truncate()` call on a handle opened on a path, outside `atomic_file.py`. Each refusal names `path:line` and says "use scripts/lib/atomic_file.py". The merge gate finds this test module itself (sdlc-devtools-tests Phase 1).
- **Callers on origin/main `c9381f22`** (the check prints the exact list at dispatch; a hit that is a move, not a write, is left and the check's rule is narrowed, not given an allow list; while the hold stands, the check skips only the held paths, in one list that names the hold, and the phase that follows the answer removes the list): accounts (accounts, new_account, next_moves, outage_alerts, refusal_watch, refusals, role_map, unit_watchdog, worker_names); agents (codex_mesh, mesh_runs); astra (astra_roles, design_roles); bevy_upgrade/upgrade_record; build_hold/build_hold; build_targets/build_targets; buildlog (ci, disk, launches, mac_idle, record, rust_release, sync); claude_to_codex/sync; direct (assign, board_json, final_pass, findings, phase_table, progress_history, review_pool, step_record, worker_state, workers); feature_log/inventory; hana_shot/hana_shot; herdr (detection_rules, herdr); hierarchies/worker_owners; hooks (banned_words_lib, codex_hooks, conversation_pause); issue_score (apply_labels, client, score_issues); lib (relaunch: held: user decision pending; scratch, unit_runs, urgency); lint (memory_admit, sweep, tmp_sweep); mac_test (copy_watch, mac_test, offload); message (escalate, install_codex_agents, postmaster, rca, send); model_study (compare, model_study, phases, report); nix_env/heal; notify (auto_silence, review, silence); prioritize/renumber; production (affected_modules, autocompact, ci_points, codex_winddown, log_backup, merge_gate, proposal_issue, review_pause, review_regime, stall_watch); review_links/review_link; shot_report (changes, episodes, shot_report, transcripts); shutdown (launch_permission, pane_record, restart, settle, shutdown_record: held: user decision pending); statusline/counts_row; style (bulk_rename, coverage_report, style_admin, style_plan); usage/waste_watch; whoami (account, agent_notes, codex_pacer, five_hour, quota_alert, usage_store).

**Files:**
- `scripts/lib/test_one_file_writer.py` — new: the copy check
- `scripts/lib/atomic_file.py`, `scripts/lib/test_atomic_file.py` — only for `write_bytes_atomic`, if needed
- each caller module in the Spec list, and its own test file where a lock moves to a sibling file
- `scripts/direct/test_workers.py` — the rerun test below (the `scripts/shutdown/test_launch_permission.py` rerun test is held: user decision pending)

**Workers:** `2 writers` — split by folder; nothing is a hub file.
- `impl` — accounts, agents, astra, bevy_upgrade, build_hold, build_targets, buildlog, claude_to_codex, direct, feature_log, hana_shot, herdr, hierarchies, hooks, issue_score
- `test` (opens as impl) — `scripts/lib/test_one_file_writer.py` first, then lib, lint, mac_test, message, model_study, nix_env, notify, prioritize, production, review_links, shot_report, shutdown, statusline, style, usage, whoami

**State:** changes how every listed record, claim, roster and marker is written, not what it holds; a lock moves from a data file to its sibling `<name>.lock`.

**Constraints from prior phases:** none.

**Acceptance gate:**
- `python3 -m unittest scripts.lib.test_one_file_writer scripts.lib.test_atomic_file` green: the check finds no copy.
- A rerun test that fails on the tree before the fix: a `workers.py` roster rewrite stopped after its first byte (a fault in the write) keeps every worker's record, and the next run reads them. The `launch_permission.py` claim test (the next `pending_launches()` reads the old claim) is held: user decision pending.
- A lock test for one moved lock: two processes rewrite one roster under the sibling lock, and no write is lost.
- basedpyright prints `0 errors, 0 warnings, 0 notes` on the changed files and every caller module.
- The full merge gate is green.
- The checkpoint notice names each owner unit whose files the phase touches; natedev agreed the `build_hold.py` and `merge_gate.py` edits before the edit.

### Phase 2 — Every linked worktree removal goes through one function  · status: todo
**Depends on:** none

Eight paths remove a linked worktree, each its own way, and two delete the folder before git forgets it (sweep `remove_run_scratch.py:118`, `folder_audit.py:256`), so a rerun cannot prune and the stale entry blocks the branch. Goal: one function in `scripts/lib/scratch.py` (`git worktree remove --force`, then `git worktree prune` on the source when the folder is already gone; a rerun after a stop at either step finishes), called by `remove_run_scratch.py`, `folder_audit.py`, `retire_unit.py`, `production_lifecycle.py`, `waiting.py`, `style_sync.py`; `perform_deletion.sh` calls it through `scripts/lib/py`. A check refuses `"worktree", "remove"` and `rmtree` of a linked worktree outside it. Owners: models-unit (`scratch.py`, `remove_run_scratch.py`), resources-unit, shutdown-unit, unit-board-unit, style-unit.

### Phase 3 — Every stored pid is checked through the one owner check  · status: todo
**Depends on:** sdlc-phases Phase 17 (`scripts/lib/process_start.py` `check_owner`: ALIVE, REPLACED, GONE, UNKNOWN)

21 files outside Phase 17's Files list keep a stored-pid probe (`os.kill(pid, 0)`, `kill -0`): accounts (accounts, outage_alerts), agents/heartbeat_watch.sh, astra (astra_roles, design_roles), berth_fix/launch.sh, direct (progress_history, review_pool, review.sh, workers), hana_shot, lib/atomic_file, lint/invoke.sh, message (notifier.sh, roster), production (production_tree, promote_showrunner, showrunners, smoke_launch.sh, unit_lookup), statusline.sh. Goal: each stored pid is checked with `check_owner`; only GONE or REPLACED may reclaim; a probe of a child this process started stays as it is. `lib/atomic_file.py` `_remove_dead_temporary_files` also probes with `os.kill(pid, 0)` only: the temp name records the writer's pid and start, and the cleanup removes a temp file only on GONE or REPLACED (unit-board-unit agrees). Open sweep rows: `astra_roles.py:276`, `implement.sh:312`, `review_pool.py:144`, and `memory_admit.py:1451` (the stored owner is the shell, not its process group). A check refuses a new `os.kill(<pid>, 0)` outside `process_start.py`. Owners agree per folder; phases-unit owns the helper.

### Phase 4 — A claim that outlives its process holds its owner, and a rerun clears a dead one  · status: todo
**Depends on:** Phase 1; Phase 3

No shared claim helper exists. Goal: a claim that only lives as long as its process uses `flock` (the kernel releases it); a claim that must outlive its process goes through one new `scripts/lib/claim.py`: `take(path, purpose)` writes the owner (pid and process start from `process_start`) with the one file writer, reclaims only on `check_owner` GONE or REPLACED, `release(path)`, and a CLI `claim.py list <folder>` / `clear <path>` that refuses a live owner. Callers: `launch_permission.py` launch claims (held: user decision pending), `design_roles.py` launch claim (sweep `:753`), `settle.py` hold registration (sweep `:791`: a plain `down` gives no expiry; held: user decision pending), `mesh_runs.py` `.reap` and the `codex_mesh.py` prime claim (sweep `:8233`; only while the mesh is still live), `five_hour.py` marker, `board.sh` and `sync_codex_catalog.sh` lock dirs (to `flock`; `run_sync.sh` already uses `prioritize/runner_lock.py`, a6246f7d), `feature_log/inventory.py` claims. A check refuses a new `O_EXCL` claim or `mkdir` lock outside `claim.py`.

### Phase 5 — A file made once is published whole, and a rerun with the same content finishes  · status: todo
**Depends on:** Phase 1

A file made with exclusive create and then filled leaves a part file after a stop, and every rerun refuses it (`astra_work.py:167`, `tmp_sweep.py:1140`, `astra_review.py:184`, `accounts.py:883`, `review_pool.py:359`, `issue_score/blind.py:111`, `score_issues.py:195`, `:250`). Goal: `publish_once(path, text)` in `scripts/lib/atomic_file.py` writes a synced temp file and links it to the name (`os.link` fails if the name exists), so the name only ever holds a whole file; it shares `write_text_atomic`'s temp-file step through one private function, so there is still one writer; when the name exists, the same content is "done" and other content is a refusal that names the path. Every caller above moves onto it; the check from Phase 1 also refuses `open(..., "x")` and `O_EXCL` on a content file outside `atomic_file.py`. unit-board-unit agrees the `atomic_file.py` change.

### Phase 6 — A notice is marked sent only after it is sent  · status: todo
**Depends on:** Phase 4

`send.py --key --repeat-minutes` is the one place for "send once", but `claim()` records the key before delivery (`send.py:931-950`) and only an emergency send forgets a failed claim (`:2212-2213`). Callers keep their own "notified" marks beside it (sweep rows `five_hour.py:92`, `codex_winddown.py:233`, `main_sync.py:122`, `send.py:1389`, `stall_watch.py:679`, `settle.py:1568` (held: user decision pending), `dailies_input.py:1095`; some fixed per site since). Goal: `send.py` forgets the claim on every send that did not go out, and the claim holds its sender through `claim.py`, so a stopped sender's claim does not block a rerun; each caller drops its own mark and sends with `--key`. messaging-unit owns `send.py`; agree the change first.

### Phase 7 — The unit lifecycle tools share one step runner and one doc commit  · status: todo
**Depends on:** Phase 1

**Held: user decision pending** (sdlc showrunner, 2026-10-30). The natedev proposal may delete the transfer journal and the long rename steps, so this phase waits for the answer, and the answer sets which tools share the step runner and the doc commit.

`rename.py`, `rename_unit.py` and `transfer_unit.py` each have their own journal and step runner (step names in one, step counts in two), and six places commit a tool's own doc with three different adoption rules. `transfer_unit.py:248` adopts an earlier commit only when it is HEAD, so a rerun after another merge-branch commit makes the commit again (prove with a test first). Goal: one step runner in `scripts/production/` (journal through the one file writer, steps by name, revalidate before each step, save after each) used by the three journals; one doc-commit function (adopt when the branch already holds the wanted content of the paths, else commit only those paths) used by `add_unit`, `retire_unit`, `resume_unit`, `transfer_unit`, `rename_unit`, `rename`, `production_lifecycle` and `promote_showrunner`. An old journal is read once and saved in the new form. Each tool keeps its rerun test (`test_rename_rerun.py`, `test_rename_unit_rerun.py`, `test_transfer_unit.py`). Owners: shutdown-unit and unit-board-unit; agree the design with both before the Work Order.

### Phase 8 — A record and its event are written in one order a rerun can finish  · status: todo
**Depends on:** Phase 1; sdlc-phases Phase 17 (`findings.py` `landed` uses the same pair)

Open rows: `progress_history.py:1474` (state before `phase_started`), `:5696` (wait appended before its marker), `settle.py:846` (timers stopped before the record is written; held: user decision pending), `workers.py:1681` and `worker_owners.py:506` (stored `ended` skips cleanup; models-unit's `codex_mesh.py end --cleanup-only` lands first). Goal: one recorder rule in `progress_history.py`: the event is written first with an id, the state names the last event id it applied, and a rerun that finds an event the state lacks applies it once. `findings.py` uses the same function, so there is no second copy. Owners: roles-unit, phases-unit, accounts-unit.

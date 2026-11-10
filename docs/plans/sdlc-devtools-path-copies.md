# Stored unit folder paths

The staff row owns the current unit folder. Other records must use the staff identity and session identity.

| Record | File and field | Writer | Readers |
|---|---|---|---|
| Staff location | `staff.json`: `rows[].worktree.path` | `production/staff.py:set_worktree` | `session_staff.RowResolver`, `unit_lookup`, all unit location readers |
| Pane launch | `herdr-panes/*.json`: `cwd` | `herdr/pane_launch.py:launch`, `agents/pane_record.py:write` | `pane_restore`, `stop_resume`, `staff_sources`, `unit_watchdog`, `session_staff` |
| Shutdown session and workers | shutdown records: `session.cwd`, `workers[].cwd`, `checkout.path` | `agents/inventory.py:_session_details`, `shutdown_record.py:create_session`, `update_session` | `restart`, `restart_support`, `unit_watchdog`, `rename_unit`, `unit_move` |
| Paused Codex seats | `codex_seats[].launcher_command` | Kept in `agents/inventory.py` and `shutdown_record.py` as task state (task options, review lens) | Resume replaces its first 3 arguments with the run folder, the staff folder and the resume prompt |
| Native worker membership | `staff.json`: fixed worker id, parent, and current session | `production/staff.py` and worker admission | Native worker and display consumers |
| Worker dispatch | Recorder pass and history records | `direct/progress_history.py` | `direct/workers.py`, native completion, and usage readers |
| Worker review context | Recorder dispatch and prompt records | `direct/assign.py`, `direct/progress_history.py` | Native worker assignment and completion |
| Progress state and history | `progress_history_state.json` and history events | `direct/progress_history.py` | Run, phase, worker, cleanup, and usage readers |
| Run folder marker | run folder: `working_dir` | direct launch/progress paths, `direct/workers.py` relocation | `retire_unit`, `unit_move`, run discovery |
| Open run pointer | The director's staff row stores `run` | `staff.set_run` | Run discovery and `staff.clear_run` |
| Detached command record | `detached_runs/*.json`: command folder and arguments | `agents/detached_runs.py` | Supervisor, delivery, duplicate detection, and lifecycle consumers |
| Dev-shell record, removed | No current mesh dev-shell record | No writer | Native pane launch owns its environment |
| Rename journal, deleted | No new record | Removed from `production/rename_unit.py` | Staff supplies the folder |
| Transfer journal, deleted | No new record | Removed from `production/transfer_unit.py` | Staff supplies the unit and parent |
| Production rename unit moves, deleted | No folder fields in new or rewritten journals | `production/rename.py:unit` | Recovery takes the current folder from staff |
| Folder move journal, deleted | No new record | Removed in Phase 4v | The move reads the current staff worktree |
| Units table | production document: `Worktree` | `add_unit`, `resume_unit`, `rename_unit`, `unit_move`, `production_units` | compatibility readers and display; location readers already use staff |
| Unit start facts | `.unit.toml`: `retired.folder` | `production/unit_config.py` | `resume_unit`, `retire_unit`, `production_lifecycle`, `staff_sources`; readers now use staff |
| Retirement note | Units Plan cell: `folder` in the retired note | `production/retire_unit.py:retired_note` | former retirement recovery readers; these now use staff |
| Showrunner promotion journal | promotion journal: `worktree` | `production/promote_showrunner.py` | promotion recovery |
| Showrunner launch receipt | Legacy `add_showrunner.json`: `cwd` | Removed from `production/add_showrunner.py` | Launch recovery uses the owner and session identities |
| Checkout move journal, deleted | No new record | Removed from checkout rename and shutdown storage | Restart reads the current session location |
| Relaunch request | Legacy relaunch JSON: `cwd` | Removed from `agents/restart.py` | `lib/relaunch.py` attaches to herdr without a folder change |
| Native Claude records | Claude session files and transcripts: `cwd`, project directory | Claude and session hooks | `message/sessions.py`, transcript readers; transcripts are found by session id |
| Claude folder trust | account config: `projects` keys | Claude, `unit_move.copy_trust`, rename commands | Claude folder permission checks |
| Native Codex records | Codex rollout/thread metadata: `cwd` | Codex server | Codex server and daemon reader; threads are found by thread id |
| Build and admission records | queue, lint and build log records: `cwd`, `folder`, `worktree` | `lint/memory_admit.py`, `build_queue/admission_service.py`, `buildlog/record.py`, `mac_test/mac_test.py` | build execution, accounting and reports; these describe commands and historical builds |
| Held design context | `design.json`: repository, document, binding, and fixed role identities | `astra/design_roles.py` | Held role launch, binding checks, and lifecycle operations |

## Ownership

1. Phase 4w owns `direct/progress_history.py` and every record below without another named owner.
2. Native worker membership uses staff. Dispatch context uses the recorder; detached commands retain their own command folders.
3. `direct/workers.py`, `production/worker_admission.py`, `astra/design_roles.py`, and `agents/detached_runs.py` own their current readers.
4. Herdr 4r owns `herdr/pane_launch.py` and its launch records.
5. Phase 4v has merged. Phase 4w removes its remaining folder copies.
6. The tester owns every test and fixture file.

## Current changes

Shutdown records omit unit `cwd` and checkout `path`. Their parser joins session ids to staff and supplies runtime paths.

Retirement records omit `retired.folder` and the note's `folder`. Resume finds transcripts by session id and uses the staff worktree.

Rename and transfer no longer write journals. Rename changes the staff name, moves the folder, renames the Git branch, and sets displays.

Transfer changes the staff parent, sends the member its new production name, and reconciles the tab. It moves no Units row.

The changed unit table writers put `—` in the Worktree cell. Run readers join the director's session id to staff.

Staff fill reads the folder from staff. It checks the Git root and branch there.

Unit lookup ignores the Plan cell when it selects a folder. An absolute plan path cannot select another checkout.

Unit progress state and events omit `working_dir` and `worktree`. Progress readers derive runtime paths from the director's staff row.

Standalone command records retain their command folders. These folders do not select a unit's current location.

Claude transcripts stay in their original folders. Readers find transcripts by session id.

Checkout rename no longer rewrites pane records. The pane record owner supplies runtime folder resolution.

## Remaining dependencies

The mesh roster, worker ledger, and mesh dev-shell implementation are removed. Native worker consumers resolve membership through staff.

Unit moves do not relocate pane records. Pane launch records retain their launch `cwd` until herdr 4r changes its reader.

Unit move readers find runs by session identity. Pane resume readers take unit folders from staff.

Production rename omits per-unit folder fields. Recovery uses the staff folder after a later folder move.

Showrunner promotion changes a unit's kind and clears its worktree. Its journal retains the former unit folder for recovery.

The lifecycle rules keep member kinds fixed. That promotion path needs a separate source change before its recovery copy can disappear.

Native trust maps and historical command locations do not select a unit's current folder.

# Rerun sweep of ~/.claude/scripts

> **Plan:** `docs/plans/sdlc-phases.md` Phase 16 · **Tip:** sdlc `097b1a0d` · **Checked:** 2026-10-29 19:59 EDT

Each row is one place where a stop or a failure leaves state that a rerun does not repair. Two workers read the 245 script files that `rerun_lens.py arm` marks armed on the whole file, with the five questions of the `rerun` review lens. The unit director checked each of the 124 rows against the tip: 122 hold, one moved to its correct line, and one was dropped because a later watcher pass repairs it. The sweep fixes nothing; the showrunner routes each row to its owner.

The questions (column Q), from `docs/direct/dual_review.md`:

1. If the command stops at any line and runs again, does it finish?
2. Does it trust a stored status without checking the live thing: pid and process start time, path, or lock holder?
3. Does a failure move or delete what a rerun needs?
4. Does one failure stop other, unrelated steps, such as `set -e` over a list?
5. Does each mark have an expiry or an owner check, and a command that clears it?

The owner is the unit in the file's last checkpoint subject. "unknown" means no checkpoint commit names the file; the showrunner picks its owner.

| Owner | Rows |
|---|---|
|  |  |
| accounts-unit | 10 |
| builds-resources-unit | 2 |
| builds-unit | 4 |
| efficiency-unit | 3 |
| enh-showrunner-pause-unit | 1 |
| enh-showrunner-unit | 3 |
| feature-log-completed-date-unit | 2 |
| fix-rounds-unit | 3 |
| guild-process-unit | 6 |
| herdr-unit | 2 |
| hierarchies-unit | 5 |
| message-clarity-unit | 3 |
| messaging-unit | 5 |
| models-unit | 4 |
| notifier-unit | 2 |
| phases-unit | 4 |
| report-unit | 1 |
| resources-unit | 5 |
| roles-unit | 14 |
| screenshot-unit | 1 |
| shutdown-unit | 9 |
| style-unit | 8 |
| two-accounts-unit | 4 |
| typesafe-unit | 1 |
| unit-board-unit | 13 |
| unknown | 8 |
| **Total** | **123** |

## accounts-unit (10)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/shutdown/settle.py:786` | 1 | Stop after registering the account hold and before line 848 completes registration. A normal `down` rerun creates a new request id and clears only that id; the old pending id makes `release_finished_account_hold` keep blocking launches after the work finishes. | accounts-unit (`checkpoint(natedev-accounts)`) |
| `scripts/shutdown/settle.py:833` | 3 | Stop after a notifier timer is stopped inside `save_timers`, before `update_session` writes the changed record. The saved record still has `timers=[]`; `begin` skips it as already in progress on rerun, so cancel or restart cannot restore the stopped timer. | accounts-unit (`checkpoint(natedev-accounts)`) |
| `scripts/shutdown/settle.py:1568` | 1 | Stop after saving `showrunner_told` but before the send at line 1590, or let the send fail. A repeated stop or outcome report treats the mark as delivered and skips that message. | accounts-unit (`checkpoint(natedev-accounts)`) |
| `scripts/shutdown/settle.py:1217` | 2 | A session is resumed outside shutdown after its successful stop record is kept. A new `down` skips the existing record at line 819, and `now` skips its stored stopped status without checking the live session, so the session stays running. | accounts-unit (`checkpoint(natedev-accounts)`) |
| `scripts/accounts/accounts.py:843` | 1 | Stop after creating the account config and before its JSON write finishes. A setup rerun accepts the empty or partial file at lines 837-838 and never seeds the config. | accounts-unit (`checkpoint(build-followups-accounts)`) |
| `scripts/accounts/role_map.py:531` | 1 | Stop an automatic move after saving the selected group but before appending its history at line 536. The next watcher pass finds no consumed entry and blocks the same queue head as target-is-current at `refusal_watch.py:1256`, so later entries cannot run. | accounts-unit (`checkpoint(build-followups-accounts)`) |
| `scripts/accounts/refusal_watch.py:949` | 1 | Stop after appending a five-hour wait and before saving its watcher state. The next pass sees the journal row and skips setting `waited`; after reset it cannot take the continuation path at line 923, so an ungrouped session can move accounts instead of continuing where it waited. | accounts-unit (`checkpoint(natedev-accounts)`) |
| `scripts/whoami/quota_alert.py:795` | 1 | Stop after switching assignments to Claude but before state_file saves the fallback. A retry finds no assignments on Codex and returns NoWorkOnCodex, so it never records which assignments must move back when Codex recovers. | accounts-unit (`checkpoint(build-followups-accounts)`) |
| `scripts/whoami/quota_alert.py:901` | 3 | Let an assignment restore fail at line 913 and let the pass finish. The saved fallback is already cleared, so later switch_back calls return without retrying the assignments left on Claude. | accounts-unit (`checkpoint(build-followups-accounts)`) |
| `scripts/whoami/quota_alert.py:1276` | 2 | Stop after appending the automatic workers move request but before accounts.swap runs. On rerun recorded_auto_entries treats that unfailed request as consumed, and lines 1192-1197 remove the queue entry without moving workers. | accounts-unit (`checkpoint(build-followups-accounts)`) |

## builds-resources-unit (2)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/production/merge_gate.py:1124` | 1 | Let the checkpoint push fail after the merge and tests succeed, or stop just before the push. A rerun rejects the local merge commit in check_local_doc_commits at line 1089, so it cannot finish pushing that checkpoint. | builds-resources-unit (`checkpoint(builds-resources)`) |
| `scripts/production/merge_gate.py:1000` | 1 | Stop promotion after its local fast-forward but before the push, or let the push fail. A rerun refuses main HEAD differs from origin/main at lines 993-994 instead of pushing the already advanced main. | builds-resources-unit (`checkpoint(builds-resources)`) |

## builds-unit (4)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/hooks/codex_hooks.py:237` | 1 | Stop an install after it replaces hooks.json to remove a retired handler, before it restores the trust keys of custom handlers that moved. The next install returns no saved trust identities at line 216, so those custom hooks stay untrusted while only the built-in HOOKS are trusted again. | builds-unit (`checkpoint(build-followups-builds)`) |
| `scripts/lib/main_sync.py:122` | 1 | Stop after saving the blocked-state marker but before notifying, or let the send at line 129 fail. Later syncs with the same blockage return false at lines 120-121 and never retry the missing warning. | builds-unit (`checkpoint(build-followups-builds)`) |
| `scripts/build_hold/build_hold.py:1173` | 1 | Stop after the last holder file is removed and before release notices finish. Rerunning release cannot find the holder; `release --resume` also requires a holder file at line 1365 and refuses at line 1376, so it cannot finish the recorded release. | builds-unit (checkpoint(build-followups-builds)) |
| `scripts/buildlog/record.py:315` | 3 | Let saving a failed step log at line 280 fail, then rerun the recorder after storage recovers. The `finally` block has deleted the source log and peak file, so the rerun cannot save the failed output or recover its memory readings. | builds-unit (checkpoint(build-followups-builds)) |

## efficiency-unit (3)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/berth_fix/launch.sh:107` | 1 | Stop after the new session ID is saved, before Claude starts (for example, the environment check at line 213 fails). The rerun selects `-r` at line 102 for a conversation that was never created, instead of starting it with `--session-id`. | efficiency-unit (checkpoint(build-followups-efficiency)) |
| `scripts/berth_fix/launch.sh:116` | 1 | Stop after the worktree move and before the project-history move at line 118. The rerun returns at line 125 because the new worktree exists; it never moves the old history, so the saved conversation cannot resume from the new project path. | efficiency-unit (checkpoint(build-followups-efficiency)) |
| `scripts/nix_env/heal.py:472` | 1 | Stop after saving the pending stop, before invoking shutdown at line 475, with no earlier matching stop record. Every later sweep waits for shutdown completion at line 365 and never sends the missing stop request; this state has no timeout or clear command. | efficiency-unit (checkpoint(build-followups-efficiency)) |

## enh-showrunner-pause-unit (1)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/validate_and_push/ci_tick.sh:26` | 4 | Let gh run view fail for one run in the list. set -e with pipefail exits before reading later runs or printing any table; each rerun with that failed run still hides the unrelated runs. | enh-showrunner-pause-unit (`checkpoint(build-followups-enh-showrunner-pause)`) |

## enh-showrunner-unit (3)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/lib/zone.py:307` | 1 | Stop set after writing the new zone but before committing it at line 308. Repeating set with that zone skips the commit at lines 301-303; it pushes and pulls the old commit while announcing the uncommitted zone as shared. | enh-showrunner-unit (`checkpoint(build-followups-enh-showrunner)`) |
| `scripts/nightly_review/candidates.py:263` | 1 | Interrupt the ledger append at line 269 after a candidate's full heading but before its final state is written. A rerun treats that prefix as already recorded, while the open-candidate reader rejects the incomplete line, so the proposal never enters the queue. | enh-showrunner-unit (checkpoint(build-followups-enh-showrunner)) |
| `scripts/settings/ensure_git_filters.sh:49` | 1 | Stop after saving the clean and smudge commands, before setting `required=true` at line 53. Every rerun skips the driver because the two commands match, leaving filter failures allowed to pass through without the required setting. | enh-showrunner-unit (checkpoint(build-followups-enh-showrunner)) |

## feature-log-completed-date-unit (2)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/feature_log/feature_log.py:1865` | 1 | For a note with an earlier amend event, stop after saving a later summary or behavior amendment but before appending its event. The same amend on a rerun reports unchanged; line 1848 accepts any older amend event, so the later change never reaches the digest. | feature-log-completed-date-unit (checkpoint(build-followups-feature-log-completed-date)) |
| `scripts/feature_log/feature_log.py:2244` | 3 | Stop after advancing the digest cursor but before printing the selected changes. A rerun starts after those events and reports nothing new, so the unseen digest is skipped. | feature-log-completed-date-unit (checkpoint(build-followups-feature-log-completed-date)) |

## fix-rounds-unit (3)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/claude_to_codex/run_sync.sh:43` | 1 | Stop after creating the lock directory and before installing the exit trap, or lose the process without running the trap. Every rerun trusts the directory, marks work pending and exits without syncing; it records no owner or expiry to recover the lock. | fix-rounds-unit (checkpoint(sdlc-fix-rounds)) |
| `scripts/direct/end_session.sh:87` | 4 | Let unbinding one run fail, for example because its held role refuses the change. Each rerun exits at that run before it cleans workers, servers or scratch for any other run in the list. | fix-rounds-unit (checkpoint(sdlc-fix-rounds)) |
| `scripts/new_rust_project/rust_generate.sh:182` | 1 | Stop after standalone project generation, before dependency setup or the initial commit finishes. The same command refuses the existing destination on a rerun; the member path has the same guard at line 99 and also cannot finish its remaining setup. | fix-rounds-unit (checkpoint(sdlc-fix-rounds)) |

## guild-process-unit (6)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/astra/astra_roles.py:276` | 2 | If an exec reviewer dies before its role is released and its pid is reused, rerunning `list` reports the dead reviewer as busy. The saved hold and liveness test carry no process start time, so an unrelated process keeps the old role visible as live. | guild-process-unit (checkpoint(sdlc-guild-process)) |
| `scripts/guilds/astra_work.py:167` | 1 | Stop after exclusive creation of the brief, before its full text is written. The same `brief` command refuses the existing path on every rerun, leaving an empty or partial assignment instead of completing it. | guild-process-unit (checkpoint(sdlc-guild-process)) |
| `scripts/guilds/roles.py:744` | 5 | Stop after saving the opening claim, before launching its thread. A rerun of `open` returns the claimed role without launching it; `recover` needs a thread ID that does not exist, and `end` refuses the claim. | guild-process-unit (checkpoint(sdlc-guild-process)) |
| `scripts/guilds/roles.py:918` | 1 | Stop after saving the new binding generation, before clearing `transition.json`. The same `rebind` command now fails its generation check, and the retained transition prevents role resumes. | guild-process-unit (checkpoint(sdlc-guild-process)) |
| `scripts/guilds/roles.py:1000` | 1 | With the other role still open, stop after saving `RoleEnded`, before clearing the transition and releasing its census hold. A rerun of `end` returns early, leaving the transition to block the other role's resume and the hold unreleased. | guild-process-unit (checkpoint(sdlc-guild-process)) |
| `scripts/guilds/roles.py:1427` | 1 | During a holder move, stop after saving the new holder and generation, before clearing the transition and updating launch routes. A rerun skips the design because it already names the destination, leaving role resumes blocked and the old launch context in place. | guild-process-unit (checkpoint(sdlc-guild-process)) |

## herdr-unit (2)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/herdr/detection_rules.py:131` | 1 | Stop after replacing the override but before reloading all live sessions, or let a reload fail. The next refresh returns at lines 124-125 because the file already matches, so those sessions keep the old rules. | herdr-unit (`checkpoint(build-followups-herdr)`) |
| `scripts/review_links/serve.py:62` | 4 | Let deleting one expired snapshot fail with a permission error. Every server restart repeats that failure in its initial sweep before binding the socket, so one bad folder prevents serving all unrelated review links. | herdr-unit (checkpoint(natedev-herdr)) |

## hierarchies-unit (5)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/production/change_review.py:397` | 3 | Interrupt the in-place list write after truncation or a partial write. A repeated edit reads the damaged list and either refuses its missing table or keeps only the surviving rows; it does not restore the lost reviews. | hierarchies-unit (`checkpoint(build-followups-hierarchies)`) |
| `scripts/production/change_review.py:1206` | 1 | Stop verdict after record_verdict moves the row to Reviewed but before end_row_scratch runs, or let that cleanup fail. The same verdict rerun refuses the row because it is no longer in To review, so its scratch folders and holds stay behind. | hierarchies-unit (`checkpoint(build-followups-hierarchies)`) |
| `scripts/production/update_registration.py:584` | 3 | Interrupt an open-question list rewrite after truncation or a partial JSON write. ask and mark-important then fail to read the saved list, and questions cannot recover the unit questions that were kept only in it. | hierarchies-unit (`checkpoint(build-followups-hierarchies)`) |
| `scripts/production/update_registration.py:613` | 3 | Stop answer after removing the question but before printing its relay recipient. A repeated answer finds no matching open item and cannot recover who must receive the answer from that list. | hierarchies-unit (`checkpoint(build-followups-hierarchies)`) |
| `scripts/hierarchies/worker_owners.py:449` | 2 | Let mesh end save `ended` and then fail its server cleanup. Rerunning worker-owner end omits that roster entry; if another source still reports it, the end check at line 553 also skips it, leaving cleanup unfinished. | hierarchies-unit (checkpoint(build-followups-hierarchies)) |

## message-clarity-unit (3)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/accounts/worker_names.py:76` | 3 | Interrupt an in-place journal conversion after a partial write, or let that write fail after writing some bytes. New rows overwrite old bytes with different lengths; a rerun retains malformed lines at lines 63-67 and cannot recover the lost move or role history. | message-clarity-unit (`checkpoint(build-followups-message-clarity)`) |
| `scripts/hooks/pre-tool-use-push-gate.py:146` | 3 | Stop after the gate deletes the failed-push marker but before the permitted push runs. Retrying that push finds no marker at lines 135-136 and denies it, although the reviewed message has not been sent. | message-clarity-unit (`checkpoint(build-followups-message-clarity)`) |
| `scripts/feature_log/inventory.py:564` | 5 | A worker can stop after claiming undisposed records. Later `next` calls for another worker skip those records indefinitely; claims have no expiry or live-owner check, and the CLI has no release or reassignment command. | message-clarity-unit (checkpoint(build-followups-message-clarity)) |

## messaging-unit (5)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/message/escalate.py:399` | 3 | Stop close after deleting the hold but before cancelling its emergency push, or let cancellation fail. A repeated close returns false because the hold is gone, so it never retries cancellation and the push can keep repeating. | messaging-unit (`checkpoint(build-followups-messaging)`) |
| `scripts/message/escalate.py:510` | 1 | Stop after saving review_held but before forwarding the review result, or let the forwarding fail. Later due runs skip every non-waiting hold at lines 495-497, so the sender never gets the request to revise the message. | messaging-unit (`checkpoint(build-followups-messaging)`) |
| `scripts/notify/silence.py:269` | 1 | Stop while extending silence after saving its new timer but before stopping the old timer at line 270. A repeated on command only stops the timer in the new record; the lost old timer later runs off without checking its owner and ends the new silence early. | messaging-unit (`checkpoint(build-followups-messaging)`) |
| `scripts/notify/silence.py:295` | 3 | Stop off after it deletes the kept-push file but before release posts all its messages, or let a post fail. A repeated off finds an empty queue, so the unsent pushes cannot be retried from the saved state. | messaging-unit (`checkpoint(build-followups-messaging)`) |
| `scripts/production/stall_watch.py:679` | 1 | Stop after saving alerted=true but before open_case at line 690, or let open_case fail. Later ticks skip prompt_alert while that mark is true, so the permission case is never opened or included in reminders. | messaging-unit (`checkpoint(natedev-messaging)`) |

## models-unit (4)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/lib/scratch.py:201` | 3 | Stop adopt after moving the source folder but before writing its task record. Repeating adopt rejects the missing source, and folders() ignores the unrecorded destination, so normal task listing and end cannot find the moved work. | models-unit (`checkpoint(build-followups-models)`) |
| `scripts/direct/board.sh:329` | 1 | Stop after the resource lock directory is created and before its metadata is saved. A rerun sees a busy lock with no pid or expiry and cannot reclaim it; an explicit release is needed before acquire can finish. | models-unit (checkpoint(build-followups-models)) |
| `scripts/direct/implement.sh:311` | 2 | Lose a follow-up launcher after its claim is saved, then let another process reuse its pid. Rerunning the follow-up refuses it as still claimed; the saved claim and liveness check have no process start time. | models-unit (checkpoint(build-followups-models)) |
| `scripts/direct/remove_run_scratch.py:118` | 3 | If git removal fails, stop after deleting the linked worktree folder and before pruning its source. The rerun finds no folder and cannot recover the source path to prune; the stale git entry can block adding that branch again. | models-unit (checkpoint(build-followups-models)) |

## notifier-unit (2)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/validate_and_push/push_pr_branch_and_merge.sh:109` | 1 | Stop after the default branch is pulled but before its post-push hook, or let the hook fail. A rerun on the now-synced default branch exits Nothing to push at lines 26-28 and never retries the hook. | notifier-unit (no checkpoint commit; owner map natedev-production.md:29, unit retired) |
| `scripts/validate_and_push/validate_ci.sh:133` | 1 | Stop after an autofix changes files but before amend_fixes commits them, or let that commit fail. The next validation rejects the dirty worktree at its entry check instead of finishing its own fix commit. | notifier-unit (no checkpoint commit; owner map natedev-production.md:29, unit retired) |

## phases-unit (4)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/direct/assign.py:363` | 2 | Lose the launcher after its claim is saved, then let another process reuse its pid. A rerun refuses the worker as still claimed because the claim check uses only the pid, with no process start time. | phases-unit (checkpoint(sdlc-phases)) |
| `scripts/direct/findings.py:1218` | 1 | Stop after saving the landed state and before appending `finding_batch_landed` to history. Rerunning `landed` says no round is in flight and never restores the event, so history readers still see an unfinished repair. | phases-unit (checkpoint(sdlc-phases)) |
| `scripts/direct/verify.sh:2065` | 2 | After a saved pass, let a forced lint rerun fail on the same tree and stop after publishing its failure record, before deleting the old pass. The next ordinary call chooses the old pass first at line 1992 and reports success despite the newer failure. | phases-unit (checkpoint(sdlc-phases)) |
| `scripts/direct/verify.sh:1157` | 3 | Let closing a recorded resource wait fail at line 1110. Cleanup still deletes the folder with its wait ID; a rerun uses a new folder and cannot retry closing that earlier wait. | phases-unit (checkpoint(sdlc-phases)) |

## report-unit (1)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/buildlog/index.py:689` | 3 | Interrupt the source-log rewrite after it writes only part of the new bytes. The rerun keeps malformed lines during migration and skips them during indexing; the original records have been overwritten and cannot be restored from this file. | report-unit (checkpoint(builds-report)) |

## resources-unit (5)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/lint/folder_audit.py:256` | 3 | Stop after deleting a linked worktree, before pruning its source at line 260. A rerun no longer finds the folder or its source link, so it cannot finish the prune and the stale worktree entry can block reuse of the branch. | resources-unit (checkpoint(builds-resources)) |
| `scripts/lint/memory_admit.py:811` | 2 | Let the owning shell die while its separately grouped child keeps running. The next admission check removes the reservation because the shell is gone, so a rerun no longer charges the child's memory against the build budget. | resources-unit (checkpoint(builds-resources)) |
| `scripts/lint/memory_admit.py:1427` | 3 | Stop the width-update helper while it rewrites a reservation, leaving incomplete JSON. Its caller ignores that failure and starts the step; the next admission check deletes the unreadable reservation, so a rerun has no charge for that live step. | resources-unit (checkpoint(builds-resources)) |
| `scripts/lint/sweep.py:2142` | 3 | Stop after truncating the removal log during retention, before the kept rows are written. The next sweep cannot recover those removal records from the log; rebuild reports lose that evidence when no journal copy exists. | resources-unit (checkpoint(builds-resources)) |
| `scripts/lint/tmp_sweep.py:1106` | 1 | Stop after exclusive creation of a cleanup plan, before its JSON is complete. Rerunning `--plan` refuses the existing path, and `--apply` cannot read it, so the same command cannot finish its plan. | resources-unit (checkpoint(builds-resources)) |

## roles-unit (14)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/agents/codex_mesh.py:1443` | 3 | Stop after truncating the worker roster and before its replacement JSON is written. A follow or reopen rerun reads an empty roster and reports no such worker; starting another worker writes a roster that has lost the other thread identities. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/agents/codex_mesh.py:2006` | 3 | Stop while adding a message after truncating the pending inbox and before writing its JSON. A rerun treats the broken inbox as empty and saves only the new message, so previously accepted messages are lost. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/agents/codex_mesh.py:8169` | 5 | Let prime fail after creating the held role's creation claim but before recording a thread. Repeating prime fails because the claim file exists; roles open keeps the claimed state, and roles recover has no thread id to resume or clear. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/agents/mesh_runs.py:258` | 5 | Kill a reaper after it creates the `.reap` claim and before it saves the finished result. Every later reap skips the existing claim at lines 259-260; it has no owner, expiry or clear command, so the worker keeps waiting for that result. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/message/send.py:912` | 2 | Stop after saving the emergency send attempt but before posting the push. A rerun inside its lifetime calls still_live at line 909 without checking delivery and skips a push that never went out; explicit retire or reopen is needed to retry at once. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/message/send.py:989` | 3 | Stop pending after it deletes the queue files but before main prints their messages. A repeated pending reads no queued messages, so the recipient cannot recover the undelivered text. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/message/send.py:1389` | 1 | Stop after writing the daily template-fix stamp but before starting its delivery child, or let that start fail. Every same-day retry returns at lines 1387-1388, so the template owner gets no notice. | roles-unit (`checkpoint(sdlc-roles)`) |
| `scripts/direct/review_pool.py:358` | 2 | Stop after creating the prime prompt file but before its content is fully written. The rerun trusts that path's existence, skips the write, and primes the reviewer with an empty or partial prompt under the full prompt's hash. | roles-unit (checkpoint(sdlc-roles)) |
| `scripts/direct/review_pool.py:144` | 2 | Let a reviewer owner die without releasing its saved claim, then let another process reuse that pid. A rerun treats the reviewer as busy using only `kill(pid, 0)`; retire leaves it in place and allocate opens another reviewer instead of recovering it. | roles-unit (checkpoint(sdlc-roles)) |
| `scripts/direct/review_pool.py:439` | 3 | Let mesh end return failure while server cleanup remains unfinished. Retire still deletes the pool entry; rerunning retire no longer selects that reviewer and cannot retry its unfinished cleanup. | roles-unit (checkpoint(sdlc-roles)) |
| `scripts/direct/progress_history.py:1480` | 1 | Stop after saving the open phase and before appending `phase_started` at line 1492. Rerunning `start-phase` returns because the phase is already open; the rounds reader requires the missing start event and omits this phase from its report. | roles-unit (checkpoint(sdlc-roles)) |
| `scripts/direct/progress_history.py:5634` | 1 | Stop after appending the turn's wait at line 5633 and before saving its marker. A rerun appends another wait; the next reply closes only the wait named by the new marker, leaving the first wait open. | roles-unit (checkpoint(sdlc-roles)) |
| `scripts/direct/workers.py:553` | 3 | Stop while rewriting a roster, after truncation leaves only part of the JSON. Adoption recovery reads that damaged roster before it can apply its saved intent, so a rerun fails to parse it and cannot restore the other workers' records. | roles-unit (checkpoint(sdlc-roles)) |
| `scripts/direct/workers.py:1604` | 2 | Let mesh end save `ended` and then fail its server cleanup. Rerunning unit retirement skips mesh end because of that saved status, then removes the unit ledger while the earlier cleanup remains unfinished. | roles-unit (checkpoint(sdlc-roles)) |

## screenshot-unit (1)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/hana_shot/hana_shot.py:2078` | 3 | Stop while writing the launch record, leaving incomplete JSON after the app starts. A new launch refuses the occupied port; `shutdown` parses the broken record before contacting the app and cannot finish cleanup. | screenshot-unit (checkpoint(build-followups-screenshot)) |

## shutdown-unit (9)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/production/merge_checkpoint.py:1083` | 1 | Stop after merging a checkpoint but before recording its Change-Review rows. On rerun already=true empties commits; if pushed it also skips record_changes, so the missing review rows are not restored. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/merge_checkpoint.py:1328` | 1 | Stop a child hand-off after the merge at line 1317 but before its tests finish. A rerun classifies it as MergedNotPushed, skips checkpoint_tests and pushes the untested merge. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/merge_checkpoint.py:1305` | 1 | Stop a child hand-off after pushing but before promotion, or let promotion fail. Its rerun takes MergedAndPushed and returns after receipts and acknowledgment, so it never retries the configured promotion. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/rename.py:481` | 1 | Stop apply after moving the update timer but before saving _rename_timer as complete. On rerun _current_production chooses the old timer from the journal at line 298 and refuses because it has moved, so the rename cannot resume. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/rename.py:564` | 1 | For a production whose merge branch keeps its name, stop after the rename commit but before saving that step, or let its push fail. The rerun rejects the changed merge-branch tip at lines 541-542 before it can recognize its own commit and retry the push. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/rename_unit.py:484` | 1 | Stop after writing the changed production doc or plan but before committing it. A rerun calls _check_checkout at line 1184, rejects those tracked edits as dirty and never reaches _commit_doc to finish its own changes. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/rename_unit.py:828` | 1 | Stop after restarting the renamed unit but before saving the relaunch step. The rerun still treats relaunch as a stopped-only step and _check_stopped rejects the now-live Claude, so it cannot reach verify or finish the journal. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/retire_unit.py:686` | 1 | Stop after writing the retired note but before committing it at line 687. A rerun sees unit.retired and skips mark_retired at line 770, so it can remove the worktree and branch while leaving the retirement doc change uncommitted. | shutdown-unit (`checkpoint(natedev-shutdown)`) |
| `scripts/production/transfer_unit.py:363` | 1 | Stop after writing the receiving Units row but before committing it, or after writing the giving doc at line 380. The same transfer resumes the step but _check_doc rejects its own uncommitted doc, so it cannot finish moving the row. | shutdown-unit (`checkpoint(natedev-shutdown)`) |

## style-unit (8)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh:49` | 2 | Stop the guide splitter after its first section write (`bevy_migration_split_guide.py:142`). The rerun sees at least one Markdown file and skips the splitter, reporting an incomplete guide set as ready. | style-unit (checkpoint(build-followups-style)) |
| `scripts/style/bulk_rename.py:577` | 1 | Lose the process after it writes an edit, before validation and the review record finish, without running exception cleanup. The rerun rejects the dirty tracked tree at line 659 and cannot finish that batch's review record. | style-unit (checkpoint(build-followups-style)) |
| `scripts/style/bulk_run.py:420` | 1 | Lose the process after it writes candidate edits, before validation or rollback. The same workspace run rejects the dirty tree at line 59; its saved proposal tickets remain open, so it cannot resume the interrupted batch. | style-unit (checkpoint(natedev-style)) |
| `scripts/style/coverage_report.py:250` | 3 | Stop after saving the report snapshot, before printing the report at line 269. The rerun uses that unseen snapshot as its baseline, so the changes in the lost report disappear from the next report's change counts. | style-unit (checkpoint(build-followups-style)) |
| `scripts/style/style_admin.py:203` | 1 | Stop after renaming the style file, before history and links are updated. The same rename command cannot find the old source on a rerun, so it never finishes those references. | style-unit (checkpoint(build-followups-style)) |
| `scripts/style/style_admin.py:221` | 1 | Stop after deleting the style file, before history and body links are cleaned. Rerunning delete refuses the missing source and leaves the remaining references unchanged. | style-unit (checkpoint(build-followups-style)) |
| `scripts/style/style_admin.py:120` | 3 | Stop while rewriting a history file after it has been truncated. The old rows are gone; even restoring the source style file and rerunning cannot recover those review records. | style-unit (checkpoint(build-followups-style)) |
| `scripts/worktree_delete/perform_deletion.sh:75` | 1 | Let the remote deletion at line 86 fail after the local branch was deleted. Even after a fresh confirmation, the rerun exits on the missing local branch here and never retries the remote deletion. | style-unit (checkpoint(build-followups-style)) |

## two-accounts-unit (4)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/production/codex_winddown.py:233` | 1 | Stop count after creating the quiet marker but before sending the reset alert. Later counts with no running agents return at lines 234-235, so no alert is sent until start, clear or a new running agent removes the marker. | two-accounts-unit (`checkpoint(build-followups-two-accounts)`) |
| `scripts/production/codex_winddown.py:217` | 3 | Stop refresh after saving the removed finished-unit entry but before sending AT_ZERO at line 222, or let that send fail. Later counts no longer find that unit in saved projections and never retry its permission-to-continue notice. | two-accounts-unit (`checkpoint(build-followups-two-accounts)`) |
| `scripts/whoami/five_hour.py:92` | 1 | Stop after creating the low-window notice marker but before relaying the Claude warning, or let a relay fail. Later passes skip the existing marker at lines 94-95 until usage recovers, so they never retry the missing warning. | two-accounts-unit (`checkpoint(build-followups-two-accounts)`) |
| `scripts/whoami/five_hour.py:44` | 3 | Let a retire subprocess fail while cancelling an outgoing account warning. The code still deletes its notice file; later passes no longer find that account to retry cancellation, so its queued warning can remain. | two-accounts-unit (`checkpoint(build-followups-two-accounts)`) |

## typesafe-unit (1)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/issue_score/client.py:202` | 3 | Stop while rewriting pending spend reservations, leaving incomplete JSON. Every later request and reservation release fails to read that file, and the previous reservation amounts have been overwritten. | typesafe-unit (checkpoint(build-followups-typesafe)) |

## unit-board-unit (13)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/production/add_unit.py:752` | 1 | Interrupt the brief stub write after the file is created but before all its source text is written. A rerun rejects the partial stub at lines 642-649 as different content, instead of completing the requested unit launch. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/add_unit.py:773` | 3 | Interrupt the in-place production-doc write after truncation or a partial write. The next launch reads the damaged Units table during preflight and cannot restore its missing rows from the command arguments. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/add_unit.py:1343` | 1 | Stop a new-unit launch after herdr starts the marked Claude process but before herdr submits its opening prompt. A rerun finds the marked unit and skips launch_session, so it can report started after Remote Control is ready while the unit has received no work. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/dailies_input.py:1095` | 1 | Stop after saving requested=true but before printing the ETA request at line 1097. A repeated input run sees EtaRequested and emits no request, so the unit is never asked for the missing ETA. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/dailies_render.py:1424` | 3 | Interrupt save_state after its in-place truncation or a partial JSON write. The next report fails in load_state at line 1330 before it can save a new state, and the first-ETA history has been lost. | unit-board-unit (`checkpoint(build-followups-unit-board)`) |
| `scripts/production/production_lifecycle.py:507` | 3 | Stop wrap after git mv archives the production doc but before committing or pushing it. The same command still names the old doc; read_production resolves that missing path strictly and refuses, so wrap cannot finish without changing its input. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/promote_showrunner.py:238` | 1 | Stop promotion after stop() closes the unit but before add_showrunner starts it. A rerun fails checked() at lines 128-129 because the unit has no live session; no saved promotion record lets it resume the stopped conversation. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/resume_unit.py:207` | 1 | Stop after removing the retired note but before committing the doc at line 208, or let that commit fail. A rerun refuses the now-active row at lines 138-139 and cannot finish its own commit. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/showrunners.py:259` | 1 | Stop copy_session_state during copytree or copy2 after the destination exists. Every later adoption skips that destination at line 256, so it keeps the incomplete state copy instead of finishing it. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/showrunners.py:533` | 1 | Stop attach after saving the parent but before tell_others sends the new routing rules. Repeating attach refuses the existing edge at lines 521-522, so it does not retry those notices. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/production/waiting.py:551` | 1 | Stop agenda after saving agenda_seen but before printing the new items at line 554. A rerun calls them already seen and prints none; eta-request has the same save-before-output gap at line 239. | unit-board-unit (`checkpoint(natedev-unit-board)`) |
| `scripts/direct/phase_reservation.py:483` | 1 | While restoring a released phase, stop after the state write and before deleting the release marker at line 485. Rerunning `start-phase` skips restore because the state exists (line 495); `open_phases` still excludes the reopened phase, so edit checks cannot attribute its files to it. | unit-board-unit (checkpoint(natedev-unit-board)) |
| `scripts/style/add_style.py:236` | 1 | Stop during the first write of a production doc or unit plan, leaving a partial file. The rerun trusts its existence and skips the missing-file writer; a partial production doc can fail parsing before setup resumes. | unit-board-unit (checkpoint(natedev-unit-board)) |

## unknown (8)

| Path:line | Q | On a rerun | Owner |
|---|---|---|---|
| `scripts/release/push_release.sh:63` | 1 | Stop after creating the local tag, or let either push fail. The rerun fails at `git tag` because that tag already exists, before it can finish the missing remote push. | unknown: no checkpoint commit names it |
| `scripts/release/restore_unreleased.sh:48` | 1 | Stop after the changelog edit, before the commit or push, and rerun on the same day. The version header still matches, so the rerun inserts a second Unreleased section instead of finishing the first edit. | unknown: no checkpoint commit names it |
| `scripts/claude_to_codex/sync.py:244` | 1 | Run without `--force` and stop after the skill directory is created at line 252, before its skill file is saved. The same command skips the existing directory on a rerun, leaving the skill missing. | unknown: no checkpoint commit names it |
| `scripts/exclude/exclude.sh:56` | 1 | Stop after appending an ignore pattern at line 60 but before untracking its file at line 67, or let untracking fail. A rerun reports `ALREADY_EXCLUDED` and exits here, leaving the file tracked. | unknown: no checkpoint commit names it |
| `scripts/prioritize/renumber.py:776` | 3 | Stop after flushing a shorter issue rewrite, before truncation at line 777. Old tail bytes remain in the note; the rerun reads them as source text and preserves them, so deterministic ranking does not restore the original body. | unknown: no checkpoint commit names it |
| `scripts/prioritize/run_watcher.sh:281` | 2 | Let the rankings export fail (`renumber.py:862`) after note updates; apply still returns success and the watcher saves its success snapshot. The next watcher run checks only note ranks and returns here, leaving the export missing or stale after storage recovers. | unknown: no checkpoint commit names it |
| `scripts/prioritize/run_watcher.sh:367` | 1 | Stop after saving the successful input snapshot, before the index refresh at line 540. The rerun takes the unchanged branch and never sets its in-memory write flag, so it leaves the generated rank changes showing as modified in git status. | unknown: no checkpoint commit names it |
| `scripts/release/finalize_changelogs.sh:28` | 1 | Stop after removing the first changelog's backup at line 39, before later files finish. The same file list fails on the first completed file because its Unreleased header is gone, so a rerun never reaches the remaining changelogs. | unknown: no checkpoint commit names it |

# Accounts and machine-local settings

The account tools separate provider identity from shared configuration and conversations.
Claude groups and Codex workers select numbered accounts through one role map.
Producers use the group account for their configured family.
Both machines use that map, but each machine keeps its own sign-in state.
Account moves preserve conversations and worker threads.
Git filters keep machine-local settings outside promoted configuration.

The [account guide](../accounts.md) describes commands and repairs.
This document describes the implementation and its constraints.

## Ownership and data flow

| Module | Responsibility |
| --- | --- |
| `scripts/accounts/accounts.py` | Account checks, preparation, launch selection, moves, trust, updates, and status. |
| `scripts/accounts/role_map.py` | The shared role map, its transport, cache, locks, and change history. |
| `scripts/accounts/next_moves.py` | Ordered moves, holds, positional edits, and validation. |
| `scripts/accounts/refusal_watch.py` | Refusal detection, login renewal, queued moves, and automatic group moves. |
| `scripts/accounts/login_sittings.py` | Weekly and safety reminders from stored Claude login end dates. |
| `scripts/accounts/refusals.py` | Provider refusals and the freshness of usage readings. |
| `scripts/accounts/moves.py` | Move outcomes, waits, assignment routing, and replay evidence. |
| `scripts/accounts/unit_watchdog.py` | Recovery from pane launch records and fresh session liveness. |
| `scripts/accounts/outage_alerts.py` | Durable alert claims for retained record-driven restart paths. |
| `scripts/whoami/agent_accounts.py` | Account identity, Codex homes, worker selection, and credit policy. |
| `scripts/whoami/note_file.py` | Numbered account notes and the shared notes-folder resolver. |
| `scripts/settings/settings_local_keys.sh` | Settings classification, local snapshots, and Git filter transformations. |
| `scripts/settings/local_key_notices.py` | Deferred notices for new machine-local keys. |
| `scripts/lib/conf.py` | Validated configuration reads and recovery. |

Shared whoami modules use canonical `scripts.whoami` imports.
Loading the same module under two names creates distinct runtime types and breaks state checks across callers.

The agent-notes job runs these parts in separate processes:

1. Notes.
2. Quota alerts.
3. Pacing.
4. Refusal moves.
5. Unit recovery.
6. Login reminders.
7. Phase note pruning.
8. Vault commits.

`scripts/maintenance/agent_notes_job.py` owns that job.
`config/agent_notes.toml` supplies its schedules and process limits.
Herdr producer recovery, layout saves, and detection rules belong to herdr-upkeep, with `config/herdr_upkeep.toml`.
Neither job collects recovery snapshots or a desktop session note.

## Preventive login reminders

The `Part.LOGINS` timer part calls `login_sittings.run` in its own child process.
It runs in parallel with the other independent parts.
`parts.logins` in `config/agent_notes.toml` sets a 120-second interval and a 45-second process limit.
The argument-free `agent_notes.main` path does not call this reminder.
The Mac usage refresher does not call this reminder.
The module sends only on `login_sitting_machine`.
It reads local dates through `accounts.read_status(source=StoredUsageOnly())`.
It uses the same numbered Claude rows and `login_ends_at` values that stored status exports.
Removed accounts cannot retain a date through an old usage-store directory link.
This shared status path inspects account readiness but does not refresh usage.
The peer command is `accounts.py status --stored --json` through `ssh -o BatchMode=yes` and `remote.other_machine()`.
An attempt time limits peer reads, including failed reads.
The last successful peer read time and dates remain available after a failed attempt.

`config/accounts.conf` supplies all reminder settings:

| Key | Current value | Meaning |
| --- | --- | --- |
| `login_sitting_machine` | `natedev` | The machine that sends reminders. |
| `login_sitting_first` | `2026-11-17T10:00:00-05:00` | The first sitting. |
| `login_sitting_interval_days` | `7` | The interval between sittings. |
| `login_sitting_window_days` | `10` | The selection window after a sitting. |
| `login_safety_margin_days` | `3` | The immediate reminder margin. |
| `login_peer_read_minutes` | `60` | The minimum interval between peer attempts. |
| `login_peer_read_timeout_seconds` | `30` | The peer command limit. |

Each pass reads these settings through `conf.read`.
The earliest end on either machine selects the account for a sitting.
The sitting push includes both known machine dates for that account.
When no account is within the window, the push selects the account with the earliest end.
No known dates means no sitting push.
The latest sitting replaces any missed sittings.
Safety pushes include only machine, account, and end-date combinations that no accepted push has named.
An unknown date never enters either list.

The atomic state file is `accounts_home()/.local/state/accounts/login_sittings.json`.
It holds the latest completed sitting, named login dates, peer dates, and peer read and attempt times.
Each save removes a named record only when the same machine and account have a different known end date.
A missing date keeps the named record.
Cached peer dates remain current evidence until the next successful peer read replaces them.
The module saves each peer attempt and its result before it reads local dates.
It contains no credential or login identity.
`locked_history` holds the lock in `login_sittings/` beside the state file throughout each pass.
The lock covers state reads, local and peer reads, sends, and saves.
A busy pass performs no account reads or sends.
The module records a sitting after an accepted push or an empty selection.
A failed push leaves its sitting and named dates unrecorded for another attempt.
The `accounts.login_sitting` catalog entry supplies both push kinds as normal notes.
The `login-sitting` and `login-safety` keys replace older retained pushes of the same kind.
Delivery uses `push_texts.user_send_argv` and checks `send_result.send_outcome`.
It never sets `--through-silence`.
The push formats dates in the user's zone.

## Account identity and storage

Claude account names have the form `claudeN`.
Codex account names have the form `codexN`.
The number identifies an account note, not necessarily the suffix of its current Codex home.
Legacy spaced labels remain valid input, but output uses compact labels.

`~/.claude` and `~/.codex` are shared configuration and conversation stores.
They are not the selected account after cutover.
Numbered homes retain private sign-in and runtime state.
Claude numbered homes link shared commands, skills, settings, transcripts, memory, and the session registry to `~/.claude`.
Codex homes link shared configuration, instructions, skills, session files, and history to `~/.codex`.
Codex authentication, app-server state, caches, logs, and SQLite databases remain private.
SQLite companion files remain private with their database.

Shared transcripts and registry paths let a conversation resume under another account.
The account homes use links rather than Git worktrees, so commands and hooks use the same promoted scripts.
Account checks detect broken or replaced shared links.
Preparation repairs safe shared links before launch.
It refuses an unknown conflicting file rather than deleting private data.

`NumberedAccountLabel` validates the tool and positive account number.
`NamedAccount` carries a Claude label and directory.
`CodexWorkersAccount` carries a Codex label and resolved home.
`PreparedAccount` carries the launch environment and variables to remove.
`AccountRefused` carries the reason a launch cannot proceed.
Directory checks distinguish logged-out, wrong-account, absent, unreadable, and broken-link states.
An unreadable identity is never an absent identity.

Codex identity resolution uses the account note's identity mapping.
It handles numbered homes and a legacy `~/.codex` home through the same resolver.
Status counts use that resolver too.
Setup and account checks detect duplicate sign-ins across homes.
Cutover journals preserve interrupted identity moves and support an explicit undo.
Setup waits for Codex to answer `initialize` before it reports success.
Shared session indexing can require up to twenty minutes.
The [trust guide](../accounts.md#trusted-worktrees) describes configured project roots and guarded trust-file updates.

## Role map and launch selection

Natedev owns the role map for both machines.
The map contains the Claude groups `units` and `showrunners`, the Codex `workers` account, and an optional timed Claude worker route.
Both Claude groups may select the same account.
Codex producers use the workers account.
Claude producers use the showrunners account.
`[producers]` in `config/models.conf` selects each producer's family, model, and effort.
An absent selection stays unconfigured until an explicit move selects it.
Status, launch, and preview reads never create a selection.

| State | Location |
| --- | --- |
| Claude groups | `~/.local/state/accounts/groups.json` |
| Worker selection | `~/.claude/config/accounts.json` |
| Role change history | `~/.local/state/accounts/roles.jsonl` |
| Peer role-map cache | `~/.local/state/accounts/role_map_cache.json` |
| Ordered account moves | `~/.local/state/accounts/next_moves.json` |

All role readers and writers use `role_map.py`.
A command reuses one map observation.
Mac writes use the shared SSH transport to natedev.
The authority locks the selection and history together.
A failed transaction restores the prior selection and history length.
A failed peer write never creates an independent local choice.

`RoleMap` carries selections and provenance.
`CurrentRoleMap`, `CachedAfterFetchFailure`, and `ExplicitStoredRoleMap` distinguish fresh, degraded, and stored observations.
A failed peer read may use a valid cache, with its observation time and failure shown.
Without a usable map, the operation refuses.
No reader invents a replacement account.

`account_for_group_launch()` resolves unit and showrunner launches.
Explicit account or directory arguments must agree with that selection.
A checked unit recovery can use its source account when the selected destination cannot launch.
The recovery records that fallback reason.
Unscoped Claude launches retain a valid caller account; otherwise, they use the configured showrunner account.
An active timed Claude worker route takes precedence for a worker launch.

`GroupNotConfigured`, `UnitsResetSoonest`, `ShowrunnersMostLeft`, and `GroupNoRoom` preserve the selection result's meaning.
Readings rank eligible destinations; they do not silently replace a configured launch account.
Units prefer the earliest weekly reset, then the most weekly allowance left.
Showrunners prefer the most weekly allowance left.
Labels provide a stable final ordering.
Recorded provider refusals block a destination until the refusal resolves.
Codex selection also checks allowance and whether `codex_credit_accounts` lets the account spend credits.

`_exec_codex_account()` serves both `codex` and numbered Codex aliases.
It recognizes `update` before account admission.
The local update stays attached to the terminal and uses `accounts_home() / ".codex"`.
Version reads are captured.
The peer command directs update output to stderr and leaves only the version on stdout.
The caller discards peer stderr and rejects an empty version.
Each machine gets one summary line; success requires both updates and version reads.

The [launch environment](nix-shell-launch.md) describes shared Nix setup and account environment preparation.

## Moves, refusals, and workers

Account moves use the shared stop and resume API.
Each session attempt writes a request and one confirmed or failed outcome to `~/.local/state/accounts/moves.jsonl`.
Each attempt has its own move ID and cause.
A session proved live on the target writes nothing.
The [shutdown design](shutdown-and-restart.md) owns process identity, pane reuse, prompt delivery, and recovery details.
All selected stops finish before the batch resumes any session.
Each session resumes its own conversation on the target account.
A selected caller delegates the batch to a detached process before any signal.
The notes timer delegates its moves through the same boundary before its part deadline can kill them.
For workers, the notes job writes the role and starts the moves in their own process.
The request stays pending until the detached process ends.
A later notes pass starts an unfinished move again; workers already on the target stay unchanged.
The detached process confirms registered requests for its final target, with the target home.
It fails registered requests for other targets with `superseded by <label>`.
Replay consumes the queued entry after either completion result.
Each worker row records its own move result.
Worker refusals do not keep the group request open.
The next assign moves a refused or stopped worker to the selected account, so a lasting anomaly cannot cause endless group retries.
One `accounts-workers-move` unit serves every target.
The runner reads the role before each worker and repeats after a changed final role.
A run lock serializes worker moves, including the fallback without systemd.
Direct Codex worker moves use this runner and read the selected role before each worker.
A Codex worker move skips a target that may not spend credits and whose stored reading has no included usage left. The worker stays, its row records the reason, and the group request fails with the same reason.
Both direct preview and execution refuse a Codex target that differs from the workers role.
The refusal says `workers role is <role>; run accounts.py swap workers <codexN> to select it`.
The swap preview shares the worker preview helper and can show a new target without changing the role.
Claude worker moves use this runner with a fixed Claude label under the same run lock.
Only Codex moves read or complete pending group requests and their marker.
A short schedule lock protects startup, the pending marker, the final role check, and group completion.
Both locks use `file_lock.exclusive`.
The marker is `~/.local/state/accounts/workers_move.json`.
Startup writes the role and marker while it holds the schedule lock.
A new handoff during unit exit retains this marker for the next notes pass.
A failed start leaves the request pending; the existing blocked-entry notice reports the cause once.
Requests use each session's observed process account, including members outside their selected group account.
Blocked notices check the queue head and target again before delivery.
Quota recipients use one session address for both names and IDs.
Workers keep their threads when their Claude owner moves.
An assignment compares the process account home with the selected account home for Claude and Codex.
It uses one process environment read for this check and the run check.
An unreadable environment permits reuse only for a worker whose pass is active in the current run.
A different account follows the existing close checks, stop, and resume in the same pane.
Workers moves and previews omit workers of the other family.
The preview shares the move decision and lists idle panes that would close, workers that would resume, and refusals with each reason.
One count line covers workers with nothing to move.
These include closed workers without an active dispatch, workers without a current session, and workers already on the target.

A group move validates the destination before changing the selection or stopping members.
It selects the account for future launches and moves live members on other accounts.
A workers swap also selects eligible Codex producers.
A showrunners group move also selects eligible Claude producers.
`eligible_producers` requires active, local, unpaused staff rows with valid settings for the requested family.
`producer_settings` returns `LaunchSettings` or `InvalidProducerSettings`.
One `settings_report_scope` limits each producer's invalid-settings warning to one per operation.
A repeated move selects a stopped producer even when its home already names the target.
Only proof that the producer runs on the target permits a skip.
Unreadable live account evidence prevents the stop, start, and home change.
The [producer launch contract](shutdown-and-restart.md#producer-launch-and-admission) defines the shared move and publication checks.
An empty group can still select its account.
Each member has an independent result.
A single-session move refuses a grouped session and names the group operation.
The move path creates no ready mark, conductor, follow command, or cancellation step.

`TranscriptRefusal`, `ProofRefusal`, and `NoSessionRefusal` distinguish provider evidence from a test assumption and absence.
Proof evidence stays with its selected session.
The watcher saves the first refusal observation and suppresses repeated actions for that entry.
Known short five-hour resets can wait instead of moving.
The default wait boundary is strictly less than 20 minutes.
An unknown reset or the exact boundary does not qualify for that wait.
Failed continuation delivery retains the retry state.

Queued moves use group-local positions and stable internal identities.
Only the head of each group can run.
An unavailable target remains queued and blocks later entries.
A changed head loses its old hold and notice state.
`FixedHold` carries a duration; `HalfPointHold` requests a pace-based duration.
`ResolvedHold` keeps the duration and its source.
The half-point estimate uses the selected account's history, including other groups that share it.
The default estimate is 40 minutes when no usable pace exists.
The duration stays with the started hold rather than changing on each pass.

A successful selection and journal entry precede removal of the queued entry.
Replay checks retain a blocked entry when history is unreadable.
An incomplete final journal row is distinct from corruption between complete rows.
Queue edits and execution share locks on natedev.
The Mac forwards edits instead of maintaining a second queue.

Without a queued workers move, provider refusal or unavailable included usage can select another Codex account.
Credit policy determines whether an exhausted account can continue.
Worker assignment fallback to Claude remains separate from the selected Codex account.
Routing returns only when the selected Codex account can work and its refusal has resolved.
An owned workers refusal with no account capacity enables `no_worker_mode` on the workers-role host, `role_map.ROLE_MAP_HOST`.
The refusal handler sets this flag independently of the first `NoRoomMove` entry and the Claude assignment switch.
A zero account with credits off enables the same flag when its workers stay on it: no other account has room, or the workers move failed.
The saved flag triggers one broadcast to showrunners and unit directors on both machines.
`config/accounts.conf` supplies `no_worker_mode_on` and `no_worker_mode_off`; `quota-alert` identifies the sender.
The ON notice directs directors to write code, run tests, and do reviews without assigning workers.
Successful Codex recovery clears the flag and broadcasts OFF after the workers account and assignment restoration succeed.
Recovery also clears the flag when no Claude assignment fallback was saved.
An unfinished Claude switch or failed restoration retains the flag until recovery can finish.
The state context saves each flag change and releases the lock before it starts a separate broadcast unit.
When the flag goes on, the same step starts `scripts/production/worker_capacity.py stop` as a second transient unit.
That command stops each Codex worker of every local parent, busy or idle, under the worker's lock, and proves the stop from a new pane observation.
It ends each open pass of a stopped worker as `canceled`, also for a Codex worker whose pane is already closed.
It closes the pane of a stopped worker when the close checks permit. Claude workers are not touched.
Each affected director gets one stop report. A worker that is not proved stopped, or a pass that is not canceled, also goes to `faults_to`.
A live detached run of a stopped worker continues; the report names its result file and its log.
No command starts workers when the flag goes off; the next assign of a director starts its worker.

The credits setting of the hold owner releases a credits-off hold: when `codex_credit_accounts` names the owner again, routing can return before the hold time.
An unresolved refusal does not block the return when credits are on for the selected account and a reading taken after the refusal shows a credit balance.
This rule is local to the return; `accounts.pick` and the next-move target check still treat that account as refused.
The start uses `systemd-run --user --collect --quiet --no-block` with the user manager's environment.
The unit uses the repository working directory and `scripts/lib/py` to run `scripts/production/broadcast.py`.
The quota pass waits only for the unit start, so slow peer delivery cannot consume its 30-second budget.
A failed unit start logs one failure line and has no retry.
The separate unit records later delivery output in its journal.
A refresh that clears the flag reports `notes refreshed; workers-back notice sent`.
The flag survives state reloads and suppresses repeated ON notices during the same episode.
`agent-notes.timer` starts `agent-notes.service` 30 seconds after the preceding service finishes, with one-second timer accuracy.
`scripts/maintenance/agent_notes_job.py` runs its quota part after the notes part.
`config/agent_notes.toml` sets the quota and notes intervals to 120 seconds.
Normal recovery takes about two to three minutes plus reading, restoration, and broadcast time.
This interval is not a fixed reset-to-notice deadline; stale readings, failed moves, or interrupted jobs can delay OFF.
Pane workers move under their dispatch lock with fresh pane evidence.
An idle worker closes its pane through the shared `worker_panes.can_close` check.
`worker_panes.close` repeats that check under its worker lock before it closes the pane.
Held dispatch locks, live launchers, and unfinished submitted passes keep the pane open.
A live detached run never holds an account move.
The move stops and resumes its worker on the target account, even when the worker is idle.
The preview and resume text name each run, its result record, its log, and the command that reads them.
The detached run continues and delivers its result through the worker's stable staff ID.
The normal delivery route uses the current session or its holder.
The new-assignment check still refuses a worker with a live run.
The preview prints `kept open: <reason>;` before its resume text.
A held worker lock instead prints `not moved: dispatch lock is held` and refuses the move.
Every finished pass needs its dispatch DONE at the end of the last transcript turn before its open pane can close.
This proof permits closure when herdr still reports a working agent.
Without a dispatch pass, the last transcript turn must end with the agent's final reply.
Unavailable transcript evidence keeps the pane open and reports its reason.
A later message removes that proof.
Unknown process identity keeps its pane and reports its reason.
Closing the idle pane writes no move journal row.
The next assign resumes it on the group account.
A busy worker or a kept worker uses `restart.stop_row` and `worker_panes.start_or_resume` to keep its conversation and pane.
A stopped worker with a held dispatch resumes in the same pane.
A closed worker without an active dispatch has nothing to move.
An active or refused pass receives resume text through the shared target check.
A worker with a live detached run also receives resume text when its pass has ended.
Single moves, group moves, swaps, refusal moves, and shutdown recovery use the same run context formatter.
`config/shutdown.conf` sets the stop deadline from `STOP_GRACE_SECONDS`, `KILL_GRACE_SECONDS`, and `MOVE_STOP_MARGIN_SECONDS`.
The [quota guide](../quota_alerts.md) owns phone episodes, forecasts, routing notices, and delivery retries.

Login renewal uses named success and failure results.
The watcher rate-limits attempts and records their reason.
A failed renewal remains visible; it does not convert a stale reading into a fresh reading.

### Worker move results

`accounts.py move workers <target>` stops same-family workers and leaves their restart pending.
`assign.sh` cannot yet carry both the fixed staff ID and the destination account for this operation.
The result names the target account: `stopped, resume pending on <account>: <reason>`.

The preview uses the same family decision before it takes a row lock or stops a source.
A same-family worker prints `<name> (<id>): would stop; restart on <target> stays pending`.
A cross-family worker prints `<name> (<id>): not moved: cannot restart on <target> through assign.sh`.
Unavailable family evidence also refuses the restart.
The real refusal leaves the source running.

Source release preserves each stop reason and gives the next action.
The [family handoff](long-lived-workers.md#family-handoff-and-retry) defines its named results and native source evidence.
A session move's opening message selects only its [own worker children](shutdown-and-restart.md#worker-resume-results).

## Recovery and alert ownership

The unit watchdog reads current names, parents, sessions, and folders from staff.
It reads the account from the pane launch record and checks current process liveness.
It saves pending jobs before it starts detached recovery services.
Each child resolves the current staff session before resume.
Historical jobs remain keyed by session id.
Account notes keep historical rows without using them as current counts.
A failed roster read reports one anomaly and sets the derived count to zero.
Recovery keeps its grace period, dispatch limit, and result until a fresh observation confirms the session state.
The watchdog sends no phone push, showrunner notice, or message to a live session.

Retained record-driven restart paths share a durable alert ledger.
The [shutdown design](shutdown-and-restart.md#outage-alert-ledger) describes claims, delivery, leases, machine ownership, and observation-scoped clearing.
The ledger survives shutdown-record archival.
Claim ownership prevents concurrent sends and stale updates.
A second outage or five-hour delivery caller prints `already claimed by <id>` and does not repeat delivery.
Its delivered state means a successful push, not an unfinished send claim.

Config publication uses a blocking `fcntl.flock` with no time limit or retry settings.
The kernel releases the lock when its holder exits.
Usage and renewal schedules keep separate claims.
A second schedule caller prints `already claimed by <id>` and exits with code 0.

## Status, counts, and notes

`StatusReading` separates stored facts from their display.
`StatusDocument` carries account rows, problems, and session input paths.
The text table appends `Login ends` in the user's zone, with the same date format as `Weekly resets`.
Each JSON account exports `login_ends_at` as an ISO 8601 UTC string, or null for an unknown date or Codex.
`Report` distinguishes `LoginEndKnown` from `LoginEndUnknown`.
The usage reader takes the date from `claudeAiOauth.refreshTokenExpiresAt` in the metadata it already loads.
Both live paths use this source, so the disk identity shortcut still avoids a Claude process start.
The usage store and the timer cache preserve the date without storing other credential fields.
An accepted reading without a date removes the stored date.
Both renderers use the same problem lines from `StatusReading.notes`, including shared-home repairs.
JSON `problems` matches the text board's problem lines, with only the `* ` footnote prefix removed.
Each account row exports `sessions` beside `role_counts`.
Each entry has a native `session` ID and a classified `state`: `busy`, `starting`, `waiting`, `idle`, `paused`, or `exited`.
The list reuses local count observations and reads no pane again.
Peer caches provide aggregate counts only; the session list contains local members.
Named problem causes and repair steps preserve the subject, fault, evidence, and action.
Status attributes a session to its account independently of the current role selection.
This preserves visible off-target members during a move.

Local role counts read the registry, staff, pane launches, and worker records without writing state.
`session_state.classify` supplies the shared unit status for the accounts board and the units table.
An exit record takes precedence over a pause record.
A busy pane gives `busy`. A starting pane gives `starting`, including with a live detached run.
The starting state shows that the pane has not reached an idle prompt.
Other live detached work gives `waiting`.
Exit and pause records take priority over all activity states.
Without a live detached run, a missing live session gives `exited`.
Other live activity gives `idle`.
Role counts include `starting` and `waiting` separately from `busy` and `idle`.
Peer counts use zero for each missing `starting` or `waiting` field.
Published counts include waiting within busy and starting within idle, so older peers retain valid totals.
The new fields identify those subsets. The decoder separates them for display.
The cache retains the published document to prevent repeated subtraction.
Claude producers count under `showrunners` with the same hold and activity rules.
Without a live session, they count on the selected showrunners account.
`session-counts-read` prints the same live count document without publishing a file.
Peer counts use the shared role-map transport in a detached fetch.
One claim prevents concurrent fetches.
The normal status path never waits for that fetch.
The cache keeps each machine's counts separate and rejects copies older than ten minutes.
Canonical `mac` and `natedev` names identify observations independently of a desktop hostname.
The board sums available machine counts and identifies an unavailable peer.

`status --stored` reads saved usage, role maps, and peer counts.
It does not wait for the detached counts fetch.
Local session counts still use current local registry evidence.
The [pane design](accounts-queue-panes.md) owns the display contract and refresh lifecycle.

Account notes use numbered filenames.
Each note shows its roles and live session count.
Existing session rows remain as history and supply no current staff facts.
Unchanged notes are not rewritten.
Usage reads retain account identity and reading outcome; a failed read never becomes a zero-percent reading.
Recent usage can be reused, but low quota, resets, queued moves, and identity changes require fresh checks.
One Codex app-server serves each home within a pass.

## Machine-local settings and promotion

The settings clean filter classifies top-level keys against `git show HEAD:settings.json`.
The fixed local list contains `model`, `effortLevel`, `modelSettings`, `extraKnownMarketplaces`, `theme`, and `voice`.
A new key absent from the committed baseline is also local.
An unreadable or invalid baseline refuses the operation; it never means an empty baseline.
The hook and plugin mergers preserve their shared portions and known local additions separately.

The clean filter saves local values in `settings.local-keys.json` and omits them from Git's shared representation.
It captures stdin once, so snapshot and clean use the same document.
An empty snapshot retains saved keys that remain local.
Smudge restores saved local values without replacing incoming shared keys.
A key that arrives through committed settings becomes shared unless the fixed list keeps it local.
Unknown keys on one machine therefore do not block promotion on another machine.

`NoticeHistory` stores `pending` or `accepted` per machine and key in `settings.local-key-notices.json`.
The filter records a pending notice but sends nothing.
The normal refresh sends the notice later to the destination in `config/settings.conf`.
Only the machine and key name enter the notice; the setting value stays local.
A stable sender key suppresses duplicates.
Successful, skipped, or durably queued delivery marks acceptance.
A failed send leaves the notice pending.
The sender holds no ledger lock during delivery.

The index refresh hashes one file copy before and after cleaning.
It refreshes only when the clean hash matches the staged blob.
The guarded refresh checks the same input and staged hashes under Git's index lock.
It preserves concurrent edits, staged content, unmerged entries, and executable-mode changes.
A clean failure stops refresh and writes its diagnostic to the local error log.
Promotion never hides an unrelated dirty setting.

## Runtime configuration and constraints

`config/accounts.conf` supplies account timeouts, ages, limits, sizes, holds, leases, and path roots.
Each pass takes one `conf.read("accounts")` and shares its `ConfigRead` with its helpers.
The read supplies `whole`, `number`, and `text` methods.
Session loops reuse a value read for that call instead of reading configuration for each session.
Paths with `~` expand through `HOME`, so scratch homes remain isolated.
A configuration change needs no build.

`CONF_DIR` selects an authoritative configuration folder.
Without that override, an absent file or key can use the repository's matching `config/<stem>.conf`.
That default lookup precedes Git recovery.
An invalid present value still uses validated Git recovery, rather than an unrelated repository default.
Explicit folders have no repository fallback.
`ConfigRead.origin(key, convert)` applies the same rules and distinguishes live, recovered, and unavailable values.
The [shared configuration contract](showrunner-automation.md#shared-runtime-configuration) defines reads, limits, and publication.

`note_file.notes_dir()` gives `AGENT_NOTES_DIR` and an explicit `AGENTS_DIR` override precedence.
Its private cached helper reads `notes_directory` at most once per process.
Account creation, identity resolution, and missing-note instructions use that same helper.
A new process sees a changed notes-folder setting.

The default usage age is ten minutes.
Codex alert readings use the same `usage_stale_minutes` setting.
The quota relay limit defaults to 40 seconds, and the registry editor limit defaults to 30 seconds.
Account commands and Claude usage HTTP requests each default to 15 seconds.
The Codex account server has three seconds to stop after termination.
The role-map transport has a five-second connection limit and a twenty-second command limit.
The outage claim lease is fifteen minutes, longer than the normal send deadline.
These values live in configuration; their values are not independent code defaults.
Maintenance schedules and recovery dispatch limits remain in their owning TOML configuration files.

Account and role changes preserve these boundaries:

1. Keep private identity files outside shared links.
2. Use the authoritative role map for every machine.
3. Keep unknown evidence distinct from absence or exhausted allowance.
4. Validate the destination before a destructive move step.
5. Preserve conversation identity and worker threads across account changes.
6. Keep status reads independent of the background counts fetch.
7. Mark an alert delivered only after successful delivery.
8. Clear only alerts that precede the observation of a live session.
9. Preserve shared settings and concurrent edits during local-key filtering.

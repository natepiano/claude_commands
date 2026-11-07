# Claude and Codex accounts

## Which account am I on?

The status line's first row shows its current staff assignment and path; its second row shows this session's account label. To print it:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/whoami/account.py
```

`/whoami` shows Claude accounts used by local live sessions, omitting unused Claude accounts. Before this machine cuts over, its legacy `~/.claude` login is also included. It also shows every Codex home; with multiple homes, the home in use is marked `(workers)`. It does not identify this session's account.

## Trusted worktrees

Run `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py trust` to trust
folders listed in `[trust].folders` in `config/accounts.toml` (currently `~/worktrees`)
in the shared Claude home and every numbered Claude home. New account seeds use
the same list. The command preserves other settings and permissions.
It reports each home independently and leaves trusted files unchanged.
It uses the account home lock and a blocking kernel lock for config publication.
The publication lock has no time limit or retry settings.
The kernel releases the lock when its holder exits.
The command acquires the publication lock once for each home.
Reading, preparation, publication, sync, and cleanup run under that lock.
The command rebuilds a changed file before publication.
It never opens a session.

## Every account in one table

`/showrunner:accounts` opens the accounts board.
`/showrunner:accounts close` closes it.
The board refreshes stored readings every ten seconds.
The board and terminal table show upcoming moves under the table and before the problems.
Both use the exact group text from `accounts.py next list`.
An empty queue shows `Upcoming moves: none`.
On the Mac, the role-map reply supplies the list and saves it in the role-map cache.
Stored and offline reads use the saved list without another SSH call.
Without a saved list, the board shows `Upcoming moves: unavailable; natedev unreachable`.
Run account operations in a terminal with `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py`.
The terminal status table lists the numbered Claude and Codex accounts.
The table includes accounts without a login.
`~/.claude` is the shared config home and has no account row.
The legacy `~/.codex` has a row only while it holds a login.
An empty numbered Codex home has no row when `~/.codex` holds its note's login.
Usage reads use the home that holds the login.
A noted account without a usable home has a `not set up` row when no home holds its login.
Each role has a separate row.
The first role row shows the account's values.
Later rows show only the role and its counts.
The text table puts counts in the Role cell, such as `units ✓ 21 (busy 5, shell 1, idle 15)`.
A role appears on each account where its members run.
`✓` marks its selected account, including when it has zero members.
An account with one role or no role keeps its existing row.
In a terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status
```

| Column | Shows |
| --- | --- |
| Folder | The account folder. |
| Label | The account's label. A named Claude folder keeps its own label; a Codex home shows the label of its login's note. |
| Role | One observed role per row, with activity counts. `✓` marks its selected account. |
| Weekly left | Percent of the weekly window left. A dash when the account has no weekly window. |
| Weekly resets | When the weekly window resets. |
| On credits | Codex only: `yes` when a window is used up and the account's credits now pay for the work. |
| Credits left | Codex only: the credit balance, or `unlimited`. |
| Read | When those numbers were read. |

Each Codex account can stay signed in in its own home. The table's `workers` role follows natedev's shared role map. An unset workers role refuses the launch. It does not select `~/.codex` as a fallback. A caller's `CODEX_HOME` does not change the `workers` role. Signed-in spares get usage readings too; their notes stay inactive and raise no quota alerts. Freestanding Codex sessions keep their own process home.

Times are in the user's zone setting (`config/user_zone.conf`). The numbers are each account's last good reading: the usage service refuses many reads, so the two-minute updater saves each one it gets, and an account with none in the last 10 minutes is read once more. `unavailable` means no reading is held for a window that is still open. Credits older than ten minutes show `stale since <time>` rather than a balance. The board fetches stale Codex readings itself on both Linux and the Mac; `--stored` makes no live usage request. Each Codex login has one row, even when two homes hold it; duplicate-home problems still name the homes. The Read cell shows the last good read. A `*` marks a failed latest attempt. An old read also shows its month and day, such as `10-17 20:15 EDT*`. One footnote per failed account gives the cause and the next step. Its reading time includes the month and day when it is not from today. It names the last usage attempt time, or says "the attempt time is not recorded". It does not promise when another read will run. A good read clears the star and footnote. The default table shows accounts and upcoming moves; `--detail` also shows unknown entries. Lines under the table name failed checks and MCP servers that differ from `~/.claude`.

`status --json` prints `{"accounts": [...], "upcoming_moves": [...], "problems": [...]}` from the same readings as the table, in the same account order. `problems` contains the problem lines below the normal table.
`upcoming_moves` contains the rendered upcoming move lines, including the heading.

Each account has `label`, `tool`, `roles`, `selected_roles`, `role_counts`, `weekly_left_percent`, `resets_at`, `credits`, `use_credits` and `read_at`.
`tool` is `claude` or `codex`.
Roles include observed members and selected roles with zero members.
`selected_roles` identifies the selected account independently.
Codex roles are `workers` or `spare`.
Each `role_counts` entry combines available machine counts for the role at the same index, as `{busy, shell, idle, total}`.
Claude sessions use their session-record state; stopped members use `shell`.
Codex workers are busy while their turns run or start; otherwise, they are idle.
Codex shell counts are null.
A role with no reported members has zero counts; `spare` has a null entry.
Missing readings are null.
Reset and read times are ISO 8601 UTC strings.
Credits are a number, `"unlimited"`, or null.
Claude has null credits and `use_credits: false`.
Codex `use_credits` is true unless its note says `use_credits: off`.

The JSON has no login or folder fields. `--json` cannot be combined with `--detail`.

The accounts board orders its columns as account, left, role, busy, shell, idle, total, resets, credits, enabled.
Each role has a separate row with its own counts from natedev and the Mac.
The first role row includes all account values.
Later rows show only the role and its counts.
For example, `units` can have a busy count of `3`, followed by `showrunners` with `2`.
Spare counts are blank.
Running Claude session records supply `busy` during a turn.
Other running Claude sessions count as idle, including records with an old `shell` state.
A missing messaging socket does not make a running Claude session count as shell.
A stopped staff member counts as `shell` on its last saved launch account.
Live process account settings take precedence over saved launch accounts.
The Mac reads those settings through the kernel, without a subprocess for each member.
Members without an observed account are not assigned to the selected account.
The detail view uses the same count snapshot as the main table.
Missing activity counts as idle only when native evidence proves that the session runs.
Worker displays read staff rows and panes through `worker_panes.observe` with one staff snapshot.
Open workers show their panes and status; stopped and closed workers retain those states.
Unknown process evidence keeps its pane and reason; incomplete observations keep their reasons.
Neither state proves that a worker stopped or disappeared.
An unreadable staff record produces a problem, not an empty worker list.
Codex shell counts remain unknown and blank.
Only an existing run pinned to `mesh` reads its retained roster and live server port.
The last row sums known counts across all accounts and roles.
Narrow panes preserve the order and the total row.
JSON retains `credits_in_use`, `read_at`, and `read_ok` for other consumers.
The board has no `in use` or `read` column or fact.
Failed reads and old readings appear in Problems.
The dot stays red until the account has a current reading without a read failure.

Counts read local session records, saved launches, staff membership, and worker observations once per status read.
Only an existing run pinned to `mesh` adds its retained roster records.
`session-counts-read` prints live local counts without a file write.
Status reads compute local counts without a file write.
Every board read starts a background fetch of the other machine's counts through the shared role-map transport.
The fetch has a 20-second limit and a five-second connection limit.
One file lock prevents concurrent fetches.
The board read never waits for the fetch.
`status --stored` reads the saved peer counts with the same ten-minute age limit.
The board uses `peer_session_counts.json` only within the existing ten-minute age limit.
A failed fetch replaces that copy with an unavailable marker.
A missing, damaged, or old copy also makes the peer unavailable.
Problems then contains one line: `<machine> unavailable: counts are <this machine> only`.
The count columns then show local counts only.
A later successful fetch restores the combined counts on the next board tick.

Each JSON account has `role_counts_by_machine`, keyed by machine and then role.
Each role value contains `busy`, `shell`, `idle`, and `total`.
The JSON omits unavailable machines from this field.
Python combines the available machine counts once in `role_counts`, in role order.
The board and text table read `role_counts` without combining machine records again.
Claude shell counts include each available machine; Codex shell counts stay null.
The JSON has no session identities or account directories.

The JSON `session_inputs` lists the files and directories that the board watches, including the peer cache.
Each existing ten-second tick refreshes local liveness.
A dead process disappears from local counts even when no file changes.
The reader skips damaged launch entries and retains valid entries.
A failed refresh retains its watches and retries on the next tick.
An unavailable local staff record supplies no group membership.


Each problem below the table uses one form: `<home or account>: <what is wrong>; <cause>; <step to fix it>`. An MCP server difference is the one exception. It has one line per account, server and direction, and it counts only the projects where the account differs from the same project in `~/.claude`: `claude2 turns brp off in 3 projects where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair claude2 brp`. The home scope counts as the project `~`; both files read the same `~/.claude` project. The command at the end makes that server match the baseline in every differing project. It writes atomically, preserves the file mode and keeps a `.claude.json.mcp-backup-<time>` beside the account file. It preserves other servers and account settings. The read-only `mcp-projects <account> <server> <off|on>` command remains available to inspect those paths before a repair. The table and JSON `problems` use the same lines; JSON omits the `* ` footnote prefix. A direct folder or sign-in check replaces the empty-row summary for that cause.

A login with no account note has one line that names the missing note. The line asks for the account's login. Problem lines use account labels and never print a login.

A reserved test-domain login has its own cause. For example: `~/.codex: test sign-in is stored; a test wrote ~/.codex/auth.json at 2026-10-29 01:14 UTC; delete ~/.codex/auth.json`. The time is the file's modification time in the user's zone. Reserved domains include `.invalid`, `.test`, `.example`, `.localhost`, `example.com`, `example.net` and `example.org`, including subdomains. Status prints no account-note step for a test sign-in. It deletes nothing.

Each Claude group with no account has a note: `units: not configured; the role map selects no account; run accounts.py move units <claudeN>` (or `showrunners`). A configured group has no such note; its account has the group in the Role column. The JSON `problems` list has the same notes.

`status --stored`, with or without `--json`, skips live usage requests and network or host discovery.
Session counts use local session records, saved launches, staff membership, and worker observations.
Only an existing run pinned to `mesh` reads its retained roster.
The command uses local process checks and shows saved readings regardless of age.
`read_at` records the reading time.
Natedev reads the local role map; other machines read `role_map_cache.json` without SSH or a cache write.
Stored mode reports no problem for a usable cached map.
Without a usable cache, roles are empty and `problems` names the missing map.
Other commands keep their role-map fetch and cache behavior.

Without `--stored`, a role map that cannot be fetched or read from cache refuses status with an error on stderr and exit 1, for both the table and JSON.

On the Mac, the `account-usage` launchd user agent polls at login and every 30
seconds in the GUI login session, where the keychain is available. It runs
`agent_notes.py usage-only --poll-seconds 30`.
Each pass reads and saves due account usage.
Each account has its own eligibility time.
The usage claim records its granted time after credential lookup.
A held claim keeps that account's time.
The Mac schedule retains the configured interval and poll allowance.
Rate-limit backoff still applies.
The usage schedule lock prevents concurrent passes.
A second caller prints `already claimed by <id>` and exits with code 0.
After the read, it checks whether an account needs renewal.
When renewal is due, it starts a detached `agent_notes.py renew-logins` process.
The usage job does not wait for the renewal probe.
A separate renewal lock prevents concurrent probes.
A second renewal caller prints `already claimed by <id>` and exits with code 0.
A busy renewal lock permits usage reads.
Renewal tries each expired local Claude account at most once per hour.
A renewal error prints one diagnostic and preserves the usage messages and saved readings.
The refresher writes no notes, sends no quota alerts, and runs neither the Codex pacer nor account moves.
Natedev owns those actions.
Install the scripts before activating `modules/darwin/account-usage.nix`.

A process that cannot reach login credentials reports `credentials unavailable
in this session`. This is not a failed usage read: it keeps the last good
reading, its time and its outcome. An SSH read cannot turn a good account's
Read cell into a failed attempt. With no previous reading, usage stays
unavailable until a logged-in desktop refresh can read it.
A readable credential entry without a token requires a session or manual login.

Natedev's `agent-notes` job runs `scripts/maintenance/agent_notes_job.py`.
Notes, quota warnings, the Codex pacer, refusal moves, crash recovery, phase-note
pruning and the vault commit each run in a separate process.
Each process has a time limit. `config/agent_notes.toml` owns their intervals and
limits and is read on each run. A slow or failed part does not stop the other
parts. Quota warnings and the pacer read after the notes part exits;
the vault commit runs after the writers exit. A rerun skips completed parts
until their next interval and retries failed parts.
A failed or timed-out part fails the job.
Completion times and the job lock live in `~/.local/state/agent-notes/job/`.

Notes reuse usage reports for less than ten minutes, keeping their original
reading times. Each notes pass refreshes session counts from the roster. Usage reports without credentials stay in
`~/.local/state/agent-notes/timer-usage.json`.

An explicit refresh reads at once. A queued account move on the role-map host, a
weekly or five-hour window at or below 10% remaining on an account in use, an active Claude fallback,
a passed window reset, or a changed login or account selection also requires a live read.
A new Claude refusal refreshes usage
before placement when a new refusal needs a decision. It does not repeat the notes pass.
An unchanged refusal already requested, cancelled, continued or assigned to a group
does not refresh usage. A failed refusal refresh reports one line and keeps placement
and the watchdog running with stored readings.
The Codex blocked-command path keeps its own immediate live read. These exceptions
keep moves and restarts on their existing cadence. A missing or damaged timer cache
requires a live read.

For a readable Claude directory with a stored subscription type, the live usage pass
uses its on-disk login and plan and sends the stored OAuth token to the usage endpoint.
It skips `claude auth status`. A missing identity or subscription type keeps the CLI
check, so plan labels stay the same in `/whoami` and the notes. Standalone account status reads keep their
auth-status check. Each Codex home keeps one app-server for its
initialize, account and usage requests during a live pass; no server starts on a
reused pass.

## Starting a session

Run these in a terminal:

| Command | Account |
| --- | --- |
| `claude` | The configured showrunners account, through `accounts.py exec showrunners`. `accounts.py exec claude` starts the default `~/.claude` home. |
| `claude1` | claude1 |
| `claude2` | claude2 |
| `claude3` | claude3 |
| `claude4` | claude4 |
| `codex` | Use the configured workers account through `accounts.py exec workers`. `codex update` updates the shared install on both machines. |
| `codex1` | Use codex1. `codex1 update` updates the shared install on both machines. |
| `codex2` | Use codex2. `codex2 update` updates the shared install on both machines. |

The showrunner installs these aliases in the shell configuration on Linux and the Mac after this change merges. In a shell without them, run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude3
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec showrunners
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex1
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex2
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec workers
```

Every `accounts.py exec` loads the launch folder's shell through `scripts/lib/nix_env.py` before it starts the agent or terminal supervisor.
The helper finds the nearest `.envrc` at or above the current directory.
With no `.envrc`, the caller's environment stays in use.
If the helper returns no usable environment, the command prints one anomaly and uses the caller's environment.
If the helper cannot import, the exec command reports the error and exits 1.

The launcher finds the executable on the loaded shell's PATH, then in `~/.local/bin`.
The search skips scripts that invoke `nix_env.py` or `lib.nix_env`.
If the search finds only wrappers, the launch fails.
The launcher restores the caller's `HOME` after the shell loads.
It then applies account variables, so the folder cannot change the selected account.
Claude labels and groups pass arguments to `claude`.
Numbered labels set `CLAUDE_CONFIG_DIR` to their numbered folder.
`exec claude` uses `~/.claude` and removes inherited account, remote-memory and OAuth-token overrides.
It does not select a group.
Put `--probe` before the label: `accounts.py exec --probe claude3 -n check-idle` sets `CLAUDE_PROBE_SESSION=1` for a probe session.
Its shutdown record has `probe: true` and `alerts: "none"`.
Restart and move preserve the marker.
A direct `send.py --to user` from that session sends no phone push.

`scripts/accounts/hello.sh <claudeN>` starts a throwaway probe session on that account, sends hello, prints the reply and exits. It needs no prompt; an allow rule covers it.

For `codexN` or `workers`, normal launches pass arguments to `codex` and set `CODEX_HOME` to the account's home. The shell aliases use these launchers. The `codex` alias uses `exec workers`. `--probe` applies only to Claude accounts.

When the first argument is `update`, the launcher uses the shared `~/.codex` install without account checks. The local update uses the terminal and receives the remaining arguments. It then updates the other machine through SSH with a 600-second limit. Each machine gets one version or failure summary line. An unreachable machine shows `unreachable; run codex update there`. The command returns zero only when both updates and version reads succeed.

Configured groups select accounts for new unit and showrunner launches.
Nightly reviews use the units account.
Set a group with `accounts.py move units|showrunners claudeN`.
An unconfigured group refuses with that command.
Explicit launch overrides must name the configured account.
New launches retain the selected account and model in herdr pane launch records.
Shared resume uses the requested target, or the conversation's retained launch account.
Each selected account uses its resolved account folder and the shared memory directory at `~/.claude`.
The launch removes an inherited OAuth token.
Commands accept numbered accounts and launch groups; `default` refuses.

### Memory on a named account

A session on `~/.claudeN` starts with two variables: `CLAUDE_CONFIG_DIR=~/.claudeN` and `CLAUDE_CODE_REMOTE_MEMORY_DIR=~/.claude`. The aliases, unit launches and `/shutdown restart` all set both.

The second one is why memory writes do not ask. Claude Code saves a memory file without asking only when no link sits in the file's path, and `~/.claudeN/projects` is a link to `~/.claude/projects`. With the variable, the session uses `~/.claude/projects/<project>/memory/` itself, the folder every account shares.

- A session started with `CLAUDE_CONFIG_DIR` alone asks on each memory write ("… which is a sensitive file"). No allow rule clears it: that check runs before allow rules. Restart the session through an alias or `/shutdown restart`.
- A session started before this change keeps asking until it is restarted.
- The variable is not in Claude Code's public documentation. After a Claude Code update that brings the prompt back, check it first: in a scratch folder run `claude3 -p --permission-mode default` and ask it to write one `.md` file into the memory folder its prompt names.
- Memory that a custom subagent keeps in its `local` scope is stored under `~/.claude/projects/<project>/agent-memory-local/` in these sessions, not in the project's `.claude/`.

## Log out and in

Each directory holds one login. `/login` or `/logout` inside a session changes it for every session using that directory.

`~/.claude` stores shared configuration and projects, rather than an account. Renew a login inside its numbered folder. `$HOME/.claude.json` remains the seed for new folders and the MCP baseline; setup copies project trust entries without the login.

To renew or fix a named account, start its alias, then `/login` as that account. For claude3:

```sh
claude3
```

```text
/login
```

Never `/logout` in a unit director's session: every unit on that account stops working. After any login, run `/showrunner:accounts`; every directory must match its label.

Historical `default`, `~/.claude` or `~/.codex` records resolve from their own login or a dated cutover entry in `config/accounts.json`, covering only records before that cutover and matching the tool. Without evidence they stay unresolved; today's login never names a past record. A resolved resume uses the numbered account's folder.

## Move a session

In this terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <session-id-or-exact-name> <claudeN> [--dry-run] [--why "<text>"]
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move units|showrunners <claudeN> [--dry-run] [--why "<text>"]
```

A move stops the selected session and resumes its conversation in its herdr pane on the target account.
The resume message explains the move and the retained workers.
Worker account moves use the pane procedure below.
Only local tabs are in scope; run the command on the other machine for its sessions.

One result line per member names its resumed account and pane, or the failure. The command returns the result directly. There is no move record, ready mark, follow or cancel step. `/showrunner:move` uses this same path and result.

A single session is found by id, then exact name.
A move outside the group account prints that the member returns to the group account at its next resume or restart.
Ambiguous or unknown sessions return an error.
A session already on the target returns success.
A group move selects its account for future launches, then moves each live member on another account once.
An empty group still selects its account.
Missing showrunner registrations are named with their repair command.

A fresh exhausted usage window without credits refuses the move.
A missing or stale usage reading permits the move with a notice.

`--dry-run` changes nothing. It uses the lightweight session listing and prints each selected name/id, source account and planned target. It starts no stop or resume.

## Picking an account

Automatic moves choose an account with room.
The ranking chooses the soonest weekly reset, then the most weekly allowance left, then the account label.
A caller can keep the current account while it has room.
Run `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py placement --explain` to see each account's readings and selection reason.

Claude accounts have room until they have an unresolved provider refusal. Without an account move entry, low and zero percentages do not move a group or remove room. Readings rank accounts: units use the soonest weekly reset, breaking ties by most weekly left; showrunners use most weekly left. A group with an account move entry waits for its head; refusal fallback runs only when that group has no entry. Completing off-target members onto its already-selected account still runs; if fallback finds no destination with room, the group keeps its account and the existing no-room notice names the earliest reset. Codex still uses fresh allowance and credits.

natedev holds the one role map for both machines: Claude groups in `~/.local/state/accounts/groups.json`, workers in `~/.claude/config/accounts.json`, and changes in `~/.local/state/accounts/roles.jsonl`. All readers and writers use `read_role_map()` / `write_role_map(change)`. On the Mac, writes run through `ssh natedev` and preserve the reason, cause and move id; failed writes refuse without changing local selections. Reads fetch the map once per command and save `~/.local/state/accounts/role_map_cache.json`. When natedev is unreachable, a cached map prints `role map from <time in the user's zone>; natedev unreachable`; without a usable cache the command refuses. No machine selects a substitute account.

`groups.json` stores explicitly selected group accounts. A manual group move, a refusal move or an account move entry writes it; selecting one group leaves the other unconfigured. Both groups may use the same account. Changes append `{at, group, label, why}` to `roles.jsonl`; historical burn skips these group entries. Launches, status, `accounts.py placement --explain` and the refusal watcher never initialize missing selections. Status and previews print `not configured`, and the watcher moves no member of an unconfigured group.

`exec unit`, `env unit` and `role unit` use the units account; `role showrunner` and new showrunner launches use the showrunners account. The legacy `unit` configuration key is ignored; `set-role unit` and `swap unit` refuse. Codex workers retain their own role.

New units and showrunners retain account and model information in their herdr pane launch records.
Group launch resolution accepts `--account` or `--config-dir` only when it agrees with the selected account.
Promoting a unit uses the configured showrunners account.
The watchdog resumes the conversation on its pane launch account.
A record-driven unit recovery can use its source account when the selected destination cannot launch.
The recovery records the fallback reason.

## Adding an account

One command adds an account and checks every place that names accounts:

```text
/new_account claude claude4
/new_account codex codex3 <login>
```

Each step prints `done:` or `for you:`: the login, and the lines it cannot write itself (`~/.claude/settings.json`, the nixos shell aliases). Run it again after those; the run after a Claude login writes the account note from that login. Everything else finds accounts by their `~/.claudeN` or `~/.codexN` folder and their note.

## Setting up a new directory

For claude3, run in a terminal:

```text
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup claude3
```

Then start `claude3` in a terminal, run `/login` as claude3, and check `/showrunner:accounts`. Setup links shared content to `~/.claude`; it reports existing real files or directories rather than replacing them. Login stays separate for each directory.

## Setting up a Codex home

Create an account note with its login first (`~/rust/hanadocs/agents/codexN.md`). For codex1:

```text
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py setup codex1
```

Setup creates `~/.codex1` with mode 0700 and links existing shared entries to `~/.codex`: configuration, hooks, instructions, skills, rules, plugins, sessions, archived sessions, the session index, history and the model cache. It reports real files or directories that prevent a link. Sign-in and Codex's home state stay separate; setup never links or copies `auth.json`.

Setup refuses without changing files or printing a sign-in command when another home already holds the account's login, because two homes on one login break token refresh.

Before printing the command, setup waits for Codex to finish indexing the shared sessions and answer its startup request. An interrupted first start can block later starts for 15 minutes, followed by the remaining index work; setup retries servers that exit unanswered and waits up to 20 minutes. If the limit is reached, rerun setup. A table row saying Codex did not start names the same repair command.

Run the printed command in a terminal and sign in as codex1:

```sh
env CODEX_HOME="$HOME/.codex1" codex login
```

Keep `env CODEX_HOME` on that command so it runs the binary directly and the login stays in the numbered home; workers always use their mapped account. Do not sign in to the shared `~/.codex` home.

Open `/showrunner:accounts` to see the new home's row.
It shows `spare` until selected for workers.
Run `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py check codex1` to check the home directly.
Problems name missing or broken shared links, the wrong login, or a login held by another home.
Terminal status with `--detail` also lists unknown entries.
Keep each login in one home; two homes on one login break token refresh.

The repository's hook installer keeps a linked `hooks.json` and adds missing hooks to its target, so numbered homes continue to share `~/.codex`'s hooks.

## Swapping the Codex workers account

One account serves every unit's Codex workers, including the `codex exec` fallback.
The updater, 5-hour watcher, `/whoami`, and accounts board follow that role.
Run `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py role workers` to read it.

```text
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py swap workers codex1 --dry-run
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py swap workers codex1
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py swap workers codex2
```

A swap reads both usage windows and the credits before it changes the role.
An unknown usage reading prints one anomaly and permits the move.
Pane workers move under their worker locks.
A live worker stops at the shell on the same terminal before its conversation resumes on the target account.
A stopped pane keeps its terminal; a closed pane resumes the same conversation in a new worker pane.
An active or refused pass receives one resume message; an idle worker receives no text.
The last dispatch supplies the role; a missing role prints its reason and starts no replacement.
A held worker lock or unknown evidence leaves that worker unchanged; unrelated proved moves continue.

A failed target resume permits one source resume only after fresh evidence proves that no target process owns the conversation.
The output reports the failed step and the source resume result.
An already running target stays running; the move starts no source beside it.
A completed rerun prints one `unchanged` line, exits 0, and sends no restart notice.

A swap prints `Workers moving <source> → <target>:` and one showrunner · unit · worker → target line per worker.
A worker interrupted during its turn adds `, interrupted and resumed`.
Each failure keeps its worker name and reason; other workers continue and the selected role stays changed.
`--dry-run` uses `Workers would move` without stopping workers or changing roles and history.
`set-role workers codexN` selects future launches without the swap's usage check or notices.
Role writes preserve other selections; `unit` and `workers` history share `~/.local/state/accounts/roles.jsonl`.

### Existing mesh-pinned runs

Use this branch only when `unit_runs.worker_transport(run)` reads `mesh` from an existing run's pin.
The move retires that run's server and confirms thread release before a replacement resumes.
An unreadable thread state or unconfirmed release starts no replacement turn.
Held messages move with the thread.
A failed resume names the worker, failed step, and manual resume command.

Swaps and sign-in changes leave ended sessions' workers unchanged, even when their launchers survive.
A run stays live while any holder session runs, with or without a messaging socket.
A run without a holder stays live only while a worker launcher runs.
Ended runs have no worker rows or holder notices.
Their outgoing servers receive the existing safe shutdown check.
The output says `N leftover servers from ended sessions, M stopped`.
Busy servers remain running with `left running: <pid> on port <port> (<run folder>): <reason>`.
A preview stops nothing and says `N leftover servers from ended sessions: M can be stopped, K would stay`.
Each busy server has a `would stay:` line.

Worker rows name the holder session, or the launching session when no holder marker exists.
Rows combine by that session and slot across plans and run folders.
A combined row shows ` ×N` and adds `, K interrupted and resumed` for K interrupted entries.
Each failed entry keeps its own row, reason, and manual resume command.

### Account availability

An account with no usage or credits needs a different target, credits, or its reset.
Unknown or expired usage needs a fresh reading.

## Automatic Codex moves

A workers next entry blocks provider-refusal and credits-off fallback moves.
Without a workers entry, a provider quota refusal moves workers to the selected Codex account with room.
Low quota above zero changes nothing; weekly zero with `use_credits: off` also requests a move.
A known 5-hour reset less than 20 minutes away waits once, with its reset time in the notice.
An unknown window or reset, a passed reset, or exactly 20 minutes remaining causes a move.
The selected home becomes the `workers` role through the `swap workers` path.
Pane workers resume their conversation and active or refused pass on that account.
Only an existing mesh-pinned run continues refused work before held messages.

When no Codex account has room, assignments fall back to Claude and the notice names the earliest reset. A failed move to the picked home also falls back to Claude, with the failed move and cause in the notice. They return only when the selected Codex account can work and its refusal is resolved. Recorded refusals override saved percentages. The moves journal at `~/.local/state/accounts/moves.jsonl` separates a requested role change from each worker's confirmed resume, and records waits, failures and renewed room.

## Automatic group rules

The account moves log on natedev holds ordered, one-shot moves for Claude groups and Codex workers in `~/.local/state/accounts/next_moves.json`. Use `/account_moves` in ordinary words, or:

```sh
accounts.py next add units 1 claude4 --hold-minutes 30
accounts.py next add units 1 claude2 --hold-minutes 30
accounts.py next add showrunners 1 claude4 --hold-minutes 30
accounts.py next add showrunners 1 claude2 --else claude4
accounts.py next add workers 1 codex2
accounts.py next add workers 1 claude
accounts.py next list
accounts.py next move showrunners 1 2
accounts.py next remove units 2
accounts.py next add units 1 claude3 --at 1
accounts.py next add units 1 claude2 --hold half-point
accounts.py next hold units 1 half-point --expect 1 claude4
```

The list has showrunners, units and workers sections in that order, numbered from 1 within each group: “units 1: at 1% → claude4, hold 30 min”. Position 1 runs next; an empty log prints “nothing set up”. `move GROUP N M` moves position N to M, `remove GROUP N` removes it, and `add … --at N` inserts at positions 1 through the group’s size plus 1 (omission appends). Edits preserve other groups’ order and internal entry identities; ids are never shown or accepted in these commands. The Mac forwards group positions to natedev, which resolves and edits them under the watch and log locks; install both machines together. A head change immediately clears the displaced head’s hold and notice keys, so the new head starts its own hold on a later pass; an unchanged head keeps its hold.

For units, showrunners and workers, agent-notes considers only the first entry per group.
Later entries wait until it records and removes the first entry.
A later pass can then process the next entry.
Later Claude entries keep no hold during that wait.
Use `--else claude4` to try claude4 when the first target cannot run.
Repeat `--else` to add more targets in order.
The list shows `showrunners 1: at 1% → claude2, else claude4`.
The move uses the first target with room and records the entry as done.
A due first entry stays queued when none of its targets can run.
It tells showrunners once when the blocking reason changes.
Blocking reasons include missing or stale readings, low quota, and unresolved quota refusals.
The current account counts as usable, even with low quota.
A due Claude entry that chooses the current account prints `unchanged` and leaves the queue.
The example holds units at 1% for 30 minutes, then moves them to claude4.
It holds them again when claude4 reads low, then moves them to claude2.
Both groups can use the same account.
The table shows `units` and `showrunners` on separate rows for that account.

`--hold-minutes N` is finite and nonnegative; omitted or zero moves immediately. `--hold half-point` instead estimates the time to use another 0.5 percentage point from the selected account’s weighted pace history, including usage by other groups sharing that account. One point per hour gives 30 minutes, rounded up to whole minutes; missing, zero or negative pace uses 40 minutes. The list shows, for example, “showrunners 1: at 1% → claude2, hold half a point (≈30 min)” or “hold half a point (40 min default: no pace history)”. The Mac prints natedev’s rendered list using natedev’s selection and readings; `next-read` remains machine JSON.

The first fresh low reading starts an aware clock and stores its resolved duration and source with `account`, `at`, `move_id` and `percent` in `refusal_watch.json`’s `auto_move_holds`. Restarts and later pace readings leave that duration fixed. A fresh high reading clears it, while stale or missing readings leave it alone. A selection change (including away and back) or percent change clears the clock. Legacy holds without a duration resolve from their entry’s fixed minutes and retain their original start time. Legacy reset progress is ignored and dropped on the next state write.

Once started, the list shows those stored minutes and their source: “hold half a point (34 min at 0.9 points/hour, 12 min left)”, “hold half a point (40 min default: no pace history, 12 min left)” or “hold 30 min (12 min left)”. Time left rounds up to whole minutes; after expiry it reads “hold over” until the runner can move. Unstarted, stale, legacy or non-head entries show the current estimate instead: “hold half a point (≈30 min)”, the 40-minute default above, or “hold 30 min”. Both machines print natedev’s rows; the list after `next add` and the `next hold` reply use the same forms.

An empty list does not read watch state. If that state cannot be read or parsed, the command reports one anomaly and shows the unstarted rows.

`next hold GROUP N half-point|MINUTES [--expect PERCENT TARGET]` changes only that entry’s hold mode, preserving its identity and position. A different mode clears its started hold under the watch and log locks before writing the list; the same mode keeps it, so retrying after a lost Mac reply preserves elapsed time. Other entries keep their holds. With `--expect`, a shifted entry refuses without changing either the log or holds. Always run `next list` immediately before a guarded update and copy its percent and target: listed percentages read back exactly to their stored values on both machines. On refusal re-list, and never recreate an already-consumed entry. Do not undo a mode change with another `next hold`: its cleared elapsed hold cannot be restored. Install natedev and the Mac together before writing any half-point entry. Workers refuse both hold modes.

Each refusal-watch pass starts a quiet probe session for each local noted Claude account with an expired or missing login token. The Mac usage refresher calls the same renewal function after it saves usage. The probe has a 120 s limit. A successful probe reads usage again. Refusal-watch then checks due moves. Renewal tries each account at most once per hour and never starts a probe in a dry run. Both callers store attempts in `~/.local/state/accounts/login_renewals.json`. Refusal-watch probes run before the move log lock. If a probe fails, the blocked line says: "claude4's login expired, and a probe session on it failed (<detail>). Log in on claude4 by hand." Showrunners receive that line through the usual relay. An expired or missing login token makes status ask you to start a session on that account. natedev renews in its notes job's refusal-watch pass; the Mac renews in its usage refresher. After a failed probe, status shows the probe time and asks for a manual login.

Account launches and renewal probes find Claude on PATH, then in `~/.local/bin`, and start its full path, including terminal relaunches; Codex account launches use the same search.

Entries require fresh readings, room above the first entry's threshold, and no unresolved target quota refusal.
A group that already moved this pass waits.
Recording the group choice removes its entry under the log lock.
Recorded `auto: <id>` history prevents a repeated move after an interrupted queue write.
Each member uses the shared stop and resume path, with one result line.
`refusal_watch.py --dry-run` changes no homes, choices, log, or hold state.

Workers entries target `codexN`, `claudeN`, or bare `claude`, without `hold_minutes`.
A numbered Claude target sets the account for new registry workers on both machines.
The role map stores the target and the source weekly reset.
At that reset, new launches use the default account.
The Mac uses its last saved map when natedev is offline.
A launch reports an error when no usable map exists.
Bare `claude` keeps the caller or showrunners account.
Subagents use their Claude session's account.
Only the first workers entry runs on natedev, before the zero policy.
The selected account needs a fresh reading at or below the entry's percentage.
A Codex target needs a fresh reading above that threshold and no unresolved quota refusal.
A failed move keeps the entry queued and delays later entries.
The watcher tells showrunners once when the failure reason changes.
Automatic moves record cause `auto` and reason `auto: <id>` in the group history and the moves log.
Each session attempt has its own `move_id`, with a request and one confirmed or failed outcome.
The request uses the session process account as its source.
Saved launch settings cannot replace the requested destination account.
The notes timer delegates moves to a detached batch before it stops any session.
The batch records each outcome when its resume and existing stay observation finish.
A failed resume uses the existing move failure report to tell the showrunners.
Blocked notices use the current queue head and target at delivery.
Each notice addresses a session once, even when its name and ID both select it.

A workers move to Claude needs a known source weekly reset and holds its work routing on Claude until that reset, even if Codex has room earlier. Status detail shows it under `Codex work routing:` with cause `auto`. While any workers entry is queued, default, provider-refusal and credits-off moves wait. With no workers entry, weekly zero first moves workers to included Codex usage when it is available. With credits on and no included room, workers stay and spend their paid credits. With credits off, or after a refusal, workers use the Codex account with room whose weekly reset is soonest, else Claude.

Failed routing-journal appends retain the Claude hold and a saved retry. A refused workers swap saves its failure before attempting the journal append; pending rows must reach the journal before replay checks can consume entries.

Both engines read recorded entry IDs before they run entries.
An unreadable workers move history preserves the queued workers entry and the workers role.
The Claude engine reports an unreadable move history and continues.
Journal writers append each complete row with one write and retry a short write.

Install this version on both natedev and the Mac before adding the first workers entry: older readers reject a log containing workers entries. Verify rollout with a scratch home.

A move checks the target's usage before it selects the account or stops a session.
Stop and resume failures print one reason.
Account preparation anomalies print one line and do not block the move.

The watchdog reads current unit names, parents, sessions, and folders from staff.
It checks process liveness before recovery.
Recovery starts after one observation pass.
Unknown liveness prevents a second session copy.
The watchdog saves the pending job before it starts a detached recovery service.
The child reads the current staff session before it calls `stop_resume.resume`.
It uses the pane launch record for the account.
A later pass reads the result and reports a failed resume.
The watchdog keeps the result until a fresh observation confirms the session state.
Live sessions receive nothing.
The watchdog sends no showrunner notices or phone pushes.
Historical jobs remain keyed by session id in `~/.local/state/accounts/unit_watchdog.json`.

## Automatic Claude moves

Watcher inventories read live sessions and `staff.json` membership. They neither collect nor read a desktop snapshot. Maintenance now runs through the separate agent-notes job; a `--dry-run` still writes nothing.

The agent-notes job’s refusal part checks local top-level sessions' last transcript entries for provider quota refusals. A refused unit or showrunner moves its whole group only when that group has no next entry; otherwise its head controls the account move. Owners outside either group move alone through the same stop-and-resume path, in their own local herdr tabs. The Mac usage refresher does not run this refusal check.

The board and group moves share one staff member list. Each live member uses its process account. Timer registrations do not decide membership. Absent registration targets are checked separately.

A known 5-hour reset in less than 20 minutes waits once per session and reset. With a queued Claude group head, any known refusal reset waits and continues on the current account when it ends; an unknown reset waits for the head, and queued workers retain their brief reset wait. Without a pending next entry, other refusals move; no room sends one account notice until a usage reading changes. When a refusal requires a move, the watcher uses the same immediate stop-and-resume path as a manual move.

`scripts/accounts/refusal_watch.py --dry-run` reports decisions without changing anything. For a live proof, `--assume-refused '<session id or name>' weekly|five-hour [--reset-in <minutes>]` holds a refusal in memory for that owner and runs once. The proof changes neither the account refusal nor other sessions’ picks. The optional reset affects its wait decision, not stored account usage. Add `--proof-group <probe id>…` to move exactly those probes together, without touching real groups. The stop-and-resume API returns the resumed account and pane, or one failure reason.

Account status with `--detail` shows session accounts, the workers account, pending continuations, and the last ten move entries.
Each entry shows its outcome and its `refusal`, `credits off`, `auto` or `by hand` cause.
Hand moves never inherit a stored refusal.
Group selections keep their transition and reason across member retries.
Each session attempt has its own `move_id` and cause.
A session already on the target writes no move entry.
`Codex work routing:` lists assignment fallbacks separately, paired by their start time, as open, routed back, or not restored.
Recovery is recorded only after all assignments are restored or kept because they were reassigned.
The first line names both groups.
Table role cells show their selected accounts.
A member still on its old account shows as `moving`.

## Low-quota alerts

When an in-use Claude account reaches 2% weekly and a group on it has no account move entry, the phone receives one reminder per episode: set up what happens next with natedev. Only entries targeting another account cover a group, and only while its replay history is readable. If entries cover every group on it, nothing is sent. Failed phone delivery retries; successful delivery never repeats until recovery or reset opens a new episode. Claude sends no session quota alert, restoration notice or instruction to stop starting work. `/quota_ack` is removed; `/quota_refresh` still refreshes readings. Codex also gets one phone reminder per low episode above 0% and at or below 2% when no workers entry targets another account and no scheduled Claude hold is open. At 0%, the default move is handled in the same pass and no reminder is sent. A reminder already delivered stays delivered across zero and subsequent readings until that weekly reset. An unreadable move journal makes coverage empty. Codex sends one rerun message to the unit director whose worker command stopped for quota, naming that command and asking for it to be rerun on Claude; an unknown director receives none. Waiting follows keep their continuation and receive no message. Zero, forecast, five-hour warnings and other refusal details are logged without session delivery; restoration is recorded only in the moves journal. Phone pushes and the natedev prompt notice remain.

Readings and refusals rank move destinations; they never replace a configured launch account. A launch with no configured group refuses without writing the group file.

## Codex account numbers and credits

A Codex number names the home that holds its login, which can be `~/.codex`.
The account note identifies that login.
A home with no named note is shown by its folder.
Both tools accept only numbered account names.
Legacy stored `default` values read as numbers on their next normal write.
Historical move entries use evidence recorded at the time, or show `home account unresolved`.
Setup creates `~/.codexN` when no home holds that account.

Set `use_credits: on` or `use_credits: off` in each Codex note. Missing values mean on. With off, an exhausted weekly or 5-hour window makes the account unavailable even with credits. At weekly 0%, workers first move to another Codex account with room when no workers entry is queued. With credits on, workers move only to included usage, and that move sends no alert. When no account has included usage left, workers use paid credits, with one phone notice and hourly forecasts. Each pass checks for included usage again. A move to that usage closes the paid episode with no alert. With credits off, workers prefer included usage, then another account using its own credits. When neither is available or the move fails, Codex assignments move to Claude until the earliest refill. Only the first forecast goes to the phone. See `docs/quota_alerts.md` for delivery retries and unknown forecasts.

Account notes use numbered filenames such as `claude1.md` and `codex2.md`.
Each note has a `role` list and a `sessions` count from the current roster.
A failed roster read reports one anomaly and sets the derived count to zero.
Existing session rows remain under `## Session history`.
The updater does not read them as current staff facts.
Legacy spaced labels remain accepted as input.
Notes list by tool and account number.


The account picker does not select a numbered home without a matching account note.
Status names the folder and missing note.
An explicit Claude launch does not require an account note.

### brp registration at login

The `claude-brp-mcp` login job registers brp only at local scope in existing `~/rust/hana`, `~/rust/nateroids` and `~/rust/bevy_brp` checkouts, for the shared Claude home and each numbered home. It removes user-scope brp registrations so other projects do not inherit the server. The setup check verifies those same local registrations. A correct registration is left untouched.

## Account configuration

`config/accounts.conf` defines the account limits, timeouts, sizes, ages, and hold durations.
The scripts read each value when they use it through `scripts/lib/conf.py`.
Install the file in `~/.claude/config`, or select a folder with `CONF_DIR`.
A configuration change needs no build.
An explicit `CONF_DIR` is authoritative and has no repository fallback.
Without `CONF_DIR`, a missing file or key uses the repository's `config/accounts.conf` before Git recovery.
An invalid present value retains the validated Git recovery path.
The file also defines the default notes folder and the home NixOS repository.
A leading `~` uses `HOME`; `AGENT_NOTES_DIR` still overrides the notes folder.
Account creation and account checks use the same notes-folder helper.
Each process reads the configured notes folder once.
Start a new process after a notes-folder change.
Missing-note instructions name that folder.
The refusal pass reads the usage age once and passes it through account selection.

See the [account design](as-built/accounts.md) for types, invariants, shared settings, and machine-local settings.

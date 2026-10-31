# Claude and Codex accounts

## Which account am I on?

The status line's first row shows this session's account label, after its role. To print it:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/whoami/account.py
```

`/whoami` shows Claude accounts used by local live sessions, omitting unused Claude accounts. Before this machine cuts over, its legacy `~/.claude` login is also included. It also shows every Codex home; with multiple homes, the home in use is marked `(workers)`. It does not identify this session's account.

## Every account in one table

`/showrunner:accounts` prints one table: a row for each numbered `~/.claudeN`, logged in or not; `~/.claude` is the shared config home and has no account row, then a row for each `~/.codexN`. The legacy `~/.codex` has a row only while it holds a login. Noted accounts whose home is absent or blocked by a file or broken link, and whose login no home holds, get `not set up` rows. In a terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status
```

| Column | Shows |
| --- | --- |
| Folder | The account folder. |
| Label | The account's label. A named Claude folder keeps its own label; a Codex home shows the label of its login's note. |
| Role | `units` and/or `showrunners` for the selected Claude groups; `workers` for the Codex home in use, `spare` for the other Codex homes. |
| Weekly left | Percent of the weekly window left. A dash when the account has no weekly window. |
| Weekly resets | When the weekly window resets. |
| On credits | Codex only: `yes` when a window is used up and the account's credits now pay for the work. |
| Credits left | Codex only: the credit balance, or `unlimited`. |
| Read | When those numbers were read. |

Each Codex account can stay signed in in its own home. The table's `workers` role follows natedev's shared role map. An unset workers role refuses to launch; it never falls back to `~/.codex`. A caller's `CODEX_HOME` does not change the `workers` role. Signed-in spares get usage readings too; their notes stay inactive and raise no quota alerts. Freestanding Codex sessions keep their own process home.

Times are in the user's zone setting (`config/user_zone.conf`). The numbers are each account's last good reading: the usage service refuses many reads, so the two-minute updater saves each one it gets, and an account with none in the last 10 minutes is read once more. `unavailable` means no reading is held for a window that is still open. The Read cell shows the last good read. A `*` marks a failed latest attempt. An old read also shows its month and day, such as `10-17 20:15 EDT*`. One footnote per failed account gives the cause and the next step. A good read clears the star and footnote. The default table shows only accounts; `--detail` also shows unknown entries. Lines under the table name failed checks and MCP servers that differ from `~/.claude`.

`status --json` prints `{"accounts": [...], "problems": [...]}` from the same readings as the table, in the same account order. `problems` contains the lines below the normal table. Each account has `label`, `tool` (`claude` or `codex`), `roles`, `weekly_left_percent`, `resets_at`, `credits`, `use_credits` and `read_at`. Roles are a list, so one Claude account can hold both `units` and `showrunners`; Codex roles are `workers` or `spare`. Missing readings are null. Reset and read times are ISO 8601 UTC strings. Credits are a number, `"unlimited"`, or null; Claude has null credits and `use_credits: false`. Codex `use_credits` is true unless its note says `use_credits: off`.

The JSON has no login or folder fields. `--json` cannot be combined with `--detail`.

Each problem below the table uses one form: `<home or account>: <what is wrong>; <cause>; <step to fix it>`. An MCP server difference is the one exception. It has one line per account, server and direction, and it counts only the projects where the account differs from the same project in `~/.claude`: `claude2 turns brp off in 3 projects where ~/.claude does not: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-projects claude2 brp off`. The home scope counts as the project `~`. The command at the end changes nothing. It prints the project paths, one per line, with `~` for the home scope. The table and JSON `problems` use the same lines; JSON omits the `* ` footnote prefix. A direct folder or sign-in check replaces the empty-row summary for that cause.

A login in the legacy home with no account note has one line, rather than separate usage-read, cutover and note hints. The line names the missing note, gives its `login:` value, then gives the cutover command. Other checks keep their facts and commands.

A reserved test-domain login has its own cause. For example: `~/.codex: test sign-in is stored; a test wrote ~/.codex/auth.json at 2026-10-29 01:14 UTC; delete ~/.codex/auth.json`. The time is the file's modification time in the user's zone. Reserved domains include `.invalid`, `.test`, `.example`, `.localhost`, `example.com`, `example.net` and `example.org`, including subdomains. Status prints no account-note or cutover step for a test sign-in. It deletes nothing.

Each Claude group with no account has a note: `units: not configured; the role map selects no account; run accounts.py move units <claudeN>` (or `showrunners`). A configured group has no such note; its account has the group in the Role column. The JSON `problems` list has the same notes.

`status --stored`, with or without `--json`, skips every live usage read, subprocess and network call. It shows saved readings regardless of their age; `read_at` records when they were read. On natedev it reads the local role map; elsewhere it reads only `role_map_cache.json`, without ssh or a cache write. If no usable cache exists, roles are empty and `problems` names the missing map. Other commands keep their normal role-map fetch and cache behavior.

Without `--stored`, a role map that cannot be fetched or read from cache refuses status with an error on stderr and exit 1, for both the table and JSON.

The two-minute account-note timer reuses its last usage reports for less than ten minutes.
It keeps the original reading times. Session tables, note session sections, the refusal
watch and the unit watchdog still run on every timer pass. Weekly and five-hour warnings
can follow the next usage read. The timer stores reports without credentials in
`~/.local/state/agent-notes/timer-usage.json`.

An explicit refresh reads at once. A queued account move on the role-map host, a
weekly or five-hour window at or below 10% remaining on an account in use, an active Claude fallback,
a passed window reset, or a changed login or account selection also requires a live read.
A new Claude refusal refreshes usage
before placement if the timer reused reports; it does not repeat session collection.
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
| `claude` | The configured showrunners account, through `accounts.py exec showrunners`. `accounts.py exec claude` starts the default `~/.claude` home, which is signed out after home cutover. |
| `claude1` | claude1 |
| `claude2` | claude2 |
| `claude3` | claude3 |
| `claude4` | claude4 |
| `codex` | The configured workers account, through `accounts.py exec workers` after the shell alias is updated. |
| `codex1` | codex1 |
| `codex2` | codex2 |

The showrunner installs these aliases in the shell configuration on Linux and the Mac after this change merges. In a shell without them, run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude3
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec showrunners
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex1
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex2
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec workers
```

Every `accounts.py exec` loads the launch folder's shell through the shared `scripts/lib/nix_env.py` helper before it starts Claude, Codex or the terminal supervisor. This applies to numbered labels and `units`, `workers` and `showrunners` on Linux and the Mac. The helper finds the nearest `.envrc` at or above the current directory. It allows that file under `~/rust`, `~/worktrees` or `~/.claude*`; elsewhere, a file that is not allowed refuses the launch and names the `direnv allow` command. A loaded shell must set `IN_NIX_SHELL`. With no `.envrc`, the caller's environment stays in use. A refusal prints the helper's reason and exits 1 without launching an agent. If the shared helper is missing, only the exec command fails with an update message.

The executable is found on the loaded shell's PATH, then in `~/.local/bin`. The search skips executable scripts that invoke `nix_env.py` or `lib.nix_env`, so a plain `claude` or `codex` wrapper cannot load the shell again. If only wrappers are found, the launch fails. After the shell loads, the caller's `HOME` is restored and account variables are applied, so the folder cannot change the selected account or home. For Claude labels and groups, arguments pass through to `claude`. Numbered labels set `CLAUDE_CONFIG_DIR` to their numbered folder; claude2 uses `~/.claude2` just like the other accounts. `exec claude` uses `~/.claude` and removes inherited account, remote-memory and OAuth-token overrides; after home cutover it starts signed out until that home has its own login. It does not select a group. `--probe` goes before the label: `accounts.py exec --probe claude3 -n check-idle` sets `CLAUDE_PROBE_SESSION=1` for a throwaway session. Its shutdown record always has `probe: true` and `alerts: "none"`; restart and move preserve the marker. Even a direct `send.py --to user` from that session sends no phone push.

`scripts/accounts/hello.sh <claudeN>` starts a throwaway probe session on that account, sends hello, prints the reply and exits. It needs no prompt; an allow rule covers it.

For `codexN` or `workers`, arguments pass through to `codex` and the launch sets `CODEX_HOME` to the home holding that account's login. After the Codex cutover, codex2 uses `~/.codex2` and codex1 uses `~/.codex1`. The `codex1`, `codex2` and bare `codex` shell aliases follow these launchers; bare `codex` follows `exec workers`. `--probe` applies only to Claude accounts.

Configuration decides the account every launch starts on: new, standby and resumed units and nightly reviews use the units account; new and resumed showrunners use the showrunners account. Set a group with `accounts.py move units|showrunners claudeN`. An unconfigured group refuses with that command; an `--account` or `--config-dir` override must name its configured account. New unit launches use herdr panes carrying the selected account variables; legacy tmux options remain available for observation and model recovery, and never choose a resume account. Every selected account runs in its own numbered folder, with the shared memory directory set to `~/.claude`. The launch removes any inherited OAuth token. Commands accept account numbers and the launch groups; `default` refuses.

A missing directory refuses without starting Claude, for example:

```text
accounts: ~/.claude3 is not set up; /showrunner:accounts setup claude3 first
```

Broken shared links or unreadable identity files also refuse. A directory that is set up but logged out can start so you can log in.

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

If the directory holds another login, the alias refuses with:

```text
accounts: ~/.claude3 is logged in as <login>, not claude3; see Log out and in in ~/.claude/docs/accounts.md
```

Start that directory directly, then log out and log in as claude3. `env -u` drops an inherited `CLAUDE_CODE_OAUTH_TOKEN`, which would otherwise override the directory's login:

```sh
env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=~/.claude3 claude
```

```text
/logout
/login
```

Never `/logout` in a unit director's session: every unit on that account stops working. After any login, run `/showrunner:accounts`; every directory must match its label.

## Moving the legacy home login

Each machine cuts over separately. While `~/.claude` still holds a login, processes without `CLAUDE_CONFIG_DIR` retain that login's attribution and status prints `~/.claude: legacy login is stored; the home still holds a login; run accounts.py cutover-home`. After cutover they have no account; a stale status-line label does not identify them. Unscoped helpers, including the message relay, retain the legacy launch without an account setting until this machine cuts over. Afterwards they keep the caller’s numbered folder or select the showrunners group.

On Linux, in a terminal after its legacy sessions have stopped:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py cutover-home claude2
```

On Linux this moves the login into `~/.claude2` by rename, refuses an occupied destination and live legacy sessions, and journals the operation before moving identity files. Rerun after an interruption to finish. `cutover-home --undo` reverses it after sessions on the destination stop. No live cutover is part of installing this code. natedev cut over on 2026-10-15 and the Mac on 2026-10-16: on both machines `~/.claude` holds no login and claude2's login lives in `~/.claude2`.

For the Mac's separate step, the director first prepares `~/.claude2` and arranges for legacy sessions to stop. The user launches that numbered account and runs `/login` once inside it:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude2
```

After the new login is checked, exit that session and run this in a Terminal on the Mac. An ssh session cannot see the login keychain:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py cutover-home claude2
```

The command checks that the new and legacy logins are the same account before changing anything. It keeps the shared seed's other settings, removes its `oauthAccount`, and removes only the old local `Claude Code-credentials` keychain copy by service name. The server login is untouched. Never `/logout`. Named-folder usage readings use that folder's own keychain entry and never fall back to the old copy. An interrupted cutover can be finished by rerunning the command. On the Mac, `--undo` restores the home identity settings; signing in to `~/.claude` again requires `/login`, as its output explains.

Historical `default`, `~/.claude` or `~/.codex` records resolve from their own login or a dated cutover entry in `config/accounts.json`, covering only records before that cutover and matching the tool. Without evidence they stay unresolved; today's login never names a past record. A resolved resume uses the numbered account's folder.

### Moving the Codex login

On each machine, stop every Codex process using `~/.codex`, including its long-lived app-server daemon, then run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py cutover-home codex2
```

The command verifies the account note, prepares the numbered home's shared links, and renames only `auth.json`; it never copies credentials. It then prints setup's wait line and starts Codex once on the numbered home, reporting done only after Codex answers `initialize`, which can take up to 20 minutes while the shared session index builds. If that startup fails, the login move stays complete and the command exits non-zero naming `accounts.py setup codex2`, which finishes the index. A live process on the source, or an unreadable process environment, refuses the move with its pid and the step to stop it. The private `cutover-codex-home.json` journal lets a rerun finish after an interruption. A completed rerun changes nothing. `cutover-home codex2 --undo` renames the login back after all processes on `~/.codex2` stop; bare `cutover-home --undo` still applies to Claude.

After this cutover on natedev and the Mac, `~/.codex` holds no login on either machine and remains the shared configuration and session store. Running the Codex executable without `CODEX_HOME` is signed out; the `codex` alias (`accounts.py exec workers`) still selects the mapped numbered home. Until cutover, status prints `codex2: legacy login is stored; ~/.codex still holds a login; run accounts.py cutover-home codex2`; afterwards that home has no account row.

## Move a session

In this terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <session-id-or-exact-name> <claudeN> [--dry-run] [--why "<text>"]
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move units|showrunners <claudeN> [--dry-run] [--why "<text>"]
```

A move stops the session's Claude process, then resumes the same conversation in its own herdr tab on the target account. It sends one resume message explaining why it moved and how to resume its Codex workers. Workers keep their threads; stopping Claude does not pause or stop them. Only tabs on this machine are in scope; run the same command there for a session on the other machine.

One result line per member names its resumed account and pane, or the failure. Before any stop, a move checks each member's folder trust on the target without writing. A member whose trust cannot be copied, or whose trust check cannot read its input, keeps running on its old account. Its result gives the reason and the command to accept trust. Other members move. Resume copies available trust before it starts Claude. The command returns the result directly. There is no move record, ready mark, follow or cancel step. `/showrunner:move` uses this same path and result.

A single session is found by id, then exact name. Workers and ambiguous or unknown sessions refuse. A session already on the target prints that fact and succeeds without stopping it. A grouped session names the command for its group. A group move selects its account for future launches, then moves every eligible live off-target member once; an empty group still selects its account. A group with every member already on the target succeeds without stopping anyone. Missing showrunner registrations are named with their repair command.

The target must pass home, sign-in and shutdown-hold checks before a group changes or any session stops. A fresh exhausted usage window without credits refuses. A missing or stale usage reading permits the move with a notice.

`--dry-run` changes nothing. It uses the lightweight session listing and prints each selected name/id, source account and planned target. It starts no stop or resume.

## Picking an account

Automatic moves choose an account with room. The ranking chooses the soonest weekly reset, then the most weekly allowance left, then the account label. It keeps the current account while it has room when the caller asks it to. `/showrunner:accounts placement --explain` shows each Claude account's readings and the reason it was chosen or skipped.

Claude accounts have room until they have an unresolved provider refusal. Without an account move entry, low and zero percentages do not move a group or remove room. Readings rank accounts: units use the soonest weekly reset, breaking ties by most weekly left; showrunners use most weekly left. A group with an account move entry waits for its head; refusal fallback runs only when that group has no entry. Completing off-target members onto its already-selected account still runs; if fallback finds no destination with room, the group keeps its account and the existing no-room notice names the earliest reset. Codex still uses fresh allowance and credits.

natedev holds the one role map for both machines: Claude groups in `~/.local/state/accounts/groups.json`, workers in `~/.claude/config/accounts.json`, and changes in `~/.local/state/accounts/roles.jsonl`. All readers and writers use `read_role_map()` / `write_role_map(change)`. On the Mac, writes run through `ssh natedev` and preserve the reason, cause and move id; failed writes refuse without changing local selections. Reads fetch the map once per command and save `~/.local/state/accounts/role_map_cache.json`. When natedev is unreachable, a cached map prints `role map from <time in the user's zone>; natedev unreachable`; without a usable cache the command refuses. No machine selects a substitute account.

A role mapped to an account missing on this machine refuses with `accounts: <role> is on <label>, which is not set up on this machine; <setup step>`. A signed-out account instead says `not logged in on this machine` and names its login step. Status prints those facts and the same command under the table in its subject, fault, cause and repair form. Set up and sign in the mapped account on each machine before launching that role.

`groups.json` stores explicitly selected group accounts. A manual group move, a refusal move or an account move entry writes it; selecting one group leaves the other unconfigured. Both groups may use the same account. Changes append `{at, group, label, why}` to `roles.jsonl`; historical burn skips these group entries. Launches, status, `accounts.py placement --explain` and the refusal watcher never initialize missing selections. Status and previews print `not configured`, and the watcher moves no member of an unconfigured group.

`exec unit`, `env unit` and `role unit` use the units account; `role showrunner` and new showrunner launches use the showrunners account. The legacy `unit` configuration key is ignored; `set-role unit` and `swap unit` refuse. Codex workers retain their own role.

New units and showrunners save `@claude_config_dir` and `@claude_model` on tmux. A resume uses its group's configured account, and accepts `--account` or `--config-dir` only when it names that account. Promoting a unit uses the configured showrunners account; recovery as a unit uses the configured units account.

## Adding an account

One command adds an account and checks every place that names accounts:

```text
/new_account claude claude4
/new_account codex codex3 <login>
```

Each step prints `done:` or `for you:`: the login, and the lines it cannot write itself (`~/.claude/settings.json`, the nixos shell aliases). Run it again after those; the run after a Claude login writes the account note from that login. Everything else finds accounts by their `~/.claudeN` or `~/.codexN` folder and their note.

## Setting up a new directory

For claude3, run inside a session:

```text
/showrunner:accounts setup claude3
```

Then start `claude3` in a terminal, run `/login` as claude3, and check `/showrunner:accounts`. Setup links shared content to `~/.claude`; it reports existing real files or directories rather than replacing them. Login stays separate for each directory.

## Setting up a Codex home

Create an account note with its login first (`~/rust/hanadocs/agents/codexN.md`). For codex1:

```text
/showrunner:accounts setup codex1
```

Setup creates `~/.codex1` with mode 0700 and links existing shared entries to `~/.codex`: configuration, hooks, instructions, skills, rules, plugins, sessions, archived sessions, the session index, history and the model cache. It reports real files or directories that prevent a link. Sign-in and Codex's home state stay separate; setup never links or copies `auth.json`.

Setup refuses without changing files or printing a sign-in command when another home already holds the account's login, because two homes on one login break token refresh.

Before printing the command, setup waits for Codex to finish indexing the shared sessions and answer its startup request. An interrupted first start can block later starts for 15 minutes, followed by the remaining index work; setup retries servers that exit unanswered and waits up to 20 minutes. If the limit is reached, rerun setup. A table row saying Codex did not start names the same repair command.

Run the printed command in a terminal and sign in as codex1:

```sh
CODEX_HOME=~/.codex1 codex login
```

Keep `CODEX_HOME` on that command so the login stays in the numbered home; workers always use their mapped account. Do not sign in to the shared `~/.codex` home.

Then run `/showrunner:accounts`. The new home's row shows `spare` and its own numbers until selected for workers. To check the home directly, use `/showrunner:accounts check codex1`. Problems under the table name missing, broken or replaced shared links, the wrong login, or a login held by another home; `--detail` also lists unknown entries. Keep each login in one home: two homes on one login break token refresh.

The repository's hook installer keeps a linked `hooks.json` and adds missing hooks to its target, so numbered homes continue to share `~/.codex`'s hooks.

## Swapping the Codex workers account

One account serves every unit's Codex workers, including the `codex exec` fallback. The updater, 5-hour watcher, `/whoami` and account table follow that role. Read it with `/showrunner:accounts role workers`.

```text
/showrunner:accounts swap workers codex1 --dry-run
/showrunner:accounts swap workers codex1
/showrunner:accounts swap workers codex2
```

A swap checks the target home, its sign-in, both usage windows and its credits before changing the role. It retires each run's current server and tells each affected unit director once. A running worker's turn is interrupted and the same thread resumes on the new account immediately, including when its launcher has gone. The continuation tells it to rerun any command stopped by the interruption. Its pass and the director's wait continue unchanged. A sign-in change also interrupts and resumes running workers on a fresh server. Held messages move with it. The move confirms that the old server released the thread before the new server resumes it. If Codex refuses an account-move resume, the swap reports the worker's failure and its manual resume command. An unreadable thread state or unconfirmed release starts no replacement turn.

A swap prints `Workers moving <source> → <target>:` and one showrunner · unit · worker → target line per worker. A worker that was mid-turn adds `, interrupted and resumed`. An interrupt or resume failure names that worker, the failed step and its manual resume command; other workers still move and the selected role stays changed. `--dry-run` uses `Workers would move` with the same worker lines and interrupts nothing, with no role or history write. `set-role workers codexN` selects future turns directly, without the swap's usage check or notices. Each authoritative role write preserves the other selections; `unit` and `workers` history share `~/.local/state/accounts/roles.jsonl`.

Swaps and sign-in changes leave ended sessions' workers alone, even when their launchers survive. A run stays live while any holder session is running, with or without a messaging socket; a run with no holder stays live only while a worker launcher is alive. Ended runs have no worker rows or holder notices. Their outgoing servers are checked for safe shutdown, and the output says `N leftover servers from ended sessions, M stopped`. Busy servers stay running and get a `left running: <pid> on port <port> (<run folder>): <reason>` line. A preview stops nothing and says `N leftover servers from ended sessions: M can be stopped, K would stay`, with a `would stay:` line for each busy server.

Worker rows name the holder session, or the session that launched the run when it has no holder marker. They fold by that session and slot, across plans and run folders. A folded row shows ` ×N`, followed by `, K interrupted and resumed` when K entries were mid-turn. Every failed entry retains its own row, reason and manual resume command.

Refusals leave both the role and history unchanged. A home-check failure names the file, link, login or duplicate sign-in to fix; rerun setup or correct that home's sign-in. An already-selected account needs no swap. An account used up with no credits needs a different target, credits or its reset. Unknown or expired usage needs a fresh reading before trying again. Selecting the numbered account held in `~/.codex` requires a real, signed-in home whose login no other home holds; its shared entries remain real files and directories.

## Automatic Codex moves

A workers next entry blocks provider-refusal and credits-off fallback moves. With no workers entry, a provider quota refusal moves workers to the picked Codex account with room; low quota above zero changes nothing, and weekly zero with `use_credits: off` also requests a move. A known 5-hour reset less than 20 minutes away waits once, with its reset time in the notice. An unknown window or reset, a passed reset, or exactly 20 minutes remaining moves instead. The picked home becomes the `workers` role through the `swap workers` path; running workers are interrupted and resumed on that account, continuing the refused work before held messages.

When no Codex account has room, assignments fall back to Claude and the notice names the earliest reset. A failed move to the picked home also falls back to Claude, with the failed move and cause in the notice. They return only when the selected Codex account can work and its refusal is resolved. Recorded refusals override saved percentages. The moves journal at `~/.local/state/accounts/moves.jsonl` separates a requested role change from each worker's confirmed resume, and records waits, failures and renewed room.

## Automatic group rules

The account moves log on natedev holds ordered, one-shot moves for Claude groups and Codex workers in `~/.local/state/accounts/next_moves.json`. Use `/account_moves` in ordinary words, or:

```sh
accounts.py next add units 1 claude4 --hold-minutes 30
accounts.py next add units 1 claude2 --hold-minutes 30
accounts.py next add showrunners 1 claude4 --hold-minutes 30
accounts.py next add showrunners 1 claude2 --hold-minutes 30
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

For units, showrunners and workers, the two-minute notes timer considers only the first entry per group; later entries wait until the head is recorded and removed, then become eligible on a later pass. Later Claude entries keep no hold until then. A due head that cannot run stays queued and tells showrunners once per changed reason (target already current, unreadable login, stale or missing reading, low target, unresolved refusal or refused preparation). A head targeting the current account blocks later entries too. The example holds units at 1% for 30 minutes, moves them to claude4, then holds again when claude4 reads low before moving them to claude2. Both groups may share an account; the Role cell then reads `units, showrunners`.

`--hold-minutes N` is finite and nonnegative; omitted or zero moves immediately. `--hold half-point` instead estimates the time to use another 0.5 percentage point from the selected account’s weighted pace history, including usage by other groups sharing that account. One point per hour gives 30 minutes, rounded up to whole minutes; missing, zero or negative pace uses 40 minutes. The list shows, for example, “showrunners 1: at 1% → claude2, hold half a point (≈30 min)” or “hold half a point (40 min default: no pace history)”. The Mac prints natedev’s rendered list using natedev’s selection and readings; `next-read` remains machine JSON.

The first fresh low reading starts an aware clock and stores its resolved duration and source with `account`, `at`, `move_id` and `percent` in `refusal_watch.json`’s `auto_move_holds`. Restarts and later pace readings leave that duration fixed. A fresh high reading clears it, while stale or missing readings leave it alone. A selection change (including away and back) or percent change clears the clock. Legacy holds without a duration resolve from their entry’s fixed minutes and retain their original start time. Legacy reset progress is ignored and dropped on the next state write.

Once started, the list shows those stored minutes and their source: “hold half a point (34 min at 0.9 points/hour, 12 min left)”, “hold half a point (40 min default: no pace history, 12 min left)” or “hold 30 min (12 min left)”. Time left rounds up to whole minutes; after expiry it reads “hold over” until the runner can move. Unstarted, stale, legacy or non-head entries show the current estimate instead: “hold half a point (≈30 min)”, the 40-minute default above, or “hold 30 min”. Both machines print natedev’s rows; the list after `next add` and the `next hold` reply use the same forms.

An empty list does not read watch state. If that state cannot be read or parsed, rows use their unstarted forms and end with one line: `started holds not shown: watch state unreadable`. This display fallback leaves the runner’s handling of corrupt state unchanged.

`next hold GROUP N half-point|MINUTES [--expect PERCENT TARGET]` changes only that entry’s hold mode, preserving its identity and position. A different mode clears its started hold under the watch and log locks before writing the list; the same mode keeps it, so retrying after a lost Mac reply preserves elapsed time. Other entries keep their holds. With `--expect`, a shifted entry refuses without changing either the log or holds. Always run `next list` immediately before a guarded update and copy its percent and target: listed percentages read back exactly to their stored values on both machines. On refusal re-list, and never recreate an already-consumed entry. Do not undo a mode change with another `next hold`: its cleared elapsed hold cannot be restored. Install natedev and the Mac together before writing any half-point entry. Workers refuse both hold modes.

Each refusal-watch pass first starts a quiet probe session on each local noted Claude account whose stored usage says its login expired. The probe has a 120 s limit. The pass reads usage again after a successful probe, then checks due moves. It tries each account at most once per hour and never starts a probe in a dry run. Probes run before the move log lock. If a probe fails, the blocked line says: "claude4's login expired, and a probe session on it failed (<detail>). Log in on claude4 by hand." Showrunners receive that line through the usual relay. Until a probe fails, status says the next notes pass renews the login. After a failure, status shows the probe time and asks for a manual login.

Account launches and renewal probes find Claude on PATH, then in `~/.local/bin`, and start its full path, including terminal relaunches; Codex account launches use the same search.

Entries require fresh readings (at most ten minutes old), a ready signed-in target with room above the head entry's threshold, and no unresolved target refusal. A group already moved this pass waits. A skipped or refused entry stays in the log. Recording the group choice consumes its entry under the log lock; recorded `auto: <id>` history prevents a second move if log removal is interrupted, and the next pass retries removal. If that history cannot be read or parsed, one diagnostic is printed and all log entries wait for a later pass; the rest of the watcher continues. Member retries keep that transition and reason. Each member stops and resumes on the selected account through the same move API, with one result line. `refusal_watch.py --dry-run` changes no homes, choices, log or hold state. The old `auto_moves` config key and `after_reset` rules are no longer used.

Workers entries target `codexN`, `claudeN` or bare `claude`, without `hold_minutes`. A numbered Claude target sets the account for registry worker launches on both machines while the route is open. The role map holds the target and the source weekly reset. At that reset, new launches use the default account, even if the quota state was lost. The Mac uses its last saved map when natedev is offline; a launch fails when no usable map exists. Bare `claude` keeps the caller or showrunners account. A missing or signed-out target gives its setup or login step. Subagents inside a Claude session use that session’s account. Only the workers head runs on natedev, before the zero policy. The selected account needs a fresh reading at or below the head's percentage; a Codex target needs a fresh reading above that threshold and no unresolved refusal. An unavailable target or refused swap keeps the head queued, blocks later entries and tells showrunners once per changed reason through the same blocked-head delivery as Claude groups. Successful moves record cause `auto` and reason `auto: <id>` in the move and role histories.

A workers move to Claude needs a known source weekly reset and holds its work routing on Claude until that reset, even if Codex has room earlier. Status detail shows it under `Codex work routing:` with cause `auto`. While any workers entry is queued, default, provider-refusal and credits-off moves wait. With no workers entry, weekly zero first moves workers to included Codex usage when it is available. With credits on and no included room, workers stay and spend their paid credits. With credits off, or after a refusal, workers use the Codex account with room whose weekly reset is soonest, else Claude.

Failed routing-journal appends retain the Claude hold and a saved retry. A refused workers swap saves its failure before attempting the journal append; pending rows must reach the journal before replay checks can consume entries.

Both engines reconcile recorded entry IDs before running entries. Unreadable or malformed replay history defers all entries for that engine and leaves the rest of the watcher running. An undecodable final line without a trailing newline is an interrupted append: readers skip it and log the file name. An undecodable line followed by another line still blocks replay checks. Journal writers append each complete encoded row with one write, retrying only a short write.

Install this version on both natedev and the Mac before adding the first workers entry: older readers reject a log containing workers entries. Verify rollout with a scratch home.

A move checks the target's account readiness and usage before selecting it or stopping a session. It does not read old shutdown holds. Stop or resume failures print their reason; they create no move record, follow command or cancellation step.

The notes pass on natedev checks units after account moves. It resumes a down unit after one pass of grace if the unit is not retired, has a worktree, and its run has not finished. A unit with no run record is standing by and can recover. It reads the production's Units table, live session marks and the unit's pane launch entry. A launch marked ended by a normal Claude exit is left closed. Unknown liveness leaves recovery evidence intact. The Mac does not run this check.

The watchdog submits each recovery to a detached user service and reads its result on a later notes pass. All submissions share a 30-second budget. A pending job prevents duplicate starts. The child checks that the pane launch has not changed, then calls `stop_resume.resume` for the same conversation on the launch entry's account, with the reason `unit watchdog recovery`. Shared resume opens a fresh tab when the old pane is gone. A failed resume returns its reason. Holds, shutdown records and recovery snapshots do not participate. Failed recovery tells the showrunner and sends the reviewed restart-failure push once per outage; failed delivery can retry. Alert claims remain in `~/.local/state/accounts/unit_outage_alerts.json`. A confirmed live observation clears only an older outage claim. Pending jobs, first-seen times and delivery status remain in `~/.local/state/accounts/unit_watchdog.json`; detached results stay beside it until the next pass reads them. Watchdog times use the user's configured time zone.


## Automatic Claude moves

Watcher inventories read the existing desktop snapshot. They never invoke the snapshot updater, which itself runs the notes timer. This prevents a recursive notes update and keeps `--dry-run` read-only.

The argument-free notes timer checks local top-level sessions' last transcript entries for provider quota refusals. A refused unit or showrunner moves its whole group only when that group has no next entry; otherwise its head controls the account move. Owners outside either group move alone through the same stop-and-resume path, in their own local herdr tabs. The Mac has no timer.

Group membership uses the lightweight listing; absent registration targets are checked separately. Manual move previews are described above.

A known 5-hour reset in less than 20 minutes waits once per session and reset. With a queued Claude group head, any known refusal reset waits and continues on the current account when it ends; an unknown reset waits for the head, and queued workers retain their brief reset wait. Without a pending next entry, other refusals move; no room sends one account notice until a usage reading changes. When a refusal requires a move, the watcher uses the same immediate stop-and-resume path as a manual move.

`scripts/accounts/refusal_watch.py --dry-run` reports decisions without changing anything. For a live proof, `--assume-refused '<session id or name>' weekly|five-hour [--reset-in <minutes>]` writes a refusal for that owner only and runs once. Only ungrouped probes are accepted. The proof changes neither the account refusal nor other sessions’ picks. The optional reset affects its wait decision, not stored account usage. A delivered quota-wait continuation deletes the assumption. Add `--proof-group <probe id>…` to move exactly those probes together, without touching real groups. The stop-and-resume API returns the resumed account and pane, or one failure reason.

Account status with `--detail` also shows live sessions' accounts, the Codex workers account and its destination on refusal, workers still owing a continuation, and the last ten move entries with outcomes and their `refusal`, `credits off`, `auto` or `by hand` cause. Hand moves never inherit a stored refusal. Group selections keep their transition and reason across member retries; member moves have no shutdown request ID. `Codex work routing:` lists assignment fallbacks separately, paired by their start time, as open, routed back, or not restored (partial or failed). Recovery is recorded only after all assignments are restored or kept because they were reassigned. The first line names both groups; table role cells show their selected accounts. A member still on its old account shows as `moving`.

## Low-quota alerts

When an in-use Claude account reaches 2% weekly and a group on it has no account move entry, the phone receives one reminder per episode: set up what happens next with natedev. Only entries targeting another account cover a group, and only while its replay history is readable. If entries cover every group on it, nothing is sent. Failed phone delivery retries; successful delivery never repeats until recovery or reset opens a new episode. Claude sends no session quota alert, restoration notice or instruction to stop starting work. `/quota_ack` is removed; `/quota_refresh` still refreshes readings. Codex also gets one phone reminder per low episode above 0% and at or below 2% when no workers entry targets another account and no scheduled Claude hold is open. At 0%, the default move is handled in the same pass and no reminder is sent. A reminder already delivered stays delivered across zero and subsequent readings until that weekly reset. An unreadable move journal makes coverage empty. Codex sends one rerun message to the unit director whose worker command stopped for quota, naming that command and asking for it to be rerun on Claude; an unknown director receives none. Waiting follows keep their continuation and receive no message. Zero, forecast, five-hour warnings and other refusal details are logged without session delivery; restoration is recorded only in the moves journal. Phone pushes and the natedev prompt notice remain.

Readings and refusals rank move destinations; they never replace a configured launch account. A launch with no configured group refuses without writing the group file.

## When a unit will not start

A showrunner's launch refuses with:

```text
add_unit: unit account <label>: <problem>; /showrunner:accounts
```

Run `/showrunner:accounts`, fix what it names (most often the account's alias and `/login` as that account), then launch again.

## Codex account numbers and credits

`codexN` names the login in its account note, rather than a folder number. Selecting `codex2` finds the existing home holding that note's login; it may be `~/.codex`. A home with no named note is shown by its folder. Both tools accept only numbered account names. Legacy stored `default` values read as numbers on their next normal write; historical move entries use evidence recorded at the time, or show `home account unresolved`. Setup still creates `~/.codexN` when no home holds that account.

Set `use_credits: on` or `use_credits: off` in each Codex note. Missing values mean on. With off, an exhausted weekly or 5-hour window makes the account unavailable even with credits. At weekly 0%, workers first move to another Codex account with room when no workers entry is queued. With credits on, workers move only to included usage, and that move sends no alert. When no account has included usage left, workers use paid credits, with one phone notice and hourly forecasts. Each pass checks for included usage again. A move to that usage closes the paid episode with no alert. With credits off, workers prefer included usage, then another account using its own credits. When neither is available or the move fails, Codex assignments move to Claude until the earliest refill. Only the first forecast goes to the phone. See `docs/quota_alerts.md` for delivery retries and unknown forecasts.

Account notes use numbered filenames such as `claude1.md` and `codex2.md`; their labels are `claude1` and `codex2`. The updater renames old filenames on its first pass, preserving bodies and modes. If both names exist it reports them and reads the numbered file. Each note carries a `role` list, a `sessions` count and a `## Live sessions` table from the roster on both machines. Live rows carry no timestamp. An unreachable or older peer keeps its last rows marked `last seen <time>`; these rows do not count in `sessions`. The updater records successful machine observations in `~/.local/state/agent-notes/sessions_seen.json`, outside the vault. A kept row retains its last-seen marker, or takes the machine's saved time, a legacy hidden observation comment, or `unknown`, in that order. Unread identities appear below the table as live sessions not attributed to an account. Legacy spaced labels remain accepted as input and are printed without spaces. Notes list by tool and account number.


A signed-in numbered home without a matching account note is never selectable. Status names the numbered folder and missing note; the corresponding `~/.codex` / `codexN.md` line remains for a Codex home. Add its numbered note before selecting it;
an unrelated numbered account remains usable.

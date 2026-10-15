# Claude and Codex accounts

## Which account am I on?

The status line's first row shows this session's account label, after its role. To print it:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/whoami/account.py
```

`/whoami` shows Claude accounts in use used by the login in `~/.claude` or local live sessions, omitting unused Claude accounts. It also shows every Codex home; with multiple homes, the home in use is marked `(workers)`. It does not identify this session's account.

## Every account in one table

`/showrunner:accounts` prints one table: a row for `~/.claude` and each `~/.claudeN`, logged in or not, then a row for `~/.codex` and each `~/.codexN`. Noted accounts whose home is absent or blocked by a file or broken link, and whose login no home holds, get `not set up` rows. In a terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status
```

| Column | Shows |
| --- | --- |
| Label | The account's label. A named Claude folder keeps its own label; a Codex home shows the label of its login's note. |
| Login | The login, `not logged in`, or `not set up` for an account that has a note and is in no folder. |
| Role | `units` and/or `showrunners` for the selected Claude groups; `workers` for the Codex home in use, `spare` for the other Codex homes. |
| Weekly left | Percent of the weekly window left, with its reset time. A dash when the account has no weekly window. |
| On credits | Codex only: `yes` when a window is used up and the account's credits now pay for the work. |
| Credits left | Codex only: the credit balance, or `unlimited`. |
| Read | When those numbers were read. |

Each Codex account can stay signed in in its own home. The table's `workers` role follows `workers` in `~/.claude/config/accounts.json`, defaulting to `~/.codex`. A caller's `CODEX_HOME` does not change the `workers` role. Signed-in spares get usage readings too; their notes stay inactive and raise no quota alerts. Freestanding Codex sessions keep their own process home.

Times are in the user's zone setting (`config/user_zone.conf`). The numbers are each account's last good reading: the usage service refuses many reads, so the two-minute updater saves each one it gets, and an account with none in the last 10 minutes is read once more. `unavailable` means no reading is held for a window that is still open. Lines under the table name anything wrong or unusual in a folder: a failed check, unclassified entries, MCP servers that differ from `~/.claude`.

## Starting a session

Run these in a terminal:

| Command | Account |
| --- | --- |
| `claude` | `~/.claude` (today claudeN, the number of its login’s note). |
| `claude-unit` | The configured units account. |
| `claude1` | claude1 |
| `claude2` | claude2 |
| `claude3` | claude3 |

These aliases come from natedev's shell configuration after a rebuild; the Mac has none. In a shell without them, run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude3
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec unit
```

Arguments after the label pass through to `claude`.

Configuration decides the account every launch starts on: new, standby and resumed units and nightly reviews use the units account; new and resumed showrunners use the showrunners account. Set a group with `accounts.py move units|showrunners claudeN`. An unconfigured group refuses with that command; an `--account` or `--config-dir` override must name its configured account. Each unit launch saves its config directory and model on tmux for observation; that directory no longer chooses a resume account. A numbered selection whose login matches `~/.claude` runs there, with `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_REMOTE_MEMORY_DIR` removed. The same launch removes any inherited OAuth token. Commands accept account numbers only; `default` refuses and reports each home’s current number.

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

To switch `~/.claude`'s account, start `claude`, then run `/login`, as today. Do not do this in a `claude-unit` or `claudeN` session.

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

## Move a session

In this terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <session-id-or-exact-name> <target> [--dry-run] [--why "<text>"]
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move units|showrunners <target> [--dry-run] [--why "<text>"]
```

The first form moves one local ungrouped session, found by id, else by exact name. The second selects the group's account for future launches and moves every live member not already on it. An empty group still selects its account (`<group>: no live session moves`); an open shutdown receives no duplicate request. A session can request its own move, then mark ready and end its turn.

### Preview

`--dry-run` writes nothing, for a group or a single session. Selection reads shutdown's lightweight listing (`ListedSession`), without worktrees, restart resources or desktop inventory; each member's line reads only its newest transcript exchange and the stored usage reading. A warm preview takes about 0.3 to 0.4 s on natedev. A group preview first prints `<group>: would select <target>`, then one line per member:

```text
<name>: <session id> · pause, then move (<source> has room)
<name>: <session id> · reap and restart now (<source> out of usage: weekly limit, resets Sat 9:00 AM)
```

A source with no fresh reading prints `pause, then move (<source> no fresh reading)`. A paused member is asked to stop at a safe point and mark ready; a member whose transcript ends in a refusal, or whose source account has a fresh reading at 0%, is reaped and restarted at once with no pause message. Process identity checks still precede every signal. Showrunner previews read the update timers once in-process, sharing that snapshot with the registration check without resolving messaging sockets, and name each production whose timer's target session is absent, with the full repair command to run inside that showrunner's own session.

### Target check and refusals

Before any group change or shutdown write, the target must pass its home and sign-in checks. A fresh reading with no credits and a window at 0 refuses:

```text
accounts: <target> is out of usage (weekly 0% left, resets Sat 8:59 AM); pick another: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py pick claude --exclude <target> in this terminal
```

A stale or missing reading permits the move and prints `no fresh usage reading for <target> (last <n> minutes ago)`. A missing or ambiguous local session, a worker, a grouped session (the refusal names `accounts.py move <group> <target>`), a session already on the target, and an unreadable account or shutdown state also refuse. A session on the other machine moves from there. Workers move with their unit director.

### Following the move

The move hands the selected ids to one `shutdown.py down --only` per source login, then prints the move-back line:

```text
to move them back, in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <subject> <account>
```

A group's line names the account it held before the move; with none stored, it names the common source of the moved members, and mixed sources print no line. A single move names that session and its source account.

Run from a terminal (`CLAUDE_CODE_SESSION_ID` unset, stdout a tty), the move then follows each request in turn, printing `<HH:MM:SS> <name>: <kind> <detail>` for every step (`reaping`, `waiting`, `ready`, `stopped`, `restarted`). It ends on `all N back on <target>`; a summary containing `taken out` counts cancelled members as done. A needs-user line gives the next command and where to run it; following continues through the remaining source requests and exits 1 if any needs help. Ctrl-C detaches with a follow command while the shutdown continues (exit 130). Run from a Claude session or with redirected output, the move prints the follow and cancel commands instead. The showrunner wrapper (`/showrunner:move`) keeps the missing-reading notice and the move-back line beside its process proof.

Each return keeps the conversation, cwd, name, model and production resume prompt, including a unit director's ongoing run. A manual restart outcome supplies its command and place. The target is checked again at launch; its prepared environment drops inherited OAuth tokens without changing credentials or other sessions.

Numbered Claude aliases through `accounts.py exec` use the terminal supervisor when stdin is a tty. Its child gets `RELAUNCH_SUPERVISED=1`; a resumed `accounts.py exec` sees that marker and execs Claude directly. Redirected and already supervised launches also exec Claude directly. Tmux respawns its existing pane. Supervised Ghostty and Terminal restarts keep the same window and scroll history; the existing fallback opens a new window if the supervisor is gone or its identity changed. Legacy Ghostty and Zed windows use the recorded desktop restart path.

## Picking an account

`/showrunner:accounts pick claude --explain` shows each candidate's weekly and 5-hour readings, its reset, and why it was picked or skipped. Use `pick codex` for Codex, repeat `--exclude "<label>"` to omit accounts, or use `--keep "<label>"` to retain a session's account while it has room.

Claude accounts have room until they have an unresolved provider refusal. Low and zero percentages do not move a group or remove room. Readings rank accounts: units use the soonest weekly reset, breaking ties by most weekly left; showrunners use most weekly left. Only a refusal moves the group. If no destination has room, the group keeps its account and the existing no-room notice names the earliest reset. Codex still uses fresh allowance and credits.

`~/.local/state/accounts/groups.json` stores explicitly selected group accounts. Only a manual group move or a move after a refusal writes it; selecting one group leaves the other unconfigured. Both groups may use the same account. Changes append `{at, group, label, why}` to `roles.jsonl`; historical burn skips these group entries. Launches, status, `accounts.py placement --explain` and the refusal watcher never initialize missing selections. Status and previews print `not configured`, and the watcher moves no member of an unconfigured group.

`claude-unit`, `exec unit`, `env unit` and `role unit` use the units account; `role showrunner` and new showrunner launches use the showrunners account. The legacy `unit` configuration key is ignored; `set-role unit` and `swap unit` refuse. Codex workers retain their own role.

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

Keep `CODEX_HOME` on that command. Without it, `codex login` signs `~/.codex` out and in, leaving its workers unable to work until login finishes; workers use that home unless their role names another.

Then run `/showrunner:accounts`. The new home's row shows `spare` and its own numbers until selected for workers. To check the home directly, use `/showrunner:accounts check codex1`. Problems under the table name missing, broken or replaced shared links, unclassified entries, the wrong login, or a login held by another home. Keep each login in one home: two homes on one login break token refresh.

The repository's hook installer keeps a linked `hooks.json` and adds missing hooks to its target, so numbered homes continue to share `~/.codex`'s hooks.

## Swapping the Codex workers account

One account serves every unit's Codex workers, including the `codex exec` fallback. The updater, 5-hour watcher, `/whoami` and account table follow that role. Read it with `/showrunner:accounts role workers`.

```text
/showrunner:accounts swap workers codex1 --dry-run
/showrunner:accounts swap workers codex1
/showrunner:accounts swap workers codex2
```

A swap checks the target home, its sign-in, both usage windows and its credits before changing the role. It retires each run's current server and tells each affected unit director once. A working worker finishes its turn on the outgoing home; its next turn runs under the new one. Held messages move with it. The move confirms that the old server released the thread before the new server resumes it. If Codex refuses that resume, the worker starts a new thread and its board line names the replacement. An unreadable thread state or unconfirmed release starts no replacement turn.

`--dry-run` prints the target account, its home and the runs that would move, with no role or history write. `set-role workers codexN` selects future turns directly, without the swap's usage check or notices. Each role write keeps the other role; `unit` and `workers` history share `~/.local/state/accounts/roles.jsonl`.

Refusals leave both the role and history unchanged. A home-check failure names the file, link, login or duplicate sign-in to fix; rerun setup or correct that home's sign-in. An already-selected account needs no swap. An account used up with no credits needs a different target, credits or its reset. Unknown or expired usage needs a fresh reading before trying again. Selecting the numbered account held in `~/.codex` requires a real, signed-in home whose login no other home holds; its shared entries remain real files and directories.

## Automatic Codex moves

A provider quota refusal moves workers to the picked Codex account with room. Above zero, low quota alone changes nothing; weekly zero with `use_credits: off` also requests a move. A known 5-hour reset less than 20 minutes away waits once, with its reset time in the notice. An unknown window or reset, a passed reset, or exactly 20 minutes remaining moves instead. The picked home becomes the `workers` role through the `swap workers` path; each worker moves after its live turn ends and continues the refused work before held messages.

When no Codex account has room, assignments fall back to Claude and the notice names the earliest reset. A failed move to the picked home also falls back to Claude, with the failed move and cause in the notice. They return only when the selected Codex account can work and its refusal is resolved. Recorded refusals override saved percentages. The moves journal at `~/.local/state/accounts/moves.jsonl` separates a requested role change from each worker's confirmed resume, and records waits, failures and renewed room.

## Automatic Claude moves

Watcher inventories read the existing desktop snapshot. They never invoke the snapshot updater, which itself runs the notes timer. The same policy reaches their shutdown conductor, preventing a recursive notes update and keeping `--dry-run` read-only.

The argument-free notes timer checks local top-level sessions' last transcript entries for provider quota refusals. A refused unit or showrunner moves its whole group. Owners outside either group move alone through the selected shutdown and restart path, across terminal, tmux, Ghostty and zed hosts. An open shutdown request stays with its conductor. The Mac has no timer.

Group membership uses the lightweight listing; absent registration targets are checked separately. Manual move previews and terminal following are described above.

A known 5-hour reset in less than 20 minutes waits once per session and reset. Other refusals move; no room sends one account notice until a usage reading changes. Shutdown waits for an idle owner, idle workers, no merge and nothing ahead of upstream. Its status line keeps the target and refusal reason visible while blocked. Cancellation records a failed move, removes the assumption and suppresses the unchanged transcript refusal.

`scripts/accounts/refusal_watch.py --dry-run` reports decisions without changing anything. For a live proof, `--assume-refused '<session id or name>' weekly|five-hour [--reset-in <minutes>]` writes a refusal for that owner only and runs once. Only ungrouped probes are accepted. The proof changes neither the account refusal nor other sessions’ picks. The optional reset affects its wait decision, not stored account usage. Restart, cancellation or a delivered continuation deletes the assumption. Add `--proof-group <probe id>…` to move exactly those probes together, without touching real groups. A process still on the source is a failed move; a target process with pending timers is confirmed while those timers stay visible.

Account status with `--detail` also shows live sessions' accounts, the Codex workers account and its destination on refusal, workers still owing a continuation, and the last ten move entries with outcomes and their `refusal`, `credits off` or `by hand` cause. Hand moves never inherit a stored refusal. Group selections persist one transition ID and origin across member retries, while each shutdown attempt keeps its own request ID. `Codex work routing:` lists assignment fallbacks separately, paired by their start time, as open, routed back, or not restored (partial or failed). Recovery is recorded only after all assignments are restored or kept because they were reassigned. The first line names both groups; table role cells show their selected accounts. A member still on its old account shows as `moving`.

## Low-quota alerts

Claude alerts forecast destinations separately: units use the soonest weekly reset with room, showrunners use the most weekly allowance left, and ungrouped sessions use the Claude picker. A no-room forecast names the earliest reset. A low reading alone moves no group.

Readings and refusals rank move destinations; they never replace a configured launch account. A launch with no configured group refuses without writing the group file.

Acknowledging an account's low-quota alert keeps it quiet across swaps. Made spare and then selected again, it stays quiet until its own quota recovers or resets.

## When a unit will not start

A showrunner's launch refuses with:

```text
add_unit: unit account <label>: <problem>; /showrunner:accounts
```

Run `/showrunner:accounts`, fix what it names (most often the account's alias and `/login` as that account), then launch again.

## Codex account numbers and credits

`codexN` names the login in its account note, rather than a folder number. Selecting `codex2` finds the existing home holding that note's login; it may be `~/.codex`. A home with no named note is shown by its folder. Both tools accept only numbered account names. Legacy stored `default` values read as numbers on their next normal write; historical move entries use evidence recorded at the time, or show `home account unresolved`. Setup still creates `~/.codexN` when no home holds that account.

Set `use_credits: on` or `use_credits: off` in each Codex note. Missing values mean on. With off, an exhausted weekly or 5-hour window makes the account unavailable even with credits. At weekly 0%, on keeps workers on paid credits with one phone and showrunner notice and hourly forecasts; off prefers an account with weekly usage left, then an account using its own credits. When neither is available or the move fails, Codex assignments move to Claude until the earliest refill. Only the first forecast goes to the phone. See `docs/quota_alerts.md` for delivery retries and unknown forecasts.

Account notes use numbered filenames such as `claude1.md` and `codex2.md`; their labels are `claude1` and `codex2`. The updater renames old filenames on its first pass, preserving bodies and modes. If both names exist it reports them and reads the numbered file. Each note carries a `role` list, a `sessions` count and a `## Live sessions` table from the roster on both machines. Live rows carry no timestamp. An unreachable or older peer keeps its last rows marked `last seen <time>`; these rows do not count in `sessions`. The updater records successful machine observations in `~/.local/state/agent-notes/sessions_seen.json`, outside the vault. A kept row retains its last-seen marker, or takes the machine's saved time, a legacy hidden observation comment, or `unknown`, in that order. Unread identities appear below the table as live sessions not attributed to an account. Legacy spaced labels remain accepted as input and are printed without spaces. Notes list by tool and account number.


A signed-in home without a matching account note is never selectable. Status prints
`~/.claude — no account note, not selectable: add ~/rust/hanadocs/agents/claudeN.md with login: <login>`
or the corresponding `~/.codex` / `codexN.md` line. Add its numbered note before selecting it;
an unrelated numbered account remains usable.

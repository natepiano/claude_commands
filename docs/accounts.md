# Claude and Codex accounts

## Which account am I on?

The status line's second row shows this session's account label. To print it:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/whoami/account.py
```

`/whoami` shows Claude accounts in use used by the default login or local live sessions, omitting unused Claude accounts. It also shows every Codex home; with multiple homes, the home in use is marked `(seats)`. It does not identify this session's account.

## Every account in one table

`/showrunner:accounts` prints one table: a row for `~/.claude` and each `~/.claudeN`, logged in or not, then a row for `~/.codex` and each `~/.codexN`. Noted accounts whose home is absent or blocked by a file or broken link, and whose login no home holds, get `not set up` rows. In a terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status
```

| Column | Shows |
| --- | --- |
| Label | The account's label. A named Claude folder keeps its own label; a Codex home shows the label of its login's note. |
| Login | The login, `not logged in`, or `not set up` for an account that has a note and is in no folder. |
| Role | `units` and/or `showrunners` for the selected Claude groups; `seats` for the Codex home in use, `spare` for the other Codex homes. |
| 5-hour left, Weekly left | Percent of each window left, each with its reset time. A dash when the account has no such window. |
| On credits | Codex only: `yes` when a window is used up and the account's credits now pay for the work. |
| Credits left | Codex only: the credit balance, or `unlimited`. |
| Read | When those numbers were read. |

Each Codex account can stay signed in in its own home. The table's `seats` role follows `seats` in `~/.claude/config/accounts.json`, defaulting to `~/.codex`. A caller's `CODEX_HOME` does not change the `seats` role. Signed-in spares get usage readings too; their notes stay inactive and raise no quota alerts. Freestanding Codex sessions keep their own process home.

Times are in the user's zone setting (`config/user_zone.conf`). The numbers are each account's last good reading: the usage service refuses many reads, so the two-minute updater saves each one it gets, and an account with none in the last 10 minutes is read once more. `unavailable` means no reading is held for a window that is still open. Lines under the table name anything wrong or unusual in a folder: a failed check, unclassified entries, MCP servers that differ from `~/.claude`.

## Starting a session

Run these in a terminal:

| Command | Account |
| --- | --- |
| `claude` | Default (`~/.claude`). |
| `claude-unit` | The configured units account. |
| `claude1` | claude 1 |
| `claude2` | claude 2 |
| `claude3` | claude 3 |

These aliases come from natedev's shell configuration after a rebuild; the Mac has none. In a shell without them, run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 3'
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec unit
```

Arguments after the label pass through to `claude`.

Configuration decides the account every launch starts on: new, standby and resumed units and nightly reviews use the units account; new and resumed showrunners use the showrunners account. Set a group with `accounts.py move units|showrunners "claude N"`. An unconfigured group refuses with that command; an `--account` or `--config-dir` override must name its configured account. Each unit launch saves its config directory and model on tmux for observation; that directory no longer chooses a resume account. A named alias whose login matches the default runs on `~/.claude`, with `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_REMOTE_MEMORY_DIR` removed. `exec default` starts plain `claude`, removing those variables and any inherited OAuth token.

A missing directory refuses without starting Claude, for example:

```text
accounts: ~/.claude3 is not set up; /showrunner:accounts setup "claude 3" first
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

To renew or fix a named account, start its alias, then `/login` as that account. For claude 3:

```sh
claude3
```

```text
/login
```

If the directory holds another login, the alias refuses with:

```text
accounts: ~/.claude3 is logged in as <login>, not claude 3; see Log out and in in ~/.claude/docs/accounts.md
```

Start that directory directly, then log out and log in as claude 3. `env -u` drops an inherited `CLAUDE_CODE_OAUTH_TOKEN`, which would otherwise override the directory's login:

```sh
env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=~/.claude3 claude
```

```text
/logout
/login
```

Never `/logout` in a unit director's session: every unit on that account stops working. After any login, run `/showrunner:accounts`; every directory must match its label.

## Move a session

Move a showrunner, unit director or top-level session from the machine where it runs:

```text
/showrunner:accounts move <session-id-or-exact-name> "claude 3"
/showrunner:accounts move <session-id-or-exact-name> default
```

In a terminal, use `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <session-id-or-exact-name> 'claude 3'`. A session can move itself. The request names its requester and stops through the normal shutdown readiness checks. It keeps its conversation, name, model and Remote Control name. SendMessage and `send.py` still reach it; Remote Control registers on the target account and leaves the old account's phone app.

Before changing anything, move refuses when:

- No live session has that id or exact name here; move a session on the other machine from there.
- Two live sessions share the name; their ids are listed so you can select one.
- The session is a worker: `seats move with their unit director`.
- The target fails `check`; its first problem is printed.
- Its login is already the target: `<name> is already on <label>`.
- A target label names the default login, or its directory holds that login: `<label> is the default account; use default`.
- That session has an open shutdown: `<name> already has a shutdown in progress`. An unrelated session's shutdown does not refuse.
- The current account or shutdown state cannot be read safely.

Move runs `shutdown.py down '<current note label>' --here --only <id> --restart --to '<target>'`. A tmux session respawns its pane; a unit resumes through `add_unit.py --resume --account '<target>'`; Ghostty, Zed and Terminal reopen a window. Use `--why "<text>"` to explain the move; the default is `<old label> is out of usage`. Every resume says it was an account move, not a stop, and continues where it stopped; a unit director continues its `/unit:direct` run. An unknown host prints exactly one resume command with `run this once <name> has exited` before starting shutdown. Run it after exit; restart reports the same command as `manual restart <name>: <line>`.

The target is checked again at launch. A moved launch uses `accounts.py exec '<target>'`, which drops inherited OAuth tokens. Moving changes no login or credentials and leaves other sessions in place. Grouped sessions move with their group; ad hoc sessions can move alone.

Move a whole group with `/showrunner:accounts move units "claude 3"` or `move showrunners "claude 3"`. The terminal command is `accounts.py move units|showrunners '<target>' [--dry-run] [--why '<text>']`. It checks the target, selects it for new launches, and requests each live member still off that account. An open shutdown record receives no second request; the notes timer completes remaining members on later ticks. Moving one grouped session refuses and names the group command. A dry run prints `<group>: would select <target>` and each off-target live name and session id, or `<group>: no live session moves`; it changes nothing. An empty group still selects its account for future launches. Showrunner moves and previews name productions whose update timer points at no live session and give the registration command.

Group requests use shutdown’s pause-for-move path: each member receives `Pause for move`, writes its checkpoint with `shutdown.py ready --where`, and its unit showrunner is told before it stops. Shutdown’s existing exceptions apply: an idle owner held by a usage limit can stop without a mark; the requester sends its own ready mark. The restart prompt carries the checkpoint. Each returns to its own conversation, cwd, name, model and production resume prompt. Tmux respawns its existing pane and keeps the attached desktop window. For a direct Ghostty showrunner, shutdown closes its window as usual and restart opens a replacement on the recorded desktop. The batch command also accepts `--why "<text>"`; each resume carries the same account-move explanation.

## Picking an account

`/showrunner:accounts pick claude --explain` shows each candidate's weekly and 5-hour readings, its reset, and why it was picked or skipped. Use `pick codex` for Codex, repeat `--exclude "<label>"` to omit accounts, or use `--keep "<label>"` to retain a session's account while it has room.

Claude accounts have room until they have an unresolved provider refusal. Low and zero percentages do not move a group or remove room. Readings rank accounts: units use the soonest weekly reset, breaking ties by most weekly left; showrunners use most weekly left. Only a refusal moves the group. If no destination has room, the group keeps its account and the existing no-room notice names the earliest reset. Codex still uses fresh allowance and credits.

`~/.local/state/accounts/groups.json` stores explicitly selected group accounts. Only a manual group move or a move after a refusal writes it; selecting one group leaves the other unconfigured. Both groups may use the same account. Changes append `{at, group, label, why}` to `roles.jsonl`; historical burn skips these group entries. Launches, status, `accounts.py placement --explain` and the refusal watcher never initialize missing selections. Status and previews print `not configured`, and the watcher moves no member of an unconfigured group.

`claude-unit`, `exec unit`, `env unit` and `role unit` use the units account; `role showrunner` and new showrunner launches use the showrunners account. The legacy `unit` configuration key is ignored; `set-role unit` and `swap unit` refuse. Codex workers retain their own role.

New units and showrunners save `@claude_config_dir` and `@claude_model` on tmux. A resume uses its group's configured account, and accepts `--account` or `--config-dir` only when it names that account. Promoting a unit uses the configured showrunners account; recovery as a unit uses the configured units account.

## Setting up a new directory

For claude 3, run inside a session:

```text
/showrunner:accounts setup "claude 3"
```

Then start `claude3` in a terminal, run `/login` as claude 3, and check `/showrunner:accounts`. Setup links shared content to `~/.claude`; it reports existing real files or directories rather than replacing them. Login stays separate for each directory.

## Setting up a Codex home

Create an account note with its login first (`~/rust/hanadocs/agents/codex N.md`). For codex 1:

```text
/showrunner:accounts setup "codex 1"
```

Setup creates `~/.codex1` with mode 0700 and links existing shared entries to `~/.codex`: configuration, hooks, instructions, skills, rules, plugins, sessions, archived sessions, the session index, history and the model cache. It reports real files or directories that prevent a link. Sign-in and Codex's home state stay separate; setup never links or copies `auth.json`.

Setup refuses without changing files or printing a sign-in command when another home already holds the account's login, because two homes on one login break token refresh.

Before printing the command, setup waits for Codex to finish indexing the shared sessions and answer its startup request. An interrupted first start can block later starts for 15 minutes, followed by the remaining index work; setup retries servers that exit unanswered and waits up to 20 minutes. If the limit is reached, rerun setup. A table row saying Codex did not start names the same repair command.

Run the printed command in a terminal and sign in as codex 1:

```sh
CODEX_HOME=~/.codex1 codex login
```

Keep `CODEX_HOME` on that command. Without it, `codex login` signs `~/.codex` out and in, leaving its workers unable to work until login finishes; workers use that default home unless their role names another.

Then run `/showrunner:accounts`. The new home's row shows `spare` and its own numbers until selected for workers. To check the home directly, use `/showrunner:accounts check "codex 1"`. Problems under the table name missing, broken or replaced shared links, unclassified entries, the wrong login, or a login held by another home. Keep each login in one home: two homes on one login break token refresh.

The repository's hook installer keeps a linked `hooks.json` and adds missing hooks to its target, so numbered homes continue to share the default home's hooks.

## Swapping the Codex workers account

One account serves every unit's Codex workers, including the `codex exec` fallback. The updater, 5-hour watcher, `/whoami` and account table follow that role. Read it with `/showrunner:accounts role seats`.

```text
/showrunner:accounts swap seats "codex 1" --dry-run
/showrunner:accounts swap seats "codex 1"
/showrunner:accounts swap seats default
```

A swap checks the target home, its sign-in, both usage windows and its credits before changing the role. It retires each run's current server and tells each affected unit director once. A working worker finishes its turn on the outgoing home; its next turn runs under the new one. Held messages move with it. The move confirms that the old server released the thread before the new server resumes it. If Codex refuses that resume, the worker starts a new thread and its board line names the replacement. An unreadable thread state or unconfirmed release starts no replacement turn.

`--dry-run` prints the target account, its home and the runs that would move, with no role or history write. `set-role seats "codex N"` (or `default`) selects future turns directly, without the swap's usage check or notices. Each role write keeps the other role; `unit` and `seats` history share `~/.local/state/accounts/roles.jsonl`.

Refusals leave both the role and history unchanged. A home-check failure names the file, link, login or duplicate sign-in to fix; rerun setup or correct that home's sign-in. An already-selected account needs no swap. An account used up with no credits needs a different target, credits or its reset. Unknown or expired usage needs a fresh reading before trying again. Selecting `default` requires a real, signed-in `~/.codex` whose login no other home holds; its shared entries remain real files and directories.

## Automatic Codex moves

A provider quota refusal moves workers to the picked Codex account with room; low quota alone changes nothing. A known 5-hour reset less than 20 minutes away waits once, with its reset time in the notice. An unknown window or reset, a passed reset, or exactly 20 minutes remaining moves instead. The picked home becomes the `seats` role through the `swap seats` path; each worker moves after its live turn ends and continues the refused work before held messages.

When no Codex account has room, assignments fall back to Claude and the notice names the earliest reset. A failed move to the picked home also falls back to Claude, with the failed move and cause in the notice. They return only when the selected Codex account can work and its refusal is resolved. Recorded refusals override saved percentages. The moves journal at `~/.local/state/accounts/moves.jsonl` separates a requested role change from each worker's confirmed resume, and records waits, failures and renewed room.

## Automatic Claude moves

Watcher inventories read the existing desktop snapshot. They never invoke the snapshot updater, which itself runs the notes timer. The same policy reaches their shutdown conductor, preventing a recursive notes update and keeping `--dry-run` read-only.

The argument-free notes timer checks local top-level sessions' last transcript entries for provider quota refusals. A refused unit or showrunner moves its whole group. Owners outside either group move alone through the selected shutdown and restart path, across terminal, tmux, Ghostty and zed hosts. An open shutdown request stays with its conductor. The Mac has no timer.

Group membership comes from shutdown inventory's `unit` and `showrunner` classifications. An update timer is not required for a classified showrunner to move with its group.

A known 5-hour reset in less than 20 minutes waits once per session and reset. Other refusals move; no room sends one account notice until a usage reading changes. Shutdown waits for an idle owner, idle workers, no merge and nothing ahead of upstream. Its status line keeps the target and refusal reason visible while blocked. Cancellation records a failed move, removes the assumption and suppresses the unchanged transcript refusal.

`scripts/accounts/refusal_watch.py --dry-run` reports decisions without changing anything. For a live proof, `--assume-refused '<session id or name>' weekly|five-hour [--reset-in <minutes>]` writes a refusal for that owner only and runs once. The optional reset affects its wait decision, not stored account usage. A confirmed restart or cancellation deletes the assumption. A process still on the source is a failed move; a target process with pending timers is confirmed while those timers stay visible.

Account status shows live sessions' accounts, the Codex workers account and its destination on refusal, workers still owing a continuation, and the last ten move entries with outcomes. The first line names both groups; table role cells show their selected accounts. A member still on its old account shows as `moving`.

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

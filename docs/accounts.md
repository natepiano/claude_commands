# Claude accounts

## Which account am I on?

The status line's second row shows this session's account label. To print it:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/whoami/account.py
```

`/whoami` shows each account in use with its role (`showrunners`, `unit directors`), omitting spares. It does not identify this session's account.

## Every account in one table

`/showrunner:accounts` prints one table: a row for `~/.claude` and each `~/.claudeN`, logged in or not, then a row for each Codex account. In a terminal:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status
```

| Column | Shows |
| --- | --- |
| Label | The account's label. A named folder keeps its own label whatever login it holds. |
| Login | The login, `not logged in`, or `not set up` for an account that has a note and is in no folder. |
| Role | `showrunners`, `unit directors`, or `spare`; `seats` for the Codex account. |
| 5-hour left, Weekly left | Percent of each window left, each with its reset time. A dash when the account has no such window. |
| On credits | Codex only: `yes` when a window is used up and the account's credits now pay for the work. |
| Credits left | Codex only: the credit balance, or `unlimited`. |
| Read | When those numbers were read. |

Codex has one home, `~/.codex`, so one Codex account is signed in at a time. Each other Codex account that has a note gets a `not logged in` row with no numbers.

Times are in the running production's User zone, or this machine's zone when no production runs. The numbers are each account's last good reading: the usage service refuses many reads, so the two-minute updater saves each one it gets, and an account with none in the last 10 minutes is read once more. `unavailable` means no reading is held for a window that is still open. Lines under the table name anything wrong or unusual in a folder: a failed check, unclassified entries, MCP servers that differ from `~/.claude`.

## Starting a session

Run these in a terminal:

| Command | Account |
| --- | --- |
| `claude` | Default (`~/.claude`): showrunners and your own work. |
| `claude-unit` | The current unit account; the default when no unit role is set. |
| `claude1` | claude 1 |
| `claude2` | claude 2 |
| `claude3` | claude 3 |

These aliases come from natedev's shell configuration after a rebuild; the Mac has none. In a shell without them, run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 3'
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec unit
```

Arguments after the label pass through to `claude`.

Units started by showrunners pick their account themselves. A named alias whose login matches the default runs on `~/.claude`, with `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_REMOTE_MEMORY_DIR` removed. Today that is `claude2`; other named aliases use their own `~/.claudeN` directory. Plain `claude` is unchanged.

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

## Swapping the unit account

The unit account runs the unit directors and the nightly review. A swap moves both: the next night's review starts on the new account.

The review cannot edit any account folder: its settings deny edits under `~/.claude1`, `~/.claude2` and `~/.claude3` as well as `~/.claude`. A new account folder needs its own line in `scripts/nightly_review/settings.json`.

Run the swap from a session on another account, normally plain `claude`, or from a terminal. This example moves units from claude 3 to claude 1:

```text
/showrunner:accounts swap unit "claude 1"
```

The terminal equivalent is:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py swap unit "claude 1"
```

It shows shutdown's output unchanged, followed by `units come back on claude 1 when claude 3 is down`. This confirms acceptance; shutdown and restart may still be running. Watch `/shutdown status`, then check the resumed units' account labels. After a complete stop, the requester gets:

```text
The sessions restart automatically: units on the unit account, other sessions on the account they ran on.
```

Before changing the role, swap refuses and names the cause. Reported values replace the angle brackets below:

```text
accounts: current unit account identity is unreadable
accounts: default account identity is unreadable
accounts: <label> is the default account; use default
accounts: <note label> is already the unit account
accounts: cannot swap <old>: <machine> <kind> <name> would not move
accounts: cannot swap <old>: <machine> unattributed session <name> (pid <pid>)
```

An unfit target reports its first `/showrunner:accounts` problem. Fix account readiness there; for a logged-out account, use its alias and `/login` as that account. Close ordinary sessions on the outgoing unit account before retrying a session refusal. Only units and their director-owned seats on natedev move; any session on the Mac refuses. Swap needs the Mac reachable even if its inventory is empty. A Mac outage refuses with the role unchanged. Swap takes no `--here`.

`unit account unchanged: still claude 3` means shutdown refused and the old role is back. Fix the printed refusal, then run the same swap again.

If shutdown ends `stop partial`, the new role stays selected and nothing restarts. Use the alert's runnable retry to stop the rest and then resume the saved sessions. For this example:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down 'claude 3' --restart
```

The retry prints `shutdown of claude 3 resumed; /shutdown status to watch`. It preserves the saved sessions, timers and scope. A different `--only` refuses with `shutdown: retry must keep the original --only scope`. A peer that is not ready refuses with `shutdown: peer is not ready to retry stopping; nothing was shut down`. The alert also keeps its manual `/shutdown restart` guidance for a session on the old account.

`claude 3: automatic restart skipped` means the peer record could not be read after a complete stop. Restore access, then run the alert's command:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py restart 'claude 3'
```

Both recovery commands name the **old note label** printed in the alert, even though the role now names the target. Here the old label is claude 3; use your alert's label.

To undo:

```text
/showrunner:accounts swap unit default
```

Moving units off the default account, which showrunners share, uses the cutover's selected-unit procedure. Swap refuses showrunners and top-level sessions.

## Setting up a new directory

For claude 3, run inside a session:

```text
/showrunner:accounts setup "claude 3"
```

Then start `claude3` in a terminal, run `/login` as claude 3, and check `/showrunner:accounts`. Setup links shared content to `~/.claude`; it reports existing real files or directories rather than replacing them. Login stays separate for each directory.

## Low-quota alerts

Acknowledging an account's low-quota alert keeps it quiet across swaps. Made spare and then selected again, it stays quiet until its own quota recovers or resets.

## When a unit will not start

A showrunner's launch refuses with:

```text
add_unit: unit account <label>: <problem>; /showrunner:accounts
```

Run `/showrunner:accounts`, fix what it names (most often the account's alias and `/login` as that account), then launch again.

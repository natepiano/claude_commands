# Shutdown and restart

## What it is

`/shutdown` stops one account's Claude sessions on natedev and the Mac through one record per top-level session. Each owner settles, stops and optionally returns independently; a busy session or a permission form delays only that owner. Seats, Codex app-servers, timers and host resources belong to their owner. `/shutdown restart` restores recorded sessions, their notes and timers. Other accounts, CI runners and system services keep running.

An account-wide request holds the account against new launches and scheduled work. A selected request holds only its sessions. Each machine conducts its own records; an unavailable peer leaves local work running. `commands/shutdown.md` defines the user command.

## How it works

### Key files

| File | Role |
| --- | --- |
| `scripts/whoami/account.py` | Account resolution and label cache. |
| `scripts/shutdown/inventory.py` | Account-attributed sessions, seats, hosts and resources. |
| `scripts/shutdown/record.py` | `SessionShutdown`, `AccountHold`, store, migration and locks. |
| `scripts/shutdown/shutdown.py` | Parser, `resolve_sessions`, status and launch checks. |
| `scripts/shutdown/settle.py` | `begin`, `down`, `next_step`, `hold_reason`, `ready`, `cancel`, `now`, `conduct`, `report_stop`, `alert_user`. |
| `scripts/shutdown/stop.py` | `fresh_session`, `StopClaim`, `stop_session` and owner cleanup. |
| `scripts/shutdown/restart.py` | `relaunch_session`, confirmation, Remote Control wait, `up`, `restart` and result transport. |
| `scripts/message/sessions.py` | Process identity, liveness and `remote_control`. |
| `scripts/shutdown/launch_permission.py` | Launch permission and barrier shared by launchers. |
| `scripts/shutdown/remote.py` | SSH with explicit `rc=<n>` status. |

### Accounts

- A process's account is the login in its Claude config dir: `CLAUDE_CONFIG_DIR` from that process's environment, else `~/.claude` (whose file is `~/.claude.json`; any other dir uses `<dir>/.claude.json`). The login is `oauthAccount.emailAddress`. The **label** is the stem of the hanadocs note in `AGENT_NOTES_DIR` (default `~/rust/hanadocs/agents/`) whose stem starts with the tool and whose `login:` matches case-folded (`claude 2`); else the login.
- `process_config_dir(pid) -> Path | None` reads `/proc/<pid>/environ` on Linux and `ps eww -o uid=,command= -p <pid>` on darwin (`parse_darwin_config_dir(output, expected_uid)`: `None` unless the uid matches and a `HOME=` token is present; the last `CLAUDE_CONFIG_DIR=` token wins). An unreadable environment is `None`, never the default dir. `account_of(pid) -> Account | None`.
- `own_claude_account()` raises `UnreadableAccount`; `named_claude_account(text)` takes a `claude*` note stem with a non-empty `login:`, else text holding `@` with no `/`, NUL or whitespace and no leading `.` as a login, else raises `UnknownAccountName`. A named account uses that resolver; most absent-account verbs use the own account. `restart.select_account` also infers from eligible session records and account holds on both machines when the own account is unreadable: one candidate wins, otherwise candidates are listed and restart exits 1. An unknown named account prints `shutdown: <message>` and exits 2. An unreachable or older peer cannot prove that candidate set complete, so restart asks for an account.
- A `SessionStart` hook runs `account.py --write-label`, which writes `<config dir>/account-label` (label plus newline) atomically and only on change, or deletes it when the account is unreadable. The label file is a cache; process configuration remains the authority for account attribution.
- **Status line.** `statusLine.command` reads `account-label` with the `read` builtin and runs `jq -r --arg pwd … --arg account … -f statusline.jq` (two processes: sh and jq). Row 1 is `<machine> | <account> | <dirname> | <tokens> | <model [effort]>`: the machine is `mac` when `$ENV.HOME` starts with `/Users/`, else `natedev`, and an empty account drops its field. Row 2, when any part exists, joins `5-hour <N>% left` and `weekly <M>% left` with ` | `, from `rate_limits.five_hour` / `seven_day` `used_percentage`; `remaining($window)` floors `100 - used` and clamps to 0–100, and a missing window or non-number drops its part. With no part, no second row.

### Inventory

`inventory(login: str, only: frozenset[str] = frozenset()) -> Inventory` keeps the live records in `~/.claude/sessions/` (`NOTIFIER_SESSIONS_DIR`) whose process config directory supplies a login matching case-insensitively. An unreadable account or a `procStart` unequal to the live process start (pid reuse) goes to `unattributed` (`UnattributedSession{pid, name, reason}`, reason `account unreadable` | `process start mismatch`), never to `sessions`. A non-empty `only` keeps those ids and their seats.

- **Kind:** `showrunner` when its id is the `TARGET` of a `showrunner-<slug>` notifier instance (`showrunners.registered_showrunners()`); `unit` from the `SHOWRUNNER_UNIT` / `SHOWRUNNER_UNIT_ID` marks on its tmux pane (`unit_lookup.marked_units`); `seat` when the record's `kind` is `bg`, owned through any `active/<director id>` run folder's `seats` ledger (`SeatOwner` = `director{session_id}` | `no director`); else `top-level`.
- **Host** (`Host`): `UnitHost{production, unit, doc, tmux_session, plan: plan{path} | no run record}` (plan from the newest run record whose `main_agent.session_id` matches); `TmuxHost{tmux_session, pane: TmuxPane{pane_id, pane_pid} | PaneNotRecorded}` from `TMUX_PANE` without marks; `WindowHost{kind: ghostty | zed, window_shell, desktop: named{name} | not in snapshot}` from a parent walk starting at the session's pid (natedev desktops from `~/rust/hanadocs/agent sessions.md`, the `agent-sessions-snapshot` output); `TerminalHost` (Mac Ghostty or Terminal); `UnknownHost`. `window_shell` is the ancestor directly beneath the terminal process.
- **Checkout:** `git{path, head: branch{name} | detached{commit}, upstream: tracking{ahead} | no upstream, dirty}` | `not a checkout`. **Model:** `model{name}` | `no reply yet`.
- **Account directory:** each session carries `config_dir`, the string returned by `process_config_dir(pid)`. One boundary reads the process directory once and resolves its account from that directory, returning `AttributedSessionConfiguration{account, config_dir}` or `UnreadableSessionConfiguration`. A readable environment without `CLAUDE_CONFIG_DIR` gives `~/.claude`; an unreadable environment or account remains `unattributed`. Registries and transcripts stay under the shared `~/.claude/sessions` and `~/.claude/projects` paths.
- **Servers and timers:** run folders are `SHUTDOWN_DELEGATE_ROOT` (default `/tmp/claude/delegate`) `/active/<session id>`; a live `mesh_server.json` pid is a `CodexServer{run_dir, pid, busy_seats}`, a `mesh_roster.json` seat in `running`/`starting` is busy only with no `launcher_pid` or a live launcher; timers are `NOTIFIER_STATE_DIR/*/conf` with `TARGET=session:<id>`.
- `parse_inventory` / `parse_session` check every `kind` and field and raise `InvalidInventory` naming the field path (`inventory.sessions[0].host.kind is invalid`). The peer's inventory is always decoded through them.

### The record store

`SHUTDOWN_STATE_DIR` defaults to `~/.local/state/shutdown`. Each case-folded login directory contains `sessions/<sanitized session id>.json`, optional `hold.json`, `history/`, `lock`, and `conduct.lock`. Characters outside `[A-Za-z0-9_.-]` become `-` in session filenames. Filename collisions are refused, never resolved by overwriting another id.

`SessionShutdown` carries `login`, `label`, `machine`, `requested_at`, `request_id`, `requested_by` (session id or terminal), `hold` (`account` or `session`), `then` (`stop` or `restart`), `force` (`wait for ready` or `now`), `alerts` (`user` or `none`), the owner's `session`, its `seats`, timer snapshots, `settle_message`, `where`, `progress`, `stop_issues`, and optional `alerted_at`. A seat never gets its own record. `AccountHold` carries the request's login, label, machine, time, id, origin and continuation. Request grouping uses `request_id`; second-resolution timestamps never establish request ownership.

Public store operations are `session_records`, `all_session_records`, `create_session`, `update_session`, `archive_session`, `set_account_hold`, `clear_account_hold`, and `clear_finished_account_hold`. `lookup_session` returns `ShutdownFound(record)` or `NoShutdown`; `lookup_account_hold` returns `AccountHeld(hold)` or `AccountOpen`. Updates run under the account lock, revalidate and write with a temp file, `fsync` and `os.replace`. Invalid paths, symlinks, invalid fields and mismatched account or session ids raise `InvalidRecord`. Absence is distinct from unreadable state.

`clear_finished_account_hold(login, *, request_ids=())` clears only when no record of the hold's request remains. Non-empty `request_ids` also require that the hold belongs to one of those requests. Selected cancellation or restore supplies the requests it finished; account-wide restore can clear a hold with no records. Account-wide cancellation always clears the hold, even when stopped records remain for restart.

Reads and dry runs convert old files in memory without writing them. `migrate(login)` is called by writing operations before they use the store. It validates every old file and destination before writing any session record or moving a source file; conflicting sanitized filenames or history destinations raise `InvalidRecord`, with no session records written or sources moved. For one session id the current file wins, then the old main file, then the newest old partial file; completed old files supply no session records or hold. Conversion attaches seats, drops record-level issues, preserves an inferred account hold without replacing an explicit one, and assigns `legacy-<machine>-<requested_at>` as the request id. It moves old sources to history and prints one conversion line per source. Status, read verbs and dry runs still see unmigrated files. Internal `records --json --here` and `holds --json --here` expose the session records and account holds used for cross-machine account inference; a peer without these interfaces is not reached.

### States

Each owner's `progress` is `waiting`, `ready{at}`, `stopping{at}`, `stopped{at}`, `already gone{at}`, `process identity lost{at}`, `stop failed{at, reason}`, `restarting{at, prior}`, `restarted{at}`, `restart failed{at, reason}`, `manual restart{command}`, or `timers pending{at, timers}`. Stop-only terminal records remain for restore. Confirmed `restarted` records are archived and never launched again.

A `stopping` or `restarting` claim belongs to its worker for `CLAIM_STALE_SECONDS = 120`. An older claim can be replaced; a relaunch resumes from `prior`. A stop worker checks its exact claim before each write, signal and cleanup operation; a relaunch confirms only its own claim. Losing a claim transfers ownership without adding a user outcome. Conductor handoff likewise has no separate user outcome.

A partial stop is `record.stop_incomplete`: `stop failed`, or a terminal stop with non-empty `stop_issues`. A Codex server or unit tmux session left running can therefore coexist with progress `stopped`. Status shows the issues; `/shutdown now --only <name>` retries owner cleanup without signalling the already stopped owner. Failed retries consume the force request so they are reported once.

### Settling and selected actions

`down [account] [--here] [--only NAME[,NAME...]] [--restart] [--no-alert]` checks `github-warm-status`, then begins locally and launches this machine's conductor. Each new non-seat owner gets `waiting`, its seats, saved timers and stopped timers; a timer-stop failure removes only that owner's request and names it. Existing records print `<name>: already in progress` and keep their continuation, hold, policy and request origin. New owners print `<name>: added`. Repeating a request starts a conductor even when every selected owner already has a record.

Without `--here`, the peer receives `down <login> --here` with the same request id, origin and continuation and conducts independently. Failure prints `<machine>: not reached; run /shutdown there`; local records are not withdrawn. Exit 0 means this machine started. Only an account-wide request sets an account hold.

`resolve_sessions(login, items, *, here=False)` resolves exact names or ids against account records and inventory, counting each id once and excluding seats. Local ids need no peer lookup; names are checked on both machines unless `--here`. Readable peer records remain candidates when its inventory fails. An unavailable peer prints `<peer>: not reached; names checked on <machine> only`; an unknown or ambiguous item exits 2 and lists candidates. Local ids execute locally; only remaining ids go to the peer. For selected `cancel`, `now` or `restart`, a local live session with no record prints `<name>: no shutdown in progress` and exits 1 without being sent to the peer.

The conductor holds `record.conductor_lock(login)` for its lifetime. A duplicate exits successfully; a quarter-second retry (`CONDUCTOR_LOCK_RETRY_DELAYS`) tolerates a status probe. After release it checks for newly added work so an ending conductor cannot leave a request unattended. Every `SETTLE_INTERVAL_SECONDS = 15`, one inventory feeds `next_step` for each owner. Running actions are skipped until their futures complete. Stops and non-window launches run in a `RESTART_LAUNCH_WORKERS = 6` pool; registry and Remote Control waits use the polling pool. Window hosts launch in order on the conductor's thread with one desktop state restored on exit.

A waiting non-requester gets the settle message; queued messages retry after `SETTLE_RETRY_SECONDS = 300`. A queued instruction whose request was cancelled or replaced before sending is discarded and never marks another request's record. `ready --where TEXT` updates only this session's waiting or ready record. Ready owners stop when idle, with every seat idle, no form, no merge in progress and nothing ahead of upstream; dirty files stay on disk. `force: "now"` bypasses readiness. Absent owners or lost identity get a stop outcome without signals. An unreadable account holds the record. The requester goes last among the same request's records on this machine; a showrunner never waits for its units.

`cancel [account] [--here] [--only NAME[,NAME...]]` restores timers and footers, retires queued shutdown messages, sends `cancelled by the user: continue where you were` to a previously messaged owner, and archives the record. Restoration and the cancelled message run under the account lock before any later request can send its instruction, so cancellation cannot replace that instruction. Selected waiting or ready owners print `<name>: taken out`. A selected `stop failed` record can be withdrawn only when the original process identity is still live; identity checking and withdrawal share the lock, and it prints `<name>: taken out, still running`. A competing stop claim keeps the record. Other retained records print `<name> was not cancelled: <progress>; /shutdown restart --only <name> brings it back`, exit 1; account-wide cancellation prints `/shutdown restart brings it back` in that sentence. Selected cancellation clears a hold only if it withdrew that hold's request's last record.

`now [account] [--here] [--only NAME[,NAME...]]` sets `force: "now"` and starts the conductor; it never stops or retries a session itself. Chosen eligible owners print `<name>: stopping now`; an ineligible owner prints `<name> is <progress>; nothing to stop`, exit 1. A multi-selection exits 1 if any selected owner could not be acted on.

### Status

`status [account] [--json] [--here]` shows the inventory, then the records and applicable actions in each machine's block. Waiting and ready lines retain `  <name>: <progress> — <hold details>` and a message note after ` · `. Hold details include `busy`, `showing a form`, `ahead N on <branch>`, `merge in progress`, `Codex seat <name> is still running`, and `message queued: <reason>`. Stop issues and failed restore details remain beneath the owner.

A read-only, non-blocking `conductor_status` probe distinguishes running, not running and unavailable. A lock filename alone is not ownership. A machine with waiting or ready records and no owner prints once:

```text
  no conductor running; /shutdown --only <name> resumes one, /shutdown cancel --only <name> takes it out
```

An unreadable or unreachable probe instead prints `  conductor ownership unavailable: <reason>`. Each block ends with at most three lines, in this order, listing applicable names in record order:

```text
  take out: /shutdown cancel --only <name> · hana, nixos
  stop now: /shutdown now --only <name> · hana, nixos
  bring back: /shutdown restart --only <name> · bevy
```

`<name>` is literal guidance to substitute one listed name or id. Peer records cannot infer local readiness. An unreachable inventory prints `<machine>: unreachable`; a bad answer prints `<machine>: unavailable (rc N)` without failing the local report.

### Stopping

`stop_session` claims before signalling and never replaces a stored pid with an observed one. `fresh_session` distinguishes a matching owner (with seats), absent owner, lost identity and unreadable account. Process identity is checked before each `SIGTERM`; two attempts each allow `STOP_WAIT = timedelta(seconds=10)`. No `SIGKILL` or pane input is used. A forced stop first ends busy Codex seats. After the owner exits, cleanup closes its Ghostty window where ownership still matches, stops its Codex servers, then removes its unit's exact tmux session. Seats receive no process signal and keep jobs and transcripts for their next message. Cleanup issues remain on the record.

### Restart

`restart [account] [--only NAME[,NAME...]] [--here] [--dry-run] [--no-alert]` selects its account through `restart.select_account` and runs `up` locally and, unless restricted, on the peer. Selected restart bypasses requester-last, so a stopped requester can return while siblings wait. Waiting, ready or stopping owners print `<name> is <progress>, not stopped; nothing to restart`, exit 1; a fresh claim prints `<name> is already restarting`, exit 0. Refusals are separate from failed relaunches: no failure count, summary or phone alert for refusals alone. Real failures beside refusals are named and can alert.

`relaunch_session(record, *, dry_run, desktop_state)` claims `restarting{at, prior}` before `launch_entry`. The caller owns and restores the desktop state. Eligible records are stopped, already gone, lost identity, manual restart, restart failed, timers pending and stale restarting; already restarted records archive without launching. A manually resumed live session is reconciled without a second launch. Live `timers pending` retries only pending timers; closed sessions relaunch, and unreadable liveness never proves absence. One launch exception marks only its owner as failed.

Non-window owners launch through the pool; window hosts launch serially. Account-wide ordering puts units, showrunners and top-level owners before the requester, without requiring a showrunner to wait for its units. A pass's launched ids share one registry wait (`SESSION_WAIT_SECONDS = 90.0`, `SESSION_POLL_SECONDS = 0.5`); `confirm_entry` then restores timers and marks live sessions restarted. An error confirming one owner marks only that owner as a failed restart; the others in the batch finish as restarted. `wait_for_confirmation` returns `RegistryConfirmation`, retaining records from its final liveness poll for the Remote Control check. The conductor tracks `ConfirmedRelaunchPass` through `PendingRegistryConfirmation`. Records that finish are archived; failures, manual commands and pending timers remain. Real runs print `<machine>: restarted <n> in <s> s`; dry runs print planned launches without conversion, mutation, archive, hold clearing or a timing line.

- **Launch by host.** The resume command is the one shell string: `cd <quoted cwd> && ENABLE_TOOL_SEARCH=true command <shlex.join([claude, --resume, id, -n, name, --remote-control, name, (--model, name), --settings, '{"disableAgentView": true}', prompt])>; exec zsh`, one argv item to `zsh -ic`. Unit → `add_unit.py --production <doc> <unit> --plan <plan> --resume <id> --cwd <cwd> --session-name <name> --tmux-session <tmux> --restart-note <file>` (plan `no run record` → `manual restart`). `<unit>` is the recorded unit id exactly: `add_unit` strips one trailing `-unit` from the name it is given and appends its own, so `x-unit` and `x-unit-unit` each resolve to themselves; stripping it in restart as well turned `x-unit-unit` into `x-unit`, a twin unit. `<plan>` is the unit's Plan cell in the production doc's Units row, read at launch (`_units_row_plan` through `add_unit.unit_table` / `cell_value`); the run record's plan is used only when the doc has no single row for the unit. `add_unit` refuses a resume whose plan differs from the row, and a run record's plan differs once the plan moves to as-built. A restore whose row matched adopts that row: `add_unit` skips the recorded-add-mode check and the Production-header check on the plan, both of which describe new work, so a unit first added from a brief, or whose Plan cell now names its as-built doc, restarts. The row also names the restored unit's worktree and branch: the naming pattern `add_unit` gives new work (`<checkout>-<unit>`, `<slug>-<unit>`) is never compared with the row or reserved against other rows on a restore, so a unit whose worktree or branch does not follow the pattern restarts. The session launches in the recorded `--cwd`, where its transcript lives; no worktree or branch is created and the row is never rewritten. A recorded cwd that is not the row's Worktree, or that has another branch checked out than the row's Branch, is launched in without comment: a unit made from an existing session keeps that session's directory while its worktree is elsewhere, so the difference is normal, and its prompt names the row's Worktree and Branch, where it edits. A restore with no Units row is refused (`unit <id> has no Units row; a restore never adds one`), never added as a new unit. Showrunner → prompt `/showrunner:produce <doc> resume`, its note sent as a `shutdown` message once live. `WindowHost` → `kdotool set_desktop <n>` (from `~/.config/kwinrc` `[Desktops]`), `systemd-run --user --collect --quiet -- ghostty -e zsh -ic …`, 1.2 s apart, then back to the starting desktop; a Zed session comes back in Ghostty. `TerminalHost` → `open -na "/Applications/Nix Apps/Ghostty.app" --args -e zsh -ic …`. `TmuxHost` → `respawn-pane -k -t <pane_id> -c <cwd> zsh -ic …` when the recorded pane is in the recorded session and is dead or still has the recorded `pane_pid` with no child; else `new-window -t =<s>:` when the session lives; else `new-session -d -s <s>`. `UnknownHost` → `manual restart` with the command printed.
- **Note** (`restart_note`): label, stop and restart times in America/Los_Angeles (`%Y-%m-%d %H:%M %Z`), the recorded `where` (for `not said`: read branch and plan), run `send.py pending` first, keep waiting if it was waiting on the user. Every first prompt is appended to `conversation_pause`'s scheduled prompts before launch (`_record_scheduled_restart_prompt`; `add_unit.py` does the same for units with the exact `prompt_for` string), so the pause hook does not treat it as typed. A failure leaves the entry unlaunched with `restart prompt not recorded: <reason>`.
- **Account on resume:** a non-unit entry whose recorded `session.config_dir` differs from `~/.claude` inserts `account.named_account_prefix(dir)` — `CLAUDE_CONFIG_DIR=<shell-quoted dir> CLAUDE_CODE_REMOTE_MEMORY_DIR=<shell-quoted ~/.claude> ` — immediately before `ENABLE_TOOL_SEARCH=true` inside the resume command. The second variable makes Claude Code use the memory under `~/.claude` directly; through the `projects` link a memory write would ask (`docs/accounts.md`, Memory on a named account). This applies to showrunners, every terminal host, and printed manual commands. Default-directory and legacy entries keep the existing command byte for byte. Units always return through `add_unit.py`, which selects the current `unit` account from `config/accounts.json`; a role swap therefore moves restarted units. The restart subprocess environment still strips `CLAUDE_*`; the assignment inside the command reaches tmux independently of its server environment.
### Remote Control after relaunch

`remote_control(record: SessionRecord) -> Literal["active", "absent"]` reads `sessions_dir() / "<pid>.json"`. A non-empty `bridgeSessionId` is active; missing, empty or unreadable is absent. `SessionRecord` and the output of `sessions.py` are unchanged.

`wait_for_remote_control(records, timeout)` uses the registry records just confirmed live, with `REMOTE_CONTROL_WAIT_SECONDS = 10.0` and `SESSION_POLL_SECONDS = 0.5`. It polls only those small files, never scans `sessions.live_sessions()` again, returns at once when all are active, and starts no poll after the deadline. A slow read can overrun by one poll's reads; there is no thread or timeout around each read. One shared wait per relaunch pass costs usually 1 to 3 s after registry confirmation, at most 10 s plus one poll's reads when one stays absent. The conductor runs the wait in its polling pool while other owners keep moving.

Each confirmed session still absent prints `<name>: running, no Remote Control`. Its record finishes as restarted and its exit status stays successful. It is not relaunched or typed into. Turn on Remote Control at the named session's terminal with `/remote-control`.

`MachineRestartResult` retains each missing session's id, name and saved alert policy after archive. Internal `up <login> --here --json` returns a validated object containing `machine`, `label`, `outcome`, `lines`, `no_remote_control` and `refused`. `no_remote_control` holds `{session_id, name, alerts}` entries. `RestartRefusal` is a union: `not stopped` carries id, name and progress; `no shutdown` carries id and name. Local and peer refusals use this same data, not text parsing or a special exit code. A peer without the object is treated as not reached. `settle.unrecorded_local_sessions` supplies local selected sessions without a record to cancel, now and restore. `PeerRestartResult` validates the wire object; its `outcome` is `done`, `partial` or `nothing`.

### Messages and notifications

Shutdown messages use sender `shutdown`, which the conversation-pause hook treats as scheduled work. Settle, cancellation and restart notes use key `shutdown-<login>-<session id>`; the final requester result uses `shutdown-result-<login>`. Queued messages are read with `send.py pending` after resume. `settle.alert_user(summary, text, action, *, machine="")` is the only phone path and uses an `ActionRequired`; it routes Mac alerts through `--machine natedev`. A hold or session record never refuses the UPS alert.

`NOT_ANSWERING_AFTER = timedelta(minutes=5)` applies to waiting owners and held ready owners, including a requester whose settle message was never sent. Age starts at `settle_message.at` for a sent message, otherwise `requested_at`. `REPEAT_HOLDOUT_ALERT` is 60 minutes; `alerted_at` records delivery scheduling. Due sessions in one pass share an alert, one line each. The single-session summary is `<name> is not answering the shutdown of <label>`; text names the machine and `hold_reason`, plus `/shutdown cancel --only <name>` (take it out; it carries on) and `/shutdown now --only <name>` (stop it anyway). The requester receives the same text. Both sends run in pools because the sender timeout is 60 seconds; they never block the conductor's stepping thread or change a step. Selected shutdowns also alert unless the record says `alerts: "none"`.

`report_stop` sends the requester this machine's results plus one peer-record read. Account-wide partial stops alert when their saved policy allows it. Restart uses `<label> is not fully back` for incomplete restoration and for missing Remote Control, including when that is the sole issue. Missing Remote Control closes with `Turn on Remote Control in each named session at its terminal: /remote-control`; retry guidance remains only when something is still left to bring back. The conductor sends the same Remote Control alert for its pass. Lines always print; `alerts: "none"` and command `--no-alert` suppress the phone send, including after archive.

For incomplete restores, restart checks policy on both machines. With `--only`, only selected records count; with `--here`, it reads no peer. When the peer is unavailable, including an answer without the result object, account-wide policy uses every local record: no record or any record with `alerts: "user"` enables the alert. Selected policy stays restricted to chosen records; no second peer call is made. Refusals alone never alert.

### Launch checks and held sessions

`launch-blocked [account] [--restart-of SESSION_ID]` exits 0 for a hold, 1 silently when launch is allowed, 2 for an unresolved account, and 3 for unreadable state. An account hold prints `<label> is held by a shutdown (since <time>)`; a selected shutdown never blocks unrelated new work. A restore's own record overrides the account hold: stopped, already gone, lost identity, restarting, restart failed and manual restart permit it. Every other recorded progress prints `<session name> is held by a shutdown (since <time>)`. Relaunch claims first, so a timers-pending retry is permitted while restarting.

`launch_permission` returns `AllowedByShutdownState`, `BlockedByShutdown(reason)` or `ShutdownStateUnreadable(detail)`; only a silent exit 1 permits launch. `launch_barrier()` holds `<root>/launch.lock` from check through spawn, and record creation and account-hold creation share it. Only the short decision and spawn are protected, never the launched work. Account locks serialize store mutations; a conductor holds its separate lifetime lock.

Scheduled nightly review and fix jobs keep firing but skip held accounts or unreadable state. `add_unit.py` applies the check to its chosen account, including a restore's id. Manual unit resume checks `--restart-of` and continues only on silent exit 1. `held-sessions` lists every owner's and seat's id; delegate cleanup treats those active runs as live so seats survive. No caller can infer launch permission from an exception or a crash exit 1 with output.

### Remote calls

`run_remote` uses SSH and the trailing `rc=<n>` from the command; SSH's status alone is never a successful answer. A missing trailer, SSH 255 or timeout is not reached. Standard calls have a 120 s limit; restart's peer restore uses link keepalives with no overall timeout (`ServerAliveInterval=15`, `ServerAliveCountMax=4`). Each machine makes progress independently; there is no cross-machine stop claim.

## Invariants and costs

- Account isolation: unreadable account means unattributed, never the default account. Stored process identity is never replaced with an observed pid.
- Claude processes receive only `SIGTERM`, with readiness unless explicitly forced and no pane input. An owned Ghostty window shell is closed with `SIGHUP` after the ownership check; owner identity is checked before signals or resource cleanup.
- Registry process identity and socket prove relaunch liveness; Remote Control is a separate field check and never changes the restart outcome.
- Only `AllowedByShutdownState` permits a launch; the barrier spans check to spawn. The UPS alert is unaffected by holds. Neither implementation nor tests read notification credentials.
- Record timestamps use UTC ISO 8601 at whole seconds; injected stop clocks produce the same format. User-facing times use America/Los_Angeles.
- Test cases derive from `ShutdownTestCase` and use temporary state and fake send, notifier, process, tmux and host commands. Tests start no real session and send no real alert.

The 2026-10-12 claude 3 restart brought 19 sessions back over 12 m 40 s: eight took 90–92 s, ten took 2–6 s. The eight had Remote Control within about 3 s, but `add_unit.py` waited its 90 s default for `/remote-control is active` on the visible tmux screen. Restart notes displaced that line in an alternate screen with no history; transcript and `bridgeSessionId` proved it had appeared earlier. Restore launches now return immediately after spawn; new-work launches keep their screen wait. Registry confirmation and six launch workers replace serial waits; competing tmux creators recover by opening a window in the session created by the other launch.

Costs: conductor pass 15 s; settle-message retry 300 s; not-answering alert 5 min and repeats at most every 60 min; two stop waits of 10 s; shared registry wait up to 90 s; Remote Control wait 10 s plus one poll's reads; windows 1.2 s apart; sender timeout 60 s; launch checks and held-session queries 30 s. On macOS each `running_session` with a recorded `procStart` uses one `ps -o lstart=` per registry record per call, with a 5 s timeout; Linux reads `/proc/<pid>/stat`. The Remote Control wait avoids that process scan.

Uncommitted edits remain on disk. Readiness means idle, nothing ahead of upstream and no merge; it never commits unfinished work. A live launch with unreadable registry state remains unresolved rather than being launched twice. Restore prompts are registered as scheduled so they do not pause restored timers. A recorded tmux pane is reused only when its identity matches; the Mac has no tmux.

## Live-check procedure

Only the unit director and user run this procedure, after both writer seats are idle. Run installed scripts from a scratch directory outside this worktree. Obtain the user's participation before touching sessions. Use a fresh scratch `HOME`, `SHUTDOWN_STATE_DIR`, Claude registry, transcripts, notifier and pause state; prepare authentication without copying session registries or using existing tmux sessions. Ensure detached conductors and restored children receive the same scratch environment: `systemd-run` and `launchctl` do not inherit shell exports automatically. Provide that environment in the scratch launch setup before starting; do not proceed if a conductor would use production state. Prepare two scratch sessions with no repository work or timers belonging to real sessions, and capture their ids and names. Keep all command output in the scratch directory.

### natedev

1. Create one dedicated scratch tmux session with two scratch Claude sessions. Leave one at a permission form; make the other reach idle readiness after receiving the shutdown request. Check their account attribution and recorded ids. Set shell variables `CHECK_LOGIN`, `CHECK_IDS` (comma-separated ids), `CHECK_IDLE_ID`, and `CHECK_BLOCKED_ID`; `CHECK_PY` names the installed script launcher and `CHECK_SHUTDOWN` the installed shutdown script. These variables refer only to the scratch sessions.
2. Run the automatic-return request:

   ```sh
   "$CHECK_PY" "$CHECK_SHUTDOWN" down "$CHECK_LOGIN" --here --only "$CHECK_IDS" --restart --no-alert
   "$CHECK_PY" "$CHECK_SHUTDOWN" status "$CHECK_LOGIN" --here
   "$CHECK_PY" "$CHECK_SHUTDOWN" launch-blocked "$CHECK_LOGIN"
   "$CHECK_PY" "$CHECK_SHUTDOWN" cancel "$CHECK_LOGIN" --here --only "$CHECK_BLOCKED_ID"
   ```

   Pass: the idle owner stops and is back within 30 s; the form owner holds only itself and cancellation restores it. The account launch check exits 1 silently throughout because selection sets no account hold. Record elapsed time and both owners' outcomes for this request separately.
3. Put one session at a permission form again and run a stop-only request:

   ```sh
   "$CHECK_PY" "$CHECK_SHUTDOWN" down "$CHECK_LOGIN" --here --only "$CHECK_IDS" --no-alert
   "$CHECK_PY" "$CHECK_SHUTDOWN" status "$CHECK_LOGIN" --here
   "$CHECK_PY" "$CHECK_SHUTDOWN" restart "$CHECK_LOGIN" --here --only "$CHECK_IDLE_ID" --no-alert
   "$CHECK_PY" "$CHECK_SHUTDOWN" cancel "$CHECK_LOGIN" --here --only "$CHECK_BLOCKED_ID"
   ```

   Pass: the idle owner stops and stays stopped until selected restart brings it back while the form owner remains waiting. Then selected cancellation returns the waiting owner. Record these outcomes separately from the automatic-return request; verify the launch check stays exit 1.
4. For every relaunched scratch session, inspect its own `<pid>.json` at the Remote Control check's final poll and the diagnostic output: a non-empty `bridgeSessionId` must agree with absence of `<name>: running, no Remote Control`; a missing, empty or unreadable field must agree with that line. Record each result and check time. Later activation does not contradict a diagnostic from the earlier deadline. No-alert requests print diagnostics but send no phone alert.
5. After inspection, end only the scratch sessions and scratch tmux session, remove scratch state, and restore any temporary environment used for the launch setup.

### Mac

The showrunner gives the user the following commands in the prepared scratch environment, outside the worktree. The user launches the two scratch Claude sessions in Ghostty and handles its permission forms and windows; no tmux is used. Set `CHECK_LOGIN`, `CHECK_IDS`, `CHECK_IDLE_ID`, `CHECK_BLOCKED_ID`, and `CHECK_LIVE_NAME` to those scratch sessions. Use the installed scripts:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down "$CHECK_LOGIN" --here --only "$CHECK_IDS" --restart --no-alert
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py status "$CHECK_LOGIN" --here
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py launch-blocked "$CHECK_LOGIN"
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py cancel "$CHECK_LOGIN" --here --only "$CHECK_BLOCKED_ID"
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down "$CHECK_LOGIN" --here --only "$CHECK_IDS" --no-alert
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py restart "$CHECK_LOGIN" --here --only "$CHECK_IDLE_ID" --no-alert
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py cancel "$CHECK_LOGIN" --here --only "$CHECK_BLOCKED_ID"
python3 scripts/message/sessions.py id "$CHECK_LIVE_NAME"
```

Run the final command from the installed configuration directory while that scratch session is live. It must print its session id, confirming macOS process-start checking. Apply the two requests' natedev pass conditions separately, including blocked-owner independence, selected restart while its sibling waits, cancellation and silent exit 1 from the account launch check. For each relaunch report `bridgeSessionId` and diagnostic agreement at the check time. The unit director records the user's report; this phase remains open until it is recorded.

### Results

- natedev: not yet run (2026-10-12). A scratch home has no account login, the stop instruction names the installed scripts under the session's home, and the message relay signs in from the default account under `HOME`; the run waits on how its sessions are signed in.
- Mac: not yet run (2026-10-12). It needs this code installed on the Mac and the user at Ghostty.

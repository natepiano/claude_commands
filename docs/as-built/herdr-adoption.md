# herdr adoption

How the production scripts start and drive Claude sessions under herdr. This
doc grows as the adoption work lands; today it holds the Claude trust check
every launcher runs before it starts a session, and the one path that reads and
types into a session on either host, tmux or herdr.

## Claude trust before every launch

### What it is

A Claude session started in a folder the account has not trusted stops at the
trust prompt, and a launcher that sends it a prompt or a slash command then
waits on a session that never answers. Every launcher in `scripts/production/`
and `scripts/nightly_review/` checks trust first and refuses with the one
command the user runs to trust the folder, before it writes or launches
anything.

### How it works

- `scripts/herdr/herdr.py` holds the one check:
  `claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile`.
  A folder is trusted when `projects[k].hasTrustDialogAccepted is True` in the
  account's `.claude.json` for any key `k` in: the folder itself, its
  `git rev-parse --show-toplevel`, and its main checkout
  (`repository_trust_key`, the parent of `--git-common-dir`).
  `repository_root` and `repository_trust_key` share `_repository_key`.
  - `Untrusted` carries `keys_tried`.
  - `UnreadableTrustFile` prints
    `cannot read <file> to check Claude trust (<ErrorClass>)`: the path and the
    error's class name, never the file's contents.
- `trust_command(folder, account: ClaudeLaunchAccount) -> str` is the line the
  user runs, accepts the prompt in, then leaves with `/exit`:
  `cd <folder> && <account prefix> claude`. Every account uses `account_prefix`
  with its numbered `CLAUDE_CONFIG_DIR` and the shared memory directory.
- Callers, each refusing with
  `<unit | child slug | session>: Claude is not trusted in <folder> on <account label>. Run: <trust_command>`
  or `<prefix>: <UnreadableTrustFile text>`:
  - `scripts/production/add_unit.py` `preflight(request, selected)` exits 2
    before any stub, table row or worktree is written. A worktree that does
    not exist yet is checked by its main checkout's key, and its trust command
    runs in the main checkout.
  - `scripts/production/add_showrunner.py` `checked`, and through it
    `promote_showrunner.py`. `ADD_SHOWRUNNER_CLAUDE_JSON` overrides the account
    file in tests.
  - `scripts/nightly_review/nightly_review.py`, on the account its spawn uses.
- Launchers use the configured group account; each trust check reads
  `<CLAUDE_CONFIG_DIR>/.claude.json` in that numbered folder.
  `~/.claude.json` remains the seed and MCP baseline.
- `scripts/herdr/trust_survey.py` is a read-only survey. For the seed and each account file
  (`~/.claude.json` and every `~/.claude[0-9]/.claude.json`) it prints
  `<account> <key> <accepted> <kind>`, the kind being main checkout, worktree
  of `<main>`, plain folder, or ancestor of `<n>` repositories, nested
  repositories included. Then, per `~/worktrees` folder, it prints whether its
  own, toplevel or main-checkout key is accepted.
- `pyrightconfig.json` puts `.` in the `scripts/herdr` environment and
  `scripts/herdr` in the `scripts/nightly_review` environment, for the
  `scripts.accounts` and `herdr` imports.

### Invariants

- Trust counts only on an exact key: the folder, its toplevel, or its main
  checkout. An accepted ancestor folder never counts.
- A refusal happens before any write or launch, and the refusal names the exact
  command that fixes it.
- The survey prints only the key, accepted and kind fields of `.claude.json`,
  never any other field.
- Tests use temporary folders and fake account files; they never start real
  herdr, tmux, Claude or Codex, and nothing answers a trust prompt.
- New herdr launchers call `claude_trust` and `trust_command`, not a check of
  their own.

### Gotchas

- Measured 2026-10-13 on all three accounts: 22 of 23 worktrees are covered by
  their main checkout's key, none by their own or toplevel key. Worktrees are
  trusted through the main checkout.
- An absent key alone does not show that a folder launched without a prompt;
  that needs runtime evidence such as a Claude project directory. The one
  uncovered worktree in the survey had none, so it never ran.
- Nightly review launches through `add_unit.py` on the configured units
  account, whose numbered directory supplies its trust file.

### Ruled out

- Ancestor keys as trust: the 2026-10-13 live check refused them.
- `trusted_by_claude`, `ProjectTrust` and `add_showrunner._project_keys`:
  replaced by `claude_trust`.
- A Codex trust check: no launcher starts interactive Codex.

## Typing into and reading sessions on either host

### What it is

Relays, `/compact`, the go-ahead, effort changes, the stall watch,
`unit_status.sh`, promotion and the shutdown screen check all read or type into
a Claude session. `scripts/production/session_pane.py` is the one path they
use, and it works the same for a session in a tmux pane and one in a herdr
pane. A relay to a herdr session blocked at a trust, permission or question
prompt is refused with a line saying so; nothing is typed into the prompt.

### How it works

- A place is `unit_lookup.Place = TmuxPlace(pane) | HerdrPlace(session, pane)`.
  `MarkedUnit(unit, label, claude, place)` carries it; `label` is the tmux
  session label or the herdr session name. Every caller dispatches on
  `unit.place`, never on a pane string.
- Library, each function branching once on the place's type:
  - `read_place(place, lines=60, *, joined=False, ansi=False) -> str` — tmux
    `capture-pane -p [-J] [-e]`; herdr `Herdr.read`
    (`pane read --source recent|recent-unwrapped --lines N [--format ansi]`).
    `lines` is the last N lines of output on both hosts; tmux trims its
    `capture-pane -S -N` output to the last N lines.
  - `type_place(place, text)` — tmux `send-keys -l <text>` then `send-keys Enter`,
    two calls; herdr `Herdr.prompt` (`agent prompt`).
  - `keys_place(place, *keys)` — tmux `send-keys`; herdr `Herdr.send_keys`.
  - `where_place(place) -> str` — `tmux:<pane>` or `herdr:<session>:<pane>`.
  - `record_place(record: SessionRecord) -> Place` — the pane of that one
    registry process. On Linux the pane is `TMUX_PANE` from
    `/proc/<pid>/environ` (`HERDR_PROC` overrides the root); on Darwin it is the
    `%N` at the end of the record's `tmux` field. With no tmux pane it is
    `herdr.herdr_place_of(pid)`; with neither, `NoSuchTarget`.
  - `session_place(session_id) -> Place` — `record_place` of the newest live
    session record with that id. Two live processes can share a session id, so
    a caller holding an observed record uses `record_place`.
  - Errors: `TmuxFailure(OSError)` for tmux, `HerdrFailure` and its subclass
    `AgentBlocked` for herdr, `NoSuchTarget` when no live session or marked unit
    matches.
- CLI: `session_pane.py read|type|keys|where <target>`, the target being
  `unit <slug> <unit>` (through `unit_lookup.marked_units`) or `session <id>`.
  `read` takes `--lines N`, `--joined` and `--ansi`; `type` takes one text
  argument; `keys` one or more keys; `where` nothing. Exits: 0 done, 1 no such
  target, 2 usage, 3 host failure, 4 blocked at a prompt
  (`<target>: blocked at a prompt; nothing typed`, from herdr's `agent_blocked`
  refusal).
- Callers:
  - `commands/showrunner/produce.md`, `dailies.md` and `promote_unit.md` run
    the CLI for relays, the `/compact` idle check, the go-ahead, pane reads and
    stopping a session (`type session <id> /exit`).
  - `unit_effort.py` reads with `ansi=True`, judges the screen with `verdict`,
    then `type_place`s `/effort <level>`.
  - `stall_watch.py` hashes `read_place(place, 400, joined=True)`.
  - `add_unit.wait_for_remote_control` polls `read_place` for
    `/remote-control is active`. Its resume path stops a tmux unit with
    `kill-session` and a herdr unit with `herdr.stop_session(label)`.
  - `live_units.py` prints `unit_lookup.place_text(place)`: the bare pane id
    for tmux, `herdr:<session>:<pane>` for herdr. `adopt.py` refuses a unit not
    in tmux.
  - `unit_status.sh` takes each unit's text from
    `session_pane.py read unit <slug> <unit> --lines 400 --joined`, and its
    Claude state and session id from `unit_lookup.py list` (column 4 the state,
    column 6 the session id). An unreadable state prints
    `CLAUDE STATE UNKNOWN`.
  - `scripts/shutdown/settle.py` `_screen` reads a herdr host through
    `read_place` (60 lines), a tmux or unit host by the pane id in its session
    record, and returns `ScreenUnreadable("no pane host")` for any other host.
  - `scripts/shutdown/remote_control.py` resolves a restarted session's pane
    with `record_place` on the registry record it just observed, reads it with
    `read_place` for `/remote-control is active`, and types `/remote-control`
    with `type_place` while the registry shows the session idle; herdr
    `AgentBlocked` uses no attempt.
- `promote_showrunner.py` refuses before anything stops when `read_place`
  fails or `unit_effort.verdict` skips the screen. `stop` types `/exit` with
  `type_place`, waits for the pid to exit, then ends the host session: tmux
  `kill-session`, herdr `herdr.stop_session(label)`. The account directory for
  the resume is the tmux session's `@claude_config_dir` option for a tmux unit,
  and `scripts.whoami.account.process_config_dir(pid)` of the live Claude for a
  herdr unit; no directory refuses with
  `<unit>: cannot read its live Claude's account environment`.

### Invariants

- Callers read and type only through `session_pane.py`. The exceptions are
  `settle._screen`'s tmux branch, the one direct `capture-pane` left, and
  `herdr.py` itself, which reads the visible screen when `agent start` reports
  `agent_not_ready`.
- A herdr session blocked at a prompt is never typed into; `type` exits 4. A
  tmux pane has no such refusal: `type` sends whatever the screen shows, so a
  caller that must not land in a form (effort, promotion) checks
  `unit_effort.verdict` first.
- One account reader: a herdr unit's account comes from
  `account.process_config_dir`, never a second process-environment reader.
- A herdr unit's name never exempts a tmux session from promotion's name
  collision check: only a tmux unit passes its label as `ending`.

### Gotchas

- On Darwin the pane id comes only from the session record's `tmux` field; `ps`
  output is never read for it.
- `herdr agent start … -- claude` adds the binary to argv itself; callers pass
  only the extra arguments.
- Trust checks go only through `herdr.claude_trust` and `trust_command`, exact
  keys only (see "Claude trust before every launch").
- `read --ansi` dimmed-suggestion detection (`\e[2m` after `❯`, which
  `unit_effort.verdict` treats as no draft) is confirmed live on tmux. Its check
  on herdr belongs to the live run with a herdr-hosted Claude in the herdr
  adoption plan's standalone-sessions phase.
- `add_unit.account_for_launch`, resuming a unit with no shutdown record and
  no `--account` or `--config-dir`, still reads the tmux `@claude_config_dir`
  option, herdr units included; a herdr unit's resume account is later work in
  the herdr adoption plan.

### Ruled out

- Parsing Darwin `ps eww` for `TMUX_PANE`: argv tokens read as environment
  entries.
- A second process-environment reader for a unit's account: replaced by
  `account.process_config_dir`.
- `MarkedUnit.pane`: replaced by `place`.

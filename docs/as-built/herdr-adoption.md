# herdr adoption

How the production scripts start and drive Claude sessions under herdr. This
doc grows as the adoption work lands; today it holds the Claude trust check
every launcher runs before it starts a session, and the shared path for reading and typing into herdr panes.

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
  herdr, Claude or Codex, and nothing answers a trust prompt.
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

## Session commands and pane access

`herdr_sessions.py` provides `list`, `attach`, `start` and `resume`. A start or
resume in an untrusted folder refuses with one line to paste: the account's
trust command, then the same invocation with `--cwd` written out. No server is
started before trust is checked.

`session_pane.py` reads and types through `HerdrPlace(session, pane)`.
`MarkedUnit(unit, claude, place)` carries that place; its session name is
`place.session`. `read_place(place, lines=60, *, joined=False, ansi=False)`
uses herdr's recent pane text. `type_place` sends an agent prompt, `keys_place`
sends keys and `where_place` prints `herdr:<session>:<pane>`.
`record_place(record)` resolves the registry pid through `herdr_place_of`;
`session_place(id)` first chooses the newest live record. An unreadable cwd
preserves process placement and unit marks.

The CLI takes `read|type|keys|where` and either `unit <slug> <unit>` or
`session <id>`. A trust, permission or question prompt refuses typing with
exit 4 and a plain line; a missing target exits 1, usage 2 and host failure 3.
Effort changes, promotion, relays, the stall watch, unit status, shutdown
screen checks and the Remote Control fixer use this path. The fixer confirms
the registry process identity and idle state before typing.

Unit marks are written when a herdr pane launches. Existing sessions cannot
be adopted by attaching marks later. Unit resumes stop their own herdr
session and launch with the recorded host name. Promotion reads the live
Claude's account with `account.process_config_dir`; unit model restoration
uses the transcript's last assistant message.

Darwin servers and shutdown helpers start through `scripts/lib/detached.py`.
Readiness includes a live child and its first sign of life. A herdr server's
session socket must belong to that child; a competing server is refused by
exclusive starts and adopted only by adopting starts. Linux uses systemd.
Mac window restores start from the unlocked desktop session; an unavailable
desktop refuses the restart with an unlock-and-restart instruction.

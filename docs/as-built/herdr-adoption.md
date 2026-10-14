# herdr adoption

How the production scripts start Claude sessions under herdr. This doc grows as
the adoption work lands; today it holds the Claude trust check every launcher
runs before it starts a session.

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
  `cd <folder> && <account prefix> claude`. The default account's prefix is
  `env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_REMOTE_MEMORY_DIR`, so an inherited
  account directory never redirects it; a named account uses `account_prefix`.
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
- Launchers pick their account by room; each check reads that account's file:
  `~/.claude.json` for the default account, `<CLAUDE_CONFIG_DIR>/.claude.json`
  for a named one.
- `scripts/herdr/trust_survey.py` is a read-only survey. Per account file
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
- When `nightly_review.py` picks the default account but inherits
  `CLAUDE_CONFIG_DIR`, it checks trust on the inherited directory, the one the
  spawned session reads.

### Ruled out

- Ancestor keys as trust: the 2026-10-13 live check refused them.
- `trusted_by_claude`, `ProjectTrust` and `add_showrunner._project_keys`:
  replaced by `claude_trust`.
- A Codex trust check: no launcher starts interactive Codex.

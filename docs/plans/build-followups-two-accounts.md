# Two accounts: showrunners on one Claude account, unit directors on another

> **Status: IMPLEMENTATION PLAN — the code phases (1–5) are approved; nothing starts until the user says go (G1).** Phases 1–5 build everything and initiate nothing: no account dir, no login, no role. Phase 6 onward waits on the user. The design: each account the user has gets its own config dir, `~/.claude1`, `~/.claude2` and `~/.claude3`. Each holds only that account's login, and everything else is a symlink into `~/.claude`. Unit directors launch with `CLAUDE_CONFIG_DIR` set to the unit account's dir. Showrunners and every other session stay on `~/.claude`'s own login, as today. When the unit account runs low, one command swaps every unit onto a spare account through `/shutdown`'s safe stop and resume. Phased, delegate-ready once approved.

> **As-built disposition: create**

> **Production: build-followups** — unit `two-accounts-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-10 13:23 PDT

Investigate and propose a plan only; change nothing live (the user, 2026-10-10 13:2x PDT): "task either an existing unit dir or a new unit dir to investigate how we can do this. my goal would be to run showrunners in one account and unit directors in another - as there are more unit dirs than show runners it might allow showrunners to burn their weekly allowance slower and we can swap out unit directors maybe a little more frequently - or maybe they both can make it all the way through the week if we do it this way - just keeping two accounts continually acive / i don't want you to do it yet - i want you to investigate it - including maybe we have a second worktree for the second account so that we can keep them up to date / i think the ~.claude would be main always but then we could have ~/.claude1, ~/.claude2, ~/.claude3 as worktrees corresponding to each account that i actually have - and they can just merge changes to main as we make them - / but have someone go figure this out and propose a plan" Mechanism found: CLAUDE_CONFIG_DIR per account (https://code.claude.com/docs/en/authentication.md, Log in with multiple accounts); each dir has its own login, settings and history. Account switching today: scripts/whoami/.

## Proposal

Investigated 2026-10-10 by two-accounts-unit. Nothing was changed.

**How it works.** `CLAUDE_CONFIG_DIR=<dir> claude` runs a session with that dir's login. On Linux the login is `<dir>/.credentials.json`. On the Mac each dir gets its own Keychain entry. The dir also holds its own `.claude.json`, and "all settings, session history, and plugins" live under it (Claude Code docs, env-vars and authentication pages). Two sessions with different dirs run side by side on different accounts.

**Account dirs are symlink farms, not git worktrees** (my call; the user suggested worktrees). `~/.claudeN` holds only what belongs to account N:

- its login
- its `.claude.json`
- its status-line label
- its Remote Control daemon state

Everything else is a symlink into `~/.claude`: `settings.json`, `CLAUDE.md`, `commands/`, `skills/`, `plugins/`, `projects/` (transcripts and memory), `sessions/` (the live session registry), and the rest of the shared list in Phase 1. Reasons:

- **Nothing to keep up to date.** A symlink always shows `~/.claude` main, so no merge into three worktrees ever runs. A worktree's `commands/` and `settings.json` would lag main until merged, while hooks still run main's `scripts/` (every hook path is `$HOME/.claude/scripts/...`). Sessions would then run one commit's skills against another commit's scripts.
- **The whole toolchain keeps working.** About 30 scripts read the session registry and transcripts at a hardcoded `~/.claude/sessions` and `~/.claude/projects`. These include `send.py`, `/shutdown`'s inventory, `unit_lookup`, `showrunners.py`, the stall watch, build-log launches and the conversation-pause hook, which would delete a live session's pause record when it cannot see it. With shared `sessions/` and `projects/`, a session on account N writes where all of them already look.
- **Memory stays one store,** and a session's transcript is visible from every account. That is what lets a unit director move to another account with `claude --resume`.

**Will two accounts last the week?** These are measured numbers, not a forecast:

- **Burn rate today, one account carrying everything:**

  | Account | Measured burn | Per day |
  | --- | --- | --- |
  | claude 1 | 58 → 1 in 45 h | ≈ 30 points |
  | claude 3 | 98 → 0 in 46 h | ≈ 51 points |
  | claude 2 | 100 → 55 in 30 h, still running | ≈ 36 points |

  (`~/.local/state/agent-notes/readings.jsonl`.) At this load one account lasts 2–3 days. A week needs about 250–350 points, and three accounts hold 300.
- **Who spends it:** over the last 7 days, sessions in unit worktrees produced about 70% of output tokens. Showrunners, top-level sessions and the nightly sessions produced about 30%. This comes from transcripts, classified by working directory, give or take 10 points. The input and cache-write shares are close to those.
- **So:**
  - The showrunner account spends about 11–15 points a day and **should last the week alone.**
  - The unit account spends about 25–35 a day and **lasts 3–4 days**: units swap once mid-week onto the third account, and those two together just about cover the week.
  - Two accounts stay active at all times, as the user wants. The split does not add capacity; it decides which sessions stop when an account runs out. Showrunners never do, and only units move.

**What changes for the user:** nothing when starting sessions by hand. `~/.claude`'s login is the showrunner account, and `/login` there switches it as today. Units launch on the unit account by themselves. When the unit account runs low, the quota alert names the spare account with the most left and the one command, `/accounts swap unit "claude N"`. Each account is logged in once in its own dir, by the user, at setup.

**Costs:**

- **Remote Control is per account.** A unit director on account N shows in account N's claude.ai session list, not the one the phone is signed into. Showrunners stay visible on the main account and relay as today. Phase 2 checks this live.
- **`.claude.json` is per dir.** A new dir is seeded with project trust, onboarding flags and the user-scope MCP servers (today `brp`), so launches never stop at a dialog. An MCP server added later to one dir does not reach the others; `/accounts status` reports the difference.
- **Claude Code may replace a symlinked file when it writes it,** for example `settings.json` after `/model`. Phase 2 tests this; the launch check catches a replaced link either way.

**Calls I made (each a one-line revert):**

- Showrunners, top-level sessions, the nightly review, the fix pipeline and the `send.py` relay stay on `~/.claude`'s login. Only units get a role account, because they spend about 70% and they are the sessions that can be stopped safely and resumed.
- A swap reuses `/shutdown`'s safe stop and resume instead of moving each unit at its next checkpoint.
- The Mac is out of scope: units launch only on natedev (`add_unit.py` uses `systemd-run` and tmux). Mac sessions stay on `~/.claude`.
- A long-lived `CLAUDE_CODE_OAUTH_TOKEN` per launch is ruled out: such a token "can't establish Remote Control sessions", and every unit runs with `--remote-control`.

**The user's answers** (2026-10-10, via the showrunner):

1. Units leaving the phone's Remote Control list, with showrunners relaying: "yes".
2. Linked directories instead of worktrees: "linked directories are fine".
3. "using claude 3 for units is also fine". `~/.claude` stays on claude 2, and claude 1 is the mid-week spare.

The user also said: "i do't want you to start the separate accounts until i have a clear sense of what happens next". Then, via the showrunner: "you can build everything that is related to running showrunners on two accounts but don't start it yet - make sure that you cover .zshrc aliases if you need to - possibly i need the ability to easily start claude sessions in the correct account myself / i'll need to know how to correctly log out and login and stay in the correct account - all of these things - so prepare efverything but don't initiate -". Phase 5 covers the aliases and the guide.

## What happens next

Each step lists what changes live, what the user does, and how to undo it. Steps 1 and 2 build everything and initiate nothing. Until the unit role is set in step 4, every launch works exactly as today, because no role means `~/.claude`.

1. **Build the tooling (Phases 1–4). Code only; nothing live changes, and the user does nothing.**
   - Phase 1: the account tool, `accounts.py`, which creates, checks and reports account dirs.
   - Phase 2: unit launch and restart learn the unit account.
   - Phase 3: the quota notes and alerts handle two accounts in use at once.
   - Phase 4: `/accounts swap`.

   Phase 2 also edits `settings.json`: allow rules and sandbox write entries for `~/.claude1`–`~/.claude3`. That edit reaches main only with the user's go.
2. **Shell commands and the guide (Phase 5). Nothing live changes.**
   - The aliases are `claude1`, `claude2`, `claude3` and `claude-unit`; plain `claude` is unchanged. natedev commits them to `/etc/nixos`, and the user rebuilds when ready. Until a dir exists, each alias runs plain `claude` or refuses.
   - `~/.claude/docs/accounts.md` says how to start a session on each account, how to log in and out without moving `~/.claude` off claude 2, and how to see which account a session is on.
3. **Prove one account dir works (Phase 6). The first live step, only when the user says go; about 15 minutes of their time.**
   - The unit creates `~/.claude3`. The user runs `claude3` and logs in as claude 3 (`rubrics.dogwood-1f@icloud.com`).
   - The unit runs one throwaway session on claude 3 and checks its login, messaging both ways, `/shutdown`, resuming across accounts, and that the links survive `/model`. The user checks Remote Control on the phone.
   - The probe stops. Every existing session stays on claude 2. Undo: `rm -rf ~/.claude3`.
4. **Cutover (Phase 7), at a time the user picks.**
   - The user logs in `~/.claude1` with `claude1` (the spare).
   - Unit directors stop safely, as `/shutdown` does it, and resume on claude 3 with their conversations intact. New units start on claude 3.
   - Showrunners and the user's own sessions stay on claude 2.
   - Undo: `/accounts swap unit default`.
5. **During the week.** When claude 3 runs low, the alert names `/accounts swap unit "claude 1"`. Nothing switches by itself.
6. **Day 7 (Phase 8).** A readout gives points per account per day, the times of any swaps, and whether the showrunner account lasted the week.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-two-accounts`, branch `build-followups-two-accounts` (unit `two-accounts-unit`); name both in every dispatch.
- **Project started:** 2026-10-10T21:07:53.634+00:00
- **Stack:** Python 3 standard library, checked by basedpyright. zsh launch strings. Claude Code 2.1.296.
- **Layout:** new `scripts/accounts/` (`accounts.py`, `test_accounts.py`) · `scripts/production/` · `scripts/shutdown/` · `scripts/whoami/` · `scripts/message/` · new `commands/accounts.md` · `settings.json` · `docs/quota_alerts.md` · `docs/as-built/shutdown-and-restart.md`. Outside the repo, live, written only in Phases 6 and 7: `~/.claude1`, `~/.claude2`, `~/.claude3`, and `config/accounts.json` (gitignored like `config/showrunners.json`).
- **Key files:**
  - `scripts/whoami/agent_accounts.py`:
    - `claude_config_dir()` 224: `CLAUDE_CONFIG_DIR`, else `~/.claude`.
    - `claude_config_file()` 228-236: `~/.claude.json` only for `~/.claude`, else `<dir>/.claude.json`.
    - `claude_credentials()` 246-254: on darwin it reads the Keychain only without `CLAUDE_CONFIG_DIR`.
    - `claude_live()` 280-316: `claude auth status`, then `/api/oauth/usage` with the dir's access token.
    - `claude_on_disk(config_dir)` 318.
    - `live_reports()` 557.
  - `scripts/whoami/account.py`:
    - `process_config_dir(pid)` 118-159 reads `/proc/<pid>/environ`, or `ps eww` on darwin.
    - `account_of(pid)`.
    - `write_label` writes `<dir>/account-label`; the SessionStart hook at `settings.json` 186 runs it.
  - `scripts/whoami/agent_notes.py`: `apply` 188-245 marks the env's own account `active` and every other account `inactive`; `update` 255-270. `quota_alert.py` 247-254 and 653 act on `state: active`; `current_session()` reads the registry at 82 and 666. `run_out.py`. `whoami.py`.
  - `scripts/production/add_unit.py`:
    - `launch_session` 660-693 builds `ENABLE_TOOL_SEARCH=true command claude …; exec zsh` and runs it under `systemd-run --user --scope … tmux new-session … zsh -ic`.
    - `launch_environment` 696-702 strips every `CLAUDE_*` variable.
    - The launch check is near 796.
    - tmux gives a new session the server's environment, not the client's, so a variable reaches `claude` only inside the command string or through `-e`.
  - `scripts/shutdown/restart.py`:
    - `_run` 262-273 strips `CLAUDE_*`.
    - The resume command is built at 355-373.
    - Units relaunch through `add_unit.py --resume` at 430-455.
    - The record stores no config dir.
  - `scripts/shutdown/inventory.py`: 1178 lists `~/.claude/sessions`, then filters by `account_of`; 1103 reads `~/.claude/projects` for the last model. `record.py` holds the record types. `shutdown.py` holds the verbs and `conduct`.
  - `scripts/message/sessions.py` 85-86: `sessions_dir()` is `$NOTIFIER_SESSIONS_DIR` or `~/.claude/sessions`. `send.py` 670-701: the relay is a headless `claude -p` with `CLAUDE_*` stripped, so it runs on `~/.claude`'s account.
  - `scripts/delegate/remove_seats.py` 98-259 and `scripts/delegate/implement.sh` 316: `claude agents --json` and `claude rm` act on the caller's config dir.
  - `commands/showrunner/produce.md` 235-237: a manual unit resume runs `claude --resume` from the showrunner's own shell.
  - `settings.json`:
    - 43 allows `Edit(~/.claude/**)`.
    - 263-265 sandbox `filesystem.allowWrite` lists `~/.claude`.
    - 243 is the status line, which reads `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/account-label`.
  - `scripts/nightly_review/nightly_review.py`: the quota gate at 84-97 assumes one active Claude note; the launch is at 109-116.
  - `docs/as-built/shutdown-and-restart.md`, `docs/quota_alerts.md`: the protocols this plan extends.
- **Test lanes:** `scripts/accounts/test_accounts.py` (new), `scripts/production/test_add_unit.py`, `scripts/shutdown/test_*.py`, `scripts/whoami/test_*.py`. Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'` for each touched dir, from the worktree root.
- **Lint:** `basedpyright <files>`: a pass ends `0 errors, 0 warnings, 0 notes`. Lint once after the seat's edits; on failure, fix what it names and lint once more.
- **Facts (checked 2026-10-10):**
  - **Session records** (`~/.claude/sessions/<pid>.json`) carry `messagingSocketPath` `/run/user/1000/cc-socks/<pid>.sock`. That is the XDG runtime dir, not the config dir. They also carry `peerFeatures` including `reply_across_default_dirs`. A `<pid>.<sha>.key` sits beside each record.
  - **`~/.claude.json`:**
    - `oauthAccount` (identity).
    - `projects`, 33 entries holding `hasTrustDialogAccepted`, `allowedTools`, `enabledMcpjsonServers`, `disabledMcpjsonServers`, `mcpServers` and `hasClaudeMdExternalIncludesApproved`. Worktrees under `~/worktrees` inherit trust from the `/home/natepiano` entry.
    - Top-level `mcpServers` `{brp}`.
    - `hasCompletedOnboarding` and `lastOnboardingVersion`.
  - **Accounts:**

    | Note | Login | Resets |
    | --- | --- | --- |
    | `claude 1` | `foyer_penal0c@icloud.com` | 10-12 02:00 |
    | `claude 2` | `browses31_perks@icloud.com` | 10-15 07:00 |
    | `claude 3` | `rubrics.dogwood-1f@icloud.com` | 10-10 09:00 |

    The notes are in `~/rust/hanadocs/agents/`. `~/.claude` is logged in as claude 2.
  - **Not created in `~/.claude`:** `keybindings.json`, `agents/` and `output-styles/`. The farm links only entries that exist, plus the dirs Claude Code creates.
  - **Burn rates and role split:** as in the Proposal. The 7-day split came from a one-off script in this session's scratchpad (`role_tokens.py`). After cutover each role has its own account, so `accounts.py burn` reads the split from per-account readings and needs no transcript scan.
- **Invariants:**
  - **No variable means the default account.** Absent `CLAUDE_CONFIG_DIR` means `~/.claude` and its login. A launch on the default account sets nothing.
  - **No copies of shared content.** In an account dir, every shared entry is a symlink into `~/.claude`. Setup never replaces a real file or dir; it reports it.
  - **Identity stays per dir.** `.credentials.json`, `.claude.json`, `account-label`, `daemon/`, `daemon-auth-*`, `jobs/`, `backups/`, `.oauth_refresh.lock*`, and anything not on the shared list are never linked.
  - **Unknown means not this account** (shutdown as-built): an unreadable environment never defaults to `~/.claude`.
  - **Account isolation from the shutdown as-built holds.** A command for account A never stops or messages a session on account B.
  - **Tests never touch live dirs.** They never touch `~/.claude*` or `~/.claude.json`: `HOME` points at a temp dir.
  - Python: no `Any`, a `TypedDict` per JSON shape, no file-level ignores.
  - Times shown to the user are America/Los_Angeles.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G0 | Phase 1 | the user | cleared 2026-10-10 for the code phases 1–5: "prepare everything but don't initiate" |
| G1 | Phase 6 | the user | the user says go, then the user has run `CLAUDE_CONFIG_DIR=~/.claude3 claude` once and finished `/login` as claude 3 |
| G2 | Phase 7 | the user | the user picks the cutover time and the first unit account (question 3), and logs in the remaining dirs |
| G3 | Phase 8 | the clock | 7 days after cutover |

## Phases

### Phase 1 — Account dirs: build, check and report, without touching live dirs  · status: done

#### As-built

- `scripts/accounts/accounts.py` builds an account dir `~/.claudeN` (label `claude N`; any other label is refused) as a symlink farm over the `SHARED` entries of `~/.claude` (user content and cross-session state; `projects` and `sessions` always linked). Each dir keeps its own `IDENTITY_ENTRIES`: `.credentials.json`, `.claude.json` and `account-label`. `.claude.json` is seeded once, 0600 with O_EXCL, from `~/.claude.json` filtered to `TOP_LEVEL_SEED_KEYS` and per-project `PROJECT_SEED_KEYS`; `oauthAccount` and caches stay out.
- Every path comes from `Path.home()` at call time: `accounts_home()`, `main_config_dir()`, `account_dir(label)`, `accounts_config_file()` (`~/.claude/config/accounts.json`), `roles_history_file()` (`~/.local/state/accounts/roles.jsonl`).
- `setup(label) -> list[str]` and `check(label) -> list[str]` return every problem, empty when fit. `setup` leaves a correct link, replaces a wrong-target link and refuses a real file where a shared link belongs. `check` reports missing dir, missing link, wrong target, real file and `not logged in`, reading login through `whoami.agent_accounts.claude_on_disk` (skipped when `.claude.json` is a link).
- Roles: `config/accounts.json` is `{"unit": "claude N"}`; `unit` is the only role. `role_account("unit")` treats only an absent key as default and raises `AccountsError` on a non-string value. `set_role(role, label | "default")` writes `accounts.json` atomically, then appends `{at, role, label}` to `roles.jsonl`; a failed append restores the previous file.
- `account_statuses()` / `status` report label, login, roles, check problems and `mcpServers` drift per dir, plus `unclassified` entries for named dirs only. CLI `main(argv) -> int`: `setup`, `check`, `status`, `role`, `env`, `set-role`.
- whoami is imported by putting the repo root on `sys.path`, then `from scripts.whoami import account` and `from scripts.whoami.agent_accounts import claude_on_disk`.

**Files:**
- `scripts/accounts/accounts.py` — the account tool and CLI
- `scripts/accounts/test_accounts.py` — 28 tests: `HOME` set to a temp dir, real symlinks, login and label lookups stubbed
- `.gitignore` — ignores `/config/accounts.json`

**Binds later work:** `check(label) -> list[str]` (empty when fit; the launch check puts the first problem in its refusal); `role_account("unit") -> DefaultAccount | NamedAccount(label, dir)`; `launch_prefix("unit") -> str`, `CLAUDE_CONFIG_DIR=<quoted dir> ` or empty for the default account; `set_role`; `main(argv) -> int`. Paths come from `Path.home()`; tests set `HOME` to a temp dir. The symlink refusals for the account dir and identity files apply wherever an account dir is used, the `exec` command included. Import as `from scripts.accounts import accounts` after putting the repo root on `sys.path`. Role state is read by unit launch, quota burn and swap; role state and the setup/check refusals are documented in the user's guide.

**Gotchas:** With no `config/accounts.json` every role resolves to the default account and `launch_prefix` is empty, so launches are byte-identical to before. A symlinked account dir or identity file would silently share one login across accounts; `setup` and `check` refuse both.

**Ruled out:** An `ACCOUNTS_HOME` override beside `HOME`, since a second home setting disagrees with `claude_on_disk`, which reads `~/.claude.json` via `Path.home()`.

### Phase 2 — Units launch, resume and restart on the unit account  · status: done

#### As-built

- `scripts/production/add_unit.py` resolves `accounts.role_account("unit")` once per launch and passes the selection to `launch_session`. A named account must pass `accounts.check(label)`, else exit 2: `add_unit: unit account <label>: <first problem>; /accounts status`. The launch guard (`launch_permission`) runs with `CLAUDE_CONFIG_DIR` set to that account's dir; `launch_environment` strips `CLAUDE_*`. The tmux command string is `accounts.account_prefix(selected) + "ENABLE_TOOL_SEARCH=true command claude …"`, so tmux passes the prefix whatever its server's environment. With no `config/accounts.json` the prefix is empty and the guard env is unchanged.
- `accounts.account_prefix(selected: AccountSelection) -> str` renders the env prefix for a selection (empty for the default account); `launch_prefix(role)` is `account_prefix(role_account(role))`.
- The manual unit resume in `commands/showrunner/produce.md` runs `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py env unit` first, stops on a nonzero exit, and puts its output verbatim before both `shutdown.py launch-blocked --restart-of <id>` and `claude --resume <id> …`; no `eval`.
- Shutdown inventory reads each session's account and config dir together at one boundary (`AttributedSessionConfiguration`) and stores the dir as `config_dir` on the session snapshot, absent meaning `~/.claude`; an unreadable environment leaves the session unattributed. `ShutdownSessionEntry` has no `config_dir`, and an entry-level key from an older record is ignored on read. `restart.py` puts `CLAUDE_CONFIG_DIR=<quoted dir> ` before `ENABLE_TOOL_SEARCH=true` for a non-unit resume whose dir is not `~/.claude`; units relaunch through `add_unit.py` on the current role.
- Claude seats inherit the owner's `CLAUDE_CONFIG_DIR`; `remove_seats.py` sets none, and its tests pin the inheritance.
- `settings.json` allows `Edit(~/.claude1/**)`…`Edit(~/.claude3/**)` beside `Edit(~/.claude/**)` and lists `~/.claude1`…`~/.claude3` in sandbox `allowWrite`, since a session on account N writes memory under `~/.claudeN/projects/…`. This change reaches main only with the user's go.

**Files:**
- `scripts/accounts/accounts.py` — `account_prefix`.
- `scripts/production/add_unit.py` — unit-account check, guard env, launch prefix; puts the repo root on `sys.path`.
- `scripts/production/test_add_unit.py` — prefix, refusal, held named account, role change after readiness, manual resume.
- `scripts/shutdown/inventory.py` — `config_dir` capture on the session snapshot.
- `scripts/shutdown/record.py` — ignores an obsolete entry-level `config_dir`.
- `scripts/shutdown/restart.py` — prefix for non-unit resumes.
- `scripts/shutdown/test_inventory.py`, `test_record.py`, `test_restart.py` — capture, round trip, prefix.
- `scripts/delegate/test_remove_seats.py` — owner's dir inherited.
- `commands/showrunner/produce.md` — manual unit resume.
- `settings.json` — Edit allow and sandbox `allowWrite` for the account dirs.
- `pyrightconfig.json` — the scripts/production environment adds `"."` so `scripts.accounts` resolves.
- `docs/as-built/shutdown-and-restart.md` — config dir on the snapshot and the restart rule.

**Binds later work:** add_unit resolves the unit role once per launch, refuses a named account failing `accounts.check` with exit 2 `add_unit: unit account <label>: <first problem>; /accounts status`, and asks the launch guard on that account's dir, so after a role change a unit relaunched through add_unit lands on and is judged against the new account (the swap of the unit account relies on this). A rule added to `accounts.check` reaches the unit launch with no change to add_unit. `account_prefix(selected)` and the `accounts.py env <role>` verb render the account prefix for any launch, including an `exec` verb. Restart restores a non-unit session on its recorded `config_dir`. `shutdown.py launch-blocked` with no account argument judges the default account (`CLAUDE_*` stripped), so a swap-time shutdown check passes the target dir. The add_unit refusal is user-actionable; the user's guide covers it under "When a unit will not start".

**Gotchas:** a check about a named account through `shutdown.py launch-blocked` must name the account or set its dir, or it judges the default one; a held named unit account refuses with `…; /shutdown restart first`. Code that imports `scripts.accounts` from `scripts/production` needs the repo root on `sys.path`.

**Ruled out:** a `config_dir` field on `ShutdownSessionEntry` (settle replaces the session snapshot, so a second copy goes stale); an explicit `CLAUDE_CONFIG_DIR` in `remove_seats.py` (inheritance already carries it).

### Phase 3 — Quota with two active accounts  · status: done

#### As-built

- With accounts configured, `~/.claude`'s login (showrunner) and the unit account are both `state: active`; spares are `inactive`, yet each fresh reading still reaches its note and `readings.jsonl`. `claude_live` and `claude_credentials` take an optional `config_dir`; `None` keeps the single-dir behavior. With no `config/accounts.json` and no named dir, output and launches are unchanged.
- One frozen `ClaudeAccountSelection` (`default_account`, `unit_account`, `multiple_accounts`, `named_accounts`) per operation, built by `accounts.claude_account_selection()` from `checked_named_accounts()` and `account_identity(dir)`; consumers take it as a value and never re-resolve roles.
- Alerts name label and role (`Quota alert: claude 3 (unit directors) at 4%`); a unit-account alert adds `swap: /accounts swap unit "<label>"` for the fit spare with the most remaining, or `no spare account has more left`. In multi-account mode an episode closes only on that account's recovery or reset, never on a role change; a low spare opens no episode.
- `whoami.py` prints each active Claude account with its role. `run_out.py` is unchanged: dailies already projects each active note. Nightly gates on the showrunner account's note and starts with `CLAUDE_*` stripped.
- `accounts.py burn --since <ISO> [--until]`: per account, points per day from `readings.jsonl`, labelled by the role served (`roles.jsonl` plus `~/.claude`'s login, matched by each dir's note label); a weekly reset starts a new span; an unreadable historical dir identity makes burn refuse.

**Files:**
- `scripts/whoami/agent_accounts.py` — `ClaudeRole`, `KnownClaudeAccount`, `UnreadableClaudeAccount`, `CheckedNamedClaudeAccount`, `ClaudeAccountSelection`; `live_reports` reads named dirs from the selection.
- `scripts/whoami/note_file.py` — standard-library leaf: `AGENTS_DIR`, `FIELD`, `Note`, `read_note`, `read_notes`.
- `scripts/whoami/agent_notes.py`, `quota_alert.py`, `whoami.py`, `account.py` — consumers of the selection and the note leaf.
- `scripts/accounts/accounts.py` — `claude_account_selection`, `checked_named_accounts`, `account_identity`, `burn` and role history.
- `scripts/nightly_review/nightly_review.py` — `quota_for_roles`, `start_for_roles`, `launch_environment(selection)`.
- `pyrightconfig.json` — `scripts/accounts` execution environment, extraPaths `[".", "scripts/whoami"]`.
- `docs/quota_alerts.md` — a notice stops new Claude work only for sessions on that account; the showrunner relays a unit-account notice to its units; episode lifetime with two active accounts.

**Binds later work:** `accounts.py` imports whoami modules by top-level name (`import account`, `import agent_accounts`); consumers put the repo root on `sys.path`, then `from scripts.accounts import accounts`. Role resolution goes through `ClaudeAccountSelection` / `claude_account_selection()` and the `agent_accounts` types, never a separate role lookup per note; named dirs come from `checked_named_accounts()`, identity from `account_identity(dir)`. The note label is the downstream identity. burn reads role history from `roles.jsonl` and resolves each line's dir through its live identity. An acknowledgement holds across spare and unit role changes until that account recovers (`test_acknowledgement_survives_spare_and_unit_role_changes_until_quota_recovers`). Lint by directory: `basedpyright scripts/accounts scripts/whoami scripts/nightly_review scripts/shutdown scripts/production`.

**Gotchas:** `account.py`, `agent_accounts.py` and `note_file.py` never import `scripts.accounts`; lazy and `TYPE_CHECKING` imports both count as cycle edges, and a file-list lint misses the cycle. A dir label and its login's note label can differ. A role change is not a recovery; treating it as one erases the acknowledgement. Per-note role lookups straddle swaps, hence one selection per operation. Nightly otherwise inherits tmux's `CLAUDE_CONFIG_DIR`.

**Ruled out:** consolidating the readings path's three homes, since it reaches `seat_usage.py`, outside the accounts scripts.

### Phase 4 — One-command swap of the unit account  · status: done

#### Work Order

**Goal:** `/accounts swap unit "claude N"` moves every unit director onto account N: each one reaches a safe stop and resumes in place on N.

**Constraints from prior phases:** Phase 1 shipped `scripts/accounts/accounts.py`. Every path comes from `Path.home()` at call time; there is no `ACCOUNTS_HOME`, and tests set `HOME` to a temp dir. `check(label) -> list[str]` returns every problem, empty when the account is fit, and already refuses an account dir that is a symlink and any symlinked identity file (`.credentials.json`, `.claude.json`, `account-label`). `role_account("unit") -> DefaultAccount | NamedAccount(label, dir)`, `launch_prefix("unit") -> str` (empty for the default account) and `main(argv) -> int`. Other scripts import it by putting the repo root on `sys.path`, then `from scripts.accounts import accounts`. Phase 2 shipped the unit launch. `add_unit.py` resolves `accounts.role_account("unit")` once per launch, refuses a named account that fails `accounts.check` with exit 2 (`add_unit: unit account <label>: <first problem>; /accounts status`), asks shutdown's launch guard with `CLAUDE_CONFIG_DIR` set to that account's dir, and launches with `accounts.account_prefix(selected)` before `ENABLE_TOOL_SEARCH=true`. So after `set-role unit <label>`, a unit that `restart` relaunches through `add_unit.py` lands on the new account and is judged against it, not against the old account that is going down. A shutdown record keeps each session's dir as `config_dir` on the session snapshot (absent means `~/.claude`), not as a field of the record entry, and `restart.py` puts `CLAUDE_CONFIG_DIR=<dir> ` before a non-unit resume. `shutdown.py launch-blocked` with no account argument runs with `CLAUDE_*` stripped and judges the default account, so any shutdown check about a named account names that account.

Phase 3 shipped quota with two active accounts. `accounts.py` itself imports the whoami modules top-level (`import account`, `import agent_accounts`, with `scripts/whoami` on `sys.path`); `pyrightconfig.json` has a `scripts/accounts` execution environment with `extraPaths` `[".", "scripts/whoami"]`. The account value types live in `scripts/whoami/agent_accounts.py`: `ClaudeRole`, `KnownClaudeAccount(login, label)`, `UnreadableClaudeAccount`, `CheckedNamedClaudeAccount(label, directory, identity)` and the frozen `ClaudeAccountSelection(default_account, unit_account, multiple_accounts, named_accounts)` with `is_default_login(login)` and `roles_for_login(login)`. `accounts.claude_account_selection()` builds that value from `checked_named_accounts()` (the named dirs that pass `check`) and `account_identity(dir)` (the dir's login and its quota note label). An operation resolves it once and passes it down; it never re-resolves per step. The note label is the account's identity everywhere downstream (quota notes, shutdown, burn); a dir label such as `claude 3` can name a dir logged in to an account whose note label differs. `burn` reads unit role history from `~/.local/state/accounts/roles.jsonl` (`RoleChange(at, role, label)`, label being the dir label or `default`) through `_unit_role_history(default_label)`, which maps each record's dir label to that dir's current note label and refuses when a historical dir's identity is unreadable. `account.py`, `agent_accounts.py` and `note_file.py` (the standard-library leaf holding `AGENTS_DIR`, `Note`, `read_note`, `read_notes`) never import `scripts.accounts`, lazily or under `TYPE_CHECKING`: either form is an import-cycle edge. Lint by directory, never a file list, because only that finds a cycle.

Shutdown facts for the `restart` seat: `shutdown.py down` dispatches to `settle.down(account, here, only)`, which runs preflight, calls `settle.begin(login, requested_by, only)` locally and `begin` on the other machine, then starts the conductor; the conductor runs `shutdown.py conduct <login>`, which returns `settle.conduct(...)`. `settle.down` returns nonzero only with `nothing was shut down`: a failed remote `begin` or conductor launch cancels the record before it returns. `settle.conduct` returns 0 for a completed stop, a cancel and a partial stop alike. `settle.py` does not import `restart.py`; `shutdown.py` imports both. Inventory sessions are `unit`, `seat` (with `owner`: `{"kind": "director", "session_id": …}` or no director), `showrunner` and `top-level`; sessions whose account cannot be read are listed as unattributed.

**Spec:**

- **`accounts.py swap unit <label>`** resolves `claude_account_selection()` once and works from that value throughout. The old account is the selection's `unit_account`; when it is `UnreadableClaudeAccount`, swap refuses before writing anything. Every shutdown call and inventory check names the old account by its resolved note label, never by the dir label. Swap calls shutdown only as a subprocess (`shutdown.py status <old> --json`, `shutdown.py down <old> --restart`), never by import. It refuses, writing nothing, when:
  - the target fails `check`
  - the target is `~/.claude`'s login
  - the target is already the unit account
  - the old account's inventory holds a session that would not come back on the new account. A `unit` moves, and so does a `seat` whose owner is the director of a `unit` in that inventory. Anything else refuses, and the refusal names it: a showrunner, a top-level session, a seat with no director or with a director that is not one of those units, and an unattributed session.

  Otherwise it runs `set-role unit <label>`, then `shutdown.py down <old> --restart`.
- **`swap unit default` is the undo.** It moves every unit back to `~/.claude`'s login. The old account is the one the role names, and the target is the default dir. Naming `~/.claude`'s login by its label is still refused, with the hint `use default`. It prints the shutdown's output and `units come back on <label> when <old> is down`.
- **When a swap fails.** The role is written before `down`, so units that relaunch land on the new account.
  - `down` exits nonzero (it refused or cancelled before any session stopped): swap writes the previous role back, prints `down`'s refusal and `unit account unchanged: still <old>`, and exits nonzero. The same swap can run again at once.
  - The shutdown starts and ends `stop partial`: the role stays on the new account and nothing restarts; the user is alerted as today, and the alert for a record with `then: restart` adds the line `shutdown.py down <old> --restart` to stop the rest and restart all of them on the new account.
- **Role history keeps the account, not only the dir.** `set_role` resolves the selected dir's identity at write time and stores its note label as `account_label` on the new `RoleChange`, `default` included (the default dir's note label). `set-role` refuses, writing nothing, when that identity is unreadable. `_unit_role_history` uses a record's `account_label`; a record written before this phase has none and keeps today's path (the dir's current identity, refusing when unreadable). So a dir that logs out or logs in to another account after a swap leaves past `burn` attribution unchanged.
- **`shutdown.py down --restart`** records `then: restart` through `begin`, locally and in the remote `begin` argv, so the record holds it from the moment it exists, before the conductor starts. A record without `then` (written before this phase) reads as `then: stop`. The `conduct` command in `shutdown.py` runs `settle.conduct`, then reads the record: only a final `down` with `then: restart` runs `restart` for that account. It decides from the record, never from `conduct`'s exit code. `restart` stays out of `settle.py`, so no `settle` ↔ `restart` import appears. A cancel or a `stop partial` does not restart.
- **`--only` with `--restart`** restarts only the selected sessions; every other session on the account keeps running. Phase 7's first move of running units uses it.
- **The requester.** `swap` runs from a session on another account, so the requester is not in the stopped inventory. `down` must not wait for that requester's `ready`. Test this.
- **`commands/accounts.md`** — `/accounts [status | setup "claude N" | swap unit "claude N"]`. It runs the matching verb and shows its output unchanged. It states that switching the default account remains `/login` in a `~/.claude` session.
- `docs/as-built/shutdown-and-restart.md`: `--restart`.
- Rename the burn test whose name begins `test_burn_refuses_to_` to `test_burn_refuses_an_unreadable_historical_account_identity`; its old name holds a word the repo's hooks block.

**Files:**

- `scripts/accounts/accounts.py` — `swap`, `account_label` on role history.
- `scripts/accounts/test_accounts.py` — each swap refusal, the dry run, rollback on a refused `down`, the partial-stop line, seats owned by units moving, and burn attribution surviving a logout and a re-login to another account after a swap.
- `scripts/shutdown/shutdown.py` — `--restart`; `conduct` restarts after `down`.
- `scripts/shutdown/settle.py` — `begin` and `down` carry `then`; the partial-stop alert's retry line.
- `scripts/shutdown/record.py` — `then` field.
- `scripts/shutdown/test_shutdown_cli.py` — `down --restart`, `--only` with `--restart`.
- `scripts/shutdown/test_settle.py` — `then` through `begin` on both machines; nonzero `down` leaves no record.
- `scripts/shutdown/test_stop.py` — completed `down` restarts, cancel and `stop partial` do not.
- `scripts/shutdown/test_record.py` — `then` round trip, and a record without `then` reads as `stop`.
- `commands/accounts.md` — new.
- `docs/as-built/shutdown-and-restart.md` — swap.

**Seats:** `2 writers`, each writing its own tests; they meet only at the `shutdown.py down <old> --restart` argv and the `status --json` inventory shape.
- `swap`: `accounts.py`, `test_accounts.py`, `commands/accounts.md`.
- `restart`: `shutdown.py`, `settle.py`, `record.py`, `test_shutdown_cli.py`, `test_settle.py`, `test_stop.py`, `test_record.py`, the as-built.

**Acceptance gate:**

- Tests pass, and `basedpyright scripts/accounts scripts/whoami scripts/nightly_review scripts/shutdown scripts/production` reports 0 errors, 0 warnings, 0 notes (it exits 3 only for the missing `.venv`).
- `swap --dry-run` prints the role write and the `down … --restart` argv.
- A shared-login fixture, where the unit dir's label and its login's note label differ, shows swap checking and stopping the account by its note label.

### Phase 5 — Starting sessions by account, and the user's guide  · status: todo

#### Work Order

**Goal:** the user can start a session on any account with one word, and has a guide that says how to start sessions, how to log in and out without moving `~/.claude` off its account, and how to see which account a session is on. Nothing here creates a dir or logs anything in.

**Constraints from prior phases:** Phase 1 shipped `scripts/accounts/accounts.py`. Every path comes from `Path.home()` at call time; there is no `ACCOUNTS_HOME`, and tests set `HOME` to a temp dir. `check(label) -> list[str]` returns every problem, empty when the account is fit, and already refuses an account dir that is a symlink and any symlinked identity file (`.credentials.json`, `.claude.json`, `account-label`). `role_account("unit") -> DefaultAccount | NamedAccount(label, dir)`, `launch_prefix("unit") -> str` (empty for the default account) and `main(argv) -> int`. Other scripts import it by putting the repo root on `sys.path`, then `from scripts.accounts import accounts`. Phase 2 shipped `accounts.account_prefix(selected) -> str`: `CLAUDE_CONFIG_DIR=<quoted dir> `, or empty for `DefaultAccount`. `launch_prefix(role)` is `account_prefix(role_account(role))`, and the `env <role>` verb prints it. `add_unit.py` already calls `accounts.check(label)` before every unit launch, so a rule added to `check` reaches the unit launch with no change to `add_unit.py`; its refusal reads `add_unit: unit account <label>: <first problem>; /accounts status`. The manual unit resume in `commands/showrunner/produce.md` runs `accounts.py env unit` first and stops on a nonzero exit.

Phase 3 shipped quota with two active accounts. `accounts.py` imports the whoami modules top-level (`import account`, `import agent_accounts`, with `scripts/whoami` on `sys.path`); `pyrightconfig.json` has a `scripts/accounts` execution environment with `extraPaths` `[".", "scripts/whoami"]`. The account value types live in `scripts/whoami/agent_accounts.py`: `KnownClaudeAccount(login, label)`, `UnreadableClaudeAccount`, `CheckedNamedClaudeAccount(label, directory, identity)` and the frozen `ClaudeAccountSelection(default_account, unit_account, multiple_accounts, named_accounts)`. `accounts.claude_account_selection()` builds it once per operation from `checked_named_accounts()` (the named dirs that pass `check`) and `account_identity(dir)` (the dir's login and its quota note label, or `UnreadableClaudeAccount` when the dir is logged out or its files cannot be read). So `check` decides which named dirs feed live readings, role activity and quota's spare ranking, and whether the unit account resolves. `multiple_accounts` comes from `multiple_accounts_configured()`: `config/accounts.json` exists or a named dir exists, whatever `check` says. `check` today reports `accounts: <dir> is not logged in` for any `UnreadableClaudeAccount`. `account.py` with no arguments prints the calling session's account (from `CLAUDE_CONFIG_DIR`); `whoami.py` in multi-account mode prints each account that holds a role, with its role, and omits spares, so it does not say which account the calling session is on. A low-quota acknowledgement holds across swaps: an account made spare and then selected again stays quiet until its own recovery or reset. `account.py`, `agent_accounts.py` and `note_file.py` never import `scripts.accounts`, lazily or under `TYPE_CHECKING`. Lint by directory, never a file list, because only that finds an import cycle.

**Spec:**

- **`accounts.py exec <claude N | unit> [claude args…]`** resolves the target and then `os.execvpe("claude", …)`.
  - `claude N` that is `~/.claude`'s login runs plain `claude`, with `CLAUDE_CONFIG_DIR` removed. So `claude2` today is the same session as `claude`.
  - Any other `claude N` runs with `CLAUDE_CONFIG_DIR=~/.claudeN`.
  - `unit` follows the role, and with no role runs plain `claude`.
  - It refuses with exit 2, without starting `claude`, when:
    - the dir does not exist: `accounts: ~/.claudeN is not set up; /accounts setup "claude N" first`
    - a link is broken
    - the dir is logged in as another account: `accounts: ~/.claudeN is logged in as <login>, not claude N; see Log out and in in ~/.claude/docs/accounts.md`

    A dir that is set up but logged out is allowed, so the login itself can run through `exec`. A dir whose identity files exist but cannot be read is not logged out: it refuses with its problem. Give the boundary one semantic readiness value (ready, logged out, logged in as another account, not set up, broken links, unreadable) that `exec` and `check` both read: `exec` starts on ready and logged out, `check` passes only ready.
- **`check` gains the login match.** A dir whose login is not the `login:` of its `claude N` note is a problem, and so is a dir with no `claude N` note. `status` shows it, and so do the unit launch (Phase 2) and `swap` (Phase 4). Because `check` feeds the selection, a mismatched dir also drops out of `named_accounts` (no live reading, no spare ranking), a mismatched unit dir makes `unit_account` an `UnreadableClaudeAccount`, and `multiple_accounts` stays true. `account_identity` keeps reporting the dir's actual login, so `status` can say `logged in as <login>, not claude N`.
- **Shell aliases.** The text below goes to natedev, who commits it to `/etc/nixos` `modules/linux/shell.nix` under `programs.zsh.shellAliases`; the rebuild is the user's. Units run only on natedev, so the Mac gets none.

  ```nix
  claude1 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 1'";
  claude2 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 2'";
  claude3 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 3'";
  claude-unit = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec unit";
  ```

  Plain `claude` is unchanged: it is `~/.claude`'s account, where showrunners and the user's own sessions run. Until cutover, every alias either runs plain `claude` or refuses, so committing them starts nothing.
- **`docs/accounts.md`, the user's guide.** Short, task-first, every command copyable:
  - **Which account am I on:** the status line's second row shows the label, and `~/.claude/scripts/lib/py ~/.claude/scripts/whoami/account.py` prints it. `/whoami` shows each account in use with its role (showrunners, unit directors), not which one this session is on; `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status` shows every dir and its login.
  - **Starting a session:**
    - `claude` for showrunners and the user's own work.
    - `claude-unit` for a session on the unit account.
    - `claude1`, `claude2` and `claude3` for a named account.
    - Units started by showrunners pick their account themselves.
  - **Log out and in** (each dir holds one login, and `/login` or `/logout` inside a session changes it for every session on that dir):
    - To switch `~/.claude`'s account, as today: `/login` in a `claude` session. Do not run it in a `claude-unit` or `claudeN` session.
    - To renew or fix account N: `claudeN`, then `/login` as claude N. `exec` refuses a dir logged in as the wrong account, so start that one directly with `CLAUDE_CONFIG_DIR=~/.claudeN claude`, then `/logout` and `/login` as claude N.
    - Never `/logout` in a unit director's session: every unit on that account stops working.
    - After any login, `/accounts status` must show every dir matching its label.
  - **Swapping the unit account:** `/accounts swap unit "claude N"`, what the user sees during it, and the undo `/accounts swap unit default`.
  - **Setting up a new dir:** `/accounts setup "claude N"`, then `claudeN` and `/login`.
  - **Low-quota alerts:** acknowledging an account's low-quota alert keeps it quiet across swaps: made spare and then selected again, it stays quiet until its own quota recovers or resets.
  - **When a unit will not start:** a showrunner's launch refuses with `add_unit: unit account <label>: <problem>; /accounts status`. Run `/accounts status`, fix what it names (most often `claudeN` and `/login` as claude N), then launch again.
- `commands/accounts.md` points to the guide.

**Files:**

- `scripts/accounts/accounts.py` — `exec` and the login-matches-note rule in `check`.
- `scripts/accounts/test_accounts.py` — `exec` with `os.execvpe` stubbed: each refusal, env set and removed, the default-login case, a logged-out dir allowed apart from an unreadable one refused. `check` with a mismatched login and with no `claude N` note: the dir leaves `named_accounts`, a mismatched unit dir makes `unit_account` unreadable, and `multiple_accounts` stays true.
- `docs/accounts.md` — new: the user's guide.
- `commands/accounts.md` — points to the guide.
- `docs/plans/build-followups-two-accounts.md` — the alias text, as sent to natedev.

**Seats:** `2 writers`, each writing its own tests; the guide quotes `exec`'s refusals from this Spec.
- `exec`: `accounts.py`, `test_accounts.py`.
- `guide`: `docs/accounts.md`, `commands/accounts.md`. The alias text in this plan is the director's. Before writing the account-display and acknowledgement statements, the seat reads the regressions that pin them: `scripts/whoami/test_agent_notes.py` (multi-account display) and `scripts/whoami/test_quota_alert.py` (acknowledgement across a swap).

**Acceptance gate:**

- Tests pass, and `basedpyright scripts/accounts scripts/whoami scripts/nightly_review scripts/shutdown scripts/production` reports 0 errors, 0 warnings, 0 notes (it exits 3 only for the missing `.venv`).
- On natedev, before any dir exists, `accounts.py exec 'claude 3' --version` refuses with the setup line, and `accounts.py exec 'claude 2' --version` prints Claude Code's version.
- natedev has the alias text.

### Phase 6 — Live proof on claude 3  · status: todo · gated by G1

#### Work Order

**Goal:** before cutover, prove on natedev that a session in a farm dir runs on its own account and is seen by the existing toolchain. Record the result of each check here.

**Spec:** the director runs it with the user; a writer joins only for fixes to `accounts.py`.

1. `accounts.py setup "claude 3"` (the unit account the user chose; `~/.claude` is claude 2).
2. The user runs `CLAUDE_CONFIG_DIR=~/.claude3 claude` in a terminal and completes `/login`. Then `accounts.py check` passes.
3. Start a scratch session `CLAUDE_CONFIG_DIR=~/.claude3 claude --remote-control acct-probe -n acct-probe` in tmux, cwd a scratch dir. Check, recording each result:
   - **(a) Logins.** `claude auth status` under each dir shows its own email, and `~/.claude/.credentials.json` mtime is unchanged.
   - **(b) Registry and transcripts.** The probe's record is in `~/.claude/sessions/`, its transcript is under `~/.claude/projects/`, and its status line shows claude 3.
   - **(c) Messaging.**
     - `ListAgents` and `SendMessage` reach the probe from this session, and its reply comes back.
     - `send.py --to acct-probe` delivers, and so does a `send.py` from the probe to this session.
     - `notify_when_idle` fires.
   - **(d) Shutdown attribution.** `account.py --pid <probe pid>` names claude 3. `shutdown.py status "claude 3"` lists the probe, and `shutdown.py status` for `~/.claude`'s account does not.
   - **(e) Resume across accounts.** A scratch session started on `~/.claude`, then exited, resumes under `~/.claude3` with `--resume <id>` and keeps its history. The reverse works too.
   - **(f) Symlinks survive writes.** In the probe run `/model` to change the model and back, and send a prompt. `~/.claude3/settings.json` and `history.jsonl` must still be links. If one was replaced, record it: Phase 2's launch check is then the defence, and `setup` repairs the link only after the user compares the two files.
   - **(g) Remote Control.** Find which claude.ai account lists `acct-probe`, and whether the phone, signed into the main account, can open it.
4. Exit the probe and delete its scratch transcript dir. Leave `~/.claude3` in place; Phase 7 uses it.

If (b), (c) or (e) fails, stop and bring the failing check to the showrunner with the fix it needs in Phases 1–5. Those three are what the symlink design rests on.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the result of each check.
- `scripts/accounts/accounts.py` — only for defects found.

**Acceptance gate:** (a)–(e) pass, and (f) and (g) are recorded.

### Phase 7 — Cutover  · status: todo · gated by G2

#### Work Order

**Goal:** units run on the chosen unit account, showrunners on `~/.claude`'s account, and every spare dir is logged in.

**Spec:** the director runs it with the user.

1. `accounts.py setup` for each account the user has, except the one `~/.claude` is logged in to. The Phase 6 dir already exists.
2. The user logs in each new dir (G2).
3. `accounts.py status`: every dir passes `check`.
4. `accounts.py set-role unit "<label>"`. Units already running on `~/.claude` move by a shutdown of the default account's units only: `shutdown.py down <default> --only <unit session ids> --restart` (Phase 4). `/accounts swap` is not used here, because the role already names the target and swap refuses that; it is for every later move. Every other session on `~/.claude` keeps running. Units launched after this point start on the unit account.
5. Confirm each unit's status line label and `shutdown.py status "<label>"`. Confirm the showrunner reaches each unit by message.
6. Record the cutover time T0 here.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the cutover time T0.

**Acceptance gate:** every unit director's process shows the unit account (`account.py --pid`), and every showrunner and top-level session shows `~/.claude`'s.

### Phase 8 — One-week readout  · status: todo · gated by G3

#### Work Order

**Goal:** answer the user's question with a week of data: did the showrunner account last the week, and how many unit swaps did it take?

**Spec:** run `accounts.py burn --since <T0> --until <T0 + 7 d>`. Report to the showrunner:

- points per account per day
- the role split
- each swap's time
- whether the default account went below `threshold_percent`

Recommend whether any change is needed, for example moving the nightly review to the unit role.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the readout.

**Acceptance gate:** the readout is in this plan and has been sent to the showrunner.

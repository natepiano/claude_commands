# Two accounts: showrunners on one Claude account, unit directors on another

> **Status: IMPLEMENTATION PLAN — phases 1–6 are done; the cutover (Phase 7) moved all ten build-followups unit directors to claude 3 on 2026-10-11, with the spare dir and the cross-machine message check still open; Phase 8 (move any one session) waits on the user's go; Phases 10–12 add the Codex accounts: a home per account, one account for all seats with a swap, and alerts per account.** Phases 1–5 built everything and initiated nothing: no account dir, no login, no role. Phase 6 proved `~/.claude3` live. The design: each account the user has gets its own config dir, `~/.claude1`, `~/.claude2` and `~/.claude3`. Each holds only that account's login, and everything else is a symlink into `~/.claude`. Unit directors launch with `CLAUDE_CONFIG_DIR` set to the unit account's dir. Showrunners and every other session stay on `~/.claude`'s own login, as today. When the unit account runs low, one command swaps every unit onto a spare account through `/shutdown`'s safe stop and resume. Any one session, a showrunner included, moves to any account the same way with `/showrunner:accounts move` (Phase 8). Phased, delegate-ready once approved.

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

**What changes for the user:** nothing when starting sessions by hand. `~/.claude`'s login is the showrunner account, and `/login` there switches it as today. Units launch on the unit account by themselves. When the unit account runs low, the quota alert names the spare account with the most left and the one command, `/showrunner:accounts swap unit "claude N"`. Each account is logged in once in its own dir, by the user, at setup.

**Costs:**

- **Remote Control is per account.** A unit director on account N shows in account N's claude.ai session list, not the one the phone is signed into. Showrunners stay visible on the main account and relay as today. Phase 2 checks this live.
- **`.claude.json` is per dir.** A new dir is seeded with project trust, onboarding flags and the user-scope MCP servers (today `brp`), so launches never stop at a dialog. An MCP server added later to one dir does not reach the others; `/showrunner:accounts` reports the difference.
- **Claude Code may replace a symlinked file when it writes it,** for example `settings.json` after `/model`. Phase 2 tests this; the launch check catches a replaced link either way.

**Calls I made (each a one-line revert):**

- Showrunners, top-level sessions, the fix pipeline and the `send.py` relay stay on `~/.claude`'s login. The nightly review ran there too until 2026-10-11, when it moved to the unit role's account (Phase 7). Only units get a role account, because they spend about 70% and they are the sessions that can be stopped safely and resumed.
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
   - Phase 4: `/showrunner:accounts swap`.

   Phase 2 also edits `settings.json`: allow rules and sandbox write entries for `~/.claude1`–`~/.claude3`. That edit reaches main only with the user's go.
2. **Shell commands and the guide (Phase 5). Nothing live changes.**
   - The aliases are `claude1`, `claude2`, `claude3` and `claude-unit`; plain `claude` is unchanged. natedev commits them to `/etc/nixos`, and the user rebuilds when ready. Until a dir exists, each alias runs plain `claude` or refuses.
   - `~/.claude/docs/accounts.md` says how to start a session on each account, how to log in and out without moving `~/.claude` off claude 2, and how to see which account a session is on.
3. **Prove one account dir works (Phase 6). The first live step, only when the user says go; about 15 minutes of their time.**
   - The unit creates `~/.claude3`. The user runs `claude3` and logs in as claude 3 (`rubrics.dogwood-1f@icloud.com`).
   - The unit runs one throwaway session on claude 3 and checks its login, messaging both ways, `/shutdown`, resuming across accounts, and that the links survive `/model`. The user checks Remote Control on the phone.
   - The probe stops. Every existing session stays on claude 2. Undo: `rm -rf ~/.claude3`.
4. **Cutover (Phase 7), one unit at a time on the user's word.**
   - The user logs in `~/.claude1` with `claude1` (the spare).
   - New units start on claude 3. Each unit director the user names stops safely, as `/shutdown` does it, and resumes on claude 3 with its conversation intact.
   - Showrunners and the user's own sessions stay on claude 2.
   - Undo: `/showrunner:accounts swap unit default`.
5. **Move any one session (Phase 8), when the user says go.**
   - `/showrunner:accounts move <session> "claude N"` moves one showrunner, unit director or top-level session to any account. It stops safely, as `/shutdown` does it, and resumes the same conversation on the target in its own tmux pane or window.
   - Undo: move it back.
6. **During the week.** When claude 3 runs low, the alert names `/showrunner:accounts swap unit "claude 1"`. Nothing switches by itself.
7. **Day 7 (Phase 9).** A readout gives points per account per day, the times of any swaps, and whether the showrunner account lasted the week.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-two-accounts`, branch `build-followups-two-accounts` (unit `two-accounts-unit`); name both in every dispatch.
- **Project started:** 2026-10-10T21:07:53.634+00:00
- **Stack:** Python 3 standard library, checked by basedpyright. zsh launch strings. Claude Code 2.1.296.
- **Layout:** new `scripts/accounts/` (`accounts.py`, `test_accounts.py`) · `scripts/production/` · `scripts/shutdown/` · `scripts/whoami/` · `scripts/message/` · new `commands/accounts.md` · `settings.json` · `docs/quota_alerts.md` · `docs/as-built/shutdown-and-restart.md`. Outside the repo, live, written only in Phases 6 and 7: `~/.claude1`, `~/.claude2`, `~/.claude3`, and `config/accounts.json` (gitignored like `config/showrunners.json`).
- **Key files** (as found at planning, cf0e505, cited by name because line numbers move with every merge; each phase's As-built records what it changed):
  - `scripts/whoami/agent_accounts.py`:
    - `claude_config_dir()`: `CLAUDE_CONFIG_DIR`, else `~/.claude`.
    - `claude_config_file()`: `~/.claude.json` only for `~/.claude`, else `<dir>/.claude.json`.
    - `claude_credentials()`: on darwin it reads the Keychain only without `CLAUDE_CONFIG_DIR`.
    - `claude_live()`: `claude auth status`, then `/api/oauth/usage` with the dir's access token.
    - `claude_on_disk(config_dir)`.
    - `live_reports()`.
  - `scripts/whoami/account.py`:
    - `process_config_dir(pid)` reads `/proc/<pid>/environ`, or `ps eww` on darwin.
    - `account_of(pid)`.
    - `write_label` writes `<dir>/account-label`; a SessionStart hook in `settings.json` runs it.
  - `scripts/whoami/agent_notes.py`: `apply` marks the env's own account `active` and every other account `inactive`; `update`. In `quota_alert.py`, `low()` and `refresh()` act on `state: active`, and `current_session()` reads the registry through the `SESSIONS` constant. `run_out.py`. `whoami.py`.
  - `scripts/production/add_unit.py`:
    - `launch_session` builds `ENABLE_TOOL_SEARCH=true command claude …; exec zsh` and runs it under `systemd-run --user --scope … tmux new-session … zsh -ic`.
    - `launch_environment` strips every `CLAUDE_*` variable.
    - The launch check is the `launch_permission.launch_permission(…)` call in `main()`.
    - tmux gives a new session the server's environment, not the client's, so a variable reaches `claude` only inside the command string or through `-e`.
  - `scripts/shutdown/restart.py`:
    - `_run` strips `CLAUDE_*`.
    - `_resume_command` builds the resume command.
    - Units relaunch through `add_unit.py --resume`; `_unit_argv` builds that command.
    - Each record entry's embedded `session.config_dir` holds the session's dir (absent means `~/.claude`); a non-unit resume keeps it, and a unit relaunches on the current unit role.
  - `scripts/shutdown/inventory.py`: `inventory()` lists `sessions.live_sessions()` (`~/.claude/sessions`), then keeps the sessions `_configuration_for_session` attributes to the login through `account_of`; `_model()` reads `~/.claude/projects` for the last model. `record.py` holds the record types. `shutdown.py` holds the verbs and `conduct`.
  - `scripts/message/sessions.py`: `sessions_dir()` is `$NOTIFIER_SESSIONS_DIR` or `~/.claude/sessions`.
  - `scripts/message/send.py`: `relay()` runs a headless `claude -p` with `CLAUDE_*` stripped, so it runs on `~/.claude`'s account. Where `~/.config/claude/oauth-token.gpg` exists (the Mac; natedev has none), it sets `CLAUDE_CODE_OAUTH_TOKEN` from that file and runs on that token's account.
  - `scripts/delegate/remove_seats.py` (`list_sessions()` and `main()`) and `scripts/delegate/implement.sh` (`claude_session()` in its inline Python): `claude agents --json` and `claude rm` act on the caller's config dir.
  - `commands/showrunner/produce.md`, the **Resume.** step of `<LaunchUnits>`: a manual unit resume runs `claude --resume` from the showrunner's own shell.
  - `settings.json`:
    - `permissions.allow` carries `Edit(~/.claude/**)`.
    - `sandbox.filesystem.allowWrite` lists `~/.claude`.
    - `statusLine` reads `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/account-label`.
  - `scripts/nightly_review/nightly_review.py`: the quota gate `quota_permission()` assumes one active Claude note; `start()` launches the review.
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
| G1 | Phase 6 | the user | the user explicitly authorizes the first live proof. Phase 6 then creates `~/.claude3`, and the user completes its login (step 2) before any probe starts |
| G2 | Phase 7 | the user | the user authorizes cutover at a chosen time and confirms the unit account. Phase 7 then sets up and logs in the remaining dirs, and every check passes before step 4 writes the role |
| G3 | Phase 9 | the clock | 7 days after cutover |

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

- `scripts/production/add_unit.py` resolves `accounts.role_account("unit")` once per launch and passes the selection to `launch_session`. A named account must pass `accounts.check(label)`, else exit 2: `add_unit: unit account <label>: <first problem>; /showrunner:accounts`. The launch guard (`launch_permission`) runs with `CLAUDE_CONFIG_DIR` set to that account's dir; `launch_environment` strips `CLAUDE_*`. The tmux command string is `accounts.account_prefix(selected) + "ENABLE_TOOL_SEARCH=true command claude …"`, so tmux passes the prefix whatever its server's environment. With no `config/accounts.json` the prefix is empty and the guard env is unchanged.
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

**Binds later work:** add_unit resolves the unit role once per launch, refuses a named account failing `accounts.check` with exit 2 `add_unit: unit account <label>: <first problem>; /showrunner:accounts`, and asks the launch guard on that account's dir, so after a role change a unit relaunched through add_unit lands on and is judged against the new account (the swap of the unit account relies on this). A rule added to `accounts.check` reaches the unit launch with no change to add_unit. `account_prefix(selected)` and the `accounts.py env <role>` verb render the account prefix for any launch, including an `exec` verb. Restart restores a non-unit session on its recorded `config_dir`. `shutdown.py launch-blocked` with no account argument judges the default account (`CLAUDE_*` stripped), so a swap-time shutdown check passes the target dir. The add_unit refusal is user-actionable; the user's guide covers it under "When a unit will not start".

**Gotchas:** a check about a named account through `shutdown.py launch-blocked` must name the account or set its dir, or it judges the default one; a held named unit account refuses with `…; /shutdown restart first`. Code that imports `scripts.accounts` from `scripts/production` needs the repo root on `sys.path`.

**Ruled out:** a `config_dir` field on `ShutdownSessionEntry` (settle replaces the session snapshot, so a second copy goes stale); an explicit `CLAUDE_CONFIG_DIR` in `remove_seats.py` (inheritance already carries it).

### Phase 3 — Quota with two active accounts  · status: done

#### As-built

- With accounts configured, `~/.claude`'s login (showrunner) and the unit account are both `state: active`; spares are `inactive`, yet each fresh reading still reaches its note and `readings.jsonl`. `claude_live` and `claude_credentials` take an optional `config_dir`; `None` keeps the single-dir behavior. With no `config/accounts.json` and no named dir, output and launches are unchanged.
- One frozen `ClaudeAccountSelection` (`default_account`, `unit_account`, `multiple_accounts`, `named_accounts`) per operation, built by `accounts.claude_account_selection()` from `checked_named_accounts()` and `account_identity(dir)`; consumers take it as a value and never re-resolve roles.
- Alerts name label and role (`Quota alert: claude 3 (unit directors) at 4%`); a unit-account alert adds `swap: /showrunner:accounts swap unit "<label>"` for the fit spare with the most remaining, or `no spare account has more left`. In multi-account mode an episode closes only on that account's recovery or reset, never on a role change; a low spare opens no episode.
- `whoami.py` prints each active Claude account with its role. `run_out.py` is unchanged: dailies already projects each active note. Nightly gates on the showrunner account's note and starts with `CLAUDE_*` stripped (changed 2026-10-11: it gates on the unit role's account, Phase 7).
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

#### As-built

- `accounts.py swap unit <label|default> [--dry-run]` is `swap(role, label, *, dry_run=False) -> SwapOutcome` (`SwapDryRun | SwapStarted | SwapRolledBack`). It resolves `claude_account_selection()` once and names the old account by its note label throughout. It reaches shutdown only by subprocess (`shutdown.py status <old> --json`, then `shutdown.py down <old> --restart`). Before any write it refuses an unreadable current unit account (`accounts: current unit account identity is unreadable`) or default identity, the default login named by label (`<label> is the default account; use default`), a target missing from the checked selection (its first `check` problem), and a target already the unit account. It also refuses any old-account session that would not move (`accounts: cannot swap <old>: <machine> <kind> <name> would not move`): only units on this machine and seats owned by those units' directors move; every peer-machine session, other kind, unowned seat, unattributed session or unavailable inventory refuses.
- Otherwise swap writes the role (`_write_unit_role` keeps a `PreviousUnitRole`), then runs `down`. A nonzero `down`, or a failed spawn, restores the exact previous `accounts.json` (an absent file included), appends a compensating role-history row, and exits with `down`'s code. The CLI prints `down`'s output, then `units come back on <target> when <old> is down` or `unit account unchanged: still <old>`. `--dry-run` prints the `set-role` and `down … --restart` argv and writes nothing.
- `set_role` resolves the selected dir's identity (`~/.claude` for `default`) and refuses an unreadable one, writing nothing. `RoleChange` extends `LegacyRoleChange` with `account_label`, the note label at write time; `_unit_role_history` uses it and resolves a legacy row through the dir's current identity.
- `shutdown.py down --restart [--only IDS]` stores `then: restart` at `begin` on both machines (remote `begin --then restart`). `record.py` holds `ShutdownContinuation = Literal["stop", "restart"]` and `continuation(record)`; an absent `then` reads as `stop` and keeps the legacy wire shape. An external requester gets the result but holds no readiness. The `conduct` dispatcher restarts only when the local record, and the peer record unless `--here`, is a final `down` with `then: restart` (`--here` runs `up` locally). A peer record that is unreachable, unavailable or unreadable alerts `<label>: automatic restart skipped` with the runnable `restart <label>` command, through `alert_user(..., notify_selected=True)`.
- A complete stop under restart tells the requester `The sessions restart automatically: units on the unit account, other sessions on the account they ran on.` A partial stop keeps the `/shutdown restart` line and adds `settle.shutdown_command("down", <label>, "--restart")`, which is also its action.
- Rerunning `down <label> --restart` on a `down` or `stop partial` restart record reuses it: same `--only` scope, peer `down`, `stop partial` or `settling` with that scope; it prints `shutdown of <label> resumed; /shutdown status to watch`. The conductor runs the hidden `shutdown.py retry-stop <login>` on the peer, then `settle.retry_stop` locally, which resets `stop failed` entries to `waiting`. Under restart, `stop._retry_owner_cleanup` retries unconfirmed Codex and unit-tmux cleanup on owners already stopped.
- `commands/accounts.md` is `/showrunner:accounts [status | setup "claude N" | swap unit "claude N" | swap unit default]`: it runs the verb and shows its output unchanged; switching the default account remains `/login` in a `~/.claude` session.

**Files:**
- `scripts/accounts/accounts.py` — `swap`, its outcome and inventory types, `set_role` identity check, `account_label` on role history.
- `scripts/accounts/test_accounts.py` — swap refusals, dry run, rollback, peer refusal, one selection through write and shutdown, burn attribution across logout and re-login.
- `scripts/shutdown/record.py` — `then`, `continuation`.
- `scripts/shutdown/settle.py` — `begin`/`down` with `then`, the retry path, `retry_stop`, `report_stop`, `report_restart_skipped`, `shutdown_command`, `alert_user(notify_selected=)`.
- `scripts/shutdown/shutdown.py` — `down --restart`, `begin --then`, `retry-stop`, the restart dispatcher in `conduct`.
- `scripts/shutdown/stop.py` — `_retry_owner_cleanup`.
- `scripts/shutdown/test_record.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_stop.py` — the above.
- `commands/accounts.md` — `/showrunner:accounts`.
- `docs/as-built/shutdown-and-restart.md` — `--restart`, partial-stop retry, automatic restart, swap.

**Binds later work:** The user's guide describes what the user sees: swap's two closing lines, each refusal before any write, the partial-stop alert's runnable `down <label> --restart` retry, and the skipped-restart alert's runnable `restart <label>`; `commands/accounts.md` exists and the guide only adds a pointer to it. A rule added to `check` reaches swap's target refusal, and an unreadable unit account refuses swap. A `down … --only <ids> --restart` without `--here` starts a record on both machines, so automatic restart also needs the peer record to reach `down` with the same scope; the cutover moves natedev units with `down --here --only <ids> --restart`. `burn` reads `account_label` from role history; the one-week readout depends on it.

**Gotchas:** Restart is decided from the record, never from `conduct`'s exit code; `settle.py` never imports `restart.py`. The conductor can archive the record before `down` saves its name; `down` tolerates the missing record only under restart intent. Swap runs `status` and `down` without `--here`, so it needs the Mac reachable even when the Mac holds nothing on the old account. The printed partial-stop retry omits `--here`; a `--here` shutdown's retry must add it. A lost reply from the peer's `retry-stop` leaves it `settling`, which the retry accepts.

**Ruled out:** writing the unit role on both machines (units launch only on natedev; peer units refuse instead); a `--here` option on swap (it would change swap's scope; the cutover runs `down --here --only` directly).

### Phase 5 — Starting sessions by account, and the user's guide  · status: done

#### As-built

- `accounts.py exec <claude N | unit> [claude args…]` (`exec_account(label, arguments) -> None`) discards `CLAUDE_CODE_OAUTH_TOKEN` in every branch, then calls `os.execvpe("claude", ["claude", *arguments], env)`. A `claude N` whose note `login:` equals `~/.claude`'s login, and `unit` with no role, run plain `claude` with `CLAUDE_CONFIG_DIR` removed; any other target runs with `CLAUDE_CONFIG_DIR=~/.claudeN`. It starts on ready or logged out (so `/login` runs through it); otherwise it exits 2 without starting `claude` and prints the first problem, among them `accounts: ~/.claudeN is not set up; /showrunner:accounts setup "claude N" first` and `accounts: ~/.claudeN is logged in as <login>, not claude N; see Log out and in in ~/.claude/docs/accounts.md`.
- `account_directory_readiness(label) -> AccountDirectoryReadiness` returns one frozen tagged value: `AccountDirectoryReady(login)`, `AccountDirectoryLoggedOut`, `AccountDirectoryWrongLogin(login)`, `AccountDirectoryNotSetUp`, `AccountDirectoryBrokenLinks` or `AccountDirectoryUnreadable`, each carrying every coexisting problem. It reads the dir's `.claude.json` (`ClaudeDirectoryLogin | ClaudeDirectoryLoggedOut | UnreadableClaudeDirectoryIdentity`) and the label's note (`ExpectedClaudeLogin | MissingClaudeAccountNote | UnreadableClaudeAccountNote`) at the filesystem boundary. `exec` and `check` both read it; `check(label)` returns its problems, so only ready passes.
- `check` requires the dir's login to match `login:` in the requested label's note (case-insensitive); a dir with no `claude N` note is a problem. Through the selection, a mismatched dir leaves `named_accounts`, a mismatched unit dir makes `unit_account` an `UnreadableClaudeAccount` (unit launch and swap refuse), and `multiple_accounts` stays true; `account_identity` still reports the dir's actual login.
- `setup` creates the missing always-linked shared targets (`projects`, `sessions`) under `~/.claude` before linking; `_link_problems` inspects a named entry even when its optional source is gone and reports a dangling expected link as `accounts: <path> is broken`. `note_file.read_note` returns None for a non-UTF-8 note.
- Aliases `claude1`, `claude2`, `claude3` and `claude-unit`, each `"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude N'"` (`exec unit` for `claude-unit`). The alias text lives in natedev's `/etc/nixos` `modules/linux/shell.nix` under `programs.zsh.shellAliases`; committing and rebuilding are outside this repo, and the Mac has none. Plain `claude` stays `~/.claude`'s account.
- `docs/accounts.md` covers which account a session is on, starting a session (aliases and the alias-free `accounts.py exec` form), Log out and in, swapping the unit account with every refusal and recovery line, setting up a dir, low-quota alerts, and a unit that will not start.

**Files:**
- `scripts/accounts/accounts.py` — `exec_account`, the readiness types and `account_directory_readiness`, login-matching `check`, `setup`'s shared-target creation.
- `scripts/accounts/test_accounts.py` — exec refusals, env set and removed, token discard; check login match and its selection effects; swap's wrong-login refusals; setup repair.
- `scripts/whoami/note_file.py`, `scripts/whoami/test_note_file.py` — a non-UTF-8 note reads as None.
- `scripts/whoami/test_agent_notes.py` — the shared-login fixture carries the requested label's note.
- `scripts/shutdown/test_shutdown_cli.py` — the fail-closed launch test injects a resolver `RuntimeError`.
- `docs/accounts.md` — the user's guide; `commands/accounts.md` — dispatch plus a pointer to the guide.

**Binds later work:** a named dir's session starts through `claudeN` / `accounts.py exec 'claude N'`, which drops an inherited OAuth token, refuses a wrong login and passes claude's arguments through. The guide's "Log out and in" is the login procedure: `claudeN`, then `/login` as claude N; a dir on the wrong login starts with `env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=~/.claudeN claude`, then `/logout` and `/login`. `setup` may create missing shared targets under `~/.claude`.

**Gotchas:**
- A raw `CLAUDE_CONFIG_DIR=~/.claudeN claude` keeps an inherited `CLAUDE_CODE_OAUTH_TOKEN`, which wins over the dir's login; `exec` discards it.
- A dir whose identity files exist but cannot be read is not logged out: its readiness is unreadable and `exec` refuses.
- `exec` skips the dir entirely when the note's login is `~/.claude`'s, so `claude2` runs plain `claude` even with no `~/.claude2`.
- `/login` or `/logout` inside a session changes the login for every session on that dir; `/logout` in a unit director's session stops every unit on that account.

**Ruled out:** comparing the requested label with `account_identity(...).label` (the dir's login is compared with the note's `login:`); forwarding `account_identity`'s `UnreadableClaudeAccount` into readiness, which joins a logout and a read failure; fixing the non-UTF-8 note crash in `accounts.py` instead of `note_file.read_note`.

### Phase 6 — Live proof on claude 3  · status: done · gated by G1

#### Work Order

**Goal:** before cutover, prove on natedev that a session in a farm dir runs on its own account and is seen by the existing toolchain. Record the result of each check here.

**Constraints from prior phases:** Phase 4 supports `shutdown.py down <note label> --here --only <comma-separated session ids> --restart`; `--here` starts and resumes only this machine's record. A non-unit scratch session resumes on its stored `session.config_dir`, so this proof sets no unit role and moves no existing unit. Exit 0 from `down` means the stop was accepted, not that the resume finished: `shutdown.py status` and the resumed process are the evidence. A partial stop's printed retry omits `--here`; a local proof adds it. Phase 5 shipped `accounts.py exec <claude N | unit> [claude args…]` and the `claudeN` aliases (on natedev after the user's rebuild; otherwise `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude N'`). `exec` discards an inherited `CLAUDE_CODE_OAUTH_TOKEN`, which a raw `CLAUDE_CONFIG_DIR=~/.claudeN claude` keeps and which would override the dir's login; it starts a logged-out dir, refuses one logged in as another account, and passes claude's own arguments through. `check` now also requires the dir's login to match `login:` in its `claude N` note and reports a dangling expected link as broken; `setup` creates missing shared targets (`projects`, `sessions`) under `~/.claude` before linking. The login procedure is the guide's "Log out and in" (`~/.claude/docs/accounts.md`).

**Spec:** the director runs it with the user; a writer joins only for fixes to `accounts.py`.

1. `accounts.py setup "claude 3"` (the unit account the user chose; `~/.claude` is claude 2).
2. The user runs `claude3` (or `accounts.py exec 'claude 3'`) in a terminal and completes `/login` as claude 3. Then `accounts.py check "claude 3"` passes.
3. Start a scratch session `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 3' --remote-control acct-probe -n acct-probe` in tmux, cwd a scratch dir. Check, recording each result:
   - **(a) Logins.** `claude auth status` under each dir shows its own email, and `~/.claude/.credentials.json` mtime is unchanged.
   - **(b) Registry and transcripts.** The probe's record is in `~/.claude/sessions/`, its transcript is under `~/.claude/projects/`, and its status line shows claude 3.
   - **(c) Messaging.**
     - `ListAgents` and `SendMessage` reach the probe from this session, and its reply comes back.
     - `send.py --to acct-probe` delivers, and so does a `send.py` from the probe to this session.
     - `notify_when_idle` fires.
   - **(d) Shutdown attribution.** `account.py --pid <probe pid>` names claude 3. `shutdown.py status "claude 3"` lists the probe, and `shutdown.py status` for `~/.claude`'s account does not. Then record the probe's session id and a baseline of every other session, and run `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down 'claude 3' --here --only '<probe session id>' --restart`. Only the probe reports ready; it resumes on its own with the same conversation, on claude 3. Every baseline session keeps running on its own account, and the scratch shutdown record completes. If it ends partial, rerun the retry with `--here` added (it keeps the saved selection); stop the proof on any failure that does not clear, and report it.
   - **(e) Resume across accounts.** A scratch session started on `~/.claude`, then exited, resumes under `~/.claude3` with `--resume <id>` and keeps its history. The reverse works too.
   - **(f) Symlinks survive writes.** In the probe run `/model` to change the model and back, and send a prompt. `~/.claude3/settings.json` and `history.jsonl` must still be links. If one was replaced, record it: Phase 2's launch check is then the defence, and `setup` repairs the link only after the user compares the two files.
   - **(g) Remote Control.** Find which claude.ai account lists `acct-probe`, and whether the phone, signed into the main account, can open it.
4. Exit the probe and delete its scratch transcript dir. Leave `~/.claude3` in place; Phase 7 uses it.

If (b), (c) or (e) fails, stop and bring the failing check to the showrunner with the fix it needs in Phases 1–5. Those three are what the symlink design rests on.

#### Results (2026-10-11, natedev)

- **Setup and login.** The user ran `accounts.py setup "claude 3"` (this session's run of it was refused by Claude Code's auto-mode permission check) and logged in through `claude3`; `accounts.py check "claude 3"` passes. `setup`'s closing hint printed the raw `CLAUDE_CONFIG_DIR=… claude`, which keeps an inherited OAuth token; it and the guide's wrong-login step now print `env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=… claude`. `exec` is not the hint: for a label whose login is `~/.claude`'s, it skips the dir, so `/login` would change `~/.claude`.
- **Probe.** `acct-probe`, pid 3024638, session `bbbeeb94-1cbc-4f15-a434-51ceefae97fc`, tmux `acct-probe`, cwd `/tmp/claude/acct-probe`. The first start stops on the folder-trust prompt.
- **(a) Logins — pass.** `claude auth status`: `~/.claude3` is `rubrics.dogwood-1f@icloud.com`, `~/.claude` is `browses31_perks@icloud.com`. `~/.claude/.credentials.json` mtime is 10:08, before the login.
- **(b) Registry and transcripts — pass.** `~/.claude/sessions/3024638.json` and `~/.claude/projects/-tmp-claude-acct-probe/bbbeeb94-….jsonl`. The status line reads `claude 3 | 5-hour 100% left | weekly 100% left` once the first prompt redraws it; `~/.claude3/account-label` is written by the SessionStart hook 7 s after start.
- **(c) Messaging — pass.** `ListAgents` lists the probe; SendMessage reaches it and its SendMessage reply returns; `send.py --to acct-probe` delivers and its reply returns; `send.py` from the probe delivers here. That relay runs on `~/.claude`'s account and briefly shows in claude 2's `shutdown.py status` as `unknown account · <relay pid> acct-probe · process start mismatch`. The `notify_when_idle` notice fired for the probe's 17:42 turn; it reached this session at its next turn boundary, not mid-turn.
- **(d) Shutdown attribution — pass.** `account.py --pid 3024638` names claude 3; `shutdown.py status "claude 3"` lists `acct-probe` and `natepiano-91` (the user's login session); claude 2's status lists neither. Claude Code's auto-mode permission check refused this session's `shutdown.py down` ("Interfere With Workloads"), so the user ran `shutdown.py down 'claude 3' --here --only 'bbbeeb94-…' --restart` at 17:49:38. Its record has scope `selected` with only the probe, one entry (`config_dir` `~/.claude3`), and ended `up` and archived. The probe came back as pid 3277165 via `claude --resume bbbeeb94-… -n acct-probe --remote-control acct-probe --model claude-opus-5-5`, on claude 3, with the same transcript (entries 21:41–21:50Z) and its history. All 27 claude 2 baseline sessions kept running. `natepiano-91` exited outside the shutdown: the user closed it, no record names it, and its transcript last wrote 14 s before the stop was requested.
- **(e) Resume across accounts — pass.** A print-mode session started on `~/.claude` resumed under `accounts.py exec 'claude 3' --resume <id>` with its history (pid and status line both claude 3), then resumed on `~/.claude` and listed every word from both accounts' turns.
- **(f) Links survive writes — pass, with a shared-model finding.** After `/model` to Sonnet and back and a prompt, `~/.claude3/settings.json` and `history.jsonl` are still links and the prompt was appended to the shared history. `/model` writes `model` through the link into `~/.claude/settings.json`, so a model choice in any account changes the default for every account, as a plain `~/.claude` session's does. The file was restored byte for byte.
- **(g) Remote Control.** The phone's app, signed into the main account (claude 2), lists nothing called `acct-probe`. Opening its direct link on the phone, signed into the main account, says "this session is not available". A claude 3 session is neither listed in nor reachable from the main account's Remote Control: the phone reaches units on claude 3 only by signing into claude 3, and showrunners relay as planned.
- **Cleanup.** The probe exited and its tmux session closed; `~/.claude/projects/-tmp-claude-acct-probe` and `-tmp-claude-acct-resume` and the scratch dirs are deleted. `~/.claude3` stays for Phase 7. No unit role or existing claude 2 session changed.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the result of each check.
- `scripts/accounts/accounts.py` — only for defects found.
- `scripts/accounts/test_accounts.py` — a regression for each `accounts.py` defect repaired during the proof. A shutdown defect goes to the showrunner for its own authorized repair.

**Seats:** `1 writer + 1 tester`, the writer opened only when the proof finds an `accounts.py` defect.
- writer: `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py`, isolated fixes the proof needs.
- tester: the director with the user, running the proof and recording its results in this plan.

**Acceptance gate:** (a)–(e) pass, the selected local stop and automatic resume in (d) included, and (f) and (g) are recorded. No existing session or unit role changes. Any source fix has its regression and passing accounts tests and lint before the proof resumes.

### Phase 7 — Cutover  · status: in progress · gated by G2

#### Work Order

**Goal:** the unit directors the user names run on the chosen unit account, new units start there, showrunners stay on `~/.claude`'s account, and every spare dir is logged in.

**Spec:** the director runs it with the user.

1. `accounts.py setup` for each account the user has, except the one `~/.claude` is logged in to. The Phase 6 dir already exists.
2. The user logs in each new dir (G2): `claudeN` (or `accounts.py exec 'claude N'`), then `/login` as claude N, per the guide's "Log out and in".
3. `accounts.py status`: every dir passes `check`.
4. Fixer follow-up 30 lands first: inventory must class build-followups units as units, or `down --restart` resumes them as top-level sessions on the account they ran on (the trial below). Record the previous unit role as `accounts.py role unit` prints it (`default` while units run on `~/.claude`) and the default login's note label, confirm the target passes `check`, and run `accounts.py set-role unit "<label>"`, so new units start there and a restarted unit resumes there. Then move each unit director the user names, one at a time and only on the user's word (user, 2026-10-11): `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down '<default note label>' --here --only '<unit session id>' --restart`. Inventory brings the director's owned seats along. `--here` keeps the Mac out, since units run only on natedev. Never send an empty `--only`: it means every session on the account.
   - A nonzero exit from this `down` stopped nothing: fix the refusal and run it again. `accounts.py set-role unit '<previous role>'` undoes the role.
   - After an accepted request, watch `shutdown.py status '<default note label>' --here` and the target's inventory.
   - On `stop partial`, retry `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down '<default note label>' --here --restart`. It keeps the saved `--only` scope and refuses a different one. The printed retry omits `--here`; add it, because no peer record exists. Follow any manual-resume notice for the old account's record, and confirm completion before the next unit.
   - Role commands name `~/.claude` only as `default`. `set-role` refuses the default login's note label, and any dir logged in to that login, with `<label> is the default account; use default`, as swap does. Before this refusal `set-role unit 'claude 2'` pointed at a missing `~/.claude2` and failed as `account identity for claude 2 is unreadable` (showrunner, 2026-10-11).
   - Trial, 2026-10-11: the showrunner moved mul_add to claude 3 by hand (`/exit`, then `accounts.py exec 'claude 3' --resume <id>` in its tmux) after `down --restart` resumed it on claude 2: inventory classed it top-level, and restart resumes a top-level session on the account it ran on. Messaging to and from it passed. The unit role is back to `default`.
   - `/showrunner:accounts swap` is not used here: it moves every unit on one account at once and refuses the showrunners and top-level sessions that share the default account. It is for a later move of all units.
5. Confirm each unit's status line label and `shutdown.py status "<label>"`. Confirm the showrunner reaches each unit by message. Then check messaging across machines and accounts with `send.py`, since the user requires that any Claude reach any other on both machines: a unit on the target account and a session on the Mac each reach the other, and each reply comes back. Direct SendMessage to the other machine goes through Remote Control, which is per account (Phase 6 (g)); `send.py` resolves through the roster and crosses machines over ssh, and on one machine a relay on either account delivers to a session on either (Phase 6 (c)). The Mac relay's `~/.config/claude/oauth-token.gpg` belongs to an account nobody has recorded; any valid login delivers, but its quota gates every Mac relay. Record its account by having the user run on the Mac `CLAUDE_CODE_OAUTH_TOKEN="$(gpg --quiet --decrypt ~/.config/claude/oauth-token.gpg)" claude auth status | grep email`.
6. Record here the cutover time T0 with its timezone, the default account's login and note label, and `threshold_percent`.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the cutover time T0.

**Acceptance gate:** re-inventory the saved session ids for their resumed pids. Every unit director's process shows the target account (`account.py --pid`), the selected local shutdown is complete, and every pre-cutover showrunner and top-level session still runs on its own account. Messages reach each unit and unit timers are restored. T0 is recorded only after these checks pass.

#### Results so far (2026-10-11)

- **T0: 2026-10-11 16:27 PDT**, when the last unit director resumed on claude 3. The default account is claude 2 (`browses31_perks@icloud.com`); the unit account is claude 3 (`rubrics.dogwood-1f@icloud.com`); `accounts.py role unit` prints `claude 3`; `threshold_percent` is 2 (`~/.claude/config/showrunners.json`).
- **Moves, all on the user's word (times from the showrunner):** mul_add by hand at about 15:1x PDT; phase-tables at 16:18 PDT, by hand after its restart failed on a plan-path mismatch (routed to the shutdown unit); then builder-report, feature-log, model-study, screenshot, showrunner-fixer, shutdown, test-threads and two-accounts through `shutdown.py down --only … --restart`, 16:21–16:27 PDT. two-accounts stopped at 16:21 and resumed at 16:24 with its conversation intact.
- **Checked after the moves:** `account.py --pid` shows claude 3 for all ten unit directors' live processes. Showrunners stay on claude 2.
- **Seen from claude 3:** SendMessage to the bare name `natedev` is refused as ambiguous, because that account's Remote Control list also holds an offline `natedev` row from an earlier session of that name. Fixed 2026-10-11 by sending to the session's address on this machine; see the section on it below.
- **Still open:** steps 1–2 for the spare (`~/.claude1` is not set up or logged in); step 5's check that the showrunner reaches each unit, and the `send.py` check across machines and accounts.

#### Defect found after the cutover: memory writes ask on a named account (2026-10-11)

- **Seen:** five hana unit directors on claude 3 (trunk, startup, widget, lfo, cables) each stopped on a "Do you want to create <memory file>?" form. On claude 2 these writes never asked.
- **Cause, read in Claude Code 2.1.296 and reproduced:** Claude Code saves a memory file without asking only when the path as written and the path after links are resolved both sit inside the account's memory folder. `~/.claude3/projects` is a link to `~/.claude/projects`, so the resolved path lands under `~/.claude` and the exemption fails. The write then meets the "sensitive file" check for anything under a `.claude` folder. That check runs before allow rules, so `Edit(~/.claude/**)` cannot clear it, and no permission-settings change would help.
- **Reproduced:** a scratch session on claude 3 in the default permission mode was refused with "… which is a sensitive file … resolves through a symlink to /home/natepiano/.claude/projects/…".
- **Fix, approved by the user 2026-10-11 22:4x PDT and built:** start every named-account session with `CLAUDE_CODE_REMOTE_MEMORY_DIR=$HOME/.claude` beside `CLAUDE_CONFIG_DIR`. Claude Code then uses `~/.claude/projects/<project>/memory/` itself, the folder the default account uses, with no link in the path. The same scratch write then went through. Three launch points set it through `account.named_account_environment` (`scripts/whoami/account.py`): `accounts.py exec`, the account prefix `add_unit.py` uses, and the resume command in `scripts/shutdown/restart.py`. A launch on the default account drops it, the nightly review launch included.
- **Why it waited on the user:** the change stops a permission prompt, so it was theirs to approve; the session's permission check refused the edit until they did. The variable is also absent from Claude Code's public documentation, so a later release could change it; the scratch test is the check to rerun after an update. One side effect: "local" memory for custom subagents would move under `~/.claude/projects`; nothing here uses it.
- **Rejected:** an allow rule (the check runs first); `autoMemoryDirectory` (one folder for every project); a real `projects` folder per account (breaks resume across accounts and splits memory).
- **Rollout:** a session on a named account that started before the fix asks on each memory write until it is restarted; each restart is on the user's word. To list the ones still to restart, look for a live Claude process whose environment has `CLAUDE_CONFIG_DIR` and no `CLAUDE_CODE_REMOTE_MEMORY_DIR`.

#### Added after the cutover: one nightly review session, on the unit account (2026-10-11)

Asked for by the user through the showrunner at 23:00 PDT. The systemd timer in `/etc/nixos` is the showrunner's and is unchanged: it still runs `nightly_review.py launch`.

- **One session a night.** `nightly-review` replaces `nightly-config` and `nightly-rust`. Its first prompt is `/nightly_review config rust`; `commands/nightly_review.md` does the modes one at a time in the order given, each still writing `NIGHT/<mode>.md` and messaging natedev before the next starts. It works in `~/rust` with `/etc/nixos` and `~/.claude` added.
- **Account.** `nightly_review.tonight()` reads `accounts.role_account("unit")`. A named account launches through `accounts.account_prefix`, the prefix units use, so the memory variable comes with it; with no role set the launch is the default-login one as before. An account that fails `accounts.check` skips the night with `skipped: unit account <label>: <problem>` in `launch.log`.
- **Gates read the same account.** The shutdown check gets that account's `CLAUDE_CONFIG_DIR`, and `quota_for_selection` reads the note whose login holds the `unit directors` role, which is the default login when no role is set. A shutdown or low quota on the other account no longer decides the night.
- **Replacing last night's session** keeps the guards: left running while the user is attached; skipped when a session no nightly review started holds the name. The two old session names are retired under the same guards and never block the night.
- **Not done:** no launch by hand. The first timer run after merge is the check, and the showrunner reads its `launch.log`.
- **Account folders are in the review's deny list (2026-10-12).** `scripts/nightly_review/settings.json` denies edits under `~/.claude1`, `~/.claude2` and `~/.claude3` beside `~/.claude`, `~/rust` and `/etc/nixos`. The user said yes to this through the showrunner on 2026-10-12. Before it, links in an account folder were covered because they resolve into `~/.claude`, and the folder's own login files were covered only by Claude Code's sensitive-file check. A fourth account folder needs its own line; a test holds the three lines to the names `accounts.account_dir` gives.

#### `/showrunner:accounts` replaces `/accounts` (2026-10-12)

Asked for by the user through the showrunner. Text above this section that names `/showrunner:accounts` was written as `/accounts`.

- **One table.** `accounts.py status`, which `/showrunner:accounts` runs with no argument, prints one Markdown table: a row for `~/.claude`, each `~/.claudeN` on disk, and each `claude N` account that has a note and is in no folder (`not set up`). Columns: folder, label, login or `not logged in`, role (`showrunners`, `unit directors`, `spare`), 5-hour and weekly left with their reset times, and when the numbers were read. A named folder keeps its own label whatever login it holds. What the old report listed per folder (check problems, unclassified entries, MCP server differences) prints as lines under the table, only when there is something to say.
- **Numbers are saved readings, not a live read.** The usage service refused 103 reads in 174 runs of the two-minute updater over six hours (HTTP 429). `scripts/whoami/usage_store.py` keeps each login's last good 5-hour and weekly window in `~/.local/state/agent-notes/usage.json`; `agent_notes.update_for_selection` saves every read that succeeds. The table reads the store, and reads a logged-in folder live only when it holds no reading from the last 10 minutes (`STALE_AFTER`). A read that succeeded with no 5-hour window stores 100% left and no reset. A window whose reset has passed shows `unavailable`.
- **Zone.** Times use the first registered showrunner's User zone, else this machine's zone.
- **Codex rows (2026-10-12, the user: "i was expecting to see codex in the accounts list and was expecting to see if we were running on credits or not").** Under the Claude rows: the Codex home (`~/.codex`, role `seats`), then each `codex N` account with a note that the home does not hold (`not logged in`). Two columns before `Read`: `On credits` and `Credits left`; Claude rows show a dash. `On credits` is `yes` when a window the reading holds has 0% left and credits remain: in `readings.jsonl` the Codex balance fell 243 times, 242 with weekly at 0% just before and 1 right after a reset. The 5-hour half of that rule is not measured: codex 2 (plan `promax`) has only a weekly window, so its 5-hour cells show a dash. `usage_store` keeps Codex readings under `codex:<login>` with a `credits` entry, and no `five_hour` entry for an account with no such window. `account/rateLimits/read` also returns `ordinaryUsageAllowed`, unused here.
- **After the user's first run (2026-10-12).** `accounts.py` with no action runs `status`; before, it exited 2. An account the default folder holds gets no `not set up` row for its own missing folder, so each label appears once unless two folders hold one login. `cache` and `.last-cleanup`, which Claude Code writes in every folder, are known per-folder names and no longer print as unclassified.
- **`/accounts` is retired**, with no forwarding command. `setup` and `swap` are arguments of `/showrunner:accounts`. The add_unit refusal, the `accounts.py exec` refusal, the quota alert's swap line and the docs name the new command.
- **Files:** `scripts/accounts/accounts.py`, `scripts/whoami/usage_store.py` (new), `scripts/whoami/agent_notes.py`, `commands/showrunner/accounts.md` (new), `commands/accounts.md` (deleted), `docs/accounts.md`, `docs/quota_alerts.md`, `docs/production_format.md`, and the tests beside each script.

#### Messages reach the session on this machine by its address (2026-10-11)

- **Seen:** `SendMessage` refuses a bare name when the sender's account lists two sessions under it. A session that moves to another account leaves an offline Remote Control row behind, so the cutover made `natedev` ambiguous for every unit on claude 3, and any later move does the same to the moved session's name.
- **Fix, approved by the showrunner:** the refusal is Claude Code's, so the change is on our side of the lookup. `sessions.py address <session:id|name>` prints `uds:<socket>` for the live session on this machine, which `SendMessage` takes as `to`. `showrunners.py address <slug>` prints it for a production's showrunner, by session id. The unit launch prompt and `docs/production_format.md` tell a unit to send there, looked up before each message, and the nightly command uses `sessions.py address natedev`.
- **Checked live:** a message sent from this unit to the looked-up address arrived, and the showrunner answered it.
- **Never reply to a message's `from` socket.** A showrunner's messages arrive through `send.py`'s relay process, which ends at once, so that socket is gone by the time a reply is written.
- **Rollout:** units started before this keep the old prompt and need the row's ref until they restart. The showrunner's own sends to units by name can hit the same refusal; `sessions.py address <unit session name>` serves it, and whether it adopts that is its call.

### Phase 8 — Move one session to any account  · status: todo · waits on the user's go

#### Work Order

**Goal:** one command moves any one session (showrunner, unit director or top-level) to any account and keeps its conversation. The unit role only picks the account for new unit launches. User, 2026-10-11: "we can run out of account usage on a showrunner too so we need the ability to move arbitrary sessions to arbitrary accounts."

**Constraints from prior phases:** today only the unit role is selectable; showrunners and top-level sessions run on `~/.claude`'s login, and swap moves only units. `shutdown.py down <account> --only <ids> --restart` already stops sessions safely (each records where it was, then exits) and resumes them in their recorded host: tmux respawns the pane, ghostty and zed open a window, a unit relaunches through `add_unit.py --resume`, and an unknown host prints `manual restart <name>: <command>`. Restart resumes a non-unit session on the config dir its process ran with (`Session.config_dir`), and `add_unit.py --resume` takes the unit role. Inventory finds units only through registered showrunners, so a build-followups unit is classed top-level (fixer follow-up 30); it moves through its tmux pane either way. Same-machine messaging works across accounts, and Remote Control is per account (Phase 6 (c), (g)). The hand move the showrunner ran for mul_add was `/exit`, then `accounts.py exec 'claude 3' --resume <id>` in its tmux. Phase 7 moves units with the role and `down --restart`; once a unit resume keeps the account it ran on (spec 4), a later move of every unit goes through swap's `--to`.

**Spec:**

1. `accounts.py move <session id | name> <claude N | default>`, also `/showrunner:accounts move`. It finds the live session in this machine's session registry by id, or by the name it runs under, and reads its current account from its process's config dir. A name two live sessions share refuses and lists their ids.
2. Before any change it refuses and names the cause:
   - no live session by that id or name on this machine (a session on the other machine moves from there);
   - a seat (`seats move with their unit director`);
   - a target that fails `check` (its first problem);
   - a target that is already the session's login (`<name> is already on <label>`);
   - the default login's note label, or a dir logged in to that login (`<label> is the default account; use default`), as set-role and swap refuse it;
   - a shutdown already open on the session's account.
3. Otherwise it runs `shutdown.py down '<current account note label>' --here --only <id> --restart --to '<target>'`. `--to` (also on `begin`, and forwarded to the peer's `begin`) stores the target on every scoped record entry; without it an entry has none. The session stops as any shutdown stops it.
4. Restart resumes an entry that has a target through `accounts.py exec '<target>'`, with restart's usual resume arguments: `--resume <id> -n <name> --remote-control <name>`, the model, and the restart note (or `/showrunner:produce <doc> resume` for a showrunner). So an inherited `CLAUDE_CODE_OAUTH_TOKEN` is dropped and the target is checked again at launch. `exec` accepts `default`: plain `claude` with `CLAUDE_CONFIG_DIR` removed. An entry with no target resumes on the account it ran on, as today. By host:
   - tmux: respawns its own pane, as restart does now.
   - unit: `add_unit.py --resume … --account '<target>'`. A unit resume without `--account` keeps the account it ran on; `add_unit.py` reads the unit role only for a new unit.
   - ghostty, zed or Terminal: reopens its window, as restart does now.
   - unknown: move prints the one resume line before the stop starts, with `run this once <name> has exited`, and restart reports the same line as `manual restart <name>: <line>`.
5. A session may move itself. Its own request is recorded as the requester, as for any shutdown it asks for.
6. `swap` keeps its refusals and becomes `set-role` plus `down '<old>' --restart --to '<new>'`, because a unit resume no longer reads the role.
7. A moved session keeps its name, so `send.py` and same-machine SendMessage reach it on either account. Its Remote Control registers on the target account, and the old account's phone app no longer lists it.

**Files:**

- `scripts/accounts/accounts.py` — `move`, `exec default`, swap's `--to`; `scripts/accounts/test_accounts.py`.
- `scripts/shutdown/shutdown.py`, `settle.py`, `record.py`, `restart.py` — `--to` on `down` and `begin`, the entry's target, resume through `exec` in every host; their tests.
- `scripts/production/add_unit.py` — `--account` on `--resume`, and a resume keeps the account it ran on; its tests.
- `docs/accounts.md` — "Move a session", with every refusal; `commands/accounts.md` — the argument hint; `docs/as-built/shutdown-and-restart.md`.

**Acceptance gate:** the accounts, shutdown, production and whoami suites and the scoped lint pass. Live on natedev with a scratch probe in tmux: move it claude 2 → claude 3 → `default`. After each move it answers a question about its earlier turns, its status line and `account.py --pid` show the target, and `send.py` reaches it and its reply comes back. A second probe with no known host gets exactly one printed resume line, and running that line resumes it on the target. `swap unit … --dry-run` prints `down … --to`. `~/.claude/.credentials.json` is unchanged and no other session moved.

### Phase 9 — One-week readout  · status: todo · gated by G3

#### Work Order

**Goal:** answer the user's question with a week of data: did the showrunner account last the week, and how many unit swaps did it take?

**Constraints from prior phases:** role history is `~/.local/state/accounts/roles.jsonl`. Records from Phase 4 on carry `at`, `role`, `label` (the dir selected, or `default`) and `account_label` (the note label at write time); use `account_label` for them. Older records keep `burn`'s current-dir resolution and can refuse an unreadable historical identity. A role row is an assignment, not a completed move: a refused `down` writes a compensating row. `agent_notes.py` keeps readings for eight days, so the readout runs at T0 + 7 d.

**Spec:** run `accounts.py burn --since <T0> --until <T0 + 7 d>`, and read the role history, `~/.local/state/agent-notes/readings.jsonl`, and the shutdown records or recorded outcomes that tell completed moves from refused or rolled-back ones. Report role-write times as assignment times and confirm each move separately; mark an unconfirmed outcome rather than counting it as a swap. Name the intervals where old units were still finishing after a new role was written. Compare the default account's observed remaining values with the `threshold_percent` recorded at T0; state gaps and observed-day coverage, and flag any change of the default login instead of inferring its past role from today's login. Report to the showrunner:

- points per account per day
- the role split
- each swap's time
- whether the default account went below `threshold_percent`

Recommend whether any change is needed. The nightly review already moved to the unit role on 2026-10-11, so include what it spent there.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the readout.

**Acceptance gate:** the readout cites its window, account identities, observed days, assignment and move evidence, and the observed quota minimum. It tells rolled-back or unconfirmed attempts from completed swaps, reports coverage gaps, and is in this plan and sent to the showrunner.

### Phase 10 — A home per Codex account  · status: todo

User, 2026-10-12, through the showrunner: "make the codex account half of this work as well". Phases 10–12 are that half. Source: the Codex half of `~/.local/state/plan-backlog/2026-10-09-per-session-accounts.md`.

#### Work Order

**Goal:** every Codex account the user has is signed in in its own home and read live, so `/showrunner:accounts` shows numbers for each.

**Constraints from prior phases:** one home today, `~/.codex`. `agent_accounts.codex_live()` takes no home and `live_reports` makes one Codex report. `agent_notes.apply` marks a Codex note `active` only when its login is that one report's, so a second report would set the first note `inactive`. The table already has the Codex rows, the credits columns and the store key `codex:<login>`; a noted account in no home reads `not logged in`.

**Spec:**

1. `~/.codexN` is the home of `codex N`. `~/.codex` stays the default home, and seats run on it until Phase 11.
2. Hand checks first, results recorded here: Codex keeps a linked `config.toml` and `hooks.json` when it writes them; `CODEX_HOME=~/.codexN codex login` leaves `~/.codex/auth.json` untouched; usage is readable under a second home while seats run on the first.
3. `accounts.py setup "codex N"` builds `~/.codexN` (0700), links the shared entries to `~/.codex` (`config.toml`, `hooks.json`, `AGENTS.md`, `skills`, `rules`, `plugins`, `sessions`, `archived_sessions`, `session_index.jsonl`, `history.jsonl`, `models_cache.json`), leaves the sign-in and Codex's own state in the home, and prints the sign-in command. `check "codex N"` reports a broken or replaced link, an entry it does not know, a login that is not the note's, and a login another home holds (two homes on one login break token refresh).
4. `agent_accounts.codex_live(home)` runs the app-server with `CODEX_HOME=<home>`. `live_reports` returns one Codex report per home, and each report names its home.
5. `agent_notes.apply`: a Codex note is `active` when any home holds its login. Quota alerts, the 5-hour hold and the pacer keep reading only the account the seats run on, until Phase 12.
6. The table: one row per Codex home, then a `not set up` row for each noted account in no home. Role `seats` on the home in use, `spare` on the others. Check problems print under the table.

**Files:**

- `scripts/accounts/accounts.py` — `setup` and `check` for a Codex home; a table row per home.
- `scripts/accounts/test_accounts.py`
- `scripts/whoami/agent_accounts.py` — `codex_live(home)`; one Codex report per home.
- `scripts/whoami/agent_notes.py` — a Codex note is active when any home holds its login.
- `scripts/whoami/test_agent_notes.py`
- `scripts/whoami/whoami.py` — each Codex report under its account.
- `docs/accounts.md`
- `commands/showrunner/accounts.md`

**Acceptance gate:** suites and lint pass. Live: `~/.codex1` is set up, the user signs codex 1 in there, and the table shows both Codex accounts with numbers read in the last 10 minutes while seats keep running on `~/.codex`.

### Phase 11 — One Codex account for all seats, and a swap  · status: todo

#### Work Order

**Goal:** the `seats` role names the Codex account every unit's seats run on, and `/showrunner:accounts swap seats "codex N"` moves them.

**Constraints from prior phases:** `codex_mesh._start_server` starts a run's server with the unit director session's environment, and `mesh_server.json` holds only the port and pid. A changed sign-in already retires a run's server and starts another, and the seats go on. The role store holds only `unit`.

**Spec:**

1. Role `seats` in `~/.claude/config/accounts.json` (absent means `~/.codex`), with `role seats`, `set-role seats` and the role history, as `unit` has.
2. `codex_mesh` starts a run's server with `CODEX_HOME` set to the role's home, whatever the caller's environment, and records the home in `mesh_server.json`. `ensure_server` retires a server whose recorded home is not the role's. The `codex exec` fallback gets the same home.
3. Hand check first: a thread started under one home continues under another when `sessions` is shared. If it does not, swap replaces the seats with new threads and says so.
4. `swap seats "codex N" | default`: refuses a target that fails `check`, that is already the role, or whose reading shows a used-up window and no credits. It writes the role, retires every run's server through the changed-sign-in steps, and sends each affected unit director one line. A seat mid-turn finishes first; its next turn runs under the new home. `--dry-run` prints what would move.
5. `scripts/message/codex_daemon.py` reads its control socket from the home in use.
6. The table's `seats` role follows the role.

One Codex account serves all seats at a time, as one Claude account serves all unit directors. A single unit on its own Codex account belongs with Phase 8.

**Files:**

- `scripts/accounts/accounts.py` — the `seats` role and `swap seats`.
- `scripts/accounts/test_accounts.py`
- `scripts/agents/codex_mesh.py` — the server starts under the role's home and records it.
- `scripts/agents/test_codex_mesh.py`
- `scripts/agents/agent_exec.sh`
- `scripts/agents/test_agent_exec.sh`
- `scripts/message/codex_daemon.py`
- `scripts/message/test_codex_daemon.py`
- `docs/accounts.md`
- `commands/showrunner/accounts.md`

**Acceptance gate:** suites and lint pass. Live: one swap to codex 1 and back while a seat works, the seat continues, `mesh_server.json` names the home, and the table shows the role moved.

### Phase 12 — Alerts per Codex account  · status: todo

#### Work Order

**Goal:** every Codex alert names its account and follows a swap.

**Constraints from prior phases:** `quota_alert` keys episodes by note, and its text says "the active Codex account". One `switch` moves every Codex assignment to Claude. `blocked` takes the first used-up note and is given no account. `five_hour` keys its hold by tool.

**Spec:**

1. Alerts read the account in the `seats` role and name it. A spare Codex account raises no alert.
2. A low or used-up alert carries a swap line when another signed-in Codex account has allowance or credits: the command, that account's weekly left and its credits.
3. The 5-hour hold and its relay are keyed by account, so a swap carries no hold across.
4. `codex_mesh` gives `blocked` the account that refused, and the alert names it. The Codex-to-Claude `switch` opens for the account in the role and closes when a swap moves the seats to an account with allowance.

**Files:**

- `scripts/whoami/quota_alert.py`
- `scripts/whoami/test_quota_alert.py`
- `scripts/whoami/five_hour.py`
- `scripts/whoami/test_five_hour.py`
- `scripts/whoami/agent_notes.py`
- `scripts/whoami/test_agent_notes.py`
- `scripts/agents/codex_mesh.py` — gives `blocked` the account that refused.
- `scripts/agents/test_codex_mesh.py`
- `docs/quota_alerts.md`

**Acceptance gate:** suites and lint pass, with fixtures of two signed-in Codex accounts: one in the role and low, one spare.

Left out of the 2026-10-09 proposal: moving seats to another Codex account automatically when Codex refuses work (every swap stays the user's call), and the Mac.

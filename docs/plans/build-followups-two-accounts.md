# Two accounts: showrunners on one Claude account, unit directors on another

> **Status: IMPLEMENTATION PLAN — phases 1–6 are done; the cutover (Phase 7) moved all ten build-followups unit directors to claude 3 on 2026-10-11, with the spare dir and the cross-machine message check still open; Phase 8 moves any one session, or every showrunner off an account, by hand; Phases 14–17 drop the unit role so every new session picks the account with room, and move Codex seats and Claude sessions by themselves when their account refuses them; Phase 9 reads out a week of moves; Phases 10–12 add the Codex accounts: a home per account, one account for all seats with a swap, and alerts per account; Phase 13 repairs a seat launch that finished its turn while the seats account changed.** Phases 1–5 built everything and initiated nothing: no account dir, no login, no role. Phase 6 proved `~/.claude3` live. The design: each account the user has gets its own config dir, `~/.claude1`, `~/.claude2` and `~/.claude3`. Each holds only that account's login, and everything else is a symlink into `~/.claude`. Unit directors launch with `CLAUDE_CONFIG_DIR` set to the unit account's dir. Showrunners and every other session stay on `~/.claude`'s own login, as today. When the unit account runs low, one command swaps every unit onto a spare account through `/shutdown`'s safe stop and resume. Any one session, a showrunner included, moves to any account the same way with `/showrunner:accounts move` (Phase 8). Phased, delegate-ready once approved.

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
6. **Accounts by room (Phases 14–17).** No role ties a kind of session to an account. A new session launches on the account with room that resets soonest (`accounts.py pick`). When an account refuses a session or the Codex seats, they move to the picked account by themselves and continue; a 5-hour refusal whose reset is under 20 minutes away waits instead. Phase 17 proves it live with the user.
7. **A week later (Phase 9).** A readout gives the moves, their causes, any time with no account having room, and points per account per day.

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
    - Each `SessionShutdown` record's embedded `session.config_dir` holds the session's dir (absent means `~/.claude`); a non-unit resume keeps it, and a unit relaunches on the current unit role.
  - `scripts/shutdown/inventory.py`: `inventory()` lists `sessions.live_sessions()` (`~/.claude/sessions`), then keeps the sessions `_configuration_for_session` attributes to the login through `account_of`; `_model()` reads `~/.claude/projects` for the last model. `record.py` holds the record types (`SessionShutdown`, one per top-level session, and `AccountHold`). `shutdown.py` holds the parser, `resolve_sessions`, status and the launch checks; `settle.py` holds the work behind the verbs and the conductor (`settle.conduct`), which the `conduct <login>` verb only calls.
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
  - Times shown to the user are in the user's zone. Code on this branch still names America/Los_Angeles; the user-zone change (build-followups-enh-showrunner 43a14830) moves every such time to one setting, `config/user_zone.conf`, read through `scripts/lib/user_zone.py`.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G0 | Phase 1 | the user | cleared 2026-10-10 for the code phases 1–5: "prepare everything but don't initiate" |
| G1 | Phase 6 | the user | the user explicitly authorizes the first live proof. Phase 6 then creates `~/.claude3`, and the user completes its login (step 2) before any probe starts |
| G2 | Phase 7 | the user | the user authorizes cutover at a chosen time and confirms the unit account. Phase 7 then sets up and logs in the remaining dirs, and every check passes before step 4 writes the role |
| G3 | Phase 9 | the clock | 7 days after Phase 17's live proof |
| G4 | Phase 17 | the user | the user is present for the live moves |

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
- Shutdown inventory reads each session's account and config dir together at one boundary (`AttributedSessionConfiguration`) and stores the dir as `config_dir` on the session snapshot, absent meaning `~/.claude`; an unreadable environment leaves the session unattributed. The shutdown record holds no second copy: the dir lives only on the snapshot (`SessionShutdown.session`, since shutdown moved to one record per session), and an old-format record's entry-level key is dropped when it is read. `restart.py` puts `CLAUDE_CONFIG_DIR=<quoted dir> ` before `ENABLE_TOOL_SEARCH=true` for a non-unit resume whose dir is not `~/.claude`; units relaunch through `add_unit.py` on the current role.
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

**Ruled out:** a second `config_dir` on the shutdown record beside the session snapshot (settle replaces the snapshot, so a second copy goes stale); an explicit `CLAUDE_CONFIG_DIR` in `remove_seats.py` (inheritance already carries it).

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

**Ruled out:** consolidating the readings path's three homes, since it reaches the usage collector (`scripts/usage/collect.py`, `scripts/usage/outcomes.py`), outside the accounts scripts.

### Phase 4 — One-command swap of the unit account  · status: done

#### As-built

- `accounts.py swap unit <label|default> [--dry-run]` is `swap(role, label, *, dry_run=False) -> SwapOutcome` (`SwapDryRun | SwapStarted | SwapRolledBack`). It resolves `claude_account_selection()` once and names the old account by its note label throughout. It reaches shutdown only by subprocess (`shutdown.py status <old> --json`, then `shutdown.py down <old> --restart`). Before any write it refuses an unreadable current unit account (`accounts: current unit account identity is unreadable`) or default identity, the default login named by label (`<label> is the default account; use default`), a target missing from the checked selection (its first `check` problem), and a target already the unit account. It also refuses any old-account session that would not move (`accounts: cannot swap <old>: <machine> <kind> <name> would not move`): only units on this machine and seats owned by those units' directors move; every peer-machine session, other kind, unowned seat, unattributed session or unavailable inventory refuses.
- Otherwise swap writes the role (`_write_unit_role` keeps a `PreviousUnitRole`), then runs `down`. A nonzero `down`, or a failed spawn, restores the exact previous `accounts.json` (an absent file included), appends a compensating role-history row, and exits with `down`'s code. The CLI prints `down`'s output, then `units come back on <target> when <old> is down` or `unit account unchanged: still <old>`. `--dry-run` prints the `set-role` and `down … --restart` argv and writes nothing.
- `set_role` resolves the selected dir's identity (`~/.claude` for `default`) and refuses an unreadable one, writing nothing. `RoleChange` extends `LegacyRoleChange` with `account_label`, the note label at write time; `_unit_role_history` uses it and resolves a legacy row through the dir's current identity.
- Shutdown moved to one record per session after this phase; this bullet and the next two describe it as it is now. `shutdown.py down --restart [--only NAMES]` stores `then: restart` on every `SessionShutdown` record the request adds, and on the `AccountHold` of an account-wide request; the peer gets `down <login> --here --request-id <id> --restart` and conducts its own records. `record.py` holds `ShutdownContinuation = Literal["stop", "restart"]`; `then` is required on both types, and only the old-format record the converter reads may lack it (read as `stop`). An external requester gets the result but holds no readiness. The conductor (`settle.conduct`) relaunches each session once its own record has stopped, without waiting for other sessions or the peer.
- `report_stop` sends the requester `<label> is back` when every record restarted, `<label> is down` with the `/shutdown restart` line for a stop-only request, `<label> is not fully back` with `settle.shutdown_command("restart", <label>)` when a restore failed, and `<label>: stop partial` with `/shutdown now retries the failed stop` when a stop is incomplete.
- A repeated `down` adds only the sessions that have no record (`<name>: added`). A session that has one prints `<name>: already in progress` and keeps its `then`, `hold`, `alerts`, `request_id` and `requested_by`; a conductor starts either way. A failed stop is retried by `/shutdown now [--only <name>]`: it sets `force: "now"` and the conductor retries. `stop._retry_owner_cleanup` retries the Codex-server and unit-tmux cleanup left over on an owner already stopped.
- `commands/accounts.md` is `/showrunner:accounts [status | setup "claude N" | swap unit "claude N" | swap unit default]`: it runs the verb and shows its output unchanged; switching the default account remains `/login` in a `~/.claude` session.

**Files:**
- `scripts/accounts/accounts.py` — `swap`, its outcome and inventory types, `set_role` identity check, `account_label` on role history.
- `scripts/accounts/test_accounts.py` — swap refusals, dry run, rollback, peer refusal, one selection through write and shutdown, burn attribution across logout and re-login.
- `scripts/shutdown/record.py` — `then` on `SessionShutdown` and `AccountHold`, `ShutdownContinuation`.
- `scripts/shutdown/settle.py` — `begin`/`down` with `then`, `report_stop`, `shutdown_command`, `alert_user`.
- `scripts/shutdown/shutdown.py` — `down --restart`.
- `scripts/shutdown/stop.py` — `_retry_owner_cleanup`.
- `scripts/shutdown/test_record.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_stop.py` — the above.
- `commands/accounts.md` — `/showrunner:accounts`.
- `docs/as-built/shutdown-and-restart.md` — `--restart`, the retry of a failed stop, restart per session.

**Binds later work:** The user's guide describes what the user sees: swap's two closing lines, each refusal before any write, and the retry of a failed stop (`/shutdown now`); `commands/accounts.md` exists and the guide only adds a pointer to it. A rule added to `check` reaches swap's target refusal, and an unreadable unit account refuses swap. A `down … --only <ids> --restart` without `--here` sends the peer only the ids this machine does not hold; each machine conducts its own records, and a peer that is not reached prints `<machine>: not reached; run /shutdown there` without delaying local work. The cutover moves natedev units with `down --here --only <ids> --restart`. `burn` reads `account_label` from role history; the one-week readout depends on it.

**Gotchas:** Restart is decided per session record, never from `conduct`'s exit code; `settle.py` imports `restart` inside the conductor's functions, never at module level. Swap runs `status` and `down` without `--here`, so it needs the Mac reachable even when the Mac holds nothing on the old account. The printed retry of a failed stop is `/shutdown now`; at a terminal it is `shutdown.py now <label> [--here] [--only <name>]`, and a repeated `down` retries nothing. A refused conversion of an old record exits 3 from `down`, which swap treats as any nonzero `down`: the role goes back.

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

#### As-built

- **Setup and login (natedev, 2026-10-11).** `~/.claude3` exists: `accounts.py setup "claude 3"` built it, and the user logged it in as claude 3 through the `claude3` alias before any check started; `accounts.py check "claude 3"` passes. Every check below ran against one scratch session on claude 3, started in tmux with `accounts.py exec 'claude 3' --remote-control <name> -n <name>` beside the running sessions of the main account (claude 2, `~/.claude`). `setup`'s closing hint and the guide's wrong-login step print `env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=… claude`; the raw `CLAUDE_CONFIG_DIR=… claude` they printed before keeps an inherited OAuth token, which takes precedence over the dir's login.
- **(a) Logins — pass.** `claude auth status` under each dir shows that dir's own account, and the claude 3 login did not write `~/.claude/.credentials.json`.
- **(b) Registry and transcripts — pass.** A claude 3 session's record is `~/.claude/sessions/<pid>.json` and its transcript is `~/.claude/projects/<cwd>/<session id>.jsonl`. Its status line reads `claude 3 | 5-hour … left | weekly … left`; the SessionStart hook writes `~/.claude3/account-label`.
- **(c) Messaging — pass.** On one machine, messaging works across accounts: `ListAgents` lists a session on the other account, SendMessage reaches it and its SendMessage reply returns, and a `send.py` relay on either account delivers to a session on either. `notify_when_idle` fires for a session on the other account; the notice arrives at the receiver's next turn boundary, not mid-turn.
- **(d) Shutdown attribution — pass.** `account.py --pid <pid>` names claude 3. `shutdown.py status "claude 3"` lists the claude 3 sessions, and claude 2's status lists none of them. `shutdown.py down 'claude 3' --here --only '<session id>' --restart` wrote a record with scope `selected` and one entry (`config_dir` `~/.claude3`) that ended `up` and archived (the record shape of that date; shutdown now keeps one record per session). The session came back without help as `claude --resume <session id> -n <name> --remote-control <name> --model <model>`, on claude 3, with the same transcript and history. All 27 claude 2 sessions kept running.
- **(e) Resume across accounts — pass.** Resume works in both directions and keeps history, because `projects` is shared: a session started on `~/.claude` resumed under `accounts.py exec 'claude 3' --resume <id>` (its pid and its status line both name claude 3), then resumed on `~/.claude` with the turns from both accounts.
- **(f) Links survive writes — pass.** After `/model` to another model and back, and one prompt, `~/.claude3/settings.json` and `history.jsonl` are still links, and the prompt is in the shared history.
- **(g) Remote Control — per account.** A session on claude 3 is neither listed in nor reachable from the main account's Remote Control: the phone app signed into the main account did not list it, and its direct link answered "this session is not available". The phone reaches units on claude 3 only by signing into claude 3; showrunners relay.
- **Cleanup.** The scratch sessions are exited, and their transcript dirs under `~/.claude/projects/` and their scratch dirs are deleted. `~/.claude3` stays in place.

**Files:**
- `scripts/accounts/accounts.py` — `setup`'s closing login hint prints the `env -u CLAUDE_CODE_OAUTH_TOKEN` command
- `scripts/accounts/test_accounts.py` — asserts that hint's text
- `docs/accounts.md` — the wrong-login step gives the same command

**Binds later work:** `~/.claude3` is set up, logged in as claude 3 and stays in place for the cutover. Results (c) (on one machine, messaging crosses accounts in both directions), (g) (Remote Control is per account) and (e) (resume crosses accounts with history) are cited by letter; (b), (c) and (e) are what the symlink design depends on. This phase wrote live state outside the repo: `~/.claude3` (the dir, its links, its login and `account-label`), which remains; scratch transcript dirs under `~/.claude/projects/`, since deleted; one archived shutdown record; and `model` in `~/.claude/settings.json`, written through the link in (f) and restored byte for byte. No unit role and no existing session changed.

**Gotchas:** A session's first start in a folder stops on the folder-trust prompt until it is answered. Claude Code's auto-mode permission check refuses a session's own `accounts.py setup`, and its `shutdown.py down` as "Interfere With Workloads", so the user runs both at a terminal. `/model` writes `model` through the link into `~/.claude/settings.json`, so a model choice in any account changes the default for every account, as a `~/.claude` session's does. A `send.py` relay from a claude 3 session runs on `~/.claude`'s account and briefly shows in claude 2's `shutdown.py status` as `unknown account · <relay pid> <session name> · process start mismatch`. The status line shows the account label only after the first prompt redraws it; `account-label` is written about 7 s after start.

**Ruled out:** `accounts.py exec` as the login hint: for a label whose login is `~/.claude`'s it skips the dir, so `/login` would change `~/.claude`.

### Phase 7 — Cutover  · status: in progress · gated by G2

#### Work Order

**Goal:** the unit directors the user names run on the chosen unit account, new units start there, showrunners stay on `~/.claude`'s account, and every spare dir is logged in.

**Spec:** the director runs it with the user.

1. `accounts.py setup` for each account the user has, except the one `~/.claude` is logged in to. The Phase 6 dir already exists.
2. The user logs in each new dir (G2): `claudeN` (or `accounts.py exec 'claude N'`), then `/login` as claude N, per the guide's "Log out and in".
3. `accounts.py status`: every dir passes `check`.
4. Fixer follow-up 30 lands first: inventory must class build-followups units as units, or `down --restart` resumes them as top-level sessions on the account they ran on (the trial below). Record the previous unit role as `accounts.py role unit` prints it (`default` while units run on `~/.claude`) and the default login's note label, confirm the target passes `check`, and run `accounts.py set-role unit "<label>"`, so new units start there and a restarted unit resumes there. Then move each unit director the user names, one at a time and only on the user's word (user, 2026-10-11): `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py down '<default note label>' --here --only '<unit session id>' --restart`. Inventory brings the director's owned seats along. `--here` keeps the Mac out, since units run only on natedev. `--only` takes session ids or exact names. An empty one is refused (`shutdown: --only needs a session id or exact name`, exit 2); a `down` with no `--only` at all means every session on the account.
   - A nonzero exit from this `down` stopped nothing: fix the refusal and run it again. `accounts.py set-role unit '<previous role>'` undoes the role.
   - After an accepted request, watch `shutdown.py status '<default note label>' --here` and the target's inventory.
   - On a failed stop (`<label>: stop partial` in the result, `stop failed` or a stop issue in status), retry with `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now '<default note label>' --here --only '<unit session id>'`: the conductor retries the stop, and for a session already stopped it retries only the cleanup left over. A repeated `down` retries nothing: it prints `<name>: already in progress` and leaves the record as it was. Follow any `manual restart` line for the session, and confirm completion before the next unit.
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

### Phase 8 — Move one session to any account  · status: done

#### As-built

- `accounts.py move <session id | name> <claude N | default> [--why '<text>']` (also `/showrunner:accounts move`) finds the live session in this machine's registry, reads its account from its process's config dir, and returns `MoveStarted | MoveStartFailed | MoveRefused`. Before any change it refuses: no live session here, a name two sessions share (lists ids), a seat, a target that fails `check`, the session's own login, the default login's label or dir (`use default`), or an open shutdown record. Otherwise it runs `shutdown.py down '<current label>' --here --only <id> --restart --to '<target>' --why '<why>'` (default why `<old label> is out of usage`); a session may move itself.
- `accounts.py move showrunners --from '<label>' --to '<claude N | default>' [--why] [--dry-run]` returns `ShowrunnersMoveStarted | ShowrunnersMoveStartFailed | ShowrunnersMoveDryRun | MoveRefused`. It selects every live inventory `showrunner` on `--from`'s login, refuses the whole command on a bad target or none found, skips one with an open record, and runs one `down` over every selected id. `--dry-run` prints the selection, the `down` line and each resume line.
- `down --to/--why` stores `target`, `source_label` and `why` on each new `SessionShutdown`; an existing record never gains them; the peer `down` carries both. A record with no target resumes on the account it ran on.
- Restart relaunches a targeted record through `accounts.py exec '<target>'` with the usual resume arguments plus one moved-not-stopped note from `restart.account_move_note` (moved from, to, because; an account move, not a stop; continue). A unit continues `/unit:direct`; a showrunner gets `/showrunner:produce <doc> resume` with the note. tmux respawns its pane; a unit resumes through `add_unit.py --resume --account '<target>'`; a Ghostty session is closed at stop and reopened on its recorded desktop (kdotool `set_desktop`), so one window per session on its desktop; an unknown host prints the resume line once before the stop, and restart reports `manual restart <name>: <line>`. A moved session keeps its name, so `send.py` reaches it; its Remote Control registers on the target account.
- `exec default` drops `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_OAUTH_TOKEN`. `swap unit` is `set-role unit` plus `down '<old>' --restart --to '<new>'` and refuses a unit with an open record; `swap seats` is unchanged.
- `add_unit.py` stores `@claude_config_dir` and `@claude_model` on each unit's tmux session and reads the unit role only for a new unit. An untargeted `--resume` uses the shutdown record's target, then those options, else refuses naming `--account`; a recorded dir outside the account farm is kept (`RetainedUnitAccount`), a missing one is refused; the recorded model goes through `--model`. `produce.md`'s manual unit resume runs `add_unit.py --resume`.

**Files:**
- `scripts/accounts/accounts.py` — `move`, `move_showrunners`, `_move_target`, `exec default`, swap through `down --to`; `test_accounts.py`.
- `scripts/shutdown/shutdown.py`, `settle.py`, `record.py`, `restart.py`, `stop.py` — `--to`/`--why`, the record fields, resume through `exec`, the moved note; `test_account_move.py` and the inventory, restart, settle, shutdown_cli and stop tests.
- `scripts/production/add_unit.py`, `test_add_unit.py` — `--account`, the tmux options, the untargeted-resume refusal.
- `commands/showrunner/produce.md` — unit resume through `add_unit.py --resume`.
- `docs/accounts.md` ("Move a session"), `commands/showrunner/accounts.md`, `docs/as-built/shutdown-and-restart.md`; `pyrightconfig.json` — shutdown extraPaths include the repo root.

**Binds later work:** "Claude sessions move on a refusal", "An account picked by room, not by role" and "Readout of the moves" call `accounts.move()` / `down --to`. Removing the unit role takes `swap unit` with it, but `exec unit` stays because nixos `shell.nix` aliases `claude-unit` to `accounts.py exec unit`. The readout counts `SessionShutdown` records with a `target` as moves.

**Gotchas:**
- Live Ghostty titles carry a status glyph (`✳ natedev`) and the shell retitles the window after exit.
- A session that already has a shutdown record is refused: a repeated `down` keeps the record as it was, so no target would be stored.
- A refused tmux/unit session already enters "held by usage limit" in shutdown (settle.py `USAGE_LIMIT_PATTERNS`) and stops once idle; Ghostty/zed sessions have no screen read.
- Transcripts record a Claude refusal for every host as an `assistant` entry with `error: "rate_limit"`, `isApiErrorMessage: true`, `apiErrorStatus: 429`, model `<synthetic>`, text "You've hit your session limit · resets …" (`session` = 5-hour, `weekly` = weekly).
- The 2-minute timer (`agent-sessions-snapshot`, nixos) runs `~/.claude/scripts/whoami/agent_notes.py` with no arguments, so a watcher hooked there needs no nixos change.

**Ruled out:** tracking and closing the old Ghostty window by title — titles change.

**Live check outstanding (with the user):** a scratch tmux probe moved claude 2 → claude 3 → `default` keeps its earlier turns, shows the target in its status line and `account.py --pid`, and answers through `send.py`; an unknown-host probe resumes from its one printed line; `move showrunners --from 'claude 2' --to 'claude 3'` brings each showrunner back in its own window on its own desktop with no old window left; `~/.claude/.credentials.json` is unchanged.

### Phase 9 — Readout of the moves  · status: todo · gated by G3

**Depends on:** Phase 17

#### Work Order

**Goal:** answer the user's question with a week of data: how often sessions and seats moved between accounts, why, and whether any time passed with no account having room.

**Constraints from prior phases:** from "An account picked by room" on, no role ties a session to an account: a new Claude session launches on `accounts.py pick claude`, Codex seats move on a refusal ("Codex seats move on a refusal") and Claude sessions move on a refusal ("Claude sessions move on a refusal"). Every automatic transition is one line of `~/.local/state/accounts/moves.jsonl`, keyed by a stable `move_id`: `requested`, `confirmed`, `failed`, `waited`, `wait ended`, `no room`, `room again`. `requested` and `waited` carry the reset when it is known (`reset` is omitted when unknown); `no room` carries `earliest_reset`. A Codex request has one `confirmed` per seat (`run`, `seat`), and one seat may fail beside others confirming; a Claude batch shares one shutdown `request_id` across several session subjects. Moves by hand (`move`, `swap`, `set-role`) are not journalled: take them from shutdown history and `roles.jsonl`. A Claude move is also a `SessionShutdown` record with `target`, `source_label` and `why` in `~/.local/state/shutdown/<login>/sessions/`, moved to `history/` there once the session is confirmed restarted; a record still in `sessions/` or ending `restart failed` is not a completed move. Role history `~/.local/state/accounts/roles.jsonl` still records each seats-home write (`role: seats`). `agent_notes.py` keeps readings (`~/.local/state/agent-notes/readings.jsonl`) for eight days, so the readout runs at T0 + 7 d, T0 being the live proof. `accounts.py burn` reads a `--since` or `--until` time with no zone in the user's zone, so give both with an explicit UTC offset.

**Spec:** for the window T0 to T0 + 7 d, read `moves.jsonl`, the shutdown records and history, `roles.jsonl` and `readings.jsonl`, and run `accounts.py burn --since <T0> --until <T0 + 7 d>`. Report to the showrunner:

- moves per tool (Claude session, Codex seats) and per cause (weekly refusal, 5-hour refusal, by hand), counted both per transition (one per `requested`, or per hand move) and per seat or session confirmed; a request with some seats confirmed and some failed is partial; each is confirmed against its record, and an unconfirmed or failed restart is marked, never counted as a move;
- the waits for a 5-hour reset under 20 minutes away, and how long each lasted (`waited` to `wait ended`; an unclosed one is an unknown interval, not a duration);
- every interval with no account having room (`no room` to `room again`), with the earliest reset each named, and unknown intervals marked;
- points per account per day, and observed-day coverage with gaps.

Recommend whether more accounts are needed.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the readout.

**Seats:** nothing splits: the unit director writes the readout from the records; no seat opens.

**Acceptance gate:** the readout cites its window, account identities, observed days, and the move, wait and no-room evidence. It tells unconfirmed or failed moves from completed ones, reports coverage gaps, and is in this plan and sent to the showrunner.

### Phase 10 — A home per Codex account  · status: done

#### As-built

- `~/.codexN` is the home of `codex N`. `~/.codex` stays the default home, and the seats run on it.
- `accounts.py setup "codex N"` needs the account's note. It builds the home (0700) and links the shared entries (`CODEX_SHARED`: `config.toml`, `hooks.json`, `AGENTS.md`, `skills`, `rules`, `plugins`, `sessions`, `archived_sessions`, `session_index.jsonl`, `history.jsonl`, `models_cache.json`) to `~/.codex`. It never links or copies the sign-in file. When another home already holds the note's login it refuses before it creates anything, with one line naming the account and that home. It prints `CODEX_HOME=~/.codexN codex login` only when setup reported no problem.
- `accounts.py check "codex N"` reports a broken or replaced link, an entry it does not know, a login that is not the note's, and a login another home holds.
- `agent_accounts.live_reports` returns one Codex report per home, and each Codex `Report` names its home in `CodexHomeSource(home)`.
- `agent_notes`: every signed-in home's note gets its numbers. `UnknownAccountActivity` leaves every Codex note's state as it is when the login of the home in use cannot be read. The 5-hour watch gets only the report of the home in use. With one home the behavior is the same as without numbered homes.
- `/whoami` prints one block per home. With several homes the block of the home in use is marked `seats`, and a block with no account is headed `Codex (~/.codexN)` or `Codex (~/.codex, seats)`. A spare home that is set up and not signed in reads `Codex (~/.codexN): Not logged in` in the `/whoami` header and in the updater's messages.
- The table has one row per home (`seats` on the home in use, `spare` on the others), then a `not set up` row for each noted `codex N` whose home is not listed and whose login no home holds, also when a file or dead link sits at the path. Check problems print under the table, and `setup` is the repair.
- The hook installer writes to the file a linked `hooks.json` leads to, with its temporary file beside that target, and the link stays, so numbered homes keep sharing the default home's hooks.

**Files:**
- `scripts/accounts/accounts.py` — Codex `setup` and `check`, `codex_statuses`, the table rows.
- `scripts/accounts/test_accounts.py` — tests of a home's setup state and of setup refused for a held login.
- `scripts/whoami/agent_accounts.py` — the home functions and one Codex report per home.
- `scripts/whoami/agent_notes.py` — numbers per signed-in home; `active` from the sign-in on disk of the home in use; `UnknownAccountActivity`.
- `scripts/whoami/test_agent_notes.py` — tests of a spare home not signed in and of an unreadable in-use login.
- `scripts/whoami/whoami.py` — one block per home.
- `scripts/whoami/test_codex_homes.py` — tests of the Codex homes, a spare home not signed in among them.
- `scripts/hooks/codex_hooks.py` — the installer that keeps a linked `hooks.json`.
- `scripts/hooks/test_codex_hooks.py` — tests that the installer keeps a linked `hooks.json`.
- `docs/accounts.md` — the Codex homes: `setup`, `check`, the setup states in the table.
- `commands/showrunner/accounts.md` — the `/showrunner:accounts` command text for Codex homes.

**Binds later work:**
- The home functions live in `agent_accounts`: `codex_home()` (this process's `CODEX_HOME`, else `~/.codex`), `codex_homes()` (the default, the numbered directories in number order, the process home), `codex_live(home)` (app-server child with `CODEX_HOME=<home>`), `CodexHomeSource(home)`, `codex_report_in_use(report)`, `codex_email_on_disk(home)`, `codex_home_display(home)` (the `~/.codexN` text for messages and headers).
- The home in use is this process's `CODEX_HOME`, else `~/.codex`, and four places read it: `agent_accounts.codex_report_in_use` (the `/whoami` `seats` mark and the 5-hour watch input), `agent_accounts.codex_homes` (it adds the home in use to the list), `agent_notes.update_for_selection` (which Codex note is `active`), `accounts.codex_statuses` (the table's `seats`). All four must follow the `seats` role in "One Codex account for all seats, and a swap", with tests for an unset and a conflicting `CODEX_HOME`: the updater runs with no `CODEX_HOME`, so while the readers stay on the process home a swap leaves the old account's note `active` and the alerts on an account no seat runs on.
- A Codex note is `active` only for the login of the home in use, read from that home's sign-in on disk. Quota alerts read `active` notes, so "Alerts per Codex account" gets no alert from a spare.
- `check` has no form for the default home. The swap in "One Codex account for all seats, and a swap" refuses a target that fails `check`, so it needs one.

**Gotchas:**
- Which login the home in use holds comes from the sign-in on disk (`codex_email_on_disk(codex_home())`), never from the live report: a failed live read must change no note's state. Read from the report, a failed read marks every Codex note `inactive` and clears the alert episode of the account the seats run on.
- `codex_homes()` lists directories only; a file or dead link at `~/.codexN` is reported by the `not set up` row and its `check` problems.
- Hand check, codex-cli 0.162.1: Codex writes through a linked `config.toml` and the link stays a link. Checked on 2026-10-12 with codex 1 signed in at `~/.codex1`: the sign-in wrote only that home's `auth.json` (`~/.codex` had none at that moment, after an unrelated `codex login` with no `CODEX_HOME`, and gained none); usage reads under the second home while seats run on the first, once that home's first start has finished (Spec item 8 of the seats-role phase); and at 10:30 PDT the table showed both Codex accounts with numbers read in the last 10 minutes while both seats worked.
- Lint exits 3 with `0 errors, 0 warnings, 0 notes` in this worktree, because `pyrightconfig.json` names an absent `.venv`.

**Ruled out:**
- Linking or copying the sign-in file into a numbered home: two homes on one login break token refresh.
- An empty login set for "the in-use login is unknown": it would mean "no account is in use" and mark every note inactive.
- Splitting "One Codex account for all seats, and a swap" in two: a role the mesh does not follow would make the table wrong.

### Phase 11 — One Codex account for all seats, and a swap  · status: done

#### As-built

- The `seats` role in `~/.claude/config/accounts.json` names the one Codex account every unit's seats run on: `default` (`~/.codex`) or `codex N` (`~/.codexN`); absent means `default`. `accounts.py role seats` prints it. `env seats` prints `CODEX_HOME=<home>` for both kinds, whatever the caller's own `CODEX_HOME` says. A role write keeps the other role. An empty role file, or one holding only `seats`, leaves the Claude role displays off. The account table's Role column shows `seats` on the selected home and `spare` on every other signed-in home. Note activity and the `/whoami` status line follow the selection: a Codex note is active only for the account in the role.
- `set-role seats <label>` writes the role after an on-disk check of the target. `swap seats <label> [--dry-run]` reads the target live, requires `codex_can_work` to answer that it can work, refuses the home already selected, then writes the role and retires the outgoing account's run servers. `_swap_seats(label: str, *, dry_run: bool) -> SeatsSwapOutcome` refuses before `_retire_seats_servers` when the target has no readable sign-in. A refused selection exits 1, writes nothing, and prints one line: `accounts: cannot select <label> (<folder>): <reason>`.
- Types: `accounts.role_account("seats")` returns `CodexSeatsAccount(home, label)`. `SelectableCodexAccount(home, login, label)` is a target whose home identity is checked before usage is read or the role is written. `codex_can_work(report: Report, now: datetime) -> CodexWorkReadiness` in `scripts/whoami/agent_accounts.py` answers `CodexWorkAvailable | CodexWorkExhausted | CodexWorkUnknown`, by allowance or by credits.
- The seat launcher calls `seats_home()` once per operation, starts its run server under that home, and records it as `codex_home` in the run's `ServerRecord` (`mesh_server.json`). A record without `codex_home` means the default home. A server retired by a swap or a new sign-in stays a `RetiredRunServer` until it stops. `agent_exec.sh` runs `codex exec` under the seats home. `codex_daemon` serves freestanding sessions on its own process home, never the role's; with `ROSTER_CODEX_SOCKET` set, `codex_daemon.queue()` talks to that socket itself.
- A seat's conversation moves to the selected account's server when the seat is next used. The launcher first checks that the old server has released it (released, waiting or unknown), then reopens it with the seat's standing instructions. A reopen the server refuses starts a replacement conversation that keeps the standing instructions, the read-only sandbox and the never-ask policy; one board line names the old and new conversation. `send` parks every message for the seat's launcher, which delivers it; with no live launcher it exits 2 with "launcher is closing; message not delivered; relaunch the seat". There is no `_load_thread`. `prime`, `fork` and `reset` run on the seat's home; `fork` and `reset` run the release check first and exit 2 with the reason while an old server of the run holds the conversation.
- Home setup: `prepare_codex_home(home: Path) -> CodexHomePreparation` starts a server under a numbered home and waits for `initialize`, up to `CODEX_HOME_PREPARATION_SECONDS` (1200 s). `setup "codex N"` runs it before it prints the sign-in command. A read with no `initialize` answer under a numbered home prints `Codex did not start at <home>; run <setup command> and try again`. `log` is in `CODEX_PRIVATE_NAMES`. `setup`, `docs/accounts.md` and `commands/showrunner/accounts.md` warn that `codex login` without `CODEX_HOME` signs `~/.codex` out and in.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py` — the `seats` role verbs, the swap, the Role column, the home setup; tests include `test_refused_seats_selection_names_the_tool_once`
- `scripts/whoami/agent_accounts.py` (`seats_home`, `codex_home`, `codex_home_display`, `codex_can_work`, `prepare_codex_home`, `user_zone`), `scripts/whoami/agent_notes.py`, `scripts/whoami/whoami.py`, `scripts/whoami/test_agent_notes.py`, `scripts/whoami/test_codex_homes.py` — note activity and the status line follow the seats selection
- `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py` — the seat launcher: home per operation, server records, the move, the release check, parked sends, `prime`, `fork` and `reset`; tests include the `test_reset_and_fork_*` group, `test_replacement_seat_keeps_standing_instructions_and_read_only_policy` and `test_held_send_reopens_on_the_seats_home_with_standing_instructions_before_delivery`
- `scripts/agents/agent_exec.sh`, `scripts/agents/test_agent_exec.sh` — one-shot Codex runs under the seats home; `scripts/message/codex_daemon.py`, `scripts/message/test_codex_daemon.py` — queueing to an explicit socket
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the guide and the command text for the role, the swap and the setup warnings

**Binds later work:** The per-account alerts read the seats account through `accounts.role_account("seats")` (`CodexSeatsAccount`: `home`, `label`; a label is `codex N` or `default`) or `agent_accounts.seats_home()`, and they own the alert surface for the selection. The refusing account for a failed turn comes from the `ServerRecord` (`codex_home`) of the server that ran it; a server retired by a swap is still on record as a `RetiredRunServer` until it stops, and its one outward sign is a board line. `codex_can_work` is the one can-work function: the alerts' swap line uses it as `swap seats` does. The role history holds `unit` and `seats` entries together, and the unit history reader leaves `seats` out of its sums, so a one-week readout counts `unit` assignments only. `swap seats` moves Codex seats; a Claude session moves with `swap unit`. One Codex account serves all seats at a time; no unit has its own. `scripts/agents/codex_mesh.py` and `scripts/agents/test_codex_mesh.py` also hold `prime`, `fork` and `reset` and their tests.

**Gotchas:**
- Not proven live: reopening a real conversation on a second Codex home, and one swap to another account and back while a seat works. That hand check with the user has not run. Real Codex handling of the standing-instruction and permission parameters on a reopen and on a replacement start has run only against test doubles. No `set-role seats` or `swap seats` has run against the real homes; the seats run on the default home, and servers started before this change keep serving while the role is `default` and its sign-in is current.
- Measured on codex-cli 0.162.1 with scratch servers: `thread/resume` with only the thread id continues a conversation under another home when `sessions` is shared, and both homes append to the one rollout file. Writer locks are per home: a second home's server resumes a conversation the first still holds, with no error, and the two then disagree, so the release check comes before every reopen. Each turn counts against the account of the home whose server runs it. With `-c thread_unload_delay_secs=0` an idle conversation unloads 0.04 s after its last connection closes; not measured for a conversation that has run turns. A numbered home has its own Codex state database, and its first `codex app-server` indexes the shared `sessions` before it answers `initialize`; a server closed mid-index leaves the index marked as running for 900 s, and each start in that time prints `state db backfill is running at <home>` and exits unanswered after about 31 s.
- Port numbers are reused across a run's retired servers, so a server is identified by its record, never by its port. A recorded pid that now belongs to an unrelated live process makes the release check answer unknown, the safe direction. A compaction refused because the seat's own conversation still works on the old server returns the live-turn code, and that launch fails.
- `codex_can_work` answers unknown ("credit balance is unavailable"), not used up, for a window at 0 with no credit reading; a swap is refused either way. The user's zone is written as a literal in `accounts.py` (`_iso_time`) and in `quota_alert.py` (`USER_ZONE`), while `agent_accounts.user_zone()` reads the production's zone. Not merged here yet: the user-zone change (build-followups-enh-showrunner 43a14830) replaces both literals with `scripts.lib.user_zone.user_zone()`.

**Ruled out:** replacing a seat's conversation only for some reopen errors (any refused reopen gets a replacement, and every prompt is complete on its own); keeping observed note activity on a machine with one Codex home (activity follows the seats selection everywhere); moving the seats to another Codex account automatically when Codex refuses work (every swap stays the user's call).

### Phase 12 — Alerts per Codex account  · status: done

#### As-built

- Quota alerts, 5-hour holds and the Codex-to-Claude fallback follow the Codex account in the `seats` role. A low or used-up alert names that account and carries `codex_swap_line(readings, now)` when another checked, signed-in home is accepted by `accounts.check_codex_target(label, seats_home)` and then by `codex_can_work`: the command (`swap seats default` for the default home, the numbered label otherwise), that account's weekly left and its credits. A spare account raises no low-quota alert.
- Weekly episodes are keyed by note name. An episode stores `weekly_reset` and closes once that time is reached or the account recovers, so an acknowledgement stays quiet while its account is spare and after the account is selected again still low.
- One frozen `CodexAlertPassReadings(seats, selected_account, checked_homes)` serves a whole updater run, each checked home a `CodexHomeReading(home, report, observed_at)`. The updater builds it with `codex_alert_pass(home, reports, observed_at)` before it writes notes and hands it to `five_hour.watch`, `quota_alert.alert` and `quota_alert.refresh`; a standalone call defaults to `READ_CURRENT_CODEX_READINGS`, and `resolve_codex_readings` collects an equal value. A reading older than `CODEX_READING_MAX_AGE` (10 minutes), a failed read, or an expired window recommends no swap.
- The seat launcher keeps `CodexServingState = ServingCodexServer | NotBoundCodexServer` on each client, captured before a request is submitted and kept through a server move and a replacement conversation. `ServingCodexServer(port, pid, home, account, started_here)` carries a `RefusingCodexAccount`; `NotBoundCodexServer` answers `UnknownCodexAccount`, so an unknown refusing account is a stated case, never inferred from the current role or from a port. A proven quota refusal is reported once per turn, on a started turn, a followed turn, a parked-message turn and a replacement conversation's turn, by `_report_quota_refusal(name, serving)`, which runs `agent_notes.py blocked`. A failure of `prime`, `fork` or `reset` keeps its serving account, and an unproven failure opens no fallback.
- `quota_alert.blocked(notes, refusing_account, now=None, *, selection, codex_readings)` opens the fallback only for a refusal from the role account while its week is low or its episode is open; above the threshold or with unknown weekly usage it moves nothing and writes one log line. Every move to Claude is announced to the sessions and to the user, and a failed move once per distinct failure. A refusal from an outgoing account sends a notice naming it and moves nothing; one from an unknown account is reported once per home. `CodexQuotaFallback = NoCodexQuotaFallback | AccountCodexQuotaFallback | UnknownOwnerCodexQuotaFallback`: a stored `switch` record without an owner becomes the unknown-owner state where the file is read. `switch_back(state, now, readings)` restores owed assignments when the selected account can work, by allowance or by credits, whatever the episodes say, and leaves assignments the user changed.
- A Codex 5-hour hold has the key `five-hour-codex-<hash of home and login>`, a `KnownCodexAccount` owner (`HoldOwner = UnownedHold | KnownCodexAccount`) and its `HeldRelay(recipient, key)` entries. `escalate.reconcile_codex_holds(home, login)`, called whenever the selected account is known, and `escalate.due(now)`, at delivery, cancel an outgoing account's holds, their relays and the old tool-wide `five-hour-codex` hold with no recovery message. An unknown identity cancels nothing and delivers no Codex hold; holds of other tools are untouched.

**Files:**
- `scripts/whoami/agent_accounts.py` — shared Codex account identity: `KnownCodexAccount(home, login, label)`, `UnknownCodexAccount(home, reason)`, `RefusingCodexAccount`, `codex_account(home) -> RefusingCodexAccount`, `codex_home_role_label(home) -> str`; one module object under both import names.
- `scripts/whoami/quota_alert.py` — alert hub: the readings value, episodes, `refusal_notices`, the swap line, the fallback.
- `scripts/whoami/five_hour.py` — 5-hour holds owned by the selected account.
- `scripts/whoami/agent_notes.py` — builds the readings for an updater run; holds the `blocked` verb.
- `scripts/message/escalate.py` — hold ownership, relay retirement, the owner check at delivery.
- `scripts/agents/codex_mesh.py` — the serving account per turn and the refusal report.
- `scripts/accounts/accounts.py` — `check_codex_target(label, seats_home) -> SelectableCodexAccount`; a set-up Codex home that is not signed in reads `~/.codex1 | codex 1 | not logged in | spare`, with `accounts: sign in as codex 1 at ~/.codex1` under the table; that row's label and the swap line's command label both come from `codex_home_role_label`.
- `scripts/whoami/test_codex_alert_accounts.py` — account ownership, swap advice and fallback recovery across swaps of the seats account.
- `docs/quota_alerts.md` — the user-facing account of the alerts.

**Binds later work:** `scripts/whoami/agent_accounts.py` is imported by name, `agent_accounts` or `scripts.whoami.agent_accounts`, never loaded by path. "Move one session to any account" edits `scripts/accounts/accounts.py` and depends on this rule; a swap of the seats account also retires the outgoing account's holds, at the next updater run or at delivery.

**Gotchas:**
- whoami modules load under two names. `agent_accounts` registers itself in `sys.modules` under both, so the identity classes are one object; a path load makes a second set of classes and `isinstance` on the shared types fails across callers.
- `blocked` never creates an episode; only `alert` does. Refusal messages are remembered per home in `refusal_notices`, apart from episodes.
- In `blocked`, the sent mark (`push` on a notice, `refusal_push` on an episode) suppresses a repeat only when no new move and no new failure occurred; returning on the mark first silences the messages of a later, real move to Claude.
- Never exercised against the real services: a real provider quota refusal, a real swap of the seats account, and a real 5-hour hold delivery. The tests use fixtures of two signed-in accounts, one in the role and low, one spare.

**Ruled out:**
- A refusal above the weekly threshold opening the fallback.
- Retrying a failed restore from `switch_back`.
- Moving seats to another Codex account automatically when Codex refuses work, because every swap stays the user's call.
- The Mac.

### Phase 13 — A launch that finished its turn ends as done across a seats change  · status: done

#### As-built

After a seat launch's own turn has ended, a move to the selected seats home that answers `SeatThreadStateUnknown` with `NoSeatTurnToCleanUp` never fails the launch. This holds at three sites in `_stream_turn` in `scripts/agents/codex_mesh.py`: the two `_move_resident` calls after a `turn/completed` and the one in the ended-unseen branch.

- A launch that is not resident closes the seat's inbox and keeps what is parked, in one `_pending_file` lock (`_close_messages_and_keep_pending(session_dir, name, thread_id) -> int`). With one or more kept it posts `launcher: 1 message held for the next launch: <reason>` (or `<N> messages`) and writes the seat-log line `mesh: <name> cannot move: <reason>; 1 message held for the next launch`. It then finishes as the ended turn deserves: the roster reads `done` or `failed` by the turn's own result, and nothing is sent to the target server.
- A resident launch logs `mesh: <name> cannot move: <reason>; waiting` and keeps serving; its idle ticks try the move again.
- A `SeatThreadStateUnknown` with `SeatTurnCleanup` still raises `SeatMoveUncertain` and cleans up on the target port. `_move_idle_seat` answers `SeatTurnCleanup(port, thread_id)` for every failure after the target accepted a turn, the pending clear, the final roster write and the lock release included; with no turn submitted a failure still means no cleanup.
- With the seat's end marker present the hold writes, posts and logs nothing.
- The next `follow` reopens the inbox, resumes on the selected home and starts one turn: its own prompt, then the held messages in order.

**Files:**
- `scripts/agents/codex_mesh.py` — the three sites, `_close_messages_and_keep_pending`, and `_move_idle_seat`'s one handler around the move.
- `scripts/agents/test_codex_mesh.py` — twelve tests of the hold, the cleanup routing between two ports, the ended seat and the resident retry.

**Gotchas:** Held messages stay with the thread: a `start` that replaces the thread drops them, a `follow` keeps them. `basedpyright` exits 3 in every checkout because its config names a `.venv` no checkout has; the pass is its last line. Both files are model-study-unit's as well. A real launch in flight across a real role change is not proven by tests; it is the live swap check with the user.

**Ruled out:** failing the launch whenever a move cannot be confirmed (the turn's own result is already known).

### Phase 14 — An account picked by room, not by role  · status: done

#### As-built

- `accounts.pick` chooses among `default` plus checked named accounts (Claude) or checked seat homes (Codex), one candidate per login (`default` first, then lowest number). Order: `keep` while it has room, else soonest weekly reset, then most weekly left, then label. Room is more than 0% left in the weekly and 5-hour windows (Codex: `agent_accounts.codex_can_work`'s shared rule, credits included); there is no minimum.
- Readings come from the usage store through `stored_reading`; a reading older than 10 minutes skips the candidate. An unresolved refusal in `~/.local/state/accounts/refusals.json` excludes the account whatever the saved percentages say.
- The verb `accounts.py pick claude|codex [--exclude '<label>']... [--keep '<label>'] [--explain]` prints the label (exit 0) or `no <tool> account has room; the earliest reset is <time>` (exit 1); `--explain` adds a line per candidate with `picked`, `has room`, `refused at <time>` or its reason.
- The unit role is gone: `role_account("unit")`, `role`/`env`/`exec unit` call `pick("claude", now)` and store nothing; a `unit` key in `config/accounts.json` is ignored. The `seats` role is unchanged.
- `add_unit.py`, `add_showrunner.py` and `nightly_review.py` launch on the picked account (no 10% gate) and record `@claude_config_dir`/`@claude_model`. A showrunner resume keeps its account: `--account`, else `--config-dir` (resolved against the invoking cwd), else tmux `@claude_config_dir`, else a one-line refusal; promotion passes the unit's recorded dir.
- Whoami, notes and quota alerts cover every login a live session runs on; `unit_swap_line` is a move line with no swap command; the status table marks the account `pick claude` chooses now with `next`.

**Files:**
- `scripts/accounts/accounts.py` — `pick`, its types and verb, `stored_reading`, `role_account`, the status table, `claude_account_selection`.
- `scripts/accounts/refusals.py` — the refusal store and its resolution rule.
- `scripts/production/add_unit.py`, `add_showrunner.py`, `promote_showrunner.py` — launch on the picked account; showrunner resume keeps its account.
- `scripts/nightly_review/nightly_review.py` — launches on the picked account.
- `scripts/whoami/agent_accounts.py`, `agent_notes.py`, `quota_alert.py`, `whoami.py` — `ClaudeAccountsInUse` and its readers.
- `scripts/shutdown/restart.py` — writes `@claude_config_dir` on a tmux respawn with a target.
- `docs/accounts.md`, `commands/showrunner/accounts.md`, `docs/quota_alerts.md` — `pick`, no unit role, `claude-unit` on the picked account.

**Binds later work:** "Codex seats move on a refusal" and "Claude sessions move on a refusal" call `accounts.pick(tool, now, *, exclude, keep) -> PickedAccount | NoAccountWithRoom` with `exclude={refusing label}`; `pick` never returns an excluded label, and with no fresh reading anywhere it returns `keep` else `default`, else `NoAccountWithRoom` when readings exist but none has room. Both write refusals through `refusals.py`: `AccountRefusal{tool, login, window: KnownWindow | UnknownWindow, reset: KnownReset | NoResetKnown, at}`, `write_refusal` (locked, atomic, an older refusal never replaces a newer one) and `resolved(refusal, reading, now)`. `accounts.stored_reading(tool, login, now) -> FreshReading | StaleReading | NoReading` is the only path from `StoredUsage` into picker types. `restart.py` sets `@claude_config_dir` on a tmux respawn with a target, so a moved session's later resume keeps the new account; restart edits sit beside that write. `ClaudeAccountsInUse` carries `live_logins` and `live_directories` (a live login on a dir failing `check` still reports). `set-role unit` and `swap unit` refuse with `UNIT_ROLE_GONE`; `exec unit` stays because `/etc/nixos/modules/linux/shell.nix` aliases `claude-unit` to it. A Codex move line parallels `unit_swap_line`'s move line.

**Gotchas:** The Mac has no usage timer, so every launch there lands on keep/default; `--explain` says `no fresh readings on this machine; using <label>`. A missing reading is never a refusal: only `refusals.json` excludes an account regardless of readings, because the usage store keeps the last good reading after a failed read. A showrunner resume must never kill an unrelated tmux session that shares its name. A relative `--config-dir` resolves against the invoking cwd, not the script's.

**Ruled out:** refusing a launch when no reading exists (a missing reading is not a refusal); a minimum room threshold (1% left is room); picking on a showrunner resume (a resume keeps its account); any role tying a kind of session to an account.

### Phase 15 — Codex seats move on a refusal  · status: done

#### As-built

- Only a refusal moves seats. `codex_mesh` reports it via `agent_notes.py blocked --window weekly|five_hour|unknown --refused-at <aware ISO>` into `quota_alert.blocked(..., window: KnownWindow | UnknownWindow, refused_at) -> list[str]`.
- `blocked`: a known 5-hour reset in the future and under 20 minutes waits (one `waited` per reset, then `wait ended`); else `accounts.pick("codex", now, exclude={seats label})` and the `swap seats` role write; a failed swap writes `failed` and falls back to Claude; no room writes `no room` + `no_room_line`, runs `switch_to_claude`, later `room again`.
- `switch_back` needs `refusals.resolved` plus allowance, and may move seats to any account with room.
- A refused seat is `continuation_owed`: on resume or after a wait it runs one `Continue where you stopped.` turn before held messages. `confirmed` is written only on a real matching resume.
- `moves.py`: `MoveEntry` union of `Requested/Confirmed/Failed/Waited/WaitEnded/NoRoom/RoomAgainMove` sharing `at, tool, subject, move_id, kind`; `append_move` validates and appends under `flock`; `read_moves()` skips malformed lines.
- `codex_swap_line`: `seats move to <picked> when refused`, or no room until `<reset>`.

**Files:**
- `scripts/accounts/moves.py`, `test_moves.py` — the journal; `refusals.py`, `accounts.py` — readings, pick.
- `scripts/whoami/quota_alert.py`, `agent_notes.py` — decisions, CLI; `scripts/agents/codex_mesh.py` — seat side.
- `docs/quota_alerts.md`, `docs/accounts.md` — automatic Codex moves.

**Binds later work:** store `~/.local/state/accounts/moves.jsonl`, reused by "Claude sessions move on a refusal" (`move_id` = shutdown `request_id`) and read by the move readout. `append_move` dedups only `confirmed`, `wait ended`, `room again`; the caller enforces one `waited` per reset. `requested` is never proof; Codex `confirmed` is keyed `(move_id, run, seat)`. `RequestedMove.reset` is omitted when unknown. `refusals.stored_reading` is the only reading path. "Live proof of the moves" drives `agent_notes.py blocked --window --refused-at`.

**Gotchas:** `accounts.py` never imports `codex_mesh`/`quota_alert` at module level (import cycle). `agent_accounts.user_zone()` reads registered showrunners and spawns `sessions.py`, so tests reaching it point `showrunners.NOTIFIER_STATE_DIR` at an empty dir, and gates rerun under the real home. The `codex_mesh` end-to-end move test needs a 30 s deadline.

**Ruled out:** environment-variable handoff of the refusal window; `stored_reading` in `accounts.py`.

### Phase 16 — Claude sessions move on a refusal  · status: todo

**Depends on:** Phase 14, Phase 15

#### Work Order

**Goal:** a Claude session that its account refuses (showrunner, unit director or top-level; in tmux, Ghostty, zed or a terminal on natedev — the Mac has no timer and is out of scope) is found and its move started within one 2-minute tick; it stops once safe (idle, seats idle, no merge, nothing ahead), resumes on the picked account and continues its work.

**Spec:**

1. **Detection, every host.** A session is refused when the last entry of its transcript (`~/.claude/projects/<project>/<session id>.jsonl`, located as `inventory._model` locates it) is an `assistant` entry with `isApiErrorMessage: true` and `error: "rate_limit"`. Its text names the window: `You've hit your session limit · resets 11:30pm (America/New_York)` is the 5-hour window, `weekly limit` the weekly one; other text takes the window the Codex path takes (`quota_alert._refusal_window`): the one window at 0 in a fresh reading taken after the refusal; none or both at 0, a reading from before the refusal, or no fresh reading is unknown. The reset time comes from the account's stored reading, never from the text. The refusal's time is that entry's own `timestamp` and its identity is (session id, entry `uuid`): a rescan of an unchanged transcript keeps the time and repeats no transition.
2. **The watcher.** `scripts/accounts/refusal_watch.py`, run at the end of `agent_notes.py`'s argument-free run (the `agent-sessions-snapshot` timer, every 2 minutes on natedev); a failure prints one stderr line and never fails the notes update. Each run reads the live top-level sessions on this machine (`shutdown/inventory.py`; seats excluded, and a session with an open shutdown record skipped) and finds the refused ones.
3. **Decide.** Record the refusal in `refusals.py`. Window and reset are typed as in "Codex seats move on a refusal". Only a known 5-hour window whose known reset is in the future and less than 20 minutes away waits: one `waited` entry per session per reset, and `wait ended` when it passes. Otherwise `accounts.pick("claude", now, exclude={<its account>})`. Sessions refused on the same account move together: one `shutdown.py down '<from>' --here --only <ids> --restart --to '<to>' --why '<from> refused: <window> limit until <reset>'`, through a `move_sessions(ids, target, *, why)` that `move_showrunners` also uses. Each gets a `requested` entry keyed by its shutdown `request_id`; `restart.py` writes `confirmed` only when the resumed session's process runs on the target account (its config dir is the target's, as `account.py --pid` reads it); an already-live session still on the source writes `failed` with reason `still on <source>`, and `restart failed` or `manual restart` writes `failed`. A target resume whose timers did not restore (`timers pending`) is `confirmed`: the move is proven, and the pending timers stay on the record's status line. `move_sessions` returns a typed outcome per session: requested with its `request_id`, or refused before any record exists (a cold key, the conductor failing, settle refusing) — a `failed` entry with that reason and no `requested`, tried again on the next tick while the refusal stands. A record cancelled by `/shutdown cancel` writes `failed` with reason `cancelled`, deletes its assumed refusal, and that refusal does not move the session again. No account with room: one alert naming the earliest reset, a `no room` entry, `room again` when an account has room, and no second alert for that account until a reading changes.
   - **The real model.** `inventory._model` skips an assistant entry whose model is `<synthetic>`, so a refused session resumes on the model of its last real reply.
4. **A refused session stops without answering.** A refused session cannot answer the settle message, so shutdown's usage-limit hold (`settle._usage_limit_shown`, today a screen read of tmux and unit panes only) also enters `held by usage limit` when the transcript shows the refusal of item 1, or an assumed refusal of item 6, for every host. The hold's stop rule is unchanged: owner idle, every seat idle, no merge in progress, nothing ahead of upstream. A move blocked there stays visible: `/shutdown status` shows each moved record's target and why on its progress line.
5. The moved session resumes with Phase 8's moved-not-stopped note (from, to, why): a unit director continues its `/unit:direct` run, a showrunner `/showrunner:produce <doc> resume`.
6. `refusal_watch.py --dry-run` prints each refused session and what it would do, and changes nothing. `refusal_watch.py --assume-refused '<session id | name>' weekly|five-hour [--reset-in <minutes>]` writes `~/.local/state/accounts/assumed_refusals/<session id>.json` and runs once; `--reset-in` is that session's reset for the wait decision only, and changes no stored reading; the watcher and the shutdown hold both read that file as a refusal of that one session only, and it is deleted when the session's record restarts or is cancelled. It is for the live proof.
7. `/showrunner:accounts status` lists each live session's account; the Codex seats' account and the account `pick codex` would move them to on a refusal, labelled as that (Claude's `next` stays Claude's); each seat that still owes its continuation after a move and why it is held (the mesh roster's `continuation_owed`); and the last 10 entries of `moves.jsonl` in the user's zone, each request with its outcome — confirmed per seat or session, failed with its reason, or pending. A rendering test covers each.
8. **The refusal time is typed.** `quota_alert.blocked`'s `refused_at: datetime | None` becomes `RefusedAt(at)` or `RefusalTimeUnknown()` (the alert pass uses its own time), converted at the `agent_notes.py blocked` argument boundary; the watcher passes `RefusedAt` from the transcript.

**Files:**

- `scripts/accounts/refusal_watch.py`, `scripts/accounts/test_refusal_watch.py` — new: the watcher and its tests.
- `scripts/accounts/accounts.py` — `move_sessions`, the status lines; `scripts/accounts/test_accounts.py`.
- `scripts/whoami/agent_notes.py` — calls the watcher, converts `--refused-at`; its test.
- `scripts/whoami/quota_alert.py` — the typed refusal time; `scripts/whoami/test_quota_alert.py`.
- `scripts/shutdown/settle.py` — the transcript and assumed refusal in the usage-limit hold, target and why on the status line; `scripts/shutdown/test_settle.py`.
- `scripts/shutdown/inventory.py` — `_model` skips `<synthetic>`; `scripts/shutdown/test_inventory.py`.
- `scripts/shutdown/restart.py` — `confirmed` and `failed` entries; `scripts/shutdown/test_restart.py`.
- `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`, `commands/showrunner/accounts.md` — sessions move on a refusal; the hold reads the transcript.

**Seats:** 1 writer + 1 tester.
- `impl` — every production file and doc above; hubs: `scripts/accounts/accounts.py` (`move_sessions`), `scripts/whoami/quota_alert.py` (the refusal time).
- `test` — the tests above, from the Spec: detection on a fixture transcript per window and per host, the window rule (reading before the refusal, both windows at 0, no reading), a rescan changing nothing, a cold key / conductor failure / cancellation each ending `failed`, an already-live session on the source ending `failed`, a target resume with timers pending ending `confirmed`, the status lines, the 20-minute wait and its end, grouping by account into one `down`, no room with one alert and `room again`, the Ghostty hold entered from the transcript and from an assumed refusal, a move blocked by a busy seat staying visible with its target and why, a real reply followed by a refusal resuming on the real model, `confirmed`/`failed` entries, `--dry-run` changing nothing.

**Constraints from prior phases:** Phase 8: `accounts.move()` and `move_showrunners()` resume through `down … --restart --to` with a `--why` stored on each `SessionShutdown` record (`target`, `source_label`, `why`) and a moved-not-stopped note; a Ghostty session's window closes at stop and reopens on its recorded desktop. "An account picked by room" ships `pick`; "Codex seats move on a refusal" ships `scripts/accounts/moves.py` (`MoveEntry`, `append_move`, `read_moves`). Shutdown's usage-limit hold matches `USAGE_LIMIT_PATTERNS` in `settle.py` on a tmux or unit pane's last 15 non-empty lines; Ghostty, zed and terminal sessions have no screen read. The timer script is `/etc/nixos/modules/linux/agent-sessions-snapshot.py` and runs `~/.claude/scripts/whoami/agent_notes.py` with no arguments, so no nixos change is needed. The Mac has no timer: a known gap, out of scope. New code calls `user_zone()` for times.

Phase 8's moved-not-stopped note and per-host restart already work; keep their tests green. "An account picked by room" made `restart.py` set the tmux option `@claude_config_dir` to the target dir on a tmux respawn with a target, so a moved showrunner's later `add_showrunner.py --resume` keeps the new account; the `confirmed`/`failed` writes go beside it. A showrunner resume takes `--account`, `--config-dir` (absolute against the invoking cwd) or its tmux `@claude_config_dir`. `pick` with `exclude` and no fresh reading anywhere returns `keep`, else `default`, never the excluded label; refusals go through `refusals.write_refusal`. `agent_accounts.ClaudeAccountsInUse` (`live_logins`, `live_directories`) is the set of accounts live sessions run on. "Codex seats move on a refusal" ships the moves log's entry kinds and the typed refusal handoff. A reading comes from `refusals.stored_reading` (moved there from `accounts.py`); `accounts.py` imports no Codex mesh or alert code at module level, or lint fails on an import cycle. `append_move` deduplicates `confirmed` (by `move_id`, `run`, `seat`), `wait ended` and `room again`; one `waited` per reset is the caller's to keep, by reading `read_moves` first, as `quota_alert` does. A `requested` with an unknown reset omits `reset`. `accounts.no_room_line` and every reset or status time go through `agent_accounts.user_zone()`, which reads the machine's registered showrunners (`showrunners.NOTIFIER_STATE_DIR`, fixed from the real home at import) and runs `sessions.py socket` for each: every test module that reaches it points `NOTIFIER_STATE_DIR` at an empty directory, as `test_nightly_review.py`'s `setUpModule` does, or it fails on a machine with showrunners.

**Acceptance gate:** the accounts, shutdown, whoami and nightly_review suites and the scoped lint pass, and the suites the phase touched pass again under the real home. `refusal_watch.py --dry-run` on natedev reports no refused session, or each one with its decision.

### Phase 17 — Live proof of the moves  · status: todo · gated by G4

**Depends on:** Phase 15, Phase 16

#### Work Order

**Goal:** show the user, on natedev, a Claude session and the Codex seats each moved by a refusal and working on the target.

**Spec:**

1. `accounts.py pick claude --explain` agrees with status's next Claude account, and `pick codex --explain` with status's account the seats would move to.
2. A scratch probe in tmux on a Claude account: `refusal_watch.py --assume-refused <probe> weekly` moves it to the picked account through the real stop path (the shutdown hold reads the assumed refusal). It resumes with the moved note, answers a question about its earlier turns, and `account.py --pid` shows the target. Run once more with `five-hour --reset-in 15` and it waits: one `waited`, then `wait ended` once the 15 minutes pass.
3. A scratch probe in a Ghostty window: the same move, with the window back on its desktop.
4. Codex, with the user present: `agent_notes.py blocked <seats home> <login> '<seats label>' --window weekly --refused-at <now, aware ISO>` moves the seats role to the picked Codex account: one `requested`, then one `confirmed` per seat as each resumes on the new home. The mesh's own path — a seat refused mid-turn resumes with exactly one "Continue where you stopped." before any held message — is proved by its tests and live by the first real Codex refusal (item 6). A Codex 5-hour wait is shown live only if a 5-hour reset under 20 minutes away occurs during the session; otherwise its tests stand.
5. `moves.jsonl` holds, for each move above, its `requested` and then a `confirmed` per seat or session (or `failed` with its reason), and for each wait its `waited` and `wait ended`; `/showrunner:accounts status` lists them with their outcomes.
6. The first real refusal after this phase, Claude or Codex, is reported to the showrunner with its entries when it happens. Phase 17 completes, and G3's clock starts, without waiting for it.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the results.

**Seats:** nothing splits: the unit director runs the proof with the user; a defect found gets a repair seat under the usual rules.

**Constraints from prior phases:** the three phases before this ship `pick`, the Codex move on a refusal, and the Claude watcher with `--dry-run` and `--assume-refused`. No `set-role`, `swap`, `move` or `shutdown down` runs against the real home without the user present. Never print a credentials file.

**Acceptance gate:** each item above observed live and written into this plan; `~/.claude/.credentials.json` unchanged; no Claude session other than the probes moved, except by a real refusal; the seats moved once in item 4, with the user present, and every running seat continued.

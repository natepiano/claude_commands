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
6. **Accounts by room (Phases 14–24).** No role ties a kind of session to an account. A new session launches on the account with room that resets soonest (`accounts.py pick`). When an account refuses a session or the Codex seats, they move to the picked account by themselves and continue; a 5-hour refusal whose reset is under 20 minutes away waits instead. Phase 18 replaces the picker with two groups (showrunners, units), each on one account and moved together. Phase 19 makes every group move pause first. Phase 20 adds a per-Codex-account credits setting and one alert when the workers' account reaches 0%; Phase 21 renames the account notes by number and gives each note its roles, live session count and session table; Phase 22 names every account by its number, never "default"; Phase 24 proves the moves live with the user.
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
    - `projects`, 33 entries holding `hasTrustDialogAccepted`, `allowedTools`, `enabledMcpjsonServers`, `disabledMcpjsonServers`, `mcpServers` and `hasClaudeMdExternalIncludesApproved`. Trust requires `hasTrustDialogAccepted is True` for the folder, its `--show-toplevel` or its main checkout (parent of `--git-common-dir`), never an ancestor, per the herdr-trust phase 1 survey (2026-10-13: 22 of 23 worktree folders covered by a main-checkout key on each account).
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
| G3 | Phase 9 | the clock | 7 days after Phase 24's live proof |
| G4 | Phase 24 | the user | the user is present for the live moves |

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

**Depends on:** Phase 24

#### Work Order

**Goal:** answer the user's question with a week of data: how often sessions and seats moved between accounts, why, and whether any time passed with no account having room.

**Constraints from prior phases:** from "Two account groups" on, unit directors and showrunners each sit on one stored account (`~/.local/state/accounts/groups.json`; each change is a `roles.jsonl` entry `{at, group, label, why}`), a new Claude session launches on its group's account, and a group moves whole on a refusal. Codex seats move on a refusal ("Codex seats move on a refusal") and Claude sessions move on a refusal ("Claude sessions move on a refusal"). Every automatic transition is one line of `~/.local/state/accounts/moves.jsonl`, keyed by a stable `move_id`: `requested`, `confirmed`, `failed`, `waited`, `wait ended`, `no room`, `room again`. `requested` and `waited` carry the reset when it is known (`reset` is omitted when unknown); `no room` carries `earliest_reset`. A Codex request has one `confirmed` per seat (`run`, `seat`), and one seat may fail beside others confirming; a Claude batch shares one shutdown `request_id` across several session subjects. A targeted Claude move, automatic or by hand (`move`, `move showrunners`), is journalled: its `requested` carries `cause` (`refusal` or `by hand`; a missing `cause` is `refusal`) and, for a refusal, the window and reset of the refusal that started it ("Proof refusals stay with their session"); reconcile it with shutdown history by request id and session id and count it once. Codex hand swaps and `set-role` are not journalled: take them from `roles.jsonl`. A five-hour wait ends `wait ended` and the session continues on its own account; the continuation is not journalled. A Claude move is also a `SessionShutdown` record with `target`, `source_label` and `why` in `~/.local/state/shutdown/<login>/sessions/`, moved to `history/` there once the session is confirmed restarted; a `confirmed` proves the move even when its record stays in `sessions/` with `timers pending`; `failed` with `manual restart`, `cancelled`, `restart failed` or `still on <source>`, and a standalone `failed` with no `requested` (refused before any record existed), are not completed moves; a session that failed and later confirmed on a new request counts once, as confirmed. A `no room` means no eligible destination for that session (its refusing logins excluded), not that every account was at zero. Role history `~/.local/state/accounts/roles.jsonl` still records each seats-home write (`role: seats`). `agent_notes.py` keeps readings (`~/.local/state/agent-notes/readings.jsonl`) for eight days, so the readout runs at T0 + 7 d, T0 being the live proof. `accounts.py burn` reads a `--since` or `--until` time with no zone in the user's zone, so give both with an explicit UTC offset.

**Spec:** for the window T0 to T0 + 7 d, read `moves.jsonl`, the shutdown records and history, `roles.jsonl` and `readings.jsonl`, and run `accounts.py burn --since <T0> --until <T0 + 7 d>`. Report to the showrunner:

- moves per tool (Claude session, Codex seats) and per cause (weekly refusal, 5-hour refusal, credits off, by hand), a group move counted once per group transition apart from its per-session requests and confirmed outcomes, counted both per transition (one per `requested`, or per hand move) and per seat or session confirmed; a request with some seats confirmed and some failed is partial; each is confirmed against its record, and an unconfirmed or failed restart is marked, never counted as a move;
- the waits for a 5-hour reset under 20 minutes away, and how long each lasted (`waited` to `wait ended`; an unclosed one is an unknown interval, not a duration);
- every interval with no account having room (`no room` to `room again`), with the earliest reset each named, and unknown intervals marked;
- points per account per day, and observed-day coverage with gaps.

Count transitions by (`tool`, `move_id`); a Claude group move is one transition under its group `move_id` ("Proof refusals stay with their session" item 7), however many requests its members took; Claude outcomes by session id and Codex confirmations by (`move_id`, `run`, `seat`); report standalone failures as refused attempts. Pair `no room` with `room again` by tool, `move_id` and session; report per-session intervals and their union separately, an unresolved end as unknown. Read with `moves.read_moves()` and shutdown's record readers (`scripts/shutdown/record.py`); status already shows sessions, seats and the last ten entries, so this phase aggregates and builds no surface.

Readings measure accounts, not groups: report each account's weekly burn while each group sat on it, attributing it to a group only for intervals that group sat alone there and marking shared intervals as shared.

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

### Phase 16 — Claude sessions move on a refusal  · status: done

#### As-built

- `refusal_watch.py` runs at the end of `agent_notes.py`'s argument-free run (the 2-minute `agent-sessions-snapshot` timer on natedev); a failure prints one stderr line and never fails the notes update. It reads this machine's live top-level sessions (`inventory.py`; seats and sessions with an open shutdown record skipped). A session is refused when its transcript's last entry is an `assistant` entry with `isApiErrorMessage: true` and `error: "rate_limit"`, or when `~/.local/state/accounts/assumed_refusals/<session id>.json` exists. `session limit` text is the 5-hour window, `weekly limit` the weekly one, other text takes `quota_alert._refusal_window`; the reset comes from the stored reading, the time from the entry's `timestamp`.
- A refusal is written to `refusals.py` once per (session id, entry `uuid`); the first observation (`FirstRefusalObservation`) stands in the watch state `~/.local/state/accounts/refusal_watch.json`, and a rescan writes nothing.
- Wait: a known 5-hour reset in the future and under 20 minutes away writes one `waited` per session per reset, then `wait ended`; the next tick on the same entry delivers one `Continue where you stopped.` on the session's own account (`settle.send_message`) and marks the entry `continued` in the watch state, never moving it. A failed delivery prints one line and retries next tick. `continued` is not a `moves.jsonl` kind.
- Move: `accounts.pick("claude", now, exclude=<every login refused this tick>)`, then one `accounts.move_sessions(ids, target, *, why)` per source login, running `shutdown.py down '<from>' --here --only <ids> --restart --to '<to>' --why '<from> refused: <window> limit until <reset>'`. It returns `SessionMoveRequested` (with `request_id`) or `SessionMoveRefused` per session; a refusal before any record exists (cold key, conductor failure, settle refusing) writes `failed` with that reason and no `requested`, retried next tick. `move_showrunners` uses it. `down` writes `requested` (keyed by `request_id`) after the records exist and withdraws them if the conductor fails to launch.
- No room: `no room`, one alert per change in reading values (`left`, `resets`, credit `balance`; never check times) to the configured sessions and running showrunners, then `room again`.
- `restart.py` writes `confirmed` only when the resumed process runs on the target (its config dir, as `account.py --pid` reads it); `timers pending` is still `confirmed`; a live session still on the source is `failed` `still on <source>`. `restart.mark_restart_failed` is the one path that sets a targeted record `restart failed`/`manual restart` and journals `failed`. On natedev only tmux, Ghostty and zed restart themselves; `terminal` and `unknown` hosts print the resume command and end `manual restart`.
- `settle.py`: the usage-limit hold also enters `held by usage limit` from a transcript or assumed refusal, on every host (stop rule unchanged); `/shutdown status` shows each moved record's target and why. `/shutdown cancel` calls `refusal_watch.cancelled` before withdrawing the record: `failed` reason `cancelled`, the assumed refusal deleted, and that refusal never moves the session again.
- `--dry-run` prints exactly what a real run would do and changes nothing; `--assume-refused '<session id | name>' weekly|five-hour [--reset-in <minutes>]` writes the assumed file and runs once, `--reset-in` feeding only the wait decision.
- `inventory._model` skips `<synthetic>`, so a moved session resumes on its last real reply's model. `quota_alert.blocked` takes `RefusedAt(at) | RefusalTimeUnknown()`, converted at the `agent_notes.py blocked` argument boundary.
- `/showrunner:accounts status` lists live sessions' accounts, the seats' account and their Codex destination, owed seat continuations (runs whose mesh server is running), and the last 10 `moves.jsonl` entries in the user's zone with each outcome.

**Files:**
- `scripts/accounts/refusal_watch.py`, `test_refusal_watch.py` — the watcher.
- `scripts/accounts/accounts.py` — `move_sessions`, status lines.
- `scripts/shutdown/settle.py` — the hold, cancel order, `down`'s journal and rollback; `restart.py` — move proof, failure journal, host restart paths; `inventory.py` — the real model.
- `scripts/whoami/quota_alert.py`, `agent_notes.py` — typed refusal time, watcher call.
- `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`, `commands/showrunner/accounts.md`.

**Binds later work:** "Live proof of the moves" expects a five-hour wait to end in `wait ended` plus one continuation on the unchanged account and no `requested`; no probe uses a bare terminal, which ends `manual restart`. "Readout of the moves" counts `failed` with `manual restart` or `cancelled` as not completed, `confirmed` with `timers pending` as completed, and reads a wait only from `waited`→`wait ended`.

**Gotchas:** an assumed refusal is also written to `refusals.py` as an account-wide refusal, and its file stays after a continuation, so a later shutdown hold still reads it; "Proof refusals stay with their session" fixes both. The transcript lookup is one level deep (`projects/*/<id>.jsonl`), read once per session per tick. Every test module reaching `agent_accounts.user_zone()` isolates `showrunners.NOTIFIER_STATE_DIR`. The Mac has no timer, so no watcher runs there.

### Phase 17 — An account prepared for any launcher  · status: done

#### As-built

- `accounts.prepared_account(label: str, *, resume: bool) -> PreparedAccount | AccountRefused` holds the account preparation for a launch on `label` without exec'ing. `unit` resolves through `role_account("unit")`, `default` to the default account, any other label to its named account.
- `PreparedAccount` (frozen dataclass): `variables: dict[str, str]` to set and `remove: frozenset[str]` to unset. It never carries a copy of the caller's environment; the caller applies it to its own base.
  - `remove` always holds `CLAUDE_CODE_OAUTH_TOKEN`.
  - A named account whose expected login equals the default directory's login (default-login alias): `account.NAMED_ACCOUNT_VARIABLES` are removed and nothing is set.
  - Any other named account: refused when not set up (`is not set up; /showrunner:accounts setup …`), when readiness is neither Ready nor LoggedOut, or when LoggedOut with `resume` (the readiness's first problem); otherwise `variables` is `account.named_account_environment(dir)`.
  - The default account: with `resume`, a problem from `check("default")` refuses with its first problem; the named-account variables are removed.
- `AccountRefused` (frozen dataclass): `reason: str`, the exact message `exec_account` raised before. A label resolution `AccountsError` (bad label, no room for `unit`) returns as `AccountRefused`, not an exception.
- `exec_account(label, arguments)` calls `prepared_account(label, resume="--resume" in arguments)`, raises `AccountsError(reason)` on a refusal, and otherwise applies the prepared account to a copy of `os.environ` and execs — same errors, messages and environment as before. `_drop_named_account` no longer exists; `PreparedAccount.remove` does its job.

**Files:**
- `scripts/accounts/accounts.py` — `PreparedAccount`, `AccountRefused`, `prepared_account`, `exec_account`.
- `scripts/accounts/test_accounts.py` — one `prepared_account` test per outcome beside the unchanged `exec_account` tests.

**Binds later work:** herdr-trial's `restart.py` calls `prepared_account(target, resume=True)` and sets `variables` through its pane environment (the herdr server runs under `systemd-run --user`, so no caller environment reaches it). The account groups phase checks a group move's target with `prepared_account` before moving. `prepared_account("unit", …)` resolves through `role_account`, so it follows the units group with no further edit; `prepared_account` and `role_account` stay stable for herdr-trial.

**Gotchas:** the default account removes every named-account variable as well as `CLAUDE_CODE_OAUTH_TOKEN`; a caller that skips `remove` leaks a named account into a default launch.

### Phase 18 — Two account groups: showrunners and units  · status: done

#### As-built

- Claude sessions form two groups, `ClaudeGroup = Literal["units", "showrunners"]` (`CLAUDE_GROUPS`); `session_group(session)` classes a live session by its inventory kind, and `live_group_members() -> dict[ClaudeGroup, list[Session]]` lists each group's local live members. A session in no group (`"ungrouped"`) moves alone.
- `~/.local/state/accounts/groups.json` (`groups_file()`) holds `GroupAccounts {units, showrunners}`. Only `set_group_account(group, label, *, why, now)` writes it, atomically under a lock, appending a `GroupAccountChange {at, group, label, why}` line to `roles.jsonl`; the role-history reader `burn` uses skips group lines. An absent key reads as `GroupNotConfigured` (prints `not configured`).
- `role_account("unit")` / `role_account("showrunner")` return the configured group account as a `ClaudeLaunchAccount`, never inferred from live sessions, readings or the launcher's account; an unconfigured group raises `AccountsError` naming `accounts.py move <group> "claude N"`. `account_for_group_launch(role, *, resume, target="", config_dir="")` wraps it for every launcher (`add_unit.py` new, standby and `--resume`; `add_showrunner.py`; `promote_showrunner.py`; nightly review; `exec unit`; restart's showrunner resume): it refuses an `--account`/`--config-dir` naming any other account and runs `prepared_account`.
- Room: an account has room unless it holds an unresolved refusal; readings only rank accounts with room, and 0% weekly with no refusal still has room. `placement(group, now, *, exclude) -> GroupPlacement {group, label, dir, reason, reasons}` picks units by soonest weekly reset (ties to most left, `UnitsResetSoonest`), showrunners by most weekly left (`ShowrunnersMostLeft`), or keeps the group in place with `GroupNoRoom`.
- `move_group(group, target, *, dry_run, why)` checks `prepared_account(target, resume=True)`, calls `set_group_account`, then calls `move_sessions` once for the members where `session_on_account(session, target)` is false. `accounts.py move units|showrunners <claude N>` runs it; `move <session>` refuses a grouped session, naming the group command.
- The refusal watcher moves a whole group, and only on a refusal of one of its live members: it checks the target with `prepared_account` (a refusal writes nothing and retries next tick), writes the group's account, then calls `move_sessions` for off-target members, recording the `group selected` watch state. Later ticks request any live member still off its group's account with no open shutdown record, so no session gets two requests. A group in a five-hour wait waits; an unconfigured or no-room group does not move.
- `accounts.py placement --explain`, `/showrunner:accounts status` (opens `units: <label>, showrunners: <label>`; role cell names the groups; an off-target member shows `moving`) and `quota_alert.py`'s forecast report per group.

**Files:**
- `scripts/accounts/accounts.py` — groups, `groups.json`, `role_account`, `account_for_group_launch`, `placement`, `move_group`, status.
- `scripts/accounts/refusal_watch.py` — group move on a refusal.
- `scripts/production/add_unit.py`, `add_showrunner.py`, `promote_showrunner.py`, `scripts/nightly_review/nightly_review.py` — launch on the configured group account.
- `scripts/shutdown/restart.py` — `launch_entry` catches `accounts.AccountsError` per entry.
- `scripts/whoami/quota_alert.py` — forecast per group.
- `docs/accounts.md`, `commands/showrunner/accounts.md`, `produce.md`, `add_showrunner.md` — the two groups and the group move.

**Binds later work:** `move_group(group, target, *, dry_run, why)` is the one group move and calls `move_sessions` once; `live_group_members()`, `session_group`, `session_on_account` and `set_group_account` are the group primitives; the watcher writes the group account before `move_sessions` and keeps the `group selected` state; `placement(group, now, *, exclude) -> GroupPlacement`; `role_account("unit")` / `role_account("showrunner")` names and return types are stable, and herdr adoption resumes through them.

**Gotchas:** every launch reads the configured group account, so an unconfigured group refuses until `accounts.py move units|showrunners "claude N"` runs (natedev's `groups.json` starts unconfigured). `restart.py` `launch_entry` must keep catching `AccountsError` per entry, or one unconfigured group aborts every other restart.

**Ruled out:** inferring a launch account from live sessions, readings or the launcher's own account; moving a healthy group because another account reads better; a weekly-reading floor on room.

### Phase 19 — Group moves pause first  · status: done

#### As-built

- `move_group(group, target, *, dry_run=False, why="") -> GroupMoveStarted | GroupMoveStartFailed | GroupMoveDryRun | MoveRefused` is the one group move, by hand and from `refusal_watch.py`; `move_showrunners` is gone. It sets the group's account (`set_group_account`), then calls `move_sessions` once for the off-target live members.
- `accounts.py move units|showrunners <target> --dry-run` prints `<group>: would select <target>`, then one `<name>: <session id>` line per off-target live member, or `<group>: no live session moves`. It writes nothing: no `groups.json`, `roles.jsonl`, `moves.jsonl`, shutdown record or stop request.
- The showrunners move and its preview print one line per `inventory.unregistered_showrunners()` entry, naming `update_registration.py register --production <doc>`. With no live showrunner the move still sets the account and exits 0.
- `move_sessions` requests `shutdown.py down --here --only <ids> --restart --to <target> --why <why>` without `now`, so `settle.py` sends each member `Pause for move: …`, stops it after its `ready --where` mark, and tells a unit's showrunner. No group-move code messages, waits or stops a session itself.
- `refusal_watch.py --dry-run` updates its in-memory group selection when it picks a destination, so a partially moved group's preview names the destination a real run uses.
- `restart.py`: `_resume_parts` resumes units and showrunners on their group's configured account; `restart_note` adds `Before it you wrote: <where>` to an account-move note when the record's `where` is `said`. Herdr-hosted units, and herdr showrunners whose resume is a `MoveResumeAccount`, return `ManualRestartRequired` carrying an `accounts.py exec` resume command.
- `recorded_account_directory` (add_showrunner, promote_showrunner) and `RetainedClaudeAccount`/`RetainedUnitAccount` (add_unit) are removed.

**Files:**
- `scripts/accounts/accounts.py` — `move_group`, `move_sessions`, preview and unregistered lines, `GroupMove*` outcomes.
- `scripts/accounts/refusal_watch.py` — dry-run group selection; watcher moves go through `move_sessions`.
- `scripts/shutdown/restart.py` — group account on resume, `where` in the move note, herdr manual path.
- `scripts/production/add_unit.py`, `add_showrunner.py`, `promote_showrunner.py` — unused account helpers removed.
- `scripts/whoami/test_quota_alert.py` — patches `move_group`.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the preview and the pause before a move.

**Binds later work:** enh-showrunner's `/showrunner:move` adapts to `move_group` (no `move_showrunners`, no `move showrunners --from/--to`). "Proof refusals stay with their session" adds one Claude account resolver every launcher calls. "Live proof of the moves" needs an isolated groups configuration for its scratch group.

**Gotchas:**
- The herdr `ManualRestartRequired` path is temporary: herdr-trial's later work replaces it with automatic restart on the group account and removes the exec line; build nothing on it.
- The pause-for-move protocol lives in `scripts/shutdown/settle.py` and is not owned here.

**Ruled out:** a PATH launcher and an automatic herdr unit relaunch inside restart.py, superseded by herdr-trial's restart.

### Phase 20 — Codex credits setting and the 0% alert  · status: done

**Depends on:** Phase 19 (numbering only)

#### Work Order

**Goal:** each Codex account carries its own `use_credits: on|off` setting; when the workers' Codex account reaches 0% weekly, one alert goes to the phone and the running showrunners with the same text, the workers either stay on credits (with an hourly credits-left forecast) or move to the Codex account with room that resets soonest; every Codex account is named by its number, never `default`.

**Spec:**

1. **The setting lives in the account's note.** Each Codex account is defined by its note `~/rust/hanadocs/agents/codex N.md` (`login:` names the account; `agent_accounts.codex_account(home)` maps a home's login to that note's stem). A new frontmatter field `use_credits: on` or `use_credits: off`; absent reads as `on` (today's behavior). Any other value reads as `on` and the alert pass prints one log line `codex N: use_credits must be on or off; reading it as on`. One reader, `agent_accounts.codex_credits_setting(label: str) -> CreditsOn | CreditsOff` (frozen dataclasses; `CodexCreditsSetting = CreditsOn | CreditsOff`), reads the note through `note_file.read_note`. `agent_notes.apply` rewrites only named keys (`set_fields` / `_rewrite`), so the updater keeps the field. The setting that counts at 0% is the one on the account the seats are on at that pass (`CodexAlertPassReadings.selected_account`).
2. **The 0% episode.** In `quota_alert._alert_for_saved_readings`, the Codex branch opens a zero episode when `selected_codex_report(codex_readings, now)` has `weekly_remaining == 0` and `weekly_reset > now` for a `KnownCodexAccount`. State: a new `State`/`StoredState` key `codex_zero: dict[str, CodexZeroEpisode]` keyed by the note label, in the existing `~/.local/state/quota_alert.json` under `state_file()`'s flock. `CodexZeroEpisode` (TypedDict): `since`, `weekly_reset`, `credits: Literal["on", "off"]` (the setting read when it opened), reread on every alert pass (each 2-minute snapshot), so the move follows within minutes: an episode opened `on` whose account now reads `off` runs the credits-off path once (`pick` + `swap`, a `requested` entry with cause `credits off`), sends no later forecast and never repeats the opening alert, while `off` to `on` mid-episode changes nothing, `phone: "due" | <ISO sent>`, `sessions: <ISO stamped>`, `forecast_at: <ISO of the last hourly forecast, or since>`, `forecast_phone: NotRequired["due" | <ISO sent>]`. It closes, silently, when `weekly_reset <= now`, or a fresh reading of that account shows weekly above 0, or the seats are no longer on that account. When the zero episode opens, that account's Codex low-threshold episode (`episodes[...]`, fed by `_low_codex_notes`) closes silently and does not reopen while the zero episode is open, so at 0% no Codex alert repeats every `repeat_minutes`, none asks for `/quota_ack`, and no `Quota restored:` relay follows. The low-threshold alert above 0% and every Claude episode are unchanged.
3. **Exactly one alert.** Opening the episode renders one catalog push (item 6) and sends: the phone through `tell_user(rendered)` (`push_texts.user_send_argv`), and the running showrunners through `deliver_and_record` with relay text `f"{rendered.title}\n{rendered.body}"`, recipients `recipients(config)` (config `always` plus each running showrunner). `sessions` is stamped under the lock before the relays start (a run the timer kills after the stamp sends no second copy). `phone` stays `"due"` until `tell_user` returns `PushSent`; each later 2-minute run retries only the phone while it is due, and never re-relays.
4. **Credits on.** The workers stay. The alert names the account, its refill time, its credits and where workers go when the credits run out: `accounts.pick("codex", now, exclude=frozenset({seats label}), keep=accounts.NoKeep())` → `PickedAccount.label`, or, for `NoAccountWithRoom`, that Codex work moves to Claude until the earliest reset (`reset_display`). Example: `Codex account codex 1 has used its weekly usage; it refills Mon 2026-10-20 00:58 EDT. Workers keep working on its 6,370 credits. When codex 1's credits run out, workers move to codex 2.`
5. **Credits off.** The workers move. `accounts.pick("codex", now, exclude=frozenset({seats label}), keep=accounts.NoKeep())` (its order is soonest weekly reset, then most weekly left, then label; for a credits-off move an account with weekly room comes before a credits-on account at 0%, which is taken only when none has room, and the alert then says that account's paid credits are spent), then one `moves.RequestedMove` (`tool: codex`, `subject: seats`, `why: "<label> weekly usage used; credits off"`, `window: weekly`, `reset`) and `accounts.swap("seats", chosen.label)`, exactly as `quota_alert.blocked` does at ~1250–1268: the swap retires each run's server and working seats finish their turn on the old home first. A failed swap appends `moves.FailedMove`; `NoAccountWithRoom` appends one `moves.NoRoomMove` (same dedup as `blocked` ~1270–1276). In both cases that same pass calls `switch_to_claude`, the path a provider refusal takes, so no paid credits are spent on a credits-off account: the workers never stay on it, and the alert says Codex work moves to Claude until the earliest reset (with the swap failure's reason when there is one). Refusal handling (`blocked`, `switch_to_claude`, `switch_back`) is otherwise unchanged. The `requested` entry carries a new optional `cause: "credits off"` (`RequestedMove.cause: NotRequired[Literal["credits off"]]`; a reader treats a missing `cause` as `refusal`), so the readout never counts this move as a refusal.
6. **Credits count only where they are on.** `accounts._codex_reading_can_work` (~1242) and `agent_accounts.codex_can_work` gain the account's setting: with `CreditsOff`, credits never make room, so `pick` skips a credits-off candidate whose weekly or 5-hour window is at 0, and `_swap_seats` refuses it with `accounts: <label> (<folder>) is used up and set not to use credits; choose another account or wait for reset`. `codex_can_work(report, now, credits: CodexCreditsSetting)`; every caller passes the setting of the report's account.
7. **The hourly forecast.** While a zero episode with `credits: on` is open and the seats are still on that account, each run where `now - forecast_at >= 1 h` sends a forecast to `recipients(config)` and sets `forecast_at = now`. The first forecast of the episode also goes to the phone (`forecast_phone` `"due"` until `PushSent`, retried each run while due); later forecasts never do. Text (catalog `quota.codex_credits_forecast`): `In the last hour Codex workers used <used> credits on codex 1; its <balance> credits last until about <time>. When they run out, workers move to codex 2.` With zero use: `Codex workers used no credits on codex 1 in the last hour; <balance> credits left.` With `unlimited`: `codex 1 has unlimited credits.` With balance `null`: `codex 1 reports no credit balance; the forecast is unknown.`
8. **Forecast inputs.** Balance now: the pass's `selected_codex_report(...).credit_balance` (`agent_accounts.codex_credits`: whole credits as text, `unlimited`, or `null`). Use in the last hour: `~/.local/state/agent-notes/readings.jsonl` (`run_out.READINGS_LOG`), whose `StoredReading {account, at, remaining, credits?}` lines `agent_notes.apply` (~227–241) appends for the matching Codex note whenever a note field changes. `credits_used(readings, label, since=now - 1 h, until=now) -> CreditsUsed | NoCreditReading`: start from the last reading at or before `since` (else the first in the hour), sum each decrease between consecutive `credits` values, skip each increase (a purchase), and return `NoCreditReading` when no line in or before the hour carries `credits`. Run-out time = `now + balance / used * 1 h`, shown in the user's zone (`scripts.lib.user_zone.user_zone()`). One reader of the log for credits: add `read_credit_readings(path) -> dict[str, list[CreditReading]]` beside `run_out.read_readings`, not a second JSON parser. `CreditsUsed` carries `used` and the observed interval (`observed_from`, `observed_to`); the rate is `used / (observed_to - observed_from)`. When the observations cover under 30 minutes of the hour, or the newest is older than `CODEX_READING_MAX_AGE`, it returns `UsageUnknown(reason)` and the forecast says the use is unknown this hour.
9. **Push texts.** Every new user-facing text is a `PushText` in `scripts/notify/push_catalog.py` with `sender="quota_alert"`, need `note`, action `NoActionNeeded()` (except `quota.codex_on_credits` and `quota.codex_on_credits_last`, whose `ActionTemplate` says how to stop paid credit use: `use_credits: off` in the account's note on the alert's machine, the move following within minutes), and two or more `PushExample`s with realistic values (codex 1 / codex 2, credits 6370 and 2114 from the notes on 2026-10-14). Ids: `quota.codex_on_credits` (item 4, a next account), `quota.codex_on_credits_last` (item 4, no other account), `quota.codex_workers_moved` (item 5, moved), `quota.codex_workers_stay` (item 5, no room or failed swap; value `reason`), `quota.codex_credits_forecast` (item 7; the zero-use, unlimited and unknown forms are their own ids or examples, writer's choice). Titles under 45 characters; `"No action needed.\n" + body` under 500 (`test_every_catalog_example_fits_the_phone_title_and_body_limits`). Senders of these pushes use `tell_user(push_texts.render(id, **values))`; `UserPush.ask` asserts `ActionRequired`, so these go through a new `QuotaNote(name, rendered)` NamedTuple, not `UserPush`. The session-only lines in `blocked` (`seats moved from …`, `seats wait for …`, `Codex refused work on …`) say `workers` and the account number.
10. **The approval gate, from the code.** `test_push_texts.test_every_catalog_example_has_current_approval` fails unless `push_texts_reviewed.json[id][example]["sha256"]` equals `push_texts.fingerprint(entry)` (a hash over sender, need, title, action, body and every example's values and filled text) for every example. To get a text approved, the seat runs from the worktree root `~/.claude/scripts/lib/py scripts/notify/review.py texts --id <id> --approve` (one `--id` per new entry). `review.texts` renders each example, runs the live reviewer (`_review`, `reviewer_prompt.md`), and records an approval only when the outcome is `Approved` with the draft unchanged; a `rewrite`, `ask` or `withhold` verdict records nothing and exits 1. On `rewrite`, copy the reviewer's printed title/body into the catalog entry and run again; any later edit to a template or an example value invalidates the approval and needs the same run. The reviewer withholds a push that "asks nothing of the user and tells nothing they must know now"; each body states the fact the user must know now (workers on paid credits, where they go, when credits end). A `withhold` that stays after one rewrite stops the seat and goes to the unit director: the user asked for these on the phone.
11. **Workers, numbers, no `default`.** `codex_swap_line` (~145) says `workers move to {chosen.label} when Codex refuses work`, or `no other Codex account has room until <reset>`. `agent_accounts.codex_home_role_label(home)` (~89) returns the note label of the home's login (`codex_account(home)`), and the home's folder (`codex_home_display`, e.g. `~/.codex`) when its login has no note; never `default`. `accounts.role_account("seats")` (~1115) and `codex_home_label` (~2231) take their label from it. Today on natedev `~/.codex` reads `codex 2` everywhere. A test renders every Codex alert text (each new catalog example, `codex_swap_line`, the relays from items 3–7, the `blocked` session lines) and asserts none contains `default` and none contains `seats`.
12. **`codex N` names an account, not a folder.** `agent_accounts.codex_account_home(label) -> CodexHomeHoldingLogin | CodexSetupHome`: the home among `codex_homes(...)` whose on-disk login (`codex_email_on_disk`) matches the `codex N` note's login (`CodexHomeHoldingLogin(label, home, login)`), else `~/.codexN` (`CodexSetupHome(label, home)`: no home holds the account yet; setup puts it there). `agent_accounts.seats_home()` (~398–413) resolves a stored `codex N` through it. `accounts.check_codex_target`, `_check_codex_home`, `check`, `codex_statuses`, `_write_role` and `_read_accounts_configuration` resolve through it; `setup "codex N"` keeps creating `~/.codexN` (rename `accounts.codex_account_home` to `codex_setup_home`). The role file stores the account label. `swap seats "codex 2"` and `set-role seats "codex 2"` then select `~/.codex` on natedev; today they fail with `~/.codex2 is missing`. `default` stays accepted as input for `~/.codex` until "Accounts named by number only".

**Files:**

- `scripts/whoami/agent_accounts.py` — `CreditsOn`, `CreditsOff`, `codex_credits_setting`, `codex_can_work(..., credits)`, `codex_home_role_label` by note, `codex_account_home`, `seats_home` by login.
- `scripts/whoami/quota_alert.py` — the zero episode, the one alert, credits on/off, the hourly forecast, `QuotaNote`, the Codex low-threshold episode closed at 0%, `codex_swap_line` and `blocked` lines in workers wording.
- `scripts/whoami/run_out.py` — `read_credit_readings`, `credits_used`.
- `scripts/accounts/accounts.py` — pick and `_swap_seats` honor the setting; `codex N` resolved by login; `codex_setup_home`.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — the new entries and their approvals.
- `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py`, `scripts/whoami/test_run_out.py`, `scripts/whoami/test_codex_homes.py`, `scripts/accounts/test_accounts.py` — tests below.
- `docs/quota_alerts.md`, `docs/accounts.md`, `commands/showrunner/accounts.md` — the setting, the 0% alert, the forecast, `codex N` by login.

**Seats:** 1 writer + 1 tester.
- `impl` — every production file and doc above, and the catalog entries with their approvals (it runs `review.py texts --id … --approve`); hub: `scripts/whoami/quota_alert.py` (the zero episode state and every alert text path).
- `test` — `test_quota_alert.py`: at 0% with credits on, one phone push and one relay per recipient on the first run, none on the next 15 runs; a failed phone push retries the phone only; the alert names the `pick` destination or the Claude line; at 0% with credits off, one `requested` move, `accounts.swap("seats", <soonest-reset label>)` called once, the alert names the target; no room writes one `no room` and the stay text; the setting of the seats' account counts, not another account's; absent and invalid `use_credits` read as on; at 0% no Codex alert repeats after 30 minutes and none mentions `/quota_ack`, while a Codex account at 1% still gets today's low-threshold alert; the forecast fires at 60 minutes, not at 58, once per hour, and only the first goes to the phone; the episode closes at the weekly reset and on a swap away with nothing sent; an open low-threshold episode entering 0% closes with no restored or repeat message; a failed credits-off swap writes one `failed` and the stay text with its reason; the first credits-on alert with an `unlimited` or `null` balance names that balance as it reads; `credits_used` with stale history and with a partial hour returns `UsageUnknown`. `test_run_out.py`: `credits_used` over a purchase mid-hour, zero use, no credit line. `test_codex_alert_accounts.py`: every Codex alert text has no `default` and no `seats`; `~/.codex` labelled by its note number. `test_accounts.py`: `swap seats "codex 2" --dry-run` and `set-role seats "codex 2"` select a temp `~/.codex` whose login is the `codex 2` note's; `"codex 1"` still selects `~/.codex1`; a stored `codex 2` resolves `seats_home()` to `~/.codex`; pick skips a credits-off candidate at 0% with credits; `_swap_seats` refuses it. `test_codex_homes.py`: `codex_home_role_label` for a noted and an unnoted `~/.codex`. `test_push_texts.py` stays green with the new entries.

**Constraints from prior phases:** The alert pass runs at the end of `agent_notes.py`'s argument-free run (`agent_notes.main` ~374–382: `update_alert_pass` then `quota_alert.alert(notes, selection=…, codex_readings=updated.codex_readings)`), started every 2 minutes by natedev's `agent-sessions-snapshot` timer; the Mac has no timer. `refresh` (`/quota_refresh`) calls `_alert_for_saved_readings` too, so the zero episode logic lives there once. One frozen `CodexAlertPassReadings(seats, selected_account, checked_homes)` serves the run; a reading older than `CODEX_READING_MAX_AGE` (10 min) gives `selected_codex_report` a problem report, which opens nothing. `state_file()` holds the lock only around reads and writes, never across a relay (`deliver_and_record` stamps first). `recipients(config)` = `config["always"]` plus running showrunners (`showrunners_now`). `tell_user` returns `PushSent | PushFailed`. `accounts.py` never imports `quota_alert`/`codex_mesh` at module level (import cycle); `quota_alert` imports `accounts` inside functions. `agent_accounts` is imported by name, never by path (its classes register under both names). Every test reaching `agent_accounts.user_zone()` points `showrunners.NOTIFIER_STATE_DIR` at an empty dir. Tests set `HOME` and `AGENT_NOTES_DIR` to temp dirs and never touch `~/.codex*` or the vault. `seats_home()` reads `~/.claude/config/accounts.json` (`{}` on natedev on 2026-10-14, so `~/.codex`). On 2026-10-14 `codex 1.md` (login slider…, `~/.codex1`) reads weekly 0, credits 6370, inactive; `codex 2.md` (login maxis…, `~/.codex`) reads 97, credits 2114, active. Credits come only as whole numbers (`codex_credits` formats `{:.0f}`), so use under 1 credit an hour reads as 0. `agent_notes.apply` (~219–240) also rewrites `weekly_usage_checked_at` on every fresh observation, so `readings.jsonl` gets a line per fresh observation even with an unchanged balance; an hour with no lines means no observation, not zero use. No `swap`, `set-role` or alert send runs against the real home in tests.

**Prerequisite:** the live text reviewer runs on the units group's account (`review.py` ~561 calls `role_account("unit")`), so `review.py texts --approve` needs `groups.json` configured on natedev. The showrunner configures the groups after promoting "Group moves pause first"; before dispatch, check `accounts.py role unit` prints an account, and ask the showrunner if it does not. Seats never run `accounts.py move`.

**Acceptance gate:** the whoami, accounts, notify and lib suites (`python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'`) and `basedpyright` on the touched files pass, and pass again under the real home; `test_push_texts` green with every new id approved; on natedev `accounts.py swap seats "codex 2" --dry-run` prints `Would move seats to codex 2 (~/.codex)` or `already serves seats`, and `accounts.py role seats` prints `codex 2`.

### Phase 21 — Account notes by number, with role and sessions  · status: todo

**Depends on:** Phase 20

#### Work Order

**Goal:** the account notes are named `claude1.md` … `codex2.md`, matching the `claude1` command line, and move to those names on the first updater pass after promotion; the hand-free `state` field is gone; each note carries `role` (the roles its account serves now) and `sessions` (its live session count), and its body holds a table of those sessions under a heading the script owns. Labels stay `claude 1` / `codex 2` everywhere; only the file names lose the space.

**Spec:**

1. **One mapping between a label and a file, in `scripts/whoami/note_file.py`** (the standard-library leaf every note reader already imports):
   - `ACCOUNT_STEM = re.compile(r"(claude|codex) ?([1-9][0-9]*)")`.
   - `label_from_stem(stem: str) -> str | None`: `"codex2"` and the old `"codex 2"` both give `"codex 2"`; any other stem gives `None`.
   - `note_name(label: str) -> str`: `"codex 2"` → `"codex2.md"`; raises `ValueError` for a string that is not `claude N` / `codex N`.
   - `notes_dir() -> Path`: `AGENT_NOTES_DIR` when set, else the module's `AGENTS_DIR` read at call time (tests that patch `AGENTS_DIR` keep working). Every `Path(os.environ.get("AGENT_NOTES_DIR", str(AGENTS_DIR)))` copy calls it instead (`agent_accounts.py:79, 106, 136`, `account.py:54, 84`), and `accounts._account_notes_directory` (`accounts.py:772–773`) returns `note_file.notes_dir()` (both resolve `Path.home()` per call).
   - `note_path(label: str) -> Path`: `notes_dir() / note_name(label)`; the old `<tool> <n>.md` only when it alone exists (a vault not yet migrated, such as the Mac's copy before its next pull or update pass). Readers never build a note path any other way.
   - `Note.label` (property, `label_from_stem(self.path.stem)`); `read_note` sets `tool` from the label (`label.split(" ")[0]`) instead of `path.stem.split(" ")[0].lower()` (`note_file.py:59`, which gives `codex2` for the new name and breaks `note.tool == "codex"` in `codex_pacer.accounts` ~352, `agent_notes.apply` ~193, `quota_alert`), and returns `None` for a file whose stem is not an account stem.
   - `read_notes()` reads `notes_dir()`, one note per label: when both names exist, the numbered one.
   - `Note.get_list(key) -> tuple[str, ...]`: a block list (`key:` then `  - item` lines, as Obsidian writes `return:` today), a flow list (`[a, b]`) or `[]`; a scalar reads as one item, an absent key as `()`.
   - `in_use(note: FieldReader) -> bool`: `note.get_list("role") != ()` or `sessions` parses to an int above 0. `FieldReader` is a `Protocol` with `get` and `get_list`.

2. **Every file-name user goes through the mapping** (file:line read 2026-10-14; re-read each before editing):

   | Site | Today | Becomes |
   | --- | --- | --- |
   | `scripts/whoami/note_file.py:10, 59, 62–63` | `AGENTS_DIR` only, tool from the stem's first word, every `*.md` | `notes_dir()`, tool from `label_from_stem`, account stems only, one note per label |
   | `scripts/whoami/account.py:51–62` `label_for` | globs `*.md`, stem starts with the tool, returns `path.stem` | `read_notes()`, matches `note.tool` and login, returns `note.label` |
   | `account.py:82–93` `named_claude_account` | stem equals the text | `label_from_stem`-style match on `note.label`; returns `note.label` |
   | `scripts/whoami/agent_accounts.py:73–85` `codex_account` | glob, returns `path.stem` | `read_notes()`, returns `note.label` |
   | `agent_accounts.py:103–113` `codex_credits_setting` (Phase 20) | `directory / f"{label}.md"` | `read_note(note_path(label))` |
   | `agent_accounts.py:130–145` `codex_account_home` (Phase 20) | `directory / f"{label}.md"` | `read_note(note_path(label))` |
   | `scripts/whoami/agent_notes.py:229` | readings `account: note.path.stem` | `note.label` (keeps `readings.jsonl` keyed `codex 2`; 8 days of history stay continuous) |
   | `agent_notes.py:287` | `no agent notes in {note_file.AGENTS_DIR}` | `notes_dir()` |
   | `agent_notes.py:290` | `usage_store.save` logins keyed by stem | `note.label` |
   | `scripts/whoami/quota_alert.py:605, 699, 700, 718, 720, 725, 804, 806, 828, 848, 870, 883, 885, 900, 929, 948, 978, 1305, 1350, 1608` | `note.path.stem` as the label | `note.label` (episode keys in `quota_alert.json` stay `codex 1`) |
   | `quota_alert.py:454–458` `AgentNote` | `path`, `tool`, `get` | adds `label: str` and `get_list` |
   | `quota_alert.py:1376–1382` `RefusingAccountNote` | no label | `label` field and `get_list` returning `()` |
   | `quota_alert.py:1131` (Phase 20) | `values["file"] = f"{label}.md"` | `note_file.note_name(label)` |
   | `quota_alert.py:1464` | `RefusingAccountNote(Path(name + ".md"), {})` | `RefusingAccountNote(note_path(name), {}, name)` |
   | `quota_alert.py:965` `Read from {note.path}` | prints the path | no edit; it prints the new name once the note moves |
   | `scripts/whoami/codex_pacer.py:349–353` | `note.tool == "codex"` | no edit; correct once `read_note` sets the tool from the label |
   | `scripts/accounts/accounts.py:776–790` `_account_note` | `f"{label}.md"`, `Note(path, label.partition(" ")[0], …)` | `note_path(label)`; the missing-note line names that path |
   | `accounts.py:2382–2387` `_noted_labels` | glob `claude *.md`, stems | `note.label` for each `read_notes()` note with tool `claude` |
   | `accounts.py:2449–2452` `codex_statuses` noted labels | glob `codex *.md`, stems | same, tool `codex` |
   | `scripts/production/dailies_render.py:126, 1413–1428, 1489–1500` | own `AGENTS_DIR`, own frontmatter parser, `state == "active"`, readings by stem | `note_file.read_notes()`, `note_file.in_use`, `readings.get(note.label)`; `AGENTS_DIR` and `agent_fields` go (the whoami dir is already on its path for `run_out`); the empty line stays `- none active` (the footer hook and its tests match it) |
   | `scripts/notify/push_catalog.py:62–65, 72–73` (Phase 20) | examples `"file": "codex 1.md"` / `"codex 2.md"` | `"codex1.md"` / `"codex2.md"` |
   | `docs/accounts.md:166` | `~/rust/hanadocs/agents/codex N.md` | `codexN.md` (`codex1.md` for codex 1) |
   | `docs/quota_alerts.md:44` | `Each codex N.md account note` | `codexN.md`; line 3 adds the `role`/`sessions` fields and the table |
   | `docs/as-built/shutdown-and-restart.md:42` | label is the note stem | label is read from the note name (`claude2.md` → `claude 2`) |
   | `docs/as-built/build-memory-admission.md:190`, `docs/as-built/build-followups-fn-length-hook.md:55` | `state: active` notes, by file stem | notes in use (`role` or `sessions`), by label |
   | `commands/quota_refresh.md:5` | "each active account" | "each account in use" |

   Not file names, unchanged: `scripts/shutdown/inventory.py` (`label_for`), `restart.py:1993` and `shutdown.py:133` (`named_claude_account`), `scripts/message/escalate.py:471` (`codex_account`), `whoami.py:46` (`label_for`), `account.write_label` (the label text). `AGENT_NOTES_DIR` stays the one test and relocation variable.

3. **The notes move when the new code first runs.** `agent_notes.rename_legacy_notes(directory: Path) -> list[str]` runs first in `update_alert_pass` (before `note_file.read_notes()`, `agent_notes.py:280`), so the timer and `refresh` both move them and `blocked` reads through `note_path`'s fallback. For each `*.md` whose stem matches `(claude|codex) ([1-9][0-9]*)` (with the space): when `<tool><n>.md` is absent, `os.link(old, new)` then `os.unlink(old)` (same inode: body and mode kept; `link` fails with `FileExistsError` instead of replacing a file that appeared since the check), log `agents: renamed codex 2.md to codex2.md`; when both exist, change nothing and log `agents: codex 2.md and codex2.md both exist; reading codex2.md` on every pass; any other `OSError` logs `agents: could not rename codex 2.md: <error>` and the pass goes on. Nobody moves a live note by hand.

4. **`state` goes.** Writers: `agent_notes.apply` (`agent_notes.py:198–201`) sets `active`/`inactive` from `AccountActivity` (`ObservedAccountActivity`, `RoleAccountActivity`, `UnknownAccountActivity`, `OBSERVED_ACCOUNT_ACTIVITY`, `agent_notes.py:55–73`), fed at `agent_notes.py:296–303`: Claude `active` = the login is `~/.claude`'s or a local live session's (`ClaudeAccountsInUse.is_in_use`, `agent_accounts.py:194–195`); Codex `active` = the workers' account (`codex_readings.selected_account`), kept as is when that account is unknown. The activity types, the `activity` parameter and lines 296–303 are removed; the update pass deletes an existing `state:` line once (item 6). Readers and their replacement:

   | Reader | Decides | Becomes |
   | --- | --- | --- |
   | `agent_notes.py:331` `mark_tier` | which Codex note shows the launch tier | `"worker" in note.get_list("role")` |
   | `quota_alert.py:590` `low` | which notes open a low-quota episode | `in_use(note)` |
   | `quota_alert.py:597` `usable` | which notes count as places with room | `in_use(note)` |
   | `quota_alert.py:634` `recovered` | a single-account Claude note not in use closes its episode | `note is None or not in_use(note)` |
   | `quota_alert.py:879` `run_out_push` candidates | a Claude note the updater manages | `candidate.get("sessions") is not None` |
   | `quota_alert.py:948` notice line | `<label>: <state>, …` | `<label>: role <showrunner, unit or none>, <n> live sessions, …` |
   | `quota_alert.py:1609` `refresh` lines | the accounts listed after a refresh | `in_use(note)` |
   | `dailies_render.py:1491` | the Agents section's accounts | `in_use(note)` |

   Calls (one line each): an account becomes in use by holding a role even with no session, which keeps the next launch's account in the alerts and the Agents section; `~/.claude`'s account is in use only through a role or a session (it no longer counts by being `~/.claude`, which "Accounts named by number only" removes as an account name anyway); a Mac session counts as soon as the roster sees it.

5. **`role`: what the account is configured to serve now**, a block list in the fixed order showrunner, unit, worker, or `role: []`. The names `role` and `sessions` are fixed by hanadocs (its `agents.base`); do not rename them.
   - Claude: `accounts.group_accounts()` (`accounts.py:1577`) and `accounts.group_label(groups, group)` (`accounts.py:1527`). Add `accounts.group_note_labels() -> dict[ClaudeGroup, str]`: each configured group's label; a stored `default` maps to the label of `account_identity(main_config_dir())` when that is a `KnownClaudeAccount`, and the group is left out otherwise. `units` gives `unit`, `showrunners` gives `showrunner`; one account can carry both. "Accounts named by number only" removes the `default` branch.
   - Codex: `worker` on the note whose label is `codex_readings.selected_account.label` when that is a `KnownCodexAccount` (the same input `state` used, `agent_notes.py:300–303`).
   - When `groups.json` cannot be read (`AccountsError`), every Claude note keeps its `role` line; when the workers' account is an `UnknownCodexAccount`, every Codex note keeps its `role` line. A pass never empties a role it could not read. Claude workers (the roster's Claude `worker` rows) appear in the table but give no Claude account the `worker` role; `worker` names the Codex account the workers run on.

6. **One writer, only on change.** `agent_notes.set_fields` (`agent_notes.py:84–102`) and `_rewrite` (`:105–120`) grow into `write_note(note, updates: dict[str, str | tuple[str, ...]], *, remove: frozenset[str] = frozenset(), section: SessionsSection | KeepSections = KEEP_SECTIONS, stamp) -> bool`; `set_fields` stays a thin call of it for the existing callers. A tuple value renders as `key:` plus `  - item` lines, or `key: []` when empty. Replacing or removing a key also removes its continuation lines (lines that start with whitespace or `- `), so a `role:` block Obsidian rewrote leaves no orphan items; an inserted key still lands in sorted order and never splits another key's block (`return:` and its `  - "[[agents.base]]"` item stay together). Frontmatter and body are rebuilt in memory and the file is written once, with the note's mode and `os.replace` as now, and only when the text differs; `date_modified` is stamped only then. The 2-minute pass leaves a note untouched when no session started, stopped or moved.

7. **The sessions table.** In each account note's body, under the heading `## Live sessions`, which the script owns:

   ```
   ## Live sessions

   | Session | Role | Machine | Folder | Session id |
   | --- | --- | --- | --- | --- |
   | two-accounts | unit | natedev | ~/worktrees/claude-build-followups-two-accounts | 8f90aab3-fd40-47ea-8d7a-2858d9c26d6e |
   | impl | worker | natedev | ~/worktrees/claude-build-followups-two-accounts | 019a1b2c-… |
   ```

   - Columns hold only values that change when a session starts, stops or moves (showrunner, binding): the roster name, the role (`showrunner`, `unit`, `worker` or `freestanding`), the machine, the folder (the session's cwd with a leading `/home/<user>` or `/Users/<user>` shown as `~`) and the id (Claude session id, Codex thread id, `—` for a row with neither). Never ages, token counts, statuses or activity times. `|` in a cell is written `\|`.
   - Rows sort by machine, role in the order above, name, id, so the same sessions always render the same text.
   - No rows: the line `No live sessions.` under the heading.
   - The section runs from the heading to the next `# ` or `## ` heading or the end of the file and is replaced in place; when absent it is appended after the body with one blank line before it. Nothing else in the body changes.
   - `sessions: <n>` is the number of rows.
   - New module `scripts/whoami/session_table.py`: `SessionRow(NamedTuple)` (`name`, `role: SessionRole`, `machine`, `folder`, `session_id`), `render_section(rows) -> str`, `section_rows(body) -> tuple[SessionRow, ...]` (parses the owned table; an unparsable row is dropped), `replace_section(body, section) -> str`.

8. **Where the rows come from: the session roster**, which already lists every live session on both machines with its role, cwd and id (`scripts/message/roster.py`, as-built `docs/as-built/session-roster.md`): Claude rows from the session registry, Codex workers from each live run's `mesh_roster.json` (live = not `ended` and its server pid or `launcher_pid` alive), blind reviewers, Codex daemon threads and herdr Codex panes. The shutdown inventory (`inventory.inventory(login)`, `live_group_members`) is per login and per machine and has no Codex rows, so it does not serve here. Role map: `showrunner` → `showrunner`, `unit director` → `unit`, `worker` → `worker`, `freestanding` → `freestanding`.
   - `RosterEntry` (`roster.py:173–186`) gains `account: AccountLabel | AccountUnread = AccountUnread("not read")` (frozen dataclasses in `roster.py`; `AccountLabel(label)`, `AccountUnread(reason)`), set on the machine that owns the session:
     - Claude rows (`_claude_entries`): `account.account_of(record["pid"])` → `Account.label` (`account.py:186–189`).
     - Codex workers (`_delegate_workers`, `roster.py:1051–1106`): the run's `mesh_server.json` `codex_home` (`ServerRecord.codex_home`, `codex_mesh.py:230–233`) when the server is alive, else `agent_accounts.seats_home()`; label from `agent_accounts.codex_account(home)` when `KnownCodexAccount`.
     - Codex daemon threads: the home holding the daemon socket (`codex_daemon.daemon_socket(environment)`, `~/.codex/app-server-control/…`). Blind reviewers and herdr Codex panes: `CODEX_HOME` from the process environment when the row has a pid (new `account.process_codex_home(pid) -> Path | None`, read like `process_config_dir`, `account.py:159–183`), else `~/.codex`; a Claude reviewer uses `account.account_of(pid)`.
   - The snapshot carries it: `SnapshotEntry.account: NotRequired[dict[str, str]]`, `AccountLabel`/`AccountUnread` join `_SNAPSHOT_STATES`, and `_decode_entry` (`roster.py:261–284`) accepts an entry with or without `account` (without → `AccountUnread("peer sent no account")`). Today it refuses any entry whose field set differs (`:263`), so a peer one version apart would read as not reached; the two machines update their `~/.claude` at different times.
   - `roster.peer_snapshot(environment) -> Roster | PeerNotReached(host, reason)`; `peer_roster` (`roster.py:1194–1215`) wraps it with today's return, so `send.py:734` and `roster()` are unchanged.
   - `session_table.live_sessions(environment) -> ReadSessions | SessionsUnread`: `roster.roster(environment, ThisMachine())` and `peer_snapshot` in parallel (as `roster()` does); `ReadSessions(rows_by_label: dict[str, tuple[SessionRow, ...]], unreached: frozenset[str])`. A row with `AccountUnread`, or whose label has no note, lands in no note.
   - The update pass (in `update_alert_pass`, after the `apply` loop and before the alert reads the notes): for each note, its rows are `rows_by_label[label]` plus, for each machine in `unreached`, the rows the note's table already holds for that machine (a sleeping Mac keeps its rows until the roster reaches it again). `SessionsUnread` (the local roster raised) leaves every `sessions` line and table as written and logs `sessions: <reason>`. A roster failure never costs the readings or the alerts (same `try` discipline as `usage_store.save`, `agent_notes.py:288–295`).
   - Cost, named: one local roster read and one `ssh` to the peer (20 s timeout, run in parallel) per 2-minute pass, inside the timer's 120 s budget for `agent_notes.py`.

9. **Phase 20's texts follow the new name.** `quota.codex_on_credits` (`push_catalog.py:59`, examples 62–65) and `quota.codex_on_credits_last` (`push_catalog.py:67`, examples 72–73) take `{file}` from `quota_alert.py:1131`; their examples change to `codex1.md` / `codex2.md`, which changes `push_texts.fingerprint(entry)`, so the impl worker re-approves both from the worktree root: `~/.claude/scripts/lib/py scripts/notify/review.py texts --id quota.codex_on_credits --approve` and `--id quota.codex_on_credits_last --approve` (a `rewrite` verdict goes into the entry and the command runs again, as in Phase 20 item 10). The template text `~/rust/hanadocs/agents/{file}` itself is unchanged. No other catalog entry names a note file.

**Files:**

- `scripts/whoami/note_file.py` — `ACCOUNT_STEM`, `label_from_stem`, `note_name`, `notes_dir`, `note_path`, `Note.label`, `get_list`, `in_use`, `FieldReader`; `read_notes` one per label.
- `scripts/whoami/agent_notes.py` — `rename_legacy_notes`, `write_note` (lists, removal, the section, one write), `state` and the activity types removed, `role` and `sessions` and the table written, labels in readings and the usage store, `mark_tier` by role.
- `scripts/whoami/session_table.py` — new: `SessionRow`, `live_sessions`, `render_section`, `section_rows`, `replace_section`.
- `scripts/whoami/account.py` — `label_for`, `named_claude_account` by label; `process_codex_home`.
- `scripts/whoami/agent_accounts.py` — `codex_account`, `codex_credits_setting`, `codex_account_home` through `note_path`/`read_notes`.
- `scripts/whoami/quota_alert.py` — `note.label`, `in_use`, `AgentNote.label`/`get_list`, `RefusingAccountNote`, `note_name` for `{file}`, the notice line.
- `scripts/accounts/accounts.py` — `_account_notes_directory`, `_account_note`, `_noted_labels`, `codex_statuses` labels; `group_note_labels`.
- `scripts/message/roster.py` — `RosterEntry.account`, the snapshot field and tolerant decode, `peer_snapshot`, `PeerNotReached`.
- `scripts/production/dailies_render.py` — the Agents section through `note_file`.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — the two examples and their approvals.
- `docs/accounts.md`, `docs/quota_alerts.md`, `docs/as-built/session-roster.md`, `docs/as-built/shutdown-and-restart.md`, `docs/as-built/build-memory-admission.md`, `docs/as-built/build-followups-fn-length-hook.md`, `commands/quota_refresh.md` — names, `role`, `sessions`, the table, the roster's account.
- `scripts/accounts/test_accounts.py` — numbered note names, no `state:`, the new behavior.
- `scripts/accounts/test_refusal_watch.py` — numbered note names, no `state:`, the new behavior.
- `scripts/agents/test_codex_mesh.py` — numbered note names, no `state:`, the new behavior.
- `scripts/message/test_codex_daemon.py` — numbered note names, no `state:`, the new behavior.
- `scripts/message/test_escalate.py` — numbered note names, no `state:`, the new behavior.
- `scripts/message/test_roster.py` — numbered note names, no `state:`, the new behavior.
- `scripts/nightly_review/test_nightly_review.py` — numbered note names, no `state:`, the new behavior.
- `scripts/notify/test_push_texts.py` — numbered note names, no `state:`, the new behavior.
- `scripts/production/test_add_showrunner.py` — numbered note names, no `state:`, the new behavior.
- `scripts/production/test_dailies_render_agents.py` — numbered note names, no `state:`, the new behavior.
- `scripts/shutdown/test_account_move.py` — numbered note names, no `state:`, the new behavior.
- `scripts/usage/test_usage_report.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_agent_notes.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_five_hour.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_note_file.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_quota_alert.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_session_table.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_account.py` — numbered note names, no `state:`, the new behavior.
- `scripts/production/test_add_unit.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_codex_alert_accounts.py` — numbered note names, no `state:`, the new behavior.
- `scripts/whoami/test_codex_homes.py` — numbered note names, no `state:`, the new behavior.
- `scripts/shutdown/test_inventory.py` — numbered note names, no `state:`, the new behavior.
- `scripts/production/test_production_lifecycle.py` — numbered note names, no `state:`, the new behavior.
- `scripts/production/test_promote_showrunner.py` — numbered note names, no `state:`, the new behavior.
- `scripts/shutdown/test_restart.py` — numbered note names, no `state:`, the new behavior.
- `scripts/shutdown/test_shutdown_cli.py` — numbered note names, no `state:`, the new behavior.
- `scripts/shutdown/test_shutdown_status.py` — numbered note names, no `state:`, the new behavior.
- `scripts/production/test_waiting.py` — numbered note names, no `state:`, the new behavior.

**Seats:** 1 writer + 1 tester.
- `impl` — every production file and doc above, and the two re-approvals; hub: `scripts/whoami/note_file.py` (the label/file mapping every other file calls).
- `test` — every note a test writes uses the numbered name (`note_file.note_name(label)` or the literal `claude3.md`), and fixtures drop `state:`:
  - `scripts/whoami/test_note_file.py`: `label_from_stem` for `codex2`, `codex 2`, `claude10`, and `None` for `codex0`, `codex`, `notes`; `note_name` and its `ValueError`; `note_path` with the numbered file, the old file alone, both, neither; `read_notes` one note per label (numbered preferred) and a non-account `.md` skipped; `tool` is `codex` for `codex2.md`; `get_list` for block, flow, `[]`, scalar and absent; `in_use` for a role alone, sessions alone, neither, an unparsable `sessions`; `notes_dir` with and without `AGENT_NOTES_DIR`.
  - `scripts/whoami/test_agent_notes.py`: a first pass renames `codex 2.md` to `codex2.md` with body and mode (0o644 under umask 0o077) kept; both names present changes neither and logs the line; no `state` is written and an existing `state:` line is removed with every other line kept; `role` for a units-and-showrunners account is the two-item block, `worker` on the selected Codex note, `role: []` elsewhere; an unknown Codex selection and an unreadable `groups.json` keep their roles; `sessions` and the table written; a second pass with the same rows writes nothing (no `os.replace`, mtime unchanged) and a status change alone writes nothing; the section replaced in place with body text above and below it unchanged, and created when missing; an unreached peer keeps its rows while local rows are replaced; `SessionsUnread` leaves tables and counts alone; an Obsidian block `role:` rewritten leaves no orphan items and `return:` stays whole; `readings.jsonl` lines from `codex2.md` carry `account: codex 2`; `mark_tier` writes the tier only on the worker note. Every test reaching `update_alert_pass` or `main` replaces `session_table.live_sessions` with a fake (no roster read, no `ssh`).
  - `scripts/whoami/test_session_table.py` (new): render and `section_rows` round trip with a `\|` cell; sort order; folder shortening for `/home/<user>` and `/Users/<user>`; `replace_section` at the end, in the middle and absent; `No live sessions.`.
  - `scripts/whoami/test_quota_alert.py`: `low`, `usable`, `recovered` and the refresh lines by `in_use`; the notice line with roles and session count; `{file}` is `codex1.md`; the `blocked` fallback note carries its label. `test_codex_alert_accounts.py`, `test_codex_homes.py`, `test_account.py`: labels `codex 2`/`claude 2` from numbered files; an old name alone still resolves.
  - `scripts/accounts/test_accounts.py`: the missing-note line names `claude3.md`; status lists noted labels from numbered names; `group_note_labels` for configured, unconfigured and a stored `default`.
  - `scripts/message/test_roster.py`: a Claude row's account from its config dir, a Codex worker's from `mesh_server.json`'s `codex_home`, a daemon thread's from the socket's home; a snapshot entry without `account` decodes as `AccountUnread`; one with it round-trips; `peer_snapshot` returns `PeerNotReached` on a timeout.
  - `scripts/production/test_dailies_render_agents.py`: the Agents section by `in_use` and by label from numbered files; `- none active` when nothing is in use.
  - Fixture renames only: `scripts/accounts/test_refusal_watch.py`, `scripts/agents/test_codex_mesh.py`, `scripts/message/test_escalate.py`, `scripts/message/test_codex_daemon.py`, `scripts/nightly_review/test_nightly_review.py`, `scripts/production/test_add_showrunner.py`, `test_add_unit.py`, `test_promote_showrunner.py`, `test_production_lifecycle.py`, `test_waiting.py`, `scripts/shutdown/test_account_move.py`, `test_inventory.py`, `test_restart.py`, `test_shutdown_status.py`, `test_shutdown_cli.py`, `scripts/usage/test_usage_report.py`, `scripts/whoami/test_five_hour.py`. `scripts/notify/test_push_texts.py` stays green with the two re-approved ids.

**Constraints from prior phases:** "Codex credits setting and the 0% alert" reads `use_credits` through `codex_credits_setting(label)` and resolves `codex N` through `codex_account_home(label)`, both by note path; its `quota.codex_on_credits`/`_last` entries carry `{file}`; keep its names and return types. "Two account groups" ships `group_accounts`, `group_label`, `GroupNotConfigured` and `role_account("unit"|"showrunner")`; names and types stay (herdr adoption depends on them). Every state file keys accounts by label — `quota_alert.json` `episodes` and `codex_zero`, `~/.local/state/agent-notes/readings.jsonl`, `usage.json`, `roles.jsonl`, `groups.json` — and labels do not change, so no state file is rewritten. The alert pass runs at the end of `agent_notes.py`'s argument-free run, every 2 minutes from natedev's `agent-sessions-snapshot` timer (`/etc/nixos/modules/linux/agent-sessions-snapshot.py`, which then commits `agents/*.md` through `hanadocs-commit.py`, leaving `date_modified`, `weekly_usage_checked_at`, `weekly_remaining_usage`, `credit_balance`, `resets` and `limit_reset` out of its change test); the Mac has no timer. `accounts.py` never imports `quota_alert` or `codex_mesh` at module level; `agent_notes` reaches `roster` and `accounts` inside functions. `agent_accounts` is imported by name, never by path. Every test reaching `agent_accounts.user_zone()` points `showrunners.NOTIFIER_STATE_DIR` at an empty dir. Tests set `HOME` and `AGENT_NOTES_DIR` to temp dirs, never touch `~/rust/hanadocs` or `~/.codex*`, and never `ssh`. `agents.base` and every other vault file are hanadocs's: this phase edits no vault file; the notes change only through `agent_notes.py` at run time. "Accounts named by number only" follows and adds `accounts.group_note_labels`'s `default` branch to its site table; its line numbers in `agent_notes.py` (~296–317) move with this phase.

**Acceptance gate:** the whoami, accounts, notify, lib, production, shutdown, nightly_review and message suites (`python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'`) pass under a temp `HOME`, and `basedpyright` on the touched files reports nothing; `test_push_texts` green with both ids re-approved; `grep -rnE '(claude|codex) \{|f"\{label\}\.md"|\.stem' scripts/whoami scripts/accounts/accounts.py scripts/production/dailies_render.py` finds no note path built outside `note_file`. No live note is moved by hand: the first timer pass after promotion renames them. After promotion (showrunner): the timer's log shows five `agents: renamed …` lines and no `both exist`; `~/rust/hanadocs/agents/` holds `claude1.md` … `codex2.md` with their bodies, each with `role`, `sessions` and `## Live sessions`; `/showrunner:accounts status` reads every account.

### Phase 22 — Accounts named by number only  · status: todo

**Depends on:** Phase 20, Phase 21

#### Work Order

**Goal:** no command, output or message names an account `default`; every Claude and Codex account is named `claude N` / `codex N` by the login its folder holds (`~/.codex` = codex 2; `~/.claude` = the number of its login's note); old `default` entries read as that number.

**Spec:**

1. **One resolution per tool.** `accounts.claude_account_dir(label) -> Path`: `~/.claude` when the `claude N` note's login equals `~/.claude`'s login (today's default-login alias in `prepared_account` ~1025–1028), else `~/.claudeN`. Codex uses `agent_accounts.codex_account_home` from "Codex credits setting". Every `main_config_dir() if label == "default" else account_dir(label)` becomes `claude_account_dir(label)`.
2. **`DefaultAccount` goes.** `ClaudeLaunchAccount = NamedAccount`; a `NamedAccount` for `~/.claude`'s account carries `dir=~/.claude`. `account_prefix` (~1145) only renders text and returns `""` for that dir; removing `account.NAMED_ACCOUNT_VARIABLES` and `CLAUDE_CODE_OAUTH_TOKEN` stays in `prepared_account`'s environment, as its alias branch does now, with tests launching the `~/.claude` account from a named-account environment holding an inherited OAuth token. Callers that test `isinstance(..., DefaultAccount)` test the dir instead.
3. **Input.** `move <session>`, `move units|showrunners`, `set-role seats`, `swap seats`, `exec`, `check`, `pick --exclude/--keep` refuse `default` with `accounts: name the account by number (~/.claude is <claude N>; ~/.codex is <codex N>)`, the numbers read live. `move … "claude N"` for `~/.claude`'s number moves to `~/.claude` (no more `<label> is the default account; use default`). `exec default` refuses with that line; the `claude` shell command is not an account name and stays.
4. **Storage.** Writes store numbers only: `groups.json` (`set_group_account`), `config/accounts.json` `seats`, `roles.jsonl`, shutdown records' `target`. Reads: a `groups.json` value `default` (`_stored_groups` ~1432 accepts it today) reads as `~/.claude`'s number; a `roles.jsonl` `label: default` reads as `~/.claude`'s number for `role: unit` (as `_unit_role_history`'s `note_labels["default"]` does now) and `~/.codex`'s number for `role: seats`; a `SessionShutdown.target` `default` (`record.py` ~962 accepts `default` or `claude N`) reads as `~/.claude`'s number; an absent `seats` key reads as `~/.codex`'s number. No file is rewritten in place.
5. **Output.** `role` prints the number (`accounts.py` ~2863); CLI usage strings say `<claude N>` (~2824, ~2828); `pick` and `placement` candidates are labelled by number (~1263, ~1331, ~1513); `session_account_label` (~1469–1472) returns the number; status keeps the `~/.claude` folder in its Folder column and shows the number as the label. Internal names follow: `ClaudeAccountsInUse.default_account` → `home_directory_account`, `is_default_login` → `is_home_directory_login` (`agent_accounts.py` ~123–134; readers `quota_alert.py` ~796, `agent_notes.py` ~317, `refusal_watch.py` ~486–492).
6. **`restart._resume_parts`** (~509–524) sets `target = "default"` when the configured group account is `DefaultAccount`; it becomes the number, and `restart._target_directory` (~1229–1233), the herdr branch (~993–1001) and ~1087 resolve through `claude_account_dir`. A record written before this phase with `target: default` restarts on `~/.claude`.

**Sites where `default` names an account, and what each becomes** (file:line when drafted, 2026-10-14; re-read each before editing):

| Site | Becomes |
| --- | --- |
| `scripts/accounts/accounts.py:198` `DefaultAccount` | removed; `NamedAccount` with `dir=~/.claude` |
| `accounts.py:212` `CodexSeatsAccount` docstring "including the default home" | "any Codex home" |
| `accounts.py:867` `check("default")` | `check("claude N")` runs `_login_problem(~/.claude)` for `~/.claude`'s number |
| `accounts.py:983–997` `_check_default_codex_home` | the `~/.codex` branch of the `codex N` check; message `set up ~/.codex first` |
| `accounts.py:1017–1041` `prepared_account` `default` label and alias | `claude_account_dir` decides the dir |
| `accounts.py:1115` `role_account("seats")` label | `codex_home_role_label` (number) |
| `accounts.py:1121` `role_account` group `default` | `NamedAccount(label, claude_account_dir(label))` |
| `accounts.py:1128–1147` `account_for_group_launch` | `--account` takes a number; `default` refused |
| `accounts.py:1263–1274` `pick` candidates `("default", main_config_dir())`, Codex `label != "default"` | numbers |
| `accounts.py:1331–1332` `pick` fallback `preferred … "default"` | `~/.claude`'s number (Claude), `~/.codex`'s number (Codex) |
| `accounts.py:1432` `_stored_groups` | `default` read as `~/.claude`'s number |
| `accounts.py:1469–1472` `session_account_label` | number |
| `accounts.py:1481–1483` `account_labels_for_logins` | numbers only |
| `accounts.py:1495, 1501, 1513` `placement` | `claude_account_dir`; tie order by number |
| `accounts.py:1575` `set_group_account` | refuses `default` |
| `accounts.py:1598` `placement_report` | `claude_account_dir` |
| `accounts.py:1686, 1701, 1718` `_unit_role_history` | legacy `default` read as in Spec 4 |
| `accounts.py:1875–1882` `_write_role` `previous_label … "default"`, `pop("seats")` | stores and reports numbers |
| `accounts.py:2093–2100` `_move_target` `is the default account; use default` | removed; `~/.claude`'s number is a valid target |
| `accounts.py:2111–2123` `move_group` rewrite to `default` | removed |
| `accounts.py:2154` `session_on_account` | `claude_account_dir` |
| `accounts.py:2219–2220` `check_codex_target` | `codex_account_home` |
| `accounts.py:2645` `status_report` group dirs | `claude_account_dir` |
| `accounts.py:2824, 2828` usage `<claude N\|default>` | `<claude N>` |
| `accounts.py:2863` `role` prints `default` | number |
| `scripts/whoami/agent_accounts.py:89–91` `codex_home_role_label` | number (done in "Codex credits setting") |
| `agent_accounts.py:123–134` `default_account`, `is_default_login` | renamed (Spec 5) |
| `scripts/whoami/quota_alert.py:796–797` `excluded.add("default")` | the number |
| `scripts/whoami/agent_notes.py:317` `is_default_login` | renamed |
| `scripts/accounts/refusal_watch.py:486–492` source `default` | the number |
| `refusal_watch.py:587` `target == "default"` | `claude_account_dir` |
| `scripts/shutdown/restart.py:517–522` `_resume_parts` | number |
| `restart.py:993–1001` herdr `DefaultAccount()` | `NamedAccount` with `~/.claude` |
| `restart.py:1087`, `restart.py:1233` `target == "default"` | `claude_account_dir`; legacy `default` read |
| `scripts/shutdown/record.py:962` target `default or claude N` | accepts legacy `default`, writes `claude N` |
| `scripts/production/add_unit.py:534–574` `"default account"` | the number |
| `add_unit.py:733–763` `DefaultAccount` | dir test |
| `add_unit.py:997` `target="default"` | number |
| `scripts/production/add_showrunner.py:67–109` `DefaultAccount`, `"default account"` | number |
| `scripts/production/promote_showrunner.py:186` | `claude_account_dir` / dir test |
| `scripts/nightly_review/nightly_review.py:361` `"default account"` | number |
| `scripts/herdr/herdr.py:438–441` `DefaultAccount` | dir test |
| `scripts/notify/review.py:157, 565` reviewer `account: "default"` (logged in `review.jsonl`) | the number of the reviewer's account |
| `docs/accounts.md:11, 41, 56, 118, 130, 184, 188, 197, 202, 204` | numbers; the `claude` row reads `~/.claude (today claude N)` |
| `commands/showrunner/accounts.md:3, 19, 23–29` | `"claude N"` / `"codex N"` only |
| `docs/as-built/shutdown-and-restart.md:34, 38` | `target` names `claude N`; legacy `default` read |
| `docs/quota_alerts.md:3, 26, 28, 40` | `~/.claude`'s account by number |
| `accounts.py:2732–2760` `_move_status_lines` renders stored `requested.from/to`, `room again.label` | normalized by tool through the journal reader (`default` → that tool's home number); the file is unchanged; Phase 9 reads through the same reader |

Not account names, unchanged: `codex_pacer.py` tier `default`, `dailies_render.py` chart style, `herdr.py:474, 569` herdr session `default`, `fake_herdr.py`, `hooks/context_usage.py`, argparse `default=`, `config/agents.conf` `codex_service_tier=default`.

**Files:**

- `scripts/accounts/accounts.py`, `scripts/accounts/refusal_watch.py`, `scripts/accounts/test_accounts.py`, `scripts/accounts/test_refusal_watch.py` — resolution, input, storage, output.
- `scripts/whoami/agent_accounts.py`, `scripts/whoami/quota_alert.py`, `scripts/whoami/agent_notes.py`, `scripts/whoami/test_agent_notes.py`, `scripts/whoami/test_quota_alert.py` — renames and labels.
- `scripts/shutdown/restart.py`, `scripts/shutdown/record.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_record.py` — targets.
- `scripts/production/test_promote_showrunner.py`, `scripts/herdr/test_herdr.py`, `scripts/notify/test_review.py` — fixtures that build `DefaultAccount`.
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`, `scripts/production/add_showrunner.py`, `scripts/production/promote_showrunner.py`, `scripts/nightly_review/nightly_review.py`, `scripts/herdr/herdr.py`, `scripts/notify/review.py` — `DefaultAccount` and output.
- `docs/accounts.md`, `commands/showrunner/accounts.md`, `docs/as-built/shutdown-and-restart.md`, `docs/quota_alerts.md` — numbers only.

**Seats:** 1 writer + 1 tester.
- `impl` — every production file and doc above; hub: `scripts/accounts/accounts.py` (`claude_account_dir`, `ClaudeLaunchAccount`).
- `test` — `default` refused with the numbered line by `move <session>`, `move units`, `set-role seats`, `swap seats`, `exec`; `move units "<~/.claude's number>"` selects `~/.claude`; a `groups.json` holding `default` reads as `~/.claude`'s number in `role_account`, status and `placement`; `roles.jsonl` legacy `unit`/`seats` `default` lines read as numbers in `burn`; a shutdown record with `target: default` restarts on `~/.claude`; `_resume_parts` for a group on `~/.claude` yields its number; `role seats` with no `seats` key prints `~/.codex`'s number; a sweep test renders `status_report` (with legacy Claude and Codex `default` journal entries seeded), `placement_report`, `pick --explain`, the move and swap refusals and asserts no `default` as an account name; the `DefaultAccount` fixtures in `test_promote_showrunner.py`, `test_herdr.py` and `test_review.py` migrate.

**Constraints from prior phases:** "Codex credits setting and the 0% alert" ships `agent_accounts.codex_account_home(label) -> CodexHomeHoldingLogin | CodexSetupHome` and `codex_home_role_label` by note, and stores Codex account labels in the seats role. "Two account groups" ships `groups.json` (`set_group_account` the only writer, a `roles.jsonl` group line per change), `role_account("unit"|"showrunner")` and `account_for_group_launch`, whose names and return types herdr adoption depends on; keep the names. `prepared_account` (from "An account prepared for any launcher") removes `CLAUDE_CODE_OAUTH_TOKEN` always and `account.NAMED_ACCOUNT_VARIABLES` for `~/.claude`; a caller that skips `remove` leaks a named account into a `~/.claude` launch. `restart.launch_entry` keeps catching `accounts.AccountsError` per entry. `/etc/nixos/modules/linux/shell.nix` aliases `claude-unit` to `accounts.py exec unit` and `claude1..3` to `exec 'claude N'`; `exec unit` stays. herdr-trial's Phase 7 lands after "Group moves pause first" and replaces its manual `accounts.py exec` line for herdr hosts in `restart.py` with herdr's automatic restart on the group account; take `restart.py`'s herdr branch as it stands on `build-followups` when this phase starts and build nothing on the manual line. `add_unit.py` and its test are enh-showrunner-unit's; edit them through berth and name them in the checkpoint notice. Tests set `HOME` to a temp dir and never touch `~/.claude*`. No `move`, `set-role`, `swap` or `shutdown down` runs against the real home.

**Acceptance gate:** the accounts, shutdown, production, whoami, nightly_review, herdr, notify and lib suites and `basedpyright` on the touched files pass, and pass again under the real home; on natedev `accounts.py status`, `placement --explain`, `pick claude --explain`, `pick codex --explain` and `role seats` print no account named `default`, and `move units default --dry-run` refuses with the numbered line.

### Phase 23 — Proof refusals stay with their session  · status: todo

**Depends on:** Phase 16, Phase 17, Phase 18, Phase 19, Phase 22

#### Work Order

**Goal:** a refusal assumed for the live proof moves or holds only its probe session and leaves the account's refusal record and every pick untouched; each `requested` entry says whether a refusal or a hand move started it.

**Spec:**

1. **A typed origin.** `refusal_watch.session_refusal` returns `TranscriptRefusal` (the transcript's `rate_limit` entry), `ProofRefusal` (an `--assume-refused` file) or `NoSessionRefusal`. The on-disk file shape is `AssumedRefusalFile`. Every consumer (the watcher, `cancelled`, settle's usage-limit hold) matches on the type, never on a text field.
2. **No account-wide write.** A `ProofRefusal` never calls `refusals.write_refusal`: the account's stored refusal and `refusals.resolved()` are unchanged by a proof, and `accounts.pick` for any other session gives the same answer before and after it. The probe's own move still excludes its source account through `pick(..., exclude=…)`. A `TranscriptRefusal` keeps writing the account refusal once per (session id, entry uuid), as now.
3. **The proof file ends with the proof.** The assumed refusal file is deleted when the session's record restarts, when it is cancelled (both as now), and when the watcher delivers the continuation after a five-hour wait.
4. **The move says its cause.** `RequestedMove`'s `cause` (added by "Codex credits setting and the 0% alert" with `credits off`) becomes `Literal["refusal", "credits off", "by hand"]`; a reader treats a missing `cause` as `refusal` (Codex entries, and Claude entries written before this phase). `move_sessions(ids, target, *, why, origin)` takes `MoveOrigin = RefusalOrigin(window, reset) | HandOrigin` and passes it to `shutdown.py down` as `--refused <weekly|five_hour|unknown> [--reset <aware ISO>]` or nothing. With `--refused`, the `requested` entry is `cause: refusal` with that window and reset. Without it (`accounts.py move`, the group move commands) is `cause: by hand`, `window: unknown`, no `reset`, and never inherits a stored account refusal; today `settle.down` reads `refusals.load()` for the window, which a hand move must not do.
5. `/showrunner:accounts status` shows `by hand` beside a hand move's request in the last-10 list.
6. **A proof stays out of the groups.** `--assume-refused` takes only an ungrouped probe (inventory kind neither `unit` nor `showrunner`) and refuses a grouped session with that reason, since a grouped session relaunches only on its group's account. A `ProofRefusal` never changes a group's stored account and never joins the exclusions the tick uses for other sessions.
7. **A group move is one counted transition.** A group move gets one `move_id` with its origin (cause, window, reset); the `roles.jsonl` group entry, every member's `requested` entry, and a later tick's reconciliation request for that group all carry it, so a partial move completes under the same id and cause. Both journal writers (`settle.down` and the fallback writer in `accounts.py` ~2025) write the cause. Phase 9 counts group transitions by that id. The id is durable: `groups.json` stores the selection's `move_id` and origin beside its label, so a reconciliation after a process restart reuses it; each shutdown attempt keeps its own `request_id` and carries the `move_id` beside it, so stale-request guards still key on the attempt. Tests: reconciliation after a restart of the watcher keeps the id; a failed attempt followed by a successful one shares the `move_id` with two `request_id`s; a hand selection with no live members records the id and a later member's move carries it. `restart.py`'s record readers (~1222–1248) and `record.py` read the `move_id`.
8. The watcher's fallback for a watch entry without its first observation goes: no such watch file exists outside tests.
10. **A scratch group for the live proof.** `refusal_watch.py --assume-refused <probe> <window> --proof-group <probe id>…` treats exactly the named ungrouped probes as one group: it picks one target, requests one `move_sessions` for them with one `move_id`, and never reads or writes `groups.json`, a real group's `roles.jsonl` line or any real member. Test: with real units and showrunners live in the inventory fixture, a proof-group move sends `Pause for move` to the two probes only and no request, record or message reaches a real member.
9. **One account resolver for every Claude launch** (showrunner, 2026-10-14, after nightly-review launched itself on an account it chose). `accounts.account_for_group_launch` (or one function it becomes) is the only place a script decides which Claude account a launch uses; `add_unit.py`, `add_showrunner.py`, `promote_showrunner.py`, `restart.py`, `refusal_watch.py` and `nightly_review.py` (moved under `add_unit` by enh-showrunner-unit) call it and pick no account themselves. Named exceptions, each with its reason in the test's allow list: `accounts.py exec <label>` (the user names the account), an ungrouped session's resume in `restart._resume_parts` (it keeps its recorded directory), the message relay `scripts/message/send.py` (~850) and the headless reviewer `scripts/notify/review.py` (~551, the units account through `role_account`). A test walks every script under `scripts/` that starts `claude` and fails when one outside that list sets `CLAUDE_CONFIG_DIR`, passes an account label or directory, or calls `pick`/`prepared_account` for a Claude launch without the resolver. The watcher keeps its own placement decision for a group move (it selects the group's account, then the resolver reads it). This item lands after enh-showrunner-unit's `nightly_review.py` change is on `build-followups`; merge it before this phase's checkpoint.

**Files:**

- `scripts/accounts/refusal_watch.py`, `scripts/accounts/test_refusal_watch.py` — the typed origin, no account write for a proof, the file deleted on continuation.
- `scripts/accounts/moves.py`, `scripts/accounts/test_moves.py` — `cause`.
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py` — `move_sessions` takes the move origin; status shows `by hand`.
- `scripts/shutdown/settle.py`, `scripts/shutdown/shutdown.py`, `scripts/shutdown/test_settle.py` — `--refused`/`--reset`, the `requested` entry's cause, window and reset; the hold matches on the refusal type.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — a proof refusal stays with its session; the cause on a move.
- `scripts/production/add_unit.py`, `add_showrunner.py`, `promote_showrunner.py`, `scripts/shutdown/restart.py`, `scripts/nightly_review/nightly_review.py` — each Claude launch calls the one resolver.
- `scripts/accounts/test_launch_accounts.py` — new: no script launches claude on an account it picked itself.

**Seats:** 1 writer + 1 tester.
- `impl` — every production file and doc above; hub: `scripts/accounts/refusal_watch.py` (the refusal types).
- `test` — the tests above: a proof refusal leaves `refusals.load()` and `pick` unchanged while its probe still moves; a transcript refusal still writes once; the proof file deleted on restart, cancel and continuation; a watcher move's `requested` has `cause: refusal` with the refusal's window and reset; a hand `move` with a stored account refusal present writes `cause: by hand`, `window: unknown`, no `reset`; a missing `cause` reads as `refusal`; `--assume-refused` on a grouped session refuses and leaves both group accounts unchanged; a group move's `move_id` and cause appear on its `roles.jsonl` entry, every member's `requested` entry and a later tick's completion request, for a hand move and a refusal move; the status line shows `by hand`; settle's hold entered from a `ProofRefusal`; the launch-account test fails on a fixture script that sets `CLAUDE_CONFIG_DIR` or picks an account itself and passes on the real tree.

**Constraints from prior phases:** "Claude sessions move on a refusal" ships the watcher (`refusal_watch.py`, run at the end of `agent_notes.py`'s argument-free run), its watch state `~/.local/state/accounts/refusal_watch.json` (`WatchedSession` per session: `entry_id`, `state` in `requested|cancelled|continued|waited|no room|retry`, `move_id`, and the first observation `FirstRefusalObservation`), the assumed refusal files `~/.local/state/accounts/assumed_refusals/<session id>.json` (`uuid`, `timestamp`, `window`, `reset`), and `move_sessions` returning `SessionMoveRequested | SessionMoveRefused` per session (a refusal before any record writes a standalone `failed`). A five-hour wait ends with `wait ended`, then one `Continue where you stopped.` through `settle.send_message` and the entry marked `continued`. `settle.down` writes the `requested` entries after the records exist and withdraws them if the conductor launch fails; `refusal_watch.cancelled` runs before a cancelled record is withdrawn. `restart.mark_restart_failed` is the one path that sets `restart failed`/`manual restart` on a targeted record and journals `failed`. `accounts.py` imports no Codex mesh or alert code at module level (lint fails on an import cycle). Every test module reaching `agent_accounts.user_zone()` points `showrunners.NOTIFIER_STATE_DIR` at an empty directory. "An account prepared for any launcher" moves `exec_account`'s account preparation into `prepared_account`. "Two account groups" adds the watch state `group selected`; the showrunner resume lookup sits in `restart._resume_parts`; `restart.launch_entry` catches `accounts.AccountsError` per entry so one refused member never stops the rest.

**Acceptance gate:** the accounts, shutdown, production, whoami and nightly_review suites and the scoped lint pass, and pass again under the real home. `refusal_watch.py --dry-run` on natedev reports no refused session, or each one with its decision.

### Phase 24 — Live proof of the moves  · status: todo · gated by G4

**Depends on:** Phase 15, Phase 16, Phase 18, Phase 19, Phase 20, Phase 22, Phase 23

#### Work Order

**Goal:** show the user, on natedev, a Claude session and the Codex seats each moved by a refusal and working on the target.

**Spec:**

1. `accounts.py placement --explain` agrees with status's `units:` and `showrunners:` accounts, and `pick codex --exclude '<current seats label>' --explain` with status's account the seats would move to, on the same stored readings.
2. A scratch probe in tmux on a Claude account: `refusal_watch.py --assume-refused <probe> weekly` moves it to the picked account through the real stop path (the shutdown hold reads the assumed refusal). It resumes with the moved note, answers a question about its earlier turns, and `account.py --pid` shows the target. While it waits in the shutdown hold, `/shutdown status` shows its target, why and what holds it. Run once more with `five-hour --reset-in 15` and it waits: one `waited`; after the reset, one `wait ended`, one `Continue where you stopped.` delivered to the probe, the probe working on, `account.py --pid` unchanged, no `requested`, and the probe's assumed refusal file gone. Neither proof changes the account's stored refusal: `pick claude --explain` names the same account before and after each.
3. A scratch probe in a Ghostty window: the same move, with the window back on its desktop.
4. Codex, with the user present: `agent_notes.py blocked <seats home> <login> '<seats label>' --window weekly --refused-at <now, aware ISO>` moves the seats role to the picked Codex account: one `requested`, then one `confirmed` per seat as each resumes on the new home. The mesh's own path — a seat refused mid-turn resumes with exactly one "Continue where you stopped." before any held message — is proved by its tests and live by the first real Codex refusal (item 6). A Codex 5-hour wait is shown live only if a 5-hour reset under 20 minutes away occurs during the session; otherwise its tests stand.
4a. **A scratch group moves whole** (showrunner, 2026-10-14; no user needed, nothing real moves). Two probe sessions taken as one group with `--proof-group` ("Proof refusals stay with their session" item 10) take a refusal: both probes get the `Pause for move` message and answer `ready --where`, both resume on the target account with their conversations, and no real session receives any message.
5. `moves.jsonl` holds, for each move above, its `requested` and then a `confirmed` per seat or session (or `failed` with its reason), and for each wait its `waited` and `wait ended`; `/showrunner:accounts status` lists them with their outcomes.
6. The first real refusal after this phase, Claude or Codex, is reported to the showrunner with its entries when it happens. Phase 24 completes, and G3's clock starts, without waiting for it.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the results.

**Seats:** nothing splits: the unit director runs the proof with the user; a defect found gets a repair seat under the usual rules.

**Constraints from prior phases:** the phases before this ship `pick`, the Codex move on a refusal, and the Claude watcher with `--dry-run` and `--assume-refused`; a proof refusal moves or holds only its probe and writes no account refusal ("Proof refusals stay with their session"). The code: `scripts/accounts/refusal_watch.py`, `accounts.py`, `refusals.py`, `moves.py`; shutdown `settle.py`, `restart.py`, `inventory.py`; whoami `agent_notes.py`, `quota_alert.py`. On Linux a `terminal` or `unknown` host takes a manual restart, so the probes run in tmux and Ghostty. A no-room alert goes to the configured sessions and the running showrunners. No `set-role`, `swap`, `move` or `shutdown down` runs against the real home without the user present. Never print a credentials file.

**Acceptance gate:** items 1–5 observed live and written into this plan, with T0 recorded as the time the proof is accepted; item 6 is the unit director's follow-up and blocks neither this phase nor G3, and a real Claude refusal does not stand in for the Codex mid-turn continuation; `~/.claude/.credentials.json` unchanged; no Claude session other than the probes moved, except by a real refusal; the seats moved once in item 4, with the user present, and every running seat continued.

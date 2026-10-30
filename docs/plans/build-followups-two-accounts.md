# Two accounts: showrunners on one Claude account, unit directors on another

> **Status: IMPLEMENTATION PLAN — phases 1–6 are done; the cutover (Phase 7) moved all ten build-followups unit directors to claude 3 on 2026-10-11, with the spare dir and the cross-machine message check still open; Phase 8 moves any one session, or every showrunner off an account, by hand; Phases 14–17 drop the unit role so every new session picks the account with room, and move Codex workers and Claude sessions by themselves when their account refuses them; Phase 9 reads out a week of moves; Phases 10–12 add the Codex accounts: a home per account, one account for all workers with a swap, and alerts per account; Phase 13 repairs a worker launch that finished its turn while the seats account changed.** Phases 1–5 built everything and initiated nothing: no account dir, no login, no role. Phase 6 proved `~/.claude3` live. The design: each account the user has gets its own config dir, `~/.claude1`, `~/.claude2` and `~/.claude3`. Each holds only that account's login, and everything else is a symlink into `~/.claude`. Unit directors launch with `CLAUDE_CONFIG_DIR` set to the unit account's dir. Showrunners and every other session stay on `~/.claude`'s own login, as today. When the unit account runs low, one command swaps every unit onto a spare account through `/shutdown`'s safe stop and resume. Any one session, a showrunner included, moves to any account the same way with `/showrunner:accounts move` (Phase 8). Phased, delegate-ready once approved.

> **As-built disposition: create**

> **Production: natedev** — unit `accounts-unit`; production doc `docs/plans/natedev-production.md`

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
- The Mac is out of scope: units launch only on natedev (`add_unit.py` used `systemd-run` and tmux then; it now starts each unit in its own herdr session). Mac sessions stay on `~/.claude`.
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
   - The aliases are `claude1`, `claude2` and `claude3`; plain `claude` is unchanged. natedev commits them to `/etc/nixos`, and the user rebuilds when ready. Until a dir exists, each alias runs plain `claude` or refuses.
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
   - `/showrunner:accounts move <session> "claude N"` moves one showrunner, unit director or top-level session to any account. It stops safely, as `/shutdown` does it, and resumes the same conversation on the target in herdr: its recorded herdr pane, or a new herdr session, attached in a window for a Ghostty session.
   - Undo: move it back.
6. **Accounts by room (Phases 14–25).** No role ties a kind of session to an account. A new session launches on the account with room that resets soonest (the ranking function; Phase 74 removed the command verb). When an account refuses a session or the Codex workers, they move to the picked account by themselves and continue; a 5-hour refusal whose reset is under 20 minutes away waits instead. Phase 18 replaces the picker with two groups (showrunners, units), each on one account and moved together. Phase 19 makes every group move pause first. Phase 20 adds a per-Codex-account credits setting and one alert when the workers' account reaches 0%; Phase 21 renames the account notes by number and gives each note its roles, live session count and session table; Phase 22 spells every label without a space (`claude4`, `codex2`); Phase 23 names every account by its number, never "default"; Phase 24 keeps a proof refusal with its session; Phase 25 proves the moves live on throwaway probe sessions.
7. **A week later (Phase 9).** A readout gives the moves, their causes, any time with no eligible destination, and points per account per day.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-natedev-accounts`, branch `natedev-accounts` (unit `accounts-unit`, production natedev); name both in every dispatch.
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
    - `launch_session` starts each unit in its own herdr session: `herdr.start_new_server`, then `herdr.open_pane` with the unit marks, `ENABLE_TOOL_SEARCH=true` and the account environment, then `herdr.start_agent` runs `claude`.
    - `launch_environment` strips every `CLAUDE_*` variable.
    - The launch check is the `launch_permission.launch_permission(…)` call in `main()`.
    - A variable reaches `claude` through `open_pane`'s `--env` on the herdr workspace; a herdr server keeps the environment it started with, so restart refuses a server that carries a variable the resume unsets.
  - `scripts/shutdown/restart.py`:
    - `_run` strips `CLAUDE_*`.
    - `_resume_command` builds the resume command.
    - Units relaunch through `add_unit.py --resume`; `_unit_argv` builds that command.
    - Each `SessionShutdown` record's embedded `session.config_dir` holds the session's dir (absent means `~/.claude`); a non-unit resume keeps it, and a unit relaunches on the current unit role.
  - `scripts/shutdown/inventory.py`: `inventory()` lists `sessions.live_sessions()` (`~/.claude/sessions`), then keeps the sessions `_configuration_for_session` attributes to the login through `account_of`; `_model()` reads `~/.claude/projects` for the last model. `record.py` holds the record types (`SessionShutdown`, one per top-level session, and `AccountHold`). `shutdown.py` holds the parser, `resolve_sessions`, status and the launch checks; `settle.py` holds the work behind the verbs and the conductor (`settle.conduct`), which the `conduct <login>` verb only calls.
  - `scripts/message/sessions.py`: `sessions_dir()` is `$NOTIFIER_SESSIONS_DIR` or `~/.claude/sessions`.
  - `scripts/message/send.py`: `relay()` runs a headless `claude -p` with `CLAUDE_*` stripped, so it runs on `~/.claude`'s account. Where `~/.config/claude/oauth-token.gpg` exists (the Mac; natedev has none), it sets `CLAUDE_CODE_OAUTH_TOKEN` from that file and runs on that token's account.
  - `scripts/delegate/remove_workers.py` (`list_sessions()` and `main()`) and `scripts/delegate/implement.sh` (`claude_session()` in its inline Python): `claude agents --json` and `claude rm` act on the caller's config dir.
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
- **Lint:** `basedpyright <files>`: a pass ends `0 errors, 0 warnings, 0 notes`. Lint once after the worker's edits; on failure, fix what it names and lint once more.
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
| G2 | Phase 7 | the user | the user runs Phase 7's two open checks with the director: the spare `~/.claude1` passing `check`, and messages across machines and accounts. The cutover, its setups, logins and role writes are done and not replayed |
| G3 | Phase 9 | the clock | 7 days after Phase 25's live proof |
| G4 | Phase 25 | the user | lifted 2026-10-15: the user cancelled the desk visit, and the unit director ran the proof on throwaway probes |

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

- `scripts/production/add_unit.py` resolves `accounts.role_account("unit")` once per launch and passes the selection to `launch_session`. A named account must pass `accounts.check(label)`, else exit 2: `add_unit: unit account <label>: <first problem>; /showrunner:accounts`. The launch guard (`launch_permission`) runs with `CLAUDE_CONFIG_DIR` set to that account's dir; `launch_environment` strips `CLAUDE_*`. Since herdr, `launch_session` puts `named_account_environment(selected.dir)` (a targeted resume: `accounts.prepared_account`'s variables) and `ENABLE_TOOL_SEARCH=true` on the unit's herdr pane through `open_pane`. With no `config/accounts.json` the prefix is empty and the guard env is unchanged.
- `accounts.account_prefix(selected: AccountSelection) -> str` renders the env prefix for a selection (empty for the default account); `launch_prefix(role)` is `account_prefix(role_account(role))`.
- The manual unit resume in `commands/showrunner/produce.md` runs `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py env unit` first, stops on a nonzero exit, and puts its output verbatim before both `shutdown.py launch-blocked --restart-of <id>` and `claude --resume <id> …`; no `eval`.
- Shutdown inventory reads each session's account and config dir together at one boundary (`AttributedSessionConfiguration`) and stores the dir as `config_dir` on the session snapshot, absent meaning `~/.claude`; an unreadable environment leaves the session unattributed. The shutdown record holds no second copy: the dir lives only on the snapshot (`SessionShutdown.session`, since shutdown moved to one record per session), and an old-format record's entry-level key is dropped when it is read. `restart.py` puts `CLAUDE_CONFIG_DIR=<quoted dir> ` before `ENABLE_TOOL_SEARCH=true` for a non-unit resume whose dir is not `~/.claude`; units relaunch through `add_unit.py` on the current role.
- Claude workers inherit the owner's `CLAUDE_CONFIG_DIR`; `remove_workers.py` sets none, and its tests pin the inheritance.
- `settings.json` allows `Edit(~/.claude1/**)`…`Edit(~/.claude3/**)` beside `Edit(~/.claude/**)` and lists `~/.claude1`…`~/.claude3` in sandbox `allowWrite`, since a session on account N writes memory under `~/.claudeN/projects/…`. This change reaches main only with the user's go.

**Files:**
- `scripts/accounts/accounts.py` — `account_prefix`.
- `scripts/production/add_unit.py` — unit-account check, guard env, launch prefix; puts the repo root on `sys.path`.
- `scripts/production/test_add_unit.py` — prefix, refusal, held named account, role change after readiness, manual resume.
- `scripts/shutdown/inventory.py` — `config_dir` capture on the session snapshot.
- `scripts/shutdown/shutdown_record.py` — ignores an obsolete entry-level `config_dir`.
- `scripts/shutdown/restart.py` — prefix for non-unit resumes.
- `scripts/shutdown/test_inventory.py`, `test_record.py`, `test_restart.py` — capture, round trip, prefix.
- `scripts/delegate/test_remove_workers.py` — owner's dir inherited.
- `commands/showrunner/produce.md` — manual unit resume.
- `settings.json` — Edit allow and sandbox `allowWrite` for the account dirs.
- `pyrightconfig.json` — the scripts/production environment adds `"."` so `scripts.accounts` resolves.
- `docs/as-built/shutdown-and-restart.md` — config dir on the snapshot and the restart rule.

**Binds later work:** add_unit resolves the unit role once per launch, refuses a named account failing `accounts.check` with exit 2 `add_unit: unit account <label>: <first problem>; /showrunner:accounts`, and asks the launch guard on that account's dir, so after a role change a unit relaunched through add_unit lands on and is judged against the new account (the swap of the unit account relies on this). A rule added to `accounts.check` reaches the unit launch with no change to add_unit. `account_prefix(selected)` and the `accounts.py env <role>` verb render the account prefix for any launch, including an `exec` verb. Restart restores a non-unit session on its recorded `config_dir`. `shutdown.py launch-blocked` with no account argument judges the default account (`CLAUDE_*` stripped), so a swap-time shutdown check passes the target dir. The add_unit refusal is user-actionable; the user's guide covers it under "When a unit will not start".

**Gotchas:** a check about a named account through `shutdown.py launch-blocked` must name the account or set its dir, or it judges the default one; a held named unit account refuses with `…; /shutdown restart first`. Code that imports `scripts.accounts` from `scripts/production` needs the repo root on `sys.path`.

**Ruled out:** a second `config_dir` on the shutdown record beside the session snapshot (settle replaces the snapshot, so a second copy goes stale); an explicit `CLAUDE_CONFIG_DIR` in `remove_workers.py` (inheritance already carries it).

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

**Gotchas:** `account.py`, `agent_accounts.py` and `note_file.py` never import `scripts.accounts`; lazy and `TYPE_CHECKING` imports both count as cycle edges, and a file-list lint misses the cycle. A dir label and its login's note label can differ. A role change is not a recovery; treating it as one erases the acknowledgement. Per-note role lookups straddle swaps, hence one selection per operation. Nightly launches through `add_unit.py`, so its pane carries the unit account's environment.

**Ruled out:** consolidating the readings path's three homes, since it reaches the usage collector (`scripts/usage/collect.py`, `scripts/usage/outcomes.py`), outside the accounts scripts.

### Phase 4 — One-command swap of the unit account  · status: done

#### As-built

- `accounts.py swap unit <label|default> [--dry-run]` is `swap(role, label, *, dry_run=False) -> SwapOutcome` (`SwapDryRun | SwapStarted | SwapRolledBack`). It resolves `claude_account_selection()` once and names the old account by its note label throughout. It reaches shutdown only by subprocess (`shutdown.py status <old> --json`, then `shutdown.py down <old> --restart`). Before any write it refuses an unreadable current unit account (`accounts: current unit account identity is unreadable`) or default identity, the default login named by label (`<label> is the default account; use default`), a target missing from the checked selection (its first `check` problem), and a target already the unit account. It also refuses any old-account session that would not move (`accounts: cannot swap <old>: <machine> <kind> <name> would not move`): only units on this machine and workers owned by those units' directors move; every peer-machine session, other kind, unowned worker, unattributed session or unavailable inventory refuses.
- Otherwise swap writes the role (`_write_unit_role` keeps a `PreviousUnitRole`), then runs `down`. A nonzero `down`, or a failed spawn, restores the exact previous `accounts.json` (an absent file included), appends a compensating role-history row, and exits with `down`'s code. The CLI prints `down`'s output, then `units come back on <target> when <old> is down` or `unit account unchanged: still <old>`. `--dry-run` prints the `set-role` and `down … --restart` argv and writes nothing.
- `set_role` resolves the selected dir's identity (`~/.claude` for `default`) and refuses an unreadable one, writing nothing. `RoleChange` extends `LegacyRoleChange` with `account_label`, the note label at write time; `_unit_role_history` uses it and resolves a legacy row through the dir's current identity.
- Shutdown moved to one record per session after this phase; this bullet and the next two describe it as it is now. `shutdown.py down --restart [--only NAMES]` stores `then: restart` on every `SessionShutdown` record the request adds, and on the `AccountHold` of an account-wide request; the peer gets `down <login> --here --request-id <id> --restart` and conducts its own records. `record.py` holds `ShutdownContinuation = Literal["stop", "restart"]`; `then` is required on both types, and only the old-format record the converter reads may lack it (read as `stop`). An external requester gets the result but holds no readiness. The conductor (`settle.conduct`) relaunches each session once its own record has stopped, without waiting for other sessions or the peer.
- `report_stop` sends the requester `<label> is back` when every record restarted, `<label> is down` with the `/shutdown restart` line for a stop-only request, `<label> is not fully back` with `settle.shutdown_command("restart", <label>)` when a restore failed, and `<label>: stop partial` with `/shutdown now retries the failed stop` when a stop is incomplete.
- A repeated `down` adds only the sessions that have no record (`<name>: added`). A session that has one prints `<name>: already in progress` and keeps its `then`, `hold`, `alerts`, `request_id` and `requested_by`; a conductor starts either way. A failed stop is retried by `/shutdown now [--only <name>]`: it sets `force: "now"` and the conductor retries. `stop._retry_owner_cleanup` retries the Codex-server and unit-session (tmux or herdr) cleanup left over on an owner already stopped.
- `commands/accounts.md` is `/showrunner:accounts [status | setup "claude N" | swap unit "claude N" | swap unit default]`: it runs the verb and shows its output unchanged; switching the default account remains `/login` in a `~/.claude` session.

**Files:**
- `scripts/accounts/accounts.py` — `swap`, its outcome and inventory types, `set_role` identity check, `account_label` on role history.
- `scripts/accounts/test_accounts.py` — swap refusals, dry run, rollback, peer refusal, one selection through write and shutdown, burn attribution across logout and re-login.
- `scripts/shutdown/shutdown_record.py` — `then` on `SessionShutdown` and `AccountHold`, `ShutdownContinuation`.
- `scripts/shutdown/settle.py` — `begin`/`down` with `then`, `report_stop`, `shutdown_command`, `alert_user`.
- `scripts/shutdown/shutdown.py` — `down --restart`.
- `scripts/shutdown/stop.py` — `_retry_owner_cleanup`.
- `scripts/shutdown/test_shutdown_record.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_stop.py` — the above.
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
- Aliases `claude1`, `claude2` and `claude3`, each `"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude N'"`. The alias text lives in natedev's `/etc/nixos` `modules/linux/shell.nix` under `programs.zsh.shellAliases`; committing and rebuilding are outside this repo, and the Mac has none. Plain `claude` stays `~/.claude`'s account.
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

**Goal:** close the cutover's two open checks: the spare `~/.claude1` passing `check`, and messages reaching each unit on this machine and across machines and accounts. The cutover and the ten unit moves are done (T0 and the moves below); the group model ("Two account groups") replaced the `set-role unit` steps, so none of them is replayed.

**Spec:** the director runs it with the user.

1. `accounts.py check claude1`. It passed on 2026-10-15: `~/.claude1` was set up and logged in before "Live proof of the moves", whose moves restarted sessions there. Only if it fails, the user repairs it per the guide's "Log out and in" (`accounts.py setup claude1` when the folder is missing, then `/login`), and `check` passes again.
2. The showrunner reaches each unit by message. Then `send.py` across machines and accounts: a unit on the units account and a session on the Mac each reach the other, and each reply comes back.

**Files:**

- `docs/plans/build-followups-two-accounts.md` — the results.

**Workers:** nothing splits: the unit director runs both checks with the user.

**Acceptance gate:** `accounts.py check claude1` passes; every message in item 2 and its reply arrive; the results are written here.

#### Results so far (2026-10-11)

- **T0: 2026-10-11 16:27 PDT**, when the last unit director resumed on claude 3. The default account is claude 2 (`browses31_perks@icloud.com`); the unit account is claude 3 (`rubrics.dogwood-1f@icloud.com`); `accounts.py role unit` prints `claude 3`; `threshold_percent` is 2 (`~/.claude/config/showrunners.json`).
- **Moves, all on the user's word (times from the showrunner):** mul_add by hand at about 15:1x PDT; phase-tables at 16:18 PDT, by hand after its restart failed on a plan-path mismatch (routed to the shutdown unit); then builder-report, feature-log, model-study, screenshot, showrunner-fixer, shutdown, test-threads and two-accounts through `shutdown.py down --only … --restart`, 16:21–16:27 PDT. two-accounts stopped at 16:21 and resumed at 16:24 with its conversation intact.
- **Checked after the moves:** `account.py --pid` shows claude 3 for all ten unit directors' live processes. Showrunners stay on claude 2.
- **Seen from claude 3:** SendMessage to the bare name `natedev` is refused as ambiguous, because that account's Remote Control list also holds an offline `natedev` row from an earlier session of that name. Fixed 2026-10-11 by sending to the session's address on this machine; see the section on it below.
- **Still open:** item 1's `check claude1` at the start (it passed 2026-10-15); item 2's check that the showrunner reaches each unit, and the `send.py` check across machines and accounts.

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

- `accounts.py move <session id | name> <claude N | default> [--why '<text>']` (also `/showrunner:accounts move`) finds the live session in this machine's registry, reads its account from its process's config dir, and returns `MoveStarted | MoveStartFailed | MoveRefused`. Before any change it refuses: no live session here, a name two sessions share (lists ids), a worker, a target that fails `check`, the session's own login, the default login's label or dir (`use default`), or an open shutdown record. Otherwise it runs `shutdown.py down '<current label>' --here --only <id> --restart --to '<target>' --why '<why>'` (default why `<old label> is out of usage`); a session may move itself.
- `accounts.py move showrunners --from '<label>' --to '<claudeN>' [--why] [--dry-run]` returns `ShowrunnersMoveStarted | ShowrunnersMoveStartFailed | ShowrunnersMoveDryRun | MoveRefused`. It selects every live inventory `showrunner` on `--from`'s login, refuses the whole command on a bad target or none found, skips one with an open record, and runs one `down` over every selected id. `--dry-run` prints the selection, the `down` line and each resume line.
- `down --to/--why` stores `target`, `source_label` and `why` on each new `SessionShutdown`; an existing record never gains them; the peer `down` carries both. A record with no target resumes on the account it ran on.
- Restart relaunches a targeted record through `accounts.py exec '<target>'` with the usual resume arguments plus one moved-not-stopped note from `restart.account_move_note` (moved from, to, because; an account move, not a stop; continue). A unit continues `/unit:direct`; a showrunner gets `/showrunner:produce <doc> resume` with the note. A herdr session resumes in its recorded pane and a tmux session in a new herdr session named after it; a unit resumes through `add_unit.py --resume --account '<target>'`; a Ghostty or zed session is closed at stop and resumes in a new herdr session, with a window attached to it through the supervisor or `_herdr_window`; an unknown host prints the resume line once before the stop, and restart reports `manual restart <name>: <line>`. A moved session keeps its name, so `send.py` reaches it; its Remote Control registers on the target account.
- `exec default` drops `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_OAUTH_TOKEN`. `swap unit` is `set-role unit` plus `down '<old>' --restart --to '<new>'` and refuses a unit with an open record; `swap seats` is unchanged.
- `add_unit.py` stores `@claude_config_dir` and `@claude_model` on each unit's tmux session and reads the unit role only for a new unit. An untargeted `--resume` uses the shutdown record's target, then those options, else refuses naming `--account`; a recorded dir outside the account farm is kept (`RetainedUnitAccount`), a missing one is refused; the recorded model goes through `--model`. `produce.md`'s manual unit resume runs `add_unit.py --resume`. Superseded by herdr-adoption Phase 7 (`docs/as-built/herdr-adoption.md`): `add_unit.py` launches every unit in herdr; only `--adopt` writes `@claude_config_dir`, on the adopted tmux session, and nothing reads it; `@claude_model` is read only for a unit still in a tmux place; every unit resume takes `accounts.role_account("unit")`, and a herdr unit its transcript's model.

**Files:**
- `scripts/accounts/accounts.py` — `move`, `move_showrunners`, `_move_target`, `exec default`, swap through `down --to`; `test_accounts.py`.
- `scripts/shutdown/shutdown.py`, `settle.py`, `record.py`, `restart.py`, `stop.py` — `--to`/`--why`, the record fields, resume through `exec`, the moved note; `test_account_move.py` and the inventory, restart, settle, shutdown_cli and stop tests.
- `scripts/production/add_unit.py`, `test_add_unit.py` — `--account`, the tmux options (since herdr only `--adopt` writes one), the untargeted-resume refusal.
- `commands/showrunner/produce.md` — unit resume through `add_unit.py --resume`.
- `docs/accounts.md` ("Move a session"), `commands/showrunner/accounts.md`, `docs/as-built/shutdown-and-restart.md`; `pyrightconfig.json` — shutdown extraPaths include the repo root.

**Binds later work:** "Claude sessions move on a refusal", "An account picked by room, not by role" and "Readout of the moves" call `accounts.move()` / `down --to`. Removing the unit role takes `swap unit` with it, but `exec unit` stays. The readout counts `SessionShutdown` records with a `target` as moves.

**Gotchas:**
- Live Ghostty titles carry a status glyph (`✳ natedev`) and the shell retitles the window after exit.
- A session that already has a shutdown record is refused: a repeated `down` keeps the record as it was, so no target would be stored.
- A refused herdr, tmux or tmux-unit session already enters "held by usage limit" in shutdown from its screen (settle.py `USAGE_LIMIT_PATTERNS`) and stops once idle; Ghostty/zed sessions have no screen read.
- Transcripts record a Claude refusal for every host as an `assistant` entry with `error: "rate_limit"`, `isApiErrorMessage: true`, `apiErrorStatus: 429`, model `<synthetic>`, text "You've hit your session limit · resets …" (`session` = 5-hour, `weekly` = weekly).
- The 2-minute timer (`agent-sessions-snapshot`, nixos) runs `~/.claude/scripts/whoami/agent_notes.py` with no arguments, so a watcher hooked there needs no nixos change.

**Ruled out:** tracking and closing the old Ghostty window by title — titles change.

**Live check outstanding (with the user):** a scratch herdr probe moved claude 2 → claude 3 → `default` keeps its earlier turns, shows the target in its status line and `account.py --pid`, and answers through `send.py`; an unknown-host probe resumes from its one printed line; `move showrunners --from 'claude 2' --to 'claude 3'` brings each showrunner back in its own window on its own desktop with no old window left; `~/.claude/.credentials.json` is unchanged.

### Phase 9 — Readout of the moves  · status: done · gated by G3

**Depends on:** Phase 25

#### As-built

- The unit director wrote the readout below from the natedev and Mac records for the seven days after Phase 25's live proof (G3). It was due Thu 10-22 11:47 AST and was written Fri 10-23. The Mac has no `moves.jsonl`.

#### Readout (2026-10-23, written from the records)

**Window.** T0 = 2026-10-15 11:47 AST to T0 + 7 d = 2026-10-22 11:47 AST. All times are AST (UTC-4). EDT has the same offset in this window, so an EDT time in a record is the same clock time here.

**Accounts.** Named by label only: claude1, claude2, claude3, claude4, codex1, codex2. codex3 shows first on 2026-10-23, after the window, and is not in the counts.

**Sources read.**
- natedev: `moves.read_journal()` from this worktree (441 entries in the window, 548 in all, no torn line). Shutdown records through `record.all_session_records()` and `record.restarted_history()`, plus a read of the other `history/` records (254 records in the window). natedev `roles.jsonl` through `worker_names.role_history_lines` (no legacy `seats` rows), `groups.json`, `config/accounts.json` `home_cutovers`, `readings.jsonl`.
- Mac (read-only over ssh): no `moves.jsonl`, so `read_journal()` returns nothing. 13 shutdown records, none with a `target`: they are stops and same-account restarts, not moves. `roles.jsonl` (4 lines, all before Phase 40), `groups.json`, `cutover-home.json` (`complete`, at 2026-10-16 10:17 AST). The Mac keeps no `readings.jsonl`.
- Retained runner output: natedev's user journal (`agent-sessions-snapshot` lines). Received messages: Claude transcripts in `~/.claude/projects`. Codex threads: rollout files of each moved worker's `worker_id`.
- When each later phase reached natedev's live checkout (from the `~/.claude` reflog): Phase 55 10-17 02:26, 56 03:41, 58 13:18, 59 14:53, 60 and 61 19:04, 63 20:10, 64 20:40, 62 21:14, 67 22:18, 69 10-18 00:11, 70 01:36, 71 03:00.

**Unresolved homes.** No entry in the window reads `home account unresolved`. No row needed the Mac `cutover-home.json` fallback, because the Mac journalled no move. The `account unknown` row is empty.

##### Moves

Claude sessions, all on natedev. There are 17 transitions with a `requested`, plus 4 role-only selection changes.

| Cause | Transitions | Sessions confirmed | Not completed |
| --- | --- | --- | --- |
| by hand, group | 7: units ecd0c49b, 32635f47, 457fbdea, b7009899, 1a0c455b, 8724b034; showrunners a512a0e1 | 2 + 1 + 35 + 39 + 35 + 41 + 4 = 157 | 3 (one each in 457fbdea, 1a0c455b, 8724b034; these three are partial) |
| by hand, one session | 5: f11f007a, 85d6cfc7 (claude4 → claude1, 10-15 12:34 and 12:36), 160fdfb8 (postmaster claude2 → claude1, 10-15 23:05, cutover), c2d4179a (claude2 → claude3, live check, 10-16 21:24), d8db9f0f (claude1 → claude3, 10-20 11:05) | 5 | 0 |
| configured rule `auto: 842eb99c…` | 1: showrunners 6b55cb99, claude1 → claude2, 10-17 11:49 | 4 | 0 |
| configured rule `auto: 0e635c70…` | 1: showrunners 4ae58cb6, claude4 → claude2, 10-17 14:32 | 4 | 0 |
| configured rule `auto: 3e4088c0…` | 1: showrunners 126db1b2, claude2 → claude3, 10-19 07:25 | 4 | 0 |
| weekly refusal | 2: 84e723e9 (claude3 → claude1, 10-16 21:31); 5db6839b (`ups`, claude1 → claude3, 10-22 08:18) | 1 | 1 (5db6839b) |
| 5-hour refusal, unknown-window refusal, credits off | 0 | 0 | 0 |

- Total: 175 sessions confirmed. Each confirmed session has a `restarted` record in `history/` under the same request. 4 moves did not complete:
  - berth-ceiling (457fbdea): not trusted on claude2.
  - startup (1a0c455b): first `failed` at 10-19 08:21, reason `start agent … timed out waiting for agent startup`. Its record now reads `restart prompt not submitted`. The first journal line counts.
  - trunk (8724b034): `restart prompt not submitted` at 10-21 22:01.
  - ups (5db6839b): `pane still holds a live agent`. Its record stays in `sessions/` as `restart failed`.
- Role-only selection changes, counted once each:
  - natedev: units e8abe283 (claude4 → claude4, 10-15 17:33).
  - Mac, before Phase 40, all by hand: showrunners 04cdb461 (claude2, 10-16 08:56), showrunners bd86a33e (claude1, 10-16 11:00), units 39bba61a (claude4, 10-16 11:00).
- The roles line of b7009899 is at 10-18 11:34, but its `requested` lines are at 16:08. At 11:34 there were 39 standalone `failed` lines (`GitHub keys are cold`), which are refused attempts. All 39 sessions then confirmed under the same move id, and each counts once, as confirmed. In 457fbdea, review failed twice (`herdr session review is already live`) and then confirmed: it counts once, as confirmed.
- Other refused attempts: 9600591e has 2 standalone `failed`, `cancelled` lines (dashboard, tile-fixes, 10-15 17:23). They close a request made before the window.
- No Mac move exists, so no auto member has missing peer evidence. No peer CLI rejection was found.

**Prompt submission for confirmed moves made before Phase 60 reached natedev (10-17 19:04).** This covers 11 transitions and 55 sessions: f11f007a, 85d6cfc7, ecd0c49b, 160fdfb8, 32635f47, 457fbdea, c2d4179a, 84e723e9, 6b55cb99, a512a0e1, 4ae58cb6. Each is a completed move, but its submission is not proven. The 10-17 check recorded in this plan found the prompt unsent in 14 sessions of 457fbdea, 2 of 6b55cb99 and 1 of c2d4179a. Every one of the 55 sessions has a later non-meta user turn, so resumed work is shown. That turn does not prove the move's own prompt was sent:
- 41 sessions took a turn within 60 min.
- In 12 sessions of 457fbdea the first later turn came 1.2 to 29 h after the move.
- Two showrunner sessions took their first later turn after 65 and 177 min. It was often a `You are now …-unit` prompt from a later message.

No `confirmed` in the window was written by reconciliation between Phase 60 and Phase 63. Every move after 10-18 03:00 ran with Phases 60 to 71 on natedev.

**Failed moves and their later sending.**
- trunk (8724b034, after Phase 64, 69 and 70): a failed move. Its text sent later is unknown: the later turns are task notifications, and none holds the prompt. The journal reason does not name the path that lost it.
- startup: a `You are now startup-polish-unit …` turn arrived at 10-19 09:07. The move stays failed.
- berth-ceiling and ups: no later user turn.

**Restart failures that are not moves** (no `target`):
- Showrunner restarts before Phase 70: cargo-liner-style (294cac84) failed at 10-18 00:40 and cargo-liner (4dbdb78e) failed at 00:46. Each was accepted: a `<command-name>/showrunner:produce</command-name>` row shows it at 00:38:44 and 00:45:17.
- trunk unit restore: failed 10-18 00:24.
- cargo-liner-style and cargo-liner records: failed again on 10-18 18:37, after Phase 71.

**Sessions blocked by an open `restart failed` record before Phase 71.**
- cargo-liner-style: 10-18 00:40 to 01:46, closed by hand with `forget`.
- cargo-liner: 00:46 to 01:46, closed the same way.
- trunk: from 00:24, end unknown. At the latest, a move could close it from 03:00, when Phase 71 reached natedev.

No move was journalled for these sessions in those spans. Read the spans as blocked, not as room or as no refusal.

**Codex workers.** There is 1 transition: c9d1f08e, a weekly refusal, codex1 → codex2, `requested` at 10-19 01:45:42, reset 10-24 16:40.
- 18 workers are confirmed (`run`, `worker`) from 01:45:58 to 01:49:37, with 0 failed. It is not partial.
- This is the proof of the workers move. On every one of the 18 threads, a turn started at the second of its `confirmed`, and each turn reached `task_complete` within 0 to 3 min. All 18 continued.
- Whether other workers were skipped because their run had ended is not journalled. Their eligibility at that time is unknown.
- Hand swaps, from `roles.jsonl` only:
  - natedev: codex2 → codex1, 10-15 22:32 (before Phase 54, no `why`).
  - Mac: codex2 → codex1, 10-16 11:00.
  - natedev: codex2 → codex1, 10-21 00:26.
- No test-fixture `why` is in `roles.jsonl`. No credits-off request and no workers next-list entry ran.

**Codex commands that exited after a refusal, with the rerun handed to the director.** None is shown. No `quota rerun <worker> -> <session>: sent` or `not delivered` line survives in task output or run dirs. No director transcript holds a received `Codex refused work for quota. Worker … stopped` message. Every match was plan or code text. Roster state is not counted. So the count is 0 shown, any others unknown, and no rerun of finished work is shown.

**Routing to Claude.** There is 1 routing: `routed to claude`, fallback_at 10-19 01:45:50, owner codex1, cause refusal. It moved delegate, cli, fix, team_review, api_review and module_review. `routed back` came at 01:47:25 with all six restored and none kept: a completed recovery after 1 min 35 s. There are 0 `routing not restored` and 0 open routings.

##### Waits

| Tool | Window | Count | Duration |
| --- | --- | --- | --- |
| Claude (queued units group, Phase 58) | weekly, reset 10-26 02:00 | 17, from 10-21 18:46 to 20:40 | Unknown. No `wait ended` was written. |
| Claude | five_hour | 0 | |
| Codex | any | 0 | |

All 17 sessions were on claude1. Each had its own `waited` line, matching runner lines `<name>: refused; wait until 2026-10-26T02:00`. Later, 16 of them confirmed in the hand move 8724b034 (10-21 21:54), and trunk failed there. That move is a separate transition. It does not close a wait in the journal.

**Blocked next-list head.** There is 1: `units 1 → claude4 due, claude4 has no fresh reading since 09:05`.
- Runner output (user journal) at 10-21 18:14:01.
- Received at 18:13:52 by the natedev, cargo-liner, cargo-liner-style and hana showrunners and by two other sessions.
- Relayed again by the cargo-liner showrunner at 21:39 and the hana showrunner at 21:53.
- It ended with the hand move 8724b034 at 21:54, 3 h 40 min after the first notice. When it started before the notice is unknown.
- A 10-21 00:43 `workers 1 → …` set of lines was a test's output and is not counted.
- The 10-17 11:49 move that skipped the claude4 head ran before Phase 58 reached natedev.

**Auto-move holds.**
- 842eb99c: mode, minutes and delay are unknown. The runner line at 11:49:30 shows only the move.
- 0e635c70: a fixed 92-minute hold, from `next` output kept in the natedev showrunner transcript at 10-17 12:16. That output was printed before Phase 67, so it is not a started row. The start and the delay from the first low reading are unknown.
- 3e4088c0: a fixed 13-minute hold, from `next` output at 10-17 13:18, before Phases 62 and 67. No started or `hold over` row for it survives. Its mode at the move (10-19 07:25), its minutes and its delay are unknown.

##### No eligible destination

There is 1 interval: workers, move 8f7fe9ef, `no room` at 10-19 01:45:47, earliest reset 10-26 01:46:06, `room again` (codex2) at 01:47:25. It lasted 1 min 38 s. The union is the same 1 min 38 s. There are no Claude intervals and no unresolved ends. The readings at 01:45:20 and 01:47:22 show codex1 at 0% and codex2 at 100%, so not every account was exhausted.

##### Burn and coverage

Readings start at 10-15 22:10, because the eight-day retention has dropped older ones. So the first 10.4 h of the window are unobserved for every account. A gap is two readings of one account more than 10 min apart.

| Account | Observed days of 7 | Long gaps (AST) |
| --- | --- | --- |
| claude1 | 6.46 | 10-15 11:47 to 22:10; plus 10 gaps of 10–30 min (154 min) |
| claude2 | 3.56 | 10-15 11:47 to 22:10; 10-16 01:53 to 19:07; 10-19 10:32 to 16:36; 10-20 00:33 to 10-21 01:24; 10-21 09:21 to 10-22 08:32 |
| claude3 | 4.65 | 10-15 11:47 to 10-16 21:31; 10-17 05:29 to 09:13; 10-17 17:13 to 10-18 11:36 |
| claude4 | 2.64 | 10-15 11:47 to 22:10; 10-16 23:43 to 10-17 12:16; 10-17 20:15 to 10-19 16:36; 10-20 00:33 to 10-21 01:07; 10-21 09:05 to 21:56 |
| codex1 | 6.56 | 10-15 11:47 to 22:10 |
| codex2 | 6.50 | 10-15 11:47 to 22:10; 10-16 11:53 to 13:17 |

Points per day are from `accounts.py burn`, run once per AST day with `-04:00` edges. The first and last days are partial. `*` marks a day whose points spread across a gap above.

| Day | claude1 | claude2 | claude3 | claude4 | codex1 | codex2 |
| --- | --- | --- | --- | --- | --- | --- |
| 10-15 from 11:47 | 0.4* | 0* | — | 5* | 4* | 1* |
| 10-16 | 8.6 | 18* | 0* | 47* | 66 | 0* |
| 10-17 | 2 | 49 | 0* | 1* | 57 | 0 |
| 10-18 | 0 | 30 | 22* | 0* | 67 | 0 |
| 10-19 | 30 | 2* | 20 | 0* | 6 | 48 |
| 10-20 | 45 | 0* | 9 | 0* | 0 | 102.2 |
| 10-21 | 25 | 0* | 7 | 7* | 54 | 1.8 |
| 10-22 to 11:47 | 0 | 0* | 3 | 15 | 21 | 0 |

**Burn per group interval** comes from `accounts.burn` over each interval of the natedev `group` lines. "Alone" means the other group was on another account. Ad hoc sessions are not tracked.

| Interval (AST) | Account | Group | Points |
| --- | --- | --- | --- |
| 10-15 11:47 to 10-16 19:05 | claude4 | units alone | 52 (10.4 h unobserved) |
| 10-15 11:47 to 10-17 11:48 | claude1 | showrunners alone | 11 (10.4 h unobserved) |
| 10-16 19:05 to 10-17 11:48 | claude2 | units alone | 43 |
| 10-17 11:48 to 12:16 | claude2 | shared | 1 |
| 10-17 12:16 to 14:32 | claude2 / claude4 | units alone / showrunners alone | 6 / 1 |
| 10-17 14:32 to 10-18 11:34 | claude2 | shared | 37.9 |
| 10-18 11:34 to 10-19 07:25 | claude3 / claude2 | units alone / showrunners alone | 35 / 11.1 |
| 10-19 07:25 to 08:10 | claude3 | shared | 2 |
| 10-19 08:10 to 10-21 21:54 | claude1 / claude3 | units alone / showrunners alone | 100 / 19 |
| 10-21 21:54 to 10-22 11:47 | claude4 / claude3 | units alone / showrunners alone | 22 / 5 |

Workers: codex2 got 1 point to 10-15 22:32, with 10.4 h unobserved. codex1 got 200 points from 10-15 22:32 to 10-19 01:45. codex2 got 152 points from 10-19 01:45 to 10-21 00:26. codex1 got 74.5 points from 10-21 00:26 to the window end. On the Mac, showrunners were on claude2 from 10-16 08:56 to 11:00. That is a Mac-only selection, and the account readings cannot split it.

##### What the data could not answer

- The turns before 10-15 22:10.
- Whether a worker was skipped as ended.
- Any Codex rerun handoff.
- Hold start times and delays.
- When the units head began to block.
- When the waits ended.
- When trunk's pre-Phase-71 record was closed.

##### Recommendation

In the full days 10-16 to 10-21, Claude spent about 54 points per day, about 377 per week, against 400 for four accounts. Gaps in the readings make this a low estimate. Only one no-room interval happened, and it lasted 1.6 min. But the week ended with claude1 at its weekly limit, 17 sessions waiting, and the only queued target unreadable for 3 h 40 min. Add one Claude account.

Codex spent about 67 points per day, about 470 per week, against 200 for two accounts. Paid credits covered the rest. codex3 was added after the window. Whether to add more Codex accounts depends on the cost of credits against the cost of an account, and only the user knows that cost.

### Phase 10 — A home per Codex account  · status: done

#### As-built

- `~/.codexN` is the home of `codex N`. `~/.codex` stays the default home, and the workers run on it.
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
- The home in use is this process's `CODEX_HOME`, else `~/.codex`, and four places read it: `agent_accounts.codex_report_in_use` (the `/whoami` `seats` mark and the 5-hour watch input), `agent_accounts.codex_homes` (it adds the home in use to the list), `agent_notes.update_for_selection` (which Codex note is `active`), `accounts.codex_statuses` (the table's `seats`). All four must follow the `seats` role in "One Codex account for all workers, and a swap", with tests for an unset and a conflicting `CODEX_HOME`: the updater runs with no `CODEX_HOME`, so while the readers stay on the process home a swap leaves the old account's note `active` and the alerts on an account no worker runs on.
- A Codex note is `active` only for the login of the home in use, read from that home's sign-in on disk. Quota alerts read `active` notes, so "Alerts per Codex account" gets no alert from a spare.
- `check` has no form for the default home. The swap in "One Codex account for all workers, and a swap" refuses a target that fails `check`, so it needs one.

**Gotchas:**
- Which login the home in use holds comes from the sign-in on disk (`codex_email_on_disk(codex_home())`), never from the live report: a failed live read must change no note's state. Read from the report, a failed read marks every Codex note `inactive` and clears the alert episode of the account the workers run on.
- `codex_homes()` lists directories only; a file or dead link at `~/.codexN` is reported by the `not set up` row and its `check` problems.
- Hand check, codex-cli 0.162.1: Codex writes through a linked `config.toml` and the link stays a link. Checked on 2026-10-12 with codex 1 signed in at `~/.codex1`: the sign-in wrote only that home's `auth.json` (`~/.codex` had none at that moment, after an unrelated `codex login` with no `CODEX_HOME`, and gained none); usage reads under the second home while workers run on the first, once that home's first start has finished (Spec item 8 of the seats-role phase); and at 10:30 PDT the table showed both Codex accounts with numbers read in the last 10 minutes while both workers worked.
- Lint exits 3 with `0 errors, 0 warnings, 0 notes` in this worktree, because `pyrightconfig.json` names an absent `.venv`.

**Ruled out:**
- Linking or copying the sign-in file into a numbered home: two homes on one login break token refresh.
- An empty login set for "the in-use login is unknown": it would mean "no account is in use" and mark every note inactive.
- Splitting "One Codex account for all workers, and a swap" in two: a role the mesh does not follow would make the table wrong.

### Phase 11 — One Codex account for all workers, and a swap  · status: done

#### As-built

- The `seats` role in `~/.claude/config/accounts.json` names the one Codex account every unit's workers run on: `default` (`~/.codex`) or `codex N` (`~/.codexN`); absent means `default`. `accounts.py role seats` prints it. `env seats` prints `CODEX_HOME=<home>` for both kinds, whatever the caller's own `CODEX_HOME` says. A role write keeps the other role. An empty role file, or one holding only `seats`, leaves the Claude role displays off. The account table's Role column shows `seats` on the selected home and `spare` on every other signed-in home. Note activity and the `/whoami` status line follow the selection: a Codex note is active only for the account in the role.
- `set-role seats <label>` writes the role after an on-disk check of the target. `swap seats <label> [--dry-run]` reads the target live, requires `codex_can_work` to answer that it can work, refuses the home already selected, then writes the role and retires the outgoing account's run servers. `_swap_seats(label: str, *, dry_run: bool) -> SeatsSwapOutcome` refuses before `_retire_seats_servers` when the target has no readable sign-in. A refused selection exits 1, writes nothing, and prints one line: `accounts: cannot select <label> (<folder>): <reason>`.
- Types: `accounts.role_account("seats")` returns `CodexSeatsAccount(home, label)`. `SelectableCodexAccount(home, login, label)` is a target whose home identity is checked before usage is read or the role is written. `codex_can_work(report: Report, now: datetime) -> CodexWorkReadiness` in `scripts/whoami/agent_accounts.py` answers `CodexWorkAvailable | CodexWorkExhausted | CodexWorkUnknown`, by allowance or by credits.
- The worker launcher calls `seats_home()` once per operation, starts its run server under that home, and records it as `codex_home` in the run's `ServerRecord` (`mesh_server.json`). A record without `codex_home` means the default home. A server retired by a swap or a new sign-in stays a `RetiredRunServer` until it stops. `agent_exec.sh` runs `codex exec` under the seats home. `codex_daemon` serves freestanding sessions on its own process home, never the role's; with `ROSTER_CODEX_SOCKET` set, `codex_daemon.queue()` talks to that socket itself.
- A worker's conversation moves to the selected account's server when the worker is next used. The launcher first checks that the old server has released it (released, waiting or unknown), then reopens it with the worker's standing instructions. A reopen the server refuses starts a replacement conversation that keeps the standing instructions, the read-only sandbox and the never-ask policy; one board line names the old and new conversation. `send` parks each message for an attached launcher. With no launcher attached, it holds the message for the worker's current roster thread until the next dispatch, which receives it after its prompt. Ended, missing and capacity-exhausted workers still refuse messages. There is no `_load_thread`. `prime`, `fork` and `reset` run on the worker's home; `fork` and `reset` run the release check first and exit 2 with the reason while an old server of the run holds the conversation.
- Home setup: `prepare_codex_home(home: Path) -> CodexHomePreparation` starts a server under a numbered home and waits for `initialize`, up to `CODEX_HOME_PREPARATION_SECONDS` (1200 s). `setup "codex N"` runs it before it prints the sign-in command. A read with no `initialize` answer under a numbered home prints `Codex did not start at <home>; run <setup command> and try again`. `log` is in `CODEX_PRIVATE_NAMES`. `setup`, `docs/accounts.md` and `commands/showrunner/accounts.md` warn that `codex login` without `CODEX_HOME` signs `~/.codex` out and in.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py` — the `seats` role verbs, the swap, the Role column, the home setup; tests include `test_refused_seats_selection_names_the_tool_once`
- `scripts/whoami/agent_accounts.py` (`seats_home`, `codex_home`, `codex_home_display`, `codex_can_work`, `prepare_codex_home`, `user_zone`), `scripts/whoami/agent_notes.py`, `scripts/whoami/whoami.py`, `scripts/whoami/test_agent_notes.py`, `scripts/whoami/test_codex_homes.py` — note activity and the status line follow the seats selection
- `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py` — the worker launcher: home per operation, server records, the move, the release check, parked sends, `prime`, `fork` and `reset`; tests include the `test_reset_and_fork_*` group, `test_replacement_worker_keeps_standing_instructions_and_read_only_policy` and `test_held_send_reopens_on_the_worker_home_with_standing_instructions_before_delivery`
- `scripts/agents/agent_exec.sh`, `scripts/agents/test_agent_exec.sh` — one-shot Codex runs under the seats home; `scripts/message/codex_daemon.py`, `scripts/message/test_codex_daemon.py` — queueing to an explicit socket
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the guide and the command text for the role, the swap and the setup warnings

**Binds later work:** The per-account alerts read the seats account through `accounts.role_account("seats")` (`CodexSeatsAccount`: `home`, `label`; a label is `codex N` or `default`) or `agent_accounts.seats_home()`, and they own the alert surface for the selection. The refusing account for a failed turn comes from the `ServerRecord` (`codex_home`) of the server that ran it; a server retired by a swap is still on record as a `RetiredRunServer` until it stops, and its one outward sign is a board line. `codex_can_work` is the one can-work function: the alerts' swap line uses it as `swap seats` does. The role history holds `unit` and `seats` entries together, and the unit history reader leaves `seats` out of its sums, so a one-week readout counts `unit` assignments only. `swap seats` moves Codex workers; a Claude session moves with `swap unit`. One Codex account serves all workers at a time; no unit has its own. `scripts/agents/codex_mesh.py` and `scripts/agents/test_codex_mesh.py` also hold `prime`, `fork` and `reset` and their tests.

**Gotchas:**
- Not proven live: reopening a real conversation on a second Codex home, and one swap to another account and back while a worker works. That hand check with the user has not run. Real Codex handling of the standing-instruction and permission parameters on a reopen and on a replacement start has run only against test doubles. No `set-role seats` or `swap seats` has run against the real homes; the workers run on the default home, and servers started before this change keep serving while the role is `default` and its sign-in is current.
- Measured on codex-cli 0.162.1 with scratch servers: `thread/resume` with only the thread id continues a conversation under another home when `sessions` is shared, and both homes append to the one rollout file. Writer locks are per home: a second home's server resumes a conversation the first still holds, with no error, and the two then disagree, so the release check comes before every reopen. Each turn counts against the account of the home whose server runs it. With `-c thread_unload_delay_secs=0` an idle conversation unloads 0.04 s after its last connection closes; not measured for a conversation that has run turns. A numbered home has its own Codex state database, and its first `codex app-server` indexes the shared `sessions` before it answers `initialize`; a server closed mid-index leaves the index marked as running for 900 s, and each start in that time prints `state db backfill is running at <home>` and exits unanswered after about 31 s.
- Port numbers are reused across a run's retired servers, so a server is identified by its record, never by its port. A recorded pid that now belongs to an unrelated live process makes the release check answer unknown, the safe direction. A compaction refused because the worker's own conversation still works on the old server returns the live-turn code, and that launch fails.
- `codex_can_work` answers unknown ("credit balance is unavailable"), not used up, for a window at 0 with no credit reading; a swap is refused either way. The user's zone is written as a literal in `accounts.py` (`_iso_time`) and in `quota_alert.py` (`USER_ZONE`), while `agent_accounts.user_zone()` reads the production's zone. Not merged here yet: the user-zone change (build-followups-enh-showrunner 43a14830) replaces both literals with `scripts.lib.user_zone.user_zone()`.

**Ruled out:** replacing a worker's conversation only for some reopen errors (any refused reopen gets a replacement, and every prompt is complete on its own); keeping observed note activity on a machine with one Codex home (activity follows the seats selection everywhere); moving the workers to another Codex account automatically when Codex refuses work (every swap stays the user's call).

### Phase 12 — Alerts per Codex account  · status: done

#### As-built

- Quota alerts, 5-hour holds and the Codex-to-Claude fallback follow the Codex account in the `seats` role. A low or used-up alert names that account and carries `codex_swap_line(readings, now)` when another checked, signed-in home is accepted by `accounts.check_codex_target(label, seats_home)` and then by `codex_can_work`: the command (`swap seats default` for the default home, the numbered label otherwise), that account's weekly left and its credits. A spare account raises no low-quota alert.
- Weekly episodes are keyed by note name. An episode stores `weekly_reset` and closes once that time is reached or the account recovers, so an acknowledgement stays quiet while its account is spare and after the account is selected again still low.
- One frozen `CodexAlertPassReadings(seats, selected_account, checked_homes)` serves a whole updater run, each checked home a `CodexHomeReading(home, report, observed_at)`. The updater builds it with `codex_alert_pass(home, reports, observed_at)` before it writes notes and hands it to `five_hour.watch`, `quota_alert.alert` and `quota_alert.refresh`; a standalone call defaults to `READ_CURRENT_CODEX_READINGS`, and `resolve_codex_readings` collects an equal value. A reading older than `CODEX_READING_MAX_AGE` (10 minutes), a failed read, or an expired window recommends no swap.
- The worker launcher keeps `CodexServingState = ServingCodexServer | NotBoundCodexServer` on each client, captured before a request is submitted and kept through a server move and a replacement conversation. `ServingCodexServer(port, pid, home, account, started_here)` carries a `RefusingCodexAccount`; `NotBoundCodexServer` answers `UnknownCodexAccount`, so an unknown refusing account is a stated case, never inferred from the current role or from a port. A proven quota refusal is reported once per turn, on a started turn, a followed turn, a parked-message turn and a replacement conversation's turn, by `_report_quota_refusal(name, serving)`, which runs `agent_notes.py blocked`. A failure of `prime`, `fork` or `reset` keeps its serving account, and an unproven failure opens no fallback.
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
- Moving workers to another Codex account automatically when Codex refuses work, because every swap stays the user's call.
- The Mac.

### Phase 13 — A launch that finished its turn ends as done across a seats change  · status: done

#### As-built

After a worker launch's own turn has ended, a move to the selected seats home that answers `WorkerThreadStateUnknown` with `NoWorkerTurnToCleanUp` never fails the launch. This holds at three sites in `_stream_turn` in `scripts/agents/codex_mesh.py`: the two `_move_resident` calls after a `turn/completed` and the one in the ended-unseen branch.

- A launch that is not resident closes the worker's inbox and keeps what is parked, in one `_pending_file` lock (`_close_messages_and_keep_pending(session_dir, name, thread_id) -> int`). With one or more kept it posts `launcher: 1 message held for the next launch: <reason>` (or `<N> messages`) and writes the worker-log line `mesh: <name> cannot move: <reason>; 1 message held for the next launch`. It then finishes as the ended turn deserves: the roster reads `done` or `failed` by the turn's own result, and nothing is sent to the target server.
- A resident launch logs `mesh: <name> cannot move: <reason>; waiting` and keeps serving; its idle ticks try the move again.
- A `WorkerThreadStateUnknown` with `WorkerTurnCleanup` still raises `WorkerMoveUncertain` and cleans up on the target port. `_move_idle_worker` answers `WorkerTurnCleanup(port, thread_id)` for every failure after the target accepted a turn, the pending clear, the final roster write and the lock release included; with no turn submitted a failure still means no cleanup.
- With the worker's end marker present the hold writes, posts and logs nothing.
- The next `follow` reopens the inbox, resumes on the selected home and starts one turn: its own prompt, then the held messages in order.

**Files:**
- `scripts/agents/codex_mesh.py` — the three sites, `_close_messages_and_keep_pending`, and `_move_idle_worker`'s one handler around the move.
- `scripts/agents/test_codex_mesh.py` — twelve tests of the hold, the cleanup routing between two ports, the ended worker and the resident retry.

**Gotchas:** Held messages stay with the thread: a `start` that replaces the thread drops them, a `follow` keeps them. `basedpyright` exits 3 in every checkout because its config names a `.venv` no checkout has; the pass is its last line. Both files are model-study-unit's as well. A real launch in flight across a real role change is not proven by tests; it is the live swap check with the user.

**Ruled out:** failing the launch whenever a move cannot be confirmed (the turn's own result is already known).

### Phase 14 — An account picked by room, not by role  · status: done

#### As-built

- `accounts.pick` chooses among `default` plus checked named accounts (Claude) or checked worker homes (Codex), one candidate per login (`default` first, then lowest number). Order: `keep` while it has room, else soonest weekly reset, then most weekly left, then label. Room is more than 0% left in the weekly and 5-hour windows (Codex: `agent_accounts.codex_can_work`'s shared rule, credits included); there is no minimum.
- Readings come from the usage store through `stored_reading`; a reading older than 10 minutes skips the candidate. An unresolved refusal in `~/.local/state/accounts/refusals.json` excludes the account whatever the saved percentages say.
- Phase 74 removed the account-picking command verb. The `pick` function, its ranking and its reasons remain for automatic moves; `placement --explain` shows the Claude account reasons.
- The unit role is gone: `role_account("unit")`, `role`/`env`/`exec unit` call `pick("claude", now)` and store nothing; a `unit` key in `config/accounts.json` is ignored. The `seats` role is unchanged.
- `add_unit.py`, `add_showrunner.py` and `nightly_review.py` launch on the picked account (no 10% gate) and record `@claude_config_dir`/`@claude_model`. A showrunner resume keeps its account: `--account`, else `--config-dir` (resolved against the invoking cwd), else tmux `@claude_config_dir`, else a one-line refusal; promotion passes the unit's recorded dir. Superseded by herdr-adoption Phase 7: `add_unit.py` and `add_showrunner.py` launch in herdr and record neither option, `nightly_review.py` launches through `add_unit.py`, and a showrunner resume takes the showrunners group account; `--account` or `--config-dir` must name it.
- Whoami, notes and quota alerts cover every login a live session runs on; `unit_swap_line` is a move line with no swap command; the status table marked the ranking's next account with `next`. Phase 74 removed the command verb.

**Files:**
- `scripts/accounts/accounts.py` — `pick`, its types (the verb was removed in Phase 74), `stored_reading`, `role_account`, the status table, `claude_account_selection`.
- `scripts/accounts/refusals.py` — the refusal store and its resolution rule.
- `scripts/production/add_unit.py`, `add_showrunner.py`, `promote_showrunner.py` — launch on the picked account; showrunner resume keeps its account.
- `scripts/nightly_review/nightly_review.py` — launches on the picked account.
- `scripts/whoami/agent_accounts.py`, `agent_notes.py`, `quota_alert.py`, `whoami.py` — `ClaudeAccountsInUse` and its readers.
- `scripts/shutdown/restart.py` — wrote `@claude_config_dir` on a tmux respawn with a target; it now resumes a tmux session in a new herdr session and writes no option.
- `docs/accounts.md`, `commands/showrunner/accounts.md`, `docs/quota_alerts.md` — `pick`, no unit role.

**Binds later work:** "Codex workers move on a refusal" and "Claude sessions move on a refusal" call `accounts.pick(tool, now, *, exclude, keep) -> PickedAccount | NoAccountWithRoom` with `exclude={refusing label}`; `pick` never returns an excluded label, and with no fresh reading anywhere it returns `keep` else `default`, else `NoAccountWithRoom` when readings exist but none has room. Both write refusals through `refusals.py`: `AccountRefusal{tool, login, window: KnownWindow | UnknownWindow, reset: KnownReset | NoResetKnown, at}`, `write_refusal` (locked, atomic, an older refusal never replaces a newer one) and `resolved(refusal, reading, now)`. `accounts.stored_reading(tool, login, now) -> FreshReading | StaleReading | NoReading` is the only path from `StoredUsage` into picker types. `restart.py` resumes a targeted tmux record in a new herdr session (`_herdr_launch`, the target's environment on its pane) and writes no `@claude_config_dir`; restart edits sit beside `_herdr_launch`. `ClaudeAccountsInUse` carries `live_logins` and `live_directories` (a live login on a dir failing `check` still reports). `set-role unit` and `swap unit` refuse with `UNIT_ROLE_GONE`; `exec unit` stays. A Codex move line parallels `unit_swap_line`'s move line.

**Gotchas:** The Mac has no usage timer, so every launch there lands on keep/default; `--explain` says `no fresh readings on this machine; using <label>`. A missing reading is never a refusal: only `refusals.json` excludes an account regardless of readings, because the usage store keeps the last good reading after a failed read. A showrunner resume must never take an unrelated session that shares its name: `add_showrunner.py` refuses when a herdr session of that name is live. A relative `--config-dir` resolves against the invoking cwd, not the script's.

**Ruled out:** refusing a launch when no reading exists (a missing reading is not a refusal); a minimum room threshold (1% left is room); picking on a showrunner resume (a resume keeps its account); any role tying a kind of session to an account.

### Phase 15 — Codex workers move on a refusal  · status: done

#### As-built

- Only a refusal moves workers. `codex_mesh` reports it via `agent_notes.py blocked --window weekly|five_hour|unknown --refused-at <aware ISO>` into `quota_alert.blocked(..., window: KnownWindow | UnknownWindow, refused_at) -> list[str]`.
- `blocked`: a known 5-hour reset in the future and under 20 minutes waits (one `waited` per reset, then `wait ended`); else `accounts.pick("codex", now, exclude={seats label})` and the `swap seats` role write; a failed swap writes `failed` and falls back to Claude; no room writes `no room` + `no_room_line`, runs `switch_to_claude`, later `room again`.
- `switch_back` needs `refusals.resolved` plus allowance, and may move workers to any account with room.
- A refused worker is `continuation_owed`: on resume or after a wait it runs one `Continue where you stopped.` turn before held messages. `confirmed` is written only on a real matching resume.
- `moves.py`: `MoveEntry` union of `Requested/Confirmed/Failed/Waited/WaitEnded/NoRoom/RoomAgainMove` sharing `at, tool, subject, move_id, kind`; `append_move` validates and appends under `flock`; `read_moves()` skips malformed lines.
- `codex_swap_line`: `seats move to <picked> when refused`, or no room until `<reset>`.

**Files:**
- `scripts/accounts/moves.py`, `test_moves.py` — the journal; `refusals.py`, `accounts.py` — readings, pick.
- `scripts/whoami/quota_alert.py`, `agent_notes.py` — decisions, CLI; `scripts/agents/codex_mesh.py` — worker side.
- `docs/quota_alerts.md`, `docs/accounts.md` — automatic Codex moves.

**Binds later work:** store `~/.local/state/accounts/moves.jsonl`, reused by "Claude sessions move on a refusal" (`move_id` = shutdown `request_id`) and read by the move readout. `append_move` dedups only `confirmed`, `wait ended`, `room again`; the caller enforces one `waited` per reset. `requested` is never proof; Codex `confirmed` is keyed `(move_id, run, seat)`. `RequestedMove.reset` is omitted when unknown. `refusals.stored_reading` is the only reading path. "Live proof of the moves" drives `agent_notes.py blocked --window --refused-at`.

**Gotchas:** `accounts.py` never imports `codex_mesh`/`quota_alert` at module level (import cycle). `agent_accounts.user_zone()` reads registered showrunners and spawns `sessions.py`, so tests reaching it point `showrunners.NOTIFIER_STATE_DIR` at an empty dir, and gates rerun under the real home. The `codex_mesh` end-to-end move test needs a 30 s deadline.

**Ruled out:** environment-variable handoff of the refusal window; `stored_reading` in `accounts.py`.

### Phase 16 — Claude sessions move on a refusal  · status: done

#### As-built

- `refusal_watch.py` runs at the end of `agent_notes.py`'s argument-free run (the 2-minute `agent-sessions-snapshot` timer on natedev); a failure prints one stderr line and never fails the notes update. It reads this machine's live top-level sessions (`inventory.py`; workers and sessions with an open shutdown record skipped). A session is refused when its transcript's last entry is an `assistant` entry with `isApiErrorMessage: true` and `error: "rate_limit"`, or when `~/.local/state/accounts/assumed_refusals/<session id>.json` exists. `session limit` text is the 5-hour window, `weekly limit` the weekly one, other text takes `quota_alert._refusal_window`; the reset comes from the stored reading, the time from the entry's `timestamp`.
- A refusal is written to `refusals.py` once per (session id, entry `uuid`); the first observation (`FirstRefusalObservation`) stands in the watch state `~/.local/state/accounts/refusal_watch.json`, and a rescan writes nothing.
- Wait: a known 5-hour reset in the future and under 20 minutes away writes one `waited` per session per reset, then `wait ended`; the next tick on the same entry delivers one `Continue where you stopped.` on the session's own account (`settle.send_message`) and marks the entry `continued` in the watch state, never moving it. A failed delivery prints one line and retries next tick. `continued` is not a `moves.jsonl` kind.
- Move: `accounts.pick("claude", now, exclude=<every login refused this tick>)`, then one `accounts.move_sessions(ids, target, *, why)` per source login, running `shutdown.py down '<from>' --here --only <ids> --restart --to '<to>' --why '<from> refused: <window> limit until <reset>'`. It returns `SessionMoveRequested` (with `request_id`) or `SessionMoveRefused` per session; a refusal before any record exists (cold key, conductor failure, settle refusing) writes `failed` with that reason and no `requested`, retried next tick. `move_showrunners` uses it. `down` writes `requested` (keyed by `request_id`) after the records exist and withdraws them if the conductor fails to launch.
- No room: `no room`, one alert per change in reading values (`left`, `resets`, credit `balance`; never check times) to the configured sessions and running showrunners, then `room again`.
- `restart.py` writes `confirmed` only when the resumed process runs on the target (its config dir, as `account.py --pid` reads it); `timers pending` is still `confirmed`; a live session still on the source is `failed` `still on <source>`. `restart.mark_restart_failed` is the one path that sets a targeted record `restart failed`/`manual restart` and journals `failed`. On natedev herdr, tmux, Ghostty and zed sessions restart themselves, each in a herdr session; `terminal` and `unknown` hosts print the resume command and end `manual restart`.
- `settle.py`: the usage-limit hold also enters `held by usage limit` from a transcript or assumed refusal, on every host (stop rule unchanged); `/shutdown status` shows each moved record's target and why. `/shutdown cancel` calls `refusal_watch.cancelled` before withdrawing the record: `failed` reason `cancelled`, the assumed refusal deleted, and that refusal never moves the session again.
- `--dry-run` prints exactly what a real run would do and changes nothing; `--assume-refused '<session id | name>' weekly|five-hour [--reset-in <minutes>]` writes the assumed file and runs once, `--reset-in` feeding only the wait decision.
- `inventory._model` skips `<synthetic>`, so a moved session resumes on its last real reply's model. `quota_alert.blocked` takes `RefusedAt(at) | RefusalTimeUnknown()`, converted at the `agent_notes.py blocked` argument boundary.
- `/showrunner:accounts status` lists live sessions' accounts, the workers' account and their Codex destination, owed worker continuations (runs whose mesh server is running), and the last 10 `moves.jsonl` entries in the user's zone with each outcome.

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
- `move_sessions` requests `shutdown.py down --here --only <ids> --restart --to <target> --why <why>` without `now`, so `settle.py` stops an idle member with no pause message, sends a busy member `Pause for move: …` and stops it after its `ready --where` mark or once it goes idle after the pause, and tells a unit's showrunner. No group-move code messages, waits or stops a session itself.
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

#### As-built

- Each Codex account note (`codex N.md`) carries `use_credits: on|off`, read only by `agent_accounts.codex_credits_setting(label) -> CodexCreditsSetting` (`CreditsOn | CreditsOff`, frozen dataclasses). Absent reads as on; any other value reads as `CreditsOn(warning)` and the alert pass logs `codex N: use_credits must be on or off; reading it as on`. `agent_notes.apply` rewrites only named keys, so the 2-minute updater keeps the line in place. The setting that counts is the one on the workers' account (`CodexAlertPassReadings.selected_account`).
- Credits make room only where they are on: `codex_can_work(report, now, credits)` and `accounts._codex_reading_can_work(reading, now, credits)`. With `CreditsOff`, `pick` skips a candidate at 0 in either window and `_swap_seats` refuses it (`… is used up and set not to use credits; choose another account or wait for reset`). `pick(..., prefer_weekly_room=True)` orders accounts with included usage in both windows before accounts that need their own paid credits; `accounts.codex_destination_uses_credits(chosen, now)` tells which a destination is.
- The 0% episode lives in `quota_alert._codex_zero_alert`, reached from `_alert_for_saved_readings`, so the timer pass and `/quota_refresh` share it. Weekly 0 with a future reset on the selected `KnownCodexAccount` opens `codex_zero[label]: CodexZeroEpisode {since, weekly_reset, credits, phone, sessions, forecast_at, forecast_phone?, notice: ZeroEpisodeNotice {id, values}, forecast_notice?}` in `quota_alert.json` under `state_file()`'s lock. One catalog push goes to the phone through `tell_user` as a `QuotaNote(name, rendered)` (retried from the saved `notice` each pass while `phone` is `"due"`) and to `recipients(config)` through `deliver_and_record` (stamped before the relays, never resent). Opening closes that account's low-threshold episode silently: at 0 nothing repeats, asks for `/quota_ack` or relays `Quota restored:`. The episode closes silently on its reset, a fresh reading above 0, or the workers leaving that account. The setting is reread each pass: `on` to `off` runs the credits-off path once and stops forecasts; `off` to `on` changes nothing.
- Credits on: workers stay. The opening push is `quota.codex_on_credits` (names the `pick(..., exclude={seats label}, keep=NoKeep())` destination) or `quota.codex_on_credits_last` (no other account: Claude after the credits end); both carry an `ActionTemplate` naming `use_credits: off` in `~/rust/hanadocs/agents/{file}` on `{machine}`. `quota.codex_credits_forecast` goes to the recipients when `now - forecast_at >= 1 h`, the first one also to the phone; it uses `selected_codex_report(...).credit_balance` and `run_out.credits_used`, with run-out at `now + balance / rate` in the user zone, and separate examples for zero use, unlimited, unknown balance and unknown use.
- Credits off: `pick(..., prefer_weekly_room=True)`, one `moves.RequestedMove` with `cause: "credits off"`, then `accounts.swap("seats", label)` and `quota.codex_workers_moved`. No room (one `NoRoomMove`, deduplicated as in `blocked`) or a failed swap (`FailedMove`) calls `switch_to_claude(..., credits_off_hold=CreditsOffHold(until))` in the same pass, `until` being the earliest known future reset, and sends `quota.codex_workers_stay`. `switch_to_claude` returns `CodexWorkSwitch = CodexWorkMovedToClaude | NoWorkOnCodex | CodexWorkSwitchFailed`. `switch_back` keeps a held fallback before `until` while the owner account is still selected and reads 0, unknown, stale or a problem; owner room, the deadline or a change of selected account releases it, and its recovery move also uses `prefer_weekly_room` and `cause: "credits off"`. The hold persists as `credits_off_until`.
- `moves.RequestedMove.cause: NotRequired[Literal["credits off"]]` is valid only on `requested`; a missing `cause` reads as `refusal`, and the moves readout prints it.
- `run_out.read_credit_readings(path) -> dict[str, list[CreditReading]]` and `read_readings` share one parser (`_reading_records`, `StoredReading`) of `readings.jsonl`. `credits_used(readings, label, *, since, until) -> CreditsUsed {used, observed_from, observed_to} | NoCreditReading | UsageUnknown(reason)` starts at the last reading at or before `since`, sums decreases and skips increases (purchases); usage is unknown when the newest reading is over 10 minutes old or the observations cover under 30 minutes of the hour.
- `codex N` names a login: `agent_accounts.codex_account_home(label) -> CodexHomeHoldingLogin | CodexSetupHome` returns the non-symlinked home whose on-disk login matches the note's `login:`, else `~/.codexN`. `seats_home()` and `accounts._codex_account_home` (check, statuses, role read and write, swap) resolve through it; `accounts.codex_setup_home` keeps `setup` on `~/.codexN`; the role file stores the account label. `codex_home_role_label(home)` returns the note label, or the folder (`codex_home_display`) when the login has no note, never `default`. `codex_home_matches_label(home, label)` still accepts `default` for `~/.codex`; `pick`'s exclude and keep and `codex_mesh._requested_worker_move` match through it. `codex_swap_line` and the `blocked` session lines say `workers`.
- `CodexQuotaAccountNote(path, fields, tool="codex")` stands in for an account note in `_zero_open_notice` and `blocked`.

**Files:**
- `scripts/whoami/quota_alert.py` — the zero episode, the one alert, credits on and off, the hourly forecast, `CreditsOffHold`, `CodexWorkSwitch`, `QuotaNote`, `CodexQuotaAccountNote`, workers wording.
- `scripts/whoami/agent_accounts.py` — `codex_credits_setting`, `codex_can_work(..., credits)`, `codex_account_home`, `codex_home_role_label`, `codex_home_matches_label`, `seats_home` by login.
- `scripts/whoami/run_out.py` — `read_credit_readings`, `credits_used`, the shared readings parser.
- `scripts/accounts/accounts.py` — `pick` with the setting and `prefer_weekly_room`, `codex_destination_uses_credits`, `_swap_seats` refusal, `codex N` by login, `codex_setup_home`, `cause` in the readout.
- `scripts/accounts/moves.py` — the `cause` field on `requested`.
- `scripts/agents/codex_mesh.py` — `_requested_worker_move` matches labels through `codex_home_matches_label`.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — the five `quota.codex_*` entries and their approvals.
- `scripts/whoami/test_quota_alert.py`, `test_codex_alert_accounts.py`, `test_run_out.py`, `test_codex_homes.py`, `test_agent_notes.py`; `scripts/accounts/test_accounts.py`, `test_moves.py`; `scripts/agents/test_codex_mesh.py` — tests; every catalog example is rendered through the real alert pass and asserted equal.
- `docs/quota_alerts.md`, `docs/accounts.md`, `commands/showrunner/accounts.md` — the setting, the 0% alert, the forecast, `codex N` by login.

**Binds later work:** "Account notes by number, with role and sessions" renames the note files, so `codex_credits_setting` and `codex_account_home` (both `directory / f"{label}.md"`), the catalog examples' `file` values, the saved `CodexZeroEpisode.notice.values["file"]` and `CodexQuotaAccountNote(Path(label + ".md"), …)` follow the new name, and the two `quota.codex_on_credits*` entries are approved again. "Accounts named by number only" removes the `default` alias in `codex_home_matches_label` and in `pick`'s exclude and keep. "Live proof of the moves" proves the credits-off move, the Claude switch and the hold release live.

**Gotchas:**
- A pending phone retry renders the saved `notice.values`, including the note file name, not a fresh render.
- `agent_notes.apply` rewrites `weekly_usage_checked_at` on each fresh observation and appends a `readings.jsonl` line with it, so an hour with no lines means no observation, not zero use. Credits are whole numbers, so under 1 credit an hour reads as 0.
- Editing a catalog template or example value changes `push_texts.fingerprint(entry)` and fails `test_every_catalog_example_has_current_approval` until `review.py texts --id <id> --approve` runs again; the render-equality tests in `test_quota_alert.py` move with it.
- `run_out` registers under both `run_out` and `scripts.whoami.run_out`, so timer and package callers share its types.

**Ruled out:** dropping a push the text reviewer withholds (the user asked for these on the phone by name); leaving workers on a credits-off account at 0% when no destination exists; a string match on the switch outcome in place of `CodexWorkSwitch`.

### Phase 21 — Account notes by number, with role and sessions  · status: done

#### As-built

- Account notes are named `claude1.md` … `codex2.md`; labels stay `claude 1` / `codex 2`. `note_file` owns the one label↔file mapping: `ACCOUNT_STEM = (claude|codex) ?([1-9][0-9]*)`, `account_stem(stem) -> AccountStem | NotAccountStem` (frozen; `AccountStem(tool, number).label`), `note_name(label)` (raises `ValueError` for a non-label), `notes_dir()` (`AGENT_NOTES_DIR`, else a patched `AGENTS_DIR`, else `~/rust/hanadocs/agents` per call), `note_path(label)` (the old `<tool> <n>.md` only when it alone exists). `read_note` returns no `Note` for a `NotAccountStem` and fills `Note.label` and `tool` from the parsed stem; `read_notes()` gives one note per label, the numbered file preferred. `Note.get_list(key)` reads block, flow, `[]` and scalar lists; `in_use(note: FieldReader)` is a non-empty `role` or `sessions > 0`.
- `agent_notes.rename_legacy_notes(directory)` runs first in `update_alert_pass`: `os.link` then `os.unlink` per spaced note; logs `agents: renamed …`, `… both exist; reading …` (nothing changes) or `could not rename …`.
- The `state` field and the account-activity types are gone; each pass deletes an existing `state:` line. Notes carry `role` (block list in the order showrunner, unit, worker, or `role: []`) and `sessions` (row count). Claude roles come from `accounts.group_note_labels()`; `worker` marks the note of the workers' selected Codex account. An unreadable `groups.json` or an unknown Codex selection keeps every role line of that tool. `mark_tier` writes the tier on the note whose role holds `worker`.
- `agent_notes.write_note(note, updates, *, remove, section, stamp) -> bool` is the one writer (`set_fields` calls it): tuple values render as blocks, a replaced or removed key drops its continuation lines, the file is written once via `os.replace` and only when the text changes. `apply` returns `QuotaUpdates` per label; `update_alert_pass` merges quota, `role`, `sessions` and the section into one write per note. A `readings.jsonl` line is appended only when quota fields change.
- `session_table.py` owns the `## Live sessions` body section (`Session | Role | Machine | Folder | Session id`, sorted by machine, role, name, id; `No live sessions.` when empty; `|` escaped). Rows come from the session roster via `live_sessions(environment) -> ReadSessions | SessionsUnread`; `ReadSessions.rows_kept` lists machines whose rows cannot be attributed (peer not reached, or a peer entry with `AccountAttributionUnavailable`), and those machines keep the rows already in the table. `SessionsUnread` leaves every table and count as written.
- The roster attributes each entry an `account: AccountLabel | AccountUnread | AccountAttributionUnavailable`. A Codex worker's account comes from the `codex_home` of the server its record names by `port`: `_run_server_homes` reads `mesh_retired.json` then `mesh_server.json`; `_worker_account` resolves the label. The snapshot carries `account` and decodes an entry without it; `peer_snapshot(environment) -> Roster | PeerNotReached`. `account.process_codex_home(pid)` gives `ProcessCodexHome | ProcessWithoutCodexHome | ProcessEnvironmentUnread`.
- `quota_alert` uses `note.label` and `in_use`, names files through `note_name`, and `_current_notice(label, notice)` rewrites a saved zero-episode notice's `file` value on each phone retry.

**Files:**
- `scripts/whoami/note_file.py` — the label/file mapping, `Note.label`, `get_list`, `in_use`, `FieldReader`, `notes_dir`, `note_path`.
- `scripts/whoami/agent_notes.py` — `rename_legacy_notes`, `write_note`, `QuotaUpdates`, the merged per-note write, `role`/`sessions`/table, `mark_tier` by role.
- `scripts/whoami/session_table.py` — `SessionRow`, `SessionsSection`/`KeepSections`, `ReadSessions`/`SessionsUnread`, `live_sessions`, `render_section`, `section_rows`, `replace_section`.
- `scripts/whoami/account.py` — `label_for`, `named_claude_account` by label; `process_codex_home`.
- `scripts/whoami/agent_accounts.py` — `codex_account`, `codex_credits_setting`, `codex_account_home` through `read_notes`/`note_path`.
- `scripts/whoami/quota_alert.py` — labels, `in_use`, `AgentNote`/`CodexQuotaAccountNote` labels and `get_list`, `_current_notice`.
- `scripts/accounts/accounts.py` — `_account_notes_directory = note_file.notes_dir`, `_account_note`, `_noted_labels`, `group_note_labels`.
- `scripts/message/roster.py` — `RosterEntry.account`, `_run_server_homes`, `_worker_account`, tolerant `_decode_entry`, `peer_snapshot`, `PeerNotReached`.
- `scripts/production/dailies_render.py` — Agents section through `note_file.read_notes`/`in_use`; empty line `- none active`.
- `scripts/production/cli_fixture.py` — patches `note_file.AGENTS_DIR`.
- `scripts/notify/push_catalog.py`, `push_texts_reviewed.json` — `codex1.md`/`codex2.md` examples, re-approved.

**Binds later work:** `note_file.AccountStem`/`NotAccountStem`, `account_stem`, `note_name`, `in_use`, `get_list` and `read_notes` are the base "Account labels without a space" builds its label parser on. Notes `codex1.md`/`claude1.md` are already renamed by `agent_notes.rename_legacy_notes`. Note fields `role` (list) and `sessions` (count) and the `## Live sessions` table from `session_table.py` are what consumers read; table labels still use the spaced form. The roster's `AccountLabel | AccountUnread | AccountAttributionUnavailable` is per-read, never persisted, consumed only by `session_table`. `dailies_render.py` reads notes through `note_file`; `cli_fixture.py` patches `note_file.AGENTS_DIR` (and `test_dailies_render.py` checks `note_file.notes_dir()`). `accounts.group_note_labels()` keeps a `default` branch handed to "Accounts named by number only". note_file, account and session_table each register under two module names (`note_file` and `scripts.whoami.note_file`, likewise for the others) via `sys.modules.setdefault`, so sibling and package imports share one module and one `AGENTS_DIR`.

**Gotchas:**
- `read_notes` sorts labels lexically: `codex 10` precedes `codex 2`.
- Two notes-dir controls exist: `AGENT_NOTES_DIR` and patching `note_file.AGENTS_DIR`; `notes_dir()` honours both.
- A pass with no fresh observation and no session change writes nothing; `weekly_usage_checked_at` changes on every fresh observation, so those always write.
- Every test reaching `update_alert_pass` or `main` fakes `session_table.live_sessions` (no roster read, no `ssh`).

**Ruled out:** reading a worker's account from the run's newest server or `agent_accounts.seats_home()`; defaulting to `~/.codex` without a read process environment; the shutdown inventory as the session source (per login, no Codex rows); emptying a role the pass could not read.

### Phase 22 — Account labels without a space  · status: done

**Depends on:** Phase 20, Phase 21

#### As-built

- Every account label is typed, stored and printed as `claudeN` / `codexN`. `note_file.parse_account_label(text) -> NumberedAccountLabel | InvalidAccountLabel` is the one parser (grammar `ACCOUNT_STEM = (claude|codex) ?([1-9][0-9]*)`): `codex2` and legacy `codex 2` both give `NumberedAccountLabel("codex", 2)`, whose `.label` is `codex2`; `codex`, `codex0` and `default` are `InvalidAccountLabel`. `canonical_label(text)` returns the parsed label or the text unchanged (logins, `default`); `canonical_label_text(text)` rewrites only the label mentions inside a notice. `note_name(label)` gives `codex2.md`.
- Spaced input is canonicalized on read and written in the new spelling on the next authorized write, never rewritten in bulk: `groups.json`, `config/accounts.json` `seats`, `roles.jsonl`, `moves.read_moves()`, shutdown records (`record._record` stores the canonical `label`), `quota_alert.json` episodes, `codex_zero` keys, fallback owner, refusal notices and saved notice text, `waiting.py` quota holds (`read_quota_state`), `readings.jsonl` and burn history (one history per parsed label), `by-label.json` and each `account-label`. `statusline.jq` looks up `account-label` with spaces removed. `showrunner_move.py` canonicalizes its target before printing.
- `read_notes()` finds a note under either spelling, prefers `codexN.md`, and orders by `(tool, number)`, so `codex2` precedes `codex10`; a second refresh changes nothing.
- `read_note -> Note | NotAccountNote | UnreadableNote | MalformedNote`; `reset_time -> KnownReset | NoResetKnown | InvalidReset`.
- `## Live sessions`: an `AccountUnread` session counts on a line under the table, `<n> live session(s) not attributed to an account`. A row kept for an unreachable or older peer shows `(last seen <time>)`, taken from the hidden `<!-- observed-at: … -->` comment in its identity cell, or `unknown` when the row predates the comment; kept rows are not counted in `sessions`, and return to live when the peer answers.
- `/new_account` is on `build-followups`: `account_label` is gone; the typed name goes through `parse_account_label`, `home_login` returns `HomeLogin | NoHomeLogin`, and the note is written as `claudeN.md`, continuing over a legacy spaced note. `scripts/nightly_review/settings.json` denies edits with one explicit line per account folder (`~/.claude1` … `~/.claude4`); `/new_account` adds the new folder's line once.
- Push catalog examples name `claude2`, `codex1`, `codex2`, `codex999`, with the ten affected texts re-approved.

**Files:**
- `scripts/whoami/note_file.py` — the label parser, `canonical_label`/`canonical_label_text`, note discovery and order, the named absent cases.
- `scripts/whoami/session_table.py` — the unattributed line, kept rows with last-seen from the hidden comment.
- `scripts/whoami/agent_notes.py`, `quota_alert.py`, `agent_accounts.py`, `run_out.py`, `usage_store.py`, `account.py`, `codex_pacer.py`, `seat_usage.py` — labels through the parser; `seat_usage` matches Codex labels in both spellings.
- `scripts/accounts/accounts.py`, `moves.py`, `refusal_watch.py` — patterns, stored labels and usage strings in the new spelling.
- `scripts/accounts/new_account.py`, `commands/new_account.md` — `/new_account` with `HomeLogin` and the per-folder deny line.
- `scripts/nightly_review/settings.json` — one edit deny per account folder.
- `scripts/production/waiting.py`, `showrunner_move.py`, `add_showrunner.py` — canonical quota holds, move target and new-showrunner label.
- `scripts/shutdown/shutdown_record.py` — both spellings read, `claudeN` stored.
- `scripts/message/roster.py`, `scripts/statusline/statusline.jq` — a peer's legacy label parsed; the space-free `by-label.json` lookup.
- `scripts/notify/push_catalog.py`, `push_texts_reviewed.json` — examples and approvals.
- `docs/accounts.md`, `docs/quota_alerts.md`, `commands/showrunner/accounts.md` and the as-builts that print a label — labels without a space.

**Binds later work:** "Accounts named by number only" inherits the hidden `<!-- observed-at: … -->` comment in `## Live sessions` rows (any row rewrite keeps it) and canonical `quota_seen` keys; `waiting.py` and `showrunner_move.py` already canonicalize legacy input, so their `default` handling reads canonical labels. `default` stays a valid non-numbered target until that phase removes it.

**Gotchas:**
- Compare labels through the parser or `canonical_label`, never as raw strings.
- Shutdown tests merged from `build-followups` may still assert a spaced label in output; the follow tests in `test_shutdown_cli.py` assert `claudeN`.
- `/etc/nixos` aliases `claude1..3` to `exec 'claude N'`; that is legacy input and still parses.

**Ruled out:** a bulk migration of stored records; a single `Edit(~/.claude[0-9]*/**)` glob in the nightly-review denies (the settings test requires one line per folder).

### Phase 23 — Accounts named by number only  · status: done

**Depends on:** Phase 20, Phase 21, Phase 22

#### As-built

- No command, output or message names an account `default`; each account is named `claudeN` / `codexN` by the login its folder holds, and `~/.claude` / `~/.codex` take the number of their login's note.
- `accounts.numbered_account_label(label, tool) -> note_file.NumberedAccountLabel` is the one numbered-only parse for every destination, every picker filter (`pick --exclude/--keep`) and `shutdown.py down --to`. `refuse_default` rejects `default` with `accounts: name the account by number (~/.claude is claudeN; ~/.codex is codexN)`, the numbers read live; any other unnumbered label is refused with `'<x>' is not a numbered <tool> account; use <tool>N`. Both refusals print on stderr and exit 1, at the input boundary before any matcher runs.
- `accounts.claude_account_dir(label) -> Path` resolves strictly: `~/.claude` when the `claudeN` note's login is `~/.claude`'s login, else `~/.claudeN`; it refuses a missing or unreadable note and an unnoted `~/.claude`. Every former `default` branch (`prepared_account`, `role_account`, `placement`, `session_on_account`, `status_report`, `restart._target_config_dir`, the herdr branch) resolves through it; Codex resolves through `agent_accounts.codex_account_home`. The picker reads the folder `claude_account_dir` resolves to.
- `DefaultAccount` is gone: `ClaudeLaunchAccount = NamedAccount`, and the `~/.claude` account is a `NamedAccount` with `dir=~/.claude`; callers test the dir. Identity is `agent_accounts.NumberedAccount(label, home, login) | UnnotedLogin(home, login) | UnreadableIdentity(home, reason)`, and destination resolution takes only `NumberedAccount`. `ClaudeAccountsInUse.home_directory_account` and `is_home_directory_login` replace `default_account` and `is_default_login`.
- A signed-in home whose login has no note is never a destination: `pick`, `placement`, `move`, `swap seats`, `set-role seats` and `exec` skip it, and status prints `~/.<tool> — no account note, not selectable: add ~/rust/hanadocs/agents/<tool>N.md with login: <login>`; a numbered request for that home's number is refused with the same line.
- Writes store numbers only (`groups.json`, `config/accounts.json` `seats`, `roles.jsonl`, shutdown `target`); no file is rewritten in place. `set_group_account` writes from `_stored_groups(resolve_home=False)`, so untouched groups stay as stored while reads resolve. A stored legacy `default` reads as the home's number: `groups.json`, `roles.jsonl` (`unit` → `~/.claude`, `seats` → `~/.codex`), `SessionShutdown.target` (restarts on `~/.claude`), and an absent `seats` key reads as `~/.codex`'s number.
- `moves.read_moves()` resolves a historical `default` (`requested` `from`/`to`, `room again` `label`) only from the `roles.jsonl` line in force at the entry's `at` or the same move's shutdown record; with neither it reads `HomeAccountUnresolved(home)`, printed `home account unresolved`. `moves.py` stays a standard-library leaf at module level. `append_move` refuses `default` after canonicalizing. `codex_home_matches_label` has no `default` branch; a pending legacy `from: default` request still confirms in `codex_mesh._requested_worker_move` through `read_moves()`.
- `shutdown.py down --to` parses through the `NumberedClaudeTarget` argparse action, which rejects an unnumbered target before any record, journal entry or conductor. `role`, usage strings (`<claudeN>`), `pick`/`placement` candidates, `session_account_label`, reviewer `account` in `review.jsonl`, and `showrunner_move.py`'s printed target all give numbers.

**Files:**
- `scripts/accounts/accounts.py` — `numbered_account_label`, `refuse_default`, `claude_account_dir`, `ClaudeLaunchAccount`, group storage, unnoted-home line, numbered output.
- `scripts/accounts/moves.py` — `HomeAccountUnresolved`, historical `default` resolution in `read_moves`, `append_move` refuses `default`.
- `scripts/accounts/refusal_watch.py` — numbered source and reconciliation target.
- `scripts/whoami/agent_accounts.py`, `quota_alert.py`, `agent_notes.py` — the identity union and the `home_directory_account` renames.
- `scripts/shutdown/shutdown.py` — the `down --to` numbered parse; `restart.py`, `record.py` — numbered targets, legacy `default` read.
- `scripts/agents/codex_mesh.py` — `_requested_worker_move` on the normalized reader.
- `scripts/production/add_unit.py`, `add_showrunner.py`, `promote_showrunner.py`, `showrunner_move.py`, `scripts/notify/review.py` — numbers in place of `DefaultAccount` and `"default account"`.
- `scripts/accounts/test_accounts.py`, `test_moves.py`, `scripts/shutdown/test_shutdown_cli.py`, `test_settle.py` — refusals, historical resolution, CLI rejection before records or conductors; `settle.py` unchanged.
- `docs/accounts.md`, `commands/showrunner/accounts.md`, `docs/as-built/shutdown-and-restart.md`, `docs/quota_alerts.md` — numbers only; the unnoted-home line.

**Binds later work:** "Proof refusals stay with their session" adds its parser options beside `--to`'s `NumberedClaudeTarget` and keeps the parser rejection before any record; any new journal writer passes numbered labels through `numbered_account_label`, since `append_move` refuses `default`; new destination code calls `claude_account_dir`; group writes keep siblings as stored via `_stored_groups(resolve_home=False)`. "Readout of the moves" reads through `moves.read_moves()`, keeps the `home account unresolved` rule and never attributes a past entry from today's login.

**Gotchas:**
- A home folder can hold one account while `~/.claudeN` holds another; the picker reads the folder `claude_account_dir` resolves to.
- `groups.json` keeps untouched groups exactly as stored: writes use `_stored_groups(resolve_home=False)`, reads resolve.
- Tests set `HOME` to a temp dir; no `move`, `set-role`, `swap` or `shutdown down` runs against the real home.

**Ruled out:** resolving a historical `default` from today's login of `~/.claude` or `~/.codex`; rewriting stored files in place (legacy values take the number on their next write); a `settle.py` change for the `--to` parse.

### Phase 24 — Proof refusals stay with their session  · status: done

**Depends on:** Phase 16, Phase 17, Phase 18, Phase 19, Phase 22, Phase 23

#### As-built

- `refusal_watch.session_refusal` returns `TranscriptRefusal | ProofRefusal | NoSessionRefusal`; `assumed_refusal` reads the `--assume-refused` file (`AssumedRefusalFile`: `uuid`, `timestamp`, `window`, `reset`) as a `ProofRefusal`. The watcher, `cancelled` and settle's usage-limit hold (`_usage_limit_shown`, which holds on either refusal type) match on the type.
- A `ProofRefusal` never calls `refusals.write_refusal`, never joins the logins the tick excludes for other sessions, and keys its no-room notice by the proof (`proof:<session id>`, or `proof group`), never the source login's fingerprint key; `refusals.load()` and every other session's `pick` are unchanged by a proof. A `TranscriptRefusal` writes the account refusal once per (session id, entry uuid) and keeps the login notice key. The probe's own move still excludes its source account. The proof file is deleted on restart (`restart._confirm_move` → `clear_assumed`), on cancel, and when the watcher delivers the five-hour continuation.
- `--assume-refused <probe> weekly|five-hour` takes only ungrouped probes (inventory kind neither `unit` nor `showrunner`) and is a parser error otherwise; under `--dry-run` it writes no file. `--proof-group <probe id>…` (requires `--assume-refused`) passes `run(..., proof_group=frozenset(ids))`: the named probes form one group with one target, one `move_sessions` call and one `move_id`, and the run reads no `groups.json`, no live group members, no real `roles.jsonl` line and skips group reconciliation.
- `accounts.MoveOrigin = RefusalOrigin(window, reset: KnownReset | NoResetKnown) | HandOrigin` (singleton `HAND_ORIGIN`); `move_origin_fields(origin) -> MoveOriginFields` (`cause`, `window`, `reset?`) and `move_origin_arguments(origin)` (`--refused <window> [--reset <ISO>]`, empty for a hand move). `move_sessions(ids, target, *, why, origin, move_id="")` parses `target` through `numbered_account_label`, uses one transition id for every session, and passes `--move-id` and the origin arguments to `shutdown.py down`. Both `requested` writers (`settle.down` and `move_sessions`' fallback) take `cause`, `window` and `reset` from the origin and never read `refusals.load()`: a hand move is `cause: by hand`, `window: unknown`, no `reset`.
- `RequestedMove.cause` is `Literal["refusal", "credits off", "by hand"]`; `_valid` accepts `cause` on `requested` only and rejects an account-move line whose `request_id` is present but not a non-empty string; `read_journal` reads a missing `cause` as `refusal`. `/showrunner:accounts status --detail` prints the cause on every request in the last-10 list, Claude and Codex alike.
- `shutdown.py down` takes `--refused {weekly,five_hour,unknown}`, `--reset` (the `AwareReset` action, aware ISO only) and a hidden `--move-id` beside `--to`'s `NumberedClaudeTarget`; `--reset` without `--refused` is a parser error, and every rejection comes before any record, journal entry or conductor. `settle.down(..., origin: MoveOrigin = HAND_ORIGIN, move_id="")` forwards both to the peer and into the cold-keys retry line.
- Group transitions: a `groups.json` value is `StoredGroupSelection` `{label, move_id, cause, window, reset?}`; a string value (every file from before) reads as that label with `NoGroupTransition`. `_stored_groups()` validates and returns each stored value as stored, string or object, legacy `default` included; `group_accounts() -> GroupAccounts` and `group_label` resolve labels as before; `group_transition(group) -> GroupTransition | NoGroupTransition`. `set_group_account(group, label, *, why, now, origin=HAND_ORIGIN, move_id="") -> GroupTransition` writes the object for the changed group only, leaves each sibling's stored value untouched, appends one `roles.jsonl` line for that group carrying `move_id`, `cause`, `window` and `reset`, and returns the `GroupTransition(move_id, origin)` it wrote, built under `_groups_lock`. `move_group`, the watcher's refusal selection and the reconciliation of a string selection dispatch `move_sessions` with that returned transition, never a reread; a reconciliation tick otherwise reuses `group_transition`'s id, so a partial group move completes under one `move_id` and cause.
- Records and outcomes: `record.SessionShutdown.move_id: NotRequired[str]`, checked non-empty by `_shutdown_request_fields` and stored by `settle.begin`. `requested`, `confirmed` and `failed` (`restart._journal_move_failure`, `_confirm_move`, settle's cancel) journal `move_id = record.get("move_id", record["request_id"])` plus the attempt's `request_id`; the stale-request guards keep comparing `request_id`. `SessionMoveRequested` carries `request_id` and `move_id` (defaulting to `request_id`). A record without `move_id` journals and restarts as a standalone move under its `request_id`.
- `restart._target_config_dir(record) -> RecordedConfigDir | NoRecordedConfigDir` and `_live_config_dir(record) -> ObservedConfigDir | LiveObservationUnavailable(reason: "not live" | "environment unreadable")`. `_prove_move` marks `restart failed` with `resumed account unreadable` for either absence and `still on <source_label>` for a mismatch, and confirms on a match; `_confirm_move` writes `config_dir` from `RecordedConfigDir` only; `clear_running_on_target` keeps the record for either absence.
- Routing journal (`moves.py`): `RoutingEntry = RoutedToClaude | RoutedBack | RoutingNotRestored`, each `{kind, at, tool: "codex", fallback_at, owner}` with no `move_id` or `subject`; `RoutedToClaude` adds `cause` (`refusal` | `credits off`) and `moved`, `RoutedBack` adds `restored` and `kept`, `RoutingNotRestored` adds `outcome` (`partial` | `failed`), `restored`, `kept` and `failed`. `JournalEntry = MoveEntry | RoutingEntry`. `read_journal() -> list[JournalEntry]` is the one parser; `account_moves(journal) -> list[MoveEntry]` narrows by a `match` ending in `assert_never`; `read_moves()` is `account_moves(read_journal())`. `append_routing(entry)` writes under the journal lock, refuses owner `default`, stores the owner through `numbered_account_label(..., "codex")` or `unknown`, and skips a repeated (`kind`, `fallback_at`). Owner reads back as `str | UnknownFallbackOwner` (prints `owner unknown`).
- `quota_alert.switch_to_claude` appends `routed to claude` after storing the fallback, only when work moved, with `cause: credits off` under a `CreditsOffHold` and `refusal` otherwise. `switch_back` writes after its restore loop: `routed back` when no key failed, else `routing not restored` with `outcome: partial` (some restored) or `failed` (none, the unreadable-registry branch included); a `NoCodexFallbackRestoration` writes nothing. `fallback_at` is the fallback's stored `at`; `owner` is the stored owner label, or `unknown` for an unknown-owner fallback and for a legacy stored owner `default`. Status prints `Codex work routing:` with one line per `fallback_at`: `routed to claude <time> (<owner>, <cause>): <keys>`, then `— routed back <time>`, `— not restored (<outcome>) <time>: <failed keys>` or `— open`.
- `scripts/accounts/test_launch_accounts.py` walks every Python and shell script under `scripts/` that starts `claude` (long and short `--` options, `claude -p`, literal, dynamic and bare-prompt launches) and fails a launcher that selects its own account (`pick`, `placement`, `role_account`, `group_label`, or a `CLAUDE_CONFIG_DIR` not from the resolver or a record); rendering through `prepared_account`, `account_prefix`, `named_account_environment` or `account_dir` passes. `accounts.account_for_group_launch` stays the one resolver; named exceptions with reasons: `accounts.exec_account`, `restart._resume_parts` (ungrouped resumes), `send.relay`, `review.reviewer_environment`. No production launcher changed.

**Files:**
- `scripts/accounts/refusal_watch.py` — `ProofRefusal`, `AssumedRefusalFile`, `--proof-group`, proof notice key, file deleted on continuation.
- `scripts/accounts/moves.py` — `cause`, `RoutingEntry`, `JournalEntry`, `read_journal`, `account_moves`, `append_routing`, `_valid` by variant.
- `scripts/accounts/accounts.py` — `MoveOrigin`, `GroupTransition`, `StoredGroupSelection`, `group_transition`, `set_group_account`'s return, `move_sessions`' origin, routing status lines.
- `scripts/shutdown/shutdown.py`, `settle.py`, `record.py`, `restart.py` — origin arguments, `move_id` on records and outcomes, the hold on a proof, the config-dir result types.
- `scripts/whoami/quota_alert.py` — the routing writers.
- `scripts/accounts/test_launch_accounts.py` — the launch-account guard.
- Tests: `scripts/accounts/test_refusal_watch.py`, `test_moves.py`, `test_accounts.py`; `scripts/shutdown/test_settle.py`, `test_shutdown_cli.py`, `test_record.py`, `test_restart.py`, `test_account_move.py`; `scripts/whoami/test_quota_alert.py`, `test_agent_notes.py`; one mixed-journal case in `scripts/agents/test_codex_mesh.py`.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — proof refusals stay with their probe, the cause on a move, the routing lines.

**Binds later work:** "Live proof of the moves" runs probes through `--assume-refused` (ungrouped only, no account refusal written) and `--proof-group`, and records each routing as `routed back`, `routing not restored` (`partial` | `failed`) or open. "Readout of the moves" reads `moves.read_journal()` (account moves and routing) and `account_moves`; counts a Claude group move once by its group `move_id`, which `roles.jsonl`, every member's `requested` and later reconciliation requests share; takes `cause` from `requested` (missing is `refusal`); pairs routing by `fallback_at` and keeps `owner unknown` unknown.

**Gotchas:**
- A `groups.json` string value has no transition; the first reconciliation that needs one writes the object form through `set_group_account` with a new `move_id`.
- A legacy stored fallback owner `default` journals `unknown`, never today's `~/.codex`.
- Dispatch a group move with the `GroupTransition` `set_group_account` returns; rereading the selection after the write can pick up a later selection's id and origin.
- A new `claude` launcher fails `test_launch_accounts.py` unless it renders the resolver's result or a record's target, or is added to its exceptions with a reason.

**Ruled out:** a proof writing the account refusal or sharing the login's no-room notice key; a hand move reading the stored account refusal for its window; a second hand-origin type in `settle.py` (it defaults to `accounts.HAND_ORIGIN`); a separate owner parse in `quota_alert.py` (`append_routing` owns it); narrowing status by a kind filter and cast instead of `account_moves`; the watcher's fallback to the stored account refusal for a watch entry without its first observation.

### Phase 25 — Live proof of the moves  · status: done

#### As-built

- The `pick` function reads a fresh Claude reading at 0% weekly or five-hour left as "no room", as Codex already does; when no account has room it names the earliest reset. `placement` goes through `pick` and shows its reasons with `placement --explain`; Phase 74 removed the command verb. Pinned by `test_picker_explains_fresh_zero_weekly_or_five_hour_as_no_room_and_placement_agrees` and `test_picker_all_fresh_zero_candidates_refuses_with_the_soonest_reset_and_placement_agrees`.
- `accounts.py status` prints Folder, Label, Login, Role, Weekly left, Weekly resets, On credits, Credits left, Read: no 5-hour column. The 5-hour reading still decides in `pick` and `placement` and still appears in `placement --explain`; Phase 74 removed the account-picking command verb. Pinned by `test_status_hides_five_hour_cells_and_keeps_weekly_and_read`.
- Proven live on 2026-10-15 with throwaway probes (started on claude2 in `~/rust/two-accounts-probe`, each checked by a code word asked through `send.py`): a tmux weekly move claude2 → claude1 (one `requested`, one `confirmed` on `~/.claude1`, `account.py --pid` reads claude1); a Ghostty weekly move that reuses the same KWin window with position, size and desktop unchanged; a two-probe `--proof-group` move under one move id, no other session in it; a five-hour wait (`waited`, then `wait ended` with no `requested`, one "Continue where you stopped." delivered and answered, account unchanged, assumed refusal file removed). `accounts.py --detail status` lists each with its outcome. Each probe row leaves claude2's note and appears on claude1's, group roles stay unchanged, no account refusal is written, and `~/.claude/.credentials.json` is not written.
- Unproven live, standing on tests: `/shutdown status` during the shutdown hold (the hold lasts about 17 s); the Codex workers move, its mid-turn continuation and a Codex five-hour wait; every credits-off path (the hold, the swap, the Claude switch, the routing outcomes), since no Codex account reached 0% weekly.
- T0 = 2026-10-15 11:47 EDT. G3 opens the readout at T0 + 7 d, 2026-10-22 11:47 EDT.

**Files:**
- `scripts/accounts/accounts.py` — the picker's no-room rule for fresh 0% Claude readings; the status table cells (`TABLE_HEADINGS`, `NO_READING`).
- `scripts/accounts/test_accounts.py` — picker, placement and status-table regression tests.
- `scripts/accounts/test_refusal_watch.py`, `scripts/whoami/test_quota_alert.py` — fixture tests matching the new rule.
- `docs/accounts.md` — the status table's columns.

**Binds later work:** the readout of the moves starts its window at T0 2026-10-15 11:47 EDT; every probe move falls before T0 and is excluded. The first real Codex refusal is the live proof of the workers move and is reported to the showrunner with its journal entries; a real Claude refusal does not stand in for the Codex mid-turn continuation.

**Gotchas:**
- `--detail` is a top-level flag: `accounts.py --detail status`.
- A non-dry-run `refusal_watch.py --assume-refused` needs the settings allow rule added 2026-10-15.
- A stale Claude reading keeps "has room; no reading in the last 10 minutes", ranked after every fresh account with room.

**Ruled out:** moving the real Codex workers for the proof (it moves every unit's workers); moving any real session (probes are throwaway sessions picked by their own ids).

### Phase 26 — Account notes unchanged when nothing changed  · status: done

**Depends on:** Phase 22

#### As-built

- `## Live sessions` rows carry no time: `render_section` writes no `<!-- observed-at: … -->` comment, and live rows have `last_seen` and `observed_at` cleared. `session_table.observed` is gone.
- `live_sessions` returns `ReadSessions.machines_read`: the local machine always, a reached peer's machines (its `other_machine()` name when it has no entries), never a peer whose attribution is unavailable. Each refresh that reads sessions merges `{machine: checked_at}` for those into `~/.local/state/agent-notes/sessions_seen.json` (`agent_notes.SESSIONS_SEEN`, expanded at call time; `read_sessions_seen`, `write_sessions_seen`), one atomic replace per refresh, outside the vault. A missing, malformed or non-aware entry reads as absent; a failed write is a `sessions seen:` message and never blocks the notes. An unread collection leaves the file and the sections alone.
- `session_table.last_seen(row, machines_seen)` freezes a kept row's `(last seen <time>)`: its existing marker, else the machine's saved time, else a legacy `observed-at` comment, else `unknown`. The text changes once, when the peer stops answering. `section_rows` still reads the legacy comment.
- A refresh two minutes after another with the same sessions differs only on lines the vault snapshot skips (`/etc/nixos/modules/linux/agent-sessions-snapshot.py` `TIMESTAMPS`, copied into `test_session_table.py`); a session starting, stopping or moving, the `sessions` count and the unattributed line still change the note. The unattributed line is recomputed each run and never parsed.

**Files:**
- `scripts/whoami/session_table.py` — no time on live rows; `machines_read`; `last_seen` with the seen map.
- `scripts/whoami/agent_notes.py` — `SESSIONS_SEEN`, its reader and writer, one write per refresh.
- `scripts/whoami/test_session_table.py`, `test_agent_notes.py` — the skip-list, real-change, frozen last-seen, legacy-comment and seen-file tests.
- `docs/accounts.md` — the last-seen source.

**Gotchas:**
- The skip list lives in `/etc/nixos`, not this repository: a new per-run frontmatter field commits every run unless that list gains it.

**Ruled out:** keeping the first observation time in each row (a long-lived row would read "last seen" days stale); a new per-run frontmatter field (not on the snapshot's skip list).

### Phase 27 — Roster rows stable between runs  · status: done

**Depends on:** Phase 26

#### As-built

- The session roster lists the same sessions on two runs when none started, stopped or moved, so the account notes stop committing between snapshot runs. Checked live 2026-10-15: after install at 14:21 EDT the first run removed the "live session(s) not attributed to an account" line and added the retained worker rows; the runs from 14:23 to 14:30 committed nothing.
- `_review_workers` adds no entry for a `review_status*` file that says reviewing when `_reviewer_process` finds no reviewer process (a lens review runs as a `codex_mesh.py follow` turn inside the mesh app-server, whose worker is already listed). A found reviewer keeps its entry, its kind and its process account.
- `_delegate_workers` lists every non-`ended` mesh record of a live run (active marker or fresh heartbeat) whether or not its app-server or launcher is alive; a run that is not live lists none.
- `_worker_account` takes the worker's home from `mesh_server.json`, then `mesh_retired.json`; with no recorded home it attributes the worker to `agent_accounts.workers_home()`, the home every new server serves and the worker's next turn runs on.

**Files:**
- `scripts/message/roster.py` — the three rules above; `_server_alive` is gone.
- `scripts/message/test_roster.py` — a test per rule.

**Binds later work:** retained idle workers count in account-note sessions, and their workers-home attribution proves no move or continuation ("Readout of the moves").

**Gotchas:** codex_mesh's idle stop deletes the run's `mesh_server.json`, so a stopped server leaves no record of its home in the run folder.

**Ruled out:** retiring the server record into `mesh_retired.json` in codex_mesh instead of the roster fallback.

### Phase 28 — Status line colors  · status: done

**Depends on:** Phase 22

#### As-built

- `config/statusline.conf` (`key=value`, `#` comments) holds the colors: `role.<role with hyphens>`, `account.<label>`, `level.ok|warn|alarm`, and the thresholds `tokens.warn|alarm` (percent of the window used, 80/90) and `left.warn|alarm` (percent left, 10/5). Colors are the eight ANSI names and their `bright-` forms. Shipped: showrunner bright magenta, unit director bright blue, worker bright cyan; claude1 cyan, claude2 magenta, claude3 blue, claude4 bright white; levels bright green, yellow, red. `unassigned` and an unlisted label stay uncolored.
- `statusline.sh` passes the file as `--rawfile colors` (`/dev/null` when unreadable) to the same single jq process, and sets zsh `nonomatch` so an empty notifier glob no longer aborts the line. `statusline.jq` parses it once per render.
- Only the role, the account, the token count and each `<N>% left` are wrapped (`ESC[<code>m…ESC[0m`). Tokens: `total_input_tokens * 100 / context_window_size`, unrounded, alarm at `>= tokens.alarm`, warn at `>= tokens.warn`. Percent left: judged on the integer shown, alarm at `<= left.alarm`, warn at `<= left.warn`. Claude Code 2.1.296 sets `total_input_tokens` to the current context (input plus cache) and `context_window_size` to the model's window.
- A missing file, an unknown key or color, a bad threshold, or no positive window size leaves that piece uncolored; with the codes stripped the line equals the uncolored one.

**Files:**
- `config/statusline.conf` — new; `config/README.md` — its entry.
- `scripts/statusline/statusline.sh`, `statusline.jq` — the colors.
- `scripts/statusline/test_statusline.py` — each color, each threshold edge, the fallbacks, the stripped-line equality, and the settings command finding the config.

### Phase 29 — Status line follow-ups  · status: done

**Depends on:** Phase 28

#### As-built

- Every role and account has a real color; none ships white, bright-white, black or bright-black. Besides the eight ANSI names and their `bright-` forms, `statusline.jq` accepts five named 256-color entries, written `ESC[38;5;<n>m`: `orange` 208, `purple` 141, `pink` 211, `teal` 30, `brown` 137. `config/statusline.conf` ships `account.claude4=orange` and `role.unassigned=purple`; `unassigned` takes its color from `role.unassigned` like any role, and with no key stays uncolored.
- A unit director's role reads `unit director for <showrunner name>`. When `SHOWRUNNER_UNIT` is set and `UNIT_TEAM_ROLE` is empty, `statusline.sh` reads `TARGET=session:<id>` from `${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}/showrunner-$SHOWRUNNER_UNIT/conf`, then takes the first `${NOTIFIER_SESSIONS_DIR:-$HOME/.claude/sessions}/<pid>.json` containing `"sessionId":"<id>"` whose pid is live (`kill -0`), with shell builtins only (`$(<file)` under zsh, `IFS= read -r` elsewhere). It passes the record text as `--arg showrunner` (empty when nothing matches); jq parses it with `try fromjson catch null` and uses a non-empty string `name`, else `unit director`. The whole text takes `role.unit-director`'s color. The name is read on every refresh, never stored, so a renamed showrunner shows at once; no other role looks anything up.
- `/new_account` adds a Claude account's color after the nightly review step: with no `account.<label>=` line, it picks the first color in palette order (the sixteen ANSI names in `statusline.jq`'s order, then `orange`, `purple`, `pink`, `teal`, `brown`) that is not red, green or yellow in either form, not `black`, `white`, `bright-black` or `bright-white`, and used by no `account.` or `role.` line; else the first used by no `account.` line; else none. The line goes after the last `account.` line (at the end when there is none) through a temporary file chmodded to the original's mode, then a replace; every other line is unchanged. Step text `status line color <color> for <label>`; a rerun writes nothing; no color left or no config file is a done step saying the label stays uncolored; an unreadable or unwritable file is a `for you:` step naming the path and the error. With the shipped file, `claude5` gets `pink`, then `claude6` `teal`.

**Files:**
- `config/statusline.conf` — claude4 orange, `role.unassigned` purple; `config/README.md` — the five named colors and that `/new_account` adds the account line.
- `scripts/statusline/statusline.sh` — the showrunner lookup; `statusline.jq` — the named colors, `unassigned`, `unit director for <name>`.
- `scripts/accounts/new_account.py` — the color step; `commands/new_account.md` — one line on it.
- `scripts/accounts/test_new_account.py` — pick order, placement, rerun, missing file, no color left, 0640 and 0600 kept.
- `scripts/statusline/test_statusline.py` — zsh render with no notifier state, the named colors, every palette name rendering, the showrunner name (found, renamed, missing, dead pid, damaged record, a worker never looking, a showrunner on another account).

**Gotchas:**
- The record reaches jq as text, never through `--slurpfile`: a damaged record makes `--slurpfile` exit 2 and blank the whole line.
- Every account's `sessions` folder links to `~/.claude/sessions` (`accounts.py` `ALWAYS_LINKED`), so the lookup finds a showrunner running on any account.
- zsh's `read` reads byte by byte and cost about 3 ms per render; `$(<file)` does not.

### Phase 30 — The accounts table shows only accounts  · status: done

#### As-built

- `KNOWN_PRIVATE_NAMES` in `scripts/accounts/accounts.py` includes `feedback` and `telemetry`; `_is_known_private` accepts the prefix `.claude.json.tmp.`. These are per-account, never linked, never reported.
- `status_report` prints Claude unknown entries (`AccountStatus.unclassified`) and Codex unknown entries (`CodexStatus.unclassified`) only under `--detail`; the default table shows only accounts.
- `_check_codex_home` does not count unknown entries, so `check codexN` passes a home whose only oddity is an unknown entry; an unreadable numbered home is still a problem.
- `sweep_claude_temp_files(now)` opens `$HOME` and each real `~/.claudeN` with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, lists each folder once, lstats and unlinks through `dir_fd`, deletes only regular `.claude.json.tmp.*` files strictly more than a day old, and skips every `OSError`. `agent_notes.update_alert_pass` calls it once per pass (the two-minute updater).

**Files:**
- `scripts/accounts/accounts.py` — private names and prefix, `--detail` gate, `CodexStatus.unclassified`, `_check_codex_home`, `sweep_claude_temp_files`.
- `scripts/whoami/agent_notes.py` — `update_alert_pass` runs the sweep.
- `docs/accounts.md` — default table shows only accounts; `--detail` shows unknown entries; the sweep.
- `scripts/accounts/test_accounts.py`, `scripts/whoami/test_agent_notes.py` — classification, detail gating, sweep and updater tests.
- `scripts/whoami/test_codex_alert_accounts.py` — the unchecked-target fixture fails its check through a private cache link, not an unknown entry.

**Binds later work:** the sweep scans both `$HOME` (sessions on `~/.claude` write `$HOME/.claude.json.tmp.*`) and each `~/.claudeN` (its own `.claude.json` temps sit inside it); a fresh `~/.claudeN` shows no unknown-entry lines. `quota_alert` may recommend a Codex home that holds unknown entries.

**Gotchas:** folders open without following links, so a `~/.claudeN` swapped for a link after discovery is never scanned.

### Phase 31 — ~/.claude is a config home, not an account  · status: done

**Depends on:** Phase 30

#### As-built

- `~/.claude` is never an account folder: no function returns it as a note's folder, the status table has no `~/.claude` row, and `check claude2` inspects `~/.claude2` like any `~/.claudeN`. A note whose `~/.claudeN` is missing reads "not set up".
- Each machine changes over on its own. While `~/.claude` holds a login (`home_account_state() -> LegacyHomeLogin | ConfigOnlyHome`), a process with no `CLAUDE_CONFIG_DIR` reads as that login's account and the table prints `~/.claude holds a login; run accounts.py cutover-home` (on macOS the note names one `/login` in `~/.claude2` instead); once it holds none, such a process reads as no account everywhere and the status line shows no account piece. `account.process_config_dir(pid)` returns `ConfiguredClaudeFolder | NoAccountSetting | ProcessEnvironmentUnread`, and every consumer handles all three.
- `accounts.py exec <claudeN|units|showrunners>` launches on a numbered folder or a group's account; `prepared_account` and `account_prefix` never unset `CLAUDE_CONFIG_DIR`. Launchers that choose no account (`message/send.py`, `cli_agent/cli_agent.sh`, `agents/agent_exec.sh`, `agent_bg.sh`, the style-eval and berth_fix launchers) go through `account_for_unscoped_launch(environment) -> ClaudeLaunchAccount | LegacyHomeLaunch`: the caller's numbered folder, else the showrunners group's folder, else `LegacyHomeLaunch` while `~/.claude` still holds a login. Shell launchers share `scripts/agents/claude_account.sh`. herdr's unit, child-showrunner, standalone and restart launches keep their pane path and carry the resolver's `CLAUDE_CONFIG_DIR`.
- `accounts.py cutover-home <label>` renames `.credentials.json` and the `oauthAccount` block of `$HOME/.claude.json` into `~/.claudeN` (both atomic, never copied, never printed), under the journal `~/.local/state/accounts/cutover-home.json` written before the first rename; a rerun finishes an interrupted run and `--undo` renames back. It refuses while any Claude process uses the affected folder (`--undo`: `~/.claudeN`), when the destination holds a login or an `oauthAccount`, when a process's argv is unreadable, and on macOS in both directions. It appends a dated `home_cutovers` entry (label, login, time) to `config/accounts.json`.
- History resolves a record naming `default` or `~/.claude` from a login the record carries or from a `home_cutovers` entry dated after it, else leaves it unresolved; today's login never names a past record. `burn`, `_historical_roles`, `_unit_role_history`, `group_accounts`, `moves.py` and `shutdown/shutdown_record.py` follow this, and a resume of such a record goes to that label's folder.
- `$HOME/.claude.json` stays the seed and MCP baseline; the seed copies each project's trust entries into a new folder's `.claude.json`. On macOS `agent_accounts.claude_credentials` reads the keychain only for `~/.claude`; a numbered folder's keychain lookup is Phase 33's.

**Files:**
- `scripts/accounts/accounts.py` — `cutover-home`, `exec`, `account_for_unscoped_launch`, the status note; `moves.py` — history resolution; `new_account.py`, `refusal_watch.py`.
- `scripts/whoami/account.py` — tagged process result, `home_account_state`; `agent_accounts.py`, `agent_notes.py`, `quota_alert.py`, `whoami.py`.
- `scripts/shutdown/` (`record.py`, `restart.py`, `inventory.py`, `settle.py`), `scripts/production/` (`add_unit.py`, `add_showrunner.py`, `rename.py`), `statusline.sh`, `ensure_git_filters.sh`, `herdr/trust_survey.py`, `notify/review.py`, `nightly_review.py` — no `~/.claude` account reads.
- `scripts/agents/claude_account.sh` — shared shell launch resolver; `scripts/accounts/__init__.py`, `scripts/whoami/__init__.py` — repo-root imports.
- `scripts/accounts/test_launch_accounts.py` — guard: every Claude launcher uses the resolver, native `start_agent(kind="claude")` launches included.
- `docs/accounts.md`, `docs/as-built/` `shutdown-and-restart.md`, `herdr-adoption.md`, `production-rename.md`, `showrunner-automation.md`.

**Binds later work:** the live cutover on natedev runs `cutover-home claude2` and first moves every Claude process with no account setting (the postmaster session, cwd `~/postmaster`), since the refusal covers it. The Mac cannot use `cutover-home`; making its `~/.claude` hold no login (keychain entry and `$HOME/.claude.json` `oauthAccount`) without `/logout` is an open gap. A hand-typed `claude` lands on the showrunners account only once its alias calls `accounts.py exec showrunners`.

**Gotchas:**
- `ps` flattens argv; the guard reads `/proc/<pid>/cmdline` on Linux and fails closed on unreadable argv.
- Launcher tests that copy a script tree must copy `claude_account.sh`.
- Two folders on one login break token refresh; `cutover-home` renames, never copies.

### Phase 32 — Cutover on natedev: claude2 moves into ~/.claude2  · status: done

#### As-built

- On natedev, claude2's login lives in `~/.claude2` and `~/.claude` holds no login: no `.credentials.json`, no `oauthAccount` in `$HOME/.claude.json`. `accounts.py check claude2` passes, and the status table shows `~/.claude2` with claude2's login and usage and no legacy-home note.
- The cutover journal (`~/.local/state/accounts/cutover-home.json`) reads complete, and `config/accounts.json` `home_cutovers` holds `{label: claude2, at: 2026-10-16T03:09:27Z}` (2026-10-15 23:09 EDT).
- No Claude process runs on `~/.claude`. The postmaster session, the only one without an account setting, runs on claude1 after a scripted move (`accounts.py move postmaster claude1`). An unscoped launch (`account_for_unscoped_launch` with no `CLAUDE_CONFIG_DIR`) picks the showrunners group's folder.
- `docs/accounts.md` carries the natedev line.

**Gotchas:** `cutover-home --undo` leaves its `home_cutovers` row in place.

### Phase 33 — Cutover on the Mac  · status: done

#### As-built

- On macOS, `accounts.py cutover-home <label>` refuses unless the numbered folder holds a login (its `.claude.json` `oauthAccount` and its keychain entry, checked by service name). It refuses on an account mismatch, compared in code and never printed, with nothing changed. It also refuses while a Claude process uses `~/.claude`; the Mac guard reads each candidate's native argv through `sysctl` `KERN_PROCARGS2` (`_mac_process_arguments`) and refuses when that argv cannot be read. It writes the journal, removes `oauthAccount` from `$HOME/.claude.json` keeping every other key, deletes `Claude Code-credentials` by service name last, and appends the dated `home_cutovers` record. A rerun finishes an interrupted run. `--undo` restores `oauthAccount` and says the keychain entry needs a `/login` in `~/.claude`; an undone journal lets a fresh forward cutover start.
- `agent_accounts.claude_keychain_service` returns `Claude Code-credentials` when `CLAUDE_CONFIG_DIR` is unset or empty, else `Claude Code-credentials-<first 8 hex of sha256 of the NFC value>`; a named folder reads only its own entry and a missing one raises "usage credentials unavailable".
- `swap workers codexN` prints `Workers moving <from> → <to>:` (dry run `Workers would move …`), then one line per switching worker: `showrunner · unit · worker → codexN`, marked `(mid-turn; switches when its turn ends)` when mid-turn; an unlisted holder shows its saved session name, else `unknown session (<run>)`.
- `accounts.py exec codexN|workers [codex args]` resolves the home through `check_codex_target` (for `workers`, the account `role workers` selects), sets `CODEX_HOME` and execs `codex`; `--probe` is Claude-only.

**Files:**
- `scripts/accounts/accounts.py` — macOS cutover, Mac process guard, swap lines, Codex exec.
- `scripts/whoami/agent_accounts.py` — keychain service names.
- `scripts/agents/codex_mesh.py` — `WorkersRunSwitch` / `SwitchingWorker` from `retire_workers_servers`.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the Mac step, natedev's cutover, Codex launch lines.

**Gotchas:** no real Mac keychain or `sysctl` call has run in tests; `security` and the kernel read are stubbed.

### Phase 34 — Cutover on the Mac: the live step  · status: done

#### As-built

- On the Mac, the showrunners group sits on claude2 (`move showrunners claude2`, by hand, 2026-10-16); units are not configured there. Codex stays on `~/.codex` (codex2, workers). The darwin `claude` alias (`accounts.py exec showrunners`, natedev's flags) went to the showrunner for `shell.nix`.
- The user ran `cutover-home claude2` in a Terminal on the Mac on 2026-10-16 at 10:17 EDT: `~/.claude.json` has no `oauthAccount`, the journal reads `complete`, and the Mac's `config/accounts.json` holds its `home_cutovers` row. `docs/accounts.md` says `~/.claude` holds no login on either machine.
- `_mac_login_keychain_visible()` in `scripts/accounts/accounts.py` reads `security list-keychains` (no domain: the search list `find-generic-password` uses). The forward Mac cutover refuses with `accounts: this session cannot see the Mac's login keychain; run cutover-home in a Terminal on the Mac` when `~/Library/Keychains/login.keychain-db` (or `login.keychain`) is not listed, before any keychain read and before the journal or an identity file changes; `--undo` and a rerun of a complete cutover skip it.

**Files:**
- `scripts/accounts/accounts.py` — the login-keychain check.
- `scripts/accounts/test_cutover_home.py` — the fake keychain answers `list-keychains`; refusal, undo and `login.keychain` tests.
- `docs/accounts.md` — the Mac cutover runs in a Terminal; `~/.claude` holds no login on either machine.

**Gotchas:**
- Over ssh the Mac's search list holds only `/Library/Keychains/System.keychain`, while `list-keychains -d user` still lists the login keychain; every keychain entry reads as absent. Claude usage readings take their token from the keychain, so claude2 reads `unavailable` from any ssh session.

**Live check outstanding (with the user):** a claude2 usage reading (`accounts.py` in a Terminal on the Mac shows a weekly number for claude2), and a new `claude` landing in `~/.claude2` once the macrebuild brings the darwin alias.

### Phase 35 — A workers swap moves running workers at once  · status: done

#### As-built

- `retire_workers_servers` (swap) and `command_signin_changed` interrupt every running worker on an outgoing server at once: each worker's account-move marker (`<worker>.account-move.json` in the run folder) is written first, then `turn/interrupt` goes through the one turn-interrupt helper (`_interrupt_turn`, shared with the `interrupt` verb, `steer` and `end`). All interrupts across all runs go out before any wait.
- `_finish_run_moves` / `_wait_account_move_resumes` confirm every continuation against one shared deadline (`ACCOUNT_MOVE_RESUME_TIMEOUT_SECS`, 30 s). A worker whose `launcher_pid` is dead or absent gets a detached `codex_mesh.py resume-account-move --session-dir <run> --to <worker>`.
- A waiting launcher that sees the marker clears it, posts `account moved to codexN; resumed`, and resumes the same thread on the new server with "Your turn was interrupted to move your Codex account; any command you had running was stopped with it, so run it again. Continue where you stopped." An interrupted turn without a marker keeps the user-stop handling. A pending `mesh_runs.py` result is consumed once under the pending-inbox lock, so the resume neither drops nor repeats it.
- Swap lines end `, interrupted and resumed` for a mid-turn worker; a failed interrupt or resume names the reason and the resume command. The swap exits 0 once the role moved and 1 only when the role move fails. `--dry-run` prints the same lines and interrupts nothing. The holder notice and server log say running workers were interrupted and resumed on the new account.

**Files:**
- `scripts/agents/codex_mesh.py` — move marker, shared interrupt, launcher resume, `resume-account-move`, shared-deadline wait.
- `scripts/accounts/accounts.py` — `_workers_swap_lines` suffixes.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — swap behavior.

**Gotchas:**
- The resume relies on no build surviving the interrupted turn: the continuation tells the worker its command stopped and to rerun it.
- The detached resume claims the roster row keeping role, previous_role and lens (read from the agents ledger), with previous_status `failed`, so a rejected resume leaves the printed manual retry usable.

### Phase 36 — A swap leaves ended sessions' workers alone  · status: done

#### As-built

- `_run_is_live(folder, holders)` in `codex_mesh.py`: a run with holders is live only while one of them is in `sessions.running_sessions()`, read per run under its lock; a run with no holder is live only while a roster `launcher_pid` is alive. The swap (`retire_workers_servers`) and the sign-in change both use it.
- An ended run (`EndedWorkersRun`) gets no marker, interrupt, resume or holder notice. Its outgoing servers stop only through `_stop_unused`; a busy one stays running as `KeptWorkersServer` (pid, port, reason) with a watcher.
- `codex_mesh.leftover_server_lines` is the one source of `N leftover servers from ended sessions, M stopped` with `left running:` lines, and the dry run's `…: M can be stopped, K would stay` with `would stay:` lines. `accounts.py` `_workers_swap_lines` folds rows per (holder, slot) through `FoldedWorkerRow` with ` ×N`; failed entries keep their own rows.
- The sign-in change moves a live run only on its current record's server; retired old-sign-in servers count only for ended runs. `_start_watcher` holds `mesh-watcher-<pid>-<port>.lock` for the watcher's life and returns `ExistingServerWatcher` when one already runs.

**Files:**
- `scripts/agents/codex_mesh.py` — liveness, ended-run cleanup, leftover lines, watcher lock.
- `scripts/accounts/accounts.py` — folded swap rows.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — ended-session line and folding.

**Ruled out:** one row per holder for a run two holders share, because a worker belongs to its run.

### Phase 37 — A folded swap row counts its mid-turn workers  · status: done

#### As-built

- `FoldedWorkerRow` in `scripts/accounts/accounts.py` counts mid-turn entries (`mid_turn_count`). `_workers_swap_lines` prints a single entry as `→ <target>, interrupted and resumed` when mid-turn, and a folded row as ` ×N`, then `, K interrupted and resumed` when K of its entries were mid-turn. Failed entries never fold.

**Files:**
- `scripts/accounts/accounts.py` — mid-turn count on folded rows.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the count in the swap output description.

### Phase 38 — Swap rows name a run's launching session  · status: done

#### As-built

- `_launcher_sessions(folder)` in `scripts/agents/codex_mesh.py` names a run with no holder by each live roster `launcher_pid`'s nearest ancestor that a running session record names, counting only records whose `procStart` matches the process. It reuses `process_table.snapshot` and, since Phase 39, `process_table.nearest_named`, so it works on Linux and macOS.
- `WorkersRunSwitch` and `WorkersRunMove` carry `launcher_sessions`; the swap and the sign-in change fill it for live runs with no holder. Liveness, interrupts, resumes and leftover servers are unchanged.
- `_workers_swap_lines` names and folds such a run like a holder (`showrunner · unit · worker`); `unknown session (<run folder>)` remains only when nothing resolves.

**Files:**
- `scripts/agents/codex_mesh.py` — launcher sessions.
- `scripts/accounts/accounts.py` — naming from a launcher session.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — swap row naming.

### Phase 39 — The accounts script has no import cycles  · status: done

#### As-built

- `process_table.nearest_named(process, table, names) -> str | None` is the one ancestry walk: the nearest ancestor (the process included) that `names` holds, `None` when the chain reaches none or loops. `codex_winddown.owner` is gone; `agent_counts` counts a `None` as `NO_SESSION`. `codex_mesh._launcher_sessions` imports only `process_table`, with no `sys.path` insertion.
- `scripts/production/production_units.py` is the one home of the production doc's Units-table reading (`Refusal`, `cell_value`, `unit_rows`, `row_cells`, `unit_headings`, `unit_table`, `plan_cell_is_retired`, `worktree_is_linked`, `SessionMarks`, `unit_is_retired`, `live_unit_table`, `retired_units`). Every user imports from it, `add_unit` included; `roster` and `live_units` no longer import `add_unit`.
- `scripts/notify/reply_asks.py` is the one home of the reply-question parser (`question_prose`, `ReplyAsks`, `reply_questions`, and the review-pool `user_reply_questions` and `peer_question_lead` taken from build-followups at 3a7f182c). `showrunner_footer` imports it, not `review`, which closed the chain accounts → shutdown.inventory → showrunner_footer → notify.review → accounts. `review` imports the parser from it.
- Whole-project basedpyright lists no import cycle through `scripts/accounts/accounts.py`; behavior is unchanged.

**Files:**
- `scripts/message/process_table.py`, `scripts/production/codex_winddown.py`, `scripts/agents/codex_mesh.py` — the ancestry walk.
- `scripts/production/production_units.py` and its importers across `scripts/production`, `scripts/message/roster.py`, `scripts/delegate/phase_table.py`, `scripts/nightly_review`, `scripts/bevy_upgrade` and `scripts/shutdown/restart.py` — the Units table.
- `scripts/notify/reply_asks.py`, `scripts/notify/review.py`, `scripts/hooks/showrunner_footer.py` — the reply-question parser.

### Phase 40 — One role map for both machines  · status: done

#### As-built

- natedev holds the one role map (Claude groups `showrunners`/`units` in `groups_file()`, the workers label in `accounts_config_file()`, history in `roles.jsonl`). `scripts/accounts/role_map.py` owns it: `read_role_map()` / `write_role_map(change)`, `ROLE_MAP_HOST = "natedev"` with `is_role_map_host()` by hostname (tests choose it), and `ssh_role_map()` as the only transport. Every writer (`move`/`move_group`, `swap`, `set-role`, `refusal_watch.py`, the Codex sign-in change) goes through `write_role_map`.
- On the Mac, a write runs `accounts.py role-map-write` on natedev over `ssh natedev` with the same why, cause and move id; a failed one prints `accounts: natedev refused <role> → <label>` and changes nothing locally. A read runs `accounts.py role-map-read` once per command (`role_map_command`) and caches it at `~/.local/state/accounts/role_map_cache.json`; with natedev unreachable it uses the cache and prints `role map from <time in the user's zone>; natedev unreachable`, and with no cache it refuses with `accounts: cannot read role map from natedev; no usable map`.
- A role on an account not set up or not logged in on this machine refuses every launch with `accounts: <role> is on <label>, which is not set up on this machine; <setup step>` (or `… not logged in …; <login step>`); status lists the same lines under the table. Nothing falls back to another account.
- `agent_accounts.workers_home()` raises `RoleAccountUnavailable` when the map names no workers account; observing readers take `observe_workers_home()` → `Path | NoWorkersHome` (whoami prints it, agent_notes adds it to messages).

**Files:**
- `scripts/accounts/role_map.py` — the map, transport, cache and host constant.
- `scripts/accounts/accounts.py` — wrappers, launch refusals, status lines, `role-map-read`/`role-map-write` CLI.
- `scripts/whoami/agent_accounts.py` — `workers_home()` and `observe_workers_home()`.
- `scripts/accounts/refusal_watch.py` — automatic moves through `write_role_map`.
- quota_alert, agent_notes, five_hour, whoami, roster, codex_mesh, `agent_exec.sh` — workers home readers on the new boundary.
- `scripts/accounts/test_role_map.py`, `scripts/accounts/test_accounts.py` — ssh faked: Mac write lands on natedev, Mac read, cache line, refusals.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the one map, ssh write and read, cache line, refusals.

**Binds later work:** role moves made on the Mac from 2026-10-16 on are lines in natedev's `roles.jsonl` with no field naming the machine; the Mac's own `groups.json`/`roles.jsonl` hold only its earlier moves, including the 2026-10-16 setup moves (showrunners claude2 → claude1, units claude4, workers codex2 → codex1), which are setup, not refusals ("Readout of the moves").

**Gotchas:**
- The Mac runs natedev's promoted `~/.claude` accounts.py over ssh, so it depends on natedev's promoted code; before promote, a Mac with no cache refuses role launches.
- A remote write is checked against the Mac's accounts only; natedev accepts it without checking its own accounts, and natedev's launch then refuses loudly.
- Before this phase the role writer rewrote `config/accounts.json` keeping only `workers`, which dropped the Mac's claude2 `home_cutovers` row on 2026-10-16; `role_map.write_role_map` keeps every key.

### Phase 41 — Codex logins out of ~/.codex  · status: done

#### As-built

- `~/.codex` holds no login on either machine, as `~/.claude` holds none: codex2's `auth.json` lives in `~/.codex2`, beside `~/.codex1`. The `codex` executable run without `CODEX_HOME` is signed out; the `codex` alias (`accounts.py exec workers`) still selects the mapped numbered home. The cutover ran on natedev 2026-10-16 11:53 EDT and on the Mac 12:00 EDT; each machine's own `config/accounts.json` holds its Codex `home_cutovers` row.
- `accounts.py cutover-home codexN` (`cutover_codex_home`) checks that `~/.codex` holds codexN's login (note login against `codex_email_on_disk`), links a missing `~/.codexN` through `_link_codex_home` (split from `_setup_codex_home`, so `setup` keeps its duplicate-login refusal and the cutover starts no Codex process), then renames `~/.codex/auth.json` to `~/.codexN/auth.json` and writes the `home_cutovers` row idempotently. `--undo` renames it back. `cutover_home` dispatches a Codex label before its macOS branch, so a Codex cutover runs no keychain check; the Claude forms are unchanged.
- Journal `~/.local/state/accounts/cutover-codex-home.json` (`moving`, `complete`, `undoing`, `undone`) is written before the rename: a rerun finishes an interrupted move (after the rename, the history write or the undo rename, or with the source `auth.json` already gone), a changed source login behind a `moving` journal refuses, a rerun of a complete cutover does nothing, and `~/.codex` signed in again after a complete move takes a fresh cutover.
- The cutover refuses before any change while a `codex` process runs with `CODEX_HOME` unset or set to the home that loses the login (`~/.codex` forward, `~/.codexN` on undo), and fails closed with `ProcessEnvironmentUnread` when an environment or the process listing cannot be read, naming each pid and its command and the step `stop it, then rerun cutover-home`. It never stops a process. `account.process_codex_home` reads the home: Linux from `/proc/<pid>/environ` (that key only), the Mac from the kernel argument record (`account.mac_process_record`, `kern_procargs2`), which `accounts._mac_process_arguments` shares.
- Status prints `~/.codex holds a login; run accounts.py cutover-home codexN` while `~/.codex` holds one and drops the `~/.codex` row from the account table once it holds none. History (`accounts.py` historical-label resolution and `moves.py`'s reader) resolves a `~/.codex` home spelling from the Codex `home_cutovers` row and leaves it unresolved where that evidence does not reach.

**Files:**
- `scripts/accounts/accounts.py` — the Codex cutover, its journal and refusal, the status line, historical-label resolution.
- `scripts/accounts/moves.py` — `~/.codex` as a historical home resolved from the Codex row.
- `scripts/whoami/account.py` — `process_codex_home` and the Mac kernel argument record reader.
- `scripts/accounts/test_cutover_home.py` — the cutover, refusals, recovery, rerun, undo and status line against a temporary home with a fake process list.
- `scripts/accounts/test_moves.py` — history across the Codex cutover.
- `scripts/whoami/test_account.py` — the kernel argument record reader.
- `scripts/accounts/test_accounts.py` — the status table without the signed-out `~/.codex` row.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — no login in `~/.codex`; the alias and the bare executable.

**Binds later work:** Codex server cleanup on the Mac reads a process's Codex home with `account.process_codex_home` and never stops a `codex` inside an application bundle. Readout of the moves resolves a `~/.codex` label from the Codex `home_cutovers` row written at each machine's cutover.

**Gotchas:**
- `ps` text cannot separate argv from environment (a prompt carrying `CODEX_HOME=/x` would pass the guard), so the Mac never reads a process environment from `ps -E`.
- macOS `ps -o ucomm=` pads the name with trailing spaces; strip before matching, and keep fake listings padded.
- The Mac's ChatGPT desktop app runs its bundled `codex` (inside `ChatGPT.app`) on `~/.codex`: the cutover refuses until the app quits, and the app is signed out of `~/.codex` afterwards.
- Codex rewrites `auth.json` on token refresh, so a process left on `~/.codex` would put a login back. `auth.json` is only renamed, never read, printed, copied or logged.

### Phase 42 — Codex server cleanup on the Mac  · status: done

**Depends on:** Phase 41

#### As-built

- The Mac judges Codex servers as natedev does: `sweep`, `sweep --stop`, the per-server idle watcher (`idle-stop`) and `signin-changed` reach the same verdicts on both machines. `_running_servers()` → `list[WebSocketServer]` (`pid`, `port`, `age_secs`) reads `ps -eo pid=,etime=,args=` on the Mac (`etime` parsed from `[[dd-]hh:]mm:ss`) and `etimes` on Linux; `_client_counts()` reads `lsof -nP -iTCP -sTCP:ESTABLISHED` on the Mac and `ss` on Linux; `_server_folder(pid)` reads `lsof -p <pid> -a -d 2 -Fn` on the Mac and `/proc/<pid>/fd/2` on Linux.
- Every cleanup command goes through `_cleanup_command`, which raises `OSError` naming the command (never its output) on a missing, denied or timed-out (10 s) command, a non-zero exit or any stderr; `lsof`'s exit 1 with no output is an empty selection (zero clients, no log). A failed command refuses to judge: `sweep` prints `nothing judged` and exits 1, `idle-stop` and cleanup keep the server and print why.
- The server folder is `MeshServerLog(folder) | NonMeshServerStderr | UnreadServerStderr(reason)`. An unread one becomes `UnreadServerOwnership(reason)` with a `ServerInUse` verdict, a named keep reason in `sweep`, `idle-stop` and `signin-changed`, never an unowned server. On the Mac an empty stderr selection counts as no mesh log only when `ps -o uid=` confirms the pid is this user's.
- A WebSocket server or daemon whose executable sits inside an application bundle (`*.app/Contents/`, judged by `_process_executable`, the kernel's path, never argv[0]) is never judged unused or signalled; an unreadable executable path keeps the server.
- `sweep` also lists Codex's own `app-server daemon`. `_running_daemons()` lists this user's `codex` processes (`ps -u <uid>`, `ucomm=` stripped on the Mac) and classifies each by `_process_identity` (`ProcessIdentity(pid, started, arguments, executable)`, start time read before and after) into `CodexDaemon | UnclassifiedCodexProcess | UnreadCodexProcess`; config flags before the subcommand are skipped, and an `exec` prompt naming a daemon stays unclassified. `_process_cleanup_verdict` judges a daemon unused only when its home (`account.process_codex_home`) is `~/.codex` and `~/.codex/auth.json` is confirmed absent; a numbered home, an unread environment, an existing login or an unconfirmed absence keeps it with that reason. The daemon line is `<pid>\tdaemon\t<label>\t<reason>\t~/.codex`, an unread record prints `<pid>\tunclassified\tkept\t<reason>\t-`, and the count line includes daemons.
- `sweep --stop` stops an unused daemon with `_stop_daemon`: it snapshots the family (`_daemon_process_tree`, identities bound to two `process_table` snapshots), then SIGSTOPs the parent and its descendants to a fixed point, tracking members by identity so a reparented child stays tracked, and checks `_process_cleanup_verdict` before each STOP and again at the fixed point. It then SIGKILLs children before the parent, rechecking the parent's verdict and every member's identity and stopped state before each signal, and reports success only when every member is gone within `REAP_WAIT_SECS`. Any refusal prints `codex_mesh: daemon family not stopped: pid <pid>: <reason>`, and `_resume_daemon_family` sends SIGCONT, by pid plus start time, only to members this cleanup stopped (`FrozenDaemonMember(identity, stopped_by_cleanup)`); a member it cannot resume is named as "may still be stopped".
- `account.MacProcessRecord` carries `executable: Path` from the kernel argument record. `role_map.write_role_map` keeps every other key of `config/accounts.json`, `home_cutovers` included.
- Live, sweep judging only on 2026-10-16: natedev listed 15 servers in use and kept a daemon on `~/.codex1`; the Mac listed none.

**Files:**
- `scripts/agents/codex_mesh.py` — platform listing, client counts and server folder, the unread-ownership keep, daemon discovery, verdict and family stop.
- `scripts/agents/test_codex_mesh.py` — faked `ps`/`lsof` for the Mac (`MacCleanupTests`), daemon verdicts (`CodexDaemonCleanupTests`), process state (`DaemonProcessStateTests`), freeze, kill and resume (`DaemonFamilySafetyTests`).
- `scripts/whoami/account.py` — `MacProcessRecord.executable`.
- `scripts/whoami/test_account.py` — the executable path read from the kernel record.
- `scripts/accounts/test_role_map.py` — a workers change keeps `home_cutovers` and every other key.

**Binds later work:** No Codex daemon descendant escapes the stop builds on `FrozenDaemonMember`, `_freeze_daemon_process`, `_resume_daemon_family`, `_daemon_process_tree`, `_process_identity` and `sessions.process_start`; any session sweep it adds checks `_process_cleanup_verdict` before SIGSTOP, and it states that a process that called `setsid` is out of reach. `_stop_daemon` takes no lock, and the daemon line's home column is fixed at `~/.codex`.

**Gotchas:**
- Codex `app-server --listen` calls `setsid`, so the daemon's session id does not cover its family; a process born and orphaned between two listings escapes snapshot ancestry and survives the stop.
- macOS `ps -o ucomm=` pads the name with trailing spaces; strip before matching, and keep fake listings padded.
- The ChatGPT desktop app's bundled `codex` runs on `~/.codex`; the bundle exclusion is what keeps it running.
- The periodic `sweep --stop` on both machines is a nix job in the nixos repo (`nate.jobs.codex-mesh-sweep`, every 1800 s), not code in this repo.

**Ruled out:** SIGTERM before SIGKILL — resuming a stopped process to let it handle TERM lets it fork again; re-listing the family by parent pid at kill time — it loses reparented children.

### Phase 43 — Codex cutover finishes the first startup  · status: done

**Depends on:** Phase 41

#### As-built

- `accounts._prepare_codex_home(label)` prints setup's wait line and runs `agent_accounts.prepare_codex_home` on the numbered home (`codex app-server --listen stdio://`, waiting up to `CODEX_HOME_PREPARATION_SECONDS`, 1200 s, for `initialize` while Codex builds its state database over the shared sessions). `setup codexN` (`_setup_codex_home`) and `cutover-home codexN` share it.
- `cutover_codex_home` runs it after the login rename, the `home_cutovers` row and the `complete` journal write, and prints `<label> (~/.codexN) login moved; shared session index ready.` only once Codex answers `initialize`.
- A failed preparation leaves the journal `complete` and raises `AccountsError` with one line: `accounts: ~/.codexN login moved, but its shared session index is not ready (<cause>); run accounts.py setup codexN to finish the index`. A rerun of a complete cutover and `--undo` run no preparation.
- `docs/accounts.md`'s cutover section describes the first startup, its wait of up to 20 minutes, and the `setup codexN` recovery.

**Files:**
- `scripts/accounts/accounts.py` — `_prepare_codex_home`, shared by setup and the Codex cutover.
- `scripts/accounts/test_cutover_home.py` — with a faked preparation: login moved before preparation, the ready line, the failure line with the journal `complete`, a rerun after failure, undo without preparation.
- `docs/accounts.md` — the cutover's first startup and recovery.

**Gotchas:**
- Preparation runs under the exclusive cutover lock for up to 1200 s, so an undo cannot rename the login back while Codex starts on it.
- An interrupted backfill leaves `backfill_state.status = running`, and Codex holds startup for 15 minutes on that lease; a `codexN` run that meets a first backfill fails with "app server did not become ready", and `accounts.py setup codexN` repairs the home.
- Tests fake `prepare_codex_home`; a suite run against the real one starts Codex.

### Phase 44 — Shared links repair themselves  · status: done

#### As-built

- `_repair_shared_links(destination, source_dir, shared, folders)` runs before any judgment on setup, Codex home linking, `prepared_account`, `check_codex_target(..., repair=True)` and move targets (`_move_target`, `check_move_target`), so a launch never refuses on something repair fixes.
- Declared folders (`ALWAYS_LINKED`, which now names every Claude folder created on demand including `shell-snapshots`; `CODEX_ALWAYS_LINKED` for Codex) are created private inside an existing shared home. A missing shared file and the shared home itself are never created.
- Repair rules: a real entry moves into a missing shared target and is linked; an empty real folder is replaced by the link silently; a non-empty folder merges entry by entry when no name clashes; a clash leaves everything in place and prints a repair step naming the clashing entries; a real file refuses only when the shared target exists. Adoption and merge notices go to stderr.
- Read-only judgment: `prepared_account_preview`, `check_codex_target(..., repair=False)` and `_inspect_account_directory(..., allow_shared_repair=True)` use `_shared_repair_problems` to count repairable entries as ready without touching disk (refusal-watch previews, automatic selection, `set_role`).
- `accounts.py check` stays strict; a declared folder whose shared target is absent is skipped only when the account entry is absent or already the expected link.

**Files:**
- `scripts/accounts/accounts.py` — `_repair_shared_links`, `_move_shared_entry`, `_shared_repair_problems`, `ALWAYS_LINKED`, `CODEX_ALWAYS_LINKED`, `prepared_account_preview`, `repair=` on target checks.
- `scripts/accounts/refusal_watch.py` — previews through the read-only path.
- `scripts/accounts/test_accounts.py`, `scripts/accounts/test_refusal_watch.py` — every repair case on scratch homes, Claude and Codex; a preview leaves both homes untouched.
- `scripts/whoami/test_agent_notes.py`, `scripts/production/test_add_showrunner.py`, `scripts/shutdown/test_account_move.py` — fixtures follow the repair rules.

**Gotchas:** `Path.rename` replaces a destination created after a clash check, so every move goes through `_move_shared_entry` (renameat2 `RENAME_NOREPLACE` on Linux, renamex_np `RENAME_EXCL` on macOS); a destination appearing mid-repair is a clash, and a losing concurrent repair accepts the winner's state. Any check reachable from `set_role` must pass `repair=False`, or it creates `~/.codex`. A move refused by its target prints one line, the first problem.

### Phase 45 — No Codex daemon descendant escapes the stop  · status: done

**Depends on:** Phase 42

#### As-built

- `_stop_daemon` takes the nonblocking `_daemon_stop_lock()` (`~/.local/state/codex-mesh/daemon-stop.lock`) and runs `_stop_daemon_locked`; a held lock keeps the family and states the reason.
- `_own_process_table()` lists only this user's processes (`ps -u <uid> -o pid=,ppid=,args=`, split on the two numeric columns only); `_daemon_process_tree()` reads identities only for family members and swept-session candidates.
- `_process_session(pid)` returns `ProcessSession | SessionGone | SessionUnread`, read once per candidate per table. After the freeze, every session led by a frozen member (`_frozen_daemon_sessions`) is swept to a fixed point before the kill; new processes join the family with exclusions checked before STOP. A session whose leader is outside the family is never swept; `SessionUnread` refuses the stop.
- After the kill, a survivor in a swept session is reported by pid and the stop is not a success. The fixed-point deadline is `max(REAP_WAIT_SECS, 4 × slowest listing)`.
- `_recover_kept_daemon` / `_recover_stopped_codex_members` read the whole family of a kept daemon (in use, signed in, refused or failed stop) or a stopped daemon: a stopped member of any name on `~/.codex` with no controlling terminal and unchanged identity is reported and, under `--stop`, resumed under the lock. A member with a terminal, an unread terminal or environment, a numbered home, or the bundled ChatGPT codex is reported as kept and never signalled.
- The sweep's daemon line prints the daemon's own home via `_daemon_home` (from `account.process_codex_home`).

**Files:**
- `scripts/agents/codex_mesh.py` — session reader, session sweep and survivor check in `_stop_daemon_locked`, candidate-only identity reads, the stop lock, stopped-member recovery, the daemon home column.
- `scripts/agents/test_codex_mesh.py` — orphan found by session, survivor reported, exclusions before STOP, untracked-leader session never swept, stopped member resumed, daemon home column; `MeshCommandTests` isolates its run folder.

**Gotchas:** `getsid` on an exited pid is `SessionGone`, never a refusal; match on the type, never the message. macOS `ps comm` is a full path with spaces, so the own-user table splits only pid and ppid. macOS `os.getsid` reads every own-user process, so the session sweep works on Darwin. A stopped process with a controlling terminal is a job-control stop and is never resumed. A descendant that calls `setsid` itself leaves every tracked session and is out of reach; the `_stop_daemon_locked` doc comment states this limit.

**Ruled out:** resuming every stopped `~/.codex` process (wakes a user's Ctrl-Z job); sweeping a session whose leader is outside the family; reading identities for every process on the machine.

### Phase 46 — Role map stays inside a test's home  · status: done

**Depends on:** Phase 40

#### As-built

- `role_map.is_role_map_host()` is true on natedev by hostname and, on any machine, wherever `Path.home()` resolves differently from the password-database home for the current uid. Both paths are resolved before the comparison, so a symlinked alias of the login home is the login home; a resolve failure counts as a scratch home.
- `_login_home()` returns `LoginHome(path) | LoginHomeUnread` from `pwd.getpwuid(os.getuid())`; an unread login home counts as a scratch home. Tests pin `_login_home` beside the hostname.
- A scratch `HOME` holds its own map on every machine: `read_role_map()` reads its `groups.json` and `config/accounts.json`, `write_role_map()` writes those files and appends its `roles.jsonl`, no ssh runs and `role_map_cache.json` is never written.
- The real `HOME` off natedev is unchanged: reads and writes go to natedev over ssh, a successful read refreshes the cache, and an unreachable natedev falls back to the cache. The remote side (`role-map-read`/`role-map-write`) is unchanged.

**Files:**
- `scripts/accounts/role_map.py` — `LoginHome`, `LoginHomeUnread`, `_login_home`, `is_role_map_host`.
- `scripts/accounts/test_role_map.py` — scratch-home read and write stay local with no ssh and no cache change; a symlinked alias in either direction still reads remotely; an unread login home keeps reads and writes local; `setUp` pins the login home to the test `HOME` so the remote tests keep the remote path.

**Gotchas:** ssh reads `~/.ssh` from the password-database home, not `$HOME`, so before this phase a scratch `HOME` on the Mac reached natedev's real map. `is_role_map_host()` is now true for a scratch home on any machine, so its name and the refusals that name natedev (`cannot read role map from natedev`, `could not write … on natedev` in `role_map.py`, `role map belongs on natedev` in `accounts.py`) overstate the local case; a follow-up renames it to `holds_role_map()` (or returns `LocalRoleMap | RemoteRoleMap`) and names the home in local refusals. `scripts/herdr/test_herdr_sessions.py` imports `herdr` at top level and needs `PYTHONPATH=<tree>:<tree>/scripts/herdr` to run alone. Suites that pin `is_role_map_host` stay valid.

**Ruled out:** hostname alone decides who holds the map (a scratch `HOME` off natedev then reads and writes natedev's real map).

### Phase 47 — Accounts status as JSON  · status: done

**Depends on:** Phase 46

#### As-built

- `accounts.py status --json [--stored] [--with-login]` prints one object, `{"accounts": [...], "problems": [...]}`. Accounts follow the table's order; `problems` holds the note lines the table prints without `--detail`. Each account carries `label`, `tool` (`claude`/`codex`), `roles` (a list, because both groups may share an account; `["workers"]`/`["spare"]` for Codex), `weekly_left_percent`, `resets_at` and `read_at` (ISO 8601 UTC), `credits` (a number, `"unlimited"` or null; null for Claude) and `use_credits` (false for Claude; true for Codex unless its note says `use_credits: off`). Readings the table shows as `—`, `unavailable` or a setup step are null. `login` appears only with `--with-login` and adds no read.
- One reader, `read_status(*, source, detail)`, returns a `StatusReading` of typed `StatusRow`s plus notes; `render_status_table` and `render_status_json` both draw from it, and `status_report` is the table renderer over that reader. A missing value is `StatusUnavailable(cell)`, which keeps the table's cell text and renders as JSON null.
- `UsageSource = RefreshStaleUsage | StoredUsageOnly` chooses the usage reads; `role_map.RoleMapSource = FetchRoleMap | StoredRoleMap` chooses the role-map read, set for the command by the `role_map.stored_role_map()` context.
- `--stored` (table or JSON) makes no live read and starts no subprocess. Off natedev it reads `role_map_cache.json` only: never ssh, never a cache write. With no cache, roles are empty, the problem line is in `problems`, and the command exits 0. Without `--stored`, an unreadable role map still refuses status with exit 1, for table and JSON.
- `--json --detail` and `--with-login` without `--json` are usage errors.

**Files:**
- `scripts/accounts/accounts.py` — `RefreshStaleUsage`, `StoredUsageOnly`, `UsageSource`, `StatusUnavailable`, `StatusRow`, `StatusReading`, `StatusAccountDocument`, `StatusDocument`, `read_status`, `_read_status`, `render_status_table`, `render_status_json`, the `status` flags.
- `scripts/accounts/role_map.py` — `FetchRoleMap`, `StoredRoleMap`, `RoleMapSource`, `stored_role_map()`.
- `docs/accounts.md` — "Every account in one table" documents the three flags, the JSON keys and the `--stored` cost rule.
- `scripts/accounts/test_accounts.py`, `scripts/accounts/test_role_map.py` — byte-identical table through the reader, the full JSON fixture, `--with-login`, equal live-read counts for table and JSON, `--stored` with every live read and subprocess patched to fail, and the cache-only role map off natedev.

**Binds later work:** handler-enh-unit (cargo-handler's summary) and unit-board-unit (the accounts view) consume `status --json`; `--stored` is the cheap rerun form, 0.21 s with no ssh on the Mac real home.

**Gotchas:** 32 failures in the accounts suites on the Mac predate this phase; a follow-up phase owns them.

### Phase 48 — Account tests pass on the Mac  · status: done

**Depends on:** Phase 47

#### As-built

- `scripts.accounts.test_accounts`, `test_role_map` and `test_cutover_home` pass on the Mac as on natedev, 376 tests on both; production files are unchanged.
- Every test class, and `test_accounts`' module setup, resolves its temporary root where it creates it (`Path(...TemporaryDirectory()).resolve()`), so every derived path is already resolved; the `MacHomeCutoverTests` interruption test resolves its per-subtest home and sets `HOME` from it.
- `HomeCutoverTests.setUp` pins `sys.platform` to `"linux"`, matching its `/proc`, socketless-process and `~/.claude.json` login fakes; a darwin test inside it patches locally.
- `test_status_describes_accounts_without_creating_any` runs under `"linux"` (`run accounts.py cutover-home`) and `test_mac_status_describes_accounts_without_creating_any` under `"darwin"` (`accounts.MAC_HOME_LOGIN_STEP`), both through `assert_status_describes_accounts_without_creating_any(home_step)`.

**Files:**
- `scripts/accounts/test_accounts.py` — resolved module and class roots; the per-platform status pair and its shared helper.
- `scripts/accounts/test_cutover_home.py` — resolved class and subtest homes; the linux pin in `HomeCutoverTests`.
- `scripts/accounts/test_role_map.py` — fixture root resolved before both scratch homes.

**Gotchas:** macOS `/tmp` links to `/private/tmp`, so a fixture compared against `resolve()` resolves its temporary root at creation; a symlinked `TMPDIR` on natedev reproduces it. A test that fakes one platform's process or login reads pins `sys.platform` to that platform; unpinned, the Mac takes the darwin branch and reads the real process table or keychain. Run the account suites on the Mac from a scratch copy of the tree with a scratch `HOME`.

**Ruled out:** skipping a test by platform (each platform's case is pinned and runs on both machines).

### Phase 49 — Automatic group moves by rule  · status: done

**Depends on:** Phase 47

#### As-built

- `auto_moves` in `~/.claude/config/accounts.json` is a list of `{"name", "group", "to", "when"}` rules, `when` being `{"left_at_most": <percent>}` or `{"after_reset": true}`. `refusal_watch.read_auto_move_rules()` returns `AutoMoveRule(name, group, to, when: LeftAtMost | AfterReset)` or `MalformedAutoMoveRule(reason)`; a malformed rule (bad group, label or percent, duplicate name) prints `auto_moves <name>: <reason>` and the rest still run. `docs/accounts.md` "Automatic group rules" holds the three seed rules: `units-low`, `showrunners-low` (1% → claude2) and `units-claude3-reset`.
- `refusal_watch.run_auto_moves(now, *, dry_run, moved_groups)` runs in `_run` after the refusal pass, under `_watch_lock`, on natedev by hostname only. A rule fires only on fresh readings, and never for an unconfigured group, a target that is the group's account, reads at or below the highest `left_at_most` in the list, or holds an unresolved refusal, a group the refusal pass moved this run, or a group with a shutdown in progress (`settle.shutdown_in_progress`, the conductor's terminal-state judgment, so retained finished records do not block). An unavailable or signed-out target prints `auto: <rule>: target <label> …` and the next rule runs.
- `after_reset` keeps `auto_moves[<rule>] = {observed_reset, acted_reset}` in `refusal_watch.json`: first sight records the target's weekly reset without firing; once that reset has passed and the target reads room, the rule fires once and records the next reset. The state is written before members are dispatched; `--dry-run` writes nothing.
- A firing rule runs `prepared_account`, `set_group_account(group, to, why="auto: <rule>", origin=AutoOrigin())`, then `move_sessions` for off-target members with `alerts="none"`, under one move_id. Members get the existing production-showrunner stopping and outcome notices; the phone gets nothing. A group with no off-target member gets only the selection and its `roles.jsonl` line.
- Cause `auto` (window `unknown`, no reset) runs through `accounts.MoveOrigin = RefusalOrigin | HandOrigin | AutoOrigin`, `MoveOriginFields`, `role_map.StoredGroupSelection`, `RoleMapChange`, `RoleChangeDocument`, `RoleHistoryEntry` and `moves.RequestedMove`; older lines still parse. `move_origin_arguments(AutoOrigin())` is `("--auto",)`, and `shutdown.py down --auto` (exclusive with `--refused`; `--reset` still needs `--refused`) rebuilds it for peer and printed retry commands.
- `GroupTransition.why` and `StoredGroupSelection.why` (stored in `groups.json`) keep the rule reason, so a completion retry in `_run` journals `auto: <rule>`.
- `settle.conduct` collects `handled_requests`; `report_stop` lists only those requests' records, local and peer. Records that finish other than `restarted` stay in `sessions/` for `shutdown.py forget`.
- With both groups on one account the status Role cell reads `units, showrunners`.

**Files:**
- `scripts/accounts/refusal_watch.py` — rule types and parser, `run_auto_moves`, `AutoResetObservation` state, `ShutdownProgressReader`, the auto why in group completion.
- `scripts/accounts/accounts.py` — `AutoOrigin`, the cause and argument helpers, `GroupTransition.why`.
- `scripts/accounts/role_map.py` — `auto` cause, `StoredGroupSelection.why`.
- `scripts/accounts/moves.py` — `auto` in `RequestedMove` and `_valid`.
- `scripts/shutdown/settle.py` — `shutdown_in_progress`, the handled-request report filter.
- `scripts/shutdown/shutdown.py` — `down --auto`.
- `docs/accounts.md`, `commands/showrunner/accounts.md` — the rules, seed config and cause.
- `scripts/accounts/test_refusal_watch.py`, `test_accounts.py`, `test_role_map.py`, `test_moves.py`, `scripts/shutdown/test_settle.py` — threshold edges, fresh against stale, once per reset, each guard, journal cause and why, no phone alert, the rendered Role cell, retries and peer forwarding keeping `--auto`, the filtered report.

**Binds later work:** the moves readout counts cause `auto` with why `auto: <rule name>` in `moves.jsonl`, `roles.jsonl` and `groups.json`, one transition per group move_id; a skipped rule writes no journal line.

**Gotchas:** the rules do nothing until `auto_moves` is added to natedev's config. An older peer CLI rejects `--auto`, leaving only a printed diagnostic and retry command, so an auto move with members on the other machine needs this `shutdown.py` there. `run_auto_moves` reaches `settle` through `import_module("settle")` and `ShutdownProgressReader` to avoid a static import cycle.

**Ruled out:** carrying the auto cause in an environment key (peer forwarding and printed retries lost it).

### Phase 50 — Named states for auto-move reasons and reset progress  · status: done

**Depends on:** Phase 49

#### As-built

- `role_map.TransitionReason = RecordedTransitionReason(text) | UnavailableTransitionReason`, re-exported by `accounts`. `role_map.selection_reason(selection)` decodes a stored `groups.json` selection: an absent `why` (written before Phase 49) is unavailable; any present text, `""` included, is recorded.
- `accounts.GroupTransition.why: TransitionReason` (default `UnavailableTransitionReason()`): `group_transition` decodes through `selection_reason`, `set_group_account` returns a recorded reason, and the refusal pass records its refusal why. The refusal-watch completion pass journals an auto transition's recorded `auto: <rule>` on every retry (`""` for an unavailable one); other origins journal `complete <group> move to <label>`.
- `refusal_watch.AutoResetObservation` is a frozen dataclass: `observed_reset` an aware `datetime`, `acted_reset: NeverActedOnReset | ActedOnReset(at)`, zones checked in `__post_init__`. `AutoResetObservationDocument` and `WatchStateDocument` are the JSON forms: `_load_state` decodes, and every `refusal_watch.json` write goes through `_save_state` (`_state_document`), keeping the two strings and `acted_reset: ""` for never acted.
- `run_auto_moves` fires `after_reset` once per reset by comparing datetimes; first sight, a reset passing while the target lacks room, and `--dry-run` writing nothing behave as in Phase 49. Journal and state-file bytes are unchanged.

**Files:**
- `scripts/accounts/role_map.py` — `TransitionReason` and its two states, `selection_reason`.
- `scripts/accounts/accounts.py` — typed `GroupTransition.why`, the re-exports.
- `scripts/accounts/refusal_watch.py` — reset-progress states, document types, `_save_state`, the typed completion reason.
- `scripts/accounts/test_accounts.py`, `test_role_map.py`, `test_refusal_watch.py` — a historical selection's unavailable reason, the auto reason through a round trip and a completion retry, legacy never-acted and acted state, malformed and naive times, successive resets, dry runs.

**Binds later work:** an auto transition's `GroupTransition.why` is `RecordedTransitionReason("auto: <rule name>")`, and the journalled why text is the same as before.

**Gotchas:** an unparsable or naive `observed_reset` or `acted_reset` drops that observation at load, so the next fresh reading is a first sight; the bad entry stays on disk until another state change rewrites the file. Observations compare by instant, so a reset reported under another UTC offset is the same reset. State changes are detected by comparing a `deepcopy` of the loaded state by value.

### Phase 51 — Hold before an automatic move, and chained targets  · status: done

**Depends on:** Phase 50

#### As-built

- `LeftAtMost(percent, hold_minutes: float = 0)`: a `left_at_most` condition accepts an optional `hold_minutes`, finite and nonnegative; absent or `0` is no hold. A bool, negative, non-finite, non-numeric or overflowing value prints `auto_moves <name>: hold_minutes must be a finite nonnegative number` and later rules still run. `after_reset` takes no hold.
- `refusal_watch.HoldStarted(account, at, move_id: str | accounts.NoGroupTransition, percent)` is frozen, with `at` zone-checked in `__post_init__`. `WatchState.auto_move_holds[<rule name>]` holds it; `HoldStartedDocument {account, at, move_id, percent}` is the `refusal_watch.json` form, with `""` for a legacy selection's missing transition. An unparsable, naive or incomplete saved hold is dropped at load.
- `run_auto_moves` discards a rule's saved hold when the group's account, its selection `move_id` or the rule percent differs from the stored one, so a move away and back between timer passes starts a new clock. For a held rule it skips an unconfigured group, a group already on the target or moved this run, then reads the source account: a stale or missing reading leaves the hold, a fresh reading above the percent clears it, and the first fresh reading at or below it stores the pass time. The target guards run only once `hold_minutes` have elapsed. A rule without a hold checks the source reading after the target guards, as in Phase 49.
- A recorded move clears the holds of every rule for that group. Rules run in config order and skip a target that is the group's account, so `units → claude4` then `units → claude2` moves to claude4 first and to claude2 after claude4 reads low; when both holds have elapsed and the first target is unavailable or lacks room, the next rule moves the group.
- `--dry-run` keeps hold changes in memory and writes nothing.

**Files:**
- `scripts/accounts/refusal_watch.py` — `LeftAtMost.hold_minutes` and its parsing, `HoldStarted`, `HoldStartedDocument`, hold load and save, the hold pass in `run_auto_moves`.
- `docs/accounts.md` — `hold_minutes`, the claude4-then-claude2 chain example, the stored hold and what clears it.
- `scripts/accounts/test_refusal_watch.py` — the full interval, a fresh high reading restarting the clock, stale and missing readings, an account change, a round trip between passes, a percent change, malformed and oversized values, the chain, a zero hold, dry runs.

**Binds later work:** holds live in `refusal_watch.json` `auto_move_holds`, keyed by rule name and bound to account, selection `move_id` and percent; a recorded group move clears every hold for that group; rule order is chain order.

**Gotchas:** the hold start is the pass time, not the reading's timestamp. Holds for a rule no longer in config stay in `refusal_watch.json`; nothing prunes them.

**Ruled out:** clearing a hold only on an account-label change (a move away and back between passes reused the old clock).

### Phase 52 — What happens next: one-shot Claude moves replace the low-quota alert  · status: done

**Depends on:** Phase 51

#### As-built

- `scripts/accounts/next_moves.py` keeps the "what happens next" log in `~/.local/state/accounts/next_moves.json` on natedev: an ordered JSON list of `NextMove(id, group: Literal["units", "showrunners"], at_percent, hold_minutes, to, added_at)`, frozen and checked in `__post_init__` (percent 0–100, finite nonnegative hold, `to` a numbered Claude label, aware `added_at`). `encode_next_moves` / `decode_next_moves` are the only JSON boundary; a bad log or duplicate id raises `NextMoveError`. Ids are `uuid4().hex`. `write_next_moves` replaces the file atomically under `next_moves_lock()`, and both refuse off natedev; the Mac reads through `role_map.ssh_role_map(("next-read",))`, adds through `next-write <one encoded entry>` and removes through `next remove <id>`.
- `accounts.py next add <units|showrunners> <percent> <account> [--hold-minutes N]`, `next list`, `next remove <id>`. `add` refuses an unknown group or account (`claude_account_dir` on natedev) and prints the stored entry; `list` prints `<id>: when units reach 1% → claude2 (hold 30 minutes)` per entry, or `nothing set up`. `/next_moves` (`commands/next_moves.md`) turns ordinary words into these commands and prints the list after every change.
- `refusal_watch.read_auto_move_rules()` returns `AutoMoveRule(entry.id, group, to, LeftAtMost(at_percent, hold_minutes))` per entry in log order, or one `MalformedAutoMoveRule` for an unreadable log. `run_auto_moves` takes `next_moves_lock()` inside the watch lock (a dry run takes neither) and keeps Phase 51's guards, holds and chain, with holds keyed by entry id. Once `set_group_account` records the choice, the entry leaves the log before members are dispatched; a skipped or refused move keeps it. Each pass first drops any entry, and its hold, whose `auto: <id>` already stands as a cause-`auto` Claude-group line in `roles.jsonl` (`_recorded_auto_move_ids`), so an interrupted log write never replays a recorded move. `next remove` runs inside `refusal_watch.removing_next_move(id)`, which holds the watch lock and then drops that entry's hold.
- `after_reset`, its reset-progress types and the `auto_moves` config key are gone; `auto_moves` in `refusal_watch.json` is ignored on load and dropped on the next write. Moves keep cause `auto` and why `auto: <entry id>`.
- Claude low quota (`scripts/whoami/quota_alert.py`): no session alert, repeat, `Quota restored:` notice or stop-work line. An episode opens at ≤ 2% on an in-use account; `claude_next_move_push` sends push `quota.claude_next_move` once when a group on that account has no log entry, and returns a status note, leaving the push due, when every group is covered, no group uses the account, or the log cannot be read. A Claude episode closes silently on recovery or once its stored `weekly_reset` has passed. `/quota_ack`, `acknowledge()` and the `ack` verb are gone for both tools, and a saved `acknowledged` field is dropped on load, so a Codex low alert repeats every `repeat_minutes` until recovery. `/quota_refresh` stays.
- `quota.claude_next_move` (`scripts/notify/push_catalog.py`, approved in `push_texts_reviewed.json`, replacing `quota.claude`): title `Claude work on {account} may stop`; body `{account} has {remaining} of its weekly usage left, and no move is set up for the units or showrunners on it. When it runs out, they move to a Claude account with room, or stop if none has any.`; action `In the natedev session, type /next_moves and the move you want, for example: when units reach 1% switch to claude2.`
- `waiting.py quota` relays only Codex notices: a Claude account line, or a notice that does not name Codex, prints `Claude quota notices no longer relay`. `drop_claude_quota_waits` removes saved Claude waits from `quota_seen.json` on every read and quota call. `commands/showrunner/produce.md` keeps only the Codex relay.

**Files:**
- `scripts/accounts/next_moves.py` — `NextMove`, the JSON boundary, lock, atomic write, add/remove, rendering, Mac transport.
- `scripts/accounts/accounts.py` — `next add|list|remove` and the natedev-only `next-read` / `next-write` verbs.
- `scripts/accounts/refusal_watch.py` — rules from the log, `removing_next_move`, `_recorded_auto_move_ids`, consumption under the log lock.
- `scripts/whoami/quota_alert.py` — `claude_next_move_push`, the Claude episode path, Claude recovery by passed reset, acknowledgement removed.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — `quota.claude_next_move` and its approvals.
- `scripts/production/waiting.py` — Codex-only relay, `drop_claude_quota_waits`.
- `commands/next_moves.md` — `/next_moves`; `commands/quota_ack.md` deleted; `commands/showrunner/produce.md`, `commands/showrunner/accounts.md`, `commands/message.md`, `docs/quota_alerts.md`, `docs/accounts.md` — the log, the one phone reminder, the removed pieces.
- `scripts/accounts/test_next_moves.py`, `test_accounts.py`, `test_refusal_watch.py`, `scripts/whoami/test_quota_alert.py`, `test_codex_alert_accounts.py`, `scripts/production/test_waiting.py` — add/list/remove and refusals, one-shot consumption, skipped moves keeping entries, replay after an interrupted write, legacy `auto_moves` and rule-name holds, one push per episode, covered and ungrouped-only accounts silent, Codex repeats without acknowledgement, legacy Claude waits dropped.

**Binds later work:** the log is the one store for what happens next: writes only on natedev under `next_moves_lock()`, lock order watch lock then log lock. `NextMove` accepts only `units` or `showrunners` and a numbered Claude target; the CLI group uses `CLAUDE_GROUPS`; `read_auto_move_rules` turns every entry into a Claude group rule; `claude_next_move_push` counts coverage from every entry's group. A new or reworded push needs `review.py texts --id <id> --approve` recorded in `push_texts_reviewed.json`, or the notify suite fails. Codex low alerts, their repeat and the Codex relay in `waiting.py quota` remain.

**Gotchas:** an entry whose target is the low account itself counts as cover in `claude_next_move_push`, while the engine skips it as already on target, so that account gets no reminder and the entry never runs. An unreadable or undecodable `roles.jsonl` makes `_recorded_auto_move_ids` raise `NextMoveError`, which ends the watcher run with exit 1 before the group completion pass. A Claude episode whose reminder resolves to a status note stays due and writes a `quota status <account>: …` log line every pass. `message()`, `instruction()` and `unit_swap_line()` keep Claude branches, though `message()` is reached only for Codex notes. The live `auto_moves` config key is ignored without a warning; live rules move by `next add`. `scripts.notify.test_push_catalog` does not exist.

**Ruled out:** consuming an entry only through the log write (an interrupted write replayed a recorded move once the group came back); a reminder for an account used only by ungrouped sessions (no entry could silence it).

### Phase 53 — A self-targeting entry no longer silences the reminder  · status: done

**Depends on:** Phase 52

#### As-built

- `claude_next_move_push` (`scripts/whoami/quota_alert.py`) counts an entry as cover only when its `to` differs from the low account (`entry.to != note.label`). An entry whose target is the low account itself, which the engine skips as already on target, leaves its group uncovered, so push `quota.claude_next_move` is sent once per episode.
- `_recorded_auto_move_ids` (`scripts/accounts/refusal_watch.py`) returns `frozenset[str] | UnreadableAutoMoveHistory`. A missing `roles.jsonl` is an empty set; a read error, bad encoding or a bad JSON line prints `next moves: cannot read recorded auto moves: <error>` to stdout and returns `UnreadableAutoMoveHistory()`, a frozen dataclass with no fields. `_run_auto_moves_locked` then returns before loading watch state: no entry is consumed, moved or held that pass, the rest of the watcher continues, and the run exits 0. History no longer raises `NextMoveError`.

**Files:**
- `scripts/whoami/quota_alert.py` — the self-target exclusion in the reminder's cover set.
- `scripts/accounts/refusal_watch.py` — `UnreadableAutoMoveHistory` and the skipped log pass.
- `docs/accounts.md` — unreadable history makes every log entry wait for a later pass.
- `scripts/whoami/test_quota_alert.py` — one reminder for units on claude2 holding only "units → claude2".
- `scripts/accounts/test_refusal_watch.py` — permission, malformed-JSON and bad-encoding histories leave every entry and selection untouched with one diagnostic; a recorded move whose log removal failed does not replay after the group is returned and the history turns malformed, and is consumed once the history reads again.
- `scripts/whoami/test_agent_notes.py` — the Episode fixture without `acknowledged`.

**Binds later work:** replay protection never treats unreadable history as empty: an unreadable journal skips every log entry for that pass with one diagnostic. A reminder counts an entry as cover only when its target is not the low account.

**Gotchas:** a readable prefix of `roles.jsonl` is not evidence; one bad line blocks every entry, including one whose move the prefix records. The diagnostic repeats every timer pass while the history stays unreadable, and the waiting entries still count as reminder cover.

**Ruled out:** returning no recorded ids on unreadable history (a recorded move whose log removal failed replays after the group is returned).

### Phase 54 — Workers entries run once  · status: done

**Depends on:** Phase 53

#### As-built

- Log (`scripts/accounts/next_moves.py`): `NextMove = ClaudeGroupMove | WorkersMove`. `WorkersMove(id, at_percent, to: CodexWorkersTarget | DelegateToClaude, added_at)` has no hold; `decode_next_moves` is the one text boundary (also used by `add_next_move`), turns a stored `"claude"` into `DelegateToClaude` and refuses a workers `hold_minutes` above 0. `next add workers <percent> <codexN|claude>`; `accounts.validate_next_move_target` checks a Codex target with `check_codex_target(…, repair=False)` on `next add` and `next-write`. `next list` renders `when workers reach 1% → codex2`.
- Engine (`quota_alert._workers_next_move_pass`): runs first in `_alert_for_saved_readings`, on the role-map host only, `next_moves_lock()` before `state_file()`; at most one workers move per pass. A Codex target writes `RequestedMove(cause="auto", why="auto: <id>")`, then `accounts.swap("workers", …, why=, cause=)`; a refused swap saves the `FailedMove` in `pending_move_failures`, then appends it, and keeps the entry. A `claude` target calls `switch_to_claude(…, hold=ScheduledMoveHold(reset), why=)`, skipped when the source weekly reset is unknown. A recorded move removes the entry; after a Codex swap the pass readings are the target's.
- Replay guard: `moves.recorded_auto_entries() -> RecordedAutoEntries | UnreadableMoveJournal` reads `moves.jsonl` under its flock; an id is recorded by a workers `requested` with cause `auto` and no `failed` of its move id, or by a `routed to claude` with why `auto: <id>`. Claude groups use `role_map.recorded_auto_moves(path) -> RecordedAutoMoves | UnreadableAutoMoveHistory`, re-exported by `refusal_watch`. Unreadable history skips that engine's entries with one diagnostic, and makes reminder cover empty.
- Hold: `AccountCodexQuotaFallback.hold: CreditsOffHold | ScheduledMoveHold | NoFallbackHold`, stored as `credits_off_until` or `scheduled_move_until`. While a `ScheduledMoveHold` is open, `switch_back`, `_codex_zero_alert` and the workers pass do nothing. `RoutedToClaude` has `cause: "refusal" | "credits off" | "auto"` and a required `why`; `read_journal` fills a legacy why from its cause. A failed routing append keeps the fallback and saves `pending_routing`; pending rows retry at the start of each workers pass.
- `swap` / `_swap_workers` / `_write_role` take `why` and `cause`; every workers `roles.jsonl` line carries its caller's cause (`RoleMapChange.cause` allows `credits off` for workers only).
- Reminder: `codex_next_move_push` sends push `quota.codex_next_move` once per episode above 0% and at or below 2%, when no unrecorded workers entry targets another account and no `ScheduledMoveHold` is open; the title names credits, the default Codex destination or Claude. The Codex low session alert and its repeat are gone; `run_out_push` has no production caller.
- Appends: `role_map.append_journal_line` writes each `roles.jsonl` and `moves.jsonl` row as one whole-line write on an `O_APPEND` descriptor. Readers skip an undecodable final line with no newline and print one line naming the file; an undecodable middle line stays unreadable history. `append_move` keeps one `failed` per move id, tool and subject.

**Files:**
- `scripts/accounts/next_moves.py` — the two entry variants and the text boundary.
- `scripts/accounts/accounts.py` — workers `next add`, target checks, `swap` why and cause.
- `scripts/accounts/moves.py` — `recorded_auto_entries`, `RoutedToClaude.why`, whole-line appends, failed dedup.
- `scripts/accounts/role_map.py` — `append_journal_line`, `recorded_auto_moves`, the `credits off` workers cause.
- `scripts/accounts/refusal_watch.py` — Claude rules from `ClaudeGroupMove` only; `entry_id`, `UnreadableNextMoveLog`.
- `scripts/whoami/quota_alert.py` — workers pass, `ScheduledMoveHold`, pending journal retries, Codex reminder.
- `scripts/notify/push_catalog.py`, `push_texts_reviewed.json` — `quota.codex_next_move` with three examples, approved with `--user-spec`.
- `commands/next_moves.md`, `docs/quota_alerts.md`, `docs/accounts.md` — workers entries, hold, reminder, torn-line rule.
- `scripts/accounts/test_next_moves.py`, `test_accounts.py`, `test_refusal_watch.py`, `test_moves.py`, `test_role_map.py`, `scripts/whoami/test_quota_alert.py`, `test_codex_alert_accounts.py`, `scripts/agents/test_codex_mesh.py` — the gate's cases.

**Binds later work:** a workers log entry writes a Codex `requested` with cause `auto` and why `auto: <entry id>`, or, for Claude, only a `routed to claude` with cause `auto`, held until the source weekly reset. Workers `roles.jsonl` lines carry their caller's cause from this phase on; earlier ones all say `by hand`. The Codex low session alert is gone; zero, forecast, refusal and restored session messages remain.

**Gotchas:**
- A zero reading marks the open Codex episode `reached_zero`; it closes only at its weekly reset, silently, so a delivered reminder is never re-armed.
- `append_journal_line` truncates an undecodable unterminated tail, or terminates a valid one, before it appends.
- While `pending_routing` or `pending_move_failures` cannot be written, workers history reads `UnreadableMoveJournal` and no workers entry runs.
- Install on natedev and the Mac before the first `next add workers`: an older `decode_next_moves` refuses the whole log.

**Ruled out:** a hold in minutes on a workers entry; a Codex low session alert beside the reminder; a torn last line blocking every entry.

### Phase 55 — Codex notices reach only the session that acts  · status: done

**Depends on:** Phase 54

#### As-built

- Rerun handoff: a Codex launch whose command exits after a quota refusal reports `agent_notes.py blocked … --stopped <session id> <worker> <command>` (the command is `shlex.join(sys.argv)`). The parser converts it into `quota_alert.StoppedWorkerCommand(session_id, worker, command) | StoppedCommandUnknown`; `blocked` relays one message to `session:<id>` ("Codex refused work for quota. Worker <worker> stopped: <command>\nRerun this command on Claude."), logs `quota rerun <worker> -> <session id>: sent | not delivered: <why>`, then routes the refusal through `_route_refusal` as before. It is sent on every exited refusal, a fallback already open included; a waiting follow never sends one, and an unknown director gets none.
- `scripts/agents/codex_mesh.py`: `_worker_director(folder) -> KnownWorkerDirector | UnknownWorkerDirector` takes the one holder from `_marker_sessions`, else `_launcher_sessions`, at stream start. `FailedWithThread` and `CapacityRetriesExhausted` carry `pending_refusal: PendingQuotaRefusal | NoPendingQuotaRefusal` and `director`; a refusal during a streamed turn, a refused move (`WorkerAwaitingQuotaRecovery.refusal`) or a refused replacement thread (`WorkerQuotaRecoveryStopped(ServingRequestFailure)` with its `port` and `thread_id`) sets the pending refusal, so `command_start` hands exactly one rerun to the director. `_report_quota_refusal(…, stopped_in=, stopped_thread_id=, director=)` marks the stopped thread `status: failed`, `continuation_owed: False` under `RosterOnThread`.
- Other Codex notices are log lines: zero (`quota zero <label>: …`), credits forecast (`quota credits forecast <label>: …`), Codex→Codex and unknown-account refusals, and the Codex 5-hour notice (`five_hour.watch`: `5-hour limit: …`, once per low spell by its marker; `cancel_outgoing_notices` still retires older queued relays). Phone pushes and `moves.jsonl` rows are unchanged. "Quota restored" reaches no session: the routing rows `routed back` / `routing not restored` are its record, and `/quota_refresh` tells nobody. The Claude 5-hour notice still relays.
- Removed: `message`, `instruction`, `restored_message`, `tell_others`, `not_live`, `run_out_push`, `account_line`, `unit_swap_line`, `user_time`, `usable`, `CodexZeroEpisode.sessions`; `waiting.py quota` with `HoldQuotaAlert`, `ClearQuotaAlerts`, `RestoreQuotaAlerts`, the line patterns, `read_quota_state`, `drop_claude_quota_waits` and `quota_seen.json`; `showrunner_footer.QUOTA_LEAD` and the Waiting-on `quota:` bullet; produce.md `<QuotaAlert/>`, the dailies "held quota alerts" topic and the watcher quota bullet. Kept for the Claude 5-hour and Claude no-room notices: `relay`, `recipients` (config `always` plus running showrunners), `showrunners_now`, `Job`, `deliver_and_record`. `repeat_minutes` is unused.

**Files:**
- `scripts/whoami/quota_alert.py` — `blocked` rerun relay, `_route_refusal`, log-line notices, deletions.
- `scripts/whoami/agent_notes.py` — `blocked --stopped`.
- `scripts/agents/codex_mesh.py` — exit handoff, pending-refusal and director types, `WorkerQuotaRecoveryStopped`.
- `scripts/whoami/five_hour.py` — Codex 5-hour notice as a log line.
- `scripts/production/waiting.py`, `scripts/hooks/showrunner_footer.py` — hold machinery removed.
- `commands/quota_refresh.md`, `commands/watcher.md`, `commands/showrunner/produce.md`, `commands/showrunner/dailies.md`, `docs/quota_alerts.md`, `docs/accounts.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/agent-registry.md`, `docs/as-built/build-followups-fn-length-hook.md` — who is told what.
- `scripts/whoami/test_quota_alert.py`, `test_codex_alert_accounts.py`, `test_agent_notes.py`, `test_five_hour.py`, `scripts/agents/test_codex_mesh.py`, `scripts/production/test_waiting.py`, `scripts/hooks/test_stop_showrunner_footer.py` — the gate's cases, including the replacement-thread recovery-timeout regression.

**Binds later work:** an exited refused command clears its continuation and hands its rerun to its director on Claude; no `moves.jsonl` row records the handoff, and its delivery shows only as the launching command's stderr line `codex_mesh: <worker>: quota: quota rerun …`. "Readout of the moves" reads this.

**Gotchas:**
- The director is resolved at stream start, while the launcher evidence is intact; resolving it at exit lost it after a failed roster write.
- A recovery timeout replaces the failure text with `no turn/completed…`; the pending refusal is kept as its own state so the handoff survives it.
- `pending_refusal` is not reset when its continuation completes, so a resident launcher that recovered from a refusal and later fails for another reason (disconnect, timeout) still hands a rerun of finished work to its director.

**Ruled out:** relaying zero, forecast, refusal, restore or Codex 5-hour notices to showrunners.

### Phase 56 — A finished continuation sends no rerun  · status: done

**Depends on:** Phase 55

#### As-built

- `scripts/agents/codex_mesh.py`: `PendingQuotaRefusal(serving, detail, *, continuation: AwaitingQuotaContinuation | SubmittedQuotaContinuation)`, where `SubmittedQuotaContinuation(thread_id, turn_id)` names the accepted continuation turn. `_stream_turn` carries the refusal only while that continuation is owed and drops to `NoPendingQuotaRefusal` when that turn completes: its `turn/completed` arrives (same thread and turn, not owed in the roster), or `_quota_continuation_finished` reads the thread back `idle` with that turn `completed` (at each idle wake, after the stream loop, and in the exception path). Neither path clears it while a capacity resume is owed. A recovered command that later fails for another reason (disconnect, stream timeout, exhausted capacity retries) reports only its ordinary failure: no `blocked --stopped`, no roster rewrite by the refusal path.
- Submission roles: `MovedWorker.submission: QuotaContinuationTurn | UnrelatedWorkerTurn` says whether a submission resumes the refused work, and `MovedWorker.pending_refusal` keeps a refusal raised during `_start_workers_turn`'s own submission retries. `_pending_after_worker_start` adopts a new refusal and retargets the tracked continuation only for a `QuotaContinuationTurn`; `_quota_retry_submission` gives account-move, context-overflow and capacity retries that role only when they retry the tracked turn; `_move_idle_worker` marks a roster-owed continuation as one.
- Typed stopped context: `_report_quota_refusal(name, serving, detail="", *, stopped: RefusalCommandContext = REFUSAL_WITHOUT_COMMAND_EXIT)`, with `RefusalCommandContext = RefusalWithoutCommandExit | CommandStoppedBeforeThread(session_dir, director) | CommandStoppedOnThread(session_dir, thread_id, director)` and director `KnownWorkerDirector | UnknownWorkerDirector | ResolveWorkerDirector` (default `RESOLVE_WORKER_DIRECTOR`). `_serving_request(…, stopped=)` forwards it; `_attach_and_run` passes `CommandStoppedBeforeThread`, `command_start` passes `CommandStoppedOnThread`. Only `CommandStoppedOnThread` writes the stopped thread `status: failed`, `continuation_owed: False`.

**Files:**
- `scripts/agents/codex_mesh.py` — refusal lifetime, continuation tracking, submission roles, typed stopped context.
- `scripts/agents/test_codex_mesh.py` — fake-server regressions: a finished continuation then a disconnect or a stream timeout sends no rerun; an idle completed continuation whose notice was lost, then a timeout, sends none; a submission retry still live at timeout sends one; a failed continuation then a completed held message sends one; a refusal still owed at exit sends one.

**Binds later work:** the rerun handoff and its roster write happen only while the stopped thread still owes its continuation; before this code reached a machine, a recovered command that later failed handed a rerun of finished work. "Readout of the moves" reads this.

**Gotchas:**
- The continuation is not "the last turn started": only a `QuotaContinuationTurn` submission retargets the refusal, so an unrelated held turn completing never clears a failed continuation's refusal.
- A completion notice can be lost; the idle read-back requires thread status `idle` and that exact turn `completed`.
- `_stream_turn` recasts five locals (`pending_refusal`, `thread_id`, `turn_id`, `followed_turn`, `port`) each loop iteration to bound basedpyright's flow graph.
- A disconnect diagnostic differs between Linux and macOS; tests check failed status and a diagnostic, never its wording.

**Ruled out:** clearing on completion notices alone (a lost notice reran finished work); tracking whichever turn started last (an unrelated turn cleared a failed continuation).

### Phase 57 — A reset account with a stale login says why it was not read  · status: done

**Depends on:** Phase 56

#### As-built

- After an account's weekly reset passes with no new reading, `accounts.py status`'s weekly cell reads `not read since HH:MM` (user zone) from the last stored reading (`read_at`) instead of `unavailable`. When the latest read attempt was skipped because the login token is stale (only a Claude session refreshes it), the cell adds `: no session to refresh its login`, e.g. `not read since 05:29: no session to refresh its login`. With no stored reading the cell stays `unavailable`; a current window is unchanged. Display only: no new read, token refresh or Claude call, and a passed reset never implies 100% remaining.
- `scripts/whoami/agent_accounts.py`: `StaleTokenSkip`, `NoWeeklyReadSkip`; `Report.weekly_read_skip: StaleTokenSkip | NoWeeklyReadSkip` (default `NoWeeklyReadSkip`), set to `StaleTokenSkip` on `claude_live`'s expired-token branch beside its `quota_problem`.
- `scripts/whoami/usage_store.py`: `StoredUsage.weekly_read_skip: Literal["stale_token"]` with `weekly_read_skip_directory: str` (the skip's `ClaudeDirectorySource.directory`), decoded by `weekly_read_skip(usage) -> StaleTokenSkip | NoWeeklyReadSkip`. `merge` sets both on a stale skip; any later attempt for that login, or any later Claude attempt from that directory (a failed one with no login, or one for another login), clears them. Last good numbers and their timestamps are kept. `agent_notes.py`'s existing report save carries the reason unchanged.
- `scripts/accounts/accounts.py`: `WeeklyNotReadSince(read_at, reason)`, a `StatusRow.weekly_left_percent` variant set in `_status_row` when the stored window's reset is at or before now and rendered in `_status_row_cells`; `render_status_json` writes `weekly_left_percent: null` for it, as before.

**Files:**
- `scripts/accounts/accounts.py` — `WeeklyNotReadSince`, status row and cell.
- `scripts/whoami/agent_accounts.py` — skip reason types, `Report.weekly_read_skip`.
- `scripts/whoami/usage_store.py` — stored skip and directory, decoder, clearing in `merge`.
- `scripts/accounts/test_accounts.py` — display cases; a real `claude_live` stale skip followed by an authentication failure clears the reason and keeps the last good reading.
- `scripts/whoami/test_agent_notes.py`, `scripts/whoami/test_usage_store.py` — the update pass stores the typed skip; persistence, decode and clearing.

**Gotchas:**
- An authentication-status failure yields a `Report` with no email; the recorded directory is what lets that attempt clear the stale reason.
- A marker stored without a directory cannot be cleared by a loginless failure.
- The reason is typed; nothing matches the `quota_problem` text.

**Ruled out:** waking or refreshing the token from the pass; inferring 100% remaining from a passed reset; detecting the skip from its problem text.

### Phase 58 — Only the head of a group's next list runs  · status: done

**Depends on:** Phase 57

#### As-built

- Head only: `refusal_watch._run_auto_moves_locked` takes the first `ClaudeGroupMove` per group in log order (`heads.setdefault(rule.group, rule)`) and `quota_alert._workers_next_move_pass` takes `workers[0]`. Every pass drops holds (`auto_move_holds`) and notices whose entry is not a head, so a later entry starts no hold. A head consumed by replay (`auto: <id>` already recorded) does not promote its successor in the same pass; the successor starts on a later pass. A recorded head move removes its entry and its notice.
- Due head: the source reading is fresh and at or below the head's percent and its hold has elapsed; a fresh source above the percent clears only the hold. The target must read fresh and strictly above the head's own percent (not the highest percent in the list). A Codex-target workers head also needs the target's weekly reset known and in the future; a Claude-target workers head still needs a known source reset.
- Blocked head: `quota_alert.BlockedHeadReason = TargetReadingUnavailable(last_read: datetime | NoTargetReading) | TargetAccountUnavailable(reason) | TargetAtOrBelowThreshold(left, threshold) | TargetUnresolvedRefusal | TargetPreparationRefused(reason) | TargetIsCurrentAccount`. A head whose target is the group's (or workers') current account is reported and blocks its successors. `blocked_next_move_line(group, target, entry_id, reason)` renders `<group> → <target> due (entry <id>), <target> has <detail>` (times in the user zone; `no reading` when none was stored); `notify_blocked_next_move` sends it to `recipients(config)` through `deliver_and_record`, discarding the delivery result. `blocked_next_move_reason_key(reason)` is the dedupe key; `TargetAtOrBelowThreshold` keys on its threshold only. Keys live in `refusal_watch.json` `auto_move_notices[id]` (Claude groups, `_record_blocked_head`) and quota_alert state `workers_move_notices[id]` (`_workers_head_blocked`). A shutdown in progress, an unconfigured group or a group moved this pass waits silently. `--dry-run` sends nothing, writes no state and prints the line.
- No fallback while queued: `_run` computes `pending_groups` from the log (an unreadable log counts every Claude group as pending). A refused member of a pending group gets no `placement`, `set_group_account`, member move or `no room`; a refusal with a known reset writes a `waited` (window `five_hour`, `weekly` or `unknown`) and continues on the current account at the reset; an unknown reset writes nothing. Completing off-target members onto the selected account still runs. For workers, `workers_entry_pending()` (an unreadable log counts as pending) stops `_route_refusal` after the brief 5-hour wait (`workers next entry is pending; <name> refusal recorded without a fallback move`), stops `switch_back`'s pick, and opens a credits-off zero episode with notice `quota.codex_workers_stay` and `CodexZeroEpisode.credits_off_fallback = "waiting_for_next_entry"`, which a later pass with an empty queue reopens as the credits-off fallback. `codex_next_move_push` returns a status note while any unrecorded workers entry is queued, a self-target head included.
- `refusal_watch.removing_next_move(entry_id)` drops the entry's hold, `auto_move_notices[id]` and `workers_move_notices[id]`.

**Files:**
- `scripts/accounts/refusal_watch.py` — head selection and pruning, `pending_groups` in the refusal pass, `_record_blocked_head`, `removing_next_move`.
- `scripts/whoami/quota_alert.py` — blocked-head types, key, line and delivery; workers head pass; `workers_entry_pending` gates in `_route_refusal`, `switch_back`, `_codex_zero_alert`, `codex_next_move_push`.
- `docs/accounts.md`, `commands/next_moves.md` — head-only rule, fallback block, blocked-head notices, queued waits.
- `scripts/accounts/test_refusal_watch.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py` — the stale-target head with an untried successor and one notice per reason, a refused group with a pending head, head-then-successor holds, the source 1% → 50% → 1% cycle sending one notice, own-threshold targets, current-account heads, queued waits and continuations, credits-off release after removal, unknown Codex source reset.

**Binds later work:** position 1 of a group's next list is its head; only the head holds a clock and a notice key, and removing an entry clears its hold and both notice keys under the watch lock. While a group or workers has a queued entry, refusals and credits-off make no fallback move and no `no room`; blocked-head notices write nothing to `moves.jsonl`. "The next list is numbered per group" and "Readout of the moves" read this.

**Gotchas:**
- Clearing the notice key on a source recovery resent an unchanged notice on relapse; only the hold clears there.
- Keying `TargetAtOrBelowThreshold` on the target percent resent the notice every reading.
- An unreadable log blocks every fallback for every group and for workers.

**Ruled out:** skipping a blocked head to try the next entry (the 2026-10-17 11:49 EDT showrunners claude1 → claude2 move skipped the queued claude4 head); a target threshold taken from the highest percent in the list.

### Phase 59 — Codex workers under a queued head wait and promise no fallback  · status: done

**Depends on:** Phase 58

#### As-built

- Queued credit wording is an overlay applied at send time. `quota_alert._current_notice(label, notice, *, workers_queued=False)` copies the saved values (refreshing `file`); while a workers entry is queued it puts `_queued_credit_choices(label)` into `turn_off` ("Turn off: paid spending stops. Workers stay on <label> and wait for the account move you queued.") and `leave_on` ("Leave on: workers spend <label>'s credits until that move runs or the credits run out, then wait."), sets `credit_action` to "To stop paid credit use" and empties `clarification` for `quota.codex_on_credits` and `quota.codex_on_credits_last`, and sets the `quota.codex_credits_forecast` `destination` to "When credits run out, workers wait for the account move you queued." The opening log line, the forecast log line, the phone push and every phone retry render through it.
- Saved retries hold the empty-queue wording: `CodexZeroEpisode.notice` and `forecast_notice` store only the empty-queue values, so a retry after the queue empties sends the ordinary notice. The read-only picker (`_zero_destination` → `accounts.pick`) runs only for that saved text; no account is chosen, swapped or requested while queued. With no workers entry every notice is byte-identical to before. Push ids are unchanged; each of the three templates has a `queued` example.
- Queued Codex refusal with a known reset: in `_route_refusal`, when the refusing account owns the workers role and `workers_entry_pending()` holds, a known reset (any distance) records one workers `waited` move with window `five_hour`, `weekly` or `unknown`, returns `workers wait for <name>'s <5-hour|weekly|unknown> reset at <time>`, and a second refusal with the same reset returns `workers already wait for …` without a second record. `_finish_workers_transitions` appends `wait ended` at the reset. No account is picked or swapped. A queued refusal with no known reset returns `workers next entry is pending; <name> refusal recorded without a fallback move`. With an empty queue the brief five-hour wait (under 20 minutes) and the pick-and-swap path are unchanged.
- `quota_alert.deliver_notice(jobs, stamp) -> bool` returns True only when every recipient's `deliver_and_record` line reads exactly `quota <kind> <name> -> <recipient>: sent`; zero recipients is False. `notify_blocked_next_move`, `refusal_watch._notify_blocked_head` and `_notify_no_room` return it. `_record_blocked_head`, `_workers_head_blocked` and the no-room notice drop the stored key before sending and store the blocked-head or no-room key only when it returns True, so a failed send retries next pass.
- `claude_next_move_push` covers a group only by its head (the first `ClaudeGroupMove` per group); a head targeting the current account leaves the group uncovered and the reminder fires.

**Files:**
- `scripts/whoami/quota_alert.py` — `_queued_credit_choices`, the `_current_notice` overlay, `deliver_notice`, the queued known-reset wait in `_route_refusal`, delivered-only `workers_move_notices`, head-only reminder cover.
- `scripts/accounts/refusal_watch.py` — delivered-only `auto_move_notices` and no-room keys.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — `queued` examples and their approvals.
- `docs/quota_alerts.md` — queued credit notices name the queue; retries keep the empty-queue facts.
- `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py`, `scripts/accounts/test_refusal_watch.py` — queued notices, retries after queue removal, known-reset waits, failed-delivery retries, head-only cover.

**Binds later work:** a blocked-head notice key is stored only when `notify_blocked_next_move` returns True, so a test expecting a stored key stubs a delivered notice; reminder cover follows each group's head, so a head change changes the cover. "The next list is numbered per group" reads both.

**Gotchas:**
- The text reviewer flips on unchanged credit examples; the three ids (`quota.codex_on_credits`, `quota.codex_on_credits_last`, `quota.codex_credits_forecast`) are recorded under the user spec (showrunner ruling 2026-10-14, accepted again 2026-10-17). `review.py texts` reads the live role map, so workers cannot run it in a scratch HOME.
- A saved queued wording survived queue removal until repair round 1: persisting the overlay leaves a retry telling workers to wait for a move that no longer exists.
- One failed recipient stores no key, so every recipient receives the notice again on the next pass.

**Ruled out:** saving the queued wording in the episode notice (it outlives the queue); counting a recipient line that ends in `: sent` as delivered (an error text can end that way).

### Phase 60 — A moved session's first prompt is proven sent  · status: done

**Depends on:** Phase 59

#### As-built

- Typed outcome in `herdr.py`: `PromptSubmitOutcome = PromptSubmitted(at) | PromptNotSubmitted(reason)`, encoded as text by `encode_prompt_outcome` / `decode_prompt_outcome`; `PromptSubmissionNotRequired` is the promptless Codex start (`NoOpeningPrompt`). `SessionTranscript(config_dir, session_id)` names the transcript the proof reads; a `herdr.start_agent` call with an `OpeningPrompt` and no `SessionTranscript` yields `PromptNotSubmitted`.
- `Herdr.submit_opening_prompt(pane, opening, launched_at, timeout_ms)`: `wait_for_prompt_input` converts herdr's raw observations of the destination pane only into `PromptInputReady | PromptInputNotReady | PromptInputTimedOut` (a ready sibling tab never counts; a session not ready within the launch timeout is `PromptNotSubmitted`); the text is typed once; `submitted_prompt` waits for a non-meta user turn later than the launch that contains the text; while none appears and `prompt_in_input` sees the text in the bordered composer, Enter alone is sent, at most 3 times. Text only in scrollback gets no Enter; `isMeta` peer and task turns never count.
- `restart.py` saves each launch's outcome as the receipt `<move note>.prompt-outcome.json`, keyed by request, session, target and attempt (`_save_prompt_outcome`, read by `_prompt_outcome`). `_require_submitted_prompt` guards `_confirm_move`, `_prove_move`, `clear_running_on_target` and `_launch_failure_left_session_live`: no path confirms on liveness alone, and launch-error recovery starts no second process. An unsubmitted prompt goes through `mark_restart_failed(record, "restart prompt not submitted")`, so the `failed` move line, the restart result and its push name the session. `confirmed` means the first prompt arrived as a non-meta user turn.
- The live `timers pending` branch of `launch_entry` (a timer-only retry) reads the receipt against the pre-claim progress (`_prompt_outcome({**record, "progress": progress})`) and passes the typed outcome through `_prove_move` / `_confirm_move`; the default `LaunchPromptProofNotRead` means "read the receipt now".
- `MoveNoteTimes(paused, resumed)` puts both times in `account_move_note`.
- Launchers: the `add_unit.py` restore prints `restart-prompt-outcome: <encoded>` for its parent `restart.py`; `add_showrunner.py` launches a new showrunner with `--session-id <uuid4>`, so its transcript is known before launch, and exits 3 (`SESSION_NOT_READY`) on an unsubmitted prompt; `herdr_sessions.py` exits 1 on one. The proof holds for a restart into a tab of the shared herdr session.
- `settle.report_stop` renders the automatic failed-restart push through `restart.restart_push`, naming the sessions.

**Files:**
- `scripts/herdr/herdr.py` — readiness gate, submit proof, Enter-only retry, typed outcome and codec.
- `scripts/shutdown/restart.py` — receipt, confirmation requires the proof, failure marking, move-note times.
- `scripts/shutdown/settle.py` — automatic failed-restart push names the sessions.
- `scripts/production/add_unit.py` — the restore passes the outcome to `restart.py`.
- `scripts/production/add_showrunner.py`, `scripts/herdr/herdr_sessions.py` — the other opening-prompt launchers prove submit.
- `docs/accounts.md` — confirmed means the first restart prompt arrived as a non-meta user turn.
- `scripts/herdr/test_herdr.py`, `scripts/herdr/test_herdr_sessions.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_account_move.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_settle.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_add_showrunner.py` — regressions, including real receipt files on disk.

**Binds later work:** four review findings, none a regression this phase caused, moved to "Submit proof holds at its edges" under the hard landing rule: `_prompt_outcome` matches the attempt only while `progress.kind == "restarting"`, so outside it (`restart failed`, `manual restart`, `timers pending`) any receipt with matching request, session and target passes; `prompt_in_input` treats the region between the last two horizontal rules as the composer, so an old bordered prompt above later output passes; it compacts whitespace with `" ".join(split())`, so a token wrapped mid-word gains a space and fails the match; `restart_push` collapses names above 400 characters to a count, now on both failed-restart paths (`restart.py` and `settle.py`). Registry rows carry `pid` and `procStart` (`_session_liveness`), the identity a receipt can bind to; the timer-only retry of the same process keeps passing. "Readout of the moves" reads the `failed` reason `restart prompt not submitted`; before this code reached a machine, `confirmed` proved only a live process on the target (move 457fbdea: 14 of 36 sessions kept the text unsent). The receipt is implementation state.

**Gotchas:**
- A resumed session still loading (its `/remote-control is active` line lands 3–20 s after herdr first sees the process) keeps typed text and loses the Enter; typing waits for input readiness.
- `_claim_relaunch` writes a fresh `progress.at` even for a timer-only retry; a receipt compared against that claim is rejected, so the timer-only branch reads it against the pre-claim progress.
- The success mock of `_prompt_outcome` hides receipt bugs; regressions drive the real receipt file.
- Every `herdr.start_agent` caller with an `OpeningPrompt` must pass a `SessionTranscript`; a new caller without one fails every launch as `PromptNotSubmitted`.

**Ruled out:** retyping the prompt on retry (duplicates text); counting a ready sibling tab or scrollback text as input; confirming a restart from a live process alone.

### Phase 61 — The next list is numbered per group  · status: done

**Depends on:** Phase 59 (independent of Phase 60's files; runs while Phase 60 waits on herdr-trial)

#### As-built

- Positions: `next_moves.GroupPosition(value)` is one-based (a bool, non-int or value below 1 raises `NextMoveError`); insertion is `AppendToGroup | InsertAt(GroupPosition)` (`APPEND_TO_GROUP`); `GroupPositionOutOfRange(group, requested, size)` is a `NextMoveError` reading `next: <group> has <size> entries; position <n> is out of range`, and the edit writes nothing. The stored log stays one ordered list; positions count entries of one group in log order, and an edit in one group leaves every other group's interleaved order and every surviving id unchanged.
- CLI: `accounts.py next list` prints showrunners, units, workers sections (blank line between, empty sections omitted, `nothing set up` when empty) through `render_next_moves`; each line is `<group> <n>: at <percent>% → <target>[, hold <m> min]`, with no id. `next move GROUP N M`, `next remove GROUP N`, `next add GROUP PERCENT TARGET [--hold-minutes N] [--at N]` (positions 1 through size + 1; omission appends). Replies: `moved <group> N to M`, `removed <group> N`; an add prints the list on natedev and `added <group> next move` on the Mac. Ids are never printed or accepted by these verbs.
- One edit path: `insert_next_move(entry, insertion)`, `remove_next_move(group, position)` and `move_next_move(group, source, destination)` resolve positions against natedev's current log inside `refusal_watch.editing_next_moves()` (watch lock, then log lock), which yields `LockedNextMoveEdit(before)`. `commit(entries)` drops the `auto_move_holds`, `auto_move_notices` and quota_alert `workers_move_notices` of every discarded id and displaced head, then calls `next_moves.write_next_moves`; a cleanup failure leaves the log unchanged, and a log failure after cleanup only restarts holds. An edit that keeps a group's head keeps its hold; a demoted then re-promoted head starts a fresh hold. `next_moves` reaches the watcher through the `NextMoveEdit` / `NextMoveCommit` protocols and `import_module` (no static import). `removing_next_move` no longer exists.
- Mac transport: the Mac sends `next move GROUP N M`, `next remove GROUP N` and `next-write <one encoded entry> [--at N]` through `role_map.ssh_role_map`; natedev resolves and applies them. `role_map.RemoteCommandRefused(OSError)` carries the remote's last non-empty stderr line on a nonzero exit, and each verb re-raises it verbatim as `NextMoveError`; ssh exit 255 stays a transport `OSError`, wrapped as `next: remote <verb> failed: <reason>`.
- Blocked-head text: `quota_alert.blocked_next_move_line(group, target, _entry_id, reason)` renders `<group> 1 → <target> due, <target> has <detail>` (or `… due, <target> is already the current account`); the runners' blocked stdout line is the same text. Journal whys and runner move lines keep `auto: <id>`.

**Files:**
- `scripts/accounts/next_moves.py` — position, insertion and range types; `NextMoveEdit`/`NextMoveCommit`; positional insert, remove, move with Mac forwarding; `render_next_moves`.
- `scripts/accounts/accounts.py` — `next list|move|remove|add --at` and `next-write --at`.
- `scripts/accounts/refusal_watch.py` — `LockedNextMoveEdit`, `editing_next_moves`.
- `scripts/accounts/role_map.py` — `RemoteCommandRefused`.
- `scripts/whoami/quota_alert.py` — blocked-head line without the id.
- `commands/next_moves.md`, `docs/accounts.md` — positional forms; ids never shown or accepted.
- `scripts/accounts/test_next_moves.py`, `scripts/accounts/test_accounts.py`, `scripts/accounts/test_refusal_watch.py`, `scripts/accounts/test_role_map.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py` — listing, positional edits, host-side resolution, Mac refusals, head clearing and its failure order, id-free notice.

**Binds later work:** every next-list edit goes through `editing_next_moves()` and `LockedNextMoveEdit.commit`, which clears only discarded ids and displaced heads — an in-place change to a kept entry clears nothing unless commit is extended. A Mac edit is a positional verb resolved on natedev, with `RemoteCommandRefused` text printed verbatim; `next-write` carries an encoded entry and `decode_next_moves` ignores unknown fields. The blocked-head notice and runner line carry no entry id. "A half-point hold from pace history" and "Readout of the moves" read this.

**Gotchas:**
- Writing the log before saving the displaced head's cleanup left an elapsed hold behind when the save failed, so a quick re-promotion moved at once; cleanup precedes the write.
- A generic transport error hid natedev's out-of-range line on the Mac; only exit 255 is a transport failure.
- Install natedev and the Mac together: an older peer sends or accepts `next remove <id>`.

**Ruled out:** a client-side read-modify-write of the log on the Mac (it races natedev's runner); accepting ids from the user beside positions.

### Phase 62 — A half-point hold from pace history  · status: done

**Depends on:** Phase 61

#### As-built

- Hold mode in `next_moves.py`: `ClaudeGroupMove.hold_mode` is `FixedHold(minutes) | HalfPointHold`; the codec field `hold_mode: "fixed" | "half-point"` reads a missing field as fixed and refuses any other value; a workers entry refuses both modes. `ClaudeGroupMove.hold_minutes` remains a property that raises `NextMoveError` for a half-point entry. `next add … --hold half-point` and `--hold-minutes N` are exclusive options; a half-point add reaches natedev from the Mac inside `next-write`.
- Resolution: `account_pace(label, now) -> PaceEstimate(points_per_hour) | NoUsablePace` reads `run_out.weighted_rate` for the selected account (shared usage included); `resolve_hold(mode, label, now) -> ResolvedHold(minutes, source: FixedHold | PaceEstimate | NoUsablePace)` gives `ceil(30 / points_per_hour)` minutes (one point per hour → 30), or 40 when the rate is `None` or not positive.
- Started holds: `refusal_watch.HoldStarted.duration` is `ResolvedHold | LegacyHoldDuration`, stored as `minutes` and `source {kind: pace | no usable pace | fixed}` beside account, start, selection transition and percent. The runner resolves it once when a head's hold starts and compares elapsed time to the stored minutes; restarts and later readings never change it. A legacy hold without stored minutes resolves from its entry's current mode on the next pass and keeps its start time.
- `next list` shows a half-point entry as `…, hold half a point (≈40 min)` or `…, hold half a point (40 min default: no pace history)`: natedev's current estimate from its own selection and readings (`group_half_point_hold`). The Mac prints natedev's rendered list over ssh; `next-read` stays machine JSON.
- `next hold GROUP N half-point|MINUTES [--expect PERCENT TARGET]` (`update_next_move_hold`) changes one entry's mode in place under `editing_next_moves()`, keeping ids and order, and replies with the changed line. `--expect` is checked inside the locks before any cleanup; a mismatch prints `next: <group> <n> is now at <p>% → <t>; nothing changed` and changes neither log nor hold. `LockedNextMoveEdit.commit` also drops the hold (not the notice keys) of an entry whose mode changed under the same id, saved before the log write; an equal mode (dataclass equality) keeps the started hold. The Mac forwards `next hold` positionally, with numbers as `repr(float(value))` (an integer percent travels as `1.0`).

**Files:**
- `scripts/accounts/next_moves.py` — hold types, codec, pace resolution, list rendering, `next hold` and Mac forwarding.
- `scripts/accounts/refusal_watch.py` — stored hold duration, legacy state, commit cleanup for changed modes, hold start.
- `scripts/accounts/accounts.py` — `--hold`/`--hold-minutes`, `next hold`, `group_half_point_hold`.
- `commands/next_moves.md`, `docs/accounts.md` — the hold mode and guarded update.
- `scripts/accounts/test_next_moves.py`, `scripts/accounts/test_refusal_watch.py`, `scripts/accounts/test_accounts.py` — regressions, including Mac round trips through fake transport and host/Mac list parity at a fixed clock.

**Binds later work:** hold mode, resolved minutes, their pace source and `next hold` edits live only in the next-move log and `refusal_watch.json` `auto_move_holds`, never in `moves.jsonl` or `roles.jsonl`, and `next hold` drops a started hold with no journal line; "Readout of the moves" reports them as unknown. `render_next_move` prints percents with `:g` while `--expect` compares exactly, and the docs say every update clears the started hold; "A listed percent guards its own update" closes both.

**Gotchas:**
- Mac-forwarded numbers must travel losslessly; `:g` rounding made `--expect` refuse an unchanged entry or accept a different one.
- `next list` shows the current estimate, never a started hold's fixed minutes or time left, so the list and the runner can disagree after a hold starts.
- A very low positive pace gives a long, uncapped hold fixed at start (0.1 point/hour → 300 minutes).
- `account_pace` imports `scripts.whoami.run_out` by package name inside the function.
- `ceil(30 / points_per_hour)` equals `ceil(0.5 / (rate * 60))` algebraically; float rounding at an exact boundary can differ by a minute.
- Install natedev and the Mac together, before any half-point entry is written.

**Ruled out:** restarting a hold on an equal-mode update (a retried command after a lost Mac reply must not restart it).

### Phase 63 — Submit proof holds at its edges  · status: done

**Depends on:** Phase 60

#### As-built

- Recipient identity in `herdr.py`: `PromptRecipientProcess(pid, started_at) | PromptRecipientUnavailable(reason)` is carried as `PromptSubmitted.process` (default unavailable). `prompt_recipient_process(session_id)` reads the session's one running registry row (`pid`, `procStart`); a missing, ambiguous or start-less row is unavailable. It imports `sessions` inside the function, so an isolated copy of `herdr.py` still loads. The codec writes `process: {pid, started_at}` or `{}`; a receipt without a valid process decodes as unavailable.
- `submit_opening_prompt` captures the recipient after input readiness, before typing. A recipient that changed by the time the turn appears gives `PromptNotSubmitted("prompt recipient changed during submission")`; each Enter retry first rechecks it (`"prompt recipient changed before Enter retry"`).
- `restart._prompt_outcome` accepts a `PromptSubmitted` receipt only when its process equals the session's current registry process, under every progress kind; the attempt check while `restarting` stays, and a timer-only retry of the same process still reads its proof. Receipt saving stores the recipient the launcher captured and never samples the registry.
- `prompt_in_input` takes the composer between the last two rule lines; every nonblank row after the closing rule must be wholly one enumerated footer phrase, else the bordered region is scrollback. The composer match allows a screen newline without a space only inside a word. The enumerated rows reject real Claude screens (labelled top rule; status-line, mode and agent rows), so Enter-only retries do not fire live; "Enter retries reach a real session's composer" replaces the phrase list with layout rules.
- `restart_push(label, names, *, machine) -> tuple[RenderedPush, ...]` groups names greedily so each push keeps `stopped` ≤ 400 characters, message < 500 and title < 45, and every session is named; a name that alone exceeds the limits gets its own push, shortened to the longest fitting prefix plus a visible `…`; no names gives one `shutdown.restart_incomplete`. `restart.restart` and `settle.report_stop` send every push, and nothing raises through either.
- `shutdown.restart_failed` catalog examples name one session each (`typical` "One session (organon-unit)", `many_long_names` "One session (build-followups-two-accounts-unit-director)"), approved under the user spec.

**Files:**
- `scripts/herdr/herdr.py` — recipient identity and lookup, process-bound outcome, Enter-retry recheck, composer footer and wrap-aware match.
- `scripts/shutdown/restart.py` — receipt read bound to the recipient process; `restart_push` splitting; `restart` sends each push.
- `scripts/shutdown/settle.py` — `report_stop` sends each push.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — named `shutdown.restart_failed` examples and their approvals.
- `scripts/herdr/test_herdr.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/notify/test_push_texts.py` — regressions through real receipt files, fake registry identities and fake panes.

**Binds later work:** a receipt proves submission only for the registry process it names. A receipt saved before this code reached a machine names no process, so a reconcile or timer-only retry across that install writes `failed` with `restart prompt not submitted` even when the prompt was sent; "Readout of the moves" reports that submission as unknown. "Enter retries reach a real session's composer" keeps the recipient recheck and the wrap-aware match, and the scrollback fixtures in `test_herdr.py` stay false.

**Gotchas:**
- Every launch fixture that reaches `_prompt_outcome` needs a registry row whose `pid` and `procStart` match the outcome's process.
- A test loads `herdr.py` as an isolated copy; any import of a sibling module stays inside the function that uses it.
- Every composer fixture is synthetic; none comes from a captured pane.

**Ruled out:** sampling the recipient after the launcher returns (a replacement process inherits the proof); a module-level `import sessions` in `herdr.py`; raising on a name too long for one push (it suppresses every push of the batch).

### Phase 64 — Enter retries reach a real session's composer  · status: done

**Depends on:** Phase 63

#### As-built

- `Herdr.prompt_in_input(pane, text)` (signature unchanged) finds the composer by layout. The closing rule is the last bare rule line (`\s*[─━-]{3,}\s*`). The top rule is the last rule line above it whose next row starts with `❯` or `>` after any indentation; it may carry one inline label of any text, rule characters included (`\s*[─━-]{3,}(?:\s+.+\s+[─━-]+)?\s*`, as in `──── two-accounts ─`). A rule-like row inside a multiline prompt is not followed by the marker, so it never stands in for the top rule. No closing rule or no top rule gives false.
- Footer by indentation: every nonblank row after the closing rule must start with whitespace (status-line, mode and background-agent rows); any column-0 row there makes the bordered region scrollback, whatever its text. The enumerated footer phrases are gone.
- The marker is stripped with any whitespace after it, the no-break space (`\xa0`) included. The wrap-aware match and the Enter-retry recipient recheck are unchanged.
- Enter-only retries fire on real Claude screens. A live pane read read-only, with the prompt put in its captured composer row, parses true with this code, false with the Phase 63 code, and false with the composer empty.

**Files:**
- `scripts/herdr/herdr.py` — composer recognition by layout in `prompt_in_input`.
- `scripts/herdr/test_herdr.py` — real-screen fixtures (named and unnamed top rule, status, mode and agent footer rows, a `/rc` status row, a `❯\xa0` marker, a multiline prompt with rule-like rows, a label with rule characters, an old bordered prompt above an empty composer); the fake Enter writes the typed prompt, so multiline proof drives the real transcript check.

**Binds later work:** a quoted `>` row after a rule-like row inside a multiline prompt (rendered `  ---` then `  > quoted line`) still selects that inner row as the top rule, so `prompt_in_input` returns false and no Enter retry fires; the launch ends `PromptNotSubmitted`, never a double send. Restart notes and production launch prompts are single paragraphs and cannot produce it; a free-text `herdr_sessions.py --prompt-file` can. "A quoted row inside a prompt never becomes the composer's top" closes it.

**Gotchas:** footer recognition depends on Claude drawing every footer row indented and every assistant or tool turn at column 0.

**Ruled out:** enumerated footer phrases (every live screen failed them); taking the top rule as the rule just before the closing rule (a rule-like prompt row displaces it).

### Phase 66 — A listed percent guards its own update  · status: done

**Depends on:** Phase 62

#### As-built

- `format_next_move_percent(percent) -> str` in `next_moves.py` prints the shortest float repr that reads back to the stored value, with a trailing `.0` removed (1.234567 → `1.234567`, 1.0 → `1`). `render_next_move` uses it for `next list` rows and the `next hold` reply, and the `--expect` refusal text uses it too, so a listed percent copied into `next hold … --expect` matches the stored entry. Both machines print natedev's text.
- `--expect` still compares floats exactly; Mac forwarding (`repr(float(value))`) and hold cleanup are unchanged.
- `docs/accounts.md` and `commands/next_moves.md` say an update to a different hold mode clears the started hold and an update to the same mode keeps it, so a command retried after a lost Mac reply never restarts a hold; guarded updates copy the listed percent and target.

**Files:**
- `scripts/accounts/next_moves.py` — `format_next_move_percent` and its call sites.
- `docs/accounts.md`, `commands/next_moves.md` — guarded update and same-mode wording.
- `scripts/accounts/test_next_moves.py` — rendered percents for units, showrunners and workers (fractions, integer boundaries, adjacent floats, a subnormal) read back exactly.
- `scripts/accounts/test_accounts.py` — the listed token passed to `next hold --expect` locally and through the fake Mac transport to the real host CLI.

**Binds later work:** every percent display uses `format_next_move_percent`, including the started-hold row of "A started hold shows its own minutes". A same-mode `next hold` keeps a started hold; that row is where it first becomes visible in `next list`.

**Gotchas:** a percent printed any other way (`:g`, rounding) makes the exact `--expect` compare refuse an unchanged entry.

**Ruled out:** restarting a hold on a same-mode update.

### Phase 65 — A quoted row inside a prompt never becomes the composer's top  · status: done

**Depends on:** Phase 64, Phase 62 (sequenced after it; no file overlap)

#### As-built

- `Herdr.prompt_in_input(pane, text)` (signature unchanged) accepts a top rule only when its next row starts with `❯` or `>` at column 0, and strips the marker with the same anchor; an indented `>` row is input, never the marker. A multiline prompt rendered `  ---` then `  > quoted line` gets its Enter-only retry and then `PromptSubmitted`. Closing rule, label, footer by indentation, whitespace after the marker, wrap-aware match and recipient recheck are unchanged; a captured live Claude screen still parses.
- The `shutdown.restart_failed` example `many_long_names` is now `one_long_name` (it shows one session); the rendered text is byte-identical. Its approvals under `typical` and `one_long_name` are re-recorded with `review.py texts --id shutdown.restart_failed --approve --user-spec …` (showrunner ruling 2026-10-17), and the text reviewer passes both with no failed rules. `shutdown.stop_failed` keeps `many_long_names`.

**Files:**
- `scripts/herdr/herdr.py` — column-0 marker anchoring in `prompt_in_input`.
- `scripts/herdr/test_herdr.py` — quoted-row retry regression; scrollback guard (an old quoted prompt above an empty live composer gets no Enter); the fixture comment names a synthetic screen modelled on live Claude panes.
- `scripts/notify/push_catalog.py` — example key rename.
- `scripts/notify/push_texts_reviewed.json` — approvals for `typical` and `one_long_name`.
- `scripts/notify/test_push_texts.py` — pins both entries' example keys.

**Gotchas:**
- Live Claude panes put the composer marker at column 0 and indent input continuation rows; a screen that indents the marker no longer reads as a composer.
- `push_texts.fingerprint(entry)` hashes every example name, so renaming any catalog example key invalidates every approval of that entry; re-record with `review.py texts --id <id> --approve --user-spec "<source>"`, never by hand.

### Phase 67 — A started hold shows its own minutes  · status: done

**Depends on:** Phase 66

#### As-built

- Each row receives `next_moves.StartedHold(at, duration: ResolvedHold) | HoldNotStarted`; `render_next_move(entry, position, *, started_hold=HOLD_NOT_STARTED)` and `render_next_moves` read the clock once per render (`render_time()`).
- Started rows: `units 1: at 1% → claude4, hold half a point (34 min at 0.9 points/hour, 12 min left)`, `…, hold half a point (40 min default: no pace history, 12 min left)`, `…, hold 30 min (12 min left)`; minutes left round up from the stored start plus stored minutes, then read `hold over`. Unstarted rows keep the estimate (`hold half a point (≈30 min)`, `… (40 min default: no pace history)`, `hold 30 min`).
- `refusal_watch.started_next_move_holds(entries) -> dict[str, StartedHold] | WatchStateUnreadable` (reached through the `StartedHoldReader` protocol) makes one unlocked `_load_state()` read, never `_watch_lock`, and keeps a head's hold only when its account, `move_id` and percent match the group's current account, transition and the entry; a non-head entry, a `LegacyHoldDuration` hold and a fixed 0-minute head print the unstarted form.
- An empty list returns `{}` without reading the watch state. `OSError`, `ValueError` or `OverflowError` from the read gives `WatchStateUnreadable`: unstarted rows, then one line `started holds not shown: watch state unreadable`. The runner's handling of corrupt state is unchanged.
- `next list` (natedev's text on the Mac), the list after `next add` (`next_moves.list_next_moves()`) and the `next hold` reply print the same row, so a same-mode update replies with the started row and a mode change returns the unstarted form.

**Files:**
- `scripts/accounts/next_moves.py` — hold row types, both row forms, the unreadable notice, the `next hold` reply.
- `scripts/accounts/refusal_watch.py` — `started_next_move_holds`.
- `scripts/accounts/accounts.py` — `next add` prints `list_next_moves()`.
- `docs/accounts.md`, `commands/next_moves.md` — both row forms.
- `scripts/accounts/test_next_moves.py`, `scripts/accounts/test_refusal_watch.py`, `scripts/accounts/test_accounts.py` — row forms, minutes kept across a pace change and runner restart, the same-mode reply without the watch lock, stale and non-head entries, corrupt state on both machines.

**Binds later work:** the three outputs show only the current head's started hold, so no dropped or consumed hold is recoverable; a retained started or `hold over` row printed after this code reached natedev is evidence for a hold's minutes and source, which "Readout of the moves" reads. "One stale-hold check for the list and the runner" replaces the accessor's copy of the runner's staleness test.

**Gotchas:**
- `started_next_move_holds` repeats `_run_auto_moves_locked`'s staleness test inline; the two must stay in step.
- `accounts.group_accounts()` and `accounts.group_transition()` run outside the accessor's `try`; an unreadable `groups.json` gets no notice (the runner shares that exposure).
- The `next hold` reply renders inside `editing_next_moves()`, which holds `_watch_lock`; taking it again in that process blocks forever.

**Ruled out:** a locked watch-state read for the list; a bare `Optional` for a row's hold.

### Phase 68 — One stale-hold check for the list and the runner  · status: done

**Depends on:** Phase 67

#### As-built

- `refusal_watch.py` holds one staleness predicate: a frozen `GroupSelection(account: str | accounts.GroupNotConfigured, move_id: str | accounts.NoGroupTransition)` built by `_group_selection(selected: accounts.GroupAccounts, group: accounts.ClaudeGroup) -> GroupSelection`, the one place a `GroupTransition` becomes a move id, and `HoldStarted.is_current(selection: GroupSelection, percent: float) -> bool`, true only when the hold's account, `move_id` and percent all equal the selection's and the given percent. An unconfigured group never matches a stored account; two `NoGroupTransition` values compare equal.
- `started_next_move_holds` and `_run_auto_moves_locked` both decide staleness through `_group_selection` and `HoldStarted.is_current`; neither compares a hold's account, `move_id` or percent inline. The runner reads `selection.account` for the `GroupNotConfigured` skip, `account_identity` and the blocked-head check, and `selection.move_id` for a new `HoldStarted`.
- Behavior is unchanged: the same verdict for every hold, the same three list outputs and the same saved `auto_move_holds`. The accessor still makes one unlocked `_load_state()` read and calls `group_transition` only for a head with a resolved hold, outside its `try`. Skips that are not staleness (non-head entry, missing hold, `LegacyHoldDuration`, fixed 0-minute head; the runner's consumed entries and readings above the percent) stay in their callers.

**Files:**
- `scripts/accounts/refusal_watch.py` — `GroupSelection`, `_group_selection`, `HoldStarted.is_current`, and both callers.
- `scripts/accounts/test_refusal_watch.py` — an eight-case verdict table (current; account differs; group unconfigured; `move_id` differs; stored `NoGroupTransition` against a recorded move and the reverse; both `NoGroupTransition`; percent differs) run through the accessor and a runner pass over the same state; a forced-predicate test that patches `HoldStarted.is_current` and asserts both callers follow it.

**Gotchas:** a new staleness condition goes into `HoldStarted.is_current` only; the forced-predicate test fails when either caller compares a hold's account, move id or percent inline.

### Phase 69 — A lost resume prompt is typed again  · status: done

#### As-built

- One private composer parse of the visible screen (the last closing rule, a top rule followed by a column-0 `❯`/`>` row, no column-0 row below) serves both `prompt_in_input` and `prompt_input_empty`. `prompt_input_state` is ready only when herdr reads `idle` or `working` with `interactive_ready` (or the `/remote-control is active` row shows) **and** the screen shows an empty composer.
- `Herdr.submit_opening_prompt(pane, opening, launched_at, timeout_ms)` types only into a visible empty composer. A prompt in neither a user turn nor the box is typed again only when herdr reads `idle` (a `working` session may hold it queued), the box is empty, `PROMPT_SETTLE_SECONDS` (10 s) have passed since the last typing or Enter with no matching user turn, the transcript read cleanly just before typing, and the recipient process is unchanged; it waits for readiness again first. At most 3 typings, none past the deadline. Enter retries for text still in the box are unchanged; a box holding other text fails without typing. An unreadable transcript blocks typing and Enter; a missing composer after typing keeps the submit waiting to its deadline.
- Not-submitted reasons include `no user turn after three typings`, `destination never became ready for re-type`, `prompt recipient changed before re-type` and `opening prompt deadline elapsed: transcript unreadable`; after any typing each ends `; prompt typed N times`. `opening prompt is not in the destination input` no longer exists.
- `TranscriptCursor`, owned by one submit call, keeps a byte offset per `projects/*/<session_id>.jsonl` and reads only appended bytes; a trailing partial line waits for its newline and a shrunk file restarts at 0. `readability: TranscriptReadable | TranscriptUnreadable` is per read. The `launched_at` filter, the remote-control check and `submitted_prompt`'s matching are unchanged.
- `PromptNotSubmitted(reason, screen)`: `screen` is the last visible screen read (ANSI-stripped, last 40 lines) or `PromptScreenNotRead` (the default); every not-submitted outcome after the first typing carries it. `encode_prompt_outcome` writes `screen` only when read; `decode_prompt_outcome` reads it back and old records decode unchanged. It persists in the `<move note>.prompt-outcome.json` receipt and the add_unit/add_showrunner outcome records; no user surface shows it.

**Files:**
- `scripts/herdr/herdr.py` — composer parse, readiness gate, re-type loop, `TranscriptCursor`, the screen field and its encoding.
- `scripts/herdr/test_herdr.py` — lost-prompt, late-turn, queued-while-working, bounds, changed-recipient, cursor, unreadable-transcript, missing-box and codec regressions.
- `scripts/herdr/test_herdr_sessions.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_add_showrunner.py` — fake screens show an empty composer.

**Binds later work:** "Readout of the moves" counts a prompt lost while the session loaded and an unreadable transcript among the paths behind `restart prompt not submitted`; before this code reached a machine, a prompt typed during a large-transcript load could be lost (trunk-unit, natedev, 2026-10-17 22:53 EDT). The receipt now holds the typing count and last screen and still proves neither a move nor which path failed.

**Gotchas:**
- Any launcher test fake must render an empty composer row, or submit never becomes ready.
- A glob error or any transcript open, read, stat or seek error makes the whole read unreadable; no matching file is a readable, empty read.

### Phase 70 — A slow restart is never marked failed while its session runs  · status: done

#### As-built

- `herdr._prompt_matches(text, recorded_text)` is the one match for the submit wait, the re-type guard and the final read. A non-meta `user` row matches when it contains the typed text, or, for a prompt that starts with `/`, when it holds `<command-name>/<name></command-name>` and, for non-empty arguments, `<command-args><arguments></command-args>` (arguments stripped). Claude Code records a slash command only in that tag form, so an accepted slash prompt now reads submitted and is never typed again.
- `submit_opening_prompt`'s `failure()` reads the transcript one last time after any typing. A match returns `PromptSubmitted` with that turn's time, unless the recipient process changed, which fails with `prompt recipient changed during submission`.
- `relaunch_session` and `launch_entry` (restart.py) check `_owner_liveness` for every `restart failed` record, from a move or a snapshot: live prints `<name>: already running; skipped` and leaves the record as it is; unknown prints `<name>: recovery refused: <reason>`; not live relaunches. `restart_failed_liveness(saved)` and `restart_failed_session_running(saved)` read liveness without changing the record.
- A live `restart failed` session reads `restart prompt not confirmed; session is running — check it`, with no command, in `shutdown status` (in place of `settle.status_record_lines`, and left out of the `bring back` action), `follow` and `settle.report_stop`. Liveness is read when the line is drawn, so a session that exits gets its restart command back; unknown liveness shows the command with `liveness unknown: <reason>`. For a peer-machine record, status reads liveness from that machine's inventory; absent there, liveness is unknown.
- A live `restart failed` record sends no not-back push, `_machine_result` reads `done` when it is the only shortfall, and a peer line `already running; skipped` forces no peer retry. `tell_showrunners_outcome` tells a unit's showrunner `<name> is back on <target>; its restart prompt was not confirmed — check it`, once, counted as told back, with no command.
- The record stays in `sessions/` as `restart failed` and is never moved to `history/`; liveness is never journalled.

**Files:**
- `scripts/herdr/herdr.py` — `_prompt_matches` and the final read in `failure()`.
- `scripts/shutdown/restart.py` — liveness checks in `relaunch_session` and `launch_entry`, `restart_failed_liveness`, `restart_failed_session_running`, `_machine_result`, the not-back filter and the peer-retry pattern.
- `scripts/shutdown/shutdown.py` — `_restart_failed_liveness` (local or peer inventory), the status record and action lines, `_follow_action`.
- `scripts/shutdown/settle.py` — `report_stop` lines and `tell_showrunners_outcome`.
- `scripts/herdr/test_herdr.py` — slash-command (including the recorded `294cac84` row), no-second-typing and final-read cases.
- `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_settle.py` — live, unknown and not-live relaunch, status, follow, push, stop report and showrunner message.

**Binds later work:** "Readout of the moves": every slash-command resume was affected — each showrunner restart (`/showrunner:produce <doc> resume`) and a fresh unit with a given plan (`/unit:direct <plan>`); unit restores use a non-slash prompt and were not. From Phase 60 reaching a machine until this code did, such a restart whose prompt Claude accepted could still record `failed` with `restart prompt not submitted`, and from Phase 69 its prompt could be typed again, at most 3 typings; submission is proven only by a later non-meta user row in the tag form. The journal's `failed` line and the `restart failed` record are unchanged; the stop report's `is back` and the showrunner's not-confirmed message are not a `confirmed`.

**Gotchas:** a `restart failed` record can belong to a live session; any new surface that prints a restart command for one reads `restart_failed_liveness` first, or following it starts a second copy.

**Ruled out:** a type alias for the three-way liveness union (polish only).

### Phase 71 — A running session's unconfirmed restart can be closed  · status: done

#### As-built

- `settle.running_restart_line(saved, *, for_showrunner=False)` is the one source of the live-session line: `<name>: restart prompt not confirmed; session is running — check it, then close its record: <command>`, where `<command>` is `settle.shutdown_command("forget", <session id>, "--account", <login>)`. The showrunner form is `<name> is back on <target>; its restart prompt was not confirmed — check it, then close its record: <command>`. `shutdown._shutdown_record_lines` (in place of `settle.status_record_lines`), `shutdown._follow_action`, `settle.report_stop` and `settle.tell_showrunners_outcome` print it.
- `accounts._prepare_move_shutdown(saved, *, dry_run) -> MoveShutdownReady | MoveRefused`: when `restart.restart_failed_session_running(saved)` is true, the record is archived by `record.forget_failed_restart(login, session_id, machine, expected_request_id)` then `acknowledge_snapshot`, printing `<name>: closed its unconfirmed restart record; the session is running`, and the session moves as any other. `--dry-run` prints `would close …` and archives nothing. Unknown liveness, a session that is not live, and every other record kind refuse with `already has a shutdown in progress`. `move_sessions` runs it after the destination, worker and already-on-target checks; `move` and `move_group` dry-runs preview it.
- `record.forget_failed_restart` archives to `history/` under the account lock only when the record's request id, `restart failed` progress and machine still match; otherwise it returns `SessionRetained`/`NoShutdown` and the move refuses. `forget_session` and `shutdown forget` are unchanged.
- `refusal_watch._run` leaves live `restart failed` records out of `open_ids`, so refusal and auto moves reach `move_sessions`.
- The journal's `failed` line stays; the record is never confirmed or moved to `restarted`.

**Files:**
- `scripts/shutdown/settle.py` — `running_restart_line`, used by `report_stop` and `tell_showrunners_outcome`.
- `scripts/shutdown/shutdown.py` — status and follow print the shared line.
- `scripts/accounts/accounts.py` — `_prepare_move_shutdown`, `MoveShutdownReady`, the move, group and session paths.
- `scripts/shutdown/shutdown_record.py` — `forget_failed_restart`.
- `scripts/accounts/refusal_watch.py` — the `open_ids` filter.
- `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_record.py`, `scripts/accounts/test_moves.py`, `scripts/accounts/test_refusal_watch.py` — the line on every surface, the move closing a live failed record and refusing the rest, the guarded archive, the watcher.

**Binds later work:** "Readout of the moves": a live `restart failed` record kept its session out of every move only until this code reached a machine (field case cargo-liner-style and cargo-liner, closed by hand with `forget` 2026-10-18 01:46 EDT); from then a move archives the record without a journal entry and moves the session on its own request. "The closing command names the machine that holds the record": `shutdown status` without `--here` draws peer records, but `forget` takes only local records, so the line's command fails for a peer record (`matches no local session`).

**Gotchas:** the archive is not journalled, so a `restart failed` record in `history/` may have been closed by `forget` or by a move.

### Phase 72 — The closing command names the machine that holds the record  · status: done

#### As-built

- `settle.running_restart_line(saved, *, for_showrunner=False)` ends `— check it, then close its record in a terminal on <saved["machine"]>: <command>` in both forms: `<name>: restart prompt not confirmed; session is running — check it, then close its record in a terminal on <machine>: <command>` and, for the showrunner, `<name> is back on <target>; its restart prompt was not confirmed — check it, then close its record in a terminal on <machine>: <command>`. `<command>` is unchanged (`shutdown_command("forget", <session id>, "--account", <login>)`).
- It stays the one source for `shutdown status`, `follow`, the stop report and the showrunner message; the liveness reads, the printing surfaces and the Phase 71 move archive are unchanged.
- `shutdown status` without `--here` reading a live peer record names the peer machine, so the printed `forget` runs where `_forget` accepts the record; a local record names the local machine.

**Files:**
- `scripts/shutdown/settle.py` — `running_restart_line` names the record's machine.
- `scripts/shutdown/test_settle.py` — stop report and showrunner message expectations name the machine.
- `scripts/shutdown/test_shutdown_cli.py` — the status/follow expectation, the printed-command archive test, and `test_status_names_the_peer_terminal_for_a_running_sessions_closing_command` (natedev reading a Mac record and the reverse; peer liveness comes from the peer inventory only).

**Binds later work:** "Readout of the moves" names this text (`, then close its record in a terminal on <machine>: <command>`); the readout reads only the journal and records, never the line.

**Gotchas:** `shutdown forget` takes only local records, so any new line that prints a record command names `saved["machine"]`.

### Phase 73 — The herdr and production suites pass on macOS  · status: done

#### As-built

- `test_add_unit.py` and `test_add_showrunner.py` build `self.root` as `Path(tempfile.TemporaryDirectory()).resolve()`. Trust keys and Worktree cells then match the paths that production resolves (on macOS, `/tmp` is a symlink to `/private/tmp`). Their fixture env pins `"HERDR_PLATFORM": "linux"`, so the Linux launcher fixtures run as Linux on every machine.
- `PromptSubmissionTests` (`scripts/herdr/test_herdr.py`) puts `"HERDR_BIN": "/nonexistent/herdr"` in its cleared env. Its class-level `client` is a typed `Mock` placeholder (`cast(herdr.Herdr, Mock())`). `setUp` builds the only real `herdr.Herdr("agents")`, after the env applies. No herdr lookup runs at import.
- `docs/as-built/herdr-adoption.md` "Test doubles" holds the four fixture rules and the Mac run command for the nine suites: a scratch HOME and `TMPDIR=/private/tmp HERDR_BIN=/nonexistent/herdr python3 -m unittest <suites>`. The command runs from the root of a copy of the tree under test, not from the Mac's live `~/.claude`.
- Test-only: no production file changes.

**Files:**
- `scripts/production/test_add_unit.py`, `scripts/production/test_add_showrunner.py` — resolved temporary root, pinned platform.
- `scripts/herdr/test_herdr.py` — `HERDR_BIN` in the cleared env; the client is built only in `setUp`.
- `docs/as-built/herdr-adoption.md` — the fixture rules and the Mac run command.

**Gotchas:**
- A class attribute that builds a herdr client runs at import, before any `setUp` env applies. Build clients only in `setUp`.
- The run-level `HERDR_BIN` in the Mac command hides an import-time herdr lookup. To check a fixture for one, run it with an empty `PATH` and no `HERDR_BIN`.
- The Mac command uses `scripts.` module names, so it runs only from the root of a tree that holds the branch under test.

**Ruled out:** `HERDR_PLATFORM` on the run command — it takes precedence over test_restart's `_PLATFORM="darwin"` patches and breaks them.

**Live check passed 2026-10-18:** the documented run command passed the nine suites on the Mac from a copy of main at ae28d4fd: 1054 tests OK in 194 s. Change-review row C51.

### Phase 74 — The pick verb leaves the accounts command line  · status: done

#### As-built

- `accounts.py` has no `pick` verb; `accounts.py pick …` exits 2 with argparse's invalid-choice error. `/showrunner:accounts` lists no `pick`. The user asked for this through the showrunner (2026-10-18): they read `/showrunner:accounts` and never run `pick`.
- `pick()`, `KeepAccount`, `PickedAccount`, `PickReason`, `no_room_line` and `_pick_explanation` stay. The automatic moves (`refusal_watch.py`), `placement --explain` and `quota_alert.py` call them.
- `check_move_target` refuses a target that is out of usage with `accounts: <target> is out of usage (<window> <n>% left, resets <time>); <label> has room`. It gets `<label>` from `pick("claude", now, exclude={target}, keep=NoKeep())`. When no account has room, it ends with `no_room_line` (`no claude account has room; the earliest reset is <time>`).
- `docs/accounts.md` "Picking an account" gives the ranking (soonest weekly reset, then most weekly room, then label) and points at `placement --explain` for each account's reason.

**Files:**
- `scripts/accounts/accounts.py` — no verb; the refusal names the account with room.
- `scripts/accounts/test_accounts.py` — the picker tests call `pick()` directly; `test_pick_is_not_a_command`.
- `scripts/shutdown/test_account_move.py` — both refusal texts, for a hand move and a group move.
- `commands/showrunner/accounts.md`, `docs/accounts.md` — no verb; the new refusal text.

**Gotchas:**
- `test_launch_accounts.py`'s shell-launch guard still rejects `accounts.py pick` in a launcher script. It guards against the verb coming back, so it stays.
- The refusal now runs the ranking. A defect in a candidate home that makes `pick()` raise changes the refusal into that error. The move is still refused.
- C4 (0% means no room in pick) stays closed under the room-until-refusal rule. This phase did not change it.

### Phase 75 — A failed read shows a star and a footnote  · status: done

#### As-built

- The Read cell of the accounts status table shows the last good read. When the latest attempt failed, it adds `*`. An old read also shows its month and day (`10-17 20:15 EDT*`). An account with no good read shows `—*`. There is no new column. The user asked for this through the showrunner (2026-10-19): claude4 showed a 10-17 time because its login token expired.
- Under the table there is one footnote per failed account: `* <label>: login token expired; start a session on it to refresh`, `usage read refused (401); start a session on it to check its login`, `usage read refused (rate limit); the next read is in 2 minutes`, `usage read failed; the next read is in 2 minutes`. The same lines go to the JSON problems list. A good read clears the star and the footnote.
- `agent_accounts.UsageReadOutcome` is a closed enum: ok, stale_token, refused_401, rate_limited_429, error. The live Claude read classifies the HTTP status where it catches the error. `usage_store` saves `read_outcome` for each login and keeps the last good numbers and times on a failure.
- `usage_store.UsageReadings` links each tool directory to its login (`directory_logins`, saved as `_directory_logins` in `usage.json`). A failed read that returns no login uses that link, then the caller's `KnownUsageLogin` (status passes the identities it read; the timer passes its named Claude selection and the default identity). With neither, it records nothing. It never takes a login from error text or credentials.

**Files:**
- `scripts/whoami/agent_accounts.py` — the outcome enum, the HTTP status classification, `KnownUsageLogin`/`UnknownUsageLogin`.
- `scripts/whoami/usage_store.py` — the saved outcome, the directory links, old-store decoding.
- `scripts/accounts/accounts.py` — the Read cell and the footnotes.
- `scripts/whoami/test_usage_store.py`, `scripts/accounts/test_accounts.py` — each cause's star and footnote, the date form, clearing on a good read, the old-store and shared-login cases.
- `docs/accounts.md` — the star and the footnote.

**Gotchas:**
- Only `usage_store` reads `usage.json`. Code from before this phase would load `_directory_logins` as an empty login entry. Both machines promote together, so this lasts only until the promotion.
- `load()` seeds the links from the old `read_source_directory` and `weekly_read_skip_directory` fields. It writes only the directory map.
- A failed identity read stays unmarked when its directory has no link and the caller knows no login, for example a first failed Codex identity read on an old store. The next good read makes the link.
- The one pool review found a blocker: a failed read with no login was dropped on a store written before this phase, and also when two directories shared a login. The directory links fixed it.

### Phase 76 — A held quota continuation recovers when its account has room  · status: done

#### As-built

- `codex_mesh._quota_continuation_ready`: a known serving account with no refusal on record is ready when `refusals.stored_reading` is a `FreshReading` and every window it holds has `left > 0`. A recorded refusal still needs `refusals.resolved()`.
- `codex_mesh._move_idle_worker`: an owed continuation that is not ready on a known account with no refusal returns `WorkerWaitingOnLiveTurn("no fresh reading shows room on the account")`. The roster keeps `continuation_owed`, and the next attempt checks again. An `UnknownCodexAccount` still returns `WorkerThreadStateUnknown`.
- The showrunner reported the defect (2026-10-19): berth-parallel's Mac seat was held for good after its refusal write failed on an ImportError during the Phase 75 promotion. After this phase is live, the seat recovers at its director's next `implement.sh --to` dispatch.

**Files:**
- `scripts/agents/codex_mesh.py` — the ready rule and the wait branch.
- `scripts/agents/test_codex_mesh.py` — recovery with and without a five-hour window; missing, stale and zero-room readings wait and recover later; an unknown account sends nothing; the cleanup test pins `ensure_server` to its stub port.

**Gotchas:**
- With no refusal on record, a fresh reading with room is enough, also when it was taken before the failure. A recorded refusal still blocks such a reading.
- The unreadable-thread cleanup test started a real app-server from its scratch HOME after a long suite run (its fake epoch made the process look older than the synthetic sign-in). Its `ensure_server` is now pinned to the stub port.
- `scripts.agents.test_codex_mesh` ran about 20 minutes, and this phase's merge gate took 1258 s, where the Phase 75 gates took about 300 s. Reported to the showrunner; not diagnosed.

### Phase 77 — A follow with no stored refusal returns at once  · status: done

#### As-built

- `codex_mesh.WorkerWaitingForRoom(reason, client, port, thread_id)` is a `WorkerMoveResult` that is not a live turn. `_move_idle_worker` returns it for a known account with no stored refusal and no fresh reading with room. The refused-account wait and the unknown-account hold do not change.
- `_wait_for_room` adopts the carried client, port and thread, then logs `<seat> waits for room on <account>: <reason>; the continuation stays owed` once. A follow or one-turn launch keeps `continuation_owed` and the held messages for the next dispatch and finishes at once. A resident stays.
- `_start_workers_turn`, `_resume_account_move`, the capacity retry, the context-overflow continuation and the account-move resume all hand the wait to `_wait_for_room`. A capacity retry that meets the wait clears `resume_owed`, so the quota continuation is the one continuation.
- The showrunner reported it (2026-10-19), and test-threads found the cause: Phase 76 returned `WorkerWaitingOnLiveTurn` there, and the follow loops polled until `CALL_TIMEOUT_SECS` (300 s).

**Files:**
- `scripts/agents/codex_mesh.py` — the new result and its handling.
- `scripts/agents/test_codex_mesh.py` — the four refusal tests store their refusal; tests for the follow, launch, resident, moved connection and single continuation.

**Gotchas:**
- The Phase 76 regression made four tests wait 300 s each and the merge gate take about 1260 s. After this phase the gate took 121 s.
- The one pool review found two blockers: the wait dropped the connection and thread a refused move had already adopted, and a capacity retry left `resume_owed` set. Both are fixed with regression tests.

### Phase 78 — A launcher sees a refused thread as ended  · status: done

#### As-built

- `codex_mesh._thread_has_no_active_turn(status)` names the app-server thread states with no active turn: `idle`, `notLoaded` and `systemError`. `_ended_unseen`, `_read_live_turn` and reset use it.
- A refused thread (status `systemError`) with an account-move marker resumes the move on the next quiet poll. A result held after a detached run ends goes through `_finish_if_ended_unseen`: the worker moves, waits for room or finishes. Reset accepts a refused or unloaded thread.
- `_move_idle_worker` returns `WorkerWaitingForRoom("the refused account has not recovered", ...)` for an owed continuation on an unchanged account with a stored refusal. `WorkerWaitingOnLiveTurn` now means only a live turn. A follow or one-turn launch logs the wait once, keeps the continuation and the held messages, and finishes. A later account move resumes the continuation through its detached resume.
- The showrunner reported it (2026-10-19): style's seat `claude-build-followups-style-impl2` stayed attached about 3 hours after codex1 reached zero.

**Files:**
- `scripts/agents/codex_mesh.py` — the predicate, its three callers and the stored-refusal room wait.
- `scripts/agents/test_codex_mesh.py` — regressions for the marked move, the held result (follow and one-turn launch, with and without a stored refusal) and reset; six refusal tests now expect the room wait, not the timeout handoff.

**Gotchas:**
- After a refused turn the app-server reports the thread as `systemError`, not `idle`. A status check that accepts only `idle` waits on a refused thread without limit.
- The one pool review found one blocker (the stored-refusal path still returned `WorkerWaitingOnLiveTurn`). It is fixed with regression tests. The first new tests fail on the old code.

### Phase 79 — One usage read at a time, in clear words  · status: done

#### As-built

- `agent_accounts.claude_live` claims each read with `usage_store.claim_read` under an `fcntl.flock` on `~/.local/state/agent-notes/usage.lock`. A login attempted less than 90 s ago, or less than 10 minutes after a 429, is not read: the report has outcome `HELD`, no quotas and no `quota_problem`. `merge`, `agent_notes.apply` and the accounts table change nothing for it. The lock is never held during the HTTP call.
- `usage_store.finish_read` keeps the attempt outcome (`attempted_at`, `attempt_outcome`) and the reading for every claimed read, also for callers that do not save (whoami). A claimed report carries its `UsageReadClaim`; a claim older than the stored `attempted_at` changes nothing in `merge`.
- `save`, `claim_read`, `finish_read` and `quota_alert._save_codex_readings` do their load, merge and write under the lock. `write` is a plain replace and takes no lock.
- A store error in the claim reads without a hold; one in the finish keeps the live report. Each prints one stderr line.
- `accounts._read_failure_note` says "the usage service is busy, so this shows the reading from HH:MM; next try after HH:MM" (429 attempt time plus 10 minutes), "the usage read failed, so this shows the reading from HH:MM; next try in 2 min", and "the usage service did not accept this login; start a session on it to check it". With no stored reading the first two say "... and no reading is stored yet". The JSON problems list carries the same lines.
- Cause (2026-10-19 08:11 EDT): during the units move, about 28 `shutdown.py status` and `ready --where` runs each started the full snapshot, which read every Claude login. Six overlapping passes made 18 reads in 24 s, and every login got 429.

**Files:**
- `scripts/whoami/agent_accounts.py` — `HELD`, the claim types and the claim and finish around the HTTP call.
- `scripts/whoami/usage_store.py` — the lock, the attempt fields, `claim_read`, `finish_read`, `merge` for held and stale claims, `save` under the lock.
- `scripts/whoami/quota_alert.py` — the Codex save under the lock.
- `scripts/whoami/agent_notes.py` — a held report writes nothing.
- `scripts/accounts/accounts.py` — the footnote words and the attempt time on each row.
- Tests: `scripts/whoami/test_agent_accounts.py`, `test_usage_store.py`, `test_quota_alert.py`, `test_agent_notes.py`, `scripts/accounts/test_accounts.py`.

**Gotchas:**
- The 90 s hold must stay shorter than the 2-minute agent-sessions timer, or the timer holds its own reads.
- `write` needs the caller to hold `read_lock`. A second flock on the lock file in one thread blocks, so never take the lock inside a path that already holds it.
- The extra passes still run the snapshot's notes refresh (in `/etc/nixos/modules/linux/agent-sessions-snapshot.py`, outside this repo); they now read nothing. The showrunner routes the change that skips it.
- The one pool review found one blocker (a late save of an older good read cleared a newer 429) and one minor (a store error cost the read). Both are fixed with tests.

### Phase 80 — The accounts table has no Login column and no roles line  · status: done

#### As-built

- The no-argument `accounts.py status` table has the columns Folder, Label, Role, Weekly left, Weekly resets, On credits, Credits left, Read. It has no Login column and no `units: …, showrunners: …` line; it starts with its header row. The user asked for both (2026-10-19): the logins were masked, and the top line repeated the Role column.
- `status --with-login` is removed (the unit-board mod stopped passing it in its phase 16). `render_status_json` has no `with_login` parameter, and the JSON has no `login` field.
- A row with no folder, no login or an unreadable folder gets one note: `* <label>: not set up: no folder holds this account`, `* <label>: not logged in` or `* <label>: its folder could not be read`, unless a folder or sign-in note already explains it (`_account_state_notes`). It has no login address.
- Each unconfigured Claude group gets `accounts: <group> has no account; run accounts.py move <group> <claudeN>` beside the Codex workers note. A configured group gets no note. The JSON problems list carries the same lines.

**Files:**
- `scripts/accounts/accounts.py` — the columns, the removed line and flag, the state and group notes.
- `scripts/accounts/test_accounts.py`, `scripts/accounts/test_refusal_watch.py` — tests.
- `docs/accounts.md`, `commands/showrunner/accounts.md`, `docs/as-built/unit-board.md` — text.

**Gotchas:**
- `group_status_line` stays: placement still prints it.
- `accounts.py status` notes can still name a login (the "no account note, not selectable … with login:" line), so never run it to find a label.
- The one pool review found one minor (a row lost its reason with the Login column). The merge gate found that status no longer said a group was not configured. Both are fixed with tests.

### Phase 81 — Account moves: the new name, and workers to any account  · status: done

#### As-built

- `/next_moves` is `/account_moves` (`commands/account_moves.md`); the docs, the showrunner accounts skill, the push texts and the log lines (`account moves: …`) use the new name. The CLI subcommand stays `accounts.py next`, because the Mac sends it to natedev over ssh. The state file stays `next_moves.json` and the module `next_moves.py`.
- A workers entry takes a numbered Claude account, a numbered Codex account, or bare `claude` (user, 2026-10-19). `DelegateToClaude.account` is `ClaudeWorkersTarget(label)` or `DefaultClaudeAccount()`. A named account is checked for setup and login when it is added (`claude_workers_account`) and again when the entry runs; a failed check blocks the head entry with its reason.
- When a named entry runs, `switch_to_claude` writes the route to the role map before the registry edit: `workers_claude: {"label": "claude4", "until": "<source weekly reset>"}`. Quota state (`AccountCodexQuotaFallback.claude_account`) and the routing journal (`RoutedToClaude.claude_account`) keep it as history. The route is rolled back only when a successful read-back shows nothing moved; a rollback error is added to the switch failure. `switch_back` clears it early; otherwise it ends at `until`.
- Worker launches read the route from the role map on both machines (`account_for_unscoped_launch(..., workers=True)`): `agent_exec.sh`, `cli_agent.sh`, and `agent_bg.sh` with `AGENT_BG_WORKERS=1`, which only `delegate/implement.sh`'s Claude worker launch sets; agent_bg removes the flag from the child. Friend sessions and the berth fixer keep the caller or showrunners account. An unreadable map with no cache refuses the launch. A route with no `until`, or past it, is the default.
- Bare `claude` and the automatic fallback with no entry keep their old choice: a Codex account with room first, then Claude on the caller or showrunners account.

**Files:**
- `scripts/accounts/next_moves.py`, `scripts/accounts/accounts.py`, `scripts/accounts/role_map.py`, `scripts/accounts/moves.py` — targets, checks, the shared route, the launch resolver.
- `scripts/whoami/quota_alert.py` — the named switch, rollback and history.
- `scripts/agents/claude_account.sh`, `scripts/agents/agent_exec.sh`, `scripts/agents/agent_bg.sh`, `scripts/cli_agent/cli_agent.sh`, `scripts/delegate/implement.sh` — worker launches.
- `scripts/accounts/test_next_moves.py`, `scripts/accounts/test_accounts.py`, `scripts/accounts/test_role_map.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py`, `scripts/agents/test_agent_exec.sh` — tests.
- `commands/account_moves.md`, `commands/showrunner/accounts.md`, `docs/accounts.md`, `docs/quota_alerts.md`, `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — text.

**Gotchas:**
- A Mac on older code ignores `workers_claude` (the decoder skips unknown keys), so it keeps the caller account until it updates.
- Subagents inside a Claude session always use that session's account; a route cannot move them.
- One pool review and two closure reviews found 8 issues (delegate launches ignored the route, the Mac could not see it, friends took it, a failed read-back undid it, lost state kept it, a rollback error escaped, a doc example used a wrong CLI name). Each fix has a test.
- `test_codex_mesh` `test_follow_cli_accepts_each_new_flag_independently` failed once under gate load and passed 3 of 3 alone.

### Phase 82 — Every worker turn takes its speed from /agent  · status: done

#### As-built

- Every `turn/start` on a worker thread sends `serviceTier`, read at that turn from the `/agent` registry (`agents.conf`) for the worker's task: a follow, a build-result wake-up, a held result, a continuation or an account-move resume (`codex_mesh._turn_service_tier`). The task's row beats its function; `pace` takes the pacer's current choice. An empty registry tier sends `default`. The Codex config tier (`~/.codex/config.toml`) never reaches a worker turn, so a `/fast` typed in a Codex window no longer changes the workers (it put every worker on fast for 14 hours on 10-20).
- The rules live once, in `agents_config.sh` (`agents_turn_service_tier`, which reuses `_agents_resolve_service_tier`); `codex_mesh` calls it through bash on each turn with `AGENTS_CONFIG_TIER_ONLY=1`, which skips the catalog sync. A resolver error goes to the seat log and the turn sends `default`.
- `implement.sh`, `review.sh` and `launch_friend.sh` pass `--task <function.subtask>` to `codex_mesh` start and follow (prime takes it too); the roster entry keeps it. `--service-tier` now applies only at thread start. An old entry with no task uses the `delegate` function tier until its next follow records one.

**Files:**
- `scripts/agents/codex_mesh.py`, `scripts/agents/agents_config.sh` — the per-turn tier and the task record.
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/ask_a_friend/launch_friend.sh` — pass `--task`.
- `scripts/agents/test_codex_mesh.py`, `scripts/agents/test_agents_config.sh`, `scripts/agents/test_launcher_tasks.sh`, `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py` — tests.

**Gotchas:**
- A thread resumed on a server started before a config change keeps that server's tier until its next turn; from this phase on, the next turn corrects it.
- Two closure rounds: automatic turns first reused the launch tier from `stream.args`, and a task found by log file name missed review and friend threads.

### Phase 83 — Workers move to a Codex account with room before paid credits  · status: done

#### As-built

- A refusal clears on a fresh reading taken after it that shows usage left in a new window: reset unknown, earlier or later than the refused reset. A reading with the same reset (rounding, lag) keeps it (`refusals.resolved`).
- A weekly window with usage left and no reset is room (`codex_can_work`, `codex_destination_uses_credits`).
- At 0% with credits on, workers move silently to a Codex account with included usage left (`_zero_open_notice`, `accounts.swap(..., included_only=True)` checks the live reading too). Each pass of an open episode looks again, with the current use_credits setting. Queued moves and open Claude fallbacks keep control. An account with only paid credits is never a credits-on destination, so two exhausted accounts never swap.
- With no account with included room, workers spend credits and the user gets `quota.paid_credits` once (`send.py --text-id quota.paid_credits --from quota_alert --value sender/account/machine/credits/refill/next_reset`). Phase 84 removed the old ids `quota.codex_on_credits(_last)`.
- The user's rule (2026-10-21) replaces design C37.

**Files:**
- `scripts/accounts/refusals.py`, `scripts/accounts/accounts.py`, `scripts/whoami/agent_accounts.py`, `scripts/whoami/quota_alert.py`; their tests; `docs/accounts.md`, `docs/quota_alerts.md`.

**Gotchas:**
- Pool review and three closure rounds: a swap loop between two 0% accounts, room that appeared later, credits turned on during a queued wait, a paid-only live reading at swap time, and the template's `sender` value.

### Phase 84 — One paid-credits alert, with its own text for a queued move  · status: done

#### As-built

- With credits on and no move made, the opening notice is always `quota.paid_credits` (`_paid_credits_notice`: `sender`, `account`, `machine`, `credits`, `refill`, `next_reset`). The catalog-presence fallback is gone.
- While a workers entry is queued, `_current_notice` renders it as `quota.paid_credits_queued` (`sender`, `account`, `machine`, `credits`, `refill`) for the first send, the log line, the phone push and every retry. The saved notice stays `quota.paid_credits`, so a retry after the queue empties sends the ordinary text.
- `quota.codex_on_credits` and `quota.codex_on_credits_last` are never sent. An open episode saved with an old id is rebuilt as `quota.paid_credits` from the current reading, with the same pick as a fresh opening (`prefer_weekly_room=True, included_only=True`).
- messaging-unit owns both templates and removes the old ids from the catalog.

**Files:**
- `scripts/whoami/quota_alert.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_codex_alert_accounts.py`; `docs/quota_alerts.md`, `docs/as-built/message-clarity.md`.

**Gotchas:**
- One repair round: the old-id rebuild first used the default pick, so a paid-only spare hid its earlier reset.
- When an included-room account exists but an open Claude fallback holds the work, `next_reset` is the source reset (as Phase 83).

### Phase 85 — An expired Claude login renews itself before anyone is asked  · status: done

#### As-built

- `refusal_watch.run` first calls `renew_expired_logins`, outside `next_moves_lock`. For each noted Claude account on this machine whose latest stored read outcome is `STALE_TOKEN`, it calls `accounts.renew_login`, at most once per account per 60 minutes. Each attempt (`at`, `renewed`/`failed`, detail) is kept in `~/.local/state/accounts/login_renewals.json`. A dry run says `would renew claudeN`.
- `renew_login` runs `accounts.py exec --probe <label> -p hello` (the command `hello.sh` wraps) in `/tmp`, with stdin from DEVNULL, a 120 s timeout and captured output that is never logged. It then reads usage with `claude_live` and saves the reading. The result is `LoginRenewed` only for exit 0 and a read that no longer shows an expired login. Otherwise it is `LoginRenewalFailed(detail)`.
- After a renewal, the rest of the pass uses a clock read taken after the probe, so the new reading is fresh and a due move goes on in the same pass.
- A move target with an expired login blocks with `TargetReadingUnavailable(<last weekly read time>)` until the next renewal. After a failed probe, it blocks with `quota_alert.TargetLoginRenewalFailed(detail)`: "<group> 1 → claudeN due, claudeN's login expired, and a probe session on it failed (<detail>). Log in on claudeN by hand." This goes by the blocked-head relay to the showrunners, who push the user.
- The status footnote for an expired login reads "the next notes pass renews it", or "a probe session at HH:MM failed; log in on it by hand". A damaged renewal file reads as no attempts.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/refusal_watch.py`, `scripts/whoami/quota_alert.py`; tests in `scripts/accounts/test_accounts.py`, `scripts/accounts/test_refusal_watch.py`, `scripts/whoami/test_quota_alert.py`; `docs/accounts.md`.

**Gotchas:**
- One repair round: the pass first used the clock from before the probe, and `stored_reading` refused the renewed reading as future-dated.
- The probe holds the refusal-watch lock for up to 120 s per expired account.

### Phase 86 — The login renewal finds claude by its full path  · status: done

#### As-built

- `accounts.resolve_program("claude"|"codex")` returns an absolute path: `shutil.which` on the PATH in use, then `~/.local/bin/<tool>` (the install place on natedev and the Mac, from nix `claude-code.nix` and darwin `shell.nix`). It raises `FileNotFoundError("<tool> program not found (looked in PATH and <path>)")`.
- Claude `exec` (the probe and supervised relaunch included), Codex `exec`, and `lib/relaunch.py` for a bare `claude`/`codex` command all start the resolved path.
- `renew_login` turns a probe exit 2 with no program found into `LoginRenewalFailed("claude program not found (…)")`.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/lib/relaunch.py`; tests in `scripts/accounts/test_accounts.py`, `test_cutover_home.py`, `test_role_map.py`, `scripts/lib/test_relaunch.py`; `docs/accounts.md`.

**Gotchas:**
- A test that mocks `execvpe` for a Claude or Codex start needs a scratch executable on its PATH or in its scratch `~/.local/bin`; the gate caught two that had none.

### Phase 87 — A move never strands units: target check, source fallback, restart skips the running  · status: done

**Depends on:** Phase 86; shutdown-unit's hold API (eb30ce4a)

#### As-built

- Cause of the 10-26 16:37 AST outage: a move keeps its stop records under the source login, and the target's `launch_blocked` found no record there, so a stale claude2 stop hold refused every restart.
- One target hold check in `accounts.py` runs before any stop in the hand group move and the single move (through `check_move_target`), and in every `refusal_watch` move path (queued rule, refusal-driven move, completing a committed group). It calls `launch_permission(target env, NewWorkLaunchPurpose())`. Blocked or unreadable: no stop, no write; the hand move returns `MoveRefused("accounts: <reason>; no session was stopped")`, and the queued rule records a blocked head with `quota_alert.TargetHeldByShutdown(reason)`. A dry run reports the planned check and does not call it.
- A released finished hold (`ReleasedHold`) gives one line: "cleared a finished <then> hold on <label> from <local time> (session <8> | requested in a terminal or by a recovery snapshot)". It is relayed to the showrunners through one helper (quota_alert session recipients, no phone push); undelivered notices wait in `pending_cleared_holds.json` under the accounts state dir and are retried per recipient. The recorded `why` stays exactly `auto: <id>`.
- `restart.py`: every target-start failure of a moved session (exception, failed prompt outcome, not live at confirmation) goes to `_try_source_fallback`, which runs only when the target is confirmed absent. It pins the record to the source, starts a unit through `_unit_argv` → `add_unit --source-fallback --account <source>` (other sessions through their own launch), requires a submitted prompt and a live session, and records "<name> came back on <source>: <target> refused (<reason>)". Both failing: the record returns to the target, becomes `restart failed` with both reasons, and sends `shutdown.move_failed` once, hand or auto.
- `add_unit.account_for_launch(..., source_fallback=True)` and `accounts.account_for_group_launch(..., source_fallback=True)` allow another account only for a recorded unit restore whose shutdown record names it as the source; every other launch keeps the group rule.
- `shutdown.py restart --only`: names resolve one at a time against records first (peer records included); a recordless live session (any account) prints "<name>: already running; skipped"; an unknown name, or one with records on both machines, gets its own line; the others restart; selection errors exit 1. `cancel` and `now` keep "no shutdown in progress".

**Files:** `scripts/accounts/accounts.py`, `refusal_watch.py`, `scripts/whoami/quota_alert.py`, `scripts/shutdown/restart.py`, `shutdown.py`, `scripts/production/add_unit.py`; tests in `scripts/accounts/test_refusal_watch.py`, `scripts/shutdown/test_account_move.py`, `test_restart.py`, `test_shutdown_cli.py`, `scripts/whoami/test_quota_alert.py`, `scripts/production/test_add_unit.py`; `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`.

**Gotchas:**
- The gate script must fetch the GitHub origin, not the local `~/.claude` clone: the local `build-followups` ref can hold a showrunner merge still in its gate.
- Since shutdown-unit's 088f0322 every restart comes back in the shared herdr session; `test_restart`'s setUp patches `herdr.shared_session` to "writing", so a test expects that, never a recorded host.
- Restarts launch in a pool: assert launched names with `assertCountEqual`.
- Carried to Phase 88: F009 (a retried cleared-hold notice after a `QUEUED` result queues again; jobs have no name).

### Phase 88 — A unit watchdog restarts any unit that is down, and a gate test rebuilds the 10-26 outage  · status: done (4b8cbb3e)

**Depends on:** Phase 87

#### As-built

- `scripts/accounts/unit_watchdog.py` runs from the two-minute notes timer on natedev. A unit that is down with no user or account stop and no work in progress gets one pass of grace, then a restart on its group account (`accounts.account_for_group_launch`), else its source account. Retired and finished units are skipped.
- Restarts run detached (`systemd-run --user --no-block`, one 30 s deadline for all submissions per pass); the result is read on a later pass. A job is always consumed before a stale restart claim or a missing record can drop its outage. State: `~/.local/state/accounts/unit_watchdog.json`, results in `unit-watchdog-results`.
- A crash in a live herdr host (registry not live, pane has no live agent: `restart.watchdog_crash_signal`) is saved as snapshot `crash: "agent exited"` (`record.mark_snapshot_agent_exited`); with an unreadable probe the watchdog keeps its own copy and restores it (`restore_snapshot_agent_exited`) once the exit is confirmed. The store entry always wins over the copy. Restart turns it into `AgentExitRecovery` with `last_seen` and names it in the prompt.
- A queued restart checks its saved source under the restart claim (`RecoverySourceAuthorized` / `RecoverySourceChanged`); a newer stop or account hold stops the launch.
- Alerts: the showrunner gets a line for each attempt. The phone gets one delivered alert per outage: `shutdown.move_failed` for a move record (shared `record.claim_restore_alert`; a failed push releases it, a live unit clears it), `shutdown.restart_failed` with a cause for a crash. A restore whose folder is gone (shutdown 61459ed7) ends the outage with one alert.
- The gate test in `test_unit_watchdog.py` rebuilds 10-26: a stale stop hold on the target and the stopped units all come back.

#### Files

`scripts/accounts/unit_watchdog.py` (new), `refusal_watch.py`, `scripts/shutdown/restart.py`, `record.py`, `session_types.py`; tests `test_unit_watchdog.py` (new), `test_refusal_watch.py`, `test_restart.py`, `test_record.py`; docs `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`.

#### Gotchas

- Run the full merge gate after every origin merge: shutdown 61459ed7 (restore dropped when its folder is gone) turned five watchdog tests red only after the merge; give restored sessions a real scratch folder.
- `test_launch_accounts` requires every Claude launcher to pick its account through `account_for_group_launch`, and `scripts/lib/test_user_zone` requires `astimezone(user_zone())`; neither is in the accounts suites, so run them.
- Residual (fixed in Phase 89): a worker could deliver `move_failed` and a later missing-folder retry archived the record, so a second, different `restart_failed` push followed for that outage.

### Phase 89 — One outage sends at most one phone push  · status: done (936445e3)

**Depends on:** Phase 88

#### As-built

- `scripts/accounts/outage_alerts.py` (new) is a per-session outage ledger in `~/.local/state/accounts/unit_outage_alerts.json`, under a file lock: `claim` → `delivered` or `release`, `reserve` for the sessions of one push, `clear` when the session is live. It survives the shutdown record's archive.
- Every per-session outage push uses it: `settle.report_stop`, the `shutdown.py restart` CLI, the restart fallback and the watchdog's crash, move and missing-folder alerts (`restart.send_failure_pushes` is shared). A sender claims one push's sessions just before it sends, names only the sessions it holds, marks delivery only after a sent push and releases the claim otherwise.
- A claim stores its owner pid and time; a dead owner or a claim older than 15 minutes is taken over with a new token, and the old token can no longer deliver or release.
- The machine that owns a session sends its pushes: the peer's `up --here --json` run sends from its own ledger (unless `--no-alert`, which `shutdown.py` now forwards); the calling CLI only prints the peer's lines.
- Every place that settles a record because its session is live clears the ledger: `clear_running_on_target`, `_record_timer_restore_progress`, `confirm_entry`, `_confirm_entries`, `settle._archive_finished`, the live skips in `launch_entry`/`relaunch_session`/`_up`, and the watchdog's live observation. `drop_restore_without_folder` keeps it. Phase 90 changed this: timer settlement and archives no longer clear, and each clear removes only alerts claimed before its observation.
- Not per-session outage pushes, left outside the ledger: `restart_incomplete`, `shutdown.stop_failed`, the holdout notice.

#### Files

`scripts/accounts/outage_alerts.py` (new), `unit_watchdog.py`, `scripts/shutdown/restart.py`, `settle.py`, `shutdown.py`; tests `test_unit_watchdog.py`, `test_restart.py`, `test_settle.py`; docs `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`.

#### Gotchas

- Copying the record's `restore_alerted` mark into the watchdog outage fails both ways: an unfinished claim reads as delivered, and an archive before the next pass loses the mark. The ledger must be separate from the record.
- Reserving all of a report's pushes up front lets a late claim pass its lease behind slow sends; claim per push.
- Residual (fixed in Phase 90): a slow batch confirm could clear a newer outage's entry and allow a second push.

### Phase 90 — A slow batch restart does not clear a newer outage's alert  · status: done (7e92e7b5)

**Depends on:** Phase 89

#### As-built

- `outage_alerts.clear(session_id, observed_at)` removes only an alert claimed strictly before `observed_at`; an alert claimed at or after it belongs to a newer outage and stays. A delivered entry keeps its claim time. `clear` returns whether the outage ended, and the record mark `restore_alerted` is cleared only then.
- Every live clear passes the time its liveness read started (`time.time()`): `_up`'s cached `live_ids`, the shared registry confirmation (`RegistryConfirmation.observed_at`, used by `confirm_entry`, `_confirm_entries` and `settle._confirm_relaunch_pass`), `clear_running_on_target`, the live skips in `launch_entry`/`relaunch_session`, the source fallback and the watchdog's probe.
- Later timer settlement (`_record_timer_restore_progress`) and record archives (`_confirm_entries`, `settle._archive_finished`) no longer clear the ledger.

#### Files

`scripts/accounts/outage_alerts.py`, `unit_watchdog.py`, `scripts/shutdown/restart.py`, `settle.py`; tests `test_unit_watchdog.py`, `test_restart.py`, `test_settle.py`; docs `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`.

#### Gotchas

- A clear must use the time of the observation it acts on, never the time of the clear: a cached reading is old by the time a slow loop reaches it.
- Known minor residual: an old alert claimed inside the one registry read that sees the unit live survives the clear; a new crash before the next watchdog live pass (about 2 minutes) then sends no push.
- `scripts.agents.test_mesh_runs` `test_a_live_supervisor_is_not_ended` failed once under the full parallel gate and passed alone and on the rerun: a load flake outside this unit.

### Phase 91 — The account notes cost less CPU  · status: done (b0c06054)

**Depends on:** Phase 90

#### As-built

- The 2-minute timer pass of `agent_notes.py` reuses a usage reading younger than 10 minutes (`usage_store.recent_timer_usage`/`save_timer_usage`, file `timer-usage.json`, keyed by `timer_usage_signature` over logins, homes and the account selection). A reused pass keeps the original reading times and skips the quota note apply, the usage store save and the readings log.
- A pass reads live when it is `agent_notes.py refresh`, a next-moves queue waits on the role-map host, an in-use account (Claude in use, or the selected Codex workers account) is at or below `LIVE_USAGE_REMAINING_THRESHOLD` (10%) weekly or 5-hour remaining, a Codex fallback is active, a reset passed, or a login or selection changed.
- The refusal watch, the unit watchdog, the session table and the note sessions sections run on every pass. `refusal_watch.run(refresh_usage=…)` reads usage before it places a new refusal on a reused pass. Refusals already open, cancelled, continued or group-selected do not cause a read. A failed read is reported and never stops placement or the watchdog. The refresh saves with the same login labels as the main pass.
- `live_reports` passes `use_disk_identity=True`: `claude_live` takes the login and `subscriptionType` from the stored credentials and starts no `claude auth status`. Without a stored plan it keeps the CLI read, so `/whoami` and the notes show the same plan as before.
- Measured before: one timer run of `agent_notes.py` used 43.4 s CPU in 39.5 s wall, and the service used 42–64 s per run. The figures after promotion come from the real timer runs.

#### Files

`scripts/whoami/agent_notes.py`, `agent_accounts.py`, `usage_store.py`, `scripts/accounts/refusal_watch.py`; tests `test_agent_notes.py`, `test_agent_accounts.py`, `test_usage_store.py`, `test_session_table.py`, `test_codex_homes.py`, `test_quota_alert.py`, `scripts/accounts/test_refusal_watch.py`; `docs/accounts.md`.

#### Gotchas

- Weekly and 5-hour usage alerts can come up to 10 minutes later. The showrunner accepted this, because those alerts only give information.
- An in-use account that runs out from above 10% inside the 10 minutes is not moved from the reading. It moves on its first refusal: Codex at once through `codex_mesh`'s `blocked` path, Claude on the next 2-minute refusal pass. The worst case is one refused request before the move.
- Measure a timer cut on the real timer runs (`journalctl --user -u agent-sessions-snapshot.service`, "Consumed … CPU time"). A by-hand `refresh` always reads live, so it shows no reuse.

### Phase 92 — Every claude and codex launch loads the folder's nix shell  · status: done

**Depends on:** Phase 91

#### As-built

- `accounts.py exec <label>` loads the nix shell of the launch folder before it starts Claude, Codex or the `relaunch.py` supervisor, for every label: `claude`, `claudeN`, `codexN`, `units`, `workers`, `showrunners`, `unit`. One call site, `launch_shell_environment(Path.cwd())`, feeds the Claude and the Codex branch of `exec_account`. It calls `scripts/lib/nix_env.ensure(directory, os.environ)`, imported lazily through `import_module("scripts.lib.nix_env")`. There is no second loader.
- A refused folder, a missing helper or an unknown result raises `LaunchShellError`. `main` prints the helper's line (for example the `direnv allow` command) and exits 1; no agent starts. Other exec errors stay exit 2, which `renew_login` reads as "program not found".
- After the load, the caller's `HOME` is put back (or removed when the caller had none), and then the account variables are applied. A folder `.envrc` cannot change the account.
- `exec claude` uses the default `~/.claude` home: `PreparedAccount({}, remove={CLAUDE_CONFIG_DIR, CLAUDE_CODE_REMOTE_MEMORY_DIR, CLAUDE_CODE_OAUTH_TOKEN})`, with no readiness check and no group selection. `--probe` works.
- `resolve_program(tool, *, environment)` searches the loaded `PATH`, then `~/.local/bin`. It skips a `#!` script that names `nix_env.py` or `lib.nix_env`, so a `PATH` wrapper cannot load the shell two times. An executable it cannot read is still used. A `PATH` with only wrappers gives `accounts: could not start ...` and exit 2.
- The `claude-unit` alias is gone from the docs and the plan; `exec unit` stays.
- Live check: a folder whose `.envrc` was not allowed yet loaded 27 nix `PATH` entries and kept `HOME`; a folder with no `.envrc` loaded none; `exec claude --version` printed the version in both.

#### Files

`scripts/accounts/accounts.py` (`LaunchShellEnvironment`, `LaunchShellRefusal`, `NixShellLoader` protocols, `LaunchShellError`, `launch_shell_environment`, `resolve_program`, `exec_account`, `_exec_codex_account`, `main`); `scripts/accounts/nix_env_fake.py` (a fake helper with no side effects, and `imports_with_nix_loader`); tests `test_launch_accounts.py`, `test_accounts.py`, `test_cutover_home.py`, `test_role_map.py`; `docs/accounts.md`, `commands/showrunner/accounts.md`.

#### Gotchas

- Tests patch `accounts.import_module` with `nix_env_fake.imports_with_nix_loader`, and each exec test sets the fake in `setUp`. Never patch `sys.modules`: it changed the imports of other modules' tests.
- On natedev `~/.claude` holds no login (home cutover, `home_cutovers` in `config/accounts.json`), so `exec claude` starts signed out there; `docs/accounts.md` states this. The bare `claude` alias (`/etc/nixos/modules/common/shell.nix`) runs `exec showrunners --remote-control ...`, which loads the shell too. Do not point it at `exec claude` while `~/.claude` has no login.
- `scripts/lib/test_claude_launch_scan.py` exempts `accounts/accounts.py`. Its guard check finds only a literal `nix_env.ensure` call, not `launch_shell_environment`.
- The Mac has explicit `claude1`-`claude4`, `codex`, `codex1` and `codex2` aliases in `modules/darwin/shell.nix`. `new_account.alias_step` still returns None on darwin and reads only `modules/linux/shell.nix`.

#### Ruled out

- Patching `sys.modules` in test fixtures: it changed other modules' tests.

### Phase 93 — /new_account gives the alias step on the Mac  · status: done

**Depends on:** Phase 92

#### As-built

- `/new_account` gives the `claudeN` or `codexN` alias step on natedev and on the Mac. `_shell_module_path()` gives `modules/darwin/shell.nix` on darwin and `modules/linux/shell.nix` on other machines. `nixos_shell_module()` looks for that path in `/etc/nixos`, then in `accounts.accounts_home() / "nixos"` (`~/nixos` on the Mac).
- `alias_step(account) -> Step` never returns None. It has four results:
  - No module found: not done. The text names the account and the path it looked for.
  - The module holds `ALIAS_LOOP_MARKER` (`hanadocs/agents/claude`) and the account is a Claude account: done, the alias comes from the account note. For a Codex account the marker does not count.
  - A `<name> =` line is in the module: done, live after a rebuild.
  - Else: not done. It prints the line to add to `shellAliases`, then rebuild: `<name> = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec <label>";`.
- A new `codexN` in a module that has only the Claude note loop and no `codexN =` line gets the add line (`test_claude_alias_loop_does_not_define_a_new_codex_alias`).
- `new_account` appends the alias step for a Claude account with no None check. `codex_steps` appends it after a successful setup and after a successful signed-in check. A failed setup or check returns no alias step.

**Files:**
- `scripts/accounts/new_account.py` — `_shell_module_path`, `nixos_shell_module`, `alias_step`, `new_account`, `codex_steps`.
- `scripts/accounts/test_new_account.py` — Mac and Linux module lookup and repository order, existing and missing alias lines, missing module path text, Codex setup and signed-in paths on both platforms, the Claude-loop-only test. Scratch repositories only.

**Gotchas:**
- The Phase 92 gotcha that `new_account.alias_step` "still returns None on darwin" and reads only `modules/linux/shell.nix` is no longer true. `alias_step` reads this machine's module on both machines.

### Phase 94 — The notes timer pass costs less than 10 s of CPU  · status: done

**Depends on:** Phase 91

#### As-built

- A reused argument-free pass of `scripts/whoami/agent_notes.py` gives the same note text, sessions sections, alerts, pacer tier, refusal placement and watchdog as before. Its two largest costs are outside `agent_notes.py`: the refusal watch transcript lookup and the Codex pacer rollout listing.
- `refusal_watch.session_refusals(session_ids: list[str]) -> dict[str, TranscriptRefusal | ProofRefusal | NoSessionRefusal]` finds the transcripts of one pass with one `os.scandir` listing per project folder under `~/.claude/projects`. When a folder cannot be listed, it does known-name `exists()` lookups in that folder. An error on one path does not stop the other paths. `_run` calls it in place of one `session_refusal` glob per session. `transcript_refusal` and `session_refusals` share `_transcript_refusal(paths)`, which reads the newest transcript by mtime. The index lives for one pass; a new transcript or exchange is read on the next pass.
- `codex_pacer.tick` lists rollouts once (`rollout_files`) and gives the list to `ingest` and to `prune(state, now, retained_rollouts)`. `rollout_files` sorts each day with `key=str`. `prune` keeps only the offsets of listed rollouts that still exist. `backfill` gives `prune` its own `rollout_files(now, SCAN_DAYS)`.
- At a live-sized fixture the pass goes from 0.587 s to 0.212 s. On natedev the live CPU per run shows no measurable drop: 9.1–9.2 s before, 9.4–11.7 s after, at load 120–130.

**Files:**
- `scripts/accounts/refusal_watch.py` — `session_refusals`, `_transcript_refusal`, `_run`.
- `scripts/whoami/codex_pacer.py` — `rollout_files`, `tick`, `prune`, `backfill`.
- `scripts/accounts/test_refusal_watch.py` — one search per project with each latest exchange; a project folder that cannot be listed.
- `scripts/whoami/test_codex_pacer.py` — filename order, one listing per tick with the same plan and state, a deleted rollout before its replacement.

**Gotchas:**
- A project folder can be traversable but not listable. Keep the known-name fallback, or readable transcripts are missed.
- A saved offset for a deleted rollout must not apply to a new file at the same path; `prune` checks `exists()`.
- The live 10 s target is not met. The rest of the live cost is outside what the fixture profile reaches.

**Ruled out:**
- A cut in `agent_notes.py`: its own work is not the largest cost in the profile.

### Phase 95 — A failed restart shows its saved screen  · status: done

**Depends on:** Phase 94 (order only)

**Also touches:** shutdown-unit (`scripts/shutdown`).

#### As-built

- A `restart failed` record whose opening-prompt submit failed shows the last screen that submit saw. The screen comes from the Phase 69 receipt `<SHUTDOWN_STATE_DIR>/<login>/restart-note-<session id>.prompt-outcome.json` (ANSI-stripped, last 40 lines).
- `restart.saved_prompt_screen(record) -> str | herdr.PromptScreenNotRead` reads the receipt through `_prompt_outcome(record)`, with its request id, session id and target checks. It returns the screen of a `herdr.PromptNotSubmitted` that read one, else `herdr.PromptScreenNotRead()`.
- `settle.failed_prompt_screen_lines(saved)` is the one render function: the header `  last screen of the failed prompt submit:`, then each screen line indented 4 spaces. It gives lines only for a `restart failed` record with a screen.
- Three surfaces put these lines under the record line:
  - `shutdown status`: `shutdown._shutdown_record_lines`, for the live form (`settle.running_restart_line`) and for `settle.status_record_lines`, only when `local_observation` is true. A peer record read from `records --json` has no local receipt and prints no screen.
  - `shutdown follow`: `shutdown._follow_action`, for a live and a not-live `restart failed` record, with no machine check (follow reads only local records).
  - The stop report: `settle.report_stop`, after the `running_restart_line` line or the `<name>: <reason>` line, when `saved["machine"].casefold() == local_machine().casefold()`.
- Phone pushes (`restart.send_failure_pushes`, `settle.alert_user`) and `settle.tell_showrunners_outcome` do not change.

**Files:**
- `scripts/shutdown/restart.py` — `saved_prompt_screen`.
- `scripts/shutdown/settle.py` — `failed_prompt_screen_lines`, `report_stop`.
- `scripts/shutdown/shutdown.py` — `_shutdown_record_lines`, `_follow_action`.
- `scripts/shutdown/test_restart.py` — the reader: a matching receipt with a screen, one with no screen, one of another request, a missing or unreadable file.
- `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_cli.py` — the stop report (live, absent, mixed-case machine), status (live and not live), follow, a peer record, a receipt with no screen, unchanged push text.
- `docs/as-built/shutdown-and-restart.md` — where the saved screen shows.

**Binds later work:** `settle.failed_prompt_screen_lines` holds a function-local `import restart as restart_work`. It is a sibling shutdown import, the same as the other function-local ones in `settle.py`, and is not a shared whoami module import ("All consumers import shared whoami modules by one name").

**Gotchas:**
- Compare machine names with `casefold()`: a record can name `Mac` on a host that reports `mac` (`test_stop_report_shows_saved_screen_when_local_machine_case_differs`).
- A receipt holds a screen when the submit read the pane at least once. `herdr.wait_for_prompt_input` keeps the last screen it read, so a failure before any typing can hold one too (`destination never became ready for input` after a `destination has no empty composer` read). A receipt has `PromptScreenNotRead` when no pane read happened, or when `restart.py` saved its own reason (`launch has no submit proof`, `restart launcher supplied no submit proof`). The surfaces print nothing for it.
- `scripts/accounts/test_unit_watchdog.py` calls `settle.report_stop`. A gate for a change to `report_stop` runs the accounts suite too, not only `scripts/shutdown`.

### Phase 94b — Cut the top live cost of the notes timer pass  · status: done

**Depends on:** Phase 95 (order only)

**Also touches:** herdr-unit (`scripts/herdr`).

#### As-built

- One notes timer pass does one herdr process scan. `herdr.process_snapshot()` is a scope that `agent_notes` main opens; in it, every `herdr_processes` reader gets the same cached scan. `herdr.process_action()` reads fresh and retires the snapshot for the rest of the pass. A copied context obeys a retired ancestor scope.
- `refusal_watch.session_observation` records the registry identities (sessionId, pid, procStart) before the report snapshot opens. Committed-group completion enters `process_action` only when the identities changed, no observation exists, or the cached members pass the completion filters. Inside that scope it builds the membership again and applies the same filters before holds, account preparation or session moves. Refusal placement and ready queued moves keep their fresh reads. An unchanged idle pass, the watchdog report included, does one scan.
- `unit_watchdog._live_account` has the `@herdr.process_action()` decorator, so each call scans fresh. The outage check proves a reported outage fresh before recovery. For an observed-live unit with a pending job, `_live_account` confirms the unit before the recovery-result delete, the outage-alert clear and the `restarted on` line. An absent fresh result goes to the not-running branch; an unreadable one keeps the outage and result evidence through the `watchdog: <production>: <error>` line.
- `scripts/whoami/profile_notes_pass.py` (`--stats`, `--scratch`, `--peer-python HOST=/nix/store/<python-package>/bin/python3`) runs `agent_notes.main` in-process under `cProfile`. Each write and act is a no-op patch, and state files are copies in a private scratch folder. Child reads go through one `TrustedChildRead` table: a pinned canonical executable, an exact argv grammar and a fixed environment per row; all other children are refused and counted as suppressed. Local Python reads run `sys.executable -B -I -S` directly, with no `py` wrapper. The output gives CPU, `incomplete components`, the top 25 cumulative entries, child counts and each patched call.
- Live A/B on natedev at load 134–142: 9.18/8.37 s → 5.61/5.24 s CPU per pass (−38%); herdr scans 3 (4.62 s cumulative) → 1 (1.45 s).

**Files:**
- `scripts/herdr/herdr.py` — `process_snapshot`, `process_action`, the scan cache.
- `scripts/accounts/refusal_watch.py` — `session_observation`, the completion precheck, `_action_group_members`.
- `scripts/accounts/unit_watchdog.py` — the fresh outage proof, the `_live_account` decorator, the observed-live order.
- `scripts/whoami/agent_notes.py` — opens the snapshot and observation scopes.
- `scripts/whoami/profile_notes_pass.py` — the profile script.
- Tests: `scripts/herdr/test_herdr.py` (scopes, copied context), `scripts/accounts/test_refusal_watch.py` (one scan on an idle pass, a unit started after the snapshot, a changed pid or process start), `scripts/accounts/test_unit_watchdog.py` (`test_live_report_without_fresh_confirmation_keeps_recovery_evidence`, `test_unreadable_fresh_confirmation_preserves_recovery_result_and_job`), `scripts/whoami/test_agent_notes.py`, `scripts/whoami/test_profile_notes_pass.py` (child table, pinned Python, patch list).

**Binds later work:**
- `_live_account` is wrapped in `herdr.process_action()`, and its unreadable path keeps the `watchdog: <production>: <error>` line. The recovery-result branch still clears before it confirms, and the held or blocked branch relies on `_recovery_result` deleting the file ("The watchdog keeps recovery evidence until a fresh check confirms the unit").
- `scripts/whoami/profile_notes_pass.py` imports `usage_store`, `run_out` and `agent_accounts` by bare name, in code and in `import_module("<name>")` strings. If a second module object exists, its no-op patches miss it and a profile run can write live state ("All consumers import shared whoami modules by one name").

**Gotchas:**
- Put "an act reads fresh" around the act, not at the read boundary of a function that decides and acts; else every idle decision scans again.
- A remote read in the profile needs `--peer-python`. With no pin it is refused and shows only in the suppressed-child count, not in `incomplete components`.
- The profile's git rows do not stop a repository-local clean filter under `git status --porcelain`. The ~/.claude worktrees have `filter.claude-settings.clean` (jq, stdin to stdout); it is read-only in practice.
- natedev load is often 100 or more, which makes CPU time read high. Compare runs at a similar load.
- Never run `agent_notes.py` by hand without the profile script: it sends alerts and can move sessions.

**Ruled out:**
- A `py` wrapper row in the child table: the wrapper writes its interpreter cache.
- `ssh -F /dev/null`: it drops the configured `mac` host mapping.

### Phase 94c — Each account problem says what is wrong, why, and what to do  · status: done

**Depends on:** Phase 94b (order only)

#### As-built

- Each problem under the accounts table is one line in the form `subject: what is wrong; cause; step`, with the facts, times and commands of the earlier texts. `render_status_problem` in `scripts/accounts/accounts.py` is the one formatter for the terminal table and for `status --json` `problems` (still `list[str]`; the unit board shows it as is). A footnote problem starts with `* ` in the terminal; `render_status_json` strips it.
- `StatusProblem(subject, wrong, cause: ProblemCause | NoSeparateCause, step: ProblemStep | NoUserStep, footnote, origin, state_source: ObservedStateCause | NoObservedStateCause)`. `ProblemCause(kind, explanation, location, account, key: ProblemCauseKey | OwnProblemCause)`. `ProblemCauseKey(kind, account, location)` is the full identity of one cause on one account or home. `ObservedStateCause(diagnostic, key)` holds the direct directory or identity fault that explains an account state; `NoObservedStateCause` means that no direct fault is known.
- One cause gives one line. `merge_status_problems` drops a derived account-state summary only when the direct cause that its `state_source` names is present. Then it keeps one problem per cause key and removes equal lines.
- A test sign-in (`is_test_login`: `.invalid`, `.test`, `.example`, `.localhost`, `example.com`, `example.net`, `example.org` and their subdomains; RFC 2606 and 6761) is its own cause. Its line names the file and its `st_mtime` in the user's zone, and gives the step `delete <file>`. It drops only its own cutover-home and account-note advice; other problems of the same home stay. Status deletes nothing.
- With stderr not a terminal, `read_status` holds stderr. A successful read keeps all notes on stdout and in `--json`, and writes nothing to stderr. The role-map cache line (the cache time in the user's zone and the real fetch failure, `natedev fetch failed: <reason>`) and usage claim and save failures (`agent_accounts.UsageClaimFailed`, `UsageSaveFailed` in `Report.store_warnings`) are problem lines. Other held lines become `accounts: read warning; <line>` problems. When a later read or render fails, the held stderr is written before the error (`StatusCommandWarnings.read_stderr`, replayed by `main`'s status branch).
- `role_map.RoleMap.provenance` is `CurrentRoleMap | CachedAfterFetchFailure | ExplicitStoredRoleMap`. `role_map.observed_map_provenance()` gives the provenance of the map that the command used; `cached_map_warning(provenance)` is the raw stderr counterpart of the cached-map problem. Outside status, the stderr advisory names the real fetch failure, not "natedev unreachable".
- Checkpoint `5ad6542f`. Its commit body holds the Assertion-Change lines for the changed problem texts.

**Files:**
- `scripts/accounts/accounts.py` — `StatusProblem`, the cause types, `render_status_problem`, `merge_status_problems`, `_bind_usage_cause`, `_bind_role_cause`, the test-login cause, `read_status` and `StatusCommandWarnings`.
- `scripts/accounts/role_map.py` — `RoleMapProvenance`, `observed_map_provenance`, `cached_map_warning`.
- `scripts/whoami/agent_accounts.py` — `UsageClaimFailed`, `UsageSaveFailed`, `Report.store_warnings`, `usage_store_warning_line`.
- `docs/accounts.md` — the line form and the test-login line.
- Tests: `scripts/accounts/test_accounts.py` (`StatusProblemCauseTests`, each problem line, the merged `~/.codex` case, each reserved domain, piped stderr), `scripts/accounts/test_cutover_home.py`, `scripts/accounts/test_role_map.py`, `scripts/accounts/test_refusal_watch.py`.

**Binds later work:**
- "Each derived account problem names the cause it comes from" owns findings F015-F017 and F019: `_bind_usage_cause` and `_bind_role_cause` still pick a cause by kind lists; the mcpServers check reads a stale `~/.claude.json`; typed problems (cache, stored map, usage claim and save failures) are lost when a later read or render fails, because `StatusCommandWarnings` holds raw stderr only. `check()` returns `AccountInspectionProblems`, a `list[str]` subclass that carries `state_source`, and `account_statuses` reads it by `isinstance`.

**Gotchas:**
- `_account_state_notes` matches `status.problems` text prefixes (`accounts: <path> is missing`, `is not a directory`, `is a link`, `is not logged in`, `accounts: could not read|inspect <path>:`). New words break the match with no error. The merge reads typed causes only, never text.
- The fake sign-in in the live `~/.codex/auth.json` is removed only with the user's yes. The fixture leak in `scripts/agents/test_codex_mesh.py` belongs to shutdown-unit.

**Ruled out:**
- One cause-key form for all producers: derived symptoms cannot be matched to their cause by key parts; they must carry their source.

### Phase 94d — The watchdog keeps recovery evidence until a fresh check confirms the unit  · status: done

**Depends on:** Phase 94b (order only)

#### As-built

- `RecoveryFinished(failure, result_path)` in `scripts/accounts/unit_watchdog.py` is a finished recovery result that is read but not yet deleted. `_recovery_result` does not delete the result file; each branch of `run()` that uses up the result deletes it.
- Recovery-result branch: the result is deleted and the job is popped only after a fresh `_live_account` read answers. A live unit gets `restarted on <account>`. An absent unit gets the retry line and the recovery. An unreadable read (`OSError`) keeps the result file, the job and the outage, and the error path writes `watchdog: <production>: <error>`; the next pass reports the restart or the retry.
- Held or blocked branch (`(source.blocked and not owns_claim) or _held(source, dry_run=False)`): deletes the result file, then `del retained[key]`. No file stays in `unit-watchdog-results`.
- No-recovery-source branch: deletes the result, then sets `dropped`, then pops the job. A failed delete keeps the job and writes no `dropped`, so a later pass retries cleanup and recovery when a source returns.
- `test_child_launch_options_cannot_substitute_executable_or_run_hooks` rejects the ps listing form `("ps", "-eo", "pid=,ppid=,comm=,args=")` with a launch option, and a positive control starts the same argv once with no launch option. The test fails only when the launch-option check is missing.
- Checkpoint `182c9f80`. Its commit body holds the Assertion-Change line for `scripts/whoami/test_profile_notes_pass.py::test_child_launch_options_cannot_substitute_executable_or_run_hooks`.

**Files:**
- `scripts/accounts/unit_watchdog.py` — `RecoveryFinished.result_path`, `_recovery_result`, the three branches of `run()` that use up a finished result.
- `scripts/accounts/test_unit_watchdog.py` — seven tests: the unreadable second fresh read, live and absent confirmation, held, blocked, no recovery source, and the failed-cleanup retry (`test_failed_result_cleanup_retries_when_a_recovery_source_returns`).
- `scripts/whoami/test_profile_notes_pass.py` — the launch-options test and its positive control.

**Gotchas:**
- Each branch that uses up a finished result deletes the result file before it writes a final mark (`dropped`, `del retained[key]`, the job pop). A failed delete is a third outcome next to live and absent: it must leave the job retryable. A saved `dropped` together with a job makes every later pass skip cleanup and recovery.

**Ruled out:**
- Delete the result after `dropped` is recorded in the no-recovery-source branch: a failed delete then stops recovery for good.

### Phase 94e — Each derived account problem names the cause it comes from  · status: done

**Depends on:** Phase 94c (order only)

#### Work Order

**Goal:** Phase 94c made each account problem one line in the form `subject: what is wrong; cause; step`. Its merge still infers which symptom comes from which cause. Routed from the Phase 94c repair round 4 Astra review (`/tmp/claude/astra/20261029T204639Z-1214987/review.md`, items 2-6): a symptom names its source cause where the source is observed, and the merge never infers it.

**Spec:**
- Direct observations make a full cause identity: the fault, its account or home, and its exact file or object. A derived symptom carries an explicit reference to the direct cause that made it. The merge drops a derived symptom only when its referenced cause is present; an unlinked symptom stays visible. No fallback to kind, account or text.
- Usage failures (`_bind_usage_cause`, `_read_failure_problem` in `scripts/accounts/accounts.py`): carry a source only when the usage producer knows it. A generic `ERROR` (for example HTTP 503: `claude_live`'s `urllib.error.HTTPError` branch, `scripts/whoami/agent_accounts.py:836`, maps every code but 401 and 429 to `ERROR`) and independent 401, 429 and stale-token failures stay their own lines. A home's startup failure and a shared login's service refusal stay separate.
- Role availability (`_bind_role_cause`): "units is on claude2, which is not set up" binds to claude2's account-state cause. One line names the cause and lists the affected roles. Unmapped roles and an unavailable role map stay independent. Update `scripts/accounts/test_role_map.py:641`, which expects separate role lines (Assertion-Change).
- A malformed or unreadable `.claude.json` gives no "mcpServers differ" line: `_mcp_server_names` keeps an unreadable result, and only two read configurations are compared.
- Producer pairings, each proved by a test through the real producers, binding, merge and both renderers (terminal and `--json`): directory/identity checks to account state (malformed JSON, invalid `oauthAccount`, linked identity, inaccessible folder, ordinary logout); account note to unnoted-home advice to role note error; role selection to account availability (two roles on one missing account; roles on different accounts); Codex startup failure to usage unavailable (with a good reading elsewhere, and with a service refusal); test sign-in suppression (one test login in two homes, role note error, independent linked file); unnoted `~/.codex` and the legacy-home hint; duplicate login across inventory rows (both producers, reversed order, three homes). Each test asserts the full problem set. Tests do not build cause keys by hand to prove a pairing.
- The mcpServers check compares each account with the settings Claude really reads for `~/.claude`, not a stale `~/.claude.json`. User, 2026-10-29: `~/.claude.json` lists `brp`, but `claude mcp list` in `~/.claude` shows no `brp`, so the board's four `mcpServers: - brp` lines were false alarms. Find the file Claude reads and test the comparison against it.
- A failed status command loses no diagnostic (routed from Phase 94c; design review /tmp/claude/astra/20261030T012157Z-1453375/review.md). Phase 94c keeps raw held stderr through a read or render failure, but not the typed problems: a usage final-save failure followed by a render failure, or by a `group_accounts()` read failure, is lost. Rule: the command owns every observed diagnostic until output succeeds. Extend `StatusCommandWarnings` into `StatusDiagnostics`, owned before reading starts; register each typed problem and raw diagnostic when it is observed, before further work that can fail. Renderers take a snapshot. On failure: restore stderr; print the retained typed problems in collection order with the single-problem formatter; print the remaining raw diagnostics in capture order; print the final error last. Drop a raw counterpart only when its information was printed; keep unmatched raw text; do not print twice what a terminal already got; a later good save is not reported as unsaved. Claim and save warnings keep `warning.reason` (`usage_store_warning_line`, `scripts/whoami/agent_accounts.py:368`, drops it today). Tests: each diagnostic (cached map, stored map, usage claim failure, unrecovered per-read save failure, final-save failure) with a later read failure (injected after the diagnostic, before `_read_status` returns) and with a render failure, table and JSON. Assert empty stdout, the full subject, cause and time on stderr, and the final error last. Add mixed typed and raw warnings, renderer warnings, a recovered save, terminal delivery, and two commands in sequence (no repeat, no leak). Include the reported Codex case (good reading, `PermissionError` on the final save, render failure).
- Keep each line's facts, times and commands; no other behavior changes.

**Files:**
- `scripts/accounts/accounts.py` — the source reference, the merge, the usage and role bindings, `_mcp_server_names`, `StatusDiagnostics`.
- `scripts/whoami/agent_accounts.py` — claim and save warnings keep their reason.
- `scripts/accounts/role_map.py` — read: `observed_map_provenance`, `cached_map_warning` (the raw counterpart of the cached-map problem); change only if its delivery needs a mark.
- `scripts/accounts/test_accounts.py`, `scripts/accounts/test_role_map.py` — the pairing tests.
- `scripts/whoami/test_agent_accounts.py` — the claim and finish store-error tests expect `warning.reason` (repair round 1).

**Assertion-Change** (accepted at review pass 1; each follows the Spec):
- `scripts/whoami/test_agent_accounts.py::SharedUsageReadTests::test_a_claim_store_error_still_returns_the_live_read` and `::test_a_finish_store_error_keeps_the_good_live_report` — the expected warning text gains its `warning.reason`.
- `scripts/accounts/test_accounts.py::AccountsTests::test_exec_refuses_wrong_login_and_status_keeps_the_actual_identity` — the line form drops the `* ` prefix (94c line form).
- `scripts/accounts/test_accounts.py` `assert_mcp_status_streams` (`test_piped_status_keeps_mcp_problem_in_table_and_json`, `test_piped_status_suppresses_nested_account_notes`, `test_terminal_status_keeps_nested_account_notes`), `test_status_detail_shows_unclassified_entries_and_mcp_difference`, `StatusProblemCauseTests::test_independent_configuration_problems_each_keep_their_repair` — the MCP baseline is the file Claude reads for `~/.claude`, and the step names it.
- `scripts/accounts/test_accounts.py::AccountsTests::test_status_explains_empty_account_rows_in_the_table_and_json_without_login_addresses` — `check()` no longer carries the state source, so the test clears readiness problems instead; its assertions stay.
- `scripts/accounts/test_accounts.py::PickerTests::test_unnoted_claude_home_is_displayed_but_never_selected_or_a_destination` and `::test_legacy_group_for_an_unnoted_home_is_display_only_and_cannot_launch` — the unnoted-home cause names its note path and the setup step.
- `scripts/accounts/test_accounts.py::StatusProblemCauseTests::test_reserved_login_reports_the_test_file_and_write_time_without_account_steps` and `::test_unnoted_legacy_login_merges_read_failure_and_keeps_both_repairs` — an independent usage failure stays its own line.
- `scripts/accounts/test_role_map.py::RoleMapTests::test_status_refuses_an_unavailable_remote_map_unless_stored` and `::test_stored_status_uses_cached_group_assignments_and_workers_instead_of_local_selections` — role availability binds to the account-state cause; observed account problems print before the final error.
- Rejected: `StatusProblemCauseTests::test_duplicate_identity_keeps_independent_rate_limit_and_its_times` must still assert which accounts the one duplicate-login line names.
- `scripts/accounts/test_accounts.py::StatusProblemCauseTests::test_account_state_merge_uses_the_cause_after_wording_changes` — its summary input carries an explicit source observed by readiness, since an unlinked summary no longer merges (repair round 1).
- Rejected: `test_test_signin_with_malformed_or_unreadable_note_has_only_delete_advice` keeps a check that the real producer's diagnostic is an `UnreadableAccountNote`.

**Workers:** 1 writer, no split.
- `impl` — all files.

**Constraints from prior phases:** Phase 94c's line form and test-login rules stay. Tests use a scratch `HOME`, `AGENT_NOTES_DIR` and Codex homes; never a live home, `~/.local/state`, usage stores, ssh or real sessions. Never run `accounts.py status` against the live home.
- What 94c built (`scripts/accounts/accounts.py`): `StatusProblem(subject, wrong, cause: ProblemCause | NoSeparateCause, step: ProblemStep | NoUserStep, footnote, origin, state_source)`; `ProblemCause.key: ProblemCauseKey | OwnProblemCause`; `ProblemCauseKey(kind, account, location)`; `render_status_problem` is the one formatter; `merge_status_problems` (line 4488) drops a problem whose `state_source: ObservedStateCause` key is a direct cause, then keeps one per key and dedupes equal lines. `_bind_usage_cause` (4531) and `_bind_role_cause` (4548) still pick the cause by kind lists; this phase replaces them with source references. `StatusCommandWarnings` (4669) and `_status_command_warnings` hold raw stderr only; `read_status` (4685) redirects non-tty stderr and drops lines in `StatusReading.represented_stderr`; `main`'s status branch replays `read_stderr` on failure.
- `check()` returns `AccountInspectionProblems`, a `list[str]` subclass that carries `state_source`, and `account_statuses` reads it by `isinstance`. Replace this side channel with the source reference: `account_statuses` takes the readiness value directly and `check()` returns a `list[str]` again (Type Design Contract).
- brp moved to local (project) scope on 2026-10-28 in every natedev account home; the Mac was not changed (memory `brp-mcp-local-scope`). Top-level `mcpServers` is then not the comparison Claude makes.
- Merge timing: no checkpoint merge during the natedev build hold, 00:05–03:05 EDT (showrunner, 2026-10-29). While any natedev build hold is in force, run no tests, gates or type checks unless this unit holds it; check `build_hold.py status` first.

**Acceptance gate:** `python3 -m unittest discover -s scripts/accounts -p 'test_*.py'` passes with a scratch HOME set before import (820 tests, about 2 minutes); `basedpyright` on the changed Python files reads `0 errors, 0 warnings, 0 notes`.

**Gotchas:**
- Attempts log `attempts-account-problem-merge` (memory): every round that set one key form for all producers failed; derived symptoms must carry their source.

### Phase 96 — A stopped account setup, group move or wait is finished by its rerun  · status: todo

**Depends on:** Phase 94e (order only; it edits `scripts/accounts/accounts.py` too)

#### Work Order

**Goal:** Three `scripts/accounts` commands that stop at one line leave state that a rerun does not repair today; after this phase each rerun finishes the work. Rows from the rerun sweep (`~/.claude/docs/plans/sdlc-rerun-sweep.md`, accounts-unit; line numbers there are at sdlc `097b1a0d`, here at `5ad6542f`).

**Spec:**
- **Seed file (`accounts.py:843` in the sweep).** `_seed_config(destination)` (`scripts/accounts/accounts.py:858`) opens `<dir>/.claude.json` with `O_CREAT | O_EXCL` (line 869), then writes the JSON. A stop between the two leaves an empty or partial file, and a setup rerun returns at `if destination.exists(): return []` (line 863), so the seed never happens. Change: write the seed to a private temporary file (mode 0600) in the same folder, flush and `fsync` it, then publish it with `os.link(temporary, destination)` and remove the temporary file. A `FileExistsError` from the link keeps the existing file, as today. A rerun removes a leftover temporary seed file of this name form before it seeds. An existing `.claude.json` of zero bytes is an interrupted seed: the rerun seeds it again. Any other existing file stays untouched (it can hold a login).
- **Group move history (`role_map.py:531` in the sweep).** `role_map.write_role_map` (`scripts/accounts/role_map.py:522`) writes the group selection (`_atomic_write`, line 564), then appends the `roles.jsonl` row (line 569). A stop between them leaves a selection with `cause` `auto` and `why` `auto: <entry id>` but no history row. `role_map.recorded_auto_moves` (line 657) reads only `roles.jsonl`, so `refusal_watch._run_auto_moves_locked` (`scripts/accounts/refusal_watch.py:1202`) does not consume the entry; the head then reaches `if selection.account == rule.to:` (line 1256) and is recorded blocked as `TargetIsCurrentAccount`, and the queue stops. Change: one `role_map` function returns the entry ids that the current stored group selections record (`cause == "auto"`, `why == "auto: <entry id>"`, label equal to the entry's target) and appends each missing `roles.jsonl` row once, with the fields `write_role_map` writes, under `_groups_lock()` and `worker_names.roles_lock()`. `_run_auto_moves_locked` adds those ids to `consumed`.
- **Five-hour wait (`refusal_watch.py:949`).** The refusal pass appends a `WaitedMove` journal row (`moves.append_move`, line 949), then sets the `waited` transition (`_set_transition`, line 953); the watch state is saved later in the pass. A stop between them leaves the row and no state. On rerun the `not any(... entry["kind"] == "waited" and entry["reset"] == wait_reset.at.isoformat() ...)` check skips both, so `waited` is never set and after the reset the continuation path (line 923, `previous["state"] == "waited"`) is not taken: an ungrouped session can move instead of continuing. Change: when the matching `waited` row exists and the session's state is not `waited` for this refusal entry, set the transition with that row's `move_id`. Append no second row.
- No other behavior changes.

**Files:**
- `scripts/accounts/accounts.py` — `_seed_config`.
- `scripts/accounts/role_map.py` — the selection-recorded entry ids and the missing history row.
- `scripts/accounts/refusal_watch.py` — `_run_auto_moves_locked` consumes them; the `waited` transition from an existing row.
- `scripts/accounts/test_accounts.py`, `scripts/accounts/test_role_map.py`, `scripts/accounts/test_refusal_watch.py` — one stop-and-rerun test per row.

**Workers:** 1 writer + 1 tester.
- `impl` — `accounts.py`, `role_map.py`, `refusal_watch.py`.
- `test` — the three stop-and-rerun tests from the Spec alone, in the three test files; each fails before the change.

**State:** `<dir>/.claude.json` and its temporary seed file; `config/groups.json` selections and `~/.local/state/accounts/roles.jsonl` rows (scratch copies in tests); the moves journal and the refusal watch state.

**Constraints from prior phases:**
- Tests use a scratch `HOME`, `AGENT_NOTES_DIR` and state dirs set before import; they never touch a live home, `~/.local/state`, usage stores, ssh, herdr or real sessions.
- Stop model for every test: a kill runs no handler. Patch the call right after the named line to raise a test-local `class Killed(BaseException)`, so no `except Exception` or `except OSError` block runs; catch it in the test; then run the same command again with no patch and assert the state is repaired (seed: the file parses as the filtered seed, mode 0600, no temporary file left; history: the entry is consumed, the queue head moves on, one `roles.jsonl` row; wait: state `waited` with the row's `move_id`, one journal row, and after the reset the pass prints `continued on its current account` and moves nothing).
- Phase 94c: `check()` returns `AccountInspectionProblems` and `_status_for` takes a `state_source`; this phase does not change them.
- `role_map.write_role_map` already rolls back the selection on an `OSError` from the append; that path stays.
- Merge timing: no checkpoint merge during the natedev build hold, 00:05–03:05 EDT (showrunner, 2026-10-29). While any natedev build hold is in force, run no tests, gates or type checks unless this unit holds it; check `build_hold.py status` first.

**Acceptance gate:** `python3 -m unittest discover -s scripts/accounts -p 'test_*.py'` passes with a scratch HOME set before import; `basedpyright` on the changed Python files reads `0 errors, 0 warnings, 0 notes`; each of the three stop-and-rerun tests fails on the code before the change.

### Phase 97 — A stopped Codex fallback switch is finished by its rerun  · status: done

**Depends on:** Phase 95 (order only)

#### As-built

- `PendingClaudeSwitch` (`scripts/whoami/quota_alert.py`) is saved in state before `switch_to_claude` edits the registry. A later switching pass that finds it completes the switch from the registry, writes the routing row once and tells the owner once.
- Switch-back keeps a fallback that holds only the keys that failed to restore; a later pass retries them, and a newer switch keeps the keys still owed.
- `RestorationOutcomeOwed`: the restore journal row and notice are saved as an owed outcome before the fallback is cleared, so a stop or a failed append is finished by the rerun.
- A refusal reads the registry before it treats work as already on Claude. A workers queue entry is consumed only when the workers role names its target or a later row of its move exists; the role is read once per pass. `state_file()` only reads and saves state.
- Tests: `scripts/whoami/test_quota_alert.py` (stop-and-rerun cases on `CodexRerunFixture` in `quota_alert_test_support.py`); `test_codex_alert_accounts.py::test_failed_restoration_is_attempted_and_reported_once` follows the retry (Assertion-Change); `test_quota_zero_episode.py` isolates state per subtest.

### Phase 98 — A notice marked before it is sent is retried until it is sent  · status: done

**Depends on:** Phase 95 (order only)

#### Work Order

**Goal:** Four markers are saved before the notice they stand for is sent, so a stop or a failed send leaves a marker that stops every retry. After this phase a marker means "sent", and a rerun sends what is owed. Rows from the rerun sweep (`~/.claude/docs/plans/sdlc-rerun-sweep.md`, two-accounts-unit; both files are the same at sdlc `097b1a0d` and `5ad6542f`).

**Spec:**
- **Five-hour warning (`five_hour.py:92`).** `watch` (`scripts/whoami/five_hour.py:47`) creates the marker `STATE/<key>.notice` with `open("x")` (line 92), then relays the Claude warning to each recipient (`relay`, line 105). A stop, or a relay that returns a problem, leaves the marker; later passes skip it at lines 94–95 until usage recovers. Change: the marker becomes sent only after every relay succeeds. While the relays run it is a claim, and a claim that its pass did not finish (stopped, or a relay failed) is retried by the next pass. Two concurrent passes still send at most once: the claim has an owner check or an expiry (rerun lens question 5). The Codex branch (no relay) keeps its marker as today.
- **Cancel outgoing warnings (`five_hour.py:44`).** `cancel_outgoing_notices` (line 34) runs `send.py retire` per recipient and ignores the result, then deletes the notice file (line 44). A failed retire loses the only record of what to cancel. Change: delete the file only when every retire exits 0; keep it otherwise, so the next pass retries.
- **Quiet alert (`codex_winddown.py:233`).** `announce_quiet` (`scripts/production/codex_winddown.py:226`) creates `STATE/QUIET` (line 233), then sends the reset alert. A failed send already removes the marker (lines 248–253); a stop between the touch and the send leaves it, and later counts return at lines 234–235. Change: the same claim-then-sent rule as the five-hour warning.
- **At-zero notice (`codex_winddown.py:217`).** `refresh` (line 209) deletes a finished unit's projection (line 217) and saves it before it sends `AT_ZERO` (line 222); `broadcast.send` returns `""` when sent, else its error, and the result is ignored. Change: a finished unit's projection becomes a named owed state (for example `ZeroNoticeOwed`) that stays saved until its send returns `""`; a later count retries it.
- No other behavior changes.

**Files:**
- `scripts/whoami/five_hour.py` — the claim and sent marker; the kept cancel file.
- `scripts/production/codex_winddown.py` — the quiet claim; the owed at-zero state.
- `scripts/whoami/test_five_hour.py`, `scripts/production/test_codex_winddown.py` — one stop-and-rerun test per row, and one failed-send test per send.

**Assertion-Change** (accepted at review pass 1): `scripts/whoami/test_five_hour.py::WatchTests::test_a_low_window_tells_the_alert_list_once_without_a_phone_hold`, renamed `test_a_low_window_retries_failed_relays_without_a_phone_hold` — after a failed relay no sent marker exists and the next pass relays again; the retry relays only to the recipient that failed, so the send count is 3, not 4; the sent marker and the empty later pass are asserted after the successful retry. `WatchTests::test_no_reading_changes_nothing` — the relay succeeds, so the unchanged marker assertion is about a sent warning.

**Repair round 1:** a retry relays only to the recipients that did not get the warning; a recipient that already got it gets no second copy.

**Workers:** 2 writers, split by file (no file in both).
- `impl` — `scripts/whoami/five_hour.py`, `scripts/whoami/test_five_hour.py`.
- `test` opens as impl — `scripts/production/codex_winddown.py`, `scripts/production/test_codex_winddown.py`.

**State:** `.notice` markers and the `QUIET` marker under each script's `STATE` folder; the wind-down projections file (scratch folders in tests).

**Constraints from prior phases:**
- Tests use scratch state folders set before import and stub `relay`, `broadcast.send` and the `send.py retire` subprocess; they never send a real message or touch a live home, `~/.local/state`, ssh, herdr or real sessions.
- Stop model: patch the call right after the named line to raise a test-local `class Killed(BaseException)`; catch it; run the same pass again with no patch; assert the notice is sent once and the marker then reads sent. A failed-send test asserts the next pass sends again.
- Merge timing: no checkpoint merge during the natedev build hold, 00:05–03:05 EDT (showrunner, 2026-10-29). While any natedev build hold is in force, run no tests, gates or type checks unless this unit holds it; check `build_hold.py status` first.

**Acceptance gate:** `python3 -m unittest discover -s scripts/whoami -p 'test_*.py'` and the same for `scripts/production` pass; `basedpyright` on the changed Python files reads `0 errors, 0 warnings, 0 notes`; each stop-and-rerun test fails on the code before the change.

### Phase 99 — A stopped `down` or `now` is finished by its rerun  · status: todo

**Depends on:** Phase 95 (order only); also waits until herdr-unit's phase 3 has merged into natedev, because herdr edits `scripts/shutdown/settle.py` now. The showrunner says when.

**Also touches:** shutdown-unit (`scripts/shutdown`).

#### Work Order

**Goal:** Five `scripts/shutdown/settle.py` paths that stop at one line leave shutdown records that a rerun does not repair today; after this phase a rerun of `down`, `now` or the outcome report finishes the work. Rows from the rerun sweep (`~/.claude/docs/plans/sdlc-rerun-sweep.md`, accounts-unit) plus one showrunner row (line 819). Line numbers are at `5ad6542f`; herdr-unit's phase 3 moves them, so find each by its function and code text.

**Spec:**
- **Account hold registration (`settle.py:786`).** `begin` registers the account hold request (`store.register_account_hold_request`, line 786) and completes it at the end (`store.complete_account_hold_registration(login, request_id)`, line 848). A stop between them leaves a pending request id. A `down` rerun makes a new request id and completes only that one, so `release_finished_account_hold` keeps blocking launches after the work ends. Change: a rerun of `begin` for the same login completes (or withdraws) every pending registration whose owner is gone — its request has no live session record, or its registering process is not running (pid and process start time). Name the owner check in the code; never trust the stored id alone.
- **Record before the conductor (`settle.py:819`, showrunner row).** `begin` writes each session's record (`store.create_session(saved)`, line 819). If `down` stops after a record is written and before the conductor starts, a rerun prints `already in progress` for that record and skips it, and no conductor ever runs it. Change: a rerun treats a record of this login whose conductor is not running (no live conductor process for it) as its own and starts the conductor for it.
- **Timer stop before the record update (`settle.py:833`).** `save_timers` (line 830) stops the notifier timers inside `store.update_session` (line 836) before the changed record is written. A stop there leaves the record with `timers=[]` and the timers stopped; `begin` then skips the record as in progress, so cancel or restart cannot restore them. Change: write the timer snapshot to the record before any timer is stopped, and stop the timers only after that write; a rerun stops any timer in the snapshot that still runs.
- **Stored stopped status (`settle.py:1217`).** `now` (line 1201) skips a record whose `progress.kind` is not waiting, ready, held or reaping (line 1217), and `down` skips an existing record at line 819, without checking the live session. A session resumed outside shutdown after a successful stop stays running. Change: before skipping a stopped record, check the live session (pid and process start time); a live session with a finished stop record gets a new stop, and its old record is closed.
- **Showrunner line marked before it is sent (`settle.py:1568`).** `_reserve_showrunner_line` (line 1562) saves `showrunner_told` (line 1568) before `tell_showrunner_stopping` sends it (`send_message`, line 1590). A stop, or a failed send, leaves the mark, and a repeated stop or outcome report skips the message. Change: the mark is a claim until the send succeeds, then sent; a claim whose send did not succeed is sent again by the next stop or outcome report, at most once per line.
- No other behavior changes.

**Files:**
- `scripts/shutdown/settle.py` — `begin`, `save_timers`, `now`, `_reserve_showrunner_line`, `tell_showrunner_stopping`, `tell_showrunners_outcome`.
- `scripts/shutdown/shutdown_record.py` — the `store` module of settle; changed only if a pending-registration or claim reader belongs there.
- `scripts/shutdown/test_settle.py` — one stop-and-rerun test per row.

**Workers:** 1 writer + 1 tester.
- `impl` — `settle.py` (and `record.py` if needed).
- `test` — the five tests from the Spec alone in `test_settle.py`; each fails before the change.

**State:** account hold registrations, `SessionShutdown` records (`timers`, `progress`, `showrunner_told`) and notifier timers (scratch shutdown state in tests).

**Constraints from prior phases:**
- Tests use a scratch `HOME` and `SHUTDOWN_STATE_DIR` set before import, and stub inventory, timers, herdr and `send_message`; they never touch live sessions, timers, `~/.local/state`, ssh or herdr.
- Stop model: patch the call right after the named line to raise a test-local `class Killed(BaseException)`; catch it; run the same verb again with no patch; assert the state is repaired (no pending registration blocks launches; the conductor runs the record; the timers are restored on cancel; the resumed session is stopped; the showrunner line is sent once).
- Phase 95: `settle.failed_prompt_screen_lines` and `report_stop` show the saved screen; `scripts/accounts/test_unit_watchdog.py` calls `settle.report_stop`, so the gate runs the accounts suite too.
- Merge origin/natedev (with herdr-unit's phase 3) before the work starts and before the gate.
- Merge timing: no checkpoint merge during the natedev build hold, 00:05–03:05 EDT (showrunner, 2026-10-29). While any natedev build hold is in force, run no tests, gates or type checks unless this unit holds it; check `build_hold.py status` first.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` and the same for `scripts/accounts` pass with a scratch HOME set before import; `basedpyright` on the changed Python files reads `0 errors, 0 warnings, 0 notes`; each stop-and-rerun test fails on the code before the change.

### Phase 100 — All consumers import shared whoami modules by one name  · status: todo

**Depends on:** Phase 94d, Phase 94e, Phase 96, Phase 97, Phase 98 (order only; each edits files whose imports this phase changes)

**Also touches:** the herdr, shutdown, message, production and agents owners.

#### Work Order

**Goal:** Every consumer imports each shared whoami module by its package name, `scripts.whoami.<module>`, and the two-name registrations go. Today sibling imports (`import note_file`) and package imports (`from scripts.whoami import note_file`) coexist. Six modules register both names in `sys.modules` to keep one module object, so type identity and patched settings stay shared: `agent_accounts`, `account`, `note_file`, `session_table`, `run_out` and `usage_store`.

**Spec:**
- The package name is the one name: it works from every directory with only the repository root on `sys.path`, and the agents, herdr, usage and nix_env consumers already use it.
- Change every bare import of the six modules (`import <module>`, `from <module> import …`, also inside functions) to the package form. Bare-import consumers found at planning:
  - `scripts/whoami`: `five_hour.py`, `quota_alert.py`, `codex_pacer.py`, `agent_notes.py`, `run_out.py`, `session_table.py`, `whoami.py`, `profile_notes_pass.py` (added by Phase 94b; also string imports `import_module("<module>")` of three of the six).
  - `scripts/accounts`: `accounts.py`, `new_account.py`, `refusal_watch.py`, `refusals.py`, `unit_watchdog.py`.
  - `scripts/shutdown`: `inventory.py`, `pane_record.py`, `pane_restore.py`, `record.py`, `restart.py`, `settle.py`, `shutdown.py`.
  - `scripts/production`: `cli_fixture.py`, `dailies_render.py`, `showrunner_move.py`.
  - `scripts/message/roster.py`; `scripts/nightly_review/nightly_review.py` (not in the next-items entry; found at planning).
  - The test files and test support files listed under Files, in `scripts/whoami`, `scripts/accounts`, `scripts/shutdown`, `scripts/production` and `scripts/nightly_review` (28 test files and 2 support files at `5ad6542f`).
  - `scripts/agents/agent_exec.sh`: its inline Python puts only `scripts/whoami` on `sys.path` and runs `from agent_accounts import workers_home`. It puts the repository root (`$script_dir/../..`) on `sys.path` and imports `scripts.whoami.agent_accounts`.
- The repository root must be on `sys.path` before the first `scripts.` import of each consumer. Today many consumers get the root only as a side effect of a bare `import account` or `import agent_accounts` (`account.py:19` and `agent_accounts.py:46` insert `parents[2]`); `settle.py:42` and `restart.py:28` already import `scripts.accounts` through that side effect. After the change the package import must find the root first. Each consumer file that has no module-level root insert adds `sys.path.insert(0, str(Path(__file__).resolve().parents[2]))` before its first `scripts.` import. Found at planning: `scripts/whoami/whoami.py` (its root insert is inside a function, line 69), `run_out.py`, `session_table.py`; `scripts/accounts/refusals.py`, `unit_watchdog.py`; `scripts/production/dailies_render.py`, `cli_fixture.py`; `scripts/shutdown/inventory.py`, `pane_record.py`, `pane_restore.py`, `record.py`, `restart.py`, `settle.py`, `shutdown.py`.
- Remove the `if __name__ == "<module>": … elif __name__ == "scripts.whoami.<module>": …` block from each of the six modules.
- Other whoami modules (`quota_alert`, `five_hour`, `codex_pacer`, `agent_notes`) keep their current import names; `scripts/lib/nix_env.py` keeps its own two-name block. Both are out of scope.
- No other behavior changes.

**Files:**
- `scripts/whoami/agent_accounts.py`, `scripts/whoami/account.py`, `scripts/whoami/note_file.py`, `scripts/whoami/session_table.py`, `scripts/whoami/run_out.py`, `scripts/whoami/usage_store.py` — the registration blocks.
- `scripts/whoami/five_hour.py`, `scripts/whoami/quota_alert.py`, `scripts/whoami/codex_pacer.py`, `scripts/whoami/agent_notes.py`, `scripts/whoami/whoami.py` — consumers.
- `scripts/accounts/accounts.py`, `scripts/accounts/new_account.py`, `scripts/accounts/refusal_watch.py`, `scripts/accounts/refusals.py`, `scripts/accounts/unit_watchdog.py` — consumers.
- `scripts/shutdown/inventory.py`, `scripts/shutdown/pane_record.py`, `scripts/shutdown/pane_restore.py`, `scripts/shutdown/shutdown_record.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/shutdown.py` — consumers.
- `scripts/production/cli_fixture.py`, `scripts/production/dailies_render.py`, `scripts/production/showrunner_move.py` — consumers.
- `scripts/message/roster.py`, `scripts/nightly_review/nightly_review.py` — consumers.
- `scripts/whoami/test_agent_notes.py`, `scripts/whoami/test_codex_alert_accounts.py`, `scripts/whoami/test_codex_homes.py`, `scripts/whoami/test_codex_pacer.py`, `scripts/whoami/test_five_hour.py`, `scripts/whoami/test_note_file.py`, `scripts/whoami/test_quota_alert.py`, `scripts/whoami/test_run_out.py`, `scripts/whoami/test_session_table.py`, `scripts/whoami/test_usage_store.py`, `scripts/whoami/test_quota_zero_episode.py` — tests.
- `scripts/whoami/quota_alert_test_support.py` — test support: bare imports of `agent_accounts`, `session_table`, `note_file`, `account` and `usage_store` (lines 27–34) and `import usage_store` inside a function (line 275).
- `scripts/accounts/test_accounts.py`, `scripts/accounts/test_cutover_home.py`, `scripts/accounts/test_new_account.py`, `scripts/accounts/test_refusals.py`, `scripts/accounts/test_refusal_watch.py`, `scripts/accounts/test_unit_watchdog.py` — tests.
- `scripts/shutdown/test_account_move.py`, `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_pane_record.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_shutdown_status.py`, `scripts/shutdown/test_stop.py` — tests.
- `scripts/production/test_add_showrunner.py`, `scripts/production/add_unit_test_support.py`, `scripts/nightly_review/test_nightly_review.py` — tests; the support file has `import usage_store` at line 39.
- `scripts/whoami/profile_notes_pass.py` — consumer: `import usage_store` and `from agent_accounts import …` inside a function, and `import_module("usage_store")`, `import_module("run_out")`, `import_module("agent_accounts")` in `load_pass` and `patch_list`.
- `scripts/agents/agent_exec.sh` — the inline Python import.
- `scripts/agents/codex_mesh.py`, `scripts/herdr/herdr_sessions.py` — already package form; read to confirm, change only a bare import found.

**Workers:** 2 writers, split by directory (no file in both).
- `whoami` — `scripts/whoami` modules and tests, the six registration blocks.
- `consumers` — `scripts/accounts`, `scripts/shutdown`, `scripts/production`, `scripts/message`, `scripts/nightly_review`, `scripts/agents`, `scripts/herdr`, and their tests.

**Constraints from prior phases:** Tests sit beside the code (Delegation Context); after this phase they import the six modules by the package name, the same object the code uses. Tests never touch live dirs: `HOME`, `AGENT_NOTES_DIR` and the patched `note_file.AGENTS_DIR` point at scratch folders. No `Any`, no file-level ignores.
- Phase 94c: `scripts/accounts/accounts.py` now also calls `agent_accounts.usage_store_warning_line` and reads `agent_accounts.UsageClaimFailed`, `UsageSaveFailed` and `Report.store_warnings`; type identity of these classes depends on the one module object. `scripts/accounts/role_map.py` has no bare import of the six modules.
- Phases 96–99 edit `accounts.py`, `role_map.py`, `refusal_watch.py`, `quota_alert.py`, `five_hour.py` and `settle.py` bodies. This phase does not wait for the `settle.py` phase (it waits on herdr-unit): both edit `settle.py` in different regions (imports here; `begin`, `now` and `_reserve_showrunner_line` there). Whichever runs second merges origin/natedev first. herdr-unit also edits `settle.py` now; merge origin/natedev before the work starts and before the gate.
- The `grep` in the acceptance gate found these bare-import files at `5ad6542f`; rerun it at the start to catch files added since.
- Merge timing: no checkpoint merge during the natedev build hold, 00:05–03:05 EDT (showrunner, 2026-10-29). While any natedev build hold is in force, run no tests, gates or type checks unless this unit holds it; check `build_hold.py status` first.

**Acceptance gate:** `python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'` passes for `whoami`, `accounts`, `shutdown`, `production`, `message`, `nightly_review`, `agents`, `herdr`, `usage`, `nix_env` and `lib`; `basedpyright` on every changed file and each caller module reads `0 errors, 0 warnings, 0 notes`; `grep -rnE '^\s*(import|from) (agent_accounts|account|note_file|session_table|run_out|usage_store)\b' scripts` finds nothing (it covers the `.sh` inline Python too); `grep -rnE 'import_module\("(agent_accounts|account|note_file|session_table|run_out|usage_store)"\)' scripts` finds nothing; no `sys.modules.setdefault` remains in `scripts/whoami`; with `/tmp` as the working directory and only the file's own directory on `sys.path` (as a script run has it), `python3 -c 'import sys; sys.path.insert(0, "<dir>"); import <module>'` imports each changed non-test consumer without `ModuleNotFoundError` (import only; never call its `main`).

**Gotchas:**
- A test that imports a module by the other name gets a second module object. Its `patch.object(note_file, "AGENTS_DIR", …)` then misses the object the code uses, and the code can write to the live notes dir. Change each test's import in the same commit as its module's consumers.
- Type identity: a `KnownCodexAccount` or `Report` made from one module object fails `isinstance` against the other. A remaining bare import shows as a failing `isinstance` test, not an import error.
- The unit suites run from the repository root, so the working directory puts the root on `sys.path` and hides a missing root insert. Only the import check from `/tmp` in the acceptance gate finds one.
- `scripts/whoami/profile_notes_pass.py` patches the pass's write and act calls to no-ops on the module objects it loads. If it keeps a bare name after the registration blocks go, its patches attach to a second module object, and a director's profile run can write the live usage store and run_out log or start a live Codex read. `test_profile_notes_pass.py` `test_argument_free_pass_keeps_all_input_files_unchanged` (zero `usage_store.save_timer_usage` calls, read through the package import) is the guard.

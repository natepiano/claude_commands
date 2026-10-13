# herdr trial: a fake production running in herdr on natedev

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A throwaway production (1 showrunner, 3 unit directors, 2 workers per unit) runs inside one herdr session on natedev, visible in a Konsole window on the KDE desktop, doing tiny fake work in a scratch repo. Every problem it hits is logged; then everything is removed and the findings are written up.

> **Approved** by the showrunner 2026-10-13 10:4x EDT with two changes, folded in below: workers follow the agents.conf defaults (Codex today, each in its own interactive pane), and the launcher never answers a trust or permission prompt.

> **As-built disposition: create** — a new `docs/as-built/herdr-trial.md`

> **Production: build-followups** — unit `herdr-trial-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-13 10:33 EDT

make a fake herdr production with 1 showrunner 3 units and each unit with 2 workers - using our current defaults - assign this work to unit director to implement so we can learn from the issues we see and i can see it visibly running here on natedev

Showrunner's brief and approval, 2026-10-13 10:3x–10:4x EDT (not the user's words): research is `~/.local/state/plan-backlog/2026-10-13-herdr.md`; install with `nix shell` from the locked nixpkgs-unstable input, no rebuild; workers in panes so each has a status row; scratch repo with a local bare origin, never touch `~/.claude` main or a real production; stop short of promote and record any refusal rather than working around it; open a herdr client window on the KDE desktop (Konsole), no kwin restart or display change; the Claude sessions on claude 1; tiny fake work; stop everything once the issues are logged and remove every scratch session, notifier instance and folder. At approval: workers take the seat families and models agents.conf assigns today, a Codex worker as its own interactive codex pane on the default codex account, with how that differs from shared app-server threads logged; an account that runs out is recorded and the worker falls back; trust and permission prompts are the user's, so every session starts in an already trusted folder, and a folder that needs trusting goes to the showrunner before Phase 3.

## Calls I made

- **Each unit's two workers are one implementer and one reviewer**, resolved at launch from agents.conf: `delegate=codex` today, so `impl=gpt-6.1-sol:high` and `review=gpt-6.1-sol:high` (the live `~/.claude/config/agents.conf`; this worktree's copy is older), each an interactive `codex` in its own pane. **The default codex account is the seats role's account** (`accounts.py` role `seats`: codex 1, `~/.codex1`, today), since that is the account seats run on. Risk: if the showrunner meant `~/.codex` (codex 2), it is a one-line change in the launcher.
- **Account fallback:** a Codex worker whose account reports its limit is recorded as an issue and relaunched on the `[delegate.claude]` row of its role (`impl=opus:high`, `review=opus:xhigh`) on claude 1.
- **The fake showrunner runs at `opus:high`** (the live `[production.claude] director` row; agents.conf has no showrunner row). Risk: none for the trial.
- **The scratch root is `/home/natepiano/rust/herdr-trial-scratch/`**, because no session may face a trust prompt and none is answered for the user. `/home/natepiano/rust` has `hasTrustDialogAccepted` in `~/.claude1/.claude.json`, and `/home/natepiano` is `trust_level = "trusted"` in the Codex config (`~/.codex1/config.toml` links to `~/.codex/config.toml`). `~/rust` is not a git repository, and the folder does not exist today. Risk: a scratch folder sits beside the real repos for the length of the trial; Phase 5 removes it by exact path. If Phase 1's live check still shows a trust prompt, the launcher stops and the unit director brings that one folder to the showrunner before Phase 3.
- **One herdr session named `herdr-trial`** (`herdr --session herdr-trial`), so nothing joins or disturbs a default herdr session. No herdr Claude integration is installed: it writes into `~/.claude/hooks/` and `settings.json`. Consequence, logged as a finding: no resume after a server restart.
- **Units are launched by this unit's launcher, not by `add_unit.py`**, which starts tmux sessions. The fake showrunner is told so, and each place its commands assume tmux is logged as a finding. That list is the main output of the trial.
- **The fake production's Promote is `none`.** It merges to its own merge branch and pushes to the bare origin; nothing reaches `~/.claude`.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-herdr-trial`, branch `build-followups-herdr-trial` (unit `herdr-trial-unit`); name both in every dispatch. Code lives in `scripts/herdr/` only.
- **Project started:** 2026-10-13T14:36:00Z
- **Stack:** Python 3 standard library, checked by basedpyright; herdr 0.9.0; Claude Code CLI; Codex CLI; zsh.
- **herdr binary:** `/nix/store/pyqm5522kvk30b9vgb1ab976pjnjwn51-herdr-0.9.0/bin/herdr`, from the locked `nixpkgs-unstable` input (rev `8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe`): `nix shell github:NixOS/nixpkgs/8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe#herdr`. Already in the store; nothing builds. The trial script finds it through `HERDR_BIN`, else that store path.
- **herdr commands** (research report §2, checked on natedev at 0.9.0): `herdr server` (headless), `herdr server stop`, `herdr workspace create --cwd <dir> --label <name> --env VAR=... --no-focus` (JSON: `.result.workspace`, `.result.tab`, `.result.root_pane.pane_id`; the label goes on the workspace only), `herdr tab ...`, `herdr pane list|get|read <pane> --source visible [--format ansi]|send-text|send-keys <pane> enter|run <pane> "<cmd>"|close|process-info`, `herdr agent list|prompt <pane> "<text>"|wait <pane> --until idle|blocked`, `herdr session list --json`. Every command takes `--session herdr-trial` (global option). Panes are addressed by id (`w3:p1`), never by label: look ids up by label in `pane list` JSON. `agent prompt` refuses with `agent_blocked` while a dialog shows.
- **Key files:**
  - `scripts/production/add_unit.py` — `prompt_for` 665 (the unit director's first prompt, three forms), `launch_session` 701 (argv: `claude --model <m> --effort <e> --remote-control <name> -n <name> --settings '{"disableAgentView": true}' <prompt>`, prefixed by `account_prefix(selected)` and `ENABLE_TOOL_SEARCH=true`; run as `systemd-run --user --scope` around `tmux new-session -d -s <name> -c <cwd> -e SHOWRUNNER_UNIT=<slug> -e SHOWRUNNER_UNIT_ID=<unit> zsh -ic "<command>; exec zsh"`), `launch_environment` 739 (drops `CLAUDE_*`). The herdr launcher mirrors this argv and these marks.
  - `scripts/production/unit_lookup.py` — `PRODUCTION_MARK = "SHOWRUNNER_UNIT"` 27, `UNIT_MARK = "SHOWRUNNER_UNIT_ID"` 28; finds units by asking tmux.
  - `scripts/accounts/accounts.py` — `account_prefix`; `role_account` (role `unit` → claude 1 `~/.claude1`; role `seats`, `CODEX_ROLE` 126 → codex 1 `~/.codex1` today).
  - `scripts/agents/agents_config.sh` — `agents_resolve_print production.director`, `delegate.impl`, `delegate.review` print `family=… agent=<model> effort=<effort>`; read them at launch, never hard-code. Call the live copy, `bash -c 'source ~/.claude/scripts/agents/agents_config.sh; agents_resolve_print <task>'` (bash only: sourced into zsh it returns empty rows); it reads `~/.claude/config/agents.conf`, which is newer than this worktree's `config/agents.conf`.
  - `scripts/delegate/implement.sh` — the codex delegate's model, effort, sandbox and approval settings (around 393–489); the interactive Codex workers take the same.
  - `scripts/production/showrunners.py` — `registered_showrunners` 190, `current_name` 230, `address <slug>`; `scripts/production/production_check.sh` (doc status line `> **Status: PRODUCTION — running.**`).
  - `scripts/message/roster.py` 612–661 — finds units through tmux panes (research §2: inside herdr the pane field is empty).
  - Production doc format: `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md` (Production Context bullets, Units table, Production rules).
- **Scratch root:** `/home/natepiano/rust/herdr-trial-scratch/` (trusted through its parents; see Calls I made) — `repo/` (the fake production's checkout), `origin.git` (bare), `worktrees/<unit>/`, `probe/`, `issues.jsonl`, `herdr.log`, `started.json`. Created by Phase 1 (`probe/`, `issues.jsonl`) and Phase 2, removed by Phase 5. Never `rm` by glob; never kill by pattern (memory `never-kill-by-pattern`): stop only pids and units the trial started, recorded in `started.json`.
- **Desktop:** KDE on Wayland (`WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:0` in `systemctl --user show-environment`); `konsole` on PATH. Open the client with `systemd-run --user --unit=herdr-trial-client konsole -e <herdr> --session herdr-trial`. No kwin restart, no display change.
- **Test:** `python3 -m unittest discover -s scripts/herdr -p 'test_*.py'`. Tests never start herdr, Claude or Codex: they run against a fake `HERDR_BIN` script that records its argv and prints canned JSON.
- **Lint:** `basedpyright scripts/herdr` — pass is its `0 errors, 0 warnings` line (it exits 3 on the missing `.venv`).
- **Invariants:**
  - Nothing touches `~/.claude` main, `~/.claude/settings.json`, `~/.claude` hooks, the `~/.claude1` or Codex config files, any real production's doc, worktree, timer or session, or a default herdr session.
  - No `production_lifecycle.py promote-main` or `wrap` on any production. A refusal by auto mode is logged as a finding, never worked around.
  - The 4 Claude sessions run on claude 1 (`~/.claude1`); the 6 workers on the seats Codex account unless they fall back. The launcher never presses a key in a trust or permission prompt: a session that shows one is logged and left for the showrunner to raise with the user.
  - Fake work stays tiny: one phase per unit, a few lines each.
  - Every issue goes to `issues.jsonl` through `trial.py issue` at the moment it is seen, with the time, the session, what was expected and what happened.
  - Python: basedpyright 0/0; no file-level ignores; `TypedDict` for every herdr JSON shape; no `Any`.

## Phases

### Phase 1 — herdr server, client window and pane helpers  · status: done

#### As-built

- `scripts/herdr/trial.py` drives herdr 0.9.0 through a `Herdr` class that runs the binary from `HERDR_BIN` (else the store path) with `--session herdr-trial` on every call.
- Subcommands: `up` (server start, then client open), `server start|stop`, `client open|close`, `open <label> [--cwd] [--command]`, `close <label>`, `pane-id <label>`, `read <label>`, `type <label> <text>`, `agents`, `issue <session> <expected> <happened>`, `trust-check <dir>`. Exit 2 on usage errors, 1 on failures, one-line diagnostic on stderr.
- `server start` launches systemd user unit `herdr-trial-server` and waits up to `HERDR_TRIAL_START_TIMEOUT` (default 10 s) for `status server` to report `status: running`; otherwise exits 1 with `server herdr-trial not answering after N seconds`. A second start while it answers changes nothing. `server stop` runs `herdr server stop`, then stops the unit if still active.
- `client open` launches unit `herdr-trial-client` (`konsole -e herdr --session herdr-trial`); a second open while active changes nothing. Each launched unit is recorded in `<scratch>/started.json` as `{"systemd_units": [...]}`; `up` validates that file before starting anything.
- `open` creates one workspace per label, renames its root pane to the label, and runs the command in it; it refuses a label that already exists. `close` closes the pane only. `pane-id` exits 1 naming the label when zero or several panes match. `read` prints herdr's plain-text output. `agents` prints `label<TAB>agent<TAB>state` per agent.
- `issue` appends `{time, session, expected, happened}` (UTC ISO time) to `<scratch>/issues.jsonl`; scratch is `HERDR_TRIAL_ROOT`, default `/home/natepiano/rust/herdr-trial-scratch`. `trust-check` reads only `$CLAUDE_CONFIG_DIR/.claude.json` (default `~/.claude1`) and `$CODEX_HOME/config.toml` (default `~/.codex1`) and requires the folder or a parent to be trusted in both.

**Files:**
- `scripts/herdr/trial.py` — the trial driver
- `scripts/herdr/test_trial.py` — 33 tests against the fake, covering argv per subcommand, label lookup, issue line shape, trust fixtures, start timeout and `started.json` handling
- `scripts/herdr/fake_herdr.py` — argv recorder used as every external program (herdr, `systemd-run`, `systemctl` via `HERDR_TRIAL_SYSTEMD_RUN` / `HERDR_TRIAL_SYSTEMCTL`)

**Binds later work:** `trial.py` lives in the worktree at `scripts/herdr/trial.py`, not under `~/.claude/scripts`. `open` makes one workspace per label; tabs and splits need `tab create` / `pane split` and labelling by the caller. The server-not-answering exit 1 is the launch gate. `started.json` lists every systemd unit the trial started and is what teardown stops.

**Gotchas:**
- herdr 0.9.0: `workspace create --label` labels only the workspace, not its root pane; closing the last pane removes its tab and workspace; `pane read` and `status server` print plain text, not JSON; `status server` exits 0 when the server is stopped, so the driver matches the `status: running` line.
- herdr creates `~/.config/herdr/` (`.plugins.lock`, `release-notes.json`, `sessions/<name>/`), which did not exist before the trial.
- A Claude pane on claude 1 turns on Remote Control by itself. `/exit` stops Claude; a typed `/quit` does not exit Codex, so a Codex pane is stopped by closing it.
- Interactive Codex on `~/.codex1` starts a managed `codex app-server` daemon under systemd `--user` that outlives its pane.
- Trust comes from folder choice: Claude on claude 1 and Codex on `~/.codex1` show no trust prompt in a trusted folder.

### Phase 2 — The scratch production  · status: done

#### As-built

- `trial.py scaffold` builds the scratch root: `origin.git` (local bare, `main`), `repo/` cloned from it and seeded with `textkit/__init__.py`, `textkit/test_textkit.py` (one passing test), a README, the fake production doc `docs/plans/herdr-fake-production.md` (`Promote: none`; passes `production_check.sh`), `docs/plans/herdr-fake-log.md` and three one-phase unit plans `docs/plans/herdr-fake-{slug,clamp,count}.md`; branch `herdr-fake` is pushed, and worktrees `worktrees/{slug,clamp,count}-unit` sit on branches `herdr-fake-{slug,clamp,count}` from it. It refuses (exit 1) when `repo/`, `origin.git` or any worktree path exists.
- Each unit plan names its workers as panes `<unit>-impl` and `<unit>-review`, reached with `trial.py work` and awaited with `trial.py wait` in place of `implement.sh` and `review.sh`.
- `trial.py work <unit> <role> <file>` resolves the file to an absolute path (exit 1 if missing), removes a stale `<file>.done`, writes `<root>/work/<unit>-<role>.json` (`{"file": <abs path>}`) by temp file and `os.replace`, and sends `agent prompt` once: "Read and carry out <file>. When done, write your report to <file>.done."
- `trial.py wait <unit> <role>` reads that file (exit 1 when absent) and runs under one deadline, `HERDR_TRIAL_WAIT_TIMEOUT` (default 1800000 ms), with the subprocess timeout above it: a settled `agent wait`; when the worker is idle/done with no report, `agent wait --until working`, then settled again. It prints the report path; exit 1 at the deadline without a report; exit 2 on `blocked` or `agent_blocked`, with an `issues.jsonl` line. It never resubmits the prompt.

**Files:**
- `scripts/herdr/trial.py` — the trial driver, now with `scaffold`, `work`, `wait`
- `scripts/herdr/scaffold_files.py` — `UNITS` and the texts `scaffold` writes: textkit package, production doc, log, unit plans
- `scripts/herdr/test_trial.py` — 50 tests; this phase adds scaffold refusals, `work`/`wait` argv, per-worker assignment files under concurrent `work`, stale `.done` removal, long waits sent once, and each `wait` outcome

**Binds later work:** Directors start in `worktrees/<unit>` (branches `herdr-fake-<slug>`), the showrunner in `repo/` (branch `herdr-fake`). Directors dispatch with `trial.py work` and `trial.py wait`; a `wait` exit 1 at its deadline is a worker that never wrote its report, not a herdr fault. Teardown removes `<root>/work/` with the scratch root.

**Gotchas:**
- `production_check.sh` is zsh; under bash it fails.
- Scratch git clears every `GIT_*` variable, sets `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null` and no signing; otherwise the user's global hooks and signing fire.
- Standalone `agent wait` tracks lifecycle, not a turn: a worker that has not yet started its prompt reads as idle and returns at once, which is why `wait` waits for `working`.

**Ruled out:** a shared `work.json` with a lock — concurrent `work` calls from three directors lost an assignment, and one file per worker needs no lock.

### Phase 3 — Launch the 10 sessions  · status: done

#### As-built

- `trial.py launch` builds workspace `herdr-fake`: tab `showrunner` (pane `showrunner`, cwd `repo/`) and one tab per unit with panes `<unit>` (director), `<unit>-impl`, `<unit>-review`, cwd the unit's worktree. It starts six workers, three directors, then the showrunner, printing `label<TAB>family<TAB>pane` for each.
- It refuses (exit 1) before creating anything when the server is not answering, workspace `herdr-fake` already exists, or a start folder fails trust. `started.json` gets `codex_daemons_before` once (a retry keeps it) and, after the workers start or fail, `codex_daemons_started`: new seats-account `codex app-server` pids and start times from `/proc`.
- Unit panes carry `SHOWRUNNER_UNIT=herdr-fake` and `SHOWRUNNER_UNIT_ID=<unit>`; the showrunner pane neither. Claude panes add `CLAUDE_CONFIG_DIR` (claude 1) and `ENABLE_TOOL_SEARCH=true`, Codex panes the seats `CODEX_HOME`; caller `CLAUDE_*` is removed.
- Models come from `agents_resolve_print` (`production.director`, which must be claude; `delegate.impl` / `delegate.review`). Codex: `-m <model> -c model_reasoning_effort=<effort> -s danger-full-access -a never`. Claude: `--model --effort`, `--remote-control <name>` for showrunner and directors, `-n <name>`. A worker resolved to claude logs an issue and starts as Claude.
- `agent start` carries no prompt; once ready, `agent prompt` sends the opening text (`/showrunner:produce` or `/unit:direct` with its doc plus `scaffold_files.TRIAL_RULE`; workers a one-line wait message). `agent_not_ready` logs the visible screen, sends no keys and stops the launch; any refused start or prompt logs an issue and exits 1. Deadline `HERDR_TRIAL_START_TIMEOUT`, default 90000 ms.
- Trust (`trust-check` and launch) reads one exact key for both Claude (`hasTrustDialogAccepted: true`) and Codex (`trust_level = "trusted"`): the repository's main worktree root (parent of `git rev-parse --git-common-dir`), or the folder itself outside git.
- `client open|up [--desktop N]` runs `HERDR_TRIAL_TERMINAL` (default ghostty) `--gtk-single-instance=false --title=herdr-trial -e <herdr argv>` as `herdr-trial-client`, waits for that window via kdotool and moves it to desktop N. No window stops the unit and exits 1; an active client unit with no window is replaced.

**Files:**
- `scripts/herdr/trial.py` — `launch`, exact-key trust, ghostty client
- `scripts/herdr/scaffold_files.py` — `TRIAL_RULE`, shared by the production doc and opening prompts
- `scripts/herdr/test_trial.py` — 73 tests; adds launch layout, env and argv, start order, refusals, daemon baseline, startup-screen logging, trust keys, client window

**Binds later work:** `launch` refuses an existing `herdr-fake` workspace, so a replacement worker starts in its own pane. Teardown compares `codex_daemons_started` against `codex_daemons_before`. `trial.py` imports `scaffold_files` and `scripts.accounts`, so it runs from the full worktree.

**Gotchas:**
- Trust on `repo/` covers every scratch worktree; the user granted it in both account configs, and those entries outlive the trial.
- Interactive Codex asks folder trust, then hooks trust; headless seats ask neither.
- `showrunners.py` and `roster.py` cannot see herdr-hosted sessions.

**Ruled out:** Konsole as the client (no window appeared); opening prompts as `agent start` arguments (herdr refuses multi-line text); trust from an ancestor folder (both agents still prompt).

### Phase 4 — Run the fake production and log what breaks  · status: done

#### As-built

- The fake production in workspace `herdr-fake` runs to completion with no relay from the real showrunner: each unit's phase 1, shrink and as-built commits are merged into `herdr-fake` and pushed to the bare `origin.git`; promote refuses on `Promote: none`. Every failure is a line in `<root>/issues.jsonl` (19 entries).
- `trial.py work` reads `herdr agent get <pane>` (`WorkerReadiness`: `agent_status`, `interactive_ready`) before its one prompt. A worker not `interactive_ready` or not `idle`/`done` is refused through `worker_blocked`: exit 2, one `issues.jsonl` line, no text or keys sent.
- After the prompt, `work` waits `--until working` under `HERDR_TRIAL_TURN_START_TIMEOUT` (default 60000 ms). A started turn or an existing report returns 0; otherwise it removes the work record, logs an issue and exits 1. `work_record(label)` is the one path helper for `<root>/work/<label>.json`; `timeout_milliseconds(variable, default)` parses both timeout variables.
- Codex workers in panes, measured: each runs `codex -m gpt-6.1-sol -c model_reasoning_effort=high -s danger-full-access -a never`, labelled `codex` by herdr, 46–83 MB RSS plus a 0.3–5 MB `codex-code-mode` child; no new `~/.codex1` app-server daemon starts with them. herdr reads `idle`, `working` and `done` correctly for Claude and Codex; `agent send-keys <name> esc` interrupts a Codex turn cleanly. No worker hit a quota limit.
- The fake sessions, the herdr session and its client window stay running after the phase, for the user to experiment with.

**Files:**
- `scripts/herdr/trial.py` — `work` readiness check and turn-start confirmation, `work_record`, `timeout_milliseconds`
- `scripts/herdr/fake_herdr.py` — responses accept `write_files` (path-to-text report fixtures)
- `scripts/herdr/test_trial.py` — 80 tests; adds the not-ready refusal, the turn-start failure and the report-already-written cases

**Binds later work:** The findings as-built ranks the entries in `<root>/issues.jsonl`. The fake production registered exactly one notifier instance, `showrunner-herdr-fake`, and the state folder `~/.local/state/showrunner/herdr-fake/`; teardown removes those by exact name and waits for the user.

**Gotchas:**
- herdr does not classify the Codex folder-trust screen as blocked; only `agent get`'s `interactive_ready` shows it.
- herdr Claude panes are ordinary local sessions, so the real postmaster's alerts reach the fake showrunner.
- `showrunners.py` finds the fake showrunner only after its state registers; `roster.py` files herdr directors as freestanding and lists no Codex pane workers; `production_lifecycle.py load` and `waiting.py quota` look units up through tmux and miss herdr panes.
- `merge_checkpoint.py` has no as-built mode and fails at its promote step under `Promote: none` after pushing, so the merge record is written by hand.
- A Codex worker can show `Working` with a frozen timer for minutes on a one-line task; `trial.py wait` times out as designed and `esc` recovers it.

### Phase 5 — Write teardown and the findings  · status: done

#### As-built

- `trial.py teardown` writes the cleanup manifest `~/.local/state/herdr-trial/cleanup.json` (`HERDR_TRIAL_MANIFEST`; `CleanupManifest`: daemon baseline, started daemons, recorded unit and notifier names, exact state paths) on its first run and reads it on every retry, so retries and `leftovers` work after the scratch tree is gone. It copies `issues.jsonl` beside the manifest, closes every workspace without sending text, prompts or keys, stops the herdr server and the units in `started.json`, removes the notifier instance, `~/.local/state/showrunner/herdr-fake/` and herdr's recorded trial state by exact path (then `sessions/` and `~/.config/herdr/` only if empty), and removes `/home/natepiano/rust/herdr-trial-scratch/` last.
- Daemon stops: only a `codex app-server --managed-daemon` process recorded as trial-started, absent from the baseline, whose pid, stat start time, command and account still match its recorded `ProcessIdentity`, and only when no other Codex client of the current user shares the account. A missing, replaced or unreadable process is never stopped; another user's process is never counted as a client (`inspect_process` returns `InspectedProcess | MissingProcess | ForeignUserProcess | UnreadableProcess`). Delegate mesh servers are never recorded or signalled.
- `trial.py leftovers` exits 0 only when no trial process, unit, timer, state folder or scratch path remains, names each survivor, prints a surviving daemon as `trial-started` or `shared`, and ignores baseline daemons, unrecorded daemons and mesh servers. Both commands report the retained trust grants (Claude `hasTrustDialogAccepted` and Codex `trust_level=trusted` for `repo/`, Codex hooks trust in `~/.codex1/config.toml`) and never change them.
- `docs/as-built/herdr-trial.md` holds the findings: what ran, what the user saw, seven issues ranked by what a real herdr production would change (session identity and unit lookup first), interactive Codex in panes against app-server threads, what worked as the research said, and the teardown design. Its **Status** stays open until teardown runs.
- Fixture boundaries: `HERDR_TRIAL_PROC` (process inspection), `HERDR_TRIAL_STOP_PROCESS` (stop), `HERDR_TRIAL_NOTIFIER_ROOT`, `HERDR_TRIAL_SHOWRUNNER_STATE`, `HERDR_TRIAL_HERDR_CONFIG`.

**Files:**
- `scripts/herdr/trial.py` — `teardown`, `leftovers`, the cleanup manifest, process identity and daemon-stop checks
- `scripts/herdr/test_trial.py` — 98 tests; adds teardown order, no input to blocked panes, unrelated herdr sessions surviving, baseline daemons never stopped, PID reuse, unreadable and foreign processes, a separate `leftovers` run after scratch deletion with a shared daemon
- `scripts/herdr/fake_herdr.py` — fixture support for teardown
- `docs/as-built/herdr-trial.md` — the findings

**Binds later work:** Teardown runs only after the user ends the trial, from the complete worktree (`trial.py` imports `scaffold_files` and `scripts.accounts`). The archived `issues.jsonl` and the manifest live in `~/.local/state/herdr-trial/`, deleted only after `leftovers` exits 0. `scripts/herdr/` is retained until teardown and leftovers verification finish; the call on deleting it goes in the as-built's Status.

**Gotchas:**
- The live `started.json` lists five delegate mesh servers from other sessions as started; teardown retains them.
- Two edges remain, harmless here because the trial recorded no managed daemon: the outside-client scan runs once before all daemon stops rather than before each, and any `app-server` argv token (even inside a `codex exec` prompt) excludes that process from the outside-client check. Fix both before reusing this teardown elsewhere.

**Ruled out:** removing the trust grants automatically; counting any `codex app-server` (rather than `--managed-daemon`) as trial-started; treating unreadable processes of other users as Codex clients.

### Phase 6 — Tear down the trial and close the findings  · status: done

#### Work Order

**Goal:** Nothing from the trial is left running or on disk, and the as-built records how it ended.

**Spec:**
- Starts only when the user says they are done with the trial; until then the herdr session, its window, the 10 agents and the scratch production stay up.
- The unit director runs `trial.py teardown`, then `trial.py leftovers` until it exits 0, from the complete worktree.
- Fill the `docs/as-built/herdr-trial.md` Status with the teardown result, the leftovers output and the retained grants; include the archived `issues.jsonl` path in the checkpoint notice, then delete `~/.local/state/herdr-trial/`.
- Delete `scripts/herdr/` unless the as-built names a reason to keep it; record the call in the as-built.

**Files:**
- `docs/as-built/herdr-trial.md` — Status filled
- `scripts/herdr/trial.py`, `scripts/herdr/test_trial.py`, `scripts/herdr/fake_herdr.py` — deleted unless the as-built keeps them

**Seats:** none — the unit director runs it.

**Constraints from prior phases:**
- Phase 5's teardown stops only a managed Codex daemon (`--managed-daemon`) it recorded as started; the live `started.json` lists five delegate mesh servers as started, which teardown retains and never signals. Two minor edges remain (one outside-client scan before all stops; any `app-server` argv token hides an outside client); neither can stop anything in this trial.

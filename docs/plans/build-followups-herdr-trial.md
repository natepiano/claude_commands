# herdr trial: a fake production running in herdr on natedev

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A throwaway production (1 showrunner, 3 unit directors, 2 workers per unit) runs inside one herdr session on natedev, visible in a Konsole window on the KDE desktop, doing tiny fake work in a scratch repo. Every problem it hits is logged; then everything is removed and the findings are written up.

> **As-built disposition: create** — a new `docs/as-built/herdr-trial.md`

> **Production: build-followups** — unit `herdr-trial-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-13 10:33 EDT

make a fake herdr production with 1 showrunner 3 units and each unit with 2 workers - using our current defaults - assign this work to unit director to implement so we can learn from the issues we see and i can see it visibly running here on natedev

Showrunner's brief, 2026-10-13 10:3x EDT (not the user's words): research is `~/.local/state/plan-backlog/2026-10-13-herdr.md`; install with `nix shell` from the locked nixpkgs-unstable input, no rebuild; workers in panes so each has a status row (Claude workers interactive; record what Codex workers in panes would cost); scratch repo with a local bare origin, never touch `~/.claude` main or a real production; stop short of promote and record any refusal rather than working around it; new sessions start in a folder that must be trusted; open a herdr client window on the KDE desktop (Konsole), no kwin restart or display change; all 10 sessions on claude 1 at the agents.conf defaults; tiny fake work; stop everything once the issues are logged and remove every scratch session, notifier instance and folder.

## Calls I made

- **Workers are Claude, one implementer and one reviewer per unit**, at `[delegate.claude]` `impl=opus:high` and `review=opus:xhigh`. The delegate default family is codex, but the brief puts all 10 sessions on claude 1, and a Claude worker can run interactive in a pane. Codex in a pane is measured once, idle, in Phase 4 (startup time and memory, no prompt). Risk: the trial does not exercise Codex seats; the measurement stands in for them.
- **The fake showrunner runs at `opus:xhigh`** (the `[production.claude] director` row; agents.conf has no showrunner row). Risk: none for the trial.
- **One herdr session named `herdr-trial`** (`herdr --session herdr-trial`), so nothing joins or disturbs a default herdr session. No herdr Claude integration is installed: it writes into `~/.claude/hooks/` and `settings.json`. Consequence, logged as a finding: no resume after a server restart.
- **Units are launched by this unit's launcher, not by `add_unit.py`**, which starts tmux sessions. The fake showrunner is told so, and each place its commands assume tmux is logged as a finding. That list is the main output of the trial.
- **Trust dialogs are answered by the launcher** (read the pane, press Enter on the trust prompt) and logged; no account config is edited to pre-trust the folder.
- **The fake production's Promote is `none`.** It merges to its own merge branch and pushes to the bare origin; nothing reaches `~/.claude`.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-herdr-trial`, branch `build-followups-herdr-trial` (unit `herdr-trial-unit`); name both in every dispatch. Code lives in `scripts/herdr/` only.
- **Project started:** 2026-10-13T14:36:00Z
- **Stack:** Python 3 standard library, checked by basedpyright; herdr 0.9.0; Claude Code CLI; zsh.
- **herdr binary:** `/nix/store/pyqm5522kvk30b9vgb1ab976pjnjwn51-herdr-0.9.0/bin/herdr`, from the locked `nixpkgs-unstable` input (rev `8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe`): `nix shell github:NixOS/nixpkgs/8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe#herdr`. Already in the store; nothing builds. The trial script finds it through `HERDR_BIN`, else that store path.
- **herdr commands** (research report §2, checked on natedev at 0.9.0): `herdr server` (headless), `herdr server stop`, `herdr workspace create --cwd <dir> --label <name> --env VAR=... --no-focus` (prints the pane id), `herdr tab ...`, `herdr pane list|get|read <pane> --source visible [--ansi]|send-text|send-keys <pane> enter|run <pane> "<cmd>"|close|process-info`, `herdr agent list|prompt <pane> "<text>"|wait <pane> --until idle|blocked`, `herdr session list --json`. Every command takes `--session herdr-trial` (global option). Panes are addressed by id (`w3:p1`), never by label: look ids up by label in `pane list` JSON. `agent prompt` refuses with `agent_blocked` while a dialog shows.
- **Key files:**
  - `scripts/production/add_unit.py` — `prompt_for` 665 (the unit director's first prompt, three forms), `launch_session` 701 (argv: `claude --model <m> --effort <e> --remote-control <name> -n <name> --settings '{"disableAgentView": true}' <prompt>`, prefixed by `account_prefix(selected)` and `ENABLE_TOOL_SEARCH=true`; run as `systemd-run --user --scope` around `tmux new-session -d -s <name> -c <cwd> -e SHOWRUNNER_UNIT=<slug> -e SHOWRUNNER_UNIT_ID=<unit> zsh -ic "<command>; exec zsh"`), `launch_environment` 739 (drops `CLAUDE_*`). The herdr launcher mirrors this argv and these marks.
  - `scripts/production/unit_lookup.py` — `PRODUCTION_MARK = "SHOWRUNNER_UNIT"` 27, `UNIT_MARK = "SHOWRUNNER_UNIT_ID"` 28; finds units by asking tmux.
  - `scripts/accounts/accounts.py` — `account_prefix`; claude 1 is `~/.claude1` (role: unit directors).
  - `scripts/agents/agents_config.sh` — `agents_resolve_print production.director`, `delegate.impl`, `delegate.review` with `family=claude` give the models above; read them at launch, never hard-code.
  - `scripts/production/showrunners.py` — `registered_showrunners` 190, `current_name` 230, `address <slug>`; `scripts/production/production_check.sh` (doc status line `> **Status: PRODUCTION — running.**`).
  - `scripts/message/roster.py` 612–661 — finds units through tmux panes (research §2: inside herdr the pane field is empty).
  - Production doc format: `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md` (Production Context bullets, Units table, Production rules).
- **Scratch root:** `/tmp/claude/herdr-trial/` — `repo/` (the fake production's checkout), `origin.git` (bare), `worktrees/<unit>/`, `issues.jsonl`, `herdr.log`. Created by Phase 2, removed by Phase 5. Never `rm` by glob outside it; never kill by pattern (memory `never-kill-by-pattern`): stop only pids and units the trial started, recorded in `/tmp/claude/herdr-trial/started.json`.
- **Desktop:** KDE on Wayland (`WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:0` in `systemctl --user show-environment`); `konsole` on PATH. Open the client with `systemd-run --user --unit=herdr-trial-client konsole -e <herdr> --session herdr-trial`. No kwin restart, no display change.
- **Test:** `python3 -m unittest discover -s scripts/herdr -p 'test_*.py'`. Tests never start herdr or Claude: they run against a fake `HERDR_BIN` script that records its argv and prints canned JSON.
- **Lint:** `basedpyright scripts/herdr` — pass is its `0 errors, 0 warnings` line (it exits 3 on the missing `.venv`).
- **Invariants:**
  - Nothing touches `~/.claude` main, `~/.claude/settings.json`, `~/.claude1` config files, any real production's doc, worktree, timer or session, or a default herdr session.
  - No `production_lifecycle.py promote-main` or `wrap` on any production. A refusal by auto mode is logged as a finding, never worked around.
  - All 10 trial sessions run on claude 1 (`~/.claude1`). Fake work stays tiny: one phase per unit, a few lines each.
  - Every issue goes to `issues.jsonl` through `trial.py issue` at the moment it is seen, with the time, the session, what was expected and what happened.
  - Python: basedpyright 0/0; no file-level ignores; `TypedDict` for every herdr JSON shape; no `Any`.

## Phases

### Phase 1 — herdr server, client window and pane helpers  · status: todo

#### Work Order

**Goal:** One command starts a headless herdr session `herdr-trial`, opens it in a Konsole window on the KDE desktop, and the trial script can create, find, type into, read and close panes by label.

**Spec:**
- New `scripts/herdr/trial.py` (argparse subcommands), with a `Herdr` class wrapping the binary (`HERDR_BIN` env, else the store path above) and always passing `--session herdr-trial`:
  - `server start`: runs `systemd-run --user --unit=herdr-trial-server <herdr> --session herdr-trial server`; waits up to 10 s for `herdr status server` to answer; records the unit name in `started.json`. A second `start` while it answers changes nothing.
  - `server stop`: `herdr server stop`, then `systemctl --user stop herdr-trial-server` if still active.
  - `client open`: the `systemd-run ... konsole -e` line above, recorded in `started.json`; `client close` stops that unit.
  - `pane-id <label>`: the id for a label from `pane list` JSON; exit 1 naming the label when none or several match.
  - `read <label>`, `type <label> <text>` (send-text then send-keys enter), `agents` (one line per agent: label, agent, state).
  - `issue <session> <expected> <happened>`: appends one JSON line (`time` in UTC ISO, `session`, `expected`, `happened`) to `/tmp/claude/herdr-trial/issues.jsonl`, creating the folder.
- Record in the phase notice where herdr 0.9.0 keeps its socket, config and state for a named session (from `herdr status`, `herdr api`, and the files that appear), so Phase 5 can remove them.
- Live check: `server start`, `client open` (the user sees an empty herdr window), create a workspace `smoke` running `zsh`, `type smoke 'echo $HOME ; ok'`, `read smoke` shows it, close the workspace. Leave the server and window running for Phase 3.

**Files:**
- `scripts/herdr/trial.py` — new
- `scripts/herdr/test_trial.py` — new: argv built for each subcommand, `pane-id` with zero, one and two matches, `issue` line shape, against a fake `HERDR_BIN`
- `scripts/herdr/fake_herdr.py` — new test double

### Phase 2 — The scratch production  · status: todo

#### Work Order

**Goal:** A scratch repo with a bare origin, three unit worktrees, a fake production doc in the real format and three one-phase unit plans exist under `/tmp/claude/herdr-trial/`, built by one command.

**Spec:**
- `trial.py scaffold` (refuses if `/tmp/claude/herdr-trial/repo` exists):
  - `origin.git` bare; `repo/` cloned from it with `textkit/__init__.py`, `textkit/test_textkit.py` (one passing test), a README; committed on `main` and pushed; merge branch `herdr-fake` pushed.
  - Worktrees `worktrees/{slug,clamp,count}-unit` on branches `herdr-fake-{slug,clamp,count}` from `herdr-fake`.
  - `repo/docs/plans/herdr-fake-production.md`: `> **Status: PRODUCTION — running.**`, Production Context bullets as in the real doc (Repository `repo/`, Merge branch `herdr-fake`, Showrunner checkout `repo/`, Showrunner session natedev, Log `docs/plans/herdr-fake-log.md`, User zone America/New_York, Updates on demand, Merge tests `python3 -m unittest discover -s textkit`, Push git (bare origin), **Promote: none**), a Units table with the three units, and a Production rules line: "Trial production: units run in herdr session herdr-trial, launched by `~/.claude/scripts/herdr/trial.py`; never launch, restart or look up a unit through tmux or `add_unit.py`; log anything that assumes tmux with `trial.py issue`; never promote or wrap."
  - Unit plans `repo/docs/plans/herdr-fake-{slug,clamp,count}.md`, each one phase in delegate-ready form: `slugify(text) -> str`, `clamp(value, low, high) -> float`, `word_count(text) -> int` in `textkit/__init__.py`, each with two tests. Each plan's Delegation Context says its two workers are the panes `<unit>-impl` and `<unit>-review`, reached with `trial.py work <unit> impl|review <work-order-file>` and awaited with `trial.py wait <unit> impl|review`, in place of `implement.sh` and `review.sh` seats.
- `trial.py work <unit> <role> <file>`: `agent prompt` to the worker pane with "Read and carry out <file>. When done, write your report to <file>.done." `trial.py wait <unit> <role>`: `agent wait --until idle`, then prints `<file>.done` (exit 1 if missing). A refused prompt (`agent_blocked`) exits 2 and logs an issue.
- Live check: `scaffold` twice (second refuses), `production_check.sh repo/docs/plans/herdr-fake-production.md` exits 0, `git -C repo log origin/herdr-fake` shows the commit.

**Files:**
- `scripts/herdr/trial.py` — `scaffold`, `work`, `wait`
- `scripts/herdr/scaffold_files.py` — new: the file texts `scaffold` writes
- `scripts/herdr/test_trial.py` — scaffold refuses an existing root; `work`/`wait` argv; `agent_blocked` exit 2 and issue line

### Phase 3 — Launch the 10 sessions  · status: todo

#### Work Order

**Goal:** The user watches 1 showrunner, 3 unit directors and 6 workers appear in the herdr window, each with its own status row, all on claude 1.

**Spec:**
- `trial.py launch`:
  - Workspace `herdr-fake` with tab `showrunner` (pane `showrunner`, cwd `repo/`) and one tab per unit holding three panes: `<unit>` (director, cwd its worktree), `<unit>-impl`, `<unit>-review` (cwd the same worktree). Every pane gets `--env SHOWRUNNER_UNIT=herdr-fake` and, for unit panes, `SHOWRUNNER_UNIT_ID=<unit>`, `CLAUDE_CONFIG_DIR=$HOME/.claude1` (via `account_prefix` for claude 1), `ENABLE_TOOL_SEARCH=true`; `CLAUDE_*` from the caller removed (`launch_environment`). The showrunner pane carries no unit mark.
  - Models from `agents_resolve_print`: showrunner and directors `production.director`; workers `delegate.impl` / `delegate.review` with family claude.
  - argv as `add_unit.launch_session`: showrunner and directors get `--remote-control <name> -n <name>`; workers get `-n <name>` only (seats have no Remote Control). Names: `herdr-fake`, `herdr-fake-<unit>`, `herdr-fake-<unit>-impl|review`.
  - First prompts: showrunner `/showrunner:produce <repo>/docs/plans/herdr-fake-production.md` followed by the trial rule from the doc; directors the `prompt_for` PlanGiven form plus the same rule; workers "You are the <role> worker of <unit> in the herdr trial. Wait; your unit director will send you Work Orders."
  - Trust dialogs: after each launch, read the pane for up to 20 s; on the folder-trust prompt press Enter and log one issue per session.
  - Launch order: workers, then directors, then the showrunner, so the showrunner finds its units running.
- Live check: `trial.py agents` lists 10 Claude agents; `ps` shows each on `CLAUDE_CONFIG_DIR=/home/natepiano/.claude1`; one screenshot of the Konsole window in the phase notice. Then `showrunners.py address herdr-fake` and `roster.py` are run once and their answers logged as issues if they miss the herdr sessions.

**Files:**
- `scripts/herdr/trial.py` — `launch`
- `scripts/herdr/test_trial.py` — pane layout and env per pane, argv per role, launch order, trust prompt detection on a canned screen

### Phase 4 — Run the fake production and log what breaks  · status: todo

#### Work Order

**Goal:** Each fake unit finishes its one phase and the fake showrunner merges all three to `herdr-fake` and pushes to the bare origin; every failure along the way is in `issues.jsonl`.

**Spec:**
- Watch, never poll: run `herdr agent wait <pane> --until blocked` per director and for the showrunner in the background and act on each return; read a blocked pane, answer only trust and permission prompts that stay inside the scratch root, log every one.
- Relay nothing from the real showrunner to the fake one. Answer a fake session's question only when the brief or this plan answers it; otherwise log it and answer "skip it for the trial".
- Places expected to fail, to watch and log (research §2, §3, §6): the fake showrunner's unit lookup, roster and dailies (tmux); `SendMessage` addresses between fake sessions; `add_unit.py` if the showrunner calls it; notifier timers it registers (record their names for Phase 5); `/unit:direct` steps that start `implement.sh` or `review.sh` seats instead of the pane workers; the fake showrunner reaching `promote-main` (must not run; record the refusal or the stop).
- Codex cost: open one pane in a separate workspace `codex-probe` running interactive `codex` on codex 1 (`CODEX_HOME=$HOME/.codex1`), no prompt; record startup to idle, RSS from `herdr pane process-info`, whether herdr labels it `codex`; close it.
- Stop condition: the merge to `herdr-fake` is pushed, or 90 minutes pass, or a blocker no rule here answers. Then stop every fake session (`/exit` typed into each pane, then `pane close`).

**Files:**
- `scripts/herdr/trial.py` — only fixes the run needs; each one logged as an issue against the trial itself

### Phase 5 — Remove everything and write the findings  · status: todo

#### Work Order

**Goal:** Nothing from the trial is left running or on disk, and the findings are written for the decision on herdr.

**Spec:**
- `trial.py teardown`: close the workspaces, `server stop`, `client close`, stop each systemd unit in `started.json`, remove the notifier instances and `~/.local/state/showrunner/herdr-fake/` the fake showrunner registered (by exact name), remove herdr's state for session `herdr-trial` (paths from Phase 1), remove `/tmp/claude/herdr-trial/` after copying `issues.jsonl` into the notice.
- `trial.py leftovers`: exit 0 only when no process, systemd unit, timer, state folder or scratch path from the trial remains; prints each one that does.
- `docs/as-built/herdr-trial.md`: what ran, what the user saw, the issues ranked by what a real herdr migration would have to change (session identity, unit lookup, launcher, trust, earlyoom, resume), the Codex-in-a-pane cost, and what worked as the research said.
- Delete `scripts/herdr/` unless the as-built names a reason to keep it; record the call.

**Files:**
- `scripts/herdr/trial.py` — `teardown`, `leftovers`
- `docs/as-built/herdr-trial.md` — new

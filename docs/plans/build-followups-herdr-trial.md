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
- **herdr commands** (research report §2, checked on natedev at 0.9.0): `herdr server` (headless), `herdr server stop`, `herdr workspace create --cwd <dir> --label <name> --env VAR=... --no-focus` (prints the pane id), `herdr tab ...`, `herdr pane list|get|read <pane> --source visible [--ansi]|send-text|send-keys <pane> enter|run <pane> "<cmd>"|close|process-info`, `herdr agent list|prompt <pane> "<text>"|wait <pane> --until idle|blocked`, `herdr session list --json`. Every command takes `--session herdr-trial` (global option). Panes are addressed by id (`w3:p1`), never by label: look ids up by label in `pane list` JSON. `agent prompt` refuses with `agent_blocked` while a dialog shows.
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

#### Work Order

**Goal:** One command starts a headless herdr session `herdr-trial`, opens it in a Konsole window on the KDE desktop, and the trial script can create, find, type into, read and close panes by label, and confirm a start folder is already trusted.

**Spec:**
- New `scripts/herdr/trial.py` (argparse subcommands), with a `Herdr` class wrapping the binary (`HERDR_BIN` env, else the store path above) and always passing `--session herdr-trial`:
  - `server start`: runs `systemd-run --user --unit=herdr-trial-server <herdr> --session herdr-trial server`; waits up to 10 s for `herdr status server` to answer; records the unit name in `started.json`. A second `start` while it answers changes nothing.
  - `server stop`: `herdr server stop`, then `systemctl --user stop herdr-trial-server` if still active.
  - `client open`: the `systemd-run ... konsole -e` line above, recorded in `started.json`; `client close` stops that unit.
  - `pane-id <label>`: the id for a label from `pane list` JSON; exit 1 naming the label when none or several match.
  - `read <label>`, `type <label> <text>` (send-text then send-keys enter), `agents` (one line per agent: label, agent, state).
  - `issue <session> <expected> <happened>`: appends one JSON line (`time` in UTC ISO, `session`, `expected`, `happened`) to `/home/natepiano/rust/herdr-trial-scratch/issues.jsonl`, creating the folder.
  - `trust-check <dir>`: exit 0 when `<dir>` or a parent has `hasTrustDialogAccepted` in `$CLAUDE_CONFIG_DIR/.claude.json` (default `~/.claude1`) and `<dir>` or a parent is `trust_level = "trusted"` in `$CODEX_HOME/config.toml` (default the seats account); otherwise exit 1 naming what is missing. Reads only.
- Record in the phase notice where herdr 0.9.0 keeps its socket, config and state for a named session (from `herdr status`, `herdr api`, and the files that appear), so Phase 5 can remove them.
- Live check: `server start`, `client open` (the user sees an empty herdr window), create a workspace `smoke` running `zsh`, `type smoke 'echo $HOME ; ok'`, `read smoke` shows it, close the workspace. Then, in `/home/natepiano/rust/herdr-trial-scratch/probe/`, one pane running `claude` on claude 1 and one running `codex` on the seats account, each with no prompt: `read` each and confirm no trust prompt shows, then `/exit` and close (nothing is pressed in a prompt; if one shows, stop and report the folder). Leave the server and window running for Phase 3.

**Files:**
- `scripts/herdr/trial.py` — new
- `scripts/herdr/test_trial.py` — new: argv built for each subcommand, `pane-id` with zero, one and two matches, `issue` line shape, `trust-check` on fixture configs (self, parent, none), against a fake `HERDR_BIN`
- `scripts/herdr/fake_herdr.py` — new test double

### Phase 2 — The scratch production  · status: todo

#### Work Order

**Goal:** A scratch repo with a bare origin, three unit worktrees, a fake production doc in the real format and three one-phase unit plans exist under `/home/natepiano/rust/herdr-trial-scratch/`, built by one command.

**Spec:**
- `trial.py scaffold` (refuses if `/home/natepiano/rust/herdr-trial-scratch/repo` exists):
  - `origin.git` bare; `repo/` cloned from it with `textkit/__init__.py`, `textkit/test_textkit.py` (one passing test), a README; committed on `main` and pushed; merge branch `herdr-fake` pushed.
  - Worktrees `worktrees/{slug,clamp,count}-unit` on branches `herdr-fake-{slug,clamp,count}` from `herdr-fake`.
  - `repo/docs/plans/herdr-fake-production.md`: `> **Status: PRODUCTION — running.**`, Production Context bullets as in the real doc (Repository `repo/`, Merge branch `herdr-fake`, Showrunner checkout `repo/`, Showrunner session natedev, Log `docs/plans/herdr-fake-log.md`, User zone America/New_York, Updates on demand, Merge tests `python3 -m unittest discover -s textkit`, Push git (bare origin), **Promote: none**), a Units table with the three units, and a Production rules line: "Trial production: units run in herdr session herdr-trial, launched by `~/.claude/scripts/herdr/trial.py`; never launch, restart or look up a unit through tmux or `add_unit.py`; log anything that assumes tmux with `trial.py issue`; never promote or wrap; never answer a trust or permission prompt for the user."
  - Unit plans `repo/docs/plans/herdr-fake-{slug,clamp,count}.md`, each one phase in delegate-ready form: `slugify(text) -> str`, `clamp(value, low, high) -> float`, `word_count(text) -> int` in `textkit/__init__.py`, each with two tests. Each plan's Delegation Context says its two workers are the panes `<unit>-impl` and `<unit>-review`, reached with `trial.py work <unit> impl|review <work-order-file>` and awaited with `trial.py wait <unit> impl|review`, in place of `implement.sh` and `review.sh` seats.
- `trial.py work <unit> <role> <file>`: `agent prompt` to the worker pane with "Read and carry out <file>. When done, write your report to <file>.done." `trial.py wait <unit> <role>`: `agent wait --until idle`, then prints `<file>.done` (exit 1 if missing). A refused prompt (`agent_blocked`) exits 2 and logs an issue.
- Live check: `scaffold` twice (second refuses), `production_check.sh repo/docs/plans/herdr-fake-production.md` exits 0, `git -C repo log origin/herdr-fake` shows the commit, `trust-check` passes for `repo/` and each worktree.

**Files:**
- `scripts/herdr/trial.py` — `scaffold`, `work`, `wait`
- `scripts/herdr/scaffold_files.py` — new: the file texts `scaffold` writes
- `scripts/herdr/test_trial.py` — scaffold refuses an existing root; `work`/`wait` argv; `agent_blocked` exit 2 and issue line

### Phase 3 — Launch the 10 sessions  · status: todo

#### Work Order

**Goal:** The user watches 1 showrunner, 3 unit directors and 6 workers appear in the herdr window, each with its own status row: the 4 Claude sessions on claude 1, the 6 Codex workers on the seats account.

**Spec:**
- `trial.py launch`:
  - Before any launch, `trust-check` every start folder; any failure stops the launch and names the folder.
  - Workspace `herdr-fake` with tab `showrunner` (pane `showrunner`, cwd `repo/`) and one tab per unit holding three panes: `<unit>` (director, cwd its worktree), `<unit>-impl`, `<unit>-review` (cwd the same worktree).
  - Every pane gets `--env SHOWRUNNER_UNIT=herdr-fake` and, for unit panes, `SHOWRUNNER_UNIT_ID=<unit>`; `CLAUDE_*` from the caller removed (`launch_environment`). Claude panes add `CLAUDE_CONFIG_DIR=$HOME/.claude1` (via `account_prefix` for claude 1) and `ENABLE_TOOL_SEARCH=true`; Codex panes add `CODEX_HOME` of the seats account. The showrunner pane carries no unit mark.
  - Models from `agents_resolve_print`: showrunner and directors `production.director`; workers `delegate.impl` / `delegate.review` in the family `[assignments] delegate` names (codex today).
  - Claude argv as `add_unit.launch_session`: showrunner and directors get `--remote-control <name> -n <name>`. Codex workers: `codex -m <model> -c model_reasoning_effort=<effort>` with the sandbox and approval settings `implement.sh` gives a codex delegate (log any that interactive codex does not accept). Names: `herdr-fake`, `herdr-fake-<unit>`, `herdr-fake-<unit>-impl|review` (a Claude fallback worker gets `-n <name>` only).
  - First prompts: showrunner `/showrunner:produce <repo>/docs/plans/herdr-fake-production.md` followed by the trial rule from the doc; directors the `prompt_for` PlanGiven form plus the same rule; workers "You are the <role> worker of <unit> in the herdr trial. Wait; your unit director will send you Work Orders."
  - After each launch, read the pane for up to 20 s; a trust or permission prompt is logged and left untouched, and the launch stops there for the showrunner.
  - Launch order: workers, then directors, then the showrunner, so the showrunner finds its units running.
- Live check: `trial.py agents` lists 4 Claude and 6 Codex agents (fewer Codex if a fallback was logged); the process environment shows each Claude session on `/home/natepiano/.claude1` and each Codex worker on the seats `CODEX_HOME`; one screenshot of the Konsole window in the phase notice. Then `showrunners.py address herdr-fake` and `roster.py` are run once and their answers logged as issues if they miss the herdr sessions.

**Files:**
- `scripts/herdr/trial.py` — `launch`
- `scripts/herdr/test_trial.py` — pane layout and env per pane, argv per role and family, launch order, a failed `trust-check` stops the launch, a prompt on a canned screen is logged and no key is sent

### Phase 4 — Run the fake production and log what breaks  · status: todo

#### Work Order

**Goal:** Each fake unit finishes its one phase and the fake showrunner merges all three to `herdr-fake` and pushes to the bare origin; every failure along the way is in `issues.jsonl`.

**Spec:**
- Watch, never poll: run `herdr agent wait <pane> --until blocked` per director and for the showrunner in the background and act on each return; read a blocked pane and log it. Never answer a trust or permission prompt: bring it to the showrunner, which asks the user.
- Relay nothing from the real showrunner to the fake one. Answer a fake session's question only when the brief or this plan answers it; otherwise log it and answer "skip it for the trial".
- Places expected to fail, to watch and log (research §2, §3, §6): the fake showrunner's unit lookup, roster and dailies (tmux); `SendMessage` addresses between fake sessions; `add_unit.py` if the showrunner calls it; notifier timers it registers (record their names for Phase 5); `/unit:direct` steps that start `implement.sh` or `review.sh` seats instead of the pane workers; the fake showrunner reaching `promote-main` (must not run; record the refusal or the stop).
- Codex workers in panes against the default (threads on one shared `codex app-server` per delegate session): log per worker its startup to idle, RSS from `herdr pane process-info`, whether herdr labels it `codex` and reads its state right, whether the director can interrupt or message it mid-run, and quota drawn. A worker whose account hits its limit is logged and relaunched on its `[delegate.claude]` row.
- Stop condition: the merge to `herdr-fake` is pushed, or 90 minutes pass, or a blocker no rule here answers. Then stop every fake session (`/exit` typed into each pane, then `pane close`).

**Files:**
- `scripts/herdr/trial.py` — only fixes the run needs; each one logged as an issue against the trial itself

### Phase 5 — Remove everything and write the findings  · status: todo

#### Work Order

**Goal:** Nothing from the trial is left running or on disk, and the findings are written for the decision on herdr.

**Spec:**
- `trial.py teardown`: close the workspaces, `server stop`, `client close`, stop each systemd unit in `started.json`, remove the notifier instances and `~/.local/state/showrunner/herdr-fake/` the fake showrunner registered (by exact name), remove herdr's state for session `herdr-trial` (paths from Phase 1), remove `/home/natepiano/rust/herdr-trial-scratch/` by that exact path after copying `issues.jsonl` into the notice.
- `trial.py leftovers`: exit 0 only when no process, systemd unit, timer, state folder or scratch path from the trial remains; prints each one that does.
- `docs/as-built/herdr-trial.md`: what ran, what the user saw, the issues ranked by what a real herdr migration would have to change (session identity, unit lookup, launcher, trust, earlyoom, resume), Codex workers in panes against app-server threads, and what worked as the research said.
- Delete `scripts/herdr/` unless the as-built names a reason to keep it; record the call.

**Files:**
- `scripts/herdr/trial.py` — `teardown`, `leftovers`
- `docs/as-built/herdr-trial.md` — new

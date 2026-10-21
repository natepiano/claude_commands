# herdr trial: a fake production in herdr panes

## What it is

On 2026-10-13 a fake production ran inside one herdr 0.9.0 session on natedev. It had one showrunner and three unit directors, all Claude, plus six Codex workers (one writer and one reviewer per unit). Each agent ran in its own pane, and the user could watch them all in one ghostty window. The point was to find out what breaks before a real production moves off tmux. The work itself was tiny: three one-phase units, each adding one helper to a small Python package, `textkit`, in a scratch repository with a bare local origin. Nothing was promoted to a real main branch, and the real `~/.claude` main was checked afterwards and holds nothing from the trial.

A driver script, `scripts/herdr/trial.py`, ran the whole trial, with 98 tests over a fake herdr. The trial is torn down and the driver is removed: `scripts/herdr/herdr.py` replaced it, and [herdr-adoption.md](herdr-adoption.md) covers the code that exists now. This document is the record of what the trial ran and found.

## What ran

| Step | When (EDT) | Result |
| --- | --- | --- |
| herdr installed via `nix shell`; server and client as systemd user units | 10:44 | `herdr-trial-server`, `herdr-trial-client` (Konsole at first, later ghostty titled `herdr-trial`) |
| Scratch production scaffolded | before 13:23 | `repo/` on `herdr-fake`, bare `origin.git`, three unit worktrees |
| 10 agents launched in workspace `w9` | 13:23 | showrunner `p1`; per unit a director plus `-impl` and `-review` Codex panes |
| Fake production ran to completion | 13:23–14:10 | each unit's phase 1, shrink and as-built merged into `herdr-fake` and pushed to origin (`c7c92f2`) |
| Teardown and leftovers check written and tested | 14:10–15:13 | 98 tests |
| Trial torn down at the user's word | 15:13 | teardown exit 0; leftovers exit 0 |

## What the user saw

- One ghostty window, titled `herdr-trial` and moved to the requested desktop, showing every agent's pane at once. The first client, Konsole, ran for two hours with no visible window (issue 4).
- Trust prompts on first contact: Claude's folder trust, then Codex's folder trust and its "Hooks need review" screen. The user answered each one once, and the answers were saved, so later panes did not ask (issues 1, 3, 9).
- Real postmaster quota alerts appearing in the fake showrunner's pane (issues 13, 20).

## Findings, ranked by what a real herdr production would have to change

1. **Session identity and unit lookup.** When the trial ran, every production tool found sessions through tmux or registered state. In the trial:
   - `production_lifecycle.py load` reported every director as `session: gone`, and `waiting.py quota` could not reach any of them (issues 5, 8).
   - `roster.py` listed none of the 10 agents at first. Once the showrunner's state registered, it listed the directors but filed them as `freestanding`, and it never listed the Codex pane workers (issues 7, 17).

   Fix before adopting herdr: the roster, the showrunner lookup and every tmux lookup need a herdr source. `herdr agent list` gives each pane's agent, status and pane id.
2. **Delivering a Work Order.** herdr does not treat Codex's folder-trust screen as blocked. Two Work Orders were pasted into panes that were sitting at that screen and were lost there. Each director then waited the full 30 minutes before resending (issues 10–12, 15). `trial.py work` was then changed so that it:
   - reads `agent get` and refuses a worker that is not `interactive_ready`;
   - confirms the turn started within `HERDR_TRIAL_TURN_START_TIMEOUT`.

   A real launcher needs both checks.
3. **Trust.** Interactive Codex asks for folder trust, keyed on the repository root, and then for hooks trust. Headless workers (`codex app-server`) ask for neither. A herdr production has to settle both before launch, or every new repository puts a prompt in front of the user (issues 1, 3, 9).
4. **Launcher.** herdr refuses multi-line start arguments (issue 2), so opening prompts go through `agent prompt` once the agent is ready. Konsole showed no window, and ghostty did (issue 4).
5. **The showrunner's view of its units.**
   - The fake showrunner reported workers as running because it read its directors' holding lines; the workers had never started (issue 14). Inside herdr it could read worker states directly, but no rule tells it to.
   - The merge script pushes and then fails at its promote step under `Promote: none`, and it has no as-built mode (issue 16). This is being fixed separately by the showrunner's fixer.
6. **Showing one unit.** The user asked for a command that opens the session showing one showrunner or unit. In herdr 0.9.0 every attached window keeps its own view. `agent focus slug-unit` moved herdr's recorded focus (`api snapshot` reported `focused_tab_id` `w9:t2`), but the open window did not move. A newly attached window opened on the showrunner's tab while herdr still reported slug-unit as focused. No CLI option, API field or config key selects the tab a window shows. A herdr production that wants `attach <unit>` needs one herdr session per unit and one for the showrunner. Within one session, the user switches in the window with Ctrl+b and the tab number, or Ctrl+b g for the goto picker.
7. **Relays reach trial sessions.** A Claude pane in herdr is an ordinary local session, so the machine's postmaster sends it real quota alerts (issues 13, 20). That is correct on a shared account. A trial that wants silence would need its own account.
8. **earlyoom and resume.** Every pane is a child of the one `herdr --session herdr-trial server` process. Losing that process (to earlyoom, a crash or a server stop) ends all 10 agents at once. Under tmux, each session is its own tree. Resuming after a server loss was not tried; the herdr Claude integration that would allow it was not installed, because it writes into `~/.claude/hooks/` and `settings.json`.

The showrunner's first lookup at 13:24 said "not running", one minute after launch. A later recheck found it. `showrunners.py` reads registered update-timer instances and session sockets, and neither depends on tmux, so the same first-minute miss is expected under tmux. That inference was not measured.

## Codex workers in panes compared with app-server threads

| | Interactive Codex in a herdr pane | Headless worker (`codex app-server` thread) |
| --- | --- | --- |
| Start | `codex -m gpt-6.1-sol -c model_reasoning_effort=high -s danger-full-access -a never` | thread on a per-session `codex app-server --listen ws://…` |
| Prompts on first use | folder trust, then hooks trust | none |
| Memory | 46–83 MB per pane, plus a 0.3–5 MB `codex-code-mode` child | shared server per session |
| State | herdr reads `idle`, `working` and `done` correctly; misses the trust screen | launcher reads turn status |
| Interrupt | `agent send-keys <name> esc` stopped a turn cleanly | steer/interrupt via the mesh |
| Daemon | uses the account's managed app-server daemon (`--managed-daemon`); none new started | one app-server per delegate session |

One Codex pane stalled with `Working` and a frozen timer on a one-line task. `trial.py wait` timed out as designed, and `esc` recovered the pane (issue 19). No worker hit a quota limit. Claude 1 used 5% of its 5-hour window across the run.

## What worked as the research said

- herdr labelled every agent correctly (`claude`, `codex`) and read their states correctly, except for the trust screen.
- `agent wait --until` and `agent prompt` behaved as documented, and herdr refused a prompt sent to a blocked agent.
- Closing a workspace's last pane removes the workspace.
- `status server` exits 0 even when the server is stopped, so the trial reads its `status: running` line instead.
- The server and client ran for the whole day as systemd user units.

## How the trial driver worked

The driver and its files (`trial.py`, `scaffold_files.py`, `test_trial.py`) are removed. What it did, as the method behind the findings:

- **Layout.** One herdr session, `herdr-trial`, with the server and a ghostty client as systemd user units (`herdr-trial-server`, `herdr-trial-client`). Workspace `herdr-fake` held a `showrunner` tab with cwd `repo/`, and per unit a tab with the director pane `<unit>`, `<unit>-impl` split right of it and `<unit>-review` split below, cwd the unit's worktree. Panes were addressed by id, looked up by label.
- **Pane environment.** Unit panes carried `SHOWRUNNER_UNIT=herdr-fake` and `SHOWRUNNER_UNIT_ID=<unit>`. Claude panes added `CLAUDE_CONFIG_DIR` (claude1) and `ENABLE_TOOL_SEARCH=true`; Codex panes added `CODEX_HOME`. The caller's `CLAUDE_*` variables were removed.
- **Start.** `agent start` with no prompt; once ready, `agent prompt` sent the opening text. Workers started first, then the directors, then the showrunner. An `agent_not_ready` screen was logged and no key was sent.
- **Work Orders.** `work` refused a worker unless `agent get` read `interactive_ready` and `idle` or `done`, sent one prompt, and confirmed the turn started (`--until working`, 60 s). `wait` only observed, under a 30-minute deadline, and never sent the prompt again.
- **Trust.** One exact key served both agents: the repository's main worktree root, or the folder itself outside git. Claude needed `projects[key].hasTrustDialogAccepted`; Codex needed `projects[key].trust_level = "trusted"` in `config.toml`. Ancestor entries did not count, and nothing wrote either file.
- **Rules.** Nothing sent a key to a trust or permission prompt, except a manual `type` command. Nothing was killed by pattern: only recorded systemd units and recorded, still-matching daemons were stopped. Removal was by exact path. Scratch git cleared every `GIT_*` variable and ran with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `core.hooksPath=/dev/null` and no signing.

## Gotchas

herdr 0.9.0:
- `workspace create --label` labels only the workspace; the root pane needs `pane rename`.
- `pane read` and `status server` print plain text, not JSON.
- herdr creates `~/.config/herdr/` (`.plugins.lock`, `release-notes.json`, `sessions/<name>/`).
- A standalone `agent wait` tracks the agent's lifecycle, not a turn: a worker that has not yet started its prompt reads as idle and returns at once. That is why the trial's `work` and `wait` waited for `working`.
- A typed `/quit` does not exit Codex; close the pane. `/exit` stops Claude.
- A Claude pane on claude1 turns on Remote Control by itself.
- Interactive Codex on `~/.codex1` uses the account's managed `codex app-server --managed-daemon`, which outlives its pane.

## Why the trial was built this way

- **Scratch root under `~/rust`.** No session could face a trust prompt, and none was answered for the user. The scratch root `/home/natepiano/rust/herdr-trial-scratch/` sat where the user could grant trust once on `repo/`, which then covered every scratch worktree.
- **One named herdr session, `herdr-trial`.** Nothing joined or disturbed a default herdr session.
- **Units launched by `trial.py launch`, not `add_unit.py`.** `add_unit.py` started tmux sessions when the trial was built. Each place the production commands assumed tmux became a finding.
- **`Promote: none`.** The fake production merged to its own branch and pushed to the bare origin; nothing reached `~/.claude`.
- **One work record per worker.** Three directors called `work` at once; one file each needed no lock.
- **The manifest outside the scratch tree.** Retries and `leftovers` still worked after the scratch tree was deleted.

## Ruled out

- A shared `work.json` with a lock: concurrent `work` calls from three directors lost an assignment.
- Konsole as the client: no window appeared.
- Opening prompts as `agent start` arguments: herdr refuses multi-line text.
- Trust from an ancestor folder: both agents still prompt.
- Removing the trust grants automatically.
- Counting any `codex app-server`, rather than `--managed-daemon`, as trial-started.
- Treating another user's unreadable processes as Codex clients.
- A `herdr-show` command over one shared session: no CLI, API or config way chooses an attached window's tab.

## Teardown

The trial ended 2026-10-13 15:13 EDT, when the user said they were done and want herdr for every production and standalone session. `trial.py teardown` exited 0. It closed workspaces `w8` and `w9` without sending a key, stopped the herdr server and both systemd units, and removed the scratch tree, `~/.config/herdr/sessions/herdr-trial` and `herdr-devcheck`, and the fake showrunner's notifier and showrunner state. It named five delegate mesh servers as `not a managed Codex daemon; retained` and left them running; all five were alive afterwards. `trial.py leftovers` then exited 0. The 20 logged issues are in this document; their archive and the cleanup manifest were deleted.

**Retained, by design:** Claude's `hasTrustDialogAccepted` for `repo/`, Codex's `trust_level=trusted` for `repo/`, and the Codex hooks trust in `~/.codex1/config.toml`. They are left for the user to remove.

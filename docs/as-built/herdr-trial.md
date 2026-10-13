# herdr trial: a fake production in herdr panes

## What it is

On 2026-10-13 a fake production ran inside one herdr 0.9.0 session on natedev. It had one showrunner and three unit directors, all Claude, plus six Codex workers (one writer and one reviewer per unit). Each agent ran in its own pane, and the user could watch them all in one ghostty window. The point was to find out what breaks before a real production moves off tmux. The work itself was tiny: three toy Rust units in a scratch repository with a bare local origin. Nothing was promoted to a real main branch, and the real `~/.claude` main was checked afterwards and holds nothing from the trial.

`scripts/herdr/trial.py` drives the whole trial. `fake_herdr.py` stands in for herdr in its 98 tests, which are in `test_trial.py`.

## What ran

| Step | When (EDT) | Result |
| --- | --- | --- |
| herdr installed via `nix shell`; server and client as systemd user units | 10:44 | `herdr-trial-server`, `herdr-trial-client` (ghostty titled `herdr-trial`) |
| Scratch production scaffolded | phase 2 | `repo/` on `herdr-fake`, bare `origin.git`, three unit worktrees |
| 10 agents launched in workspace `w9` | 13:23 | showrunner `p1`; per unit a director plus `-impl` and `-review` Codex panes |
| Fake production ran to completion | 13:23–14:10 | each unit's phase 1, shrink and as-built merged into `herdr-fake` and pushed to origin (`c7c92f2`) |
| Teardown and leftovers check written and tested | phase 5 | 98 tests |
| Trial torn down at the user's word | 15:13 | teardown exit 0; leftovers exit 0 |

## What the user saw

- One ghostty window, titled `herdr-trial` and moved to the requested desktop, showing every agent's pane at once. The first client, Konsole, ran for two hours with no visible window (issue 4).
- Trust prompts on first contact: Claude's folder trust, then Codex's folder trust and its "Hooks need review" screen. The user answered each one once, and the answers were saved, so later panes did not ask (issues 1, 3, 9).
- Real postmaster quota alerts appearing in the fake showrunner's pane (issues 13, 20).

## Findings, ranked by what a real herdr production would have to change

1. **Session identity and unit lookup.** Every production tool finds sessions through tmux or registered state. In the trial:
   - `production_lifecycle.py load` reported every director as `session: gone`, and `waiting.py quota` could not reach any of them (issues 5, 8).
   - `roster.py` listed none of the 10 agents at first. Once the showrunner's state registered, it listed the directors but filed them as `freestanding`, and it never listed the Codex pane workers (issues 7, 17).

   Fix before adopting herdr: the roster, the showrunner lookup and every tmux lookup need a herdr source. `herdr agent list` gives each pane's agent, status and pane id.
2. **Delivering a Work Order.** herdr does not treat Codex's folder-trust screen as blocked. Two Work Orders were pasted into panes that were sitting at that screen and were lost there. Each director then waited the full 30 minutes before resending (issues 10–12, 15). `trial.py work` now:
   - reads `agent get` and refuses a worker that is not `interactive_ready`;
   - confirms the turn started within `HERDR_TRIAL_TURN_START_TIMEOUT`.

   A real launcher needs both checks.
3. **Trust.** Interactive Codex asks for folder trust, keyed on the repository root, and then for hooks trust. Headless seats (`codex app-server`) ask for neither. A herdr production has to settle both before launch, or every new repository puts a prompt in front of the user (issues 1, 3, 9).
4. **Launcher.** herdr refuses multi-line start arguments (issue 2), so opening prompts go through `agent prompt` once the agent is ready. Konsole showed no window, and ghostty did (issue 4).
5. **The showrunner's view of its units.**
   - The fake showrunner reported workers as running because it read its directors' holding lines; the workers had never started (issue 14). Inside herdr it could read worker states directly, but no rule tells it to.
   - The merge script pushes and then fails at its promote step under `Promote: none`, and it has no as-built mode (issue 16). This is being fixed separately by the showrunner's fixer.
6. **Showing one unit.** The user asked for a command that opens the session showing one showrunner or unit. In herdr 0.9.0 every attached window keeps its own view. `agent focus slug-unit` moved herdr's recorded focus (`api snapshot` reported `focused_tab_id` `w9:t2`), but the open window did not move. A newly attached window opened on the showrunner's tab while herdr still reported slug-unit as focused. No CLI option, API field or config key selects the tab a window shows. A herdr production that wants `attach <unit>` needs one herdr session per unit and one for the showrunner. Within one session, the user switches in the window with Ctrl+b and the tab number, or Ctrl+b g for the goto picker.
7. **Relays reach trial sessions.** A Claude pane in herdr is an ordinary local session, so the machine's postmaster sends it real quota alerts (issues 13, 20). That is correct on a shared account. A trial that wants silence would need its own account.
8. **earlyoom and resume.** Every pane is a child of the one `herdr --session herdr-trial server` process. Losing that process (to earlyoom, a crash or a server stop) ends all 10 agents at once. Under tmux, each session is its own tree. Resuming after a server loss was not tried.

The showrunner's first lookup at 13:24 said "not running", one minute after launch. A later recheck found it. `showrunners.py` reads registered update-timer instances and session sockets, and neither depends on tmux, so the same first-minute miss is expected under tmux. That inference was not measured.

## Codex workers in panes compared with app-server threads

| | Interactive Codex in a herdr pane | Headless seat (`codex app-server` thread) |
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

## Teardown

`trial.py teardown` reads the cleanup manifest at `~/.local/state/herdr-trial/cleanup.json` (overridden by `HERDR_TRIAL_MANIFEST`), writing it on its first run. The manifest sits outside the scratch tree so that retries and `leftovers` still work after the scratch tree is deleted. Teardown then:
- copies `issues.jsonl` beside the manifest;
- closes every workspace without sending a single key;
- stops the herdr server and the recorded systemd units;
- stops only a managed Codex daemon that it recorded as started and that still has the same pid and start time, and only when no other Codex client of the current user uses the account;
- removes the recorded state paths by exact name, then the scratch root.

`trial.py leftovers` exits 0 only when nothing from the trial remains:
- It ignores Codex daemons from the baseline, daemons it never recorded, and delegate mesh servers.
- It reports the retained trust grants without changing them.

Review found that the first version would have recorded five other sessions' delegate mesh servers as trial-started. It would also have treated other users' unreadable processes as Codex clients. Both are fixed, with regression tests. Two narrower edges remain, found by the last review and left as written because this trial recorded no managed daemon, so neither can stop anything here: the outside-client scan runs once before all daemon stops rather than before each one, and any argv token `app-server` (even in a `codex exec` prompt) excludes that process from the outside-client check. Fix both before reusing this teardown elsewhere.

**Retained, by design:** Claude's `hasTrustDialogAccepted` for `repo/`, Codex's `trust_level=trusted` for `repo/`, and the Codex hooks trust in `~/.codex1/config.toml`. Teardown reports these and leaves them for the user to remove.

**Status:** ended 2026-10-13 15:13 EDT, when the user said they were done and want herdr for every production and standalone session.
- `trial.py teardown` exited 0. It closed workspaces `w8` and `w9` without sending a key, stopped the herdr server and both systemd units, and removed the scratch tree plus the recorded state:
  - `~/.config/herdr/sessions/herdr-trial` and `herdr-devcheck`;
  - the fake showrunner's notifier and showrunner state.
- It named the five delegate mesh servers as `not a managed Codex daemon; retained` and left them running. All five were confirmed alive afterwards.
- `trial.py leftovers` then exited 0, listing only the three retained trust grants above.
- The 20 logged issues are in this document. Their archive and the cleanup manifest under `~/.local/state/herdr-trial/` were deleted after the checkpoint notice.
- **`scripts/herdr/` is kept.** The user is moving productions and standalone sessions to herdr, and these helpers are the tested starting point: server and client as systemd units, readiness before a prompt, turn-start confirmation, and a teardown that never signals another session's processes. The trial-only parts (scaffold, fake units, scratch paths) go when that work replaces them.

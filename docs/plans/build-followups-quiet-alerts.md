# Quiet Remote Control and probe sessions

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Nothing tells the user that Remote Control is off, and a probe session started for a live check or test can never push to the phone.

> **As-built disposition: amend** — `docs/as-built/shutdown-and-restart.md` (Remote Control, Settling, Results, live check), `docs/accounts.md` (exec), `docs/as-built/herdr-adoption.md`, `docs/as-built/session-roster.md`

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Project started:** 2026-10-15T20:36:30.143+00:00
- **Stack:** Python 3.13 standard library, checked by basedpyright. No `Any`, a `TypedDict` per JSON shape, no file-level ignores.
- **Source:** the user, via the showrunner, 2026-10-15: "i want you to stop sending messages about a claude session not having phone access - we use enough different accounts now that i will worry about remote control being on or off myself - you don't need to". And: a holdout push `pm-move-ghostty Restart is blocked` (priority 1, 11:29:55 EDT) reached the user from a throwaway live-check session, because that `accounts.py move` ran before `--no-alert` existed, so its shutdown record carried `alerts: "user"`. The showrunner: live checks and tests must never push to the phone, enforced, with a test.
- **Key files** (find by name; line numbers move):
  - `scripts/shutdown/remote_control.py` — `alert_failed` (~203, pushes `shutdown.remote_control` through `settle.alert_user`), `fix` (~210, calls it on `Failed` when `alerts == "user"`), `launch_fixer` (~98, passes `--alerts` ~106), `_fix`.
  - `scripts/shutdown/restart.py` — `remote_control_missing` (~438-458: prints `<name>: Remote Control not on yet; turning it on in the background` ~457, and on a fixer launch failure `<name>: Remote Control is off; turn it on at its terminal with /remote-control` ~453 plus `alert_failed` ~455); `_confirm_entries` (~1815) calls it; peer wire `no_remote_control` (~1977-1980) and `parse_peer_result`; `_resume_parts` (~545-562, env assignments in resume argv); `up` alerts (~2200-2220), `_restart_alerts_enabled` (~2089).
  - `scripts/shutdown/settle.py` — `begin` (~739-772, builds `SessionShutdown(alerts=alerts)`), `alert_user` (~346) → `_deliver_message` (~276) → `send.py --to user --text-id …`; conductor relaunch pass calls `remote_control_missing` (~1500); `_holdout_due` (~1505, gated on `saved["alerts"] == "user"`), `_send_holdout_alert` (~1565); `report_stop` (~1295, ~1342-1359).
  - `scripts/shutdown/shutdown.py` — remote-control fixer subcommand: epilog (~657), parser `--alerts` (~712), dispatch (~770).
  - `scripts/shutdown/record.py` — `NoRemoteControl` (~33-36, has `alerts`), `SessionShutdown` (`alerts` field ~378).
  - `scripts/shutdown/inventory.py` — `Session` / `SessionFields`; `_process_environment(pid)` (~1050; Linux `/proc/<pid>/environ`, Mac `ps eww`) already reads each live session's env for `CLAUDE_CONFIG_DIR`.
  - `scripts/accounts/accounts.py` — `exec` subcommand (launches `claude` under an account; `-n <name>`).
  - `scripts/message/send.py` — `--to user` path (~976 runs `scripts/notify/pushover.py`).
  - `scripts/notify/push_catalog.py` — `PushText("shutdown.remote_control", …)` (~57-62). `scripts/notify/push_texts_reviewed.json` — its `shutdown.remote_control` block (~289-365).
  - Tests: `scripts/shutdown/test_remote_control.py`, `test_restart.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_send_guard.py`, `test_support.py` (`ShutdownTestCase` blocks every send leaf; `_block_launch_remote_control` ~148), `test_account_move.py`; `scripts/notify/test_push_texts.py`, `scripts/notify/test_review.py` (~799 uses `shutdown.remote_control` as a literal id); `scripts/accounts/test_*.py`; `scripts/message/test_*.py`.
- **Test lanes:** tests live beside the code in each `scripts/<dir>/test_*.py`.
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/<dir> -p 'test_*.py' </dev/null` for each of `shutdown`, `accounts`, `lib`, `production`, `whoami`, `notify`, `message`.
- **Lint:** `basedpyright <changed .py files>`; pass is the last line `0 errors, 0 warnings, 0 notes` (exit 3 from the missing `.venv`).
- **Invariants:** tests start no session and send no message or push; a Claude process gets only SIGTERM after its identity check; times shown to the user are in `user_zone()`; every user-facing line naming a command names where to run it; a changed push text is re-approved with `scripts/notify/review.py texts --id … --approve`; command files (`commands/*.md`) are edited per `commands/succinct_style.md`.

## Phases

### Phase 1 — Remote Control is never reported; a probe session never pushes  · status: done

#### As-built

- A session left without Remote Control after a restart or move is never reported: no phone push, no message, no printed line. `restart.remote_control_missing` launches the quiet fixer for each confirmed session still missing it, and a fixer that cannot start goes to syslog. The `shutdown.remote_control` push text is gone from the catalog and the approvals file. `restart._up`, `up_result` and `up` take no `notify` argument.
- `accounts.py exec --probe <account>` sets `CLAUDE_PROBE_SESSION=1` (`PROBE_ENV` in `scripts/lib/probe_session.py`). Inventory reads `Session.probe` as true only for the exact value `1`. `settle.begin` stores `probe: true` and forces `alerts: "none"` for a probe. Records without the field read `probe: false`.
- Restart and move keep the marker: `_resume_parts` exports it, and a tmux unit restore passes `add_unit.py --probe`, which writes the marker into the final shell command.
- `send.py --to user` under the marker prints `not sent: probe sessions never push to the phone` and exits 0 before review, logging or delivery.

**Files:**
- `scripts/lib/probe_session.py` — `PROBE_ENV`
- `scripts/accounts/accounts.py` — `exec --probe`
- `scripts/shutdown/inventory.py`, `record.py`, `settle.py` — the probe field and the forced `alerts: "none"`
- `scripts/shutdown/restart.py`, `scripts/production/add_unit.py` — the quiet fixer; the marker carried through resume and unit restore
- `scripts/shutdown/remote_control.py`, `scripts/notify/push_catalog.py`, `push_texts_reviewed.json` — no Remote Control alert
- `scripts/message/send.py` — the probe refusal

**Gotchas:** `add_unit.launch_environment` strips every `CLAUDE_*` variable, so only the `--probe` flag carries the marker into a unit restore. `exec <account> --probe` passes `--probe` to claude, unmarked. `PeerRestartResult` keeps `"no_remote_control": []`, and `up --no-alert` stays accepted, for older peers.

**Ruled out:** a probe ledger (a manual step); a name prefix (collisions); filtering an older peer's Remote Control lines (transition-only, and a text-match workaround).

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

#### Work Order

**Goal:** no push, message or printed line tells anyone that Remote Control is off or failed to turn on, and a session launched as a probe can never cause a phone push, whatever flags a later command forgets.

**Spec:**

*Remote Control (the user's rule):*
1. Delete `remote_control.alert_failed` and every call to it (`fix` on `Failed`, `restart.remote_control_missing` on a fixer launch failure). The fixer still runs: `remote_control_missing` still starts `launch_fixer` for each confirmed local session that lacks Remote Control, and `fix`/`_fix` keep retrying and writing their journal lines; the fixer CLI may still exit 1 on `Failed` (it reaches only the systemd/launchd log). A fixer launch failure is swallowed after one journal or stderr line in the conductor's own log, never a user-facing line.
2. Remove the `alerts` parameter end to end from the fixer: `launch_fixer`, `fix`, the `--alerts` CLI option and its epilog text in `shutdown.py`, `record.NoRemoteControl.alerts`. If `NoRemoteControl` has no other remaining use, delete the type.
3. Remove both printed lines from `remote_control_missing`: `Remote Control not on yet; turning it on in the background` and `Remote Control is off; turn it on at its terminal with /remote-control`. These lines reach the requester and the showrunner through `report_stop` and peer `lines`, so they are messages. A restart's summary and exit status are unchanged by Remote Control state.
4. Peer wire: new code always emits `"no_remote_control": []`, so an older peer's `parse_peer_result` (which requires the key, with `alerts` on each entry) still accepts it; the new parser ignores the key whether present, empty or carrying old entries.
5. Delete the `shutdown.remote_control` `PushText` from `scripts/notify/push_catalog.py` and its block from `scripts/notify/push_texts_reviewed.json`. Switch `scripts/notify/test_review.py`'s literal `shutdown.remote_control` id to another catalog id.
6. Out of scope (author's choice): `add_unit.py` `wait_for_remote_control` (a launch gate that fails a new unit launch, not an alert), `sessions.remote_control()`, the `--remote-control` launch argument, `stall_watch`, `agent_bg.sh`.

*Probe sessions never push (the showrunner's rule):*
7. `accounts.py exec --probe <account> …` launches `claude` with `CLAUDE_PROBE_SESSION=1` in its environment. Constant `PROBE_ENV = "CLAUDE_PROBE_SESSION"` lives in one shared place both `accounts.py` and the shutdown scripts import (the shutdown package's `record.py` or `scripts/lib`, whichever the import graph already allows; no import cycle). `--help` text: `--probe  mark the session as a throwaway probe: no shutdown, restart or move for it ever pushes to the phone`.
8. `inventory` reads the marker from the same per-pid environment it already reads for `CLAUDE_CONFIG_DIR` into `Session.probe: bool` (value exactly `"1"` → True; absent or anything else → False; an unreadable environment → False).
9. `SessionShutdown` gains `probe: bool`. `settle.begin` sets it from the inventory session and, when `probe` is True, stores `alerts="none"` whatever the request passed. Old records without the key read `probe=False`. Every existing gate already honors `alerts` (holdout, `report_stop`, restart pushes), so no gate changes.
10. `restart._resume_parts` puts `CLAUDE_PROBE_SESSION=1` in the resume environment for a record with `probe=True`, so a restarted or moved probe stays a probe.
11. `send.py --to user` sends nothing when its own environment carries `CLAUDE_PROBE_SESSION=1`: it prints `not sent: probe sessions never push to the phone` and exits 0, before any review, log write or pushover call. This covers a probe calling `/alert_user` itself.
12. `restart.up`'s pushes (`_restart_alerts_enabled`) follow the records' `alerts`; a probe record's `"none"` therefore silences them with no further change.

*Docs:*
13. `docs/as-built/shutdown-and-restart.md`: Remote Control sections (~151, ~159, ~177, ~265) describe the quiet fixer and no alert; the live-check section launches every probe with `accounts.py exec --probe <account> -n <name>`; state that a probe record always has `alerts: "none"`. `docs/accounts.md`: `exec --probe`. `docs/as-built/herdr-adoption.md` (~159-161) and `docs/as-built/session-roster.md` (~325, the send-leaf list): drop the Remote Control push. `commands/shutdown.md` (~27, ~29): drop the Remote Control alert and the `--alerts` fixer option, per `commands/succinct_style.md`.

**Files:**
- `scripts/shutdown/remote_control.py` — items 1–2.
- `scripts/shutdown/restart.py` — items 1, 3, 4, 10.
- `scripts/shutdown/settle.py` — items 1, 9.
- `scripts/shutdown/shutdown.py` — item 2.
- `scripts/shutdown/record.py` — items 2, 7, 9.
- `scripts/shutdown/inventory.py` — item 8.
- `scripts/accounts/accounts.py` — item 7.
- `scripts/message/send.py` — item 11.
- `scripts/notify/push_catalog.py` — item 5 (owner message-clarity-unit; edited at the showrunner's direction).
- `scripts/notify/push_texts_reviewed.json` — item 5.
- `docs/as-built/shutdown-and-restart.md` — item 13.
- `docs/accounts.md` — item 13.
- `docs/as-built/herdr-adoption.md` — item 13.
- `docs/as-built/session-roster.md` — item 13.
- `commands/shutdown.md` — item 13.
- `scripts/shutdown/test_remote_control.py` — tests.
- `scripts/shutdown/test_restart.py` — tests.
- `scripts/shutdown/test_settle.py` — tests.
- `scripts/shutdown/test_shutdown_cli.py` — tests.
- `scripts/shutdown/test_send_guard.py` — tests.
- `scripts/shutdown/test_support.py` — tests.
- `scripts/notify/test_push_texts.py` — tests.
- `scripts/notify/test_review.py` — tests.
- `scripts/accounts/` — the `exec` tests in its `test_*.py`.
- `scripts/message/` — the `send.py` tests in its `test_*.py`.

**Workers:** 1 writer + 1 tester.
- `impl` — every non-test file above, including the docs; hub: `scripts/shutdown/record.py` (shared `PROBE_ENV` and the record field).
- `test` — every test file above. Rewrite the existing Remote Control tests to assert the fixer still launches and retries but nothing pushes, messages or prints a Remote Control line, and that the CLI has no `--alerts`; `test_push_texts.py` asserts no `shutdown.remote_control` id remains in the catalog or the approvals file. New tests: (a) `accounts.py exec --probe` puts `CLAUDE_PROBE_SESSION=1` in the launched env and without `--probe` it is absent; (b) inventory reads `probe` True only for value `"1"`; (c) `settle.begin` with a probe session and requested `alerts="user"` stores `alerts="none"`, `probe=True`, and a due holdout for it sends nothing; (d) an old record without `probe` loads as False; (e) `_resume_parts` for a probe record carries `CLAUDE_PROBE_SESSION=1`, a non-probe record does not; (f) `send.py --to user` under `CLAUDE_PROBE_SESSION=1` prints the not-sent line, exits 0 and never invokes pushover or writes its log; (g) the peer wire emits `"no_remote_control": []` and the new parser accepts a payload with old `no_remote_control` entries carrying `alerts`.

**Constraints from prior phases:** move-without-consent shipped: an idle session is stopped with no pause; `accounts.py move --no-alert` adds `--no-alert` to each `down`; the conductor fingerprints six shutdown modules and re-execs itself on a change, so an install reaches a running shutdown without a restart.

**Acceptance gate:** every test command in Delegation Context green; `basedpyright` clean on every changed `.py`; tests (a)–(g) present and passing; `grep -rn 'shutdown.remote_control\|alert_failed' scripts` finds nothing outside removed-code history.

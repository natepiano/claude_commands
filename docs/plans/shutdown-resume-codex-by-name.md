# Resume by name and recovery on the right account, in the right tab

> **Production:** sdlc. Owner: shutdown-unit. Starts after the Phase 10 hash.

## Goal
1. `resume <name>` finds a saved Codex conversation by its exact name, and resumes it on the configured workers account with its history.
2. A closed showrunner or unit starts on its role account on the first start. No watchdog move follows.
3. A restart with no saved tab opens in the parent showrunner's workspace.
4. Work that was shut down stays down: the watchdog restarts only a unit whose newest launch record exists and is not `ended`.

## Work
1. `scripts/shutdown/stop_resume.py` `_resume`: look up an unknown name by exact match in the shared `session_index.jsonl`. One id resolves. More than one id gives the existing "ambiguous name; candidates" failure.
2. One function picks the target account for both families. It reads the role at invocation, for a staff row and for a session with no row. `--to` overrides it. The source account does not count.
3. `running_on_account` leaves a session that already runs unchanged.
4. One function picks the workspace: the row's parent showrunner, else the entry name, else the shared session. `stop_resume._resume_once` and `restart.start_row` both call it. `start_row` passes the result as the fallback when no saved tab exists.
5. `scripts/accounts/unit_watchdog.py`: a unit with no launch record, or with a newest record that is `ended`, gets no restart. Delete the no-record restart path. Correct `test_crashed_unit_without_a_pane_launch_record_restarts_on_its_launch_account` and `test_public_stop_keeps_a_unit_down_across_watchdog_ticks` to state the new rule.

## Accepted risk
A crashed unit with no launch record stays down until someone resumes it. sdlc accepted this on 2026-11-06.

## Proof, on TEST sessions only
Never brain or morty. Do not move namecheck's pane.
1. A TEST Codex session named `zz-move-*` starts on one account. It resumes by name on the workers account, with a thread row only on the other account, and recalls a word from its history.
2. A closed TEST showrunner with an old pane record resumes by name through the script and starts on its role account the first time.
3. A closed TEST unit with no saved tab restarts in its parent showrunner's workspace.
4. A TEST unit with an `ended` record and a TEST unit with no record stay down across watchdog passes.

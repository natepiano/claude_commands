# Resume by name and recovery on the right account, in the right tab

> **Production:** sdlc. Owner: shutdown-unit. Status: done, merged as cdf3e0ea8.

## As-built
1. `resume <name>` looks up an unknown name by exact match in the shared `session_index.jsonl` of the Codex home. One id resolves. More than one id gives the "ambiguous name; candidates" failure. A canonical UUID is used as the id and reads no index.
2. `stop_resume.resume_account` picks the target account for both families. It reads the role at invocation: a Codex session takes the workers role, and a unit or showrunner row takes its role. Other Claude sessions keep their saved account. `--to` overrides it for both families. The source account does not count.
3. A herdr server restore passes the saved home to `restart.start_row` as an `AccountSelection`, so the session keeps its saved account.
4. `running_on_account` leaves a session that already runs unchanged, and prints `unchanged` with its account. In an unknown process, a pane that reports the session id counts as running.
5. `herdr.row_workspace` picks the workspace: the row's parent showrunner, else the entry name, else the shared session. `stop_resume._resume_once` and `restart.start_row` both call it.
6. `scripts/accounts/unit_watchdog.py` restarts only a unit whose newest launch record exists and is not `ended`.
7. `resume_unit` completes the director's resume first: it writes the receipt and discards the parked facts. It then restores the workers and returns the worker's exit code. A rerun restores the workers.

## Known limits
1. A crashed unit with no launch record stays down until someone resumes it. sdlc accepted this on 2026-11-06.
2. A session in an unknown process, with no pane that reports its id, can get a second process on the same conversation.
3. The role account step does not change a pane shell whose `resume` alias holds an old `CLAUDE_CONFIG_DIR`. A herdr restart or a reboot clears those shells.

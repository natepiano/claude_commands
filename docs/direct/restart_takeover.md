# Restart takeover

<RestartTakeover>
Apply this contract after a director restarts, compacts, moves, or changes its name.

1. Read the staff row and the selected command file.
2. Receive saved messages with the command for your agent:

   | Agent | Command |
   | --- | --- |
   | Claude | `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py pending` |
   | Codex | `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py pending "codex:$CODEX_THREAD_ID"` |

3. Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/worker.py restore`.
4. Read each listed result that you did not act on yet.
5. Check each operation's evidence before repeating it: commit, push receipt, gate record, or build artifact.
6. Repeat only the work that the evidence proves incomplete.

Restore reaps detached orphans before it resumes workers.
A full restore reaps once per held run folder.
A limited restore reaps once per selected worker in each held run folder.
It preserves excluded workers and workers with unknown pane evidence.
It then lists completed passes in open phases, newest first, with worker, phase, dispatch id, and reply path.
An absent reply path prints `unavailable`.
The JSON worker list also supplies the dispatch id, run path, completion state, and reply path.
Repeated restore calls list the same results while their phases remain open.

Keep busy workers and live remote builds running.
Keep unknown workers unchanged and report the missing evidence.
Keep the same worker id and dispatch id.
An absent local supervisor does not prove that a remote build or push stopped.
</RestartTakeover>

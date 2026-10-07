---
description: Wind Codex agents down on every showrunner and count the ones still running, sort them into stop-now and finish, or give the all clear. Args - `start`, `triage`, `clear`, or none for the count now.
argument-hint: "[start|triage|clear]"
---

`$ARGUMENTS` is `start`, `triage`, `clear`, or empty. Run the line and show its output unchanged. With any other argument, name the choices and stop. Run `start`, `triage` or `clear` only on the user's word.

1. Empty: `python3 ~/.claude/scripts/production/codex_winddown.py status`. It prints whether a wind-down is on and every session's running Codex agents on this machine, and changes nothing.
2. `start`: `python3 ~/.claude/scripts/production/codex_winddown.py start --from <your session name>`. Every showrunner on this machine is told to let its running Codex agents finish and launch no new one, and prints `<session> - <count> - <projected finish>` for its unit directors, in alphabetical order, every 2 minutes. The count asks each unit director with a running agent for the finish; until it answers the line reads `asked, answer owed`. When no Codex agent is left on the machine, the user gets one phone notification.
3. `triage`: `python3 ~/.claude/scripts/production/codex_winddown.py triage --from <your session name>`. Every showrunner has its unit directors stop each running Codex agent that can stop, onto a resume list, and let the rest finish.
4. `clear`: `python3 ~/.claude/scripts/production/codex_winddown.py clear --from <your session name>`. The user's all clear: the counts stop and every showrunner is told Codex agents may launch again.

A showrunner that receives these messages:
- **Wind-down:** pass it to your unit directors, and launch no Codex agent until the all clear. Unit directors are encouraged to continue work on their own.
- **Codex count:** run the command it names and paste its output word for word as the reply.
- **Triage:** pass it to every unit director with a running Codex agent.
- **All clear:** pass it to your unit directors.

A unit director asked for its finish runs the `eta` line the message names, and again when its estimate changes. When its count reaches 0 it is told at once that it may work on its own or launch Claude agents for short time frames.

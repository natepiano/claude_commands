---
description: Wind Codex agents down on every showrunner and count the ones still running, or give the all clear. Args - `start`, `clear`, or none for the count now.
argument-hint: "[start|clear]"
---

`$ARGUMENTS` is `start`, `clear`, or empty. Run the line and show its output unchanged. With any other argument, name the choices and stop. Run `start` or `clear` only on the user's word.

1. Empty: `python3 ~/.claude/scripts/production/codex_winddown.py status`. It prints whether a wind-down is on and every session's running Codex agents on this machine, and changes nothing.
2. `start`: `python3 ~/.claude/scripts/production/codex_winddown.py start --from <your session name>`. Every showrunner on this machine is told to let its running Codex agents finish and launch no new one, and prints `<session> - <count>` for its unit directors, in alphabetical order, every 2 minutes.
3. `clear`: `python3 ~/.claude/scripts/production/codex_winddown.py clear --from <your session name>`. The user's all clear: the counts stop and every showrunner is told Codex agents may launch again.

A showrunner that receives these messages:
- **Wind-down:** pass it to your unit directors, and launch no Codex agent until the all clear.
- **Codex count:** run the command it names and paste its output word for word as the reply.
- **All clear:** pass it to your unit directors.

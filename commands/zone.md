---
description: Show or change the user's time zone, everywhere, in one command.
---

**Usage:** `/zone [place, IANA zone or label]`

**Arguments**: $ARGUMENTS

Every time shown to the user is written in one zone, `config/user_zone.conf`.
This command shows it or changes it, on both machines.

## Steps

1. With no argument, run
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/zone.py"` and
   show its output. It names the zone, its label, its UTC offset and the time
   now. If a change waits for the Mac, it sends it.
2. With an argument, run
   `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/zone.py" set "<argument>"`
   on natedev, with the sandbox off. It takes an IANA zone (`America/Curacao`),
   the city of one (`Curacao`, `New York`), or a label only one zone uses. Then it:
   - writes the zone in `config/user_zone.conf`;
   - commits that file on `~/.claude` main and pushes it;
   - pulls the Mac's `~/.claude`, or queues the pull when the Mac is offline;
   - tells every session on both machines with `broadcast.py --all`;
   - prints the new zone and the time now.
3. Exit 2 means nothing changed. Read the message:
   - **No zone has that name** (a city such as `Seattle`): find the IANA zone
     of the place (`America/Los_Angeles`) and run step 2 again with it.
   - **The name fits more than one zone** (a label such as `AST`, or a city two
     zones share): pick the zone of the place the user named, if they named one,
     and run step 2 again. If they did not, ask in one line and name two or
     three of the zones it printed.
   - **The checkout is not ready** (not on main, or main and origin/main have
     both moved): report the line. Do not repair the checkout.
4. Exit 1 means the zone changed, but a push or some deliveries failed. Report
   the lines that say so.
5. Report in one or two lines: the new zone with its label, the time now, and
   whether the Mac pulled it or waits. Write that time in the new zone.

A queued Mac change goes out at the next bare `/zone` or the next promotion's
Mac pull.

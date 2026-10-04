---
description: Change how often a running production's scheduled updates come, and restart its notifier instance.
argument-hint: "[minutes]"
---

# Interval

Changes how often the scheduled updates come. Run it in the showrunner session
during `/showrunner:produce`, whose state (`PRODUCTION_DOC`, `PROMPT_FILE`,
`LOG`, `ZONE`, `CHECKOUT`, `NOTIFIER`, `UPDATES`) it uses.

**Usage:** `/showrunner:interval [minutes]`. With no argument, run
`NOTIFIER status UPDATES`, report the interval and the next tick, and change
nothing. With anything but a whole number of minutes above 0, say so and stop.

1. **Change it.** With the Edit tool, change the production doc's
   `**Updates:** every N minutes` line and `every N minutes` in `PROMPT_FILE`.
   Run `NOTIFIER interval UPDATES <minutes>`; it restarts the instance and
   prints `next_due`. When it refuses (exit 2), report what it names and stop.
2. **Commit** the production doc in `CHECKOUT` as
   `production(<name>): updates every <minutes> minutes`. It goes out with the
   next push of the merge branch.
3. **Log** one line in `LOG`: the old and new interval, and the next tick.
4. **Tell the user** in one line: the new interval and the next tick, in `ZONE`
   and UTC.

The new interval holds from then on: a `/showrunner:dailies` the user runs
restarts the instance at it (`/showrunner:dailies` → Status check and clock).

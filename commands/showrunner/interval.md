---
description: Change how often a running production's scheduled updates come, and restart the update timer so the next one is that many minutes from now.
argument-hint: "[minutes]"
---

# Interval

Changes how often the scheduled updates come. Run it in the showrunner session
during `/showrunner:produce`, whose state (`PRODUCTION_DOC`, `LOG`, `ZONE`,
`CHECKOUT`, `TIMER_CONF`, `TIMER`) it uses.

**Usage:** `/showrunner:interval [minutes]`. With no argument, run
`TIMER status TIMER_CONF`, report the interval and the next tick, and change
nothing. With anything but a whole number of minutes above 0, say so and stop.

1. **Change it.** Run `TIMER interval TIMER_CONF <minutes>`. It sets the
   production doc's **Updates** line and the tick prompt to the new interval,
   then restarts the timer, so the next tick comes `<minutes>` minutes from now.
   When it refuses (exit 2), report what it names and stop.
2. **Commit** the production doc in `CHECKOUT` as
   `production(<name>): updates every <minutes> minutes`. It goes out with the
   next push of the merge branch.
3. **Log** one line in `LOG`: the old and new interval, and the next tick.
4. **Tell the user** in one line: the new interval and the next tick, in `ZONE`
   and UTC.

The new interval holds from then on: a `/showrunner:dailies` the user runs
restarts the timer at it (`/showrunner:dailies` → Status check and clock), and
so does every resume.

---
description: Set the production report interval.
argument-hint: "[minutes]"
---

# Report interval

This command changes the interval for scheduled updates.
Run it in the showrunner session during `/showrunner:produce`.
It uses `PRODUCTION_DOC`, `LOG`, `ZONE`, `CHECKOUT`, `NOTIFIER`, and `UPDATES` from that session.

**Usage:** `/showrunner:report_interval [minutes]`.
Without an argument, run `NOTIFIER status UPDATES`.
Report the interval and the next tick.
If the document specifies `on demand`, report that setting.
An interval must be a whole number of minutes above zero.
Report an invalid argument without changes.

1. Change the production document's `**Updates:**` line to `every N minutes`.
2. Add that line under Production Context if the line is absent.
3. Run `NOTIFIER interval UPDATES <minutes>`.
4. Report any refusal from that command.
5. Commit the production document in `CHECKOUT` with `production(<name>): updates every <minutes> minutes`.

The interval command restarts the timer and prints `next_due`.
A refusal has exit code 2 and ends this command.
The document commit accompanies the next push of the merge branch.
A document that specified `on demand`, or had no Updates line, has a stopped timer.
For that timer, continue after the document commit:

1. Run `update_registration.py register` as `/showrunner:produce` does at start.
2. Run `NOTIFIER start UPDATES`.

Registration sets the timer interval.
The start command enables the timer and prints `next_due`.
Each tick builds its prompt from the owner's current staff row and production document.

1. Record the old interval, the new interval, and the next tick in `LOG`.
2. Report the new interval and next tick in `ZONE` and UTC.

A user-requested `/showrunner:dailies` restarts the timer with the new interval.

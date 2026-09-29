---
description: Report a running production's state to the executive producer (the user) — every unit and every open topic — at one of three lengths, simple (default), page or elaborate.
argument-hint: "[simple|page|elaborate]"
---

# Dailies

In film, dailies are what the executive producer watches each day to see how
the shoot is going. Here the showrunner reports the production to the executive
producer: the user. Run it in the showrunner session during `/showrunner:produce`,
whose state (`PRODUCTION_DOC`, `LOG`, `ZONE`, `UNITS`, `CHECKOUT`,
`MERGE_BRANCH`) it uses.

**Usage:** `/showrunner:dailies [simple|page|elaborate]`. With no argument,
`simple`. With any other argument, name the three choices and stop.

## Status check and clock

While `/showrunner:produce` runs scheduled updates (its `SCHEDULE_ID` in `LOG`),
a dailies the user runs takes the next tick's place. N is the production doc's
**Updates** interval. Two steps do that:

1. **Check every unit.** Before Gather, run the status script that the
   scheduled-update prompt names. It checks every unit, each time: that its
   session and Claude are running, any form or decision waiting on the user,
   and its latest step and ETA. Put anything it flags first (SESSION GONE,
   CLAUDE NOT RUNNING, FORM WAITING, a usage limit, a DECISION), as a scheduled
   tick does.
2. **Reset the clock.** After the report, restart the schedule so the next tick
   comes N minutes after this report. CronList to find the job. CronDelete it.
   CronCreate it again with the prompt the last scheduled tick delivered, word
   for word, and the minute field `<current minute mod N>-59/N`: at 19:21 with
   N = 15, that is `6-59/15 * * * *`, so the next tick is 19:36. Log the new
   `SCHEDULE_ID`.

A scheduled tick skips both steps. It has run the script already, and a restart
at its own fire time would move the schedule by the scheduler's delay each tick.

## Gather

Read the current state, not memory, and check what you state the way
`/showrunner:produce` checks a unit's claims.

1. **Time.** `TZ=<ZONE> date '+%H:%M %Z'; date -u '+%H:%M UTC'`.
2. **Log.** The latest `### STATE` block in `LOG` and every event after it.
3. **Each unit.** Capture its pane (`tmux capture-pane -p -t <session> -S -60`):
   its phase, what it is doing now, its latest phase ETA, and any `— decision:`,
   `— blocked:` or form waiting. Text after `❯` may be a prompt suggestion, not
   the user's draft.
4. **Merge branch.** Its last merge, whether it is pushed, and anything held or
   testing.
5. **Open topics.** Everything in `LOG` not yet closed: held quota alerts, CI,
   defects routed between units, gates, and items waiting on the user.

## Subjects

Each unit, earliest `eta:` first, so the order itself shows who lands next; a
unit with no ETA goes after those with one. Then each open topic; then the
merge branch, only when a merge is held, testing or not pushed. A topic that
closed since the last report gets none. A subject that needs the user goes
first, ahead of the ETA order.

A topic that only waits on one unit's phase gets no section of its own. It goes
under that unit as a `waiting on it:` line and shares the unit's `eta:`.

## Output

```
**Dailies (<Simple | Page | Elaborate>)**, <ZONE time> / <UTC time>

### <unit>, Phase <N> of <M>
- update: <what it is doing now>
- eta: <HH:MM ZONE> (unchanged | changed: ±h:mm) | none measured | no ETA stated yet
- waiting on it: <a topic that lands with this phase, and who waits>
- needed: <the follow-up, and who does it>
- then: <what the unit does after this, when it is not simply the next phase>

### <open topic>
- update: …
- eta: …

next run at <HH:MM> - nothing needed
```

- **First line:** names the length used, capitalized: `**Dailies (Simple)**, 19:05 PDT / 02:05 UTC`.
- **Header:** one section header per subject, every unit reported in full.
- **Title:** a unit title gives `Phase <N> of <M>` from its plan. Work outside a numbered plan gives its place in the unit's queue and what it is: `follow-up 1 of 3 (wording and look polish)`.
- **update:** what the subject is doing now.
- **eta:** always present. The unit's latest stated phase ETA, converted to
  `ZONE`; for a topic, when it lands. Compare it with the ETA the last report
  gave that subject (the `LOG` line below) and add one note:
  - `eta: 18:38 PDT (unchanged)`
  - `eta: 19:05 PDT (changed: +0:27)` or `(changed: -0:10)`
  - `eta: 18:38 PDT (unchanged, overdue)` when the time has passed with no new one
  - no note on a subject's first ETA

  When there is none, never make one up. Send that unit `/unit:eta` in this turn
  (the unmeasured-ETA rule in `/showrunner:produce`) and show
  `eta: none measured - requested`.
- **waiting on it:** only for a topic that lands with this unit's phase, e.g.
  `waiting on it: the rear-card fix; trunk merges it and re-runs its one red test`.
- **needed:** only when the subject needs a follow-up that nobody has started:
  from you (the user), the showrunner or another unit. Say who.
- **then:** the section's last line, only when what comes next is not simply
  the plan's next phase. Two cases:
  - The unit is on work inserted ahead of its plan (a follow-up, or a phase
    added mid-run) while plan phases are still open. Name the plan phase it
    goes back to, by number and what it does, and anything queued before it:
    `then: precompose redesign and dimming, then back to the plan at Phase 41
    (new tools placed by the arrangement engine)`.
  - The unit is on its plan's last phase and has more work queued: name it and
    its source: `then: the hana_organon plan (docs/hana/hana-organon-design.md),
    six phases, once this phase is merged`. With nothing queued, `then: nothing
    queued`.

  Read the plans and design docs for this, not the unit's queue alone. A unit's
  own handoff may list only the work in front of it.
- **Timeline:** after the last section and before the last line, a code block
  drawing when each unit's phase lands, rows in the same order as the sections.
  One column per 5 minutes from now (widen the step when the latest ETA is more
  than 4 hours out, so no row passes about 50 columns). An axis row of hour
  marks, then one row per unit: `█` from now to its ETA, `░` from the ETA to
  the top of its range, `→` where a range runs past the axis end, then the ETA
  and range as text. A unit with no ETA gets `?` and no bar.

  ```
           20:00       21:00       22:00       23:00
  trunk    ███████ 20:30
  GM       █████████████████████████████████░░░░→ 22:40 (to 04:59)
  widget   ██████████████████████████████████ 22:43
  ```
- **Last line:** always the next scheduled run, from the schedule after any
  restart: `next run at 20:10 - nothing needed` when no subject has a `needed:`,
  or `next run at 20:10` when one does. With no schedule running, `no run
  scheduled - nothing needed`.
  The report replaces the turn's `— waiting on:` line.

After the report, write one `LOG` line with each subject's ETA as reported, so
the next report can compare: `- HH:MM <zone>: dailies ETAs: <subject> <eta>; …`.

## Length

| Argument | `update:` gets |
| --- | --- |
| `simple` | One short line. For when the user is already following along. |
| `page` | The `simple` line plus one more sentence of brief context: what a named thing is (a helper, seat, round or check) and why it matters now. Example: "fifth pass on the app's wording has started; the fourth left 16 tests expecting the old words. Trunk hands each pass to a short-lived helper agent, and each can run out of room partway, so the work takes several passes." |
| `elaborate` | More on each subject: what is moving or at risk gets the most, what is only waiting the least. Up to two pages for the whole report, and only as long as the state needs. The user asks when they want more. |

Every length keeps the same output: the same headers, `update:`, `eta:`,
`needed:` and last line. Only the `update:` text grows.

For every length:
- Plain words. Technical terms are fine where they are the right ones.
- Name work by what it changes in the app, never by a unit's own labels: no helper
  names (`look-b5`), batch letters or item numbers. The user does not see them.
- Say "you" for the user.
- Do no other work in this turn, except the `/unit:eta` requests and the two
  steps in Status check and clock.

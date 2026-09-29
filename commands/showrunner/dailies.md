---
description: Report a running production's state to the executive producer (the user) — every unit and every open topic — at one of three lengths, simple (default), page or elaborate.
argument-hint: "[simple|page|elaborate]"
---

# Dailies

In film, dailies are what the executive producer watches each day to see how
the shoot is going. Here the showrunner reports the production to the executive
producer: the user. Run it in the showrunner session during `/showrunner:produce`,
whose state (`PRODUCTION_DOC`, `LOG`, `ZONE`, `UNITS`, `CHECKOUT`,
`MERGE_BRANCH`, `TIMER_CONF`, `TIMER`) it uses.

**Usage:** `/showrunner:dailies [simple|page|elaborate]`. With no argument,
`simple`. With any other argument, name the three choices and stop.

## Status check and clock

While `/showrunner:produce` runs scheduled updates (its update timer,
`TIMER_CONF`), a dailies the user runs takes the next tick's place. N is the
production doc's **Updates** interval. Two steps do that:

1. **Check every unit.** Before Gather, run the status script that the
   scheduled-update prompt names. It checks every unit, each time: that its
   session and Claude are running, any form or decision waiting on the user,
   and its latest step and ETA. Put anything it flags first (SESSION GONE,
   CLAUDE NOT RUNNING, FORM WAITING, a usage limit, a DECISION), as a scheduled
   tick does.
2. **Reset the clock.** After the report, restart the timer so the next tick
   comes N minutes after this report. Run `TIMER stop TIMER_CONF`, then
   `TIMER start TIMER_CONF`: at 19:21 with N = 15, the next tick is 19:36. Log
   the next fire that `start` prints.

A scheduled tick skips both steps. It has run the script already, and its clock
is already right.

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

Every unit, every time. Then each open topic; the merge branch is a topic only
when a merge is held, testing or not pushed. A topic that closed since the last
report gets none. A topic that only waits on one unit's phase gets no entry of
its own: it goes in that unit's `waiting_on_it` and shares the unit's ETA.

The renderer orders the sections: subjects that need you first, then units by
ETA, earliest first, then units with no ETA, then the other topics.

## Output

The report comes from a fixed template, never written by hand. Write the input
JSON, run the renderer, and paste its output word for word as the whole report.
Never edit the output: to change a line, change the input and run it again.
When the renderer refuses the input (exit 2), fix what it names and run again.

```sh
python3 ~/.claude/scripts/production/dailies_render.py <scratchpad>/dailies_input.json \
  --state <scratchpad>/dailies_state.json --log <LOG>
```

- `--state` holds each unit's last reported phase and ETA. The renderer
  compares this report against it for the change notes, then saves this
  report's. Keep the same file for the whole production. When it is missing,
  that report shows no change notes.
- `--log` appends the `dailies ETAs:` line to `LOG`, so the next report can
  compare. Write no such line by hand.

### Input

```json
{
  "length": "simple",
  "zone": "America/Los_Angeles",
  "next_run": "07:28",
  "units": [
    {
      "unit": "tool-based-ui-trunk",
      "label": "trunk",
      "phase": "follow-up 3 of 4: look polish",
      "update": "fixing the Open layout's overlapping members",
      "eta": {"time": "07:40", "earliest": "07:35", "latest": "07:55", "detail": "checks and build included"},
      "waiting_on_it": "the cable fix; widget merges it into Phase 18",
      "needed": "the showrunner: merge the checkpoint",
      "needs_user": false,
      "then": "dimming after widget Phase 18, then nothing queued"
    }
  ],
  "topics": [
    {"title": "CI on init/catalyst", "update": "run 36579701444 is queued", "eta": "no ETA stated yet", "needed": null, "needs_user": false}
  ]
}
```

| Field | Rule |
| --- | --- |
| `length` | `simple`, `page` or `elaborate`, from the argument. |
| `zone` | `ZONE`, as an IANA name. |
| `next_run` | The next scheduled run, `HH:MM` in `ZONE`, after any restart. Leave it out when no schedule runs. |
| `unit` | The unit's session name. |
| `label` | The timeline row name, at most 8 characters. Defaults to the unit name without `-unit`. |
| `phase` | `Phase <N> of <M>: <what it changes>` from the unit's plan. Work outside a numbered plan gives its place in the unit's queue: `follow-up <K> of <Q>: <what it changes>`. The renderer refuses anything else. |
| `update` | What the unit is doing now, one line. The length sets how long (below). A unit waiting on another unit says so, with the wait's start and expected clear times from `LOG` (`/showrunner:produce` → Dependencies). |
| `eta` | The unit's latest stated phase ETA, in `ZONE`. `time` is `HH:MM`, `+1` for tomorrow (`11:21+1`); add `earliest` and `latest` when the unit gave a range. With no ETA, `none` in place of `time`, one of: `none measured - requested` (after sending that unit `/unit:eta` in this turn, the unmeasured-ETA rule in `/showrunner:produce`), `none measured`, `no ETA stated yet`. Never make one up. `detail` is an optional short note, such as what the time covers. |
| `waiting_on_it` | Only for a topic that lands with this unit's phase, and who waits. |
| `needed` | Only when the subject needs a follow-up nobody has started, from you (the user), the showrunner or another unit. Say who. |
| `needs_user` | `true` when the subject waits on you. It then goes first. |
| `then` | See below. Required on a follow-up and on a plan's last phase. On a follow-up it must name the plan phase the unit returns to (`the plan at Phase <N>`), or say `plan done`; the renderer refuses anything else. Read the plan doc's `todo` phases to write it. |
| topic `title`, `update`, `eta` | The topic's name, what it is doing now, and when it lands, as text. |

**`then`** says what the unit does after this, when it is not simply the plan's
next phase:
- On work inserted ahead of its plan (a follow-up, or a phase added mid-run)
  while plan phases are still open: name the plan phase it goes back to, by
  number and what it does, and anything queued before it: `precompose redesign
  and dimming, then back to the plan at Phase 41 (new tools placed by the
  arrangement engine)`.
- On its plan's last phase with more work queued: name it and its source: `the
  hana_organon plan (docs/hana/hana-organon-design.md), six phases, once this
  phase is merged`. With nothing queued, `nothing queued`.

Read the plans and design docs for this, not the unit's queue alone. A unit's
own handoff may list only the work in front of it.

### What the renderer writes

- **First line:** the length and both times: `**Dailies (Simple)**, 19:05 PDT / 02:05 UTC`.
- **One section per subject:** `### <unit>, <phase>`, then `update:`, `eta:`,
  and `waiting on it:`, `needed:` and `then:` when given.
- **eta note:** against the last report's ETA for the same phase:
  `(unchanged)`, `(changed: +0:27)`, or `(unchanged, overdue)` once the time
  has passed. No note on a subject's first ETA or a new phase. A range follows
  the note.
- **Timeline:** a code block after the sections. Each row is white from now to
  the ETA, with a green cell at the earliest time and a red cell at the latest;
  `→` means the latest runs past the axis end; `?` means no ETA.
- **Last line:** `next run at 20:10 - nothing needed` when no subject has a
  `needed:`, `next run at 20:10` when one does, or `no run scheduled` without a
  schedule. The report replaces the turn's `— waiting on:` line.

## Length

| Argument | `update:` gets |
| --- | --- |
| `simple` | One short line. For when the user is already following along. |
| `page` | The `simple` line plus one more sentence of brief context: what a named thing is (a helper, seat, round or check) and why it matters now. Example: "fifth pass on the app's wording has started; the fourth left 16 tests expecting the old words. Trunk hands each pass to a short-lived helper agent, and each can run out of room partway, so the work takes several passes." |
| `elaborate` | More on each subject: what is moving or at risk gets the most, what is only waiting the least. Up to two pages for the whole report, and only as long as the state needs. The user asks when they want more. |

Every length keeps the same template; only the `update:` text grows. An
`update` is always one line, at most 240 characters for `simple` and 480 for
`page`; the renderer refuses a longer one.

For every length:
- Plain words. Technical terms are fine where they are the right ones.
- Name work by what it changes in the app, never by a unit's own labels: no helper
  names (`look-b5`), batch letters or item numbers. The user does not see them.
- Say "you" for the user.
- Do no other work in this turn, except the `/unit:eta` requests, the two
  steps in Status check and clock, and acting on a BLOCK past its limit
  (`/showrunner:produce` → Dependencies, rule 4).

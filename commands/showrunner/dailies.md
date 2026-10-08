---
description: Report a running production's state to the executive producer (the user) — every unit and every open topic — at one of four lengths, gantt (default: the chart, with only what needs the user and what changed), simple, page or elaborate.
argument-hint: "[gantt|simple|page|elaborate] [default|ascii]"
---

# Dailies

In film, dailies are what the executive producer watches each day to see how
the shoot is going. Here the showrunner reports the production to the executive
producer: the user. Run it in the showrunner session during `/showrunner:produce`,
whose state (`PRODUCTION_DOC`, `LOG`, `ZONE`, `UNITS`, `CHECKOUT`,
`MERGE_BRANCH`, `NOTIFIER`, `UPDATES`) it uses.

**Usage:** `/showrunner:dailies [gantt|simple|page|elaborate] [default|ascii]`. With
no length, `gantt`. With any other argument, name the choices and stop.

**Chart mode.** `default` draws the timeline in coloured squares; `ascii`
draws it with characters a code font has, for when you are remote, since the
desktop app draws emoji wider than two columns. The mode lives in
`~/.local/state/showrunner/dailies.conf`, which every showrunner's renderer
reads, so no session remembers it. A mode argument sets it before the report:
run `python3 ~/.claude/scripts/production/dailies_render.py --chart <mode>`.
So do your words: "ascii mode", "switch back to default". It holds until
changed. User, 2026-10-03.

## Status check and clock

While `/showrunner:produce` runs scheduled updates through `UPDATES`, a
dailies the user runs takes the next tick's place. N is the
production doc's **Updates** interval. Two steps do that:

1. **Check every unit.** Save the status script's complete output before Gather:
   `zsh ~/.claude/scripts/production/unit_status.sh <scratchpad>/unit_status <ZONE> --production PRODUCTION_DOC > <scratchpad>/unit_status.txt`.
   The input builder reads that file and prints `flags first:` for SESSION GONE,
   CLAUDE NOT RUNNING, FORM WAITING, a usage limit, and a DECISION. Put these
   first in the report.
2. **Reset the clock.** Pass `--user-run` to the input builder for a dailies
   the user invoked. It validates the full input before it runs `NOTIFIER restart
   UPDATES`, takes the new `next_due` for the report, and logs that line. At
   19:21 with N = 15, the next tick is 19:36. An aligned timer keeps the
   clock: at 19:21 with N = 60, 20:00; at 19:45, 21:00.

A scheduled tick's prompt saves `<SCRATCH>/unit_status.txt`, then runs
`/showrunner:dailies gantt`. Pass that file to the builder without
`--user-run`; its clock is already right.

## Gather

Read the current state, not memory, and check what you state the way
`/showrunner:produce` checks a unit director's claims.

1. **Time.** `TZ=<ZONE> date '+%H:%M %Z'`. `ZONE` only, never UTC (user, 2026-10-02).
2. **Log.** The latest `### STATE` block in `LOG` and every event after it.
3. **Each unit.** Use the saved status output for its latest activity. For a
   numbered open plan phase, the input builder takes its start and ETA from
   the unit's run records when that phase number matches the judgment. No
   `Phase ETA:` line is needed in the status capture. A judgment value wins;
   in particular, a judgment ETA `time` or `none` is final. Otherwise the
   input builder keeps the ETA's first-seen time and prints
   `request /unit:eta: <unit>` once per phase when that ETA is over an hour
   old or past. Send that request this turn. It writes `set HH:MM` for a stale
   ETA and `none measured - requested` for a passed one. A held unit's ETA
   that has not changed since the builder first saw it is never requested and
   stays as stated, unless a new one was already asked for before the hold:
   that one is not brought back. A unit whose run has finished and that shows
   no form and no decision needs no entry: the builder leaves it out of the report and
   names it in a `left out` line. Supply the phase, started time, current update, and ETA
   numbers from the unit's plan and reports. Text after `❯` may be a prompt
   suggestion, not the user's draft:
   `capture-pane -e` shows a suggestion dimmed (`ESC[2m`).
4. **Merge branch.** The input builder reads the last merge and whether the
   branch is pushed. Supply anything held or testing in the judgment file.
5. **Open topics.** Everything in `LOG` not yet closed: held quota alerts, CI,
   defects routed between units, gates, and items waiting on the user.
6. **Review watch.** The input builder runs `review_regime.py watch` and adds
   its line as a topic until it prints `acknowledged`; exit 3 sets
   `needs_user` (`/showrunner:produce` → <MergeCheckpoint/> step 6).
   User, 2026-10-04.

## Subjects

Every unit, every time. Then each open topic; the merge branch is a topic only
when a merge is held, testing or not pushed. A topic that closed since the last
report gets none. A topic that only waits on one unit's phase gets no entry of
its own: it goes in that unit's `waiting_on_it` and shares the unit's ETA.

The renderer orders the sections: subjects that need you first, then units by
ETA, earliest first, then units with no ETA, then the other topics.

## Output

The report comes from a fixed template, never written by hand. Write only the
judgment JSON described below. Run the input builder, then the renderer on its
output, and paste the renderer's output word for word as the whole report.
Never edit the output: to change a line, change the input and run it again.
When the renderer refuses the input (exit 2), fix what it names and run again.
When the builder prints `<step>: failed — <reason>`, give that exact line to
the user as the failure message. Correct the named input before reporting.
When it prints `phase tables: unavailable — <reason>`, continue. Only a unit
whose records could not be read takes its start and ETA from the saved status
capture; failed removal of retired notes changes nothing else.

```sh
python3 ~/.claude/scripts/production/dailies_input.py \
  --production <PRODUCTION_DOC> --status <scratchpad>/unit_status.txt \
  --judgment <scratchpad>/dailies_judgment.json --state-dir <scratchpad>/dailies_input_state \
  --out <scratchpad>/dailies_input.json --length <gantt|simple|page|elaborate> \
  --render-state <scratchpad>/dailies_state.json \
  --notifier <NOTIFIER> [--user-run]
python3 ~/.claude/scripts/production/dailies_render.py <scratchpad>/dailies_input.json \
  --state <scratchpad>/dailies_state.json --log <LOG> --outstanding <OUTSTANDING>
```

- `--state` holds each unit's last reported phase and ETA. The renderer
  compares this report against it for the change notes, then saves this
  report's. Keep the same file for the whole production. When it is missing,
  that report shows no change notes.
- `--log` appends the `dailies ETAs:` line to `LOG`, so the next report can
  compare. Write no such line by hand.

### Input

The judgment file has `units` and optional `topics` and `merge`. Each unit
has its Units table id in `unit`, never the session's current name; the builder
joins run records and status blocks on that id. It has only the fields requiring judgment: `project`,
`goal` when measured, `phase`, `started`, `held`, `held_examples` when useful,
`update`, `waiting_on_it`, `needed`, `needs_user` when you know the answer, `idle`,
`then`, `label` (up to eight characters; needed when the session name without
`-unit` is longer), and ETA numbers (`percent`, `earliest`, `latest`, `first`, `fixes`,
`why`) in `eta`. For a matching numbered open phase the builder takes an absent
start and ETA fields from the run record; a judgment value wins. Otherwise it
takes the ETA time from status. Write `merge.held`
or `merge.testing` when relevant. It fills `length`, `zone`, `unit`,
`next_run`, build holds, review watch and merge branch. A refusal names what
needs correction and leaves the output file untouched; fix the judgment file
and run it again.

The builder's output, which the renderer consumes:

```json
{
  "length": "simple",
  "zone": "America/Los_Angeles",
  "next_run": "07:28",
  "units": [
    {
      "unit": "tool-based-ui-trunk",
      "label": "trunk",
      "project": "tools you build and edit in the 3D scene",
      "phase": "follow-up 3 of 4: look polish",
      "started": "2026-10-01T05:12",
      "held": null,
      "update": "fixing the Open layout's overlapping members",
      "eta": {"time": "07:40", "earliest": "07:35", "latest": "07:55", "percent": 80, "detail": "checks and build included"},
      "waiting_on_it": "the cable fix; widget merges it into Phase 18",
      "needed": "the showrunner: merge the checkpoint",
      "needs_user": false,
      "then": [
        "dimming, once widget's Phase 18 lands",
        "the plan at Phase 19: new tools appear in the 3D scene"
      ]
    }
  ],
  "topics": [
    {"title": "CI on init/catalyst", "update": "run 36579701444 is queued", "eta": "no ETA stated yet", "needed": null, "needs_user": false}
  ]
}
```

| Field | Rule |
| --- | --- |
| `length` | `gantt`, `simple`, `page` or `elaborate`, from the argument. |
| `zone` | `ZONE`, as an IANA name. |
| `next_run` | The next scheduled run, `HH:MM` in `ZONE`, after any restart. Leave it out when no schedule runs. |
| `unit` | The Units table's unit id, never the session's current name. The builder joins on this id. |
| `name` | The plan the unit runs, when its session name does not say it (a lane that took on another plan): `hana_organon`. Defaults to `unit`. |
| `label` | The timeline row name, at most 8 characters. Defaults to the unit name without `-unit`. |
| `project` | The goal of the unit's whole plan, in a few words, from its plan doc's opening: what you get when every phase is done. Not this phase, and not a list. Example: `tools you build and edit in the 3D scene`. It is the section heading. |
| `goal` | Required when the unit owns a measurable goal: `target` (the goal in words, its number included), `unit` (`ms`), and the measured `start`, `now` and `aim` from the unit's latest measured run. The renderer prints `goal: <target> - <x>% of <aim> <unit> target achieved`, x from the three numbers. A goal with no number yet gets one before the next report: ask the unit director for a measured baseline and settle the aim. User, 2026-10-04. |
| `phase` | `Phase <N> of <M>: <what it changes>` from the unit's plan. Work outside a numbered plan gives its place in the unit's queue: `follow-up <K> of <Q>: <what it changes>`. The renderer refuses anything else. |
| `started` | When the phase started, `YYYY-MM-DDTHH:MM` in `ZONE`. A matching numbered open phase supplies it from the run record; a judgment value wins. Otherwise use the unit director or `LOG`. The timeline row starts there. |
| `held` | Required. When the phase's checkpoint waits unmerged, the reason alone, in a few words, written to follow "not merged, because": `the design check found 16 defects`. `null` when no checkpoint waits. No examples here; the renderer refuses `such as`. |
| `idle` | Only while the unit waits on a clock gate, a data window or another unit and nothing of its runs: no seats, no helpers, no build or test. `waits_for` is what it waits for, one short line of at most 80 characters; `until` is when it comes back, `YYYY-MM-DDTHH:MM` in `ZONE`, from the unit director's own statement or `LOG`, never made up. Remove it once the unit works again; the renderer refuses an `until` that has passed. A unit with no known return time gets no `idle`. |
| unit `build_hold` | `true` while the unit is under a `/build_hold` (its unit director told to stop builds); leave it out otherwise. A holder's own unit is never marked. It ends the unit's timeline row with `build hold`. Holder files in `~/.local/state/build-hold/` supply one footer line per active holder. A Mac block has no unit row or input key; its file in `~/.local/state/mac-test/` supplies its footer line. The renderer refuses a marker with no holder file and active files with no marked unit. No marker means not held (user, 2026-10-03: "Without that marker I will assume it is not held"). |
| `held_examples` | Optional examples for `held`, written to follow a comma: `such as a main bar clipped in small windows`. In a `simple` report the renderer prints them only the first time that reason appears for the phase; `page` and `elaborate` always print them. |
| `update` | What the unit is doing now, one line. The length sets how long (below). A unit waiting on another unit says so, with the wait's start and expected clear times from `LOG` (`/showrunner:produce` → Dependencies). |
| `eta` | The unit's latest phase ETA, in `ZONE`. A matching numbered open phase supplies its absent values from the run record; no `Phase ETA:` line is needed in the status capture, and each judgment value wins. A judgment `time` or `none` decides the ETA entirely. Otherwise the status capture supplies the time. `time` is `HH:MM`, `+1` for tomorrow (`11:21+1`); add `earliest` and `latest` when the unit director gave a range. The builder omits both ends of a recorded range when the renderer would place either on the wrong day. With no ETA, `none` in place of `time`, one of: `none measured - requested` (after sending that unit director `/unit:eta` in this turn, the unmeasured-ETA rule in `/showrunner:produce`), `none measured`, `no ETA stated yet`. Never make one up. `percent` is required with `time`: the unit director's phase percent done (the recorder's phase `%`, 0–100), or `null` when it stated none. `detail` is an optional short note on what the time covers (`checks and build included`). It never says where the ETA came from: no `from the recorder`, no `from past runs` (user, 2026-10-02); the renderer refuses a `detail` that starts with `from`. `why` says in a few words why the ETA moved since the last report, from the unit director's own reports (`the four remaining app tests need a new floor anchor`); the renderer prints it after `because` and refuses a move of 15 minutes or more without it (user, 2026-10-01). |
| `eta.first`, `eta.fixes` | The phase's first stated ETA (`YYYY-MM-DDTHH:MM`), and the fix rounds added since it. `first` seeds the state once; the state then keeps it for the phase. Give `fixes` every report: the repair rounds started after the first ETA. User, 2026-10-03. |
| `eta.stated` | Written by the builder with `eta.time`, never by you. A record-backed ETA uses its `stated_at` or projection `as_of` minute in `ZONE`; when that would make a bare clock resolve to the wrong day, it uses the target's own minute. The renderer reads a time that names no day on that date. It is absent without `time`. |
| `waiting_on_it` | Only for a topic that lands with this unit's phase, and who waits. |
| `needed` | Only when the subject needs a follow-up nobody has started, from you (the user), the showrunner or another unit director. Say who. |
| `needs_user` | `true` when the subject waits on you. It then goes first. |
| `then` | A non-empty list only: each one-line item names one upcoming phase or follow-up; never chain two in one item. Lead with its phase number when it has one. A range with one shared purpose is one item: `76–79: edge cases in selection, jack panels, the palette and Log, saved scenes and reset`. Required on a follow-up and on a plan's last phase. On a follow-up, an item must name the plan phase the unit returns to (`the plan at Phase <N>`), or say `plan done`; the renderer refuses anything else. Read the plan doc's `todo` phases to write it. |
| topic `title`, `update`, `eta` | The topic's name, what it is doing now, and when it lands, as text. |

**`then`** is always a list of one-line items in order. Give each upcoming
phase or follow-up its own item; never chain two in one item. Group a phase
range only when it has one shared purpose. For example: `"then": ["Phase 3: panel labels stay legible", "Phase 4: panel edges align"]`.
`simple` shows only the first item. `page` and `elaborate` show each
item as a sub-bullet under `- then:`; a single item stays on the `- then:` line.

- On work inserted ahead of its plan (a follow-up, or a phase added mid-run)
  while plan phases are still open: list anything queued before the return,
  then name the plan phase it goes back to, by number and what it does:
  `["40: precompose redesign and dimming", "the plan at Phase 41: new tools
  placed by the arrangement engine"]`.
- On its plan's last phase with more work queued: name it and its source:
  `["the hana_organon plan (docs/hana/hana-organon-design.md), six phases, once this phase is merged"]`.
  With nothing queued, `["nothing queued"]`.

Read the plans and design docs for this, not the unit's queue alone. A unit
director's own handoff may list only the work in front of it.

Phases run in number order, so each `then` item never names a phase at or before
the heading's: `Phase 3 of 3` followed by `["2: tool-face sizing"]` reads as impossible.
When a unit runs its phases out of order, renumber its plan so the numbers
follow the run order (packaging: your call), tell the unit director, and report
the new numbers. The renderer refuses any `then` item that goes backwards,
unless it names another plan's document. User, 2026-10-02.

### What the renderer writes

- **First line:** the length and the time in `ZONE`: `**Dailies (Simple)**, 19:05 PDT`.
- **`gantt`:** the first line, the chart and the last lines, and above the chart only two things. A subject
  that needs the user keeps its full section. A unit that changed since the last report gets one line for each
  change: `- <unit>: now on <phase>`, `- <unit>: checkpoint not merged, because ...` or `checkpoint no longer
  held`, `- <unit>: eta ...` when the ETA moved 15 minutes or more, and `- <unit>: no longer in the report`.
  Nothing changed, no lines. The rest of this list is what the other lengths print.
- **One section per subject:** `### <unit>: <project>`, then `goal:` when given, `phase:`,
  `checkpoint: not merged, because ...` when given, `update:`, `eta:`, and
  `waiting on it:`, `needed:` and `then:` when given. In `simple`, a held
  reason shows its examples only the first time. The word "held" is kept for
  a build hold, so a checkpoint waiting to merge reads `checkpoint:`.
- **Waiting and idle:** in `simple` only, each unit with `idle` and no `needed`, no unmerged checkpoint and nothing waiting on you gives up its section for one line under `### Waiting and idle`, after the other units: `- cache-evict until 16:33: a day of sweep readings`. A later day adds its weekday (`Sat 22:44`), and a week or more away its date (`Wed 2026-10-14 07:50`). It keeps its timeline row. `page` and `elaborate` keep its full section. User, 2026-10-07.
- **eta:** the time, then the percent done: `10:46 PDT, 85% done`.
- **eta note:** against the last report's ETA for the same phase:
  `(unchanged)`, `(changed: +0:27 because <why>)`, or `(unchanged, overdue)`
  once the time has passed. A subject's first ETA or a new phase has no note,
  except `(overdue)` when its time has already passed.
  A new phase has a new title; a renumbered phase with the same title keeps its notes.
- **first eta:** once the ETA has moved from the phase's first: `05:43 PDT (now
  +17:12, 8 fix rounds added)`. User, 2026-10-03. A
  range follows the note; an end on another day carries its weekday
  (`range Fri 08:24–Sat 07:37`).
- **Timeline:** a code block after the sections, always 24 hours of one-hour
  cells, labelled every three hours, with `▼` at now. It always rolls to fit
  the rows: it opens at the three-hour mark at or before the earliest phase
  start, and later only as far as keeps every latest time in view, never past
  now's mark (`12 15 18 21 00 03 ▼ 09`). While the chart is wider than the app
  shows (95 columns), it opens later still, three hours at a time, so the plan
  bars fit. User, 2026-10-04. Each row is white from the phase's
  start (or the left edge, when it started earlier) to the ETA, with a blue
  cell at the ETA, a green cell at the earliest time and a red cell at the
  latest (no green or red without a range). When blue shares an hour with
  green, blue takes it; red keeps its own. `→` means the latest
  runs past the right edge; `?` means no ETA. A phase that started before
  the left edge shows its start as `mmm-dd hh:mm` between its name and its
  cells (`widget   Sep-30 17:11 ⬜⬜…`); other rows leave that space blank, so
  the cells stay under the axis. User, 2026-10-01. A unit under a build hold ends its row with `build hold`.
  Right of the rows, in one right-aligned column: `Phase N of M - P%`, the
  whole plan's percent done (earlier phases whole, this one at its `percent`),
  then ten blocks, one per 10% rounded, and `│` at 100%, with `100%` ending above
  it on the axis line. A follow-up row has none. User, 2026-10-03.
  The `ascii` chart draws the same rows with characters a code font has,
  because the desktop app draws emoji wider than two columns: `──` while the
  phase runs, a heavy `━━` from the earliest time to the latest, `●` at the
  ETA, and no `(earliest–latest)` text, so the row fits. User, 2026-10-03.
  Only glyphs the app's Anthropic Mono has: it lacks `┼` and `┤`.
- **Agents:** after the chart and any build-hold and Mac-block lines, one line per active account shows its week used, when it runs out at its current pace (each rise weighted by half per 12 hours of age, across refills, `scripts/whoami/run_out.py`), whether it reaches its refill first, and its available resets. With no pace, it says the account does not run out at this pace; when exhausted, it says the account ran out: `- claude 1: 40%; runs out about Tue 07:10 PDT, before its Sun 23:00 refill; 1 reset available until Oct 22` (user, 2026-10-05; wording updated 2026-10-06).
- **Footer:** the reply footer (`/showrunner:produce` → Footer), from
  `next_run`, the build-holder files, the Mac block file, `OUTSTANDING` and whether
  any subject has `needed:`. The report ends with that footer. The Waiting on
  block follows it, as in every reply.

## Length

| Argument | `update:` gets |
| --- | --- |
| `gantt` | The same one short line as `simple`. The report prints it only for a subject that needs the user; write it for every subject all the same, since the input is checked in full. |
| `simple` | One short line. For when the user is already following along. Waiting, idle units take one line each. |
| `page` | The `simple` line plus one more sentence of brief context: what a named thing is (a helper, seat, round or check) and why it matters now. Example: "fifth pass on the app's wording has started; the fourth left 16 tests expecting the old words. Trunk hands each pass to a short-lived helper agent, and each can run out of room partway, so the work takes several passes." |
| `elaborate` | More on each subject: what is moving or at risk gets the most, what is only waiting the least. Up to two pages for the whole report, and only as long as the state needs. The user asks when they want more. |

Cutting repeats is for `simple`: it says only what changed or what you need. `page` may repeat context a reader needs, and `elaborate` more. User, 2026-10-01.

Every length keeps the same template and the `update:` text grows with it,
except that `simple` gives each waiting, idle unit the one line in **Waiting
and idle** above. An `update` is always one line, at most 240 characters for
`gantt` and `simple` and 480 for `page`; the renderer refuses a longer one.

For every length:
- **One phase per unit.** The heading names one phase: the oldest one not yet
  merged (`/showrunner:produce` → Rules: parallel by default); `update` names
  any other phase running beside it, with `because`. A held
  checkpoint keeps its phase in the heading until it merges.
- **Say why it is held.** Whenever a checkpoint waits unmerged, `held` gives
  the reason. The renderer refuses a unit without the field. Give examples
  in `held_examples`; `simple` shows them once, then the reason stands alone.
- **Report against a count.** Once a report counts something, later updates
  say how much of it is done: `8 of 16 fixed`. The renderer refuses an update
  that does not report against a count in `held`. User, 2026-10-01.
- **`update:` is the present only.** What the unit does now, not its history.
  It names no other phase number unless it says why that phase is here
  (`because ...`); the renderer refuses one that does. Later work goes in `then`,
  never in both: an update says nothing about what follows this phase. User, 2026-10-04.
- Plain words. Technical terms are fine where they are the right ones.
- Name work by what it changes in the app, never by a unit director's own labels: no helper
  names (`look-b5`), batch letters or item numbers. The user does not see them.
- Say "you" for the user.
- **Read it as the user before you paste it.** Read every line of the
  rendered report as someone who sees only the app and this report. A line
  that cannot be true as written is a defect in the production: fix its cause
  (renumber the plan, correct the state, ask the unit director), then render
  again. A line that would make the user ask "what does that mean?" gets new
  words. Never paste it and explain it afterwards. Bad lines from 2026-10-02
  (user), each with what it should have said:

  | Bad | Why | Better |
  | --- | --- | --- |
  | `Phase 3 of 3` with `then: Phase 2 (tool-face sizing)` | Impossible as written: the lane ran its phases out of order. | Renumber the plan first: `Phase 2 of 3` with `then: Phase 3 (tool-face sizing)`. |
  | `a helper is retaking every view, 4 of 9 shots done` | Shorthand: "retaking every view" says nothing. | `taking new screenshots to check the glow change, 4 of 9 done` |
  | `its stale file holds from the merged work are released` | Production plumbing that changes nothing you see. | Leave it out. |
  | `the writer and tester are renaming every crate; file clashes get settled at its checkpoint` | Seat roles and plumbing. | `renaming Composite to Assembly in every crate's code and tests` |
  | `then: Phase 8 (fault words say what they hold)` | The plan's heading copied; it names no result you would notice (user, 2026-10-04). | `then: Phase 8 (error messages say clearly what went wrong)` |
  | `the full widget tests and a live check run. The next fix, words on the first frame, starts once this phase merges.` | Repeats `then:` (user, 2026-10-04). | `the full widget tests and a live check run.` |

  A `phase:` or `then:` is never the plan's heading copied: say what you
  will notice in the app when it is done.

  The renderer refuses the plumbing words it knows (`PLUMBING`: berth,
  reservation, holder, file holds, file clashes, writer, tester, retake,
  re-shoot). That list is a net, not the rule: the read is the rule.
- Do no other work in this turn, except the `/unit:eta` requests, the two
  steps in Status check and clock, acting on a BLOCK past its limit
  (`/showrunner:produce` → Dependencies, rule 4), and compacting a unit
  director after its checkpoint (`/showrunner:produce` → Compact after a checkpoint).

# /build:queue view — design brief

Please make an information design, not a rewrite of the text. Draw at least two
visual forms that are clearly different from each other. Draw each one as real
terminal output from the live queue below, at 100 and at 60 columns.

## The question the view answers

1. Why is the first step that cannot get in held, and what must happen before
   it starts?
2. Where am I? This applies to the session that asks, when it has a waiting step.

Those two questions are the whole view. Everything else is detail on demand
(`/build:queue <place|session>` opens one step).

## What the user rejected (2026-10-30)

- Each row drawn as a 10-line card. Markdown tables become records in a narrow
  terminal.
- A long list: 40 rows, one per waiting step.
- The rationale at the bottom: "#2-#41: strict until 18:13:29: younger steps
  wait; 0 of 40 fit now". The user could not use it.
- Totals: "40 waiting, 264 GiB in all". Only the first step that cannot get in
  matters.
- Text density: a 7-line text mock was still too many words.
- Made-up times: a start estimate from the last hour's pace ignored memory. No
  time may show that is not measured.

## User rules

- No totals. Only the first step that cannot get in matters.
- Few words. Show it, do not write it.
- No made-up times. Waited time is measured; start estimates are not.
- The pane and the inline path show the same view where the path can draw it.
- Never offer a bump as the fix. Only the user bumps.

## Live data (natedev, 18:05 EDT)

The budget is 21.0 GiB, and running steps hold 21.0 GiB of it:

| running step | holds GiB | ran |
| --- | --- | --- |
| nextest · tool-based-ui · merge-gate | 5.3 | 24 min |
| clippy · tool-based-ui · merge-gate | 0.2 | 22 min |
| nextest · tool-based-ui · widget | 8.6 | 22 min |
| clippy · tool-based-ui · demo | 6.9 | 3 min |

The head (#1) is nextest · cargo-liner · tile. It has waited 101 min and needs
10.8 GiB of budget. It starts when the held budget falls to 10.2 GiB or less, so
10.8 GiB must come back.
- Free memory is 17.5 GiB and the step needs 13.8 (10.8 plus earlyoom 3.0): this
  check passes.
- Memory pressure: no hold.

A strict window runs until 18:13:29. Until then no other step may start before
#1, even one that fits. 0 of the 40 behind it fit now.

There are 41 waiting steps in all. Places 2–12:

| # | waited | step · production · unit | needs GiB |
| --- | --- | --- | --- |
| 2 | 96 min | nextest · tool-based-ui · inspectors | 12.2 |
| 3 | 89 min | clippy · cargo-liner-style · names | 3.0 |
| 4 | 74 min | clippy · cargo-liner · handler | 3.0 |
| 5 | 73 min | doc · tool-based-ui · hana | 4.9 |
| 6 | 72 min | clippy · cargo-liner-style · types | 3.0 |
| 7 | 67 min | merge-gate · builds | 6.0 |
| 8 | 67 min | clippy · performance · metrics-visualization | 6.8 |
| 9 | 66 min | clippy · tool-based-ui · geometry-material | 7.4 |
| 10 | 60 min | clippy · tool-based-ui · cables (session fix-router) | 5.9 |
| 11 | 59 min | clippy · cargo-liner-style · values | 3.0 |
| 12 | 46 min | mend · cargo-liner · tile | 7.3 |

Places 13–41 waited between 31 and 1 min and need between 1.0 and 12.2 GiB each.
By production, waiting: tool-based-ui 19 · cargo-liner 12 · cargo-liner-style 4 ·
performance 4 · builds 2.

Example "me": the session fix-router (tool-based-ui · cables) is #10. It has
waited 60 min and needs 5.9. Nine steps are ahead of it.

Other facts the data holds, which you may use or drop: a step moved up (who moved
it, when, from which place); a running step's measured use against what it holds;
a session with several waiting steps.

## What the surfaces can draw

**Pane** (Claude Code mod, `unit-board`), refreshed every 10 s:
- Layout: `Box`, `Row` and `Column` elements, and `Text` spans.
- `Text` props: `color` and `backgroundColor` (a theme key: success, error,
  warning, inactive, subtle, suggestion, claude, … or a raw colour), plus bold,
  dimColor, inverse and underline.
- Any Unicode, block characters (`█▓▒░▏▎▍▌▋▊▉`, `■□`, `●○`) included.
- Width: the pane body is 60 to 140 columns. Design for 100, and check it at 60.
  A line must never wrap.
- Height: a pane of about 10–15 rows is comfortable. Fewer is better.

**Inline path**: `/build:queue` in a session that has not loaded the mod. The
model prints the script's output as is in a fenced `text` block:
- Monospace, no colour, no bold.
- Unicode and block characters draw.
- Same widths. Never a Markdown table.

The design must work in monochrome inline, and colour must only add to it.

## Method

- One question, one picture: the binding constraint (here, the budget) must be
  the strongest mark on screen.
- Encode the number that matters by length or position, not by words. Examples
  of the kind wanted: a memory bar with the head's need marked, or a strip that
  shows place and wait. Treat these as starting points, not specs.
- Label marks directly. No legend, no footnotes, no rationale paragraph.
- Data ink only: drop every word or rule that does not answer the two questions.
- Show the rest of the queue only as far as it answers "where am I".

## Deliverable

At least two different forms. For each form:
1. The view at 100 and at 60 columns, from the data above, as it would print.
2. The colour each part takes in the pane.
3. The inline (monochrome) version, if it differs.
4. One line on what the eye reads first.
5. The variant for a session with no waiting step.

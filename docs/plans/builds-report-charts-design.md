# Build history charts

## Goal and delivery

Show whether build changes reduce Hana phase time and recorded build costs.
Phase time remains the score.
The user approved this view on 2026-11-03 with “go”.

The charts live on the shared build page.
`/build:page` prints its link.
`charts.page_data` supplies the charts and ranked costs from one index snapshot.
The page offers seven-day and thirty-day windows.
`/build:report` remains retired. `buildlog report` remains available.

## Three charts

1. **Hana phase time.** Show daily median and p90 wall hours as two lines.
   Keep seconds in the table only.
   Show finish counts, valid durations, and missing starts.
   Use `moments.nearest_rank` and `moments.hana_name`.
2. **Costs by cause.** Show six vertical panels for the selected machine or all machines.
   Each cause has its own y-axis. All panels share the time axis.
   Causes overlap, so the chart does not stack them or show a total.
3. **Costs by machine.** Show machine lines for the selected cause.
   Include configured targets and other recorded machines.

The data shows trends over time, so all three views use lines.
This form follows `choosing-a-form.md`.
Each plot has one y-axis.
All plots use the same width and right gutter.
Dates and moment lines align across the page.
One cost-unit control selects h/day or h/finished phase for both cost charts.
Per-phase costs use all finished phases, including finishes without valid durations.

## Color and interaction

Use the categorical slots from the supplied `palette.md` in this order:

| Slot | Light | Dark |
|---|---|---|
| 1 | #2a78d6 | #3987e5 |
| 2 | #eb6834 | #d95926 |
| 3 | #1baf7a | #199e70 |
| 4 | #eda100 | #c98500 |
| 5 | #e87ba4 | #d55181 |
| 6 | #559b16 | #559b16 |
| 7 | #6250d6 | #9085e9 |
| 8 | #e34948 | #e66767 |

Cause slots follow the fixed `CAUSES` order.
Machine slots follow natedev, mac, CI, then other names in sorted order.
The page assigns slots from its complete machine list before any filter changes.
With more than eight machines, slot eight groups the remaining machines as Other.
No filter changes a surviving series color.
Phase median uses slot one; p90 uses slot two and a dashed line.
No series uses a status color.
The green slot uses a new step, #559b16, to separate Mac idle from aqua reruns.
Each cause occupies its own panel, so color never carries its identity alone.

The light chart surface is #fcfcfb; the dark chart surface is #1a1a19.
Text uses the primary or secondary text colors from `palette.md`.
Auto starts selected and follows the system theme.
The theme control also offers Light and Dark.
An explicit selection changes both the control and the rendered theme.
Both palettes pass the supplied validator.
The light palette has three contrast warnings; each chart provides a table view as the required relief.

Lines use 2px strokes and round ends.
Markers have an 8px diameter and a 2px surface ring.
Gridlines and axes use solid, recessive 1px strokes.
Each chart has a legend when it shows two or more series.
Plots with four or fewer series also have endpoint labels with leader lines.
Each label stays beside the last measured point.
A trailing gap shows an unavailable label and its reason, with a separator.
The reason names the missing indexed source.
Cause panel titles provide the direct labels for their lines.
Long labels retain their full text in legends, tooltips, and tables.

Window, machine, cause, unit, and theme controls share one row above the charts.
The row wraps on narrow screens.
The window control selects the displayed window without changing the recorded data.
Each chart has a shared date crosshair and a tooltip containing every series at that date.
The whole plot accepts pointer input; the reader need not hit a marker.
Keyboard focus shows the same tooltip. Arrow keys select adjacent dates.
Each chart has a table view with all values and unavailable reasons.
Labels enter the DOM through `textContent`, never HTML strings.

Live `MOMENTS` entries appear at their exact instants.
Numbered chips name each live moment.
Each plot prints the same number above the exact moment line.
Names, scopes, and local times remain available through focus and hover.
Pending moments appear as not live without marker times.

## Buckets and data path

The rolling window is `[now - N days, now)`, captured once in UTC.
Buckets start at local midnight in the zone from the shared `user_zone()` reader.
Moment labels use the same zone. The `TZ` environment variable does not select the chart zone.
The first and last buckets stop at the selected window edges.
Each bucket includes its left edge and excludes its right edge.
Seven days can contain eight buckets; thirty days can contain thirty-one.
Day rates use actual elapsed seconds, including 23-hour and 25-hour days.
Partial buckets show thin. Today's bucket never includes future hours.

1. Open the index with `index.read_only()` and one read transaction.
2. Read `moments.progress_events()` once for the full chart set.
3. Read steps, CI costs, and hold history through `costs.read_costs()` once.
4. Pair nextest runs continuously across the full window and its boundary context.
5. Sweep candidate waits, Mac executions, holds, and source coverage continuously.
6. Assign event costs to buckets and split interval spans at bucket edges.

`charts.page_data` uses the shared cost reader and definitions.
The reader reuses the outcome, previous-run, passed-tree, cold-build, candidate, and machine rules.
No chart runs a separate daily SQL query.

The reader selects the window through `moments.window_bounds()` and the existing `steps_started` index.
An indexed lookup seeds each machine/session with its last nextest before the first edge.
Expression indexes select earlier executions and later waits that cross the window edges.
The calls lookup uses recorded ids and the `calls_copy` index.
The reader does not scan the full steps or calls history.
The hold reader provides earlier hold context.
Context establishes pairs and crossing intervals; it adds no step costs outside the window.

Step and CI costs use the full cost at the recorded start.
A repeat uses the later step's start bucket.
A phase uses its full wall time at its finish.
Mac idle uses clipped interval time.
The page states these different rules.

## Unknown values

Use named states instead of nulls or zero substitutes.
`MeasuredCost`, `MacIdleUpperBound`, and the shared `CostUnavailable` define chart values.
`ProgressFinish.wall_s` retains `PhaseStartUnavailable` for missing starts.

The Mac idle line shows the known part.
A hatched band reaches from the known part to the upper bound.
The texture encodes uncertainty; it does not decorate measured data.
Only recorded execution and complete holds remove occupied time from candidate waits.
The shared costs sweep classifies each residual span by continuous Mac source coverage.
Residual spans without coverage remain unknown.
Incomplete holds also retain unknown time.
An individual execution or hold does not prove continuous source coverage.
The readout names candidate wait, execution, complete holds, known idle, unknown time, and the upper bound.

Missing phase starts do not enter percentiles; the page shows their count.
No valid durations produces a gap and the reason no valid durations.
No finishes produces per-phase gaps and the reason no finished phases.
A bucket without source coverage remains unavailable.
A complete indexed monthly log can establish zero on an empty day before its latest recorded event.
The indexed byte offset must match the local source file size.
Adjacent complete months provide continuous coverage across the month edge.
Missing files and unindexed bytes retain gaps.
Configured targets remain in the machine controls even without recorded events.
Their costs remain unavailable without source coverage.
This check proves coverage of the local copy, not remote recorder completeness.

A NULL memory wait on an applicable step means the wait was not recorded.
The shared reader preserves its timestamp, machine, and cause.
A chart bucket with any missing applicable wait remains unavailable, including buckets with measured waits.
The CLI shows the measured subtotal and the number of missing applicable waits.
A recorded zero remains measured zero.
Launch steps take no memory admission, so their NULL waits do not create gaps.

The source audit found Mac steps after October 28 on October 29–31 and November 2–3.
November 1 has no Mac steps.
The October and November indexed offsets matched all three local Mac log sizes.
The November log has later events, so November 1 shows measured zero.

## Required fixtures

1. Pair passing runs across midnight and the first edge.
2. Keep Mac execution coverage across midnight and the first edge.
3. Split waits across buckets without counting parallel waits twice.
4. Split complete holds and retain uncertainty for unmatched starts and later same-key starts.
5. Reject false pairs with missing session, tree, outcome, or duration fields.
6. Assign exact-edge steps, phases, CI attempts, and moments to the correct bucket.
7. Cover daylight-saving changes, partial buckets, and identical timestamps.
8. Preserve missing starts, invalid durations, zero finishes, and unavailable per-phase costs.
9. Separate unknown machines, missing coverage, and measured zero.
10. Check bucket sums, one read per source, and unchanged index bytes.
11. Show zero for an empty Mac day in a complete indexed monthly log.
12. Keep named gaps when the monthly source is missing or has unindexed bytes.
13. Split residual Mac waits across absent and partial source coverage.
14. Preserve unknown time around executions, complete holds, and incomplete holds without source coverage.
15. Separate NULL applicable waits, recorded zero, mixed buckets, and launch NULL waits.
16. Retain configured targets without events and use the shared user zone for edges and labels.

Browser checks cover light and dark modes, narrow screens, keyboard input, tables, fixed colors, and labels.

## Timing and acceptance

Measure three fresh processes for each window through final HTML publication.
The gate uses process CPU time: user plus system time from `resource.getrusage`.
The seven-day limit is 1.5 CPU seconds; the thirty-day limit is 3 CPU seconds.
Include imports, SQL reads, progress reads, pairing, bucket assignment, rendering, and file writes.
Exclude browser startup.
Report wall time and the one-minute load average beside CPU time.
High load can increase wall time without failing the CPU gate.
Report any shared index update separately; these trials made no index update calls.

The final run used a private SQLite backup of the live index.
The source connection used read-only mode.
The private copy contained 102,896 steps.
The backup took 1.618 seconds; private index installation took 0.193 seconds.
These preparation costs remain separate from chart generation.
The run removed the private copy after measurement.

| Window | Trial | CPU seconds | Wall seconds | Load, one minute |
|---|---:|---:|---:|---:|
| 7 days | 1 | 0.396515 | 0.469935 | 21.75 |
| 7 days | 2 | 0.373055 | 0.398744 | 21.75 |
| 7 days | 3 | 0.359104 | 0.381748 | 21.75 |
| 30 days | 1 | 1.045284 | 1.078356 | 21.75 |
| 30 days | 2 | 1.066737 | 1.098031 | 21.75 |
| 30 days | 3 | 1.031450 | 1.058192 | 21.75 |

All six trials pass the CPU gate.
Both window lengths use the same indexed query structure.
Window reads use `SEARCH steps USING INDEX steps_started`.
Context reads use `steps_session_previous`, `steps_execution_end`, and `steps_wait_started`.
Call reads use the primary key and `calls_copy`.
The plans scan small selected sets, not the full steps or calls history.
They retain a temporary sort for chronological order.

Earlier plans scanned full history for both window lengths.
Disk contention and cache state could make seven-day reads slower than thirty-day reads.
The old first-trial delay did not come from an index update.
The indexed reader removes those full-history scans.

The final gate passed 373 tests.
The type check reported 0 errors, 0 warnings, 0 notes.
Browser checks passed for light, dark, and narrow layouts.
Read each source once per chart set.
No timer, background collector, or persistent series cache is required.

## Approved decisions

1. Show the charts on the shared build page.
2. Use daily buckets with a seven-day default and a thirty-day option.
3. Keep start-based costs beside interval-based Mac idle.
4. Use the validated palette, shared controls, table views, and Auto theme selection.

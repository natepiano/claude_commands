# Delegate — phase report

**Usage:** `/plan:delegate_phase_report`

Type this when a phase finished and no report arrived, or when the report that
arrived was thin. It runs inside the current session and already knows the plan,
the phase, and what was committed. If no delegate run is active, say so in one
line and stop.

`/plan:delegate` reads this file after a completed phase. It defines
`<VerbosePostPhaseReport/>`, `<CombinedWindowReport/>`, and
`<RemainingWorkOutlook/>` in full. Never compose the report from memory of an
earlier read: the closing control line and the phase count are the parts that
go missing first.

Everything below is the contract.

---

<VerbosePostPhaseReport>
After a completed verbose phase outside an auto window, report only that phase
from its reviewed diff, accepted fixes, `As-built` block, and checkpoint. Inside
an active window, emit no per-phase report; when the window's last phase
completes, emit one <CombinedWindowReport/> instead of per-phase reports.

Single-phase report:

```
## Phase N complete — <title>

### Why this phase exists
[purpose and deliberate boundary]

### What now works
[reviewed behavior]

### Important types and APIs
| Type / trait / API | Status | Role | System relationship |
| --- | --- | --- | --- |

### Verification and review
[gate, meaningful tests/lint, review/fixes, smoke]

**Checkpoint:** `<short hash>`

### What remains
[phases remaining out of the plan total, then the auto-together recommendation]
```

Use the diff over planned claims. Include only load-bearing new/materially
changed types; use the same statuses as <PhaseBriefing/>, write every cell under
<TypeTableCells/>, and say when none exist.
Everything above `### What remains` describes only the completed phase.

<CombinedWindowReport>
One report covering every phase the window ran, built from the same sources as
the single-phase report. Keep it succinct and plain-spoken — ordinary sentences
a reader away from the details can follow — while still naming types, modules,
and crates exactly. Describe what the window built as one piece of work, not a
per-phase replay:

```
## Phases N–M complete — <window summary title>

### What now works
[combined reviewed behavior across the window]

### Important types and APIs
| Phase | Type / trait / API | Status | Role | System relationship |
| --- | --- | --- | --- | --- |

### Verification and review
[one combined summary; call out only per-phase results that differed]

**Checkpoints:** phase N `<short hash>`, …, phase M `<short hash>`

### What remains
[same content as the single-phase report]
```

Unify the types into that single table with its `Phase` column — never one
table per phase. Include only load-bearing new/materially changed types across
the whole window, using the <PhaseBriefing/> statuses and <TypeTableCells/>;
say when none exist.
<RemainingWorkOutlook/> applies to `### What remains` unchanged.
</CombinedWindowReport>

<RemainingWorkOutlook>
`### What remains` is the one place the report looks forward. It states what is
left and whether the next phases are safe to run without stopping between them.
It never briefs a phase and never asks for authorization; <VerbosePostPhaseGate/>
owns the ask.

**Count.** Read it from

`python3 ~/.claude/scripts/delegate/progress_history.py phase-count --plan-doc "<plan>"`

and report its `todo` and `total`. Never count phases by hand or by grep: a
heading takes three forms over its life and any pattern keyed on `status:`
silently ignores every shrunk phase, which is the mistake <ProgressContract/>
records. When `todo` is zero, say the plan is out of phases and that final
workspace verification runs next; skip the recommendation.

**Recommendation.** Read the Work Orders of the next todo phases in order, up to
three. A consecutive run is auto-together only while every phase in it:

- carries no unresolved `**Pending decision:**` block — that phase stops the run
  at its own pre-dispatch check whatever the window says;
- extends a subsystem shipped by a `done` phase rather than opening a new one;
- has an acceptance gate that runs unattended, with no smoke action needing the
  user;
- and does not depend on an outcome only knowable once an earlier phase in the
  run has actually landed.

The run ends at the first phase failing any of these. Name that phase and the
one reason it stops there. When the very next phase fails a test, recommend that
single phase; never round the window up to a phase you would then have to
interrupt. Cap the recommendation at three phases even when more would qualify,
so a batch briefing stays readable.

When the only criterion stopping a longer window is an unresolved
`**Pending decision:**` block on a covered phase, and the phases otherwise
cohere enough that running them together is clearly desirable, say so and offer
to surface those decision questions now: answering them here resolves the
blocks and lets the next auto control cover the full run. Before bringing a
decision forward, apply <DecisionEconomy/>: decide any with an obviously better
answer yourself and bring only true, substantive tradeoffs. Ask only the
blocking questions; the batch briefing still owns briefing the phases.

Write it as two or three sentences of ordinary English under <UserFacingText/> —
what the next phases do, why they group or do not — and always end with the
concrete control to type: `auto through phase X` with the actual phase number,
or `proceed` when only the single next phase qualifies. Never leave the number
for the user to work out. This closing control line is required in every
single-phase and combined-window report while todo phases remain. Do not emit a
table, a per-phase criteria checklist, or the criteria vocabulary above.
</RemainingWorkOutlook>
</VerbosePostPhaseReport>

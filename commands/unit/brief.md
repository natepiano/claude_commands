---
description: Show the next phase's briefing in a delegate run, or give one a gate skipped.
---

# Delegate — briefing

**Usage:** `/unit:brief`

Type this when a gate asked to start a phase without its briefing, or to see the
next phase's briefing now. It runs inside the current session and already knows
the plan and its phases. If no delegate run is active, say so in one line and
stop.

`/unit:delegate` reads this file at every pre-phase gate and auto control, and
before the types table of a phase report. It defines `<VerbosePrePhaseGate/>`,
`<BriefingFreshness/>`, `<PhaseBriefing/>`, `<TypeTableCells/>`,
`<CombinedWindowBriefing/>`, and `<AutoWindowBatchBriefing/>` in full. Never work
from memory of an earlier read.

Everything below is the contract.

---

<BriefingFreshness>
A phase is freshly briefed when the user received either its complete
<PhaseBriefing/> or the complete <CombinedWindowBriefing/> for an auto range
containing it in the current uninterrupted pre-phase review sequence, every
pending decision and user amendment was surfaced and resolved, and no later
edit changed its behavior, scope, files, or verification. Sequential individual
briefings count; they need not appear in one batch message. A follow-up question
or explanation does not stale a briefing. Recording an accepted decision that
the briefing and discussion already described does not stale it.

When an auto control arrives and every covered phase is freshly briefed, that
control is the batch approval: set the approved `AUTO_WINDOW` and proceed to
<CoordinateDelegatedPhaseReservation/> without repeating briefings or asking for
`proceed`. If any
covered phase is unbriefed, stale, or has an unresolved decision, route to
<AutoWindowBatchBriefing/>. Compressed rows and phase titles are not briefings.
</BriefingFreshness>

<PhaseBriefing>
Build only from Delegation Context, the Work Order, and command-line amendments:

```
## Phase N ready — <title>

### Why this phase exists
[purpose, dependencies, deliberate exclusions]

### Work to be done
[behavior, state transitions, ownership, visible effect]

### Important types and APIs this phase will introduce or change
| Type / trait / API | Status | Planned role | System relationship |
| --- | --- | --- | --- |

### Files and verification
[modules, acceptance gate, meaningful checks]

### Opening
[the Seats opening line verbatim, or "default — no Seats field"]
```

Rows include only types the Work Order explicitly names as part of its change. Status
is `New`, `Existing - Changes`, or `Existing - No Changes`, inferred from that
Work Order without code research. Where it leaves a status unclear, mark it
uncertain rather than inventing one; say explicitly when it names no such type. Write every cell under
<TypeTableCells/>.
</PhaseBriefing>

<TypeTableCells>
Governs the `Planned role` and `System relationship` cells in every types table
— phase briefing, window briefing, and completion report alike. The
`Type / trait / API` cell is the only place a code identifier belongs; the other
two are written for someone who has never opened the file and never will. Name
the thing in ordinary words — a tag, a list, a rule, the box around the members,
the step that copies it — and say what it does, or what breaks without it.

| Test every cell must pass | Rejected | Written |
| --- | --- | --- |
| **Say it out loud** to someone watching the running application | "Durable back-reference from an instance shell to the registered Look definition it was built from" | "A tag on each Look saying which of the seven it came from" |
| **Name the consequence**, not just the mechanism | "Copied onto duplicates by the new integration point" — copied by what, and what happens if it is not? | "Duplicating has to copy it deliberately, because the duplicator copies wiring but no tags" |

Three specific failures, each fluent English that informs nobody: a code
identifier used as a noun where an ordinary word exists — *the shell*, *the
definition*, *the publication*; a noun phrase compounded from three or more plan
terms, such as naming a transaction by the three steps it performs; and
plan-internal vocabulary the user has never been shown — gate ids, phase numbers
as adjectives, *promotion*, *staging*, *additive*, *erased*.

One sentence per cell is the target and two the ceiling, but length is not the
constraint — a cell needing a clause of context gets it. Terseness bought by
compressing plan terms into a noun phrase is the failure this contract exists to
stop.
</TypeTableCells>

<CombinedWindowBriefing>
For an auto window, build one high-level preview from Delegation Context, every
covered Work Order, and command-line amendments:

```
## Phases N–M ready — <one outcome-oriented title>

### Overview
[succinct human-readable explanation of what the window accomplishes as one
piece of work, why these phases belong together, and the deliberate boundary]

### Phase summaries
- **Phase N — <title>:** [succinct purpose, dependency, behavior, and deliberate
  exclusion]
- **Phase N+1 — <title>:** [...]

### Important types and APIs
| Phase | Type / trait / API | Status | Planned role | System relationship |
| --- | --- | --- | --- | --- |

### Files and verification
[one combined module/package and acceptance summary; name per-phase differences
only where they matter]

### Wrap-up
[succinct statement of the resulting capability, what remains outside this
window, and why the window can run without an intermediate stop]
```

Keep the overview and phase summaries behavioral and high-level; do not replay
each Work Order. The complete preview must still preserve every state
transition, ownership change, visible effect, dependency, and exclusion the
user needs to authorize the range.

Table rows include only types the applicable Work Order explicitly names as
part of its change. Use status `New`, `Existing - Changes`, or
`Existing - No Changes`, inferred from that Work Order without code research.
Order rows by editing sequence: covered phase order first, then the order each
type is first introduced or changed within that phase. Never alphabetize the
table or regroup it by crate. Where a Work Order leaves a status unclear, mark
it uncertain rather than inventing one; say explicitly when the window names no
such type. Write every cell
under <TypeTableCells/>.

Keep `### Wrap-up` short. It synthesizes the authorization boundary; it does not
repeat the overview, phase summaries, table, or verification section.
</CombinedWindowBriefing>

<VerbosePrePhaseGate>
When `MODE=verbose` and no approved auto window is active, run
<ReviewPendingAddOns/>, emit <PhaseBriefing/>, and ask exactly:

`Start Phase N? Reply \`proceed\` to run only this phase, \`auto next N phases\`, \`auto through phase X\`, or \`stop\`.`

Apply <AuthorizationContract/>. Questions preserve the gate; confusion invokes
<ExplainOnDemand/>. Opening an auto window applies <BriefingFreshness/>: a fully
fresh range is authorized by the auto control itself; otherwise route to
<AutoWindowBatchBriefing/> before dispatch.
</VerbosePrePhaseGate>

<AutoWindowBatchBriefing>
Resolve the covered todo phases and apply <BriefingFreshness/> first. If every
covered phase is fresh, set the approved `AUTO_WINDOW` and continue directly to
<CoordinateDelegatedPhaseReservation/> without another briefing or gate.
Otherwise run <ReviewPendingAddOns/>, read every Work Order
now, and emit one complete <CombinedWindowBriefing/> for the covered range;
surface any pending decision. Ask:

`Run phases <list> without stopping? Reply \`proceed\` to authorize all of them, \`proceed phase N\` to authorize only phase N and re-gate after it, or \`stop\`.`

Approval runs the resolved range without intermediate gates. Narrowing updates
`AUTO_WINDOW`; questions preserve the batch gate. The full combined preview,
not a phase-title list or type table alone, owns batch authorization.
</AutoWindowBatchBriefing>

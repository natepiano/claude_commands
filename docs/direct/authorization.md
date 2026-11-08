# User authorization

<AuthorizationContract>
Mode behavior:

| Mode | Before phase | After phase | Commit |
| --- | --- | --- | --- |
| `single` | no gate | end | none |
| `loop` | automatic | next phase | one checkpoint |
| `verbose` | <VerbosePrePhaseGate/> unless an approved auto window is active | report, then `continue` gate; a window defers both into one combined report at its close | one checkpoint |

Loop invocation authorizes its phase checkpoints. Verbose authorization occurs
only after the required briefing:

- `proceed` or `approved`: current phase only; trailing text amends its prompt.
- `auto next N phases`: positive N including the current phase.
- `auto through phase X`: current through a current-or-later todo phase X.
- `stop`: end without the described phase.
- Post-phase `continue`: show the next briefing only; it never authorizes work.
  An auto control at that gate is a normal window control and routes through
  <BriefingFreshness/> like any other.

An auto window removes intermediate stops, not explanations. Apply
<BriefingFreshness/> before its first dispatch. An auto control on initial
invocation still requires <AutoWindowBatchBriefing/> because no phase has been
briefed.

Before the first loop/verbose dispatch, stop on a dirty tree unless the selected
plan doc is the only dirty path; then include it in the first checkpoint.

Loop stops only for that dirty-tree guard, an unresolved current Pending
decision, a real design choice, reviews conflicting on intended behavior, a
required gate that cannot run, or worker/environment error. It never stops
for add-ons: <ConsiderNextItems/> accumulates them, and <ReviewPendingAddOns/>
walks them only at an interactive point — one of those stops once its decision
is resolved, a verbose gate outside a window, a user instruction that is more
than an authorization word, <RunSummary/>, or `/unit:add_ons`. The
findings ledger is not on this list and never joins it: a
convergence advisory is reported and the round runs. Everything else
auto-routes, resequences, or defers. Verbose adds only its authorization gates.
</AuthorizationContract>
